"""下一代 · steer 机械层 (S1-v2) —— **Agent SDK 驱动 + `canUseTool`**, 拿到「远程多选按钮」。

RUNNER_SDK_PLAN.md 的 runner: 把「给一个会话发一条 prompt」落成一次 **SDK 驱动的回合**;
持久的只有 `session_id`(UUID); 每回合起一个 client, 用完即弃 —— 某回合崩只崩那一回合, 绝不污染活 transcript。

**为什么换掉 `claude -p`** (2026-07 实测, 三条都不是推测):
- `-p` 是一次性回合, 没有可返回的交互循环 —— 模型把多选题**打成纯文本就结束回合**, 压根不调「问用户」的工具。结构性不可能。
- 只有 `canUseTool` 能拦下 `AskUserQuestion`、拿到结构化 `{questions, options}`、把你的选择塞回会话。
- **A0 spike 已钉死** (2026-07-19): canUseTool **确实**收到 AskUserQuestion; `PermissionResultAllow(updated_input={...,"answers":{问题: label}})`
  塞回后会话**带着你的选择继续**; SDK 会话**照样**把 transcript 写进 `~/.claude/projects/` -> 现有支柱白捡监控成立。

**P4 (支柱之上, 单向依赖)**: 本模块只 import stdlib + Agent SDK。不碰任何采集内核, 也不 import events/notify ——
**审计与 `COMMAND_ISSUED` 是 `control.plane` 的事 (策略/机械分离)**。runner 只管干活。

**失败安全 (P7 铁律)**: SDK 未装 / 二进制找不到 / 空 prompt / 会话崩 / 超时 -> 一律**明确报「做不到」**, 绝不伪装成功。
⚠️ **提问超时必须 deny, 绝不替你挑一个选项**; **作答不全也绝不当成已作答** —— 那都等于伪造你的决定 (PLAN §5.2)。

**§6.1 async↔线程桥**: SDK 是 asyncio, `tokmon serve` 是 ThreadingHTTPServer。
故 SDK 事件循环跑在**专用后台线程**; `canUseTool` 里 `await` 一个 asyncio.Event;
HTTP 线程提交答案时用 `loop.call_soon_threadsafe(...)` 去 set 它 —— **绝不**在 HTTP 线程直接碰 asyncio 对象。
唤醒用的是**该 ask 自己记下的 loop**, 不是 `self._loop` (万一起过第二个 loop, 用错 loop 会让唤醒石沉大海)。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
import uuid as _uuidlib
from dataclasses import dataclass, field
from pathlib import Path

try:                                    # 唯一新依赖 (NORTH_STAR 零依赖偏好在此有意破例, 换「真·多选按钮」)
    from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient,
                                  PermissionResultAllow, PermissionResultDeny)
    _HAS_SDK = True
except ImportError:                     # 缺 SDK -> steer 明确报「做不到」, 不假装
    _HAS_SDK = False

# ---- 二进制解析 (spike: claude.exe 在 ~/.local/bin, 不在 PATH) ----
_KNOWN_BINS = [
    Path.home() / ".local" / "bin" / "claude.exe",
    Path.home() / ".local" / "bin" / "claude",
    Path.home() / ".claude" / "local" / "claude.exe",
    Path.home() / ".claude" / "local" / "claude",
]
if os.environ.get("APPDATA"):
    _KNOWN_BINS += [Path(os.environ["APPDATA"]) / "npm" / "claude.cmd",
                    Path(os.environ["APPDATA"]) / "npm" / "claude"]


def resolve_claude() -> str | None:
    """PATH -> 已知安装位置回退。找不到 -> None (调用方据此失败安全报「做不到」)。"""
    p = shutil.which("claude")
    if p:
        return p
    for c in _KNOWN_BINS:
        try:
            if c.exists():
                return str(c)
        except OSError:
            pass
    return None


def clean_env() -> dict:
    """净化「嵌套标记」(继承 CLAUDE_CODE_* 会让子 claude 侦测到嵌套而 hang 死)。

    ⚠️ SDK 语义: transport 把 `options.env` **叠加**在 os.environ 之上 (`{**inherited, **options.env}`),
    所以「不写这个 key」**删不掉**它 —— 必须显式覆盖成空串 (pop 在 overlay 语义下等于没做)。
    例外 `CLAUDE_CODE_ENTRYPOINT`: SDK 自己要把它设成 sdk-py, 我们别覆盖它。"""
    env = dict(os.environ)
    for k in list(env):
        if (k == "CLAUDECODE" or k.startswith("CLAUDE_CODE")) and k != "CLAUDE_CODE_ENTRYPOINT":
            env[k] = ""            # 覆盖成空串, 而非 pop
    return env


# ---- 答案归一 (纯函数, 单测钉住): UI 送来的选择 -> SDK updated_input["answers"] ----
def normalize_answers(questions, picked) -> dict:
    """把 {问题文本 or index: label(s)} 归一成 {问题文本: label}。多选合成逗号串。
    只认 questions 里**真实存在**的问题与选项 —— 伪造的 label 一律丢弃 (绝不把没给过的选项当你的决定)。"""
    out: dict = {}
    if not isinstance(picked, dict):
        return out
    for i, q in enumerate(questions or []):
        if not isinstance(q, dict):
            continue
        qtext = q.get("question")
        valid = {o.get("label") for o in (q.get("options") or []) if isinstance(o, dict)}
        v = picked.get(qtext)
        if v is None:
            v = picked.get(str(i))                 # 也接受按序号提交
        if v is None:
            continue
        labels = v if isinstance(v, list) else [v]
        keep = [str(x) for x in labels if str(x) in valid]   # 只认真实选项
        if keep:
            out[qtext] = ", ".join(keep) if len(keep) > 1 else keep[0]
    return out


def missing_questions(questions, answers) -> list:
    """还没作答的问题文本。**多问题必须答全才算数** —— 部分作答绝不解决整个提问 (P7: 答案只能来自你的点击)。"""
    need = [q.get("question") for q in (questions or []) if isinstance(q, dict)]
    have = set(answers or {})
    return [q for q in need if q not in have]


@dataclass
class Turn:
    session_id: str
    project: str | None
    cwd: str
    started: float
    resume: bool = False
    done: bool = False
    ok: bool = False
    text: str = ""
    cost_usd: float = 0.0
    is_error: bool = False
    reason: str = ""
    kinds: list = field(default_factory=list)
    awaiting_ask: str | None = None                 # 正卡在哪个待答问题 (UI 用)
    _stream: list = field(default_factory=list)     # 实时文本增量 (UI 流式, 尾部有界)


@dataclass
class PendingAsk:
    """一个待你作答的提问。照抄 control._Pending 的挂起/唤醒套路 (PLAN §3)。"""
    id: str
    session_id: str
    project: str | None
    questions: list
    created: float
    ev: object = None                               # asyncio.Event —— **只在 loop 线程碰**
    loop: object = None                             # 该 ask 所属的 loop (唤醒必须用它, 别用 self._loop)
    answers: dict | None = None                     # 已累积的作答 (可分次点)
    resolved: bool = False


_TURN_TIMEOUT_S = 1800.0      # 单回合硬上限 (含等你作答的时间); 到点取消 -> client __aexit__ 收子进程
_ASK_TIMEOUT_S = 600.0        # 等你点按钮的上限; 超时 -> deny「用户未作答」, **绝不替你选**
_DEFAULT_BUDGET_USD = 0.50    # 每回合成本封顶 (防 steer 出的 agent 烧穿)
_MAX_ASKS = 32                # 并发待答硬上限 (失败安全, 防堆积)
_MAX_TURNS_KEPT = 64          # 只留最近这么多回合 (老的已完成回合先淘汰), 防无界增长
_MAX_STREAM_CHUNKS = 512      # 单回合流式缓冲块数上限 (只留尾部)


class Runner:
    """进程内单例。每个 session_id 记最近一回合; 另记待答提问表。纯机械, 无策略/无审计。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._turns: dict[str, Turn] = {}
        self._asks: dict[str, PendingAsk] = {}
        self._loop = None

    # --- 只读视图 (供 serve/UI) ---
    def available(self) -> bool:
        return _HAS_SDK and resolve_claude() is not None

    def status(self) -> dict:
        with self._lock:
            return {"claude_found": resolve_claude() is not None, "sdk_installed": _HAS_SDK,
                    "sessions": [self._view(t) for t in self._turns.values()]}

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            t = self._turns.get(session_id)
            return self._view(t) if t else None

    def _view(self, t: Turn) -> dict:
        return {"session_id": t.session_id, "project": t.project, "cwd": t.cwd,
                "resume": t.resume, "done": t.done, "ok": t.ok, "is_error": t.is_error,
                "text": t.text, "stream": "".join(t._stream)[-4000:],
                "cost_usd": round(t.cost_usd, 4), "reason": t.reason,
                "awaiting_ask": t.awaiting_ask,
                "age_s": int(time.time() - t.started)}

    # --- 待答提问 (供 GET /api/control/asks 与 POST /api/control/answer) ---
    def asks(self) -> list[dict]:
        """待答提问列表。问题正文只走这个本机 token 端点给 UI 渲染按钮; **不进事件/通知** (§5.4)。"""
        with self._lock:
            return [{"id": a.id, "session_id": a.session_id, "project": a.project,
                     "questions": a.questions, "answered": dict(a.answers or {}),
                     "age_s": int(time.time() - a.created)}
                    for a in self._asks.values() if not a.resolved]

    def answer(self, ask_id: str, picked) -> dict:
        """你在 UI 上点了选项 -> (答全了才) 唤醒被阻塞的 canUseTool。
        失败安全: id 对不上 / 会话已死 / 无有效选项 -> 明确报「做不到」, **绝不**报假成功。
        多问题: 分次点击会累积; **答全之前不唤醒**, 绝不让会话收到半份答案。"""
        with self._lock:
            a = self._asks.get(str(ask_id))
            if not a or a.resolved:
                return {"ok": False, "reason": "unknown-ask"}
            tt = self._turns.get(a.session_id)
            if tt is None or tt.done:              # 回合已死 -> 没人接收, 绝不报「已作答」
                self._asks.pop(str(ask_id), None)
                return {"ok": False, "reason": "turn-gone"}
            answers = normalize_answers(a.questions, picked)
            if not answers:
                return {"ok": False, "reason": "no-valid-option"}   # 伪造/空选择 -> 不当成你的决定
            merged = dict(a.answers or {})
            merged.update(answers)
            a.answers = merged
            missing = missing_questions(a.questions, merged)
            if missing:                            # 还有题没答 -> 继续挂着等你, 不唤醒
                return {"ok": True, "ask_id": str(ask_id), "answered": len(merged),
                        "pending": missing}
            a.resolved = True
            loop, ev, n = a.loop, a.ev, len(merged)
        if loop is None or ev is None:
            return {"ok": False, "reason": "loop-gone"}
        loop.call_soon_threadsafe(ev.set)      # §6.1: 跨线程唤醒的唯一正确姿势 (用该 ask 自己的 loop)
        return {"ok": True, "ask_id": str(ask_id), "answered": n}

    # --- asyncio 事件循环 (专用后台线程) ---
    def _ensure_loop(self):
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop                  # 用 is_closed 而非 is_running: 启动窗口内不会误起第二个
            loop = asyncio.new_event_loop()
            started = threading.Event()
            threading.Thread(target=self._loop_forever, args=(loop, started),
                             name="runner-loop", daemon=True).start()
            self._loop = loop
        started.wait(timeout=5)                    # 等 loop 真的跑起来再交出去 (消除启动竞态)
        return loop

    @staticmethod
    def _loop_forever(loop, started):
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        loop.run_forever()

    # --- 启动一回合 (调度到 loop, 立即返回 handle) ---
    def steer(self, cwd: str, prompt: str, *, session_id: str | None = None,
              project: str | None = None, model: str | None = None,
              budget_usd: float = _DEFAULT_BUDGET_USD) -> dict:
        """spawn(session_id=None) 或 resume(带 session_id) 一回合。
        注意: **不校验 cwd / 不做鉴权 / 不审计** —— 那些是 control.plane 的 P7 gate 的事, runner 只管跑。"""
        resume = bool(session_id)
        sid = session_id or str(_uuidlib.uuid4())
        t = Turn(session_id=sid, project=project, cwd=cwd, started=time.time(), resume=resume)
        with self._lock:
            self._turns[sid] = t
            self._evict_turns_locked(sid)
        if not _HAS_SDK:
            t.done, t.ok, t.reason = True, False, "Agent SDK 未安装 (pip install claude-agent-sdk)"
            return self._view(t)
        if not resolve_claude():
            t.done, t.ok, t.reason = True, False, "claude 二进制找不到 (PATH 与 ~/.local/bin 均无)"
            return self._view(t)
        if not prompt or not prompt.strip():
            t.done, t.ok, t.reason = True, False, "空 prompt"
            return self._view(t)
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._run_turn(t, prompt, model, budget_usd), loop)
        # 调度层异常也要落地成「做不到」, 不能只躺在没人取的 Future 里
        fut.add_done_callback(lambda f: self._on_sched_done(t, f))
        return self._view(t)

    def _on_sched_done(self, t: Turn, f):
        try:
            exc = f.exception()
        except Exception:                          # 被取消等
            exc = None
        if exc is not None:
            self._finish(t, False, reason=f"回合调度失败: {type(exc).__name__}: {exc}"[:300])

    def _evict_turns_locked(self, keep_sid: str):
        """已上锁调用。只留最近 _MAX_TURNS_KEPT 个, 优先淘汰**已完成**的老回合 (dict 保插入序)。"""
        if len(self._turns) <= _MAX_TURNS_KEPT:
            return
        for k, v in list(self._turns.items()):
            if len(self._turns) <= _MAX_TURNS_KEPT:
                break
            if k != keep_sid and v.done:
                self._turns.pop(k, None)

    # --- canUseTool: 拦下 AskUserQuestion -> 挂起等你点 (PLAN §3) ---
    def _make_can_use_tool(self, t: Turn):
        async def can_use_tool(tool_name, input_data, context):
            if tool_name != "AskUserQuestion":
                # 其余工具: 沿用 headless 既有行为放行 (该会话已被 cwd allow-list + 每回合预算封顶双重约束)。
                # canUseTool 同时也是这些会话的 permission 审批口 —— 接到 UI 是后续切片, 不在本片。
                return PermissionResultAllow()
            questions = (input_data or {}).get("questions") or []
            loop = asyncio.get_running_loop()
            with self._lock:
                if len(self._asks) >= _MAX_ASKS:
                    return PermissionResultDeny(message="待答问题过多, 已拒绝 (失败安全)")
                aid = _uuidlib.uuid4().hex          # 跨重启不复用: 陈旧页面点不中新会话的 ask
                ask = PendingAsk(id=aid, session_id=t.session_id, project=t.project,
                                 questions=questions, created=time.time(),
                                 ev=asyncio.Event(), loop=loop)
                self._asks[aid] = ask
                t.awaiting_ask = aid
            timed_out = False
            try:
                await asyncio.wait_for(ask.ev.wait(), timeout=_ASK_TIMEOUT_S)
            except asyncio.TimeoutError:
                timed_out = True
            finally:
                # 任何退出路径 (含 CancelledError / 回合被拆除) 都必须摘掉, 否则 ask 永久泄漏,
                # 之后对它作答会返回 ok:True 却没有任何会话在等 —— 那是伪造成功。
                # 同时在**同一把锁**里裁决「作答 vs 超时」: 抢在 pop 之前落地的作答一律兑现。
                with self._lock:
                    self._asks.pop(aid, None)
                    if t.awaiting_ask == aid:
                        t.awaiting_ask = None
                    won = dict(ask.answers) if (ask.resolved and ask.answers) else None
            if won is not None:
                if missing_questions(questions, won):        # 兜底: 半份答案绝不当成已作答
                    return PermissionResultDeny(message="用户未作答 (作答不完整)")
                upd = dict(input_data or {})
                upd["answers"] = won                # A0 实测: 会话带着这个选择继续
                return PermissionResultAllow(updated_input=upd)
            # ⚠️ 铁律: 没作答**绝不**替你挑选项 —— 明确 deny, 理由写清
            return PermissionResultDeny(message="用户未作答 (超时)" if timed_out else "用户未作答")
        return can_use_tool

    def _drop_asks_of(self, session_id: str):
        """回合终结时收掉它名下所有挂起提问, 免得 UI 上留着点不动的幽灵按钮。"""
        with self._lock:
            for k in [k for k, a in self._asks.items() if a.session_id == session_id]:
                self._asks.pop(k, None)

    # --- 一回合 (在 loop 线程里跑) ---
    async def _run_turn(self, t: Turn, prompt: str, model, budget_usd):
        try:
            await asyncio.wait_for(self._drive(t, prompt, model, budget_usd),
                                   timeout=_TURN_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._drop_asks_of(t.session_id)
            self._finish(t, False, reason=f"回合超时 (>{int(_TURN_TIMEOUT_S)}s 硬上限)")
        except asyncio.CancelledError:
            self._drop_asks_of(t.session_id)
            self._finish(t, False, reason="回合被取消")
            raise
        except Exception as e:
            self._drop_asks_of(t.session_id)
            self._finish(t, False, reason=f"SDK 会话失败: {type(e).__name__}: {e}"[:300])

    async def _drive(self, t: Turn, prompt: str, model, budget_usd):
        # 注意: options 构造也放在调用方的 try 里 —— 版本漂移导致的 TypeError 也要落成「做不到」
        opts = ClaudeAgentOptions(
            cwd=t.cwd,
            can_use_tool=self._make_can_use_tool(t),
            # 不写 system_prompt 时 SDK 会发 `--system-prompt ""` (空系统提示词), 与旧 `claude -p` 不等价;
            # 用 preset 保持与 CLI 默认一致 (否则被驱动会话失去 Claude Code 身份与工具引导)。
            system_prompt={"type": "preset", "preset": "claude_code"},
            strict_mcp_config=True,          # 切掉继承 MCP (否则挂在别人的 OAuth 上)
            env=clean_env(),                 # 净化嵌套标记 (overlay 语义: 覆盖成空串)
            # ⚠️ None 是 SDK **默认值 = 全都加载**, 不是隔离! 用 ["project"] 排除 user 级 settings ——
            # M4 的 PermissionRequest hook 装在 ~/.claude/settings.json, 若被继承, 被驱动会话的权限请求
            # 会 POST 回本服务并阻塞, 自己咬自己。保留 project 以便 CLAUDE.md 仍生效。
            setting_sources=["project"],
            max_budget_usd=(budget_usd if budget_usd and budget_usd > 0 else None),
        )
        if t.resume:
            opts.resume = t.session_id
        else:
            opts.session_id = t.session_id
        if model:
            opts.model = model
        binp = resolve_claude()
        if binp:
            opts.cli_path = binp
        texts: list[str] = []
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                self._on_msg(t, msg, texts)
        if t.done:                            # 已被判超时/取消
            return
        if "result" not in t.kinds:
            self._finish(t, False, reason="回包无 result 事件 (非预期)")
            return
        if not t.text and texts:
            t.text = "\n".join(texts)
        self._finish(t, not t.is_error, reason=("会话内报错" if t.is_error else ""))

    def _on_msg(self, t: Turn, msg, texts: list):
        tn = type(msg).__name__
        if tn == "AssistantMessage":
            t.kinds.append("assistant")
            for b in (getattr(msg, "content", None) or []):
                if type(b).__name__ == "TextBlock":
                    tx = getattr(b, "text", "") or ""
                    if tx:
                        texts.append(tx)
                        with self._lock:
                            t._stream.append(tx)
                            if len(t._stream) > _MAX_STREAM_CHUNKS:
                                del t._stream[:-(_MAX_STREAM_CHUNKS // 2)]   # 只留尾部
        elif tn == "ResultMessage":
            t.kinds.append("result")
            t.is_error = bool(getattr(msg, "is_error", False))
            t.cost_usd = float(getattr(msg, "total_cost_usd", 0.0) or 0.0)
            r = getattr(msg, "result", None)
            if r is not None:
                t.text = str(r)
        elif tn == "SystemMessage":
            t.kinds.append("system")

    def _finish(self, t: Turn, ok: bool, *, reason: str = ""):
        with self._lock:
            if t.done:
                return
            t.done = True
            t.ok = ok
            t.reason = reason
            t.awaiting_ask = None
            for k in [k for k, a in self._asks.items() if a.session_id == t.session_id]:
                self._asks.pop(k, None)      # 内联 (self._lock 非重入, 不能在这里调 _drop_asks_of)


runner = Runner()          # 进程内单例 (同 control.plane / notify 的模块单例风格)
