"""模组加载器自动安装器：Fabric / Forge / NeoForge / Quilt。

整合包下载解压完成后，根据整合包的 mod_loader 字段自动：
1. 下载对应加载器的官方安装器 jar（带缓存，避免重复下载）
2. 若 mod_loader_version 未指定，查询官方 meta API 取最新稳定版本
3. 用 java 运行安装器，把加载器装进整合包目录（相当于 .minecraft）
4. 通过统一进度回调 (stage, detail, pct) 报告各阶段状态

安装完成后，整合包目录即可作为游戏根目录直接启动（见 launcher.py）。

各加载器官方 CLI（来自官方 wiki / 文档）：
  Fabric : java -jar fabric-installer.jar client -dir <dir> -mcversion <mc> -loader <loader> -noprofile
  Forge  : java -jar forge-<mc>-<forge>-installer.jar --installClient <dir>
  NeoForg: java -jar neoforge-<ver>-installer.jar --installClient <dir>
  Quilt  : java -jar quilt-installer.jar install client <mc> --install-dir=<dir> --no-profile [--loader=<loader>]
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import httpx


# 进度回调签名: (stage, detail, percent)
#   stage ∈ {"download", "install", "done", "error"}
ProgressCb = Callable[[str, str, int], None]

# 通用超时：连接 15s，读取 120s（安装器 jar / meta 查询都不应太慢，但给足余量）
_HTTP_TIMEOUT = httpx.Timeout(15.0, read=120.0)

# Fabric 安装器使用固定稳定版（1.1.0，官方 Maven，长期不变）
FABRIC_INSTALLER_URL = (
    "https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.1.0/fabric-installer-1.1.0.jar"
)
FABRIC_LOADER_META = "https://meta.fabricmc.net/v2/versions/loader/{mc}"

# Quilt 安装器：版本动态取最新（Maven metadata）
QUILT_INSTALLER_META = (
    "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/maven-metadata.xml"
)
QUILT_INSTALLER_JAR = (
    "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/{ver}/quilt-installer-{ver}.jar"
)
QUILT_LOADER_META = "https://meta.quiltmc.org/v3/versions/loader/{mc}"

# Forge 安装器：版本 = mc + forge 版本，URL 直接拼
FORGE_INSTALLER_URL = (
    "https://maven.minecraftforge.net/net/minecraftforge/forge/{mc}-{forge}/"
    "forge-{mc}-{forge}-installer.jar"
)
FORGE_PROMOS_URL = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"

# NeoForge 安装器：版本 = neoforge 版本，URL 直接拼
NEOFORGE_INSTALLER_URL = (
    "https://maven.neoforged.org/releases/net/neoforged/neoforge/{ver}/neoforge-{ver}-installer.jar"
)
NEOFORGE_META_URL = "https://maven.neoforged.org/releases/net/neoforged/neoforge/maven-metadata.xml"


# 支持的加载器（vanilla 无需安装器）
SUPPORTED_LOADERS = ("fabric", "forge", "neoforge", "quilt")


def _cache_dir(install_base_dir: str) -> str:
    """所有加载器安装器 jar 的缓存目录。"""
    d = os.path.join(install_base_dir, ".cache", "loaders")
    os.makedirs(d, exist_ok=True)
    return d


def _download_to_cache(
    url: str,
    cache_path: str,
    progress: Optional[ProgressCb],
    cancel_event: Optional[threading.Event],
    label: str,
) -> str:
    """通用下载：流式下载到 cache_path.part，完成后原子替换。已缓存且非空则直接复用。"""
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        if progress:
            progress("download", f"已使用缓存的{label}", 100)
        return cache_path

    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    tmp_path = cache_path + ".part"
    if progress:
        progress("download", f"正在下载{label}…", 0)
    try:
        with httpx.stream("GET", url, timeout=_HTTP_TIMEOUT, follow_redirects=True) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            done = 0
            last = -1
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("已取消")
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    if progress and pct != last:
                        last = pct
                        progress("download", f"下载{label}… {pct}%", pct)
        os.replace(tmp_path, cache_path)
        if progress:
            progress("download", f"{label}下载完成", 100)
        return cache_path
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _run_java_installer(
    cmd: list[str],
    progress: Optional[ProgressCb],
    cancel_event: Optional[threading.Event],
    cwd: str,
    detail_prefix: str,
) -> None:
    """运行 java 安装器进程，实时读取输出推进度，检查取消与退出码。

    安装器本身不输出百分比，故按输出行数推进，封顶 95%（留 5% 给收尾）。
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    lines_seen = 0
    while True:
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise RuntimeError("已取消")
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                break
            continue
        lines_seen += 1
        text = line.strip()
        if text and progress:
            pct = min(95, lines_seen * 5)
            progress("install", f"{detail_prefix}: {text}", pct)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"{detail_prefix}退出码 {rc}，请检查 Java 路径与网络后重试")


