"""S1 对抗式 review 确认的 31 个问题的回归测试 —— 每个用例钉住一个曾经真实存在的错误。

用例名里的 #N 对应 review 的确认编号 (见 docs 里的 CHANGELOG 0.10.0 条目)。
"""

import base64
import json
import struct
import zlib

import pytest

from tokmon import parser, serve, trace
from test_trace import SID, T0, asst, human, iso, result, tool_use, usage, write_jsonl


@pytest.fixture
def clean(monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})


def _session(tmp_path, lines, agents=None):
    base = tmp_path / "projects"
    proj = base / "c--Users-u--vscode-demo"
    main = proj / f"{SID}.jsonl"
    write_jsonl(main, lines)
    for aid, alines in (agents or {}).items():
        write_jsonl(proj / SID / "subagents" / f"agent-{aid}.jsonl", alines)
    return base, main


def _tasks(main):
    return {t["id"]: t for t in trace.session_tasks(main)[0]}


def _nodes(built, pred):
    return [n for n in trace.walk([built["tree"]]) if pred(n)]


def _agent_lines(aid, t, prompt, out=11):
    return [
        {"type": "user", "timestamp": iso(t), "sessionId": SID, "agentId": aid, "isSidechain": True,
         "message": {"role": "user", "content": prompt}},
        asst(t + 1, f"m-{aid}", f"r-{aid}", {"type": "text", "text": "done"}, usage(out=out), agent=aid),
    ]


# ------------------------------------------------------------------ 时间 (#1 #9)

def test_idle_gap_and_trailing_command_do_not_stretch_task(clean, tmp_path):
    """#1 #9: 任务 18 秒就做完了, 10 小时后敲 /model 再问下一个 —— 上一个任务不能被报成 10 小时。"""
    lines = [
        human(0, "P1", "做一件小事"),
        asst(1, "m1", "r1", tool_use("t1", "Bash", {"command": "ls"}), usage(out=10)),
        result(3, "t1", "a b"),
        asst(18, "m2", "r2", {"type": "text", "text": "好了"}, usage(out=5)),
        {"type": "user", "timestamp": iso(36000), "sessionId": SID,
         "message": {"role": "user", "content": "<command-name>/model</command-name>\n<command-args>opus</command-args>"}},
        human(36010, "P2", "下一件事"),
        asst(36011, "m3", "r3", {"type": "text", "text": "ok"}, usage(out=5)),
    ]
    base, main = _session(tmp_path, lines)
    ts = _tasks(main)
    b1 = trace.build_task(ts["P1"])
    assert b1["summary"]["time"]["active"] == pytest.approx(18, abs=0.01)
    assert b1["summary"]["t1"] == pytest.approx(T0 + 18, abs=0.01)
    cmd_in_p2 = [r for s in ts["P2"]["segments"] for r in s["rows"] if r.get("ev") == "command"]
    assert cmd_in_p2, "/model 属于下一轮"


def test_long_idle_inside_a_segment_is_not_model_time(clean, tmp_path):
    """#9: 同一轮里停了 3 小时 (比如恢复会话后才继续), 空闲不能算「模型生成」。"""
    lines = [
        human(0, "P1", "开始"),
        asst(5, "m1", "r1", {"type": "text", "text": "先这样"}, usage(out=5)),
        asst(10800, "m2", "r2", {"type": "text", "text": "继续"}, usage(out=5)),
    ]
    base, main = _session(tmp_path, lines)
    s = trace.build_task(_tasks(main)["P1"])["summary"]
    assert s["time"]["model"] == pytest.approx(5, abs=0.01)
    assert s["time"]["span"] == pytest.approx(10800, abs=0.01)   # 跨度照实, 但活跃只有 5 秒


# ------------------------------------------------------------------ agent 回链 (#2)

