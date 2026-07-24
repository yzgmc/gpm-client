"""Java 运行时自动安装器。

根据 Minecraft 版本匹配所需 Java 大版本，自动下载并解压到本地，供加载器安装
与游戏启动使用，无需用户手动配置 Java 路径。

MC 版本 → Java 大版本映射（基于 Mojang 官方运行时要求）：
  MC ≤ 1.16.5       → Java 8
  MC 1.17 ~ 1.20.4  → Java 17（1.17 官方要求 16+，Java 17 兼容且为 LTS）
  MC ≥ 1.20.5       → Java 21

下载源（按顺序自动选可用者，国内优先满速镜像）：
  1. 清华大学 TUNA 镜像（Adoptium 镜像，国内满速）
     目录页 https://mirrors.tuna.tsinghua.edu.cn/Adoptium/{ver}/jdk/x64/windows/
     解析 HTML 取最新 .zip 直链下载
  2. Adoptium 官方 API（海外，回退用）
     https://api.adoptium.net/v3/binary/latest/{ver}/ga/windows/x64/jdk/hotspot/normal/eclipse

Java 安装位置：<install_base_dir>/.cache/java/jdk-<major>/bin/java.exe
安装包缓存：<install_base_dir>/.cache/java/jdk-<major>.zip（避免重复下载）

已下载复用：ensure_java 优先复用本地已解压的匹配版本（jdk-<major>/bin/java.exe
存在且可用），其次复用已下载的 zip 缓存，仅在都没有时才下载。

进度回调与 loader_installer 一致：ProgressCb = (stage, detail, percent)
  stage ∈ {"download", "extract", "done", "error"}
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import zipfile
from typing import Callable, Optional

import httpx


# 进度回调签名: (stage, detail, percent)
#   stage ∈ {"download", "extract", "done", "error"}
ProgressCb = Callable[[str, str, int], None]

# 下载超时：连接 15s，读取 300s（JDK 较大，约 200MB，慢网络需余量）
_HTTP_TIMEOUT = httpx.Timeout(15.0, read=300.0)

# ---- 下载源配置（按优先级，国内镜像在前）----
# 1. 清华 TUNA 镜像（Adoptium 完整镜像，国内满速）
#    目录页列出该大版本所有 jdk zip，解析 HTML 取最新一个的直链。
_TUNA_LIST_URL = "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/{ver}/jdk/x64/windows/"
# 2. Adoptium 官方 API（海外，TUNA 不可用时回退）
ADOPTIUM_JDK_URL = (
    "https://api.adoptium.net/v3/binary/latest/{ver}/ga/windows/x64/jdk/hotspot/normal/eclipse"
)


def required_java_major(mc_version: str) -> int:
    """根据 Minecraft 版本返回所需 Java 大版本号。

    规则（Mojang 官方运行时要求）：
      MC ≤ 1.16.5       → Java 8
      MC 1.17 ~ 1.20.4  → Java 17
      MC ≥ 1.20.5       → Java 21
    无法解析时兜底返回 17。
    """
    empty = not (mc_version or "").strip()
    parts = (mc_version or "").split(".")
    try:
        major = int(parts[0]) if parts and parts[0] else 0
        minor = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        patch = int(parts[2]) if len(parts) > 2 and parts[2] else 0
    except ValueError:
        return 17  # 无法解析，兜底用 17
    # 空字符串 / 非 MC 版本（major=0）：无法判断，兜底用 17
    if empty or major == 0:
        return 17
    ver = (major, minor, patch)
    if ver <= (1, 16, 5):
        return 8
    if ver < (1, 20, 5):
        return 17
    return 21


def _java_cache_dir(install_base_dir: str) -> str:
    """所有 Java 安装包与解压结果的缓存根目录。"""
    d = os.path.join(install_base_dir, ".cache", "java")
    os.makedirs(d, exist_ok=True)
    return d


def _java_home_for(install_base_dir: str, major: int) -> str:
    """指定大版本的 Java 解压目标目录（JDK 根，含 bin/lib）。"""
    return os.path.join(_java_cache_dir(install_base_dir), f"jdk-{major}")


def find_local_java(install_base_dir: str, required_major: int) -> Optional[str]:
    """在本地缓存目录查找匹配版本的 java.exe。找到返回路径，否则 None。

    查找顺序：
    1. 标准路径 jdk-<major>/bin/java.exe（解压后顶层目录已提升的常规情况）
    2. jdk-<major> 下递归找 bin/java.exe（兼容解压结构异常）
    3. 扫描所有 jdk-* 目录，用 detect_java_major 检测实际版本是否匹配
       （兼容目录名与实际版本不一致的情况）
    """
    cache = _java_cache_dir(install_base_dir)
    home = _java_home_for(install_base_dir, required_major)
    # 1. 标准路径
    exe = os.path.join(home, "bin", "java.exe")
    if os.path.isfile(exe):
        return exe
    # 2. 该版本目录下递归找
    if os.path.isdir(home):
        for root, dirs, files in os.walk(home):
            if "java.exe" in files:
                return os.path.join(root, "java.exe")
    # 3. 扫描所有 jdk-* 目录，按实际版本匹配
    if os.path.isdir(cache):
        for name in os.listdir(cache):
            if not name.startswith("jdk-"):
                continue
            d = os.path.join(cache, name)
            if not os.path.isdir(d):
                continue
            cand = os.path.join(d, "bin", "java.exe")
            if not os.path.isfile(cand):
                continue
            actual = detect_java_major(cand)
            if actual == required_major:
                return cand
    return None


def _is_usable_java(java_path: str) -> bool:
    """检测 java 可执行文件是否存在且可运行。"""
    if not java_path or not os.path.isfile(java_path):
        return False
    try:
        r = subprocess.run(
            [java_path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def detect_java_major(java_path: str) -> Optional[int]:
    """运行 java -version 解析其大版本号。失败返回 None。

    Java 9+ 输出形如：openjdk version "21.0.3" 2024-04-16
    Java 8 输出形如：  openjdk version "1.8.0_422"
    """
    if not _is_usable_java(java_path):
        return None
    try:
        r = subprocess.run(
            [java_path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # 版本信息在 stderr（Java 8/17 都是）
        out = r.stderr or r.stdout
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
        if not m:
            return None
        first = int(m.group(1))
        second = int(m.group(2)) if m.group(2) else 0
        # Java 8 报告为 1.8；Java 9+ 直接报大版本号
        return second if first == 1 and second > 0 else first
    except Exception:
        return None


def _resolve_tuna_zip_url(major: int) -> Optional[str]:
    """解析清华 TUNA 镜像目录页，返回最新 jdk zip 的完整直链。

    TUNA 是 nginx autoindex 目录页，HTML 里有形如：
      OpenJDK21U-jdk_x64_windows_hotspot_21.0.11_10.zip
      OpenJDK8U-jdk_x64_windows_hotspot_8u492b09.zip
    的文件名。按文件名排序取最后一个（版本号最大）即为最新。
    解析失败/网络异常返回 None，由调用方回退 Adoptium 官方源。
    """
    list_url = _TUNA_LIST_URL.format(ver=major)
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as c:
            r = c.get(list_url)
            r.raise_for_status()
            html = r.text
    except Exception:
        return None
    # 宽松匹配该大版本所有 jdk zip 文件名
    pat = re.compile(r"OpenJDK%dU-jdk_x64_windows_hotspot_[^\"<>\s]+\.zip" % major)
    names = sorted(set(pat.findall(html)))
    if not names:
        return None
    # 文件名按字符串排序，版本号大的在后（21.0.11_10 > 21.0.7_6）
    latest = names[-1]
    return list_url + latest


def _download_java_zip(
    major: int,
    zip_path: str,
    progress: Optional[ProgressCb],
    cancel_event: Optional[threading.Event],
) -> str:
    """下载 JDK zip 到 zip_path.part，完成后原子替换。已缓存且非空则复用。

    下载源按优先级自动尝试：
      1. 清华 TUNA 镜像（国内满速，解析目录页取最新 zip 直链）
      2. Adoptium 官方 API（海外，回退）
    全部失败时抛出包含各源失败原因的聚合异常。
    """
    if os.path.exists(zip_path) and os.path.getsize(zip_path) > 0:
        if progress:
            progress("download", "已使用缓存的 Java 安装包", 100)
        return zip_path

    os.makedirs(os.path.dirname(zip_path) or ".", exist_ok=True)
    tmp = zip_path + ".part"

    # 候选源：(label, url)
    sources = []
    tuna_url = _resolve_tuna_zip_url(major)
    if tuna_url:
        sources.append(("清华 TUNA 镜像", tuna_url))
    sources.append(("Adoptium 官方", ADOPTIUM_JDK_URL.format(ver=major)))

    errors: list[str] = []
    for label, url in sources:
        if progress:
            progress("download", f"正在从{label}下载 Java {major}…", 0)
        try:
            with httpx.stream("GET", url, timeout=_HTTP_TIMEOUT, follow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0) or 0)
                done = 0
                last = -1
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("已取消")
                        f.write(chunk)
                        done += len(chunk)
                        pct = int(done * 100 / total) if total else 0
                        if progress and pct != last:
                            last = pct
                            progress("download", f"下载 Java {major}（{label}）… {pct}%", pct)
            os.replace(tmp, zip_path)
            if progress:
                progress("download", f"Java 安装包下载完成（{label}）", 100)
            return zip_path
        except RuntimeError:
            # 取消：立即抛出，不回退其它源
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            continue

    raise RuntimeError("Java 下载失败，所有源均不可用：\n" + "\n".join(errors))


def _extract_java_zip(
    zip_path: str,
    dest_home: str,
    progress: Optional[ProgressCb],
    cancel_event: Optional[threading.Event],
) -> str:
    """解压 JDK zip 到 dest_home。

    Adoptium zip 内顶层是一个目录（如 jdk-21.0.x+x），需要把它提升为 dest_home，
    使 dest_home 直接就是 JDK 根目录（含 bin/、lib/ 等）。
    """
    os.makedirs(os.path.dirname(dest_home) or ".", exist_ok=True)
    if progress:
        progress("extract", "正在解压 Java…", 0)

    tmp_extract = dest_home + ".extract"
    if os.path.exists(tmp_extract):
        shutil.rmtree(tmp_extract, ignore_errors=True)
    os.makedirs(tmp_extract, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.namelist()
            total = len(members)
            last_pct = -1
            for i, name in enumerate(members):
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("已取消")
                zf.extract(name, tmp_extract)
                pct = int((i + 1) * 100 / total) if total else 0
                if progress and pct != last_pct and pct % 5 == 0:
                    last_pct = pct
                    progress("extract", f"解压 Java… {pct}%", pct)

        # zip 内顶层目录提升为 dest_home
        entries = [e for e in os.listdir(tmp_extract) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(tmp_extract, entries[0])):
            inner = os.path.join(tmp_extract, entries[0])
            if os.path.exists(dest_home):
                shutil.rmtree(dest_home, ignore_errors=True)
            shutil.move(inner, dest_home)
        else:
            # 无统一顶层目录，直接作为 home
            if os.path.exists(dest_home):
                shutil.rmtree(dest_home, ignore_errors=True)
            shutil.move(tmp_extract, dest_home)

        if progress:
            progress("extract", "Java 解压完成", 100)
        return dest_home
    except Exception:
        shutil.rmtree(tmp_extract, ignore_errors=True)
        raise
    finally:
        if os.path.exists(tmp_extract):
            shutil.rmtree(tmp_extract, ignore_errors=True)


def ensure_java(
    mc_version: str,
    install_base_dir: str,
    java_path: Optional[str],
    progress: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """确保有可用的 Java 运行时。返回 java.exe 路径。

    流程：
    1. 若 java_path 配置且可运行，直接返回。
    2. 否则按 mc_version 计算所需 Java 大版本。
    3. 在本地 .cache/java/ 查找已下载的匹配版本，找到直接返回。
    4. 找不到则从 Adoptium 下载 zip 并解压，返回新安装的 java.exe。

    所有异常向上抛（含 "已取消" RuntimeError），由调用方处理。
    """
    # 1. 用户已配置的 java
    if java_path and _is_usable_java(java_path):
        if progress:
            progress("done", "使用已配置的 Java", 100)
        return java_path

    if not mc_version:
        raise RuntimeError("未识别到游戏版本，无法确定所需 Java 版本")

    major = required_java_major(mc_version)

    # 2. 本地缓存查找（已下载且解压成功的 Java 直接复用，不重新下载）
    local = find_local_java(install_base_dir, major)
    if local:
        if progress:
            progress("done", f"使用本地已下载的 Java {major}", 100)
        return local

    cache_dir = _java_cache_dir(install_base_dir)
    zip_path = os.path.join(cache_dir, f"jdk-{major}.zip")
    dest_home = _java_home_for(install_base_dir, major)

    # 3. 若有已下载的 zip 缓存但 jdk 目录不完整/被删，先尝试用本地 zip 解压，
    #    避免因目录缺失就重新下载（省流量、省时间）。
    #    _download_java_zip 内部会检测 zip 缓存并复用，这里无需额外分支。
    _download_java_zip(major, zip_path, progress, cancel_event)
    _extract_java_zip(zip_path, dest_home, progress, cancel_event)

    exe = os.path.join(dest_home, "bin", "java.exe")
    if not os.path.isfile(exe):
        raise RuntimeError(f"Java 解压完成但未找到 java.exe：{exe}")
    if not _is_usable_java(exe):
        raise RuntimeError(f"下载的 Java 无法运行：{exe}")
    if progress:
        progress("done", f"Java {major} 已安装到 {dest_home}", 100)
    return exe
