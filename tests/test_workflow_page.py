"""/workflow 页面的前端冒烟测试: 合成会话 -> serve 接口数据 -> Node 桩 DOM 里跑一遍页面 JS。

RECAP 侧批 #6: serve.py 与前端 JS 此前零覆盖, bug 恰好长在那里。这个测试让页面脚本进测试套件:
渲染 / 三种展开模式 / 每类节点的明细抽屉 / 跳转 / 流程条 / 依赖徽章 / 脚本对照 / 全文, 任一处抛错或被 try 吞掉都会失败。
S3: 统计子页的每个可点数字都要能点开 (计数 == 明细条数), 每条明细都要能跳回回放里的那一步。
没装 node 就跳过 (不影响纯 Python 环境)。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tokmon import parser, serve, trace
from test_trace import SID, asst, human, iso, result, tool_use, usage, write_jsonl

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "js" / "workflow_smoke.js"
PAGE = ROOT / "tokmon" / "pages" / "workflow.html"
PID = "41ec60f2-b610-482f-ab06-6633b29e6b6b"

SCRIPT = """export const meta = {
  name: 'wf-demo', description: 'demo run',
  phases: [{ title: 'Investigate', detail: 'look around' }, { title: 'Verify', detail: 'double check' }],
}
phase('Investigate')
const a = await agent('look', { label: `investigate:${k}` })
const b = await agent('check', { label: 'verify:all', phase: 'Verify' })
"""


@pytest.fixture
def session(tmp_path, monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {}), ("_STATS", {}), ("_STAMPS", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})
    base = tmp_path / "projects"
    proj = base / "c--Users-u--vscode-demo"
    main = proj / f"{SID}.jsonl"
    sdir = proj / SID
    rdir = sdir / "subagents" / "workflows" / "wf_demo-001"
    sp = sdir / "workflows" / "scripts" / "wf-demo-wf_demo-001.js"
    sp.parent.mkdir(parents=True)
    sp.write_text(SCRIPT, encoding="utf-8")
    long_text = "结论如下：\n" + "\n".join(f"- 第 {i} 条发现" for i in range(80))
    write_jsonl(main, [
        {"type": "attachment", "timestamp": iso(-1), "sessionId": SID, "attachment": {
            "type": "skill_listing", "names": ["ops"], "content": "- ops: Run the ops playbook."}},
        {"type": "attachment", "timestamp": iso(-1), "sessionId": SID, "attachment": {
            "type": "mcp_instructions_delta", "addedBlocks": ["## railway\nRailway MCP server."]}},
        human(0, "P1", "上线前检查一遍"),
        asst(1, "m1", "r1", tool_use("sk", "Skill", {"skill": "ops"}), usage(out=5)),
        result(2, "sk", "Launching skill: ops"),
        asst(3, "m2", "r2", tool_use("cfg", "Bash", {"command": "cat .railway/config.json", "description": "Read config"}),
             usage(out=5), skill="ops"),
        result(4, "cfg", json.dumps({"project": PID})),
        asst(5, "m3", "r3", tool_use("ls", "mcp__railway__list_services", {"project_id": PID}), usage(out=5), skill="ops"),
        result(6, "ls", "[]"),
        asst(7, "m4", "r4", tool_use("t1", "Bash", {"command": "npm test"}), usage(out=5)),
        result(9, "t1", "FAIL", err=True),
        asst(10, "m5", "r5", tool_use("ed", "Edit", {"file_path": "a.js", "old_string": "x", "new_string": "y"}), usage(out=5)),
        result(11, "ed", "ok"),
        asst(11.2, "m5b", "r5b", tool_use("rmx", "Bash", {"command": "rm -rf build"}), usage(out=5)),
        result(11.5, "rmx", "ok"),
        asst(12, "m6", "r6", tool_use("t2", "Bash", {"command": "npm test"}), usage(out=5)),
        result(14, "t2", "PASS"),
        {"type": "attachment", "timestamp": iso(15), "sessionId": SID,
         "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "顺便看下日志"}},
        asst(16, "m7", "r7", tool_use("wf", "Workflow", {"name": "wf-demo"}), usage(out=5)),
        result(17, "wf", "launched", tur={"status": "async_launched", "runId": "wf_demo-001", "workflowName": "wf-demo"}),
        asst(18, "m8", "r8", tool_use("ask", "AskUserQuestion", {"questions": [{"question": "现在上线吗?"}]}), usage(out=5)),
        result(40, "ask", "上"),
        asst(41, "m9", "r9", {"type": "text", "text": long_text}, usage(out=50)),
        asst(42, "m10", "r10", tool_use("lg", "mcp__railway__get_logs", {"service_id": PID}), usage(out=5)),
        result(43, "lg", "Failed to get logs: Unauthorized. Please run `railway login` again.", err=True),
    ])
    for aid, phase, desc, t in (("w1", "Investigate", "investigate:config", 17.5), ("w2", "Verify", "verify:all", 25)):
        write_jsonl(rdir / f"agent-{aid}.jsonl", [
            {"type": "user", "timestamp": iso(t), "sessionId": SID, "agentId": aid, "message": {"role": "user", "content": desc}},
            asst(t + 1, f"m-{aid}", f"r-{aid}", tool_use(f"g-{aid}", "Grep", {"pattern": "TODO"}), usage(out=7), agent=aid),
            result(t + 2, f"g-{aid}", "a.js:3", agent=aid),
        ])
        (rdir / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": "workflow-subagent", "description": desc,
                                                                  "workflowPhase": phase}), encoding="utf-8")
    return base


def _export(base, out: Path) -> dict:
    """像页面那样把接口都调一遍, 把返回写成脚手架要的数据文件。"""
    tasks = serve._wf_tasks(base, {"since": ["all"]})["tasks"]
    data = {"list": tasks, "tasks": [], "calls": {}, "texts": {}, "scripts": {}}
    for t in tasks:
        d = serve._wf_task(base, {"id": [t["id"]]})
        data["tasks"].append(d)
        for n in trace.walk([d["tree"]]):
            if n["kind"] == "call":
                data["calls"][n["id"]] = serve._wf_call(base, {"task": [t["id"]], "call": [n["id"]]})
            elif n["kind"] in ("say", "think") and n.get("long"):
                data["texts"][n["id"]] = serve._wf_text(base, {"task": [t["id"]], "node": [n["id"]]})
            elif n["kind"] == "workflow":
                data["scripts"][n["run_id"]] = serve._wf_script(base, {"task": [t["id"]], "run": [n["run_id"]]})
    st = serve._wf_stats(base, {"since": ["all"]})
    data["stats"] = st
    refs = {"tasks", "all", "chg-tasks", "chg-calls", "chg-inferred", "chg-unknown", "chg-after", "chg-noverify"}
    for r in st.get("risks") or []:
        refs |= {r["ref_tasks"], r["ref_calls"]}
    for r in st["tools"]:
        refs |= {r["ref"], r["ref_occ"]}
    for r in st["skills"]:
        refs |= {r["ref_occ"], r["ref_calls"]}
    for m in st["mcp"]:
        refs |= {m["ref"], m["ref_occ"]} | {x["ref"] for x in m["tools"]}
    for rows in st["compare"].values():
        refs |= {r["ref"] for r in rows}
    data["drills"] = {f"{ref}|{flag}": serve._wf_drill(base, {"since": ["all"], "stamp": [st["stamp"]], "ref": [ref],
                                                               "flag": [flag]})
                      for ref in refs for flag in ("", "fail", "retry", "loop", "slow", "huge", "partial")}
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_workflow_page_runs_clean(session, tmp_path):
    data = _export(session, tmp_path / "data.json")
    assert data["scripts"] and all("error" not in v for v in data["scripts"].values())
    assert all("error" not in v for v in data["texts"].values()) and data["texts"]
    proc = subprocess.run(["node", str(HARNESS), str(PAGE), str(tmp_path / "data.json")],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.stdout.strip(), proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["errors"] == [], report["errors"]
    assert report["drawers"] >= 10
    seen = report["seen"]
    for k in ("stage_strip", "band", "dep_badge", "script_view", "glossary", "full_text",
              "stats", "dots", "mcp_errors", "drill_jump", "changes", "chg_jump", "risks", "risk_jump"):
        assert seen[k], f"页面没有渲染出 {k}"
    assert report["stat_drills"] >= 15 and report["stat_jumps"] >= 15, report
