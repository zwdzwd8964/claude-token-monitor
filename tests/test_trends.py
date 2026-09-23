"""改动与风险 S3 的单测: 趋势 (vs 上一周期 + 小趋势线) / 偏慢按命令族 / MCP 失败原因归类 / 会话简报 / 风险事件源 /
/api/sessions 挂简报 / 统一导航。

完成判据①「趋势数字与同窗口的汇总自洽」在这里落成断言: 本期指标 == 概览里同一批任务算出来的数, 各桶相加 == 本期,
每个桶点开列出的任务数 == 桶上的数; 行尾小趋势线各段相加 == 那一行的数。
"""

import json
import time
from datetime import datetime

import pytest

from tokmon import activity, events, notify, parser, procmon, serve, trace
from tokmon.event_sources import risk_source
from test_trace import SID, asst, human, result, tool_use, usage, write_jsonl
from test_trace_s3 import s3  # noqa: F401  (fixture)


# ------------------------------------------------------------------ 上一周期 / 分桶

def test_prev_window_matches_tokens_convention():
    midnight = datetime(2026, 9, 20).timestamp()
    now = midnight + 5.5 * 3600
    lo, hi = trace.prev_window(midnight, now)                            # 今天 -> 昨天 00:00 到昨天的同一时刻
    assert lo == datetime(2026, 9, 19).timestamp() and hi == lo + 5.5 * 3600
    since = now - 7 * 86400
    assert trace.prev_window(since, now) == (now - 14 * 86400, since)     # 7 天 -> 紧挨着的上一个 7 天
    assert trace.prev_window(None, now) is None                          # 全部时间没有上一周期


def test_bucket_granularity_follows_range():
    midnight = datetime(2026, 9, 20).timestamp()
    unit, st = trace._bucket_starts(midnight, midnight + 5.5 * 3600, None)
    assert unit == "hour" and len(st) == 6 and st[0] == midnight
    since = datetime(2026, 9, 10, 15, 0).timestamp()
    unit, st = trace._bucket_starts(since, since + 7 * 86400, None)
    assert unit == "day" and st[0] == datetime(2026, 9, 10).timestamp() and len(st) == 8
    unit, st = trace._bucket_starts(since, since + 30 * 86400, None)
    assert unit == "week" and all(datetime.fromtimestamp(t).weekday() == 0 for t in st)   # 周一起
    first = datetime(2026, 9, 2, 13).timestamp()                                            # 周三
    unit, st = trace._bucket_starts(None, first + 20 * 86400, first)
    assert unit == "week" and st[0] == datetime(2026, 8, 31).timestamp()


def _row(task, t0, calls=10, fails=0, rep=0, slow=0, mcp=(0, 0), tokens=1000, active=60.0, risk=False,
         chg=False, unverified=False, tools=None, skills=(), rules=()):
    return {"task": task, "t0": t0, "calls": calls, "fails": fails, "rep": rep, "slow": slow,
            "mcp_calls": mcp[0], "mcp_fails": mcp[1], "tokens": tokens, "active": active, "risk": risk,
            "rules": list(rules), "chg": chg, "unverified": unverified, "tools": tools or {"Bash": calls},
            "mcp": {"railway": list(mcp)} if mcp[0] else {}, "skills": list(skills), "project": "demo"}


