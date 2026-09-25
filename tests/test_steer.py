"""_do_steer 的 P7 allow-list / gate 单测 (serve 层)。**不 spawn 真进程** —— mock 掉 activity/runner。

盯死的护栏: 控制模式 gate / 空 prompt / cwd allow-list (挡任意路径) / 拒绝 resume 活会话 / 审计发生。
"""

import pytest

from tokmon import serve


@pytest.fixture
def env(monkeypatch):
    sessions = [
        {"session_id": "s-idle", "cwd": "C:/proj/A", "project": "A",
         "state": "AWAITING_USER", "tool_pending": False, "title": "t"},
        {"session_id": "s-busy", "cwd": "C:/proj/B", "project": "B",
         "state": "WORKING", "tool_pending": True, "title": "t"},
        {"session_id": "s-open", "cwd": "C:/proj/C", "project": "C",        # 空闲, 但还开在 VS Code 面板里
         "state": "AWAITING_USER", "tool_pending": False, "liveness": True, "title": "t"},
    ]
    monkeypatch.setattr(serve.activity, "snapshot", lambda base, live=None: {"sessions": sessions})
    monkeypatch.setattr(serve.procmon, "live_claude_index", lambda: None)
    calls = []

    def fake_steer(cwd, prompt, session_id=None, project=None, budget_usd=0.5):
        calls.append({"cwd": cwd, "prompt": prompt, "session_id": session_id, "project": project})
        return {"session_id": session_id or "new-uuid", "done": False, "ok": True, "reason": ""}

    monkeypatch.setattr(serve.runner.runner, "steer", fake_steer)
    audits = []
    monkeypatch.setattr(serve.control.plane, "audit_action",
                        lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(serve.control.plane, "remote_mode", True)   # 默认开, 各用例按需覆盖
    return {"calls": calls, "audits": audits, "monkeypatch": monkeypatch}


def test_rejected_when_control_mode_off(env):
    env["monkeypatch"].setattr(serve.control.plane, "remote_mode", False)
    r = serve._do_steer({"cwd": "C:/proj/A", "prompt": "hi"}, base=".")
    assert r["ok"] is False and r["reason"] == "control-mode-off"
    assert env["calls"] == []                    # 没起进程


def test_rejected_empty_prompt(env):
    r = serve._do_steer({"cwd": "C:/proj/A", "prompt": "   "}, base=".")
    assert not r["ok"] and r["reason"] == "empty-prompt"
    assert env["calls"] == []


def test_rejected_unknown_cwd(env):
    r = serve._do_steer({"cwd": "C:/Windows", "prompt": "rm -rf"}, base=".")
    assert not r["ok"] and r["reason"] == "unknown-cwd"   # allow-list 挡住任意路径
    assert env["calls"] == []


def test_spawn_known_cwd_ok_and_audited(env):
    r = serve._do_steer({"cwd": "C:/proj/A", "prompt": "do it"}, base=".")
    assert r["ok"] is True
    assert env["calls"][0]["cwd"] == "C:/proj/A"
    assert env["calls"][0]["session_id"] is None          # 新 spawn, 不是 resume
    assert env["audits"]                                    # 审计发生 (COMMAND_ISSUED)


def test_resume_rejects_busy_session(env):
    r = serve._do_steer({"session_id": "s-busy", "prompt": "hi"}, base=".")
    assert not r["ok"] and r["reason"] == "session-busy"   # 拒绝 resume 活会话 (防双写)
    assert env["calls"] == []


def test_resume_rejects_session_still_open_in_a_process(env):
    # 进程重启残留的回合现在判"等你"(tool_pending=False), 但进程还持有它: resume = 两个进程写同一 transcript
    r = serve._do_steer({"session_id": "s-open", "prompt": "hi"}, base=".")
    assert not r["ok"] and r["reason"] == "session-open"
    assert env["calls"] == []


def test_resume_idle_session_ok(env):
    r = serve._do_steer({"session_id": "s-idle", "prompt": "continue"}, base=".")
    assert r["ok"] is True
    assert env["calls"][0]["session_id"] == "s-idle"       # resume 带上 id
    assert env["calls"][0]["cwd"] == "C:/proj/A"           # cwd 来自快照, 非客户端


def test_unknown_session(env):
    r = serve._do_steer({"session_id": "nope", "prompt": "hi"}, base=".")
    assert not r["ok"] and r["reason"] == "unknown-session"
    assert env["calls"] == []


def test_audit_target_has_no_prompt_text(env):
    """§6: 审计/事件里绝不出现 prompt 正文。"""
    secret = "SECRET_PROMPT_TEXT_should_not_leak"
    serve._do_steer({"cwd": "C:/proj/A", "prompt": secret}, base=".")
    blob = repr(env["audits"])
    assert secret not in blob


# ---- S1-v2: _do_answer 的 P7 gate (回答被驱动会话的提问) ----

@pytest.fixture
def ansenv(monkeypatch):
    """mock 掉 runner 的 asks/answer, 只测 serve 层的 gate 与审计。"""
    pending = [{"id": "7", "session_id": "sess-abcdef12", "project": "A", "age_s": 3,
                "questions": [{"question": "选哪个框架?", "options": [{"label": "Flask"}]}]}]
    monkeypatch.setattr(serve.runner.runner, "asks", lambda: list(pending))
    called = []
    monkeypatch.setattr(serve.runner.runner, "answer",
                        lambda aid, picked: called.append((aid, picked)) or {"ok": True, "answered": 1})
    audits = []
    monkeypatch.setattr(serve.control.plane, "audit_action", lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(serve.control.plane, "remote_mode", True)
    return {"called": called, "audits": audits, "monkeypatch": monkeypatch}


def test_answer_rejected_when_control_mode_off(ansenv):
    ansenv["monkeypatch"].setattr(serve.control.plane, "remote_mode", False)
    r = serve._do_answer({"ask_id": "7", "answers": {"选哪个框架?": "Flask"}})
    assert r["ok"] is False and r["reason"] == "control-mode-off"
    assert ansenv["called"] == []              # 没碰 runner


def test_answer_rejected_missing_ask_id(ansenv):
    r = serve._do_answer({"answers": {"选哪个框架?": "Flask"}})
    assert not r["ok"] and r["reason"] == "missing-ask-id"
    assert ansenv["called"] == []


def test_answer_ok_and_audited(ansenv):
    r = serve._do_answer({"ask_id": "7", "answers": {"选哪个框架?": "Flask"}})
    assert r["ok"] is True
    assert ansenv["called"][0][0] == "7"
    assert ansenv["audits"], "每次作答都要审计 (COMMAND_ISSUED)"


def test_answer_audit_has_no_question_or_option_text(ansenv):
    """§5.4 内容最小化: 问题正文与选项文本**绝不**进审计/事件, 只记 {会话, 项目, answered}。"""
    serve._do_answer({"ask_id": "7", "answers": {"选哪个框架?": "Flask"}})
    blob = repr(ansenv["audits"])
    assert "选哪个框架" not in blob
    assert "Flask" not in blob
    assert "answered" in blob
