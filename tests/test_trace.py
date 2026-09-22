"""trace 支柱 (/workflow 数据层) 的单测: 纯函数 + 一个合成的完整会话端到端。

盯死的护栏:
- 耗时三段: 并行按并集、等你优先、空闲不计;
- token: 同一响应多行取最后一行、按响应去重, 任务真值 == parser (/tokens 同一契约);
- 层级: Skill 归属只信标记、Agent 按 agentId 回链、Workflow 按 runId + 脚本 phase 顺序;
- 任务边界: 真人提问切分, 后台完成通知按 <tool-use-id> 回到启动它的任务, 上下文压缩不算提问;
- 诚实: 发起成本按并行数均摊、重试/打转只在同一时间线内判、样本不足不判「慢」。
"""

import json
from pathlib import Path

import pytest

from tokmon import parser, trace


# ================================================================ 纯函数

def test_est_tokens():
    assert trace.est_tokens("") == 0
    assert trace.est_tokens("a" * 400) == 100            # ASCII ≈ 4 字符 / token
    assert trace.est_tokens("中文字符测试") == 6            # 非 ASCII ≈ 1 字符 / token


def test_split_time_sequential():
    r = trace.split_time([(0, 10, "model"), (2, 5, "machine")])
    assert r["machine"] == 3 and r["model"] == 7 and r["active"] == 10 and r["span"] == 10


def test_split_time_parallel_is_union_not_sum():
    """两个并行工具 [0,4] 与 [2,6]: 机器执行是 6 秒 (并集), 不是 8 秒。"""
    r = trace.split_time([(0, 4, "machine"), (2, 6, "machine")])
    assert r["machine"] == 6


def test_split_time_wait_has_priority():
    r = trace.split_time([(0, 5, "wait"), (3, 8, "machine"), (0, 10, "model")])
    assert r["wait"] == 5 and r["machine"] == 3 and r["model"] == 2


def test_split_time_idle_gap_not_counted():
    r = trace.split_time([(0, 2, "model"), (10, 12, "model")])
    assert r["active"] == 4 and r["span"] == 12            # 中间 8 秒没有任何活动 -> 不计入


def test_split_time_ignores_broken_intervals():
    r = trace.split_time([(None, 5, "machine"), (1, None, "model"), (0, 1, "bogus")])
    assert r["active"] == 0


def test_norm_key_bash_whitespace_insensitive():
    a = trace.norm_key("Bash", {"command": "npm   test\n"})
    b = trace.norm_key("Bash", {"command": "npm test"})
    assert a == b


def test_flag_repeats_retry_and_loop():
    calls = [{"key": "k", "failed": True, "out": "same", "flags": []},
             {"key": "k", "failed": False, "out": "same", "flags": []},
             {"key": "k", "failed": False, "out": "same", "flags": []},
             {"key": "other", "failed": False, "out": "x", "flags": []}]
    trace.flag_repeats(calls)
    assert calls[0]["flags"] == []
    assert calls[1]["flags"] == ["retry"]                  # 失败之后同一件事又做了一次
    assert set(calls[2]["flags"]) == {"retry", "loop"}     # 第 3 次、结果也一样: 原地打转
    assert calls[3]["flags"] == []


def test_loop_needs_unchanged_output():
    """review #16: 「改代码 -> 重跑测试」输出每次都变, 是正常循环, 不能判成打转。"""
    calls = [{"key": "k", "failed": False, "out": f"run-{i}", "flags": []} for i in range(4)]
    trace.flag_repeats(calls)
    assert all("loop" not in c["flags"] for c in calls)
    bg = [{"key": "k", "failed": False, "out": "same", "async": True, "flags": []} for _ in range(4)]
    trace.flag_repeats(bg)
    assert all(c["flags"] == [] for c in bg)               # 后台启动不参与判断


def test_p90():
    assert trace.p90([]) is None
    assert trace.p90(list(range(1, 11))) == 10


def test_parse_script_meta_reads_literals_only():
    src = """export const meta = {
  name: 'edge-clicks',
  description: "Investigate the \\"dead click\\" bug",
  phases: [
    { title: 'Investigate', detail: 'five lenses' },
    { title: 'Verify', detail: 'two skeptics' },
  ],
}
const X = 1"""
    m = trace.parse_script_meta(src)
    assert m["name"] == "edge-clicks"
    assert [p["title"] for p in m["phases"]] == ["Investigate", "Verify"]
    assert m["phases"][1]["detail"] == "two skeptics"
    assert trace.parse_script_meta("no meta here")["phases"] == []


