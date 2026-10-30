"""控制台支柱 (CONSOLES_PLAN S1): 三种方言的合成夹具 -> 标签表 / 任务切分 / 读文件的边界。"""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tokmon import consoles

FIX = Path(__file__).parent / "fixtures" / "consoles"


def U(tag):
    return "00000000-0000-4000-8000-" + tag.rjust(12, "0")


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "c"
    shutil.copytree(FIX, root)
    old = time.time() - 3600                                        # 夹具文件都是「早就安静了」的 (见 SETTLE_S)
    for f in root.rglob("*"):
        os.utime(f, (old, old))
    tpl = (FIX / "consoles.template.json").read_text(encoding="utf-8")
    p = tmp_path / "consoles.json"
    p.write_text(tpl.replace("{root}", root.as_posix()), encoding="utf-8")
    consoles.set_config(p)
    return root


def test_missing_or_bad_config_is_an_empty_snapshot(tmp_path):
    consoles.set_config(tmp_path / "nope.json")
    s = consoles.snapshot(force=True)
    assert s.consoles == [] and s.labels == {} and s.label(U("c0")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    consoles.set_config(bad)
    s = consoles.snapshot(force=True)
    assert s.consoles == [] and any("JSON" in p for p in s.problems)


def test_office_c_labels_and_task_windows(cfg):
    s = consoles.snapshot(force=True)
    c0, c1, c2 = (s.label(U(x)) for x in ("c0", "c1", "c2"))
    assert (c0.console, c0.run, c0.role, c0.agent, c0.grain) == ("C", "20261026-013942-session", "conductor", "conductor", "session")
    assert (c1.role, c1.agent) == ("seat", "server") and c2.role == "helper"
    assert c0.project == "proj-c"                              # 控制台服务的项目 = project_root 的项目身份
    # 派活窗口: 座位 server 在 1000-2000 做任务 1, 3000-4000 做任务 2
    assert s.task_of(c1, 1500) == "1" and s.task_of(c1, 3500) == "2"
    # 中控贯穿整次运行: 落在最近一个已开始的任务上; 第一个任务开始前不归任何任务
    assert s.task_of(c0, 2950) == "2" and s.task_of(c0, 2600) == "1" and s.task_of(c0, 920) == ""
    info = s.runs[("C", "20261026-013942-session")]
    assert info.status == "idle" and info.started == 900.0          # 状态以 state.json 为准 (事件是历史)
    assert info.task_status == {"1": "done", "2": "waiting_decision"}
    assert info.ledger["output"] == 20
    assert [k for _, k in info.task_starts] == ["1", "2"]            # 写到一半的最后一行 (task 3) 没被读进来


def test_office_e_registry_successor_probes_and_foreign(cfg):
    s = consoles.snapshot(force=True)
    run = "20261028-143926-task"
    e0, e1, e2 = s.label(U("e0")), s.label(U("e1")), s.label(U("e2"))
    assert (e0.run, e0.role, e0.agent) == (run, "core", "core")     # 中控换代前的旧会话 (state.json 只留了新的)
    assert (e1.run, e1.role) == (run, "core")
    assert (e2.role, e2.agent) == ("critic", "w01")                 # critic = verifier 外壳 + critic 标记
    assert s.task_of(e2, 5500) == "T5"                              # E 的任务键是全局的 T<n>
    assert s.task_of(e1, 7500) == "T6"
    p1, p2 = s.label(U("p1")), s.label(U("p2"))
    for p in (p1, p2):
        assert (p.console, p.run, p.role, p.grain) == ("E", consoles.SELF_TEST, consoles.SELF_TEST, "console")
        assert p.project == "proj-e" and s.task_of(p, 5000) == ""
    assert s.label(U("f9")) is None                                 # intake.foreign_session 里的是别人的会话


def test_missiond_agents_json_keeps_only_four_keys_and_worktree_fallback(cfg):
    s = consoles.snapshot(force=True)
    run = "20261022-083431-smoke"
    b0, b1 = s.label(U("b0")), s.label(U("b1"))
    assert (b0.run, b0.role, b0.agent, b0.grain) == (run, "conductor", "conductor", "session")
    assert (b1.role, b1.agent) == ("worker", "a01")
    assert s.task_of(b1, 300) == "1" and s.runs[("B", run)].status == "idle"
    agent_files = [(k, v) for k, (_sig, v) in consoles._FILES.items() if Path(k).parent.name == "agents"]
    assert len(agent_files) == 2
    for _k, val in agent_files:
        assert set(val) <= {"id", "session_id", "role", "kind"}      # 白名单: 其余字段读进来就丢
    # 没有 json 的会话: sdk 会话按 worktree 目录兜底 (只认到运行); 交互会话不按目录猜
    wt = (cfg / "missiond" / "worktrees" / f"{run}--a02" / "src").as_posix()
    lab = s.label(U("zz"), wt, "sdk-py")
    assert (lab.console, lab.run, lab.agent, lab.grain) == ("B", run, "a02", "run")
    assert s.label(U("zz"), wt, "claude-vscode") is None
    other = (cfg / "missiond" / "worktrees" / "20261022-083431-smokeX").as_posix()
    assert s.label(U("zy"), other, "sdk-py") is None                # 段名必须和某个运行目录名完全一致


def test_conductor_by_cwd_runs_tournaments_and_fixture_runs(cfg):
    s = consoles.snapshot(force=True)
    runs = cfg / "conductor" / "runs"
    run = "20261022-114706-run"
    ws = s.label(U("a1"), str(runs / run / "workspace"), "sdk-py")
    assert (ws.console, ws.run, ws.role, ws.grain) == ("A", run, "", "run")
    ent = s.label(U("a2"), str(runs / run / "tournaments" / "t1" / "entry_B" / "x"), "sdk-py")
    assert (ent.role, ent.agent, ent.grain) == ("competitor", "e02", "session")
    judge = s.label(U("a3"), str(runs / run / "tournaments" / "t1"), "sdk-py")
    assert (judge.role, judge.agent, judge.grain) == ("judge", "j01", "session")
    assert s.label(U("a4"), str(runs / "00000000-000000-ui-fixture" / "workspace"), "sdk-py") is None
    assert s.label(U("a5"), str(runs / run / "workspace"), "claude-vscode") is None
    # Git Bash 风格的 /c/... 与大小写差异都认
    posix = "/" + (runs / run / "workspace").as_posix().replace(":", "", 1)
    if os.name == "nt":
        assert s.label(U("a6"), posix.upper(), "sdk-py").run == run
    assert s.task_of(ws, 200) == "1"
    old = s.label(U("a7"), str(runs / "20261020-090000-old" / "workspace"), "sdk-py")
    assert s.task_of(old, 50) == "1"                                # 老运行没有 task.*: 整次运行算任务 1
    assert ("A", "00000000-000000-ui-fixture") not in s.runs


def test_never_opens_forbidden_files(cfg, monkeypatch):
    opened = []
    real = consoles._open_shared

    def spy(path):
        opened.append(str(path))
        return real(path)

    monkeypatch.setattr(consoles, "_open_shared", spy)
    consoles.set_config(consoles.config_path())
    consoles.snapshot(force=True)
    assert opened
    for p in opened:
        low = p.replace("\\", "/").lower()
        assert not low.endswith(".token") and not low.endswith("daemon.log") and not low.endswith(".env")
        assert not (low.endswith(".jsonl") and "/agents/" in low)


def test_events_are_tailed_incrementally(cfg):
    ev = cfg / "office" / "runs_c" / "20261026-013942-session" / "events.jsonl"
    s = consoles.snapshot(force=True)
    assert [k for _, k in s.runs[("C", ev.parent.name)].task_starts] == ["1", "2"]
    with open(ev, "ab") as f:                                       # 把写到一半的那行补完 + 再追加一行
        f.write(b': 3}}\n' + json.dumps({"seq": 9, "ts": 5000.0, "type": "task.finished",
                                         "data": {"index": 3, "status": "done"}}).encode() + b"\n")
    s = consoles.snapshot(force=True)
    info = s.runs[("C", ev.parent.name)]
    assert [k for _, k in info.task_starts] == ["1", "2", "3"] and info.task_status["3"] == "done"
    ev.write_text(json.dumps({"seq": 0, "ts": 1.0, "type": "task.started", "data": {"index": 7}}) + "\n", encoding="utf-8")
    s = consoles.snapshot(force=True)                               # 文件变短 = 被重写: 从头读
    assert [k for _, k in s.runs[("C", ev.parent.name)].task_starts] == ["7"]


def test_snapshot_is_cached_for_ttl(cfg, monkeypatch):
    a = consoles.snapshot(force=True)
    assert consoles.snapshot() is a
    monkeypatch.setattr(consoles, "TTL_S", 0.0)
    assert consoles.snapshot() is not a


def test_state_json_is_read_only_after_it_settles(cfg):
    # 控制台用「写临时文件再 rename 覆盖」保存 state.json; 我们拿着句柄时它的 rename 会失败 (C 不重试)。
    # 所以刚被写过的 state.json 不读: 先用上一份, 安静 SETTLE_S 秒之后再读。
    st = cfg / "office" / "runs_c" / "20261026-013942-session" / "state.json"
    s = consoles.snapshot(force=True)
    assert s.label(U("c2")).role == "helper"
    d = json.loads(st.read_text(encoding="utf-8"))
    d["agents"].append({"spec": {"id": "h02", "kind": "helper", "session_id": U("c3")}})
    st.write_text(json.dumps(d), encoding="utf-8")                  # 控制台刚刚存过档 (mtime = 现在)
    s = consoles.snapshot(force=True)
    assert s.label(U("c3")) is None and s.label(U("c2")).role == "helper"   # 不读刚写的; 旧的照用
    old = time.time() - consoles.SETTLE_S - 1
    os.utime(st, (old, old))
    s = consoles.snapshot(force=True)
    assert s.label(U("c3")).agent == "h02"                          # 安静下来以后读到了


def test_brand_new_state_json_waits_until_settled(cfg):
    run = cfg / "office" / "runs_c" / "20261030-000000-new"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({"agents": [{"spec": {"id": "conductor", "kind": "conductor",
                                                                     "session_id": U("n1")}}]}), encoding="utf-8")
    assert consoles.snapshot(force=True).label(U("n1")) is None     # 一份都还没读过: 这一帧先不认
    old = time.time() - consoles.SETTLE_S - 1
    os.utime(run / "state.json", (old, old))
    assert consoles.snapshot(force=True).label(U("n1")).run == run.name


