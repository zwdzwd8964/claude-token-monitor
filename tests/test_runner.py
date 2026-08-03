"""runner (steer 机械层, S1-v2 Agent SDK 版) 纯函数 + 失败安全单测。**不起真会话、不烧钱**。

注: 旧的 `build_args` / `parse_stream` 单测已随 CLI 驱动一并移除 —— 那两个纯函数编码的是
`claude -p` 的命令行/stream-json 不变量, SDK 驱动后不复存在 (RUNNER_SDK_PLAN §4「驱动方式替换」)。
本文件改为覆盖新机制的命门: **canUseTool 的纯决策** + **超时绝不替你选** (§9.5)。
"""

import asyncio
import time

from tokmon import runner


# ---- clean_env: spike 大发现 (嵌套标记必须 strip) ----

def test_clean_env_strips_nesting_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-vscode")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "xyz")
    monkeypatch.setenv("ANTHROPIC_KEEPME", "keep")
    e = runner.clean_env()
    # SDK 把 options.env 叠加在 os.environ 之上 -> "不写 key" 删不掉它, 必须显式覆盖成空串
    assert e["CLAUDECODE"] == ""
    assert e["CLAUDE_CODE_SESSION_ID"] == ""
    assert e.get("CLAUDE_CODE_ENTRYPOINT") == "claude-vscode"   # 这一个留给 SDK 自己设 sdk-py
    assert e.get("ANTHROPIC_KEEPME") == "keep"     # 鉴权类保留, 只中和嵌套标记


# ---- normalize_answers (纯函数): 只认真实存在的问题与选项 ----

_Q = [{"question": "框架?", "header": "F",
       "options": [{"label": "Flask"}, {"label": "FastAPI"}], "multiSelect": False}]


def test_normalize_answers_valid_label():
    assert runner.normalize_answers(_Q, {"框架?": "FastAPI"}) == {"框架?": "FastAPI"}


def test_normalize_answers_drops_forged_label():
    # 伪造一个从没给过的选项 -> 丢弃 (绝不把没给过的选项当成你的决定)
    assert runner.normalize_answers(_Q, {"框架?": "Django"}) == {}


def test_normalize_answers_accepts_index_key():
    assert runner.normalize_answers(_Q, {"0": "Flask"}) == {"框架?": "Flask"}


