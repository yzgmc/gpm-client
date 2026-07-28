"""微软账号正版登录：MSA OAuth → XBL → XSTS → MC Token → Profile。

完整认证链路（首次浏览器登录）：
  1. 本地起 HTTP 服务等 OAuth 回调
  2. 打开浏览器让用户登录微软账号
  3. 用授权码换 MSA access_token + refresh_token
  4. MSA token → XBL token（+ uhs）
  5. XBL token → XSTS token（+ uhs）
  6. XSTS token → MC access_token（24h）
  7. 检查游戏所有权（mcstore）
  8. 取玩家档案（name + uuid）

后续启动用 refresh_token 静默续登（不弹浏览器），重跑 4→8。

client_id 用 Prism Launcher 公开的（已过 Mojang 审核，支持 localhost loopback）。
"""

from __future__ import annotations

import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from app.downloader import get_sync_client


# ============ 配置 ============
# Prism Launcher 公开 client_id（见 PrismLauncher CMakeLists.txt 的 Launcher_MSA_CLIENT_ID：
# https://github.com/PrismLauncher/PrismLauncher/blob/develop/CMakeLists.txt#L243）
# 已注册 http://localhost loopback 且过 Mojang 审核，mcstore 不会 403。
# 这是公开的非机密值，仅标识应用身份，不涉及账号安全。
CLIENT_ID = "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb"

REDIRECT_PORT = 8917  # 本地回调端口（Azure 应用注册 http://localhost 即匹配任意端口）
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/"
SCOPE = "XboxLive.signin offline_access"  # offline_access 才有 refresh_token

# API 端点
MS_AUTHORIZE = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
MS_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_AUTH = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_AUTH = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_STORE = "https://api.minecraftservices.com/entitlements/mcstore"
MC_PROFILE = "https://api.minecraftservices.com/minecraft/profile"

_HTTP_TIMEOUT = httpx.Timeout(15.0, read=30.0)


class MsaCredentials:
    """微软账号登录后获得的完整凭据（可序列化到 config 持久化）。"""

    def __init__(
        self,
        username: str,
        uuid: str,
        mc_access_token: str,
        ms_refresh_token: str,
        mc_expires_at: float,
    ) -> None:
        self.username = username  # 玩家名（如 Steve）
        self.uuid = uuid  # 无连字符 32 位 hex
        self.mc_access_token = mc_access_token  # MC token（24h）
        self.ms_refresh_token = ms_refresh_token  # MSA refresh_token（长期，静默续登用）
        self.mc_expires_at = mc_expires_at  # MC token 过期时间戳

    @property
    def is_mc_token_valid(self) -> bool:
        """MC token 是否仍在有效期内（留 60s 缓冲）。"""
        return time.time() < self.mc_expires_at - 60

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "uuid": self.uuid,
            "mc_access_token": self.mc_access_token,
            "ms_refresh_token": self.ms_refresh_token,
            "mc_expires_at": self.mc_expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MsaCredentials":
        return cls(
            username=d.get("username", ""),
            uuid=d.get("uuid", ""),
            mc_access_token=d.get("mc_access_token", ""),
            ms_refresh_token=d.get("ms_refresh_token", ""),
            mc_expires_at=float(d.get("mc_expires_at", 0)),
        )


# ============ 第1步：本地 HTTP 服务接收 OAuth 回调 ============
class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        code = q.get("code", [None])[0]
        err = q.get("error", [None])[0]
        err_desc = q.get("error_description", [None])[0]
        self.server.captured_code = code  # type: ignore[attr-defined]
        self.server.captured_err = err  # type: ignore[attr-defined]
        self.server.captured_err_desc = err_desc  # type: ignore[attr-defined]
        if code:
            body = b"<h1>\xe7\x99\xbb\xe5\xbd\x95\xe6\x88\x90\xe5\x8a\x9f</h1><p>\xe5\x8f\xaf\xe5\x85\xb3\xe9\x97\xad\xe6\xad\xa4\xe9\xa1\xb5\xe9\x9d\xa2\xe8\xbf\x94\xe5\x9b\x9e\xe5\x90\xaf\xe5\x8a\xa8\xe5\x99\xa8\xe3\x80\x82</p>"
        else:
            desc = err_desc or err or "未知错误"
            body = f"<h1>\xe7\x99\xbb\xe5\xbd\x95\xe5\xa4\xb1\xe8\xb4\xa5</h1><p>{desc}</p>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D401,N802 - 静音日志
        pass