@pytest.mark.skipif(os.name != "nt", reason="Windows 的 rename 覆盖会被打开着的句柄挡住")
def test_an_open_handle_does_block_rename_so_reads_must_be_brief(tmp_path):
    # 记下这个事实 (为什么要 SETTLE_S + 读完即关): 共享删除也救不了 rename 覆盖
    target, tmp = tmp_path / "state.json", tmp_path / "state.json.tmp"
    target.write_text('{"v": 1}', encoding="utf-8")
    tmp.write_text('{"v": 2}', encoding="utf-8")
    with consoles._open_shared(target) as f:
        f.read(3)
        with pytest.raises(PermissionError):
            os.replace(tmp, target)
    os.replace(tmp, target)                                         # 句柄一关就好了
    assert consoles._read_shared(target) == b'{"v": 2}'


# ---------------------------------------------------------------- 审查补的回归 (多视角静态审查 2026-10-29)

def test_assignment_windows_win_over_timeline_and_are_normalized(cfg):
    s = consoles.snapshot(force=True)
    h01 = s.label(U("c2"))
    assert s.task_of(h01, 3150) == "1"            # 时间表上是任务 2, 但 h01 这段派活属于任务 1: 派活窗口优先
    assert s.task_of(h01, 3500) == "2"            # 最后一段没写结束 = 正在做 -> 一直算它
    assert s.task_of(h01, 2500) == "1"            # 没写结束的旧窗口被截到下一段开始, 不会把后面的都盖住
    assert s.task_of(h01, 3250) == "2"            # 两段之间的空隙: 落回运行的时间表 (任务 2)


