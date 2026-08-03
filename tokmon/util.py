"""格式化与小工具。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def parse_since(spec: str | None) -> datetime | None:
    """'today' / 'all' / 'Nh' / 'Nd' / 'Nw' -> cutoff datetime (本地) 或 None(=全部)。"""
    if not spec or spec.lower() == "all":
        return None
    now = datetime.now().astimezone()
    spec = spec.strip().lower()
    if spec == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.fullmatch(r"(\d+)\s*([hdw])", spec)
    if not m:
        raise ValueError(f"无法解析时间窗口: {spec!r} (用 today/all/24h/7d/2w)")
    n = int(m.group(1))
    unit = m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    return now - delta


def fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s 前"
    if seconds < 3600:
        return f"{int(seconds / 60)}m 前"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h 前"
    return f"{int(seconds / 86400)}d 前"
