"""S2 (学习层) 的单测: 阶段标注 (推断) / 数据依赖 (推断) / 名词说明 / 脚本对照。"""

import json

import pytest

from tokmon import parser, serve, trace
from test_trace import SID, asst, human, iso, result, tool_use, usage, write_jsonl


@pytest.fixture
def clean(monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})


# ------------------------------------------------------------------ 阶段分类 (推断)

@pytest.mark.parametrize("name,inp,want", [
    ("Read", {"file_path": "a.py"}, "查看"),
    ("Grep", {"pattern": "x"}, "查看"),
    ("Edit", {"file_path": "a.py"}, "修改"),
    ("Write", {"file_path": "a.py"}, "修改"),
    ("WebSearch", {"query": "x"}, "调研"),
    ("Agent", {"description": "d"}, "编排"),
    ("Skill", {"skill": "s"}, "编排"),
    ("AskUserQuestion", {}, "等你"),
    ("Bash", {"command": "cd server && npm test"}, "验证"),
    ("Bash", {"command": "PYTHONPATH=. python -m pytest -q"}, "验证"),
    ("Bash", {"command": "npm run build && npm test"}, "验证"),          # 链里有测试 -> 目的是验证
    ("Bash", {"command": "cat a.txt | head -5"}, "查看"),
    ("Bash", {"command": "git status && git diff --stat"}, "查看"),
    ("Bash", {"command": "git add -A && git commit -m x"}, "修改"),
    ("Bash", {"command": "cat > notes.md <<'EOF'"}, "修改"),            # 重定向写文件
    ("Bash", {"command": "sed -i 's/a/b/' f.py"}, "修改"),
    ("Bash", {"command": "echo hi 2>&1"}, "查看"),                     # 2>&1 不是写文件
    ("Bash", {"command": "python - <<'PY'"}, "运行"),                   # 看不出目的 -> 运行
    ("Bash", {"command": "python - <<'PY'", "description": "Run the smoke test"}, "验证"),
    ("PowerShell", {"command": "Get-Content a.txt"}, "查看"),
    ("mcp__railway__get_logs", {}, "查看"),
    ("mcp__railway__list_services", {}, "查看"),
    ("mcp__railway__docs_fetch", {}, "调研"),
    ("mcp__railway__create_project", {}, "修改"),
    ("mcp__railway__set_variables", {}, "修改"),
    ("SomethingNew", {}, "运行"),
])
def test_classify_call(name, inp, want):
    assert trace.classify_call(name, inp) == want


def _c(i, stage, fail=False, t=None, name="X"):
    t0 = t if t is not None else i * 10.0
    return {"id": f"c{i}", "name": name, "stage": stage, "t0": t0, "t1": t0 + 2, "fail": fail, "strong": fail}


def test_stage_runs_merge_single_call_sandwich():
    """修改中途读了一眼文件: 那 1 次查看并进前后的修改里。"""
    runs = trace.stage_runs([_c(0, "修改"), _c(1, "修改"), _c(2, "查看"), _c(3, "修改")])
    assert [r["stage"] for r in runs] == ["修改"]
    assert runs[0]["n"] == 4 and runs[0]["absorbed"] == 1 and runs[0]["absorbed_stages"] == ["查看"]


def test_stage_runs_never_merge_failure_or_wait():
    runs = trace.stage_runs([_c(0, "修改"), _c(1, "验证", fail=True), _c(2, "修改"), _c(3, "等你"), _c(4, "修改")])
    assert [r["stage"] for r in runs] == ["修改", "验证", "修改", "等你", "修改"]
    assert runs[1]["fail"] is True


def test_stage_runs_duration_stops_at_idle_gap():
    """每段时长 = 到下一段开始 (中间是模型在想); 但相隔超过 IDLE_GAP 的空闲不算进去。"""
    runs = trace.stage_runs([_c(0, "查看", t=0), _c(1, "修改", t=10), _c(2, "验证", t=5000)], t_end=5003)
    assert runs[0]["dur"] == pytest.approx(10)
    assert runs[1]["dur"] == pytest.approx(2)               # 12 -> 5000 的空档不算
    assert runs[2]["dur"] == pytest.approx(3)


# ------------------------------------------------------------------ 数据依赖 (推断)

def test_id_tokens_only_id_like():
    txt = ("project 41ec60f2-b610-482f-ab06-6633b29e6b6b model claude-fable-5-1 "
           "sha 3f9a2c1b7d4e5f60 path server_routes_platform word authentication_required_error "
           "req req_011CfEgvz4uieKDXudwRJb2T")
    got = set(trace.id_tokens(txt))
    assert "41ec60f2-b610-482f-ab06-6633b29e6b6b" in got
    assert "3f9a2c1b7d4e5f60" in got and "req_011CfEgvz4uieKDXudwRJb2T" in got
    assert "claude-fable-5-1" not in got and "authentication_required_error" not in got   # 名字 / 单词不是 ID


