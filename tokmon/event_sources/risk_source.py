"""风险事件源 (改动与风险 S3): 盯着开着的会话, 当前任务一冒出新的风险标记, 就往事件总线发一条事件。

- 只读: 标记来自 trace.session_brief —— 和回放页、统计页同一套规则, 不另算;
- 冷启动静默: 第一帧只把「已经在那儿的标记」记下来, 不把历史当新事件发;
- 去重: 同一个任务的同一个标记只发一次 (dedup_key = 类型:任务:标记), 计数变大也不重复发;
- 只发 info: 风险规则在真实数据上审过精确率之前不升级 (误报零容忍)。notify 本来就不推 info —— 通知仍关,
  以后想推只需在 notify 配置里把这几类打开;
- §6 内容最小化: 事件里只带规则名、计数与破坏性操作的种类 (如「rm -rf」), 不带路径 / 命令 / 文件名。
- 「改到工作目录以外」只在页面上看, 不进总线 (防事件蔓延: 它回答不了「推进 / 卡住 / 烧钱 / 乱改」里更具体的问题)。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from .. import activity, trace
from ..events import Event, bus

RULE_EVENT = {"destructive": "DESTRUCTIVE_OP", "error-spike": "ERROR_SPIKE", "thrash": "REPEATED_FILE_EDIT",
              "large-edit": "LARGE_DIFF", "large-task": "LARGE_DIFF"}
OPEN_STATES = frozenset({"WORKING", "PROCESSING", "AMBIGUOUS_PENDING", "AWAITING_USER"})
_SEEN_CAP = 5000

_seen: "OrderedDict[str, None]" = OrderedDict()
_lock = threading.Lock()
_baseline_done = False
_pump_thread: threading.Thread | None = None
_pump_stop = threading.Event()


def derive(row: dict, brief: dict, now: float) -> list[tuple[str, Event]]:
    """一个会话 + 它当前任务的简报 -> [(去重键, 事件)]。纯函数。"""
    out = []
    for r in brief.get("risks") or []:
        typ = RULE_EVENT.get(r.get("rule"))
        if not typ:
            continue
        key = f"{typ}:{brief['task']}:{r['key']}"
        kind = r["label"].split("：", 1)[-1] if r.get("rule") == "destructive" else None
        out.append((key, Event.make(typ, pillar="trace", severity="info", session=row.get("session_id"),
                                    project=row.get("project"), timestamp=r.get("at") or now, detected_at=now,
                                    dedup_key=key,
                                    rule=r["rule"], count=r.get("n"), kind=kind)))
    return out


def tick(base=None, live_factory=None) -> int:
    """跑一帧: 开着的会话 -> 当前任务的风险标记 -> 新冒出来的发事件。返回发出的事件数。第一帧只播种。"""
    global _baseline_done
    live = live_factory() if live_factory is not None else None
    snap = activity.snapshot(base, live=live)
    now = time.time()
    emitted = 0
    with _lock:
        baseline = not _baseline_done
        for row in snap.get("sessions", []):
            if row.get("state") not in OPEN_STATES or not row.get("file"):
                continue
            try:
                brief = trace.session_brief(row["file"], running=row.get("state") in ("WORKING", "PROCESSING"))
            except Exception:
                continue                                      # 单个会话算不出来不拖垮整帧
            if not brief:
                continue
            for key, ev in derive(row, brief, now):
                if key in _seen:
                    continue
                _seen[key] = None
                if len(_seen) > _SEEN_CAP:
                    _seen.popitem(last=False)
                if not baseline:
                    bus.emit(ev)
                    emitted += 1
        _baseline_done = True
    return emitted


def _loop(base, tick_s: float, live_factory=None):
    # 播种 (冷启动静默) 放在线程里: 第一次算简报要等耗时基线 (冷启动十几秒), 不能卡住服务启动
    while not trace.baseline_ready() and not _pump_stop.wait(2.0):
        pass
    try:
        tick(base, live_factory)
    except Exception:
        pass
    while not _pump_stop.wait(tick_s):
        try:
            tick(base, live_factory)
        except Exception:
            pass


def start_pump(base=None, tick_s: float = 15.0, live_factory=None):
    """懒启动风险事件 pump (serve.run_serve 调一次)。线程里等基线热好 -> 播种一帧 -> 每 tick_s 秒一帧。"""
    global _pump_thread
    if _pump_thread and _pump_thread.is_alive():
        return _pump_thread
    _pump_stop.clear()
    _pump_thread = threading.Thread(target=_loop, args=(base, tick_s, live_factory), name="mc-risk-pump", daemon=True)
    _pump_thread.start()
    return _pump_thread


def reset_for_test():
    global _baseline_done
    with _lock:
        _seen.clear()
        _baseline_done = False
