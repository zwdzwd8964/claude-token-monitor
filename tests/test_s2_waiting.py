"""CONSOLES_PLAN S2「等你」: 每次运行一台状态机 / 工单回放 / 控制台事件 / worker 不再误报「等你」。"""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tokmon import activity, consoles, events, serve
from tokmon.consoles import RunInfo
from tokmon.event_sources import activity_source, console_source

FIX = Path(__file__).parent / "fixtures" / "consoles"
C_RUN, E_RUN, B_RUN = "20261026-013942-session", "20261028-143926-task", "20261022-083431-smoke"


@pytest.fixture
def snap(tmp_path):
    root = tmp_path / "c"
    shutil.copytree(FIX, root)
    old = time.time() - 3600
    for f in root.rglob("*"):
        os.utime(f, (old, old))
    p = tmp_path / "consoles.json"
    p.write_text((FIX / "consoles.template.json").read_text(encoding="utf-8").replace("{root}", root.as_posix()),
                 encoding="utf-8")
    consoles.set_config(p)
    return consoles.snapshot(force=True)


# ---------------------------------------------------------------- 状态机 (各代方言)

def test_office_c_open_ticket_means_waiting_you(snap):
    info = snap.runs[("C", C_RUN)]
    assert info.pending() == [("Q-001", "question", 3050.0)]        # C 的工单没有「是否阻塞」: 开了就等你
    assert info.state(time.time()) == "waiting_you"
    assert "title" not in json.dumps(info.tickets) and "不该被读出来" not in json.dumps(info.tickets)   # 不记题目和正文


def test_office_e_blocking_answered_nonblocking_adopted_and_injection_ignored(snap):
    info = snap.runs[("E", E_RUN)]
    assert info.waiting_at(7150) and not info.waiting_at(7250)       # 阻塞工单: 开着时等你, 答了就不等
    assert not info.waiting_at(7300.2)                               # 不阻塞的: 当场默认采纳, 从来不算等你
    assert info.park_until is None                                   # 故障注入的假停泊不认
    assert info.budget_level == "warn"
    assert info.state(time.time()) == "finished"                     # 结束了的运行: 结束优先


def test_missiond_open_approval_waits_idle_park_and_ram_meter_do_not(snap):
    info = snap.runs[("B", B_RUN)]
    assert info.waiting_at(150) and not info.waiting_at(170)         # 提问答了
    assert info.pending() == [("a:ap1", "approval", 300.0)]          # 审批还开着
    assert info.park_until is None and info.budget_level == ""       # idle 停泊不是额度; ram 不是预算
    assert info.state(time.time()) == "waiting_you"


def test_conductor_killed_run_goes_stale_and_rejected_run_parks(snap):
    now = time.time()
    killed = snap.runs[("A", "20261023-000000-killed")]
    assert killed.status == "running" and killed.state(now) == "running"         # 夹具 1 小时前才写过
    assert killed.state(now + consoles.STALE_S) == "stale"                         # 2 小时没动静: 不再说它在跑
    rej = snap.runs[("A", "20261024-000000-rejected")]
    assert rej.state(now) == "parked" and rej.park_until == 9999999999.0          # 被拒: 等到重置
    assert rej.budget_level == "soft_stop"


def test_state_precedence():
    r = RunInfo("Z", "r", status="running")
    now = 1000.0
    assert r.state(now) == "running"
    r.budget_level = "warn"
    assert r.state(now) == "budget"
    r.park_until = now + 60
    assert r.state(now) == "parked" and r.state(now + 120) == "budget"           # 停泊过了期就不算
    r.tickets["Q"] = {"kind": "question", "opened": 900.0, "closed": None, "blocking": True}
    assert r.state(now) == "waiting_you"                                          # 等你 优先于 停泊 / 预算
    r.finished = 999.0
    assert r.state(now) == "finished"                                             # 结束了就是结束了
    r2 = RunInfo("Z", "r2", status="idle", parked=True)
    assert r2.state(now) == "parked"                                              # 状态本身是等额度 (不知道到几点)


def test_replay_every_fixture_ticket(snap):
    """S2 判据 ① 的同一套检查 (真数据上也跑了一遍, 见 CHANGELOG): 开了就等你, 答了 (没有别的没答的) 就不等。"""
    n = 0
    for info in snap.runs.values():
        for t in info.tickets.values():
            if not t["blocking"] or t["opened"] is None:
                continue
            n += 1
            assert info.waiting_at(t["opened"] + 0.1)
            if t["closed"] is not None:
                assert info.waiting_at((t["opened"] + t["closed"]) / 2) and not info.waiting_at(t["closed"] + 0.1)
    assert n == 4                                                    # C 1 + E 1 + B 2


