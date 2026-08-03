"""对话活动支柱: 从 transcript 推断每个 Claude Code session 的实时状态 (`/api/sessions` 的数据源)。

Mission Control 的 M1「先看见」切片: 只读、本地、不通知。它回答——
「我每个正在跑的 session, 现在是 推进 / 处理 / 久未返回 / 等我 / 读不出?」

立场 (对齐 MISSION_CONTROL.md 与 procmon 同源):
- **纯只读**: 只读 transcript JSONL, 绝不写、不 hook、不替 Claude Code 点 permission。
- **支柱独立 (P4)**: 只依赖 stdlib + discovery + project; **不 import** token 内核(parser/aggregate/pricing/records)
  也不 import procmon。汇合只发生在 serve 层。
- **真实优先 / 误报零容忍 (P6)**: 拿不准就降级为 UNKNOWN, 绝不伪造 PERMISSION_NEEDED 或断言 STUCK。
- **时钟 = 最后一条*消息*的 timestamp, 绝不用文件 mtime** —— 元数据行(mode/ai-title/last-prompt/...)
  会在 resume/UI 改动时刷新 mtime, 让一个 79h 没动的 session 看起来"刚活动过"。mtime 只当读缓存键。
- **挂起判定只看尾部位置**, 不做 tool_use<->tool_result 全局配对(已验证脆弱: Agent/Workflow/AskUserQuestion
  的 tool_use id 不会回写 tool_result)。
- **渐进降级**: 单个文件坏/格式漂移 -> 该行降级 UNKNOWN, 不拖垮整页或其它支柱。

classify_state 是**纯函数**(无 I/O), 便于单测 (参照 project.py 的 I2 精神)。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .discovery import default_base, discover, friendly_project
from .project import workspace_identity


@dataclass(frozen=True)
class ActivityConfig:
    idle_after_s: int = 600      # AWAITING_USER 超过这个算 idle (仅 UI 排序标记, 不是告警, 不改状态)
    stuck_after_s: int = 120     # 挂起超过这个 -> 进入 AMBIGUOUS_PENDING (唯一门控不确定标签的阈值, 故意保守)
    snapshot_ttl_s: float = 2.0  # 整次扫描的短 TTL 记忆 (并发/自动刷新共享一帧), 同 procmon


_LOCK = threading.Lock()
_LAST_SNAP: dict | None = None
_LAST_SNAP_T = 0.0
_TAIL_BYTES = 262144                 # 只读每个文件最后 ~256KB (transcript 可达数 MB, 不能全读)
_MAX_TAIL_BYTES = 4_000_000          # 末条记录比窗口还大(巨型 thinking)时, 退一步多读的封顶
_tail_cache: dict[str, tuple] = {}   # path -> (mtime, size, objs); (mtime,size) 不变就不重读

# 元数据行类型: 定位「最后一条消息」时要跳过它们。
_META_TYPES = {"mode", "last-prompt", "queue-operation", "file-history-snapshot", "ai-title", "attachment"}
_INTERRUPT = "[Request interrupted by user]"


def _parse_ts(s) -> float | None:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return None          # 无时区的时间戳无法可靠定位 -> 宁可 UNKNOWN 也不按本地时区瞎算 (真实优先)
    return dt.timestamp()


# ---- 纯函数: 状态机 (无 I/O, 易测) ----

def classify_state(last_msg: dict | None, now: float, cfg: ActivityConfig,
                   liveness: bool | None = None, bg_open: bool = False) -> dict:
    """判定 session 状态。纯函数 (hint 也是入参)。

    先只用 transcript 判 (`_classify_transcript`), 再叠加两个可选提示 (默认关闭, 不改历史回测/doctor 行为):
      - bg_open=True: 有未收口的后台 Workflow/子任务在跑 -> 覆盖"已完成/久未"为「后台运行中」(Bug1: 后台跑却显示已完成)。
      - liveness: 进程活性 (procmon 注入)。True=会话进程还活着 -> 把 AMBIGUOUS 消歧为「长任务运行中」(Bug2: 长推理被误判没响应);
        False=进程已退出 -> 判「会话已关闭」(不再挂"等你输入"/"可能卡住"); None=未知 -> 保持 transcript 判断。"""
    out = _classify_transcript(last_msg, now, cfg)
    # Bug1 — 后台任务在飞: 前台回合虽然结束/久未, 但后台 workflow/子任务确实在跑 -> 会话仍在推进。
    if bg_open and out["state"] in ("AWAITING_USER", "AMBIGUOUS_PENDING"):
        out["state"] = "WORKING"
        out["state_label"] = "后台运行中 · Workflow/子任务在跑"
        out["ambiguous"] = False
        out["idle"] = False
        out["background"] = True
    # Bug2 — 进程活性消歧 (只对"拿不准/说已完成"的情形收紧或放宽, 不动确凿的前台在飞)。
    if liveness is True:
        if out["state"] == "AMBIGUOUS_PENDING":
            out["state"] = "WORKING"
            out["state_label"] = "运行中 · 长任务（进程在跑）"
            out["ambiguous"] = False
    elif liveness is False:
        # 进程已退出 = 这个会话不在活动了。不管 transcript 尾巴长什么样, 都不是"在跑/等你", 而是已关闭。
        out["state"] = "CLOSED"
        out["state_label"] = "会话已关闭 · 进程已退出"
        out["ambiguous"] = False
        out["idle"] = False
        out["background"] = False       # 进程都退出了, 别再算进"后台在跑" (评审 low#6)
    out["liveness"] = liveness
    return out


def _classify_transcript(last_msg: dict | None, now: float, cfg: ActivityConfig) -> dict:
    """只凭「最后一条消息」判定 session 状态 (纯 transcript, 无进程/后台信息)。缺字段/读不出 -> UNKNOWN。"""
    out = {
        "state": "UNKNOWN", "state_label": "无法判断 · 记录读不出",
        "ambiguous": True, "idle": False, "synthetic": False,
        "tool_pending": False, "pending_tool_name": None, "last_text": None,
        "last_activity_epoch": None, "last_activity_age_s": None,
    }
    if not last_msg:
        out["state_label"] = "无法判断 · transcript 里没有对话消息"
        return out
    ts = _parse_ts(last_msg.get("timestamp"))
    if ts is None:
        out["state_label"] = "无法判断 · 时间戳读不出"
        return out
    age = max(0.0, now - ts)
    out["last_activity_epoch"] = int(ts)
    out["last_activity_age_s"] = int(age)
    out["ambiguous"] = False

    role = last_msg.get("type")
    msg = last_msg.get("message") or {}
    content = msg.get("content")

    def last_block():
        if isinstance(content, list):
            for b in reversed(content):
                if isinstance(b, dict):
                    return b
        return None

    def has_tool_result():
        return isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)

    def first_text():
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    return b.get("text")
        return None

    if role == "assistant":
        lb = last_block()
        pending = lb if (lb and lb.get("type") == "tool_use") else None
        # 工具回合在飞: 末块是 tool_use(挂起), 或 stop_reason==tool_use(整个用工具的回合, 含被拆成单独行的
        # text/thinking 中途行——它们 stop_reason 仍是 tool_use, 末块却是 text/thinking; 此前被误判成"等你"。L2 实测发现)。
        if pending or msg.get("stop_reason") == "tool_use":
            name = pending.get("name") if pending else None
            out["tool_pending"] = bool(pending)
            out["pending_tool_name"] = name
            if age >= cfg.stuck_after_s:
                out["state"] = "AMBIGUOUS_PENDING"
                out["ambiguous"] = True
                out["state_label"] = "久未返回 · 可能长任务 / 在等你授权 / 会话已关闭（transcript 无法区分）"
            else:
                out["state"] = "WORKING"
                out["state_label"] = f"运行中 · 正在跑 {name}" if name else "运行中 · 生成中"
            return out
        if msg.get("stop_reason") == "max_tokens":
            # 触顶截断, Claude Code 会自动续写 -> 仍在推进, 不是在等你 (推断 doctor 发现的未处理 stop_reason)
            if age >= cfg.stuck_after_s:
                out["state"] = "AMBIGUOUS_PENDING"
                out["ambiguous"] = True
                out["state_label"] = "触顶截断后久未续写 · 可能卡住 / 会话已关闭"
            else:
                out["state"] = "PROCESSING"
                out["state_label"] = "处理中 · 触顶截断, 即将自动续写"
            return out
        # 只有 thinking 块 (无 text / 无 tool_use) 的 assistant 行 = 回合中途被拆开的流式片段, 不是真正的回合结束。
        # (L2 实测: 语料里 173/173 这类行都后接同一 message.id 的续接片段, 0 个是真结束。) 此前 end_turn 的 thinking-only
        # 片段被误判 AWAITING -> 若它恰是 tail 末行 (流式窗口/中途崩溃), derive_events 会误报 TASK_COMPLETED。修正为在飞。
        blk_types = {b.get("type") for b in content if isinstance(b, dict)} if isinstance(content, list) else set()
        if "thinking" in blk_types and "text" not in blk_types and "tool_use" not in blk_types:
            if age >= cfg.stuck_after_s:
                out["state"] = "AMBIGUOUS_PENDING"
                out["ambiguous"] = True
                out["state_label"] = "久未续写 · 可能长任务 / 会话已关闭（transcript 无法区分）"
            else:
                out["state"] = "WORKING"
                out["state_label"] = "运行中 · 思考中"
            return out
        # 回合结束 (end_turn/stop_sequence/其它非 tool_use 尾) -> 等你
        txt = first_text()
        model = msg.get("model")
        synthetic = (model == "<synthetic>") or (isinstance(txt, str) and txt.strip() == "No response requested.")
        out["synthetic"] = synthetic
        snippet = (txt or "").strip().replace("\n", " ")
        out["last_text"] = snippet[:140] or None
        out["idle"] = age >= cfg.idle_after_s
        out["state"] = "AWAITING_USER"
        out["state_label"] = "等你输入 · 这轮已完成"
        return out

    if role == "user":
        # 区分: 机器续接(tool_result / <task-notification>) vs 人类打断 vs 新提问。
        # 打断标记在真实 transcript 里是 list 内容 [{type:text,text:"[Request interrupted by user]"}],
        # 不是纯字符串 —— 必须按"首段文本"判断, 不论 str/list 形态 (否则把打断误判成 处理中, 误报)。
        lead = content.strip() if isinstance(content, str) else (first_text() or "").strip()
        if lead.startswith(_INTERRUPT):
            out["state"] = "AWAITING_USER"
            out["state_label"] = "等你输入 · 你打断了上一轮"
            out["idle"] = age >= cfg.idle_after_s
            return out
        # 续接型 / 新提问型, 都意味着 Claude 接下来该动; 久了则进入 ambiguous
        if age >= cfg.stuck_after_s:
            out["state"] = "AMBIGUOUS_PENDING"
            out["ambiguous"] = True
            out["state_label"] = "久未继续 · 可能长任务 / 等授权 / 会话已关闭"
        else:
            out["state"] = "PROCESSING"
            out["state_label"] = "处理中 · Claude 即将回应"
        return out

    out["state"] = "UNKNOWN"
    out["ambiguous"] = True
    out["state_label"] = "无法判断 · 消息结构异常"
    return out


# ---- I/O 层 ----

def _read_tail(path: Path) -> list[dict] | None:
    """只读文件尾部 ~256KB, 解析其中完整的 JSONL 行。(mtime,size) 缓存。坏文件返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _tail_cache.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(path, "rb") as f:
            if st.st_size > _TAIL_BYTES:
                f.seek(st.st_size - _TAIL_BYTES)
                data = f.read()
                nl = data.find(b"\n")          # 丢掉可能被截断的首行
                data = data[nl + 1:] if nl != -1 else data
                if not data.strip():
                    # 末条记录比窗口还大(整条横跨窗口) -> 退一步读更大窗口(封顶), 别把活跃会话误判成读不出
                    cap = min(st.st_size, _MAX_TAIL_BYTES)
                    f.seek(st.st_size - cap)
                    data = f.read()
                    if cap < st.st_size:
                        data = data[data.find(b"\n") + 1:]
            else:
                data = f.read()
    except OSError:
        return None
    objs: list[dict] = []
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(o, dict):
                objs.append(o)
        except Exception:
            continue
    _tail_cache[key] = (st.st_mtime, st.st_size, objs)
    return objs


