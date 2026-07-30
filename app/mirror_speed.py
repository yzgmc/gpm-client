"""智能镜像选择：测速 + 缓存，自动挑选最快的下载源。

设计目标：
1. **多镜像自动测速**：在下载前/启动时对已知镜像做轻量测速（HEAD 或 Range GET 0-0）
2. **结果缓存 24 小时**：避免每次下载都重测，磁盘上 <data>/mirror_speed.json
3. **零阻塞**：测速用后台线程，不影响主流程
4. **降级策略**：测速失败时使用 URL 提供方指定的默认顺序（与现有逻辑兼容）

测速方法：
- HEAD 请求：测 RTT（连接时间 + TLS 握手 + 首字节）
- Range GET 0-0（256KB）：测首字节后的传输速率（避免被 CDN 边缘节点缓存命中误导）
- 综合得分 = 0.7 × RTT_score + 0.3 × speed_score

集成方式：
- pick_fastest_mirror(mirrors: list[str]) -> str
  输入候选 URL 列表，返回最优者。
  第一次调用时启动后台测速（不阻塞），返回列表中第一个；后续调用时
  如果测速完成则用结果排序。

- downloader.py 内的 CDN 候选 URL 解析使用本模块挑选最优。
- installer.py 内多 CDN fallback（Modrinth cdn / cdn-raw）使用本模块排序。
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


# ============================================================================
# 镜像定义（已知镜像列表 + 默认优先级）
# ============================================================================
# 评分高的在前。每次启动可由后台测速重排。
KNOWN_MIRRORS: dict[str, str] = {
    # Modrinth 官方
    "modrinth-cdn": "https://cdn.modrinth.com",
    "modrinth-cdn-raw": "https://cdn-raw.modrinth.com",
    # 国内镜像（第三方，BMCLAPI 风格）
    "mc-bmclapi": "https://bmclapi2.bangbang93.com",
}


# ============================================================================
# 持久化
# ============================================================================
_score_path: Optional[Path] = None
_scores: dict[str, dict] = {}  # url -> {"score": float, "rtt": float, "speed": float, "ts": float}
_lock = threading.Lock()
_tested_once: bool = False


def _default_data_dir() -> Path:
    """获取 <DATA_DIR>/mirror_speed.json 路径。"""
    try:
        from app.config import DATA_DIR
        return DATA_DIR / "mirror_speed.json"
    except Exception:
        # 兜底：当前目录
        return Path("data") / "mirror_speed.json"


def init(path: Optional[str | Path] = None) -> None:
    """初始化：加载持久化的镜像测速分数（如果有）。"""
    global _score_path, _scores
    p = Path(path) if path else _default_data_dir()
    p.parent.mkdir(parents=True, exist_ok=True)
    _score_path = p
    if p.is_file():
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _scores = data
        except (OSError, json.JSONDecodeError, ValueError):
            _scores = {}


def _save() -> None:
    if _score_path is None:
        return
    try:
        tmp = _score_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_scores, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _score_path)
    except OSError:
        pass


# ============================================================================
# 测速
# ============================================================================
def _measure_one(url: str, timeout: float = 3.0) -> Optional[dict]:
    """测单个 URL：RTT + 速度。

    RTT：HEAD 请求
    速度：Range GET 0-262143（256KB）算传输耗时
    返回 {"url", "rtt", "speed", "score"} 或 None（失败）
    """
    from app.downloader import get_sync_client
    client = get_sync_client()
    # ---- RTT ----
    rtt_start = time.perf_counter()
    try:
        r = client.head(url, timeout=timeout, follow_redirects=True)
        rtt_ms = (time.perf_counter() - rtt_start) * 1000
        if r.status_code >= 400:
            return None
    except Exception:
        return None
    # ---- 速度：Range GET 0-262143 ----
    speed_bps = 0.0
    try:
        speed_start = time.perf_counter()
        r2 = client.get(url, headers={"Range": "bytes=0-262143"}, timeout=timeout, follow_redirects=True)
        if r2.status_code in (200, 206):
            received = 0
            for chunk in r2.iter_bytes(chunk_size=8192):
                received += len(chunk)
            speed_bps = received / max(0.001, time.perf_counter() - speed_start)
    except Exception:
        pass
    # ---- 综合得分（越小越好） ----
    # 经验权重：RTT 影响首字节，speed 影响下载速度；RTT 更影响体感
    rtt_score = rtt_ms
    speed_score = 1e6 / max(1, speed_bps)  # 速度越快分越低
    score = 0.7 * rtt_score + 0.3 * speed_score
    return {
        "url": url,
        "rtt_ms": round(rtt_ms, 1),
        "speed_bps": round(speed_bps),
        "score": round(score, 2),
    }


def measure_blocking(urls: list[str], timeout: float = 3.0) -> list[dict]:
    """同步测速（用于 benchmark）。返回 [{url, rtt_ms, speed_bps, score}, ...] 排序后。"""
    results: list[dict] = []
    for u in urls:
        r = _measure_one(u, timeout=timeout)
        if r:
            results.append(r)
    results.sort(key=lambda x: x["score"])
    return results


def measure_background(urls: list[str], timeout: float = 3.0) -> None:
    """后台线程测速。完成后写磁盘。"""
    global _tested_once
    def _worker():
        results = measure_blocking(urls, timeout=timeout)
        with _lock:
            for r in results:
                _scores[r["url"]] = {
                    "score": r["score"],
                    "rtt_ms": r["rtt_ms"],
                    "speed_bps": r["speed_bps"],
                    "ts": time.time(),
                }
            _save()
        _tested_once = True
    t = threading.Thread(target=_worker, daemon=True, name="mirror-speed")
    t.start()


# ============================================================================
# 镜像选择 API
# ============================================================================
_SCORE_TTL = 24 * 3600  # 24h


def get_score(url: str) -> Optional[dict]:
    """获取 URL 的最新测速分数（24h 内有效）。"""
    with _lock:
        s = _scores.get(url)
    if not s:
        return None
    if time.time() - s.get("ts", 0) > _SCORE_TTL:
        return None
    return s


def pick_fastest_mirror(urls: list[str], default_index: int = 0) -> str:
    """从候选 URL 列表中挑最优镜像。

    - 有测速分数 → 按 score 升序选最优
    - 无分数 → 启动后台测速，返回默认顺序的第一个（保持向后兼容）
    - 仅 1 个 URL → 直接返回
    """
    if not urls:
        raise ValueError("empty urls list")
    if len(urls) == 1:
        return urls[0]
    # 启动后台测速（如果还没测过）
    if not _tested_once and init.__name__:
        measure_background(urls)
    # 按测速分数排序
    candidates: list[tuple[float, str]] = []
    for i, u in enumerate(urls):
        s = get_score(u)
        if s:
            candidates.append((s["score"], u))
        else:
            # 无分数：放最后，顺序按 default_index
            candidates.append((float("inf") + i, u))
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def rank_mirrors(urls: list[str]) -> list[str]:
    """按测速分数排序候选 URL 列表。"""
    if not urls:
        return []
    return [u for _, u in sorted(
        ((get_score(u)["score"] if get_score(u) else float("inf"), u) for u in urls),
        key=lambda x: x[0]
    )]


# ============================================================================
# 启动时自动测速
# ============================================================================
def startup_probe() -> None:
    """应用启动时调用：测速已知镜像（后台），不阻塞启动。"""
    init()
    urls = list(KNOWN_MIRRORS.values())
    measure_background(urls)