def test_input_ids_paths():
    got = trace.input_ids({"project_id": "41ec60f2-b610-482f-ab06-6633b29e6b6b", "vars": {"SERVICE": "x" * 3},
                           "list": ["7571ebee-e949-46f6-8e3a-b4edf8d7f032"]})
    assert got == {"41ec60f2-b610-482f-ab06-6633b29e6b6b": "project_id", "7571ebee-e949-46f6-8e3a-b4edf8d7f032": "list[0]"}


PID = "41ec60f2-b610-482f-ab06-6633b29e6b6b"
SVC = "7571ebee-e949-46f6-8e3a-b4edf8d7f032"


def _dep_session(tmp_path):
    lines = [
        human(0, "P1", "看一下线上日志"),
        asst(1, "m1", "r1", tool_use("cfg", "Bash", {"command": "cat .railway/config.json", "description": "Read config"}),
             usage(out=5)),
        result(2, "cfg", json.dumps({"project": PID})),
        asst(3, "m2", "r2", tool_use("ls", "mcp__railway__list_services", {"project_id": PID}), usage(out=5)),
        result(4, "ls", json.dumps([{"id": SVC, "name": "web"}])),
        asst(5, "m3", "r3", tool_use("logs", "mcp__railway__get_logs", {"project_id": PID, "service_id": SVC}), usage(out=5)),
        result(6, "logs", "ok"),
        asst(7, "m4", "r4", tool_use("ed", "Edit", {"file_path": "a.py", "old_string": PID, "new_string": "x"}), usage(out=5)),
        result(8, "ed", "ok"),
    ]
    base = tmp_path / "projects"
    main = base / "c--Users-u--vscode-demo" / f"{SID}.jsonl"
    write_jsonl(main, lines)
    return base, main


def test_mcp_dependencies_found_and_scoped(clean, tmp_path):
    base, main = _dep_session(tmp_path)
    t = {x["id"]: x for x in trace.session_tasks(main)[0]}["P1"]
    calls = {n["id"]: n for n in trace.walk([trace.build_task(t)["tree"]]) if n["kind"] == "call"}
    d_ls = calls["ls"]["meta"]["deps"]
    assert d_ls == [{"from": "cfg", "from_name": "Bash", "from_label": "Read config", "key": "project_id", "value": PID}]
    srcs = {d["from"] for d in calls["logs"]["meta"]["deps"]}
    assert srcs == {"cfg", "ls"}                                # project_id 来自 Bash, service_id 来自 list_services
    assert "deps" not in calls["ed"]["meta"]                    # 目标只看 MCP (Edit 不找来源)


def test_dependency_value_and_secret_keys_scrubbed(clean, tmp_path):
    tree = {"id": "t", "kind": "task", "children": [{"id": "c", "kind": "call", "label": "x", "meta": {"deps": [
        {"from": "a", "from_name": "Bash", "from_label": "l", "key": "project_id", "value": PID},
        {"from": "b", "from_name": "Bash", "from_label": "l", "key": "api_token", "value": PID}]}}]}
    serve._wf_scrub_tree(tree)
    deps = tree["children"][0]["meta"]["deps"]
    assert deps[0]["value"] == PID[:12] + "…"                   # 只给开头 12 位
    assert deps[1]["value"] == "***"                            # 字段名像密钥: 不给值


# ------------------------------------------------------------------ 名词说明

def test_parse_listing_handles_colons_in_names():
    content = "- use-railway: Operate Railway infrastructure.\n- anthropic-skills:docs: Living docs people share."
    got = trace.parse_listing(["use-railway", "anthropic-skills:docs"], content)
    assert got == {"use-railway": "Operate Railway infrastructure.", "anthropic-skills:docs": "Living docs people share."}


def test_glossary_only_for_used_names(clean, tmp_path):
    lines = [
        {"type": "attachment", "timestamp": iso(-1), "sessionId": SID, "attachment": {
            "type": "skill_listing", "names": ["ops", "unused"], "content": "- ops: Run the ops playbook.\n- unused: Never used."}},
        {"type": "attachment", "timestamp": iso(-1), "sessionId": SID, "attachment": {
            "type": "mcp_instructions_delta", "addedBlocks": ["## railway\nRailway MCP server. Manage projects."]}},
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("sk", "Skill", {"skill": "ops"}), usage(out=5)),
        result(2, "sk", "Launching skill: ops"),
        asst(3, "m2", "r2", tool_use("lg", "mcp__railway__get_logs", {}), usage(out=5)),
        result(4, "lg", "ok"),
    ]
    base = tmp_path / "projects"
    main = base / "c--Users-u--vscode-demo" / f"{SID}.jsonl"
    write_jsonl(main, lines)
    g = trace.build_task(trace.session_tasks(main)[0][0])["summary"]["glossary"]
    assert g["skill"] == {"ops": "Run the ops playbook."}
    assert g["mcp"] == {"railway": "Railway MCP server. Manage projects."}