def test_registry_only_session_maps_to_run_via_tn(cfg):
    s = consoles.snapshot(force=True)
    e3 = s.label(U("e3"))                          # state.json 里没有、也不在换代事件里: 只能靠台账的 T6 -> 运行
    assert (e3.run, e3.agent, e3.role, e3.grain) == ("20261028-143926-task", "w02", "verifier", "session")


def test_bad_config_shapes_never_raise(tmp_path):
    p = tmp_path / "c.json"
    for cfg in ({"consoles": {"code": "A"}}, {"consoles": [{"code": "Z", "runs_dir": 5}]},
                {"consoles": [{"code": "Z", "dialect": "office", "runs_dir": str(tmp_path), "project_root": 1}]},
                [1, 2], "x"):
        p.write_text(json.dumps(cfg), encoding="utf-8")
        consoles.set_config(p)
        s = consoles.snapshot(force=True)          # 不抛; 有问题的写进 problems
        assert s.label(U("c0")) is None
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"consoles": [{"code": "Z", "dialect": "office", "runs_dir": str(tmp_path),
                                                              "project_root": 1}]}).encode())
    consoles.set_config(p)
    s = consoles.snapshot(force=True)              # 带 BOM 也认; project_root 不是字符串 -> 记问题, 不炸
    assert [c.code for c in s.consoles] == ["Z"] and any("project_root" in x for x in s.problems)


