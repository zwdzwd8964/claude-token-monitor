"""本地 Web 看板 (`tokmon serve`) —— 早期预览版。

只监听 localhost, 纯标准库 (http.server), 零额外依赖, 默认不外发。
严格遵守北极星原则 4: 它是内核的消费者, 复用 load_records()/aggregate, 不碰内核。

  GET /                      -> 单页 HTML 看板 (内联, 无外部 CDN, 可离线)
  GET /api/summary?since=&scope=&vscode_only=  -> JSON 汇总 (含「vs 上一周期」自基线对比)

自基线对比 (复盘): 对所选窗口, 额外聚合「紧邻的、等长的上一周期」(同一份 load_records()
输出的第二次 filter+summarize), 给出 token/成本/项目的 Δ% —— 回答北极星第三问
「跟我的预期差多少」, 用「你自己的上一周期」当基线, 不是凭空的预算 (那是 v0.4)。
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import activity, billing, control, notify, procmon, remote, runner, trace
from .aggregate import Agg, filter_since, group_by, summarize
from .event_sources import activity_source, cost_source, risk_source
from .events import bus as event_bus
from .parser import load_records
from .pricing import cost_usd
from .util import parse_since

SCOPE_KINDS = {
    "all": {"main", "subagent", "workflow"},
    "main": {"main"},
}


def _agg_dict(a: Agg) -> dict:
    return {
        "tokens": a.total_tokens,
        "input": a.input_tokens,
        "output": a.output_tokens,
        "cache_read": a.cache_read,
        "cache_write": a.cache_write,
        "web_search": a.web_search,
        "web_fetch": a.web_fetch,
        "cost": round(a.cost, 4),
        "count": a.count,
        "any_unpriced": a.any_unpriced,
    }


def _rows(records, keyfn, label_fn=str, sort_label=False):
    groups = group_by(records, keyfn)
    items = list(groups.items())
    if sort_label:
        items.sort(key=lambda kv: label_fn(kv[0]), reverse=True)
    else:
        items.sort(key=lambda kv: kv[1].total_tokens, reverse=True)
    return [{"label": label_fn(k), **_agg_dict(a)} for k, a in items]


# ---- 自基线: 紧邻的等长上一周期 (纯 serve 层, 不碰 aggregate.py / I6) ----

def _filter_window(records, lo: datetime, hi: datetime):
    """[lo, hi) 半开区间。比 filter_since 多了上界, 给基线用。"""
    return [r for r in records if lo <= r.timestamp < hi]


def _baseline_window(since: str | None, now: datetime, cutoff: datetime | None):
    """返回紧邻的、可比的上一周期 (prev_lo, prev_hi); 无可比周期则 None。

    - all / None / cutoff is None  -> None (没有等长的上一周期可比)。
    - today -> 拿「昨天的同一时段」(到此刻为止的前 N 小时), 而非整个昨天 —— 部分天要对部分天才诚实。
    - 滚动窗 Nh/Nd/Nw -> 紧邻当前窗之前、等长的那一段。
    """
    if cutoff is None:
        return None
    spec = (since or "").strip().lower()
    if spec == "today":
        elapsed = now - cutoff           # 今天已过去的时长
        prev_lo = cutoff - timedelta(days=1)
        return (prev_lo, prev_lo + elapsed)
    length = now - cutoff                # 当前窗 [cutoff, now] 的长度
    return (cutoff - length, cutoff)


def build_summary(base: Path, since: str, scope: str, vscode_only: bool) -> dict:
    include_kinds = SCOPE_KINDS.get(scope, SCOPE_KINDS["all"])
    records = load_records(base, include_kinds, vscode_only)

    now = datetime.now().astimezone()    # 只取一次, 当前窗与基线共用 -> 两段严格等长、首尾相接
    cutoff = parse_since(since)
    rows = filter_since(records, cutoff)
    total = summarize(rows)

    baseline = None
    project_deltas: list[dict] = []
    bw = _baseline_window(since, now, cutoff)
    if bw is not None:
        prev_lo, prev_hi = bw
        prev_rows = _filter_window(records, prev_lo, prev_hi)
        prev_total = summarize(prev_rows)
        earliest = min((r.timestamp for r in records), default=None)
        partial = earliest is not None and prev_lo < earliest   # 历史不足 2× 窗口 -> 基线偏低, 诚实标注
        baseline = {
            "prev_lo": prev_lo.isoformat(timespec="seconds"),
            "prev_hi": prev_hi.isoformat(timespec="seconds"),
            "kind": "prev-day-partial" if (since or "").strip().lower() == "today" else "prev-period",
            "partial": partial,
            "total": _agg_dict(prev_total),
        }
        # 项目级 Δ: 当前 ∪ 上期 的并集, 含「已停」(上期有、本期无) 项目, 让复盘看得见退场。
        cur_pg = group_by(rows, lambda r: r.project)
        prev_pg = group_by(prev_rows, lambda r: r.project)
        for lb in set(cur_pg) | set(prev_pg):
            ca, pa = cur_pg.get(lb), prev_pg.get(lb)
            project_deltas.append({
                "label": lb,
                "cost": round(ca.cost, 4) if ca else 0.0,
                "tokens": ca.total_tokens if ca else 0,
                "prev_cost": round(pa.cost, 4) if pa else 0.0,
                "prev_tokens": pa.total_tokens if pa else 0,
                "any_unpriced": bool((ca and ca.any_unpriced) or (pa and pa.any_unpriced)),
            })

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": since or "all",
        "scope": scope,
        "vscode_only": vscode_only,
        "total": _agg_dict(total),
        "baseline": baseline,
        "project_deltas": project_deltas,
        "by_day": _rows(rows, lambda r: r.timestamp.date(),
                        label_fn=lambda d: d.isoformat(), sort_label=True),
        "by_project": _rows(rows, lambda r: r.project),
        "by_model": _rows(rows, lambda r: r.model),
        "by_source": _rows(rows, lambda r: r.source_kind),
    }


def _set_budget(body: dict, base: Path) -> dict:
    """校验并写预算; 立刻重算一帧状态返回。只接受 >=0 的数值, 非法忽略。"""
    def _num(v):
        try:
            n = float(v)
            return n if (n >= 0 and math.isfinite(n)) else None   # 拒 inf/nan (会写出非法 JSON / 静默禁用预算)
        except (TypeError, ValueError):
            return None
    daily = _num(body.get("daily_usd")) if "daily_usd" in body else None
    weekly = _num(body.get("weekly_usd")) if "weekly_usd" in body else None
    projects = None
    if isinstance(body.get("project_usd"), dict):
        projects = {str(k): _num(v) for k, v in body["project_usd"].items()}
        projects = {k: v for k, v in projects.items() if v is not None and v > 0}
    cost_source.save_budget(daily_usd=daily, weekly_usd=weekly, project_usd=projects)
    try:
        cost_source.tick(base)        # 立刻重算, 让 /api/budget 马上反映
    except Exception:
        pass
    return {"ok": True, **cost_source.status()}


def _doctor_status(base: Path) -> dict:
    """两半体检: 成本/测量层 (doctor) + 推断层 (inference_doctor)。按需全量扫描, 故 /doctor 不自动刷新。"""
    from .doctor import build_lines as _cost_lines
    from .doctor import scan as _cost_scan
    from .inference_doctor import build_inference_lines, scan_inference
    try:
        cl, cw = _cost_lines(_cost_scan(base))
    except Exception as e:
        cl, cw = [f"cost doctor 出错: {e}"], 1
    try:
        il, iw = build_inference_lines(scan_inference(base))
    except Exception as e:
        il, iw = [f"inference doctor 出错: {e}"], 1
    return {"cost": {"lines": cl, "warns": cw}, "inference": {"lines": il, "warns": iw}}


def _do_terminate(body: dict) -> dict:
    """终止进程: 控制模式关 -> 拒绝; 否则交 procmon 做新鲜扫描 + 服务端二次校验, 然后审计 (无论成败)。"""
    if not control.plane.remote_mode:
        return {"ok": False, "reason": "control-mode-off"}
    try:
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad-pid"}
    action = "kill" if body.get("action") == "kill" else "terminate"
    r = procmon.terminate(pid, body.get("create_time"), action=action)
    target = f"pid {pid}" + (f" ({r.get('name')})" if r.get("name") else "")
    control.plane.audit_action("terminate", target, r.get("outcome") if r.get("ok") else r.get("reason"))
    return r


def _do_free_port(body: dict) -> dict:
    """释放端口: 控制模式关 -> 拒绝; 否则 procmon 逐 owner 走白名单+身份复核, 然后审计。"""
    if not control.plane.remote_mode:
        return {"ok": False, "reason": "control-mode-off"}
    try:
        port = int(body.get("port"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad-port"}
    action = "kill" if body.get("action") == "kill" else "terminate"
    r = procmon.free_port(port, action=action)
    control.plane.audit_action("free-port", f"port {port}", "ok" if r.get("ok") else r.get("reason"))
    return r


def _set_provider_keys(body: dict) -> dict:
    """写厂商 admin key 到 ~/.tokmon/providers.json (0600), 立刻重采一帧。
    **绝不回显 key** —— 只回「已配置/未配置」。空串 = 清除。"""
    a = body.get("anthropic")
    o = body.get("openai")
    billing.save_keys(anthropic=(str(a) if isinstance(a, str) else None),
                      openai=(str(o) if isinstance(o, str) else None))
    try:
        billing.collect()      # 立刻拉一次, 让页面马上有数
    except Exception:
        pass
    return {"ok": True, **billing.key_status()}


def _steer_targets(base) -> dict:
    """可 steer 目标 = activity 快照里的 main 会话。这是 steer 的 **allow-list 来源** (P7 ①):
    只能 steer 进 Claude 实际跑过的项目根 (by_cwd) 或 resume 已知会话 (by_sid), 手机不能自由填路径。"""
    try:
        snap = activity.snapshot(base, live=procmon.live_claude_index())
    except Exception:
        return {"by_cwd": {}, "by_sid": {}}
    by_cwd, by_sid = {}, {}
    for s in snap.get("sessions", []):
        if s.get("cwd"):
            by_cwd.setdefault(s["cwd"], s)
        if s.get("session_id"):
            by_sid[s["session_id"]] = s
    return {"by_cwd": by_cwd, "by_sid": by_sid}


def _steer_target_list(base) -> list:
    """给 UI 下拉用的可 steer 目标 (只含非敏感元数据, 无会话正文)。"""
    tg = _steer_targets(base)
    return [{"cwd": cwd, "project": s.get("project"), "session_id": s.get("session_id"),
             "state": s.get("state"), "title": s.get("title")}
            for cwd, s in tg["by_cwd"].items()]


def _do_steer(body: dict, base) -> dict:
    """S1 · 平台 spawn/resume 一个可 steer 会话。P7 gate 全落地:
    控制模式关 -> 拒绝; cwd/会话必须在 allow-list 内; 拒绝 resume 活会话 (防双写 transcript); 审计**不含 prompt 正文** (§6)。
    机械交给 runner (失败安全在那里); 这里只做策略/鉴权/审计 (策略与机械分离)。"""
    if not control.plane.remote_mode:
        return {"ok": False, "reason": "control-mode-off"}
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "reason": "empty-prompt"}
    tg = _steer_targets(base)
    sid = body.get("session_id")
    if sid:                                    # resume 已知会话
        row = tg["by_sid"].get(sid)
        if not row:
            return {"ok": False, "reason": "unknown-session"}
        if row.get("state") in ("WORKING", "PROCESSING") or row.get("tool_pending"):
            return {"ok": False, "reason": "session-busy"}   # 失败安全: 不 resume 活会话
        cwd, project, resume = row.get("cwd") or "", row.get("project"), True
    else:                                      # 新 spawn: cwd 必须是已知项目根
        cwd = body.get("cwd") or ""
        row = tg["by_cwd"].get(cwd)
        if not row:
            return {"ok": False, "reason": "unknown-cwd"}    # allow-list: 挡住任意路径
        project, resume = row.get("project"), False
    v = runner.runner.steer(cwd, prompt, session_id=(sid if resume else None), project=project)
    outcome = "started" if not v.get("done") else (v.get("reason") or ("ok" if v.get("ok") else "failed"))
    control.plane.audit_action("steer", f"{project or '?'} · {(v.get('session_id') or '')[:8]}",
                               outcome, session=v.get("session_id"), project=project)  # §6: 无 prompt 正文
    return v


def _do_answer(body: dict) -> dict:
    """S1-v2 · 回答被驱动会话的提问 (Claude 的多选题 -> 你在网页/手机上点一下)。P7:
    控制模式关 -> 拒绝; 只认 runner 挂着的真实 ask_id 与**真实存在的选项** (伪造选项在 runner 里被丢弃);
    审计**不含问题正文与选项文本** (§5.4 内容最小化) —— 正文只经本机 token 端点给 UI 渲染。"""
    if not control.plane.remote_mode:
        return {"ok": False, "reason": "control-mode-off"}
    ask_id = str(body.get("ask_id") or "")
    if not ask_id:
        return {"ok": False, "reason": "missing-ask-id"}
    pending = {a["id"]: a for a in runner.runner.asks()}
    meta = pending.get(ask_id) or {}
    r = runner.runner.answer(ask_id, body.get("answers"))
    sid = meta.get("session_id") or ""
    project = meta.get("project")
    control.plane.audit_action("answer", f"{project or '?'} · {sid[:8]}",
                               "answered" if r.get("ok") else (r.get("reason") or "failed"),
                               session=sid, project=project)      # §5.4: 无问题正文/选项文本
    return r


def _backtest_status(base: Path, days: int = 7) -> dict:
    """L2 三把尺回测, 供前端并排评测。按需全量窗口扫描, 故 /backtest 不自动刷新。"""
    from .inference_backtest import ORACLES, backtest, build_backtest_lines
    out = []
    for o in ORACLES:
        try:
            r = backtest(base, o, days=days)
            acc = round(100.0 * r.correct / r.judgeable, 1) if r.judgeable else 0.0
            cov = round(100.0 * r.judgeable / r.points, 0) if r.points else 0.0
            out.append({"name": o.name, "desc": o.desc, "acc": acc, "cov": cov,
                        "judgeable": r.judgeable, "unjudgeable": r.unjudgeable,
                        "over": r.over_optimism, "false_done": r.false_completion,
                        "lines": build_backtest_lines(r)})
        except Exception as e:
            out.append({"name": o.name, "desc": o.desc, "error": str(e)})
    return {"days": days, "oracles": out}


# ---- /workflow: 工作流回放 (WORKFLOW_TAB_PLAN S1) ----
# trace 支柱只出结构、时间、token 分项; 这里在 serve 层汇合三样它刻意不碰的东西 (P4):
#   ① $ —— 按模型用 pricing.cost_usd 换算 (I3 定价集中, 不复制单价);
#   ② 脱敏 —— 先把本服务**自己知道的密钥原值**精确打码 (控制令牌 / 通知 token / 厂商 admin key),
#      再走 procmon 的模式脱敏 (尽力而为, 不是安全边界);
#   ③ 运行态 —— 取 activity 快照 (**必须带进程存活索引**: activity 的 2s 快照缓存是全局共享的,
#      不带 live 的调用会把一帧没有存活信息的结果喂给 /sessions 与事件 pump —— 对抗式 review 实测的回归)。
_WF_WINDOWS = {"7d": 7 * 86400, "30d": 30 * 86400}
_WF_TEXT_FIELDS = ("label", "sub", "text", "detail", "note")
_WF_SECRET_KEY = re.compile(r"(?i)(token|api[-_]?key|secret|passw(or)?d|pwd|auth(?!or)|bearer|cookie|credential"
                            r"|private[-_]?key|session[-_]?key|access[-_]?key)")
_WF_SECRETS = {"t": 0.0, "vals": ()}


def _wf_since(key: str):
    if key == "all":
        return None
    if key == "today":
        now = datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return time.time() - _WF_WINDOWS.get(key, 7 * 86400)


def _wf_running(base) -> set:
    """正在跑的会话: WORKING / PROCESSING, 或挂着一个等你的调用 (AskUserQuestion / ExitPlanMode)。"""
    try:
        snap = activity.snapshot(base, live=procmon.live_claude_index())
    except Exception:
        return set()
    out = set()
    for x in snap.get("sessions", []):
        st = x.get("state")
        if st in ("WORKING", "PROCESSING") or (
                st == "AMBIGUOUS_PENDING" and x.get("pending_tool_name") in trace.HUMAN_WAIT_TOOLS):
            out.add(x.get("session_id"))
    return out


def _wf_price(by_model: dict) -> tuple:
    """按模型分别换算 $ -> (美元, 含未知单价模型)。统计层通过注入使用 (trace 不依赖计价)。"""
    total, unknown = 0.0, False
    for model, v in (by_model or {}).items():
        c, known = cost_usd(model, v[0], v[1], v[2], v[3], v[4])
        total += c
        unknown = unknown or (not known and sum(v) > 0)   # 0 token 的 <synthetic> 占位消息不算「未知单价」
    return round(total, 4), unknown


def _wf_cost(tokens):
    """在 token 分项上原地补 cost / unpriced (按模型分别换算; 未知模型如实标出, 不瞎估)。"""
    if not isinstance(tokens, dict) or "by_model" not in tokens:
        return
    tokens["cost"], tokens["unpriced"] = _wf_price(tokens["by_model"])
    tokens.pop("by_model", None)                 # 前端不需要按模型的原始数组


def _wf_known_secrets() -> tuple:
    """本服务自己持有的密钥原值 (30s 缓存)。transcript 里一旦出现 (比如测试时打印过), 回放页绝不原样送出。"""
    now = time.time()
    if now - _WF_SECRETS["t"] < 30:
        return _WF_SECRETS["vals"]
    vals = [control.plane.token]
    try:
        n = notify.get_notifier()
        if n is not None:
            vals += [n.cfg.telegram_token, n.cfg.pushover_token, n.cfg.pushover_user]
    except Exception:
        pass
    try:
        vals += list(billing.load_keys().values())
    except Exception:
        pass
    out = tuple(sorted({v for v in vals if isinstance(v, str) and len(v) >= 8}, key=len, reverse=True))
    _WF_SECRETS.update(t=now, vals=out)
    return out


def _wf_red(v):
    if not isinstance(v, str) or not v:
        return v
    for sec in _wf_known_secrets():
        if sec in v:
            v = v.replace(sec, "***")
    return procmon._redact(v)


def _wf_red_obj(v, key: str = ""):
    """结构化数据的脱敏: 字段名像密钥 (password / apiKey / token …) 的值整个打码, 其余字符串走 _wf_red。"""
    if isinstance(v, dict):
        return {k: _wf_red_obj(x, str(k)) for k, x in v.items()}
    if isinstance(v, list):
        return [_wf_red_obj(x, key) for x in v]
    if key and _WF_SECRET_KEY.search(key) and isinstance(v, (str, int, float)) and not isinstance(v, bool) \
            and v not in ("", None):
        return "***"
    return _wf_red(v) if isinstance(v, str) else v


def _wf_scrub_tree(node):
    """整棵树: 文字脱敏 + token 换算 $。就地修改。"""
    stack = [node]
    while stack:
        n = stack.pop()
        for f in _WF_TEXT_FIELDS:
            if f in n:
                n[f] = _wf_red(n[f])
        m = n.get("meta")
        if isinstance(m, dict):
            m.pop("preview", None)               # 页面不用预览 (明细按需取原文并脱敏) —— 少送一份原文
            m.pop("key", None)                   # 归一键只在后端判重复用, 可能含原始命令/参数
            if "instruction" in m:
                m["instruction"] = _wf_red(m["instruction"])
            for d in m.get("deps") or []:        # 数据依赖: 字段名像密钥就不给值; 其余只给开头 12 位
                d["from_label"] = _wf_red(d.get("from_label"))
                v = _wf_red(str(d.get("value") or ""))
                d["value"] = "***" if _WF_SECRET_KEY.search(str(d.get("key") or "")) else (v[:12] + ("…" if len(v) > 12 else ""))
        _wf_cost(n.get("tokens"))
        stack.extend(n.get("children") or [])


def _wf_scrub_summary(sm):
    for f in ("prompt", "prompt_full"):
        if f in sm:
            sm[f] = _wf_red(sm[f])
    L = sm.get("changes")                            # 改动清单: 路径也过一遍脱敏 (路径里可能带 token)
    if L:
        for row in L.get("files") or []:
            row["path"] = _wf_red(row["path"])
        if L.get("verify"):
            L["verify"]["label"] = _wf_red(L["verify"].get("label"))
    for row in sm.get("files") or []:
        row[0] = _wf_red(row[0])
    for x in sm.get("risks") or []:                  # 风险标记: 标题与证据里可能有路径 / 命令片段
        x["label"] = _wf_red(x.get("label"))
        if x.get("detail"):
            x["detail"] = _wf_red(x["detail"])
    for m in sm.get("moments") or []:
        m["label"] = _wf_red(m.get("label"))
    for kind, d in (sm.get("glossary") or {}).items():
        sm["glossary"][kind] = {k: _wf_red(v) for k, v in d.items()}
    _wf_cost(sm.get("tokens"))
    return sm


def _wf_tasks(base, q) -> dict:
    since = _wf_since((q.get("since") or ["7d"])[0])
    project = (q.get("project") or [""])[0] or None
    rows = trace.list_tasks(base, since=since, project=project, running_sessions=_wf_running(base))
    for r in rows:
        _wf_scrub_summary(r)
    return {"tasks": rows}


def _wf_task(base, q) -> dict:
    tid = (q.get("id") or [""])[0]
    built = trace.get_task(tid, base, running_sessions=_wf_running(base)) if tid else None
    if not built:
        return {"error": "找不到这个任务 (可能已超出缓存或会话文件被移走)"}
    tree = built["tree"]
    _wf_scrub_tree(tree)
    return {"summary": _wf_scrub_summary(built["summary"]), "tree": tree}


def _wf_call(base, q) -> dict:
    tid, cid = (q.get("task") or [""])[0], (q.get("call") or [""])[0]
    d = trace.get_call_detail(tid, cid, base) if tid and cid else None
    if not d:
        return {"error": "找不到这个调用"}
    return {k: _wf_red_obj(v) for k, v in d.items()}


def _wf_script(base, q) -> dict:
    tid, run = (q.get("task") or [""])[0], (q.get("run") or [""])[0]
    d = trace.get_script(tid, run, base) if tid and run else None
    if not d:
        return {"error": "找不到这个 workflow 的脚本"}
    d["text"] = _wf_red(d["text"])
    return d


def _wf_text(base, q) -> dict:
    tid, nid = (q.get("task") or [""])[0], (q.get("node") or [""])[0]
    d = trace.get_text_detail(tid, nid, base) if tid and nid else None
    if not d:
        return {"error": "找不到这段文字"}
    return {"kind": d["kind"], "text": _wf_red(d["text"])}


def _wf_window(q) -> tuple:
    return _wf_since((q.get("since") or ["7d"])[0]), ((q.get("project") or [""])[0] or None)


def _wf_scrub_stats(out: dict) -> dict:
    """统计结果的脱敏 (就地; 传进来的必须是副本)。明细按哈希键取, 显示名打码不影响追溯。"""
    for t in (out.get("tasks") or {}).values():
        if t:
            t["prompt"] = _wf_red(t.get("prompt"))
    for r in out.get("tools") or []:
        r["name"] = _wf_red(r["name"])
    for r in out.get("skills") or []:
        r["name"] = _wf_red(r["name"])
    for m in out.get("mcp") or []:
        m["server"] = _wf_red(m["server"])
        for tt in m.get("tools") or []:
            tt["tool"] = _wf_red(tt["tool"])
        for e in m.get("errors") or []:
            e["tool"], e["excerpt"] = _wf_red(e.get("tool")), _wf_red(e.get("excerpt"))
        for c in m.get("causes") or []:              # 失败原因归类: 归类文字与示例原文都来自工具返回
            c["cause"], c["example"] = _wf_red(c.get("cause")), _wf_red(c.get("example"))
            c["tools"] = [_wf_red(t) for t in c.get("tools") or []]
    done = set()                                     # 最慢 / 最贵 与 points 里的是同一个对象 (deepcopy 保留共享): 只处理一次
    for r in out.get("risks") or []:
        r["why"] = _wf_red(r.get("why"))
    for rows in (out.get("compare") or {}).values():
        for r in rows:
            r["name"] = _wf_red(r["name"])
            for o in [r.get("slowest"), r.get("priciest")] + list(r.get("points") or []):
                if o and id(o) not in done:
                    done.add(id(o))
                    if o.get("sub"):
                        o["sub"] = _wf_red(o["sub"])
    return out


def _wf_stats(base, q) -> dict:
    since, project = _wf_window(q)
    res = trace.stats(base, since=since, project=project, running_sessions=_wf_running(base), price=_wf_price,
                      fresh=(q.get("fresh") or [""])[0] == "1")
    return _wf_scrub_stats(copy.deepcopy(res))       # 缓存里的原件不动 (明细还要用它)


def _wf_drill(base, q) -> dict:
    """统计数字背后的明细。那份统计已过期 -> 按同样的窗口重新统计再取, 并告诉页面数字已刷新。"""
    g = lambda k, d="": (q.get(k) or [d])[0]
    ref = g("ref")
    if not ref:
        return {"error": "缺少 ref"}
    try:
        offset, limit = int(g("offset", "0")), int(g("limit", "100"))
    except ValueError:
        offset, limit = 0, 100
    kw = {"flag": g("flag") or None, "sort": g("sort", "time"), "offset": offset, "limit": limit}
    d = trace.stats_refs(g("stamp"), ref, **kw) if g("stamp") else None
    restamped = d is None
    if d is None:
        since, project = _wf_window(q)
        res = trace.stats(base, since=since, project=project, running_sessions=_wf_running(base), price=_wf_price)
        d = trace.stats_refs(res["stamp"], ref, **kw)
        if d is None:
            return {"error": "统计刚被刷新, 请再点一次"}
    out = copy.deepcopy(d)
    for x in out["items"]:
        for f in ("label", "sub"):
            if x.get(f):
                x[f] = _wf_red(x[f])
    for t in out["tasks"].values():
        if t:
            t["prompt"] = _wf_red(t.get("prompt"))
    out["restamped"] = restamped
    return out


_BRIEF_MAX_AGE = 86400                 # 一天没动静的会话不算简报 (列表里可能有几十个老会话)


def _sessions_with_briefs(base) -> dict:
    """/api/sessions = activity 快照 + 每个会话当前任务的简报 (风险标记 / 改动计数 / 回放入口)。
    activity.snapshot 有 2 秒的**共享**缓存 (事件 pump 也在用): 只拷贝、绝不原地改。"""
    snap = activity.snapshot(base, live=procmon.live_claude_index())
    out = dict(snap)
    rows = []
    ready = trace.baseline_ready()      # 冷启动基线还没热好: 先不挂简报 (页面照常出, 十几秒后角标自己出现)
    for row in snap.get("sessions", []):
        r = dict(row)
        age = row.get("last_activity_age_s")
        if ready and row.get("file") and (age is None or age < _BRIEF_MAX_AGE):
            try:
                b = trace.session_brief(row["file"], running=row.get("state") in ("WORKING", "PROCESSING"))
            except Exception:
                b = None
            if b:
                r["workflow"] = {"task": b["task"], "changes": b["changes"], "files": b["files"],
                                 "risks": [{"rule": x["rule"], "label": _wf_red(x["label"]), "n": x.get("n")}
                                           for x in b["risks"]]}
        rows.append(r)
    out["sessions"] = rows
    return out


def _make_handler(base: Path, rcfg: remote.RemoteConfig | None = None):
    rcfg = rcfg or remote.RemoteConfig()          # 默认 = 本机模式 (零行为变化)
    throttle = remote.LoginThrottle()

    class Handler(BaseHTTPRequestHandler):
        # 静默默认日志, 避免污染终端 (盯盘时不想被刷屏)。
        def log_message(self, *args):  # noqa: D401
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, build):
            """渐进降级: 单次请求失败 (含缺 psutil) 不拖垮服务, 回 JSON error。"""
            try:
                body = json.dumps(build(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                err = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self._send(500, err, "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            """挡 DNS-rebinding: 重绑定请求带的是攻击者域名的 Host。
            本机 Host 永远放行; 远程模式下额外放行 MC_REMOTE_HOSTS 里**显式登记**的隧道域名 (非通配)。"""
            return rcfg.host_allowed(self.headers.get("Host"))

        def _ctl_guard(self) -> bool:
            """控制门: Host 白名单 + 有效 control token, **只认自定义头, 永不认 Cookie**。

            自定义头跨站发不出去 (会触发 CORS 预检) —— 这正是控制面today的抗 CSRF 性质。
            读门 (`_read_guard`) 认 Cookie 是因为浏览器导航没别的办法; 两者**故意不合并**,
            谁要"顺手统一"成一个, 就等于把控制面送给 CSRF。"""
            if not self._host_ok() or not control.plane.check_token(self.headers.get("X-Control-Token")):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return False
            return True

        def _read_guard(self, is_page: bool = False) -> bool:
            """读门: 本机模式下恒放行 (零行为变化); 远程模式下**读页/读 API 也要令牌**。

            令牌来自 HttpOnly Cookie (浏览器导航唯一可行的方式) 或 X-Control-Token 头 (命令行客户端)。
            未持令牌: 页面 -> 登录页 (不泄任何数据); API -> 403。"""
            if not self._host_ok():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return False
            if not rcfg.enabled:
                return True
            tok = (remote.cookie_token(self.headers.get("Cookie"))
                   or self.headers.get("X-Control-Token"))
            if control.plane.check_token(tok):
                return True
            if is_page:
                self._send(401, LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return False

        def do_HEAD(self):
            # 让健康探测识别本服务为「存活」(返回状态、无 body), 而非 BaseHTTPRequestHandler 默认的 501。
            known = {"/", "/tokens", "/processes", "/sessions", "/notify", "/control", "/doctor", "/backtest",
                     "/billing", "/workflow", "/api/workflow/tasks", "/api/workflow/task", "/api/workflow/call",
                     "/api/workflow/text", "/api/workflow/script", "/api/workflow/stats", "/api/workflow/drill",
                     "/api/summary", "/api/processes", "/api/health", "/api/sessions", "/api/events",
                     "/api/notifications", "/api/notify-test", "/api/control", "/api/budget", "/api/doctor",
                     "/api/backtest", "/api/billing"}
            p = urlparse(self.path).path
            code = 200 if p in known else 404
            if code == 200 and rcfg.enabled and not control.plane.check_token(
                    remote.cookie_token(self.headers.get("Cookie")) or self.headers.get("X-Control-Token")):
                code = 401                        # 远程模式: 未鉴权只回"我活着", 不确认这个端点存在
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            # 主页分流: / 主页 -> /tokens 现有看板 + /processes 进程监控 (并行同级)
            pages = {"/": HOME, "/tokens": PAGE, "/processes": PROC_PAGE, "/workflow": WORKFLOW_PAGE,
                     "/sessions": SESS_PAGE, "/notify": NOTIFY_PAGE, "/control": CONTROL_PAGE,
                     "/doctor": DOCTOR_PAGE, "/backtest": BACKTEST_PAGE, "/billing": BILLING_PAGE}
            if path in pages:
                if not self._read_guard(is_page=True):
                    return
                self._send(200, pages[path].encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/login":                  # 远程模式下的令牌输入页 (本机模式下也可访问, 无害)
                self._send(200, LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            # 读 API 统一过读门: 远程模式下它们同样吐会话正文/花费, 不能比页面松
            if path.startswith("/api/") and not self._read_guard():
                return
            if path == "/api/workflow/tasks":
                self._json(lambda: _wf_tasks(base, parse_qs(parsed.query)))
                return
            if path == "/api/workflow/task":
                self._json(lambda: _wf_task(base, parse_qs(parsed.query)))
                return
            if path == "/api/workflow/call":
                self._json(lambda: _wf_call(base, parse_qs(parsed.query)))
                return
            if path == "/api/workflow/text":
                self._json(lambda: _wf_text(base, parse_qs(parsed.query)))
                return
            if path == "/api/workflow/script":
                self._json(lambda: _wf_script(base, parse_qs(parsed.query)))
                return
            if path == "/api/workflow/stats":
                self._json(lambda: _wf_stats(base, parse_qs(parsed.query)))
                return
            if path == "/api/workflow/drill":
                self._json(lambda: _wf_drill(base, parse_qs(parsed.query)))
                return
            if path == "/api/billing":
                self._json(billing.status)          # 只回聚合数字, 绝不含 key
                return
            if path == "/api/control":
                self._json(control.plane.status)
                return
            if path == "/api/control/hook-config":
                if not self._ctl_guard():         # 不再无鉴权吐 token: 需已持令牌才给 hook 片段 (评审: 防本机他进程/重绑定窃取)
                    return
                base_url = "http://" + (self.headers.get("Host") or "127.0.0.1:8765")
                self._json(lambda: control.plane.hook_config(base_url))
                return
            if path == "/api/control/steer":
                if not self._ctl_guard():         # steer 状态/流式含会话正文 -> 必须持令牌 (§6)
                    return
                q = parse_qs(parsed.query)
                sid = q.get("session", [None])[0]
                if sid:
                    self._json(lambda: runner.runner.get(sid) or {"error": "unknown-session"})
                else:
                    self._json(lambda: {"targets": _steer_target_list(base), "active": runner.runner.status()})
                return
            if path == "/api/control/asks":
                if not self._ctl_guard():         # 问题正文只给持令牌者 (§5.4: 不进事件/通知, 但 UI 要渲染按钮)
                    return
                self._json(lambda: {"asks": runner.runner.asks()})
                return
            if path == "/api/summary":
                q = parse_qs(parsed.query)
                since = q.get("since", ["7d"])[0]
                scope = q.get("scope", ["all"])[0]
                vscode_only = q.get("vscode_only", ["0"])[0] in ("1", "true", "True")
                self._json(lambda: build_summary(base, since, scope, vscode_only))
                return
            if path == "/api/processes":
                self._json(procmon.snapshot)
                return
            if path == "/api/sessions":
                # 组合层在这里把 process 支柱的活性索引注入 activity —— activity 本身不 import procmon;
                # 再把 trace 支柱的「当前任务简报」挂上 (角标 + 实时回放入口)。
                self._json(lambda: _sessions_with_briefs(base))
                return
            if path == "/api/events":
                q = parse_qs(parsed.query)
                since = int(q.get("since", ["0"])[0] or 0)
                types = q.get("type") or None
                pillar = q.get("pillar", [None])[0]
                self._json(lambda: event_bus.since(since, types=types, pillar=pillar))
                return
            if path == "/api/notifications":
                n = notify.get_notifier()
                self._json(lambda: n.status() if n else {"error": "notifier off"})
                return
            if path == "/api/budget":
                self._json(cost_source.status)
                return
            if path == "/api/doctor":
                self._json(lambda: _doctor_status(base))
                return
            if path == "/api/backtest":
                q = parse_qs(parsed.query)
                days = int(q.get("days", ["7"])[0] or 7)
                self._json(lambda: _backtest_status(base, days))
                return
            if path == "/api/notify-test":
                n = notify.get_notifier()
                self._json(lambda: n.send_test() if n else {"ok": False, "detail": "notifier off"})
                return
            if path == "/api/health":
                q = parse_qs(parsed.query)
                raw = q.get("ports", [""])[0]
                ports = [int(x) for x in raw.split(",")
                         if x.strip().isdigit() and 1 <= int(x) <= 65535] or None
                self._json(lambda: procmon.probe_health(ports))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            q = parse_qs(parsed.query)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    body = {}
            except Exception:
                body = {}
            # Claude Code PermissionRequest hook 回调: 阻塞拿决定或 defer (失败安全)
            # token 优先走 header (§6: 隧道/边缘可能把 query 写进日志); query 仅为兼容已装好的旧 hook 配置,
            # 且**只在本机 Host 上**接受 —— 经隧道来的请求一律要 header。
            if path == "/hook/permission":
                token = self.headers.get("X-Control-Token")
                if not token and remote.normalize_host(self.headers.get("Host")) in remote.LOCAL_HOSTS:
                    token = q.get("token", [None])[0]
                result = control.plane.handle_permission(body, token)
                self._json(lambda: control.shape_hook_response(result))
                return
            # 远程模式的读门登录: 拿令牌换一个 HttpOnly Cookie (浏览器导航没法带自定义头)。
            # 注意: 这只开**读**门 —— 控制端点照旧只认 X-Control-Token 头, Cookie 对它无效。
            if path == "/api/login":
                if not self._host_ok():
                    self._send(403, b"forbidden", "text/plain; charset=utf-8")
                    return
                tok = str(body.get("token") or "")
                if not control.plane.check_token(tok):
                    warn = throttle.on_failure()
                    if warn:
                        print(warn)               # 让"有人在试"这件事出现在你的终端里
                    time.sleep(throttle.delay_s)  # 常数时间比较已在 check_token; 这层只为拖慢尝试
                    self._send(403, b'{"ok":false}', "application/json; charset=utf-8")
                    return
                throttle.on_success()
                body_ok = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_ok)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", remote.build_set_cookie(tok, secure=rcfg.cookie_secure()))
                self.end_headers()
                self.wfile.write(body_ok)
                return
            if path == "/api/logout":
                out = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", remote.clear_cookie(secure=rcfg.cookie_secure()))
                self.end_headers()
                self.wfile.write(out)
                return
            # 本机用户从看板发起的状态变更: 本机 Host + token (自定义头 -> 跨站/DNS-rebinding 都过不了)
            if path in ("/api/control/mode", "/api/control/decide", "/api/budget",
                        "/api/control/terminate", "/api/control/free-port", "/api/control/steer",
                        "/api/control/answer", "/api/billing/keys"):
                if not self._ctl_guard():
                    return
                if path == "/api/billing/keys":     # 写入 org-admin 级密钥 -> 必须持令牌
                    self._json(lambda: _set_provider_keys(body))
                    return
                if path == "/api/control/mode":
                    self._json(lambda: {"ok": True, "remote_mode": control.plane.set_mode(bool(body.get("on")))})
                elif path == "/api/control/decide":
                    self._json(lambda: {"ok": control.plane.resolve(body.get("id"), body.get("decision"))})
                elif path == "/api/control/terminate":
                    self._json(lambda: _do_terminate(body))
                elif path == "/api/control/free-port":
                    self._json(lambda: _do_free_port(body))
                elif path == "/api/control/steer":
                    self._json(lambda: _do_steer(body, base))
                elif path == "/api/control/answer":
                    self._json(lambda: _do_answer(body))
                else:   # /api/budget 设置预算
                    self._json(lambda: _set_budget(body, base))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def run_serve(base: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    if not Path(base).exists():
        print(f"找不到 Claude 数据目录: {base}")
        return
    # 远程暴露收口 (MC_REMOTE): 配置不自洽 -> **拒绝启动**, 绝不带着半个洞跑起来 (P7 ⑤ 失败安全)。
    rcfg = remote.from_env()
    problem = remote.preflight(rcfg, host, control.plane.token)
    if problem:
        print(problem)
        return
    activity_source.start_pump(Path(base), live_factory=procmon.live_claude_index)   # M2: 对话活动事件 pump (5s, 带活性消歧)
    cost_source.start_pump(Path(base))        # M3.5: 成本预算 pump (60s)
    risk_source.start_pump(Path(base), live_factory=procmon.live_claude_index)   # 改动与风险 S3: 风险事件 pump (15s, 只发 info)
    billing.start_pump()                      # B1: 厂商账单 pump (5min; 未配 key 则零外发)
    trace.start_warmer(Path(base))            # /workflow: 后台把 transcript 读进缓存 + 算耗时基线 (冷启动约 6-10s)
    notifier = notify.start_notifier()        # M3: 通知层订阅总线 (默认仅本地, 配 token 才外发)
    httpd = ThreadingHTTPServer((host, port), _make_handler(Path(base), rcfg))
    url = f"http://{host}:{port}/"
    scope = "远程模式: 读页也要令牌" if rcfg.enabled else "只监听本机"
    print(f"Claude Mission Control 已启动 ({scope}):  {url}")
    print(f"  · 对话/Session 状态  {url}sessions")
    print(f"  · Token 看板         {url}tokens")
    print(f"  · 工作流              {url}workflow  (回放: 一次提问怎么被完成的 · 统计: 排行 / MCP 健康 / 跨任务对照)")
    _chans = [c for c, ok in (("Telegram", notifier.cfg.telegram_configured()),
                              ("Pushover", notifier.cfg.pushover_configured())) if ok]
    if _chans:
        print(f"  ⚠ 外发通道已配置: {' + '.join(_chans)} —— 会话事件会推到手机 (通知页已从导航隐藏, 仍可访问 {url}notify)")
    print(f"  · 进程/端口监控       {url}processes" + ("" if procmon.available() else "   (缺 psutil, 该页会提示安装)"))
    print(f"  · 体检 (doctor)       {url}doctor   (成本契约 + 推断契约, 对真相校验)")
    print(f"  控制令牌 (页面首次开控制模式/终止进程时粘贴一次, 之后存浏览器): {control.plane.token}")
    print("  (令牌不再经 HTTP 下发, 只在这里/~/.tokmon/control_token 可见 —— 防本机他进程/网页窃取后终止你的进程)")
    if rcfg.enabled:
        print(f"  ⚠ 远程模式已开: 全部页面与 /api/* 都要令牌; Host 白名单 = {', '.join(sorted(rcfg.hosts))}")
        print("    手机上先开 /login 贴一次令牌 (存 HttpOnly Cookie, 12h 过期)。")
        print("    补偿纪律 (token-only 是唯一的闸): 隧道 URL 当秘密 · 用完即关隧道 · 泄露即轮换 ~/.tokmon/control_token")
        print("    诚实边界: 它的入站攻击面 > Telegram 零端口长轮询 —— 那才是更安全的终态。")
    print("在浏览器打开主页。Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


# ---- 单页前端 (内联, 无外部依赖, 可离线) ----
# 远程模式 (MC_REMOTE) 的令牌输入页。它在未鉴权时**代替**被请求的页面返回 (HTTP 401),
# 所以登录成功后只需 reload —— 浏览器带着新 Cookie 重新请求同一个 URL, 直接落在你本来要去的页。
# 纪律: 令牌只进 password 框, 绝不回显、绝不进 URL (隧道边缘会记 query)、绝不存 localStorage(读门用 HttpOnly Cookie)。
LOGIN_PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mission Control · 需要令牌</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --line:#262b36; --fg:#e6e9ef; --dim:#8b93a7;
          --accent:#7aa2f7; --warn:#e0af68; --bad:#f7768e; }
  * { box-sizing:border-box }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
         font-family:"Segoe UI",system-ui,-apple-system,"Microsoft YaHei",sans-serif; font-size:15px; padding:20px; }
  .box { width:100%; max-width:390px; background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:26px 24px; }
  h1 { font-size:18px; margin:0 0 6px; }
  p.sub { color:var(--dim); font-size:13px; margin:0 0 18px; line-height:1.6; }
  input { width:100%; padding:11px 13px; font-size:15px; border-radius:9px; border:1px solid var(--line);
          background:#11141a; color:var(--fg); }
  input:focus { outline:none; border-color:var(--accent); }
  button { width:100%; margin-top:11px; padding:11px; font-size:15px; border-radius:9px; border:0; cursor:pointer;
           background:var(--accent); color:#0f1115; font-weight:600; }
  button:disabled { opacity:.55; cursor:default; }
  .msg { margin-top:12px; font-size:13px; min-height:18px; }
  .msg.err { color:var(--bad); }
  .note { margin-top:18px; padding-top:14px; border-top:1px solid var(--line); color:var(--dim); font-size:12px; line-height:1.65; }
  .note b { color:var(--warn); font-weight:600; }
</style></head>
<body>
<div class="box">
  <h1>需要控制令牌</h1>
  <p class="sub">这台机器开了远程模式，所有页面与接口都要令牌。<br>
     令牌在启动 <code>tokmon serve</code> 的终端里，或 <code>~/.tokmon/control_token</code>。</p>
  <form id="f" autocomplete="off">
    <input type="password" id="t" placeholder="control token" autocomplete="off" autocapitalize="off" spellcheck="false">
    <button id="b" type="submit">进入</button>
  </form>
  <div class="msg" id="m"></div>
  <div class="note">
    <b>token-only 是唯一的闸</b>，所以：隧道 URL 当秘密、用完即关隧道、泄露就轮换令牌。<br>
    令牌只存在这台浏览器的 HttpOnly Cookie 里（12 小时过期），不进 URL、不进 localStorage。
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
$('#f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const tok = $('#t').value.trim();
  if(!tok) return;
  $('#b').disabled = true; $('#m').className='msg'; $('#m').textContent='校验中…';
  try {
    const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({token: tok})});
    if (r.ok) { $('#m').textContent='通过，正在进入…'; location.reload(); return; }
    $('#m').className='msg err'; $('#m').textContent='令牌不对（服务端会拖慢重试，并在终端记一笔）';
  } catch (err) {
    $('#m').className='msg err'; $('#m').textContent='请求失败: ' + err;
  }
  $('#t').value=''; $('#b').disabled=false;
});
$('#t').focus();
</script>
</body></html>
"""


PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tokmon · Token 看板</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --line:#262b36; --fg:#e6e9ef; --dim:#8b93a7;
          --accent:#7aa2f7; --warn:#e0af68; --good:#9ece6a; --bar:#3d59a1; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--line);
           display:flex; flex-wrap:wrap; gap:16px; align-items:center; }
  h1 { font-size:18px; margin:0; font-weight:600; }
  h1 span { color:var(--dim); font-weight:400; font-size:13px; margin-left:8px; }
  .controls { display:flex; gap:10px; align-items:center; margin-left:auto; flex-wrap:wrap; }
  select, button, label.chk { background:var(--panel); color:var(--fg);
           border:1px solid var(--line); border-radius:8px; padding:6px 10px; font-size:13px; }
  button { cursor:pointer; } button:hover { border-color:var(--accent); }
  label.chk { display:inline-flex; gap:6px; align-items:center; cursor:pointer; }
  main { padding:24px; display:grid; gap:20px;
         grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); max-width:1280px; }
  .kpis { grid-column:1/-1; display:grid; gap:14px;
          grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); }
  .kpi { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .kpi .v { font-size:24px; font-weight:600; }
  .kpi .k { color:var(--dim); font-size:12px; margin-bottom:4px; }
  .chiprow { margin-top:7px; }
  .chip { display:inline-block; font-size:11px; padding:2px 7px; border-radius:6px;
          border:1px solid var(--line); white-space:nowrap; }
  .chip.good { color:var(--good); border-color:#2c3a23; background:#151d12; }
  .chip.bad  { color:var(--warn); border-color:#3a3320; background:#1d1a12; }
  .chip.flat { color:var(--dim); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .card h2 { font-size:14px; margin:0 0 12px; font-weight:600; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--dim); font-weight:500; font-size:12px; }
  td.lbl { max-width:220px; overflow:hidden; text-overflow:ellipsis; }
  .bars { display:flex; flex-direction:column; gap:6px; }
  .bar-row { display:grid; grid-template-columns:90px 1fr auto; gap:10px; align-items:center; font-size:12px; }
  .bar-track { background:#11141a; border-radius:5px; height:18px; overflow:hidden; }
  .bar-fill { background:var(--bar); height:100%; border-radius:5px; min-width:2px; }
  .pd-row { display:grid; grid-template-columns:1fr auto auto; gap:12px; align-items:center; font-size:12px; }
  .muted { color:var(--dim); } .warn { color:var(--warn); }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
  a { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1><a href="/" style="color:var(--dim);text-decoration:none;margin-right:6px" title="返回监控台主页">←</a>tokmon <span>Token 看板 · 早期预览</span> <a href="/workflow" style="font-size:13px;margin-left:10px;font-weight:400">钱花在哪 → 工作流回放</a></h1>
  <div class="controls">
    <select id="since" title="时间窗口">
      <option value="today">今天</option>
      <option value="24h">近 24h</option>
      <option value="7d" selected>近 7 天</option>
      <option value="2w">近 2 周</option>
      <option value="all">全部</option>
    </select>
    <select id="scope" title="范围">
      <option value="all" selected>全部 (含子智能体/workflow)</option>
      <option value="main">仅主会话</option>
    </select>
    <label class="chk"><input type="checkbox" id="vscode"> 仅 .vscode</label>
    <label class="chk"><input type="checkbox" id="auto"> 自动刷新 5s</label>
    <button id="refresh">刷新</button>
  </div>
</header>
<div id="budget" style="padding:0 24px;margin-top:14px"></div>
<main id="app"><div class="muted" style="grid-column:1/-1">加载中…</div></main>
<footer id="foot"></footer>

<script>
const $ = s => document.querySelector(s);
// 标签来自 cwd 文件夹名 (未公开格式, 视为真相) -> 转义后再进 innerHTML, 保证忠实显示且不被当作标记。
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtTokens(n){
  n = +n || 0;
  if(n>=1e9) return (n/1e9).toFixed(2)+"B";
  if(n>=1e6) return (n/1e6).toFixed(2)+"M";
  if(n>=1e3) return (n/1e3).toFixed(1)+"k";
  return String(n|0);
}
function fmtUsd(x){ return "$"+(+x||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }

// 自基线 Δ 徽标。higherIsGood: 命中率这种「越高越好」传 true; 成本/token 传 false。
// 诚实约定: 无基线 -> 「无基线」; 上期为 0 而本期>0 -> 「新增」; 先给绝对量再给 %。
function deltaChip(cur, prev, hasBaseline, fmtAbs, higherIsGood){
  if(!hasBaseline) return `<span class="chip flat" title="当前窗口无可比的上一周期 (选 today/24h/7d/2w 才有基线)">— 无基线</span>`;
  const diff = cur - prev;
  const absTxt = (diff>=0?"+":"−") + fmtAbs(Math.abs(diff));
  if(prev === 0){
    if(cur === 0) return `<span class="chip flat" title="上一周期也为 0">持平</span>`;
    const cls = higherIsGood ? "good" : "bad";
    return `<span class="chip ${cls}" title="上一周期为 0 (${absTxt})">＋新增</span>`;
  }
  const p = (cur - prev) / prev * 100;
  const cls = Math.abs(p) <= 0.5 ? "flat" : (((p > 0) === higherIsGood) ? "good" : "bad");
  const arrow = p > 0.5 ? "▲" : (p < -0.5 ? "▼" : "·");
  return `<span class="chip ${cls}" title="对比你自己的上一等长周期 (非预算)">${arrow} ${absTxt} · ${(p>=0?"+":"")+p.toFixed(0)}%</span>`;
}

function hitRate(a){
  const denom = (a.cache_read||0) + (a.cache_write||0) + (a.input||0);
  return denom > 0 ? a.cache_read/denom : null;   // 命中率 = 缓存读 / (缓存读+缓存写+输入)
}

function barList(rows, link){
  if(!rows.length) return `<div class="muted">无数据</div>`;
  const m = Math.max(1, ...rows.map(r=>r.tokens));
  return `<div class="bars">` + rows.map(r=>`
    <div class="bar-row">
      ${link ? `<a class="lbl" href="${link(r.label)}" title="看 ${esc(r.label)} 的任务回放 →" style="color:inherit">${esc(r.label)} ↗</a>`
             : `<span class="lbl" title="${esc(r.label)}">${esc(r.label)}</span>`}
      <div class="bar-track"><div class="bar-fill" style="width:${(r.tokens/m*100).toFixed(1)}%"></div></div>
      <span>${fmtTokens(r.tokens)} · ${fmtUsd(r.cost)}${r.any_unpriced?'<span class="warn">*</span>':''}</span>
    </div>`).join("") + `</div>`;
}
function tableOf(rows){
  if(!rows.length) return `<div class="muted">无数据</div>`;
  return `<table><thead><tr>
    <th>名称</th><th>Tokens</th><th>Input</th><th>Output</th><th>Cache R/W</th><th>Cost</th><th>Msgs</th>
    </tr></thead><tbody>` + rows.map(r=>`<tr>
      <td class="lbl" title="${esc(r.label)}">${esc(r.label)}</td>
      <td>${fmtTokens(r.tokens)}</td>
      <td>${fmtTokens(r.input)}</td>
      <td>${fmtTokens(r.output)}</td>
      <td>${fmtTokens(r.cache_read)}/${fmtTokens(r.cache_write)}</td>
      <td>${fmtUsd(r.cost)}${r.any_unpriced?'<span class="warn">*</span>':''}</td>
      <td>${r.count}</td>
    </tr>`).join("") + `</tbody></table>`;
}

// 「变化最大的项目」: 按成本相对上一周期的变化绝对值排序, 含新增/已停。
function projectDeltaCard(pd, hasBaseline){
  if(!hasBaseline || !pd.length) return "";
  const rows = pd.map(p=>({...p, diff:(p.cost||0)-(p.prev_cost||0)}))
                 .filter(p=>p.cost>0 || p.prev_cost>0)
                 .sort((a,b)=>Math.abs(b.diff)-Math.abs(a.diff))
                 .slice(0,6);
  if(!rows.length) return "";
  const body = rows.map(p=>{
    let status, cls;
    const absd = (p.diff>=0?"+":"−")+fmtUsd(Math.abs(p.diff));
    if(p.prev_cost===0 && p.cost>0){ status="＋新增"; cls="bad"; }
    else if(p.cost===0 && p.prev_cost>0){ status="已停 ✓"; cls="good"; }
    else { const pp=(p.cost-p.prev_cost)/p.prev_cost*100;
           status=(pp>0.5?"▲ +":(pp<-0.5?"▼ ":"· "))+Math.abs(pp).toFixed(0)+"%";
           cls=pp>0.5?"bad":(pp<-0.5?"good":"flat"); }
    const star = p.any_unpriced ? '<span class="warn">*</span>' : '';
    return `<div class="pd-row">
      <span class="lbl" title="${esc(p.label)}">${esc(p.label)}</span>
      <span class="muted">${fmtUsd(p.cost)}${star}</span>
      <span class="chip ${cls}" title="vs 上一周期 ${absd}">${status} · ${absd}</span></div>`;
  }).join("");
  return `<div class="card"><h2>变化最大的项目 <span class="muted" style="font-weight:400;font-size:12px;margin-left:6px">成本 vs 上一周期</span></h2><div class="bars">${body}</div></div>`;
}

let reqId = 0;
async function load(){
  const my = ++reqId;                       // 只让最新一次请求作数 -> 丢弃乱序/过期响应
  const since = $("#since").value, scope = $("#scope").value;
  const vscode = $("#vscode").checked ? "1" : "0";
  $("#foot").textContent = "加载中…";
  try{
    const r = await fetch(`/api/summary?since=${since}&scope=${scope}&vscode_only=${vscode}`);
    const d = await r.json();
    if(my !== reqId) return;                 // 期间已有更新的请求发出, 本次结果作废
    if(d.error){ $("#app").innerHTML = `<div class="warn" style="grid-column:1/-1">出错: ${esc(d.error)}</div>`; return; }
    render(d);
    let base = "";
    if(d.baseline){
      base = d.baseline.kind==="prev-day-partial"
        ? " · 基线=昨天同一时段 (非预算)"
        : ` · 基线=上一等长周期 ${d.baseline.prev_lo.slice(0,10)}~${d.baseline.prev_hi.slice(0,10)} (非预算)`;
      if(d.baseline.partial) base += ` · <span class="warn">⚠ 历史不足, 基线偏低</span>`;
      if(d.baseline.total.any_unpriced) base += ` · <span class="warn">* 基线含未知单价模型, 对比偏高</span>`;
    } else {
      base = " · 当前窗口无可比基线 (选 today/24h/7d/2w 可对比)";
    }
    $("#foot").innerHTML = `数据生成于 ${esc(d.generated_at)} · 窗口=${esc(d.window)} · scope=${esc(d.scope)}`
      + base
      + (d.total.any_unpriced ? ` · <span class="warn">* 含未知单价模型, 成本偏低</span>` : "")
      + ` · 纯本地只读, 不外发`;
  }catch(e){ if(my === reqId) $("#foot").textContent = "请求失败: "+e; }
}

function render(d){
  const t = d.total, b = d.baseline, hasB = !!b;
  const hr = hitRate(t), hrPrev = hasB ? hitRate(b.total) : null;
  const hrChip = (hr!==null && hrPrev!==null)
    ? deltaChip(hr*100, hrPrev*100, true, x=>x.toFixed(1)+"pp", true) : "";
  const kpis = [
    {k:"总 Tokens", v:fmtTokens(t.tokens),
     chip:deltaChip(t.tokens, hasB?b.total.tokens:0, hasB, fmtTokens, false)},
    {k:"等价成本", v:fmtUsd(t.cost)+((t.any_unpriced||(hasB&&b.total.any_unpriced))?' *':''),
     chip:deltaChip(t.cost, hasB?b.total.cost:0, hasB, fmtUsd, false)},
    {k:"缓存命中率", v: hr===null?"—":(hr*100).toFixed(0)+"%", chip:hrChip},
    {k:"Output", v:fmtTokens(t.output), chip:""},
    {k:"消息数", v:String(t.count), chip:""},
  ];
  const kpiHtml = kpis.map(c=>`<div class="kpi"><div class="k">${c.k}</div>`
    + `<div class="v">${c.v}</div>${c.chip?`<div class="chiprow">${c.chip}</div>`:""}</div>`).join("");
  $("#app").innerHTML = `
    <div class="kpis">${kpiHtml}</div>
    <div class="card"><h2>按天</h2>${barList(d.by_day)}</div>
    <div class="card"><h2>按项目 <span class="muted" style="font-size:12px;font-weight:400">点项目名看它的任务回放</span></h2>${barList(d.by_project, l => '/workflow?project=' + encodeURIComponent(l) + '&since=' + ({today:'today','24h':'7d','7d':'7d','2w':'30d',all:'all'}[$("#since").value] || '7d'))}</div>
    ${projectDeltaCard(d.project_deltas, hasB)}
    <div class="card"><h2>按模型</h2>${tableOf(d.by_model)}</div>
    <div class="card"><h2>按来源 (main / subagent / workflow)</h2>${tableOf(d.by_source)}</div>
  `;
}

// ---- 预算 / 阈值告警 (M3.5) ----
let CTRL_TOKEN=localStorage.getItem('mc_ctl_token')||'';
const SCOPE_ZH={daily:'今日预算',weekly:'近7天预算',project:'项目预算'};
function bcolor(pct){ return pct>=90?'#f7768e':(pct>=70?'var(--warn)':'var(--good)'); }
async function loadBudget(){
  const af=document.activeElement;                 // 别在你正打字时把表单重绘没了
  if(af && (af.id==='bd'||af.id==='bw')) return;
  try{
    const d=await (await fetch('/api/budget')).json();
    const bs=d.budgets||[];
    let inner='';
    if(bs.length){
      inner='<div style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">'+bs.map(b=>{
        const pct=Math.max(0,b.pct), w=Math.min(100,pct), col=bcolor(pct);
        const name=(SCOPE_ZH[b.scope]||b.scope)+(b.project?(' · '+esc(b.project)):'');
        return `<div class="card" style="padding:12px 14px">
          <div style="display:flex;justify-content:space-between"><span>${name}</span><span style="color:${col};font-weight:600">${pct}%</span></div>
          <div style="height:8px;border-radius:5px;background:#11141a;margin-top:8px;overflow:hidden"><div style="height:100%;width:${w}%;background:${col}"></div></div>
          <div class="muted" style="font-size:12px;margin-top:6px">${fmtUsd(b.spend)} / ${fmtUsd(b.limit)}</div></div>`;
      }).join('')+'</div>';
    } else {
      inner='<div class="muted" style="font-size:13px">未设预算。设个日/周等价美元上限, 越线(70%/90%)就主动提醒你 →</div>';
    }
    const form=`<div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
        <span class="muted" style="font-size:12px">设预算 $:</span>
        <input id="bd" type="number" min="0" step="1" placeholder="日" style="width:80px;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:5px 8px">
        <input id="bw" type="number" min="0" step="1" placeholder="周(近7天)" style="width:110px;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:5px 8px">
        <button id="bsave" style="background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:5px 12px;cursor:pointer">保存</button>
        <span id="bmsg" class="muted" style="font-size:12px"></span></div>`;
    $('#budget').innerHTML=`<h2 style="font-size:15px;margin:0 0 8px">预算 / 阈值告警 <span class="muted" style="font-weight:400;font-size:12px">越线 → 事件 → 通知 (critical@90%)</span></h2>${inner}${form}`;
    $('#bsave').addEventListener('click', saveBudget);
  }catch(e){}
}
async function saveBudget(){
  if(!CTRL_TOKEN){ const t=prompt('粘贴控制令牌 (见服务器控制台 / ~/.tokmon/control_token):'); if(t&&t.trim()){ CTRL_TOKEN=t.trim(); localStorage.setItem('mc_ctl_token',CTRL_TOKEN); } }
  if(!CTRL_TOKEN){ $('#bmsg').textContent='需要控制令牌'; return; }
  const body={}; const d=$('#bd').value, w=$('#bw').value;
  if(d!=='') body.daily_usd=parseFloat(d);
  if(w!=='') body.weekly_usd=parseFloat(w);
  $('#bmsg').textContent='保存中…';
  try{
    const r=await fetch('/api/budget',{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':CTRL_TOKEN},body:JSON.stringify(body)});
    if(r.status===403){ localStorage.removeItem('mc_ctl_token'); CTRL_TOKEN=''; $('#bmsg').textContent='令牌无效, 重试'; return; }
    $('#bmsg').textContent = r.ok?'已保存':('失败 '+r.status); loadBudget();
  }catch(e){ $('#bmsg').textContent='失败: '+e; }
}

let timer = null;
function tickAll(){ load(); loadBudget(); }
function setAuto(){
  if(timer){ clearInterval(timer); timer=null; }
  if($("#auto").checked){ timer = setInterval(tickAll, 5000); }
}
["since","scope","vscode"].forEach(id=>$("#"+id).addEventListener("change", load));
$("#auto").addEventListener("change", setAuto);
$("#refresh").addEventListener("click", tickAll);
load(); loadBudget();
</script>
</body>
</html>
"""


# ---- 公共样式 (主页 + 进程页共用基础皮肤) ----
_BASE_CSS = """
  :root { --bg:#0f1115; --panel:#181b22; --line:#262b36; --fg:#e6e9ef; --dim:#8b93a7;
          --accent:#7aa2f7; --warn:#e0af68; --good:#9ece6a; --bad:#f7768e; --bar:#3d59a1; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  a { color:var(--accent); }
  header { padding:18px 24px; border-bottom:1px solid var(--line);
           display:flex; flex-wrap:wrap; gap:16px; align-items:center; }
  h1 { font-size:18px; margin:0; font-weight:600; }
  h1 span { color:var(--dim); font-weight:400; font-size:13px; margin-left:8px; }
  .nav { display:flex; gap:14px; margin-left:auto; align-items:center; font-size:13px; }
  .nav a { color:var(--dim); text-decoration:none; padding:4px 8px; border-radius:7px; }
  .nav a:hover, .nav a.active { color:var(--fg); background:var(--panel); }
  .nav details.more { position:relative; }
  .nav details.more summary { list-style:none; cursor:pointer; color:var(--dim); padding:4px 8px; border-radius:7px; }
  .nav details.more summary::-webkit-details-marker { display:none; }
  .nav details.more summary:hover, .nav details.more summary.active, .nav details.more[open] summary { color:var(--fg); background:var(--panel); }
  .nav details.more .menu { position:absolute; right:0; top:calc(100% + 4px); display:flex; flex-direction:column; gap:2px;
      min-width:120px; background:var(--panel); border:1px solid var(--line); border-radius:9px; padding:4px; z-index:60;
      box-shadow:0 8px 24px rgba(0,0,0,.45); }
  .muted { color:var(--dim); } .warn { color:var(--warn); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }
"""

# ---- 统一导航 (改动与风险 S3: 低频页收进「更多」; 一处定义, 各页替换 __NAV__) ----
_NAV_MAIN = [("/", "主页"), ("/sessions", "Session 状态"), ("/tokens", "Token 看板"), ("/workflow", "工作流"),
             ("/processes", "进程监控")]
_NAV_MORE = [("/doctor", "体检"), ("/backtest", "回测"), ("/billing", "厂商账单")]


def _nav_html(active: str = "") -> str:
    def a(h, t):
        return f'<a href="{h}"' + (' class="active"' if h == active else "") + f">{t}</a>"
    cur = next((t for h, t in _NAV_MORE if h == active), "")
    return ("".join(a(h, t) for h, t in _NAV_MAIN)
            + f'<details class="more"><summary' + (' class="active"' if cur else "") + ">"
            + (f"更多 · {cur}" if cur else "更多") + ' ▾</summary><div class="menu">'
            + "".join(a(h, t) for h, t in _NAV_MORE) + "</div></details>")

# ---- 主页 (并行同级入口) ----
HOME = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>本地监控台</title>
<style>__BASE__
  main { padding:40px 24px; max-width:880px; margin:0 auto; }
  .lead { color:var(--dim); margin:0 0 28px; }
  .grid { display:grid; gap:20px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }
  .tile { display:block; text-decoration:none; color:inherit; background:var(--panel);
          border:1px solid var(--line); border-radius:14px; padding:22px; transition:border-color .15s; }
  .tile:hover { border-color:var(--accent); }
  .tile h2 { margin:0 0 8px; font-size:17px; }
  .tile p { margin:0; color:var(--dim); font-size:13px; }
  .tile .ico { font-size:26px; margin-bottom:10px; }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); text-align:center; }
</style></head>
<body>
<header><h1>Claude Mission Control <span>AI coding 运维驾驶舱 · 纯本地只读, 不外发</span></h1></header>
<main>
  <p class="lead">几个并行同级的监控视图, 都只读本机、只监听 127.0.0.1, 默认不通知、不外发。</p>
  <div class="grid">
    <a class="tile" href="/sessions">
      <div class="ico">🛰️</div>
      <h2>对话 / Session 状态 →</h2>
      <p>每个 Claude Code session 现在是「推进 / 处理 / 久未返回 / 等你 / 读不出」: 从 transcript 推断, 诚实标注不确定。M1·先看见, 不通知。</p>
    </a>
    <a class="tile" href="/tokens">
      <div class="ico">📊</div>
      <h2>Token 看板 →</h2>
      <p>Claude Code 烧了多少 token / 等价多少钱: 按天/项目/模型/来源, 带「vs 上一周期」自基线对比。</p>
    </a>
    <a class="tile" href="/workflow">
      <div class="ico">🧭</div>
      <h2>工作流回放 →</h2>
      <p>一次提问是怎么被完成的: 调了哪些 skill / 工具 / MCP / 子 agent / workflow, 各花多少时间和 token, 哪里失败、打转、等你。</p>
    </a>
    <a class="tile" href="/billing">
      <div class="ico">🧾</div>
      <h2>厂商账单 →</h2>
      <p>OpenAI · Anthropic 的<b>真实</b> API 平台开销 (官方 usage/cost API)。与 /tokens 的<b>等价估算</b>是两个计费池, 不该相等。Google 无官方 API → 诚实标「不可得」。默认不配 key 则零外发。</p>
    </a>
    <a class="tile" href="/processes">
      <div class="ico">⚙️</div>
      <h2>进程 / 端口监控 →</h2>
      <p>本机进程资源、localhost 监听端口→占用进程、活动网络连接, 以及 cloudflared 隧道进程。纯只读。</p>
    </a>
    <a class="tile" href="/doctor">
      <div class="ico">🩺</div>
      <h2>体检 (doctor) →</h2>
      <p>对真相校验: 成本契约 + **推断契约**(activity/events 的格式假设)。Claude Code 一变, 你先看见, 而不是推断悄悄算错。</p>
    </a>
    <a class="tile" href="/backtest">
      <div class="ico">🎯</div>
      <h2>准确率回测 (L2) →</h2>
      <p>用 transcript 的"未来"当真值, 量 classify_state 到底准不准: 准确率 + 混淆矩阵 + 危险格。三把尺(严/中/宽)并排, 你挑最信的。</p>
    </a>
  </div>
</main>
<footer>Claude Mission Control · 纯标准库 + psutil · 只监听本机 · 默认不通知不外发</footer>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- 进程 / 端口 / cloudflared 监控页 ----
PROC_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>进程 / 端口监控</title>
<style>__BASE__
  .controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  input[type=text], select, button {
      background:var(--panel); color:var(--fg); border:1px solid var(--line);
      border-radius:8px; padding:6px 10px; font-size:13px; }
  button { cursor:pointer; } button:hover { border-color:var(--accent); }
  label.chk { display:inline-flex; gap:6px; align-items:center; cursor:pointer; font-size:13px; }
  main { padding:20px 24px; display:flex; flex-direction:column; gap:18px; max-width:1320px; }
  .kpis { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); }
  .kpi { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
  .kpi .k { color:var(--dim); font-size:12px; } .kpi .v { font-size:21px; font-weight:600; }
  .cf-grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }
  .cf { background:#15140f; border:1px solid #3a3320; border-radius:12px; padding:14px; }
  .cf .nm { font-weight:600; } .cf .row { display:flex; justify-content:space-between; font-size:13px; margin-top:4px; }
  h2.sec { font-size:14px; margin:4px 0 2px; font-weight:600; }
  .bar { height:7px; border-radius:4px; background:#11141a; overflow:hidden; margin-top:6px; }
  .bar > i { display:block; height:100%; background:var(--bar); }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:500; position:sticky; top:0; background:var(--panel); cursor:default; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  td.cmd { color:var(--dim); max-width:520px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wrap { max-height:360px; overflow:auto; border:1px solid var(--line); border-radius:10px; }
  .badge { font-size:11px; padding:1px 6px; border-radius:5px; border:1px solid var(--line); }
  .badge.lo { color:var(--good); border-color:#2c3a23; } .badge.any { color:var(--warn); border-color:#3a3320; }
  .hb { font-size:11px; padding:1px 6px; border-radius:5px; border:1px solid var(--line); white-space:nowrap; }
  .hb.ok { color:var(--good); border-color:#2c3a23; } .hb.bad { color:var(--bad); border-color:#4a2730; }
  .hb.warn { color:var(--warn); border-color:#3a3320; } .hb.dim { color:var(--dim); }
  .count { color:var(--dim); font-size:12px; margin-left:8px; font-weight:400; }
  .catgrid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); }
  .catcard { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
  .catcard h3 { font-size:13.5px; margin:0 0 8px; font-weight:600; }
  .catcard.other { opacity:.85; }
  td.act, th.act { text-align:right; white-space:nowrap; }
  .kill { font-size:11px; padding:2px 9px; border-radius:6px; border:1px solid #4a2730; background:#1b1113; color:#f7768e; cursor:pointer; }
  .kill:hover { border-color:var(--bad); background:#251317; }
  .kill.force { border-color:var(--bad); background:#2a1418; color:#ff9db0; font-weight:600; }
  .modebtn { font-size:12px; padding:4px 12px; border-radius:8px; border:1px solid var(--line); background:var(--panel); color:var(--dim); cursor:pointer; }
  .modebtn.on { color:var(--good); border-color:#2c3a23; background:#151d12; }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1>进程 / 端口监控 <span>只读 · 本机 (终止需开控制模式)</span></h1>
  <div class="nav">
    __NAV__
  </div>
</header>
<main>
  <div class="controls">
    <input type="text" id="filter" placeholder="过滤: 进程名 / 端口 / 命令行 / 远端地址…" style="min-width:280px">
    <span class="muted">进程排序</span>
    <select id="sort"><option value="rss">按内存</option><option value="cpu">按 CPU</option></select>
    <label class="chk"><input type="checkbox" id="loonly"> 仅 localhost 端口</label>
    <label class="chk"><input type="checkbox" id="auto"> 自动刷新 5s</label>
    <button id="probe" title="对本机可经 127.0.0.1/::1 触达的监听端口做一次探活 (主动连接·HTTP 层只读幂等·只碰本机·不外发)">探测本地服务</button>
    <button id="refresh">刷新</button>
  </div>
  <div id="app"><div class="muted">加载中…</div></div>
</main>
<footer id="foot"></footer>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtBytes(n){ n=+n||0; if(n>=1e9)return (n/1073741824).toFixed(2)+'GB'; if(n>=1e6)return (n/1048576).toFixed(0)+'MB'; if(n>=1e3)return (n/1024).toFixed(0)+'KB'; return n+'B'; }
function fmtUp(s){ s=+s||0; const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  if(d>0)return d+'d '+h+'h'; if(h>0)return h+'h '+m+'m'; if(m>0)return m+'m'; return s+'s'; }
function matches(txt,q){ return !q || String(txt).toLowerCase().includes(q); }
function healthBadge(h){
  if(!h) return '';
  // 收到任何 HTTP 状态 = 服务存活并在响应; 2xx/3xx 绿, 4xx/5xx 黄(响应但报错, 含不支持 HEAD), 仅连不上才红。
  if(h.state==='http'){ const cls=h.status<400?'ok':'warn'; return `<span class="hb ${cls}" title="HTTP HEAD 有响应=存活">HTTP ${h.status} · ${h.ms}ms</span>`; }
  if(h.state==='open') return `<span class="hb dim" title="TCP 可连, 但未响应 HTTP (可能是非 HTTP 服务)">开放·非HTTP · ${h.ms}ms</span>`;
  return `<span class="hb bad" title="无法连接">无响应</span>`;
}
function remoteCard(rows,q){
  let rr=(rows||[]).filter(r=>matches(r.raddr_ip,q)||r.procs.some(p=>matches(p,q)));
  if(!rr.length) return '';
  rr=rr.slice(0,40);
  const body=rr.map(r=>`<tr>
    <td>${esc(r.raddr_ip)}</td><td class="num">${r.count}</td>
    <td>${esc(r.procs.slice(0,4).join(', '))}${r.procs.length>4?' <span class="muted">+'+(r.procs.length-4)+'</span>':''}</td>
    <td class="cmd" title="${esc(r.rports.join(', '))}">${esc(r.rports.join(', '))}</td></tr>`).join('');
  return `<div><h2 class="sec">连接按远端聚合 <span class="count">${rr.length} 个远端 · 本地聚合, 无反向 DNS</span></h2>
    <div class="wrap"><table><thead><tr><th>远端 IP</th><th class="num">连接数</th><th>进程</th><th>远端端口</th></tr></thead>
    <tbody>${body}</tbody></table></div></div>`;
}

let DATA=null, HEALTH={}, reqId=0;
async function load(){
  const my=++reqId;
  try{
    const r=await fetch('/api/processes'); const d=await r.json();
    if(my!==reqId) return;
    if(d.error){ $('#app').innerHTML='<div class="card warn">进程监控不可用: '+esc(d.error)+
      '<br><span class="muted">该页需要 psutil。安装后刷新即可: <code>pip install psutil</code> (Token 看板不受影响)。</span></div>'; $('#foot').textContent=''; return; }
    DATA=d; render();
  }catch(e){ if(my===reqId) $('#foot').textContent='请求失败: '+e; }
}

function render(){
  if(!DATA) return;
  const d=DATA, q=$('#filter').value.trim().toLowerCase(), loonly=$('#loonly').checked, sortKey=$('#sort').value;
  const h=d.host;
  const kpis=[['CPU', (h.cpu_percent||0)+'%'],['内存', (h.mem_percent||0)+'%  ·  '+fmtBytes(h.mem_used)+'/'+fmtBytes(h.mem_total)],
    ['进程数', h.proc_count],['监听端口', d.listening.length],['活动连接', d.conn_total]];
  const kpiHtml=kpis.map(k=>`<div class="kpi"><div class="k">${k[0]}</div><div class="v">${esc(k[1])}</div></div>`).join('');

  // cloudflared
  let cf='';
  if(d.cloudflared.length){
    cf=`<h2 class="sec">🛰️ cloudflared 隧道进程 <span class="count">本机运行中</span></h2><div class="cf-grid">`+
      d.cloudflared.map(c=>`<div class="cf">
        <div class="nm">${esc(c.name)} <span class="muted">pid ${c.pid}</span></div>
        <div class="row"><span class="muted">CPU / 内存</span><span>${c.cpu}% · ${fmtBytes(c.rss)}</span></div>
        <div class="row"><span class="muted">运行时长</span><span>${fmtUp(c.uptime)}</span></div>
        <div class="row"><span class="muted">本机监听口</span><span>${c.listen_ports.length?esc(c.listen_ports.join(', ')):'—'}</span></div>
        <div class="row muted" style="font-size:11px;margin-top:8px">隧道走 QUIC, 边缘链路在 socket 层不可见 (只显示本机可见信息)</div>
      </div>`).join('')+`</div>`;
  } else {
    cf=`<h2 class="sec">🛰️ cloudflared</h2><div class="card muted">未发现本机运行的 cloudflared 进程。</div>`;
  }

  // listening
  let lis=d.listening.filter(l=>(!loonly||l.loopback) && (matches(l.port,q)||matches(l.name,q)||matches(l.cmd,q)||matches(l.addr,q)));
  const lisRows=lis.map(l=>`<tr>
      <td class="num">${l.port}</td>
      <td>${esc(l.addr)} ${l.loopback?'<span class="badge lo">loopback</span>':'<span class="badge any">all-if</span>'}</td>
      <td>${healthBadge((HEALTH[l.port] && (HEALTH[l.port].pid==null || HEALTH[l.port].pid===l.pid)) ? HEALTH[l.port] : null)}</td>
      <td>${esc(l.name)} <span class="muted">${l.pid==null?'':'· '+l.pid}</span> ${(l.category&&l.category!=='other')?'<span class="badge lo">'+esc(l.category)+'</span>':''}</td>
      <td class="cmd" title="${esc(l.cmd)}">${esc(l.cmd)}</td>
      <td class="act">${(CTRL_MODE&&l.terminable)?`<button class="kill" data-port="${l.port}" onclick="freePort(this)" title="终止占用此端口的进程">释放</button>`:(l.pid&&!l.terminable?'<span class="muted" title="仅 VS Code/Cloudflare/Railway 可终止">🔒</span>':'')}</td></tr>`).join('');
  const lisCard=`<div><h2 class="sec">监听端口 → 占用进程 <span class="count">${lis.length}/${d.listening.length}</span></h2>
    <div class="wrap"><table><thead><tr><th class="num">端口</th><th>地址</th><th>健康</th><th>进程</th><th>命令行 (已脱敏)</th><th class="act"></th></tr></thead>
    <tbody>${lisRows||'<tr><td colspan=6 class="muted">无匹配</td></tr>'}</tbody></table></div></div>`;

  // 分类进程: VS Code / Cloudflare / Railway 详列(可终止), 其余汇总为"其他"(只读)
  function catCard(key,label,icon){
    const c=(d.categories&&d.categories[key])||{procs:[],count:0,rss:0,listen_ports:[]};
    const procs=(c.procs||[]).filter(p=>matches(p.name,q)||matches(p.pid,q)||matches(p.cmd,q))
          .sort((a,b)=>(b[sortKey]||0)-(a[sortKey]||0));
    const rows=procs.map(p=>`<tr id="pr${p.pid}">
        <td title="${esc(p.cmd)}">${esc(p.name)}${p.name==='claude.exe'?' <span class="badge lo">会话</span>':''}</td>
        <td class="num">${p.pid}</td><td class="num">${p.cpu||0}%</td><td class="num">${fmtBytes(p.rss)}</td>
        <td class="num">${(p.listen_ports&&p.listen_ports.length)?esc(p.listen_ports.join(',')):'—'}</td>
        <td class="act">${CTRL_MODE?`<button class="kill" data-pid="${p.pid}" data-ct="${p.create_time}" data-name="${esc(p.name)}" onclick="killProc(this)">终止</button>`:''}</td></tr>`).join('');
    return `<div class="catcard"><h3>${icon} ${esc(label)} <span class="count">${c.count} 进程 · ${fmtBytes(c.rss)}${(c.listen_ports&&c.listen_ports.length)?' · 端口 '+esc(c.listen_ports.slice(0,12).join(',')):''}</span></h3>
      ${c.count?`<div class="wrap"><table><thead><tr><th>进程</th><th class="num">PID</th><th class="num">CPU</th><th class="num">内存</th><th class="num">监听</th><th class="act"></th></tr></thead><tbody>${rows||'<tr><td colspan=6 class="muted">无匹配</td></tr>'}</tbody></table></div>`:'<div class="muted">未运行</div>'}</div>`;
  }
  const other=(d.categories&&d.categories.other)||{count:0,rss:0};
  const otherRow=`<div class="catcard other"><h3>▦ 其他 <span class="count">${other.count} 个进程 · 总内存 ${fmtBytes(other.rss)} · 只读, 不可终止</span></h3></div>`;
  const modeBtn=CTRL_MODE
    ? `<button class="modebtn on" onclick="toggleMode()" title="这也是 M4 的远程审批模式: 同时会把 Claude Code 的 permission 请求路由到网页(每条最多阻塞 25s 再回退本地)。用完记得关。">● 控制模式开 · 点击关闭</button>`
    : `<button class="modebtn" onclick="toggleMode()" title="开启后才出现终止按钮 (需本机控制令牌; 服务端 kill 时仍二次校验类别+身份)。注意: 这也是远程审批模式, 开着会把 permission 请求路由到网页并阻塞至多 25s。">○ 只读 · 开控制模式启用终止</button>`;
  const catsCard=`<div><h2 class="sec">分类进程 <span class="count">只这三类可终止 · 其余汇总为"其他" · 终止 = 先 terminate 不掉再强制</span> &nbsp;${modeBtn}</h2>
    <div class="catgrid">${catCard('vscode','VS Code','🟦')}${catCard('cloudflare','Cloudflare','🟧')}${catCard('railway','Railway','🚝')}${otherRow}</div></div>`;

  // connections
  let cn=d.connections.filter(c=>matches(c.name,q)||matches(c.raddr,q)||matches(c.laddr,q)||matches(c.status,q));
  const cnRows=cn.map(c=>`<tr>
      <td>${esc(c.proto||'')}</td><td>${esc(c.laddr)}</td><td>${esc(c.raddr)}</td>
      <td>${esc(c.status)}</td><td>${esc(c.name)} <span class="muted">${c.pid==null?'':'· '+c.pid}</span></td></tr>`).join('');
  const cnCard=`<div><h2 class="sec">活动网络连接 <span class="count">显示 ${cn.length}/${d.connections.length}${d.conn_total>d.connections.length?` (共 ${d.conn_total}, 已截断)`:''} <span class="muted">· 仅 ESTABLISHED/UDP</span></span></h2>

    <div class="wrap"><table><thead><tr><th>协议</th><th>本地</th><th>远端</th><th>状态</th><th>进程</th></tr></thead>
    <tbody>${cnRows||'<tr><td colspan=5 class="muted">无匹配</td></tr>'}</tbody></table></div></div>`;

  $('#app').innerHTML=`<div class="kpis">${kpiHtml}</div>${catsCard}${cf}${lisCard}${remoteCard(d.by_remote,q)}${cnCard}`;
  const ts=new Date(d.generated_at_epoch*1000).toLocaleTimeString();
  $('#foot').innerHTML=`采样于 ${ts} · ${h.proc_count} 进程 / ${d.listening.length} 监听口 / ${d.conn_total} 活动连接 · 观测只读不外发, 命令行已脱敏 · 终止仅限三类, 服务端二次校验类别+身份`;
}

async function probeHealth(){
  const btn=$('#probe'), old=btn.textContent;
  btn.disabled=true; btn.textContent='探测中…';
  try{
    const r=await fetch('/api/health'); const d=await r.json();
    if(d.error){ $('#foot').textContent='探测失败: '+d.error; return; }
    HEALTH={}; (d.results||[]).forEach(x=>HEALTH[x.port]=x);
    render();
    const res=d.results||[], n=res.length;
    const http=res.filter(x=>x.state==='http').length, open=res.filter(x=>x.state==='open').length, down=res.filter(x=>x.state==='down').length;
    $('#foot').innerHTML=`已探测 ${n} 个本机端口: ${http} 个 HTTP 响应 · ${open} 个开放(非HTTP) · ${down} 个无响应 · 主动探活, 只碰本机不外发 · `+$('#foot').innerHTML;
  }catch(e){ $('#foot').textContent='探测失败: '+e; }
  finally{ btn.disabled=false; btn.textContent=old; }
}

// --- 控制面 (P7): 终止进程/释放端口。默认只读; 开控制模式 + 令牌 + 服务端二次校验 + 审计 ---
// 令牌不再经 HTTP 下发 (评审: 防本机他进程/网页窃取): 首次操作时粘贴一次(见服务器控制台/~/.tokmon/control_token), 存浏览器。
let CTRL_TOKEN=localStorage.getItem('mc_ctl_token')||'', CTRL_MODE=false;
function ctrlHeaders(){ return {'Content-Type':'application/json','X-Control-Token':CTRL_TOKEN}; }
function ensureToken(){
  if(!CTRL_TOKEN){ const t=prompt('粘贴控制令牌以启用控制操作\n(见服务器控制台启动输出, 或 ~/.tokmon/control_token):'); if(t&&t.trim()){ CTRL_TOKEN=t.trim(); localStorage.setItem('mc_ctl_token',CTRL_TOKEN); } }
  return !!CTRL_TOKEN;
}
async function ctlPost(path, body){
  if(!ensureToken()) return null;
  const res=await fetch(path,{method:'POST',headers:ctrlHeaders(),body:JSON.stringify(body)});
  if(res.status===403){ localStorage.removeItem('mc_ctl_token'); CTRL_TOKEN=''; alert('控制令牌无效, 请重新粘贴'); return null; }
  return res.json();
}
async function loadCtrl(){
  try{ const c=await (await fetch('/api/control')).json(); CTRL_MODE=!!c.remote_mode; }catch(e){}
}
async function toggleMode(){
  const r=await ctlPost('/api/control/mode',{on:!CTRL_MODE});
  if(r) CTRL_MODE=!!r.remote_mode;
  render();
}
async function killProc(btn){
  const pid=+btn.dataset.pid, ct=+btn.dataset.ct, name=btn.dataset.name, action=btn.dataset.action||'terminate';
  let msg=(action==='kill'?'强制结束':'终止')+' '+name+' (pid '+pid+')?\n\nWindows 上 terminate 即强制结束 (无优雅 SIGTERM)。';
  if(name==='claude.exe') msg+='\n⚠ 这是一个 Claude Code 会话进程, 终止会中断该会话。';
  if(!confirm(msg)) return;
  const cell=document.querySelector('#pr'+pid+' .act'); if(cell) cell.innerHTML='<span class="muted">处理中…</span>';
  try{
    const r=await ctlPost('/api/control/terminate',{pid,create_time:ct,action});
    if(r) showKill(pid,ct,name,r); else if(cell) cell.innerHTML='';
  }catch(e){ if(cell) cell.innerHTML='<span class="hb bad">请求失败</span>'; }
}
function showKill(pid,ct,name,r){
  const cell=document.querySelector('#pr'+pid+' .act'); if(!cell) return;
  if(!r.ok){ const m={'control-mode-off':'控制模式未开','not-terminable':'不可终止'+(r.category?'('+r.category+')':''),'pid-reused':'进程已变·请刷新','gone':'已不存在','bad-pid':'PID 无效','no-psutil':'缺 psutil','identity-required':'请刷新','refuse-self':'不能终止监控自身'}[r.reason]||r.reason;
    cell.innerHTML='<span class="hb bad" title="'+esc(m)+'">'+esc(m)+'</span>'; return; }
  const o=r.outcome;
  if(o==='terminated'||o==='killed'||o==='gone'){ cell.innerHTML='<span class="hb ok">已结束</span>'; setTimeout(load,900); }
  else if(o==='still-alive'){ cell.innerHTML='<button class="kill force" data-pid="'+pid+'" data-ct="'+ct+'" data-name="'+esc(name)+'" data-action="kill" onclick="killProc(this)" title="terminate 没生效, 强制结束">强制结束</button>'; }
  else if(o==='access-denied'){ cell.innerHTML='<span class="hb bad" title="需管理员权限或受保护进程">需管理员</span>'; }
  else { cell.innerHTML='<span class="hb warn">'+esc(o)+'</span>'; }
}
async function freePort(btn){
  const port=+btn.dataset.port;
  if(!confirm('释放端口 '+port+'? 将终止占用它的进程 (Windows 上为强制结束)。')) return;
  btn.disabled=true; const old=btn.textContent; btn.textContent='…';
  try{ const r=await ctlPost('/api/control/free-port',{port});
    if(!r){ btn.disabled=false; btn.textContent=old; return; }
    if(!r.ok){ btn.disabled=false; btn.textContent=old; alert('未能释放: '+(r.reason||'?')); return; }
    setTimeout(load,900);
  }catch(e){ btn.disabled=false; btn.textContent=old; }
}

let timer=null;
function setAuto(){ if(timer){clearInterval(timer);timer=null;} if($('#auto').checked) timer=setInterval(load,5000); }
['filter','sort','loonly'].forEach(id=>$('#'+id).addEventListener('input', render));
$('#sort').addEventListener('change', render);
$('#loonly').addEventListener('change', render);
$('#auto').addEventListener('change', setAuto);
$('#probe').addEventListener('click', probeHealth);
$('#refresh').addEventListener('click', load);
loadCtrl().then(load);
</script>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- 对话 / Session 状态页 (Mission Control M1: 先看见, 不通知) ----
SESS_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session 状态 · Mission Control</title>
<style>__BASE__
  .controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  input[type=text], button { background:var(--panel); color:var(--fg); border:1px solid var(--line);
      border-radius:8px; padding:6px 10px; font-size:13px; }
  button { cursor:pointer; } button:hover { border-color:var(--accent); }
  label.chk { display:inline-flex; gap:6px; align-items:center; cursor:pointer; font-size:13px; }
  main { padding:18px 24px; display:flex; flex-direction:column; gap:14px; max-width:1100px; }
  .counts { display:flex; gap:10px; flex-wrap:wrap; }
  .pill { background:var(--panel); border:1px solid var(--line); border-radius:999px; padding:5px 12px; font-size:13px; }
  .pill b { font-weight:600; } .pill.live b { color:var(--good); } .pill.amb b { color:var(--warn); } .pill.unk b { color:var(--dim); }
  .pill.clk { cursor:pointer; } .pill.clk:hover { border-color:var(--accent); }
  .pill.on { border-color:var(--accent); background:#14212e; box-shadow:inset 0 0 0 1px var(--accent); }
  select { background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:6px 8px; font-size:13px; }
  .chips { display:flex; gap:7px; flex-wrap:wrap; }
  .chip { background:var(--panel); border:1px solid var(--line); border-radius:999px; padding:4px 11px; font-size:12.5px; cursor:pointer; white-space:nowrap; }
  .chip:hover { border-color:var(--accent); } .chip b { color:var(--dim); font-weight:600; margin-left:3px; }
  .chip.on { border-color:var(--accent); background:#14212e; box-shadow:inset 0 0 0 1px var(--accent); }
  .chip.zero { opacity:.5; } .chip.zero.on { opacity:.85; }
  .fbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; background:#14212e; border:1px solid var(--accent);
      border-radius:10px; padding:8px 12px; font-size:13px; }
  .fbar .warn { color:var(--warn); } .fbar .muted { color:var(--dim); }
  .clearbtn { font-size:12px; padding:3px 10px; border-radius:7px; cursor:pointer; }
  .sess { display:flex; flex-direction:column; gap:7px; }
  .row { background:var(--panel); border:1px solid var(--line); border-left-width:3px; border-radius:11px; padding:12px 14px; }
  .row.live { border-left-color:var(--good); } .row.amb { border-left-color:var(--warn); }
  .row.wait { border-left-color:#3a4256; } .row.unk { border-left-color:#33384a; } .row.closed { border-left-color:#33384a; opacity:.72; }
  .r1 { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .title { font-weight:600; font-size:14px; } .proj { font-weight:600; } .meta { color:var(--dim); font-size:12px; }
  .meta2 { color:var(--dim); font-size:12px; margin-top:2px; }
  .step { color:var(--fg); opacity:.82; font-size:12.5px; margin-top:5px; line-height:1.5; }
  .step .tool { color:var(--good); font-weight:600; }
  .age { margin-left:auto; color:var(--dim); font-size:12px; white-space:nowrap; }
  .sb { font-size:12px; padding:2px 9px; border-radius:7px; border:1px solid var(--line); white-space:nowrap; }
  .sb.live { color:var(--good); border-color:#2c3a23; background:#151d12; }
  .sb.wait { color:var(--dim); }
  .sb.amb  { color:var(--warn); border-color:#3a3320; background:#1d1a12; }
  .sb.unk  { color:var(--dim); border-color:#33384a; }
  .sb.closed { color:var(--dim); border-color:#33384a; }
  .idle { color:var(--dim); font-size:11px; border:1px solid var(--line); border-radius:6px; padding:1px 6px; }
  .rkb { color:var(--warn); font-size:11px; border:1px solid #4a3d22; border-radius:6px; padding:1px 6px; text-decoration:none;
         max-width:320px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .rkb:hover { border-color:var(--warn); }
  .wfl { font-size:11.5px; color:var(--accent); text-decoration:none; white-space:nowrap; }
  .wfl:hover { text-decoration:underline; }
  .detail { color:var(--dim); font-size:12.5px; margin-top:2px; }
  .snippet { color:var(--fg); opacity:.8; font-style:italic; }
  .events { display:flex; gap:6px; flex-wrap:wrap; margin-top:7px; }
  .ev { font-size:11px; color:var(--dim); border:1px solid var(--line); border-radius:5px; padding:1px 6px; }
  .ev.err { color:var(--warn); border-color:#3a3320; }
  h2.sec { font-size:14px; margin:6px 0 6px; font-weight:600; }
  .tl { display:flex; flex-direction:column; max-height:320px; overflow:auto; border:1px solid var(--line); border-radius:11px; background:var(--panel); }
  .evrow { display:grid; grid-template-columns:62px 96px 1fr; gap:10px; align-items:baseline; font-size:12.5px; padding:5px 12px; border-bottom:1px solid #20242e; }
  .evrow:last-child { border-bottom:0; }
  .evrow .evt { font-weight:600; }
  .evrow.info .evt { color:var(--dim); } .evrow.warning .evt { color:var(--warn); } .evrow.critical .evt { color:var(--bad); }
  .evtime { color:var(--dim); } .evdet { color:var(--dim); } .evproj { opacity:.85; }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1>Session 状态 <span>Mission Control · M1 先看见, 不通知</span></h1>
  <div class="nav">
    __NAV__
  </div>
</header>
<main>
  <div class="controls">
    <input type="text" id="filter" placeholder="过滤: 标题 / 项目 / 步骤 / 工具…" style="min-width:260px">
    <!-- 默认**不勾**: 实测 71 个「等你」里 70 个都 >10min, 默认勾上等于把这一页唯一的存在理由藏掉 71 分之 70
         (SESSIONS_V3_PLAN §0/§186 审计)。原则 1「真实优先」+ P6「误报零容忍」同样适用于 UI 的默认值:
         **默认静默**说的是通知, 不是看板。未来若想改回 checked, 先回答"用户打开这一页是为了看什么"。 -->
    <label class="chk"><input type="checkbox" id="hideidle" title="「等你已久」= Claude 已经回复你、超过 10min 没人接话 —— 是你欠它一句话, 不是它闲着"> 隐藏「等你已久」</label>
    <span class="muted" style="font-size:12px">活跃时间窗</span>
    <select id="agewin"><option value="">全部</option><option value="3600">近 1h</option><option value="86400">近 24h</option><option value="604800">近 7d</option></select>
    <label class="chk"><input type="checkbox" id="auto" checked> 自动刷新 30s</label>
    <button id="refresh">刷新</button>
  </div>
  <div id="projchips" class="chips"></div>
  <div id="app"><div class="muted">加载中…</div></div>
  <div>
    <h2 class="sec">事件流 <span class="meta" style="font-weight:400">M2 · 本机事件总线, 零通知 (后台每 5s 推进, 不依赖你开没开页面)</span></h2>
    <div class="tl" id="timeline"><div class="muted" style="padding:10px">暂无事件 (新事件会随会话状态变化出现)</div></div>
  </div>
</main>
<footer id="foot"></footer>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtAgo(s){ if(s==null)return '—'; s=+s; if(s<60)return s+'s 前'; if(s<3600)return Math.floor(s/60)+'m 前'; if(s<86400)return Math.floor(s/3600)+'h 前'; return Math.floor(s/86400)+'d 前'; }
function fmtClock(epoch){ if(!epoch) return '—'; try { return new Date(epoch*1000).toLocaleTimeString(); } catch(e){ return '—'; } }
const EVT_LABEL = {SESSION_STARTED:'会话开始', TASK_COMPLETED:'任务完成', TOOL_ERROR:'工具出错',
  SESSION_IDLE:'空闲', SESSION_STUCK:'久未返回', TOKEN_BUDGET_WARNING:'预算告警', PROCESS_CRASHED:'进程崩溃', COMMAND_ISSUED:'已下指令',
  DESTRUCTIVE_OP:'破坏性操作', ERROR_SPIKE:'连续失败', REPEATED_FILE_EDIT:'改了又改没通过', LARGE_DIFF:'大改动'};
let EVENTS=[], evCursor=0, evDropped=0;
function evDetail(e){   // 返回纯文本; 转义由唯一的外层 esc(evDetail(e)) 负责, 这里不再 esc 以免双重转义
  const p=e.payload||{};
  if(e.type==='SESSION_IDLE') return '空闲已 '+(p.idle_step||1)+' 级 ('+(Math.round((p.age_s||0)/60))+'m)';
  if(e.type==='SESSION_STUCK') return p.state_label||'久未返回';
  if(e.type==='TOOL_ERROR') return '工具 '+(p.tool_name||'?')+' 报错';
  if(e.type==='TASK_COMPLETED') return p.state_from?('从 '+p.state_from+' 完成一轮'):'完成一轮';
  if(e.type==='DESTRUCTIVE_OP') return (p.kind||'?')+'（规则判断，只标不拦）';
  if(e.type==='ERROR_SPIKE') return '连着失败 '+(p.count||'?')+' 次';
  if(e.type==='REPEATED_FILE_EDIT') return '同一个文件来回 '+(p.count||'?')+' 轮都没通过';
  if(e.type==='LARGE_DIFF') return p.rule==='large-task'?('一个任务动了 '+p.count+' 个文件'):('单次大改动 '+(p.count||'?')+' 次');
  return '';
}
function renderTimeline(){
  const tl=$('#timeline'); if(!tl) return;
  if(!EVENTS.length){ tl.innerHTML='<div class="muted" style="padding:10px">暂无事件 (新事件会随会话状态变化出现)</div>'; return; }
  const rows=EVENTS.slice(-200).reverse().map(e=>`<div class="evrow ${esc(e.severity)}">
    <span class="evtime">${fmtClock(e.timestamp)}</span>
    <span class="evt">${esc(EVT_LABEL[e.type]||e.type)}</span>
    <span><span class="evproj">${esc(e.project||e.session||'')}</span> <span class="evdet">${esc(evDetail(e))}</span></span>
  </div>`).join('');
  tl.innerHTML=rows + (evDropped>0?`<div class="muted" style="padding:6px 12px">（事件 ring 已轮转 ${evDropped} 条更早历史，可能含已读）</div>`:'');
}
async function loadEvents(){
  try{
    const r=await fetch('/api/events?since='+evCursor); const d=await r.json();
    if(d.error) return;
    if(d.events && d.events.length){
      const have=new Set(EVENTS.map(e=>e.seq));        // 按 seq 去重, 防并发拉取重复 append
      for(const e of d.events) if(!have.has(e.seq)) EVENTS.push(e);
      if(EVENTS.length>400) EVENTS=EVENTS.slice(-400);
    }
    if(typeof d.last_seq==='number' && d.last_seq>evCursor) evCursor=d.last_seq;
    evDropped=d.dropped||0;
    renderTimeline();
  }catch(e){}
}
function matches(t,q){ return !q || String(t==null?'':t).toLowerCase().includes(q); }
const CLS = {WORKING:'live',PROCESSING:'live',AWAITING_USER:'wait',AMBIGUOUS_PENDING:'amb',CLOSED:'closed',UNKNOWN:'unk'};

// ---- 过滤器: FILTERS 是唯一真相源, DOM 只是它的投影 (§1)。持久化到 localStorage, 坏数据回退默认。----
const FKEY='mc_sess_filters';
const STGRP_LABEL={live:'活跃',wait:'等你',amb:'久未返回',closed:'已关闭',unk:'读不出'};
const AGE_LABEL={'3600':'近 1h','86400':'近 24h','604800':'近 7d'};
let FILTERS={projects:[],states:[],ageMax:null};
try{ const s=JSON.parse(localStorage.getItem(FKEY)||'{}');
  if(Array.isArray(s.projects)) FILTERS.projects=[...new Set(s.projects.filter(x=>typeof x==='string'))];
  if(Array.isArray(s.states)) FILTERS.states=[...new Set(s.states.filter(x=>STGRP_LABEL[x]))];
  if(typeof s.ageMax==='number' && AGE_LABEL[String(s.ageMax)]) FILTERS.ageMax=s.ageMax;   // 只认三个合法窗, 防坏数据让 select 与实际筛选脱节
}catch(e){}
function saveFilters(){ try{ localStorage.setItem(FKEY, JSON.stringify(FILTERS)); }catch(e){} }
function groupOf(state){ return CLS[state]||'unk'; }
// 纯函数: (会话数组, 条件) -> 数组。不碰 DOM, 不改入参 (§2/§5)。AND 逐级收窄。
function applyFilters(sessions, F){
  return sessions.filter(s=>{
    if(F.projects.length && !F.projects.includes(s.project||'')) return false;   // 与 chip 的 key 一致 (§3.2 无项目也可筛)
    if(F.states.length && !F.states.includes(groupOf(s.state))) return false;
    if(F.ageMax!=null && !(s.last_activity_age_s!=null && s.last_activity_age_s<=F.ageMax)) return false;  // null 年龄有时间窗时排除 (§3.3 会明示)
    if(F.hideIdle && s.state==='AWAITING_USER' && s.idle) return false;
    const q=F.text;
    return matches(s.project,q)||matches(s.title,q)||matches(s.current_step,q)||matches(s.state,q)||matches(s.state_label,q)||matches(s.pending_tool_name,q)||matches(s.git_branch,q);
  });
}
function toggleState(g){ const i=FILTERS.states.indexOf(g); if(i<0) FILTERS.states.push(g); else FILTERS.states.splice(i,1); saveFilters(); render(); }
function toggleProject(p){ const i=FILTERS.projects.indexOf(p); if(i<0) FILTERS.projects.push(p); else FILTERS.projects.splice(i,1); saveFilters(); render(); }
function clearFilters(){ FILTERS.projects=[]; FILTERS.states=[]; FILTERS.ageMax=null; saveFilters(); $('#filter').value=''; $('#agewin').value=''; render(); }
const EVLABEL = {task_completed:'✓ 完成', tool_returned:'↩ 返回', tool_returned_error:'✗ 工具出错',
  continued_after_subagent:'↺ 子代理返回', interrupted_by_user:'⎋ 你打断'};
function evChip(e){
  let t = e.kind==='tool_started' ? ('▶ '+esc(e.name||'工具')) : (EVLABEL[e.kind]||esc(e.kind));
  return `<span class="ev ${e.kind==='tool_returned_error'?'err':''}">${t}</span>`;
}

let DATA=null, reqId=0;
async function load(){
  const my=++reqId;
  try{
    const r=await fetch('/api/sessions'); const d=await r.json();
    if(my!==reqId) return;
    if(d.error){ $('#app').innerHTML='<div class="row unk warn">读取失败: '+esc(d.error)+'</div>'; $('#foot').textContent=''; return; }
    DATA=d; render();
  }catch(e){ if(my===reqId) $('#foot').textContent='请求失败: '+e; }
}

function render(){
  if(!DATA) return;
  const d=DATA, q=$('#filter').value.trim().toLowerCase(), hideIdle=$('#hideidle').checked;
  const c=d.counts;
  // 5 个状态组 pill 可点即筛; 后台在跑/空闲/共 只读展示 (定: 严按 plan §2)
  function spill(label,count,group,cls){
    const on=FILTERS.states.includes(group);
    return `<span class="pill ${cls||''} clk ${on?'on':''}" data-st="${group}" title="点一下按此状态筛选">${label} <b>${count}</b></span>`;
  }
  const counts=`<div class="counts">
    ${spill('活跃', c.working, 'live', 'live')}
    ${c.background?`<span class="pill live">后台在跑 <b>${c.background}</b></span>`:''}
    ${spill('久未返回', c.ambiguous, 'amb', 'amb')}
    ${spill('等你', c.awaiting, 'wait', '')}
    <span class="pill" title="「等你」的子集: Claude 已回复你且 >10min 无人接话 (不是并列状态)">其中等你已久 <b>${c.idle}</b></span>
    ${spill('已关闭', c.closed, 'closed', 'unk')}
    ${spill('读不出', c.unknown, 'unk', 'unk')}
    <span class="pill">共 <b>${c.total}</b></span></div>`;

  // 项目 chips: distinct project + 计数, 降序; 含"选中但本帧无会话"的项目 (§3.2, 不静默消失)
  const projCounts=Object.create(null);   // 无原型链: 项目名若正好叫 toString/constructor 等也不串味
  d.sessions.forEach(s=>{ const p=s.project||''; projCounts[p]=(projCounts[p]||0)+1; });   // key = 归一后的项目名 (与 applyFilters 一致)
  FILTERS.projects.forEach(p=>{ if(!(p in projCounts)) projCounts[p]=0; });
  const projList=Object.keys(projCounts).sort((a,b)=>(projCounts[b]-projCounts[a])||a.localeCompare(b));
  $('#projchips').innerHTML = (projList.length>1 || FILTERS.projects.length) ? projList.map(p=>{
    const on=FILTERS.projects.includes(p), zero=projCounts[p]===0;   // 选中态从 FILTERS 恢复, 不靠 DOM (§3.1)
    return `<span class="chip ${on?'on':''} ${zero?'zero':''}" data-proj="${esc(p)}" title="${zero?'本帧无会话':'点一下按项目筛选'}">${esc(p||'(无项目)')} <b>${projCounts[p]}</b></span>`;
  }).join('') : '';

  const F={projects:FILTERS.projects, states:FILTERS.states, ageMax:FILTERS.ageMax, text:q, hideIdle};
  let rows = applyFilters(d.sessions, F);

  // §3.3 只要有 filter 生效就醒目提示 + 一键清除; §3.2 空项目 note; null 年龄被时间窗排除 note
  const bits=[];
  if(FILTERS.projects.length) bits.push('项目='+FILTERS.projects.map(p=>esc(p||'(无项目)')).join(', '));
  if(FILTERS.states.length) bits.push('状态='+FILTERS.states.map(g=>STGRP_LABEL[g]).join(', '));
  if(FILTERS.ageMax!=null) bits.push(AGE_LABEL[String(FILTERS.ageMax)]||('近 '+FILTERS.ageMax+'s'));
  let nullExcl=0;
  if(FILTERS.ageMax!=null){ const Fn=Object.assign({}, F, {ageMax:null}); nullExcl=applyFilters(d.sessions, Fn).filter(s=>s.last_activity_age_s==null).length; }
  const emptyProj=FILTERS.projects.filter(p=>(projCounts[p]||0)===0);
  let fbar='';
  if(bits.length){
    let extra='';
    if(emptyProj.length) extra+=` · <span class="warn">项目 ${emptyProj.map(p=>esc(p||'(无项目)')).join(', ')} 本帧无会话</span>`;
    if(nullExcl) extra+=` · <span class="muted">另有 ${nullExcl} 个会话时间读不出, 已被时间窗排除</span>`;
    fbar=`<div class="fbar"><b>过滤中</b>：${bits.join(' · ')} <span class="muted">显示 ${rows.length}/${d.sessions.length}</span>${extra} <button id="clearf" class="clearbtn">清除全部过滤</button></div>`;
  }
  const body = rows.map(s=>{
    const cls=CLS[s.state]||'unk';
    const idleBadge = (s.state==='AWAITING_USER'&&s.idle) ? '<span class="idle" title="Claude 已回复你, 超过 10min 没人接话 —— 你欠它一句话">等你已久</span>' : '';
    const wf = s.workflow, live = s.state==='WORKING'||s.state==='PROCESSING';
    const wfHref = wf ? '/workflow?task='+encodeURIComponent(wf.task) : '';
    const riskBadge = (wf && wf.risks.length) ? `<a class="rkb" href="${wfHref}" title="${esc('风险（规则判断，推断；只标不拦）\n'+wf.risks.map(r=>'· '+r.label).join('\n')+'\n点击看当前任务的回放')}">⚠ ${esc(wf.risks[0].label)}${wf.risks.length>1?' 等 '+wf.risks.length+' 条':''}</a>` : '';
    const wfLink = wf ? `<a class="wfl" href="${wfHref}" title="${live?'打开当前任务的实时回放（每 5 秒自动刷新）':'打开这个会话最后一个任务的回放'}">${live?'实时回放 →':'回放 →'}</a>` : '';
    const titleText = s.title || s.project;                 // 标题为主, 无标题回退项目名
    const metaBits = [];
    if(s.title) metaBits.push(esc(s.project)+(s.subpath?' /'+esc(s.subpath):''));   // 有标题时项目名降为辅
    if(s.git_branch) metaBits.push(esc(s.git_branch));
    if(s.model) metaBits.push(esc(s.model));
    // 当前步骤: 工具动作 + 叙述 (思考原文不可得, 用可见叙述替代)
    let step='';
    if(s.tool_pending && s.pending_tool_name) step += `<span class="tool">▶ ${esc(s.pending_tool_name)}</span> `;
    const narr = s.current_step || (s.state==='AWAITING_USER'&&!s.synthetic ? s.last_text : null);
    if(narr){ const mark = s.step_kind==='thinking' ? '💭 ' : ''; step += `<span class="snippet">${mark}${esc(narr)}</span>`; }
    const evs = (s.recent_events||[]).slice(-6).map(evChip).join('');
    return `<div class="row ${cls}">
      <div class="r1">
        <span class="title" title="${esc(s.title||'')}">${esc(titleText)}</span>
        <span class="sb ${cls}" title="${esc(s.state_label)}">${esc(s.state_label)}</span>
        ${idleBadge}${riskBadge}${wfLink}
        <span class="age">${fmtAgo(s.last_activity_age_s)}</span>
      </div>
      ${metaBits.length?`<div class="meta2">${metaBits.join(' · ')}</div>`:''}
      ${step?`<div class="step">${step}</div>`:''}
      ${evs?`<div class="events">${evs}</div>`:''}
    </div>`;
  }).join('');
  // 无 filter 生效时才用旧的低调 note (被隐藏"等你已久"/文本不匹配); 有 filter 时上面的 fbar 已醒目说清
  const shownNote = (!bits.length && rows.length<d.sessions.length) ? `<span class="meta">（显示 ${rows.length}/${d.sessions.length}，已隐藏「等你已久」/不匹配）</span>` : '';
  $('#app').innerHTML = fbar + counts + `<div class="sess">${body||'<div class="muted">无匹配会话</div>'}</div>` + (shownNote?`<div style="margin-top:6px">${shownNote}</div>`:'');

  const ts=new Date(d.generated_at_epoch*1000).toLocaleTimeString();
  const th=d.thresholds;
  $('#foot').innerHTML=`采样于 ${ts} · 状态据每个 session 最后一条<b>消息</b>的时间戳(非文件 mtime) · `
    +`「等你已久」阈值 ${Math.round(th.idle_after_s/60)}min · 久未返回阈值 ${Math.round(th.stuck_after_s/60)}min · `
    +`<span class="warn">久未返回=无法从 transcript 区分「长任务/等授权/会话已关闭」</span> · 纯本地只读, 不通知不外发`;
}

let timer=null;
function tickAll(){ load(); loadEvents(); }
function setAuto(){ if(timer){clearInterval(timer);timer=null;} if($('#auto').checked) timer=setInterval(tickAll,30000); }
['filter','hideidle'].forEach(id=>$('#'+id).addEventListener('input', render));
$('#hideidle').addEventListener('change', render);
$('#auto').addEventListener('change', setAuto);
$('#refresh').addEventListener('click', tickAll);
// 事件委托挂在稳定容器上 (pill/chip 每帧重建, 委托不受影响; 选中态从 FILTERS 恢复 §3.1)
$('#app').addEventListener('click', e=>{
  const pl=e.target.closest('.pill.clk'); if(pl){ toggleState(pl.dataset.st); return; }
  if(e.target.closest('#clearf')){ clearFilters(); }
});
$('#projchips').addEventListener('click', e=>{ const ch=e.target.closest('.chip'); if(ch) toggleProject(ch.dataset.proj); });
$('#agewin').addEventListener('change', ()=>{ FILTERS.ageMax = $('#agewin').value ? +$('#agewin').value : null; saveFilters(); render(); });
$('#agewin').value = FILTERS.ageMax!=null ? String(FILTERS.ageMax) : '';   // 从持久化恢复时间窗到 select
setAuto(); tickAll();
</script>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- 通知 / 规则页 (Mission Control 右栏: M3 通知层) ----
NOTIFY_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>通知 / 规则 · Mission Control</title>
<style>__BASE__
  main { padding:18px 24px; display:flex; flex-direction:column; gap:14px; max-width:1100px; }
  .controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  button { background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:6px 12px; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  .pills { display:flex; gap:10px; flex-wrap:wrap; }
  .pill { background:var(--panel); border:1px solid var(--line); border-radius:999px; padding:5px 12px; font-size:13px; }
  .pill b { font-weight:600; } .pill.on b { color:var(--good); } .pill.off b { color:var(--dim); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .setup { font-size:13px; line-height:1.7; } .setup code { background:#11141a; padding:1px 6px; border-radius:5px; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:5px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--dim); font-weight:500; }
  td.sum { white-space:normal; color:var(--dim); }
  .wrap { max-height:420px; overflow:auto; border:1px solid var(--line); border-radius:10px; }
  .tag { font-size:11px; padding:1px 7px; border-radius:6px; border:1px solid var(--line); }
  .tag.push { color:var(--good); border-color:#2c3a23; } .tag.supp { color:var(--dim); }
  .sev.warning { color:var(--warn); } .sev.critical { color:var(--bad); } .sev.info { color:var(--dim); }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1>通知 / 规则 <span>Mission Control · M3 · 出站推送 (有用且不烦)</span></h1>
  <div class="nav">
    __NAV__
  </div>
</header>
<main>
  <div class="controls">
    <button id="test">发送测试通知</button>
    <label class="nav" style="margin:0"><input type="checkbox" id="auto" checked> 自动刷新 5s</label>
    <button id="refresh">刷新</button>
    <span id="testresult" class="muted"></span>
  </div>
  <div class="pills" id="status"></div>
  <div id="setup"></div>
  <div>
    <h2 style="font-size:14px;margin:4px 0 6px">通知 feed <span class="muted" style="font-weight:400;font-size:12px">推送与被抑制的都记下来, 方便你调"不烦"的参数</span></h2>
    <div class="wrap"><table id="feed"><thead><tr><th>时间</th><th>严重度</th><th>事件</th><th>项目</th><th>结果</th><th>详情</th></tr></thead><tbody id="feedbody"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody></table></div>
  </div>
</main>
<footer id="foot"></footer>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtClock(ts){ try { return new Date(ts*1000).toLocaleTimeString(); } catch(e){ return '—'; } }
const REASON = {'below-threshold':'低于阈值','quiet-hours':'静默时段','debounce':'去抖(刚推过同类)','rate-limit':'限流','type-disabled':'该类型已关','push':'推送'};
let reqId=0;
async function load(){
  const my=++reqId;
  try{
    const r=await fetch('/api/notifications'); const d=await r.json();
    if(my!==reqId) return;
    if(d.error){ $('#status').innerHTML='<span class="pill off">通知层未启动</span>'; return; }
    const tg=d.telegram_configured, po=d.pushover_configured, on=tg||po;
    const qh=d.quiet_hours?('每天 '+d.quiet_hours[0]+':00–'+d.quiet_hours[1]+':00 只放行 critical'):'未设';
    $('#status').innerHTML=[
      ['出站', d.egress, on?'on':'off'],
      ['Telegram', tg?'已配置':'未配置', tg?'on':'off'],
      ['Pushover', po?'已配置':'未配置', po?'on':'off'],
      ['推送阈值', d.push_min_severity, ''],
      ['静默时段', qh, ''],
      ['去抖', d.debounce_s+'s/同类', ''],
      ['限流', d.rate_max+' 条/'+Math.round(d.rate_window_s/60)+'min', ''],
      ['已推送', d.sent, 'on'], ['已抑制', d.suppressed, ''], ['错误', d.errors, d.errors?'off':''],
    ].map(x=>`<span class="pill ${x[2]}">${x[0]} <b>${esc(x[1])}</b></span>`).join('');
    $('#setup').innerHTML = on ? '' :
      `<div class="card setup">📵 <b>当前默认仅本地, 不外发任何字节。</b>
       两个通道任选其一即可让手机真的震 (也可两个都配, 互为冗余):<br><br>
       <b>A · Pushover (最快, 只出)</b><br>
       1) pushover.net 注册 → 首页拿 <code>User Key</code>; 2) Create an Application → 拿 <code>API Token</code>;<br>
       3) 设 <code>MC_PUSHOVER_USER</code> 和 <code>MC_PUSHOVER_TOKEN</code> 后重启本服务 → 点上方「发送测试通知」。<br>
       <span class="muted">critical 会以 high priority 发 (突破手机端免打扰); 永不用 emergency 优先级 —— 不做尖叫的闹钟。</span><br><br>
       <b>B · Telegram (未来双向的基础)</b><br>
       1) 找 <code>@BotFather</code> 建 bot 拿 token; 2) 给 bot 发一句话, 用
       <code>https://api.telegram.org/bot&lt;token&gt;/getUpdates</code> 拿 chat id;<br>
       3) 设 <code>MC_TELEGRAM_TOKEN</code> / <code>MC_TELEGRAM_CHAT_ID</code> (或写进 <code>~/.tokmon/notify.json</code>) 后重启。<br>
       <span class="muted">两者都是纯出站, 不开任何入站端口、不暴露看板。内容已最小化: 无命令行/路径/密钥。</span></div>`;
    const rows=(d.feed||[]).slice().reverse().map(f=>{
      const res = f.decision==='push'
        ? `<span class="tag push">推送</span> <span class="muted">${esc(f.delivery||'…')}</span>`
        : `<span class="tag supp">抑制</span> <span class="muted">${esc(REASON[f.reason]||f.reason)}</span>`;
      return `<tr><td>${fmtClock(f.ts)}</td><td class="sev ${esc(f.severity)}">${esc(f.severity)}</td>
        <td>${esc(f.type)}</td><td>${esc(f.project||f.session||'')}</td><td>${res}</td><td class="sum">${esc(f.summary)}</td></tr>`;
    }).join('');
    $('#feedbody').innerHTML = rows || '<tr><td colspan="6" class="muted">暂无通知候选事件 (warning 及以上才进这里)</td></tr>';
    $('#foot').innerHTML = '通知层是事件总线的消费者; 严重度/静默/去抖/限流都在这一层 · 默认不外发, 配 token 才出站 · 内容已最小化(无命令行/路径/密钥)';
  }catch(e){ if(my===reqId) $('#foot').textContent='请求失败: '+e; }
}
async function sendTest(){
  const b=$('#test'); b.disabled=true; $('#testresult').textContent='发送中…';
  try{ const r=await fetch('/api/notify-test'); const d=await r.json();
    $('#testresult').innerHTML='<span class="'+(d.ok?'':'warn')+'">'+esc(d.detail||'')+'</span>'; }
  catch(e){ $('#testresult').textContent='失败: '+e; }
  finally{ b.disabled=false; load(); }
}
let timer=null;
function setAuto(){ if(timer){clearInterval(timer);timer=null;} if($('#auto').checked) timer=setInterval(load,5000); }
$('#auto').addEventListener('change', setAuto);
$('#refresh').addEventListener('click', load);
$('#test').addEventListener('click', sendTest);
setAuto(); load();
</script>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- 远程审批 / 控制页 (M4 v0 控制层) ----
CONTROL_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>远程审批 / 控制 · Mission Control</title>
<style>__BASE__
  main { padding:18px 24px; display:flex; flex-direction:column; gap:16px; max-width:1000px; }
  button { background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:7px 14px; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.ok { color:var(--good); border-color:#2c3a23; } button.no { color:var(--bad); border-color:#4a2730; }
  .mode { display:flex; align-items:center; gap:14px; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .mode .big { font-size:15px; font-weight:600; }
  .switch { font-size:13px; padding:6px 14px; border-radius:999px; border:1px solid var(--line); cursor:pointer; }
  .switch.on { color:var(--good); border-color:#2c3a23; background:#151d12; } .switch.off { color:var(--dim); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  h2.sec { font-size:14px; margin:2px 0 8px; font-weight:600; }
  .pend { border:1px solid var(--warn); border-radius:11px; padding:12px 14px; margin-bottom:10px; background:#1d1a12; }
  .pend .tool { font-weight:600; } .pend .cmd { color:var(--fg); opacity:.85; font-family:ui-monospace,Consolas,monospace; font-size:12px; margin:6px 0; word-break:break-all; }
  .pend .row { display:flex; gap:10px; align-items:center; margin-top:8px; }
  .meta { color:var(--dim); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:4px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--dim); font-weight:500; }
  pre { background:#11141a; border:1px solid var(--line); border-radius:8px; padding:10px; overflow:auto; font-size:12px; }
  .setup { font-size:13px; line-height:1.7; } .setup code { background:#11141a; padding:1px 6px; border-radius:5px; }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1>远程审批 / 控制 <span>Mission Control · M4 · 你显式下达, 全程审计, 失败回退本地</span></h1>
  <div class="nav">
    __NAV__
  </div>
</header>
<main>
  <div class="mode">
    <span class="big">远程审批模式</span>
    <span id="modesw" class="switch off">关 (本地弹窗照常)</span>
    <span class="meta">开启后, Claude Code 的 permission 会转到这里/手机等你点; 关闭则一律走正常本地弹窗。</span>
  </div>
  <div>
    <h2 class="sec">待答问题 <span class="meta" id="askmeta">Claude 问你的多选题 → 点一下, 会话带着你的选择继续</span></h2>
    <div id="asks"><div class="meta">无待答问题 (被平台驱动的会话提问时, 这里会冒出按钮)</div></div>
  </div>
  <div>
    <h2 class="sec">待审批 <span class="meta" id="pendmeta"></span></h2>
    <div id="pending"><div class="meta">无待审批 (装好 hook + 开远程模式后, 这里会冒出来)</div></div>
  </div>
  <div>
    <h2 class="sec">审计日志 <span class="meta">每个远程决定都记在这里, 并发一条 COMMAND_ISSUED 事件</span></h2>
    <div class="card"><table id="audit"><thead><tr><th>时间</th><th>动作</th><th>会话</th><th>工具</th><th>结果</th></tr></thead><tbody id="auditbody"><tr><td colspan="5" class="meta">暂无</td></tr></tbody></table></div>
  </div>
  <div>
    <h2 class="sec">安装 hook (一次性, 你手动加)</h2>
    <div class="card setup">
      把下面这段合并进 <code>~/.claude/settings.json</code>, 然后<b>重启你的 Claude Code 会话</b>生效。它只拦截 permission 弹窗,
      调本机 <code>127.0.0.1</code> (token 走 <code>X-Control-Token</code> 头), 不开任何入站端口。服务没开 / 超时 / token 不符 -> Claude Code 自动回退正常本地弹窗。
      <pre id="snippet">加载中…</pre>
      <div id="hooknote" class="meta" style="margin:-4px 0 8px"></div>
      <button id="copy">复制</button> <span id="copied" class="meta"></span>
    </div>
  </div>
</main>
<footer id="foot"></footer>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtClock(ts){ try { return new Date(ts*1000).toLocaleTimeString(); } catch(e){ return '—'; } }
let reqId=0, snippetText='', CTRL_TOKEN=localStorage.getItem('mc_ctl_token')||'';
function ensureToken(){
  if(!CTRL_TOKEN){ const t=prompt('粘贴控制令牌 (见服务器控制台启动输出, 或 ~/.tokmon/control_token):'); if(t&&t.trim()){ CTRL_TOKEN=t.trim(); localStorage.setItem('mc_ctl_token',CTRL_TOKEN); } }
  return !!CTRL_TOKEN;
}
async function load(){
  const my=++reqId;
  try{
    const r=await fetch('/api/control'); const d=await r.json();
    if(my!==reqId) return;
    const on=!!d.remote_mode;
    const sw=$('#modesw'); sw.className='switch '+(on?'on':'off'); sw.textContent=on?'开 (审批转到这里/手机)':'关 (本地弹窗照常)';
    const pend=d.pending||[];
    $('#pendmeta').textContent = pend.length?('共 '+pend.length+' 条等你'):'';
    $('#pending').innerHTML = pend.length ? pend.map(p=>`<div class="pend">
        <div><span class="tool">${esc(p.tool)}</span> <span class="meta">· ${esc(p.project||p.session||'')} · ${p.age_s}s 前</span></div>
        <div class="cmd">${esc(p.summary)}</div>
        <div class="row"><button class="ok" onclick="decide('${esc(p.id)}','allow')">允许</button>
          <button class="no" onclick="decide('${esc(p.id)}','deny')">拒绝</button>
          <span class="meta">不点的话, hook 会在超时后自动回退到本地弹窗</span></div>
      </div>`).join('') : '<div class="meta">无待审批 (装好 hook + 开远程模式后, 这里会冒出来)</div>';
    const au=d.audit||[];
    $('#auditbody').innerHTML = au.length ? au.slice().reverse().map(a=>`<tr><td>${fmtClock(a.ts)}</td><td>${esc(a.action)}</td>
        <td>${esc(a.session||'')}</td><td>${esc(a.tool||'')}</td><td>${esc(a.outcome)}</td></tr>`).join('') : '<tr><td colspan="5" class="meta">暂无</td></tr>';
    $('#foot').innerHTML='控制层: 只做 permission 审批 · 你显式点击才决定 · 全程审计 · token 鉴权 · 任何失败一律回退正常本地弹窗 (绝不替你自动批/拒)';
  }catch(e){ if(my===reqId) $('#foot').textContent='请求失败: '+e; }
}
function ctrlHeaders(){ return {'Content-Type':'application/json','X-Control-Token':CTRL_TOKEN}; }
async function ctlPost(path, body){
  if(!ensureToken()) return null;
  const res=await fetch(path,{method:'POST',headers:ctrlHeaders(),body:JSON.stringify(body)});
  if(res.status===403){ localStorage.removeItem('mc_ctl_token'); CTRL_TOKEN=''; alert('控制令牌无效, 请重新粘贴'); return null; }
  return res.json();
}
async function loadHook(){   // hook 片段现需令牌才可取 (含 token, 只给已持令牌者)
  if(!ensureToken()){ $('#snippet').textContent='(粘贴控制令牌后显示 hook 配置)'; return; }
  try{ const res=await fetch('/api/control/hook-config',{headers:ctrlHeaders()});
    if(res.status===403){ localStorage.removeItem('mc_ctl_token'); CTRL_TOKEN=''; $('#snippet').textContent='令牌无效'; return; }
    const d=await res.json(); snippetText=JSON.stringify(d.snippet,null,2); $('#snippet').textContent=snippetText;
    if($('#hooknote')) $('#hooknote').textContent=d.note||''; }
  catch(e){ $('#snippet').textContent='读取失败'; }
}
async function setMode(on){ await ctlPost('/api/control/mode',{on}); load(); }
async function decide(id,decision){ await ctlPost('/api/control/decide',{id,decision}); load(); }

// --- S1-v2 待答问题: Claude 的多选题 -> 按钮 -> 选齐后提交, 会话带着你的选择继续 ---
// 两段式 (先选后交): 多问题必须**答全**才提交 —— 半份答案绝不塞回会话 (P7: 答案只能来自你的点击);
// multiSelect 也才有得多选。SEL[askId][qi] = label (单选) 或 [labels] (多选)。
let ASKS=[], SEL={};
async function loadAsks(){
  if(!CTRL_TOKEN){ $('#asks').innerHTML='<div class="meta">(粘贴控制令牌后显示待答问题)</div>'; return; }
  try{
    const res=await fetch('/api/control/asks',{headers:ctrlHeaders()});
    if(res.status===403){ localStorage.removeItem('mc_ctl_token'); CTRL_TOKEN=''; return; }
    const d=await res.json(); ASKS=d.asks||[];
  }catch(e){ $('#asks').innerHTML='<div class="meta warn">读取待答问题失败 (服务不可达)</div>'; return; }
  const live={}; ASKS.forEach(a=>{ if(SEL[a.id]) live[a.id]=SEL[a.id]; }); SEL=live;   // 丢掉已消失 ask 的暂存选择
  $('#askmeta').textContent = ASKS.length ? ('共 '+ASKS.length+' 条等你作答 · 不作答会超时, 届时明确拒绝, 绝不替你选')
                                          : 'Claude 问你的多选题 → 选齐后提交, 会话带着你的选择继续';
  if(!ASKS.length){ $('#asks').innerHTML='<div class="meta">无待答问题 (被平台驱动的会话提问时, 这里会冒出按钮)</div>'; return; }
  $('#asks').innerHTML = ASKS.map(a=>{
    const sel=SEL[a.id]||{};
    const qs=(a.questions||[]);
    const body=qs.map((q,qi)=>{
      const cur=sel[qi];
      const isOn=l => q.multiSelect ? (Array.isArray(cur)&&cur.indexOf(l)>=0) : (cur===l);
      return `<div class="cmd">${esc(q.question||'')}${q.multiSelect?' <span class="meta">(可多选)</span>':''}</div>
        <div class="row">${(q.options||[]).map(o=>
          `<button class="${isOn(o.label)?'ok':''}" data-ask="${esc(a.id)}" data-qi="${qi}" data-label="${esc(o.label||'')}"
             title="${esc(o.description||'')}" onclick="pickOpt(this)">${isOn(o.label)?'✓ ':''}${esc(o.label||'')}</button>`).join(' ')}</div>`;
    }).join('');
    const ready=qs.every((q,qi)=>{ const c=sel[qi]; return q.multiSelect ? (Array.isArray(c)&&c.length) : !!c; });
    return `<div class="pend">
      <div><span class="tool">${esc(a.project||'?')}</span> <span class="meta">· 会话 ${esc((a.session_id||'').slice(0,8))} · ${a.age_s}s 前</span></div>
      ${body}
      <div class="row"><button class="ok" data-ask="${esc(a.id)}" ${ready?'':'disabled'} onclick="submitAsk(this)">提交${ready?'':' (先选齐所有问题)'}</button>
        <span class="meta">不作答会超时 → 明确拒绝, 绝不替你选</span></div>
    </div>`;
  }).join('');
}
function pickOpt(btn){                       // 只改暂存选择, 不发请求
  const askId=btn.dataset.ask, qi=+btn.dataset.qi, label=btn.dataset.label;
  const ask=ASKS.find(a=>String(a.id)===String(askId)); if(!ask) return;
  const q=(ask.questions||[])[qi]; if(!q) return;
  const sel=SEL[askId]||(SEL[askId]={});
  if(q.multiSelect){
    const cur=Array.isArray(sel[qi])?sel[qi].slice():[];
    const i=cur.indexOf(label); if(i>=0) cur.splice(i,1); else cur.push(label);
    sel[qi]=cur;
  } else { sel[qi] = (sel[qi]===label ? undefined : label); }
  loadAsks();                                 // 重绘选中态 (选择存在 SEL, 不存 DOM)
}
async function submitAsk(btn){
  const askId=btn.dataset.ask;
  const ask=ASKS.find(a=>String(a.id)===String(askId)); if(!ask) return;
  const sel=SEL[askId]||{}, answers={};
  (ask.questions||[]).forEach((q,qi)=>{ const c=sel[qi]; if(c && (!Array.isArray(c)||c.length)) answers[q.question]=c; });
  btn.disabled=true; btn.textContent='提交中…';               // 防重复提交
  const r=await ctlPost('/api/control/answer',{ask_id:askId, answers});
  if(!r||!r.ok){ btn.disabled=false; btn.textContent='提交'; alert('提交失败: '+((r&&r.reason)||'?')); loadAsks(); return; }
  if(r.pending && r.pending.length){ btn.disabled=false; btn.textContent='提交'; alert('还有问题没答: '+r.pending.join(' / ')); loadAsks(); return; }
  delete SEL[askId]; loadAsks(); load();
}

$('#modesw').addEventListener('click',()=>setMode($('#modesw').classList.contains('off')));
$('#copy').addEventListener('click',()=>{ navigator.clipboard && navigator.clipboard.writeText(snippetText); $('#copied').textContent='已复制'; setTimeout(()=>$('#copied').textContent='',1500); });
$('#snippet').addEventListener('click', loadHook);
load(); loadAsks(); setInterval(load, 3000); setInterval(loadAsks, 3000);
</script>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- 体检页 (M4.5: 成本契约 + 推断契约) ----
DOCTOR_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>体检 · Mission Control</title>
<style>__BASE__
  main { padding:18px 24px; display:flex; flex-direction:column; gap:16px; max-width:1000px; }
  button { background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:7px 14px; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  .sec { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .sec h2 { font-size:14px; margin:0 0 8px; font-weight:600; display:flex; align-items:center; gap:10px; }
  .badge { font-size:12px; padding:2px 9px; border-radius:7px; border:1px solid var(--line); }
  .badge.ok { color:var(--good); border-color:#2c3a23; background:#151d12; }
  .badge.warn { color:var(--warn); border-color:#3a3320; background:#1d1a12; }
  pre { margin:0; white-space:pre-wrap; word-break:break-word; font:12.5px/1.55 ui-monospace,Consolas,monospace; color:var(--fg); }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1>体检 / doctor <span>Mission Control · M4.5 · 对真相校验, 让格式漂移可被发现</span></h1>
  <div class="nav">
    __NAV__
  </div>
</header>
<main>
  <div><button id="run">重新体检</button> <span id="msg" class="muted" style="font-size:12px"></span></div>
  <div id="out"><div class="muted">运行中… (全量扫描, 约 1-2 秒)</div></div>
</main>
<footer>体检按需全量扫描, 不自动刷新 · 升级 Claude Code 后跑一次最划算 · 纯本地只读</footer>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function section(title, d){
  const warns=d.warns||0;
  const badge = warns===0 ? '<span class="badge ok">✓ 健康</span>' : ('<span class="badge warn">⚠ '+warns+' 项需注意</span>');
  return `<div class="sec"><h2>${esc(title)} ${badge}</h2><pre>${esc((d.lines||[]).join('\n'))}</pre></div>`;
}
async function run(){
  $('#msg').textContent='扫描中…'; $('#run').disabled=true;
  try{
    const d=await (await fetch('/api/doctor')).json();
    $('#out').innerHTML = section('推断契约 (activity / events 的格式假设)', d.inference||{})
                        + section('成本契约 (解析 / 去重 / 定价)', d.cost||{});
    const tw=(d.inference?.warns||0)+(d.cost?.warns||0);
    $('#msg').innerHTML = tw===0 ? '<span style="color:var(--good)">全部健康</span>' : ('共 '+tw+' 项需注意');
  }catch(e){ $('#out').innerHTML='<div class="sec">体检失败: '+esc(e)+'</div>'; }
  finally{ $('#run').disabled=false; }
}
$('#run').addEventListener('click', run);
run();
</script>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- 准确率回测页 (L2: 三把尺并排, 你分别评测) ----
BACKTEST_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>准确率回测 · Mission Control</title>
<style>__BASE__
  main { padding:18px 24px; display:flex; flex-direction:column; gap:16px; max-width:1200px; }
  button, select { background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:7px 12px; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  .grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .card h2 { font-size:14px; margin:0 0 4px; font-weight:600; }
  .card .desc { color:var(--dim); font-size:12px; margin-bottom:10px; }
  .acc { font-size:30px; font-weight:700; }
  .kv { display:flex; justify-content:space-between; font-size:12.5px; padding:2px 0; border-bottom:1px solid #20242e; }
  .kv b { font-weight:600; } .danger b { color:var(--warn); } .danger0 b { color:var(--good); }
  pre { margin:8px 0 0; white-space:pre-wrap; word-break:break-word; font:11.5px/1.5 ui-monospace,Consolas,monospace; color:var(--dim); }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1>准确率回测 / L2 <span>用 transcript 的未来当真值 · 三把尺并排, 你分别评测</span></h1>
  <div class="nav">
    __NAV__
  </div>
</header>
<main>
  <div><button id="run">重新回测</button>
    窗口 <select id="days"><option>3</option><option selected>7</option><option>14</option><option>30</option></select> 天
    <span id="msg" class="muted" style="font-size:12px;margin-left:8px"></span></div>
  <div id="out"><div class="muted">运行中… (全量窗口扫描, 约 1-3 秒)</div></div>
</main>
<footer>挑尺标准: 准确率高 + "不可判"诚实(不掺进分母) + 危险格(过度乐观/假完成)少 · 三把尺数字接近=分类器稳健, 不挑食 · 纯本地只读</footer>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function accColor(a){ return a>=90?'var(--good)':(a>=75?'var(--warn)':'#f7768e'); }
function card(o){
  if(o.error) return `<div class="card"><h2>${esc(o.name)}</h2><div class="warn">出错: ${esc(o.error)}</div></div>`;
  const dz=(o.over+o.false_done)===0;
  return `<div class="card">
    <h2>${esc(o.name)} 尺</h2><div class="desc">${esc(o.desc)}</div>
    <div class="acc" style="color:${accColor(o.acc)}">${o.acc}%</div>
    <div class="kv"><span>可判样本 (分母)</span><b>${o.judgeable}</b></div>
    <div class="kv"><span>不可判 (诚实剔除)</span><b>${o.unjudgeable}</b></div>
    <div class="kv"><span>覆盖率</span><b>${o.cov}%</b></div>
    <div class="kv ${dz?'danger0':'danger'}"><span>危险格 过度乐观</span><b>${o.over}</b></div>
    <div class="kv ${dz?'danger0':'danger'}"><span>危险格 假完成</span><b>${o.false_done}</b></div>
    <pre>${esc((o.lines||[]).slice(3).join('\n'))}</pre>
  </div>`;
}
async function run(){
  $('#msg').textContent='扫描中…'; $('#run').disabled=true;
  try{
    const d=await (await fetch('/api/backtest?days='+$('#days').value)).json();
    $('#out').innerHTML='<div class="grid">'+(d.oracles||[]).map(card).join('')+'</div>';
    $('#msg').textContent='近 '+d.days+' 天';
  }catch(e){ $('#out').innerHTML='<div class="card warn">回测失败: '+esc(e)+'</div>'; }
  finally{ $('#run').disabled=false; }
}
$('#run').addEventListener('click', run);
$('#days').addEventListener('change', run);
run();
</script>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- 厂商 API 账单页 (第 4 支柱: 真实账单, 非估算) ----
BILLING_PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>厂商账单 · Mission Control</title>
<style>__BASE__
  main { padding:18px 24px; display:flex; flex-direction:column; gap:16px; max-width:1150px; }
  button { background:var(--panel); color:var(--fg); border:1px solid var(--line); border-radius:8px; padding:7px 14px; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  input { background:#0e0e11; color:var(--fg); border:1px solid var(--line); border-radius:7px; padding:7px 10px; font-size:13px; }
  .sec { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .sec h2 { font-size:14px; margin:0 0 10px; font-weight:600; display:flex; align-items:center; gap:10px; }
  .badge { font-size:12px; padding:2px 9px; border-radius:7px; border:1px solid var(--line); }
  .badge.ok { color:var(--good); border-color:#2c3a23; background:#151d12; }
  .badge.warn { color:var(--warn); border-color:#3a3320; background:#1d1a12; }
  .badge.off { color:var(--dim); }
  .note { background:#131318; border:1px solid var(--line); border-left:3px solid var(--warn); border-radius:8px; padding:11px 14px; color:var(--dim); font-size:12.5px; line-height:1.6; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:right; padding:6px 9px; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--dim); font-weight:500; }
  .na { color:var(--dim); }
  .cost { color:var(--good); }
  .reason { color:var(--dim); font-size:12.5px; line-height:1.6; margin-top:4px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  footer { padding:14px 24px; color:var(--dim); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1>厂商账单 / billing <span>OpenAI · Anthropic 的真实 API 平台开销 (官方 usage/cost API)</span></h1>
  <div class="nav">
    __NAV__
  </div>
</header>
<main>
  <div class="note">
    <b>这一页和 /tokens 是两个不同的计费池, 数字本就不该相等。</b><br>
    · <b>本页</b> = 你的 <b>API 平台</b>开销 (app 调 API 的钱) —— 厂商官方账单, <b>真实</b>。<br>
    · <b>/tokens</b> = Claude Code 的 token 用量 <b>等价估算</b> —— 且你的 Claude Code 走<b>订阅</b>计费, 其用量<b>根本不进 API 账单</b>。<br>
    · 所以两边对不上是<b>正确的</b>, 不是 bug。(见 PROVIDER_BILLING_PLAN.md §0.3)
  </div>

  <div class="sec">
    <h2>密钥 <span id="egress" class="badge off"></span></h2>
    <div class="row">
      <input id="anth" type="password" placeholder="Anthropic admin key (sk-ant-admin01-…)" style="min-width:320px">
      <input id="oai" type="password" placeholder="OpenAI admin key (sk-admin-…)" style="min-width:320px">
      <button id="save">保存</button>
      <span id="msg" class="muted" style="font-size:12px"></span>
    </div>
    <div class="reason">
      存到 <code>~/.tokmon/providers.json</code> (0600) 或用环境变量 <code>ANTHROPIC_ADMIN_KEY</code>/<code>OPENAI_ADMIN_KEY</code>。
      <b>密钥永不回显</b>, 只报「已配置/未配置」。未配置 → <b>一个字节都不出本机</b>。<br>
      ⚠️ 这些是 <b>org-admin 级</b>凭据 (Anthropic 无只读档: 同一把 key 也能踢组织成员、停用别人的 API key)。妥善保管。
    </div>
  </div>

  <div id="out"><div class="muted">加载中…</div></div>
</main>
<footer>数据每 5 分钟刷新 · 费用只有日粒度 (两家 API 都只给 1d) · 拿不到就显式标「不可得」, 绝不瞎估</footer>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const NAMES = {anthropic:'Anthropic', openai:'OpenAI', google:'Google (Gemini)'};
const n = v => v==null ? '<span class="na">—</span>' : Number(v).toLocaleString();
const usd = v => v==null ? '<span class="na">—</span>' : '<span class="cost">$'+Number(v).toFixed(2)+'</span>';

function card(p){
  const title = esc(NAMES[p.name]||p.name);
  if(!p.available){
    const b = p.name==='google' ? '<span class="badge warn">不可得</span>'
            : (p.configured ? '<span class="badge warn">暂不可得</span>' : '<span class="badge off">未配置</span>');
    return `<div class="sec"><h2>${title} ${b}</h2><div class="reason">${esc(p.reason||'')}</div></div>`;
  }
  const days = (p.days||[]).map(d=>`<tr><td>${esc(d.date)}</td><td>${usd(d.cost_usd)}</td>
      <td>${n(d.input)}</td><td>${n(d.cached_input)}</td><td>${n(d.output)}</td><td>${n(d.requests)}</td></tr>`).join('');
  const models = (p.by_model||[]).map(m=>`<tr><td>${esc(m.model)}</td><td>${usd(m.cost_usd)}</td>
      <td>${n(m.input)}</td><td>${n(m.cached_input)}</td><td>${n(m.output)}</td><td>${n(m.requests)}</td></tr>`).join('');
  const note = p.name==='anthropic'
    ? '<div class="reason">注: Anthropic 的用量 API <b>不提供请求数</b>, 费用也<b>没有 per-model 分组</b> —— 故显示「—」而非 0。</div>' : '';
  return `<div class="sec"><h2>${title} <span class="badge ok">可用</span>
      <span class="muted" style="font-size:12px;font-weight:400">近 ${'${DAYS}'} 天合计 ${usd(p.total_cost_usd).replace(/<[^>]+>/g,'')}</span></h2>
    <table><thead><tr><th>日期</th><th>费用</th><th>输入</th><th>缓存读</th><th>输出</th><th>请求数</th></tr></thead>
      <tbody>${days||'<tr><td colspan=6 class="na">窗口内无用量</td></tr>'}</tbody></table>
    <h2 style="margin-top:14px">按模型</h2>
    <table><thead><tr><th>模型</th><th>费用</th><th>输入</th><th>缓存读</th><th>输出</th><th>请求数</th></tr></thead>
      <tbody>${models||'<tr><td colspan=6 class="na">无</td></tr>'}</tbody></table>
    ${note}</div>`;
}

async function load(){
  try{
    const d = await (await fetch('/api/billing')).json();
    $('#egress').textContent = d.egress || '';
    $('#out').innerHTML = (d.providers||[]).map(p=>card(p).split('${DAYS}').join(d.window_days)).join('');
  }catch(e){ $('#out').innerHTML = '<div class="sec">加载失败: '+esc(e)+'</div>'; }
}

function token(){
  let t = localStorage.getItem('mc_ctl_token');
  if(!t){ t = prompt('粘贴控制令牌 (启动 tokmon serve 时控制台打印的那个):') || ''; if(t) localStorage.setItem('mc_ctl_token', t); }
  return t;
}
$('#save').addEventListener('click', async ()=>{
  const t = token(); if(!t){ $('#msg').textContent='需要控制令牌'; return; }
  const body = {};
  if($('#anth').value) body.anthropic = $('#anth').value;
  if($('#oai').value) body.openai = $('#oai').value;
  if(!Object.keys(body).length){ $('#msg').textContent='没填任何 key'; return; }
  $('#msg').textContent='保存中…';
  try{
    const r = await fetch('/api/billing/keys', {method:'POST',
      headers:{'Content-Type':'application/json','X-Control-Token':t}, body:JSON.stringify(body)});
    if(r.status===403){ $('#msg').textContent='令牌无效 (403)'; localStorage.removeItem('mc_ctl_token'); return; }
    const d = await r.json();
    $('#anth').value=''; $('#oai').value='';                 // 立刻清掉输入框, 不留在 DOM
    $('#msg').textContent = d.ok ? '已保存 (拉取中…)' : '保存失败';
    setTimeout(load, 1500);
  }catch(e){ $('#msg').textContent='保存失败: '+e; }
});
load();
setInterval(load, 60000);
</script>
</body></html>
""".replace("__BASE__", _BASE_CSS)


# ---- /workflow 页面 (独立文件, 不再往本文件里内联 —— RECAP 侧批 #6) ----
def _load_page(name: str) -> str:
    nav = _nav_html("/workflow")
    try:
        src = (Path(__file__).parent / "pages" / name).read_text(encoding="utf-8")
    except OSError:
        return f"<h1>页面文件缺失: tokmon/pages/{name}</h1>"
    return src.replace("__BASE__", _BASE_CSS).replace("__NAV__", nav)


WORKFLOW_PAGE = _load_page("workflow.html")
PROC_PAGE = PROC_PAGE.replace("__NAV__", _nav_html("/processes"))
SESS_PAGE = SESS_PAGE.replace("__NAV__", _nav_html("/sessions"))
NOTIFY_PAGE = NOTIFY_PAGE.replace("__NAV__", _nav_html("/notify"))
CONTROL_PAGE = CONTROL_PAGE.replace("__NAV__", _nav_html("/control"))
DOCTOR_PAGE = DOCTOR_PAGE.replace("__NAV__", _nav_html("/doctor"))
BACKTEST_PAGE = BACKTEST_PAGE.replace("__NAV__", _nav_html("/backtest"))
BILLING_PAGE = BILLING_PAGE.replace("__NAV__", _nav_html("/billing"))
