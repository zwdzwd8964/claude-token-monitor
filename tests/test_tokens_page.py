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
              "f_p", "f_m", "f_s", "f_x", "f_d", "f_h", "f_wd", "f_c", "f_k", "f_e",
              # 审查修复的回归 (触屏半截框选 / 悬停坐标 / 未知单价项目 / 图例自筛 / 切窗口在途与失败 / 今天按星期的假基线 / 按时段不外推)
              "touch_cancel", "burn_hover", "ghost", "legend_self", "switch_inflight", "switch_fail",
              "today_wd_nobase", "no_proj_hour"):
        assert seen.get(k), f"页面没有覆盖到 {k}: {seen}"


def test_tokens_page_is_offline_and_uses_global_nav():
    """零依赖、可离线: 页面里不引用任何外部资源; 换上了统一导航, 不再是旧页头。"""
    html = serve.PAGE
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html and "<link rel=\"stylesheet\"" not in html
    assert '<a href="/tokens" class="active">' in html
    assert "__BASE__" not in html and "__NAV__" not in html
    raw = PAGE.read_text(encoding="utf-8")
    assert "__BASE__" in raw and "__NAV__" in raw
