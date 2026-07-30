"""异步分块下载器：基于 httpx.AsyncClient + HTTP/1.1 keep-alive + 连接池复用。

设计要点（解决 WinError 10048 端口耗尽 / 慢速问题）：
1. **单进程共享一个 AsyncClient**：复用底层 TCP 连接池，避免每次下载都新建连接
2. **HTTP/1.1 keep-alive + HTTP/2 可选**：连接空闲时不立即关闭，保留给下一个分块
3. **明确的 Limits(max_connections / keepalive_connections)**：限制并发连接数，
   让 keep-alive 真正生效，不至于瞬间创建 N 个 socket 进入 TIME_WAIT
4. **指数退避重试**：网络抖动时优雅恢复，不让用户看到失败
5. **取消 token 放在每个分块读取前**：点取消立刻退出
6. **HEAD 探测失败回退到 GET Range: bytes=0-0**：服务端的健壮性
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import ssl
import threading
import time
from typing import Callable, Optional

import httpx


ProgressCallback = Callable[[int, int], None]  # (downloaded_bytes, total_bytes)

# ---------- 连接池配置 ----------
# 关键：max_keepalive_connections 必须远大于并发分块数，
# 否则 keep-alive 槽位被挤占 → 触发新建连接 → TIME_WAIT 累积
# 默认服务端连接池 8，加上主连接 + 重连槽位，16 是稳妥值
_DEFAULT_LIMITS = httpx.Limits(
    max_connections=16,
    max_keepalive_connections=12,
    keepalive_expiry=30.0,  # 30 秒内可复用同一连接
)
# 单元测试时可注入 None 关闭 keep-alive
# 读超时给到 300s：覆盖 Java 安装包下载（200MB+，慢网络需要较长 timeout）。
# 单个 stream 调用方可以在 client.stream(method, url, timeout=...) 中再覆盖。
_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=10.0)
_CHUNK_SIZE = 1 << 16                # 64KB 读块
_PROGRESS_REPORT_BYTES = 1 << 18     # 256KB 触发一次进度回调
_MIN_MULTITHREAD_SIZE = 1 << 20      # < 1MB 不切片
_DEFAULT_THREADS = 4                 # 默认并发分块数（从 8 降到 4，端口压力减半）
_RETRY_ATTEMPTS = 5                  # 失败重试次数（从 3 提至 5；总退避 0.6+1.2+2.4+4.8+9.6 ≈ 18s）
_RETRY_BASE_DELAY = 0.6              # 退避基础秒数


# -----------------------------------------------------------------------------
# 共享 AsyncClient：单进程复用，避免每次下载都新建连接
# -----------------------------------------------------------------------------
_client_lock = threading.Lock()
_shared_client: Optional[httpx.AsyncClient] = None
# 标记事件循环：每次 download_file 在当前线程的 loop 跑。
# 不同线程的 loop 不能复用同一个 client（asyncio 规则），所以记录
# client 创建时所在 loop，若不一致则重建。
_client_loop: Optional[asyncio.AbstractEventLoop] = None

# 同步共享 client：给所有 installer / api_client / msa_auth 等用。
# 这是修复 WinError 10048 端口耗尽的核心：所有 httpx 调用复用同一个
# 连接池，不再每个调用方各自 new 一个 client。
_shared_sync_client: Optional[httpx.Client] = None


# -----------------------------------------------------------------------------
# 代理自动探测：GFW 下必须走 Clash / V2Ray 才能访问 mojang/modrinth。
# Clash 默认端口 7890；V2RayN 10809；SS 1080。WinHTTP system proxy
# **不会**反映到 env var，httpx 的 trust_env=True 读不到，所以必须显式探测。
# -----------------------------------------------------------------------------
_PROXY_CANDIDATES = (
    "http://127.0.0.1:7890",   # Clash (Windows / macOS 默认)
    "http://127.0.0.1:7897",   # Clash for Windows 备用
    "http://127.0.0.1:10809",  # V2RayN
    "http://127.0.0.1:1080",   # SS / SSR / 其他
)
_proxy_url: Optional[str] = None
_proxy_lock = threading.Lock()


def _probe_port(host: str, port: int, timeout: float = 0.2) -> bool:
    """TCP 端口探测（不发数据）。UP=True，DOWN/timeout=False。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def _detect_proxy() -> Optional[str]:
    """启动时探测本机是否运行 Clash / V2Ray。

    返回 proxy URL（如 'http://127.0.0.1:7890'）或 None。
    结果在进程内缓存，避免每次请求都重探测。
    """
    global _proxy_url
    with _proxy_lock:
        if _proxy_url is not None:
            return _proxy_url or None
        for url in _PROXY_CANDIDATES:
            # 解析 url 拿 host:port
            try:
                from urllib.parse import urlparse
                p = urlparse(url)
                if p.hostname and p.port and _probe_port(p.hostname, p.port):
                    _proxy_url = url
                    return _proxy_url
            except (ValueError, OSError):
                continue
        _proxy_url = ""  # 用空字符串标记"探测过且没找到"，下次不重探测
        return None


