"""S3 (统计子页) 的单测: 排行 / MCP 健康 / 跨任务对照 + 「每个数字都能追溯到同样条数的明细」。

合成会话里 3 个任务:
- P1: skill ops (被你的插话切成两段; 里面派了一个同样带 ops 标记的子 agent —— 嵌套同名, 不许重复计数),
      一次失败后重试的 npm test, 一个失败的 MCP 调用 (返回里带密钥), 一次向你提问 (等你, 不是工具耗时);
- P2: workflow, 阶段 Review (review:auth / review:perf) + Fix (fix:auth), 其中一个调用失败;
- P3: skill ops 又用了一次 (提问里带密钥)。
"""

import json

import pytest

from tokmon import parser, serve, trace
from test_trace import SID, T0, asst, human, iso, result, tool_use, usage, write_jsonl

SECRET = "S3CRET-VALUE-123456789"


@pytest.fixture
def s3(tmp_path, monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {}), ("_STATS", {}), ("_STAMPS", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})
    monkeypatch.setattr(serve, "_wf_running", lambda base: set())
    monkeypatch.setattr(serve, "_wf_known_secrets", lambda: (SECRET,))
    base = tmp_path / "projects"
    proj = base / "c--Users-u--vscode-demo"
    sdir = proj / SID
    write_jsonl(proj / f"{SID}.jsonl", [
        human(0, "P1", "上线前检查一遍"),
        asst(1, "m1", "r1", tool_use("sk1", "Skill", {"skill": "ops"}), usage(out=5)),
        result(1.5, "sk1", "Launching skill: ops"),
        asst(2, "m2", "r2", tool_use("b1", "Bash", {"command": "npm test"}), usage(out=5), skill="ops"),
        result(4, "b1", "FAIL", err=True),
        asst(5, "m3", "r3", tool_use("b2", "Bash", {"command": "npm test"}), usage(out=5), skill="ops"),
        result(7, "b2", "PASS"),
        asst(8, "m4", "r4", tool_use("ag", "Agent", {"description": "Explore the repo", "prompt": "look around"}),
             usage(out=5), skill="ops"),
        result(20, "ag", "found it", tur={"status": "completed", "agentId": "e1"}),
        {"type": "attachment", "timestamp": iso(21), "sessionId": SID,
         "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "顺便看下日志"}},
        asst(22, "m5", "r5", tool_use("lg", "mcp__railway__get_logs", {"lines": 50}), usage(out=5), skill="ops"),
        result(23, "lg", f"Unauthorized: bad token {SECRET}", err=True),
        asst(24, "m6", "r6", tool_use("ask", "AskUserQuestion", {"questions": [{"question": "上线吗?"}]}), usage(out=5)),
        result(84, "ask", "上"),
        asst(85, "m7", "r7", {"type": "text", "text": "好"}, usage(out=5)),

        human(200, "P2", "跑一遍 review"),
        asst(201, "m8", "r8", tool_use("wf", "Workflow", {"name": "rev"}), usage(out=5)),
        result(202, "wf", "launched", tur={"status": "async_launched", "runId": "wf_r", "workflowName": "rev"}),
        asst(215, "m9", "r9", {"type": "text", "text": "review 完成"}, usage(out=5)),

        human(300, "P3", f"再跑一次 ops, 令牌是 {SECRET}"),
        asst(301, "m10", "r10", tool_use("sk3", "Skill", {"skill": "ops"}), usage(out=5)),
        result(301.5, "sk3", "Launching skill: ops"),
        asst(302, "m11", "r11", tool_use("b3", "Bash", {"command": "ls"}), usage(out=5), skill="ops"),
        result(303, "b3", "a b"),
        asst(304, "m12", "r12", {"type": "text", "text": "ok"}, usage(out=5)),
    ])
    # P1 里的同步子 agent: 它的行也带 ops 标记 -> 回放里是一个嵌套在 ops 里的 ops (延续) 节点
    write_jsonl(sdir / "subagents" / "agent-e1.jsonl", [
        {"type": "user", "timestamp": iso(8.5), "sessionId": SID, "agentId": "e1", "isSidechain": True,
         "message": {"role": "user", "content": "look around"}},
        asst(9, "me1", "re1", tool_use("g1", "Grep", {"pattern": "TODO"}), usage(out=7), skill="ops", agent="e1"),
        result(10, "g1", "a.js:3", agent="e1"),
        asst(11, "me2", "re2", {"type": "text", "text": "found it"}, usage(out=3), skill="ops", agent="e1"),
    ])
    (sdir / "subagents" / "agent-e1.meta.json").write_text(json.dumps({"agentType": "Explore",
                                                                        "description": "Explore the repo"}), encoding="utf-8")
    rdir = sdir / "subagents" / "workflows" / "wf_r"
    for aid, desc, phase, tid, name, t, err in (("r1", "review:auth", "Review", "rg", "Grep", 203, False),
                                                ("r2", "review:perf", "Review", "rr", "Read", 203, True),
                                                ("f1", "fix:auth", "Fix", "fe", "Edit", 208, False)):
        write_jsonl(rdir / f"agent-{aid}.jsonl", [
            {"type": "user", "timestamp": iso(t), "sessionId": SID, "agentId": aid, "message": {"role": "user", "content": desc}},
            asst(t + 1, f"m-{aid}", f"r-{aid}", tool_use(tid, name, {"pattern": "x", "file_path": "a.py"}), usage(out=9), agent=aid),
            result(t + 3, tid, "boom" if err else "ok", err=err, agent=aid),
        ])
        (rdir / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": "workflow-subagent", "description": desc,
                                                                 "workflowPhase": phase}), encoding="utf-8")
    scripts = sdir / "workflows" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "rev-wf_r.js").write_text("export const meta = {\n  name: 'rev',\n  phases: [{ title: 'Review' }, { title: 'Fix' }],\n}\n",
                                         encoding="utf-8")
    return base