def test_fallback_never_steals_an_explicit_agent(clean, tmp_path):
    """#2(a): 被打断的调用 (无 agentId) 与重试 (有 agentId 'aaa') 同一条指令 ——
    重试拿自己的 aaa, 被打断的按「指令全文 + 启动时间」唯一匹配到自己的 bbb, token 全部对得上。"""
    prompt = "Review the payment module carefully and report issues with file:line references."
    lines = [
        human(0, "P1", "审一下"),
        asst(1, "m1", "r1", tool_use("tu1", "Agent", {"description": "review", "prompt": prompt}), usage(out=10)),
        result(20, "tu1", "[Request interrupted by user for tool use]", err=True),
        asst(30, "m2", "r2", tool_use("tu2", "Agent", {"description": "review", "prompt": prompt}), usage(out=10)),
        result(60, "tu2", "report", tur={"agentId": "aaa", "status": "completed"}),
    ]
    base, main = _session(tmp_path, lines, {"bbb": _agent_lines("bbb", 2, prompt, out=31),
                                            "aaa": _agent_lines("aaa", 31, prompt, out=47)})
    built = trace.build_task(_tasks(main)["P1"])
    calls = {c["id"]: c for c in _nodes(built, lambda n: n["kind"] == "call")}
    assert calls["tu2"]["children"][0]["agent_id"] == "aaa"
    assert calls["tu1"]["children"][0]["agent_id"] == "bbb"
    assert "linked-by-prompt" in calls["tu1"]["children"][0]["flags"]
    p_total = sum(r.total_tokens for r in parser.load_records(base))
    assert built["summary"]["tokens"]["total"] == p_total


def test_fallback_respects_time_window_across_tasks(clean, tmp_path):
    """#2(b): P1 里被拒的 Agent 调用 (没生成文件) 不能认领 P2 后来用同一指令起的 agent。"""
    prompt = "Write the platform route tests for the new endpoints, run them, and report."
    lines = [
        human(0, "P1", "写测试"),
        asst(1, "m1", "r1", tool_use("tu1", "Agent", {"description": "tests", "prompt": prompt}), usage(out=10)),
        result(2, "tu1", "User rejected tool use", err=True),
        human(100, "P2", "再来"),
        asst(101, "m2", "r2", tool_use("tu2", "Agent", {"description": "tests", "prompt": prompt}), usage(out=10)),
        result(150, "tu2", "done", tur={"agentId": "ccc"}),
    ]
    base, main = _session(tmp_path, lines, {"ccc": _agent_lines("ccc", 102, prompt, out=40)})
    ts = _tasks(main)
    b1, b2 = trace.build_task(ts["P1"]), trace.build_task(ts["P2"])
    tu1 = _nodes(b1, lambda n: n.get("id") == "tu1")[0]
    assert tu1["children"] == [] and tu1["meta"].get("unlinked")
    assert b1["summary"]["tokens"]["total"] + b2["summary"]["tokens"]["total"] == \
        sum(r.total_tokens for r in parser.load_records(base))   # 不再重复计数


def test_fallback_ambiguous_is_left_unlinked(clean, tmp_path):
    """#2(c): 两个候选都对得上 -> 不猜, 标 ambiguous。"""
    prompt = "Same templated instruction for both parallel reviewers, word for word identical."
    lines = [
        human(0, "P1", "并行审"),
        asst(1, "m1", "r1", tool_use("tu1", "Agent", {"description": "r", "prompt": prompt}), usage(out=10)),
    ]
    base, main = _session(tmp_path, lines, {"x1": _agent_lines("x1", 2, prompt), "x2": _agent_lines("x2", 2, prompt)})
    tu1 = _nodes(trace.build_task(_tasks(main)["P1"]), lambda n: n.get("id") == "tu1")[0]
    assert tu1["children"] == [] and "ambiguous" in tu1["flags"]


# ------------------------------------------------------------------ 运行态 (#3)

def test_running_goes_to_task_owning_the_tail(clean, tmp_path):
    """#3: 旧任务的后台延续正在跑 -> 运行中的是它, 不是最后一个提问。"""
    lines = [
        human(0, "P1", "启动 workflow"),
        asst(1, "m1", "r1", tool_use("wf1", "Workflow", {"name": "w"}), usage(out=10)),
        result(2, "wf1", "launched", tur={"status": "async_launched", "runId": "wf_zz"}),
        human(10, "P2", "顺便问一句"),
        asst(11, "m2", "r2", {"type": "text", "text": "答"}, usage(out=5)),
        {"type": "user", "timestamp": iso(50), "sessionId": SID, "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": "<task-notification><tool-use-id>wf1</tool-use-id><status>completed</status></task-notification>"}},
        asst(51, "m3", "r3", tool_use("b9", "Bash", {"command": "npm test"}), usage(out=8)),
    ]
    base, main = _session(tmp_path, lines)
    rows = trace.list_tasks(base, running_sessions={SID})
    by = {r["id"]: r for r in rows}
    assert by["P1"]["running"] is True and by["P2"]["running"] is False
    b1 = trace.get_task("P1", base, running_sessions={SID})
    b9 = _nodes(b1, lambda n: n.get("id") == "b9")[0]
    assert "running" in b9["flags"]