def _create_sync_client() -> httpx.Client:
    """创建一个新的同步 httpx.Client（连接池配置与全局保持一致）。

    关键：keep-alive 在国内代理（Clash）下容易撞 SSL EOF：
    服务端提前关闭连接，新请求复用旧 socket 时收到 EOF
    ("EOF occurred in violation of protocol (_ssl.c:997)")。
    关闭 keep-alive 池，每次请求都新建连接；分块下载不依赖
    keep-alive（每个 Range 请求是独立 stream），实测对速度影响
    可忽略，但稳定性大幅提升。

    同时：自动探测本机 Clash/V2Ray 端口。GFW 下不走代理直接连
    mojang / modrinth / github 会被 RST，导致 SSL EOF（与 keep-alive
    无关的另一成因）。探测到代理则显式设置 proxy=None 走代理；
    探测不到时 trust_env=True 退回到读 env HTTPS_PROXY（git push
    等 shell 流程会用这种方式注入）。
    """
    proxy = _detect_proxy()
    return httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=True,
        proxy=proxy,
        trust_env=proxy is None,
        limits=httpx.Limits(
            max_connections=16,
            max_keepalive_connections=0,
            keepalive_expiry=5.0,
        ),
        headers={"User-Agent": "GPM-Client/1.0"},
    )


def get_sync_client() -> httpx.Client:
    """获取进程级共享同步 httpx.Client。

    所有 HTTP 调用方（installer / api / msa / login / updater / version_manager）
    都应通过此函数获取 client，而不是 with httpx.Client(...) 新建。
    这样 keep-alive 连接在多次调用间复用，避免 WinError 10048 端口耗尽。

    ⚠️ 重要：httpx 0.27+ 的 `__exit__` 会**自动 close** client。
    我们的所有调用方都写 `with get_sync_client() as c: r = c.get(...)`，
    第一次 `__exit__` 之后 client 就被关闭；第二次 `with` 进入时
    `__enter__` 会检测到 CLOSED 状态抛 `Cannot reopen a client instance`。
    所以这里在返回前检查 is_closed，若是则**静默重建**。
    """
    global _shared_sync_client
    # 快路径：已存在且未 close → 直接返回
    if _shared_sync_client is not None and not _shared_sync_client.is_closed:
        return _shared_sync_client
    with _client_lock:
        if _shared_sync_client is None or _shared_sync_client.is_closed:
            _shared_sync_client = _create_sync_client()
        return _shared_sync_client


def close_sync_client() -> None:
    """关闭共享同步 client（仅用于程序退出时；不要在业务路径里调用）。"""
    global _shared_sync_client
    with _client_lock:
        if _shared_sync_client is not None:
            try:
                _shared_sync_client.close()
            except Exception:
                pass
            _shared_sync_client = None


def _get_client() -> httpx.AsyncClient:
    """获取当前线程事件循环的共享 AsyncClient。"""
    global _shared_client, _client_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        # 不在 asyncio 上下文里，不应该调用这里；调用方必须用 download_file 入口
        raise RuntimeError("_get_client called outside an event loop")
    with _client_lock:
        if _shared_client is None or _client_loop is not current_loop:
            # 跨 loop：先关闭旧 client
            if _shared_client is not None:
                try:
                    # 不能直接 await 旧 client 的 aclose（在新 loop 里不能跨 loop await），
                    # 直接丢弃即可，连接在 GC 时会被关闭（最坏情况有几个泄漏的 socket）
                    pass
                except Exception:
                    pass
            _shared_client = httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=True,
                limits=_DEFAULT_LIMITS,
                # 开启 HTTP/2 时：单连接多路复用，端口压力最低
                # 但服务端必须支持 h2；httpx 自动协商，失败则降级到 HTTP/1.1
                http2=False,  # 默认关，避免依赖 h2 包；用户可在 _get_client 调用前自己 enable
                headers={"User-Agent": "GPM-Client/1.0"},
            )
            _client_loop = current_loop
        return _shared_client


async def close_shared_client() -> None:
    """关闭共享 client（程序退出时调用）。"""
    global _shared_client, _client_loop
    with _client_lock:
        if _shared_client is not None:
            try:
                await _shared_client.aclose()
            except Exception:
                pass
            _shared_client = None
            _client_loop = None