def _numbers(res):
    """统计里每一个「可点的计数」 -> (说明, 数字, 索引键, 标记)。页面上带 data-n 的就是这些。"""
    ov = res["overview"]
    out = [("ov.tasks", ov["tasks"], "tasks", None), ("ov.calls", ov["calls"], "all", None),
           ("ov.fail", ov["fail"], "all", "fail"), ("ov.partial", ov["partial"], "tasks", "partial")]
    out += [(f"ov.{f}", ov["flags"][f], "all", f) for f in trace.COUNT_FLAGS]
    for r in res["tools"]:
        out += [(f"tool {r['name']} calls", r["calls"], r["ref"], None), (f"tool {r['name']} tasks", r["tasks"], r["ref_occ"], None),
                (f"tool {r['name']} fail", r["fail"], r["ref"], "fail")]
        out += [(f"tool {r['name']} {f}", r[f], r["ref"], f) for f in trace.COUNT_FLAGS]
    for r in res["skills"]:
        out += [(f"skill {r['name']} tasks", r["tasks"], r["ref_occ"], None),
                (f"skill {r['name']} calls", r["calls"], r["ref_calls"], None),
                (f"skill {r['name']} fail", r["fail"], r["ref_calls"], "fail")]
    for m in res["mcp"]:
        out += [(f"mcp {m['server']} calls", m["calls"], m["ref"], None), (f"mcp {m['server']} fail", m["fail"], m["ref"], "fail"),
                (f"mcp {m['server']} tasks", m["tasks"], m["ref_occ"], None)]
        for x in m["tools"]:
            out += [(f"mcp {x['tool']} calls", x["calls"], x["ref"], None), (f"mcp {x['tool']} fail", x["fail"], x["ref"], "fail")]
    for dim, rows in res["compare"].items():
        for r in rows:
            out += [(f"cmp {dim} {r['name']} n", r["n"], r["ref"], None),
                    (f"cmp {dim} {r['name']} fail", r["fail_occ"], r["ref"], "fail")]
    return out


