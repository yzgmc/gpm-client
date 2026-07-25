"""Vanilla Minecraft 版本安装：版本 JSON + client jar + libraries + assets。

Fabric/Quilt/Forge/NeoForge 安装器只安装加载器本身，不下载原版游戏文件。
启动时 Fabric 的 game provider 需要在 classpath 上找到原版 client jar，
否则报 "Minecraft game provider couldn't locate the game!"。

本模块负责下载完整的原版 MC 运行时（client jar + 依赖库 + 资源文件），
使加载器能正常找到并启动游戏。

多源兜底：每个文件优先走 BMCLAPI 国内镜像（bangbang93.com），若镜像不可达
或返回 404/超时，自动回退到 Mojang 官方源，保证可下载性。
所有文件用 SHA1 校验完整性，已存在且校验通过的文件自动跳过（幂等）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import httpx


# 进度回调签名: (stage, detail, percent)
#   stage ∈ {"download", "done", "error"}
ProgressCb = Callable[[str, str, int], None]

# 国内镜像（BMCLAPI），官方源国内下载极慢
# BMCLAPI 完整兼容 Mojang 官方 API 路径，仅替换域名
_BMCLAPI_ROOT = "https://bmclapi2.bangbang93.com"
_MANIFEST_URL = "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json"
_LIBRARIES_BASE = "https://bmclapi2.bangbang93.com/maven/"
_ASSETS_BASE = "https://bmclapi2.bangbang93.com/assets/"

# 官方源（兜底）：BMCLAPI 不可达或返回 404 时自动回退，保证可下载性
_OFFICIAL_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
_OFFICIAL_LIB_BASE = "https://libraries.minecraft.net/"
_OFFICIAL_ASSETS_BASE = "https://resources.download.minecraft.net/"
# Mojang 元数据域名（版本 JSON / client jar / asset index 可能用其中任一）
_MOJANG_DOMAINS = (
    "https://piston-meta.mojang.com",
    "https://piston-data.mojang.com",
    "https://launcher.mojang.com",
    "https://launchermeta.mojang.com",
)

_TIMEOUT = httpx.Timeout(15.0, read=120.0)
# 资源/库文件并发下载数（小文件多，适当提高并发）
_MAX_WORKERS = 16


def _sha1_file(path: str) -> str:
    """计算文件 SHA1。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bmcl_mirror(original_url: str) -> str:
    """把 Mojang 官方 URL 替换为 BMCLAPI 镜像 URL。

    - libraries.minecraft.net → BMCLAPI maven
    - piston-*.mojang.com / launcher.mojang.com / launchermeta.mojang.com → BMCLAPI 根
    其它 URL 原样返回（无法镜像）。
    """
    if original_url.startswith("https://libraries.minecraft.net/"):
        return original_url.replace("https://libraries.minecraft.net/", _LIBRARIES_BASE)
    for domain in _MOJANG_DOMAINS:
        if original_url.startswith(domain):
            return _BMCLAPI_ROOT + original_url[len(domain):]
    return original_url


def _mirror_urls(original_url: str) -> list[str]:
    """根据官方 URL 构建镜像 URL 列表：BMCLAPI 优先，官方源兜底（去重保序）。"""
    bmcl = _bmcl_mirror(original_url)
    urls: list[str] = []
    if bmcl and bmcl != original_url:
        urls.append(bmcl)
    if original_url:
        urls.append(original_url)
    return urls


