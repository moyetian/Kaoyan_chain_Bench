"""ISO8601 / 单调时钟 / 耗时工具（零依赖）。

**可复现性原则**：核心评分路径不得使用"当前时间"。所有需要的时刻必须由外部注入
（``now``）。本模块只提供**格式化与解析**能力，以及命令行边界处的 ``utcnow_iso()``
（这个函数在 CLI 与 Runner 里使用，不参与评分判定）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

__all__ = [
    "ISO_LOCAL",
    "utcnow",
    "utcnow_iso",
    "to_iso",
    "parse_iso",
    "now_iso",
    "duration_ms",
    "elapsed_ms",
    "MonotonicTimer",
]

ISO_LOCAL = "%Y-%m-%dT%H:%M:%S%z"


def utcnow() -> datetime:
    """当前 UTC 时刻（**仅限 CLI / Runner 边界使用**）。"""
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """datetime → ISO 8601 带时区字符串（秒精度，UTC 用 ``+00:00``）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    text = dt.isoformat(timespec="seconds")
    return text


def utcnow_iso() -> str:
    return to_iso(utcnow())


def now_iso(now: datetime | str | None = None) -> str:
    """把外部注入的 ``now`` 归一化为 ISO 字符串；``None`` 时回退到当前 UTC。

    注入形式支持 ``datetime`` 或已经是 ISO 字符串。
    """
    if now is None:
        return utcnow_iso()
    if isinstance(now, datetime):
        return to_iso(now)
    return str(now)


def parse_iso(text: str) -> datetime:
    """解析 ISO 8601 字符串；失败抛 ``ValueError``（可读消息）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("空的时间字符串无法解析")
    candidate = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(candidate, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"无法解析 ISO8601 时间：{text!r}")


def duration_ms(started_at: str, ended_at: str) -> int:
    """两个 ISO 时刻之间的毫秒数（负数归零）。"""
    try:
        delta = parse_iso(ended_at) - parse_iso(started_at)
    except ValueError:
        return 0
    return max(0, int(delta.total_seconds() * 1000))


class MonotonicTimer:
    """单调计时器。用于测量真实耗时（不参与评分判定，故不破坏可复现性）。"""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    def reset(self) -> None:
        self._start = time.monotonic()


def elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def shift_iso(text: str, *, seconds: float) -> str:
    """在 ISO 时刻上偏移若干秒（测试辅助，保证确定性）。"""
    return to_iso(parse_iso(text) + timedelta(seconds=seconds))
