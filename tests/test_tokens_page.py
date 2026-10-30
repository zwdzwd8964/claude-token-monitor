"""/tokens 页面的前端冒烟测试: 合成会话 -> serve.build_cube -> Node 桩 DOM 里跑一遍页面 JS。

与 test_workflow_page.py 同一套路: 页面脚本进测试套件, 任一处抛错 / 被 safe() 吞掉的渲染错误 / 没转义的标签都会失败。
覆盖: 首屏 / $↔tokens / 四种堆叠 / 每种筛选 (且 KPI 跟着变) / 多选 OR · 跨维度 AND / 「其他」多值 / 框选 /
窗口切换 (小时粒度、预算线、无基线、请求在途 / 失败) / URL 往返 / 恶意 URL (含原型链键、不存在的日期) / 空数据 / 自己对账 /
触屏滑动留下的半截框选 / 只有未知单价 token 的项目。没装 node 就跳过。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tokmon import serve
from test_tokens_view import base  # noqa: F401  (pytest fixture: 三项目 × 三来源 × 含未知单价的合成会话)

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "js" / "tokens_smoke.js"
PAGE = ROOT / "tokmon" / "pages" / "tokens.html"


def _export(b, out: Path) -> dict:
    cubes = {s: serve.build_cube(b, s, "all", False) for s in ("today", "24h", "7d", "2w", "all")}
    empty = json.loads(json.dumps(cubes["7d"]))
    empty.update(rows=[], sessions=[], projects=[], days=[])
    empty["baseline"] = None
    empty["total"] = {k: (0 if not isinstance(v, bool) else False) for k, v in empty["total"].items()}
    cubes["empty"] = empty
    data = {"cubes": cubes, "budget": {"configured": True, "budgets": [
        {"scope": "daily", "project": None, "limit": 1.0, "spend": 0.5, "pct": 50},
        {"scope": "weekly", "project": None, "limit": 5.0, "spend": 4.0, "pct": 80}]}}
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _page_html(tmp_path: Path) -> Path:
    """跟服务端一样换掉占位符, 再交给 Node (脚本本身不依赖它们, 但保持同一份输入)。"""
    p = tmp_path / "tokens.html"
    p.write_text(serve.PAGE, encoding="utf-8")
    return p


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_tokens_page_runs_clean(base, tmp_path):  # noqa: F811
    data = _export(base, tmp_path / "data.json")
    assert data["cubes"]["7d"]["rows"] and data["cubes"]["today"]["rows"]
    proc = subprocess.run(["node", str(HARNESS), str(_page_html(tmp_path)), str(tmp_path / "data.json")],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.stdout.strip(), proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["errors"] == [], report["errors"]
    seen = report["seen"]
    for k in ("first", "reconciled", "callout_comp", "callout_heat", "callout_pareto", "baseline_chip", "wf_link",
              "escaped", "metric", "multi", "url_roundtrip", "multi_vs", "brush", "hourly", "budget_line",
              "no_baseline", "hostile_url", "empty", "brush_pointer",
              "save", "save_list",                                   # 省钱 S2 卡片 (有数据的那一支)
              "f_p", "f_m", "f_s", "f_x", "f_d", "f_h", "f_wd", "f_c", "f_k", "f_e",
              # 审查修复的回归 (触屏半截框选 / 悬停坐标 / 未知单价项目 / 图例自筛 / 切窗口在途与失败 / 今天按星期的假基线 / 按时段不外推)
              "touch_cancel", "burn_hover", "ghost", "legend_self", "switch_inflight", "switch_fail",
              "today_wd_nobase", "no_proj_hour",
              "cons_none"):                                          # 没配控制台: 控制台卡片写明, 不出可点的东西
        assert seen.get(k), f"页面没有覆盖到 {k}: {seen}"


@pytest.fixture
def console_base(base, tmp_path):  # noqa: F811
    """在 base 上加一个合成的 office 控制台 (认领 s-delta, 两个任务) + 一个认不出的 sdk 会话 -> 三种归属都有。"""
    import os
    import time
    from datetime import datetime, timedelta

    from tokmon import consoles
    from test_tokens_view import VS, _asst, _write

    now = datetime.now().astimezone()
    run = tmp_path / "console" / "runs" / "20261029-000000-task"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"status": "running", "agents": [
        {"spec": {"id": "core", "kind": "core", "session_id": "s-delta"}}]}), encoding="utf-8")
    ev = lambda seq, dt, typ, data: {"seq": seq, "ts": (now - dt).timestamp(), "type": typ, "agent": None, "data": data}  # noqa: E731
    (run / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [
        ev(0, timedelta(hours=5), "session.started", {}),
        ev(1, timedelta(hours=4), "task.started", {"index": 1}),
        ev(2, timedelta(hours=1), "task.started", {"index": 2})]), encoding="utf-8")
    old = time.time() - 3600
    os.utime(run / "state.json", (old, old))
    run2 = tmp_path / "console" / "runs" / "20261029-010000-notask"          # 第二次运行: 没有 task.* 事件 (切不出任务)
    run2.mkdir()
    (run2 / "state.json").write_text(json.dumps({"status": "idle", "agents": [
        {"spec": {"id": "core", "kind": "core", "session_id": "s-gamma"}}]}), encoding="utf-8")
    (run2 / "events.jsonl").write_text(json.dumps(ev(0, timedelta(days=9), "session.started", {})) + "\n", encoding="utf-8")
    os.utime(run2 / "state.json", (old, old))
    cfg = tmp_path / "consoles.json"
    cfg.write_text(json.dumps({"version": 1, "consoles": [
        {"code": "D", "name": "丁", "dialect": "office", "project_root": VS + r"\delta",
         "runs_dir": str(run.parent), "url": "http://127.0.0.1:9004/"}]}), encoding="utf-8")
    consoles.set_config(cfg)
    lines = [_asst(1, now - timedelta(hours=2), "s-sdk", "claude-sonnet-5-5", 30, 30, cr=300),
             _asst(2, now - timedelta(minutes=30), "s-sdk", "claude-sonnet-5-5", 30, 30, cr=300)]
    for x in lines:
        x["entrypoint"] = "sdk-py"
    _write(base / "c--vs-sdk" / "s-sdk.jsonl", VS + r"\sdkproj", lines)
    return base


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_tokens_page_console_card_drills_down(console_base, tmp_path):
    data = _export(console_base, tmp_path / "data.json")
    assert data["cubes"]["7d"]["origins"] == ["interactive", "console:D", "unclaimed"]
    proc = subprocess.run(["node", str(HARNESS), str(_page_html(tmp_path)), str(tmp_path / "data.json")],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.stdout.strip(), proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["errors"] == [], report["errors"]
    for k in ("cons_card", "cons_drill", "cons_deep", "cons_all_runs", "f_o", "reconciled"):
        assert report["seen"].get(k), f"页面没有覆盖到 {k}: {report['seen']}"


def test_tokens_page_is_offline_and_uses_global_nav():
    """零依赖、可离线: 页面里不引用任何外部资源; 换上了统一导航, 不再是旧页头。"""
    html = serve.PAGE
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html and "<link rel=\"stylesheet\"" not in html
    assert '<a href="/tokens" class="active">' in html
    assert "__BASE__" not in html and "__NAV__" not in html
    raw = PAGE.read_text(encoding="utf-8")
    assert "__BASE__" in raw and "__NAV__" in raw
