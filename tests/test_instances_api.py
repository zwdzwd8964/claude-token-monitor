"""实例层 v0.22 · serve 这一侧: /api/instances* 的闸门、审计、参数传递, 事件 / 通知 / 铃铛的新类型。

盯死的护栏 (用户决定, 不是实现细节):
- 变更只认 X-Control-Token **头** (Cookie 不行) + **只认本机 Host** —— 手机经隧道来, 令牌对也只读 (403 local-only);
- 实例启停**不要求**控制模式 (remote_mode 关着照样能用);
- 每个变更都审计, 审计目标只写实例名 / id / 组名, 绝不写命令行 / 路径;
- 读 API 在远程模式下照样过读门 (要令牌), 并告诉页面这次请求是不是本机。

instances / autostart 两个模块整个换成假的 (它们各有自己的测试); 这里只测 serve 的策略与接线。
数据全是合成的 (demo-api / C:/Users/u/proj / 18xxx 端口)。
"""

import http.client
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tokmon import activity, events, notify, procmon, remote, serve

TOKEN = "test-token-inst-123"
SERVE_PORT = 18765                      # 交给 _make_handler 的「本服务端口」(与测试服务器实际端口无关)
TUNNEL = "tunnel.example.com"


_UNSET = "<unset>"


class FakeInstances:
    """instances 模块的替身: 记下每次调用, 按 results 回话, boom 里的动作抛异常。"""

    def __init__(self):
        self.calls = []
        self.results = {}
        self.boom = set()
        self.avail = True
        self.status = {}                                  # 覆盖 status_all 顶层字段 (owner / boot ...)
        self.records = {"demo-api": {
            "id": "demo-api", "name": "示例 API", "project": "demo", "description": "合成数据",
            "group": "core", "kind": "service", "cwd": "C:/Users/u/proj",
            "command": "python -m http.server 18080 --token=abcdefgh12345678",
            "env": {"API_KEY": "not-a-real-secret"}, "ports": [18080], "open_path": "/",
            "deps": [], "boot": "manual", "stop_grace_s": 10, "start_timeout_s": 60}}

    def _hit(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        if name in self.boom:
            raise RuntimeError("boom")
        return self.results.get(name, {"ok": True})

    def named(self, name):
        return [c for c in self.calls if c[0] == name]

    def available(self):
        return self.avail

    def status_all(self, serve_port=None):
        self.calls.append(("status_all", (), {"serve_port": serve_port}))
        d = {"ok": True, "available": True, "generated_at": 1.0, "manifest_error": None,
             "owner": {"is_owner": True, "pid": 4242, "port": SERVE_PORT},
             "boot": {"handled": None, "pending": []}, "instances": []}
        d.update(self.status)
        return d

    def log_tail(self, iid, n=200):
        self.calls.append(("log_tail", (iid, n), {}))
        return {"ok": True, "lines": ["hello"], "size": 6, "exists": True}

    def get_instance(self, iid):
        rec = self.records.get(iid)
        return dict(rec) if rec else None

    def start(self, iid, force=False, wait_deps=False, actor="user"):
        return self._hit("start", iid, force=force, wait_deps=wait_deps, actor=actor)

    def stop(self, iid, actor="user"):
        return self._hit("stop", iid, actor=actor)

    def restart(self, iid, actor="user"):
        return self._hit("restart", iid, actor=actor)

    def start_group(self, group="core", actor="user"):
        return self._hit("start_group", group, actor=actor)

    def save_instance(self, data, original_id=None):
        return self._hit("save_instance", data, original_id=original_id)

    def patch_instance(self, iid, fields):
        return self._hit("patch_instance", iid, fields)

    def delete_instance(self, iid):
        self.records.pop(iid, None)                       # 删完就查不到名字了 —— 审计必须在删之前取名
        return self._hit("delete_instance", iid)

    def draft_from_pid(self, pid, create_time):
        return self._hit("draft_from_pid", pid, create_time)

    def boot_ack(self, action):
        return self._hit("boot_ack", action)

    def start_supervisor(self, *, boot=False, serve_port=None, emit=None):
        self.calls.append(("start_supervisor", (), {"boot": boot, "serve_port": serve_port}))


class FakeAutostart:
    def __init__(self):
        self.calls = []
        self.installed = False

    def status(self):
        self.calls.append(("status",))
        return {"supported": True, "installed": self.installed,
                "method": "task" if self.installed else None, "detail": ""}

    def install(self, port=8765, host="127.0.0.1", claude_dir=_UNSET):
        self.calls.append(("install", port, host, claude_dir))     # _UNSET = 调用方根本没传这个参数
        self.installed = True
        return {"ok": True, "method": "task", "reason": None, "detail": ""}

    def uninstall(self):
        self.calls.append(("uninstall",))
        self.installed = False
        return {"ok": True}


@pytest.fixture
def world(tmp_path, monkeypatch):
    fi, fa = FakeInstances(), FakeAutostart()
    monkeypatch.setattr(serve, "instances", fi)
    monkeypatch.setattr(serve, "autostart", fa)
    monkeypatch.setitem(serve._AUTOSTART_CACHE, "t", 0.0)
    monkeypatch.setitem(serve._AUTOSTART_CACHE, "v", None)
    monkeypatch.setattr(serve.control.plane, "token", TOKEN)
    monkeypatch.setattr(serve.control.plane, "check_token", lambda t: bool(t) and t == TOKEN)
    monkeypatch.setattr(serve.control.plane, "remote_mode", False)        # 控制模式关着: 实例启停照样能用
    audit = []
    monkeypatch.setattr(serve.control.plane, "audit_action",
                        lambda kind, target, outcome, session=None, project=None: audit.append((kind, target, outcome)))
    servers = []

    def start(cfg=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._make_handler(tmp_path, cfg, port=SERVE_PORT))
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    w = type("W", (), {})()
    w.inst, w.auto, w.audit, w.start = fi, fa, audit, start
    yield w
    for h in servers:
        h.shutdown()
        h.server_close()


def _req(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def _post(url, path, body, token=TOKEN, host=None):
    h = {}
    if token:
        h["X-Control-Token"] = token
    if host:
        h["Host"] = host
    code, text, _ = _req(url + path, "POST", h, body)
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


ALL_POSTS = [
    ("/api/instances/start", {"id": "demo-api"}),
    ("/api/instances/stop", {"id": "demo-api"}),
    ("/api/instances/restart", {"id": "demo-api"}),
    ("/api/instances/start-group", {"group": "core"}),
    ("/api/instances/save", {"instance": {"name": "示例 API"}}),
    ("/api/instances/patch", {"id": "demo-api", "fields": {"boot": "auto"}}),
    ("/api/instances/delete", {"id": "demo-api"}),
    ("/api/instances/get", {"id": "demo-api"}),
    ("/api/instances/draft", {"pid": 4321, "create_time": 1.5}),
    ("/api/instances/boot-ack", {"action": "dismiss"}),
    ("/api/autostart", {"on": True}),
]
MUTATORS = ("start", "stop", "restart", "start_group", "save_instance", "patch_instance",
            "delete_instance", "draft_from_pid", "boot_ack")


def _no_mutation(w):
    assert not [c for c in w.inst.calls if c[0] in MUTATORS], w.inst.calls
    assert not [c for c in w.auto.calls if c[0] != "status"], w.auto.calls


# ---------------- 闸门 ----------------

@pytest.mark.parametrize("path,body", ALL_POSTS)
def test_missing_token_is_forbidden(world, path, body):
    url = world.start()
    code, text = _post(url, path, body, token=None)
    assert code == 403 and text == "forbidden"
    _no_mutation(world)
    assert world.audit == []


@pytest.mark.parametrize("path,body", ALL_POSTS)
def test_tunnel_host_is_read_only_even_with_token(world, path, body):
    """手机经隧道 (远程模式开 + 隧道域名已登记 + 令牌对): 读可以, 变更一律 403 local-only (用户决定 #5)。"""
    url = world.start(remote.RemoteConfig(enabled=True, hosts=frozenset({TUNNEL})))
    code, d = _post(url, path, body, host=TUNNEL)
    assert code == 403 and d == {"ok": False, "reason": "local-only"}
    _no_mutation(world)
    assert world.audit == []
    code, d = _post(url, "/api/instances/get", {"id": "demo-api"}, host=TUNNEL + ":443")   # 带端口的写法也一样
    assert code == 403 and d["reason"] == "local-only"


@pytest.mark.parametrize("hdr", ["Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For"])
def test_forwarded_request_is_not_local_even_with_localhost_host(world, hdr):
    """隧道若被配成把 Host 改写成 localhost: 边缘加的转发头照样把它认成远程 (只收紧, 不放宽)。"""
    url = world.start()
    code, _, _ = _req(url + "/api/instances/start", "POST",
                      {"X-Control-Token": TOKEN, "Host": "localhost:8765", hdr: "203.0.113.7"}, {"id": "demo-api"})
    assert code == 403
    _no_mutation(world)
    code, d = _get(url, "/api/instances", {hdr: "203.0.113.7"})
    assert code == 200 and d["local"] is False


def test_cookie_never_opens_instance_mutations(world):
    """读门的 Cookie 对实例变更无效 (与控制门同一条纪律: 只认自定义头, 挡 CSRF)。"""
    url = world.start(remote.RemoteConfig(enabled=True, hosts=frozenset({TUNNEL})))
    _, _, hdrs = _req(url + "/api/login", "POST", body={"token": TOKEN})
    jar = hdrs["Set-Cookie"].split(";")[0]
    code, _, _ = _req(url + "/api/instances/start", "POST", {"Cookie": jar}, {"id": "demo-api"})
    assert code == 403
    _no_mutation(world)


def test_rebinding_host_rejected(world):
    url = world.start()
    code, text = _post(url, "/api/instances/start", {"id": "demo-api"}, host="evil.example.com")
    assert code == 403 and text == "forbidden"                 # Host 白名单先挡 (纯文本, 不是 local-only)
    _no_mutation(world)


def test_remote_mode_off_does_not_block(world):
    """控制模式 (remote_mode) 关着 —— 终止进程会被拒, 但实例启停不看它 (用户决定 #4)。"""
    assert serve.control.plane.remote_mode is False
    url = world.start()
    code, d = _post(url, "/api/instances/start", {"id": "demo-api"})
    assert code == 200 and d == {"ok": True}
    assert world.inst.named("start") == [("start", ("demo-api",), {"force": False, "wait_deps": False, "actor": "user"})]
    code, d = _post(url, "/api/control/terminate", {"pid": 1})
    assert d == {"ok": False, "reason": "control-mode-off"}   # 对照: 老的控制端点照旧要控制模式


def test_local_host_with_remote_enabled_still_works(world):
    url = world.start(remote.RemoteConfig(enabled=True, hosts=frozenset({TUNNEL})))
    code, d = _post(url, "/api/instances/stop", {"id": "demo-api"}, host="localhost:8765")
    assert code == 200 and d["ok"] is True and world.inst.named("stop")


def test_unknown_instances_path_is_404(world):
    url = world.start()
    code, _ = _post(url, "/api/instances/nope", {"id": "demo-api"})
    assert code == 404


def _direct(handler_cls, method, path, headers, body=None, peer=("192.168.1.50", 50123)):
    """不走 socket 直接跑一次 Handler —— 为了造一个**非回环的 TCP 对端** (本机测试连不出局域网地址)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    h = handler_cls.__new__(handler_cls)
    h.client_address = peer
    h.command, h.path, h.request_version = method, path, "HTTP/1.1"
    h.requestline = f"{method} {path} HTTP/1.1"
    msg = http.client.HTTPMessage()
    for k, v in {**headers, "Content-Length": str(len(data))}.items():
        msg[k] = v
    h.headers = msg
    h.rfile, h.wfile = io.BytesIO(data), io.BytesIO()
    getattr(h, "do_" + method)()
    head, _, payload = h.wfile.getvalue().partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), payload.decode("utf-8", "replace")


def test_lan_peer_claiming_localhost_is_not_local(world, tmp_path):
    """远程模式绑到 0.0.0.0 时, 局域网设备直连并自称 `Host: localhost` + 令牌: 仍然只读 (看 TCP 对端, 不信 Host 头)。"""
    cfg = remote.RemoteConfig(enabled=True, hosts=frozenset({"192.168.1.5"}))
    H = serve._make_handler(tmp_path, cfg, port=SERVE_PORT)
    hdr = {"Host": "localhost", "X-Control-Token": TOKEN, "Content-Type": "application/json"}
    for path, body in ALL_POSTS:
        code, text = _direct(H, "POST", path, hdr, body)
        assert code == 403 and json.loads(text) == {"ok": False, "reason": "local-only"}, path
    _no_mutation(world)
    assert world.audit == []
    code, text = _direct(H, "GET", "/api/instances", hdr)
    assert code == 200 and json.loads(text)["local"] is False
    code, text = _direct(H, "POST", "/api/instances/start", hdr, {"id": "demo-api"}, peer=("127.0.0.1", 50124))
    assert code == 200 and json.loads(text)["ok"] is True                # 对照: 同样的请求从回环来就放行
    code, text = _direct(H, "POST", "/api/instances/stop", hdr, {"id": "demo-api"}, peer=("::ffff:127.0.0.1", 1, 0, 0))
    assert code == 200 and json.loads(text)["ok"] is True                # 双栈监听下的回环写法


@pytest.mark.parametrize("peer,ok", [
    (("127.0.0.1", 1), True), (("127.5.6.7", 1), True), (("::1", 1, 0, 0), True),
    (("::ffff:127.0.0.1", 1, 0, 0), True), (("fe80::1%12", 1, 0, 12), False),
    (("192.168.1.50", 1), False), (("::ffff:192.168.1.50", 1, 0, 0), False), (("10.0.0.2", 1), False),
    (("localhost", 1), False), (("", 1), False), (None, False), ((), False)])
def test_peer_is_loopback(peer, ok):
    assert serve._peer_is_loopback(peer) is ok


def test_not_owner_passes_through_and_is_audited(world):
    """另一个 tokmon 拿着实例层的锁: 实例层回 not-owner, serve 原样透传 (页面据此提示去哪边操作), 审计记 not-owner。"""
    detail = "另一个 tokmon（pid 4321 · 端口 18777）在管理实例，请在它的页面操作"
    for name in ("start", "stop", "restart", "start_group", "save_instance", "patch_instance", "delete_instance", "boot_ack"):
        world.inst.results[name] = {"ok": False, "reason": "not-owner", "detail": detail}
    url = world.start()
    for path, body in ALL_POSTS:
        if path in ("/api/instances/get", "/api/instances/draft", "/api/autostart"):
            continue
        code, d = _post(url, path, body)
        assert code == 200 and d == {"ok": False, "reason": "not-owner", "detail": detail}, path
    assert world.audit and all(outcome == "not-owner" for _, _, outcome in world.audit), world.audit
    assert len(world.audit) == 8


def test_status_passes_owner_through(world):
    world.inst.status = {"owner": {"is_owner": False, "pid": 4321, "port": 18777}}
    url = world.start()
    code, d = _get(url, "/api/instances")
    assert code == 200 and d["owner"] == {"is_owner": False, "pid": 4321, "port": 18777}
    # 修复轮 2: 原主人已退出 (stale) / 冲突里占着端口的实例 id (by) 原样给页面
    stale = {"is_owner": False, "pid": None, "port": None, "stale": True}
    busy = {"kind": "port-busy", "port": 18080, "owner": {"pid": 4321, "name": "python.exe"}, "by": "demo-web"}
    world.inst.status = {"owner": stale, "instances": [{"id": "demo-api", "name": "示例 API", "conflicts": [busy]}]}
    code, d = _get(url, "/api/instances")
    assert code == 200 and d["owner"] == stale and d["instances"][0]["conflicts"] == [busy]


def test_portless_confirm_then_force_passes_through(world):
    """没有端口的实例: 实例层要你确认 (portless-confirm, 带 detail) -> 页面确认后带 force:true 重试; serve 只透传 + 审计。"""
    detail = "没有端口，认不出它是不是已经在别处跑着；确定要再起一份吗？"
    url = world.start()
    world.inst.results["start"] = {"ok": False, "reason": "portless-confirm", "detail": detail}
    code, d = _post(url, "/api/instances/start", {"id": "demo-api", "force": False})
    assert code == 200 and d == {"ok": False, "reason": "portless-confirm", "detail": detail}
    world.inst.results["start"] = {"ok": True, "state": "starting", "pid": 1}
    code, d = _post(url, "/api/instances/start", {"id": "demo-api", "force": True})
    assert code == 200 and d["ok"] is True
    assert [c[2]["force"] for c in world.inst.named("start")] == [False, True]
    assert world.audit == [("instance-start", "示例 API", "portless-confirm"), ("instance-start", "示例 API", "ok")]


# ---------------- 分发 + 审计 ----------------

def test_start_passes_force_strictly_and_audits_name(world):
    url = world.start()
    _post(url, "/api/instances/start", {"id": "demo-api", "force": True})
    _post(url, "/api/instances/start", {"id": "demo-api", "force": "yes"})      # 只认 JSON true
    assert [c[2]["force"] for c in world.inst.named("start")] == [True, False]
    assert world.audit == [("instance-start", "示例 API", "ok")] * 2


def test_failure_reason_is_the_audit_outcome(world):
    world.inst.results["start"] = {"ok": False, "reason": "deps-not-ready", "deps": [{"name": "依赖A", "port": 18401}]}
    url = world.start()
    code, d = _post(url, "/api/instances/start", {"id": "demo-api"})
    assert code == 200 and d["reason"] == "deps-not-ready" and d["deps"][0]["port"] == 18401
    assert world.audit == [("instance-start", "示例 API", "deps-not-ready")]


def test_exception_is_audited_and_500(world):
    world.inst.boom.add("stop")
    url = world.start()
    code, d = _post(url, "/api/instances/stop", {"id": "demo-api"})
    assert code == 500 and "error" in d
    assert world.audit == [("instance-stop", "示例 API", "error")]


def test_every_mutation_dispatches_and_audits(world):
    url = world.start()
    _post(url, "/api/instances/restart", {"id": "demo-api"})
    _post(url, "/api/instances/start-group", {"group": "backup"})
    _post(url, "/api/instances/start-group", {})
    _post(url, "/api/instances/save", {"instance": {"name": "新服务", "command": "python app.py"}, "original_id": "demo-api"})
    _post(url, "/api/instances/save", {"instance": {"name": "另一个"}})
    _post(url, "/api/instances/patch", {"id": "demo-api", "fields": {"boot": "ask"}})
    _post(url, "/api/instances/boot-ack", {"action": "start"})
    _post(url, "/api/instances/draft", {"pid": 4321, "create_time": 1700000000.5})
    _post(url, "/api/instances/delete", {"id": "demo-api"})
    c = world.inst
    assert c.named("restart")[0][1] == ("demo-api",)
    assert [x[1] for x in c.named("start_group")] == [("backup",), ("core",)]      # 缺省 = 核心组
    assert c.named("save_instance")[0][1][0]["name"] == "新服务" and c.named("save_instance")[0][2] == {"original_id": "demo-api"}
    assert c.named("save_instance")[1][2] == {"original_id": None}                  # 新建
    assert c.named("patch_instance")[0][1] == ("demo-api", {"boot": "ask"})
    assert c.named("boot_ack")[0][1] == ("start",)
    assert c.named("draft_from_pid")[0][1] == (4321, 1700000000.5)
    assert c.named("delete_instance")[0][1] == ("demo-api",)
    assert world.audit == [
        ("instance-restart", "示例 API", "ok"),
        ("instance-start-group", "group backup", "ok"),
        ("instance-start-group", "group core", "ok"),
        ("instance-save", "新服务", "ok"),
        ("instance-save", "另一个", "ok"),
        ("instance-patch", "示例 API", "ok"),
        ("instance-boot-ack", "start", "ok"),
        ("instance-draft", "pid 4321", "ok"),
        ("instance-delete", "示例 API", "ok"),              # 名字是删之前取的
    ]


def test_audit_targets_never_carry_command_or_path(world):
    url = world.start()
    for path, body in ALL_POSTS:
        _post(url, path, body)
    assert world.audit
    for kind, target, _ in world.audit:
        assert "C:/Users/u" not in target and "http.server" not in target and "abcdefgh" not in target, (kind, target)


def test_get_returns_full_record_locally_and_is_not_audited(world):
    url = world.start()
    code, d = _post(url, "/api/instances/get", {"id": "demo-api"})
    assert code == 200 and d["ok"] is True
    assert d["instance"]["env"] == {"API_KEY": "not-a-real-secret"} and d["instance"]["cwd"] == "C:/Users/u/proj"
    code, d = _post(url, "/api/instances/get", {"id": "ghost"})
    assert d == {"ok": False, "reason": "not-found"}
    assert world.audit == []


def test_bad_arguments_are_rejected_before_the_module(world):
    url = world.start()
    assert _post(url, "/api/instances/start-group", {"group": "../etc"})[1] == {"ok": False, "reason": "bad-group"}
    assert _post(url, "/api/instances/boot-ack", {"action": "nuke"})[1] == {"ok": False, "reason": "bad-action"}
    assert _post(url, "/api/instances/draft", {"pid": "x"})[1] == {"ok": False, "reason": "bad-pid"}
    assert _post(url, "/api/instances/save", {"instance": "nope"})[1]["reason"] == "invalid"
    assert _post(url, "/api/instances/patch", {"id": "demo-api", "fields": ["boot"]})[1]["reason"] == "invalid"
    assert _post(url, "/api/autostart", {"on": True, "port": 70000})[1] == {"ok": False, "reason": "bad-port"}
    assert _post(url, "/api/autostart", {})[1] == {"ok": False, "reason": "bad-on"}          # 缺字段不当成「卸载」
    assert _post(url, "/api/autostart", {"on": "false"})[1] == {"ok": False, "reason": "bad-on"}
    for bad in (float("nan"), float("inf"), "soon"):           # NaN 会让「差多少秒」的身份核对永远通过
        assert _post(url, "/api/instances/draft", {"pid": 4321, "create_time": bad})[1] ==             {"ok": False, "reason": "bad-create-time"}, bad
    _no_mutation(world)


def test_draft_requires_create_time_for_identity(world):
    """pid 可能已被复用: 没有 create_time 就不读那个进程的命令行, 让页面刷新后重试。"""
    url = world.start()
    code, d = _post(url, "/api/instances/draft", {"pid": 4321, "create_time": None})
    assert code == 200 and d == {"ok": False, "reason": "identity-required"}
    assert not world.inst.named("draft_from_pid")
    assert world.audit == [("instance-draft", "pid 4321", "identity-required")]


def test_autostart_install_uninstall(world, tmp_path):
    url = world.start()
    code, d = _get(url, "/api/instances")
    assert d["autostart"]["installed"] is False
    n_status = len([c for c in world.auto.calls if c[0] == "status"])
    code, d = _get(url, "/api/instances")                     # 缓存命中: 不再起 schtasks
    assert len([c for c in world.auto.calls if c[0] == "status"]) == n_status
    code, d = _post(url, "/api/autostart", {"on": True})
    assert code == 200 and d["ok"] is True
    cd = os.path.abspath(str(tmp_path))                        # 这个 serve 用的是非默认数据目录 -> 开机那次也带上
    assert ("install", SERVE_PORT, "127.0.0.1", cd) in world.auto.calls   # 默认 = 本服务端口, 只绑回环
    _post(url, "/api/autostart", {"on": True, "port": 18999})
    assert ("install", 18999, "127.0.0.1", cd) in world.auto.calls
    code, d = _get(url, "/api/instances")                     # 装完缓存作废: 马上看到真实状态
    assert d["autostart"]["installed"] is True
    _post(url, "/api/autostart", {"on": False})
    assert ("uninstall",) in world.auto.calls
    assert [a for a in world.audit if a[0] == "autostart"] == [
        ("autostart", "install", "ok"), ("autostart", "install", "ok"), ("autostart", "uninstall", "ok")]


def test_autostart_install_default_claude_dir_is_not_passed(world, tmp_path, monkeypatch):
    """用默认数据目录起的 serve: 不带 --claude-dir (开机命令保持最短, 也不依赖 install 认这个参数)。"""
    monkeypatch.setattr(serve, "default_base", lambda: tmp_path)
    url = world.start()
    code, d = _post(url, "/api/autostart", {"on": True})
    assert code == 200 and d["ok"] is True
    assert world.auto.calls[-1] == ("install", SERVE_PORT, "127.0.0.1", _UNSET)


def test_claude_dir_arg_is_absolute_and_skips_default(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "default_base", lambda: tmp_path / "projects")
    assert serve._claude_dir_arg(None) is None
    assert serve._claude_dir_arg(tmp_path / "projects") is None
    assert serve._claude_dir_arg(str(tmp_path / "projects") + os.sep) is None       # 同一个目录的另一种写法
    other = serve._claude_dir_arg(tmp_path / "elsewhere")
    assert other == os.path.abspath(str(tmp_path / "elsewhere")) and os.path.isabs(other)
    monkeypatch.chdir(tmp_path)
    assert serve._claude_dir_arg("rel/dir") == os.path.abspath(os.path.join(str(tmp_path), "rel", "dir"))


# ---------------- 读 API ----------------

def _get(url, path, headers=None):
    code, text, _ = _req(url + path, headers=headers)
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


def test_status_carries_autostart_local_flag_and_serve_port(world):
    url = world.start()
    code, d = _get(url, "/api/instances")
    assert code == 200 and d["ok"] is True and d["available"] is True
    assert d["local"] is True
    assert d["autostart"] == {"supported": True, "installed": False, "method": None, "detail": ""}
    assert world.inst.named("status_all") == [("status_all", (), {"serve_port": SERVE_PORT})]


def test_status_over_tunnel_needs_token_and_says_not_local(world):
    url = world.start(remote.RemoteConfig(enabled=True, hosts=frozenset({TUNNEL})))
    code, _ = _get(url, "/api/instances", {"Host": TUNNEL})
    assert code == 403                                          # 远程模式: 读 API 也要令牌
    code, d = _get(url, "/api/instances", {"Host": TUNNEL, "X-Control-Token": TOKEN})
    assert code == 200 and d["local"] is False                  # 页面据此藏掉启停按钮
    code, _, _ = _req(url + "/api/instances", "HEAD", {"Host": TUNNEL})
    assert code == 401


def test_autostart_status_failure_degrades(world, monkeypatch):
    def bad():
        raise OSError("no schtasks")
    monkeypatch.setattr(world.auto, "status", bad)
    url = world.start()
    code, d = _get(url, "/api/instances")
    assert code == 200 and d["autostart"]["installed"] is False and "查询失败" in d["autostart"]["detail"]


def test_log_endpoint_passes_id_and_n(world):
    url = world.start()
    code, d = _get(url, "/api/instances/log?id=demo-api&n=37")
    assert code == 200 and d["lines"] == ["hello"]
    _get(url, "/api/instances/log?id=demo-api&n=lots")
    _get(url, "/api/instances/log?id=demo-api")
    assert [c[1] for c in world.inst.named("log_tail")] == [("demo-api", 37), ("demo-api", 200), ("demo-api", 200)]


def test_head_knows_instance_endpoints(world):
    url = world.start()
    for p in ("/api/instances", "/api/instances/log"):
        code, _, _ = _req(url + p, "HEAD")
        assert code == 200, p


# ---------------- run_serve: --boot 与监督线程 ----------------

def _stub_pumps(monkeypatch, order):
    for mod in (serve.activity_source, serve.cost_source, serve.risk_source, serve.context_source):
        monkeypatch.setattr(mod, "start_pump", lambda *a, **k: None)
    monkeypatch.setattr(serve.billing, "start_pump", lambda *a, **k: None)
    monkeypatch.setattr(serve.trace, "start_warmer", lambda *a, **k: None)
    monkeypatch.setattr(serve.remote, "from_env", lambda env=None: remote.RemoteConfig())
    monkeypatch.setattr(serve.notify, "start_notifier",
                        lambda cfg=None: order.append("notifier") or type("N", (), {"cfg": notify.NotifyConfig()})())


def _stub_run_serve(monkeypatch, order):
    _stub_pumps(monkeypatch, order)

    class FakeHTTPD:
        allow_reuse_address = True

        def __init__(self, addr, handler, bind_and_activate=True):
            assert bind_and_activate is False                     # 先改 allow_reuse_address 再绑
            self.addr = addr

        def server_bind(self):
            order.append(("bind", self.addr, self.allow_reuse_address))

        def server_activate(self):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(serve, "ThreadingHTTPServer", FakeHTTPD)


def _bind_in_range(handler):
    """在 18900-18999 里找一个空端口绑上 (测试端口段约定, 不碰你在用的端口)。"""
    for p in range(18900, 19000):
        try:
            return serve._bind_server(("127.0.0.1", p), handler)
        except OSError:
            continue
    pytest.skip("18900-18999 没有空端口")


def test_run_serve_boot_starts_supervisor_after_bind(world, tmp_path, monkeypatch, capsys):
    assert "boot" in inspect.signature(serve.run_serve).parameters
    order = []
    _stub_run_serve(monkeypatch, order)
    orig = world.inst.start_supervisor
    monkeypatch.setattr(world.inst, "start_supervisor",
                        lambda **kw: order.append("supervisor") or orig(**kw))
    serve.run_serve(tmp_path, "127.0.0.1", 18765, boot=True)
    assert world.inst.named("start_supervisor") == [("start_supervisor", (), {"boot": True, "serve_port": 18765})]
    bind = ("bind", ("127.0.0.1", 18765), sys.platform != "win32")   # Windows: 关掉 SO_REUSEADDR 再绑
    assert order.index("notifier") < order.index(bind) < order.index("supervisor")
    out = capsys.readouterr().out
    assert "processes#instances" in out and "--boot" in out


def test_run_serve_default_is_not_boot_and_skips_without_psutil(world, tmp_path, monkeypatch, capsys):
    _stub_run_serve(monkeypatch, [])
    serve.run_serve(tmp_path, "127.0.0.1", 18766)
    assert world.inst.named("start_supervisor")[0][2] == {"boot": False, "serve_port": 18766}
    world.inst.calls.clear()
    world.inst.avail = False
    serve.run_serve(tmp_path, "127.0.0.1", 18767)
    assert not world.inst.named("start_supervisor")
    assert "缺 psutil" in capsys.readouterr().out


def test_second_serve_cannot_bind_the_same_port(tmp_path):
    """Windows 上 SO_REUSEADDR = 允许两个进程同绑一个端口: 第二个 serve 必须绑不上 (否则两套实例监督抢一份状态)。"""
    h = serve._make_handler(tmp_path)
    first = _bind_in_range(h)
    try:
        port = first.server_address[1]
        if sys.platform == "win32":
            assert first.allow_reuse_address is False
        with pytest.raises(OSError):
            serve._bind_server(("127.0.0.1", port), h)
    finally:
        first.server_close()
    again = serve._bind_server(("127.0.0.1", port), h)             # 前一个关了: 立刻能重新绑 (重启 tokmon 不用等)
    again.server_close()


def test_run_serve_port_taken_exits_without_supervisor(world, tmp_path, monkeypatch, capsys):
    order = []
    _stub_pumps(monkeypatch, order)
    first = _bind_in_range(serve._make_handler(tmp_path))
    try:
        port = first.server_address[1]
        ran = serve.run_serve(tmp_path, "127.0.0.1", port, boot=True)   # 真的去绑: 绑不上 -> 友好提示后退出
    finally:
        first.server_close()
    assert ran is False                                             # 报「没起来」, cli 据此退出码 1
    assert not world.inst.named("start_supervisor")                 # 没绑上端口的不碰实例层, 更不做开机拉起
    out = capsys.readouterr().out
    assert f"端口 {port} 绑不上" in out and "已启动" not in out


def test_cli_serve_exits_nonzero_when_it_did_not_start(world, tmp_path, monkeypatch, capsys):
    """R5: 端口绑不上 (或远程配置被拒) 时 `tokmon serve` 退出码 1 —— `tokmon serve || ...` 和任务计划程序的
    「上次运行结果」要看得出这次没起来; 正常起来再 Ctrl+C 停掉照旧是 0。"""
    from tokmon import cli
    _stub_pumps(monkeypatch, [])
    first = _bind_in_range(serve._make_handler(tmp_path))
    try:
        port = first.server_address[1]
        assert cli.main(["serve", "--port", str(port), "--claude-dir", str(tmp_path)]) == 1
    finally:
        first.server_close()
    assert f"端口 {port} 绑不上" in capsys.readouterr().out
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "18764", "--claude-dir", str(tmp_path)]) == 1   # 远程预检拒绝
    _stub_run_serve(monkeypatch, [])                                # 能绑上: serve_forever 立刻收到 Ctrl+C
    assert cli.main(["serve", "--port", "18763", "--claude-dir", str(tmp_path)]) == 0
    assert "已停止" in capsys.readouterr().out


def test_boot_without_claude_dir_still_serves_instances(world, tmp_path, monkeypatch, capsys):
    """开机自启那次: Claude 数据目录不在也照常起服务 + 实例监督 (实例层不需要它); 手动 serve 照旧直接退出。"""
    order = []
    _stub_run_serve(monkeypatch, order)
    missing = tmp_path / "no-such-dir"
    serve.run_serve(missing, "127.0.0.1", 18768)
    assert not world.inst.named("start_supervisor") and "找不到 Claude 数据目录" in capsys.readouterr().out
    serve.run_serve(missing, "127.0.0.1", 18768, boot=True)
    assert world.inst.named("start_supervisor") == [("start_supervisor", (), {"boot": True, "serve_port": 18768})]
    out = capsys.readouterr().out
    assert "找不到 Claude 数据目录" in out and "已启动" in out


class _TTY(io.StringIO):
    def isatty(self):
        return True


def test_token_only_printed_to_a_terminal(world, tmp_path, monkeypatch, capsys):
    """输出落进日志 (开机自启 / 重定向) 时令牌原文绝不写进去 —— 日志会被拿去排障、贴出去。"""
    _stub_run_serve(monkeypatch, [])
    serve.run_serve(tmp_path, "127.0.0.1", 18769)
    out = capsys.readouterr().out
    assert TOKEN not in out and "~/.tokmon/control_token" in out and "不写进日志" in out
    tty = _TTY()
    monkeypatch.setattr(sys, "stdout", tty)
    serve.run_serve(tmp_path, "127.0.0.1", 18769)
    assert TOKEN in tty.getvalue()                                  # 终端里照旧给你看 (首次粘贴用)


def test_startup_note_says_read_only_when_not_owner(world):
    world.inst.status = {"owner": {"is_owner": False, "pid": 4321, "port": 18777}}
    note = serve._serve_instances_note(True, SERVE_PORT)
    assert "只读" in note and "4321" in note and "18777" in note and "--boot" not in note
    assert ("status_all", (), {"serve_port": SERVE_PORT}) in world.inst.calls
    # R4: 原来的主人已经退出 (owner.json 里的 pid 死了) -> 不说「另一个 tokmon 在管」, 说正在接管; 也不许出现 pid ?
    world.inst.status = {"owner": {"is_owner": False, "pid": None, "port": None, "stale": True}}
    note = serve._serve_instances_note(True, SERVE_PORT)
    assert "正在接管" in note and "另一个 tokmon" not in note and "?" not in note and "--boot" not in note
    world.inst.status = {"owner": {"is_owner": False, "pid": None, "port": None}}     # 锁被占着、但读不到是谁
    note = serve._serve_instances_note(True, SERVE_PORT)
    assert "只读: 另一个 tokmon 在管理实例" in note and "?" not in note
    world.inst.status = {}
    assert "--boot" in serve._serve_instances_note(True, SERVE_PORT)
    def boom(serve_port=None):
        raise RuntimeError("x")
    world.inst.status_all = boom                                      # 查不到主人: 不影响启动横幅
    assert "--boot" in serve._serve_instances_note(True, SERVE_PORT)


# ---------------- 事件 / 通知 / 铃铛 ----------------

def test_event_types_and_payload_allow_list():
    e = events.Event.make("INSTANCE_BOOT_PENDING", pillar="instances", count=2, state_label="重启后有 2 个实例等你确认",
                          command="python secret.py", cwd="C:/Users/u/proj")
    assert e.severity == "warning"
    assert e.payload == {"count": 2, "state_label": "重启后有 2 个实例等你确认"}   # 命令 / 路径被 allow-list 丢掉
    c = events.Event.make("PROCESS_CRASHED", pillar="instances", severity="warning", project="demo",
                          instance="示例 API", exit_code=1, state_label="意外退出", log="tail with secrets")
    assert c.severity == "warning" and c.payload == {"instance": "示例 API", "exit_code": 1, "state_label": "意外退出"}


def test_notify_summaries():
    s = notify.summarize({"type": "PROCESS_CRASHED", "project": "demo",
                          "payload": {"instance": "示例 API", "exit_code": 1}})
    assert s == "进程崩溃 · demo · 示例 API 意外退出 (退出码 1)"
    assert notify.summarize({"type": "PROCESS_CRASHED", "payload": {"instance": "示例 API", "exit_code": 0}}).endswith("(退出码 0)")
    assert "退出码" not in notify.summarize({"type": "PROCESS_CRASHED", "payload": {"instance": "示例 API", "exit_code": None}})
    assert notify.summarize({"type": "PROCESS_CRASHED", "payload": {}}).endswith("? 意外退出")
    assert notify.summarize({"type": "INSTANCE_BOOT_PENDING", "payload": {"count": 2}}) == "实例待拉起 · 重启后 2 个实例等你确认拉起"


def test_attention_delivers_instance_events(world, monkeypatch):
    """崩溃走事件 (一次性); 「重启后等你确认」不走事件 (常驻状况, 走 instances 字段) —— 两条路都走会弹两次。"""
    assert "PROCESS_CRASHED" in serve._ATTN_TYPES and "INSTANCE_BOOT_PENDING" not in serve._ATTN_TYPES
    bus = events.EventBus()
    monkeypatch.setattr(serve, "event_bus", bus)
    monkeypatch.setattr(activity, "snapshot", lambda base=None, live=None: {"sessions": []})
    monkeypatch.setattr(procmon, "live_claude_index", lambda: {})
    seq = serve._attention(None, -1)["seq"]
    bus.emit(events.Event.make("PROCESS_CRASHED", pillar="instances", severity="warning", project="demo",
                               dedup_key="PROCESS_CRASHED:demo-api:1", instance="示例 API", exit_code=1))
    bus.emit(events.Event.make("INSTANCE_BOOT_PENDING", pillar="instances", dedup_key="INSTANCE_BOOT_PENDING:1", count=1))
    bus.emit(events.Event.make("TOOL_ERROR", dedup_key="x"))
    d = serve._attention(None, seq)
    assert [e["type"] for e in d["events"]] == ["PROCESS_CRASHED"]
    assert d["events"][0]["pillar"] == "instances"               # 铃铛靠 pillar 认出「实例层的崩溃」
    assert "instances" not in d                                   # 没有实例在等你确认: 不带这个字段


def test_attention_carries_boot_pending_even_on_first_poll(world, monkeypatch):
    monkeypatch.setattr(activity, "snapshot", lambda base=None, live=None: {"sessions": []})
    monkeypatch.setattr(procmon, "live_claude_index", lambda: {})
    world.inst.status = {"boot": {"handled": 1700000000.5, "pending": ["demo-api", "demo-web"]}}
    for since in (-1, 0):                                         # 首次轮询 (只给游标) 也带: 它是常驻状况
        d = serve._attention(None, since, SERVE_PORT)
        assert d["instances"] == {"pending": 2, "key": "INSTANCE_BOOT_PENDING:1700000000.5"}
    assert all(c[2] == {"serve_port": SERVE_PORT} for c in world.inst.named("status_all"))   # 与 /api/instances 共用缓存
    world.inst.status["owner"] = {"is_owner": False, "pid": 4321, "port": 18777}
    assert "instances" not in serve._attention(None, -1, SERVE_PORT)   # 只由实例层的主人报: 两个 tokmon 也只响一次
    world.inst.status = {"boot": {"handled": 1700000000.5, "pending": []}}
    assert "instances" not in serve._attention(None, -1, SERVE_PORT)
    world.inst.status = {"boot": {"handled": 1700000000.5, "pending": ["demo-api"]}}
    world.inst.avail = False
    assert "instances" not in serve._attention(None, -1, SERVE_PORT)   # 缺 psutil: 不问实例层
    world.inst.avail = True

    def boom(serve_port=None):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(world.inst, "status_all", boom)
    d = serve._attention(None, -1, SERVE_PORT)                     # 实例层出错: 铃铛照常工作, 只是不带这一项
    assert "instances" not in d and "seq" in d


def test_attention_endpoint_uses_serve_port(world, monkeypatch):
    monkeypatch.setattr(activity, "snapshot", lambda base=None, live=None: {"sessions": []})
    monkeypatch.setattr(procmon, "live_claude_index", lambda: {})
    world.inst.status = {"boot": {"handled": 1700000000.5, "pending": ["demo-api"]}}
    url = world.start()
    code, d = _get(url, "/api/attention?since=-1")
    assert code == 200 and d["instances"] == {"pending": 1, "key": "INSTANCE_BOOT_PENDING:1700000000.5"}
    assert world.inst.named("status_all")[-1][2] == {"serve_port": SERVE_PORT}


def test_notify_debounce_is_per_instance():
    """两个实例前后脚崩 (共同依赖挂了): 两条都推; 同一个实例 120 秒内再崩才去抖。"""
    n = notify.Notifier(notify.NotifyConfig())

    def mk(inst, k):
        return {"type": "PROCESS_CRASHED", "severity": "warning", "pillar": "instances", "session": None,
                "project": "demo", "dedup_key": k, "payload": {"instance": inst, "exit_code": 1}}
    n.on_event(mk("示例 API", "PROCESS_CRASHED:demo-api:1"))
    n.on_event(mk("示例 web", "PROCESS_CRASHED:demo-web:1"))
    n.on_event(mk("示例 API", "PROCESS_CRASHED:demo-api:2"))
    assert [(r["decision"], r["reason"]) for r in n.feed] == [("push", "push"), ("push", "push"), ("suppress", "debounce")]
    assert notify.debounce_key(mk("示例 API", "k")) == ("PROCESS_CRASHED", "示例 API", "demo")
    assert notify.debounce_key({"type": "SESSION_IDLE", "session": "s1", "project": "P",
                                "payload": {"instance": "x"}}) == ("SESSION_IDLE", "s1")    # 有会话: 照旧按会话
    assert notify.debounce_key({"type": "TOKEN_BUDGET_WARNING", "session": None, "project": "P",
                                "payload": {}}) == ("TOKEN_BUDGET_WARNING", None, "P")
    cfg = notify.NotifyConfig(debounce_s=120)                      # 纯函数 decide 用同一把键
    last = {("PROCESS_CRASHED", "示例 API", "demo"): 1000.0}
    assert notify.decide(mk("示例 API", "a"), cfg, last, 0, 1050.0)[1] == "debounce"
    assert notify.decide(mk("示例 web", "b"), cfg, last, 0, 1050.0)[0] is True


def test_notify_debounce_same_name_in_two_projects():
    """实例名不唯一 (只有 id 唯一): 两个项目各有一个「api」, 前后脚崩 -> 两条都推 (第二条不能被当成同一个实例去抖掉)。"""
    n = notify.Notifier(notify.NotifyConfig())

    def mk(project, k):
        return {"type": "PROCESS_CRASHED", "severity": "warning", "pillar": "instances", "session": None,
                "project": project, "dedup_key": k, "payload": {"instance": "api", "exit_code": 1}}
    n.on_event(mk("demo-shop", "PROCESS_CRASHED:api:1"))
    n.on_event(mk("demo-blog", "PROCESS_CRASHED:api-2:1"))
    n.on_event(mk("demo-blog", "PROCESS_CRASHED:api-2:2"))              # 同一个 (名字 + 项目) 120 秒内再崩: 去抖
    assert [(r["decision"], r["reason"]) for r in n.feed] == [("push", "push"), ("push", "push"), ("suppress", "debounce")]
    cfg = notify.NotifyConfig(debounce_s=120)
    last = {notify.debounce_key(mk("demo-shop", "a")): 1000.0}
    assert notify.decide(mk("demo-blog", "b"), cfg, last, 0, 1050.0)[0] is True
    assert notify.decide(mk("demo-shop", "c"), cfg, last, 0, 1050.0)[1] == "debounce"
    # 同名、同项目的两个实例 (核心 / 备用两个变体都叫 web): 按 id 区分, 前后脚崩也两条都推 (第三轮复查)
    last = {notify.debounce_key(mk("demo-shop", "PROCESS_CRASHED:web:1")): 1000.0}
    assert notify.decide(mk("demo-shop", "PROCESS_CRASHED:web-2:1"), cfg, last, 0, 1050.0)[0] is True
    assert notify.decide(mk("demo-shop", "PROCESS_CRASHED:web:2"), cfg, last, 0, 1050.0)[1] == "debounce"


_BELL_HARNESS = r"""
"use strict";
const fs = require("fs"), vm = require("vm");
const code = fs.readFileSync(process.argv[2], "utf-8");
const evs = JSON.parse(fs.readFileSync(process.argv[3], "utf-8"));
const store = new Map([["mc.notify.on", "1"]]);                  // 铃铛已打开
const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k), key: i => [...store.keys()][i] ?? null, get length() { return store.size; } };
const shown = [], responses = [], polls = [];
let href = "";
class Notification { constructor(t, o) { this.title = t; this.opts = o || {}; shown.push(this); } close() {}
  static requestPermission() { return Promise.resolve("granted"); } }