def _get_json(url: str) -> object:
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def _get_text(url: str) -> str:
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


# ============================ Fabric ============================

def _resolve_fabric_loader(mc_version: str, loader_version: Optional[str]) -> str:
    if loader_version:
        return loader_version
    try:
        data = _get_json(FABRIC_LOADER_META.format(mc=mc_version))
        if isinstance(data, list):
            for item in data:
                loader = (item or {}).get("loader") or {}
                if loader.get("stable"):
                    return loader.get("version", "")
            if data:
                return ((data[0] or {}).get("loader") or {}).get("version", "")
    except Exception:
        pass
    return ""


def _install_fabric(install_dir, mc_version, loader_version, install_base_dir,
                    java_path, progress, cancel_event) -> None:
    installer_jar = os.path.join(_cache_dir(install_base_dir), "fabric-installer-1.1.0.jar")
    _download_to_cache(FABRIC_INSTALLER_URL, installer_jar, progress, cancel_event, "Fabric 安装器")

    loader = _resolve_fabric_loader(mc_version, loader_version)
    if not loader:
        raise RuntimeError(
            f"无法确定 Fabric Loader 版本（MC {mc_version}），请在服务端为整合包填写 mod_loader_version"
        )
    if not java_path:
        raise RuntimeError("未配置 Java 路径，无法运行 Fabric 安装器。请在设置中填写 java 可执行文件路径。")

    if progress:
        progress("install", f"正在安装 Fabric {loader} (MC {mc_version})…", 0)
    cmd = [
        java_path, "-jar", installer_jar, "client",
        "-dir", install_dir,
        "-mcversion", mc_version,
        "-loader", loader,
        "-noprofile",
    ]
    _run_java_installer(cmd, progress, cancel_event, install_dir, "Fabric 安装")
    if progress:
        progress("install", "Fabric 安装完成", 100)


# ============================ Quilt ============================

def _resolve_quilt_loader(mc_version: str, loader_version: Optional[str]) -> str:
    if loader_version:
        return loader_version
    try:
        data = _get_json(QUILT_LOADER_META.format(mc=mc_version))
        if isinstance(data, list):
            for item in data:
                loader = (item or {}).get("loader") or {}
                if loader.get("stable"):
                    return loader.get("version", "")
            if data:
                return ((data[0] or {}).get("loader") or {}).get("version", "")
    except Exception:
        pass
    return ""


def _latest_quilt_installer_version() -> str:
    """从 Quilt Maven metadata 取最新（release 优先，否则 latest）。"""
    try:
        text = _get_text(QUILT_INSTALLER_META)
        root = ET.fromstring(text)
        # 优先 <release>，其次 <latest>
        rel = root.findtext(".//versioning/release")
        if rel:
            return rel.strip()
        latest = root.findtext(".//versioning/latest")
        if latest:
            return latest.strip()
        # 兜底取 versions 列表最后一个
        versions = root.findall(".//versioning/versions/version")
        if versions:
            return (versions[-1].text or "").strip()
    except Exception:
        pass
    return ""