def test_prompt_text_skips_ide_injections():
    content = [{"type": "text", "text": "<ide_opened_file>x</ide_opened_file>"},
               {"type": "text", "text": "修一下点击失效"},
               {"type": "image", "source": {}}]
    text, att = trace.prompt_text(content)
    assert text == "修一下点击失效" and att == 1


def test_call_label_prefers_bash_description():
    assert trace.call_label("Bash", {"command": "npm test", "description": "Run tests"}) == ("Run tests", "npm test")
    assert trace.call_label("Read", {"file_path": "C:/a/b/app.js"})[0] == "app.js"
    assert trace.call_label("mcp__railway__get_logs", {"lines": 60})[0] == "get_logs"


def test_notify_fields():
    f = trace.notify_fields("<task-notification>\n<task-id>abc</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n"
                            "<status>completed</status>\n<summary>done it</summary>")
    assert f["tids"] == ["toolu_1"] and f["task_ids"] == ["abc"] and f["status"] == "completed"
    assert f["summary"] == "done it"


# ================================================================ 合成会话 fixture

T0 = 1_760_000_000.0                         # 固定起点 (epoch 秒)
SID = "11111111-2222-3333-4444-555555555555"


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(T0 + t, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def usage(inp=10, out=20, cread=1000, c1h=100):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cread,
            "cache_creation_input_tokens": c1h,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": c1h}}


def asst(t, mid, rid, block, u, skill=None, agent=None, model="claude-opus-5"):
    d = {"type": "assistant", "timestamp": iso(t), "sessionId": SID, "requestId": rid, "uuid": f"u-{mid}-{t}",
         "cwd": "C:\\Users\\u\\.vscode\\demo", "message": {"id": mid, "model": model, "content": [block], "usage": u}}
    if skill:
        d["attributionSkill"] = skill
    if agent:
        d["agentId"] = agent
        d["isSidechain"] = True
    return d


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(t, tid, text, err=False, tur=None, agent=None):
    d = {"type": "user", "timestamp": iso(t), "sessionId": SID, "uuid": f"r-{tid}",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text,
                                                  "is_error": err}]}}
    if tur is not None:
        d["toolUseResult"] = tur
    if agent:
        d["agentId"] = agent
    return d