# ------------------------------------------------------------------ 纯函数

@pytest.mark.parametrize("label,typ,want", [
    ("review:auth", "workflow-subagent", ("review", "prefix")),
    ("Review: all", None, ("review", "prefix")),
    ("修复：登录", None, ("修复", "prefix")),                      # 全角冒号
    ("Fix bug: parser", "Explore", ("Explore", "type")),          # 前缀里有空格 -> 不是角色前缀, 按类型
    ("https://example.com", None, (None, None)),                   # 网址不是角色
    ("plain label", "workflow-subagent", (None, None)),            # workflow 子 agent 的通用类型不算角色
    ("a" * 30 + ":x", None, (None, None)),                         # 太长的前缀不当角色
])
def test_role_of(label, typ, want):
    assert trace.role_of(label, typ) == want


def test_tool_key_and_median():
    assert trace.tool_key({"cat": "mcp", "name": "mcp__railway__get_logs", "meta": {"server": "railway"}}) == "mcp·railway"
    assert trace.tool_key({"cat": "builtin", "name": "Bash"}) == "Bash"
    assert trace._median([]) is None and trace._median([3, 1, 2]) == 2 and trace._median([1, 2, 3, 4]) == 2.5


# ------------------------------------------------------------------ 聚合

def test_overview_and_tool_rows(s3):
    res = trace.stats(s3)
    ov = res["overview"]
    assert ov["tasks"] == 3 and ov["calls"] == 13 and ov["fail"] == 3 and ov["flags"]["retry"] == 1
    tools = {r["name"]: r for r in res["tools"]}
    bash = tools["Bash"]
    assert (bash["calls"], bash["tasks"], bash["fail"], bash["retry"]) == (3, 2, 1, 1)
    assert bash["time"] == pytest.approx(2 + 2 + 1) and bash["p50"] == pytest.approx(2)
    assert tools["AskUserQuestion"]["cat"] == "ask"               # 等你: 页面单列, 不参加排行
    assert tools["Agent"]["nested"] and tools["Workflow"]["nested"]
    assert tools["mcp·railway"]["fail"] == 1                      # MCP 按 server 合成一行
    assert res["tools"][0]["name"] == "AskUserQuestion"           # 数据里如实按耗时排 (60s); 页面把它挪出排行


def test_skill_rows_merge_segments_and_dedupe_nested(s3):
    res = trace.stats(s3)
    ops = {r["name"]: r for r in res["skills"]}["ops"]
    assert ops["tasks"] == 2
    # P1: Skill / npm test x2 / Agent / 子 agent 的 Grep / 插话后的 MCP (延续段) = 6; P3: Skill / ls = 2
    assert ops["calls"] == 8 and ops["fail"] == 2
    t1 = next(t for t in trace.session_tasks(next(s3.rglob(f"{SID}.jsonl")))[0] if t["id"] == "P1")
    tree = trace.build_task(t1)["tree"]
    outer = []                                                     # 最外层的 ops 节点 (嵌套的那个不算)

    def visit(n, inside):
        if n["kind"] == "skill" and n["label"] == "ops" and not inside:
            outer.append(n)
        for c in n.get("children") or []:
            visit(c, inside or (n["kind"] == "skill" and n["label"] == "ops"))
    visit(tree, False)
    assert len(outer) == 2                                         # 主段 + 插话后的延续段
    occ = {o["task"]: o for o in trace.stats_refs(res["stamp"], ops["ref_occ"])["items"]}
    assert occ["P1"]["tokens"] == sum(n["tokens"]["total"] for n in outer)
    assert ops["tokens"] == occ["P1"]["tokens"] + occ["P3"]["tokens"] and ops["untagged"] == 0


