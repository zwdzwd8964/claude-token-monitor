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

import os
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import discover, fs_path
from .parser import _parse_ts
from .pricing import PRICING_FETCHED, STANDARD_GEOS, STANDARD_SPEEDS, rates
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
    unknown_basis: dict = field(default_factory=dict)        # model -> "family" (按同家族价估算) | "none" (按 0)
    price_conditions: dict = field(default_factory=dict)     # "speed=fast" / "geo=us" / … -> 条数 (已按官方加价计的非标准条件)
    unconfirmed_conditions: dict = field(default_factory=dict)  # (model, "speed=…,geo=…") -> (条数, tokens): 条件没有官方规则


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
    conds: Counter = Counter()             # 已按官方加价计的非标准计价条件 -> 条数
    bad_conds: dict[tuple[str, str], list[int]] = {}   # (model, 条件) -> [count, tokens]: 模型认得, 条件没有官方规则
    key_kinds: dict[tuple[str, str], set[str]] = {}  # 去重键 -> 出现过的 source_kind

    for path, _raw_dir, kind in discover(base):
        rep.files_total += 1
        by_kind[kind] += 1
        file_usage = 0
        try:
            fh = open(fs_path(path), "r", encoding="utf-8")
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
                speed, geo = usage.get("speed"), usage.get("inference_geo")
                odd = [f"{k}={v}" for k, v, std in (("speed", speed, STANDARD_SPEEDS), ("geo", geo, STANDARD_GEOS))
                       if v and str(v).lower() not in std]
                q = rates(model)
                if odd and q.known:
                    if rates(model, 0, speed, geo).known:          # fast / us 在这个模型上有官方加价规则
                        for c in odd:
                            conds[c] += 1
                    else:                                         # 没有规则 (如 Sonnet 的 fast、Opus 4.5 的 us): 按标准价, 标未确认
                        e = bad_conds.setdefault((model, ",".join(odd)), [0, 0])
                        e[0] += 1
                        e[1] += _usage_tokens(usage)
                if q.known:
                    known[model] += 1
                else:
                    e = unknown.setdefault(model, [0, 0])
                    e[0] += 1
                    e[1] += _usage_tokens(usage)
                    rep.unknown_basis[model] = q.basis
        if file_usage == 0:
            rep.files_zero_usage += 1

    rep.files_by_kind = dict(by_kind)
    rep.types = dict(types)
    rep.known_models = dict(known)
    rep.unknown_models = {m: (c, t) for m, (c, t) in unknown.items()}
    rep.price_conditions = dict(conds)
    rep.unconfirmed_conditions = {k: (c, t) for k, (c, t) in bad_conds.items()}
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
    out.append(f"── 模型与定价 (官方价目 {PRICING_FETCHED}) ──")
    out.append(f"  ✓ 已知模型 {len(rep.known_models)} 种: "
               + ", ".join(sorted(rep.known_models)[:6])
               + (" …" if len(rep.known_models) > 6 else ""))
    real_unknown = {m: (c, t) for m, (c, t) in rep.unknown_models.items() if t > 0}
    for basis, title in (("family", "单价未确认 · 按同家族最新型号估算 (建议加进 pricing.py)"),
                         ("none", "认不出的模型 · 按 0 计 (成本被低估, 建议加进 pricing.py)")):
        part = {m: v for m, v in real_unknown.items() if rep.unknown_basis.get(m, "none") == basis}
        if part:
            warns += 1
            out.append(f"  ⚠ {title}:")
            for m, (c, t) in sorted(part.items(), key=lambda kv: kv[1][1], reverse=True):
                out.append(f"       {m:28} {c:>4}条   {fmt_tokens(t):>8} tokens")
    if rep.price_conditions:
        out.append("  · 已按官方加价计的计价条件 (fast mode / us 推理): "
                   + ", ".join(f"{k}({c})" for k, c in sorted(rep.price_conditions.items())))
    if rep.unconfirmed_conditions:
        warns += 1
        out.append("  ⚠ 单价未确认 · 计价条件没有官方规则 (按该模型标准价计, 建议核对 pricing.py 的 FAST_PRICING / US_GEO_MODELS):")
        for (m, cond), (c, t) in sorted(rep.unconfirmed_conditions.items(), key=lambda kv: kv[1][1], reverse=True):
            out.append(f"       {m:28} {cond:24} {c:>4}条   {fmt_tokens(t):>8} tokens")
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


# ---- 控制台归属与对账 (CONSOLES_PLAN S1) ----

CLAIM_GOOD, CLAIM_WARN = 95.0, 80.0          # sdk 会话 token 的认领率: >= 95% 算健康 (S1 判据 ①)
RECON_TOL = 0.01                             # 每次运行 transcript 与控制台账本的 token 差在 1% 内算对得上 (判据 ②)