def human(t, uuid, text):
    return {"type": "user", "timestamp": iso(t), "sessionId": SID, "uuid": uuid, "origin": {"kind": "human"},
            "cwd": "C:\\Users\\u\\.vscode\\demo", "gitBranch": "main",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def write_jsonl(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")


@pytest.fixture
def session(tmp_path, monkeypatch):
    # 独立缓存, 避免和真实数据 / 其他用例串味
    monkeypatch.setattr(trace, "_FILES", {})
    monkeypatch.setattr(trace, "_TASK_INDEX", {})
    monkeypatch.setattr(trace, "_AGENT_IDX", {})
    monkeypatch.setattr(trace, "_SCRIPTS", {})
    monkeypatch.setattr(trace, "_META", {})
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(trace, "_BUILT", {})
    monkeypatch.setattr(trace, "_CLAIMED", {})
    monkeypatch.setattr(parser, "_file_cache", {})
    base = tmp_path / "projects"
    proj = base / "c--Users-u--vscode-demo"
    main = proj / f"{SID}.jsonl"
    sdir = proj / SID
    lines = [
        {"type": "system", "subtype": "init", "timestamp": iso(-5), "sessionId": SID},
        human(0, "P1", "修复 Edge 下点击失效"),
        # R1: 一次响应里 说话 + 两个并行调用; usage 逐行变大, 最后一行为准
        asst(1, "m1", "r1", {"type": "text", "text": "先跑一下测试"}, usage(out=10)),
        asst(1, "m1", "r1", tool_use("tu1", "Bash", {"command": "npm test", "description": "Run tests"}), usage(out=30)),
        asst(1, "m1", "r1", tool_use("tu2", "Read", {"file_path": "C:/demo/app.js"}), usage(out=50)),
        result(3, "tu2", "x" * 50),
        result(5, "tu1", "FAIL 1 test", err=True, tur={"stdout": "", "stderr": "boom", "interrupted": False}),
        # R2: 同一条命令再跑一次 -> 重试 (推断)
        asst(6, "m2", "r2", tool_use("tu3", "Bash", {"command": "npm  test"}), usage(out=12)),
        result(8, "tu3", "ok"),
        # R3: 加载 skill (这一行本身没有归属标记)
        asst(9, "m3", "r3", tool_use("tu4", "Skill", {"skill": "demo-skill"}), usage(out=5)),
        result(9.5, "tu4", "Launching skill: demo-skill", tur={"success": True, "commandName": "demo-skill"}),
        {"type": "user", "isMeta": True, "timestamp": iso(9.6), "sessionId": SID,
         "message": {"role": "user", "content": [{"type": "text", "text": "SKILL BODY"}]}},
        # R4/R5: 带 attributionSkill 的行 -> 归到 skill 节点下
        asst(10, "m4", "r4", tool_use("tu5", "Agent", {"description": "sub task", "prompt": "do the sub task please, carefully",
                                                      "run_in_background": True}), usage(out=40), skill="demo-skill"),
        result(10.5, "tu5", "launched", tur={"isAsync": True, "status": "async_launched", "agentId": "a1"}),
        asst(11, "m5", "r5", tool_use("tu6", "Workflow", {"name": "wf-demo"}), usage(out=60), skill="demo-skill"),
        result(11.2, "tu6", "Workflow launched", tur={"status": "async_launched", "runId": "wf_x", "workflowName": "wf-demo"}),
        # 你中途插话 (queued_command, commandMode=prompt)
        {"type": "attachment", "timestamp": iso(12), "sessionId": SID,
         "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": [{"type": "text", "text": "别跑全量"}]}},
        # R6: 向你提问, 等了 60 秒
        asst(13, "m6", "r6", tool_use("tu7", "AskUserQuestion", {"questions": [{"question": "选哪个?"}]}), usage(out=8)),
        result(73, "tu7", "A"),
        asst(74, "m7", "r7", {"type": "text", "text": "好, 已修复。"}, usage(out=15)),
        # 后台 workflow 完成通知 -> 回到 P1 的延续段
        {"type": "user", "timestamp": iso(100), "sessionId": SID, "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": "<task-notification>\n<task-id>wmx</task-id>\n<tool-use-id>tu6</tool-use-id>\n"
                                                "<status>completed</status>\n<summary>wf-demo finished</summary>"}},
        asst(101, "m8", "r8", {"type": "text", "text": "workflow 跑完了"}, usage(out=9)),
        # 第二个任务 + 上下文压缩 (不算提问)
        human(200, "P2", "再看一眼"),
        asst(201, "m9", "r9", {"type": "text", "text": "看过了"}, usage(out=7)),
        {"type": "system", "subtype": "compact_boundary", "timestamp": iso(202), "sessionId": SID},
        {"type": "user", "isCompactSummary": True, "timestamp": iso(203), "sessionId": SID,
         "message": {"role": "user", "content": "This session is being continued..."}},
    ]
    write_jsonl(main, lines)
    # 后台 agent a1
    write_jsonl(sdir / "subagents" / "agent-a1.jsonl", [
        {"type": "user", "timestamp": iso(10.6), "sessionId": SID, "agentId": "a1", "isSidechain": True,
         "message": {"role": "user", "content": "do the sub task please, carefully"}},
        asst(11, "ma1", "ra1", tool_use("ta1", "Grep", {"pattern": "click"}), usage(out=21), agent="a1"),
        result(12, "ta1", "app.js:3", agent="a1"),
        asst(13, "ma2", "ra2", {"type": "text", "text": "found it"}, usage(out=11), agent="a1"),
    ])
    (sdir / "subagents" / "agent-a1.meta.json").write_text(json.dumps({"agentType": "general-purpose",
                                                                        "description": "sub task"}), encoding="utf-8")
    # workflow wf_x: 两个阶段各一个 agent; 第二个的 MCP 调用失败
    wdir = sdir / "subagents" / "workflows" / "wf_x"
    write_jsonl(wdir / "agent-w1.jsonl", [
        {"type": "user", "timestamp": iso(11.5), "sessionId": SID, "agentId": "w1", "message": {"role": "user", "content": "investigate"}},
        asst(12, "mw1", "rw1", tool_use("tw1", "Bash", {"command": "ls"}), usage(out=13), agent="w1"),
        result(14, "tw1", "a b c", agent="w1"),
        asst(20, "mw2", "rw2", {"type": "text", "text": "inv done"}, usage(out=6), agent="w1"),
    ])
    (wdir / "agent-w1.meta.json").write_text(json.dumps({"agentType": "workflow-subagent", "description": "inv:alpha",
                                                          "workflowPhase": "Investigate"}), encoding="utf-8")
    write_jsonl(wdir / "agent-w2.jsonl", [
        {"type": "user", "timestamp": iso(21), "sessionId": SID, "agentId": "w2", "message": {"role": "user", "content": "verify"}},
        asst(22, "mw3", "rw3", tool_use("tw2", "mcp__railway__get_logs", {"lines": 60}), usage(out=17), agent="w2"),
        result(25, "tw2", "unauthorized", err=True, agent="w2"),
        asst(30, "mw4", "rw4", {"type": "text", "text": "verify done"}, usage(out=4), agent="w2"),
    ])
    (wdir / "agent-w2.meta.json").write_text(json.dumps({"agentType": "workflow-subagent", "description": "ver:beta",
                                                          "workflowPhase": "Verify"}), encoding="utf-8")
    scripts = sdir / "workflows" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "wf-demo-wf_x.js").write_text(
        "export const meta = {\n  name: 'wf-demo',\n  description: 'demo workflow',\n  phases: [\n"
        "    { title: 'Investigate', detail: 'look around' },\n    { title: 'Verify', detail: 'double check' },\n  ],\n}\n",
        encoding="utf-8")
    return {"base": base, "main": main}