def _last_message(objs: list[dict]) -> dict | None:
    for o in reversed(objs):
        if o.get("type") in ("assistant", "user"):
            return o
    return None


def _last_assistant_model(objs: list[dict]) -> str | None:
    for o in reversed(objs):
        if o.get("type") == "assistant":
            m = (o.get("message") or {}).get("model")
            if m and m != "<synthetic>":   # 跳过合成 no-op 回合, 取真正在跑的模型
                return m
    return None


def _recent_events(objs: list[dict], limit: int = 10) -> list[dict]:
    """从尾部派生「最近活动」时间线行 —— 仅 UI 内的观测事实, 不是 §4 事件总线 (那是 M2)。"""
    out: list[dict] = []
    for o in objs:
        t = o.get("type")
        msg = o.get("message") or {}
        epoch = _parse_ts(o.get("timestamp"))
        epoch = int(epoch) if epoch else None
        c = msg.get("content")
        if t == "assistant":
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        out.append({"kind": "tool_started", "name": b.get("name"), "epoch": epoch})
            if msg.get("stop_reason") in ("end_turn", "stop_sequence") and msg.get("model") != "<synthetic>":
                out.append({"kind": "task_completed", "epoch": epoch})
        elif t == "user":
            lead = None
            if isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        out.append({"kind": "tool_returned_error" if b.get("is_error") else "tool_returned",
                                    "epoch": epoch})
                    elif b.get("type") == "text" and lead is None:
                        lead = (b.get("text") or "").strip()
            elif isinstance(c, str):
                lead = c.strip()
            if lead:                              # 打断/子代理续接标记可能出现在 list 或 str 形态
                if lead.startswith(_INTERRUPT):
                    out.append({"kind": "interrupted_by_user", "epoch": epoch})
                elif lead.startswith("<task-notification>"):
                    out.append({"kind": "continued_after_subagent", "epoch": epoch})
    return out[-limit:]


