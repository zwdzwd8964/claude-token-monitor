"""/tokens 数据立方 (tokens_view + serve.build_cube) 的对账测试。

盯死的护栏 (NORTH_STAR §2.1 真实优先):
- 立方与 /api/summary (build_summary) **同源同口径**: 同一 since/scope/vscode_only 下,
  总量、按天/项目/模型/来源、基线总量、项目 Δ 全部逐项对上;
- 成本拆分 (input / output / 缓存写 / 缓存读 / 工具) 之和 == 成本, 且与 pricing.cost_usd 逐项同式;
- 日期 / 小时 / 星期按**记录自己的本地时区**分桶 (不是 UTC);
- 新接口过读门: 远程模式 (MC_REMOTE) 未持令牌 -> 403, HEAD 不确认端点存在;
- /tokens 页从 tokmon/pages/tokens.html 载入, 换上了统一导航。
"""

from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tokmon import parser, pricing, remote, serve, tokens_view
from tokmon.records import UsageRecord

VS = r"c:\Users\u\.vscode"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _asst(n, ts: datetime, sid: str, model: str = "claude-opus-4-8", inp=100, out=50, c5=0, c1=0, cr=0, ws=0):
    return {   # n 在整个 fixture 里唯一 (去重键 = (message_id, request_id), 重了会被 parser 合并)
        "type": "assistant", "requestId": f"req-{sid}-{n}", "timestamp": _iso(ts), "sessionId": sid,
        "message": {"id": f"msg-{sid}-{n}", "model": model, "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
            "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1},
            "server_tool_use": {"web_search_requests": ws}}},
    }


