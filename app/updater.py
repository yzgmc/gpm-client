"""客户端自升级模块：版本检查 → 下载 → 调用外部 updater 进程替换。

设计要点：
1. 隔离 self-update 与主进程：
   - 主 exe 运行中无法被覆盖（Windows 文件占用），必须先退出主进程再替换。
   - 所以本模块只负责"检查+下载+派发"，物理替换由独立的 updater_helper 子进程完成。
2. 版本比较：使用 packaging.version.Version 标准 semver（含预发布标识）。
3. 安全性：
   - 仅信任 github.com 的 Release API（公开 API，无需 token）。
   - 下载的 exe 校验 sha256（从 Release body/digest 解析，或从 asset digest 头）。
   - 升级前再次确认用户已保存数据（即将退出应用）。
4. 异常处理：所有阶段失败抛 RuntimeError，UI 层捕获并友好提示。

升级流程：
  用户点击"检查更新"
    ↓
  check_for_update() 调 GitHub API
    ↓ 有新版本？
  ↓
  show_update_dialog() 让用户确认
    ↓ 确认
  download_update() 下载新 exe 到 cache 目录
    ↓
  launch_updater_helper() 启动 updater_helper 子进程并立即退出主程序
    ↓
  updater_helper 等待主进程退出 → 原子替换 → 启动新 exe
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import httpx
from packaging.version import InvalidVersion, Version

from app.downloader import download_file
from app.version import APP_VERSION, GITHUB_OWNER, GITHUB_REPO, UPDATE_ASSET_NAME

# GitHub Releases API（公开，无需 token，60 次/小时/IP 限流足够个人客户端使用）
_GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_RELEASE_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
_TIMEOUT = httpx.Timeout(10.0, read=60.0)


@dataclass
class UpdateInfo:
    """版本检查结果。"""

    current_version: str
    latest_version: str
    has_update: bool
    release_notes: str
    asset_url: str
    asset_size: int
    asset_name: str
    html_url: str  # 用户查看 release 的浏览器链接
    digest_sha256: Optional[str] = None  # 资产 SHA-256（从 Release body 中解析或 None）


def _parse_version_from_tag(tag: str) -> Optional[Version]:
    """从 Git tag（如 v0.1.0 / v0.1.0-20260727）解析出版本号。"""
    if not tag:
        return None
    # 去掉前导 v
    s = tag.lstrip("v").strip()
    # 截取 - 之前的部分（适配 v0.1.0-20260727-abc 格式）
    for sep in ("-", "+"):
        if sep in s:
            s = s.split(sep, 1)[0]
    try:
        return Version(s)
    except InvalidVersion:
        return None


def _extract_digest_from_release(release: dict, asset_name: str) -> Optional[str]:
    """从 release body 解析 sha256:<hex> 摘要。

    支持多种格式：
    - "SHA256: abc123..."（我们 yml 写的格式）
    - 表格行 "abc123  gpm-client.exe"
    """
    body = release.get("body") or ""
    asset_lower = asset_name.lower()
    for line in body.splitlines():
        line_strip = line.strip()
        if not line_strip:
            continue
        # 1) SHA256: <hex>（独立行）
        if line_strip.upper().startswith("SHA256:"):
            hex_part = line_strip.split(":", 1)[1].strip().split()[0]
            if len(hex_part) == 64 and all(c in "0123456789abcdefABCDEF" for c in hex_part):
                return hex_part.lower()
        # 2) "  <hex>  gpm-client.exe" 或 "*<hex>*gpm-client.exe*"
        if asset_lower in line_strip.lower():
            tokens = line_strip.split()
            for tok in tokens:
                tok = tok.strip("`*|")
                if len(tok) == 64 and all(c in "0123456789abcdefABCDEF" for c in tok):
                    return tok.lower()
    return None


def check_for_update() -> UpdateInfo:
    """查询 GitHub Releases API，比较当前版本与最新版本。

    返回 UpdateInfo，无论有无更新都返回（调用方通过 has_update 判断）。

    异常：
    - httpx.HTTPError：网络/HTTP 错误
    - RuntimeError：API 返回格式异常、当前版本无法解析
    """
    current_ver = _parse_version_from_tag(APP_VERSION)
    if current_ver is None:
        raise RuntimeError(f"当前版本号无法解析：{APP_VERSION!r}")

    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
        r = c.get(
            _GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"gpm-client/{APP_VERSION}",
            },
        )
        r.raise_for_status()
        release = r.json()

    latest_tag = release.get("tag_name") or ""
    latest_ver = _parse_version_from_tag(latest_tag)
    if latest_ver is None:
        raise RuntimeError(f"最新 Release tag 无法解析：{latest_tag!r}")

    # 找匹配 UPDATE_ASSET_NAME 的 asset
    asset_url = ""
    asset_size = 0
    asset_name = ""
    for asset in release.get("assets", []):
        if asset.get("name") == UPDATE_ASSET_NAME:
            asset_url = asset.get("browser_download_url", "")
            asset_size = int(asset.get("size", 0) or 0)
            asset_name = asset.get("name", "")
            break
    if not asset_url:
        raise RuntimeError(
            f"最新 Release 中未找到资产 {UPDATE_ASSET_NAME}。"
            f"请前往 {_RELEASE_PAGE} 手动下载。"
        )

    digest = _extract_digest_from_release(release, asset_name)

    return UpdateInfo(
        current_version=str(current_ver),
        latest_version=str(latest_ver),
        has_update=latest_ver > current_ver,
        release_notes=release.get("body", "") or "",
        asset_url=asset_url,
        asset_size=asset_size,
        asset_name=asset_name,
        html_url=release.get("html_url", _RELEASE_PAGE),
        digest_sha256=digest,
    )


def download_update(
    info: UpdateInfo,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """下载更新到临时目录，返回本地路径。

    路径格式：<data_dir>/update/<asset_name>，便于 updater_helper 定位。
    """
    # data_dir 来自 config（exe 同级 data/）
    from app.config import DATA_DIR

    dest_dir = os.path.join(str(DATA_DIR), "update")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, info.asset_name)

    download_file(
        info.asset_url,
        dest,
        expected_hash=info.digest_sha256,  # 若 Release 提供了 sha256 则强制校验
        progress=progress,
        cancel_event=cancel_event,
    )
    return dest


def is_frozen() -> bool:
    """是否在打包后的 exe 环境运行（PyInstaller / Nuitka）。"""
    return getattr(sys, "frozen", False) or "__compiled__" in dir() or bool(getattr(sys, "executable", "") and sys.executable.lower().endswith(".exe"))


def current_exe_path() -> str:
    """返回当前主 exe 路径。开发态返回 run.py 路径。"""
    if is_frozen():
        return os.path.abspath(sys.executable)
    # 开发态：用 run.py
    return os.path.abspath("run.py")


def launch_updater_and_exit(new_exe: str) -> None:
    """启动"自身"作为升级子进程（单 exe 方案），立即退出主程序。

    关键设计：不再依赖独立 updater_helper.exe，而是让主 exe 启动时检测
    --gpm-updater 参数来执行升级逻辑。Nuitka 打包时通过 --include-module
    嵌入 app._updater_helper 模块。

    流程：
    1. 用 sys.executable 启动自身 + --gpm-updater 参数（detach）
    2. 立即 os._exit(0) 退出主程序（释放 exe 文件锁）
    3. 子进程（同一个 exe）检测到 --gpm-updater → 等父进程退出 → 替换 → 重启
    """
    if is_frozen():
        # 打包态：sys.executable 就是主 exe 本身
        cmd = [
            sys.executable,
            "--gpm-updater",
            "--new-exe", new_exe,
            "--current-exe", current_exe_path(),
        ]
    else:
        # 开发态：用 python run.py 模拟（run.py 检测 --gpm-updater）
        run_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "run.py"))
        cmd = [
            sys.executable,
            run_py,
            "--gpm-updater",
            "--new-exe", new_exe,
            "--current-exe", current_exe_path(),
        ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    subprocess.Popen(
        cmd,
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 立即退出主程序
    os._exit(0)
