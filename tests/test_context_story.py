"""省钱 S3 · 上下文是怎么涨起来的 (trace.context_story): 主线程每一轮的上下文 (usage 真值), 大涨归到期间回来的调用,
压缩 / 离开超过缓存有效期后的整段重建; 子 agent 的轮次不进主线程曲线; 调用标签过脱敏。"""

import json

import pytest

from tokmon import parser, serve, trace
from test_trace import SID, T0, asst, human, result, tool_use, write_jsonl

SECRET = "CTX-SECRET-424242"


def u(inp=10, out=20, cread=0, c1h=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cread,
            "cache_creation_input_tokens": c1h,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": c1h}}


@pytest.fixture
def story(tmp_path, monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {}), ("_BRIEF", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})
    monkeypatch.setattr(serve, "_wf_known_secrets", lambda: (SECRET,))
    big = "x = 1  # filler\n" * 6000                       # 一个很大的文件内容 -> 很大的工具结果
    main = tmp_path / "projects" / "c--Users-u--vscode-demo" / f"{SID}.jsonl"
    write_jsonl(main, [
        human(0, "P1", "读一下这个大文件然后改"),
        asst(1, "m1", "r1", tool_use("rd", "Bash", {"command": f"cat C:/Users/u/demo/{SECRET}/big.py"}), u(inp=40_000, c1h=40_000)),
        result(2, "rd", big),
        asst(3, "m2", "r2", tool_use("ag", "Agent", {"description": "看看", "prompt": "看看"}),
             u(inp=100, cread=80_000, c1h=45_000, out=500)),                    # 涨了 45k: 大多是 Read 的结果
        result(20, "ag", "done", tur={"status": "completed", "agentId": "e1"}),
        asst(21, "m3", "r3", {"type": "text", "text": "压缩一下"}, u(inp=100, cread=125_000, c1h=200)),
        asst(22, "m4", "r4", {"type": "text", "text": "压缩后"}, u(inp=100, cread=0, c1h=30_000)),   # 骤降: 压缩 / 清空
        asst(22 + 7200, "m5", "r5", {"type": "text", "text": "两小时后回来"}, u(inp=50, cread=0, c1h=31_000)),  # 离开后整段重建
    ])
    write_jsonl(main.parent / SID / "subagents" / "agent-e1.jsonl", [
        {"type": "user", "timestamp": "x", "sessionId": SID, "agentId": "e1", "isSidechain": True,
         "message": {"role": "user", "content": "看看"}},
        asst(10, "me1", "re1", {"type": "text", "text": "子 agent 自己的大上下文"}, u(inp=5, cread=900_000), agent="e1"),
    ])
    tasks, _ = trace.session_tasks(main)
    return trace.build_task(tasks[-1]), main


def test_points_are_main_thread_usage_truth(story):
    built, _ = story
    C = built["summary"]["context"]
    assert [p[1] for p in C["points"]] == [80_000, 125_100, 125_300, 30_100, 31_050]   # 新输入 + 缓存读 + 缓存写
    assert [p[0] for p in C["points"]] == pytest.approx([T0 + 1, T0 + 3, T0 + 21, T0 + 22, T0 + 7222])
    assert C["n"] == 5 and C["first"] == 80_000 and C["last"] == 31_050 and C["max"] == 125_300
    assert 900_005 not in [p[1] for p in C["points"]]      # 子 agent 的轮次不进主线程曲线


def test_jump_attributed_to_the_call_whose_result_arrived_in_between(story):
    built, _ = story
    C = built["summary"]["context"]
    (j,) = C["jumps"]                                      # 只有 80k -> 125.1k 这一步 >= 2 万
    assert j["from"] == 80_000 and j["to"] == 125_100 and j["out"] == 20
    assert j["calls"][0]["id"] == "rd" and j["calls"][0]["est"] == j["tool_est"] > 0
    rd = next(c for c in trace.walk([built["tree"]]) if c.get("id") == "rd")
    assert j["tool_est"] == rd["result_est"]               # 归因用的就是回放里那一步的结果体积估算
    assert C["top_calls"][0]["id"] == "rd"
    assert 0 < C["coverage"] <= 1 and C["growth"] == 45_100 + 200 + 950


def test_compaction_and_rebuild_after_idle(story):
    C = story[0]["summary"]["context"]
    assert C["compactions"] == [{"t": pytest.approx(T0 + 22), "from": 125_300, "to": 30_100}]
    (r,) = C["rebuilds"]
    assert r["ctx"] == 31_050 and r["gap"] == 7200        # 空了 2 小时 > 1 小时有效期, 这一轮缓存写 >= 上下文一半


def test_serve_redacts_call_labels(story):
    built, _ = story
    raw = json.dumps(built["summary"]["context"], ensure_ascii=False)
    assert SECRET in raw                                   # 原件里有 (路径里带了密钥样的串)
    sm = serve._wf_scrub_summary(json.loads(json.dumps(built["summary"], default=list)))
    assert SECRET not in json.dumps(sm["context"], ensure_ascii=False)
