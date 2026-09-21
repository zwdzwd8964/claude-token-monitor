"""格式化与小工具。"""

from __future__ import annotations

import sys

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


# ---- 终端编码降级 (原则 5: 渐进可降级) ----
# 背景 (EVOLUTION.md §Gen0.3 实测): Windows 的 GBK/cp936 控制台编码不了 ✓ ⚠ ✗,
# rich 在 legacy 渲染路径上直接抛 UnicodeEncodeError —— 数据层已经算完几百个文件,
# 却崩在最后一步的图标上。立场: **显示不了就换个字符显示, 绝不让展示层的编码问题杀掉算对的结果。**
_ASCII_FALLBACK = {
    "✓": "[OK]", "✔": "[OK]",
    "✗": "[X]", "✘": "[X]",
    "⚠": "[!]",
    "→": "->", "←": "<-",
    "●": "*", "▶": ">", "█": "#", "░": ".",
}


def term_encodable(sample: str = "✓⚠✗") -> bool:
    """当前 stdout 能原样写出这些字符吗? 读不出编码时保守按"能"(常见于被重定向的 UTF-8 管道)。"""
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return True
    try:
        sample.encode(enc)
        return True
    except Exception:
        return False


def term_text(s: str) -> str:
    """把当前控制台写不出的字符降级成 ASCII 近似; 仍写不出的用 replace 兜底。**绝不抛。**"""
    if term_encodable():
        return s
    for ch, alt in _ASCII_FALLBACK.items():
        s = s.replace(ch, alt)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return s.encode(enc, "replace").decode(enc, "replace")
    except Exception:
        return s.encode("ascii", "replace").decode("ascii")