def test_build_trends_buckets_add_up_to_window():
    since = datetime(2026, 9, 10, 15, 0).timestamp()
    now = since + 7 * 86400
    day = lambda d, h=12: datetime(2026, 9, d, h).timestamp()
    rows = [_row("a", day(10, 16), calls=40, fails=4, risk=True, rules=["destructive"], chg=True, unverified=True),
            _row("b", day(10, 18), calls=20, fails=0, mcp=(6, 3), skills=["ops"]),
            _row("c", day(12), calls=5, fails=5, chg=True),
            _row("d", day(12, 13), calls=5, rep=2, tools={"Bash": 3, "Read": 2}),
            _row("e", day(12, 14), calls=30, slow=3, skills=["ops"], rules=["thrash", "destructive"], risk=True),
            _row("f", day(16), calls=1, tokens=None, active=None)]
    idx = {"tasks": [{"task": r["task"]} for r in rows]}
    T = trace.build_trends(rows, since, now, [_row("p", since - 86400, calls=50, fails=1)], idx)
    assert T["unit"] == "day" and len(T["buckets"]) == 8
    B = T["buckets"]
    assert sum(b["tasks"] for b in B) == 6 and sum(b["calls"] for b in B) == 101 == T["cur"]["calls"]
    assert [b["tasks"] for b in B] == [2, 0, 3, 0, 0, 0, 1, 0]
    assert T["cur"]["m"] == trace._window_metrics(rows)["m"]
    assert T["cur"]["m"]["fail_rate"] == pytest.approx(9 / 101)
    assert T["cur"]["m"]["risk_share"] == pytest.approx(2 / 6)
    assert T["cur"]["m"]["unverified_share"] == pytest.approx(1 / 2)
    assert T["cur"]["m"]["mcp_fail_rate"] == pytest.approx(3 / 6)
    assert T["cur"]["m"]["tok_median"] == 1000 and T["cur"]["sparse"]["unverified_share"]     # 只有 2 个有改动的任务 -> 样本不足
    assert B[0]["m"]["fail_rate"] == pytest.approx(4 / 60) and not B[0]["sparse"]["fail_rate"]
    assert B[0]["sparse"]["risk_share"]                                   # 这一天只有 2 个任务
    assert B[6]["sparse"]["fail_rate"] and B[1]["m"]["fail_rate"] is None  # 1 次调用: 画空心; 空桶: 不画
    for b, want in zip(B, [{"a", "b"}, set(), {"c", "d", "e"}, set(), set(), set(), {"f"}, set()]):
        assert {x["task"] for x in idx[b["ref"]]} == want                 # 点一个桶 = 那个桶的任务, 条数 == 桶上的数
    assert T["prev"]["tasks"] == 1 and T["prev_window"] == (since - 7 * 86400, since)
    sp = T["spark"]
    assert sum(sp["tool"]["Bash"]) == 99 and sum(sp["tool"]["Read"]) == 2
    assert sp["skill"]["ops"] == [1, 0, 1, 0, 0, 0, 0, 0] and sp["rule"]["destructive"] == [1, 0, 1, 0, 0, 0, 0, 0]
    assert sp["mcp"]["railway"][0] == [6, 3]


def test_stats_trends_consistent_with_overview(s3):
    """判据①: 趋势与同一窗口的汇总自洽 —— 同一批任务、同一套口径, 抽查对账。"""
    res = trace.stats(s3)
    ov, T = res["overview"], res["trends"]
    assert T["cur"]["tasks"] == ov["tasks"] == sum(b["tasks"] for b in T["buckets"])
    assert T["cur"]["calls"] == ov["calls"] == sum(b["calls"] for b in T["buckets"])
    assert T["cur"]["m"]["fail_rate"] == pytest.approx(ov["fail"] / ov["calls"])
    assert T["prev"] is None and T["prev_window"] is None                # 全部时间: 没有上一周期
    for b in T["buckets"]:
        if b["tasks"]:
            d = trace.stats_refs(res["stamp"], b["ref"], limit=500)
            assert d["total"] == b["tasks"] == len(d["items"])
    nb = len(T["buckets"])
    for r in res["tools"]:
        assert len(r["spark"]) == nb and sum(r["spark"]) == r["calls"], r["name"]
    for r in res["skills"]:
        assert sum(r["spark"]) == r["tasks"], r["name"]
    for m in res["mcp"]:
        assert sum(c for c, _ in m["spark"]) == m["calls"] and sum(f for _, f in m["spark"]) == m["fail"]
    for r in res["risks"]:
        assert sum(r["spark"]) == r["tasks"], r["rule"]
    assert "spark" not in T                                              # 名字不单独出现在趋势里 (脱敏只管各行)


