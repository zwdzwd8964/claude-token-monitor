"""CONSOLES_PLAN S1 端到端: transcript + 控制台夹具 -> 内核 (entrypoint / scratchpad 项目) -> /sessions / /tokens。"""

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmon import activity, consoles, parser, procmon, serve, trace
from tokmon.discovery import fs_path

FIX = Path(__file__).parent / "fixtures" / "consoles"
PARENT = "11111111-2222-4333-8444-555555555555"
KID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
GRAND = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
LOOSE = "12121212-3434-4565-8787-909090909090"
TMPS = "13131313-2424-4535-8646-757575757575"


def U(tag):
    return "00000000-0000-4000-8000-" + tag.rjust(12, "0")


def line(sid, mid, ts, cwd, ep, model="claude-opus-5-5", inp=100, out=10):
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {"type": "assistant", "timestamp": iso, "sessionId": sid, "requestId": "r" + mid, "uuid": "u" + mid,
            "cwd": cwd, "entrypoint": ep,
            "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": "text", "text": "x"}],
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}


def write(path: Path, lines):
    os.makedirs(fs_path(path.parent), exist_ok=True)
    with open(fs_path(path), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """控制台夹具 (C / E / B / A) + 一批 transcript: 控制台会话、未认领的 sdk 会话、交互会话、scratchpad 里的会话。"""
    root = tmp_path / "c"
    shutil.copytree(FIX, root)
    old = time.time() - 3600
    for f in root.rglob("*"):
        os.utime(f, (old, old))
    cfgp = tmp_path / "consoles.json"
    cfgp.write_text((FIX / "consoles.template.json").read_text(encoding="utf-8").replace("{root}", root.as_posix()),
                    encoding="utf-8")
    consoles.set_config(cfgp)
    for name in ("_file_cache",):
        monkeypatch.setattr(parser, name, {})
    monkeypatch.setattr(activity, "_LAST_SNAP", None)
    monkeypatch.setattr(activity, "_tail_cache", {})
    monkeypatch.setattr(serve, "_RECS_VIEW", {"t": 0.0, "key": None, "v": None})
    monkeypatch.setattr(procmon, "live_claude_index", lambda *a, **k: None)
    monkeypatch.setattr(trace, "brief_peek", lambda *a, **k: (None, True))   # 本测试不看任务简报

    base = tmp_path / "projects"
    proj_c = str(root / "vscode" / "proj-c")
    vs = "C:\\Users\\u\\.vscode\\demo"
    # 控制台 C: 座位 server (c1) 在任务 1 / 2 各做了一段, 中控 c0 一条
    write(base / "c--proj-c" / f"{U('c1')}.jsonl", [line(U("c1"), "c1a", 1500, proj_c, "sdk-py"),
                                                     line(U("c1"), "c1b", 3500, proj_c, "sdk-py")])
    write(base / "c--proj-c" / f"{U('c0')}.jsonl", [line(U("c0"), "c0a", 2950, proj_c, "sdk-py", inp=1000)])
    # 控制台 E 的自测探针: 在临时沙盒里跑 -> 按 cwd 是假项目, 标签把它改回 E 的项目
    write(base / "C--Temp-sandbox" / f"{U('p1')}.jsonl", [line(U("p1"), "p1a", 1000, "C:\\Temp\\sandbox\\probe", "sdk-py")])
    # 认不出的 sdk 会话 + 交互会话
    write(base / "c--Users-u--vscode-demo" / f"{LOOSE}.jsonl", [line(LOOSE, "la", 1200, "C:\\Users\\u\\other", "sdk-py")])
    write(base / "c--Users-u--vscode-demo" / f"{PARENT}.jsonl", [line(PARENT, "pa", 1000, vs, "claude-vscode", inp=5000)])
    # scratchpad: 在 PARENT 的 scratchpad 里跑的会话, 再嵌一层
    sp = f"C:\\Users\\u\\AppData\\Local\\Temp\\claude\\c--Users-u--vscode-demo\\{PARENT}\\scratchpad\\s-tall__whole"
    sp2 = f"C:\\Users\\u\\AppData\\Local\\Temp\\claude\\C--Users-u-AppData-Local-Temp-x\\{KID}\\scratchpad\\deeper"
    write(base / f"C--Users-u-AppData-Local-Temp-claude-c--Users-u--vscode-demo-{PARENT}-scratchpad-s-tall--whole"
          / f"{KID}.jsonl", [line(KID, "ka", 1100, sp, "sdk-py")])
    write(base / "C--nested" / f"{GRAND}.jsonl", [line(GRAND, "ga", 1150, sp2, "sdk-py")])
    # pytest 的临时目录里跑的 sdk 会话: 路径里没有线索指向真项目 -> 「临时目录」, 不造 repo 这种假项目
    write(base / "C--Users-u-AppData-Local-Temp-pytest-of-u-pytest-1-test-x0-repo" / f"{TMPS}.jsonl",
          [line(TMPS, "ta", 1300, "C:\\Users\\u\\AppData\\Local\\Temp\\pytest-of-u\\pytest-1\\test_x0\\repo", "sdk-py")])
    return {"base": base, "root": root, "proj_c": proj_c}


def test_parser_records_entrypoint_and_scratchpad_parent_project(world):
    recs = {r.message_id: r for r in parser.load_records(world["base"])}
    assert recs["c1a"].entrypoint == "sdk-py" and recs["pa"].entrypoint == "claude-vscode"
    assert recs["ka"].project == "demo" and recs["ka"].under_vscode         # scratchpad 里的会话归父会话的项目
    assert recs["ga"].project == "demo"                                    # 嵌套: 父会话自己也在 scratchpad 里, 再往上找
    assert recs["pa"].project == "demo"
    assert recs["ta"].project == "临时目录" and not recs["ta"].under_vscode  # pytest 临时目录: 不造假项目
    assert recs["p1a"].project == "临时目录"                               # 内核里也是; 控制台标签在展示层再改成 E 的项目
    assert parser._file_cache                                              # 缓存里的原记录没被改
    cached = [r for _sig, rs in parser._file_cache.values() for r in rs if r.message_id == "ka"][0]
    assert cached.project == "s-tall__whole"


def test_scratchpad_parent_missing_falls_back_to_encoded_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "_file_cache", {})
    sp = f"C:\\Users\\u\\AppData\\Local\\Temp\\claude\\c--Users-u--vscode-gone\\{PARENT}\\scratchpad\\leaf"
    write(tmp_path / "x" / f"{KID}.jsonl", [line(KID, "k", 1000, sp, "sdk-py")])
    (r,) = parser.load_records(tmp_path)
    assert r.project == "vscode-gone"                                      # 父会话不在: 按它的编码目录名兜底 (friendly_project)


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH 只在 Windows 上卡人")
def test_long_path_transcripts_are_read_everywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "_file_cache", {})
    monkeypatch.setattr(activity, "_LAST_SNAP", None)
    monkeypatch.setattr(activity, "_tail_cache", {})
    # 与真实情况同形: 目录本身 < 248 字符 (列得出来), 加上文件名才超过 260 (打不开 / stat 不出来)
    deep = tmp_path / ("C--Users-u-AppData-Local-Temp-claude-" + "x" * (238 - len(str(tmp_path)) - 38))
    p = deep / f"{KID}.jsonl"
    assert len(str(deep)) < 248 and len(str(p)) > 260
    write(p, [line(KID, "k", 1000, "C:\\Users\\u\\.vscode\\demo", "sdk-py")])
    assert [r.message_id for r in parser.load_records(tmp_path)] == ["k"]            # 成本内核
    rows = activity.snapshot(tmp_path)["sessions"]
    assert rows[0]["state"] != "UNKNOWN" and rows[0]["project"] == "demo"            # /sessions
    assert [x.name for x in trace.main_files(tmp_path)] == [p.name]                  # /workflow


