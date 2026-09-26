"""会话驾驶舱 S2: 等你时叫你 —— /api/attention (轻量轮询) + 导航里的提醒脚本 (Node 冒烟)。

纪律: 只对进程自报的「等你授权 / 等你回答」(0.15.1 的 PERMISSION_NEEDED / QUESTION_PENDING, 持续 60 秒才有);
只在本机浏览器, 零外发, 默认关; 刚打开页面不补弹旧事件; 同一段等待只弹一次 (多个标签页也一样)。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tokmon import activity, events, procmon, serve

HARNESS = Path(__file__).parent / "js" / "bell_smoke.js"


@pytest.fixture
def attn(monkeypatch):
    bus = events.EventBus()
    monkeypatch.setattr(serve, "event_bus", bus)
    snap = {"sessions": [
        {"session_id": "S1", "project": "demo", "title": "修登录页", "state": "BLOCKED_ON_USER",
         "state_label": "等你回答 · Claude 在问你", "file": "x", "last_text": "不该外泄的正文"},
        {"session_id": "S2", "project": "demo", "title": "别的", "state": "WORKING", "state_label": "运行中"}]}
    monkeypatch.setattr(activity, "snapshot", lambda base=None, live=None: snap)
    monkeypatch.setattr(procmon, "live_claude_index", lambda: {})
    return bus


def test_attention_first_poll_gives_cursor_only(attn):
    attn.emit(events.Event.make("QUESTION_PENDING", session="S1", project="demo", severity="warning",
                                timestamp=1.0, dedup_key="QUESTION_PENDING:S1:reg:1"))
    d = serve._attention(None, -1)
    assert d["events"] == [] and d["seq"] == 1                           # 刚打开页面: 不补旧事件
    assert d["blocked"] == [{"session_id": "S1", "project": "demo", "title": "修登录页",
                             "state_label": "等你回答 · Claude 在问你"}]   # 只带这几个字段, 不带正文


def test_attention_only_waiting_events_after_cursor(attn):
    mk = lambda t, k: attn.emit(events.Event.make(t, session="S1", project="demo", timestamp=1.0, dedup_key=k))
    mk("QUESTION_PENDING", "a")
    seq = serve._attention(None, -1)["seq"]
    mk("TOOL_ERROR", "b")
    mk("PERMISSION_NEEDED", "c")
    mk("DESTRUCTIVE_OP", "d")                                             # 风险事件继续静默
    mk("CONTEXT_LARGE", "e")                                              # 上下文过 30 万: 下发, 页面上勾了才弹
    d = serve._attention(None, seq)
    assert [e["dedup_key"] for e in d["events"]] == ["c", "e"] and d["seq"] == 5
    assert serve._attention(None, d["seq"])["events"] == []


def test_every_nav_page_carries_the_bell():
    for name in ("SESS_PAGE", "PROC_PAGE", "WORKFLOW_PAGE", "PAGE", "DOCTOR_PAGE"):
        page = getattr(serve, name)
        assert page.count('id="mcbell"') == 1 and page.count('<script id="mc-bell-js">') == 1, name


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_bell_script_behaviour(tmp_path):
    js = re.search(r'<script id="mc-bell-js">(.*?)</script>', serve.SESS_PAGE, re.S).group(1)
    f = tmp_path / "bell.js"
    f.write_text(js, encoding="utf-8")
    proc = subprocess.run(["node", str(HARNESS), str(f)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.stdout.strip(), proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["errors"] == [], report
    assert len(report["checks"]) >= 19 and all(report["checks"].values())
