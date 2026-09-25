"""会话状态准确率修复: 用 Claude Code 自己的会话注册表 (~/.claude/sessions/<pid>.json) 纠正 transcript 推断。

每条测试钉住一个实测到的误判 (2026-09-24 现场: 9 个活进程, 页面却显示 5 个"运行中"里 3 个是假的、14 个"等你"里 6 个早已关闭):
  R1 进程重启后, 上个进程没跑完的回合/后台通知留在 transcript 尾巴 -> 被当成"长任务运行中" (其实已空闲, 在等你)
  R2 同项目里别的会话还开着 -> 早已关闭的会话被当成"等你输入"
  R3 后台 Agent 的回执 "Async agent launched successfully." 没被认出 -> 后台子代理在跑却显示"这轮已完成"
  R4 queueTranscriptOnly 的排队消息 (从不发给模型) 被当成最后一条消息 -> "处理中/久未返回"
  R5 等你授权 (权限弹窗) 在 transcript 里看起来和长任务一样 -> 现在由进程自报 waiting 直接判出
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokmon import activity, procmon  # noqa: E402
from tokmon.activity import ActivityConfig, _last_message, _open_bg_tasks, classify_state  # noqa: E402
from tokmon.event_sources.activity_source import derive_events  # noqa: E402
from tokmon.procmon import LiveIndex, _norm_path, _registry_entry  # noqa: E402

CFG = ActivityConfig()
NOW = 1_790_000_000.0


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _asst(blocks, stop, at):
    return {"type": "assistant", "timestamp": _iso(at), "message": {"model": "m", "stop_reason": stop, "content": blocks}}


def _user(content, at, **extra):
    return {"type": "user", "timestamp": _iso(at), "message": {"content": content}, **extra}


def _proc(status, status_at, started_at=None, waiting_for=None):
    return {"status": status, "status_at": status_at, "started_at": started_at or status_at - 60,
            "waiting_for": waiting_for}


# ---------------------------------------------------------------- 注册表记录校验 (procmon, 纯函数)

def test_registry_entry_parses_and_converts_ms():
    raw = {"sessionId": "S", "startedAt": 1_000_000, "status": "busy", "statusUpdatedAt": 1_005_000,
           "waitingFor": "permission prompt"}
    e = _registry_entry(raw, create_time=995.5)            # 进程先出生 (~4.5s), 后登记
    assert e == {"status": "busy", "waiting_for": "permission prompt", "status_at": 1005.0, "started_at": 1000.0}


def test_registry_entry_rejects_pid_reuse_and_garbage():
    raw = {"sessionId": "S", "startedAt": 1_000_000, "status": "idle"}
    assert _registry_entry(raw, create_time=1000 + procmon._REG_SLACK_S + 1) is None   # 同 pid 的进程比登记还晚出生 = 陈旧登记
    assert _registry_entry(raw, create_time=None) is None
    assert _registry_entry({"startedAt": 1_000_000}, create_time=900) is None           # 没有 sessionId
    assert _registry_entry({"sessionId": "S"}, create_time=900) is None                 # 没有 startedAt
    odd = _registry_entry({"sessionId": "S", "startedAt": 1_000_000, "status": "sleeping"}, create_time=900)
    assert odd["status"] is None                            # 未知取值不硬猜 -> 只当活性用, 状态退回 transcript


def test_read_registry_skips_dead_pids_and_non_session_files(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "100.json").write_text(json.dumps({"sessionId": "alive", "startedAt": 50_000, "status": "idle"}), "utf-8")
    (d / "200.json").write_text(json.dumps({"sessionId": "crashed", "startedAt": 50_000, "status": "busy"}), "utf-8")
    (d / "300.json").write_text("{not json", "utf-8")
    (d / "100.abcdef.key").write_text("k", "utf-8")
    (d / "11007.lock").write_text("", "utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    class _Gone:
        def __init__(self, pid):
            raise procmon.psutil.NoSuchProcess(pid)
    monkeypatch.setattr(procmon.psutil, "Process", _Gone)
    reg, pids = procmon._read_registry({100: 40.0, 300: 40.0})
    assert set(reg) == {"alive"} and pids == {100}          # 200: 进程已退出 (崩溃遗留); 300: 坏文件; .key/.lock 不是登记
    assert reg["alive"]["pid"] == 100


def test_read_registry_missing_dir_means_no_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    assert procmon._read_registry({}) is None               # 老版本 / 非默认布局 -> 退回旧口径, 而不是"全都没登记"


# ---------------------------------------------------------------- R2: 活性索引 —— 已登记的进程只承载它登记的会话

def test_liveindex_registered_processes_prove_other_sessions_closed():
    proj = _norm_path("C:/proj/alpha")
    reg = {"A": _proc("idle", NOW), "B": _proc("busy", NOW)}
    idx = LiveIndex({}, {proj}, reg, open_cwds=set())       # 项目里 2 个活进程, 都登记了 (A, B)
    assert idx.status("A", "C:/proj/alpha") is True
    assert idx.status("old", "C:/proj/alpha") is False      # R2: 同项目的旧会话没有进程承载 -> 已关闭 (以前是 None -> "等你")
    assert idx.session_status("B")["status"] == "busy"
    assert idx.session_status("old") is None


def test_liveindex_unregistered_process_keeps_same_project_unknown():
    proj = _norm_path("C:/proj/alpha")
    idx = LiveIndex({}, {proj}, {"A": _proc("idle", NOW)}, open_cwds={proj})   # 还有一个没登记的进程 (刚启动/老版本)
    assert idx.status("old", "C:/proj/alpha/sub") is None   # 它可能承载任何同项目会话 -> 不敢判关闭 (P6)
    assert idx.status("x", "C:/proj/beta") is False


def test_liveindex_without_registry_keeps_legacy_semantics():
    proj = _norm_path("C:/proj/alpha")
    idx = LiveIndex({"R": proj}, {proj})                    # 旧构造方式: 没注册表 -> open_cwds=None -> 用 live_cwds
    assert idx.status("R", None) is True
    assert idx.status("old", "C:/proj/alpha") is None
    assert idx.status("old", "C:/proj/beta") is False
    assert idx.session_status("R") is None


class _FakeProc:
    def __init__(self, pid, cwd, cmdline, create_time, name="claude.exe"):
        self.pid = pid
        self.info = {"name": name, "cmdline": cmdline, "cwd": cwd, "create_time": create_time}


def _run_index(monkeypatch, tmp_path, procs, entries):
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    for pid, raw in entries.items():
        (d / f"{pid}.json").write_text(json.dumps(raw), "utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(procmon, "_HAS_PSUTIL", True)
    monkeypatch.setattr(procmon.psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(procmon, "_LIVE_CACHE", None)
    monkeypatch.setattr(procmon, "_LIVE_HISTORY", [])
    return procmon.live_claude_index()


def test_live_index_end_to_end(monkeypatch, tmp_path):
    t0 = time.time() - 100
    procs = [
        _FakeProc(1, "C:/proj/alpha", ["claude.exe", "--resume", "A"], t0),        # 登记为 B (面板里换过会话)
        _FakeProc(2, "C:/proj/alpha", ["claude.exe"], t0),                         # 登记为 C
        _FakeProc(3, "C:/proj/beta", ["claude.exe", "--resume", "D"], t0),         # 没登记 -> 走 --resume 旧路径
        _FakeProc(4, "C:/x", ["node", "server.js"], t0, name="node.exe"),          # 不是 claude
    ]
    entries = {1: {"sessionId": "B", "startedAt": (t0 + 4) * 1000, "status": "busy", "statusUpdatedAt": (t0 + 50) * 1000},
               2: {"sessionId": "C", "startedAt": (t0 + 4) * 1000, "status": "waiting",
                   "waitingFor": "permission prompt", "statusUpdatedAt": (t0 + 60) * 1000}}
    idx = _run_index(monkeypatch, tmp_path, procs, entries)
    assert idx.status("B", "C:/proj/alpha") is True
    assert idx.status("C", "C:/proj/alpha") is True
    assert idx.status("A", "C:/proj/alpha") is False        # 进程 1 的 cmdline 还写着 --resume A, 但它现在承载 B: A 已关闭
    assert idx.status("D", "C:/proj/beta") is True          # 没登记的进程照旧靠 --resume 认
    assert idx.status("E", "C:/proj/beta") is None          # 没登记的进程可能承载同项目别的会话 -> 不敢判
    assert idx.session_status("C")["waiting_for"] == "permission prompt"
    assert idx.session_status("D") is None


def test_live_index_registry_latest_frame_wins(monkeypatch, tmp_path):
    t0 = time.time() - 100
    procs = [_FakeProc(1, "C:/proj/alpha", ["claude.exe"], t0)]
    base = {"sessionId": "B", "startedAt": (t0 + 4) * 1000}
    _run_index(monkeypatch, tmp_path, procs, {1: {**base, "status": "busy", "statusUpdatedAt": (t0 + 10) * 1000}})
    monkeypatch.setattr(procmon, "_LIVE_CACHE", None)       # 过 TTL, 保留 OR 衰减历史
    (tmp_path / "sessions" / "1.json").write_text(
        json.dumps({**base, "status": "idle", "statusUpdatedAt": (t0 + 20) * 1000}), "utf-8")
    idx = procmon.live_claude_index()
    assert idx.session_status("B")["status"] == "idle"      # 活性做并集, 但回合状态以最新一帧为准


# ---------------------------------------------------------------- R1 / R5: 进程自报的回合状态 (activity, 纯函数)

def test_r1_stale_tool_turn_after_restart_is_awaiting():
    last = _asst([{"type": "tool_use", "name": "Bash"}], "tool_use", at=NOW - 30_000)   # 上个进程死在工具调用中
    old = classify_state(last, NOW, CFG, liveness=True)
    assert old["state"] == "WORKING" and "长任务" in old["state_label"]                  # 修复前: 假"长任务"
    st = classify_state(last, NOW, CFG, liveness=True, proc=_proc("idle", NOW - 900))
    assert st["state"] == "AWAITING_USER" and "没有正常收尾" in st["state_label"]
    assert st["tool_pending"] is False and st["pending_tool_name"] is None
    assert st["idle"] is True and st["state_source"] == "registry" and st["ambiguous"] is False


def test_r1_unanswered_notification_after_restart_is_awaiting():
    last = _user("<task-notification><status>failed</status></task-notification>", at=NOW - 30_000)
    st = classify_state(last, NOW, CFG, liveness=True, proc=_proc("idle", NOW - 900))
    assert st["state"] == "AWAITING_USER"


def test_busy_turns_end_turn_into_background_and_ambiguous_into_long_task():
    done = _asst([{"type": "text", "text": "后台 Builder 在改, 等它"}], "end_turn", at=NOW - 600)
    st = classify_state(done, NOW, CFG, proc=_proc("busy", NOW - 700))
    assert st["state"] == "WORKING" and st["background"] is True and "后台" in st["state_label"]   # R3 的进程侧证据
    long = _asst([{"type": "tool_use", "name": "Bash"}], "tool_use", at=NOW - 900)
    st = classify_state(long, NOW, CFG, proc=_proc("busy", NOW - 900))
    assert st["state"] == "WORKING" and "长任务" in st["state_label"] and st["ambiguous"] is False
    fresh = _asst([{"type": "tool_use", "name": "Bash"}], "tool_use", at=NOW - 5)
    assert classify_state(fresh, NOW, CFG, proc=_proc("busy", NOW - 10))["state_label"] == "运行中 · 正在跑 Bash"   # 保留细节


def test_r5_waiting_is_blocked_on_user_with_reason():
    pend = _asst([{"type": "tool_use", "name": "Bash"}], "tool_use", at=NOW - 400)
    st = classify_state(pend, NOW, CFG, proc=_proc("waiting", NOW - 399, waiting_for="permission prompt"))
    assert st["state"] == "BLOCKED_ON_USER" and st["state_label"].startswith("等你授权") and st["ambiguous"] is False
    ask = classify_state(pend, NOW, CFG, proc=_proc("waiting", NOW - 399, waiting_for="input needed"))
    assert ask["state_label"].startswith("等你回答")
    other = classify_state(pend, NOW, CFG, proc=_proc("waiting", NOW - 399, waiting_for="some new thing"))
    assert other["state"] == "BLOCKED_ON_USER" and "some new thing" in other["state_label"]


def test_idle_registry_lagging_behind_fresh_prompt_keeps_transcript():
    prompt = _user("帮我看看这个", at=NOW - 2)              # 你刚发了话; 注册表上一帧还是 idle
    st = classify_state(prompt, NOW, CFG, proc=_proc("idle", NOW - 300))
    assert st["state"] == "PROCESSING" and st["state_source"] == "transcript"   # 不误报"等你"(否则会误发 TASK_COMPLETED)


def test_idle_with_background_launched_in_this_process_stays_working():
    done = _asst([{"type": "text", "text": "workflow 已在后台启动"}], "end_turn", at=NOW - 60)
    st = classify_state(done, NOW, CFG, bg_open=True, proc=_proc("idle", NOW - 55))
    assert st["state"] == "WORKING" and st["background"] is True       # Workflow 是否让 SDK 保持 busy 未证实 -> 保守


def test_shell_status_is_awaiting_with_note():
    done = _asst([{"type": "text", "text": "dev server 起好了"}], "end_turn", at=NOW - 30)
    st = classify_state(done, NOW, CFG, proc=_proc("shell", NOW - 29))
    assert st["state"] == "AWAITING_USER" and "后台 shell" in st["state_label"]


def test_unknown_proc_status_falls_back_to_old_heuristics():
    long = _asst([{"type": "tool_use", "name": "Bash"}], "tool_use", at=NOW - 900)
    st = classify_state(long, NOW, CFG, liveness=True, proc={"status": None, "status_at": None})
    assert st["state"] == "WORKING" and st["state_source"] == "transcript" and "进程在跑" in st["state_label"]


# ---------------------------------------------------------------- R3 / R4: transcript 侧的修正

def test_r3_async_agent_ack_is_not_a_completion():
    launch = {"type": "assistant", "timestamp": _iso(NOW - 60), "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": "toolu_B", "input": {"run_in_background": True}}]}}
    ack = _user([{"type": "tool_result", "tool_use_id": "toolu_B",
                  "content": [{"type": "text", "text": "Async agent launched successfully. agentId: a1"}]}], at=NOW - 59)
    assert _open_bg_tasks([launch, ack]) == {"toolu_B"}
    done = _user("<task-notification><tool-use-id>toolu_B</tool-use-id><status>completed</status></task-notification>",
                 at=NOW - 5)
    assert _open_bg_tasks([launch, ack, done]) == set()


def test_r4_transcript_only_queue_message_is_not_the_last_message():
    end = _asst([{"type": "text", "text": "做完了"}], "end_turn", at=NOW - 90_000)
    orphan = _user("<task-notification><status>stopped</status></task-notification>", at=NOW - 10_000,
                   queueTranscriptOnly=True, origin={"kind": "task-notification"})
    assert _last_message([end, orphan]) is end
    st = classify_state(_last_message([end, orphan]), NOW, CFG)
    assert st["state"] == "AWAITING_USER"                   # 修复前: 尾巴是 user 消息 -> 处理中 / 久未返回


# ---------------------------------------------------------------- 组装: 快照 + 事件

class _Live:
    def __init__(self, reg, closed=()):
        self.reg, self.closed = reg, set(closed)

    def status(self, sid, cwd):
        return True if sid in self.reg else (False if sid in self.closed else None)

    def session_status(self, sid):
        return self.reg.get(sid)


def _write(base, sid, objs):
    d = base / "C--proj"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps({**o, "sessionId": sid, "cwd": "C:/proj"}) for o in objs) + "\n", "utf-8")
    return p


def test_snapshot_filters_background_tasks_from_before_restart(tmp_path, monkeypatch):
    now = time.time()
    wf = lambda tid, at: {"type": "assistant", "timestamp": _iso(at), "message": {"content": [   # noqa: E731
        {"type": "tool_use", "name": "Workflow", "id": tid, "input": {}}]}}
    ack = lambda tid, at: _user([{"type": "tool_result", "tool_use_id": tid,                 # noqa: E731
                                  "content": "Workflow launched in background. Task ID: w"}], at=at)
    end = lambda at: _asst([{"type": "text", "text": "已在后台启动"}], "end_turn", at=at)     # noqa: E731
    _write(tmp_path, "old", [wf("toolu_O", now - 5000), ack("toolu_O", now - 4999), end(now - 4990)])
    _write(tmp_path, "new", [wf("toolu_N", now - 50), ack("toolu_N", now - 49), end(now - 40)])
    _write(tmp_path, "gone", [end(now - 90_000)])
    _write(tmp_path, "perm", [_asst([{"type": "tool_use", "name": "Bash"}], "tool_use", at=now - 400)])
    started = now - 1000                                    # 两个会话的进程都在 1000s 前 (重新) 启动
    live = _Live({"old": _proc("idle", now - 900, started_at=started),
                  "new": _proc("idle", now - 30, started_at=started),
                  "perm": _proc("waiting", now - 399, started_at=started, waiting_for="permission prompt")},
                 closed={"gone"})
    monkeypatch.setattr(activity, "_LAST_SNAP", None)
    snap = activity.snapshot(tmp_path, live=live)
    by = {s["session_id"]: s for s in snap["sessions"]}
    assert by["old"]["state"] == "AWAITING_USER"            # workflow 启动于进程重启前 -> 已随旧进程死掉
    assert by["new"]["state"] == "WORKING" and by["new"]["background"] is True
    assert by["gone"]["state"] == "CLOSED"
    assert by["perm"]["state"] == "BLOCKED_ON_USER" and by["perm"]["proc_status"] == "waiting"
    assert by["perm"]["waiting_for"] == "permission prompt"
    assert snap["sessions"][0]["session_id"] == "perm"      # 卡在你身上的排最前
    assert snap["counts"]["blocked"] == 1 and snap["counts"]["awaiting"] == 1 and snap["counts"]["working"] == 1


def _erow(st, age, m2, **kw):
    return {"session_id": "s", "project": "P", "state": st, "last_activity_epoch": m2 - age,
            "last_activity_age_s": age, "state_label": "x", "recent_events": [], **kw}


M2 = 2_000_000_000.0


def test_blocked_on_user_alerts_once_after_threshold_never_as_completion():
    from tokmon.event_sources.activity_source import BLOCKED_ALERT_S
    _, memo = derive_events(None, _erow("WORKING", 5, M2), M2)
    t0 = M2 - 10                                             # 10s 前弹出权限框
    blk = dict(proc_status_at=t0, waiting_for="permission prompt", pending_tool_name="Bash")
    evs, memo = derive_events(memo, _erow("BLOCKED_ON_USER", 10, M2, **blk), M2)
    assert evs == []                                        # 弹框 != 任务完成; 几秒内就点掉的不值得推送
    later = t0 + BLOCKED_ALERT_S + 5
    evs, memo = derive_events(memo, _erow("BLOCKED_ON_USER", later - (M2 - 10), later, **blk), later)
    assert [e.type for e in evs] == ["PERMISSION_NEEDED"]   # 等了 >= 阈值: 提醒一次 (以前的 SESSION_STUCK 不能丢)
    assert evs[0].timestamp == t0 + BLOCKED_ALERT_S and evs[0].payload["tool_name"] == "Bash"
    evs, memo = derive_events(memo, _erow("BLOCKED_ON_USER", 9000, later + 3000, **blk), later + 3000)
    assert evs == []                                        # 同一段等待不重复
    evs, memo = derive_events(memo, _erow("WORKING", 2, later + 3001), later + 3001)
    evs, _ = derive_events(memo, _erow("AWAITING_USER", 1, later + 3002), later + 3002)
    assert [e.type for e in evs] == ["TASK_COMPLETED"]      # 批准后跑完 -> 照常完成一次


def test_blocked_question_and_new_episode_and_no_backfill():
    from tokmon.event_sources.activity_source import BLOCKED_ALERT_S
    ask = dict(proc_status_at=M2 - 5000, waiting_for="input needed")
    _, memo = derive_events(None, _erow("BLOCKED_ON_USER", 5000, M2, **ask), M2)   # 首次见到就已卡了很久
    evs, memo = derive_events(memo, _erow("BLOCKED_ON_USER", 5001, M2 + 1, **ask), M2 + 1)
    assert evs == []                                        # 冷启动/首次见到: 播种, 不补发
    ask2 = dict(proc_status_at=M2 + 10, waiting_for="input needed")                # 答完又被问了一次
    now = M2 + 10 + BLOCKED_ALERT_S
    evs, _ = derive_events(memo, _erow("BLOCKED_ON_USER", 5, now, **ask2), now)
    assert [e.type for e in evs] == ["QUESTION_PENDING"]    # 新的一段等待 -> 新提醒


def test_r1_reload_mid_tool_does_not_fake_completion_or_idle_storm():
    # 评审 #1A: 会话在跑 Bash 时 VS Code 重载, 新进程登记 idle -> 转"等你", 但那一轮是被杀掉的, 不是完成
    _, memo = derive_events(None, _erow("WORKING", 5, M2), M2)
    last = _asst([{"type": "tool_use", "name": "Bash"}], "tool_use", at=M2 - 8000)
    st = classify_state(last, M2, CFG, liveness=True, proc=_proc("idle", M2 - 30))
    assert st["state"] == "AWAITING_USER" and st["turn_unfinished"] is True
    row = {**_erow("AWAITING_USER", 8000, M2), "turn_unfinished": True}
    evs, memo = derive_events(memo, row, M2)
    assert evs == []                                        # 无 TASK_COMPLETED, 也不补发 10min/30min/2h 空闲台阶
    evs, _ = derive_events(memo, {**row, "last_activity_age_s": 9000}, M2 + 1000)
    assert evs == []


def test_idle_lag_guard_expires():
    # 评审 #1B: la > sa 的保留只容忍注册表的短暂滞后, 过了就信进程自报 (且标 turn_unfinished, 不算完成)
    msg = _user("<command-name>/something</command-name>", at=NOW - 25)
    st = classify_state(msg, NOW, CFG, proc=_proc("idle", NOW - 300))
    assert st["state"] == "AWAITING_USER" and st["turn_unfinished"] is True and st["state_source"] == "registry"
    fresh = classify_state(_user("hi", at=NOW - 3), NOW, CFG, proc=_proc("idle", NOW - 300))
    assert fresh["state"] == "PROCESSING" and fresh["turn_unfinished"] is False


def test_local_command_output_is_awaiting_even_without_registry():
    out = _user("<local-command-stdout>Set model to `claude-opus-5`</local-command-stdout>", at=NOW - 900)
    st = classify_state(out, NOW, CFG)
    assert st["state"] == "AWAITING_USER" and "本地命令" in st["state_label"]   # 以前: 处理中 -> 久未返回


def test_closed_needs_evidence_older_than_the_last_message():
    # 评审 #6: 面板里刚 /clear 换到新会话, 活性帧 (~3s 缓存) 还没看到它 -> 不能判已关闭
    fresh = _user("新会话第一句", at=NOW - 2)
    assert classify_state(fresh, NOW, CFG, liveness=False)["state"] == "PROCESSING"
    old = _asst([{"type": "text", "text": "done"}], "end_turn", at=NOW - CFG.closed_grace_s - 5)
    assert classify_state(old, NOW, CFG, liveness=False)["state"] == "CLOSED"


def test_r3_agent_without_flag_counts_only_with_async_ack():
    use = {"type": "assistant", "timestamp": _iso(NOW - 60), "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": "toolu_X", "input": {"prompt": "p"}}]}}
    ack = _user([{"type": "tool_result", "tool_use_id": "toolu_X",
                  "content": "Async agent launched successfully. agentId: a1"}], at=NOW - 59)
    sync_done = _user([{"type": "tool_result", "tool_use_id": "toolu_X", "content": "the full report"}], at=NOW - 5)
    assert _open_bg_tasks([use, ack]) == {"toolu_X"}        # 没写 flag 但异步启动了 (评审 #5)
    assert _open_bg_tasks([use]) == set()                   # 还没回执: 当同步调用, 不算后台
    assert _open_bg_tasks([use, sync_done]) == set()


def test_registry_idle_is_authoritative_for_background_agents(tmp_path, monkeypatch):
    # 评审 #4: 后台 Agent 在跑时 SDK 保持 busy (实测) -> idle 就说明它已结束; 只有 Workflow 还需要 transcript 兜底
    now = time.time()
    agent = {"type": "assistant", "timestamp": _iso(now - 50), "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": "toolu_A", "input": {"run_in_background": True}}]}}
    ack = _user([{"type": "tool_result", "tool_use_id": "toolu_A", "content": "Async agent launched successfully."}],
                at=now - 49)
    end = _asst([{"type": "text", "text": "等它"}], "end_turn", at=now - 40)
    _write(tmp_path, "ag", [agent, ack, end])
    monkeypatch.setattr(activity, "_LAST_SNAP", None)
    snap = activity.snapshot(tmp_path, live=_Live({"ag": _proc("idle", now - 10, started_at=now - 1000)}))
    assert snap["sessions"][0]["state"] == "AWAITING_USER"
    monkeypatch.setattr(activity, "_LAST_SNAP", None)
    snap = activity.snapshot(tmp_path, live=_Live({"ag": _proc("busy", now - 60, started_at=now - 1000)}))
    assert snap["sessions"][0]["state"] == "WORKING" and snap["sessions"][0]["background"] is True


def test_registry_entry_exact_procstart_check_on_windows():
    ft = lambda epoch: str(int((epoch + 11644473600) * 1e7))   # noqa: E731
    raw = {"sessionId": "S", "startedAt": 1_790_000_004_000, "status": "idle", "pidDomain": "win32:msi"}
    assert _registry_entry({**raw, "procStart": ft(1_790_000_000.25)}, create_time=1_790_000_000.25) is not None
    # 同 pid 的另一个进程恰好在登记前 3s 内出生: startedAt 检查放过了, FILETIME 精确校验拦下
    assert _registry_entry({**raw, "procStart": ft(1_790_000_000.25)}, create_time=1_790_000_003.0) is None


def test_duplicate_registration_prefers_freshest_status(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "10.json").write_text(json.dumps({"sessionId": "S", "startedAt": 50_000, "status": "busy",
                                           "statusUpdatedAt": 60_000}), "utf-8")
    (d / "20.json").write_text(json.dumps({"sessionId": "S", "startedAt": 70_000, "status": "idle",
                                           "statusUpdatedAt": 80_000}), "utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    reg, pids = procmon._read_registry({10: 40.0, 20: 65.0})
    assert reg["S"]["status"] == "idle" and reg["S"]["pid"] == 20 and pids == {10, 20}