def _tasks(session):
    tasks, pre = trace.session_tasks(session["main"])
    return {t["id"]: t for t in tasks}, pre


def _find(node, pred):
    out = []
    for n in trace.walk([node]):
        if pred(n):
            out.append(n)
    return out


# ================================================================ 端到端

def test_task_boundaries(session):
    tasks, pre = _tasks(session)
    assert list(tasks) == ["P1", "P2"]                     # 上下文压缩续写不算一次提问
    p1 = tasks["P1"]
    kinds = [s["kind"] for s in p1["segments"]]
    assert kinds == ["main", "continuation"]               # 后台完成通知回到了启动它的任务
    assert p1["segments"][1]["linked"] is True              # 按 <tool-use-id> 真值回链


def test_tree_structure(session):
    tasks, _ = _tasks(session)
    built = trace.build_task(tasks["P1"])
    tree = built["tree"]
    skills = _find(tree, lambda n: n["kind"] == "skill")
    assert len(skills) == 1 and skills[0]["label"] == "demo-skill"
    # skill 节点里装着: 加载它的 Skill 调用 + 带标记的 Agent / Workflow 调用
    inside = [c.get("name") for c in skills[0]["children"] if c["kind"] == "call"]
    assert inside == ["Skill", "Agent", "Workflow"]
    # AskUserQuestion 那一行没有标记 -> 不在 skill 里 (不猜归属)
    top_calls = [c.get("name") for c in tree["children"] if c["kind"] == "call"]
    assert "AskUserQuestion" in top_calls
    agent = _find(tree, lambda n: n["kind"] == "agent" and n.get("agent_id") == "a1")[0]
    assert agent["label"] == "sub task" and agent["tokens"]["total"] > 0
    wf = _find(tree, lambda n: n["kind"] == "workflow")[0]
    assert [p["label"] for p in wf["children"]] == ["Investigate", "Verify"]     # 脚本里的 phase 顺序
    assert wf["children"][0]["detail"] == "look around"
    assert _find(tree, lambda n: n["kind"] == "user" and n.get("ev") == "interject")   # 你的插话成了一行标记
    assert _find(tree, lambda n: n["kind"] == "segment")                               # 延续段


def test_flags_and_estimates(session):
    tasks, _ = _tasks(session)
    built = trace.build_task(tasks["P1"], baseline={"Bash": {"n": 30, "p90": 1.0}})
    calls = {c["id"]: c for c in _find(built["tree"], lambda n: n["kind"] == "call")}
    assert "fail" in calls["tu1"]["flags"]
    assert "retry" in calls["tu3"]["flags"]                 # 同一命令 (空白不同) 失败后又跑
    assert "fail" in calls["tw2"]["flags"]                  # workflow 里 MCP 调用失败
    assert "slow" in calls["tu1"]["flags"]                  # 4s > p90 1s 且样本 >= 20
    # 发起成本: 响应最终 output 50, 里面 2 个并行调用 -> 各 ≈25
    assert calls["tu1"]["issue_est"] == 25 and calls["tu2"]["issue_est"] == 25
    assert calls["tu1"]["meta"]["parallel"] == 2
    few = trace.build_task(tasks["P1"], baseline={"Bash": {"n": 5, "p90": 1.0}})
    c1 = [c for c in _find(few["tree"], lambda n: n.get("id") == "tu1")][0]
    assert "slow" not in c1["flags"]                        # 样本不足: 不判慢