def test_pending_call_already_passed_is_not_running(clean, tmp_path):
    """#3: 时间线已经往下走了的无结果调用, 不能在运行中的任务里显示成「运行中」并一直涨时间。"""
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("lost", "Bash", {"command": "a"}), usage(out=5)),
        asst(5, "m2", "r2", tool_use("now", "Bash", {"command": "b"}), usage(out=5)),
    ]
    base, main = _session(tmp_path, lines)
    b = trace.build_task(_tasks(main)["P1"], running=True)
    calls = {c["id"]: c for c in _nodes(b, lambda n: n["kind"] == "call")}
    assert "no-result" in calls["lost"]["flags"] and "running" in calls["now"]["flags"]


# ------------------------------------------------------------------ 缺失不写 0 (#4 #17 #21)

def test_missing_workflow_dir_and_missing_result_are_none(clean, tmp_path):
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("wf1", "Workflow", {"name": "w"}), usage(out=5)),
        result(2, "wf1", "launched", tur={"status": "async_launched", "runId": "wf_gone"}),
        asst(3, "m2", "r2", tool_use("c1", "Bash", {"command": "x"}), usage(out=5)),
    ]
    base, main = _session(tmp_path, lines)
    b = trace.build_task(_tasks(main)["P1"])
    wf = _nodes(b, lambda n: n["kind"] == "workflow")[0]
    assert wf["tokens"] is None and "missing" in wf["flags"]
    c1 = _nodes(b, lambda n: n.get("id") == "c1")[0]
    assert c1["result_est"] is None and c1["meta"]["result_chars"] is None
    assert b["summary"]["partial"] >= 1                     # 合计是下限, 页面标 ≥


# ------------------------------------------------------------------ 结果体积 (#5 #12 #13)

def _png_b64(w, h):
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + chunk + b"\x00" * 64).decode()


def test_image_tokens_from_header():
    assert trace.image_tokens(_png_b64(750, 100)) == 100
    assert trace.image_tokens(_png_b64(4000, 4000)) == trace.IMAGE_TOKEN_CAP
    assert trace.image_tokens("not-an-image") is None


def test_result_sizes_media_skill_and_persisted(clean, tmp_path):
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(750, 100)}}
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("shot", "Read", {"file_path": "a.png"}), usage(out=5)),
        {"type": "user", "timestamp": iso(2), "sessionId": SID,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "shot", "content": [img]}]}},
        asst(3, "m2", "r2", tool_use("ts", "ToolSearch", {"query": "select:X"}), usage(out=5)),
        {"type": "user", "timestamp": iso(4), "sessionId": SID,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "ts",
                                                  "content": [{"type": "tool_reference", "tool_name": "X"}]}]}},
        asst(5, "m3", "r3", tool_use("sk", "Skill", {"skill": "big-skill"}), usage(out=5)),
        result(6, "sk", "Launching skill: big-skill"),
        {"type": "user", "isMeta": True, "sourceToolUseID": "sk", "timestamp": iso(6.1), "sessionId": SID,
         "message": {"role": "user", "content": [{"type": "text", "text": "S" * 8000}]}},
        asst(7, "m4", "r4", tool_use("big", "Bash", {"command": "cat log"}), usage(out=5)),
        result(8, "big", "preview only", tur={"persistedOutputPath": "C:/tmp/out.txt", "stdout": "", "stderr": ""}),
    ]
    base, main = _session(tmp_path, lines)
    calls = {c["id"]: c for c in _nodes(trace.build_task(_tasks(main)["P1"]), lambda n: n["kind"] == "call")}
    assert calls["shot"]["result_est"] == 100                    # 图片按尺寸估, 不是「[图片]」的 3 个字
    assert calls["ts"]["result_est"] is None                     # 工具定义块估不出 -> ≈?
    assert calls["sk"]["result_est"] >= 2000 and calls["sk"]["meta"]["skill_body"]   # skill 正文算进来
    assert "persisted" in calls["big"]["flags"] and "huge" not in calls["big"]["flags"]