def test_sessions_rows_carry_console_labels_and_runs_group_them(world):
    out = serve._sessions_with_briefs(world["base"])
    by = {r["session_id"]: r for r in out["sessions"]}
    c1 = by[U("c1")]
    assert c1["entrypoint"] == "sdk-py"
    assert c1["console"] == {"code": "C", "run": "20261026-013942-session", "role": "seat", "agent": "server",
                             "project": "proj-c", "grain": "session", "run_state": "waiting_you", "park_until": None}
    assert c1["project"] == "proj-c" and c1["project_source"] == "console"
    probe = by[U("p1")]
    assert probe["console"]["run"] == consoles.SELF_TEST and probe["project"] == "proj-e"   # 临时沙盒的假项目修掉了
    kid = by[KID]
    assert kid["project"] == "demo" and kid["project_source"] == "scratchpad" and kid["parent_session_id"] == PARENT
    assert by[GRAND]["project"] == "demo"
    assert by[TMPS]["project"] == "临时目录" and by[TMPS]["project_source"] == "temp"
    assert by[LOOSE]["console"] is None and by[PARENT]["console"] is None
    run = out["runs"]["C:20261026-013942-session"]
    assert sorted(run["sessions"]) == sorted([U("c0"), U("c1")])
    assert run["roles"] == {"seat": 1, "conductor": 1} and run["status"] == "idle" and run["tasks"] == 2
    assert run["tokens"] == 110 + 110 + 1010 and run["cost"] > 0
    assert "E:" + consoles.SELF_TEST in out["runs"]
    assert not any(r["project"] in ("s-tall__whole", "sandbox", "probe", "deeper", "repo") for r in out["sessions"])


