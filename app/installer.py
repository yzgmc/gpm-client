"""整合包安装器：解压 + 在线下载缺失的 mod 文件。

整合包安装流程（参考 PCL2 / HMCL / Modrinth App / CurseForge App）：
1. 解析 modpack 内部清单（manifest.json / modrinth.index.json）
   - Modrinth: modrinth.index.json，含 files[] 每条带完整 downloads url
   - CurseForge: manifest.json，files[] 仅含 projectID + fileID
2. 下载缺失的 mod 文件
   - Modrinth: 直接拉 files[].downloads[0]，按 path 放到 install_dir 对应子目录
   - CurseForge: 通过服务端 API /api/v1/curseforge/resolve 拿到下载 URL，再拉
3. 释放 overrides/ 目录（CFG / scripts / config / mods 等所有预设资源）

设计要点：
- 进度回调 (stage, detail, percent)，stage ∈ {"parse", "download", "extract", "done", "error"}
- 取消支持：threading.Event，下载前检查
- 错误：单文件失败可选继续（per-file 容错），整体失败抛 RuntimeError
- 不重复下载：已存在且 SHA1 校验通过的文件跳过
"""
from __future__ import annotations

import os
import shutil
import threading
import zipfile
from typing import Callable, Optional

from gpm_common import GameAdapterRegistry
from gpm_common.adapters.minecraft import MinecraftAdapter

# download_file 在 downloader.py，提供哈希校验 + 多线程分块 + 进度回调
# get_sync_client 是进程级共享 httpx.Client，所有 HTTP 调用复用同一连接池
# （避免 WinError 10048 端口耗尽）
from app.downloader import download_file, get_sync_client

import httpx  # 仅用于 httpx.HTTPError 捕获


