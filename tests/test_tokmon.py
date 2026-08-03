"""tokmon 单元测试。

钉住北极星架构不变量: I2(纯函数) / I3(定价集中) / I5(去重) / 以及 v0.2 的项目身份。

两种跑法:
    python -m pytest tests/        # 有 pytest
    python tests/test_tokmon.py    # 无依赖, 自带 runner
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone  # noqa: E402

from tokmon.activity import ActivityConfig, classify_state  # noqa: E402
from tokmon.doctor import build_lines, scan  # noqa: E402
from tokmon.parser import load_records  # noqa: E402
from tokmon.pricing import cost_usd  # noqa: E402
from tokmon.project import workspace_identity  # noqa: E402


# ---- 项目身份 (v0.2 核心) ----

def test_project_vscode_subfolder():
    wid = workspace_identity(r"c:\Users\zwdzw\.vscode\API")
    assert wid.project == "API"
    assert wid.under_anchor is True
    assert wid.subpath == ""


def test_project_deeper_cwd_rolls_up():
    # 更深的 cwd 归并到所属的 .vscode 子文件夹, 余下进 subpath
    wid = workspace_identity(r"c:\Users\zwdzw\.vscode\API\auto refresh strategy\prod_20260604")
    assert wid.project == "API"
    assert wid.subpath == "auto refresh strategy/prod_20260604"


def test_project_preserves_spaces():
    assert workspace_identity(r"c:\Users\zwdzw\.vscode\edgar api").project == "edgar api"
    assert workspace_identity(r"c:\Users\zwdzw\.vscode\news feed 0622").project == "news feed 0622"


def test_project_outside_vscode_falls_back_to_basename():
    wid = workspace_identity(r"C:\Users\zwdzw")
    assert wid.project == "zwdzw"
    assert wid.under_anchor is False


def test_project_case_insensitive_anchor_and_slashes():
    wid = workspace_identity("c:/Users/zwdzw/.VSCode/API")  # 大小写 + 正斜杠
    assert wid.project == "API"
    assert wid.under_anchor is True


def test_project_anchor_as_last_component():
    wid = workspace_identity(r"c:\Users\zwdzw\.vscode")
    assert wid.under_anchor is True
    assert wid.project == ".vscode"  # 没有子文件夹, 退化为 basename


def test_project_missing_cwd_returns_none():
    assert workspace_identity(None) is None
    assert workspace_identity("") is None


def test_project_nested_vscode_uses_closest():
    # 嵌套 .vscode: 取最近的那个, 项目=inner, subpath 不应漏进 '.vscode'
    wid = workspace_identity(r"c:\Users\zwdzw\.vscode\proj\.vscode\inner")
    assert wid.project == "inner"
    assert wid.subpath == ""


def test_project_drive_relative_path():
    # 驱动器相对路径 c:.vscode\API (盘符后无分隔符)
    wid = workspace_identity(r"c:.vscode\API")
    assert wid.project == "API"
    assert wid.under_anchor is True


# ---- 定价 (I3) ----

def test_pricing_opus_known():
    cost, known = cost_usd("claude-opus-4-8", 1_000_000, 0, 0, 0, 0)
    assert known is True
    assert abs(cost - 5.0) < 1e-9   # 1M input * $5/1M

    cost, _ = cost_usd("claude-opus-4-8", 0, 1_000_000, 0, 0, 0)
    assert abs(cost - 25.0) < 1e-9  # 1M output * $25/1M


def test_pricing_cache_multipliers():
    # 1M input 基准 $5 -> 5m写×1.25=6.25, 1h写×2=10, 读×0.1=0.5
    c5m, _ = cost_usd("claude-opus-4-8", 0, 0, 1_000_000, 0, 0)
    c1h, _ = cost_usd("claude-opus-4-8", 0, 0, 0, 1_000_000, 0)
    cr, _ = cost_usd("claude-opus-4-8", 0, 0, 0, 0, 1_000_000)
    assert abs(c5m - 6.25) < 1e-9
    assert abs(c1h - 10.0) < 1e-9
    assert abs(cr - 0.5) < 1e-9


def test_pricing_web_search():
    cost, _ = cost_usd("claude-haiku-4-5", 0, 0, 0, 0, 0, web_search=1000)
    assert abs(cost - 10.0) < 1e-9  # $10 / 1000 次


def test_pricing_unknown_model():
    cost, known = cost_usd("<synthetic>", 1_000_000, 1_000_000, 0, 0, 0)
    assert known is False
    assert cost == 0.0


# ---- 解析 + 去重 + 项目身份 (端到端) ----

def _write_session(base: Path, enc_dir: str, fname: str, cwd: str, lines: list[dict]):
    d = base / enc_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / fname
    out = [{"type": "user", "cwd": cwd, "timestamp": "2026-06-28T01:00:00.000Z"}]
    out.extend(lines)
    p.write_text("\n".join(json.dumps(x) for x in out), encoding="utf-8")
    return p


def _assistant(msg_id: str, req_id: str, inp: int, out: int):
    return {
        "type": "assistant",
        "requestId": req_id,
        "timestamp": "2026-06-28T01:00:01.000Z",
        "sessionId": "sess-1",
        "message": {
            "id": msg_id, "model": "claude-opus-4-8",
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": 0},
        },
    }


def test_parser_dedup_and_project_from_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dup = _assistant("msg-1", "req-1", 100, 50)
        _write_session(base, "c--Users-zwdzw--vscode-API", "s.jsonl",
                       r"c:\Users\zwdzw\.vscode\API", [dup, dup])  # 两条相同 -> 去重为 1
        records = load_records(base)
        assert len(records) == 1
        r = records[0]
        assert r.project == "API"          # 来自真实 cwd, 不是目录名
        assert r.under_vscode is True
        assert r.input_tokens == 100 and r.output_tokens == 50


def test_parser_per_record_cwd_attribution():
    # 同一文件内 cwd 中途变化: 每条记录按自己的 cwd 归属, 不被第一个 cwd 错算。
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        a1 = _assistant("m1", "r1", 10, 1)
        a1["cwd"] = r"c:\Users\zwdzw\.vscode\API"
        a2 = _assistant("m2", "r2", 20, 2)
        a2["cwd"] = r"c:\Users\zwdzw\.vscode\edgar api"   # 切到了另一个项目
        a3 = _assistant("m3", "r3", 30, 3)
        a3["cwd"] = r"c:\Users\zwdzw\.vscode\API\moomoo"  # API 内更深 -> subpath
        _write_session(base, "c--Users-zwdzw--vscode-API", "s.jsonl",
                       r"c:\Users\zwdzw\.vscode\API", [a1, a2, a3])
        recs = load_records(base)
        assert {r.project for r in recs} == {"API", "edgar api"}
        deep = [r for r in recs if r.message_id == "m3"][0]
        assert deep.project == "API" and deep.subpath == "moomoo"


def test_parser_vscode_only_filter():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_session(base, "c--Users-zwdzw--vscode-API", "a.jsonl",
                       r"c:\Users\zwdzw\.vscode\API", [_assistant("m1", "r1", 10, 1)])
        _write_session(base, "c--Users-zwdzw", "b.jsonl",
                       r"C:\Users\zwdzw", [_assistant("m2", "r2", 20, 2)])
        assert len(load_records(base)) == 2
        only = load_records(base, vscode_only=True)
        assert len(only) == 1
        assert only[0].project == "API"


# ---- doctor 体检 ----

def test_doctor_scan_counts_and_warns():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        good = _assistant("m1", "r1", 10, 5)
        good["cwd"] = r"c:\Users\zwdzw\.vscode\API"
        no_ts = _assistant("m2", "r2", 1, 1)
        del no_ts["timestamp"]                       # 触发 would_drop
        unk = _assistant("m3", "r3", 100, 0)
        unk["message"]["model"] = "<synthetic-x>"    # 未知模型, 有 token
        _write_session(base, "c--Users-zwdzw--vscode-API", "s.jsonl",
                       r"c:\Users\zwdzw\.vscode\API", [good, no_ts, unk])
        rep = scan(base)
        assert rep.assistant_total == 3
        assert rep.assistant_usage == 3
        assert rep.would_drop_no_ts == 1 and rep.ts_ok == 2
        assert rep.unknown_models.get("<synthetic-x>") == (1, 100)
        _, warns = build_lines(rep)
        assert warns >= 1


# ---- M1 对话活动: 状态机 (纯函数, 钉住"误报零容忍") ----

_ACFG = ActivityConfig()
_NOW = 1_750_000_000.0


def _iso(age_s):
    return datetime.fromtimestamp(_NOW - age_s, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _amsg(blocks, stop=None, model="claude-opus-4-8", age=5):
    return {"type": "assistant", "timestamp": _iso(age),
            "message": {"model": model, "stop_reason": stop, "content": blocks}}


def _umsg(content, age=5):
    return {"type": "user", "timestamp": _iso(age), "message": {"content": content}}


def test_activity_awaiting_user_end_turn():
    st = classify_state(_amsg([{"type": "text", "text": "Done."}], stop="end_turn"), _NOW, _ACFG)
    assert st["state"] == "AWAITING_USER"
    assert st["ambiguous"] is False and st["idle"] is False
    assert st["last_text"] == "Done."


def test_activity_stop_sequence_is_awaiting():
    assert classify_state(_amsg([{"type": "text", "text": "x"}], stop="stop_sequence"),
                          _NOW, _ACFG)["state"] == "AWAITING_USER"


def test_activity_idle_flag_does_not_alarm():
    st = classify_state(_amsg([{"type": "text", "text": "x"}], stop="end_turn", age=_ACFG.idle_after_s + 10),
                        _NOW, _ACFG)
    assert st["state"] == "AWAITING_USER" and st["idle"] is True
    assert st["ambiguous"] is False        # idle 永远不是告警 / 不 ambiguous


def test_activity_working_fresh_tool():
    st = classify_state(_amsg([{"type": "tool_use", "name": "Bash"}], stop="tool_use", age=5), _NOW, _ACFG)
    assert st["state"] == "WORKING"
    assert st["tool_pending"] is True and st["pending_tool_name"] == "Bash"
    assert st["ambiguous"] is False


def test_activity_stale_tool_is_ambiguous_never_permission():
    st = classify_state(_amsg([{"type": "tool_use", "name": "Bash"}], stop="tool_use",
                              age=_ACFG.stuck_after_s + 30), _NOW, _ACFG)
    assert st["state"] == "AMBIGUOUS_PENDING"          # 绝不 PERMISSION_NEEDED / STUCK
    assert st["state"] not in ("PERMISSION_NEEDED", "SESSION_STUCK")
    assert st["ambiguous"] is True
    assert "授权" in st["state_label"]                  # 诚实并列两种可能(含权限), 不断言


def test_activity_processing_tool_result():
    assert classify_state(_umsg([{"type": "tool_result", "is_error": False}], age=3),
                          _NOW, _ACFG)["state"] == "PROCESSING"


def test_activity_processing_task_notification():
    assert classify_state(_umsg("<task-notification>x</task-notification>", age=3),
                          _NOW, _ACFG)["state"] == "PROCESSING"


def test_activity_stale_user_continuation_is_ambiguous():
    assert classify_state(_umsg([{"type": "tool_result", "is_error": False}], age=_ACFG.stuck_after_s + 30),
                          _NOW, _ACFG)["state"] == "AMBIGUOUS_PENDING"


def test_activity_interrupted_is_awaiting_user():
    assert classify_state(_umsg("[Request interrupted by user]", age=5), _NOW, _ACFG)["state"] == "AWAITING_USER"


def test_activity_interrupt_list_form_is_awaiting_user():
    # 真实 transcript 里打断是 list 形态, 不是纯字符串 —— 不能误判成 PROCESSING (评审在实测数据上发现)
    m = _umsg([{"type": "text", "text": "[Request interrupted by user]"}], age=5)
    st = classify_state(m, _NOW, _ACFG)
    assert st["state"] == "AWAITING_USER" and st["ambiguous"] is False


def test_activity_naive_timestamp_is_unknown():
    # 无时区时间戳 -> UNKNOWN, 不按本地时区瞎算
    m = {"type": "assistant", "timestamp": "2026-06-29T03:00:00",
         "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "x"}]}}
    assert classify_state(m, _NOW, _ACFG)["state"] == "UNKNOWN"


def test_activity_missing_timestamp_is_unknown():
    m = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"}}
    assert classify_state(m, _NOW, _ACFG)["state"] == "UNKNOWN"


def test_activity_none_is_unknown():
    st = classify_state(None, _NOW, _ACFG)
    assert st["state"] == "UNKNOWN" and st["ambiguous"] is True


def test_activity_title_takes_last_ai_title():
    from tokmon.activity import _conversation_title
    objs = [{"type": "ai-title", "aiTitle": "旧标题"},
            {"type": "assistant", "message": {}},
            {"type": "ai-title", "aiTitle": "新标题"}]
    assert _conversation_title(objs) == "新标题"
    assert _conversation_title([{"type": "assistant", "message": {}}]) is None   # 无 ai-title -> None


def test_activity_current_step_last_assistant_text():
    from tokmon.activity import _current_step
    objs = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "早先的话"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": ""},                 # 思考为空(只存签名), 不该被选
            {"type": "text", "text": "我正在验证后端"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},  # 纯工具, 无文字
    ]
    text, kind = _current_step(objs)
    assert text == "我正在验证后端" and kind == "narration"   # 取最近非空叙述, 跳过纯工具消息和空思考


# ---- M2 事件总线 + 事件派生 (纯函数 derive_events + bus 幂等) ----

from tokmon.event_sources.activity_source import IDLE_STEPS, SessionMemo, derive_events  # noqa: E402
from tokmon.events import Event, EventBus  # noqa: E402

_M2NOW = 2_000_000_000.0


def _row(state, sid="s1", age=5, la=None, events=None, project="P"):
    la = la if la is not None else _M2NOW - age
    return {"session_id": sid, "project": project, "state": state,
            "last_activity_epoch": la, "last_activity_age_s": age,
            "state_label": "x", "recent_events": events or []}


def test_m2_new_session_seeds_and_started():
    evs, memo = derive_events(None, _row("WORKING"), _M2NOW)
    assert memo.last_state == "WORKING" and memo.seen
    assert [e.type for e in evs] == ["SESSION_STARTED"]


def test_m2_steady_state_emits_nothing():
    row = _row("AWAITING_USER", age=5)
    _, memo = derive_events(None, row, _M2NOW)          # seed
    evs, memo2 = derive_events(memo, row, _M2NOW)        # same state again
    assert evs == []                                     # 边沿触发: 状态没变 -> 不重复


def test_m2_task_completed_on_transition():
    _, memo = derive_events(None, _row("WORKING", age=5), _M2NOW)
    evs, _ = derive_events(memo, _row("AWAITING_USER", age=1), _M2NOW)
    assert "TASK_COMPLETED" in [e.type for e in evs]


def test_m2_idle_is_stepped_not_per_poll():
    # 进入空闲后, 同一台阶只发一次; 跨更高台阶再发一次
    base = SessionMemo(last_state="AWAITING_USER", since_epoch=_M2NOW - 5, last_fact_cursor=0,
                       last_idle_step=0, last_stuck_step=0)
    just_over = IDLE_STEPS[0] + 30
    evs1, m1 = derive_events(base, _row("AWAITING_USER", age=just_over), _M2NOW)
    assert [e.type for e in evs1] == ["SESSION_IDLE"]
    evs2, _ = derive_events(m1, _row("AWAITING_USER", age=just_over + 60), _M2NOW)
    assert evs2 == []                                    # 还在同一台阶 -> 不再发


def test_m2_stuck_never_permission_and_hedged():
    _, memo = derive_events(None, _row("WORKING", age=5), _M2NOW)
    evs, _ = derive_events(memo, _row("AMBIGUOUS_PENDING", age=200), _M2NOW)
    types = [e.type for e in evs]
    assert "SESSION_STUCK" in types
    assert "PERMISSION_NEEDED" not in types              # 绝不合成 permission
    stuck = [e for e in evs if e.type == "SESSION_STUCK"][0]
    assert stuck.payload.get("unresolved") is True


def test_m2_unknown_emits_nothing():
    _, memo = derive_events(None, _row("WORKING"), _M2NOW)
    evs, _ = derive_events(memo, _row("UNKNOWN"), _M2NOW)
    assert evs == []                                     # 读不出 -> 不报警


def test_m2_unknown_first_then_recovery_no_replay():
    # 首次见到就是 UNKNOWN(空文件), 恢复后不能把整条陈旧尾部重放成新事件 (评审发现的 P6 bug)
    old = int(_M2NOW - 4800)                              # 80min 前的陈旧事实
    _, memo = derive_events(None, _row("UNKNOWN", la=None, events=[]), _M2NOW)
    recovered = _row("AWAITING_USER", age=4800,
                     events=[{"kind": "task_completed", "epoch": old},
                             {"kind": "tool_returned_error", "name": "Bash", "epoch": old + 10}])
    evs, _ = derive_events(memo, recovered, _M2NOW)
    assert evs == []                                     # 静默重播种, 不补发陈旧历史


def test_m2_tool_error_fact_once():
    row1 = _row("WORKING", events=[{"kind": "tool_returned_error", "name": "Bash", "epoch": int(_M2NOW - 3)}])
    _, memo = derive_events(None, row1, _M2NOW)           # seed cursor past the fact
    evs, _ = derive_events(memo, row1, _M2NOW)            # same fact again
    assert evs == []                                      # 游标已过 -> 不重复


def test_m2_payload_minimization_drops_sensitive():
    e = Event.make("TASK_COMPLETED", session="s", timestamp=1, tool_name="Bash",
                   secret="LEAK", cmdline="rm -rf /")
    assert "tool_name" in e.payload
    assert "secret" not in e.payload and "cmdline" not in e.payload   # §6 只留 allow-list


def test_m2_bus_dedup_and_cursor():
    b = EventBus()
    e = Event.make("TASK_COMPLETED", session="s", timestamp=10, dedup_key="k1")
    b.emit(e)
    b.emit(Event.make("TASK_COMPLETED", session="s", timestamp=10, dedup_key="k1"))  # 同 key
    assert b.snapshot_meta()["last_seq"] == 1            # 第二条被 dedup, seq 不增
    b.emit(Event.make("SESSION_IDLE", session="s", timestamp=11, dedup_key="k2"))
    out = b.since(1)
    assert [x["dedup_key"] for x in out["events"]] == ["k2"]   # since 游标只给新的


# ---- M3 通知层: 策略引擎 (纯函数 decide) ----

from tokmon.notify import NotifyConfig, decide, summarize  # noqa: E402


def _ev(sev="warning", typ="SESSION_IDLE", session="s1"):
    return {"severity": sev, "type": typ, "session": session, "project": "P", "payload": {"age_s": 700}}


def test_m3_info_below_threshold_suppressed():
    cfg = NotifyConfig(push_min_severity="warning")
    push, reason = decide(_ev(sev="info"), cfg, {}, 0, 1000.0)
    assert push is False and reason == "below-threshold"


def test_m3_warning_pushes_by_default():
    push, reason = decide(_ev(sev="warning"), NotifyConfig(), {}, 0, 1000.0)
    assert push is True and reason == "push"


def test_m3_quiet_hours_only_critical():
    cfg = NotifyConfig(quiet_start=0, quiet_end=8)         # 当前 now 落在 [0,8) 之内
    import time as _t
    midnight = _t.mktime(_t.struct_time((2026, 6, 29, 3, 0, 0, 0, 0, -1)))  # 本地 03:00
    assert decide(_ev(sev="warning"), cfg, {}, 0, midnight)[0] is False      # warning 被静默
    assert decide(_ev(sev="warning"), cfg, {}, 0, midnight)[1] == "quiet-hours"
    assert decide(_ev(sev="critical"), cfg, {}, 0, midnight)[0] is True       # critical 仍放行


def test_m3_debounce_same_type_session():
    cfg = NotifyConfig(debounce_s=120)
    last = {("SESSION_IDLE", "s1"): 1000.0}
    assert decide(_ev(), cfg, last, 0, 1050.0)[1] == "debounce"   # 50s < 120s
    assert decide(_ev(), cfg, last, 0, 1200.0)[0] is True          # 200s 后放行


def test_m3_rate_limit():
    cfg = NotifyConfig(rate_max=3)
    assert decide(_ev(), cfg, {}, 3, 1000.0)[1] == "rate-limit"
    assert decide(_ev(), cfg, {}, 2, 1000.0)[0] is True


def test_m3_critical_bypasses_rate_limit_and_debounce():
    # §7: critical 优先推送, 不被限流/去抖挡掉 (评审发现 PROCESS_CRASHED 可能被淹没)
    cfg = NotifyConfig(rate_max=3, debounce_s=120)
    crit = _ev(sev="critical", typ="PROCESS_CRASHED")
    last = {("PROCESS_CRASHED", "s1"): 1000.0}
    assert decide(crit, cfg, last, 99, 1001.0)[0] is True      # 窗口满 + 刚推过, 仍放行
    assert decide(_ev(sev="warning"), cfg, last, 99, 1001.0)[0] is False  # warning 照样被挡


def test_m3_summary_is_minimal_no_secrets():
    # summarize 只取白名单字段, 不会带出敏感载荷 (即便 payload 里混入)
    ev = {"type": "TOOL_ERROR", "project": "P", "session": "s",
          "payload": {"tool_name": "Bash", "cmdline": "rm -rf /", "secret": "X"}}
    s = summarize(ev)
    assert "Bash" in s and "rm -rf" not in s and "X" not in s


# ---- M4 控制层: 远程审批 (失败安全 + P7) ----

import threading as _threading  # noqa: E402

from tokmon.control import ControlPlane, shape_hook_response  # noqa: E402
from tokmon.events import bus as _bus  # noqa: E402


def _mkplane():
    p = ControlPlane()
    p.token = "T"          # 固定 token, 不依赖磁盘
    return p


def test_m4_bad_token_defers():
    p = _mkplane()
    r = p.handle_permission({"session_id": "s", "tool_name": "Bash"}, "WRONG")
    assert r.get("_defer") is True and r["reason"] == "bad-token"


def test_m4_local_mode_defers_but_detects():
    p = _mkplane()                       # remote_mode 默认 False
    r = p.handle_permission({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}}, "T")
    assert r.get("_defer") is True and r["reason"] == "local-mode"   # 不阻塞, 回退本地


def test_m4_remote_mode_blocks_until_decision():
    p = _mkplane()
    p.set_mode(True)
    out = {}

    def worker():
        out["r"] = p.handle_permission({"session_id": "s", "tool_name": "Bash",
                                        "tool_input": {"command": "rm x"}}, "T")
    t = _threading.Thread(target=worker)
    t.start()
    # 等 pending 出现, 然后批准
    for _ in range(50):
        if p.status()["pending"]:
            break
        time.sleep(0.02)
    assert p.status()["pending"], "应出现一条待审批"
    pid = p.status()["pending"][0]["id"]
    assert p.resolve(pid, "allow") is True
    t.join(timeout=5)
    assert out["r"] == {"decision": "allow"}
    assert any(a["outcome"] == "allow" for a in p.status()["audit"])   # 审计有记录


def test_m4_resolve_rejects_bad_decision():
    p = _mkplane()
    assert p.resolve("nope", "allow") is False        # 不存在的 id
    assert p.resolve("1", "maybe") is False           # 非法决定


def test_m4_shape_hook_response():
    assert "decision" not in shape_hook_response({"_defer": True})["hookSpecificOutput"]
    allow = shape_hook_response({"decision": "allow"})
    assert allow["hookSpecificOutput"]["decision"]["behavior"] == "allow"


# ---- M3.5 成本预算: 阈值逻辑 + 事件 ----

from tokmon.event_sources import cost_source as _cost  # noqa: E402
from tokmon.events import EventBus as _EB  # noqa: E402


def test_m35_highest_threshold():
    thr = (70, 90, 100)
    assert _cost._highest_threshold(50, thr) is None        # 没到 70
    assert _cost._highest_threshold(75, thr) == 70
    assert _cost._highest_threshold(95, thr) == 90
    assert _cost._highest_threshold(120, thr) == 100        # 超 100% 也只到最高台阶


def test_m35_emit_budget_severity_and_dedup():
    # 90%+ = critical; 同 (scope,period,thr) 经总线 dedup 只入队一条
    b = _EB()
    orig = _cost.bus
    _cost.bus = b                       # 必须 patch cost_source 自己命名空间里的 bus
    try:
        assert _cost._emit_budget(100.0, 95.0, "daily", "2026-06-29", None, (70, 90, 100)) == 1
        assert _cost._emit_budget(100.0, 96.0, "daily", "2026-06-29", None, (70, 90, 100)) == 1
        evs = b.since(0)["events"]
        assert len(evs) == 1            # 总线 dedup -> 实际只一条
        assert evs[0]["severity"] == "critical" and evs[0]["payload"]["pct"] == 95
    finally:
        _cost.bus = orig


def test_m35_no_budget_no_event():
    b = _EB()
    orig = _cost.bus
    _cost.bus = b
    try:
        assert _cost._emit_budget(0, 50, "daily", "p", None, (70, 90, 100)) == 0   # 没上限 -> 不发
        assert _cost._emit_budget(100.0, 50.0, "daily", "p", None, (70, 90, 100)) == 0  # 50% 未越线
        assert _cost._emit_budget(float("inf"), 50.0, "daily", "p", None, (70, 90, 100)) == 0  # inf 上限 -> 不发
        assert b.since(0)["events"] == []
    finally:
        _cost.bus = orig


def test_m35_bus_dedup_key_refreshed_not_aged_out():
    # 长期越线的预算键被反复重发时, 不该老化出窗后又重响 (move_to_end 修复)
    b = _EB(ring_size=50, dedup_window=3)
    persistent = Event.make("TOKEN_BUDGET_WARNING", session="x", timestamp=1, dedup_key="BUDGET:keep")
    assert b.emit(persistent).seq == 1
    for i in range(10):                       # 灌入大量新键, 超过 dedup_window
        b.emit(Event.make("TOOL_ERROR", session="s", timestamp=100 + i, dedup_key="e%d" % i))
        b.emit(Event.make("TOKEN_BUDGET_WARNING", session="x", timestamp=1, dedup_key="BUDGET:keep"))  # 每轮重发
    again = b.since(0)["events"]
    assert sum(1 for e in again if e["dedup_key"] == "BUDGET:keep") == 1   # 始终只一条, 没被重响


# ---- M4.5 推断 doctor + doctor 发现的修复 ----

from tokmon.activity import _conversation_title as _ctitle  # noqa: E402
from tokmon.activity import _current_step as _cstep  # noqa: E402
from tokmon.inference_doctor import InferenceReport, build_inference_lines  # noqa: E402


def test_m45_custom_title_precedes_ai_title():
    objs = [{"type": "ai-title", "aiTitle": "AI标题"},
            {"type": "custom-title", "customTitle": "我的标题"}]
    assert _ctitle(objs) == "我的标题"                       # 用户自定义优先
    assert _ctitle([{"type": "ai-title", "aiTitle": "只有AI"}]) == "只有AI"


def test_m45_current_step_prefers_thinking():
    objs = [{"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "我在推理这一步"},
        {"type": "text", "text": "我在叙述"}]}}]
    text, kind = _cstep(objs)
    assert kind == "thinking" and text == "我在推理这一步"     # thinking 非空 -> 优先
    # thinking 为空 -> 回退叙述
    objs2 = [{"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": ""}, {"type": "text", "text": "只有叙述"}]}}]
    text2, kind2 = _cstep(objs2)
    assert kind2 == "narration" and text2 == "只有叙述"


def test_m45_max_tokens_is_processing_not_awaiting():
    m = _amsg([{"type": "text", "text": "被截断"}], stop="max_tokens", age=5)
    assert classify_state(m, _NOW, _ACFG)["state"] == "PROCESSING"   # 触顶=自动续写, 不是在等你


def test_m45_inference_doctor_flags_drift():
    # 未知行类型 + 未处理 stop_reason -> 该报 ⚠
    rep = InferenceReport(base="x", sessions=1, tails_read=1,
                          unknown_types={"weird-new-type": 3}, unhandled_stop={"surprise": 1},
                          stop_reasons={"end_turn": 1}, line_types={"assistant": 1})
    lines, warns = build_inference_lines(rep)
    assert warns >= 2
    assert any("未知" in ln for ln in lines) and any("surprise" in ln for ln in lines)


# ---- L2 准确率回测: 纯函数 oracle / 计分 / 修复 (合成、构造时就知道答案) ----

from tokmon.inference_backtest import (  # noqa: E402
    ORACLES, _danger, _is_correct, _is_intra_turn, _successor_kind, _truth)

_STRICT, _MEDIUM, _LENIENT = ORACLES


def _u(content):
    return {"type": "user", "message": {"content": content}}


def test_l2_truth_human_prompt_awaiting_vs_idle():
    hp = _u("帮我做个东西")                                  # 人类新提问 -> cur 当时在等用户
    assert _truth(hp, 60, 0, 600, 120, _MEDIUM) == "awaiting"
    assert _truth(hp, 1200, 0, 600, 120, _MEDIUM) == "idle"   # 大空档 -> idle


def test_l2_truth_continuation_working_vs_longtask():
    cont = _u([{"type": "tool_result", "is_error": False}])
    assert _truth(cont, 5, 0, 600, 120, _MEDIUM) == "working"
    assert _truth(cont, 300, 0, 600, 120, _MEDIUM) == "long_task"   # >stuck, <flight_max


def test_l2_truth_strict_vs_lenient_gray():
    cont = _u([{"type": "tool_result"}])
    # gap 3600s: > strict.flight_max(1800) 但 < medium(7200)/lenient(inf) -> 三把尺分化
    assert _truth(cont, 3600, 0, 600, 120, _STRICT) == "unjudgeable"
    assert _truth(cont, 3600, 0, 600, 120, _MEDIUM) == "long_task"
    assert _truth(cont, 3600, 0, 600, 120, _LENIENT) == "long_task"


def test_l2_truth_no_successor_and_gray():
    assert _truth(None, 0, 30, 600, 120, _MEDIUM) == "unjudgeable"      # 很新 -> 可能在飞
    assert _truth(None, 0, 99999, 600, 120, _MEDIUM) == "ended"         # 很老 -> 结束
    assert _truth(None, 0, 7200, 600, 120, _STRICT) == "unjudgeable"    # 中等年龄灰区
    assert _truth(None, 0, 7200, 600, 120, _LENIENT) == "ended"


def test_l2_interrupt_is_working():
    intr = _u([{"type": "text", "text": "[Request interrupted by user]"}])
    assert _truth(intr, 10, 0, 600, 120, _MEDIUM) == "working"          # 用户停掉一次在跑的活


def test_l2_scoring_correct_and_danger():
    assert _is_correct("ambiguous", "long_task") and _is_correct("ambiguous", "ended")
    assert not _is_correct("ambiguous", "working")        # 对冲只对它并列的三种
    assert _danger("working", "idle") == "over"           # 过度乐观
    assert _danger("awaiting", "working") == "false_done"  # 假完成
    assert _danger("working", "working") is None


def test_l2_classify_midturn_tooluse_is_working():
    # stop_reason=tool_use 但末块是 text/thinking -> WORKING (L2 回测发现并修的 bug)
    m = {"type": "assistant", "timestamp": _iso(5),
         "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "我来跑一下"}]}}
    assert classify_state(m, _NOW, _ACFG)["state"] == "WORKING"


def test_l2_classify_thinking_only_endturn_is_working():
    # 只有 thinking 块的 end_turn 行 = 回合中途被拆开的片段, 不是真结束 -> WORKING (review 修, 防误报 TASK_COMPLETED)
    m = {"type": "assistant", "timestamp": _iso(5),
         "message": {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "想一想…"}]}}
    assert classify_state(m, _NOW, _ACFG)["state"] == "WORKING"
    # 对照: 有 text 的 end_turn 仍是真结束 -> AWAITING
    m2 = {"type": "assistant", "timestamp": _iso(5),
          "message": {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "想"}, {"type": "text", "text": "好了"}]}}
    assert classify_state(m2, _NOW, _ACFG)["state"] == "AWAITING_USER"


def test_l2_background_task_notification_is_unjudgeable():
    # 后台 <task-notification> successor 见证不了前台回合 -> 不可判, 不冤枉 classify_state (review 修)
    bg = _u([{"type": "text", "text": "<task-notification><status>stopped</status></task-notification>"}])
    assert _successor_kind(bg) == "background"
    assert _truth(bg, 0.1, 0, 600, 120, _MEDIUM) == "unjudgeable"
    assert _truth(bg, 0.1, 0, 600, 120, _LENIENT) == "unjudgeable"


def test_l2_intra_turn_split_lines_skipped():
    # 同 message.id 的连续 assistant 行 = 回合内中途行, 活分类器 _last_message 看不到 -> 回测应跳过
    a1 = {"type": "assistant", "message": {"id": "msg_X", "content": [{"type": "thinking", "thinking": "t"}]}}
    a2 = {"type": "assistant", "message": {"id": "msg_X", "content": [{"type": "text", "text": "答"}]}}
    assert _is_intra_turn(a1, a2) is True
    # 不同 message.id (真回合边界) 不跳过; assistant->user 也不跳过
    a3 = {"type": "assistant", "message": {"id": "msg_Y", "content": [{"type": "text", "text": "新回合"}]}}
    assert _is_intra_turn(a1, a3) is False
    assert _is_intra_turn(a2, _u("人类提问")) is False


# ---- 准确率补丁: 后台任务在飞 (Bug1) + 进程活性消歧 (Bug2) ----

from tokmon.activity import _open_bg_tasks  # noqa: E402
from tokmon.procmon import LiveIndex, _norm_path, _resume_uuid  # noqa: E402


def _wf_launch(tool_id, name="Workflow", inp=None):
    return {"type": "assistant", "timestamp": _iso(5),
            "message": {"content": [{"type": "tool_use", "name": name, "id": tool_id, "input": inp or {}}]}}


def _task_notif(tool_id):
    body = "<task-notification><tool-use-id>%s</tool-use-id><status>completed</status></task-notification>" % tool_id
    return {"type": "user", "timestamp": _iso(3), "message": {"content": body}}


def _tool_result(tool_id, content, is_error=False):
    return {"type": "user", "timestamp": _iso(3), "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}]}}


def test_patch_open_bg_task_detection():
    ack = _tool_result("toolu_A", "Workflow launched in background. Task ID: wl1")
    assert _open_bg_tasks([_wf_launch("toolu_A"), ack]) == {"toolu_A"}                # 只有"已启动"回执 -> 在飞
    assert _open_bg_tasks([_wf_launch("toolu_A"), ack, _task_notif("toolu_A")]) == set()  # 完成通知 -> 关
    # 启动失败 (error 回执, 无完成通知) -> 收口, 不算在飞 (评审 high#3)
    assert _open_bg_tasks([_wf_launch("toolu_A"), _tool_result("toolu_A", "Invalid workflow script", True)]) == set()


def test_patch_sync_agent_not_background():
    # 缺省 run_in_background 的 Agent = 同步返回, 不算后台 (评审 high#3: 反转默认 + tool_result 收口)
    absent = _wf_launch("toolu_S", name="Agent", inp={})
    assert _open_bg_tasks([absent, _tool_result("toolu_S", "full agent result text")]) == set()
    # 显式后台 Agent + 只有启动回执 -> 在飞
    bg = _wf_launch("toolu_B", name="Agent", inp={"run_in_background": True})
    assert _open_bg_tasks([bg, _tool_result("toolu_B", "Agent launched in background. ID: x")]) == {"toolu_B"}


def test_patch_resume_uuid_forms():
    assert _resume_uuid("--resume", "abc") == "abc"
    assert _resume_uuid("-r", "abc") == "abc"
    assert _resume_uuid("--resume=abc", None) == "abc"
    assert _resume_uuid("-r=abc", None) == "abc"
    assert _resume_uuid("--resume", "--flag") is None      # 下一个是 flag, 不是 uuid
    assert _resume_uuid("--other", "abc") is None


def test_patch_bug1_background_overrides_completed():
    m = _amsg([{"type": "text", "text": "已启动 workflow, 完成会通知你"}], stop="end_turn")
    assert classify_state(m, _NOW, _ACFG)["state"] == "AWAITING_USER"                # 无 bg_open: 照旧
    st = classify_state(m, _NOW, _ACFG, bg_open=True)
    assert st["state"] == "WORKING" and st.get("background") is True and "后台" in st["state_label"]


def test_patch_bug2_liveness_resolves_ambiguous():
    m = _amsg([{"type": "tool_use", "name": "Bash"}], stop="tool_use", age=_ACFG.stuck_after_s + 60)
    assert classify_state(m, _NOW, _ACFG)["state"] == "AMBIGUOUS_PENDING"            # liveness 未知: 照旧对冲
    alive = classify_state(m, _NOW, _ACFG, liveness=True)
    assert alive["state"] == "WORKING" and alive["ambiguous"] is False               # 进程在跑 -> 长任务, 非卡住
    assert classify_state(m, _NOW, _ACFG, liveness=False)["state"] == "CLOSED"        # 进程没了 -> 已关闭


def test_patch_liveness_false_closes_any_state():
    await_msg = _amsg([{"type": "text", "text": "done"}], stop="end_turn")
    assert classify_state(await_msg, _NOW, _ACFG, liveness=False)["state"] == "CLOSED"   # 连"等你"也判已关闭
    assert classify_state(await_msg, _NOW, _ACFG, liveness=True)["state"] == "AWAITING_USER"  # True 不动确凿的等你


def test_patch_liveindex_three_valued():
    cwd_live = _norm_path("C:/proj/alpha")
    idx = LiveIndex({"sessX": cwd_live}, {cwd_live})
    assert idx.status("sessX", "C:/whatever") is True            # --resume 精确命中 -> 活
    assert idx.status("other", "C:/proj/alpha") is None          # 精确同项目 -> 未知
    assert idx.status("other", "C:/proj/alpha/sub/deep") is None  # 会话 cd 进子目录 -> 仍算相关, 不误杀 (评审 critical#1)
    assert idx.status("other", "C:/proj") is None                # 进程在子目录、会话在父 -> 也算相关
    assert idx.status("other", "C:/proj/beta") is False          # 无关项目 -> 确定已结束
    assert idx.status("other", "C:/proj/alphaXX") is False       # 前缀但非目录边界 -> 无关 (不被 startswith 误伤)
    assert idx.status("other", None) is None                     # 没记 cwd -> None, 别妄断 (评审 critical#1 推论)
    assert LiveIndex({}, set()).status("s", "C:/x") is None      # 空/缺 psutil -> None (不证伪)


def test_patch_closed_resets_background():
    m = _amsg([{"type": "text", "text": "done"}], stop="end_turn")
    st = classify_state(m, _NOW, _ACFG, liveness=False, bg_open=True)
    assert st["state"] == "CLOSED" and st.get("background") is False   # 死会话不算"后台在跑" (评审 low#6)


def test_patch_closed_state_emits_no_events():
    _, memo = derive_events(None, _row("WORKING", age=5), _M2NOW)
    evs, memo2 = derive_events(memo, _row("CLOSED", age=5), _M2NOW)     # WORKING->CLOSED 无事实 -> 不发
    assert evs == [] and memo2.last_state == "CLOSED"


def test_patch_closed_flushes_completion_fact():
    # WORKING->CLOSED 且带 task_completed 事实: 补发一次 TASK_COMPLETED 再收尾 (别吞掉真完成, 评审 medium#4)
    _, memo = derive_events(None, _row("WORKING", age=30), _M2NOW)
    row = _row("CLOSED", age=5, events=[{"kind": "task_completed", "epoch": int(_M2NOW - 4)}])
    evs, memo2 = derive_events(memo, row, _M2NOW)
    assert "TASK_COMPLETED" in [e.type for e in evs] and memo2.last_state == "CLOSED"
    # 但上一拍不是在干活 (AWAITING->CLOSED) 不补发 —— 只救"正干着就被关掉"的真完成
    _, m2 = derive_events(None, _row("AWAITING_USER", age=30), _M2NOW)
    evs2, _ = derive_events(m2, _row("CLOSED", age=5, events=[{"kind": "task_completed", "epoch": int(_M2NOW - 3)}]), _M2NOW)
    assert evs2 == []


# ---- 进程分类 + 终止执行器 (P7 白名单 · 身份复核 · 祖先提升) ----

from tokmon import procmon as _pm  # noqa: E402


def _pr(name, cmd=""):
    return {"name": name, "_raw_cmd": cmd}


def test_procctl_categorize_direct():
    assert _pm._cat_direct(_pr("cloudflared.exe")) == "cloudflare"
    assert _pm._cat_direct(_pr("railway.exe")) == "railway"
    assert _pm._cat_direct(_pr("node.exe", "node C:\\x\\node_modules\\@railway\\cli\\index.js up")) == "railway"
    assert _pm._cat_direct(_pr("node.exe", "node C:\\x\\node_modules\\.bin\\railway up")) == "railway"
    assert _pm._cat_direct(_pr("node.exe", "node C:\\railway\\server.js")) is None           # 文件夹名叫 railway -> 不算
    assert _pm._cat_direct(_pr("node.exe", "node C:\\proj\\deploy-railway")) is None          # endswith 已废弃 -> 不算
    assert _pm._cat_direct(_pr("Code.exe")) == "vscode"
    assert _pm._cat_direct(_pr("claude.exe")) == "vscode"
    # argv[0] 自身是扩展可执行体 -> vscode
    assert _pm._cat_direct(_pr("node.exe", "c:\\u\\.vscode\\extensions\\foo\\node.exe run")) == "vscode"
    # 关键回归 (评审 high): 参数里带 .vscode / shell-snapshots 路径但进程自身无关 -> 绝不算 vscode (否则会变可终止)
    assert _pm._cat_direct(_pr("powershell.exe", "powershell -File C:\\proj\\.vscode\\tasks\\deploy.ps1")) is None
    assert _pm._cat_direct(_pr("robocopy.exe", "robocopy C:\\u\\.vscode\\extensions D:\\backup /MIR")) is None
    assert _pm._cat_direct(_pr("tar.exe", "tar czf b.tgz /c/u/.claude/shell-snapshots/")) is None
    assert _pm._cat_direct(_pr("bash.exe", "bash -c source /c/u/.claude/shell-snapshots/s.sh")) is None  # 终端靠祖先认, 非直接
    assert _pm._cat_direct(_pr("svchost.exe", "svchost -k netsvcs")) is None


def _tree():
    return {
        10: {"pid": 10, "name": "Code.exe", "user": "u", "create_time": 100.0, "_raw_cmd": "Code.exe"},
        20: {"pid": 20, "name": "bash.exe", "user": "u", "create_time": 200.0,
             "_raw_cmd": "bash -c source /c/u/.claude/shell-snapshots/s.sh"},
        30: {"pid": 30, "name": "node.exe", "user": "u", "create_time": 300.0, "_raw_cmd": "node server.js"},
    }


def test_procctl_promote_ancestry():
    procs = _tree(); _pm._categorize(procs)
    assert procs[30]["category"] == "other"                 # 直接信号认不出 dev server
    _pm._promote_ancestry(procs, [30], {30: 20, 20: 10, 10: 1})   # 预置 ppid 缓存, 不碰真 psutil
    assert procs[30]["category"] == "vscode" and procs[30]["terminable"] is True   # 沿祖先提升
    procs = _tree(); procs[20]["create_time"] = 999.0; _pm._categorize(procs)      # 父晚于子 = 复用假边
    _pm._promote_ancestry(procs, [30], {30: 20, 20: 10, 10: 1})
    assert procs[30]["category"] == "other"
    procs = _tree(); procs[20]["user"] = "SYSTEM"; _pm._categorize(procs)          # 跨用户不认
    _pm._promote_ancestry(procs, [30], {30: 20, 20: 10, 10: 1})
    assert procs[30]["category"] == "other"


def test_procctl_terminate_gating():
    orig = _pm._fresh_proc_table
    tbl = {500: {"pid": 500, "name": "svchost.exe", "user": "SYSTEM", "create_time": 50.0,
                 "category": "other", "terminable": False},
           600: {"pid": 600, "name": "node.exe", "user": "u", "create_time": 60.0,
                 "category": "vscode", "terminable": True}}
    _pm._fresh_proc_table = lambda: {k: dict(v) for k, v in tbl.items()}
    try:
        assert _pm.terminate(500, 50.0)["reason"] == "not-terminable"     # 正确身份的 other 也拒绝 (核心安全属性)
        assert _pm.terminate(600, 999.0)["reason"] == "pid-reused"        # 身份对不上
        assert _pm.terminate(700, 1.0)["reason"] == "gone"               # 不存在
        assert _pm.terminate(600, None)["reason"] == "identity-required"  # 缺身份
        assert _pm.terminate(_pm.os.getpid(), 1.0)["reason"] == "refuse-self"  # 不杀监控自身
    finally:
        _pm._fresh_proc_table = orig


def test_procctl_serve_gated_by_control_mode():
    from tokmon import serve, control
    control.plane.set_mode(False)
    assert serve._do_terminate({"pid": 1, "create_time": 1.0})["reason"] == "control-mode-off"
    assert serve._do_free_port({"port": 1})["reason"] == "control-mode-off"
    control.plane.set_mode(False)


# ---- 无 pytest 时的自带 runner ----

if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