def _wait_for_code(timeout: float = 300.0) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """起临时本地 HTTP 服务，阻塞等待浏览器带回 code。返回 (code, error, error_description)。"""
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server.captured_code = None  # type: ignore[attr-defined]
    server.captured_err = None  # type: ignore[attr-defined]
    server.captured_err_desc = None  # type: ignore[attr-defined]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if server.captured_code or server.captured_err:  # type: ignore[attr-defined]
                break
            time.sleep(0.2)
        return (  # type: ignore[attr-defined]
            server.captured_code,
            server.captured_err,
            server.captured_err_desc,
        )
    finally:
        server.shutdown()
        server.server_close()


# ============ 第1步：构造授权 URL 并打开浏览器 ============
def _get_auth_url() -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "response_mode": "query",
    }
    return f"{MS_AUTHORIZE}?{urlencode(params)}"


def _exchange_code_for_ms_token(code: str) -> dict:
    """用授权码换 MSA access_token + refresh_token。"""
    data = {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }
    with get_sync_client() as c:
        r = c.post(MS_TOKEN, data=data)
        r.raise_for_status()
        j = r.json()
    return {
        "access_token": j["access_token"],
        "refresh_token": j["refresh_token"],
        "expires_in": j.get("expires_in", 3600),
    }


# ============ 第7步：用 refresh_token 刷新（静默续登）============
def _refresh_ms_token(refresh_token: str) -> dict:
    """用 refresh_token 换新的 MSA access_token + refresh_token。

    MSA 的 refresh_token 是一次性的，每次刷新都换发新的，必须存最新的。
    """
    data = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPE,
    }
    with get_sync_client() as c:
        r = c.post(MS_TOKEN, data=data)
        r.raise_for_status()
        j = r.json()
    return {
        "access_token": j["access_token"],
        "refresh_token": j["refresh_token"],
        "expires_in": j.get("expires_in", 3600),
    }


