"""整合包安装器：解压 + 在线下载缺失的 mod 文件。

整合包安装流程（参考 PCL2 / HMCL / Modrinth App / CurseForge App）：
1. 解析 modpack 内部清单（manifest.json / modrinth.index.json）
   - Modrinth: modrinth.index.json，含 files[] 每条带完整 downloads url
   - CurseForge: manifest.json，files[] 仅含 projectID + fileID
2. 下载缺失的 mod 文件
   - Modrinth: 直接拉 files[].downloads[0]，按 path 放到 install_dir 对应子目录
   - CurseForge: 通过服务端 API /api/v1/curseforge/resolve 拿到下载 URL，再拉
   - **fallback**：Modrinth manifest 给出 downloads[1..N] 作为备用 CDN，
     主 URL 失败时按顺序尝试，0.3s 退避；CF 没有等价列表，重试 3 次主 URL
3. 释放 overrides/ 目录（CFG / scripts / config / mods 等所有预设资源）

设计要点：
- 进度回调 (stage, detail, percent)，stage ∈ {"parse", "download", "extract", "done", "error"}
- 取消支持：threading.Event，下载前检查
- 错误：单文件失败可选继续（per-file 容错），整体失败抛 RuntimeError
- 不重复下载：已存在且 SHA1 校验通过的文件跳过
- **断点续传**：install_dir/.gpm-install.json sidecar 持久化已完成/失败 mod 列表；
  cancel + 重启后只下载缺失的 mod，已下载的零重传（避免上百个 mod 中途取消后
  必须全部重下的痛苦体验）。这是对 PCL/HMCL 行为的一比一复刻。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import zipfile
from typing import Callable, Optional

from gpm_common import GameAdapterRegistry
from gpm_common.adapters.minecraft import MinecraftAdapter

# download_file 在 downloader.py，提供哈希校验 + 多线程分块 + 进度回调
# get_sync_client 是进程级共享 httpx.Client，所有 HTTP 调用复用同一连接池
# （避免 WinError 10048 端口耗尽）
from app.downloader import download_file, get_sync_client, _hash_file

import httpx  # 仅用于 httpx.HTTPError 捕获


# 进度回调签名: (stage, detail, percent)
#   stage ∈ {"parse", "download", "extract", "done", "error"}
ProgressCb = Callable[[str, str, int], None]


# ============ install.json sidecar（断点续传）============
# 参考 PCL2 / HMCL 行为：取消后重启只下缺失的 mod。
# 写入位置：<install_dir>/.gpm-install.json
#
# Schema:
# {
#   "version": 1,
#   "modpack_zip": "<absolute zip path>",
#   "modpack_zip_mtime": <float>,  # 用于检测"用户重新导入同一个 zip"
#   "modpack_name": "...",
#   "modpack_version": "...",
#   "started_at": "<iso8601>",
#   "completed": {
#     "<rel_path>": {
#       "sha1": "abc...",  # 可选；存在则二次校验 dest
#       "size": 12345,
#       "completed_at": "<iso8601>"
#     }
#   },
#   "failed": [
#     {"path": "<rel_path>", "error": "...", "at": "<iso8601>"}
#   ],
#   "stage": "parse" | "download" | "extract" | "done"
# }
#
# 原子写：先写 .tmp 再 os.replace，避免中途崩溃损坏 sidecar。
# 线程安全：所有读 / 写都通过 _manifest_lock 串行化（多 worker 并发下）。
_MANIFEST_NAME = ".gpm-install.json"
_MANIFEST_VERSION = 1
_manifest_lock = threading.Lock()


def _manifest_path(install_dir: str) -> str:
    return os.path.join(install_dir, _MANIFEST_NAME)


def _now_iso() -> str:
    """当前时间 ISO 8601 字符串（带本地时区）。失败不抛。"""
    try:
        import datetime
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def _read_manifest(install_dir: str) -> dict:
    """读 sidecar。文件不存在或损坏返回空 dict。线程安全。"""
    with _manifest_lock:
        p = _manifest_path(install_dir)
        if not os.path.isfile(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != _MANIFEST_VERSION:
                return {}  # 旧版本/格式不符 → 视为空
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            return {}


def _write_manifest(install_dir: str, manifest: dict) -> None:
    """原子写 sidecar。失败不抛（best effort）。

    实现：
    1. 写 .tmp 到同目录（同盘才能 os.replace 原子）
    2. fsync 强制落盘
    3. os.replace → 原子覆盖
    """
    with _manifest_lock:
        p = _manifest_path(install_dir)
        try:
            os.makedirs(install_dir, exist_ok=True)
            # 同目录 tmp，避免 os.replace 跨盘失败
            fd, tmp = tempfile.mkstemp(
                prefix=".gpm-install.", suffix=".tmp", dir=install_dir,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp, p)
            except Exception:
                # 写失败时清理 tmp，避免污染目录
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            # sidecar 写失败不应阻塞主流程（用户仍能正常安装）
            pass


def _manifest_record_completed(install_dir: str, rel_path: str, sha1: str, size: int) -> None:
    """记一个 mod 已完成。同时从 failed 列表中移除（如果存在）。"""
    m = _read_manifest(install_dir)
    m.setdefault("completed", {})
    m["completed"][rel_path] = {
        "sha1": sha1,
        "size": size,
        "completed_at": _now_iso(),
    }
    if m.get("failed"):
        m["failed"] = [f for f in m["failed"] if f.get("path") != rel_path]
    _write_manifest(install_dir, m)


def _manifest_record_failed(install_dir: str, rel_path: str, error: str) -> None:
    """记一个 mod 失败（不抛错；非取消型失败）。"""
    m = _read_manifest(install_dir)
    failed = m.setdefault("failed", [])
    # 去重：同一 path 已有 → 更新 error + at
    failed = [f for f in failed if f.get("path") != rel_path]
    failed.append({"path": rel_path, "error": error[:500], "at": _now_iso()})
    m["failed"] = failed
    _write_manifest(install_dir, m)


def _manifest_set_stage(install_dir: str, stage: str, **extra) -> None:
    """更新 stage 字段。"""
    m = _read_manifest(install_dir)
    m["stage"] = stage
    for k, v in extra.items():
        m[k] = v
    _write_manifest(install_dir, m)


def _build_url_candidates(item: dict) -> list[str]:
    """构造下载 URL 候选列表：主 URL 优先 + manifest fallback_urls 兜底。

    Modrinth manifest 格式：
      "downloads": [
        "https://cdn.modrinth.com/data/.../file.jar",   # 主 CDN（edge 命中）
        "https://cdn-raw.modrinth.com/data/.../file.jar",  # 备用
      ]
    之前我们只用 downloads[0]，遇到主 CDN 限流/挂掉就 fail。
    现在按顺序尝试所有候选，全失败才计入 warnings。
    """
    urls: list[str] = []
    primary = item.get("url") or ""
    if primary:
        urls.append(primary)
    fallbacks = item.get("fallback_urls") or []
    if isinstance(fallbacks, list):
        for u in fallbacks:
            if u and u not in urls:
                urls.append(u)
    return urls


# ============ CurseForge 解析器（通过服务端 API）============
# 服务端持有 CF API key，客户端拿不到也不该拿。
# 这里仅作函数存在性检查，真实解析见 resolve_curseforge_files。

def _resolve_cf_via_server(
    cf_files: list[dict],
    server_url: str,
    token: str,
    cancel_event: Optional[threading.Event],
) -> list[dict]:
    """把 CurseForge file 列表 (projectID, fileID) 转成可下载 URL 列表。

    通过服务端 /api/v1/curseforge/resolve 批量解析，避免客户端持有 CF API key。
    服务端会缓存已解析的 file（CF rate limit 保护）。

    返回 list of {"projectID", "fileID", "url", "fileName", "size", "sha1"}
    失败抛 RuntimeError。
    """
    import json

    if not cf_files:
        return []
    if not server_url:
        raise RuntimeError("未配置服务端地址，无法解析 CurseForge 文件")
    # token 可选（解析端点通常不要求登录，但带 token 更稳）
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = {
        "files": [
            {"projectID": f["projectID"], "fileID": f["fileID"]}
            for f in cf_files
        ]
    }
    url = server_url.rstrip("/") + "/api/v1/curseforge/resolve"
    # 复用进程级共享 client，连接池不重新创建（避免 WinError 10048 端口耗尽）
    try:
        with get_sync_client() as c:
            r = c.post(url, json=body, headers=headers, timeout=30.0, follow_redirects=True)
    except httpx.HTTPError as e:
        raise RuntimeError(f"调用服务端 CurseForge 解析 API 失败：{e}") from e
    if r.status_code >= 400:
        raise RuntimeError(
            f"服务端 CurseForge 解析失败 (HTTP {r.status_code}): {r.text[:200]}"
        )
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"服务端返回非 JSON: {e}") from e
    files = data.get("files") or []
    # 校验：返回数量应与请求一致（服务端严格按入参顺序返回）
    if len(files) != len(cf_files):
        raise RuntimeError(
            f"服务端返回数量不符：请求 {len(cf_files)} 返回 {len(files)}"
        )
    return files


# ============ overrides 解压 ============

def _extract_overrides(zf: zipfile.ZipFile, install_dir: str) -> int:
    """解压 overrides/ 下的全部文件到 install_dir。返回写入文件数。

    PCL/HMCL 行为：overrides/ 解压时保留子目录结构（如 overrides/config/foo.txt
    → install_dir/config/foo.txt）。
    """
    count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if not (name.startswith("overrides/") or name.startswith("./overrides/")):
            continue
        # 去掉 overrides/ 前缀（兼容 ./overrides/）
        if name.startswith("./overrides/"):
            rel = name[len("./overrides/"):]
        else:
            rel = name[len("overrides/"):]
        if not rel:
            continue
        target = os.path.join(install_dir, rel)
        os.makedirs(os.path.dirname(target) or install_dir, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    return count


# ============ 整合包安装主流程 ============

def _safe_path_within(base_dir: str, candidate: str) -> str:
    """把 candidate 解析成绝对路径并断言它在 base_dir 之下（防 zip slip）。

    candidate 可以是绝对路径或相对路径；解析后必须以 base_dir 的绝对路径为前缀，
    否则抛 ValueError。这样即便整合包 manifest 里写了 ../../etc/passwd 也写不到
    安装目录之外。
    """
    base_abs = os.path.abspath(base_dir)
    cand_abs = os.path.abspath(os.path.join(base_abs, candidate))
    # 公共路径必须等于 base_abs 自身（不是子串前缀，避免 /a/foo vs /a/foobar 误判）
    try:
        common = os.path.commonpath([base_abs, cand_abs])
    except ValueError:
        # 不同盘符等异常情况
        raise ValueError(f"路径越界: {candidate}")
    if common != base_abs:
        raise ValueError(f"路径越界: {candidate}")
    return cand_abs


def install_modpack(
    archive_path: str,
    install_dir: str,
    *,
    server_url: str = "",
    token: str = "",
    progress: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> dict:
    """完整安装整合包：解析清单 → 下载 mod → 解压 overrides。

    Args:
        archive_path: 已下载到本地的整合包 zip 路径
        install_dir: 安装根目录（相当于 .minecraft）
        server_url: 服务端地址（用于解析 CurseForge file）
        token: 登录 token（可选）
        progress: 进度回调
        cancel_event: 取消事件

    Returns:
        dict: 安装摘要
        {
            "kind": "modrinth" | "curseforge" | "overrides_only" | "raw",
            "installed_files": 写入的文件总数,
            "downloaded_mods": 实际下载的 mod 数（缓存命中不计）,
            "total_mods": mod 文件总数,
            "meta": {name, version, game_version, mod_loader, mod_loader_version},
        }

    Raises:
        RuntimeError: 解析失败、下载失败、用户取消
    """
    if not os.path.isfile(archive_path):
        raise RuntimeError(f"整合包不存在: {archive_path}")
    if not zipfile.is_zipfile(archive_path):
        raise RuntimeError(f"不是有效的 zip 文件: {archive_path}")
    os.makedirs(install_dir, exist_ok=True)

    if progress:
        progress("parse", "解析整合包清单…", 0)

    adapter = MinecraftAdapter()
    plan = adapter.parse_modpack_files(archive_path)
    kind = plan.get("kind", "raw")
    meta = plan.get("meta") or {}

    summary = {
        "kind": kind,
        "installed_files": 0,
        "downloaded_mods": 0,
        "total_mods": 0,
        "meta": meta,
    }

    # ---- 读 sidecar：断点续传 ----
    # 如果之前安装成功（stage == "done"），且 zip 文件未变（mtime 匹配），
    # 且 plan 中所有 mod 都在 completed → 直接返回，避免重复下载。
    existing_manifest = _read_manifest(install_dir)
    try:
        zip_mtime = os.path.getmtime(archive_path)
    except OSError:
        zip_mtime = 0.0
    same_zip = (
        existing_manifest.get("modpack_zip") == os.path.abspath(archive_path)
        and abs(float(existing_manifest.get("modpack_zip_mtime") or 0) - zip_mtime) < 0.5
    )
    if not same_zip:
        # zip 变了 → 清空 completed / failed（用户重新导入）
        existing_manifest = {
            "version": _MANIFEST_VERSION,
            "modpack_zip": os.path.abspath(archive_path),
            "modpack_zip_mtime": zip_mtime,
            "modpack_name": meta.get("name", ""),
            "modpack_version": meta.get("version", ""),
            "started_at": _now_iso(),
            "completed": {},
            "failed": [],
            "stage": "parse",
        }
    # 写一次最新的 sidecar（确保 modpack_zip/mtime 字段更新）
    existing_manifest.setdefault("version", _MANIFEST_VERSION)
    existing_manifest["modpack_zip"] = os.path.abspath(archive_path)
    existing_manifest["modpack_zip_mtime"] = zip_mtime
    existing_manifest.setdefault("completed", {})
    existing_manifest.setdefault("failed", [])
    _write_manifest(install_dir, existing_manifest)

    # ---- 快速返回：上次安装已 done 且所有 mod 仍就位 ----
    # 校验：每个 mod 的 rel_path 都在 completed 且 dest 文件存在 + hash 匹配
    if existing_manifest.get("stage") == "done" and plan.get("downloads"):
        expected_paths: list[str] = []
        for it in plan["downloads"]:
            rel = it.get("path", "").lstrip("/").replace("/", os.sep)
            if rel:
                expected_paths.append(rel)
        completed_map = existing_manifest.get("completed") or {}
        if expected_paths and all(p in completed_map for p in expected_paths):
            # 二次确认 dest 还在 + hash 对得上（防止用户手动删了文件后 sidecar 还在）
            all_intact = True
            for it in plan["downloads"]:
                rel = it.get("path", "").lstrip("/").replace("/", os.sep)
                if not rel:
                    continue
                dest = _safe_path_within(install_dir, rel)
                if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
                    all_intact = False
                    break
                expected_hash = (it.get("sha1") or "").lower()
                recorded_hash = (completed_map.get(rel, {}).get("sha1") or "").lower()
                if expected_hash and recorded_hash and expected_hash != recorded_hash:
                    all_intact = False
                    break
            if all_intact:
                # 全部就位 → 直接返回，零下载
                summary["total_mods"] = len(expected_paths)
                summary["downloaded_mods"] = 0  # 全部 cache-hit
                if progress:
                    progress(
                        "done",
                        f"整合包已就绪（{len(expected_paths)}/{len(expected_paths)} mod，跳过下载）",
                        100,
                    )
                return summary

    try:
        with zipfile.ZipFile(archive_path) as zf:
            # ---- 1. 在线下载 mod 文件 ----
            # 并发配置：4 个 mod 同时下载。PCL/HMCL 默认 3-5，过高会撞 Clash 代理
            # TIME_WAIT 和服务端 rate limit。4 是经验稳态值。
            from concurrent.futures import ThreadPoolExecutor, as_completed
            MAX_PARALLEL = 4

            def _download_mod_worker(
                idx: int,
                item: dict,
                total: int,
                total_bytes: int,
                install_dir: str,
                cum_done_lock: threading.Lock,
                cum_done_ref: list,
                progress_cb,
                cancel_evt,
                summary_dict: dict,
                kind: str,
            ) -> int:
                """下载单个 mod，**不**抛非取消异常。返回累计字节增量。

                cum_done_ref 是单元素 list（可变引用）—— 闭包内多个 worker
                通过锁共享同一计数器，下载完一个 mod 时累加。

                kind: "modrinth" | "curseforge" — 影响目标路径计算。
                """
                url_candidates = _build_url_candidates(item)
                if not url_candidates:
                    legacy = item.get("url")
                    if legacy:
                        url_candidates = [legacy]
                if not url_candidates:
                    return 0
                url = url_candidates[0]  # 用于 path 计算（Modrinth 才有 sub-path）
                mod_size = int(item.get("size") or 0) or 1024

                # 1) 计算目标路径
                if kind == "curseforge":
                    file_name = (
                        item.get("fileName")
                        or f"{item.get('projectID')}-{item.get('fileID')}.jar"
                    )
                    rel = file_name
                else:
                    rel = item.get("path", "").lstrip("/").replace("/", os.sep)
                if not rel:
                    return 0
                try:
                    if kind == "curseforge":
                        dest = _safe_path_within(
                            install_dir, os.path.join("mods", rel)
                        )
                    else:
                        dest = _safe_path_within(install_dir, rel)
                except ValueError as e:
                    with cum_done_lock:
                        summary_dict.setdefault("warnings", []).append(
                            f"跳过非法路径 {rel}: {e}"
                        )
                    return mod_size  # 占位累加

                os.makedirs(os.path.dirname(dest) or install_dir, exist_ok=True)

                # 2) Manifest 命中（**F.3 续传核心**）
                # 如果 sidecar 已记这个 mod 完成（上次 install_modpack 中途 cancel，
                # 但该 mod 已经下完写入 dest），重启后直接跳过。
                # 二次校验：dest 文件存在 + size 一致 + hash 匹配
                manifest_completed = (existing_manifest.get("completed") or {}).get(rel)
                if manifest_completed:
                    recorded_size = int(manifest_completed.get("size") or 0)
                    recorded_hash = (manifest_completed.get("sha1") or "").lower()
                    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                        if recorded_size and os.path.getsize(dest) != recorded_size:
                            pass  # size 不一致 → 重新走下面 hash 校验流程
                        elif recorded_hash:
                            actual_hash = _hash_file(dest, "sha1").lower()
                            if actual_hash == recorded_hash:
                                with cum_done_lock:
                                    cum_done_ref[0] += mod_size
                                return mod_size  # manifest 命中，跳过下载
                        elif not recorded_hash and recorded_size and os.path.getsize(dest) == recorded_size:
                            with cum_done_lock:
                                cum_done_ref[0] += mod_size
                            return mod_size

                # 3) 命中缓存（**先验 hash**，防止下载过但被破坏的文件被永久跳过）
                # H 改动后：download_file 内部自带 .part + .meta 续传，所以即使
                # dest 损坏 / 部分下载，都会在后续 download_file 阶段自动重试，
                # 而不会让"损坏但有 size"的缓存文件蒙混过关。
                expected_hash = (item.get("sha1") or "").lower()
                if expected_hash and os.path.isfile(dest) and os.path.getsize(dest) > 0:
                    actual_hash = _hash_file(dest, "sha1").lower()
                    if actual_hash == expected_hash:
                        # 补一次 manifest 记录（首次 install 命中 hash 但 sidecar
                        # 还没写的情况；幂等）
                        _manifest_record_completed(
                            install_dir, rel, actual_hash, os.path.getsize(dest),
                        )
                        with cum_done_lock:
                            cum_done_ref[0] += mod_size
                        return mod_size
                    # hash 不匹配 → 当 cache miss，让 download_file 走正常下载流程
                    # （包括 H 提供的 .part 续传能力；os.replace 会原子覆盖损坏的 dest）
                elif os.path.isfile(dest) and os.path.getsize(dest) > 0:
                    # 无预期 hash → 信任现有文件（无法验证）
                    _manifest_record_completed(
                        install_dir, rel, "", os.path.getsize(dest),
                    )
                    with cum_done_lock:
                        cum_done_ref[0] += mod_size
                    return mod_size

                # 3) 单 mod 进度回调：包装成全局累计字节
                last_pct = [-1]

                def _per_mod_cb(d: int, t: int) -> None:
                    with cum_done_lock:
                        overall = cum_done_ref[0] + d
                    pct = 5 + int(overall * 60 / total_bytes)
                    pct = min(65, max(pct, 5 + int((idx - 1) * 60 / total)))
                    if pct != last_pct[0]:
                        last_pct[0] = pct
                        if progress_cb:
                            mod_pct = int(d * 100 / max(1, t))
                            progress_cb(
                                "download",
                                f"下载 mod {idx}/{total}（{mod_pct}%）",
                                pct,
                            )

                # 4) 实际下载：尝试主 URL + 所有 manifest fallback_urls
                # Modrinth manifest 的 files[].downloads 数组里，index 0 是主 CDN，
                # index 1+ 是备用（modrinth 实际可能给 cdn / cdn-raw / maven. 二选一）。
                # CurseForge 的 cf_files 没有等价 fallback，所以 candidates 只有
                # 服务端 resolve 给的 url，重试靠 download_file 内部的 3 次重试。
                url_candidates = _build_url_candidates(item)
                if not url_candidates:
                    # 兼容旧格式：fallback_urls 不存在或为空时只用一个 url
                    legacy = item.get("url")
                    if legacy:
                        url_candidates = [legacy]
                last_err: Optional[Exception] = None
                for attempt_idx, try_url in enumerate(url_candidates, 1):
                    try:
                        download_file(
                            try_url,
                            dest,
                            expected_hash=item.get("sha1"),
                            expected_hash_alg="sha1",  # Modrinth/CF 清单约定
                            cancel_event=cancel_evt,
                            progress=_per_mod_cb,
                        )
                        # 写 sidecar：记录本 mod 已完成（即使后续 cancel 也不影响这个）
                        try:
                            actual_sha1 = ""
                            if item.get("sha1"):
                                actual_sha1 = _hash_file(dest, "sha1").lower()
                            _manifest_record_completed(
                                install_dir, rel,
                                actual_sha1 or (item.get("sha1") or "").lower(),
                                os.path.getsize(dest),
                            )
                        except OSError:
                            pass
                        with cum_done_lock:
                            cum_done_ref[0] += mod_size
                        return mod_size
                    except Exception as e:  # noqa: BLE001
                        # 取消必须 raise（让外层 as_completed 终止）
                        if "取消" in str(e) or "Cancelled" in str(e):
                            raise
                        last_err = e
                        # 还有下一个候选就退避后重试
                        if attempt_idx < len(url_candidates):
                            time.sleep(0.3 * attempt_idx)  # 0.3s, 0.6s, 0.9s
                            continue
                        # 所有候选失败：记 warning
                        break
                # 全部候选失败：记 sidecar 失败 + 警告
                try:
                    _manifest_record_failed(install_dir, rel, str(last_err)[:200])
                except OSError:
                    pass
                with cum_done_lock:
                    summary_dict.setdefault("warnings", []).append(
                        f"下载 {rel} 失败（尝试 {len(url_candidates)} 个 URL）: {last_err}"
                    )
                    cum_done_ref[0] += mod_size
                return mod_size

            downloaded = 0
            if kind == "modrinth" and plan.get("downloads"):
                downloads = plan["downloads"]
                total_n = len(downloads)
                total_bytes = sum(
                    int(d.get("size") or 0) or 1024 for d in downloads
                ) or 1
                cum_done_lock = threading.Lock()
                cum_done_ref = [0]  # 闭包共享的可变引用

                summary["total_mods"] = total_n
                if progress:
                    progress("download", f"开始下载 {total_n} 个 mod（{total_bytes // 1024} KB）…", 5)

                # 并发下推 4 个 worker。
                # - 闭包 + lock 保证 cum_done_ref[0] 安全累加
                # - as_completed 顺序完成，先完先记 cum_done
                # - cancel_event.set() 时所有 worker 内 download_file 在下个分块
                #   检测到取消抛异常，外层捕获后 cancel 其他 future
                with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
                    futures = {
                        ex.submit(
                            _download_mod_worker,
                            i, item, total_n, total_bytes, install_dir,
                            cum_done_lock, cum_done_ref, progress, cancel_event,
                            summary, "modrinth",
                        ): i
                        for i, item in enumerate(downloads, 1)
                        if item.get("url")
                    }
                    for fut in as_completed(futures):
                        if cancel_event is not None and cancel_event.is_set():
                            # 取消其余 worker
                            for f in futures:
                                f.cancel()
                            raise RuntimeError("已取消")
                        try:
                            fut.result()  # 取消时此处会抛 RuntimeError
                            downloaded += 1
                        except RuntimeError as e:
                            if "取消" in str(e) or "Cancelled" in str(e):
                                for f in futures:
                                    f.cancel()
                                raise
                            # 其它异常（已记 warnings）忽略
                pct = 5 + int(cum_done_ref[0] * 60 / total_bytes)
                if progress:
                    progress("download", f"下载 mod 完成（{downloaded}/{total_n}）", min(pct, 65))

            elif kind == "curseforge" and plan.get("cf_files"):
                cf_files = plan["cf_files"]
                total_n = len(cf_files)
                summary["total_mods"] = total_n
                if progress:
                    progress("download", f"开始解析 {total_n} 个 CurseForge mod…", 5)
                if not server_url:
                    raise RuntimeError(
                        "CurseForge 整合包需要服务端支持（持有 CF API key），请配置服务端地址"
                    )
                resolved = _resolve_cf_via_server(
                    cf_files, server_url, token, cancel_event
                )
                total_bytes = sum(
                    int(r.get("size") or 0) or 1024 for r in resolved
                ) or 1
                cum_done_lock = threading.Lock()
                cum_done_ref = [0]
                with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
                    futures = {
                        ex.submit(
                            _download_mod_worker,
                            i, item, total_n, total_bytes, install_dir,
                            cum_done_lock, cum_done_ref, progress, cancel_event,
                            summary, "curseforge",
                        ): i
                        for i, item in enumerate(resolved, 1)
                        if item.get("url")
                    }
                    for fut in as_completed(futures):
                        if cancel_event is not None and cancel_event.is_set():
                            for f in futures:
                                f.cancel()
                            raise RuntimeError("已取消")
                        try:
                            fut.result()
                            downloaded += 1
                        except RuntimeError as e:
                            if "取消" in str(e) or "Cancelled" in str(e):
                                for f in futures:
                                    f.cancel()
                                raise
                pct = 5 + int(cum_done_ref[0] * 60 / total_bytes)
                if progress:
                    progress("download", f"下载 mod 完成（{downloaded}/{total_n}）", min(pct, 65))

            summary["downloaded_mods"] = downloaded

            # ---- 2. 释放 overrides/ ----
            if progress:
                progress("extract", "解压 overrides/ 目录…", 70)
            n_overrides = _extract_overrides(zf, install_dir)
            summary["installed_files"] += n_overrides

            # ---- 3. 纯 overrides/ 或裸 zip（无 manifest）时也走一遍 zf.extractall ----
            # 兼容 HMCL 风格：overrides/ 之外的其它文件（如根目录的 *.txt）直接释放
            if kind in ("overrides_only", "raw"):
                names = zf.namelist()
                # 跳过已通过 overrides/ 处理过的，再跳过 manifest 文件
                skip_prefixes = ("overrides/", "./overrides/")
                skip_files = {
                    "manifest.json", "./manifest.json",
                    "modrinth.index.json", "./modrinth.index.json",
                }
                count = 0
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if any(name.startswith(p) for p in skip_prefixes):
                        continue
                    if name in skip_files:
                        continue
                    rel = name.lstrip("./")
                    if not rel:
                        continue
                    # zip slip 防御
                    try:
                        target = _safe_path_within(install_dir, rel.replace("/", os.sep))
                    except ValueError:
                        continue
                    if not os.path.exists(target):  # 已存在不覆盖
                        os.makedirs(os.path.dirname(target) or install_dir, exist_ok=True)
                        with zf.open(info) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        count += 1
                summary["installed_files"] += count

    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"整合包安装失败: {e}") from e

    # ---- 标记安装完成 ----
    # 即使有部分 mod 失败（warnings），stage 仍标 "done_partial"；只有全部成功才标 "done"。
    final_stage = (
        "done" if not summary.get("warnings") else "done_partial"
    )
    _manifest_set_stage(
        install_dir, final_stage,
        finished_at=_now_iso(),
    )

    if progress:
        progress(
            "done",
            f"整合包安装完成（{summary['downloaded_mods']}/{summary['total_mods']} mod, "
            f"{summary['installed_files']} 文件）",
            100,
        )
    return summary


# ============ 单 mod 安装（用户"另存为"流程） ============


def install_mod(file_path: str, modpack_meta: dict, install_base_dir: str, target_dir: str | None = None) -> str:
    """将单个模组 jar 复制到目标目录。

    - target_dir 给定时：直接复制到该目录（用户"另存为"选择的文件夹）。
    - target_dir 为 None：复制到 modpack_meta 对应整合包的 mods/ 目录。
    返回目标路径。
    """
    import shutil

    if target_dir:
        mods_dir = target_dir
    else:
        game = modpack_meta.get("game", "minecraft")
        adapter = GameAdapterRegistry.get(game)
        if adapter:
            modpack_dir = adapter.install_dir_hint(install_base_dir, modpack_meta)
        else:
            modpack_dir = os.path.join(install_base_dir, game, modpack_meta.get("name", "default"))
        mods_dir = os.path.join(modpack_dir, "mods")
    os.makedirs(mods_dir, exist_ok=True)
    dest = os.path.join(mods_dir, os.path.basename(file_path))
    shutil.copy2(file_path, dest)
    return dest