def test_time_split_counts_wait(session):
    tasks, _ = _tasks(session)
    s = trace.build_task(tasks["P1"])["summary"]
    assert s["time"]["wait"] == pytest.approx(60, abs=0.01)   # AskUserQuestion 13 -> 73
    assert s["time"]["machine"] > 0 and s["time"]["model"] > 0
    assert s["time"]["active"] <= s["time"]["span"] + 1e-6


def test_tokens_reconcile_with_parser(session):
    """任务真值 == parser (/tokens) 在同一批文件上的总和。这是 S1 的对账判据。"""
    tasks, pre = _tasks(session)
    t_total = sum(trace.build_task(t)["summary"]["tokens"]["total"] for t in tasks.values())
    recs = parser.load_records(session["base"])
    p_total = sum(r.total_tokens for r in recs)
    assert t_total == p_total
    # 同一响应多行: 取最后一行 (m1 的 output=50, 不是 10 或 30)
    m1 = [r for r in recs if r.message_id == "m1"][0]
    assert m1.output_tokens == 50


def test_moments_and_summary(session):
    tasks, _ = _tasks(session)
    s = trace.build_task(tasks["P1"])["summary"]
    kinds = [m["kind"] for m in s["moments"]]
    assert "fail" in kinds and "wait" in kinds and "workflow" in kinds and "done" in kinds
    assert s["skills"] == ["demo-skill"]
    assert s["workflows"] == [{"label": "wf-demo", "phases": 2, "agents": 2}]
    assert s["mcp_servers"] == ["railway"]
    assert s["continuations"] == 1


def test_list_and_get_and_call_detail(session):
    rows = trace.list_tasks(session["base"])
    assert [r["id"] for r in rows] == ["P2", "P1"]          # 时间倒序
    assert rows[1]["project"] == "demo"                     # .vscode 下的子文件夹
    got = trace.get_task("P1", session["base"])
    assert got["summary"]["prompt_full"] == "修复 Edge 下点击失效"
    d = trace.get_call_detail("P1", "tu1", session["base"])
    assert d["input"]["command"] == "npm test"
    assert d["output"] == "FAIL 1 test" and d["is_error"] is True
    assert d["extra"]["stderr"] == "boom"


def test_incremental_load_and_half_line(session):
    main = session["main"]
    ft = trace.load_file(main)
    n0 = len(ft.rows)
    with open(main, "a", encoding="utf-8") as f:
        f.write(json.dumps(human(300, "P3", "第三问"), ensure_ascii=False) + "\n")
        f.write('{"type": "assistant", "half')                    # 正在写的半行
    ft2 = trace.load_file(main)
    assert len(ft2.rows) == n0 + 1                               # 只读新增的完整行
    assert ft2.rows[-1]["k"] == "prompt"
    ids = [r.get("i") for r in ft2.rows]
    assert ids == list(range(len(ft2.rows)))                     # 行序号连续 (节点 id 稳定的前提)


def test_missing_agent_file_is_honest(session):
    """Agent 结果里的 agentId 找不到文件 -> 节点标 missing、tokens=None, 绝不填 0。"""
    (session["main"].with_suffix("") / "subagents" / "agent-a1.jsonl").unlink()
    trace._AGENT_IDX.clear()
    tasks, _ = _tasks(session)
    tree = trace.build_task(tasks["P1"])["tree"]
    a = _find(tree, lambda n: n["kind"] == "agent" and n.get("agent_id") == "a1")[0]
    assert "missing" in a["flags"] and a["tokens"] is None


def test_zero_token_synthetic_is_not_unpriced():
    """<synthetic> 占位消息 0 token: 不能让整个任务被标成「含未知单价模型」(误报也是不诚实)。"""
    from tokmon import serve
    tok = {"total": 100, "by_model": {"claude-opus-5": [10, 20, 0, 50, 20], "<synthetic>": [0, 0, 0, 0, 0]}}
    serve._wf_cost(tok)
    assert tok["unpriced"] is False and tok["cost"] > 0
    tok2 = {"total": 5, "by_model": {"mystery-model": [1, 2, 0, 0, 2]}}
    serve._wf_cost(tok2)
    assert tok2["unpriced"] is True                        # 真有 token 的未知模型照样如实标出
