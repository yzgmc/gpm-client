"""多线程分块下载器：HTTP Range 并行下载，支持进度回调、取消与 sha256 校验。

策略：
1. HEAD 探测：拿到总大小 + 是否支持 Accept-Ranges。
2. 支持 Range 且文件够大：切成 N 块，多线程并行下载到 .partN，合并时流式哈希校验。
3. 不支持 Range 或文件太小：回退单线程顺序下载（与旧行为一致）。
取消检查放在每块读取前后，保证点取消能立刻退出。
"""

from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import httpx


ProgressCallback = Callable[[int, int], None]  # (downloaded_bytes, total_bytes)

# 多线程参数
_MIN_MULTITHREAD_SIZE = 1 << 20          # < 1MB 不值得切片，走单线程
_DEFAULT_THREADS = 8                     # 默认并发数
_CHUNK_SIZE = 1 << 16                     # 每次读取 64KB
# 连接 10s、读取 60s（多线程单块慢点没关系，但别无限卡）
_TIMEOUT = httpx.Timeout(10.0, read=60.0)


def _head_info(url: str) -> tuple[int, bool]:
    """HEAD 探测：返回 (total_bytes, supports_ranges)。失败时返回 (0, False)。"""
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as c:
            r = c.head(url)
            # 某些服务端不支持 HEAD，回退用 GET+stream 立即关闭
            if r.status_code >= 400:
                r = c.get(url, headers={"Range": "bytes=0-0"})
                total = int(r.headers.get("content-length", 0) or 0)
                # Range 请求返回 206 说明支持断点
                supports = r.status_code == 206
                if supports and r.headers.get("content-range"):
                    # content-range: bytes 0-0/12345
                    try:
                        total = int(r.headers["content-range"].split("/")[-1])
                    except (IndexError, ValueError):
                        pass
                return total, supports
            total = int(r.headers.get("content-length", 0) or 0)
            supports = r.headers.get("accept-ranges", "").lower() in ("bytes", "1")
            return total, supports
    except httpx.HTTPError:
        return 0, False


def _make_range_downloader(total: int, downloaded_counter: list[int], counter_lock: threading.Lock,
                           last_report: list[int], progress: Optional[ProgressCallback], cancel_event):
    """生成一个分块下载闭包，闭包内共享 total/计数器。"""
    def _dl(url: str, part_path: str, start: int, end: int) -> int:
        headers = {"Range": f"bytes={start}-{end}"}
        bytes_done = 0
        with httpx.stream("GET", url, headers=headers, timeout=_TIMEOUT, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(part_path, "wb") as f:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("下载已取消")
                for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("下载已取消")
                    f.write(chunk)
                    bytes_done += len(chunk)
                    # 更新全局已下载计数并触发进度回调
                    with counter_lock:
                        downloaded_counter[0] += len(chunk)
                        cur = downloaded_counter[0]
                        last = last_report[0]
                    if progress and cur - last >= (1 << 18):  # 每 256KB 回调
                        with counter_lock:
                            last_report[0] = cur
                        progress(cur, total)
        return bytes_done
    return _dl


def _download_single(
    url: str,
    tmp_path: str,
    progress: Optional[ProgressCallback],
    cancel_event,
) -> str:
    """单线程顺序下载（不支持 Range 或文件太小时的回退路径）。"""
    hasher = hashlib.sha256()
    with httpx.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0) or 0)
        downloaded = 0
        if progress:
            progress(0, total)
        with open(tmp_path, "wb") as f:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("下载已取消")
            last_report = 0
            for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("下载已取消")
                f.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                if progress and (downloaded - last_report >= (1 << 18) or downloaded == total):
                    progress(downloaded, total)
                    last_report = downloaded
    return hasher.hexdigest()


def download_file(
    url: str,
    dest_path: str,
    expected_hash: Optional[str] = None,
    progress: Optional[ProgressCallback] = None,
    cancel_event=None,
    threads: int = _DEFAULT_THREADS,
) -> str:
    """多线程下载文件到 dest_path，返回最终路径。若 expected_hash 给定则校验。

    cancel_event: 可选的 threading.Event，set() 后尽快终止下载。
    自动探测是否支持 HTTP Range：支持则多线程分块下载，否则回退单线程。
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp_path = dest_path + ".part"

    # HEAD 探测
    total, supports_ranges = _head_info(url)

    use_multithread = supports_ranges and total >= _MIN_MULTITHREAD_SIZE and threads > 1

    try:
        if not use_multithread:
            # ===== 单线程回退 =====
            actual_hash = _download_single(url, tmp_path, progress, cancel_event)
        else:
            # ===== 多线程分块下载 =====
            n = min(threads, max(1, total // _MIN_MULTITHREAD_SIZE))
            # 切分区间
            part_size = total // n
            ranges: list[tuple[int, int]] = []
            for i in range(n):
                start = i * part_size
                end = (total - 1) if i == n - 1 else (start + part_size - 1)
                ranges.append((start, end))

            part_paths = [f"{tmp_path}.part{i}" for i in range(n)]
            downloaded_counter = [0]
            last_report = [0]
            counter_lock = threading.Lock()

            if progress:
                progress(0, total)

            # 用闭包共享 total/计数器给各分块下载任务
            _dl = _make_range_downloader(total, downloaded_counter, counter_lock, last_report, progress, cancel_event)

            # 并发下载各分块
            with ThreadPoolExecutor(max_workers=n) as pool:
                futures = {
                    pool.submit(_dl, url, part_paths[i], ranges[i][0], ranges[i][1]): i
                    for i in range(n)
                }
                try:
                    for fut in as_completed(futures):
                        fut.result()  # 抛出异常会进入 except
                except Exception:
                    # 任一分块失败：取消其余 + 清理
                    if cancel_event is not None:
                        cancel_event.set()
                    raise

            # 取消检查（合并前）
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("下载已取消")

            # 合并分块 + 流式哈希校验
            hasher = hashlib.sha256()
            with open(tmp_path, "wb") as out:
                for pp in part_paths:
                    with open(pp, "rb") as pf:
                        while True:
                            buf = pf.read(1 << 20)  # 1MB
                            if not buf:
                                break
                            out.write(buf)
                            hasher.update(buf)
                    os.remove(pp)
            actual_hash = hasher.hexdigest()

    except RuntimeError:
        _cleanup(tmp_path, use_multithread)
        raise
    except httpx.TimeoutException as e:
        _cleanup(tmp_path, use_multithread)
        raise RuntimeError(f"下载超时：{e}") from e
    except Exception:
        _cleanup(tmp_path, use_multithread)
        raise

    # 哈希校验
    if expected_hash and actual_hash.lower() != expected_hash.lower():
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise ValueError(f"哈希校验失败: 期望 {expected_hash} 实际 {actual_hash}")

    os.replace(tmp_path, dest_path)
    return dest_path


def _cleanup(tmp_path: str, multithread: bool) -> None:
    """清理临时文件（单线程的 .part 或多线程的 .partN）。"""
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    if multithread:
        # 分块文件按 0..n-1 连续命名，第一个不存在的就停
        i = 0
        while i < 128:
            pp = f"{tmp_path}.part{i}"
            if os.path.exists(pp):
                try:
                    os.remove(pp)
                except OSError:
                    pass
                i += 1
            else:
                break