def _conversation_title(objs: list[dict]) -> str | None:
    """会话标题: 用户自定义标题(`custom-title`)优先于 AI 生成标题(`ai-title`); 都取尾部里最后一条。无则 None。"""
    custom = ai = None
    for o in objs:
        t = o.get("type")
        if t == "custom-title":
            c = o.get("customTitle")
            if isinstance(c, str) and c.strip():
                custom = c.strip()
        elif t == "ai-title":
            a = o.get("aiTitle")
            if isinstance(a, str) and a.strip():
                ai = a.strip()
    return custom or ai


def _current_step(objs: list[dict], limit: int = 240):
    """「当前步骤」-> (文字, 种类)。优先最近一段非空 **思考**(模型推理), 否则用 assistant **叙述**文字。
    (推断 doctor 实测: thinking 块约 29% 的会话末段非空 —— 拿得到就显示真实思考, 拿不到回退叙述。)"""
    think = narr = None
    for o in reversed(objs):
        if o.get("type") != "assistant":
            continue
        c = (o.get("message") or {}).get("content")
        if not isinstance(c, list):
            continue
        for b in reversed(c):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "thinking" and think is None:
                tv = (b.get("thinking") or "").strip()
                if tv:
                    think = tv
            elif b.get("type") == "text" and narr is None:
                nv = (b.get("text") or "").strip()
                if nv:
                    narr = nv
        if think is not None and narr is not None:
            break
    if think:
        return think.replace("\n", " ")[:limit], "thinking"
    if narr:
        return narr.replace("\n", " ")[:limit], "narration"
    return None, None