def test_doctor_console_section(world):
    from tokmon.doctor import console_lines
    lines, warns = console_lines(parser.load_records(world["base"]), consoles.snapshot(force=True))
    text = "\n".join(lines)
    assert "控制台 4 个" in text and "认领率" in text
    assert LOOSE[:8] in text and TMPS[:8] in text                          # 没认领的 sdk 会话按 token 列出来
    assert U("c1")[:8] not in text.split("没认领的")[1].split("项目修正")[0]  # 认领了的不列
    # 控制台标签改项目: E 的自测探针 + C 的两个会话 (夹具的项目根在 pytest 的临时目录里, 内核先归「临时目录」, 标签再改回)
    assert "scratchpad 里的会话归父会话的项目 2 个会话" in text and "控制台标签改了项目 3 个会话" in text
    assert "归「临时目录」1 个会话" in text
    assert warns >= 1                                                      # 认领率 < 80% (夹具很小)
    # 每次运行的对账在 S3 挪进了「控制台契约」一节: 夹具里 C 的账本合计 100 token, transcript 有 1230 -> 标出来, 说清是控制台记账的事
    from tokmon.doctor import contract_lines
    text, cwarns = contract_lines(parser.load_records(world["base"]), consoles.snapshot(force=True))
    text = "\n".join(text)
    assert "⚠ 20261026-013942-session" in text and "响应 3 vs 账本 2" in text and "对不上" in text
    assert "B 不记 usage" in text and cwarns >= 1


def test_temp_dir_shapes():
    from tokmon.project import in_temp_dir, in_temp_dir_from_dir
    assert in_temp_dir(r"C:\Users\u\AppData\Local\Temp\pytest-of-u\x\repo")
    assert in_temp_dir(r"C:\Temp\sandbox") and in_temp_dir("/tmp/build/x")
    assert not in_temp_dir(r"C:\Users\u\.vscode\temp-tools") and not in_temp_dir(r"C:\Users\u\.vscode\proj\tmp")
    assert not in_temp_dir(None) and not in_temp_dir(r"C:\Users\u\AppData\Local\Temp")
    assert in_temp_dir_from_dir("C--Users-u-AppData-Local-Temp-pytest-of-u-x-repo") and in_temp_dir_from_dir("C--Temp-sandbox")
    assert not in_temp_dir_from_dir("c--Users-u--vscode-temp-tools")