def _write(path: Path, cwd: str, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    first = lines[0]["timestamp"] if lines else _iso(datetime.now().astimezone())
    out = [{"type": "user", "cwd": cwd, "timestamp": first}] + lines
    path.write_text("\n".join(json.dumps(x) for x in out), encoding="utf-8")


@pytest.fixture
def base(tmp_path, monkeypatch):
    """三个项目 × 三种来源 × 多个模型 (含未知单价、0 token 的 <synthetic>) × 分布在当前窗 / 上一周期 / 更早。"""
    monkeypatch.setattr(parser, "_file_cache", {})
    now = datetime.now().astimezone()
    b = tmp_path / "projects"
    ago = lambda **kw: now - timedelta(**kw)   # noqa: E731
    # alpha: .vscode 下, main + subagent + workflow
    _write(b / "c--vs-alpha" / "s-alpha.jsonl", VS + r"\alpha", [
        _asst(1, ago(minutes=3), "s-alpha", "claude-opus-5", 10, 200, c5=3000, cr=90000),
        _asst(2, ago(hours=5, minutes=7), "s-alpha", "claude-opus-5", 5, 80, c1=1000, cr=40000, ws=2),
        _asst(3, ago(days=3, hours=2), "s-alpha", "claude-fable-5-1", 7, 30, cr=20000),
        _asst(4, ago(days=9), "s-alpha", "claude-opus-5", 1, 10, c5=500, cr=1000),        # 7d 的上一周期
        _asst(5, ago(days=40), "s-alpha", "claude-opus-4-8", 100, 100),                     # 只在 all 里
        _asst(6, ago(hours=1), "s-alpha", "<synthetic>", 0, 0),                             # 0 token 占位, 未知单价
    ])
    _write(b / "c--vs-alpha" / "s-alpha" / "subagents" / "agent-a1.jsonl", VS + r"\alpha\deep\er", [
        _asst("a1", ago(minutes=50), "s-alpha", "claude-fable-5-1", 3, 60, c5=800, cr=5000),
        _asst("a2", ago(days=1, hours=1), "s-alpha", "claude-opus-5", 2, 20, cr=3000),
    ])
    _write(b / "c--vs-alpha" / "s-alpha" / "subagents" / "workflows" / "wf_x" / "agent-w1.jsonl", VS + r"\alpha", [
        _asst("w1", ago(hours=2), "s-alpha", "claude-opus-5-5", 4, 400, c1=2000, cr=70000),
        _asst("w2", ago(days=12), "s-alpha", "claude-opus-5-5", 4, 40, cr=7000),               # 2w 内, 7d 基线
    ])
    # beta: .vscode 下, 含一个真未知单价模型 (有 token -> 成本按 0 计, 必须被标出来)
    _write(b / "c--vs-beta" / "s-beta.jsonl", VS + r"\beta <img src=x>", [
        _asst(1, ago(hours=26), "s-beta", "mystery-model-9", 50, 50, cr=100),
        _asst(2, ago(days=6), "s-beta", "claude-opus-5", 20, 20, cr=500),
        _asst(3, ago(days=16), "s-beta", "claude-opus-5", 20, 20, cr=500),                  # 2w 的上一周期
    ])
    # delta: 大上下文 (省钱 S2): 写 18 万 1 小时缓存 -> 命中 18 万 (其中 3 万是上下文税) -> 空了 2.5 小时回来整段重建
    _write(b / "c--vs-delta" / "s-delta.jsonl", VS + r"\delta", [
        _asst(1, ago(hours=3), "s-delta", "claude-opus-5", 20, 300, c1=180_000),
        _asst(2, ago(hours=2, minutes=50), "s-delta", "claude-opus-5", 20, 300, cr=180_000),
        _asst(3, ago(minutes=20), "s-delta", "claude-opus-5", 20, 300, c1=182_000),
    ])
    # gamma: 不在 .vscode 下 (vscode_only 时应被排除)
    _write(b / "c--Users-u" / "s-gamma.jsonl", r"C:\Users\u\Desktop", [
        _asst(1, ago(hours=3), "s-gamma", "claude-opus-5", 9, 9, cr=900),
        _asst(2, ago(days=8), "s-gamma", "claude-opus-5", 9, 9, cr=900),
    ])
    return b


def _decode(cube: dict, rows_key: str = "rows") -> list[dict]:
    rows = cube[rows_key] if rows_key == "rows" else cube["baseline"]["rows"]
    cols = cube["cols"]
    out = []
    for a in rows:
        r = dict(zip(cols, a))
        r["day"] = cube["days"][r["d"]]["d"]
        r["wd"] = cube["days"][r["d"]]["wd"]
        r["project"] = cube["projects"][r["p"]]["id"]
        r["model"] = cube["models"][r["m"]]["id"]
        r["source"] = cube["sources"][r["s"]]
        r["session"] = cube["sessions"][r["x"]]["id"]
        out.append(r)
    return out


def _sum(rows, keyfn=None):
    g = defaultdict(lambda: defaultdict(float))
    for r in rows:
        k = keyfn(r) if keyfn else "_"
        for c in ("n", "in", "out", "cr", "cw", "ws", "wf", "c", "un"):
            g[k][c] += r[c]
    return g


def _same(agg: dict, s: dict):
    """cube 的一组和 == summary 的一行 _agg_dict。"""
    assert s["count"] == agg["n"]
    assert s["input"] == agg["in"] and s["output"] == agg["out"]
    assert s["cache_read"] == agg["cr"] and s["cache_write"] == agg["cw"]
    assert s["tokens"] == agg["in"] + agg["out"] + agg["cr"] + agg["cw"]
    assert s["web_search"] == agg["ws"] and s["web_fetch"] == agg["wf"]
    assert math.isclose(s["cost"], round(agg["c"], 4), abs_tol=1e-4)
    assert s["any_unpriced"] == (agg["un"] > 0)


@pytest.mark.parametrize("since", ["today", "24h", "7d", "2w", "all"])
@pytest.mark.parametrize("scope", ["all", "main"])
@pytest.mark.parametrize("vscode_only", [False, True])
def test_cube_reconciles_with_summary(base, since, scope, vscode_only):
    sm = serve.build_summary(base, since, scope, vscode_only)
    cb = serve.build_cube(base, since, scope, vscode_only)
    rows = _decode(cb)
    assert cb["total"] == sm["total"]                      # 随包下发的对账值与 summary 完全相同
    tot = _sum(rows)["_"] if rows else defaultdict(float)
    _same(tot, sm["total"])
    for key, fn in (("by_day", lambda r: r["day"]), ("by_project", lambda r: r["project"]),
                    ("by_model", lambda r: r["model"]), ("by_source", lambda r: r["source"])):
        g = _sum(rows, fn)
        assert set(g) == {x["label"] for x in sm[key]}, key
        for x in sm[key]:
            _same(g[x["label"]], x)
    # 基线: 有无一致; 总量一致; 项目 Δ (含已停) 一致
    assert (cb["baseline"] is None) == (sm["baseline"] is None)
    if sm["baseline"]:
        for k in ("prev_lo", "prev_hi", "kind", "partial", "total"):
            assert cb["baseline"][k] == sm["baseline"][k], k
        prev = _decode(cb, "baseline")
        ptot = _sum(prev)["_"] if prev else defaultdict(float)
        _same(ptot, sm["baseline"]["total"])
        cur_p, prev_p = _sum(rows, lambda r: r["project"]), _sum(prev, lambda r: r["project"])
        want = {d["label"]: d for d in sm["project_deltas"]}
        assert set(want) == set(cur_p) | set(prev_p)
        for lb, d in want.items():
            assert math.isclose(d["cost"], round(cur_p[lb]["c"], 4), abs_tol=1e-4)
            assert math.isclose(d["prev_cost"], round(prev_p[lb]["c"], 4), abs_tol=1e-4)
            assert d["tokens"] == sum(cur_p[lb][c] for c in ("in", "out", "cr", "cw"))
            assert d["prev_tokens"] == sum(prev_p[lb][c] for c in ("in", "out", "cr", "cw"))


def test_fixture_exercises_every_window(base):
    """防止对账测试空转: 每个窗口都真有数据, 7d/2w 都真有基线, 真有未知单价。"""
    for since in ("today", "24h", "7d", "2w", "all"):
        cb = serve.build_cube(base, since, "all", False)
        assert cb["rows"], since
    assert serve.build_cube(base, "7d", "all", False)["baseline"]["rows"]
    assert serve.build_cube(base, "2w", "all", False)["baseline"]["rows"]
    cb = serve.build_cube(base, "all", "all", False)
    assert {"main", "subagent", "workflow"} == {r["source"] for r in _decode(cb)}
    priced = {m["id"]: m["priced"] for m in cb["models"]}
    assert priced["mystery-model-9"] is False and priced["<synthetic>"] is False and priced["claude-opus-5"] is True


def test_components_sum_to_cost(base):
    cb = serve.build_cube(base, "all", "all", False)
    rows = _decode(cb)
    assert any(r["ct"] > 0 for r in rows)                   # web search 的工具费单独成一项
    for r in rows:
        assert math.isclose(r["ci"] + r["co"] + r["ccw"] + r["ccr"] + r["ct"], r["c"], abs_tol=1e-9)
    unk = [r for r in rows if r["model"] == "mystery-model-9"]
    assert unk and all(r["c"] == 0 and r["un"] == 1 and r["ut"] == 200 for r in unk)
    syn = [r for r in rows if r["model"] == "<synthetic>"]
    assert syn and all(r["un"] == 1 and r["ut"] == 0 for r in syn)


def test_cost_parts_match_pricing_formula():
    ts = datetime(2026, 9, 17, 2, 30, tzinfo=timezone(timedelta(hours=-4)))
    rec = UsageRecord(timestamp=ts, project="p", session_id="s", model="claude-fable-5-1", source_kind="main",
                      input_tokens=1000, output_tokens=2000, cache_5m=3000, cache_1h=4000, cache_read=500000,
                      web_search=3, web_fetch=1, cost_usd=0.0, known_price=True, message_id="m", request_id="r")
    ci, co, ccw, ccr, ct = tokens_view.cost_parts(rec)
    i, o, _ = pricing.rates_for("claude-fable-5-1")
    assert math.isclose(ci, 1000 * i / 1e6)
    assert math.isclose(co, 2000 * o / 1e6)
    assert math.isclose(ccw, (3000 * i * pricing.CACHE_WRITE_5M_MULT + 4000 * i * pricing.CACHE_WRITE_1H_MULT) / 1e6)
    assert math.isclose(ccr, 500000 * i * pricing.CACHE_READ_MULT / 1e6)
    assert math.isclose(ct, 3 * pricing.WEB_SEARCH_USD_PER_REQUEST + 1 * pricing.WEB_FETCH_USD_PER_REQUEST)
    full, known = pricing.cost_usd("claude-fable-5-1", 1000, 2000, 3000, 4000, 500000, 3, 1)
    assert known and math.isclose(ci + co + ccw + ccr + ct, full, rel_tol=1e-12)


def _rec(ts, **kw):
    d = dict(project="p", session_id="s", model="claude-opus-5", source_kind="main", input_tokens=1, output_tokens=1,
             cache_5m=0, cache_1h=0, cache_read=0, web_search=0, web_fetch=0, cost_usd=0.0, known_price=True,
             message_id=str(ts), request_id="r")
    d.update(kw)
    return UsageRecord(timestamp=ts, **d)


def test_hour_and_weekday_bucket_in_record_local_tz():
    """02:30 在 UTC-4 是周四 (2026-09-17) 的 2 点; 同一瞬间的 UTC 是 06:30 —— 必须按本地的 2 点归桶。"""
    tz = timezone(timedelta(hours=-4))
    a = datetime(2026, 9, 17, 2, 30, tzinfo=tz)
    b = datetime(2026, 9, 16, 23, 59, tzinfo=tz)      # UTC 已是 9-17, 本地仍是周三 9-16 的 23 点
    cb = tokens_view.build([_rec(a), _rec(b, message_id="b")], None, [_rec(a), _rec(b, message_id="b")])
    rows = [dict(zip(cb["cols"], r)) for r in cb["rows"]]
    got = {(cb["days"][r["d"]]["d"], cb["days"][r["d"]]["wd"], r["h"]) for r in rows}
    assert got == {("2026-09-17", 3, 2), ("2026-09-16", 2, 23)}
    assert {r["e"] for r in rows} == {int(a.timestamp() // 3600), int(b.timestamp() // 3600)}
    assert cb["prev_rows"] is None


def test_real_parser_local_bucketing(base):
    """经 parser 的记录 (本地时区) 分桶 == 用 datetime.astimezone() 算出来的本地日期/小时。"""
    cb = serve.build_cube(base, "all", "all", False)
    recs = parser.load_records(base)
    want = defaultdict(int)
    for r in recs:
        lt = r.timestamp.astimezone()
        want[(lt.date().isoformat(), lt.weekday(), lt.hour)] += 1
    got = defaultdict(int)
    for r in _decode(cb):
        got[(r["day"], r["wd"], r["h"])] += r["n"]
    assert got == want


def test_model_rank_is_window_independent(base):
    """模型的 rank (配色用) 按全部已加载记录排, 换窗口不变 —— 颜色跟着实体走。"""
    ranks = []
    for since in ("today", "7d", "all"):
        cb = serve.build_cube(base, since, "all", False)
        ranks.append({m["id"]: m["rank"] for m in cb["models"]})
    common = set(ranks[0]) & set(ranks[1]) & set(ranks[2])
    assert common and all(ranks[0][m] == ranks[1][m] == ranks[2][m] for m in common)


def test_sessions_carry_project_and_span(base):
    cb = serve.build_cube(base, "all", "all", False)
    by_id = {s["id"]: s for s in cb["sessions"]}
    a = by_id["s-alpha"]
    assert cb["projects"][a["p"]]["id"] == "alpha" and a["t0"] < a["t1"]
    assert cb["span"]["d0"] <= cb["span"]["d1"] and cb["span"]["lo"] <= cb["span"]["hi"]


def test_bad_since_is_an_error_not_a_crash(base):
    with pytest.raises(ValueError):
        serve.build_cube(base, "banana", "all", False)


# ---------------- HTTP: 读门 + 页面 ----------------

def _req(url, method="GET", headers=None):
    r = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _serve(tmp_path, cfg):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._make_handler(tmp_path, cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_cube_endpoint_behind_read_guard_in_remote_mode(base, monkeypatch):
    token = "tok-cube-123"
    monkeypatch.setattr(serve.control.plane, "token", token)
    monkeypatch.setattr(serve.control.plane, "check_token", lambda t: bool(t) and t == token)
    httpd, url = _serve(base, remote.RemoteConfig(enabled=True, hosts=frozenset({"t.example.com"})))
    try:
        code, text = _req(url + "/api/tokens/cube?since=all")
        assert code == 403 and "rows" not in text               # 没令牌: 不给数据
        code, _ = _req(url + "/api/tokens/cube", "HEAD")
        assert code == 401                                      # HEAD 也不确认端点存在
        code, _ = _req(url + "/tokens")
        assert code == 401                                      # 页面 -> 登录页
        code, text = _req(url + "/api/tokens/cube?since=all", headers={"X-Control-Token": token})
        assert code == 200 and json.loads(text)["rows"]
        code, _ = _req(url + "/api/tokens/cube?since=all",
                       headers={"X-Control-Token": token, "Host": "evil.example.com"})
        assert code == 403                                      # DNS-rebinding: Host 不在白名单照拒
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cube_endpoint_local_mode_and_page(base):
    httpd, url = _serve(base, remote.RemoteConfig())
    try:
        code, text = _req(url + "/api/tokens/cube?since=7d&scope=main&vscode_only=1")
        d = json.loads(text)
        assert code == 200 and d["scope"] == "main" and d["vscode_only"] is True and d["window"] == "7d"
        assert _req(url + "/api/tokens/cube", "HEAD")[0] == 200
        code, text = _req(url + "/api/tokens/cube?since=banana")
        assert code == 500 and "error" in json.loads(text)       # 坏参数 -> JSON 错误, 服务不崩
        code, html = _req(url + "/tokens")
        assert code == 200
        assert "__BASE__" not in html and "__NAV__" not in html
        assert '<a href="/tokens" class="active">' in html        # 统一导航, 当前页高亮
        assert "/api/tokens/cube" in html
        code, wf = _req(url + "/workflow")
        assert code == 200 and '<a href="/workflow" class="active">' in wf
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ 省钱 S2: 上下文税 / 离开后重建 (都是已有成本的一部分)

def test_savings_columns_are_slices_of_existing_cost(base):
    cube = serve.build_cube(base, "7d", "all", False)
    rows = _decode(cube)
    for r in rows:                                            # tx 是缓存读的一部分, rb 是缓存写的一部分
        assert r["tx"] <= r["ccr"] + 1e-9 and r["rb"] <= r["ccw"] + 1e-9 and r["rn"] in (0, 1)
    d = [r for r in rows if r["session"] == "s-delta"]
    rate = pricing.rates_for("claude-opus-5")[0]
    assert math.isclose(sum(r["tx"] for r in d), 30_000 * rate * pricing.CACHE_READ_MULT / 1e6, rel_tol=1e-9)
    assert sum(r["rn"] for r in d) == 1
    assert math.isclose(sum(r["rb"] for r in d),
                        182_000 * (pricing.CACHE_WRITE_1H_MULT - pricing.CACHE_READ_MULT) * rate / 1e6, rel_tol=1e-9)
    assert sum(r["rn"] for r in rows if r["session"] != "s-delta") == 0     # 别的会话: 子 agent 第一轮写缓存不算重建
    assert cube["save"] == {"line": 150_000}


def test_rebuild_sees_previous_turn_outside_the_window(base):
    """重建要看上一轮: 上一轮在窗口外 (昨天) 也要认得出来 —— 标记是在全部记录上判的。"""
    recs = parser.load_records(base, {"main", "subagent", "workflow"}, False)
    d = sorted((r for r in recs if r.session_id == "s-delta"), key=lambda r: r.timestamp)
    cube = tokens_view.build([d[-1]], None, recs)            # 窗口里只有重建那一轮
    r = dict(zip(cube["cols"], cube["rows"][0]))
    assert r["rn"] == 1 and r["rb"] > 0
    assert tokens_view.build([d[-1]], None, [d[-1]])["rows"][0][cube["cols"].index("rn")] == 0   # 看不到上一轮: 不猜
