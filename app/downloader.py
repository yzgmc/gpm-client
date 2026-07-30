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
import os
import socket
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
_RETRY_ATTEMPTS = 3                  # 失败重试次数
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


def _create_sync_client() -> httpx.Client:
    """创建一个新的同步 httpx.Client（连接池配置与全局保持一致）。

    关键：keep-alive 在国内代理（Clash）下容易撞 SSL EOF：
    服务端提前关闭连接，新请求复用旧 socket 时收到 EOF
    ("EOF occurred in violation of protocol (_ssl.c:997)")。
    关闭 keep-alive 池，每次请求都新建连接；分块下载不依赖
    keep-alive（每个 Range 请求是独立 stream），实测对速度影响
    可忽略，但稳定性大幅提升。
    """
    return httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=True,
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
# HEAD 探测
# -----------------------------------------------------------------------------
async def _head_info(url: str) -> tuple[int, bool]:
    """HEAD 探测：返回 (total_bytes, supports_ranges)。失败回退到 GET Range: bytes=0-0。"""
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
            return total, supports
        total = int(r.headers.get("content-length", 0) or 0)
        supports = r.headers.get("accept-ranges", "").lower() in ("bytes", "1")
        return total, supports
    except httpx.HTTPError:
        return 0, False


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
) -> int:
    """下载 [start, end] 区间到 part_path。失败时抛异常。"""
    headers = {"Range": f"bytes={start}-{end}"}
    last_err: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("下载已取消")
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                with open(part_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("下载已取消")
                        f.write(chunk)
            return os.path.getsize(part_path)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout, ConnectionError) as e:
            last_err = e
            # 退避：0.6s, 1.2s, 2.4s
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
) -> str:
    """单线程顺序下载（不支持 Range 或文件太小时）。"""
    hasher = hashlib.new(hash_algo)
    last_err: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0) or 0)
                downloaded = 0
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
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectTimeout, httpx.ReadTimeout, ConnectionError) as e:
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
    """同步入口：内部跑 asyncio loop。

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

    # 优先在已运行的事件循环里跑（PySide QThread 主循环场景），
    # 否则新开 loop。统一逻辑替换原来嵌套 try/except 的"猜"做法。
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    async def _run() -> str:
        return await _download_file_async(url, tmp_path, progress, cancel_event, threads, expected_hash_alg)

    if in_loop:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(_run()))
            actual_hash = future.result()
    else:
        actual_hash = asyncio.run(_run())

    if expected_hash and actual_hash.lower() != expected_hash.lower():
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
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
    """异步实现。"""
    client = _get_client()
    total, supports_ranges = await _head_info(url)
    use_multithread = supports_ranges and total >= _MIN_MULTITHREAD_SIZE and threads > 1

    if not use_multithread:
        return await _download_single_async(client, url, tmp_path, progress, cancel_event, hash_algo)

    # ===== 多线程分块下载 =====
    n = min(threads, max(1, total // _MIN_MULTITHREAD_SIZE))
    part_size = total // n
    ranges: list[tuple[int, int]] = []
    for i in range(n):
        start = i * part_size
        end = (total - 1) if i == n - 1 else (start + part_size - 1)
        ranges.append((start, end))

    part_paths = [f"{tmp_path}.part{i}" for i in range(n)]

    if progress:
        progress(0, total)

    # 用 asyncio.gather 跑多个分块，共享同一 client → 共享连接池
    tasks = [
        _download_range(client, url, part_paths[i], ranges[i][0], ranges[i][1], cancel_event)
        for i in range(n)
    ]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        if cancel_event is not None:
            cancel_event.set()
        _cleanup(tmp_path, multithread=True)
        raise

    if cancel_event is not None and cancel_event.is_set():
        _cleanup(tmp_path, multithread=True)
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
    return hasher.hexdigest()


# -----------------------------------------------------------------------------
# 清理
# -----------------------------------------------------------------------------
def _cleanup(tmp_path: str, multithread: bool) -> None:
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
