"""同步管理器：拉取服务端条目并与本地 installed.json 比对状态。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gpm_common import SyncResponse

from app.api_client import ApiClient
from app.config import ClientConfig, load_installed


@dataclass
class ItemStatus:
    """单条目的本地状态。"""

    state: str  # "not_installed" / "installed" / "update_available"
    local_version: Optional[str] = None
    local_hash: Optional[str] = None


class SyncManager:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.client = ApiClient(config.server_url)
        self.last_sync: Optional[SyncResponse] = None

    def update_server(self, server_url: str) -> None:
        self.config.server_url = server_url.rstrip("/")
        self.config.save()
        self.client = ApiClient(self.config.server_url)

    def sync(self) -> SyncResponse:
        data = self.client.sync()
        self.last_sync = data
        from datetime import datetime, timezone

        self.config.last_sync_at = datetime.now(timezone.utc).isoformat()
        self.config.save()
        return data

    def modpack_status(self, modpack: dict) -> ItemStatus:
        installed = load_installed()
        rec = installed.get(modpack["id"])
        if not rec:
            return ItemStatus(state="not_installed")
        if rec.get("hash") != modpack.get("file_hash"):
            return ItemStatus(
                state="update_available",
                local_version=rec.get("version"),
                local_hash=rec.get("hash"),
            )
        return ItemStatus(
            state="installed",
            local_version=rec.get("version"),
            local_hash=rec.get("hash"),
        )

    def mod_status(self, mod: dict) -> ItemStatus:
        installed = load_installed()
        rec = installed.get(mod["id"])
        if not rec:
            return ItemStatus(state="not_installed")
        if rec.get("hash") != mod.get("file_hash"):
            return ItemStatus(state="update_available", local_version=rec.get("version"))
        return ItemStatus(state="installed", local_version=rec.get("version"))