def test_tokens_cube_origins_runs_tasks_and_project_fix(world):
    cube = serve.build_cube(world["base"], "all", "all", False)
    assert cube["origins"] == ["interactive", "console:C", "console:E", "unclaimed"]
    sess = {s["id"]: s for s in cube["sessions"]}
    o = lambda sid: cube["origins"][sess[sid]["o"]]   # noqa: E731
    assert o(U("c1")) == "console:C" and o(U("p1")) == "console:E"
    assert o(LOOSE) == "unclaimed" and o(KID) == "unclaimed" and o(PARENT) == "interactive"
    run = cube["runs"][sess[U("c1")]["r"]]
    assert run["id"] == "C/20261026-013942-session" and run["status"] == "idle" and run["grain"] == "session"
    assert sess[U("c1")]["ro"] == "seat" and sess[U("c1")]["ag"] == "server"
    assert sess[U("p1")]["ro"] == consoles.SELF_TEST and sess[U("p1")]["g"] == "console"
    assert cube["runs"][sess[U("p1")]["r"]]["id"] == "E/" + consoles.SELF_TEST
    cols = cube["cols"]
    rows = [dict(zip(cols, r)) for r in cube["rows"]]
    xi = {s["id"]: i for i, s in enumerate(cube["sessions"])}
    tasks_of = lambda sid: sorted(cube["tasks"][r["t"]] if r["t"] >= 0 else "" for r in rows if r["x"] == xi[sid])  # noqa: E731
    assert tasks_of(U("c1")) == ["C/20261026-013942-session/1", "C/20261026-013942-session/2"]   # 一个会话切到两个任务
    assert tasks_of(U("c0")) == ["C/20261026-013942-session/2"]
    assert tasks_of(PARENT) == [""]
    # 项目修正: 立方与 /api/summary 同一份 (自测探针归 E 的项目, scratchpad 归父会话的项目)
    projects = [p["id"] for p in cube["projects"]]
    assert "proj-e" in projects and "sandbox" not in projects and "probe" not in projects
    summ = serve.build_summary(world["base"], "all", "all", False)
    assert {r["label"]: r["tokens"] for r in summ["by_project"]} == {
        p: sum(r["in"] + r["out"] + r["cr"] + r["cw"] for r in rows if cube["projects"][r["p"]]["id"] == p) for p in projects}


def test_no_console_config_means_everything_is_interactive_or_unclaimed(world, tmp_path):
    consoles.set_config(tmp_path / "none.json")
    cube = serve.build_cube(world["base"], "all", "all", False)
    assert cube["origins"] == ["interactive", "unclaimed"] and cube["runs"] == [] and cube["tasks"] == []
    assert all(r[cube["cols"].index("t")] == -1 for r in cube["rows"])


def test_scratchpad_child_of_console_session_follows_console_project(world):
    # 在控制台会话 (C 的座位 c1) 的 scratchpad 里跑的子会话: /tokens 与 /sessions 都归 C 的项目
    sp = "C:/Users/u/AppData/Local/Temp/claude/c--proj-c/" + U("c1") + "/scratchpad/t"
    child = "99999999-8888-4777-8666-555555555555"
    write(world["base"] / "C--sp-child" / f"{child}.jsonl", [line(child, "sc", 1600, sp, "sdk-py")])
    cube = serve.build_cube(world["base"], "all", "all", False)
    sess = {s["id"]: s for s in cube["sessions"]}
    assert cube["projects"][sess[child]["p"]]["id"] == "proj-c"
    out = serve._sessions_with_briefs(world["base"])
    assert {r["session_id"]: r for r in out["sessions"]}[child]["project"] == "proj-c"


