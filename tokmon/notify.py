"""Mission Control 通知层 (M3) —— 事件总线的消费者, 平台**唯一**会外发的能力。

§2/§3/§7 落点: 监控只 emit "发生了什么"; 这里 subscribe, 决定"该不该打扰你、怎么打扰"。

铁律:
- **默认不外发 (首个 egress 能力的显式 opt-in)**: 没配 Telegram token+chat_id 就一个字节都不出本机, 只进本地 feed。
- **不阻塞总线**: `on_event` 只做快速内存决策 + 入队; 真正的 HTTP 发送在后台 sender 线程 (总线 fan-out 必须秒回)。
- **内容最小化 (§6)**: 只发 严重度/类型/项目/极简详情, 绝不发命令行/diff/路径/密钥 (事件 payload 本就不含, 这里再收一道)。
- **有用且不烦 (§7)**: 严重度门控 + 静默时段(只放行 critical) + 去抖(同 类型+会话) + 限流。默认静默胜过默认吵。
- **token 不外露**: 任何 API/feed 只暴露 "已配置/未配置", 绝不回显 token。
- **P4**: 只 import stdlib + events; 不碰任何 pillar。
- **通道是可插拔 output (§6)**: Telegram(双向的一半: 现只出) + Pushover(只出)。**全部默认关**,
  配了哪个发哪个, 都不配就一个字节不出本机。加新通道 = 加一个 `_xxx_send` + 进 `_deliver()`, 不动策略层。

策略函数 `decide` 是纯函数, 易单测 (像 classify_state / derive_events 那样钉行为)。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .events import bus

SEV_RANK = {"info": 0, "warning": 1, "critical": 2}

_TYPE_LABEL = {
    "SESSION_STARTED": "会话开始", "TASK_COMPLETED": "任务完成", "TOOL_ERROR": "工具出错",
    "SESSION_IDLE": "空闲", "SESSION_STUCK": "久未返回", "PERMISSION_NEEDED": "等待授权",
    "TOKEN_BUDGET_WARNING": "预算告警", "PROCESS_CRASHED": "进程崩溃", "COMMAND_ISSUED": "已下指令",
    "DESTRUCTIVE_OP": "破坏性操作", "ERROR_SPIKE": "连续失败", "REPEATED_FILE_EDIT": "改了又改没通过",
    "LARGE_DIFF": "大改动",
}


@dataclass
class NotifyConfig:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    pushover_token: str = ""             # Pushover 的 application/API token
    pushover_user: str = ""              # Pushover 的 user key
    push_min_severity: str = "warning"   # info|warning|critical 起步门槛 (info 永不推送, 只进时间线)
    quiet_start: int | None = None       # 静默时段本地小时 [start,end); None=不启用
    quiet_end: int | None = None
    debounce_s: int = 120                # 同 (类型,会话) 去抖窗口
    rate_max: int = 10                   # rate_window_s 内最多推送条数
    rate_window_s: int = 300
    enabled_types: set | None = None     # None = 全部类型; 否则只推这些

    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def pushover_configured(self) -> bool:
        return bool(self.pushover_token and self.pushover_user)

    def any_channel(self) -> bool:
        """有没有任何外发通道。都没有 -> 一个字节不出本机 (默认状态)。"""
        return self.telegram_configured() or self.pushover_configured()


def load_config() -> NotifyConfig:
    """从环境变量 + 可选 ~/.tokmon/notify.json 读取。默认 Telegram 关 (无 token)。"""
    cfg = NotifyConfig()
    cfg.telegram_token = os.environ.get("MC_TELEGRAM_TOKEN", "").strip()
    cfg.telegram_chat_id = os.environ.get("MC_TELEGRAM_CHAT_ID", "").strip()
    cfg.pushover_token = os.environ.get("MC_PUSHOVER_TOKEN", "").strip()
    cfg.pushover_user = os.environ.get("MC_PUSHOVER_USER", "").strip()
    p = Path.home() / ".tokmon" / "notify.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # 数值字段强制转 int (写成字符串会在 decide/_in_quiet 里抛 TypeError, 被总线吞掉 -> 静默失灵)
            for k in ("quiet_start", "quiet_end", "debounce_s", "rate_max", "rate_window_s"):
                if k in data and data[k] is not None:
                    try:
                        setattr(cfg, k, int(data[k]))
                    except (TypeError, ValueError):
                        pass
            if data.get("push_min_severity") in SEV_RANK:
                cfg.push_min_severity = data["push_min_severity"]
            if not cfg.telegram_token and data.get("telegram_token"):
                cfg.telegram_token = str(data["telegram_token"]).strip()
            if not cfg.telegram_chat_id and data.get("telegram_chat_id"):
                cfg.telegram_chat_id = str(data["telegram_chat_id"]).strip()
            if not cfg.pushover_token and data.get("pushover_token"):
                cfg.pushover_token = str(data["pushover_token"]).strip()
            if not cfg.pushover_user and data.get("pushover_user"):
                cfg.pushover_user = str(data["pushover_user"]).strip()
            if isinstance(data.get("enabled_types"), list):
                cfg.enabled_types = set(data["enabled_types"])
        except Exception:
            pass
    return cfg


def _in_quiet(cfg: NotifyConfig, now: float) -> bool:
    if cfg.quiet_start is None or cfg.quiet_end is None or cfg.quiet_start == cfg.quiet_end:
        return False
    h = time.localtime(now).tm_hour
    s, e = cfg.quiet_start, cfg.quiet_end
    return (s <= h < e) if s < e else (h >= s or h < e)   # 后者跨午夜


def summarize(ev: dict) -> str:
    """§6 极简、可外发的一行 (绝不含命令行/diff/路径/密钥)。"""
    t = ev.get("type")
    p = ev.get("payload") or {}
    label = _TYPE_LABEL.get(t, t)
    who = ev.get("project") or ev.get("session") or ""
    detail = ""
    if t == "SESSION_IDLE":
        detail = f"空闲约 {round((p.get('age_s') or 0) / 60)} 分钟"
    elif t == "SESSION_STUCK":
        detail = "可能长任务/等授权/会话已关闭"
    elif t == "TOOL_ERROR":
        detail = f"{p.get('tool_name') or '?'} 报错"
    elif t == "PERMISSION_NEEDED":
        detail = f"等待授权 · {p.get('tool_name') or '?'}"
    elif t == "DESTRUCTIVE_OP":
        detail = f"{p.get('kind') or '?'}"
    elif t == "ERROR_SPIKE":
        detail = f"连着失败 {p.get('count') or '?'} 次"
    elif t == "REPEATED_FILE_EDIT":
        detail = f"同一个文件来回 {p.get('count') or '?'} 轮"
    elif t == "LARGE_DIFF":
        detail = (f"一个任务动了 {p.get('count')} 个文件" if p.get("rule") == "large-task"
                  else f"单次大改动 {p.get('count') or '?'} 次")
    elif t == "TOKEN_BUDGET_WARNING":
        scope_zh = {"daily": "今日", "weekly": "近7天", "project": "项目"}.get(p.get("scope"), p.get("scope") or "")
        detail = f"{scope_zh}预算已用 {p.get('pct')}%"
    out = f"{label} · {who}" if who else label
    return out + (f" · {detail}" if detail else "")


def decide(ev: dict, cfg: NotifyConfig, last_push: dict, recent_count: int, now: float):
    """纯函数: 该不该推送 + 原因。recent_count = 限流窗口内已推送条数 (由调用方算好)。"""
    sev = SEV_RANK.get(ev.get("severity"), 0)
    if cfg.enabled_types is not None and ev.get("type") not in cfg.enabled_types:
        return False, "type-disabled"
    base = SEV_RANK.get(cfg.push_min_severity, 1)
    quiet = _in_quiet(cfg, now)
    floor = max(base, SEV_RANK["critical"]) if quiet else base
    if sev < floor:
        return False, ("quiet-hours" if quiet and sev >= base else "below-threshold")
    # critical = 优先推送 (§7): 既不被去抖也不被限流挡掉 —— 重要的事必须主动找到你
    is_critical = sev >= SEV_RANK["critical"]
    last = last_push.get((ev.get("type"), ev.get("session")), 0)
    if not is_critical and now - last < cfg.debounce_s:
        return False, "debounce"
    if not is_critical and recent_count >= cfg.rate_max:
        return False, "rate-limit"
    return True, "push"


def _telegram_send(token: str, chat_id: str, text: str, timeout: float = 8.0) -> bool:
    """唯一的出站网络调用。固定打到 api.telegram.org, 仅 sendMessage。"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return 200 <= r.status < 300


