"""实例管理 (v0.22) 的页面: /processes 最上面的「★ 我的实例」。

字符串层: 区块在 <main> 最上面、自己轮询 /api/instances、弹窗 / 日志抽屉 / 备用容器都在、实例动作只认令牌不看控制模式、
服务端字符串都过 esc()、按钮不把 id 拼进 onclick 的 JS 字符串。
Node 冒烟 (没装 node 就跳过): 桩 DOM + fetch 把页面脚本跑一遍 —— 核心卡片 / 备用收起 / 远程只读不画按钮 /
各种动作的请求形状 (依赖未就绪 -> 确认 -> force; 编辑读原始登记; 字段错误回显; 📌 预填; local-only 的 403 不清令牌)。
修复轮 (v0.22 评审): 卡片 id 改 ic-<id> (实例 id 叫 modal / log / backup 不再套上定位容器的样式) 且旧深链 #inst-<id> 仍认;
停止确认按实例自己的 stop_grace_s 说; 环境变量值去两头空格; 新状态 degraded / unknown / ambiguous 冲突 / note;
另一个 tokmon 在管实例时整块只读 + 横幅; not-owner / degraded / ambiguous 的中文原因。
修复轮 2: 没有端口的启动要你确认 (portless-confirm -> 确定才 force 重试); 保存 / 改开机策略的 portless-auto 中文;
端口被另一个登记过的实例占着 (by) 时说实例名; 开机提醒跳过的也说出来; 原主人已退出 (owner.stale) -> 「正在接管」+ 快轮询。
数据全是合成的 (demo-*, C:/Users/u/..., 18xxx 端口)。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE_FILE = ROOT / "tokmon" / "pages" / "processes.html"
HARNESS = ROOT / "tests" / "js" / "instances_smoke.js"


def _page() -> str:
    return PAGE_FILE.read_text(encoding="utf-8")


def _script() -> str:
    return re.search(r"<script>(.*?)</script>", _page(), re.S).group(1)


def _block() -> str:
    """页面脚本里实例那一段 (两条标记之间)。"""
    s = _script()
    a, b = s.index("// ---- 实例 (v0.22) 开始 ----"), s.index("// ---- 实例 (v0.22) 结束 ----")
    return s[a:b]


def test_section_is_on_top_of_main():
    p = _page()
    main = p.index("<main>")
    sec = p.index('<section id="instances">')
    assert main < sec < p.index('<div class="controls">') < p.index('<div id="app">')   # 实例在最上, 原来的控件照旧在下面


def test_served_page_carries_the_section():
    from tokmon import serve                                         # 放函数里: 别的模块半成品时只挂这一条
    page = serve.PROC_PAGE
    assert page.count('id="instances"') == 1 and "/api/instances" in page and "__BASE__" not in page


def test_containers_and_endpoints_present():
    p = _page()
    for needle in ('id="inst-modal"', 'id="inst-log"', 'id="inst-backup"', "fetch('/api/instances')",
                   "/api/instances/log?id=", "'/api/instances/start'", "'/api/instances/stop'", "'/api/instances/restart'",
                   "'/api/instances/start-group'", "'/api/instances/save'", "'/api/instances/patch'", "'/api/instances/delete'",
                   "'/api/instances/get'", "'/api/instances/draft'", "'/api/instances/boot-ack'", "'/api/autostart'"):
        assert needle in p, needle
    tag = re.search(r"<details id=\"inst-backup\"[^>]*>", p).group(0)
    assert " open" not in tag                                        # 备用默认收起


def test_instance_actions_only_need_the_token_not_control_mode():
    blk = _block()
    assert "CTRL_MODE" not in blk                                    # 控制模式 = 远程审批开关, 与实例无关
    assert "ensureToken()" in blk and "ctrlHeaders()" in blk         # 与终止进程同一把令牌
    assert "local-only" in blk                                       # 远程 403 不当成令牌失效


def test_card_ids_cannot_collide_with_fixed_containers():
    """c19: 实例 id 可以叫 log / modal / backup —— 卡片 / 备用行的 id 不能落进 inst- 命名空间 (#inst-log/#inst-modal 是 position:fixed)。"""
    blk, p = _block(), _page()
    assert 'id="inst-${' not in blk and blk.count('id="ic-${esc(i.id)}"') == 2      # 卡片 + 备用行
    css = re.search(r"<style>(.*?)</style>", p, re.S).group(1)
    assert set(re.findall(r"#(inst-[\w-]+)", css)) <= {"inst-log", "inst-modal", "inst-backup"}
    assert "#ic-" not in css                                             # 卡片不靠 id 上样式
    assert "(?:ic|inst)-" in blk                                         # 旧深链 #inst-<id> 映射到 ic-<id>


def test_stop_confirm_uses_the_instance_grace():
    """c20: 确认文案按实例自己的 stop_grace_s, 不再写死 10 秒。"""
    blk = _block()
    g = blk[blk.index("function instGrace"):blk.index("async function instStop")]
    assert "stop_grace_s" in g and "10" not in g


def test_polls_on_its_own_and_pauses_when_hidden():
    blk = _block()
    assert "4000" in blk and "document.hidden" in blk
    assert "setInterval(load" not in blk                             # 不挂在原来的「自动刷新」上


_RISKY = re.compile(r"\b[a-z]\.(name|project|description|last_error|command_display|cwd|manifest_error|state_label|detail|with|note)\b")
_TAG = re.compile(r"<(span|div|button|a|td|tr|option|select|details|h2|b)\b")


def test_server_strings_go_through_esc():
    """抽查: 拼 HTML 的行里, 服务端给的字符串字段都在 esc( 里。"""
    bad = []
    for ln in _block().splitlines():
        if not _TAG.search(ln):
            continue
        for m in _RISKY.finditer(ln):
            if ln[m.end():m.end() + 1] == "?" or ln[max(0, m.start() - 3):m.start()] == "if(":
                continue                                             # 只拿来判真假 (三元 / if), 不进 HTML
            if "esc(" not in ln[max(0, m.start() - 32):m.start()]:
                bad.append(ln.strip()[:160])
    assert bad == []


def test_no_ids_inside_inline_js():
    blk = _block()
    assert not re.search(r'onclick="[^"]*\$\{', blk)                # id 只进 data-*, 不进 JS 字符串
    assert "innerHTML=" not in blk.replace("$(sel).innerHTML=html", "")   # 实例区只经 setHTML 写 HTML


# ---- Node 冒烟 ----

def _inst(iid, name, **kw):
    d = {"id": iid, "name": name, "project": "demo", "description": "", "group": "core", "kind": "service", "boot": "manual",
         "ports": [], "open_path": "/", "open_url": None, "state": "stopped", "state_label": "未运行", "started_by": None,
         "pid": None, "uptime_s": None, "listening": [], "deps": [], "deps_ready": True, "tunnel": None, "conflicts": [],
         "exit_code": None, "last_error": None, "last_at": None, "command_display": "python -m http.server",
         "cwd": "C:/Users/u/proj", "env_keys": [], "boot_pending": False, "can_control": True, "log_exists": False,
         "stop_grace_s": 10, "note": None}
    d.update(kw)
    if d["ports"] and "open_url" not in kw:
        d["open_url"] = f"http://localhost:{d['ports'][0]}{d['open_path']}"
    return d


def _status(local=True):
    insts = [
        _inst("demo-api", "示例 API", description="本地 API 服务", boot="auto", ports=[18080, 18081], state="running",
              state_label="运行中", started_by="external", pid=4321, uptime_s=7380.4, log_exists=True,
              listening=[{"port": 18080, "pid": 4321, "name": "python.exe", "mine": True}],
              tunnel={"url": "https://demo-tunnel.trycloudflare.com", "kind": "quick", "pid": 777},
              conflicts=[{"kind": "shared-port", "port": 18080, "with": "demo-old"}], env_keys=["API_KEY"],
              command_display="python -m http.server 18080", stop_grace_s=30,
              note="端口由 com.docker.backend.exe 代理", last_error="端口由 com.docker.backend.exe 代理"),
        _inst("demo-web", "示例 Web", boot="ask", boot_pending=True, ports=[18085],
              deps=[{"name": "数据库", "port": 18900, "ready": False}], deps_ready=False),
        _inst("demo-busy", "占口样例", ports=[18090], state="port_busy", state_label="端口被占用",
              listening=[{"port": 18090, "pid": 999, "name": "other.exe", "mine": False}],
              conflicts=[{"kind": "port-busy", "port": 18090, "owner": {"pid": 999, "name": "other.exe"}}]),
        _inst("demo-crash", "崩溃样例", ports=[18095], state="crashed", state_label="意外退出", exit_code=1,
              last_at=1700000000.0, log_exists=True),
        _inst("tokmon-self", "tokmon", kind="self", ports=[18765], state="self", state_label="本服务", can_control=False),
        _inst("evil", "<img src=x onerror=alert(1)>", project="'\";alert(3)//", description='"><script>alert(2)</script>',
              command_display="<b>x</b>", cwd="C:/Users/u/<i>"),
        # 修复轮的新状态
        _inst("demo-deg", "半死样例", ports=[18097], state="degraded", state_label="进程在 · 端口没在监听",
              started_by="tokmon", pid=2222, uptime_s=65, stop_grace_s=0, log_exists=True,
              last_error="端口在监听，但持有者 other.exe 认不出是它"),
        _inst("demo-unk", "未知样例", ports=[18098], state="unknown", state_label="状态未知（读不到端口表）"),
        _inst("demo-amb", "歧义甲", ports=[18099], state="port_busy", state_label="端口被占用",
              listening=[{"port": 18099, "pid": 3333, "name": "python.exe", "mine": False}],
              conflicts=[{"kind": "ambiguous", "port": 18099, "owner": {"pid": 3333, "name": "python.exe"},
                          "with": ["demo-amb2"]}]),
        _inst("demo-amb2", "歧义乙", ports=[18099], state="port_busy", state_label="端口被占用",
              conflicts=[{"kind": "ambiguous", "port": 18099, "owner": {"pid": 3333, "name": "python.exe"},
                          "with": ["demo-amb"]}]),
        # c19: id 撞上定位容器名的实例
        _inst("modal", "Modal", ports=[18101]),
        _inst("log", "Log", ports=[18102], state="running", state_label="运行中", started_by="tokmon", pid=4444,
              listening=[{"port": 18102, "pid": 4444, "name": "python.exe", "mine": True}]),
        _inst("demo-old", "旧版 API", group="backup", ports=[18080],
              conflicts=[{"kind": "shared-port", "port": 18080, "with": "demo-api"}]),
        _inst("backup", "Backup", group="backup", ports=[18103]),
        # 修复轮 2: 端口被另一个登记过的实例占着 (by) / 没有端口 (外部起的认不出, 启动要你确认)
        _inst("demo-var", "变体乙", ports=[18080, 18081], state="port_busy", state_label="端口被占用",
              listening=[{"port": 18080, "pid": 4321, "name": "python.exe", "mine": False}],
              conflicts=[{"kind": "port-busy", "port": 18080, "owner": {"pid": 4321, "name": "python.exe"}, "by": "demo-api"},
                         {"kind": "port-busy", "port": 18081, "owner": {"pid": 4321, "name": "python.exe"}, "by": "demo-api"}]),
        _inst("demo-np", "无端口样例", ports=[], state_label="未运行（没有端口，外部启动的认不出）",
              deps=[{"name": "数据库", "port": 18900, "ready": False}], deps_ready=False),
    ]
    return {"ok": True, "available": True, "generated_at": 1700000000.0, "manifest_error": "清单坏了: JSON 解析失败",
            "boot": {"handled": 1699990000.0, "pending": ["demo-web"]},                 # 修复轮: 不再有 boot_time
            "owner": {"is_owner": True, "pid": 1111, "port": 18765},
            "instances": insts, "local": local,
            "autostart": {"supported": True, "installed": False, "method": None, "detail": "未登记"}}


def _processes():
    empty = {"procs": [], "count": 0, "rss": 0, "listen_ports": []}
    return {"host": {"cpu_percent": 5, "mem_percent": 40, "mem_used": 1e9, "mem_total": 8e9, "proc_count": 100},
            "listening": [
                {"port": 18080, "addr": "127.0.0.1", "loopback": True, "pid": 4321, "name": "python.exe",
                 "cmd": "python -m http.server 18080", "category": "other", "terminable": False, "create_time": 1700000000.5},
                {"port": 18095, "addr": "0.0.0.0", "loopback": False, "pid": 5555, "name": "node.exe",
                 "cmd": "node server.js", "category": "vscode", "terminable": True, "create_time": None}],
            "categories": {"vscode": {"procs": [{"pid": 5555, "name": "node.exe", "cpu": 0, "rss": 1000, "listen_ports": [18095],
                                                 "create_time": 1700000001.25, "cmd": "node server.js"}],
                                      "count": 1, "rss": 1000, "listen_ports": [18095]},
                           "cloudflare": empty, "railway": empty, "other": {"count": 99, "rss": 5000}},
            "cloudflared": [], "connections": [], "by_remote": [], "conn_total": 0, "generated_at_epoch": 1700000000}


_PORTLESS = "没有端口，认不出它是不是已经在别处跑着；确定要再起一份吗？"


def _replies():
    record = {"id": "demo-api", "name": "示例 API", "project": "demo", "description": "本地 API 服务", "group": "core",
              "kind": "service", "cwd": "C:/Users/u/proj", "command": "python -m http.server 18080",
              "env": {"API_KEY": "secret-value"}, "ports": [18080, 18081], "open_path": "/",
              "deps": [{"name": "数据库", "port": 18900}], "boot": "auto", "stop_grace_s": 10, "start_timeout_s": 60}
    draft = {"name": "node", "project": "proj", "cwd": "C:/Users/u/proj", "command": "node server.js --port 18095",
             "ports": [18095], "kind": "service", "boot": "manual", "group": "core"}
    return {
        "/api/instances/start": [{"ok": False, "reason": "deps-not-ready", "detail": None, "owner": None,
                                  "deps": [{"name": "数据库", "port": 18900, "ready": False}]},
                                 {"ok": True, "state": "starting", "pid": 1234},
                                 {"ok": False, "reason": "port-busy", "detail": None, "owner": {"pid": 999, "name": "other.exe"}},
                                 {"ok": False, "reason": "degraded", "detail": "进程还在但端口没在监听，请用「重启」"},
                                 {"ok": True, "state": "starting", "pid": 1235},
                                 {"ok": False, "reason": "portless-confirm", "detail": _PORTLESS},
                                 {"ok": False, "reason": "portless-confirm", "detail": _PORTLESS},
                                 {"ok": True, "state": "starting", "pid": 1236},
                                 {"ok": False, "reason": "port-busy", "detail": "端口 18080 被实例「示例 API」占用",
                                  "owner": {"pid": 4321, "name": "python.exe"}},
                                 {"ok": False, "reason": "port-busy", "detail": "端口 18080 被实例「示例 API」占用",
                                  "owner": {"pid": 4321, "name": "python.exe"}, "by": "demo-api"}],
        "/api/instances/stop": [{"ok": True, "state": "stopping"},
                                {"__status": 403, "body": {"ok": False, "reason": "local-only"}},
                                {"ok": False, "reason": "ambiguous", "detail": None},
                                {"ok": False, "reason": "not-owner",
                                 "detail": "另一个 tokmon（pid 4242 · 端口 18766）在管理实例，请在它的页面操作"}],
        "/api/instances/restart": [{"ok": True, "state": "stopping"}],
        "/api/instances/get": [{"ok": True, "instance": record}],
        "/api/instances/save": [{"ok": False, "reason": "invalid", "errors": {"cwd": "cwd-missing", "ports": "dup-port",
                                                                             "weird": "required"}},
                                {"ok": False, "reason": "invalid", "errors": {"boot": "portless-auto"}},
                                {"ok": True, "instance": dict(record, ports=[18080])}],
        "/api/instances/draft": [{"ok": True, "draft": draft, "warning": "命令行可能含密钥, 保存前核对一下"},
                                 {"ok": False, "reason": "identity-required"}],
        "/api/instances/patch": [{"ok": True, "instance": record}, {"ok": True, "instance": record},
                                 {"ok": False, "reason": "invalid", "errors": {"boot": "portless-auto"}}],
        "/api/instances/start-group": [{"ok": True, "queued": ["demo-web"],
                                        "skipped": [{"id": "demo-api", "reason": "already-running"}]}],
        "/api/instances/boot-ack": [{"ok": True, "queued": ["demo-web"], "skipped": [{"id": "demo-np", "reason": "portless"}]}],
        "/api/autostart": [{"ok": True, "method": "task", "reason": None, "detail": None}],
        "/api/instances/delete": [{"ok": True}],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_instances_section_behaviour(tmp_path):
    js = tmp_path / "page.js"
    js.write_text(_script(), encoding="utf-8")
    unavailable = dict(_status(), available=False, instances=[], manifest_error=None,
                       boot={"handled": None, "pending": []})
    notowner = dict(_status(True), manifest_error=None, owner={"is_owner": False, "pid": 4242, "port": 18766})
    stale = dict(_status(True), manifest_error=None, owner={"is_owner": False, "pid": None, "port": None, "stale": True})
    data = {"status": {"local": _status(True), "remote": dict(_status(False), manifest_error=None), "unavailable": unavailable,
                       "notowner": notowner, "stale": stale},
            "processes": _processes(),
            "log": {"ok": True, "lines": ["=== 由 tokmon 启动 (user) ===", "listening on 18080"], "size": 2048, "exists": True},
            "replies": _replies()}
    f = tmp_path / "data.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(["node", str(HARNESS), str(js), str(f)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.stdout.strip(), proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["errors"] == [], report
    failed = [k for k, v in report["checks"].items() if not v]
    assert failed == [] and len(report["checks"]) >= 70, report["checks"]
