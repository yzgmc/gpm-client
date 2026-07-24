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

    打包后（exe）：配置与数据必须放 **exe 同级**，重启不丢失。
    源码运行：放仓库根 data/。

    关键：Nuitka --onefile 模式下，sys.executable 指向**解压到临时目录的 exe 副本**，
    sys.__file__ / __file__ 也指向临时目录里的 .py。若用它们当基准，配置会写到
    临时目录，重启即丢。

    正确做法（Nuitka 官方文档 + 作者 kayhayen 多个 issue 亲自确认）：
    **sys.argv[0] 在 onefile 下就是用户双击的原始 exe 路径**，且 Nuitka 保证它是
    绝对路径。这是唯一官方推荐方式。

    注意：网传的 NUITKA_ONEFILE_BINARY 环境变量**根本不存在**（Nuitka 源码
    OnefileBootstrap.c 里无此符号），不能用。
    """
    # 1. Nuitka 打包态（onefile / standalone）：sys.argv[0] = 用户双击的真实 exe
    if "__compiled__" in dir():
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0:
            p = Path(argv0)
            if not p.is_absolute():
                p = Path.cwd() / p
            return p.resolve().parent
    # 2. 源码运行：sys.argv[0] 是 run.py，其父目录即仓库根
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        p = Path(argv0)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p.resolve().parent
    # 3. 兜底
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
    jvm_args: list[str] = field(default_factory=list)  # 空=启动时自动分配内存+优化参数
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
                if not isinstance(data, dict):
                    raise ValueError("配置文件不是有效的 JSON 对象")
                # 兼容旧配置文件无新字段的情况：用 dataclass 字段定义的默认值兜底。
                # 注意：field(default_factory=...) 的字段在"类"上没有同名属性，
                # 不能用 getattr(cls, k)（会抛 AttributeError），必须用 fields() 取默认值。
                from dataclasses import fields as _dc_fields, MISSING
                kwargs: dict = {}
                for f in _dc_fields(cls):
                    if f.name == "reporter_id":
                        continue  # reporter_id 由 __post_init__ 处理
                    if f.name in data:
                        kwargs[f.name] = data[f.name]
                    elif f.default is not MISSING:
                        kwargs[f.name] = f.default
                    elif f.default_factory is not MISSING:  # type: ignore[misc]
                        kwargs[f.name] = f.default_factory()  # type: ignore[misc]
                return cls(**kwargs)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass  # 配置损坏，回退到默认配置
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
