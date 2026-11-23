"""滚动窗口指标计算（延迟 / 丢包 / 抖动）。

这里的统计只用于卡片上的「最近 N 次」实时概览；
按时间范围查询历史数据由 :mod:`netmon.history` 负责。
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence


def window_stats(samples: Sequence[Optional[float]]) -> Dict[str, object]:
    """计算最近若干次探测的概览。

    ``samples`` 中 ``None`` 表示一次失败探测。
    """
    total = len(samples)
    oks = [float(x) for x in samples if x is not None]

    latency: Optional[float] = None
    if total:
        last = samples[-1]
        latency = round(float(last), 2) if last is not None else None

    avg: Optional[float] = round(sum(oks) / len(oks), 2) if oks else None

    jitter = 0.0
    if len(oks) >= 2:
        diffs = [abs(oks[i] - oks[i - 1]) for i in range(1, len(oks))]
        jitter = round(sum(diffs) / len(diffs), 2)

    loss = round(((total - len(oks)) / total) * 100, 1) if total else 0.0

    return {
        "latency": latency,
        "avg": avg,
        "loss": loss,
        "jitter": jitter,
        "samples": total,
        "ok": len(oks),
    }
