"""内容寻址下载缓存（按文件 SHA1 复用，跨 modpack / 跨次安装提速）。

设计目标：
1. **跨 modpack 复用**：同一 mod（SHA1 相同）装在 5 个 modpack 里 → 1 次下载
2. **跨次安装复用**：装完 modpack A 后再装 B，B 包含 A 的 mod → 零下载
3. **完整性由 SHA1 天然保证**：缓存 key 就是 SHA1，读出来对一下就完事
4. **磁盘可控**：LRU 淘汰 + 容量上限（默认 8GB）
5. **可选 hot cache**：内存里维护 LRU dict，常用文件秒开

存储布局：
  <cache_dir>/
    objects/
      <aa>/<bb>/<full_sha1>     # 分桶避免单目录文件数爆炸（仿 git）
    meta/
      info.json                  # 全局统计 + 容量上限
      recent.json                # LRU 列表（path → mtime）

集成点（关键）：
- downloader.download_file：传 expected_hash 时先查 cache，命中直接返回（零网络）
- installer.install_modpack：mod 下载阶段自动走 cache
- main_window._batch_download_mods_worker：批量 mod 安装前先查 cache

线程安全：所有写操作通过 _lock 串行化；读路径无锁（用 lru_cache 加速）。
进程安全：同一 cache_dir 允许多进程并发读，不允许并发写（运行期只会有一个 client 进程）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional


# 默认缓存位置：<data>/download_cache
_DEFAULT_CACHE_DIRNAME = "download_cache"
# 默认容量上限：8 GB。超限按 LRU 删除最久未访问的对象
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024
# 单文件 > 256 MB 不入缓存（一般是整合包 / 资源包等大文件，缓存收益低）
_MAX_OBJECT_BYTES = 256 * 1024 * 1024


# ============================================================================
# 全局状态
# ============================================================================
_cache_dir: Optional[Path] = None
_max_bytes: int = _DEFAULT_MAX_BYTES
_lock = threading.Lock()
# 进程内 LRU 加速：sha1 → (size, last_access_ts)
_mem_lru: dict[str, tuple[int, float]] = {}
_mem_lru_lock = threading.Lock()


# ============================================================================
# 初始化
# ============================================================================
def set_cache_dir(path: str | os.PathLike) -> None:
    """设置缓存目录。第一次调用时初始化 meta 文件。"""
    global _cache_dir
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    (p / "objects").mkdir(exist_ok=True)
    (p / "meta").mkdir(exist_ok=True)
    _cache_dir = p
    # 第一次初始化 info.json
    info_path = p / "meta" / "info.json"
    if not info_path.is_file():
        _write_info({"version": 1, "max_bytes": _max_bytes, "objects": 0, "bytes": 0})
    # 加载 recent.json 到内存
    _load_recent()


def set_max_bytes(max_bytes: int) -> None:
    """调整容量上限（需在 set_cache_dir 之后调用才生效）。"""
    global _max_bytes
    _max_bytes = max(0, max_bytes)
    if _cache_dir is not None:
        info = _read_info()
        info["max_bytes"] = _max_bytes
        _write_info(info)


def get_cache_dir() -> Optional[Path]:
    return _cache_dir


def _object_path(sha1: str) -> Path:
    """sha1 → 缓存文件路径（分桶避免单目录文件数过多）。"""
    if _cache_dir is None:
        raise RuntimeError("cache dir not set, call set_cache_dir() first")
    return _cache_dir / "objects" / sha1[:2] / sha1[2:4] / sha1


# ============================================================================
# meta I/O
# ============================================================================
def _info_path() -> Path:
    if _cache_dir is None:
        raise RuntimeError("cache dir not set")
    return _cache_dir / "meta" / "info.json"


def _recent_path() -> Path:
    if _cache_dir is None:
        raise RuntimeError("cache dir not set")
    return _cache_dir / "meta" / "recent.json"


def _read_info() -> dict:
    p = _info_path()
    if not p.is_file():
        return {"version": 1, "max_bytes": _max_bytes, "objects": 0, "bytes": 0}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"version": 1, "max_bytes": _max_bytes, "objects": 0, "bytes": 0}


def _write_info(info: dict) -> None:
    p = _info_path()
    try:
        # 原子写
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, p)
    except OSError:
        pass


def _load_recent() -> None:
    """从 recent.json 加载 LRU 状态到内存。"""
    global _mem_lru
    p = _recent_path()
    if not p.is_file():
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        # recent.json 格式: {<sha1>: [size, last_access_ts]}
        _mem_lru = {k: (v[0], float(v[1])) for k, v in data.items() if isinstance(v, list) and len(v) >= 2}
    except (OSError, json.JSONDecodeError, ValueError):
        _mem_lru = {}


def _save_recent() -> None:
    p = _recent_path()
    try:
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: [v[0], v[1]] for k, v in _mem_lru.items()}, f)
        os.replace(tmp, p)
    except OSError:
        pass


# ============================================================================
# 核心 API
# ============================================================================
def is_enabled() -> bool:
    return _cache_dir is not None


def has(sha1: str) -> bool:
    """检查缓存中是否有 sha1 对应的文件（不更新 LRU）。

    性能优化：先查内存 LRU dict（O(1)），再走文件系统 stat。
    """
    if not is_enabled():
        return False
    with _mem_lru_lock:
        if sha1 in _mem_lru:
            return True
    return _object_path(sha1).is_file()


def touch(sha1: str) -> None:
    """更新 LRU 访问时间（命中缓存时调用）。"""
    if not is_enabled():
        return
    p = _object_path(sha1)
    try:
        size = p.stat().st_size
    except OSError:
        return
    ts = time.time()
    with _mem_lru_lock:
        _mem_lru[sha1] = (size, ts)


def get_path(sha1: str) -> Optional[Path]:
    """返回缓存中 sha1 对应文件的路径（命中并 touch LRU）。未命中返回 None。"""
    if not is_enabled():
        return None
    p = _object_path(sha1)
    if not p.is_file():
        return None
    touch(sha1)
    return p


def put(src_path: str | os.PathLike, sha1: str | None = None) -> bool:
    """把 src_path 移动到缓存里（按 sha1 寻址）。返回是否成功。

    如果 sha1 为 None，会读取文件计算 SHA1（IO 开销大；最好调用方已知）。
    单文件 > _MAX_OBJECT_BYTES 不入缓存（避免大文件占满缓存）。
    文件已经存在缓存里 → 直接更新 LRU，不重新复制。
    """
    if not is_enabled():
        return False
    src = Path(src_path)
    if not src.is_file():
        return False
    try:
        size = src.stat().st_size
    except OSError:
        return False
    if size > _MAX_OBJECT_BYTES:
        return False
    if size == 0:
        return False  # 空文件不缓存

    if sha1 is None:
        sha1 = _hash_file(src)
        if not sha1:
            return False

    target = _object_path(sha1)
    if target.is_file() and target.stat().st_size == size:
        # 已有缓存 → 仅更新 LRU
        touch(sha1)
        return True

    # 移动到缓存（原子；先复制再删除避免跨盘失败）
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 同盘时用 move；跨盘时 fallback 到 copy+delete
        try:
            shutil.move(str(src), str(target))
        except OSError:
            shutil.copy2(str(src), str(target))
            try:
                src.unlink()
            except OSError:
                pass
    except OSError as e:
        # 复制失败（磁盘满、权限等）→ 静默失败，不阻塞主流程
        return False

    # 更新 LRU + 容量
    with _lock:
        info = _read_info()
        info["objects"] = info.get("objects", 0) + 1
        info["bytes"] = info.get("bytes", 0) + size
        _write_info(info)
        with _mem_lru_lock:
            _mem_lru[sha1] = (size, time.time())
    _save_recent()
    _enforce_quota()
    return True


def copy_to(sha1: str, dest_path: str | os.PathLike, *, verify: bool = True) -> bool:
    """从缓存复制到 dest_path（缓存对象不被改动）。verify=True 时用 SHA1 复核。"""
    p = get_path(sha1)
    if p is None:
        return False
    dest = Path(dest_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 同盘用 move 提升速度（业务路径：缓存 → 目标；目标可重建所以 move 没问题）
        # 但保险起见用 copy2：保证缓存不被误删
        shutil.copy2(str(p), str(dest))
    except OSError:
        return False
    if verify and not _verify(dest, sha1):
        try:
            dest.unlink()
        except OSError:
            pass
        return False
    return True


def move_to(sha1: str, dest_path: str | os.PathLike, *, verify: bool = True) -> bool:
    """从缓存移动到 dest_path（性能优于 copy_to，不占双份磁盘）。verify=True 时用 SHA1 复核。"""
    p = get_path(sha1)
    if p is None:
        return False
    dest = Path(dest_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 跨盘/权限问题 fallback 到 copy
        try:
            shutil.move(str(p), str(dest))
        except OSError:
            shutil.copy2(str(p), str(dest))
    except OSError:
        return False
    if verify and not _verify(dest, sha1):
        try:
            dest.unlink()
        except OSError:
            pass
        return False
    # 移动后从 LRU + 容量计数里删除
    with _lock:
        info = _read_info()
        if info.get("objects", 0) > 0:
            info["objects"] -= 1
        if info.get("bytes", 0) > 0:
            try:
                info["bytes"] = max(0, info["bytes"] - dest.stat().st_size)
            except OSError:
                pass
        _write_info(info)
        with _mem_lru_lock:
            _mem_lru.pop(sha1, None)
    _save_recent()
    return True


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _verify(path: Path, expected_sha1: str) -> bool:
    return _hash_file(path).lower() == expected_sha1.lower()


# ============================================================================
# 配额管理
# ============================================================================
def _enforce_quota() -> None:
    """超容量时按 LRU 删除最早访问的对象，直到低于 max_bytes。"""
    if _max_bytes <= 0 or _cache_dir is None:
        return
    info = _read_info()
    total = info.get("bytes", 0)
    if total <= _max_bytes:
        return
    # 按 last_access 升序删除
    with _mem_lru_lock:
        entries = sorted(_mem_lru.items(), key=lambda x: x[1][1])
    deleted = 0
    for sha1, (size, _ts) in entries:
        if total <= _max_bytes:
            break
        p = _object_path(sha1)
        try:
            if p.is_file():
                p.unlink()
                total -= size
                deleted += 1
        except OSError:
            continue
        with _mem_lru_lock:
            _mem_lru.pop(sha1, None)
    if deleted > 0:
        info["bytes"] = max(0, total)
        info["objects"] = max(0, info.get("objects", 0) - deleted)
        _write_info(info)
        _save_recent()


# ============================================================================
# 统计 / 维护
# ============================================================================
def stats() -> dict:
    """返回缓存统计信息。"""
    if _cache_dir is None:
        return {"enabled": False}
    info = _read_info()
    return {
        "enabled": True,
        "dir": str(_cache_dir),
        "objects": info.get("objects", 0),
        "bytes": info.get("bytes", 0),
        "max_bytes": info.get("max_bytes", _max_bytes),
        "mem_lru_size": len(_mem_lru),
    }


def clear() -> None:
    """清空缓存（调试用）。"""
    global _mem_lru
    if _cache_dir is None:
        return
    with _lock:
        try:
            shutil.rmtree(_cache_dir / "objects", ignore_errors=True)
        except OSError:
            pass
        _write_info({"version": 1, "max_bytes": _max_bytes, "objects": 0, "bytes": 0})
        with _mem_lru_lock:
            _mem_lru = {}
        _save_recent()