def console_lines(records, snap, top: int = 8) -> tuple[list[str], int]:
    """控制台一节: 认领率、没认领的 sdk 会话、项目修正了多少、每次运行的 token 对账。纯函数 (记录 + 控制台快照进, 行出)。

    钱和 token 只以 transcript 为准 (I3/I4); 控制台账本只拿来对账 —— 对不上说明控制台记账有偏差, 不影响 tokmon 的数字。"""
    from .project import TEMP_PROJECT, in_temp_dir, scratchpad_parent
    out: list[str] = []
    warns = 0
    first: dict = {}
    tok: Counter = Counter()
    proj_of: dict = {}
    for r in records:
        f = first.get(r.session_id)
        if f is None or r.timestamp < f[0]:
            first[r.session_id] = (r.timestamp, r.cwd, r.entrypoint)
        tok[r.session_id] += r.total_tokens
        proj_of.setdefault(r.session_id, r.project)

    cons = getattr(snap, "consoles", None) or []
    if not cons:
        out.append("  · 没配控制台 (~/.tokmon/consoles.json 不存在或为空): 控制台派出的会话会算在「未认领 sdk」里")
    else:
        out.append(f"  · 控制台 {len(cons)} 个: " + " · ".join(f"{c.code} {c.dialect}" for c in cons)
                   + f" · 认得的会话 {len(snap.labels)} 个 · 运行 {len(snap.runs)} 次")
    probs = Counter(p for p in getattr(snap, "problems", []) or [])
    if probs:
        warns += 1
        out.append("  ⚠ 读控制台文件时的问题: " + "; ".join(f"{p}" + (f" ×{n}" if n > 1 else "") for p, n in probs.most_common(6)))

    labels = {}
    for sid, (_t, cwd, ep) in first.items():
        lab = snap.label(sid, cwd, ep) if snap is not None else None
        if lab is not None:
            labels[sid] = lab
    sdk = [s for s, (_t, _c, ep) in first.items() if (ep or "").startswith("sdk")]
    sdk_tok = sum(tok[s] for s in sdk)
    claimed = sum(tok[s] for s in sdk if s in labels)
    if sdk_tok:
        pct = 100.0 * claimed / sdk_tok
        icon = _icon(pct, CLAIM_GOOD, CLAIM_WARN)
        if icon != "✓":
            warns += 1
        out.append(f"  {icon} sdk 会话 (entrypoint=sdk-*) 的 token 认领率 {pct:.2f}% "
                   f"({len(sdk)} 个会话, 认领 {sum(1 for s in sdk if s in labels)} 个)")
    by_con: dict = {}
    for sid, lab in labels.items():
        e = by_con.setdefault(lab.console, Counter())
        e[lab.grain] += 1
    if by_con:
        zh = {"session": "会话级", "run": "运行级", "console": "只认到控制台"}
        out.append("  · 按控制台: " + " · ".join(
            f"{c} " + "/".join(f"{n} 个{zh.get(g, g)}" for g, n in sorted(e.items())) for c, e in sorted(by_con.items())))
    un = sorted(((tok[s], s) for s in sdk if s not in labels), reverse=True)
    if un:
        out.append(f"  · 没认领的 sdk 会话 {len(un)} 个 (按 token 排, 前 {min(top, len(un))} 个; 不是控制台派的就该留在这里):")
        for t, s in un[:top]:
            ts, _cwd, ep = first[s]
            out.append(f"       {s[:8]}  {proj_of.get(s, '?')[:28]:28} {fmt_tokens(t):>8} tokens  {ts.date()}  {ep}")

    kind: dict = {}                                        # cwd -> "sp" / "tmp" / None (每个 cwd 只判一次)
    for r in records:
        if r.cwd and r.cwd not in kind:
            kind[r.cwd] = "sp" if scratchpad_parent(r.cwd) is not None else ("tmp" if in_temp_dir(r.cwd) else None)
    sp = [r for r in records if r.cwd and kind[r.cwd] == "sp"]
    tmp = [r for r in records if r.cwd and kind[r.cwd] == "tmp" and r.session_id not in labels]
    lab_fix = {s for s, lab in labels.items() if lab.project and lab.project != proj_of.get(s)}
    out.append(f"  · 项目修正: scratchpad 里的会话归父会话的项目 {len({r.session_id for r in sp})} 个会话 / {len(sp)} 条记录; "
               f"控制台标签改了项目 {len(lab_fix)} 个会话 / {sum(1 for r in records if r.session_id in lab_fix)} 条记录; "
               f"其余在系统临时目录里跑的归「{TEMP_PROJECT}」{len({r.session_id for r in tmp})} 个会话 / {len(tmp)} 条记录")

    out.append("  · 每次运行的 token / $ 对账见下一节「控制台契约」")
    return out, warns


