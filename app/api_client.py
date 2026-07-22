"""服务端 HTTP 客户端，封装所有 API 调用。"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from gpm_common import SyncResponse


class ApiClient:
    def __init__(self, server_url: str, timeout: float = 10.0) -> None:
        self.base_url = server_url.rstrip("/")
        self._timeout = timeout

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}/api/v1{path}"

    def sync(self) -> SyncResponse:
        with httpx.Client(timeout=self._timeout) as c:
            r = c.get(self._url("/sync"))
            r.raise_for_status()
            return SyncResponse(**r.json())

    def list_modpacks(self) -> list[dict]:
        with httpx.Client(timeout=self._timeout) as c:
            r = c.get(self._url("/modpacks"))
            r.raise_for_status()
            return r.json().get("modpacks", [])

    def list_mods(self) -> list[dict]:
        with httpx.Client(timeout=self._timeout) as c:
            r = c.get(self._url("/mods"))
            r.raise_for_status()
            return r.json().get("mods", [])

    def download_url(self, kind: str, item_id: str) -> str:
        """返回下载 URL（kind: modpacks / mods）。供下载器流式拉取。"""
        return self._url(f"/{kind}/{item_id}/download")

    def server_status(self) -> Optional[dict]:
        try:
            with httpx.Client(timeout=self._timeout) as c:
                r = c.get(self._url("/status"))
                r.raise_for_status()
                return r.json()
        except Exception:
            return None
