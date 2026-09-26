"""上下文事件源 (省钱 S3, CONTEXT_COST_PLAN): 正在跑的会话, 主线程上下文刚越过 30 万 -> 往事件总线发一条 CONTEXT_LARGE。

- 只读: 数来自内核 load_records() + context_view (与 /sessions 行里的「上下文 N」同一个数);
- 只对**正在跑**的会话 (最后一轮在 ACTIVE_S 秒内): 放了一夜的大会话不吵;
- 每越过一次只发一次; 掉到 REARM 以下 (压缩 / 清空) 之后再越过, 算新的一次;
- 冷启动静默: 第一帧只记下谁已经在 30 万以上, 不补发;
- 只发 info: notify 的外发通道不推 info; 浏览器铃铛只有你勾了「上下文过 30 万也提醒」才弹 (默认关);
- §6 内容最小化: payload 只有上下文 token 数 (count)。
"""

from __future__ import annotations

import threading
import time

from ..context_view import BIG_CTX, session_contexts
from ..discovery import default_base
from ..events import Event, bus
from ..parser import load_records

ACTIVE_S = 600                 # 最后一轮在 10 分钟内 = 正在跑
REARM = int(BIG_CTX * 0.8)     # 掉到 24 万以下再越过 30 万, 算新的一次
KINDS = {"main", "subagent", "workflow"}

_above: dict = {}              # 会话 id -> 这一段是否已经在线上 (发过了)
_lock = threading.Lock()
_baseline_done = False
_pump_thread: threading.Thread | None = None
_pump_stop = threading.Event()


def derive(ctxs: dict, now: float, state: dict, emit: bool) -> list:
    """{会话: 上下文体检} + 上一帧的「在不在线上」 -> 这一帧该发的事件 (就地更新 state)。纯逻辑, 不碰总线。"""
    out = []
    for sid, c in ctxs.items():
        was = state.get(sid, False)
        if c["ctx"] >= BIG_CTX:
            if not was and now - c["t"] <= ACTIVE_S:
                state[sid] = True
                if emit:
                    out.append(Event.make("CONTEXT_LARGE", pillar="cost", severity="info", session=sid,
                                          project=c.get("project"), timestamp=c["t"], detected_at=now,
                                          dedup_key=f"CONTEXT_LARGE:{sid}:{int(c['t'])}", count=c["ctx"]))
        elif c["ctx"] < REARM:
            state[sid] = False
    return out


def tick(base=None) -> int:
    global _baseline_done
    ctxs = session_contexts(load_records(base or default_base(), KINDS, False))
    with _lock:
        evs = derive(ctxs, time.time(), _above, emit=_baseline_done)
        if not _baseline_done:                      # 冷启动: 已经在线上的记为「发过了」, 不补发
            for sid, c in ctxs.items():
                if c["ctx"] >= BIG_CTX:
                    _above[sid] = True
        _baseline_done = True
    for ev in evs:
        bus.emit(ev)
    return len(evs)


def _loop(base, tick_s: float):
    try:
        tick(base)
    except Exception:
        pass
    while not _pump_stop.wait(tick_s):
        try:
            tick(base)
        except Exception:
            pass


def start_pump(base=None, tick_s: float = 30.0):
    """懒启动 (serve.run_serve 调一次)。线程里先播种一帧 -> 每 tick_s 秒一帧。"""
    global _pump_thread
    if _pump_thread and _pump_thread.is_alive():
        return _pump_thread
    _pump_stop.clear()
    _pump_thread = threading.Thread(target=_loop, args=(base, tick_s), name="mc-context-pump", daemon=True)
    _pump_thread.start()
    return _pump_thread


def reset_for_test():
    global _baseline_done
    with _lock:
        _above.clear()
        _baseline_done = False
