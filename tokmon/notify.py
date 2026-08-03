"""Mission Control 通知层 (M3) —— 事件总线的消费者, 平台**唯一**会外发的能力。

§2/§3/§7 落点: 监控只 emit "发生了什么"; 这里 subscribe, 决定"该不该打扰你、怎么打扰"。

铁律:
- **默认不外发 (首个 egress 能力的显式 opt-in)**: 没配 Telegram token+chat_id 就一个字节都不出本机, 只进本地 feed。
- **不阻塞总线**: `on_event` 只做快速内存决策 + 入队; 真正的 HTTP 发送在后台 sender 线程 (总线 fan-out 必须秒回)。
- **内容最小化 (§6)**: 只发 严重度/类型/项目/极简详情, 绝不发命令行/diff/路径/密钥 (事件 payload 本就不含, 这里再收一道)。
- **有用且不烦 (§7)**: 严重度门控 + 静默时段(只放行 critical) + 去抖(同 类型+会话) + 限流。默认静默胜过默认吵。
- **token 不外露**: 任何 API/feed 只暴露 "已配置/未配置", 绝不回显 token。
- **P4**: 只 import stdlib + events; 不碰任何 pillar。

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
}


@dataclass
class NotifyConfig:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    push_min_severity: str = "warning"   # info|warning|critical 起步门槛 (info 永不推送, 只进时间线)
    quiet_start: int | None = None       # 静默时段本地小时 [start,end); None=不启用
    quiet_end: int | None = None
    debounce_s: int = 120                # 同 (类型,会话) 去抖窗口
    rate_max: int = 10                   # rate_window_s 内最多推送条数
    rate_window_s: int = 300
    enabled_types: set | None = None     # None = 全部类型; 否则只推这些

    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def load_config() -> NotifyConfig:
    """从环境变量 + 可选 ~/.tokmon/notify.json 读取。默认 Telegram 关 (无 token)。"""
    cfg = NotifyConfig()
    cfg.telegram_token = os.environ.get("MC_TELEGRAM_TOKEN", "").strip()
    cfg.telegram_chat_id = os.environ.get("MC_TELEGRAM_CHAT_ID", "").strip()
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
            if not self.cfg.telegram_configured():
                with self._lock:
                    rec["delivery"] = "未配置(仅本地)"
                    self.sent += 1                # 已"推送"(本地), 只是没外发
                continue
            text = f"[{rec['severity']}] " + rec["summary"]
            try:
                ok = _telegram_send(self.cfg.telegram_token, self.cfg.telegram_chat_id, text)
                info = "telegram-已送达" if ok else "telegram-错误:状态码"
            except Exception as e:
                ok, info = False, "telegram-错误:" + type(e).__name__
            with self._lock:
                rec["delivery"] = info
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

    def send_test(self) -> dict:
        """用户主动触发的一条测试通知, 走真实通道, 验证配置。"""
        text = "[test] Claude Mission Control 测试通知 · 若你在手机上看到这条, Telegram 已打通。"
        if not self.cfg.telegram_configured():
            return {"ok": False, "detail": "Telegram 未配置 (设 MC_TELEGRAM_TOKEN / MC_TELEGRAM_CHAT_ID 后重启)"}
        try:
            ok = _telegram_send(self.cfg.telegram_token, self.cfg.telegram_chat_id, text)
            return {"ok": ok, "detail": "已尝试发送 (查收手机)" if ok else "发送失败: 状态码非 2xx"}
        except Exception as e:
            return {"ok": False, "detail": "发送失败: " + type(e).__name__}

    def status(self) -> dict:
        with self._lock:
            return {
                "telegram_configured": self.cfg.telegram_configured(),
                "push_min_severity": self.cfg.push_min_severity,
                "quiet_hours": (None if self.cfg.quiet_start is None
                                else [self.cfg.quiet_start, self.cfg.quiet_end]),
                "debounce_s": self.cfg.debounce_s,
                "rate_max": self.cfg.rate_max, "rate_window_s": self.cfg.rate_window_s,
                "sent": self.sent, "suppressed": self.suppressed, "errors": self.errors,
                "feed": [dict(r) for r in list(self.feed)[-150:]],   # 锁内深拷贝, 避免与 sender 线程改 rec 撕裂读

                "egress": "仅本地(默认)" if not self.cfg.telegram_configured() else "Telegram 出站",
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