# 进度回调签名: (stage, detail, percent)
#   stage ∈ {"parse", "download", "extract", "done", "error"}
ProgressCb = Callable[[str, str, int], None]


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

    try:
        with zipfile.ZipFile(archive_path) as zf:
            # ---- 1. 在线下载 mod 文件 ----
            downloaded = 0
            if kind == "modrinth" and plan.get("downloads"):
                downloads = plan["downloads"]
                total_n = len(downloads)
                # 预估总字节（Modrinth 提供 fileSize，否则按文件名长度占位）
                total_bytes = sum(
                    int(d.get("size") or 0) or 1024 for d in downloads
                ) or 1
                # 累计已下载字节（mod 完成时累加），用于把单文件 (d, t) 映射为
                # 全局 (cum_done + d) / total_bytes → 5-65% 进度
                cum_done_lock = threading.Lock()
                cum_done = 0

                summary["total_mods"] = total_n
                if progress:
                    progress("download", f"开始下载 {total_n} 个 mod（{total_bytes // 1024} KB）…", 5)
                for i, item in enumerate(downloads, 1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("已取消")
                    url = item.get("url") or ""
                    if not url:
                        continue
                    rel = item.get("path", "").lstrip("/").replace("/", os.sep)
                    if not rel:
                        continue
                    # zip slip 防御：即使 manifest 里写了 ../ 也只写到 install_dir 下
                    try:
                        dest = _safe_path_within(install_dir, rel)
                    except ValueError as e:
                        summary.setdefault("warnings", []).append(f"跳过非法路径 {rel}: {e}")
                        continue
                    os.makedirs(os.path.dirname(dest) or install_dir, exist_ok=True)
                    # 跳过已存在（用 size 粗略判定，避免重下）
                    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                        pct = 5 + int((cum_done + int(item.get("size") or 0)) * 60 / total_bytes)
                        if progress:
                            progress(
                                "download",
                                f"已存在 mod {i}/{total_n}（{os.path.basename(rel)}），跳过",
                                min(pct, 65),
                            )
                        with cum_done_lock:
                            cum_done += int(item.get("size") or 0) or 1024
                        continue
                    # 当前 mod 的预估大小（用于实时进度权重）
                    mod_size = int(item.get("size") or 0) or 1024

                    def _make_per_mod_progress(idx: int, total: int, mod_sz: int):
                        # 闭包：捕获 cum_done_lock + cum_done 在调用时的引用
                        last_reported_pct = [-1]

                        def _cb(d: int, t: int, _idx=idx, _total=total, _msz=mod_sz,
                                _last=last_reported_pct):
                            with cum_done_lock:
                                overall = cum_done + d
                            pct = 5 + int(overall * 60 / total_bytes)
                            pct = min(65, max(pct, 5 + int((_idx - 1) * 60 / _total)))
                            if pct != _last[0]:
                                _last[0] = pct
                                if progress:
                                    mod_pct = int(d * 100 / max(1, t))
                                    progress(
                                        "download",
                                        f"下载 mod {_idx}/{_total}（{mod_pct}%）",
                                        pct,
                                    )

                        return _cb

                    try:
                        download_file(
                            url,
                            dest,
                            expected_hash=item.get("sha1"),
                            expected_hash_alg="sha1",  # Modrinth 清单约定
                            cancel_event=cancel_event,
                            progress=_make_per_mod_progress(i, total_n, mod_size),
                        )
                        downloaded += 1
                        with cum_done_lock:
                            cum_done += mod_size
                    except Exception as e:  # noqa: BLE001
                        # 单个 mod 失败不阻断整合包安装
                        if "取消" in str(e) or "Cancelled" in str(e):
                            raise
                        summary.setdefault("warnings", []).append(
                            f"下载 {rel} 失败: {e}"
                        )
                        # 失败也按预估字节推进进度，避免卡住
                        with cum_done_lock:
                            cum_done += mod_size
                    # 当前 mod 完成后给个稳定 pct
                    pct = 5 + int(cum_done * 60 / total_bytes)
                    if progress:
                        progress("download", f"下载 mod {i}/{total_n}", min(pct, 65))

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
                cum_done = 0
                for i, item in enumerate(resolved, 1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("已取消")
                    url = item.get("url") or ""
                    file_name = item.get("fileName") or f"{item.get('projectID')}-{item.get('fileID')}.jar"
                    mod_size = int(item.get("size") or 0) or 1024
                    if not url:
                        summary.setdefault("warnings", []).append(
                            f"CF #{item.get('projectID')}/{item.get('fileID')} 无下载 URL"
                        )
                        with cum_done_lock:
                            cum_done += mod_size
                        continue
                    # CurseForge 整合包的 mod 全部进 mods/（不含 sub-path）
                    try:
                        dest = _safe_path_within(install_dir, os.path.join("mods", file_name))
                    except ValueError as e:
                        summary.setdefault("warnings", []).append(
                            f"CF 跳过非法路径 {file_name}: {e}"
                        )
                        with cum_done_lock:
                            cum_done += mod_size
                        continue
                    os.makedirs(os.path.dirname(dest) or install_dir, exist_ok=True)
                    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                        if progress:
                            pct = 5 + int(cum_done * 60 / total_bytes)
                            progress("download", f"已存在 mod {i}/{total_n}", min(pct, 65))
                        with cum_done_lock:
                            cum_done += mod_size
                        continue

                    def _make_cf_progress(idx: int, total: int, msz: int):
                        last_reported_pct = [-1]

                        def _cb(d: int, t: int, _idx=idx, _total=total, _msz=msz,
                                _last=last_reported_pct):
                            with cum_done_lock:
                                overall = cum_done + d
                            pct = 5 + int(overall * 60 / total_bytes)
                            pct = min(65, max(pct, 5 + int((_idx - 1) * 60 / _total)))
                            if pct != _last[0]:
                                _last[0] = pct
                                if progress:
                                    mod_pct = int(d * 100 / max(1, t))
                                    progress(
                                        "download",
                                        f"下载 mod {_idx}/{_total}（{mod_pct}%）",
                                        pct,
                                    )

                        return _cb

                    try:
                        download_file(
                            url,
                            dest,
                            expected_hash=item.get("sha1"),
                            expected_hash_alg="sha1",
                            cancel_event=cancel_event,
                            progress=_make_cf_progress(i, total_n, mod_size),
                        )
                        downloaded += 1
                        with cum_done_lock:
                            cum_done += mod_size
                    except Exception as e:  # noqa: BLE001
                        if "取消" in str(e) or "Cancelled" in str(e):
                            raise
                        summary.setdefault("warnings", []).append(
                            f"下载 {file_name} 失败: {e}"
                        )
                        with cum_done_lock:
                            cum_done += mod_size
                    pct = 5 + int(cum_done * 60 / total_bytes)
                    if progress:
                        progress("download", f"下载 mod {i}/{total_n}", min(pct, 65))

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