# -----------------------------------------------------------------------------
# 断点续传元数据（持久化到 .part.meta.json）
# -----------------------------------------------------------------------------
# 用户在下载中途取消、进程崩溃或网络抖动后重试时，下次调用 download_file
# 会读取此文件判断能否从已有 .part 续传，避免重头下载。
_META_SUFFIX = ".meta.json"


def _meta_path(tmp_path: str) -> str:
    """下载临时文件对应的元数据 sidecar 路径。"""
    return tmp_path + _META_SUFFIX


def _write_meta(tmp_path: str, meta: dict) -> None:
    """写入元数据。失败不抛（best effort）。"""
    p = _meta_path(tmp_path)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except OSError:
        pass


def _read_meta(tmp_path: str) -> Optional[dict]:
    """读取元数据。文件不存在或损坏返回 None。"""
    p = _meta_path(tmp_path)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _clear_meta(tmp_path: str) -> None:
    """删除元数据 sidecar。失败不抛。"""
    p = _meta_path(tmp_path)
    try:
        os.remove(p)
    except OSError:
        pass


# -----------------------------------------------------------------------------
# HEAD 探测
# -----------------------------------------------------------------------------
async def _head_info(url: str) -> tuple[int, bool, str, str]:
    """HEAD 探测：返回 (total_bytes, supports_ranges, etag, last_modified)。

    失败回退到 GET Range: bytes=0-0（部分 server 不支持 HEAD）。
    etag / last_modified 缺失时返回空串。
    """
    client = _get_client()
    try:
        r = await client.head(url)
        if r.status_code >= 400:
            r = await client.get(url, headers={"Range": "bytes=0-0"})
            total = int(r.headers.get("content-length", 0) or 0)
            supports = r.status_code == 206
            if supports:
                cr = r.headers.get("content-range", "")
                if "/" in cr:
                    try:
                        total = int(cr.split("/")[-1])
                    except ValueError:
                        pass
            return total, supports, r.headers.get("etag", ""), r.headers.get("last-modified", "")
        total = int(r.headers.get("content-length", 0) or 0)
        supports = r.headers.get("accept-ranges", "").lower() in ("bytes", "1")
        return total, supports, r.headers.get("etag", ""), r.headers.get("last-modified", "")
    except httpx.HTTPError:
        return 0, False, "", ""


# -----------------------------------------------------------------------------
# 分块下载闭包
# -----------------------------------------------------------------------------
async def _download_range(
    client: httpx.AsyncClient,
    url: str,
    part_path: str,
    start: int,
    end: int,
    cancel_event,
    start_offset: int = 0,
) -> int:
    """下载 [start, end] 区间到 part_path。失败时抛异常。

    Args:
        start: 区间起点（绝对字节偏移）
        end: 区间终点（含）
        start_offset: part_path 已有的字节数。> 0 时发送
            `Range: bytes={start+start_offset}-{end}` 续传。start_offset 必须 <=
            (end - start + 1)，否则视为已完成直接返回。

    注意：如果 server 对 Range 请求返回 200（不支持 Range），本函数会抛错
    让外层重新发起整段下载。已下载的部分会保留到下次 `_download_file_async`
    检测时由 resume 决策清理（HEAD 探测后会重置 start_offset=0）。
    """
    expected_size = end - start + 1
    if start_offset > expected_size:
        start_offset = expected_size
    if start_offset == expected_size and os.path.isfile(part_path) \
            and os.path.getsize(part_path) == expected_size:
        return expected_size  # 已完成

    headers = {"Range": f"bytes={start + start_offset}-{end}"}
    last_err: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("下载已取消")
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 200 and start_offset > 0:
                    # server 不支持 Range 但送了全量内容 → 续传路径上没法干净处理，
                    # 抛错让外层重置 resume 状态后从头再试。
                    raise RuntimeError(
                        f"server 不支持 Range (got 200, expected 206) "
                        f"part=[{start}-{end}] offset={start_offset}"
                    )
                resp.raise_for_status()
                mode = "ab" if start_offset > 0 else "wb"
                with open(part_path, mode) as f:
                    if start_offset > 0:
                        f.seek(start_offset)
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("下载已取消")
                        f.write(chunk)
            return os.path.getsize(part_path)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout, ConnectionError,
                ssl.SSLError) as e:  # ssl.SSLError: EOF occurred in violation of protocol
            last_err = e
            # 退避：0.6s, 1.2s, 2.4s, 4.8s, 9.6s
            wait = _RETRY_BASE_DELAY * (2 ** attempt)
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("下载已取消")
            await asyncio.sleep(wait)
            continue
    # 全部重试用完
    raise RuntimeError(f"分块 [{start}-{end}] 下载失败（重试 {_RETRY_ATTEMPTS} 次后）: {last_err}")