def _install_quilt(install_dir, mc_version, loader_version, install_base_dir,
                   java_path, progress, cancel_event) -> None:
    qi_ver = _latest_quilt_installer_version()
    if not qi_ver:
        raise RuntimeError("无法获取 Quilt 安装器版本，请检查网络或手动指定")
    installer_jar = os.path.join(_cache_dir(install_base_dir), f"quilt-installer-{qi_ver}.jar")
    url = QUILT_INSTALLER_JAR.format(ver=qi_ver)
    _download_to_cache(url, installer_jar, progress, cancel_event, "Quilt 安装器")

    loader = _resolve_quilt_loader(mc_version, loader_version)
    if not loader:
        raise RuntimeError(
            f"无法确定 Quilt Loader 版本（MC {mc_version}），请在服务端为整合包填写 mod_loader_version"
        )
    if not java_path:
        raise RuntimeError("未配置 Java 路径，无法运行 Quilt 安装器。请在设置中填写 java 可执行文件路径。")

    if progress:
        progress("install", f"正在安装 Quilt {loader} (MC {mc_version})…", 0)
    cmd = [
        java_path, "-jar", installer_jar, "install", "client", mc_version,
        f"--install-dir={install_dir}",
        f"--loader={loader}",
        "--no-profile",
    ]
    _run_java_installer(cmd, progress, cancel_event, install_dir, "Quilt 安装")
    if progress:
        progress("install", "Quilt 安装完成", 100)


# ============================ Forge ============================

def _resolve_forge_version(mc_version: str, loader_version: Optional[str]) -> str:
    """返回 Forge 版本号（不含 mc 前缀）。loader_version 给定时直接用。"""
    if loader_version:
        return loader_version
    try:
        data = _get_json(FORGE_PROMOS_URL)
        promos = (data or {}).get("promos") or {}
        # 优先 recommended，其次 latest
        for suffix in ("recommended", "latest"):
            v = promos.get(f"{mc_version}-{suffix}")
            if v:
                return v
    except Exception:
        pass
    return ""


def _install_forge(install_dir, mc_version, loader_version, install_base_dir,
                   java_path, progress, cancel_event) -> None:
    forge_ver = _resolve_forge_version(mc_version, loader_version)
    if not forge_ver:
        raise RuntimeError(
            f"无法确定 Forge 版本（MC {mc_version}），请在服务端为整合包填写 mod_loader_version"
        )
    if not java_path:
        raise RuntimeError("未配置 Java 路径，无法运行 Forge 安装器。请在设置中填写 java 可执行文件路径。")

    installer_name = f"forge-{mc_version}-{forge_ver}-installer.jar"
    installer_jar = os.path.join(_cache_dir(install_base_dir), installer_name)
    url = FORGE_INSTALLER_URL.format(mc=mc_version, forge=forge_ver)
    _download_to_cache(url, installer_jar, progress, cancel_event, "Forge 安装器")

    if progress:
        progress("install", f"正在安装 Forge {forge_ver} (MC {mc_version})…", 0)
    cmd = [java_path, "-jar", installer_jar, "--installClient", install_dir]
    _run_java_installer(cmd, progress, cancel_event, install_dir, "Forge 安装")
    if progress:
        progress("install", "Forge 安装完成", 100)


# ============================ NeoForge ============================

def _neoforge_version_prefix(mc_version: str) -> str:
    """根据 MC 版本推断 NeoForge 版本号前缀。

    特例：MC 1.20.1 → NeoForge 47.x（继承自 Forge 47）
    其余：去掉 MC 版本开头的 "1."，例如 1.20.2 → "20.2"，1.21 → "21"，1.21.1 → "21.1"
    """
    if mc_version == "1.20.1":
        return "47"
    parts = mc_version.split(".")
    if len(parts) >= 3 and parts[0] == "1":
        return ".".join(parts[1:])  # 20.2 / 21.1
    if len(parts) == 2 and parts[0] == "1":
        return parts[1]  # 21
    return mc_version