# ============ 第2步：XBL 认证 ============
def _auth_xbl(ms_access_token: str) -> dict:
    body = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={ms_access_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    with get_sync_client() as c:
        r = c.post(XBL_AUTH, json=body, headers={"Accept": "application/json"})
        if r.status_code == 400:
            # 极少数情况要去掉 d= 重试
            body["Properties"]["RpsTicket"] = ms_access_token
            r = c.post(XBL_AUTH, json=body, headers={"Accept": "application/json"})
        r.raise_for_status()
        j = r.json()
    return {"token": j["Token"], "uhs": j["DisplayClaims"]["xui"][0]["uhs"]}


# ============ 第3步：XSTS 认证 ============
def _auth_xsts(xbl_token: str) -> dict:
    body = {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "rp://api.minecraftservices.com/",  # 末尾斜杠必须有
        "TokenType": "JWT",
    }
    with get_sync_client() as c:
        r = c.post(XSTS_AUTH, json=body, headers={"Accept": "application/json"})
        r.raise_for_status()
        j = r.json()
    return {"token": j["Token"], "uhs": j["DisplayClaims"]["xui"][0]["uhs"]}


# ============ 第4步：MC 认证 ============
def _auth_minecraft(uhs: str, xsts_token: str) -> dict:
    body = {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}
    with get_sync_client() as c:
        r = c.post(MC_AUTH, json=body)
        r.raise_for_status()
        j = r.json()
    return {"access_token": j["access_token"], "expires_in": j.get("expires_in", 86400)}


# ============ 第5步：检查游戏所有权 ============
def _check_entitlements(mc_access_token: str) -> bool:
    with get_sync_client() as c:
        r = c.get(MC_STORE, headers={"Authorization": f"Bearer {mc_access_token}"})
    if r.status_code == 404:
        return False
    if r.status_code >= 400:
        return False
    items = (r.json() or {}).get("items", [])
    return bool(items)


# ============ 第6步：取玩家档案 ============
def _get_profile(mc_access_token: str) -> Optional[dict]:
    with get_sync_client() as c:
        r = c.get(MC_PROFILE, headers={"Authorization": f"Bearer {mc_access_token}"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    j = r.json()
    return {"name": j.get("name", ""), "uuid": j.get("id", "")}


# ============ 完整链路：首次浏览器登录 ============
def login_with_browser() -> MsaCredentials:
    """首次登录：打开浏览器让用户登录微软账号，返回完整凭据。

    抛 RuntimeError 表示登录失败（用户取消/未购买游戏/网络错误等）。
    """
    url = _get_auth_url()
    if not webbrowser.open(url):
        # 打不开浏览器时把 URL 返回给调用方展示
        raise RuntimeError(f"无法打开浏览器，请手动访问此 URL 登录：\n{url}")

    code, err, err_desc = _wait_for_code()
    if err or not code:
        # error_description 通常含可读的错误说明（如 AADSTS 错误码 + 描述）
        desc = err_desc or err or "未知错误"
        raise RuntimeError(f"微软账号授权失败：{desc}")

    # 1. MSA token
    ms = _exchange_code_for_ms_token(code)
    # 2→3→4. XBL → XSTS → MC
    xbl = _auth_xbl(ms["access_token"])
    xsts = _auth_xsts(xbl["token"])
    mc = _auth_minecraft(xsts["uhs"], xsts["token"])
    # 5. 所有权
    if not _check_entitlements(mc["access_token"]):
        raise RuntimeError("该微软账号未购买 Minecraft，无法登录")
    # 6. 档案
    profile = _get_profile(mc["access_token"])
    if not profile:
        raise RuntimeError("无法获取 Minecraft 玩家档案（可能未创建档案）")

    return MsaCredentials(
        username=profile["name"],
        uuid=profile["uuid"],
        mc_access_token=mc["access_token"],
        ms_refresh_token=ms["refresh_token"],
        mc_expires_at=time.time() + mc["expires_in"],
    )


# ============ 完整链路：用 refresh_token 静默续登 ============
def relogin_with_refresh_token(refresh_token: str) -> MsaCredentials:
    """已有 refresh_token 时无需浏览器，静默换新 token 并重跑 XBL→XSTS→MC。

    抛 RuntimeError 表示续登失败（refresh_token 失效需重新浏览器登录等）。
    """
    # 7. 刷新 MSA token
    ms = _refresh_ms_token(refresh_token)
    # 2→3→4. XBL → XSTS → MC
    xbl = _auth_xbl(ms["access_token"])
    xsts = _auth_xsts(xbl["token"])
    mc = _auth_minecraft(xsts["uhs"], xsts["token"])
    # 5. 所有权
    if not _check_entitlements(mc["access_token"]):
        raise RuntimeError("该微软账号未购买 Minecraft，无法登录")
    # 6. 档案
    profile = _get_profile(mc["access_token"])
    if not profile:
        raise RuntimeError("无法获取 Minecraft 玩家档案")

    return MsaCredentials(
        username=profile["name"],
        uuid=profile["uuid"],
        mc_access_token=mc["access_token"],
        ms_refresh_token=ms["refresh_token"],  # 新的 refresh_token
        mc_expires_at=time.time() + mc["expires_in"],
    )


def ensure_valid_credentials(creds: MsaCredentials) -> MsaCredentials:
    """确保 MC token 有效，过期则用 refresh_token 静默续登。

    返回有效凭据（可能是传入的，也可能是续登后的新凭据）。
    refresh_token 失效时抛 RuntimeError，调用方应提示用户重新浏览器登录。
    """
    if creds.is_mc_token_valid:
        return creds
    return relogin_with_refresh_token(creds.ms_refresh_token)