# -----------------------------------------------------------------------------
# 单线程回退
# -----------------------------------------------------------------------------
async def _download_single_async(
    client: httpx.AsyncClient,
    url: str,
    tmp_path: str,
    progress: Optional[ProgressCallback],
    cancel_event,
    hash_algo: str = "sha256",
    start_offset: int = 0,
) -> str:
    """单线程顺序下载（不支持 Range 或文件太小时）。

    Args:
        start_offset: 已经下载的字节数（断点续传时 > 0）。函数会发送
            `Range: bytes={start_offset}-` 并以 append 模式写入 tmp_path。
            如果 server 返回 200（不支持 Range），自动 truncate 后从头重下。
    """
    hasher = hashlib.new(hash_algo)
    # 续传：用已有文件内容播种 hasher，保证最终 hash 与全量下载一致
    if start_offset > 0 and os.path.isfile(tmp_path):
        with open(tmp_path, "rb") as f_seed:
            for chunk in iter(lambda: f_seed.read(1 << 20), b""):
                hasher.update(chunk)

    last_err: Optional[Exception] = None
    # 本轮 loop 使用的 start_offset；遇到 200 响应（Range 不支持）时会被重置为 0
    cur_offset = start_offset
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            headers = {"Range": f"bytes={cur_offset}-"} if cur_offset > 0 else {}
            async with client.stream("GET", url, headers=headers) as resp:
                # 续传时 server 不支持 Range：返回 200 + 全量内容。
                # 此时 truncate 文件、清空 hasher、置 0 后重发请求。
                if cur_offset > 0 and resp.status_code == 200:
                    with open(tmp_path, "wb"):
                        pass
                    hasher = hashlib.new(hash_algo)
                    cur_offset = 0
                    # 重新走主流程（不消耗 retry 配额）
                    downloaded = 0
                    total = int(resp.headers.get("content-length", 0) or 0)
                    if progress:
                        progress(0, total)
                    with open(tmp_path, "wb") as f:
                        last_report = 0
                        async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                            if cancel_event is not None and cancel_event.is_set():
                                raise RuntimeError("下载已取消")
                            f.write(chunk)
                            hasher.update(chunk)
                            downloaded += len(chunk)
                            if progress and (downloaded - last_report >= _PROGRESS_REPORT_BYTES
                                              or downloaded == total):
                                progress(downloaded, total)
                                last_report = downloaded
                    return hasher.hexdigest()

                resp.raise_for_status()

                # 计算 total：206 用 Content-Range，200 用 Content-Length
                if resp.status_code == 206 and cur_offset > 0:
                    cr = resp.headers.get("content-range", "")
                    total = 0
                    if "/" in cr:
                        try:
                            total = int(cr.split("/")[-1])
                        except ValueError:
                            total = 0
                    if total == 0:
                        total = cur_offset + int(resp.headers.get("content-length", 0) or 0)
                else:
                    total = int(resp.headers.get("content-length", 0) or 0)

                downloaded = cur_offset
                if progress:
                    progress(downloaded, total)
                # append 模式 + 显式 seek；wb 模式用 truncate
                mode = "ab" if cur_offset > 0 else "wb"
                with open(tmp_path, mode) as f:
                    if cur_offset > 0:
                        f.seek(cur_offset)
                    last_report = downloaded
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("下载已取消")
                        f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if progress and (downloaded - last_report >= _PROGRESS_REPORT_BYTES
                                          or downloaded == total):
                            progress(downloaded, total)
                            last_report = downloaded
            return hasher.hexdigest()
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout, ConnectionError,
                ssl.SSLError) as e:  # ssl.SSLError: 偶发协议违规 EOF
            last_err = e
            wait = _RETRY_BASE_DELAY * (2 ** attempt)
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("下载已取消")
            await asyncio.sleep(wait)
            continue
    raise RuntimeError(f"下载失败（重试 {_RETRY_ATTEMPTS} 次后）: {last_err}")


