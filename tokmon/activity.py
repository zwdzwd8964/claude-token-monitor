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
- **进程自报优先**: transcript 只记"发生过什么", 看不见"现在卡在哪" (等授权 / 进程重启后上一轮已死 / 后台子代理在跑)。
  Claude Code 自己在会话注册表里写了回合状态 (busy/idle/waiting), 由 serve 层经 `live` 注入 (`proc`); 有它就以它为准,
  transcript 只补细节 (在跑哪个工具)。没有它 (老版本 / 缺 psutil) -> 退回纯 transcript 推断, 行为与以前一致。
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
    closed_grace_s: int = 30     # 最后活动在这之内的会话不判 CLOSED: 活性帧 (~3s 缓存) 可能早于它刚写下的消息 (如面板里刚 /clear 换会话)


_LOCK = threading.Lock()
_LAST_SNAP: dict | None = None
_LAST_SNAP_T = 0.0
_TAIL_BYTES = 262144                 # 只读每个文件最后 ~256KB (transcript 可达数 MB, 不能全读)
_MAX_TAIL_BYTES = 4_000_000          # 末条记录比窗口还大(巨型 thinking)时, 退一步多读的封顶
_tail_cache: dict[str, tuple] = {}   # path -> (mtime, size, objs); (mtime,size) 不变就不重读

# 元数据行类型: 定位「最后一条消息」时要跳过它们。
_META_TYPES = {"mode", "last-prompt", "queue-operation", "file-history-snapshot", "ai-title", "attachment"}
_INTERRUPT = "[Request interrupted by user]"
_LOCAL_CMD_OUT = "<local-command-stdout>"


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
                   liveness: bool | None = None, bg_open: bool = False, proc: dict | None = None) -> dict:
    """判定 session 状态。纯函数 (hint 也是入参)。

    先只用 transcript 判 (`_classify_transcript`), 再叠加可选提示 (默认关闭, 不改历史回测/doctor 行为):
      - proc: 进程自报的回合状态 (procmon 读 Claude Code 会话注册表, 见 LiveIndex.session_status)。
        有已知 status 时它说了算 (`_apply_proc`), 下面两条启发式不再参与判定。
      - bg_open=True: 有未收口的后台 Workflow/子任务在跑 -> 覆盖"已完成/久未"为「后台运行中」(Bug1: 后台跑却显示已完成)。
      - liveness: 进程活性 (procmon 注入)。True=会话进程还活着 -> 把 AMBIGUOUS 消歧为「长任务运行中」(Bug2: 长推理被误判没响应);
        False=进程已退出 -> 判「会话已关闭」(不再挂"等你输入"/"可能卡住"); None=未知 -> 保持 transcript 判断。"""
    out = _classify_transcript(last_msg, now, cfg)
    out["state_source"] = "transcript"
    if proc and proc.get("status") in _PROC_STATUSES:
        _apply_proc(out, proc, cfg, bg_open)
        out["liveness"] = True if liveness is None else liveness
        return out
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
    elif liveness is False and not (out["last_activity_age_s"] is not None
                                    and out["last_activity_age_s"] < cfg.closed_grace_s):
        # 进程已退出 = 这个会话不在活动了。不管 transcript 尾巴长什么样, 都不是"在跑/等你", 而是已关闭。
        # (刚写过消息的不判: 活性帧可能比这条消息旧, 证据不够新就不下"已关闭" —— P6)
        out["state"] = "CLOSED"
        out["state_label"] = "会话已关闭 · 进程已退出"
        out["ambiguous"] = False
        out["idle"] = False
        out["background"] = False       # 进程都退出了, 别再算进"后台在跑" (评审 low#6)
    out["liveness"] = liveness
    return out


# 注册表 status 的取值 (claude 二进制: SDK 会话态 running->busy / requires_action->waiting / idle->idle;
# 终端 UI 空闲但有后台 shell -> shell)。busy 会一直持续到后台子代理也跑完 ("idle ... 是权威的回合结束信号")。
_PROC_STATUSES = ("busy", "idle", "waiting", "shell")
_REG_LAG_S = 20          # 注册表落后 transcript 的最长容忍 (活性帧缓存 ~3s + 快照 2s, 留足余量); 超过就信进程自报
_WAITING_LABELS = {
    "permission prompt": "等你授权 · 权限弹窗开着",
    "input needed": "等你回答 · Claude 在问你",
    "dialog open": "等你处理 · 有对话框开着",
    "sandbox request": "等你授权 · 沙箱要联网",
    "worker request": "等你处理 · 子任务在请示",
}


