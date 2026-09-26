"""Mission Control 事件总线 (M2)。**支柱无关的中立基础设施**。

§3 三层架构的中间层: 监控层把"发生了什么"emit 进来, 消费者(UI timeline、未来的 M3 通知、控制层审计)subscribe 出去。
这是 P2/P3 解耦的落点: 生产者只调 `emit`, 永不直接调通知/UI/控制; 消费者只读 §4 typed Event。

**铁律 (本模块绝不破坏)**:
- **本模块 import 任何 pillar 都不允许** —— 它只认中立的 Event。各支柱的 `event_sources/*_source.py` 适配器
  才 import 自己那个 pillar 的 snapshot (P4 结构性独立)。
- **本模块不含任何 §7 策略** (严重度门控 / 静默时段 / 防打扰去抖 / 通道)。这些是 M3 *消费者*边的事。
  这里唯一的"去抖"是**结构性幂等**(同一逻辑事件永不重复), 不是"该不该打扰你"的决定。
- M2 只 emit, 不注册任何 subscriber, 不发任何通知 (see-first)。

幂等是两层保险, 误报/重复要两层同时失效才会发生: ① 源头边沿触发+单调台阶记忆; ② 这里的 dedup_key 窗口。
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field

# §4「默认严重度」表 —— 编码在唯一一处。加事件类型 = 在这里加一行。
_DEFAULT_SEVERITY = {
    "SESSION_STARTED": "info",
    "TASK_COMPLETED": "info",
    "TOOL_ERROR": "info",
    "SESSION_IDLE": "warning",
    "SESSION_STUCK": "warning",
    "PERMISSION_NEEDED": "warning",   # M4: 真实检测到"正在等授权" (经 PermissionRequest hook, 或 Claude Code 进程自报 waiting)
    "QUESTION_PENDING": "warning",    # 进程自报 waiting 且不是授权: Claude 在问你 / 有对话框等你 (PERMISSION_NEEDED 的亲兄弟)
    # 预留 (此处仅登记默认严重度):
    "TOKEN_BUDGET_WARNING": "warning",
    "PROCESS_CRASHED": "critical",
    "COMMAND_ISSUED": "info",
    # 改动与风险 (§4 质量/风险类): 规则在真实数据上审过精确率之前一律 info (误报零容忍) —— notify 不推 info
    "DESTRUCTIVE_OP": "info",
    "ERROR_SPIKE": "info",
    "REPEATED_FILE_EDIT": "info",
    "LARGE_DIFF": "info",
    # 省钱 (CONTEXT_COST_PLAN S3): 正在跑的会话上下文刚过 30 万; 浏览器铃铛只有你勾了才弹
    "CONTEXT_LARGE": "info",
}
EVENT_TYPES = set(_DEFAULT_SEVERITY)

# §6 内容最小化: payload 只允许这些 key, 其余一律丢弃 (绝不让命令行/diff/路径/密钥进事件)。
_ALLOWED_PAYLOAD = {
    "tool_name", "state_from", "state_to", "age_s", "idle_step",
    "unresolved", "state_label", "kind", "model", "branch",
    "scope", "pct",                    # 预算告警: 预算口径 + 百分比 (非敏感)
    "rule", "count",                   # 风险事件: 规则名 + 计数 (不带路径 / 命令 / 文件名)
}   # 故意不含 last_text/title/current_step 等对话内容 —— 事件/通知绝不带会话正文 (§6)


@dataclass
class Event:
    type: str
    pillar: str = "activity"
    severity: str = "info"
    session: str | None = None
    project: str | None = None
    timestamp: float | None = None     # 逻辑事件时刻 (事实=消息ts; 时间事件=跨阈那一刻); 绝不是 now()/mtime
    detected_at: float | None = None   # 观察到它的那次 tick 的墙钟 (调试/迟到补发用)
    dedup_key: str = ""
    payload: dict = field(default_factory=dict)
    seq: int = -1                      # 由总线 emit 时赋的单调 id, 消费者据此做游标

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "type": self.type, "pillar": self.pillar,
            "severity": self.severity, "session": self.session, "project": self.project,
            "timestamp": self.timestamp, "detected_at": self.detected_at,
            "dedup_key": self.dedup_key, "payload": self.payload,
        }

    @classmethod
    def make(cls, type: str, *, pillar: str = "activity", severity: str | None = None,
             session: str | None = None, project: str | None = None,
             timestamp: float | None = None, detected_at: float | None = None,
             dedup_key: str | None = None, **payload) -> "Event":
        sev = severity or _DEFAULT_SEVERITY.get(type, "info")
        clean = {k: v for k, v in payload.items() if k in _ALLOWED_PAYLOAD}  # §6 allow-list
        key = dedup_key or f"{type}:{session}:{int(timestamp or 0)}"
        return cls(type=type, pillar=pillar, severity=sev, session=session, project=project,
                   timestamp=timestamp, detected_at=detected_at, dedup_key=key, payload=clean)


class EventBus:
    """内存事件总线: 有界 ring + 单调 seq + dedup 窗口 + 同步 push 订阅。纯 stdlib。"""

    def __init__(self, ring_size: int = 2000, dedup_window: int = 4000):
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._recent_keys: "OrderedDict[str, int]" = OrderedDict()
        self._dedup_window = dedup_window
        self._subscribers: list = []
        self._seq = 0
        self._dropped = 0           # 被有界 ring 轮转挤掉的历史事件数 (可能已被读过; 仅诚实暴露 ring 上限)
        self._lock = threading.Lock()

    def emit(self, event: Event) -> Event:
        with self._lock:
            if event.dedup_key in self._recent_keys:
                self._recent_keys.move_to_end(event.dedup_key)   # 刷新: 持续被重发的键(如长期越线的预算)不该老化出窗后又重响
                return event                      # 幂等: 同一逻辑事件不重复入队、不重复 fan-out
            self._seq += 1
            event.seq = self._seq
            if len(self._ring) == self._ring.maxlen:
                self._dropped += 1                # 即将被轮转挤掉一条历史 (可能已读)
            self._ring.append(event)
            self._recent_keys[event.dedup_key] = self._seq
            while len(self._recent_keys) > self._dedup_window:
                self._recent_keys.popitem(last=False)
            subs = list(self._subscribers)
        for fn in subs:                           # 锁外 fan-out; 单个坏消费者不拖垮生产者/其它消费者
            try:
                fn(event)
            except Exception:
                pass
        return event

    def emit_many(self, events) -> list:
        return [self.emit(e) for e in events]

    def subscribe(self, fn):
        """注册同步回调 (M3 通知 / 控制层审计在此挂)。返回 unsubscribe。M2 不注册任何 subscriber。"""
        with self._lock:
            self._subscribers.append(fn)

        def _unsub():
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)
        return _unsub

    def since(self, seq: int = 0, *, types=None, pillar: str | None = None,
              session: str | None = None, limit: int = 500) -> dict:
        with self._lock:
            out = []
            for ev in self._ring:                # ring 是 旧->新 顺序
                if ev.seq <= seq:
                    continue
                if types and ev.type not in types:
                    continue
                if pillar and ev.pillar != pillar:
                    continue
                if session and ev.session != session:
                    continue
                out.append(ev.to_dict())
            truncated = len(out) > limit
            page = out[:limit]                   # 给最旧的一批; 多于 limit 时分多次 poll 排空, 不跳过
            # 游标只前进到"真正发出去"的最后一条 —— 截断时不能跳到 _seq, 否则没给的会丢
            last_seq = page[-1]["seq"] if (truncated and page) else self._seq
            dropped = self._dropped
        return {"events": page, "last_seq": last_seq, "dropped": dropped}

    def snapshot_meta(self) -> dict:
        with self._lock:
            return {"last_seq": self._seq, "dropped": self._dropped,
                    "subscribers": len(self._subscribers), "buffered": len(self._ring)}

    def clear(self):
        with self._lock:
            self._ring.clear()
            self._recent_keys.clear()
            self._seq = 0
            self._dropped = 0


bus = EventBus()        # 进程内单例 (同 activity/procmon 的模块单例风格)