def test_compare_dimensions(s3):
    cmp = trace.stats(s3)["compare"]
    phase = {r["name"]: r for r in cmp["phase"]}
    assert phase["Review"]["n"] == 1 and phase["Review"]["fail_occ"] == 1 and phase["Fix"]["n"] == 1
    assert phase["Review"]["points"][0]["sub"] == "rev"            # 点的说明: 属于哪个 workflow
    role = {r["name"]: r for r in cmp["role"]}
    assert role["review"]["n"] == 2 and role["review"]["src"] == "prefix" and role["fix"]["n"] == 1
    assert role["Explore"]["n"] == 1 and role["Explore"]["src"] == "type"
    skill = {r["name"]: r for r in cmp["skill"]}
    assert skill["ops"]["n"] == 2 and skill["ops"]["tasks"] == 2
    tool = {r["name"]: r for r in cmp["tool"]}
    assert tool["Bash"]["n"] == 2 and tool["Bash"]["slowest"]["task"] == "P1"
    assert tool["Bash"]["priciest"] is None                        # 工具调用没有 token 真值
    rev = role["review"]
    assert rev["slowest"]["dur"] == max(p["dur"] for p in rev["points"])


def test_mcp_health_recent_errors(s3):
    m = trace.stats(s3)["mcp"][0]
    assert m["server"] == "railway" and m["calls"] == 1 and m["fail"] == 1
    assert m["tools"][0]["tool"] == "get_logs" and m["tools"][0]["fail"] == 1
    e = m["errors"][0]
    assert e["task"] == "P1" and e["node"] == "lg" and "Unauthorized" in e["excerpt"]


# ------------------------------------------------------------------ 追溯: 数字 == 明细条数, 明细 -> 回放节点

def test_every_number_traces_to_same_count(s3):
    res = trace.stats(s3)
    bad = []
    for label, n, ref, flag in _numbers(res):
        d = trace.stats_refs(res["stamp"], ref, flag=flag, limit=500)
        if d is None or d["total"] != n or len(d["items"]) != n:
            bad.append((label, n, d and d["total"]))
    assert bad == []


def test_drill_items_point_at_real_tree_nodes(s3):
    res = trace.stats(s3)
    trees = {}
    for label, n, ref, flag in _numbers(res):
        for x in trace.stats_refs(res["stamp"], ref, flag=flag, limit=500)["items"]:
            if x["task"] not in trees:
                trees[x["task"]] = {m["id"] for m in trace.walk([trace.get_task(x["task"], s3)["tree"]])}
            assert x["node"] in trees[x["task"]], (label, x)


def test_drill_filter_sort_and_page(s3):
    res = trace.stats(s3)
    bash = {r["name"]: r for r in res["tools"]}["Bash"]
    st = res["stamp"]
    assert [x["node"] for x in trace.stats_refs(st, bash["ref"], flag="fail")["items"]] == ["b1"]
    assert [x["node"] for x in trace.stats_refs(st, bash["ref"], flag="retry")["items"]] == ["b2"]
    by_dur = trace.stats_refs(st, bash["ref"], sort="dur")["items"]
    assert [x["dur"] for x in by_dur] == sorted([x["dur"] for x in by_dur], reverse=True)
    by_time = trace.stats_refs(st, "all")["items"]
    assert by_time[0]["task"] == "P3"                              # 默认: 最新的在前
    page = trace.stats_refs(st, "all", offset=10, limit=2)
    assert page["total"] == 13 and len(page["items"]) == 2 and page["offset"] == 10
    assert set(page["tasks"]) == {x["task"] for x in page["items"]}  # 只带这一页用到的任务
    assert trace.stats_refs(st, "tool:nope")["total"] == 0


