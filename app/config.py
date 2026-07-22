"""客户端配置：服务端地址、安装目录、Java 路径等。持久化到 data/client_config.json。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_FILE = DATA_DIR / "client_config.json"
INSTALLED_FILE = DATA_DIR / "installed.json"
REPORTER_ID_FILE = DATA_DIR / ".reporter_id"


def _load_or_create_reporter_id() -> str:
    """读取或生成持久化的客户端上报 ID。"""
    if REPORTER_ID_FILE.exists():
        try:
            return REPORTER_ID_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    import uuid

    rid = f"client-{uuid.uuid4().hex[:8]}"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        REPORTER_ID_FILE.write_text(rid, encoding="utf-8")
    except OSError:
        pass
    return rid


@dataclass
class ClientConfig:
    server_url: str = "http://127.0.0.1:8000"
    install_base_dir: str = str(DATA_DIR / "games")
    java_path: str = ""
    jvm_args: list[str] = field(default_factory=lambda: ["-Xmx4G", "-Xms1G"])
    last_sync_at: str = ""
    # Push 模型：向 web-admin 上报心跳
    admin_url: str = os.getenv("GPM_ADMIN_URL", "")  # 留空则不上报
    reporter_interval: float = float(os.getenv("GPM_REPORTER_INTERVAL", "10"))
    client_name: str = os.getenv("GPM_CLIENT_NAME", "Windows 客户端")
    reporter_id: str = ""  # 启动时填充

    def __post_init__(self) -> None:
        if not self.reporter_id:
            self.reporter_id = _load_or_create_reporter_id()

    @classmethod
    def load(cls) -> "ClientConfig":
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