def test_normalize_answers_multiselect_joins():
    qs = [{"question": "选哪些?", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": True}]
    assert runner.normalize_answers(qs, {"选哪些?": ["A", "B"]}) == {"选哪些?": "A, B"}
    # 列表里混入伪造项 -> 只留真实的
    assert runner.normalize_answers(qs, {"选哪些?": ["A", "ZZZ"]}) == {"选哪些?": "A"}


def test_normalize_answers_garbage_in_empty_out():
    assert runner.normalize_answers(_Q, None) == {}
    assert runner.normalize_answers(_Q, {}) == {}
    assert runner.normalize_answers(None, {"x": "y"}) == {}


# ---- canUseTool 决策 (命门): 拦截 / 超时 deny / 作答 allow ----

def _turn():
    return runner.Turn(session_id="s1", project="P", cwd="C:/x", started=time.time())


def _ask_input():
    return {"questions": [{"question": "框架?", "header": "F",
                           "options": [{"label": "Flask"}, {"label": "FastAPI"}],
                           "multiSelect": False}]}


def test_can_use_tool_passes_through_other_tools():
    r, t = runner.Runner(), _turn()
    cb = r._make_can_use_tool(t)
    res = asyncio.run(cb("Bash", {"command": "ls"}, None))
    assert res.behavior == "allow"
    assert r.asks() == []                       # 非提问不挂起


def test_can_use_tool_timeout_denies_and_never_picks(monkeypatch):
    """🔴 P7 铁律: 不作答 -> 明确 deny, **绝不**自动挑一个选项 (那等于伪造你的决定)。"""
    monkeypatch.setattr(runner, "_ASK_TIMEOUT_S", 0.05)
    r, t = runner.Runner(), _turn()
    cb = r._make_can_use_tool(t)

    async def go():
        r._loop = asyncio.get_running_loop()
        return await cb("AskUserQuestion", _ask_input(), None)

    res = asyncio.run(go())
    assert res.behavior == "deny"
    assert "未作答" in res.message
    assert r.asks() == []                        # 挂起已清理, 不泄漏
    assert t.awaiting_ask is None


def test_can_use_tool_answer_wakes_and_allows():
    """你点了按钮 -> canUseTool 醒来 -> Allow 带着 answers 塞回会话 (A0 实测的核心机制)。"""
    r, t = runner.Runner(), _turn()
    r._turns[t.session_id] = t                   # steer() 真实路径会登记回合; answer() 要据此判活
    cb = r._make_can_use_tool(t)

    async def go():
        r._loop = asyncio.get_running_loop()
        task = asyncio.create_task(cb("AskUserQuestion", _ask_input(), None))
        for _ in range(50):                      # 等回调把 ask 挂起
            await asyncio.sleep(0.01)
            if r.asks():
                break
        pending = r.asks()
        assert len(pending) == 1 and pending[0]["session_id"] == "s1"
        assert t.awaiting_ask == pending[0]["id"]
        out = r.answer(pending[0]["id"], {"框架?": "FastAPI"})
        assert out["ok"] is True and out["answered"] == 1
        return await asyncio.wait_for(task, timeout=3)

    res = asyncio.run(go())
    assert res.behavior == "allow"
    assert res.updated_input["answers"] == {"框架?": "FastAPI"}
    assert res.updated_input["questions"]        # 原 input 其余字段保留
    assert r.asks() == []


def test_answer_failsafe_unknown_and_forged():
    r, t = runner.Runner(), _turn()
    r._turns[t.session_id] = t
    cb = r._make_can_use_tool(t)

    async def go():
        r._loop = asyncio.get_running_loop()
        task = asyncio.create_task(cb("AskUserQuestion", _ask_input(), None))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if r.asks():
                break
        aid = r.asks()[0]["id"]
        assert r.answer("no-such-id", {"框架?": "Flask"}) == {"ok": False, "reason": "unknown-ask"}
        # 伪造选项 -> 不当成你的决定, 也不唤醒
        assert r.answer(aid, {"框架?": "Django"})["reason"] == "no-valid-option"
        assert r.asks(), "被伪造答案拒绝后, 提问应仍挂着等真答案"
        r.answer(aid, {"框架?": "Flask"})        # 收尾, 别让 task 悬着
        return await asyncio.wait_for(task, timeout=3)

    res = asyncio.run(go())
    assert res.behavior == "allow"


# ---- steer 失败安全: 不起真会话 ----

def test_steer_failsafe_sdk_missing(monkeypatch):
    monkeypatch.setattr(runner, "_HAS_SDK", False)
    v = runner.runner.steer(cwd=".", prompt="hi")
    assert v["done"] and not v["ok"] and "SDK" in v["reason"]


def test_steer_failsafe_binary_missing(monkeypatch):
    monkeypatch.setattr(runner, "resolve_claude", lambda: None)
    v = runner.runner.steer(cwd=".", prompt="hi", session_id=None)
    assert v["done"] is True and v["ok"] is False
    assert "找不到" in v["reason"]           # 明确报「做不到」, 不起会话


def test_steer_failsafe_empty_prompt(monkeypatch):
    monkeypatch.setattr(runner, "resolve_claude", lambda: "C:/fake/claude.exe")
    v = runner.runner.steer(cwd=".", prompt="   ")
    assert v["done"] and not v["ok"] and "空 prompt" in v["reason"]


# ---- 评审确认项的回归测试 (S1-v2 对抗式 review, 2026-07-19) ----

def _two_q():
    return {"questions": [
        {"question": "框架?", "options": [{"label": "Flask"}, {"label": "FastAPI"}], "multiSelect": False},
        {"question": "数据库?", "options": [{"label": "SQLite"}, {"label": "PG"}], "multiSelect": False}]}


def test_missing_questions_pure():
    qs = _two_q()["questions"]
    assert runner.missing_questions(qs, {"框架?": "Flask"}) == ["数据库?"]
    assert runner.missing_questions(qs, {"框架?": "Flask", "数据库?": "PG"}) == []


def test_partial_answer_does_not_resolve_multi_question_ask():
    """🔴 高危回归: 多问题 ask 只答一题**绝不**唤醒会话 —— 半份答案不是你的决定。"""
    r, t = runner.Runner(), _turn()
    r._turns[t.session_id] = t
    cb = r._make_can_use_tool(t)

    async def go():
        r._loop = asyncio.get_running_loop()
        task = asyncio.create_task(cb("AskUserQuestion", _two_q(), None))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if r.asks():
                break
        aid = r.asks()[0]["id"]
        one = r.answer(aid, {"框架?": "Flask"})
        assert one["ok"] is True and one["pending"] == ["数据库?"]   # 诚实告知还差哪题
        await asyncio.sleep(0.05)
        assert not task.done(), "只答一题就唤醒了会话 -> 半份答案泄漏"
        assert r.asks(), "未答全时提问必须继续挂着"
        r.answer(aid, {"数据库?": "PG"})                              # 补齐 -> 才唤醒
        return await asyncio.wait_for(task, timeout=3)

    res = asyncio.run(go())
    assert res.behavior == "allow"
    assert res.updated_input["answers"] == {"框架?": "Flask", "数据库?": "PG"}


def test_cancelled_task_does_not_leak_ask_and_answer_is_honest():
    """🔴 高危回归: canUseTool 被取消 (client 拆除) 时 ask 必须摘掉; 之后作答**不得**报成功。"""
    r, t = runner.Runner(), _turn()
    r._turns[t.session_id] = t
    cb = r._make_can_use_tool(t)

    async def go():
        r._loop = asyncio.get_running_loop()
        task = asyncio.create_task(cb("AskUserQuestion", _ask_input(), None))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if r.asks():
                break
        aid = r.asks()[0]["id"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return aid

    aid = asyncio.run(go())
    assert r.asks() == [], "被取消后 ask 泄漏了 (会占满 _MAX_ASKS 并让后续作答假成功)"
    assert r.answer(aid, {"框架?": "Flask"})["reason"] == "unknown-ask"   # 绝不报 ok:True


def test_answer_on_dead_turn_reports_turn_gone():
    """回合已死 -> 没人接收, 必须明确报「做不到」而不是 answered。"""
    r, t = runner.Runner(), _turn()
    r._turns[t.session_id] = t
    cb = r._make_can_use_tool(t)

    async def go():
        r._loop = asyncio.get_running_loop()
        task = asyncio.create_task(cb("AskUserQuestion", _ask_input(), None))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if r.asks():
                break
        aid = r.asks()[0]["id"]
        t.done = True                       # 模拟回合已终结
        out = r.answer(aid, {"框架?": "Flask"})
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return out

    assert asyncio.run(go())["reason"] == "turn-gone"


def test_finish_reaps_pending_asks():
    """回合终结 -> 名下挂起提问一并收掉, UI 不留点不动的幽灵按钮。"""
    r, t = runner.Runner(), _turn()
    r._turns[t.session_id] = t
    cb = r._make_can_use_tool(t)

    async def go():
        r._loop = asyncio.get_running_loop()
        task = asyncio.create_task(cb("AskUserQuestion", _ask_input(), None))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if r.asks():
                break
        r._finish(t, False, reason="模拟回合失败")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    assert r.asks() == []


def test_ask_ids_are_not_reused_across_runners():
    """ask id 用 uuid, 不是进程内自增 —— 重启后陈旧页面点不中别的会话的新 ask。"""
    ids = set()
    for _ in range(3):
        r, t = runner.Runner(), _turn()
        r._turns[t.session_id] = t
        cb = r._make_can_use_tool(t)

        async def go():
            r._loop = asyncio.get_running_loop()
            task = asyncio.create_task(cb("AskUserQuestion", _ask_input(), None))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if r.asks():
                    break
            aid = r.asks()[0]["id"]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return aid
        ids.add(asyncio.run(go()))
    assert len(ids) == 3 and all(len(i) > 8 for i in ids)


def test_turns_are_evicted_not_unbounded(monkeypatch):
    monkeypatch.setattr(runner, "_HAS_SDK", False)      # 立即失败收尾, 不起真会话
    r = runner.Runner()
    for i in range(runner._MAX_TURNS_KEPT + 20):
        r.steer(cwd=".", prompt="x", session_id=f"s{i}")
    assert len(r._turns) <= runner._MAX_TURNS_KEPT


def test_sdk_flags_are_actually_emitted():
    """这两个选项**静默失败**: 写错了不报错, 只是行为悄悄变。故在「真实发出的命令行」层面钉死。

    - setting_sources=None 是 SDK 默认(=按 CLI 默认加载, 会带进 ~/.claude 的 M4 hook -> 自己咬自己),
      必须显式 ["project"] 才会发 --setting-sources=project。
    - 不写 system_prompt 时 SDK 发 `--system-prompt ""` (空系统提示词), 与旧 `claude -p` 不等价;
      preset 才能让 CLI 用回默认 Claude Code 提示词 (表现为不发这个 flag)。
    """
    sdk = __import__("claude_agent_sdk")
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport as S

    o = sdk.ClaudeAgentOptions(
        cwd=".", can_use_tool=lambda *a: None,
        system_prompt={"type": "preset", "preset": "claude_code"},
        strict_mcp_config=True, env=runner.clean_env(),
        setting_sources=["project"], max_budget_usd=0.5)
    o.session_id = "sid"
    o.cli_path = "C:/fake/claude.exe"
    tr = S(prompt="x", options=o)
    tr._cli_path = "C:/fake/claude.exe"
    cmd = [str(c) for c in tr._build_command()]

    assert any(c.startswith("--setting-sources=") for c in cmd), "user 级 settings 没被排除 -> M4 hook 会被继承"
    assert "user" not in next(c for c in cmd if c.startswith("--setting-sources="))
    assert "--system-prompt" not in cmd, "发了 --system-prompt 说明系统提示词被覆盖 (空提示词 regression)"
    assert "--strict-mcp-config" in cmd