def test_snapshot_survives_unexpected_errors(cfg, monkeypatch):
    monkeypatch.setattr(consoles, "_build", lambda: 1 / 0)
    s = consoles.snapshot(force=True)
    assert s.labels == {} and any("ZeroDivisionError" in x for x in s.problems)


def test_failed_read_is_not_cached(cfg, monkeypatch):
    s = consoles.snapshot(force=True)
    assert s.label(U("c0")) is not None
    st = cfg / "office" / "runs_c" / "20261026-013942-session" / "state.json"
    st.write_text(st.read_text(encoding="utf-8"), encoding="utf-8")   # 变一下 (内容不变, mtime 变)
    old = time.time() - consoles.SETTLE_S - 1
    os.utime(st, (old, old))
    real = consoles._read_shared
    monkeypatch.setattr(consoles, "_read_shared", lambda p: None if p.name == "state.json" else real(p))
    s = consoles.snapshot(force=True)
    assert s.label(U("c0")) is not None            # 读失败: 先用上一份, 不把「没读到」缓存成空
    assert any("读不了 state.json" in x for x in s.problems)
    monkeypatch.setattr(consoles, "_read_shared", real)
    assert consoles.snapshot(force=True).label(U("c0")) is not None    # 下一帧照常重读


def test_rename_saved_files_are_never_stat_ed(cfg, monkeypatch):
    # os.stat 在 Windows 上也会短暂开一个句柄 —— 会被 rename 覆盖保存的文件只从目录列表拿签名
    seen = []
    real = os.stat

    def spy(path, *a, **k):
        seen.append(str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(consoles.os, "stat", spy)
    consoles.snapshot(force=True)
    touched = [p.replace("\\", "/") for p in seen]
    assert not [p for p in touched if p.endswith("/state.json") or (p.endswith(".json") and "/agents/" in p)]
    assert any(p.endswith("events.jsonl") for p in touched)        # 只追加的文件照常 stat