def _download_one(
    urls: list[str],
    dest: str,
    expected_sha1: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """下载单个文件到 dest，SHA1 校验。已存在且校验通过则跳过。

    多源兜底：按 urls 顺序逐个尝试，任一成功即返回；
    取消则抛 RuntimeError("已取消")；全部失败抛最后一个异常。
    """
    # 已存在且校验通过 → 跳过（幂等）
    if os.path.isfile(dest):
        if expected_sha1:
            if _sha1_file(dest).lower() == expected_sha1.lower():
                return
        else:
            return  # 无校验值，存在即跳过

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    last_err: Exception | None = None
    for url in urls:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("已取消")
        try:
            with httpx.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("已取消")
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("已取消")
                        f.write(chunk)
            if expected_sha1:
                actual = _sha1_file(tmp)
                if actual.lower() != expected_sha1.lower():
                    raise ValueError(f"SHA1 校验失败: {os.path.basename(dest)}")
            os.replace(tmp, dest)
            return  # 成功
        except RuntimeError as e:
            # 取消异常：立即抛出，不再尝试其它源
            if "取消" in str(e):
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                raise
            last_err = e
            # 其它 RuntimeError：继续尝试下一个源
        except Exception as e:  # noqa: BLE001
            last_err = e
            # 继续尝试下一个源
    # 全部失败
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    if last_err:
        raise last_err
    raise RuntimeError(f"所有镜像源均失败: {os.path.basename(dest)}")


def _get_json(url: str, cancel_event: Optional[threading.Event] = None) -> dict:
    """GET JSON（单 URL）。"""
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("已取消")
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def _get_manifest(cancel_event: Optional[threading.Event] = None) -> dict:
    """获取版本清单：BMCLAPI 优先，失败回退官方源。"""
    last_err: Exception | None = None
    for url in (_MANIFEST_URL, _OFFICIAL_MANIFEST_URL):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("已取消")
        try:
            return _get_json(url, cancel_event)
        except RuntimeError as e:
            if "取消" in str(e):
                raise
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("获取版本清单失败")


# ============ library 路径与平台过滤（与 MinecraftAdapter 保持一致）============

def _lib_path(install_dir: str, lib: dict) -> str:
    """library name(group:artifact:version[:classifier]) -> 本地 jar 路径。"""
    name = lib.get("name", "")
    parts = name.split(":")
    if len(parts) < 3:
        return ""
    group, artifact, version = parts[0], parts[1], parts[2]
    group_path = group.replace(".", "/")
    classifier = parts[3] if len(parts) > 3 else ""
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += ".jar"
    return os.path.join(install_dir, "libraries", group_path, artifact, version, filename)


def _lib_urls(lib: dict) -> list[str]:
    """library -> 下载 URL 列表（BMCLAPI 优先，官方源兜底）。

    优先 downloads.artifact.url（含镜像替换），否则拼 maven 路径（双源）。
    """
    urls: list[str] = []
    dl = (lib.get("downloads") or {}).get("artifact") or {}
    if dl.get("url"):
        urls.extend(_mirror_urls(dl["url"]))
    # 兜底：拼 maven 路径
    name = lib.get("name", "")
    parts = name.split(":")
    if len(parts) >= 3:
        group, artifact, version = parts[0], parts[1], parts[2]
        group_path = group.replace(".", "/")
        classifier = parts[3] if len(parts) > 3 else ""
        filename = f"{artifact}-{version}"
        if classifier:
            filename += f"-{classifier}"
        filename += ".jar"
        path = f"{group_path}/{artifact}/{version}/{filename}"
        urls.append(f"{_LIBRARIES_BASE}{path}")
        urls.append(f"{_OFFICIAL_LIB_BASE}{path}")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _lib_allowed(lib: dict) -> bool:
    """按 rules 判断 library 是否适用于当前平台。"""
    import sys

    rules = lib.get("rules", [])
    if not rules:
        return True
    allowed = False
    for rule in rules:
        action = rule.get("action")
        os_rule = rule.get("os")
        if not os_rule:
            if action == "allow":
                allowed = True
            else:
                return False
        else:
            os_name = os_rule.get("name", "")
            if sys.platform == "win32" and os_name == "windows":
                allowed = action == "allow"
            elif sys.platform.startswith("linux") and os_name == "linux":
                allowed = action == "allow"
            elif sys.platform == "darwin" and os_name == "osx":
                allowed = action == "allow"
    return allowed


# ============ 并发下载多个文件 ============

def _download_many(
    items: list[tuple[list[str], str, Optional[str]]],  # (urls, dest, sha1)
    progress: Optional[ProgressCb],
    stage: str,
    label: str,
    cancel_event: Optional[threading.Event],
) -> None:
    """并发下载多个文件，报告总进度。

    items 中已存在的文件（_download_one 内部跳过）不占带宽但仍计数。
    每个 item 的 urls 为镜像列表，逐源兜底。
    """
    total = len(items)
    done = 0
    done_lock = threading.Lock()
    last_pct = -1

    def _work(item: tuple[list[str], str, Optional[str]]) -> None:
        nonlocal done, last_pct
        urls, dest, sha1 = item
        _download_one(urls, dest, sha1, cancel_event)
        with done_lock:
            done += 1
            pct = int(done * 100 / total) if total else 100
            if progress and pct != last_pct:
                last_pct = pct
                progress(stage, f"{label} {done}/{total}", pct)

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = [pool.submit(_work, item) for item in items]
        try:
            for fut in as_completed(futures):
                fut.result()  # 抛出异常会进入 except
        except Exception:
            # 任一失败：取消其余
            if cancel_event is not None:
                cancel_event.set()
            raise


# ============ 主入口 ============

def ensure_vanilla_version(
    install_dir: str,
    mc_version: str,
    progress: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """确保原版 MC 版本文件齐全：version JSON + client jar + libraries + assets。

    幂等：已存在且 SHA1 校验通过的文件跳过。
    缺少文件时下载，已有则秒过。

    抛 RuntimeError 表示下载失败或取消。
    """
    if not mc_version:
        raise RuntimeError("未指定 Minecraft 版本")

    version_dir = os.path.join(install_dir, "versions", mc_version)
    version_json_path = os.path.join(version_dir, f"{mc_version}.json")
    client_jar_path = os.path.join(version_dir, f"{mc_version}.jar")

    # ---- 1. 版本 JSON ----
    if not os.path.isfile(version_json_path):
        if progress:
            progress("download", f"获取 MC {mc_version} 版本清单…", 2)
        manifest = _get_manifest(cancel_event)
        versions = manifest.get("versions", [])
        entry = next((v for v in versions if v.get("id") == mc_version), None)
        if not entry:
            raise RuntimeError(f"版本清单中找不到 Minecraft {mc_version}")
        # 版本 JSON：BMCLAPI 镜像优先，官方源兜底
        vj_urls = _mirror_urls(entry["url"])
        os.makedirs(version_dir, exist_ok=True)
        _download_one(vj_urls, version_json_path, entry.get("sha1"), cancel_event)

    with open(version_json_path, "r", encoding="utf-8") as f:
        vj = json.load(f)

    # ---- 2. client jar ----
    client = (vj.get("downloads") or {}).get("client") or {}
    if not os.path.isfile(client_jar_path):
        if progress:
            progress("download", f"下载 MC {mc_version} 客户端 jar…", 10)
        if not client.get("url"):
            raise RuntimeError(f"版本 JSON 中无 client jar 下载地址: {mc_version}")
        client_urls = _mirror_urls(client["url"])
        _download_one(client_urls, client_jar_path, client.get("sha1"), cancel_event)

    # ---- 3. libraries ----
    all_libs = [l for l in vj.get("libraries", []) if _lib_allowed(l)]
    lib_items: list[tuple[list[str], str, Optional[str]]] = []
    for lib in all_libs:
        p = _lib_path(install_dir, lib)
        if not p:
            continue
        if os.path.isfile(p):
            continue  # 已有，跳过
        urls = _lib_urls(lib)
        if not urls:
            continue
        sha1 = ((lib.get("downloads") or {}).get("artifact") or {}).get("sha1")
        lib_items.append((urls, p, sha1))
    if lib_items:
        if progress:
            progress("download", f"下载依赖库（{len(lib_items)} 个缺失）…", 20)
        _download_many(lib_items, progress, "download", "依赖库", cancel_event)

    # ---- 4. assets ----
    asset_index = vj.get("assetIndex") or {}
    if asset_index:
        index_id = asset_index.get("id", mc_version)
        index_dir = os.path.join(install_dir, "assets", "indexes")
        index_path = os.path.join(index_dir, f"{index_id}.json")
        if not os.path.isfile(index_path):
            if progress:
                progress("download", "下载资源索引…", 50)
            ai_urls = _mirror_urls(asset_index["url"])
            os.makedirs(index_dir, exist_ok=True)
            _download_one(ai_urls, index_path, asset_index.get("sha1"), cancel_event)

        with open(index_path, "r", encoding="utf-8") as f:
            ai = json.load(f)
        objects = ai.get("objects", {})
        asset_items: list[tuple[list[str], str, Optional[str]]] = []
        for _name, obj in objects.items():
            h = obj.get("hash", "")
            if not h:
                continue
            prefix = h[:2]
            obj_path = os.path.join(install_dir, "assets", "objects", prefix, h)
            if os.path.isfile(obj_path):
                continue  # 已有，跳过
            # 资源对象：BMCLAPI 优先，官方 resources.download.minecraft.net 兜底
            urls = [f"{_ASSETS_BASE}{prefix}/{h}", f"{_OFFICIAL_ASSETS_BASE}{prefix}/{h}"]
            asset_items.append((urls, obj_path, h))
        if asset_items:
            if progress:
                progress("download", f"下载游戏资源（{len(asset_items)} 个缺失）…", 60)
            _download_many(asset_items, progress, "download", "游戏资源", cancel_event)

    if progress:
        progress("done", f"Minecraft {mc_version} 原版文件就绪", 100)


def is_vanilla_version_ready(install_dir: str, mc_version: str) -> bool:
    """快速检查原版 MC 是否已安装（client jar 存在即视为就绪）。

    用于启动前的快速判断，避免每次启动都扫描全部文件。
    """
    if not mc_version:
        return False
    client_jar = os.path.join(
        install_dir, "versions", mc_version, f"{mc_version}.jar"
    )
    return os.path.isfile(client_jar)