def test_stamp_cache_fresh_and_eviction(s3):
    a = trace.stats(s3)
    assert trace.stats(s3) is a                                   # TTL 内复用同一份
    b = trace.stats(s3, fresh=True)
    assert b["stamp"] != a["stamp"]
    assert trace.stats_refs(a["stamp"], "all")["total"] == 13     # 旧的那份还留着: 页面上的数字照样能追溯
    for _ in range(trace.STAMP_KEEP):
        trace.stats(s3, fresh=True)
    assert trace.stats_refs(a["stamp"], "all") is None            # 超出保留份数 -> 过期
    assert trace.stats(s3, project="别的项目")["overview"]["tasks"] == 0
    for i in range(10):                                           # 换很多个窗口: 登记表跟着清, 不会无限长
        trace.stats(s3, project=f"p{i}")
    assert len(trace._STATS) <= trace.STAMP_KEEP and len(trace._STAMPS) <= trace.STAMP_KEEP


def test_stats_follow_same_filter_as_list(s3):
    """统计和左栏列表用同一个遍历 (_iter_built): 同样的时间窗口 / 项目 -> 同样的任务集合。"""
    assert trace.stats(s3)["overview"]["tasks"] == len(trace.list_tasks(s3)) == 3
    since = T0 + 250                                              # P2 与 P3 之间
    assert trace.stats(s3, since=since)["overview"]["tasks"] == len(trace.list_tasks(s3, since=since)) == 1
    proj = trace.list_tasks(s3)[0]["project"]
    assert trace.stats(s3, project=proj)["overview"]["tasks"] == 3


# ------------------------------------------------------------------ serve 层: 脱敏 / 计价 / 过期重算

def test_serve_stats_redacts_and_prices(s3):
    d = serve._wf_stats(s3, {"since": ["all"]})
    blob = json.dumps(d, ensure_ascii=False)
    assert SECRET not in blob and "***" in blob                   # 提问与 MCP 错误原文里的密钥都打码
    ops = {r["name"]: r for r in d["skills"]}["ops"]
    assert isinstance(ops["cost"], float) and ops["cost"] > 0      # $ 由 serve 注入换算
    raw = trace.stats(s3, price=serve._wf_price)
    assert SECRET in raw["mcp"][0]["errors"][0]["excerpt"]         # 缓存里的原件没被 serve 改动 (明细还要用它)


def test_serve_drill_redacts_and_restamps(s3):
    d = serve._wf_stats(s3, {"since": ["all"]})
    ok = serve._wf_drill(s3, {"since": ["all"], "stamp": [d["stamp"]], "ref": ["tasks"]})
    assert ok["total"] == 3 and not ok["restamped"]
    assert SECRET not in json.dumps(ok, ensure_ascii=False)
    stale = serve._wf_drill(s3, {"since": ["all"], "stamp": ["gone"], "ref": ["all"], "flag": ["fail"]})
    assert stale["restamped"] and stale["total"] == 3              # 过期: 按同一窗口重算, 并告诉页面刷新数字
    assert serve._wf_drill(s3, {"since": ["all"]})["error"]


def test_untagged_skill_duration_is_unknown_not_zero(tmp_path, monkeypatch):
    """没有归属标记的 skill: 回放里只装着加载它的那一步, 它管到哪不知道 -> 时长和 token 都是「拿不到」, 不能写成 0 秒。"""
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {}), ("_STATS", {}), ("_STAMPS", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    base = tmp_path / "projects"
    write_jsonl(base / "c--Users-u--vscode-demo" / f"{SID}.jsonl", [
        human(0, "P1", "写个文档"),
        asst(1, "m1", "r1", tool_use("sk", "Skill", {"skill": "docs"}), usage(out=5)),
        result(1.2, "sk", "Launching skill: docs"),
        asst(2, "m2", "r2", tool_use("b", "Bash", {"command": "ls"}), usage(out=5)),      # 没有 attributionSkill
        result(9, "b", "ok"),
    ])
    res = trace.stats(base)
    docs = {r["name"]: r for r in res["skills"]}["docs"]
    assert docs["tasks"] == 1 and docs["untagged"] == 1 and docs["time"] == 0 and docs["tokens"] == 0
    pt = {r["name"]: r for r in res["compare"]["skill"]}["docs"]["points"][0]
    assert pt["dur"] is None and pt["tokens"] is None and pt["untagged"]
