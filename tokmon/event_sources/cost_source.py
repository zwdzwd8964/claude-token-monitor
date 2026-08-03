"""成本支柱 -> 事件 (M3.5)。预算/阈值告警: 当今天/近7天/某项目的等价花费越过 70%/90%/100%, 发 `TOKEN_BUDGET_WARNING`。

把 tokmon 北极星第三问「跟我的预期差多少」从"我去看"变成"它提醒我"。

只 import: stdlib + tokmon.events + 本支柱(cost)自己的内核 (parser/aggregate/discovery/pricing 都在内核里, 经 load_records)。
不碰 activity / procmon / notify / serve (P4: 适配器只认自己的 pillar + 总线)。

幂等: 每 tick 只发"当前越过的最高阈值"那一条, dedup_key 含 (scope, 周期, 阈值) —— 总线天然去重,
同一周期同一阈值只响一次; 跨日/跨周周期变了, 自动重新可响 (无需 memo, 无状态)。

隐私/§6: 事件只带 {scope, pct} + project 名, 绝不带花费明细/命令行/会话正文。
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..aggregate import filter_days, filter_today, group_by, summarize
from ..discovery import default_base
from ..events import Event, bus
from ..parser import load_records

_BUDGET_PATH = Path.home() / ".tokmon" / "budget.json"
_DEFAULT_THRESHOLDS = (70, 90, 100)


@dataclass
class BudgetConfig:
    daily_usd: float = 0.0
    weekly_usd: float = 0.0              # 近 7 天滚动窗
    project_usd: dict = field(default_factory=dict)   # {项目名: 今日等价美元上限}
    thresholds: tuple = _DEFAULT_THRESHOLDS

    def configured(self) -> bool:
        return self.daily_usd > 0 or self.weekly_usd > 0 or bool(self.project_usd)


def load_budget() -> BudgetConfig:
    def _clean(v):                       # 文件可能被手改成 Infinity/NaN (json 会解析它) -> 归零, 别让非有限值进来
        try:
            n = float(v)
            return n if (n >= 0 and math.isfinite(n)) else 0.0
        except (TypeError, ValueError):
            return 0.0

    cfg = BudgetConfig()
    try:
        if _BUDGET_PATH.exists():
            d = json.loads(_BUDGET_PATH.read_text(encoding="utf-8"))
            cfg.daily_usd = _clean(d.get("daily_usd"))
            cfg.weekly_usd = _clean(d.get("weekly_usd"))
            pj = d.get("project_usd") or {}
            if isinstance(pj, dict):
                cfg.project_usd = {str(k): _clean(v) for k, v in pj.items() if _clean(v) > 0}
            thr = d.get("thresholds")
            if isinstance(thr, list) and thr:
                cfg.thresholds = tuple(sorted(int(x) for x in thr))
    except (OSError, ValueError, TypeError):
        pass
    return cfg


def save_budget(daily_usd=None, weekly_usd=None, project_usd=None) -> BudgetConfig:
    """写 ~/.tokmon/budget.json。只接受 >=0 的数值, 校验在调用前/此处双重保险。"""
    cur = load_budget()
    if daily_usd is not None:
        cur.daily_usd = max(0.0, float(daily_usd))
    if weekly_usd is not None:
        cur.weekly_usd = max(0.0, float(weekly_usd))
    if isinstance(project_usd, dict):
        cur.project_usd = {str(k): max(0.0, float(v)) for k, v in project_usd.items() if float(v) > 0}
    try:
        _BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BUDGET_PATH.write_text(json.dumps({
            "daily_usd": cur.daily_usd, "weekly_usd": cur.weekly_usd,
            "project_usd": cur.project_usd, "thresholds": list(cur.thresholds),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return cur


def _highest_threshold(pct: float, thresholds) -> int | None:
    crossed = [t for t in thresholds if pct >= t]
    return max(crossed) if crossed else None


# ---- 状态 + tick ----

_lock = threading.Lock()
_last_status: dict = {"configured": False, "updated_epoch": None, "budgets": []}
_pump_thread: threading.Thread | None = None
_pump_stop = threading.Event()


def _emit_budget(limit, spend, scope, period, project, thresholds) -> int:
    if not limit or limit <= 0 or not math.isfinite(limit):
        return 0
    pct = spend / limit * 100
    thr = _highest_threshold(pct, thresholds)
    if thr is None:
        return 0
    sev = "critical" if thr >= 90 else "warning"     # critical 优先推送、不被去抖/限流挡 (M3)
    bus.emit(Event.make("TOKEN_BUDGET_WARNING", pillar="cost", project=project, severity=sev,
                        timestamp=time.time(), dedup_key=f"TOKEN_BUDGET_WARNING:{scope}:{period}:{thr}",
                        scope=scope, pct=int(pct)))
    return 1


def tick(base: Path | None = None) -> int:
    base = Path(base) if base else default_base()
    cfg = load_budget()
    now = time.time()
    lt = datetime.fromtimestamp(now)
    day = lt.strftime("%Y-%m-%d")

    budgets: list[dict] = []
    emitted = 0
    if cfg.configured():
        recs = load_records(base)
        today_recs = filter_today(recs)
        today_cost = summarize(today_recs).cost
        week_cost = summarize(filter_days(recs, 7)).cost
        if cfg.daily_usd > 0:
            budgets.append({"scope": "daily", "project": None, "limit": cfg.daily_usd,
                            "spend": round(today_cost, 2), "pct": int(today_cost / cfg.daily_usd * 100)})
            emitted += _emit_budget(cfg.daily_usd, today_cost, "daily", day, None, cfg.thresholds)
        if cfg.weekly_usd > 0:
            budgets.append({"scope": "weekly", "project": None, "limit": cfg.weekly_usd,
                            "spend": round(week_cost, 2), "pct": int(week_cost / cfg.weekly_usd * 100)})
            # 周预算量的是"近7天滚动窗", 故 dedup 周期也按*天*走 (跟着窗口每天滑动重置), 不能用日历周
            # —— 否则同周内真实再越线被吞、跨周边界又误响 (评审发现的窗口/周期错配)。
            emitted += _emit_budget(cfg.weekly_usd, week_cost, "weekly", day, None, cfg.thresholds)
        if cfg.project_usd:
            by_proj = group_by(today_recs, lambda r: r.project)
            for proj, limit in cfg.project_usd.items():
                a = by_proj.get(proj)
                spend = a.cost if a else 0.0
                budgets.append({"scope": "project", "project": proj, "limit": limit,
                                "spend": round(spend, 2), "pct": int(spend / limit * 100) if limit else 0})
                emitted += _emit_budget(limit, spend, "project", f"{day}:{proj}", proj, cfg.thresholds)

    with _lock:
        _last_status.clear()
        _last_status.update({"configured": cfg.configured(), "updated_epoch": int(now),
                             "budgets": budgets, "thresholds": list(cfg.thresholds)})
    return emitted


def status() -> dict:
    with _lock:
        return dict(_last_status)


def _loop(base, tick_s: float):
    while not _pump_stop.wait(tick_s):
        try:
            tick(base)
        except Exception:
            pass            # 单次 tick 出错不拖垮 pump


def start_pump(base: Path | None = None, tick_s: float = 60.0):
    """懒启动成本预算 pump (60s 一次, 比活动 pump 慢 —— 预算跨阈不需要秒级)。"""
    global _pump_thread
    if _pump_thread and _pump_thread.is_alive():
        return _pump_thread
    try:
        tick(base)          # 先算一帧, 让 /api/budget 立刻有状态
    except Exception:
        pass
    _pump_stop.clear()
    _pump_thread = threading.Thread(target=_loop, args=(base, tick_s), name="mc-cost-pump", daemon=True)
    _pump_thread.start()
    return _pump_thread
