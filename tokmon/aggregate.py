"""聚合: 把 UsageRecord 列表按天/项目/模型/来源汇总。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

from .records import UsageRecord


@dataclass
class Agg:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    web_search: int = 0
    web_fetch: int = 0
    cost: float = 0.0
    count: int = 0
    any_unpriced: bool = False

    def add(self, rec: UsageRecord) -> None:
        self.input_tokens += rec.input_tokens
        self.output_tokens += rec.output_tokens
        self.cache_write += rec.cache_write
        self.cache_read += rec.cache_read
        self.web_search += rec.web_search
        self.web_fetch += rec.web_fetch
        self.cost += rec.cost_usd
        self.count += 1
        if not rec.known_price:
            self.any_unpriced = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write + self.cache_read


def summarize(records: Iterable[UsageRecord]) -> Agg:
    agg = Agg()
    for rec in records:
        agg.add(rec)
    return agg


def group_by(records: Iterable[UsageRecord], keyfn: Callable[[UsageRecord], object]) -> dict:
    out: dict[object, Agg] = {}
    for rec in records:
        out.setdefault(keyfn(rec), Agg()).add(rec)
    return out


# ---- 常用过滤窗口 ----

def filter_since(records: Iterable[UsageRecord], cutoff: datetime | None) -> list[UsageRecord]:
    if cutoff is None:
        return list(records)
    return [r for r in records if r.timestamp >= cutoff]


def filter_today(records: Iterable[UsageRecord], today: date | None = None) -> list[UsageRecord]:
    today = today or datetime.now().astimezone().date()
    return [r for r in records if r.timestamp.date() == today]


def filter_days(records: Iterable[UsageRecord], days: int) -> list[UsageRecord]:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    return filter_since(records, cutoff)