def pushover_priority(severity: str) -> int:
    """严重度 -> Pushover 优先级。**纯函数, 单测钉死。**

    critical -> 1 (high: 会突破手机端的 quiet hours —— "进度被你阻塞"就该突破);
    其余      -> 0 (normal)。

    **永不使用 priority 2 (emergency)**: 它会反复重试直到你手动 ack ——
    那正是 §7「有用且不烦」最想避免的东西。一个让人想关掉的通知系统是负价值;
    宁可漏一次, 不做尖叫的闹钟。
    """
    return 1 if SEV_RANK.get(severity, 0) >= SEV_RANK["critical"] else 0


def _pushover_send(token: str, user: str, text: str, severity: str = "warning",
                   timeout: float = 8.0) -> bool:
    """出站网络调用 #2。固定打到 api.pushover.net, 仅 messages.json。内容同 §6 最小化。"""
    data = urllib.parse.urlencode({
        "token": token, "user": user, "message": text,
        "title": "Claude Mission Control",
        "priority": str(pushover_priority(severity)),
    }).encode("utf-8")
    req = urllib.request.Request("https://api.pushover.net/1/messages.json",
                                 data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return 200 <= r.status < 300


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last_push: dict = {}
        self._window: deque = deque()           # 限流: 近 rate_window_s 的推送时间戳
        self.feed: deque = deque(maxlen=300)     # 通知 feed (推送/抑制 都记, 供 UI 复盘调参)
        self._q: "queue.Queue" = queue.Queue()
        self.sent = 0
        self.suppressed = 0
        self.errors = 0
        self._stop = threading.Event()
        self._sender: threading.Thread | None = None

    # --- 总线订阅入口 (在 bus.emit fan-out 里同步调用, 必须快) ---
    def on_event(self, event):
        ev = event.to_dict() if hasattr(event, "to_dict") else event
        sev = SEV_RANK.get(ev.get("severity"), 0)
        if sev < SEV_RANK["warning"]:
            return                               # info 永不进通知 feed (它在时间线里)
        now = time.time()
        with self._lock:
            while self._window and now - self._window[0] > self.cfg.rate_window_s:
                self._window.popleft()
            for k in [k for k, v in self._last_push.items() if now - v > self.cfg.debounce_s]:
                del self._last_push[k]                # 清掉过期去抖键, 防进程长跑内存缓慢增长
            push, reason = decide(ev, self.cfg, self._last_push, len(self._window), now)
            rec = {
                "ts": int(now), "type": ev.get("type"), "project": ev.get("project"),
                "session": ev.get("session"), "severity": ev.get("severity"),
                "decision": "push" if push else "suppress", "reason": reason,
                "summary": summarize(ev), "delivery": None,
            }
            self.feed.append(rec)
            if push:
                self._last_push[(ev.get("type"), ev.get("session"))] = now
                self._window.append(now)
                self._q.put((ev, rec))
            else:
                self.suppressed += 1

    def _sender_loop(self):
        while not self._stop.is_set():
            try:
                ev, rec = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self.cfg.any_channel():
                with self._lock:
                    rec["delivery"] = "未配置(仅本地)"
                    self.sent += 1                # 已"推送"(本地), 只是没外发
                continue
            text = f"[{rec['severity']}] " + rec["summary"]
            results = self._deliver(text, rec.get("severity") or "warning")
            ok = any(r[1] for r in results)       # 任一通道成功即算送达 (通道互为冗余, 单家挂不拖垮全局)
            with self._lock:
                rec["delivery"] = " / ".join(r[2] for r in results)
                if ok:
                    self.sent += 1
                else:
                    self.errors += 1

    def start(self):
        if self._sender and self._sender.is_alive():
            return
        self._stop.clear()
        self._sender = threading.Thread(target=self._sender_loop, name="mc-notify-sender", daemon=True)
        self._sender.start()

    def _deliver(self, text: str, severity: str) -> list:
        """往**所有已配置**的通道各发一次。返回 [(通道名, 成功?, 人读的结果)]。

        单个通道抛异常/超时只记在自己那一条上, 绝不影响别的通道 (原则 4 渐进降级)。
        """
        out = []
        if self.cfg.telegram_configured():
            try:
                ok = _telegram_send(self.cfg.telegram_token, self.cfg.telegram_chat_id, text)
                out.append(("telegram", ok, "telegram-已送达" if ok else "telegram-错误:状态码"))
            except Exception as e:
                out.append(("telegram", False, "telegram-错误:" + type(e).__name__))
        if self.cfg.pushover_configured():
            try:
                ok = _pushover_send(self.cfg.pushover_token, self.cfg.pushover_user, text, severity)
                out.append(("pushover", ok, "pushover-已送达" if ok else "pushover-错误:状态码"))
            except Exception as e:
                out.append(("pushover", False, "pushover-错误:" + type(e).__name__))
        return out

    def send_test(self, severity: str = "warning") -> dict:
        """用户主动触发的一条测试通知, 走真实通道, 验证配置。"""
        text = "[test] Claude Mission Control 测试通知 · 若你在手机上看到这条, 手机环已经闭上了。"
        if not self.cfg.any_channel():
            return {"ok": False,
                    "detail": "没有任何通道 (Telegram: MC_TELEGRAM_TOKEN+MC_TELEGRAM_CHAT_ID; "
                              "Pushover: MC_PUSHOVER_TOKEN+MC_PUSHOVER_USER; 设好后重启)"}
        results = self._deliver(text, severity)
        return {"ok": any(r[1] for r in results),
                "detail": " / ".join(r[2] for r in results) or "无通道"}

    def status(self) -> dict:
        with self._lock:
            return {
                "telegram_configured": self.cfg.telegram_configured(),
                "pushover_configured": self.cfg.pushover_configured(),
                "channels": [c for c, on in (("telegram", self.cfg.telegram_configured()),
                                             ("pushover", self.cfg.pushover_configured())) if on],
                "push_min_severity": self.cfg.push_min_severity,
                "quiet_hours": (None if self.cfg.quiet_start is None
                                else [self.cfg.quiet_start, self.cfg.quiet_end]),
                "debounce_s": self.cfg.debounce_s,
                "rate_max": self.cfg.rate_max, "rate_window_s": self.cfg.rate_window_s,
                "sent": self.sent, "suppressed": self.suppressed, "errors": self.errors,
                "feed": [dict(r) for r in list(self.feed)[-150:]],   # 锁内深拷贝, 避免与 sender 线程改 rec 撕裂读

                "egress": ("仅本地(默认)" if not self.cfg.any_channel() else
                           " + ".join(c for c, on in (("Telegram", self.cfg.telegram_configured()),
                                                      ("Pushover", self.cfg.pushover_configured())) if on) + " 出站"),
            }


_notifier: Notifier | None = None


def start_notifier(cfg: NotifyConfig | None = None) -> Notifier:
    """懒启动通知层并订阅总线 (由 serve.run_serve 调一次)。"""
    global _notifier
    if _notifier is not None:
        return _notifier
    _notifier = Notifier(cfg or load_config())
    _notifier.start()
    bus.subscribe(_notifier.on_event)           # §3: 通知层是总线的消费者
    return _notifier


def get_notifier() -> Notifier | None:
    return _notifier
