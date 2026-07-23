"""流式下载器：边下载边写盘，支持进度回调与 sha256 校验。"""

from __future__ import annotations

import hashlib
import os
from typing import Callable, Optional

import httpx


ProgressCallback = Callable[[int, int], None]  # (downloaded_bytes, total_bytes)


def download_file(
    url: str,
    dest_path: str,
    expected_hash: Optional[str] = None,
    progress: Optional[ProgressCallback] = None,
    cancel_event=None,
) -> str:
    """流式下载文件到 dest_path，返回最终路径。若 expected_hash 给定则校验。

    cancel_event: 可选的 threading.Event，set() 后尽快终止下载。
    使用有限超时（连接 10s / 读取 30s）避免服务端不响应时无限阻塞；
    取消检查放在每个 chunk 读取前后，保证点取消能立刻退出。
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + ".part"
    hasher = hashlib.sha256()

    # 连接 10s、读取每个 chunk 最多等 30s；否则服务端挂起会无限卡住
    timeout = httpx.Timeout(10.0, read=30.0)
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            downloaded = 0
            # 连接建立后立即反馈一次，让 UI 脱离"等待"状态、显示总大小
            if progress:
                progress(0, total)
            with open(tmp_path, "wb") as f:
                # 读取前先检查取消（用户在下载开始前就点取消）
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("下载已取消")
                last_report = 0
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    # 每个 chunk 写入后检查取消，保证点取消能在下个循环立即退出
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("下载已取消")
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    # 每 256KB 或完成时回调一次，避免高频投递定时器堆积导致 UI 卡顿
                    if progress and (downloaded - last_report >= (1 << 18) or downloaded == total):
                        progress(downloaded, total)
                        last_report = downloaded
    except RuntimeError:
        # 取消：清理临时文件后向上抛
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    except httpx.TimeoutException as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise RuntimeError(f"下载超时：{e}") from e

    actual_hash = hasher.hexdigest()
    if expected_hash and actual_hash.lower() != expected_hash.lower():
        os.remove(tmp_path)
        raise ValueError(
            f"哈希校验失败: 期望 {expected_hash} 实际 {actual_hash}"
        )

    os.replace(tmp_path, dest_path)
    return dest_path
