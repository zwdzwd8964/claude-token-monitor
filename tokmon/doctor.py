"""数据契约体检 (`tokmon doctor`)。

我们依赖 Claude Code 未公开的 JSONL 内部格式 (见 NORTH_STAR §5) —— 这是最大的系统性风险。
doctor 做一次原始扫描, 把「格式是否还如我们假设」量化出来, 让漂移**可被发现**而非悄悄算错:

  - 识别率: assistant 消息 / 含 usage 的比例
  - 关键字段覆盖: cwd(项目归属) / message.id+requestId(去重) / timestamp / cache_creation 拆分
  - 去重健康: 是否有空去重键 (会让不同记录塌缩成一条 -> 少算)
  - 未知模型: 不在定价表里的 model (成本被低估), 按 token 量排序

升级 Claude Code 后跑一次 `tokmon doctor`, 是最廉价的回归检查。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import discover
from .parser import _parse_ts
from .pricing import rates_for
from .util import fmt_tokens, term_text

try:
    from rich.console import Console
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False


@dataclass
class DoctorReport:
    base: str
    files_total: int = 0
    files_by_kind: dict = field(default_factory=dict)
    files_zero_usage: int = 0
    types: dict = field(default_factory=dict)
    assistant_total: int = 0
    assistant_usage: int = 0
    ts_ok: int = 0
    would_drop_no_ts: int = 0
    field_cwd: int = 0
    field_msgid: int = 0
    field_reqid: int = 0
    field_cache_breakdown: int = 0
    empty_msgid: int = 0
    empty_reqid: int = 0
    both_empty: int = 0
    distinct_keys: int = 0
    cross_kind_keys: int = 0       # 同一去重键出现在多个 source_kind (来源拆分风险, 北极星债#3)
    known_models: dict = field(default_factory=dict)         # model -> count
    unknown_models: dict = field(default_factory=dict)       # model -> (count, tokens)


def _usage_tokens(usage: dict) -> int:
    cc = usage.get("cache_creation") or {}
    cache = (int(cc.get("ephemeral_5m_input_tokens") or 0)
             + int(cc.get("ephemeral_1h_input_tokens") or 0))
    if not cc:
        cache = int(usage.get("cache_creation_input_tokens") or 0)
    return (int(usage.get("input_tokens") or 0)
            + int(usage.get("output_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + cache)


def scan(base: Path) -> DoctorReport:
    rep = DoctorReport(base=str(base))
    types: Counter = Counter()
    by_kind: Counter = Counter()
    known: Counter = Counter()
    unknown: dict[str, list[int]] = {}     # model -> [count, tokens]
    key_kinds: dict[tuple[str, str], set[str]] = {}  # 去重键 -> 出现过的 source_kind

    for path, _raw_dir, kind in discover(base):
        rep.files_total += 1
        by_kind[kind] += 1
        file_usage = 0
        try:
            fh = open(path, "r", encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                types[d.get("type", "?")] += 1
                if d.get("type") != "assistant":
                    continue
                rep.assistant_total += 1
                msg = d.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                rep.assistant_usage += 1
                file_usage += 1

                if _parse_ts(d.get("timestamp")) is not None:
                    rep.ts_ok += 1
                else:
                    rep.would_drop_no_ts += 1

                if d.get("cwd"):
                    rep.field_cwd += 1
                mid = str(msg.get("id") or "")
                rid = str(d.get("requestId") or "")
                if mid:
                    rep.field_msgid += 1
                else:
                    rep.empty_msgid += 1
                if rid:
                    rep.field_reqid += 1
                else:
                    rep.empty_reqid += 1
                if not mid and not rid:
                    rep.both_empty += 1
                key_kinds.setdefault((mid, rid), set()).add(kind)
                if usage.get("cache_creation"):
                    rep.field_cache_breakdown += 1

                model = msg.get("model") or "unknown"
                _, _, is_known = rates_for(model)
                if is_known:
                    known[model] += 1
                else:
                    e = unknown.setdefault(model, [0, 0])
                    e[0] += 1
                    e[1] += _usage_tokens(usage)
        if file_usage == 0:
            rep.files_zero_usage += 1

    rep.files_by_kind = dict(by_kind)
    rep.types = dict(types)
    rep.known_models = dict(known)
    rep.unknown_models = {m: (c, t) for m, (c, t) in unknown.items()}
    rep.distinct_keys = len(key_kinds)
    rep.cross_kind_keys = sum(1 for v in key_kinds.values() if len(v) > 1)
    return rep


# ---- 渲染 ----

def _icon(pct: float, good: float, warn: float) -> str:
    if pct >= good:
        return "✓"
    if pct >= warn:
        return "⚠"
    return "✗"


def _pct(n: int, total: int) -> float:
    return 100.0 * n / total if total else 100.0


def build_lines(rep: DoctorReport) -> tuple[list[str], int]:
    """返回 (展示行, 警告数)。纯函数, 便于测试。"""
    out: list[str] = []
    warns = 0
    u = rep.assistant_usage

    out.append(f"扫描目录: {rep.base}")
    kinds = "  ".join(f"{k} {v}" for k, v in sorted(rep.files_by_kind.items()))
    out.append(f"会话文件: {rep.files_total}   ({kinds})   0用量文件 {rep.files_zero_usage}")

    out.append("")
    out.append("── 记录识别 ──")
    usage_pct = _pct(rep.assistant_usage, rep.assistant_total)
    i = _icon(usage_pct, 99, 90)
    warns += i != "✓"
    out.append(f"  {i} assistant 消息含 usage   {rep.assistant_usage}/{rep.assistant_total}  ({usage_pct:.1f}%)")
    ts_pct = _pct(rep.ts_ok, u)
    i = _icon(ts_pct, 99, 90)
    warns += i != "✓"
    note = f"   (无时间戳会被丢弃: {rep.would_drop_no_ts})" if rep.would_drop_no_ts else ""
    out.append(f"  {i} 可解析时间戳            {rep.ts_ok}/{u}  ({ts_pct:.1f}%){note}")

    out.append("")
    out.append("── 关键字段覆盖 (含 usage 的记录) ──")
    for name, val, good, warn, tail in [
        ("cwd (项目归属)", rep.field_cwd, 95, 80, "缺失回退编码目录名"),
        ("message.id (去重)", rep.field_msgid, 99, 90, ""),
        ("requestId (去重)", rep.field_reqid, 99, 90, ""),
        ("cache_creation 拆分", rep.field_cache_breakdown, 90, 50, "缺失按 5m 计"),
    ]:
        pct = _pct(val, u)
        i = _icon(pct, good, warn)
        warns += i != "✓"
        tail = f"   ({tail})" if tail else ""
        out.append(f"  {i} {name:22} {val}/{u}  ({pct:.1f}%){tail}")

    out.append("")
    out.append("── 去重健康 ──")
    # message.id 是主去重键; 缺失它才有碰撞风险, 单缺 requestId 有 id 兜底不影响。
    if rep.both_empty:
        warns += 1
        out.append(f"  ✗ 有 {rep.both_empty} 条记录去重键全空 -> 会塌缩成一条, 少算!")
    elif rep.empty_msgid:
        warns += 1
        out.append(f"  ⚠ {rep.empty_msgid} 条缺 message.id (主去重键), 可能误合并")
    else:
        out.append("  ✓ 去重键完好 (message.id 全在)")
        if rep.empty_reqid:
            out.append(f"  · {rep.empty_reqid} 条无 requestId, 有 message.id 兜底, 不影响去重")
    # 跨来源碰撞: 同一消息若同时出现在 main 与 subagent/workflow, 来源拆分会不准 (债#3)。
    if rep.cross_kind_keys:
        warns += 1
        out.append(f"  ⚠ {rep.cross_kind_keys} 条去重键跨来源出现 -> main/subagent/workflow 拆分可能不准")
    else:
        out.append("  ✓ 无跨来源碰撞, main/subagent/workflow 拆分可信")
    merged = rep.assistant_usage - rep.distinct_keys
    ratio = _pct(merged, rep.assistant_usage)
    out.append(f"  原始 usage 行 {rep.assistant_usage} -> 去重后 {rep.distinct_keys}  "
               f"(合并 {merged} 条, {ratio:.0f}%)")
    if ratio >= 40:
        out.append("  · 高合并率属正常: 会话续接/分叉会重复记录历史, 去重是必要的 (否则会多算)")

    out.append("")
    out.append("── 模型与定价 ──")
    out.append(f"  ✓ 已知模型 {len(rep.known_models)} 种: "
               + ", ".join(sorted(rep.known_models)[:6])
               + (" …" if len(rep.known_models) > 6 else ""))
    real_unknown = {m: (c, t) for m, (c, t) in rep.unknown_models.items() if t > 0}
    if real_unknown:
        warns += 1
        out.append("  ⚠ 未知模型 (成本被低估, 建议加进 pricing.py):")
        for m, (c, t) in sorted(real_unknown.items(), key=lambda kv: kv[1][1], reverse=True):
            out.append(f"       {m:28} {c:>4}条   {fmt_tokens(t):>8} tokens")
    zero_unknown = {m: c for m, (c, t) in rep.unknown_models.items() if t == 0}
    if zero_unknown:
        out.append("  · 0-token 的非真实模型 (无成本影响): "
                   + ", ".join(f"{m}({c})" for m, c in zero_unknown.items()))
    if not rep.unknown_models:
        out.append("  ✓ 无未知模型")

    out.append("")
    if warns == 0:
        out.append("结论: ✓ 数据契约健康, 与 tokmon 的解析假设一致。")
    else:
        out.append(f"结论: ⚠ {warns} 项需注意 (见上)。若刚升级过 Claude Code, 优先核对格式是否变化。")
    return out, warns


def run_doctor(base: Path) -> None:
    rep = scan(base)
    lines, warns = build_lines(rep)
    title = "Claude Code 数据体检 (tokmon doctor)"
    lines = [term_text(ln) for ln in lines]      # 原则 5: GBK 控制台写不出 ✓ 也不该让体检崩掉
    title = term_text(title)
    if _HAS_RICH:
        console = Console()
        console.rule(title)
        for ln in lines:
            console.print(ln)
    else:
        print(title)
        print("=" * len(title))
        for ln in lines:
            print(ln)