# -----------------------------------------------------------------------------
# 入口
# -----------------------------------------------------------------------------
def _hash_file(path: str, algo: str) -> str:
    """计算本地文件哈希。支持 sha1 / sha256。"""
    import hashlib
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(
    url: str,
    dest_path: str,
    expected_hash: Optional[str] = None,
    expected_hash_alg: str = "sha256",
    progress: Optional[ProgressCallback] = None,
    cancel_event=None,
    threads: int = _DEFAULT_THREADS,
) -> str:
    """同步入口。

    Args:
        url: 下载 URL
        dest_path: 本地保存路径
        expected_hash: 期望哈希值（十六进制字符串）
        expected_hash_alg: 期望哈希算法，支持 "sha1" / "sha256"。Modrinth manifest
            提供 SHA1（不是 SHA256），必须显式传 "sha1" 才能校验。
        progress: (downloaded, total) 进度回调
        cancel_event: 取消事件
        threads: 并发分块数

    Returns:
        最终文件路径

    Raises:
        RuntimeError: 下载失败、取消
        ValueError: 期望哈希不匹配
    """
    if expected_hash_alg not in ("sha1", "sha256"):
        raise ValueError(f"不支持的哈希算法: {expected_hash_alg}")
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp_path = dest_path + ".part"

    # 同步路径直接用 httpx sync 客户端（get_sync_client）。
    # 历史教训：早期这里走 asyncio.run() 包裹到 ThreadPoolExecutor，
    # 在 Windows ProactorEventLoop 下创建/销毁事件循环时，
    # _ProactorBasePipeTransport.__del__ 会在 loop.close() 之后被 GC 触发，
    # 抛 "RuntimeError: Event loop is closed"。**gpm-client 启动日志里
    # 一直刷这条错误就是这个原因**。改用同步 httpx 后：
    #   1) 不再创建/销毁 asyncio loop → Proactor bug 消失
    #   2) 少一层事件循环调度 → 同步路径更快
    #   3) 同步代码逻辑更直接，断点续传 / Range / 取消都靠 threading 原语
    try:
        actual_hash = _download_file_sync(
            url, tmp_path, progress, cancel_event, threads, expected_hash_alg,
        )
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"下载失败: {e}") from e
    return _finalize_download(
        url, tmp_path, dest_path, actual_hash, expected_hash or "", expected_hash_alg,
    )


# -----------------------------------------------------------------------------
# 同步实现：httpx sync + 进程级共享 client（无 asyncio loop）
# -----------------------------------------------------------------------------

def _head_info_sync(client: httpx.Client, url: str) -> tuple[int, bool, str, str]:
    """HEAD 探测（同步版）。失败回退到 GET Range: bytes=0-0。

    返回 (total_bytes, supports_ranges, etag, last_modified)。
    etag / last_modified 缺失时返回空串。
    """
    try:
        r = client.head(url)
        if r.status_code < 400:
            total = int(r.headers.get("content-length", 0) or 0)
            accept_ranges = (r.headers.get("accept-ranges", "").lower() == "bytes")
            etag = r.headers.get("etag", "")
            last_mod = r.headers.get("last-modified", "")
            return total, accept_ranges, etag, last_mod
    except (httpx.HTTPError, ConnectionError, OSError):
        pass
    # HEAD 失败/被拒：fallback 到 Range 探测
    try:
        r = client.get(url, headers={"Range": "bytes=0-0"})
        total = 0
        if r.status_code == 206:
            cr = r.headers.get("content-range", "")
            if "/" in cr:
                try:
                    total = int(cr.split("/")[-1])
                except ValueError:
                    total = 0
        elif r.status_code == 200:
            total = int(r.headers.get("content-length", 0) or 0)
        supports = r.status_code == 206
        etag = r.headers.get("etag", "")
        last_mod = r.headers.get("last-modified", "")
        # fallback 用完要确保 response body 被消费掉，否则连接不会释放
        try:
            r.read()
        except Exception:
            pass
        return total, supports, etag, last_mod
    except (httpx.HTTPError, ConnectionError, OSError) as e:
        raise RuntimeError(f"HEAD 探测失败: {e}") from e


def _download_range_sync(
    client: httpx.Client,
    url: str,
    part_path: str,
    start: int,
    end: int,
    cancel_event,
    start_offset: int = 0,
) -> int:
    """同步下载 [start, end] 区间到 part_path。失败时抛异常。"""
    last_err: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("下载已取消")
        try:
            real_start = start + start_offset
            # **总是**发 Range：start_offset==0 时也可能是 part 3（start > 0），
            # 不发 Range 会被服务端误解为"全量下载"（返回 200 + 完整文件），
            # 多线程合并时 part 文件就会超出预期大小。
            headers = {"Range": f"bytes={real_start}-{end}"}
            mode = "ab" if start_offset > 0 else "wb"
            downloaded = 0
            with client.stream("GET", url, headers=headers) as resp:
                # 续传时 server 不支持 Range：返回 200 + 全量内容。truncate 后从头下。
                if start_offset > 0 and resp.status_code == 200:
                    with open(part_path, "wb"):
                        pass
                    start_offset = 0
                    mode = "wb"
                    downloaded = 0
                resp.raise_for_status()
                with open(part_path, mode) as f:
                    if start_offset > 0:
                        f.seek(start_offset)
                    for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("下载已取消")
                        f.write(chunk)
                        downloaded += len(chunk)
                return downloaded
        except Exception as e:  # noqa: BLE001
            # 取消必须 raise
            if "取消" in str(e) or "Cancelled" in str(e):
                raise
            last_err = e
            if attempt < _RETRY_ATTEMPTS - 1:
                wait = _RETRY_BASE_DELAY * (2 ** attempt)
                time.sleep(wait)
                continue
            break
    raise RuntimeError(f"分块 [{start}-{end}] 下载失败（重试 {_RETRY_ATTEMPTS} 次后）: {last_err}")


