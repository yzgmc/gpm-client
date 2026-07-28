"""服务端 HTTP 客户端，封装所有 API 调用。

错误处理：所有网络/解析异常统一包装为 RuntimeError，附带人类可读的中文说明。
调用方拿到的栈追踪包含服务端地址 + HTTP 状态码 + 路径，方便排查。
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from gpm_common import SyncResponse
from app.downloader import get_sync_client


class ApiCallError(RuntimeError):
    """API 调用失败的统一异常类型。"""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 url: str = "", cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.__cause__ = cause


def _friendly_http_error(e: BaseException, method: str, url: str) -> RuntimeError:
    """把 httpx 异常翻译成 ApiCallError，附带可读信息。"""
    if isinstance(e, httpx.HTTPStatusError):
        body = ""
        try:
            body = e.response.text[:200] if e.response is not None else ""
        except Exception:
            pass
        msg = f"{method} {url} 返回 {e.response.status_code}"
        if body:
            msg += f"，响应: {body}"
        return ApiCallError(msg, status=e.response.status_code, url=url, cause=e)
    if isinstance(e, httpx.ConnectError):
        return ApiCallError(
            f"无法连接到服务端 {url}，请检查地址与网络（{type(e).__name__}: {e}）",
            url=url, cause=e,
        )
    if isinstance(e, httpx.TimeoutException):
        return ApiCallError(
            f"请求 {url} 超时，请稍后重试或检查服务端状态", url=url, cause=e,
        )
    if isinstance(e, httpx.RequestError):
        return ApiCallError(
            f"{method} {url} 网络异常: {e}", url=url, cause=e,
        )
    if isinstance(e, (ValueError, KeyError, TypeError)):
        return ApiCallError(
            f"{method} {url} 响应解析失败: {e}", url=url, cause=e,
        )
    return e


class ApiClient:
    def __init__(self, server_url: str, timeout: float = 10.0) -> None:
        self.base_url = server_url.rstrip("/")
        self._timeout = timeout

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}/api/v1{path}"

    def _get(self, path: str) -> Any:
        url = self._url(path)
        try:
            with get_sync_client() as c:
                r = c.get(url)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise _friendly_http_error(e, "GET", url) from e

    def sync(self) -> SyncResponse:
        data = self._get("/sync")
        return SyncResponse(**data)

    def list_modpacks(self) -> list[dict]:
        return self._get("/modpacks").get("modpacks", [])

    def list_mods(self) -> list[dict]:
        return self._get("/mods").get("mods", [])

    def download_url(self, kind: str, item_id: str) -> str:
        """返回下载 URL（kind: modpacks / mods）。供下载器流式拉取。"""
        return self._url(f"/{kind}/{item_id}/download")

    def server_status(self) -> Optional[dict]:
        try:
            return self._get("/status")
        except Exception:
            return None