# ------------------------------------------------------------------ 树的顺序 (#6)

def test_interjection_inside_skill_keeps_time_order(clean, tmp_path):
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("sk", "Skill", {"skill": "ops"}), usage(out=5)),
        result(2, "sk", "Launching skill: ops"),
        asst(3, "m2", "r2", tool_use("a", "Bash", {"command": "a"}), usage(out=5), skill="ops"),
        result(4, "a", "ok"),
        {"type": "attachment", "timestamp": iso(5), "sessionId": SID,
         "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": "停一下"}},
        asst(6, "m3", "r3", tool_use("b", "Bash", {"command": "b"}), usage(out=5), skill="ops"),
        result(7, "b", "ok"),
    ]
    base, main = _session(tmp_path, lines)
    kinds = [n["kind"] for n in trace.build_task(_tasks(main)["P1"])["tree"]["children"]]
    assert kinds == ["skill", "user", "skill"]                  # 插话在两段 skill 之间, 而不是排在最后


# ------------------------------------------------------------------ 脚本解析 (#8)

def test_script_meta_is_string_aware():
    src = """export const meta = {
  name: 'wf', description: "has } and ] inside",
  phases: [
    { title: 'Grep', detail: 'grep arr[0] usages' },
    { title: 'Fix', detail: 'close the } brace' },
  ],
}"""
    m = trace.parse_script_meta(src)
    assert [p["title"] for p in m["phases"]] == ["Grep", "Fix"]
    assert m["phases"][0]["detail"] == "grep arr[0] usages" and m["phases"][1]["detail"] == "close the } brace"
    assert m["description"] == "has } and ] inside"


# ------------------------------------------------------------------ 后台失败 (#11)

def test_failed_background_task_is_a_failure(clean, tmp_path):
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("bgrun", "Bash", {"command": "npm run regress", "run_in_background": True}),
             usage(out=5)),
        result(2, "bgrun", "started", tur={"backgroundTaskId": "bt1", "stdout": "", "stderr": ""}),
        {"type": "user", "timestamp": iso(60), "sessionId": SID, "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": "<task-notification><task-id>bt1</task-id><tool-use-id>bgrun</tool-use-id>"
                                                "<status>failed</status><summary>exit code 1</summary></task-notification>"}},
        asst(61, "m2", "r2", {"type": "text", "text": "回归失败了"}, usage(out=5)),
    ]
    base, main = _session(tmp_path, lines)
    b = trace.build_task(_tasks(main)["P1"])
    call = _nodes(b, lambda n: n.get("id") == "bgrun")[0]
    assert "bg-fail" in call["flags"]
    ev = _nodes(b, lambda n: n["kind"] == "event" and n.get("ev") == "notify-fail")[0]
    assert ev["label"].startswith("后台任务失败")
    assert b["summary"]["flags"].get("bg-fail") == 1


# ------------------------------------------------------------------ 占位消息 / 关键时刻 (#14 #22)

def test_synthetic_placeholder_is_not_a_reply(clean, tmp_path):
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", {"type": "text", "text": "真正的回复"}, usage(out=5)),
        asst(90000, "syn", "rs", {"type": "text", "text": "No response requested."},
             {"input_tokens": 0, "output_tokens": 0}, model="<synthetic>"),
    ]
    base, main = _session(tmp_path, lines)
    b = trace.build_task(_tasks(main)["P1"])
    says = _nodes(b, lambda n: n["kind"] == "say")
    assert [n["label"] for n in says] == ["真正的回复"]
    done = [m for m in b["summary"]["moments"] if m["kind"] == "done"][0]
    assert done["t"] == pytest.approx(T0 + 1, abs=0.01)
    assert b["summary"]["t1"] == pytest.approx(T0 + 1, abs=0.01)      # 不被 25 小时后的占位撑开


def test_compaction_is_one_moment(clean, tmp_path):
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", {"type": "text", "text": "a"}, usage(out=5)),
        {"type": "system", "subtype": "compact_boundary", "timestamp": iso(2), "sessionId": SID},
        {"type": "user", "isCompactSummary": True, "timestamp": iso(2.2), "sessionId": SID,
         "message": {"role": "user", "content": "continued..."}},
        asst(3, "m2", "r2", {"type": "text", "text": "b"}, usage(out=5)),
    ]
    base, main = _session(tmp_path, lines)
    ms = trace.build_task(_tasks(main)["P1"])["summary"]["moments"]
    assert sum(1 for m in ms if m["label"] == "上下文压缩") == 1