# ------------------------------------------------------------------ 脚本对照

SCRIPT = """export const meta = {
  name: 'wf-demo',
  description: 'demo',
  phases: [{ title: 'Investigate', detail: 'look' }, { title: 'Verify', detail: 'check' }, { title: 'Report' }],
}
phase('Investigate')
const found = await parallel(LENSES.map(lens => () =>
  agent(prompt(lens), { label: `investigate:${lens.key}`, schema: S })))
const v = await agent('verify it', { label: 'verify:all', phase: 'Verify' })
const r = await agent(p, { label: 'report:' + name })
"""


def test_script_map_phases_three_ways_and_agent_labels():
    m = trace.script_map(SCRIPT)
    assert m["phases"]["Investigate"] == 6 and m["phase_how"]["Investigate"] == "phase()"
    assert m["phase_how"]["Verify"] == "opts"                   # agent() 参数里的 phase:
    assert m["phase_how"]["Report"] == "meta"                   # 只在头部声明过
    assert trace.match_agent_line("investigate:known-issues", m["agents"]) == 8       # 模板
    assert trace.match_agent_line("verify:all", m["agents"]) == 9                     # 字面量
    assert trace.match_agent_line("report:weekly", m["agents"]) == 10                 # 拼接前缀
    assert trace.match_agent_line("something-else", m["agents"]) is None              # 对不上就不猜


def test_get_script_uses_true_path_in_sibling_project_dir(clean, tmp_path):
    """真实情况: 会话中途 cd 进子目录后启动的 workflow, 脚本存在「那一刻 cwd」对应的兄弟项目目录下。"""
    base = tmp_path / "projects"
    proj = base / "c--Users-u--vscode-demo"
    main = proj / f"{SID}.jsonl"
    sibling = base / "C--Users-u--vscode-demo-server" / SID / "workflows" / "scripts"
    sibling.mkdir(parents=True)
    sp = sibling / "wf-demo-wf_abc-123.js"
    sp.write_text(SCRIPT, encoding="utf-8")
    rdir = proj / SID / "subagents" / "workflows" / "wf_abc-123"
    write_jsonl(rdir / "agent-w1.jsonl", [
        {"type": "user", "timestamp": iso(3), "sessionId": SID, "agentId": "w1", "message": {"role": "user", "content": "go"}},
        asst(4, "mw", "rw", {"type": "text", "text": "done"}, usage(out=5), agent="w1")])
    (rdir / "agent-w1.meta.json").write_text(json.dumps({"description": "investigate:known-issues",
                                                         "workflowPhase": "Investigate"}), encoding="utf-8")
    write_jsonl(main, [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("wf", "Workflow", {"name": "wf-demo"}), usage(out=5)),
        result(2, "wf", "launched", tur={"status": "async_launched", "runId": "wf_abc-123", "workflowName": "wf-demo",
                                         "scriptPath": str(sp), "transcriptDir": str(rdir)}),
    ])
    trace.list_tasks(base)
    got = trace.get_script("P1", "wf_abc-123", base)
    assert got is not None and got["phases"]["Investigate"] == 6
    assert got["agents"] == {"w1": 8}                           # 回放里的 agent -> 启动它的 agent() 那一行
    assert trace.get_script("P1", "wf_nope", base) is None


def test_script_path_outside_projects_is_ignored(clean, tmp_path):
    """transcript 里记的路径不在 ~/.claude/projects 之内 -> 不读 (不跟着任意路径走)。"""
    outside = tmp_path / "elsewhere.js"
    outside.write_text(SCRIPT, encoding="utf-8")
    base = tmp_path / "projects"
    main = base / "c--Users-u--vscode-demo" / f"{SID}.jsonl"
    write_jsonl(main, [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("wf", "Workflow", {"name": "w"}), usage(out=5)),
        result(2, "wf", "launched", tur={"status": "async_launched", "runId": "wf_zzz-000", "scriptPath": str(outside)}),
    ])
    trace.list_tasks(base)
    assert trace.get_script("P1", "wf_zzz-000", base) is None
