"""控制台事件源 (CONSOLES_PLAN S2): 对比前后两帧控制台快照, 往事件总线发事件。

- CONSOLE_TICKET_PENDING (warning, 铃铛会响): 某次运行有阻塞工单, 开了 >= 60 秒还没人答 (与铃铛对会话的 60 秒同一条规则)。
  每张工单只发一次。P6 误报零容忍: **只有这一种会响**。
- CONSOLE_PARKED (info): 运行进入额度停泊 (每段停泊一次)。
- CONSOLE_RUN_FINISHED (info): 运行结束 (一次)。
- TOKEN_BUDGET_WARNING (scope=console, **info**): 控制台报的预算级别升到 warn / soft_stop / hard_stop (每个级别一次)。
  只发 info: 通知层不推 info —— 「只有等你会响」(P6)。不带百分比 (控制台的级别不总有对应的百分比, 不编)。

- 冷启动静默**按运行算**: 一次运行第一次被看到的那一帧只记下「已经是这样了」, 不补发 —— 重启 tokmon、后来才加进清单的控制台、
  第一帧没读到的运行, 都不会把三天前的老工单再响一遍;
- §6 内容最小化: payload 只有 console / run / ticket_kind / age_s / count / scope / pct —— **不带工单题目和正文、任务标题**;
- 支柱独立 (P4): 只 import stdlib + events + 控制台快照 (consoles)。
"""

from __future__ import annotations

import threading
import time

from .. import consoles
from ..events import Event, bus

TICKET_ALERT_S = 60                 # 阻塞工单开了这么久还没答 -> 提醒一次 (与铃铛对会话的 60 秒一致)
_BUDGET_LEVELS = ("warn", "soft_stop", "hard_stop")

_seen: set = set()                  # 已经发过 (或第一次看到时就已经存在) 的事件键
_known: set = set()                 # 见过的运行 (代号, 运行): 第一次见到的那一帧只播种
_lock = threading.Lock()
_pump_thread: threading.Thread | None = None
_pump_stop = threading.Event()


def derive(snap, now: float, seen: set, emit: bool, quiet=frozenset()) -> list:
    """控制台快照 -> 这一帧该发的事件 (就地把发过的键记进 seen)。纯逻辑, 不碰总线。
    quiet: 这一帧第一次见到的运行 —— 只播种, 不发。"""
    out = []
    for (code, run), info in sorted(snap.runs.items()):
        con = snap.console(code)
        project = con.project if con else code
        loud = emit and (code, run) not in quiet

        def add(key, make, loud=loud):
            if key in seen:
                return
            seen.add(key)
            if loud:
                out.append(make())

        state = info.state(now)
        if state != "finished":
            for tid, kind, opened in info.pending():
                if now - opened >= TICKET_ALERT_S:
                    add(f"CONSOLE_TICKET_PENDING:{code}:{run}:{tid}",
                        lambda tid=tid, kind=kind, opened=opened: Event.make(
                            "CONSOLE_TICKET_PENDING", pillar="console", project=project, timestamp=opened + TICKET_ALERT_S,
                            detected_at=now, dedup_key=f"CONSOLE_TICKET_PENDING:{code}:{run}:{tid}",
                            console=code, run=run, ticket_kind=kind, age_s=int(now - opened),
                            count=len(info.pending())))
        park_mark = f"CONSOLE_PARKED:{code}:{run}"
        if state == "parked":                                     # 每段停泊一次: 进入停泊发, 离开停泊就重新上膛
            until = int(info.park_until or 0)
            add(park_mark,
                lambda until=until: Event.make(
                    "CONSOLE_PARKED", pillar="console", project=project, timestamp=now, detected_at=now,
                    dedup_key=f"{park_mark}:{int(now)}", console=code, run=run,
                    age_s=max(0, until - int(now)) if until else None))
        else:
            seen.discard(park_mark)
        if state == "finished":
            add(f"CONSOLE_RUN_FINISHED:{code}:{run}",
                lambda: Event.make(
                    "CONSOLE_RUN_FINISHED", pillar="console", project=project, timestamp=info.finished or now,
                    detected_at=now, dedup_key=f"CONSOLE_RUN_FINISHED:{code}:{run}", console=code, run=run,
                    state_label=",".join(f"{k}×{v}" for k, v in sorted(info.outcomes().items())) or None))
        lvl = info.budget_level
        if lvl in _BUDGET_LEVELS and state != "finished":
            add(f"TOKEN_BUDGET_WARNING:console:{code}:{run}:{lvl}",
                lambda lvl=lvl: Event.make(
                    "TOKEN_BUDGET_WARNING", pillar="console", severity="info", project=project, timestamp=now,
                    detected_at=now, dedup_key=f"TOKEN_BUDGET_WARNING:console:{code}:{run}:{lvl}", console=code,
                    run=run, scope="console", state_label=lvl))
    return out


def tick() -> int:
    snap = consoles.snapshot()
    with _lock:
        fresh = {k for k in snap.runs if k not in _known}         # 第一次见到的运行: 只播种 (冷启动时就是全部)
        evs = derive(snap, time.time(), _seen, emit=True, quiet=fresh)
        _known.update(snap.runs)
    for ev in evs:
        bus.emit(ev)
    return len(evs)


def _loop(tick_s: float):
    while True:
        try:
            tick()
        except Exception:
            pass                                    # 单次出错不拖垮 pump (渐进降级)
        if _pump_stop.wait(tick_s):
            return


def start_pump(tick_s: float = 15.0):
    """懒启动 (serve.run_serve 调一次)。线程里先播种一帧, 再每 tick_s 秒一帧。"""
    global _pump_thread
    if _pump_thread and _pump_thread.is_alive():
        return _pump_thread
    _pump_stop.clear()
    _pump_thread = threading.Thread(target=_loop, args=(tick_s,), name="mc-console-pump", daemon=True)
    _pump_thread.start()
    return _pump_thread


def stop_pump():
    _pump_stop.set()


def reset_for_tests():
    with _lock:
        _seen.clear()
        _known.clear()