def test_stats_prev_window_uses_until(tmp_path, monkeypatch):
    """上一周期只收 [上一段开始, 本段开始) 里的任务, 本期的任务不会被算两遍。"""
    seen = []
    real = trace._iter_built

    def spy(base, since, project, rs, until=None):
        seen.append((since, until))
        return real(base, since, project, rs, until=until)
    monkeypatch.setattr(trace, "_iter_built", spy)
    monkeypatch.setattr(trace, "_STATS", {})
    now = time.time()
    trace.stats(tmp_path, since=now - 7 * 86400, fresh=True)
    (s0, u0), = [x for x in seen if x[1] is None]                       # 本期
    (s1, u1), = [x for x in seen if x[1] is not None]                   # 上一周期
    assert len(seen) == 2 and u1 == pytest.approx(s0) and s1 == pytest.approx(s0 - (now - s0), abs=5)


# ------------------------------------------------------------------ 偏慢按命令族 / 失败原因归类

@pytest.mark.parametrize("cmd,want", [
    ("uv run pytest -q tests", "Bash:uv run pytest"),
    ("git status", "Bash:git status"),
    ("cd /c/repo && git push origin main", "Bash:git push"),
    ("FOO=1 npm run build", "Bash:npm run build"),
    ("python -m pytest -x", "Bash:python -m pytest"),
    ("python scripts/tool.py --x", "Bash:python tool.py"),
    ("ls -la", "Bash:ls"),
])
def test_cmd_family(cmd, want):
    assert trace.cmd_family("Bash", {"command": cmd}) == want


def test_cmd_family_other_tools_by_name():
    assert trace.cmd_family("Read", {"file_path": "a.py"}) == "Read"
    assert trace.cmd_family("PowerShell", {"command": "Get-ChildItem"}) == "PowerShell:get-childitem"   # PowerShell 不分大小写, 命令名统一小写


def test_error_cause_groups_same_error():
    a = trace.error_cause("Unauthorized. Please run `railway login` again.\nat https://x.example/y")
    b = trace.error_cause("Unauthorized. Please run `railway login` again.")
    assert a == b
    c1 = trace.error_cause("Error: request 3f9a2b1c-1111-2222-3333-444455556666 failed after 30s at C:\\work\\a.py")
    c2 = trace.error_cause("Error: request 99992b1c-aaaa-2222-3333-444455556666 failed after 12s at C:\\other\\b.py")
    assert c1 == c2 and "<id>" in c1 and "<path>" in c1 and "N" in c1 and "work" not in c1
    assert trace.error_cause("") == trace.error_cause(None) == "（没有返回内容）"
    assert trace.error_cause("Timeout after https://api.x.io/v1/a?k=1") == "Timeout after <url>"


def test_mcp_causes_sum_to_failures_and_trace(s3):
    res = trace.stats(s3)
    m = res["mcp"][0]
    assert sum(c["n"] for c in m["causes"]) == m["fail"] == 1
    c = m["causes"][0]
    assert c["cause"].startswith("Unauthorized") and c["tools"] == ["get_logs"]
    d = trace.stats_refs(res["stamp"], c["ref"], limit=50)
    assert d["total"] == c["n"] and d["items"][0]["node"] == "lg"
    blob = json.dumps(serve._wf_stats(s3, {"since": ["all"]})["mcp"], ensure_ascii=False)
    assert "S3CRET" not in blob                                           # 归类文字和示例原文都打码


# ------------------------------------------------------------------ 会话简报 (/sessions 角标 + 风险事件源共用)