# ---------------------------------------------------------------- 控制台事件

def test_console_events_cold_start_silent_then_once_each(snap):
    seen: set = set()
    now = time.time()
    assert console_source.derive(snap, now, seen, emit=False) == []               # 冷启动: 已有的只播种
    assert console_source.derive(snap, now, seen, emit=True) == []                # 之后没变化: 一条都不发
    # 新开一张阻塞工单: 不到 60 秒不响, 过了 60 秒响一次, 再来一帧不重复
    snap.runs[("C", C_RUN)].tickets["Q-009"] = {"kind": "plan", "opened": now - 10, "closed": None, "blocking": True}
    assert console_source.derive(snap, now, seen, emit=True) == []
    evs = console_source.derive(snap, now + 60, seen, emit=True)
    assert [e.type for e in evs] == ["CONSOLE_TICKET_PENDING"] and evs[0].severity == "warning"
    p = evs[0].payload
    assert p["console"] == "C" and p["run"] == C_RUN and p["ticket_kind"] == "plan" and p["count"] == 2
    assert set(p) <= events._ALLOWED_PAYLOAD and "title" not in p                 # 不带题目 / 正文
    assert console_source.derive(snap, now + 120, seen, emit=True) == []
    # 停泊 / 结束 / 预算: 各一次, 都不是 warning 以上 (只有「等你」会响)
    snap.runs[("C", C_RUN)].tickets.clear()
    snap.runs[("C", C_RUN)].park_until = now + 3600
    snap.runs[("C", C_RUN)].budget_level = "hard_stop"
    evs = console_source.derive(snap, now + 130, seen, emit=True)                 # 停泊和预算各管各的, 同一帧各一条
    assert sorted((e.type, e.severity, e.payload.get("scope")) for e in evs) == [
        ("CONSOLE_PARKED", "info", None), ("TOKEN_BUDGET_WARNING", "info", "console")]
    assert all("pct" not in e.payload for e in evs)                               # 控制台的级别不编百分比
    snap.runs[("C", C_RUN)].park_until = None
    assert console_source.derive(snap, now + 140, seen, emit=True) == []          # 同一级预算不重复
    snap.runs[("C", C_RUN)].finished = now + 150
    evs = console_source.derive(snap, now + 160, seen, emit=True)
    assert [e.type for e in evs] == ["CONSOLE_RUN_FINISHED"]


def test_each_park_episode_fires_once():
    r = RunInfo("Z", "r", status="running", last_seen=time.time())
    snap = consoles.Snapshot()
    snap.runs[("Z", "r")] = r
    seen: set = set()
    now = time.time()
    console_source.derive(snap, now, seen, emit=False)
    r.park_until = now + 600
    assert [e.type for e in console_source.derive(snap, now + 1, seen, emit=True)] == ["CONSOLE_PARKED"]
    r.park_until = now + 900                                                      # 同一段停泊里延期: 不再响
    assert console_source.derive(snap, now + 2, seen, emit=True) == []
    r.park_until = None                                                           # 恢复了
    assert console_source.derive(snap, now + 3, seen, emit=True) == []
    r.park_until = now + 1200                                                     # 第二段停泊: 再发一次, 总线去重键也不同
    evs = console_source.derive(snap, now + 4, seen, emit=True)
    assert [e.type for e in evs] == ["CONSOLE_PARKED"] and evs[0].dedup_key.endswith(f":{int(now + 4)}")


def test_cold_start_is_per_run(snap, monkeypatch):
    """后来才出现的运行 (新加进清单的控制台 / 第一帧没读到) 第一次被看到时也只播种, 不把老工单补响一遍。"""
    console_source.reset_for_tests()
    emitted = []
    monkeypatch.setattr(console_source.bus, "emit", emitted.append)
    later = snap.runs.pop(("C", C_RUN))
    monkeypatch.setattr(consoles, "snapshot", lambda *a, **k: snap)
    assert console_source.tick() == 0                                             # 冷启动
    snap.runs[("C", C_RUN)] = later                                               # C 这次运行现在才出现, 工单早就开着了
    assert console_source.tick() == 0 and emitted == []
    later.tickets["Q-009"] = {"kind": "plan", "opened": time.time() - 120, "closed": None, "blocking": True}
    assert console_source.tick() == 1 and emitted[0].type == "CONSOLE_TICKET_PENDING"   # 之后新开的照样响
    console_source.reset_for_tests()


