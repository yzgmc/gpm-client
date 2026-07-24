"""客户端配置：服务端地址、安装目录、Java 路径等。

持久化策略：
- 打包成 exe 后：配置文件与下载目录都放 **exe 同级**，重启不丢失。
- 源码运行：放仓库根 data/。

目录布局（exe 同级）：
  gpm-client.exe
  data/                  # 配置与状态
    client_config.json   # 主配置（服务端地址、登录态等）
    installed.json       # 已安装条目记录
    .reporter_id         # 客户端上报 ID
  downloads/             # 下载的 mod 与整合包安装根目录
    minecraft/...        # 按游戏分目录安装
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _detect_app_dir() -> Path:
    """返回应用根目录（配置/数据的存放基准）。

    打包后（exe）：sys.executable 指向 gpm-client.exe，用其所在目录。
    源码运行：sys.executable 是 python/python3，用本文件所在的仓库根目录。

    不依赖 Nuitka 的 __compiled__ 注入（子模块 globals 里不一定有），
    也不依赖 PyInstaller 的 sys._MEIPASS（Nuitka 不设置），
    改用 exe 文件名检测，最稳健。
    """
    exe_path = Path(sys.executable).resolve()
    exe_name = exe_path.name.lower()
    # 打包后 exe 名以 gpm-client 开头；源码运行时是 python/python3/python.exe
    if exe_name.startswith("gpm-client"):
        return exe_path.parent
    return Path(__file__).resolve().parent.parent


# 应用根目录：打包后为 exe 同级，源码运行时为仓库根
APP_DIR = _detect_app_dir()
# 配置目录：exe 同级 data/
DATA_DIR = APP_DIR / "data"
# 下载与安装根目录：exe 同级 downloads/（存储 mod 和整合包）
_DOWNLOADS_DIR = APP_DIR / "downloads"
_DEFAULT_INSTALL_BASE = str(_DOWNLOADS_DIR)

CONFIG_FILE = DATA_DIR / "client_config.json"
INSTALLED_FILE = DATA_DIR / "installed.json"
REPORTER_ID_FILE = DATA_DIR / ".reporter_id"


def ensure_dirs() -> None:
    """首次启动时创建配置目录与下载目录（exe 同级）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _load_or_create_reporter_id() -> str:
    """读取或生成持久化的客户端上报 ID。"""
    if REPORTER_ID_FILE.exists():
        try:
            return REPORTER_ID_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    rid = f"client-{uuid.uuid4().hex[:8]}"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        REPORTER_ID_FILE.write_text(rid, encoding="utf-8")
    except OSError:
        pass
    return rid


@dataclass
class ClientConfig:
    server_url: str = "http://127.0.0.1:8001"
    install_base_dir: str = _DEFAULT_INSTALL_BASE
    java_path: str = ""
    jvm_args: list[str] = field(default_factory=lambda: ["-Xmx4G", "-Xms1G"])
    last_sync_at: str = ""
    # Push 模型：向 web-admin 上报心跳
    admin_url: str = os.getenv("GPM_ADMIN_URL", "")  # 留空则不上报
    reporter_interval: float = float(os.getenv("GPM_REPORTER_INTERVAL", "10"))
    client_name: str = os.getenv("GPM_CLIENT_NAME", "Windows 客户端")
    reporter_id: str = ""  # 启动时填充
    # 登录系统：用户名 + token（登录后持久化，下次启动免登录）
    username: str = ""
    token: str = ""

    def __post_init__(self) -> None:
        if not self.reporter_id:
            self.reporter_id = _load_or_create_reporter_id()

    @classmethod
    def load(cls) -> "ClientConfig":
        # 首次启动确保目录存在
        ensure_dirs()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                # 兼容旧配置文件无新字段的情况
                fields = cls.__dataclass_fields__
                kwargs = {k: data.get(k, getattr(cls, k)) for k in fields if k != "reporter_id"}
                return cls(**kwargs)
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        cfg = cls()
        cfg.save()
        return cfg

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


def load_installed() -> dict:
    """读取本地已安装条目记录。结构：{ item_id: {hash, version, installed_path, kind} }"""
    if INSTALLED_FILE.exists():
        try:
            return json.loads(INSTALLED_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_installed(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTALLED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