def _apply_proc(out: dict, proc: dict, cfg: ActivityConfig, bg_open: bool) -> None:
    """用进程自报的回合状态改写 transcript 判定 (原地)。transcript 只保留它独有的细节 (在跑哪个工具 / 最后一句话)。

    唯一的保留: 进程说 idle, 但 transcript 里有一条**比这个 idle 更新**、还很新鲜 (< _REG_LAG_S) 的"在飞"消息 -> 注册表
    还没跟上 (你刚发了话, busy 还没落盘), 这一帧先信 transcript, 不把刚开始的回合说成"等你"。

    进程说 idle 而 transcript 停在回合中途 (进程重启杀掉了上一轮 / 未知的本地动作) -> 改判"等你", 并标 turn_unfinished:
    这不是"任务完成", 事件层据此不发 TASK_COMPLETED、不补发空闲台阶。"""
    st = proc.get("status")
    t_state = out["state"]
    out["state_source"] = "registry"
    out["ambiguous"] = False
    if st == "waiting":
        wf = proc.get("waiting_for")
        out["state"] = "BLOCKED_ON_USER"
        out["state_label"] = _WAITING_LABELS.get(wf) or (f"等你操作 · {wf}" if wf else "等你操作")
        out["idle"] = False
        out["background"] = False
        return
    if st == "busy":
        out["idle"] = False
        if t_state == "AWAITING_USER":
            # 前台这轮说完了, 进程却还在忙 = 后台子代理/Workflow 在跑 (SDK 要等它们跑完才报 idle)
            out["state"] = "WORKING"
            out["state_label"] = "后台运行中 · Workflow/子任务在跑"
            out["background"] = True
        elif t_state == "AMBIGUOUS_PENDING":
            out["state"] = "WORKING"
            out["state_label"] = "运行中 · 长任务（Claude Code 自报在忙）"
        elif t_state not in ("WORKING", "PROCESSING"):
            out["state"] = "WORKING"
            out["state_label"] = "运行中"
        return
    # idle / shell: 回合已结束, 球在你这边
    la, age = out["last_activity_epoch"], out["last_activity_age_s"]
    sa = proc.get("status_at")
    if (t_state in ("WORKING", "PROCESSING") and la is not None and sa is not None
            and la > sa and age is not None and age < _REG_LAG_S):
        out["state_source"] = "transcript"      # 注册表还没跟上刚开始的回合
        return
    if bg_open:
        # 本进程里启动的后台 Workflow/子任务还没收口 (调用方已滤掉进程启动前的那些 —— 它们随旧进程死了)。
        # 只实测确认了后台 Agent 会让 SDK 保持 busy, Workflow 未证实 -> 保守算在跑, 不回退 Bug1。
        out["state"] = "WORKING"
        out["state_label"] = "后台运行中 · Workflow/子任务在跑"
        out["background"] = True
        out["idle"] = False
        return
    if t_state != "AWAITING_USER":
        out["state"] = "AWAITING_USER"
        out["state_label"] = ("等你输入 · 已空闲（上一轮没有正常收尾）" if la is not None else "等你输入 · 已空闲")
        out["tool_pending"] = False
        out["pending_tool_name"] = None
        out["turn_unfinished"] = True
    out["idle"] = age is not None and age >= cfg.idle_after_s
    if st == "shell":
        out["state_label"] += " · 有后台 shell 在跑"


