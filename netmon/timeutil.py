"""与时间相关的小工具：时间戳格式化、用户输入的时间解析。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def format_ts(ts: Optional[float]) -> Optional[str]:
    """时间戳 -> 本地时间字符串。"""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).strftime(TIME_FMT)
    except (OverflowError, OSError, ValueError):
        return None


def format_clock(ts: Optional[float]) -> str:
    """时间戳 -> 仅时分秒。"""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def now_str() -> str:
    return datetime.now().strftime(TIME_FMT)


def parse_ts(value) -> Optional[float]:
    """解析 epoch 秒或 ``YYYY-MM-DD HH:MM:SS`` / ISO8601 字符串。

    ISO 字符串不带时区时按本机时区理解。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass

    normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, TIME_FMT),
        lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M"),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M"),
    ):
        try:
            return parser(normalized).timestamp()
        except (TypeError, ValueError):
            continue
    return None


def clamp_range(start: float, end: float, max_span: float = 366 * 86400) -> tuple:
    """保证 start < end 且跨度不超过上限。"""
    end = min(end, time.time() + 86400)
    if end <= start:
        end = start + 60
    if end - start > max_span:
        start = end - max_span
    return start, end