Notification.permission = "granted";
const bell = { textContent: "", title: "", classList: { toggle() {} }, addEventListener() {} };
const ctx = { document: { title: "进程监控", getElementById: id => (id === "mcbell" ? bell : null) },
  localStorage, Notification, Promise, JSON, Math, Date, String, encodeURIComponent,
  fetch: () => { const d = responses.shift(); return Promise.resolve({ ok: !!d, json: () => Promise.resolve(d) }); },
  setInterval: fn => { polls.push(fn); return 1; },
  CustomEvent: class { constructor(t, i) { this.detail = (i || {}).detail; } } };
ctx.window = { Notification, addEventListener() {}, focus() {}, dispatchEvent() {} };
ctx.location = { get href() { return href; }, set href(v) { href = v; } };
const tick = () => new Promise(r => setImmediate(r));
(async () => {
  const out = { error: null };
  try {
    const boot = { pending: 2, key: "INSTANCE_BOOT_PENDING:1700000000.5" };
    responses.push({ blocked: [], events: evs, seq: 1, instances: boot });   // 第一次轮询: 旧事件不补弹, 常驻的「等你确认」照弹
    vm.createContext(ctx); vm.runInContext(code, ctx);
    await tick(); await tick();
    out.first = shown.map(n => n.title);
    responses.push({ blocked: [], events: evs, seq: 9, instances: boot });
    polls[0](); await tick(); await tick();
    out.shown = shown.map(n => ({ title: n.title, body: n.opts.body, tag: n.opts.tag }));
    if (shown.length > 1) shown[1].onclick();
    out.href = href;
  } catch (e) { out.error = String((e && e.stack) || e); }
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node")
def test_bell_fires_for_instance_events(tmp_path):
    js = re.search(r'<script id="mc-bell-js">(.*?)</script>', serve.PROC_PAGE, re.S).group(1)
    (tmp_path / "bell.js").write_text(js, encoding="utf-8")
    (tmp_path / "harness.js").write_text(_BELL_HARNESS, encoding="utf-8")
    evs = [
        {"seq": 2, "type": "PROCESS_CRASHED", "pillar": "instances", "project": "demo", "session": None,
         "dedup_key": "PROCESS_CRASHED:demo-api:1", "payload": {"instance": "示例 API", "exit_code": 1, "state_label": "意外退出"}},
        {"seq": 3, "type": "PROCESS_CRASHED", "pillar": "process", "project": "demo", "session": None,
         "dedup_key": "PROCESS_CRASHED:other", "payload": {}},                       # 别的支柱的崩溃: 不按实例处理
        {"seq": 4, "type": "INSTANCE_BOOT_PENDING", "pillar": "instances", "project": None, "session": None,
         "dedup_key": "INSTANCE_BOOT_PENDING:1", "payload": {"count": 2, "state_label": "重启后有 2 个实例等你确认"}},
        {"seq": 5, "type": "PROCESS_CRASHED", "pillar": "instances", "project": "demo", "session": None,
         "dedup_key": "PROCESS_CRASHED:demo-api:1", "payload": {"instance": "示例 API", "exit_code": 1}},   # 同一次崩溃: 只弹一次
        {"seq": 6, "type": "PROCESS_CRASHED", "pillar": "instances", "project": "demo", "session": None,
         "dedup_key": "PROCESS_CRASHED:demo-web:2", "payload": {"instance": "示例 web", "exit_code": None}},
    ]
    (tmp_path / "evs.json").write_text(json.dumps(evs, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(["node", str(tmp_path / "harness.js"), str(tmp_path / "bell.js"), str(tmp_path / "evs.json")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.stdout.strip(), proc.stderr
    r = json.loads(proc.stdout.strip().splitlines()[-1])
    assert r["error"] is None, r
    assert r["first"] == ["重启后有 2 个实例等你确认拉起"]
    titles = [n["title"] for n in r["shown"]]
    # 「重启后等你确认」只弹一次 (事件那条路不弹, 第二次轮询同一把键也不弹); 别的支柱的崩溃不按实例处理; 同一次崩溃只弹一次
    assert titles == ["重启后有 2 个实例等你确认拉起", "实例意外退出 · 示例 API", "实例意外退出 · 示例 web"], titles
    assert r["shown"][0]["tag"] == "INSTANCE_BOOT_PENDING:1700000000.5"
    assert "退出码 1" in r["shown"][1]["body"] and "不会自动重启" in r["shown"][1]["body"]
    assert "退出码" not in r["shown"][2]["body"]
    assert r["href"] == "/processes#instances"


def test_bell_script_still_in_every_page():
    for name in ("PROC_PAGE", "SESS_PAGE"):
        page = getattr(serve, name)
        assert page.count('<script id="mc-bell-js">') == 1
        assert '"/processes#instances"' in page


# ---------------- 「打开控制模式」收回到本机 (0.22 评审: 否则持令牌者经隧道开模式 -> steer 出命令执行) ----------------

def test_control_mode_on_is_local_only_but_off_works_remotely(world, tmp_path, monkeypatch):
    monkeypatch.setattr(serve.control.plane, "set_mode", lambda on: setattr(serve.control.plane, "remote_mode", on) or on)
    url = world.start(remote.RemoteConfig(enabled=True, hosts=frozenset({TUNNEL})))
    code, d = _post(url, "/api/control/mode", {"on": True}, host=TUNNEL)
    assert code == 200 and d == {"ok": False, "reason": "local-only", "remote_mode": False}   # 200 不是 403: 页面别把令牌当失效
    assert serve.control.plane.remote_mode is False
    assert ("control-mode", "on", "local-only") in world.audit                               # 被挡的尝试也留痕
    code, d = _post(url, "/api/control/mode", {"on": True})                                  # 本机照常能开
    assert code == 200 and d["ok"] is True and serve.control.plane.remote_mode is True
    code, d = _post(url, "/api/control/mode", {"on": False}, host=TUNNEL)                    # 手机上仍能关
    assert code == 200 and d["ok"] is True and serve.control.plane.remote_mode is False


def test_control_mode_on_from_lan_peer_claiming_localhost_is_refused(world, tmp_path, monkeypatch):
    monkeypatch.setattr(serve.control.plane, "set_mode", lambda on: setattr(serve.control.plane, "remote_mode", on) or on)
    H = serve._make_handler(tmp_path, remote.RemoteConfig(enabled=True, hosts=frozenset({"192.168.1.5"})), port=SERVE_PORT)
    hdr = {"Host": "localhost", "X-Control-Token": TOKEN, "Content-Type": "application/json"}
    code, text = _direct(H, "POST", "/api/control/mode", hdr, {"on": True})
    assert code == 200 and json.loads(text)["reason"] == "local-only"
    assert serve.control.plane.remote_mode is False


def test_hook_query_token_needs_loopback_peer(world, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(serve.control.plane, "handle_permission", lambda body, token: seen.append(token) or {"decision": "defer"})
    H = serve._make_handler(tmp_path, remote.RemoteConfig(enabled=True, hosts=frozenset({"192.168.1.5"})), port=SERVE_PORT)
    hdr = {"Host": "localhost", "Content-Type": "application/json"}
    _direct(H, "POST", "/hook/permission?token=" + TOKEN, hdr, {})                            # 局域网对端自称 localhost
    _direct(H, "POST", "/hook/permission?token=" + TOKEN, hdr, {}, peer=("127.0.0.1", 50200))  # 对照: 真回环
    assert seen == [None, TOKEN]