def _parse_version_key(ver: str) -> tuple:
    """把版本字符串转成可比较的元组，稳定版优先于 -beta。

    beta_flag 放在最前：稳定=2 优先于 beta=1，再按版本号数字降序。
    这样同一大版本下稳定版总排在 beta 版之前。
    """
    base, _, suffix = ver.partition("-")
    nums = []
    for p in base.split("."):
        m = re.match(r"\d+", p)
        nums.append(int(m.group()) if m else 0)
    beta_flag = 1 if suffix.lower().startswith("beta") else 2  # 稳定=2 > beta=1
    return (beta_flag,) + tuple(nums)


def _resolve_neoforge_version(mc_version: str, loader_version: Optional[str]) -> str:
    """返回 NeoForge 版本号。loader_version 给定时直接用，否则按 MC 版本前缀从 Maven 取最新。"""
    if loader_version:
        return loader_version
    prefix = _neoforge_version_prefix(mc_version)
    try:
        text = _get_text(NEOFORGE_META_URL)
        # 用正则提取所有 <version>...</version>，避免命名空间解析问题
        candidates = re.findall(r"<version>\s*([^<]+?)\s*</version>", text)
        matched = [c for c in candidates if c.startswith(prefix + ".") or c == prefix]
        if not matched:
            return ""
        # 优先非 beta，且版本号最大
        matched.sort(key=_parse_version_key, reverse=True)
        return matched[0]
    except Exception:
        pass
    return ""


def _install_neoforge(install_dir, mc_version, loader_version, install_base_dir,
                      java_path, progress, cancel_event) -> None:
    neo_ver = _resolve_neoforge_version(mc_version, loader_version)
    if not neo_ver:
        raise RuntimeError(
            f"无法确定 NeoForge 版本（MC {mc_version}），请在服务端为整合包填写 mod_loader_version"
        )
    if not java_path:
        raise RuntimeError("未配置 Java 路径，无法运行 NeoForge 安装器。请在设置中填写 java 可执行文件路径。")

    installer_name = f"neoforge-{neo_ver}-installer.jar"
    installer_jar = os.path.join(_cache_dir(install_base_dir), installer_name)
    url = NEOFORGE_INSTALLER_URL.format(ver=neo_ver)
    _download_to_cache(url, installer_jar, progress, cancel_event, "NeoForge 安装器")

    if progress:
        progress("install", f"正在安装 NeoForge {neo_ver} (MC {mc_version})…", 0)
    cmd = [java_path, "-jar", installer_jar, "--installClient", install_dir]
    _run_java_installer(cmd, progress, cancel_event, install_dir, "NeoForge 安装")
    if progress:
        progress("install", "NeoForge 安装完成", 100)


# ============================ 统一入口 ============================

_INSTALLERS = {
    "fabric": _install_fabric,
    "quilt": _install_quilt,
    "forge": _install_forge,
    "neoforge": _install_neoforge,
}


def install_loader(
    loader: str,
    install_dir: str,
    mc_version: str,
    loader_version: Optional[str],
    install_base_dir: str,
    java_path: str,
    progress: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """完整流程：下载安装器 + 运行安装。返回 install_dir。

    loader: "fabric" | "forge" | "neoforge" | "quilt"（vanilla 直接跳过，不调用本函数）
    progress 回调签名: (stage, detail, percent)
    """
    loader = (loader or "").lower()
    fn = _INSTALLERS.get(loader)
    if fn is None:
        raise RuntimeError(f"暂不支持的模组加载器: {loader}（支持: {', '.join(SUPPORTED_LOADERS)}）")
    if not mc_version:
        raise RuntimeError("未识别到游戏版本，无法安装模组加载器")
    if not os.path.isdir(install_dir):
        os.makedirs(install_dir, exist_ok=True)

    fn(
        install_dir=install_dir,
        mc_version=mc_version,
        loader_version=loader_version or None,
        install_base_dir=install_base_dir,
        java_path=java_path,
        progress=progress,
        cancel_event=cancel_event,
    )
    if progress:
        progress("done", f"{loader.capitalize()} 已安装到 {install_dir}", 100)
    return install_dir
