"""客户端配置：服务端地址、安装目录、Java 路径等。持久化到 data/client_config.json。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_FILE = DATA_DIR / "client_config.json"
INSTALLED_FILE = DATA_DIR / "installed.json"


@dataclass
class ClientConfig:
    server_url: str = "http://127.0.0.1:8000"
    install_base_dir: str = str(DATA_DIR / "games")
    java_path: str = ""
    jvm_args: list[str] = field(default_factory=lambda: ["-Xmx4G", "-Xms1G"])
    last_sync_at: str = ""

    @classmethod
    def load(cls) -> "ClientConfig":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                return cls(**{k: data.get(k, getattr(cls, k)) for k in cls.__dataclass_fields__})
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
