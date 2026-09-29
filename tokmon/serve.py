"""本地 Web 看板 (`tokmon serve`) —— 早期预览版。

只监听 localhost, 纯标准库 (http.server), 零额外依赖, 默认不外发。
严格遵守北极星原则 4: 它是内核的消费者, 复用 load_records()/aggregate, 不碰内核。

  GET /                      -> 单页 HTML 看板 (内联, 无外部 CDN, 可离线)
  GET /api/summary?since=&scope=&vscode_only=  -> JSON 汇总 (含「vs 上一周期」自基线对比)
  GET /api/tokens/cube?since=&scope=&vscode_only=  -> /tokens 页的数据立方 (同口径, 前端本地切片/联动筛选)

自基线对比 (复盘): 对所选窗口, 额外聚合「紧邻的、等长的上一周期」(同一份 load_records()
输出的第二次 filter+summarize), 给出 token/成本/项目的 Δ% —— 回答北极星第三问
「跟我的预期差多少」, 用「你自己的上一周期」当基线, 不是凭空的预算 (那是 v0.4)。
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (activity, autostart, billing, context_view, control, instances, notify, procmon, remote, runner,
               tokens_view, trace)
from .aggregate import Agg, filter_since, group_by, summarize
from .discovery import default_base
from .event_sources import activity_source, context_source, cost_source, risk_source
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


def build_cube(base: Path, since: str, scope: str, vscode_only: bool) -> dict:
    """GET /api/tokens/cube —— /tokens 页的数据立方 (tokens_view.build), 口径与 build_summary 完全相同:
    同一个 load_records / parse_since / _baseline_window / _filter_window, 所以两边的总量、按天/项目/模型/来源、
    基线都能逐项对上 (tests/test_tokens_view.py 钉死)。`total` / `baseline.total` 随包下发, 前端自己对账。"""
    include_kinds = SCOPE_KINDS.get(scope, SCOPE_KINDS["all"])
    records = load_records(base, include_kinds, vscode_only)
    now = datetime.now().astimezone()
    cutoff = parse_since(since)
    rows = filter_since(records, cutoff)
    bw = _baseline_window(since, now, cutoff)
    prev_rows = _filter_window(records, *bw) if bw is not None else None
    cube = tokens_view.build(rows, prev_rows, records)
    prev_enc = cube.pop("prev_rows")
    baseline = None
    if bw is not None:
        prev_lo, prev_hi = bw
        earliest = min((r.timestamp for r in records), default=None)
        baseline = {
            "prev_lo": prev_lo.isoformat(timespec="seconds"),
            "prev_hi": prev_hi.isoformat(timespec="seconds"),
            "lo": round(prev_lo.timestamp(), 3),
            "hi": round(prev_hi.timestamp(), 3),
            "kind": "prev-day-partial" if (since or "").strip().lower() == "today" else "prev-period",
            "partial": earliest is not None and prev_lo < earliest,
            "total": _agg_dict(summarize(prev_rows)),
            "rows": prev_enc,
        }
    return {
        "v": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "window": since or "all",
        "scope": scope,
        "vscode_only": vscode_only,
        "span": tokens_view.span_of(cutoff, now, records),
        "total": _agg_dict(summarize(rows)),
        "baseline": baseline,
        **cube,
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


# ---- 实例层 v0.22 (instances / autostart): 策略与审计在这里, 机械在 instances.py ----
# 闸门 (P7): 令牌头 (_ctl_guard, 永不认 Cookie) + **只认本机 Host** (手机经隧道只读) —— 两道都在 Handler 里;
# 刻意**不看控制模式** (用户决定 #4: 启停自己登记的服务不该先去开一个远程审批开关)。
# 审计: 每个变更 (含 draft: 它读原始命令行) 都记一笔; target 只写实例名 / id / 组名, 绝不写命令行 / 路径。
_INST_POSTS = {
    "/api/instances/start": "instance-start", "/api/instances/stop": "instance-stop",
    "/api/instances/restart": "instance-restart", "/api/instances/start-group": "instance-start-group",
    "/api/instances/save": "instance-save", "/api/instances/patch": "instance-patch",
    "/api/instances/delete": "instance-delete", "/api/instances/get": None,     # get: 只读 (本机+令牌), 不审计
    "/api/instances/draft": "instance-draft", "/api/instances/boot-ack": "instance-boot-ack",
    "/api/autostart": "autostart",
}
_FORWARD_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Forwarded-Host", "Forwarded")
_AUTOSTART_TTL = 20.0                  # 页面每 4 秒轮询一次; schtasks 查询要起子进程, 缓存一下
_AUTOSTART_CACHE: dict = {"t": 0.0, "v": None}


def _peer_is_loopback(addr) -> bool:
    """TCP 对端是不是本机回环。Host 头是客户端自己写的: 远程模式绑到 0.0.0.0 时, 局域网里谁都能直连并写
    `Host: localhost` —— 只有对端地址骗不了。反过来它**单独不够**: cloudflared 这类本机隧道也从回环进来,
    所以 _is_local 还要配合 Host / 转发头。解析不了 (怪地址) 一律算不是本机 (只收紧)。"""
    try:
        host = addr[0] if isinstance(addr, (tuple, list)) else addr
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])     # 去掉 IPv6 的 %scope
    except (ValueError, TypeError, IndexError):
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip                    # 双栈监听: ::ffff:127.0.0.1 也是回环
    return ip.is_loopback


def _claude_dir_arg(base) -> str | None:
    """开机任务要不要带 --claude-dir: 这个 serve 用的不是默认目录, 开机那次也得用它 —— 否则一进来就
    「找不到 Claude 数据目录」。给绝对路径 (开机任务的工作目录是仓库根, 相对路径会指错地方); 默认目录就不带。"""
    if base is None:
        return None
    b = os.path.abspath(str(base))
    try:
        same = os.path.normcase(b) == os.path.normcase(os.path.abspath(str(default_base())))
    except Exception:
        same = False
    return None if same else b


def _attn_instances(serve_port=None) -> dict | None:
    """铃铛的「重启后有实例等你确认」: 这是**常驻状况** (不是一次性事件) —— 开机后的事件只在那几秒发一次,
    页面多半还没打开; 所以每次轮询都带上, 页面按「这次开机拉起」的时刻去重, 首次轮询也弹。
    只由实例层的主人报 (两个 tokmon 各开一页也只响一次); 与 /api/instances 同一个 serve_port -> 共用它的状态缓存。
    查不到就当没有 (铃铛不因实例层出错而坏)。"""
    try:
        if not instances.available():
            return None
        st = instances.status_all(serve_port=serve_port)
    except Exception:
        return None
    owner = st.get("owner")
    if isinstance(owner, dict) and owner.get("is_owner") is False:
        return None
    boot = st.get("boot") or {}
    pending = boot.get("pending") or []
    if not pending:
        return None
    return {"pending": len(pending), "key": f"INSTANCE_BOOT_PENDING:{boot.get('handled')}"}


def _autostart_status(fresh: bool = False) -> dict:
    now = time.time()
    if not fresh and _AUTOSTART_CACHE["v"] is not None and now - _AUTOSTART_CACHE["t"] < _AUTOSTART_TTL:
        return _AUTOSTART_CACHE["v"]
    try:
        v = autostart.status()
    except Exception as e:             # 查不到不拖垮实例区: 如实说查询失败
        v = {"supported": False, "installed": False, "method": None, "detail": f"查询失败: {type(e).__name__}"}
    _AUTOSTART_CACHE.update(t=now, v=v)
    return v


def _instances_status(port, local: bool) -> dict:
    """GET /api/instances = 实例状态 + 开机自启状态 + 这次请求是不是本机 (页面据此藏掉启停按钮)。"""
    d = dict(instances.status_all(serve_port=port))
    d["autostart"] = _autostart_status()
    d["local"] = local
    return d


def _instances_log(q) -> dict:
    iid = (q.get("id") or [""])[0]
    try:
        n = int((q.get("n") or ["200"])[0])
    except ValueError:
        n = 200
    return instances.log_tail(iid, n)            # n 的夹紧 (1..500) 与逐行脱敏在 instances 里


def _inst_name(iid: str) -> str:
    """审计里的目标: 你给实例起的名字, 取不到就用 id。只取 name, 别的字段 (命令 / 环境变量) 一概不碰。"""
    try:
        rec = instances.get_instance(iid) if iid else None
    except Exception:
        rec = None
    return str((rec or {}).get("name") or iid or "?")


def _inst_audited(kind: str, target: str, fn) -> dict:
    """跑一个实例动作并审计 (无论成败; 抛异常也记一笔 "error" 再抛给 _json 回 500)。"""
    try:
        r = fn()
    except Exception:
        control.plane.audit_action(kind, target, "error")
        raise
    control.plane.audit_action(kind, target, "ok" if r.get("ok") else str(r.get("reason") or "failed"))
    return r


def _do_instance(path: str, body: dict, serve_port=None, base=None) -> dict:
    """实例层 POST 的分发 (闸门已在 Handler 里过完)。参数不对 -> 明确 reason, 不猜。
    实例层回 not-owner (另一个 tokmon 在管实例) 原样透传给页面, 审计结果记 not-owner。"""
    kind = _INST_POSTS[path]
    iid = str(body.get("id") or "").strip()
    if path == "/api/instances/get":             # 完整记录 (含环境变量原值) —— 只给本机持令牌的编辑框
        rec = instances.get_instance(iid) if iid else None
        return {"ok": True, "instance": rec} if rec else {"ok": False, "reason": "not-found"}
    if path == "/api/instances/start":
        return _inst_audited(kind, _inst_name(iid), lambda: instances.start(iid, force=body.get("force") is True))
    if path == "/api/instances/stop":
        return _inst_audited(kind, _inst_name(iid), lambda: instances.stop(iid))
    if path == "/api/instances/restart":
        return _inst_audited(kind, _inst_name(iid), lambda: instances.restart(iid))
    if path == "/api/instances/start-group":
        group = str(body.get("group") or "core")
        if group not in ("core", "backup"):
            return {"ok": False, "reason": "bad-group"}
        return _inst_audited(kind, f"group {group}", lambda: instances.start_group(group))
    if path == "/api/instances/save":
        data = body.get("instance")
        if not isinstance(data, dict):
            return {"ok": False, "reason": "invalid", "errors": {"instance": "required"}}
        orig = str(body.get("original_id") or "").strip() or None
        target = str(data.get("name") or data.get("id") or orig or "?")[:40]   # 提交的名字 (已知是你自己起的标签)
        return _inst_audited(kind, target, lambda: instances.save_instance(data, original_id=orig))
    if path == "/api/instances/patch":
        fields = body.get("fields")
        if not isinstance(fields, dict):
            return {"ok": False, "reason": "invalid", "errors": {"fields": "required"}}
        return _inst_audited(kind, _inst_name(iid), lambda: instances.patch_instance(iid, fields))
    if path == "/api/instances/delete":
        target = _inst_name(iid)                 # 删之前取名字, 删完就查不到了
        return _inst_audited(kind, target, lambda: instances.delete_instance(iid))
    if path == "/api/instances/draft":
        try:
            pid = int(body.get("pid"))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "bad-pid"}
        ct = body.get("create_time")
        if ct is None:                           # 没有 create_time 就核不了身份 (pid 可能已被复用) -> 让页面刷新后重试
            control.plane.audit_action(kind, f"pid {pid}", "identity-required")
            return {"ok": False, "reason": "identity-required"}
        try:
            ct = float(ct)
        except (TypeError, ValueError):
            ct = math.nan
        if not math.isfinite(ct):                # NaN 跟谁比都「不超差」-> 会把身份核对整个绕过去 (json 认 NaN 字面量)
            return {"ok": False, "reason": "bad-create-time"}
        return _inst_audited(kind, f"pid {pid}", lambda: instances.draft_from_pid(pid, ct))
    if path == "/api/instances/boot-ack":
        action = str(body.get("action") or "")
        if action not in ("start", "dismiss"):
            return {"ok": False, "reason": "bad-action"}
        return _inst_audited(kind, action, lambda: instances.boot_ack(action))
    # /api/autostart: 开 = 登记到任务计划程序 (失败退到注册表 Run); 关 = 两处都删。开机实例一律绑本机回环。
    on = body.get("on")
    if not isinstance(on, bool):                 # 缺字段 / 写成字符串: 不猜是装还是卸
        return {"ok": False, "reason": "bad-on"}
    try:
        p = int(body.get("port") or serve_port or 8765)
    except (TypeError, ValueError):
        p = 0
    if not 1 <= p <= 65535:
        return {"ok": False, "reason": "bad-port"}
    kw = {}
    cd = _claude_dir_arg(base)
    if cd:                                       # 非默认数据目录: 开机那次也用它 (默认目录就不带, 老接口照样能用)
        kw["claude_dir"] = cd
    try:
        return _inst_audited(kind, "install" if on else "uninstall",
                             (lambda: autostart.install(port=p, host="127.0.0.1", **kw)) if on else autostart.uninstall)
    finally:
        _AUTOSTART_CACHE.update(t=0.0, v=None)   # 装/卸之后下一次轮询重新查, 页面马上看到真实状态


def _serve_instances_note(boot: bool, port=None) -> str:
    """启动横幅里实例那一行的尾巴。只用纯文字 (GBK 控制台写不出 ✓ 这类符号, 会在启动时崩)。"""
    if not instances.available():
        return "   (缺 psutil, 实例区只读提示)"
    st = _autostart_status(fresh=True)
    how = {"task": "任务计划程序", "run-key": "注册表 Run"}.get(st.get("method"), "")
    note = (f"   开机自启: 已装 ({how})" if st.get("installed")
            else ("   开机自启: 未装 (tokmon autostart install)" if st.get("supported") else ""))
    try:                                         # 另一个 tokmon 拿着实例层的锁: 这个只看不管, 开机拉起也不归它
        owner = instances.status_all(serve_port=port).get("owner")
    except Exception:
        owner = None
    if isinstance(owner, dict) and owner.get("is_owner") is False:
        if owner.get("stale"):                   # 锁的主人已经退出: 这个 tokmon 几秒内自己接管 (不补做开机拉起)
            return note + "   · 原来管理实例的 tokmon 已退出, 正在接管 (几秒后这边就能启停)"
        who = " · ".join(x for x in (f"pid {owner['pid']}" if owner.get("pid") else "",
                                     f"端口 {owner['port']}" if owner.get("port") else "") if x)
        return note + (f"   · 只读: 另一个 tokmon{' (' + who + ')' if who else ''}"
                       " 在管理实例, 启停 / 开机拉起请在它那边")
    if boot:
        note += "   · --boot: 本次由实例层拉起 auto 实例 / 提醒 ask 实例"
    return note


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
        if row.get("state") in ("WORKING", "PROCESSING", "BLOCKED_ON_USER") or row.get("tool_pending"):
            return {"ok": False, "reason": "session-busy"}   # 失败安全: 不 resume 活会话
        if row.get("liveness") is True:
            return {"ok": False, "reason": "session-open"}   # 空闲但还开在某个进程里 (如 VS Code 面板): resume = 两个进程写同一 transcript
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
    """正在跑的会话: WORKING / PROCESSING / BLOCKED_ON_USER (回合卡在等你授权/回答), 或挂着一个等你的调用 (AskUserQuestion / ExitPlanMode)。"""
    try:
        snap = activity.snapshot(base, live=procmon.live_claude_index())
    except Exception:
        return set()
    out = set()
    for x in snap.get("sessions", []):
        st = x.get("state")
        if st in ("WORKING", "PROCESSING", "BLOCKED_ON_USER") or (
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
    C = sm.get("context")                            # 上下文曲线 (省钱 S3): 调用标签里可能有路径 / 命令片段
    if C:
        for c in [c for j in C.get("jumps") or [] for c in j.get("calls") or []] + list(C.get("top_calls") or []):
            c["label"] = _wf_red(c.get("label"))
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


BURN_WINDOW_S = 600                    # 「正在烧」= 近 10 分钟新增的 token
_RECS_TTL = 30.0
_RECS_VIEW: dict = {"t": 0.0, "key": None, "v": None}


def _records_view(base) -> dict:
    """/sessions 里两样「要扫全部记录」的东西, 一次扫描一起算、30 秒内复用:
    - today: 今天 (本地 00:00 起) 全部会话的 token 真值与等价 $ —— 与 /tokens「今天 · 全部」同一套 load_records / summarize
      口径 (scope=all, 不限 .vscode); 拿不到 -> None (页面不显示, 不瞎估);
    - ctx: 每个会话主线程最后一轮的上下文体检 (context_view, 省钱 S1)。"""
    now = time.time()
    key = str(base)
    if _RECS_VIEW["v"] is not None and _RECS_VIEW["key"] == key and now - _RECS_VIEW["t"] < _RECS_TTL:
        return _RECS_VIEW["v"]
    try:
        recs = load_records(base, SCOPE_KINDS["all"], False)
    except Exception:
        recs = None
    today, ctx = None, {}
    if recs is not None:
        try:
            agg = summarize(filter_since(recs, parse_since("today")))
            today = {"tokens": agg.total_tokens, "cost": round(agg.cost, 4), "unpriced": bool(agg.any_unpriced)}
        except Exception:
            pass
        try:
            ctx = context_view.session_contexts(recs)
        except Exception:
            pass
    v = {"today": today, "ctx": ctx}
    _RECS_VIEW.update(t=now, key=key, v=v)
    return v


def _spend_today(base) -> dict | None:
    return _records_view(base)["today"]


# 会话简报的后台线程: /api/sessions 绝不当场解析会话 (冷启动时十几个会话一起算要近一分钟, 页面会白等);
# 只读缓存 (可以旧一点), 没有或过期的排队交给这一个线程, 算好了下一次刷新就带上。正在跑的会话排在前面。
_BRIEF_Q: "queue.PriorityQueue" = queue.PriorityQueue()
_BRIEF_PENDING: set = set()
_BRIEF_GUARD = threading.Lock()
_BRIEF_THREAD: list = [None]
_BRIEF_SEQ = [0]


def _brief_worker():
    while True:
        _pri, _seq, path, running = _BRIEF_Q.get()
        try:
            trace.session_brief(path, running=running)
        except Exception:
            pass                                          # 单个会话算不出来不拖垮别的
        finally:
            with _BRIEF_GUARD:
                _BRIEF_PENDING.discard(path)
            _BRIEF_Q.task_done()


def _brief_nowait(path: str, running: bool):
    """缓存里的简报 (可能是旧的; 还没有 -> None)。不新鲜就排队让后台线程重算, 本次请求不等。"""
    brief, fresh = trace.brief_peek(path, running=running)
    if not fresh:
        with _BRIEF_GUARD:
            if path not in _BRIEF_PENDING:
                _BRIEF_PENDING.add(path)
                _BRIEF_SEQ[0] += 1
                _BRIEF_Q.put((0 if running else 1, _BRIEF_SEQ[0], path, running))
            if _BRIEF_THREAD[0] is None or not _BRIEF_THREAD[0].is_alive():
                _BRIEF_THREAD[0] = threading.Thread(target=_brief_worker, name="mc-brief", daemon=True)
                _BRIEF_THREAD[0].start()
    return brief


_ATTN_TYPES = ["PERMISSION_NEEDED", "QUESTION_PENDING", "CONTEXT_LARGE",   # CONTEXT_LARGE 只有页面上勾了才弹
               "PROCESS_CRASHED"]                                          # 实例层 v0.22: 实例意外退出
# INSTANCE_BOOT_PENDING 故意不走事件这条路: 它是常驻状况, 由 _attn_instances 每次轮询带上 (两条路都走会弹两次)。


def _attention(base, since: int, serve_port=None) -> dict:
    """「等你」的轻量轮询 (各页导航里的提醒开关, 每 5 秒): 此刻卡在你身上的会话 + 游标之后新的「等你」事件。
    只读 activity 快照 (2 秒共享缓存) 与事件总线, 不解析会话、不扫记录。since < 0 = 刚打开页面: 只给游标, 不补旧事件。
    另: 有实例在等你确认拉起时带 "instances" (常驻状况, 首次轮询也带)。"""
    snap = activity.snapshot(base, live=procmon.live_claude_index())
    blocked = [{"session_id": r.get("session_id"), "project": r.get("project"), "title": r.get("title"),
                "state_label": r.get("state_label")}
               for r in snap.get("sessions", []) if r.get("state") == "BLOCKED_ON_USER"]
    if since < 0:
        out = {"blocked": blocked, "events": [], "seq": event_bus.snapshot_meta()["last_seq"]}
    else:
        ev = event_bus.since(since, types=_ATTN_TYPES)
        out = {"blocked": blocked, "events": ev["events"], "seq": ev["last_seq"]}
    inst = _attn_instances(serve_port)
    if inst:
        out["instances"] = inst
    return out


def _sessions_with_briefs(base) -> dict:
    """/api/sessions = activity 快照 + 每个会话当前任务的简报 (风险标记 / 改动计数 / 回放入口 / token 与等价 $ /
    近 10 分钟 / 活跃时长) + 今天全部会话已烧多少。
    activity.snapshot 有 2 秒的**共享**缓存 (事件 pump 也在用): 只拷贝、绝不原地改。"""
    snap = activity.snapshot(base, live=procmon.live_claude_index())
    out = dict(snap)
    rows = []
    now = time.time()
    pending = 0
    rv = _records_view(base)
    for row in snap.get("sessions", []):
        r = dict(row)
        age = row.get("last_activity_age_s")
        c = rv["ctx"].get(row.get("session_id"))
        if c and (age is None or age < _BRIEF_MAX_AGE):
            r["context"] = c                                 # 省钱 S1: 主线程最后一轮的上下文 / 每轮 $ / 缓存有效期
        if row.get("file") and (age is None or age < _BRIEF_MAX_AGE):
            try:
                b = _brief_nowait(row["file"], row.get("state") in ("WORKING", "PROCESSING", "BLOCKED_ON_USER"))
            except Exception:
                b = None
            if b is None:
                pending += 1
            if b:
                cost, unpriced = _wf_price(b.get("by_model"))
                r["workflow"] = {"task": b["task"], "changes": b["changes"], "files": b["files"],
                                 "risks": [{"rule": x["rule"], "label": _wf_red(x["label"]), "n": x.get("n")}
                                           for x in b["risks"]],
                                 "tokens": b.get("tokens"), "cost": cost, "unpriced": unpriced, "partial": b.get("partial"),
                                 "active": b.get("active"),
                                 "burn": trace.recent_tokens(b.get("usage_ts") or [], now - BURN_WINDOW_S)}
        rows.append(r)
    out["sessions"] = rows
    out["spend_today"] = rv["today"]
    out["burn_window_s"] = BURN_WINDOW_S
    out["context_line"], out["big_ctx"] = context_view.TAX_LINE, context_view.BIG_CTX
    out["briefs_pending"] = pending                      # 还没算出当前任务的会话数 (服务刚启动时会有)
    return out


def _make_handler(base: Path, rcfg: remote.RemoteConfig | None = None, port: int | None = None):
    rcfg = rcfg or remote.RemoteConfig()          # 默认 = 本机模式 (零行为变化)
    serve_port = port                             # 实例层: 认出「本服务自己」那条实例 (kind=self) 用
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

        def _is_local(self) -> bool:
            """这次请求是不是从本机来的: TCP 对端是回环 **且** Host 是回环写法 **且** 没带转发头。
            实例层的全部变更只认本机 (用户决定 #5: 手机只读), 哪怕令牌对、远程模式开着。
            - 对端: 远程模式绑到 0.0.0.0 时, 局域网设备能直连并自称 `Host: localhost` —— Host 头骗得了, 对端地址骗不了;
            - Host: 隧道 (cloudflared) 在本机, 对端也是回环, 只能靠它带的隧道域名认出来;
            - 转发头: 只收紧不放宽的保险 —— 万一隧道被配成把 Host 改写成 localhost, 手机照样只读。本机伪造只会把自己降成只读。"""
            if not _peer_is_loopback(self.client_address):
                return False
            if any(self.headers.get(h) for h in _FORWARD_HEADERS):
                return False
            return remote.normalize_host(self.headers.get("Host")) in remote.LOCAL_HOSTS

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
                     "/api/summary", "/api/tokens/cube", "/api/processes", "/api/health", "/api/sessions", "/api/attention", "/api/events",
                     "/api/notifications", "/api/notify-test", "/api/control", "/api/budget", "/api/doctor",
                     "/api/backtest", "/api/billing", "/api/instances", "/api/instances/log"}
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
            if path == "/api/tokens/cube":        # 已过上面的读门 (远程模式要令牌), 与 /api/summary 同参数同口径
                q = parse_qs(parsed.query)
                since = q.get("since", ["7d"])[0]
                scope = q.get("scope", ["all"])[0]
                vscode_only = q.get("vscode_only", ["0"])[0] in ("1", "true", "True")
                self._json(lambda: build_cube(base, since, scope, vscode_only))
                return
            if path == "/api/processes":
                self._json(procmon.snapshot)
                return
            if path == "/api/instances":          # 实例层: 已过读门 (远程模式要令牌); 命令已脱敏, 不含环境变量值
                local = self._is_local()
                self._json(lambda: _instances_status(serve_port, local))
                return
            if path == "/api/instances/log":
                self._json(lambda: _instances_log(parse_qs(parsed.query)))
                return
            if path == "/api/sessions":
                # 组合层在这里把 process 支柱的活性索引注入 activity —— activity 本身不 import procmon;
                # 再把 trace 支柱的「当前任务简报」挂上 (角标 + 实时回放入口)。
                self._json(lambda: _sessions_with_briefs(base))
                return
            if path == "/api/attention":
                q = parse_qs(parsed.query)
                try:
                    since = int(q.get("since", ["-1"])[0] or -1)
                except ValueError:
                    since = -1
                self._json(lambda: _attention(base, since, serve_port))
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
                if (not token and remote.normalize_host(self.headers.get("Host")) in remote.LOCAL_HOSTS
                        and _peer_is_loopback(self.client_address)):   # 绑到 0.0.0.0 时局域网也能自称 localhost, 对端地址骗不了
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
                    on = bool(body.get("on"))
                    if on and not self._is_local():
                        # 「打开」只认本机 (0.22 评审 + 你的决定): 控制模式一开, 持令牌者就能经隧道 steer 出命令执行;
                        # 关掉 / 审批仍可在手机上做。回 200 + ok:false (不是 403), 免得页面把令牌当失效清掉。
                        control.plane.audit_action("control-mode", "on", "local-only")
                        self._json(lambda: {"ok": False, "reason": "local-only",
                                            "remote_mode": control.plane.remote_mode})
                        return
                    self._json(lambda: {"ok": True, "remote_mode": control.plane.set_mode(on)})
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
            # 实例层 v0.22: 令牌头 + 只认本机 Host; **不**要求控制模式 (见 _INST_POSTS 上方的说明)
            if path in _INST_POSTS:
                if not self._ctl_guard():
                    return
                if not self._is_local():          # 手机经隧道: 令牌对也只读 (用户决定 #5)
                    self._send(403, b'{"ok":false,"reason":"local-only"}', "application/json; charset=utf-8")
                    return
                self._json(lambda: _do_instance(path, body, serve_port, base))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def _bind_server(addr, handler):
    """建 HTTP 服务并绑端口。Windows 上 http.server 默认开 SO_REUSEADDR, 那里它的意思是「允许别的进程同绑这个端口」
    (实测: 第二个 serve 绑同一端口不报错, 两个进程一起收请求、各跑一套实例监督)。关掉它, 第二个就绑不上 ——
    run_serve 靠「绑上端口」来判断自己是不是唯一的那个。POSIX 的 SO_REUSEADDR 只是允许复用 TIME_WAIT, 保留。"""
    httpd = ThreadingHTTPServer(addr, handler, bind_and_activate=False)
    if sys.platform == "win32":
        httpd.allow_reuse_address = False
    try:
        httpd.server_bind()
        httpd.server_activate()
    except BaseException:
        httpd.server_close()
        raise
    return httpd


def _stdout_is_tty() -> bool:
    """横幅是打到人眼前的终端, 还是落进日志文件 (开机自启走 pythonw, 输出重定向到 ~/.tokmon/logs/serve.log)。"""
    try:
        return bool(sys.stdout is not None and sys.stdout.isatty())
    except Exception:
        return False


def run_serve(base: Path, host: str = "127.0.0.1", port: int = 8765, boot: bool = False) -> bool:
    """boot=True: 开机自启任务 (`tokmon serve --boot`) 拉起的这一次 —— 实例层据此做一次开机拉起。
    返回 False = 没起来 (目录 / 远程配置不对 / 端口绑不上): cli 据此以退出码 1 结束 —— 脚本和任务计划程序的
    「上次运行结果」要看得出失败, 不能把一次没起来的 serve 报成成功。正常跑完 (Ctrl+C) 返回 True。"""
    if not Path(base).exists():
        if not boot:
            print(f"找不到 Claude 数据目录: {base}")
            return False
        # 开机那次不能因为这个退出: 实例拉起 / 进程页与 Claude 数据目录无关, 会话 / token 页面只是空着 (照常降级)
        print(f"找不到 Claude 数据目录: {base} —— 开机自启照常起服务 (实例层不需要它), 会话 / token 页面会是空的")
    # 远程暴露收口 (MC_REMOTE): 配置不自洽 -> **拒绝启动**, 绝不带着半个洞跑起来 (P7 ⑤ 失败安全)。
    rcfg = remote.from_env()
    problem = remote.preflight(rcfg, host, control.plane.token)
    if problem:
        print(problem)
        return False
    activity_source.start_pump(Path(base), live_factory=procmon.live_claude_index)   # M2: 对话活动事件 pump (5s, 带活性消歧)
    cost_source.start_pump(Path(base))        # M3.5: 成本预算 pump (60s)
    risk_source.start_pump(Path(base), live_factory=procmon.live_claude_index)   # 改动与风险 S3: 风险事件 pump (15s, 只发 info)
    context_source.start_pump(Path(base))      # 省钱 S3: 正在跑的会话上下文刚过 30 万 (30s, 只发 info)
    billing.start_pump()                      # B1: 厂商账单 pump (5min; 未配 key 则零外发)
    trace.start_warmer(Path(base))            # /workflow: 后台把 transcript 读进缓存 + 算耗时基线 (冷启动约 6-10s)
    notifier = notify.start_notifier()        # M3: 通知层订阅总线 (默认仅本地, 配 token 才外发)
    try:
        httpd = _bind_server((host, port), _make_handler(Path(base), rcfg, port=port))
    except OSError as e:
        print(f"端口 {port} 绑不上 ({e.strerror or e}) —— 多半已经有一个 tokmon (或别的程序) 在用它。"
              f"换个端口: --port <别的>")
        return False
    # 实例层 v0.22: 端口绑上之后才起监督线程 —— 绑不上 (已有一个 tokmon 在跑) 就不该第二个人去推进实例状态 / 做开机拉起。
    # 换个端口起的第二个 tokmon 靠实例层自己的主人锁挡住 (它只读)。
    # 放在通知层订阅之后: 开机提醒 / 意外退出的事件要能进 feed。
    if instances.available():
        instances.start_supervisor(boot=boot, serve_port=port)
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
    print(f"  · 实例 (启停/开机拉起) {url}processes#instances" + _serve_instances_note(boot, port))
    if _stdout_is_tty():
        print(f"  控制令牌 (页面首次开控制模式/终止进程时粘贴一次, 之后存浏览器): {control.plane.token}")
        print("  (令牌不再经 HTTP 下发, 只在这里/~/.tokmon/control_token 可见 —— 防本机他进程/网页窃取后终止你的进程)")
    else:   # 输出落进日志文件 (开机自启 / 重定向): 日志会被拿去排障、贴出去, 令牌原文绝不进去
        print("  控制令牌：见 ~/.tokmon/control_token（不写进日志）")
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
    return True


# ---- 单页前端: 页面都在 tokmon/pages/*.html (零外部依赖, 可离线), 文件末尾统一载入。
# 下面是各页共用的皮肤 (__BASE__) 与统一导航 (__NAV__) ----
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
  .nav { display:flex; flex-wrap:wrap; gap:6px 14px; margin-left:auto; align-items:center; font-size:13px; }
  .nav a { color:var(--dim); text-decoration:none; padding:4px 8px; border-radius:7px; white-space:nowrap; }
  .nav a:hover, .nav a.active { color:var(--fg); background:var(--panel); }
  .nav details.more { position:relative; }
  .nav details.more summary { list-style:none; cursor:pointer; color:var(--dim); padding:4px 8px; border-radius:7px; white-space:nowrap; }
  .nav details.more summary::-webkit-details-marker { display:none; }
  .nav details.more summary:hover, .nav details.more summary.active, .nav details.more[open] summary { color:var(--fg); background:var(--panel); }
  .nav details.more .menu { position:absolute; right:0; top:calc(100% + 4px); display:flex; flex-direction:column; gap:2px;
      min-width:120px; background:var(--panel); border:1px solid var(--line); border-radius:9px; padding:4px; z-index:60;
      box-shadow:0 8px 24px rgba(0,0,0,.45); }
  .nav .bell { background:none; border:1px solid var(--line); color:var(--dim); border-radius:7px; padding:2px 8px;
      cursor:pointer; font-size:13px; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .nav .bell:hover { border-color:var(--accent); } .nav .bell.on { color:var(--fg); }
  .nav .bell.hot { color:var(--warn); border-color:#4a3d22; background:#1d1a12; }
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
            + "".join(a(h, t) for h, t in _NAV_MORE) + "</div></details>" + _BELL)


# 会话驾驶舱 S2: 导航里的「等你时提醒」开关 + 轮询 (/api/attention, 每 5 秒)。浏览器通知, 只在本机, 默认关。
_BELL = r"""<button type="button" class="bell" id="mcbell" title="等你时提醒（已关）">🔕</button><script id="mc-bell-js">
(function () {   // 会话驾驶舱 S2: 会话卡在你身上 ≥ 60 秒 -> 浏览器通知。只在本机, 零外发; 默认关, 开关存在浏览器本地。
  var KEY = "mc.notify.on", STATS = "mc.notify.stats", PFX = /^\(\d+\) 等你 · /;
  var bell = document.getElementById("mcbell"), seq = -1, lastN = 0;
  function ls(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function on() { return ls(KEY) === "1"; }
  function stats() {   // 近 7 天弹了几次 / 点开几次 (一周打扰预算的实测, 存在本浏览器)
    var s; try { s = JSON.parse(ls(STATS) || "{}"); } catch (e) { s = {}; }
    var cut = Date.now() - 7 * 86400e3;
    s.shown = (s.shown || []).filter(function (t) { return t > cut; });
    s.clicked = (s.clicked || []).filter(function (t) { return t > cut; });
    return s;
  }
  function bump(k) { var s = stats(); s[k].push(Date.now()); ls(STATS, JSON.stringify(s)); }
  function paint(n) {
    if (n !== lastN) {   // 等你的会话数变了: 告诉页面 (/sessions 立刻刷新列表, 不等 30 秒), 列表和铃铛说的一致
      try { window.dispatchEvent(new CustomEvent("mc:attention", {detail: {blocked: n}})); } catch (e) {}
    }
    lastN = n;
    var s = stats(), perm = ("Notification" in window) ? Notification.permission : "unsupported";
    bell.textContent = (on() ? "🔔" : "🔕") + (n ? " " + n : "");
    bell.classList.toggle("on", on());
    bell.classList.toggle("hot", n > 0);
    bell.title = (n ? "此刻有 " + n + " 个会话在等你授权 / 回答\n" : "")
      + (on() ? "等你时提醒：开（再点一下关掉）\n会话卡在你身上超过 60 秒、实例意外退出、重启后有实例等你确认时弹一条通知；这个页面要开着，在后台时浏览器可能最多晚一分钟"
              : (perm === "denied" ? "等你时提醒：浏览器没给通知权限 —— 点地址栏左边的站点设置，把「通知」改成允许，再点一下"
                                   : (perm === "unsupported" ? "这个浏览器不支持通知" : "等你时提醒：关（点一下打开）")))
      + "\n近 7 天弹了 " + s.shown.length + " 次，点开 " + s.clicked.length + " 次";
    document.title = (n ? "(" + n + ") 等你 · " : "") + document.title.replace(PFX, "");
  }
  function fire(e, blocked) {
    if (!on() || !("Notification" in window) || Notification.permission !== "granted") return;
    // 实例层 v0.22: 只认实例层发的崩溃 (别的支柱将来的 PROCESS_CRASHED 口径不同, 这里不猜)。
    // 「重启后等你确认」不走事件这条路 (见 fireBoot), 万一来了也不弹, 免得弹两次。
    if (e.type === "INSTANCE_BOOT_PENDING") return;
    var inst = e.type === "PROCESS_CRASHED" && e.pillar === "instances";
    if (e.type === "PROCESS_CRASHED" && !inst) return;
    var mark = "mc.notified." + e.dedup_key;
    if (ls(mark)) return;                          // 别的标签页已经弹过这一段等待
    ls(mark, String(Date.now()));
    var b = null;
    for (var i = 0; i < blocked.length; i++) if (blocked[i].session_id === e.session) b = blocked[i];
    var p = e.payload || {}, what = e.type === "PERMISSION_NEEDED" ? "等你授权" : "等你回答", body, title = null,
        go = "/sessions#s-" + encodeURIComponent(e.session || "");
    if (inst) {                                   // 点开 -> /processes 的实例区 (看日志 / 重启)
      go = "/processes#instances";
      title = "实例意外退出 · " + (p.instance || "?");
      body = (p.state_label || "意外退出") + (p.exit_code != null ? "（退出码 " + p.exit_code + "）" : "")
        + "。不会自动重启 —— 去实例区看日志再决定。";
    } else if (e.type === "CONTEXT_LARGE") {             // 省钱 S3: 只有在 /sessions 勾了「上下文过 30 万也提醒」才弹
      if (ls("mc.notify.ctx") !== "1") return;
      what = "上下文过 30 万";
      body = "这一轮 " + Math.round((p.count || 0) / 10000) + " 万 token，之后每一轮都要把它再读一遍。合适的时候 /compact，或者换新会话。";
    } else {
      body = ((b && b.title) ? b.title + "\n" : "") + (p.state_label || what) + "（已等 " + Math.max(1, Math.round((p.age_s || 60) / 60)) + " 分钟）";
    }
    var n = new Notification(title || (what + " · " + ((b && b.project) || e.project || "Claude Code")), {body: body, tag: e.dedup_key, renotify: false});
    bump("shown");
    n.onclick = function () { bump("clicked"); window.focus(); location.href = go; n.close(); };
  }
  function fireBoot(x) {   // 实例层 v0.22: 重启后有实例等你确认 = 常驻状况 (像「等你授权」一样一直在): 首次轮询也弹,
    // 按「这次开机拉起」的键去重 (多个标签页 / 刷新都只弹一次); 你拉起或忽略之后服务端就不再带它
    if (!x || !(x.pending > 0) || !on() || !("Notification" in window) || Notification.permission !== "granted") return;
    var mark = "mc.notified." + x.key;
    if (ls(mark)) return;
    ls(mark, String(Date.now()));
    var n = new Notification("重启后有 " + x.pending + " 个实例等你确认拉起",
                             {body: "点开去实例区一键拉起，或者忽略。", tag: x.key, renotify: false});
    bump("shown");
    n.onclick = function () { bump("clicked"); window.focus(); location.href = "/processes#instances"; n.close(); };
  }
  function poll() {
    fetch("/api/attention?since=" + seq).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      var first = seq < 0;
      seq = d.seq;
      paint(d.blocked.length);
      if (!first) d.events.forEach(function (e) { fire(e, d.blocked); });   // 打开页面前的旧事件不补弹
      fireBoot(d.instances);                                                  // 常驻状况: 首次轮询也看
    }).catch(function () {});
  }
  bell.addEventListener("click", function () {
    if (on()) { ls(KEY, "0"); paint(lastN); return; }
    if (!("Notification" in window)) { paint(lastN); return; }
    var go = function (p) {
      if (p !== "granted") { ls(KEY, "0"); paint(lastN); return; }
      ls(KEY, "1"); paint(lastN);
      new Notification("Mission Control · 等你时提醒已打开", {body: "会话卡在你身上超过 60 秒（等授权 / 等你回答）时，这里会弹一条。页面要开着。", tag: "mc-hello"});
    };
    if (Notification.permission === "default") Notification.requestPermission().then(go, function () { go("denied"); });
    else go(Notification.permission);
  });
  window.addEventListener("storage", function (e) { if (e.key === KEY) paint(lastN); });
  try { for (var i = localStorage.length - 1; i >= 0; i--) { var k = localStorage.key(i);   // 两天前的「已弹过」标记清掉
    if (k && k.indexOf("mc.notified.") === 0 && Date.now() - (+localStorage.getItem(k) || 0) > 2 * 86400e3) localStorage.removeItem(k); } } catch (e) {}
  paint(0); poll(); setInterval(poll, 5000);
})();
</script>"""


# ---- 页面载入 (不再往本文件里内联, RECAP 侧批 #6; 会话驾驶舱 S0 把其余页面也搬了出去) ----
def _load_page(name: str, active: str = "/workflow") -> str:
    """读 tokmon/pages/<name>, 换上公共皮肤与统一导航 (active = 高亮哪一项)。文件缺失 -> 友好提示页, 不崩。"""
    nav = _nav_html(active)
    try:
        src = (Path(__file__).parent / "pages" / name).read_text(encoding="utf-8")
    except OSError:
        return f"<h1>页面文件缺失: tokmon/pages/{name}</h1>"
    return src.replace("__BASE__", _BASE_CSS).replace("__NAV__", nav)


WORKFLOW_PAGE = _load_page("workflow.html", "/workflow")     # 工作流回放 + 统计 (WORKFLOW_TAB_PLAN / CHANGE_RISK_PLAN)
PAGE = _load_page("tokens.html", "/tokens")                  # /tokens 一屏分析台 (名字沿用旧的内联常量)
HOME = _load_page("home.html", "/")                          # 主页: 并行同级入口
SESS_PAGE = _load_page("sessions.html", "/sessions")         # 对话 / Session 状态 (M1: 先看见)
PROC_PAGE = _load_page("processes.html", "/processes")       # 进程 / 端口 / cloudflared
NOTIFY_PAGE = _load_page("notify.html", "/notify")           # 通知层 feed + 通道配置 (导航里隐藏)
CONTROL_PAGE = _load_page("control.html", "/control")        # 远程审批 / 控制层 (导航里隐藏)
DOCTOR_PAGE = _load_page("doctor.html", "/doctor")           # 体检: 成本契约 + 推断契约
BACKTEST_PAGE = _load_page("backtest.html", "/backtest")     # 状态推断准确率回测 (L2)
BILLING_PAGE = _load_page("billing.html", "/billing")        # 厂商 API 账单 (真实账单, 非估算)
# 远程模式 (MC_REMOTE) 的令牌输入页: 未鉴权时**代替**被请求的页面返回 (HTTP 401), 登录成功后只需 reload ——
# 浏览器带着新 Cookie 重新请求同一个 URL, 直接落在你本来要去的页。
# 纪律: 令牌只进 password 框, 绝不回显、绝不进 URL (隧道边缘会记 query)、绝不存 localStorage (读门用 HttpOnly Cookie)。
LOGIN_PAGE = _load_page("login.html", "")
