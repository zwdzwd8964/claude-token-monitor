"""对话活动支柱 -> 事件 (M2)。把 activity.snapshot 的状态/事实, 翻译成 §4 typed Event。

只 import: stdlib + tokmon.events + tokmon.activity (P4: 不碰 procmon / token 内核 / serve / 通知)。
activity.py 保持纯只读快照, 永不 import 本模块 —— 状态只活在这里的 `_session_memory`。

诚实纪律 (P6 误报零容忍):
- UNKNOWN / 读不出 -> 不产生任何事件。
- 绝不从 transcript 合成 PERMISSION_NEEDED (transcript 证明不了)。只在 Claude Code **进程自报** waiting (BLOCKED_ON_USER)
  持续 BLOCKED_ALERT_S 后发一次 PERMISSION_NEEDED / QUESTION_PENDING —— 那是它自己的状态, 不是推断。
- 进程自报 idle 而 transcript 停在回合中途 (turn_unfinished: 重启杀掉了上一轮) -> 转"等你", 但**不是任务完成**: 不发
  TASK_COMPLETED, 空闲台阶静默播种到当前 (不补发)。
- SESSION_STUCK 带 activity 的原话对冲 + unresolved。
- 边沿触发 + 单调台阶: 同一逻辑事件只发一次; 叠加总线 dedup_key, 重复要两层同时失效。
- 冷启动基线静默: 第一次 tick 只播种记忆、不补发历史 (no backfill storm)。
- 时钟 = 消息 ts (事实) 或 跨阈那一刻 = since_epoch+阈值 (时间事件), 绝不是 now()/mtime。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .. import activity
from ..events import Event, bus

# 空闲台阶 (秒): 10min / 30min / 2h。每过一个台阶提醒一次, 而非每分钟一次 (§7 去抖)。
IDLE_STEPS = (600, 1800, 7200)
# 久未返回(stuck)升级台阶: 进入即 warning(step1); 更久升一级(step2, 仍 warning, 不 critical —— M1 诚实纪律)。
STUCK_STEPS = (0, 1800)   # 进入(0s)=1, 再过 30min=2
# 回合卡在你身上 (授权/回答) 持续这么久才提醒: 你在键盘前几秒内就点掉的, 不值得推到手机 (§7 有用且不烦)
BLOCKED_ALERT_S = 60
_PERMISSION_WAITS = ("permission prompt", "sandbox request")

# 不产生任何事件的状态: UNKNOWN(读不出) 和 CLOSED(进程已退出 —— 会话不再活动, 别为死会话补发 TASK_COMPLETED/STUCK)。
_DEAD_STATES = ("UNKNOWN", "CLOSED")


@dataclass
class SessionMemo:
    last_state: str
    since_epoch: float | None      # 进入 last_state 的时刻
    last_fact_cursor: int          # 已发过的 recent_events 最大 epoch (高水位)
    last_idle_step: int
    last_stuck_step: int
    seen: bool = True
    blocked_alerted: float | None = None   # 已为哪一段"卡在你身上"提醒过 (= 那段的开始时刻); 每段只提醒一次


def _step(value: float, ladder) -> int:
    """value 越过 ladder 中第几个台阶 (0=一个都没过)。"""
    n = 0
    for i, th in enumerate(ladder, 1):
        if value >= th:
            n = i
    return n


def derive_events(prev: SessionMemo | None, row: dict, now: float, cfg=None):
    """纯函数: 给定上一次记忆 + 当前 session 行 -> (新事件列表, 新记忆)。无 I/O, 可单测。

    prev=None 表示第一次见到这个 session: 播种记忆 (调用方 pump 在冷启动基线 tick 会丢弃返回的事件)。
    """
    state = row.get("state")
    sid = row.get("session_id")
    proj = row.get("project")
    la = row.get("last_activity_epoch")
    age = row.get("last_activity_age_s") or 0
    facts = row.get("recent_events") or []
    fact_eps = [e.get("epoch") for e in facts if e.get("epoch")]
    max_fact = max(fact_eps) if fact_eps else int(la or 0)
    cur_idle = _step(age, IDLE_STEPS) if state == "AWAITING_USER" else 0
    cur_stuck = _step(age, STUCK_STEPS) if state == "AMBIGUOUS_PENDING" else 0
    blocked_at = row.get("proc_status_at") if state == "BLOCKED_ON_USER" else None
    blocked_due = bool(blocked_at) and now - blocked_at >= BLOCKED_ALERT_S

    # UNKNOWN / 读不出 / CLOSED: 不报警; **保留**游标与台阶记忆 (别在抖动里清零, 否则恢复时会重新触发)
    if state in _DEAD_STATES:
        memo = prev or SessionMemo(state, la, max_fact, 0, 0)
        evs: list[Event] = []
        # CLOSED = 进程刚退出。若上一拍还在干活, 且 transcript 已证实这一拍完成了任务/报了错, 补发一次事实事件再收尾
        # —— 否则"后台任务跑完的同一拍进程也退出"这类真完成会被 CLOSED 吞掉 (评审 medium#4)。全程基于 transcript 事实, 不合成。
        if state == "CLOSED" and prev is not None and prev.last_state in ("WORKING", "PROCESSING"):
            for e in facts:
                ep = e.get("epoch")
                if not ep or ep <= memo.last_fact_cursor:
                    continue
                if e.get("kind") == "task_completed":
                    evs.append(Event.make("TASK_COMPLETED", session=sid, project=proj, timestamp=ep,
                                          detected_at=now, dedup_key=f"TASK_COMPLETED:{sid}:{int(ep)}"))
                elif e.get("kind") == "tool_returned_error":
                    evs.append(Event.make("TOOL_ERROR", session=sid, project=proj, severity="info",
                                          timestamp=ep, detected_at=now,
                                          dedup_key=f"TOOL_ERROR:{sid}:{int(ep)}", tool_name=e.get("name")))
        return evs, SessionMemo(state, la, max(memo.last_fact_cursor, max_fact),
                                memo.last_idle_step, memo.last_stuck_step)

    # 新 session, 或**从 UNKNOWN 恢复**: 重新播种到"当前", 把游标设到当前 max_fact —— 绝不补发尾部历史。
    # (否则首次见到就是 UNKNOWN/空文件时, 游标会被播成 0, 恢复后整条尾部当成新事件重放, 带陈旧时间戳 -> 误报。)
    # 只有真正的新 session 发 SESSION_STARTED; 从 UNKNOWN 恢复不算新会话, 静默重播种。
    if prev is None or prev.last_state in _DEAD_STATES:
        seeded = SessionMemo(last_state=state, since_epoch=la, last_fact_cursor=max_fact,
                             last_idle_step=cur_idle, last_stuck_step=cur_stuck,
                             blocked_alerted=blocked_at if blocked_due else None)
        started = ([Event.make("SESSION_STARTED", session=sid, project=proj, timestamp=la,
                               detected_at=now, dedup_key=f"SESSION_STARTED:{sid}:{int(la or 0)}")]
                   if prev is None and la else [])
        return started, seeded

    evs: list[Event] = []
    since = prev.since_epoch

    # --- 状态边沿 ---
    if state != prev.last_state:
        since = la
        if (state == "AWAITING_USER" and prev.last_state in ("WORKING", "PROCESSING") and la
                and not row.get("turn_unfinished")):
            evs.append(Event.make("TASK_COMPLETED", session=sid, project=proj, timestamp=la,
                                  detected_at=now, dedup_key=f"TASK_COMPLETED:{sid}:{int(la)}",
                                  state_from=prev.last_state, state_to=state))
        if state == "AMBIGUOUS_PENDING" and la:
            evs.append(Event.make("SESSION_STUCK", session=sid, project=proj, severity="warning",
                                  timestamp=la, detected_at=now,
                                  dedup_key=f"SESSION_STUCK:{sid}:{int(la)}",
                                  state_label=row.get("state_label"), unresolved=True, age_s=int(age)))

    # --- 事实 (transcript 里实有的行): 游标增量 ---
    for e in facts:
        ep = e.get("epoch")
        if not ep or ep <= prev.last_fact_cursor:
            continue
        kind = e.get("kind")
        if kind == "tool_returned_error":
            evs.append(Event.make("TOOL_ERROR", session=sid, project=proj, severity="info",
                                  timestamp=ep, detected_at=now,
                                  dedup_key=f"TOOL_ERROR:{sid}:{int(ep)}", tool_name=e.get("name")))
        elif kind == "task_completed":
            evs.append(Event.make("TASK_COMPLETED", session=sid, project=proj, timestamp=ep,
                                  detected_at=now, dedup_key=f"TASK_COMPLETED:{sid}:{int(ep)}"))
    new_cursor = max(prev.last_fact_cursor, max_fact)

    # --- 时间台阶: 空闲 ---
    new_idle = prev.last_idle_step if state == "AWAITING_USER" else 0
    if state == "AWAITING_USER" and row.get("turn_unfinished") and prev.last_state != "AWAITING_USER":
        new_idle = cur_idle             # 刚从"死掉的回合"转过来: 时钟是那条旧消息, 台阶早过了 —— 静默播种, 不补发
    if state == "AWAITING_USER" and la and cur_idle > new_idle:
        th = IDLE_STEPS[cur_idle - 1]
        evs.append(Event.make("SESSION_IDLE", session=sid, project=proj, severity="warning",
                              timestamp=la + th, detected_at=now,
                              dedup_key=f"SESSION_IDLE:{sid}:step{cur_idle}:{int(la)}",
                              age_s=int(age), idle_step=cur_idle))
        new_idle = cur_idle

    # --- 时间台阶: 久未返回升级 (进入边沿已发 step1; 这里只管再升级) ---
    new_stuck = prev.last_stuck_step if state == "AMBIGUOUS_PENDING" else 0
    if state == "AMBIGUOUS_PENDING" and la and cur_stuck > max(prev.last_stuck_step, 1):
        th = STUCK_STEPS[cur_stuck - 1]
        evs.append(Event.make("SESSION_STUCK", session=sid, project=proj, severity="warning",
                              timestamp=la + th, detected_at=now,
                              dedup_key=f"SESSION_STUCK:{sid}:step{cur_stuck}:{int(la)}",
                              state_label=row.get("state_label"), unresolved=True, age_s=int(age)))
        new_stuck = cur_stuck
    elif state == "AMBIGUOUS_PENDING":
        new_stuck = max(prev.last_stuck_step, cur_stuck, 1)

    # --- 回合卡在你身上 (进程自报 waiting): 持续 BLOCKED_ALERT_S 提醒一次, 每段等待一次 ---
    new_blocked = prev.blocked_alerted if state == "BLOCKED_ON_USER" else None
    if blocked_due and prev.blocked_alerted != blocked_at:
        et = "PERMISSION_NEEDED" if row.get("waiting_for") in _PERMISSION_WAITS else "QUESTION_PENDING"
        evs.append(Event.make(et, session=sid, project=proj, severity="warning",
                              timestamp=blocked_at + BLOCKED_ALERT_S, detected_at=now,
                              dedup_key=f"{et}:{sid}:reg:{int(blocked_at)}",
                              tool_name=row.get("pending_tool_name"), state_label=row.get("state_label"),
                              age_s=int(now - blocked_at)))
        new_blocked = blocked_at

    memo = SessionMemo(last_state=state, since_epoch=since, last_fact_cursor=new_cursor,
                       last_idle_step=new_idle, last_stuck_step=new_stuck, blocked_alerted=new_blocked)
    return evs, memo


# ---- pump: 唯一的只读后台 ticker (让事件不依赖"有没有人在看") ----

_session_memory: dict[str, SessionMemo] = {}
_mem_lock = threading.Lock()
_baseline_done = False
_pump_thread: threading.Thread | None = None
_pump_stop = threading.Event()


def tick(base=None, cfg=None, live_factory=None) -> int:
    """跑一帧: snapshot -> 每个 session derive_events -> bus.emit。返回发出的事件数。冷启动基线丢弃。

    live_factory: 可选的进程活性索引工厂 (serve 注入 procmon.live_claude_index)。本模块不 import procmon,
    只在被注入时调用 —— 让事件也吃到活性消歧 (后台在跑不误报完成 / 死会话不误报卡住), 同时保持 P4 解耦。"""
    global _baseline_done
    live = live_factory() if live_factory is not None else None
    snap = activity.snapshot(base, cfg, live=live)
    now = time.time()
    emitted = 0
    with _mem_lock:
        baseline = not _baseline_done
        live = set()
        for row in snap.get("sessions", []):
            sid = row.get("session_id")
            if not sid:
                continue
            live.add(sid)
            prev = _session_memory.get(sid)
            evs, memo = derive_events(prev, row, now, cfg)
            _session_memory[sid] = memo
            if not baseline and evs:
                bus.emit_many(evs)
                emitted += len(evs)
        for dead in [k for k in _session_memory if k not in live]:   # 文件消失 -> 清记忆, 不伪造事件
            _session_memory.pop(dead, None)
        _baseline_done = True
    return emitted


def _loop(base, cfg, tick_s: float, live_factory=None):
    while not _pump_stop.wait(tick_s):
        try:
            tick(base, cfg, live_factory)
        except Exception:
            pass            # 单次 tick 出错不拖垮 pump (§2.4 渐进降级)


def start_pump(base=None, cfg=None, tick_s: float = 5.0, live_factory=None):
    """懒启动唯一的 pump 守护线程 (由 serve.run_serve 调一次)。先跑一帧基线播种, 再起循环。"""
    global _pump_thread
    if _pump_thread and _pump_thread.is_alive():
        return _pump_thread
    try:
        tick(base, cfg, live_factory)     # 基线: 播种记忆, 不发事件
    except Exception:
        pass
    _pump_stop.clear()
    _pump_thread = threading.Thread(target=_loop, args=(base, cfg, tick_s, live_factory),
                                    name="mc-event-pump", daemon=True)
    _pump_thread.start()
    return _pump_thread


def reset_for_test():
    """单测用: 清空记忆与基线标记。"""
    global _baseline_done
    with _mem_lock:
        _session_memory.clear()
        _baseline_done = False
