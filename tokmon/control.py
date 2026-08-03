"""Mission Control 控制层 (M4 v0) —— 唯一的**入站控制**: 从网页/手机远程审批 Claude Code 的 permission。

机制 (经研究确认, 干净、官方): Claude Code 的 `PermissionRequest` hook 用 `type:"http"` POST 到本服务,
本服务据「你」的远程决定回 allow/deny。无 OS 按键注入。

严格落 §8 P7 控制层守则:
- ① allow-list: 只做 permission 审批这一种动作, 不执行任意指令。
- ② 你显式触发: 系统**永不自动**决定; 决定只来自你在 UI 上的点击; 超时=不决定。
- ③ 鉴权: hook 端点校验 control token (防本机其它进程伪造)。
- ④ 全审计: 每个决定进审计日志 + 发 `COMMAND_ISSUED` 事件进总线。
- ⑤ 失败安全 (铁律): token 错 / 超时 / 服务异常 / 远程模式关 -> 一律**回退正常本地弹窗** (defer),
  **绝不**自动 allow/deny, 绝不伪装成功。

激活: 默认 `remote_mode=False` -> 每个 permission 只做检测(发事件)然后 defer 本地弹窗, 不打扰你的日常流程;
你在 UI 打开「远程审批模式」(人离开时) -> 才阻塞等你的远程决定。

P4: 只 import stdlib + events (+ 纯函数 project 身份工具)。不碰 pillar 采集 / notify / serve。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from .events import Event, bus
from .project import workspace_identity

_TOKEN_PATH = Path.home() / ".tokmon" / "control_token"
_WAIT_S = 25.0          # 阻塞等远程决定的上限 (要 < hook 的 timeout, 给回退留余量)
_MAX_PENDING = 32       # 并发待审批硬上限 (超出 -> defer 失败安全, 防 hook 洪水耗尽线程)


def _load_or_create_token() -> str:
    try:
        if _TOKEN_PATH.exists():
            t = _TOKEN_PATH.read_text(encoding="utf-8").strip()
            if t:
                return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    try:
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(_TOKEN_PATH.parent, 0o700)     # POSIX 收紧; Windows 上基本是 no-op
        except OSError:
            pass
        # 原子地以 0600 创建, 避免短暂的 world-readable 窗口 (令牌不该被同机别的用户读到)
        fd = os.open(_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(t)
    except OSError:
        try:
            _TOKEN_PATH.write_text(t, encoding="utf-8")
        except OSError:
            pass
    return t


def _project_of(cwd) -> str | None:
    try:
        wid = workspace_identity(cwd) if cwd else None
        return wid.project if wid else None
    except Exception:
        return None


def _tool_summary(tool: str, tinput) -> str:
    """给本地审批看的、够你做判断的预览 (本机自己的命令, 截断即可)。"""
    if isinstance(tinput, dict):
        for k in ("command", "file_path", "path", "url", "pattern", "query"):
            v = tinput.get(k)
            if v:
                return f"{tool}: {str(v).replace(chr(10), ' ')[:200]}"
    return tool


class _Pending:
    __slots__ = ("id", "session", "project", "tool", "summary", "created", "ev", "decision")

    def __init__(self, pid, session, project, tool, summary):
        self.id = pid
        self.session = session
        self.project = project
        self.tool = tool
        self.summary = summary
        self.created = time.time()
        self.ev = threading.Event()
        self.decision: str | None = None


class ControlPlane:
    def __init__(self):
        self.token = _load_or_create_token()
        self.remote_mode = False
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._audit: deque = deque(maxlen=200)
        self._seq = 0

    def check_token(self, tok) -> bool:
        return bool(tok) and secrets.compare_digest(str(tok), self.token)

    def set_mode(self, on: bool) -> bool:
        with self._lock:
            self.remote_mode = bool(on)
            return self.remote_mode

    def _record(self, action: str, session, tool, outcome: str):
        self._audit.append({"ts": int(time.time()), "action": action,
                            "session": session, "tool": tool, "outcome": outcome})

    def audit_action(self, kind: str, target: str, outcome: str, session=None, project=None):
        """记录一次控制动作 (终止/释放端口) 到审计 + 发 COMMAND_ISSUED (每次唯一 dedup, 不会被总线去重吞掉)。
        payload 只带 state_label (§6 允许); target 是 pid/name/port 这类非敏感描述, 不含 cmdline/路径。"""
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._record(kind, session, target, outcome)
        bus.emit(Event.make("COMMAND_ISSUED", pillar="control", session=session, project=project,
                            severity="info", timestamp=time.time(),
                            dedup_key=f"COMMAND_ISSUED:{kind}:{seq}",
                            state_label=f"{kind} {target} · {outcome}"))

    # --- hook 入口: 阻塞地拿到决定, 或 defer (失败安全) ---
    def handle_permission(self, payload: dict, token) -> dict:
        if not self.check_token(token):
            return {"_defer": True, "reason": "bad-token"}     # 鉴权失败 -> 不阻塞、不决定
        try:
            session = str(payload.get("session_id") or "")
            tool = str(payload.get("tool_name") or "?")
            tinput = payload.get("tool_input") or {}
            project = _project_of(payload.get("cwd"))
            with self._lock:
                self._seq += 1
                rid = str(self._seq)             # 每个请求唯一 id: 用于事件 dedup + pid, 不再按秒分桶
            # 检测: 不论远程模式开关, 都发一条真实的 PERMISSION_NEEDED (修好 M1 测不准的缺口)
            bus.emit(Event.make("PERMISSION_NEEDED", session=session, project=project,
                                severity="warning", timestamp=time.time(), tool_name=tool,
                                dedup_key=f"PERMISSION_NEEDED:{rid}"))
            if not self.remote_mode:
                return {"_defer": True, "reason": "local-mode"}  # 默认: 回退本地弹窗
            p = _Pending(rid, session, project, tool, _tool_summary(tool, tinput))
            with self._lock:
                if len(self._pending) >= _MAX_PENDING:
                    return {"_defer": True, "reason": "overloaded"}   # 失败安全: 不阻塞、不决定
                self._pending[rid] = p
            p.ev.wait(timeout=_WAIT_S)                            # 阻塞等你点
            with self._lock:
                self._pending.pop(rid, None)
                dec = p.decision if p.decision in ("allow", "deny") else None  # 锁内取决定为准, 化解超时竞态
            if dec is None:
                self._record("permission", session, tool, "timeout→defer")
                return {"_defer": True, "reason": "timeout"}     # 超时 -> 回退本地, 不替你决定
            self._record("permission", session, tool, dec)
            bus.emit(Event.make("COMMAND_ISSUED", pillar="control", session=session, project=project,
                                severity="info", timestamp=time.time(),
                                dedup_key=f"COMMAND_ISSUED:{rid}",
                                state_label=f"permission {dec} · {tool}"))
            return {"decision": dec}
        except Exception:
            return {"_defer": True, "reason": "error"}           # 任何异常 -> 失败安全 defer

    def resolve(self, pid, decision) -> bool:
        """你在 UI 上点 允许/拒绝 -> 唤醒被阻塞的 hook 请求。"""
        if decision not in ("allow", "deny"):
            return False
        with self._lock:
            p = self._pending.get(str(pid))
            if not p:
                return False
            p.decision = decision
            p.ev.set()
            return True

    def status(self) -> dict:
        with self._lock:
            pend = [{"id": p.id, "session": p.session, "project": p.project, "tool": p.tool,
                     "summary": p.summary, "age_s": int(time.time() - p.created)}
                    for p in self._pending.values()]
            audit = list(self._audit)[-60:]
        return {"remote_mode": self.remote_mode, "pending": pend, "audit": audit,
                "token_set": bool(self.token)}      # 绝不回显 token 本体

    def hook_config(self, base_url: str) -> dict:
        """给用户手动粘进 ~/.claude/settings.json 的精确片段 (含 token + 本服务 URL)。"""
        url = f"{base_url}/hook/permission?token={self.token}"
        snippet = {
            "hooks": {
                "PermissionRequest": [
                    {"matcher": "*", "hooks": [{"type": "http", "url": url, "timeout": 30}]}
                ]
            }
        }
        return {"url": url, "snippet": snippet}


def shape_hook_response(result: dict) -> dict:
    """把内部结果转成 Claude Code PermissionRequest hook 的回包形状。
    defer = 不带 decision (走默认本地流程); 否则 allow/deny。
    (注: 确切字段以真实 Claude Code 文档/实测为准, 这里取已记录的形状; defer 永远安全。)"""
    base = {"hookSpecificOutput": {"hookEventName": "PermissionRequest"}}
    if result.get("_defer"):
        return base
    dec = result.get("decision")
    if dec in ("allow", "deny"):
        base["hookSpecificOutput"]["decision"] = {"behavior": dec}
    return base


plane = ControlPlane()