def test_stale_wins_over_budget_and_b_meter_warning_expires():
    now = time.time()
    r = RunInfo("Z", "r", status="running", last_seen=now - 3 * 3600, budget_level="warn")
    assert r.state(now) == "stale"                                                # 报在跑却早没动静: 先说「没动静」
    r = RunInfo("Z", "r", status="running", last_seen=now, budget_level="warn", budget_at=now - 6 * 3600)
    assert r.state(now) == "running"                                              # B 的五小时窗口告警: 过了窗口就不作数
    r.budget_at = now - 60
    assert r.state(now) == "budget"


def test_cached_older_state_json_cannot_reopen_a_ticket_answered_later(snap):
    """控制台一直在存档时我们用的是缓存的旧 state.json —— 它说 Q-001 开着, 但事件里之后已经答了: 不能再算等你。"""
    rdir = Path(consoles.config_path()).parent / "c" / "office" / "runs_c" / C_RUN
    st = rdir / "state.json"
    t_state = st.stat().st_mtime
    with open(rdir / "events.jsonl", "a", encoding="utf-8") as f:     # 夹具最后是半行 (测尾读用): 先换行
        f.write("\n" + json.dumps({"seq": 99, "ts": t_state + 600, "type": "ticket.answered", "agent": None,
                            "data": {"id": "Q-001"}}) + "\n")
    info = consoles.snapshot(force=True).runs[("C", C_RUN)]
    assert info.pending() == [] and info.state(time.time()) != "waiting_you"
    newer = t_state + 1200                                                        # 之后又存了一份, 里面 Q-001 又开着: 重开, 以它为准
    os.utime(st, (newer, newer))
    info = consoles.snapshot(force=True).runs[("C", C_RUN)]
    assert [x[0] for x in info.pending()] == ["Q-001"]


def test_sessions_page_names_the_console_as_the_state_source():
    page = (Path(serve.__file__).parent / "pages" / "sessions.html").read_text(encoding="utf-8")
    assert "s.state_source==='console'" in page and "来源: 控制台" in page


# ---------------------------------------------------------------- worker 不再误报「等你」

def _row(state, role="worker", run_state="running", park_until=None):
    return {"session_id": "s", "project": "p", "state": state, "state_label": "等你回复", "idle": True,
            "console": {"code": "C", "run": C_RUN, "role": role, "agent": "w01", "project": "p", "grain": "session",
                        "run_state": run_state, "park_until": park_until}}


def test_console_rows_wait_on_the_conductor_not_on_you():
    r = _row("AWAITING_USER")
    activity._console_wait(r)
    assert (r["state"], r["state_label"], r["state_source"], r["idle"]) == ("CONSOLE_WAIT", "等中控", "console", False)
    r = _row("AWAITING_USER", run_state="parked", park_until=time.mktime((2026, 10, 29, 6, 32, 0, 0, 0, -1)))
    activity._console_wait(r)
    assert r["state_label"] == "额度停泊 · 到 06:32"
    r = _row("AWAITING_USER", role="core", run_state="waiting_you")
    activity._console_wait(r)
    assert r["state_label"] == "等你回控制台的工单"
    r = _row("BLOCKED_ON_USER")
    activity._console_wait(r)
    assert (r["state"], r["state_label"]) == ("CONSOLE_WAIT", "等控制台放行")


def test_console_wait_rows_never_raise_idle_or_question_events():
    now = time.time()
    row = dict(_row("AWAITING_USER"), last_activity_epoch=now - 7200, last_activity_age_s=7200, recent_events=[])
    activity._console_wait(row)
    _evs, memo = activity_source.derive_events(None, row, now)
    evs, _ = activity_source.derive_events(memo, dict(row, last_activity_age_s=99999), now + 99999)
    assert [e.type for e in evs if e.type in ("SESSION_IDLE", "QUESTION_PENDING", "PERMISSION_NEEDED")] == []


def test_attention_includes_console_runs_waiting_on_you(snap, monkeypatch):
    monkeypatch.setattr(activity, "snapshot", lambda *a, **k: {"sessions": []})
    monkeypatch.setattr(serve.procmon, "live_claude_index", lambda *a, **k: None)
    out = serve._attention(None, -1)
    got = {(x["code"], x["run"], x["n"]) for x in out["consoles"]}
    assert ("C", C_RUN, 1) in got and ("B", B_RUN, 1) in got
    assert all(set(x) == {"code", "run", "n", "since"} for x in out["consoles"])
    assert "CONSOLE_TICKET_PENDING" in serve._ATTN_TYPES
    nav = serve.SESS_PAGE
    assert "d.blocked.length + (d.consoles ? d.consoles.length : 0)" in nav     # 铃铛的数字算上控制台
    assert 'e.type === "CONSOLE_TICKET_PENDING"' in nav