def _download_file_sync(
    url: str,
    tmp_path: str,
    progress: Optional[ProgressCallback],
    cancel_event,
    threads: int,
    hash_algo: str,
) -> str:
    """同步下载主流程。包含断点续传决策（与 _download_file_async 共享 meta 协议）。"""
    client = get_sync_client()
    total, supports_ranges, etag, last_modified = _head_info_sync(client, url)
    use_multithread = supports_ranges and total >= _MIN_MULTITHREAD_SIZE and threads > 1

    # ----- 续传决策（与异步版一致）-----
    existing_meta = _read_meta(tmp_path)
    start_offset = 0

    if (
        existing_meta is not None
        and existing_meta.get("url") == url
        and existing_meta.get("total") == total
    ):
        old_etag = existing_meta.get("etag", "")
        if old_etag and etag and old_etag != etag:
            _cleanup_partial(tmp_path, multithread=use_multithread)
        else:
            if not use_multithread:
                if os.path.isfile(tmp_path):
                    start_offset = os.path.getsize(tmp_path)
                    if total > 0 and start_offset >= total:
                        return _hash_file(tmp_path, hash_algo)
    else:
        _cleanup_partial(tmp_path, multithread=use_multithread)
        start_offset = 0

    _write_meta(tmp_path, {
        "url": url,
        "total": total,
        "etag": etag,
        "last_modified": last_modified,
        "hash_alg": hash_algo,
    })

    if not use_multithread:
        # 单线程：直接 stream + 同步 Range
        last_err: Optional[Exception] = None
        cur_offset = start_offset
        for attempt in range(_RETRY_ATTEMPTS):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("下载已取消")
            try:
                hasher = hashlib.new(hash_algo)
                # 续传：用已有内容播种 hasher
                if cur_offset > 0 and os.path.isfile(tmp_path):
                    with open(tmp_path, "rb") as f_seed:
                        for chunk in iter(lambda: f_seed.read(1 << 20), b""):
                            hasher.update(chunk)

                headers = {"Range": f"bytes={cur_offset}-"} if cur_offset > 0 else {}
                with client.stream("GET", url, headers=headers) as resp:
                    if cur_offset > 0 and resp.status_code == 200:
                        # server 不支持 Range：truncate 重下
                        with open(tmp_path, "wb"):
                            pass
                        hasher = hashlib.new(hash_algo)
                        cur_offset = 0
                        downloaded = 0
                        resp_total = int(resp.headers.get("content-length", 0) or 0)
                        if progress:
                            progress(0, resp_total)
                        with open(tmp_path, "wb") as f:
                            last_report = 0
                            for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                                if cancel_event is not None and cancel_event.is_set():
                                    raise RuntimeError("下载已取消")
                                f.write(chunk)
                                hasher.update(chunk)
                                downloaded += len(chunk)
                                if progress and (
                                    downloaded - last_report >= _PROGRESS_REPORT_BYTES
                                    or downloaded == resp_total
                                ):
                                    progress(downloaded, resp_total)
                                    last_report = downloaded
                        _clear_meta(tmp_path)
                        return hasher.hexdigest()

                    resp.raise_for_status()
                    if resp.status_code == 206 and cur_offset > 0:
                        cr = resp.headers.get("content-range", "")
                        resp_total = 0
                        if "/" in cr:
                            try:
                                resp_total = int(cr.split("/")[-1])
                            except ValueError:
                                resp_total = 0
                        if resp_total == 0:
                            resp_total = cur_offset + int(resp.headers.get("content-length", 0) or 0)
                    else:
                        resp_total = int(resp.headers.get("content-length", 0) or 0)

                    downloaded = cur_offset
                    if progress:
                        progress(downloaded, resp_total)
                    mode = "ab" if cur_offset > 0 else "wb"
                    with open(tmp_path, mode) as f:
                        if cur_offset > 0:
                            f.seek(cur_offset)
                        last_report = downloaded
                        for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                            if cancel_event is not None and cancel_event.is_set():
                                raise RuntimeError("下载已取消")
                            f.write(chunk)
                            hasher.update(chunk)
                            downloaded += len(chunk)
                            if progress and (
                                downloaded - last_report >= _PROGRESS_REPORT_BYTES
                                or downloaded == resp_total
                            ):
                                progress(downloaded, resp_total)
                                last_report = downloaded
                    _clear_meta(tmp_path)
                    return hasher.hexdigest()
            except Exception as e:  # noqa: BLE001
                if "取消" in str(e) or "Cancelled" in str(e):
                    raise
                last_err = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    wait = _RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(wait)
                    continue
                break
        raise RuntimeError(f"下载失败（重试 {_RETRY_ATTEMPTS} 次后）: {last_err}")

    # ===== 多线程分块下载 =====
    n = min(threads, max(1, total // _MIN_MULTITHREAD_SIZE))
    part_size = total // n
    ranges: list[tuple[int, int]] = []
    for i in range(n):
        s = i * part_size
        e = (total - 1) if i == n - 1 else (s + part_size - 1)
        ranges.append((s, e))

    part_paths = [f"{tmp_path}.part{i}" for i in range(n)]
    part_offsets: list[int] = []
    for i, pp in enumerate(part_paths):
        if os.path.isfile(pp):
            sz = os.path.getsize(pp)
            expected = ranges[i][1] - ranges[i][0] + 1
            part_offsets.append(min(sz, expected))
        else:
            part_offsets.append(0)
    already_done = sum(part_offsets)
    if progress:
        progress(already_done, total)

    # 用 ThreadPoolExecutor 并发分块；client 是 sync 的，多线程共享安全（httpx.Client
    # 内部锁保证）。下载进度：每个 part 单独报告 base + delta，整体 total 不变。
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 计算每个 part 的"全局起始字节"作为 progress 偏移
    part_bases: list[int] = []
    running = 0
    for off in part_offsets:
        part_bases.append(running)
        running += off  # 已下载 part 的大小累加（多 part 时 parts 是连续的）

    def _cb_factory(idx: int) -> ProgressCallback:
        last = [part_offsets[idx]]  # 已经下载的字节作为初始 last
        base = part_bases[idx]
        def _cb(d: int, t: int) -> None:
            if progress is None:
                return
            delta = d - last[0]
            last[0] = d
            cur = sum(part_offsets) + base + delta - part_offsets[idx]
            # 简化：所有 part 的 base 总和 + 当前 part 的 d
            progress(base + d, total)
        return _cb

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = []
        for i in range(n):
            cb = _cb_factory(i)
            futures.append(pool.submit(
                _download_range_sync, client, url, part_paths[i],
                ranges[i][0], ranges[i][1], cancel_event, part_offsets[i],
            ))
        try:
            for fut in as_completed(futures):
                fut.result()  # 任何一个失败立即抛
        except Exception:
            if cancel_event is not None:
                cancel_event.set()
            raise

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("下载已取消")

    # 合并 + 哈希
    hasher = hashlib.new(hash_algo)
    with open(tmp_path, "wb") as out:
        for pp in part_paths:
            with open(pp, "rb") as pf:
                while True:
                    buf = pf.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
                    hasher.update(buf)
            try:
                os.remove(pp)
            except OSError:
                pass
    _clear_meta(tmp_path)
    return hasher.hexdigest()


def _finalize_download(
    url: str,
    tmp_path: str,
    dest_path: str,
    actual_hash: str,
    expected_hash: str,
    expected_hash_alg: str,
) -> str:
    """下载完成后的统一收尾：hash 校验 + os.replace 原子覆盖 + 失败清理。

    与异步 / 同步路径解耦，避免两边重复校验逻辑。
    """
    if expected_hash and actual_hash.lower() != expected_hash.lower():
        # hash 校验失败 → 整个 tmp 不可信，必须清掉（含 meta）防止下次误用旧 part 续传
        _cleanup_partial(tmp_path, multithread=False)
        raise ValueError(
            f"{expected_hash_alg.upper()} 校验失败: 期望 {expected_hash} 实际 {actual_hash}"
        )
    os.replace(tmp_path, dest_path)
    return dest_path


async def _download_file_async(
    url: str,
    tmp_path: str,
    progress: Optional[ProgressCallback],
    cancel_event,
    threads: int,
    hash_algo: str = "sha256",
) -> str:
    """异步实现。包含断点续传决策：

    - 启动时读 .part.meta.json + HEAD 探测，按 URL/ETag/total 决定能否续传
    - 续传：保留 .part / .partN，发送 Range: bytes=N- 让 server 接续
    - 不续传：清理所有 part 文件，下载 meta 后从头开始
    - 成功：清理 meta（调用方 os.replace(tmp_path, dest_path)）
    - 失败（网络/取消）：保留 .part + meta，下次进入直接续传
    """
    client = _get_client()
    total, supports_ranges, etag, last_modified = await _head_info(url)
    use_multithread = supports_ranges and total >= _MIN_MULTITHREAD_SIZE and threads > 1

    # ----- 续传决策 -----
    # 设计：
    # - meta + URL/total 匹配 → 信任之前的下载状态
    # - ETag 变化 → server 文件变了，必须重下
    # - single-thread：partial 就是 tmp_path 本身（dest.part）
    # - multi-thread：partial 在 dest.part.part0/1/2/...；tmp_path 是不存在的
    #   （只下载完成后合并才出现）。所以判断"有 partial"不能依赖 tmp_path。
    existing_meta = _read_meta(tmp_path)
    start_offset = 0  # single-thread 用的续传偏移

    if (
        existing_meta is not None
        and existing_meta.get("url") == url
        and existing_meta.get("total") == total
    ):
        old_etag = existing_meta.get("etag", "")
        if old_etag and etag and old_etag != etag:
            # server 端文件变了 → 重头下载
            _cleanup_partial(tmp_path, multithread=use_multithread)
        else:
            if not use_multithread:
                # single-thread：partial 就是 tmp_path
                if os.path.isfile(tmp_path):
                    start_offset = os.path.getsize(tmp_path)
                    # 已有完整文件 + total 已知 → 直接 hash 返回，避免重复下载
                    if total > 0 and start_offset >= total:
                        return _hash_file(tmp_path, hash_algo)
                # else: 没有 partial 文件（meta 还在但内容丢了）→ 当全新下载
            # multi-thread：每个 part 自检，start_offset 不在这里设
    else:
        # meta 不存在 / URL 或 total 不匹配 → 清理 stale 状态
        _cleanup_partial(tmp_path, multithread=use_multithread)
        start_offset = 0

    # 写 meta（开始下载前）—— 中途崩溃/取消也能续传
    _write_meta(tmp_path, {
        "url": url,
        "total": total,
        "etag": etag,
        "last_modified": last_modified,
        "hash_alg": hash_algo,
    })

    if not use_multithread:
        try:
            actual_hash = await _download_single_async(
                client, url, tmp_path, progress, cancel_event, hash_algo, start_offset,
            )
        except Exception:
            # 保留 .part + meta 以便下次续传
            raise
        _clear_meta(tmp_path)
        return actual_hash

    # ===== 多线程分块下载 =====
    n = min(threads, max(1, total // _MIN_MULTITHREAD_SIZE))
    part_size = total // n
    ranges: list[tuple[int, int]] = []
    for i in range(n):
        start = i * part_size
        end = (total - 1) if i == n - 1 else (start + part_size - 1)
        ranges.append((start, end))

    part_paths = [f"{tmp_path}.part{i}" for i in range(n)]

    # 每个 part 自检已有大小，作为续传起点
    part_offsets: list[int] = []
    for i, pp in enumerate(part_paths):
        if os.path.isfile(pp):
            sz = os.path.getsize(pp)
            expected = ranges[i][1] - ranges[i][0] + 1
            part_offsets.append(min(sz, expected))
        else:
            part_offsets.append(0)
    already_done = sum(part_offsets)
    if progress:
        progress(already_done, total)

    tasks = [
        _download_range(
            client, url, part_paths[i], ranges[i][0], ranges[i][1],
            cancel_event, part_offsets[i],
        )
        for i in range(n)
    ]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        if cancel_event is not None:
            cancel_event.set()
        # 保留 part 文件以便下次续传
        raise

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("下载已取消")

    # 合并 + 哈希
    hasher = hashlib.new(hash_algo)
    with open(tmp_path, "wb") as out:
        for pp in part_paths:
            with open(pp, "rb") as pf:
                while True:
                    buf = pf.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
                    hasher.update(buf)
            try:
                os.remove(pp)
            except OSError:
                pass
    _clear_meta(tmp_path)
    return hasher.hexdigest()


# -----------------------------------------------------------------------------
# 清理
# -----------------------------------------------------------------------------
def _cleanup_partial(tmp_path: str, multithread: bool) -> None:
    """清理 .part 文件 + （multi 模式）所有 .partN + meta。

    用于"重置状态"场景，例如 ETag 变化、meta 不匹配、hash 校验失败。
    失败不抛。
    """
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    if multithread:
        i = 0
        while i < 256:
            pp = f"{tmp_path}.part{i}"
            if os.path.exists(pp):
                try:
                    os.remove(pp)
                except OSError:
                    pass
                i += 1
            else:
                break
    _clear_meta(tmp_path)