# 后台会跑起独立子任务的工具。Workflow 恒后台; Agent/Task 仅在显式 run_in_background=True 时才后台
# (默认/缺省是同步返回 —— 评审 high#3: 别把同步子 Agent 当成后台在飞)。
_BG_TOOLS = {"Workflow", "Agent", "Task"}
_TASK_CLOSE_RE = re.compile(r"<tool-use-id>(toolu_[0-9A-Za-z]+)</tool-use-id>")
# "已在后台启动"的回执 —— 它只是确认 launch, 不代表任务结束, 不能当收口 (真收口是之后的 task-notification)。
_BG_ACK_MARKERS = ("launched in background", "running in background")


def _lead_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text") or ""
    return ""


def _is_bg_launch(block: dict) -> bool:
    if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in _BG_TOOLS):
        return False
    if block.get("name") == "Workflow":
        return True                                    # Workflow 恒后台
    return (block.get("input") or {}).get("run_in_background") is True   # Agent/Task 需显式后台


def _open_bg_tasks(objs: list[dict]) -> set:
    """尾巴里"已启动但还没收到完成信号"的后台任务 (toolu_id 集合)。非空 = 有后台工作在飞。

    收口信号有两种: (a) <task-notification>...<tool-use-id> (真后台任务完成/失败);
    (b) 该 toolu 的 tool_result —— 同步返回或启动失败都会带 tool_result, 唯"已在后台启动"的回执除外 (它不算结束)。
    只看 tail (够覆盖"刚起了 workflow、前台回合结束还在跑"主场景); 启动早于 tail 的极端情况会漏, 可接受。"""
    launched: dict = {}
    closed: set = set()
    for o in objs:
        t = o.get("type")
        c = (o.get("message") or {}).get("content")
        if t == "assistant" and isinstance(c, list):
            for b in c:
                if _is_bg_launch(b) and b.get("id"):
                    launched[b["id"]] = b.get("name")
        elif t == "user":
            lead = _lead_text(c)
            if lead.startswith("<task-notification>"):
                m = _TASK_CLOSE_RE.search(lead)
                if m:
                    closed.add(m.group(1))
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                        rc = b.get("content")
                        s = (rc if isinstance(rc, str) else _lead_text(rc)).lower()
                        if not any(mk in s for mk in _BG_ACK_MARKERS):   # 非"已在后台启动"回执 = 收口
                            closed.add(b["tool_use_id"])
    return set(launched) - closed