def _labelled(records, snap) -> tuple[dict, dict]:
    """会话 -> (最早一条的 (时刻, cwd, entrypoint)), 会话 -> 标签 (认得出的)。"""
    first: dict = {}
    for r in records:
        f = first.get(r.session_id)
        if f is None or r.timestamp < f[0]:
            first[r.session_id] = (r.timestamp, r.cwd, r.entrypoint)
    labels = {}
    for sid, (_t, cwd, ep) in first.items():
        lab = snap.label(sid, cwd, ep) if snap is not None else None
        if lab is not None:
            labels[sid] = lab
    return first, labels


A_RECON_TOL = 0.05                           # A 被杀掉的运行: 最后几次调用可能只落了一边 (S1 实测逐 agent ±5%)


def contract_lines(records, snap, stats: dict | None = None) -> tuple[list[str], int]:
    """控制台契约一节 (CONSOLES_PLAN S3): 按控制台列文件是否可读、信封合规率、类型覆盖 (没认出的类型)、关联覆盖率、
    每次运行的 token 对账、控制台自记的 $ 和 tokmon 的 $ 差多少。纯函数 (记录 + 快照 + 契约计数进, 行出)。

    钱和 token 只以 transcript 为准 (I3/I4); 控制台的账只拿来对账 —— 对不上说明控制台记账有偏差, 不影响 tokmon 的数字。"""
    from . import consoles as _consoles
    out: list[str] = []
    warns = 0
    cons = getattr(snap, "consoles", None) or []
    if not cons:
        return ["  · 没配控制台 (~/.tokmon/consoles.json 不存在或为空)"], 0
    stats = stats if stats is not None else _consoles.contract(snap)
    first, labels = _labelled(records, snap)

    # transcript 一侧: 按标签归到运行 (会话级和运行级都算: A 只认得到运行)
    comp: dict = {}
    for r in records:
        lab = labels.get(r.session_id)
        if lab is None or not lab.run or lab.grain == "console":
            continue
        a = comp.setdefault((lab.console, lab.run), [0, 0, 0, 0, 0, 0.0])
        a[0] += r.input_tokens
        a[1] += r.output_tokens
        a[2] += r.cache_read
        a[3] += r.cache_write
        a[4] += 1
        a[5] += r.cost_usd
    found = {s.lower() for s in first}

    for con in cons:
        code = con.code
        e = stats.get(code) or {}
        used = _consoles.USED_TYPES.get(con.dialect, frozenset())
        types = e.get("types") or {}
        unknown = e.get("unknown") or {}
        lines, ok = e.get("lines", 0), e.get("ok", 0)
        env = 100.0 * ok / lines if lines else 100.0
        icon = "✓" if not e.get("missing") and not e.get("unreadable") and not unknown and env >= 99.9 else "⚠"
        if icon == "⚠":
            warns += 1
        if e.get("missing"):
            out.append(f"  ⚠ {code} 的 runs_dir 不存在或列不出 (consoles.json 里的路径过期了?)")
        out.append(f"  {icon} {code} {con.dialect:9} events {e.get('files', 0)} 份 (读不了 {e.get('unreadable', 0)}) · "
                   f"信封合规 {ok}/{lines} ({env:.1f}%) · 类型 {len(types)} 种: 用到 {sum(1 for t in types if t in used)} · "
                   f"登记不用 {sum(1 for t in types if t not in used and t not in unknown)} · 没认出 {len(unknown)}")
        if unknown:
            out.append("       没认出的类型 (控制台升级加的? 认了就登记进 CONSOLES_PLAN §3.2 和 consoles.REGISTERED_TYPES): "
                       + ", ".join(f"{t} ×{n}" for t, n in sorted(unknown.items(), key=lambda x: -x[1])[:8]))

        # 关联覆盖率: 控制台记下的会话里, transcript 找得到几个; 运行里有几次接上了 transcript
        known = [s for s, lab in snap.labels.items() if lab.console == code and lab.grain == "session"]
        runs = [k for k in snap.runs if k[0] == code and k[1] != _consoles.SELF_TEST]
        joined = sum(1 for k in runs if comp.get(k))
        bits = []
        if known:
            hit = sum(1 for s in known if s in found)
            bits.append(f"控制台记下的会话 {len(known)} 个, transcript 里找得到 {hit} 个 ({100.0 * hit / len(known):.0f}%)")
        elif con.dialect == "conductor":
            bits.append("A 不记 session_id, 只按目录认到运行")
        bits.append(f"运行 {len(runs)} 次, 接上 transcript 的 {joined} 次")
        out.append("       关联: " + " · ".join(bits))

        # 对账: 每次运行 transcript vs 控制台自记的账 (office: state.json 账本; A: usage 事件加总; B 不记)
        if con.dialect == "missiond":
            out.append("       对账: B 不记 usage (只在 agents json 里有累计数, 不在白名单里), 不对账")
            continue
        tol = A_RECON_TOL if con.dialect == "conductor" else RECON_TOL
        rows, deleted, bad = [], 0, 0
        sum_t = sum_l = 0
        cost_t = cost_l = 0.0
        for (c, run), info in sorted(snap.runs.items()):
            if c != code or not info.ledger:
                continue
            led = info.ledger
            L = [int(led.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write")]
            T = comp.get((c, run), [0, 0, 0, 0, 0, 0.0])
            lt, tt = sum(L), sum(T[:4])
            if lt == 0 and tt == 0:
                continue
            sum_l, sum_t = sum_l + lt, sum_t + tt
            if tt == 0 and con.dialect == "conductor":
                deleted += 1                                       # A 正常收尾删掉了自己的 transcript
                continue
            ratio = tt / lt if lt else float("inf")
            # A 收尾的 agent 会删掉自己的 transcript: 被杀掉的运行只剩还活着的那几个 -> transcript 少是预期的, 多了才是账的问题
            partial = con.dialect == "conductor" and ratio < 1 - tol
            good = abs(ratio - 1) <= tol or partial
            bad += 0 if good else 1
            parts = []
            cols_ok = True                                       # 每一列都对得上 (合计对得上也可能是一列多一列少)
            for name, t, l in zip(("输入", "输出", "缓存读", "缓存写"), T[:4], L):
                if l and abs(t / l - 1) > tol:
                    parts.append(f"{name} ×{t / l:.2f}")
                    cols_ok = False
                elif not l and t:
                    parts.append(f"{name} 账本为 0")
                    cols_ok = False
            calls = led.get("calls")
            if isinstance(calls, int) and calls != T[4]:
                parts.append(f"响应 {T[4]} vs 账本 {calls}")
            lc = _consoles._num(led.get("cost"))
            if lc and T[5] and abs(ratio - 1) <= tol and cols_ok:   # $ 只比每列 token 都对得上的运行: 否则差的有漏记 / 删掉的, 不全是价目
                cost_l, cost_t = cost_l + lc, cost_t + T[5]
                parts.append(f"$ 控制台÷tokmon ×{lc / T[5]:.2f}")
            elif lc and T[5]:
                parts.append("$ 不比: token 先对不上")
            if partial:
                parts.insert(0, "transcript 只剩一部分: A 收尾的 agent 会删掉自己的 transcript")
            rows.append(f"         {'·' if partial else ('✓' if good else '⚠')} {run[:40]:40} token transcript÷控制台 ×{ratio:.4f}"
                        + (f"  ({'; '.join(parts)})" if parts else ""))
        head = f"       对账 (只对账, 钱以 transcript 为准; 差 > {tol:.0%} 标 ⚠):"
        if sum_l and sum_t:
            head += f" 合计 transcript÷控制台 ×{sum_t / sum_l:.4f}"
            if con.dialect == "conductor":
                head += f" (即控制台自记的是 transcript 的 {sum_l / sum_t:.1f} 倍)"
        if cost_l and cost_t:
            head += f" · $ 控制台÷tokmon ×{cost_l / cost_t:.2f} (每列 token 都对得上的运行)"
        out.append(head)
        out.extend(rows)
        if deleted:
            out.append(f"         · 另有 {deleted} 次运行 transcript 已不在: A 正常收尾时会删掉自己的 transcript (只剩被杀掉的运行)")
        if bad:
            warns += 1
            out.append(f"         ⚠ {bad} 次运行对不上: 控制台的账和 transcript 不一致 —— 多半是控制台记账的问题"
                       f" (比如输出 token 记少了、漏记了响应), tokmon 的数字照 transcript 算, 不受影响")
    out.append("  · $ 只比每列 token 都对得上的运行: 那里 $ 差多少是两边的价目不同 (控制台的缓存写一律按 5 分钟价、"
               "部分模型的缓存读倍率旧了, 见 CONSOLES_PLAN 附录 A) —— 控制台自记的 $ 不当真值")
    return out, warns


def run_console_doctor(base: Path) -> None:
    from . import consoles
    from .parser import load_records
    recs, snap = load_records(base), consoles.snapshot(force=True)
    for title, (lines, _w) in (("控制台归属 (CONSOLES_PLAN S1)", console_lines(recs, snap)),
                               ("控制台契约: 文件 / 信封 / 类型 / 关联 / 对账 (CONSOLES_PLAN S3)", contract_lines(recs, snap))):
        title = term_text(title)
        print(title)
        print("=" * len(title))
        for ln in lines:
            print(term_text(ln))
        print()


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