@pytest.fixture
def brief_env(tmp_path, monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {}), ("_BRIEF", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(trace, "BRIEF_MIN_AGE", 0.0)
    monkeypatch.setattr(parser, "_file_cache", {})
    main = tmp_path / "projects" / "c--Users-u--vscode-demo" / f"{SID}.jsonl"
    base_lines = [
        human(0, "P1", "清一下构建产物再跑测试"),
        asst(1, "m1", "r1", tool_use("b1", "Bash", {"command": "rm -rf build/secret-dir"}), usage(out=5)),
        result(2, "b1", ""),
    ]
    write_jsonl(main, base_lines)
    return main, base_lines


def _spike_lines(t0=10):
    out = []
    for i in range(3):
        out += [asst(t0 + 2 * i, f"mf{i}", f"rf{i}", tool_use(f"f{i}", "Bash", {"command": f"npm test -- --case {i}"}), usage(out=5)),
                result(t0 + 2 * i + 1, f"f{i}", "FAIL", err=True)]
    return out


def test_session_brief_risks_have_key_and_logical_time(brief_env):
    main, _ = brief_env
    b = trace.session_brief(main)
    assert b["task"] and b["running"] is False
    (r,) = b["risks"]
    assert r["rule"] == "destructive" and r["label"] == "破坏性操作：rm -rf" and r["n"] == 1
    assert r["key"].startswith("destructive:")
    from test_trace import T0
    assert r["at"] == pytest.approx(T0 + 2)                                # 事件时间 = 证据发生的时刻, 不是 now()
    assert b["changes"] == 1 and b["files"] == 1
    assert trace.session_brief(main) is b                                  # 文件没变: 直接复用
    assert trace.session_brief(main.parent / "nope.jsonl") is None


def test_session_brief_throttles_busy_session(brief_env, monkeypatch):
    main, lines = brief_env
    monkeypatch.setattr(trace, "BRIEF_MIN_AGE", 60.0)
    b1 = trace.session_brief(main, running=True)
    write_jsonl(main, lines + _spike_lines())
    assert trace.session_brief(main, running=True) is b1                   # 60 秒内: 正在跑的会话不重算
    b3 = trace.session_brief(main, running=False)                          # 状态变了: 重算
    assert {r["rule"] for r in b3["risks"]} == {"destructive", "error-spike"}


# ------------------------------------------------------------------ 风险事件源

@pytest.fixture
def pump(brief_env, monkeypatch):
    main, lines = brief_env
    bus = events.EventBus()
    monkeypatch.setattr(risk_source, "bus", bus)
    risk_source.reset_for_test()
    rows = [{"session_id": SID, "project": "demo", "state": "WORKING", "file": str(main)},
            {"session_id": "old", "project": "demo", "state": "CLOSED", "file": str(main)}]
    monkeypatch.setattr(activity, "snapshot", lambda base=None, live=None: {"sessions": rows})
    yield main, lines, bus
    risk_source.reset_for_test()


def test_risk_source_cold_start_silent_then_emits_new_marker_once(pump):
    main, lines, bus = pump
    assert risk_source.tick() == 0 and bus.since(0)["events"] == []     # 冷启动: 已经在那儿的 rm -rf 不当新事件
    write_jsonl(main, lines + _spike_lines())
    assert risk_source.tick() == 1
    (ev,) = bus.since(0)["events"]
    assert ev["type"] == "ERROR_SPIKE" and ev["severity"] == "info" and ev["pillar"] == "trace"
    assert ev["payload"] == {"rule": "error-spike", "count": 3, "kind": None}
    assert ev["session"] == SID and ev["dedup_key"].startswith("ERROR_SPIKE:")
    blob = json.dumps(ev, ensure_ascii=False)
    assert "build" not in blob and "npm" not in blob and "secret-dir" not in blob   # §6: 不带路径 / 命令
    assert risk_source.tick() == 0                                          # 同一个标记只发一次


def test_risk_source_maps_rules_and_skips_outside():
    row = {"session_id": "s", "project": "p"}
    brief = {"task": "T", "risks": [
        {"rule": "destructive", "label": "破坏性操作：git reset --hard", "n": 2, "key": "destructive:1", "at": 5.0},
        {"rule": "thrash", "label": "a.py 改了 3 轮都没通过", "n": 3, "key": "thrash:2", "at": 6.0},
        {"rule": "large-task", "label": "这次任务动了 250 个文件", "n": 250, "key": "large-task:3", "at": None},
        {"rule": "outside", "label": "改了工作目录以外的 1 个文件", "n": 1, "key": "outside:4", "at": 7.0}]}
    out = risk_source.derive(row, brief, now=100.0)
    types = [e.type for _, e in out]
    assert types == ["DESTRUCTIVE_OP", "REPEATED_FILE_EDIT", "LARGE_DIFF"]   # 界外不进总线
    d, t, lg = (e for _, e in out)
    assert d.payload == {"rule": "destructive", "count": 2, "kind": "git reset --hard"} and d.timestamp == 5.0
    assert t.payload == {"rule": "thrash", "count": 3, "kind": None} and "a.py" not in json.dumps(t.to_dict())
    assert lg.timestamp == 100.0 and lg.detected_at == 100.0               # 没有单步证据: 退回观察时刻
    assert all(e.severity == "info" for _, e in out)


def test_new_event_types_are_info_and_never_notify():
    for t in ("DESTRUCTIVE_OP", "ERROR_SPIKE", "REPEATED_FILE_EDIT", "LARGE_DIFF"):
        assert t in events.EVENT_TYPES and events._DEFAULT_SEVERITY[t] == "info"
    ev = events.Event.make("DESTRUCTIVE_OP", pillar="trace", kind="rm -rf", count=1, rule="destructive",
                           command="rm -rf /secret", path="/secret").to_dict()
    assert ev["payload"] == {"kind": "rm -rf", "count": 1, "rule": "destructive"}   # allow-list 挡住命令 / 路径
    assert notify.summarize(ev).startswith("破坏性操作")
    for t, p in (("ERROR_SPIKE", {"count": 4}), ("REPEATED_FILE_EDIT", {"count": 3}),
                 ("LARGE_DIFF", {"rule": "large-task", "count": 250}), ("LARGE_DIFF", {"rule": "large-edit", "count": 2})):
        s = notify.summarize({"type": t, "payload": p, "project": "demo"})
        assert str(p["count"]) in s and "demo" in s


# ------------------------------------------------------------------ /api/sessions 挂简报 / 统一导航

def test_sessions_api_joins_briefs_without_mutating_shared_snapshot(brief_env, monkeypatch):
    main, _ = brief_env
    shared = {"sessions": [{"session_id": SID, "state": "AWAITING_USER", "file": str(main), "last_activity_age_s": 30},
                           {"session_id": "x", "state": "CLOSED", "file": str(main), "last_activity_age_s": 90000}],
              "counts": {"AWAITING_USER": 1}}
    monkeypatch.setattr(activity, "snapshot", lambda base=None, live=None: shared)
    monkeypatch.setattr(procmon, "live_claude_index", lambda: {})
    out = serve._sessions_with_briefs(None)
    assert "workflow" not in out["sessions"][0]                            # 基线还没热好: 先不挂 (不让高频接口触发冷启动)
    trace.baseline(main.parent.parent, ttl=0)
    out = serve._sessions_with_briefs(None)
    wf = out["sessions"][0]["workflow"]
    assert wf["risks"] == [{"rule": "destructive", "label": "破坏性操作：rm -rf", "n": 1}] and wf["changes"] == 1
    assert "workflow" not in out["sessions"][1]                            # 一天没动静的老会话不算
    assert all("workflow" not in r for r in shared["sessions"]) and out is not shared   # 共享缓存原样不动
    assert out["counts"] == shared["counts"]


def test_nav_centralized_with_more_menu():
    h = serve._nav_html("/doctor")
    assert '<a href="/doctor" class="active">体检</a>' in h and "更多 · 体检" in h and '<summary class="active">' in h
    h = serve._nav_html("/sessions")
    assert '<a href="/sessions" class="active">Session 状态</a>' in h and "<summary>更多 ▾</summary>" in h
    main = h.split("<details")[0]
    assert "/doctor" not in main and "/backtest" not in main and "/billing" not in main   # 低频页收进「更多」
    for name in ("PROC_PAGE", "SESS_PAGE", "NOTIFY_PAGE", "CONTROL_PAGE", "DOCTOR_PAGE", "BACKTEST_PAGE",
                 "BILLING_PAGE", "WORKFLOW_PAGE"):
        page = getattr(serve, name)
        assert "__NAV__" not in page and '<details class="more">' in page, name