def _session_row(path: Path, raw_dir: str, now: float, cfg: ActivityConfig,
                 live=None) -> dict:
    objs = _read_tail(path)
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        mtime = None
    if not objs:
        return {
            "session_id": path.stem, "project": friendly_project(raw_dir), "subpath": "",
            "source_kind": "main", "cwd": "", "git_branch": None, "model": None,
            "state": "UNKNOWN", "state_label": "无法判断 · 文件读不出/空", "ambiguous": True,
            "idle": False, "synthetic": False, "tool_pending": False, "pending_tool_name": None,
            "last_text": None, "last_activity_epoch": None, "last_activity_age_s": None,
            "title": None, "current_step": None, "step_kind": None,
            "file_mtime_epoch": mtime, "recent_events": [], "file": str(path),
        }
    last = _last_message(objs)
    cwd = (last or {}).get("cwd")
    sid = (last or {}).get("sessionId") or path.stem
    bg_open = bool(_open_bg_tasks(objs))
    liveness = live.status(sid, cwd) if live is not None else None
    st = classify_state(last, now, cfg, liveness=liveness, bg_open=bg_open)
    step_text, step_kind = _current_step(objs)
    wid = workspace_identity(cwd) if cwd else None
    row = {
        "session_id": (last or {}).get("sessionId") or path.stem,
        "project": wid.project if wid else friendly_project(raw_dir),
        "subpath": wid.subpath if wid else "",
        "source_kind": "main",
        "cwd": cwd or "",
        "git_branch": (last or {}).get("gitBranch"),
        "model": _last_assistant_model(objs),
        "title": _conversation_title(objs),
        "current_step": step_text,
        "step_kind": step_kind,
        "file_mtime_epoch": mtime,
        "recent_events": _recent_events(objs),
        "file": str(path),
    }
    row.update(st)
    return row