# ------------------------------------------------------------------ 慢 (#15)

def test_deliberate_waits_are_never_slow(clean, tmp_path):
    lines = [
        human(0, "P1", "x"),
        asst(1, "m1", "r1", tool_use("w", "Bash", {"command": "sleep 60", "description": "Wait for build"}),
             usage(out=5)),
        result(61, "w", ""),
    ]
    base, main = _session(tmp_path, lines)
    b = trace.build_task(_tasks(main)["P1"], baseline={"Bash": {"n": 100, "p90": 2.0}})
    assert "slow" not in _nodes(b, lambda n: n.get("id") == "w")[0]["flags"]


# ------------------------------------------------------------------ 全文 (#25)

def test_say_full_text_keeps_newlines(clean, tmp_path):
    long_text = "第一行\n" + ("x" * 900) + "\n最后一行"
    lines = [human(0, "P1", "x"), asst(1, "m1", "r1", {"type": "text", "text": long_text}, usage(out=5))]
    base, main = _session(tmp_path, lines)
    got = trace.get_task("P1", base)
    say = _nodes(got, lambda n: n["kind"] == "say")[0]
    assert say["long"] is True and len(say["label"]) <= trace.TEXT_CHARS
    full = trace.get_text_detail("P1", say["id"], base)
    assert full["text"] == long_text


# ------------------------------------------------------------------ 安全 (#26 #28 #29)

def test_known_secret_values_are_masked(monkeypatch):
    """#26: transcript 里打印过的控制令牌, 回放页绝不原样送出 (它能解锁控制接口)。"""
    monkeypatch.setattr(serve.control.plane, "token", "tok_live_ABCDEFGH12345678")
    monkeypatch.setattr(serve, "_WF_SECRETS", {"t": 0.0, "vals": ()})
    out = serve._wf_red("Set-Cookie: mc_read=tok_live_ABCDEFGH12345678; Path=/")
    assert "tok_live_ABCDEFGH12345678" not in out


def test_truncation_never_leaves_a_secret_fragment():
    """#28: 截断点落在长串中间时整段丢掉, 不留下 38/40 个字符的半截 PAT。"""
    s = "x" * 120 + " ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    out = trace.short(s, 160)
    assert "ghp_A1b2" not in out and out.endswith("…")


def test_structured_secrets_are_masked_by_key():
    """#29: {"password": ...} 这种字段名说明是密钥的值, 整个打码。"""
    d = serve._wf_red_obj({"password": "hunter2hunter2", "apiKey": "abcd1234efgh5678",
                           "variables": {"SESSION_SECRET": "s3cr3tvalue99"}, "lines": 60})
    assert d["password"] == d["apiKey"] == d["variables"]["SESSION_SECRET"] == "***"
    assert d["lines"] == 60


def test_tree_payload_drops_raw_previews():
    tree = {"id": "task:x", "kind": "task", "children": [
        {"id": "c", "kind": "call", "label": "l", "meta": {"preview": "raw output", "key": "Bash|cmd"}}]}
    serve._wf_scrub_tree(tree)
    assert "preview" not in tree["children"][0]["meta"] and "key" not in tree["children"][0]["meta"]


# ------------------------------------------------------------------ 运行态不污染 /sessions (#27 #10)

def test_running_check_passes_liveness(monkeypatch):
    """#27: activity 的快照缓存是共享的, 不带 live 的调用会把一帧没有存活信息的结果喂给 /sessions。"""
    seen = {}

    def fake_snapshot(base, cfg=None, live=None):
        seen["live"] = live
        return {"sessions": [{"session_id": "s1", "state": "AMBIGUOUS_PENDING", "pending_tool_name": "AskUserQuestion"},
                             {"session_id": "s2", "state": "AWAITING_USER"}]}

    monkeypatch.setattr(serve.activity, "snapshot", fake_snapshot)
    monkeypatch.setattr(serve.procmon, "live_claude_index", lambda: "LIVE-INDEX")
    assert serve._wf_running("base") == {"s1"}             # 挂着等你的提问 = 正在跑
    assert seen["live"] == "LIVE-INDEX"