def test_console_projects_carries_under_vscode_with_the_project():
    from datetime import datetime, timezone
    from tokmon.consoles import Console, Label
    from tokmon.records import UsageRecord
    from tokmon.tokens_view import console_projects

    class Labels:
        labels = {"s": Label("Z", "r1", "worker", "w01", "proj", "session")}
        _cwd_rules = []

        def label(self, sid, cwd=None, ep=None):
            return self.labels.get(sid)

        def console(self, code):
            return Console("Z", "Z", "office", "", "proj", True)

    t = datetime(2026, 10, 29, tzinfo=timezone.utc)
    r = UsageRecord(timestamp=t, project="sandbox", session_id="s", model="m", source_kind="main", input_tokens=1,
                    output_tokens=1, cache_5m=0, cache_1h=0, cache_read=0, web_search=0, web_fetch=0, cost_usd=0.0,
                    known_price=True, message_id="m", request_id="r", cwd="C:/Temp/sandbox", under_vscode=False)
    (out,) = console_projects([r], Labels())
    assert (out.project, out.under_vscode) == ("proj", True)      # 「仅 .vscode」时它跟着新项目留下
    assert (r.project, r.under_vscode) == ("sandbox", False)       # 原记录 (缓存里的) 不动


def test_no_task_brief_is_cached_and_not_counted_pending(tmp_path, monkeypatch):
    for name in ("_FILES", "_TASK_INDEX", "_AGENT_IDX", "_SCRIPTS", "_META", "_BUILT", "_CLAIMED", "_STATS", "_STAMPS",
                 "_BRIEF"):
        monkeypatch.setattr(trace, name, {})
    p = tmp_path / "x" / f"{KID}.jsonl"
    write(p, [line(KID, "k", 1000, "C:/Users/u/.vscode/demo", "sdk-py")])        # 控制台的 sdk 会话: 没有人类提问行
    assert trace.brief_peek(p) == (None, False)                    # 还没算过
    assert trace.session_brief(p) is None
    assert trace.brief_peek(p) == (None, True)                     # 算过了, 就是「没有任务」: 新鲜, 不再排队
    monkeypatch.setattr(trace, "brief_peek", lambda *a, **k: (None, True))
    assert serve._brief_nowait(str(p), False) == (None, True)


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="没装 node")
def test_sessions_page_folds_console_runs(world, tmp_path):
    import subprocess
    out = serve._sessions_with_briefs(world["base"])
    run = "C:20261026-013942-session"
    kids = sorted(out["runs"][run]["sessions"])
    out.update(__run=run, __kids=kids, __loose=PARENT, __kidsFilter="uniq-only-one-kid")
    out["quota"]["seven_day"].update(stale=False, expired=False, utilization=0.896)   # 新鲜的一格; 89.6% 写成 90% 就得是红的
    page = tmp_path / "sessions.html"
    page.write_text(serve.SESS_PAGE, encoding="utf-8")
    harness = Path(__file__).parent / "js" / "sessions_smoke.js"

    def smoke(d):
        data = tmp_path / "s.json"
        data.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(["node", str(harness), str(page), str(data)], capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
        assert proc.stdout.strip(), proc.stderr
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        assert report["errors"] == [], report["errors"]
        return report["seen"]

    seen = smoke(out)
    for k in ("folded", "toggle_open", "filtered_count", "hash_expand", "quota_five_hour", "quota_seven_day"):
        assert seen.get(k), f"没覆盖到 {k}: {seen}"
    out["quota"]["five_hour"] = None                                   # 这个窗口没有读数: 那一格不画
    seen = smoke(out)
    assert not seen.get("quota_five_hour") and seen.get("quota_seven_day")
    # JS 里写了 hidden 不够: .kids 的 display:flex 会盖掉浏览器默认的 [hidden] (审查发现收起没用), 要有这条
    assert ".kids[hidden] { display:none; }" in serve.SESS_PAGE