_STATE_ORDER = {"WORKING": 0, "PROCESSING": 0, "AMBIGUOUS_PENDING": 1,
                "AWAITING_USER": 2, "CLOSED": 3, "UNKNOWN": 4}


def snapshot(base: Path | None = None, cfg: ActivityConfig | None = None, live=None) -> dict:
    """采一帧 session 状态快照。短 TTL 内并发调用共享同一帧。绝不为单个坏文件抛错。

    live: 可选的进程活性索引 (procmon.LiveIndex 或任何有 .status(sid,cwd) 的对象)。由 serve/pump 注入,
    activity 本身不 import procmon —— 支柱间的耦合只发生在组合层, 保持 kernel 可独立测试。缺省 None = 不用活性。"""
    cfg = cfg or ActivityConfig()
    base = Path(base) if base else default_base()
    global _LAST_SNAP, _LAST_SNAP_T
    with _LOCK:
        now = time.time()
        if _LAST_SNAP is not None and (now - _LAST_SNAP_T) < cfg.snapshot_ttl_s:
            return _LAST_SNAP

        sessions: list[dict] = []
        live_paths: set[str] = set()
        for path, raw_dir, kind in discover(base):
            if kind != "main":
                continue
            live_paths.add(str(path))
            try:
                sessions.append(_session_row(path, raw_dir, now, cfg, live=live))
            except Exception:
                sessions.append({
                    "session_id": path.stem, "project": friendly_project(raw_dir), "subpath": "",
                    "source_kind": "main", "cwd": "", "git_branch": None, "model": None,
                    "state": "UNKNOWN", "state_label": "无法判断 · 解析异常", "ambiguous": True,
                    "idle": False, "synthetic": False, "tool_pending": False, "pending_tool_name": None,
                    "last_text": None, "last_activity_epoch": None, "last_activity_age_s": None,
                    "title": None, "current_step": None, "step_kind": None,
                    "file_mtime_epoch": None, "recent_events": [], "file": str(path),
                })

        for k in [k for k in _tail_cache if k not in live_paths]:   # 清掉已删除/轮转文件的缓存, 防无限增长
            _tail_cache.pop(k, None)

        sessions.sort(key=lambda s: (_STATE_ORDER.get(s["state"], 4),
                                     s["last_activity_age_s"] if s["last_activity_age_s"] is not None else 1e12))
        counts: dict[str, int] = {}
        idle_n = 0
        bg_n = 0
        for s in sessions:
            counts[s["state"]] = counts.get(s["state"], 0) + 1
            if s.get("idle"):
                idle_n += 1
            if s.get("background"):
                bg_n += 1
        snap = {
            "generated_at_epoch": int(now),
            "thresholds": {"idle_after_s": cfg.idle_after_s, "stuck_after_s": cfg.stuck_after_s},
            "counts": {
                "working": counts.get("WORKING", 0) + counts.get("PROCESSING", 0),
                "background": bg_n,
                "ambiguous": counts.get("AMBIGUOUS_PENDING", 0),
                "awaiting": counts.get("AWAITING_USER", 0),
                "idle": idle_n,
                "closed": counts.get("CLOSED", 0),
                "unknown": counts.get("UNKNOWN", 0),
                "total": len(sessions),
            },
            "sessions": sessions,
        }
        _LAST_SNAP, _LAST_SNAP_T = snap, time.time()
        return snap