def _classify_transcript(last_msg: dict | None, now: float, cfg: ActivityConfig) -> dict:
    """只凭「最后一条消息」判定 session 状态 (纯 transcript, 无进程/后台信息)。缺字段/读不出 -> UNKNOWN。"""
    out = {
        "state": "UNKNOWN", "state_label": "无法判断 · 记录读不出",
        "ambiguous": True, "idle": False, "synthetic": False,
        "tool_pending": False, "pending_tool_name": None, "last_text": None,
        "last_activity_epoch": None, "last_activity_age_s": None, "turn_unfinished": False,
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
        if lead.startswith(_LOCAL_CMD_OUT):
            # /model 这类本地命令: 输出直接记进 transcript, 不开启模型回合 (语料 17 次 /model, 其后都没有 Claude 回应)
            out["state"] = "AWAITING_USER"
            out["state_label"] = "等你输入 · 本地命令已执行"
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
            # queueTranscriptOnly: 只记进 transcript、**从不发给模型**的排队消息 (如 resume 时补记"上个进程没跑完的后台
            # shell")。它不开启回合, 没人会回它 —— 当成最后一条会把会话误判成"处理中/久未返回"。实测语料 4 条, 0 条被回复。
            if o.get("type") == "user" and o.get("queueTranscriptOnly") is True:
                continue
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
# 后台 Agent 的回执是 "Async agent launched successfully." (语料 16/16), 不含前两个短语 —— 漏了它, 后台子代理一启动就被当成已结束。
_BG_ACK_MARKERS = ("launched in background", "running in background", "async agent launched")


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
    """尾巴里"已启动但还没收到完成信号"的后台任务 (toolu_id 集合)。非空 = 有后台工作在飞。"""
    return set(_open_bg(objs))


def _open_bg(objs: list[dict]) -> dict:
    """同 _open_bg_tasks, 但带名字与启动时刻: {toolu_id: {"name", "epoch"}}。

    Agent/Task 没写 run_in_background 也可能被异步启动 (回执同样是 "Async agent launched", 语料 19 条回执只有 17 条带 flag):
    这类只在见到 async 回执时才算后台 —— 没回执的仍当同步调用 (评审 high#3: 别把同步子 Agent 当成后台)。

    收口信号有两种: (a) <task-notification>...<tool-use-id> (真后台任务完成/失败);
    (b) 该 toolu 的 tool_result —— 同步返回或启动失败都会带 tool_result, 唯"已在后台启动"的回执除外 (它不算结束)。
    只看 tail (够覆盖"刚起了 workflow、前台回合结束还在跑"主场景); 启动早于 tail 的极端情况会漏, 可接受。"""
    launched: dict = {}
    maybe: dict = {}                     # 没写 flag 的 Agent/Task: 等 async 回执确认
    closed: set = set()
    for o in objs:
        t = o.get("type")
        c = (o.get("message") or {}).get("content")
        if t == "assistant" and isinstance(c, list):
            for b in c:
                if not (isinstance(b, dict) and b.get("id")):
                    continue
                info = {"name": b.get("name"), "epoch": _parse_ts(o.get("timestamp"))}
                if _is_bg_launch(b):
                    launched[b["id"]] = info
                elif b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                    maybe[b["id"]] = info
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
                        elif b["tool_use_id"] in maybe:
                            launched[b["tool_use_id"]] = maybe.pop(b["tool_use_id"])
    return {k: v for k, v in launched.items() if k not in closed}


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
    liveness = live.status(sid, cwd) if live is not None else None
    ss = getattr(live, "session_status", None)
    proc = (ss(sid) or ss(path.stem)) if ss is not None else None
    open_bg = _open_bg(objs)
    if proc and proc.get("started_at"):
        # 后台任务活不过启动它的进程: 进程重启前启动、至今没收口的那些已随旧进程死掉, 不算"后台在跑"
        cut = proc["started_at"] - 1
        open_bg = {k: v for k, v in open_bg.items() if v["epoch"] is None or v["epoch"] >= cut}
    if proc and proc.get("status") in _PROC_STATUSES:
        # 后台 Agent 在跑时 SDK 保持 busy (实测), 它的 idle 就是权威结论; 只有未证实的 Workflow 还需要 transcript 兜底
        open_bg = {k: v for k, v in open_bg.items() if v["name"] == "Workflow"}
    st = classify_state(last, now, cfg, liveness=liveness, bg_open=bool(open_bg), proc=proc)
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
        "proc_status": (proc or {}).get("status"),          # 进程自报的原始状态 (无注册表 -> None), 供页面标注来源
        "waiting_for": (proc or {}).get("waiting_for"),
        "proc_status_at": (proc or {}).get("status_at"),     # 进入这个自报状态的时刻 (事件层: 等你多久了)
    }
    row.update(st)
    return row


# BLOCKED_ON_USER 排最前: 回合卡在你身上 (授权/回答), 是整页最该先看的一行。
_STATE_ORDER = {"BLOCKED_ON_USER": -1, "WORKING": 0, "PROCESSING": 0, "AMBIGUOUS_PENDING": 1,
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
                "blocked": counts.get("BLOCKED_ON_USER", 0),
                "idle": idle_n,
                "closed": counts.get("CLOSED", 0),
                "unknown": counts.get("UNKNOWN", 0),
                "total": len(sessions),
            },
            "sessions": sessions,
        }
        _LAST_SNAP, _LAST_SNAP_T = snap, time.time()
        return snap
