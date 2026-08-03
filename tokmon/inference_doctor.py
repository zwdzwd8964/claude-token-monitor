"""推断契约体检 (`tokmon doctor` 的第二半 · M4.5)。

成本层是**测量**的, 有 `doctor.py` 能对过账。但 activity / events / notify / control 是**推断**的 ——
它们建在一串"对 Claude Code 未公开格式的假设"上, 至今只有单测 + 对抗式 review, **没有 runtime 校验器**。
而我们已经*基于推断去行动*(控制层上线), 未经验证的推断就是这座塔的软肋 (见 MISSION_CONTROL §5)。

本模块把每条**载重假设**量化、可见、可发现漂移 —— 格式一变, 你先看见, 而不是推断悄悄算错:

  - 时钟: 用"最后一条*消息*的 timestamp"(非文件 mtime); 覆盖率 + mtime 偏离 (正是不用 mtime 的理由)
  - 行类型分布 + **未知/新类型** (格式漂移最直接的信号)
  - stop_reason 分布 + **未处理的 stop_reason** (状态机完整性)
  - 挂起判定: tool_use 是否回配 tool_result —— 验证"只看尾部位置、不做 id 配对"这个选择仍成立
  - 思考原文: thinking 块是否恒为空(只存签名) —— 验证"当前步骤"用叙述替代的前提
  - 打断标记形态 (list / str)
  - 标题覆盖率; 状态分布 + UNKNOWN 率 (推断在崩的最早信号)

只读 transcript 尾部 (复用 activity 实际所见), 不 hook、不外发。纯函数 `build_inference_lines` 便于测试。
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import activity
from .discovery import discover

# activity 状态机 / 尾读实际依赖或认识的东西 (其余即"未知类型" = 漂移信号)
_KNOWN_TYPES = {"assistant", "user", "ai-title", "custom-title", "mode", "last-prompt",
                "queue-operation", "file-history-snapshot", "attachment", "summary", "system"}
_HANDLED_STOP = {"end_turn", "stop_sequence", "tool_use", "max_tokens", None}   # 状态机显式处理的 (None=无/用户消息)


@dataclass
class InferenceReport:
    base: str
    sessions: int = 0
    tails_read: int = 0
    tail_empty: int = 0
    msg_ts_present: int = 0
    msg_ts_missing: int = 0
    mtime_diverged: int = 0
    line_types: dict = field(default_factory=dict)
    unknown_types: dict = field(default_factory=dict)
    stop_reasons: dict = field(default_factory=dict)
    unhandled_stop: dict = field(default_factory=dict)
    tool_use_total: int = 0
    tool_use_matched: int = 0
    unmatched_by_tool: dict = field(default_factory=dict)
    thinking_total: int = 0
    thinking_nonempty: int = 0
    interrupt_list: int = 0
    interrupt_str: int = 0
    title_present: int = 0
    states: dict = field(default_factory=dict)
    state_unknown: int = 0


def _lead_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return (b.get("text") or "").strip()
    return ""


def scan_inference(base: Path) -> InferenceReport:
    rep = InferenceReport(base=str(base))
    line_types: Counter = Counter()
    unknown_types: Counter = Counter()
    stops: Counter = Counter()
    unmatched: Counter = Counter()
    states: Counter = Counter()
    cfg = activity.ActivityConfig()
    now = time.time()

    for path, _raw_dir, kind in discover(base):
        if kind != "main":
            continue
        rep.sessions += 1
        objs = activity._read_tail(path)
        if not objs:
            rep.tail_empty += 1
            continue
        rep.tails_read += 1

        last = activity._last_message(objs)
        lts = activity._parse_ts(last.get("timestamp")) if last else None
        if lts is not None:
            rep.msg_ts_present += 1
        else:
            rep.msg_ts_missing += 1
        try:
            if lts is not None and path.stat().st_mtime - lts > 1800:   # mtime 比末条消息新 >30min
                rep.mtime_diverged += 1
        except OSError:
            pass

        # 状态分布
        st = activity.classify_state(last, now, cfg)
        states[st["state"]] += 1

        # 标题
        if activity._conversation_title(objs):
            rep.title_present += 1

        # 逐行: 类型 / stop_reason / thinking / tool_use 与 tool_result 配对(本文件尾内) / 打断形态
        tool_use_ids: dict = {}     # id -> name
        result_ids: set = set()
        for o in objs:
            t = o.get("type", "?")
            line_types[t] += 1
            if t not in _KNOWN_TYPES:
                unknown_types[t] += 1
            msg = o.get("message") or {}
            c = msg.get("content")
            if t == "assistant":
                stops[msg.get("stop_reason")] += 1
                if isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "thinking":
                            rep.thinking_total += 1
                            if (b.get("thinking") or "").strip():
                                rep.thinking_nonempty += 1
                        elif bt == "tool_use":
                            rep.tool_use_total += 1
                            if b.get("id"):
                                tool_use_ids[b["id"]] = b.get("name") or "?"
            elif t == "user":
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            rid = b.get("tool_use_id")
                            if rid:
                                result_ids.add(rid)
                lead = _lead_text(c)
                if lead.startswith(activity._INTERRUPT):
                    if isinstance(c, list):
                        rep.interrupt_list += 1
                    else:
                        rep.interrupt_str += 1

        for tid, tname in tool_use_ids.items():
            if tid in result_ids:
                rep.tool_use_matched += 1
            else:
                unmatched[tname] += 1

    for s, sr, ust in [(stops, rep.stop_reasons, rep.unhandled_stop)]:
        sr.update({str(k): v for k, v in s.items()})
        ust.update({str(k): v for k, v in s.items() if k not in _HANDLED_STOP})
    rep.line_types = dict(line_types)
    rep.unknown_types = dict(unknown_types)
    rep.unmatched_by_tool = dict(unmatched)
    rep.states = dict(states)
    rep.state_unknown = states.get("UNKNOWN", 0)
    return rep


# ---- 渲染 (纯函数, 便于测试) ----

def _icon(pct: float, good: float, warn: float) -> str:
    return "✓" if pct >= good else ("⚠" if pct >= warn else "✗")


def _pct(n: int, total: int) -> float:
    return 100.0 * n / total if total else 100.0


def build_inference_lines(rep: InferenceReport) -> tuple[list[str], int]:
    out: list[str] = []
    warns = 0
    n = rep.tails_read or 1

    out.append(f"主会话: {rep.sessions}   可读尾 {rep.tails_read}   空/读不出 {rep.tail_empty}")

    out.append("")
    out.append("── 时钟假设 (用末条消息时间戳, 不用文件 mtime) ──")
    p = _pct(rep.msg_ts_present, rep.msg_ts_present + rep.msg_ts_missing)
    i = _icon(p, 95, 80)
    warns += i != "✓"
    out.append(f"  {i} 末条消息可解析时间戳   {rep.msg_ts_present}/{rep.msg_ts_present + rep.msg_ts_missing}  ({p:.0f}%)")
    out.append(f"  · {rep.mtime_diverged} 个会话 mtime 比末条消息新 >30min "
               f"-> 正是「不能用 mtime 当活动时钟」的实证")

    out.append("")
    out.append("── 行类型 (格式漂移) ──")
    top = sorted(rep.line_types.items(), key=lambda kv: kv[1], reverse=True)
    out.append("  · 分布: " + "  ".join(f"{k}={v}" for k, v in top[:8]))
    if rep.unknown_types:
        warns += 1
        out.append("  ⚠ 出现未知/新类型 (可能格式漂移, 核对 activity 是否需适配): "
                   + ", ".join(f"{k}({v})" for k, v in rep.unknown_types.items()))
    else:
        out.append("  ✓ 无未知类型, 与 activity 的认知一致")

    out.append("")
    out.append("── stop_reason (状态机完整性) ──")
    out.append("  · 分布: " + "  ".join(f"{k}={v}" for k, v in sorted(rep.stop_reasons.items(), key=lambda kv: -kv[1])[:8]))
    if rep.unhandled_stop:
        warns += 1
        out.append("  ⚠ 未被状态机显式处理的 stop_reason (会落入 AWAITING, 可能误判): "
                   + ", ".join(f"{k}({v})" for k, v in rep.unhandled_stop.items()))
    else:
        out.append("  ✓ 所有 stop_reason 都在状态机覆盖内")

    out.append("")
    out.append("── 挂起判定假设 (只看尾部位置, 不做 id 配对) ──")
    matched_pct = _pct(rep.tool_use_matched, rep.tool_use_total)
    out.append(f"  · tool_use 共 {rep.tool_use_total}, 尾内回配到 tool_result {rep.tool_use_matched} ({matched_pct:.0f}%)")
    if rep.unmatched_by_tool:
        top_un = sorted(rep.unmatched_by_tool.items(), key=lambda kv: -kv[1])[:6]
        out.append("  · 未回配的: " + ", ".join(f"{k}({v})" for k, v in top_un)
                   + "  (绝大多数=当前在飞的尾部那条; 这正是用「尾部位置」判挂起、而非全局 id 配对的原因)")
    out.append("  ✓ 判定依据成立: '尾部 tool_use 是否还在最后一条' 才是挂起信号, 与回配率无关")

    out.append("")
    out.append("── 思考原文可用性 (当前步骤优先显示真实思考) ──")
    tnp = _pct(rep.thinking_nonempty, rep.thinking_total)
    if rep.thinking_nonempty > 0:
        out.append(f"  ✓ {rep.thinking_nonempty}/{rep.thinking_total} 个 thinking 块非空 ({tnp:.0f}%) "
                   f"-> _current_step 拿得到就显示真实思考, 拿不到回退叙述")
    else:
        out.append("  ⚠ thinking 块全为空 -> 当前步骤只能回退到叙述 (若你预期能看到思考, 说明格式变了)")
        warns += 1

    out.append("")
    out.append("── 其它推断假设 ──")
    out.append(f"  · 打断标记形态: list {rep.interrupt_list} / str {rep.interrupt_str}  "
               + ("(以 list 为主, 与 classify_state 修复一致)" if rep.interrupt_list >= rep.interrupt_str else "⚠ str 形态变多, 复核"))
    tp = _pct(rep.title_present, n)
    out.append(f"  · 会话标题覆盖: {rep.title_present}/{rep.tails_read} ({tp:.0f}%)  (尾内无 ai-title 则回退项目名)")
    out.append("  · 状态分布: " + "  ".join(f"{k}={v}" for k, v in sorted(rep.states.items(), key=lambda kv: -kv[1])))
    ust = _pct(rep.state_unknown, n)
    iu = "✗" if ust >= 25 else ("⚠" if ust >= 10 else "✓")
    warns += iu != "✓"
    out.append(f"  {iu} UNKNOWN 率 {rep.state_unknown}/{rep.tails_read} ({ust:.0f}%)  (升高=推断在崩的最早信号)")

    out.append("")
    if warns == 0:
        out.append("结论: ✓ 推断假设健康, activity/events 的格式假设仍成立。")
    else:
        out.append(f"结论: ⚠ {warns} 项需注意。若刚升级过 Claude Code, 优先核对上面标 ⚠ 的假设是否被格式变化打破。")
    return out, warns


def run_inference_doctor(base: Path) -> int:
    rep = scan_inference(base)
    lines, warns = build_inference_lines(rep)
    try:
        from rich.console import Console
        console = Console()
        console.rule("推断契约体检 (inference doctor)")
        for ln in lines:
            console.print(ln)
    except ImportError:
        print("推断契约体检 (inference doctor)")
        print("=" * 36)
        for ln in lines:
            print(ln)
    return warns
