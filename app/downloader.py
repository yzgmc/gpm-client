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
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + ".part"
    hasher = hashlib.sha256()

    with httpx.stream("GET", url, timeout=None, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 16):
                if cancel_event is not None and cancel_event.is_set():
                    f.close()
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise RuntimeError("下载已取消")
                f.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)

    actual_hash = hasher.hexdigest()
    if expected_hash and actual_hash.lower() != expected_hash.lower():
        os.remove(tmp_path)
        raise ValueError(
            f"哈希校验失败: 期望 {expected_hash} 实际 {actual_hash}"
        )

    os.replace(tmp_path, dest_path)
    return dest_path
