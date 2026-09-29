"""实例层 (tokmon/instances.py) 测试。

单元部分用一个假的「本机一帧」(LISTEN 表 + 进程表 + 父子关系) 替换 psutil 观测原语, 全部合成数据;
末尾一个真实集成测试: 拉起一个临时目录里的小 HTTP 服务 -> 等它跑起来 -> 优雅停止 (Ctrl+C / SIGINT) -> 确认收尾标记与进程树都没了。
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokmon import events  # noqa: E402
from tokmon import instances as inst  # noqa: E402


# ---------------------------------------------------------------- 夹具 / 假世界

_REAL_EXECUTE_STOP = inst._execute_stop


@pytest.fixture
def home(tmp_path, monkeypatch):
    inst._reset_for_tests()
    inst.set_home(tmp_path / "home")
    for k in ("USERPROFILE", "HOME"):                  # 万一有代码按 Path.home() 现算, 也只落在临时目录
        monkeypatch.setenv(k, str(tmp_path / "userhome"))
    monkeypatch.setattr(inst, "_STATUS_TTL", 0.0)
    audits: list = []
    monkeypatch.setattr(inst, "_audit", lambda k, t, o: audits.append((k, t, o)))
    assert inst.claim_owner()                          # 真实服务在 run_serve 里经 start_supervisor 接管; 测试直接拿锁 (锁文件在临时目录)
    yield audits
    inst._reset_for_tests()                            # 先等在途后台线程收尾 (复位里 join), 再清空
    inst.set_home(tmp_path / "parked")                 # 绝不指回真实 ~/.tokmon: 漏网线程最多写进临时目录


class FakePopen:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class FakeHandle:
    """假 psutil.Process 句柄: 身份 = (pid, create_time), 杀/挂起都落到假世界里。"""

    def __init__(self, world, pid, ct):
        self.world, self.pid, self._ct = world, pid, ct

    def create_time(self):
        return self._ct

    def is_running(self):
        p = self.world.procs.get(self.pid)
        return bool(p) and p["create_time"] == self._ct

    def children(self, recursive=False):
        if not self.is_running():
            return []
        return [FakeHandle(self.world, c, self.world.procs[c]["create_time"]) for c in self.world.desc(self.pid)]

    def kill(self):
        self.world.kill([self.pid], code=1)

    def suspend(self):
        self.world.suspended.add(self.pid)

    def resume(self):
        self.world.suspended.discard(self.pid)


class World:
    """一帧假的本机观测。进程: pid -> {name, cwd, create_time, parent}; 监听: [(port, pid, ip)]。
    respawn[pid] = (port, cwd): 该进程是个「守护循环」—— 它的子进程死了、它自己还活着且没被挂起, 就再拉一个新的;
    新拉起的要过 respawn_delay 次端口扫描才开始监听 (真实服务起来要时间, 杀完立刻看一眼是看不到它的)。
    probe_up: 端口表读不到时, 回环试连能连上的端口 (单元测试里不真的去连)。"""

    def __init__(self, monkeypatch):
        self.procs: dict = {}
        self.listen: list = []
        self.pending: list = []                         # [(剩几次扫描才出现, (port, pid, ip))]
        self.respawn_delay = 1
        self.scan_ok = True                             # False = 端口全表读不到 (如 macOS 非 root)
        self.probe_up: set = set()
        self.cf: list = []
        self.stops: list = []
        self.popens: dict = {}
        self.respawn: dict = {}
        self.suspended: set = set()
        self.spawned: list = []
        self._next = iter(range(700, 800))
        self.gate = None                                # threading.Event: 让假停止卡住, 好观察 stopping 态
        monkeypatch.setattr(inst, "_HAS_PSUTIL", True)
        monkeypatch.setattr(inst, "_scan_listeners", self.scan)
        monkeypatch.setattr(inst, "_loopback_listening", lambda port, fresh=False: port in self.probe_up)
        monkeypatch.setattr(inst, "_proc_info", self.info)
        monkeypatch.setattr(inst, "_descendants", self.desc)
        monkeypatch.setattr(inst, "_ppid", lambda pid: (self.procs.get(pid) or {}).get("parent"))
        monkeypatch.setattr(inst, "_cmdline", lambda pid: list((self.procs.get(pid) or {}).get("cmdline") or []))
        monkeypatch.setattr(inst, "_proc_listen_ports", lambda pid: {x[0] for x in self.listen if x[1] == pid})
        monkeypatch.setattr(inst, "_cloudflared_procs", lambda: list(self.cf))
        monkeypatch.setattr(inst, "_proc_handle", self.handle)
        monkeypatch.setattr(inst, "_execute_stop", self.execute_stop)

    def add(self, pid, name="python.exe", cwd=None, ct=5000.0, parent=None, cmdline=None):
        self.procs[pid] = {"name": name, "cwd": cwd, "create_time": ct, "parent": parent, "cmdline": cmdline}

    def scan(self):
        if not self.scan_ok:
            return None
        out = list(self.listen)
        due = [x for n, x in self.pending if n <= 1]
        self.pending = [(n - 1, x) for n, x in self.pending if n > 1]
        self.listen += due                              # 这次扫描看不到, 下一次才在听
        return out

    def info(self, pid):
        p = self.procs.get(pid)
        return {"pid": pid, "name": p["name"], "cwd": p["cwd"], "create_time": p["create_time"]} if p else None

    def desc(self, pid):
        out, todo = [], [pid]
        while todo:
            cur = todo.pop()
            for c, p in self.procs.items():
                if p["parent"] == cur and c not in out:
                    out.append(c)
                    todo.append(c)
        return out

    def handle(self, pid):
        p = self.procs.get(pid)
        return FakeHandle(self, pid, p["create_time"]) if p else None

    def kill(self, pids, code=None):
        parents = []
        for pid in pids:
            p = self.procs.pop(pid, None)
            if p:
                parents.append(p["parent"])
            if pid in self.popens:
                self.popens[pid].returncode = code
        self.listen = [x for x in self.listen if x[1] not in pids]
        self.pending = [(n, x) for n, x in self.pending if x[1] not in pids]
        for pp in parents:                              # 守护循环: 子进程一死就再拉一个 (被挂起 / 已死则不能)
            if pp in self.respawn and pp in self.procs and pp not in self.suspended:
                port, cwd = self.respawn[pp]
                new = next(self._next)
                self.add(new, "python.exe", cwd, ct=8000.0 + new, parent=pp)
                if self.respawn_delay:
                    self.pending.append((self.respawn_delay, (port, new, "127.0.0.1")))
                else:
                    self.listen.append((port, new, "127.0.0.1"))
                self.spawned.append(new)

    def wait_procs(self, procs, timeout=None):
        return [p for p in procs if not p.is_running()], [p for p in procs if p.is_running()]

    def real_stop(self, monkeypatch, interrupt):
        """换回真实的 _execute_stop (补扫 / 强杀逻辑), 进程与 psutil.wait_procs 仍是假的; interrupt(plan) 模拟 Ctrl+C。"""
        monkeypatch.setattr(inst, "_execute_stop", _REAL_EXECUTE_STOP)
        monkeypatch.setattr(inst.psutil, "wait_procs", self.wait_procs)
        monkeypatch.setattr(inst, "_send_interrupt", interrupt)
        monkeypatch.setattr(inst, "_SWEEP_GAP_S", 0.0)

    def execute_stop(self, plan, grace):
        if self.gate is not None:
            self.gate.wait(5)
        pids = [h.pid for h in plan["procs"]]
        self.stops.append({"pids": sorted(pids), "grace": grace, "plan": plan})
        self.kill(pids, code=0)
        return "graceful" if grace else "forced"


@pytest.fixture
def world(home, monkeypatch):
    w = World(monkeypatch)
    yield w
    if w.gate is not None:
        w.gate.set()                                    # 测试中途失败也放行卡住的假停止, 好让夹具 join 到它


def fake_launcher(world, monkeypatch, listen=True):
    """假拉起: 根 cmd.exe + 一个监听主端口的子进程 (cwd = 实例目录)。world.launch_times 记每次拉起的时刻。"""
    launched: list = []
    world.launch_times = []
    seq = iter(range(100, 100000, 10))

    def _launch(rec, actor):
        root = next(seq)
        world.add(root, "cmd.exe", rec["cwd"], ct=6000.0 + root)
        world.add(root + 1, "python.exe", rec["cwd"], ct=6000.0 + root + 1, parent=root)
        if listen and rec["ports"]:
            world.listen.append((rec["ports"][0], root + 1, "127.0.0.1"))
        p = FakePopen(root)
        world.popens[root] = p
        launched.append((rec["id"], actor))
        world.launch_times.append((rec["id"], time.time()))
        return {"popen": p, "pid": root, "create_time": 6000.0 + root}

    monkeypatch.setattr(inst, "_launch", _launch)
    return launched


def mk(tmp_path, name="demo-api", **kw):
    d = tmp_path / "work" / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "cwd": str(d), "command": "python -m http.server 18080", "ports": [18080]}
    data.update(kw)
    r = inst.save_instance(data)
    assert r["ok"], r
    return r["instance"]


def status_of(iid, serve_port=None):
    for x in inst.status_all(serve_port)["instances"]:
        if x["id"] == iid:
            return x
    raise AssertionError(iid)


def restart_tokmon():
    """模拟 tokmon 进程重启: 复位 (等后台线程收尾、放主人锁、清内存) 再重新接管。"""
    inst._reset_for_tests()
    assert inst.claim_owner()


# ---------------------------------------------------------------- 校验

def test_validate_minimal_ok_with_defaults(tmp_path):
    rec, errs = inst.validate({"name": "Demo API", "cwd": str(tmp_path), "command": "run it", "junk": 1})
    assert errs == {}
    assert rec["id"] == "demo-api"
    assert rec["group"] == "core" and rec["kind"] == "service" and rec["boot"] == "manual"
    assert rec["ports"] == [] and rec["deps"] == [] and rec["env"] == {}
    assert rec["open_path"] == "/" and rec["stop_grace_s"] == 10 and rec["start_timeout_s"] == 60
    assert "junk" not in rec


@pytest.mark.parametrize("patch,field,code", [
    ({"name": ""}, "name", "required"),
    ({"name": "x" * 41}, "name", "too-long"),
    ({"cwd": "C:/Users/u/definitely-missing-dir-xyz"}, "cwd", "cwd-missing"),
    ({"command": ""}, "command", "required"),
    ({"command": "a\nb"}, "command", "bad-format"),
    ({"command": "x" * 2001}, "command", "too-long"),
    ({"ports": "abc"}, "ports", "bad-port"),
    ({"ports": [70000]}, "ports", "bad-port"),
    ({"ports": [18080, 18080]}, "ports", "dup-port"),
    ({"ports": [18001, 18002, 18003, 18004, 18005, 18006]}, "ports", "too-many"),
    ({"group": "misc"}, "group", "bad-choice"),
    ({"kind": "daemon"}, "kind", "bad-choice"),
    ({"boot": "always"}, "boot", "bad-choice"),
    ({"stop_grace_s": 500}, "stop_grace_s", "range"),
    ({"start_timeout_s": 1}, "start_timeout_s", "range"),
    ({"start_timeout_s": "soon"}, "start_timeout_s", "bad-format"),
    ({"env": {"1BAD": "x"}}, "env", "bad-format"),
    ({"env": {f"K{i}": "v" for i in range(31)}}, "env", "too-many"),
    ({"env": {"K": "v" * 1001}}, "env", "too-long"),
    ({"open_path": "index.html"}, "open_path", "bad-format"),
    ({"deps": [{"name": "", "port": 18888}]}, "deps", "required"),
    ({"deps": [{"name": "auth-svc", "port": 0}]}, "deps", "bad-port"),
    ({"deps": [{"name": f"d{i}", "port": 18000 + i} for i in range(6)]}, "deps", "too-many"),
    ({"id": "Bad_ID"}, "id", "bad-format"),
    ({"id": "ok-id\n"}, "id", "bad-format"),
    ({"description": "x" * 301}, "description", "too-long"),
    ({"project": "p" * 61}, "project", "too-long"),
])
def test_validate_error_codes(tmp_path, patch, field, code):
    data = {"name": "demo", "cwd": str(tmp_path), "command": "run"}
    data.update(patch)
    rec, errs = inst.validate(data)
    assert rec is None
    assert errs.get(field) == code, errs


def test_validate_self_kind_exempt_from_cwd_and_command():
    rec, errs = inst.validate({"name": "tokmon", "kind": "self", "cwd": "C:/Users/u/nowhere", "ports": [18765]})
    assert errs == {} and rec["kind"] == "self"


def test_validate_accepts_form_strings(tmp_path):
    rec, errs = inst.validate({"name": "demo", "cwd": str(tmp_path), "command": "run",
                               "ports": "18080, 18081", "deps": "依赖A：18888\nother:18889",
                               "env": "A=1\n# 注释\nB = two", "stop_grace_s": "5"})
    assert errs == {}
    assert rec["ports"] == [18080, 18081]
    assert rec["deps"] == [{"name": "依赖A", "port": 18888}, {"name": "other", "port": 18889}]
    assert rec["env"] == {"A": "1", "B": "two"}
    assert rec["stop_grace_s"] == 5


def test_validate_env_text_trims_keys_and_values(tmp_path):
    # 表单原文 KEY=VALUE: 键和值两头的空白 (空格 / tab / 行尾空格) 都去掉, 值中间的 = 和空格原样保留
    rec, errs = inst.validate({"name": "demo", "cwd": str(tmp_path), "command": "run",
                               "env": "API_TOKEN = abc123\r\n\tDATA_DIR=C:/data \nB= two=three \n"})
    assert errs == {}
    assert rec["env"] == {"API_TOKEN": "abc123", "DATA_DIR": "C:/data", "B": "two=three"}
    assert inst.validate({"name": "demo", "cwd": str(tmp_path), "command": "run", "env": "NOEQUALS"})[1] == \
        {"env": "bad-format"}


def test_derive_id():
    assert inst._derive_id("示例", set()) == "inst-1"
    assert inst._derive_id("示例", {"inst-1"}) == "inst-2"
    assert inst._derive_id("Demo API", {"demo-api"}) == "demo-api-2"
    assert inst._derive_id("Demo API", {"demo-api", "demo-api-2"}) == "demo-api-3"
    long = inst._derive_id("a" * 60, {"a" * 40})
    assert len(long) <= 40 and long.endswith("-2")


# ---------------------------------------------------------------- 持久化

def test_save_patch_delete_roundtrip(home, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    r = inst.save_instance({"name": "Demo API", "cwd": str(d), "command": "run", "ports": [18080],
                            "env": {"TOKEN": "s3cret"}, "unknown": "dropped"})
    assert r["ok"] and r["instance"]["id"] == "demo-api"
    doc = json.loads(inst.MANIFEST.read_text(encoding="utf-8"))
    assert doc["version"] == 1 and len(doc["instances"]) == 1
    assert "unknown" not in doc["instances"][0]
    assert inst.get_instance("demo-api")["env"] == {"TOKEN": "s3cret"}      # 完整记录只给本机编辑用

    # 自动派生 id 撞了 -> 加后缀; 显式 id 撞了 -> id-exists
    r2 = inst.save_instance({"name": "Demo API", "cwd": str(d), "command": "run"})
    assert r2["ok"] and r2["instance"]["id"] == "demo-api-2"
    assert inst.save_instance({"id": "demo-api", "name": "x", "cwd": str(d), "command": "run"}) == \
        {"ok": False, "reason": "id-exists"}
    bad = inst.save_instance({"name": "", "cwd": str(d), "command": "run"})
    assert bad["reason"] == "invalid" and bad["errors"]["name"] == "required"

    # 更新: 不给 id -> 保持原 id; 给新 id -> 改名 (新 id 被占 -> id-exists)
    up = inst.save_instance({"name": "Renamed", "cwd": str(d), "command": "run2"}, original_id="demo-api")
    assert up["ok"] and up["instance"]["id"] == "demo-api" and up["instance"]["command"] == "run2"
    assert inst.save_instance({"id": "demo-api-2", "name": "x", "cwd": str(d), "command": "r"},
                              original_id="demo-api")["reason"] == "id-exists"
    mv = inst.save_instance({"id": "demo-web", "name": "x", "cwd": str(d), "command": "r"}, original_id="demo-api")
    assert mv["ok"] and inst.get_instance("demo-api") is None and inst.get_instance("demo-web")
    assert inst.save_instance({"name": "x", "cwd": str(d), "command": "r"}, original_id="nope")["reason"] == "not-found"

    # patch: 只许白名单字段, 且要过校验
    p = inst.patch_instance("demo-web", {"description": "说明", "group": "backup"})   # (无端口的实例开机策略只能手动, 见 portless 用例)
    assert p["ok"] and p["instance"]["description"] == "说明" and p["instance"]["group"] == "backup"
    assert inst.patch_instance("demo-web", {"command": "evil"})["reason"] == "invalid"
    assert inst.patch_instance("demo-web", {"boot": "always"})["errors"] == {"boot": "bad-choice"}
    assert inst.patch_instance("nope", {"boot": "ask"})["reason"] == "not-found"

    assert inst.delete_instance("demo-web") == {"ok": True}
    assert inst.delete_instance("demo-web")["reason"] == "not-found"
    assert [r["id"] for r in inst.load_manifest()] == ["demo-api-2"]


def test_corrupt_manifest_is_preserved_not_overwritten(home, tmp_path):
    inst.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    inst.MANIFEST.write_text("{ this is not json", encoding="utf-8")
    st = inst.status_all()
    assert st["instances"] == [] and st["manifest_error"]
    d = tmp_path / "proj"
    d.mkdir()
    r = inst.save_instance({"name": "demo", "cwd": str(d), "command": "run"})
    assert r["ok"]
    baks = list(inst.MANIFEST.parent.glob("instances.json.corrupt-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == "{ this is not json"
    assert [x["id"] for x in inst.load_manifest()] == ["demo"]
    assert "corrupt-" in (inst.status_all()["manifest_error"] or "")      # 告诉你原文件另存到哪了


def test_partially_invalid_manifest_backed_up_before_save(home, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    inst.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    good = {"id": "good", "name": "good", "cwd": str(d), "command": "run"}
    inst.MANIFEST.write_text(json.dumps({"version": 1, "instances": [good, {"id": "BAD!!"}]}), encoding="utf-8")
    assert [r["id"] for r in inst.load_manifest()] == ["good"]
    assert "1 条记录无效" in inst.status_all()["manifest_error"]
    assert inst.patch_instance("good", {"group": "backup"})["ok"]
    assert len(list(inst.MANIFEST.parent.glob("instances.json.corrupt-*"))) == 1


# ---------------------------------------------------------------- 状态识别 (假观测)

def test_external_instance_recognized_by_cwd(world, tmp_path):
    rec = mk(tmp_path)
    world.add(4242, "python.exe", cwd=os.path.join(rec["cwd"], "sub"), ct=time.time() - 120)
    world.listen = [(18080, 4242, "0.0.0.0"), (18080, 4242, "::")]      # 双栈同进程只算一次
    s = status_of(rec["id"])
    assert s["state"] == "running" and s["state_label"] == "运行中"
    assert s["started_by"] == "external" and s["pid"] == 4242
    assert s["listening"] == [{"port": 18080, "pid": 4242, "name": "python.exe", "mine": True}]
    assert 100 < s["uptime_s"] < 200
    assert s["open_url"] == "http://localhost:18080/"
    assert s["conflicts"] == [] and s["can_control"] is True


def test_port_busy_by_foreign_owner(world, tmp_path):
    rec = mk(tmp_path)
    world.add(777, "node.exe", cwd="C:/Users/u/other-proj")
    world.listen = [(18080, 777, "127.0.0.1")]
    s = status_of(rec["id"])
    assert s["state"] == "port_busy" and s["started_by"] is None
    assert s["conflicts"] == [{"kind": "port-busy", "port": 18080, "owner": {"pid": 777, "name": "node.exe"}}]
    r = inst.start(rec["id"])
    assert r["ok"] is False and r["reason"] == "port-busy" and r["owner"] == {"pid": 777, "name": "node.exe"}
    assert world.stops == [] and 777 in world.procs                        # 只报告, 绝不替你杀


def test_secondary_port_busy_is_conflict_only(world, tmp_path):
    rec = mk(tmp_path, ports=[18080, 18081])
    world.add(10, "python.exe", cwd=rec["cwd"])
    world.add(11, "redis-server.exe", cwd="C:/Users/u/elsewhere")
    world.listen = [(18080, 10, "127.0.0.1"), (18081, 11, "127.0.0.1")]
    s = status_of(rec["id"])
    assert s["state"] == "running"
    assert s["conflicts"] == [{"kind": "port-busy", "port": 18081, "owner": {"pid": 11, "name": "redis-server.exe"}}]


def test_self_instance_and_service_never_claims_tokmon(world, tmp_path):
    me = os.getpid()
    selfrec = inst.save_instance({"name": "tokmon", "kind": "self", "ports": [18765]})["instance"]
    svc = mk(tmp_path, ports=[18765])
    world.add(me, "python.exe", cwd=svc["cwd"])        # 即便 cwd 落在服务目录下, 也绝不认成服务的进程
    world.listen = [(18765, me, "127.0.0.1")]
    s = status_of(selfrec["id"])
    assert s["state"] == "self" and s["state_label"] == "本服务" and s["pid"] == me and s["can_control"] is False
    v = status_of(svc["id"])
    assert v["state"] == "port_busy"
    assert {"kind": "shared-port", "port": 18765, "with": selfrec["id"]} in v["conflicts"]
    assert inst.start(selfrec["id"])["reason"] == "self" and inst.stop(selfrec["id"])["reason"] == "self"
    assert inst.stop(svc["id"])["reason"] == "not-running"


def test_shared_port_conflicts_both_sides(world, tmp_path):
    a = mk(tmp_path, "web-a", ports=[18173])
    b = mk(tmp_path, "web-b", ports=[18173, 18174])
    assert {"kind": "shared-port", "port": 18173, "with": "web-b"} in status_of(a["id"])["conflicts"]
    assert {"kind": "shared-port", "port": 18173, "with": "web-a"} in status_of(b["id"])["conflicts"]


def test_deps_readiness(world, tmp_path):
    rec = mk(tmp_path, deps=[{"name": "依赖A", "port": 18888}, {"name": "依赖B", "port": 18889}])
    world.add(50, "dep-a.exe", cwd="C:/Users/u/apps")
    world.listen = [(18888, 50, "0.0.0.0")]
    s = status_of(rec["id"])
    assert s["deps"] == [{"name": "依赖A", "port": 18888, "ready": True}, {"name": "依赖B", "port": 18889, "ready": False}]
    assert s["deps_ready"] is False
    r = inst.start(rec["id"])
    assert r["reason"] == "deps-not-ready" and len(r["deps"]) == 2


def test_status_redacts_command_and_hides_env_values(world, tmp_path):
    rec = mk(tmp_path, command="python app.py --token abcdef123456", env={"API_KEY": "s3cret-value"})
    s = status_of(rec["id"])
    assert "abcdef123456" not in s["command_display"]
    assert s["env_keys"] == ["API_KEY"]
    assert "s3cret-value" not in json.dumps(inst.status_all(), ensure_ascii=False)


def test_status_without_psutil_degrades(home, tmp_path, monkeypatch):
    rec = mk(tmp_path)
    monkeypatch.setattr(inst, "_HAS_PSUTIL", False)
    st = inst.status_all()
    assert st["ok"] is True and st["available"] is False
    assert st["instances"][0]["id"] == rec["id"] and st["instances"][0]["state"] == "stopped"
    assert inst.start(rec["id"])["reason"] == "no-psutil"


def test_status_all_returns_fresh_top_level_dict(world, tmp_path, monkeypatch):
    mk(tmp_path)
    monkeypatch.setattr(inst, "_STATUS_TTL", 60.0)
    a = inst.status_all(18765)
    a["local"] = False                                  # serve 往顶层塞东西不能污染共享缓存帧
    assert "local" not in inst.status_all(18765)


def test_view_carries_stop_grace_note_owner_and_boot_shape(world, tmp_path):
    rec = mk(tmp_path, stop_grace_s=45)
    s = status_of(rec["id"])
    assert s["stop_grace_s"] == 45 and s["note"] is None               # 页面的停止确认要说真话 (c20)
    st = inst.status_all(18765)
    assert set(st["boot"]) == {"handled", "pending"}
    assert st["owner"] == {"is_owner": True, "pid": os.getpid(), "port": None}


# ---------------------------------------------------------------- 状态照实 (修正轮)

def test_other_instances_tracked_process_is_never_mine(world, tmp_path, monkeypatch):
    # 同一目录、同一端口登记了两个变体 (比如 blue / green): tokmon 起了 blue, green 绝不能因为目录对得上就认领它 (c6)
    fake_launcher(world, monkeypatch)
    blue = mk(tmp_path, "svc-blue", ports=[18431], command="python app.py --variant blue")
    green = mk(tmp_path, "svc-green", ports=[18431], cwd=blue["cwd"], group="backup",
              command="python app.py --variant green")
    inst.start(blue["id"])
    inst._tick()
    assert status_of(blue["id"])["state"] == "running"
    s = status_of(green["id"])
    assert s["state"] == "port_busy" and s["started_by"] is None and s["pid"] is None
    assert not [x for x in s["listening"] if x["mine"]]
    # 占着端口的是另一个登记实例: 冲突里点名是哪个 (by), 启动回执也说实例名, 不只报一个进程名
    assert {"kind": "port-busy", "port": 18431, "owner": {"pid": 101, "name": "python.exe"},
            "by": blue["id"]} in s["conflicts"]
    r = inst.start(green["id"])
    assert r["reason"] == "port-busy" and r["detail"] == "端口 18431 被实例「svc-blue」占用"   # 不是 already-running
    assert r["by"] == blue["id"]                   # 回执也带 by: 页面先认它, 卡片上的冲突还没刷新也能说出实例名
    assert inst.stop(green["id"])["reason"] == "not-running"
    assert world.stops == [] and {100, 101} <= set(world.procs)          # blue 毫发无伤


def test_untracked_owner_matching_two_instances_tiebreaks_on_cmdline(world, tmp_path):
    blue = mk(tmp_path, "svc-blue", ports=[18431], command="run_blue.bat")
    green = mk(tmp_path, "svc-green", ports=[18431], cwd=blue["cwd"], command="run_green.bat")
    world.add(499, "cmd.exe", cwd=blue["cwd"], cmdline=["C:\\Windows\\system32\\cmd.exe", "/c", "run_green.bat"])
    world.add(500, "python.exe", cwd=blue["cwd"], parent=499, cmdline=["C:/py/python.exe", "app.py"])
    world.listen = [(18431, 500, "127.0.0.1")]
    # owner 自己的命令行分不出, 但它上面 .bat 的 cmd 祖先说了是 green
    assert status_of(green["id"])["state"] == "running" and status_of(green["id"])["started_by"] == "external"
    p = status_of(blue["id"])
    assert p["state"] == "port_busy" and [c["kind"] for c in p["conflicts"] if c["kind"] != "shared-port"] == ["port-busy"]



def test_tiebreak_looks_past_an_owner_that_carries_no_evidence(world, tmp_path):
    # 第三轮复查: 两个变体特有词个数不等 (run_a.bat vs run_b.bat --safe), 进程自己是 `python app.py` —— 两边都不沾。
    # 旧逻辑在这层按「缺几个词」就定了, 结果谁都判不了; 该往上看: 父进程 cmd /c run_a.bat 说了是 a。
    a = mk(tmp_path, "svc-a", ports=[18431], command="run_a.bat")
    b = mk(tmp_path, "svc-b", ports=[18431], cwd=a["cwd"], command="run_b.bat --safe")
    world.add(499, "cmd.exe", cwd=a["cwd"], cmdline=["cmd.exe", "/c", "run_a.bat"])
    world.add(500, "python.exe", cwd=a["cwd"], parent=499, cmdline=["C:/py/python.exe", "app.py"])
    world.listen = [(18431, 500, "127.0.0.1")]
    assert status_of(a["id"])["state"] == "running"
    assert status_of(b["id"])["state"] == "port_busy"

_SUBSET = "python -m http.server 18431 --bind 127.0.0.1"


def _subset_variants(tmp_path):
    """一对变体: full 的命令 = base 的命令 + 两个特有词 (base 自己没有特有词)。"""
    base = mk(tmp_path, "svc-base", ports=[18431], command=_SUBSET)
    full = mk(tmp_path, "svc-full", ports=[18431], cwd=base["cwd"], group="backup", command=_SUBSET + " --directory .")
    return base, full


def test_tiebreak_never_lets_a_distant_ancestor_pick_the_superset_variant(world, tmp_path):
    # 复现: 外部按 base 的命令起了它; 3、4 层祖先是包装脚本 (终端 / IDE / bash -c), 命令行里恰好带着 full 的词。
    # 旧实现把 5 层命令行混在一起打分, full 2 : base 0 -> full 显示「运行中」, 点 full 的停止杀掉的是 base 的进程 (c6)
    base, full = _subset_variants(tmp_path)
    world.add(496, "bash.exe", cwd=base["cwd"], cmdline=["bash", "-c", "source env && eval 'python probe.py'"])
    world.add(497, "bash.exe", cwd=base["cwd"], parent=496, cmdline=["bash", "-c", "python probe.py --directory ."])
    world.add(498, "python.exe", cwd=base["cwd"], parent=497, cmdline=["python", "probe.py", "--directory", "."])
    world.add(499, "python.exe", cwd=base["cwd"], parent=498, cmdline=_SUBSET.split())
    world.listen = [(18431, 499, "127.0.0.1")]
    for me in (base, full):
        s = status_of(me["id"])
        assert s["state"] == "port_busy" and s["started_by"] is None, me["id"]   # 它自己的命令行缺 full 的词: 到此为止, 认不准
        assert [c["kind"] for c in s["conflicts"] if c["kind"] != "shared-port"] == ["ambiguous"]
    assert inst.stop(full["id"])["reason"] == "ambiguous"
    assert world.stops == [] and 499 in world.procs


def test_tiebreak_superset_variant_is_recognized_only_by_nearby_command_lines(world, tmp_path):
    base, full = _subset_variants(tmp_path)
    world.add(498, "cmd.exe", cwd=base["cwd"], cmdline=["cmd.exe", "/c", "start.bat"])
    world.add(499, "python.exe", cwd=base["cwd"], parent=498, cmdline=(_SUBSET + " --directory .").split())
    world.listen = [(18431, 499, "127.0.0.1")]
    assert status_of(full["id"])["state"] == "running"                   # 它自己的命令行带着 full 的特有词: 0 比正, 近处可判
    assert status_of(base["id"])["state"] == "port_busy"
    # 自己和父进程的命令行都读不到, 只有更远的祖先带着 full 的词: 远处的 0 比正不作数 -> 认不准
    world.procs[499]["cmdline"] = None
    world.procs[498]["cmdline"] = None
    world.add(497, "bash.exe", cwd=base["cwd"], cmdline=["bash", "-c", "python -m http.server --directory ."])
    world.procs[498]["parent"] = 497
    for me in (base, full):
        s = status_of(me["id"])
        assert s["state"] == "port_busy" and [c["kind"] for c in s["conflicts"] if c["kind"] != "shared-port"] == \
            ["ambiguous"], me["id"]


def test_ambiguous_owner_is_nobodys_and_is_never_stopped(world, tmp_path):
    a = mk(tmp_path, "twin-a", ports=[18431], command="python app.py")
    b = mk(tmp_path, "twin-b", ports=[18431], cwd=a["cwd"], command="python app.py", env={"MODE": "b"})
    world.add(500, "python.exe", cwd=a["cwd"], cmdline=["python.exe", "app.py"])
    world.listen = [(18431, 500, "127.0.0.1")]
    for me, other in ((a, b), (b, a)):
        s = status_of(me["id"])
        assert s["state"] == "port_busy" and s["started_by"] is None      # 认不准: 谁都不显示在跑
        assert {"kind": "ambiguous", "port": 18431, "owner": {"pid": 500, "name": "python.exe"},
                "with": [other["id"]]} in s["conflicts"]
        for fn in (inst.stop, inst.restart, inst.start):
            assert fn(me["id"])["reason"] == "ambiguous", fn
    assert world.stops == [] and 500 in world.procs


def test_tracked_alive_but_port_silent_is_degraded_not_running(world, tmp_path, monkeypatch):
    # watcher 类: 应用崩了、监听关了, watcher 还活着 -> 以前一直显示「运行中」、启动被 already-running 挡住 (c8)
    launched = fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    inst.start(rec["id"])
    inst._tick()
    world.listen = []
    s = status_of(rec["id"])
    assert s["state"] == "degraded" and s["state_label"] == "进程在 · 端口没在监听"
    assert s["started_by"] == "tokmon" and s["pid"] == 100 and s["can_control"] is True
    r = inst.start(rec["id"])
    assert r["reason"] == "degraded" and "重启" in r["detail"]
    assert inst.start_group("core")["skipped"] == [{"id": rec["id"], "reason": "degraded"}]
    world.add(777, "node.exe", cwd="C:/Users/u/other-proj")
    world.listen = [(18080, 777, "127.0.0.1")]
    assert status_of(rec["id"])["state"] == "port_busy"                  # 期间别人占了主端口: 如实说被占
    world.listen = []
    assert inst.restart(rec["id"])["ok"]                                  # 重启照常: 先停再起
    inst._join_workers(5)
    assert world.stops[0]["pids"] == [100, 101] and len(launched) == 2


def test_proxied_listener_counts_as_serving_and_is_never_killed(world, tmp_path, monkeypatch):
    # docker compose / WSL: 端口记在代理进程名下, 不在实例树里、目录也对不上 (c9)
    fake_launcher(world, monkeypatch, listen=False)
    rec = mk(tmp_path, ports=[18438], command="docker compose up db")
    inst.start(rec["id"])
    assert json.loads(inst.STATE.read_text(encoding="utf-8"))["tracked"][rec["id"]]["free_at_launch"] == [18438]
    world.add(880, "com.docker.backend.exe", cwd="C:/Program Files/Docker")
    world.listen = [(18438, 880, "0.0.0.0")]
    inst._tick()
    s = status_of(rec["id"])
    assert s["state"] == "running" and s["started_by"] == "tokmon"
    assert s["note"] == "端口由 com.docker.backend.exe 代理"
    assert s["listening"] == [{"port": 18438, "pid": 880, "name": "com.docker.backend.exe", "mine": True}]
    world.real_stop(monkeypatch, lambda plan: False)                      # 连补扫一起走真实停止逻辑
    assert inst.stop(rec["id"])["ok"]
    inst._join_workers(5)
    assert 880 in world.procs and not {100, 101} & set(world.procs)      # 只停它自己的树, 代理绝不碰
    assert status_of(rec["id"])["state"] == "port_busy"                   # 代理还在听: 此后照实是被占


def test_start_timeout_with_unrecognized_listener_is_degraded_not_killed(world, tmp_path, monkeypatch, home):
    fake_launcher(world, monkeypatch, listen=False)
    rec = mk(tmp_path, start_timeout_s=5)
    inst.start(rec["id"])
    world.add(881, "relay.exe", cwd="C:/Users/u/elsewhere")              # 拉起后才出现在空端口上、认不出是谁
    world.listen = [(18080, 881, "127.0.0.1")]
    inst._tick()
    assert status_of(rec["id"])["state"] == "starting"
    inst._TRACK[rec["id"]]["deadline"] = time.time() - 1
    inst._tick()
    inst._join_workers(5)
    assert world.stops == [] and {100, 101, 881} <= set(world.procs)     # 端口其实在听: 不收尸
    s = status_of(rec["id"])
    assert s["state"] == "degraded" and s["state_label"] == "进程在 · 端口持有者认不出"
    assert s["last_error"] == "端口在监听，但持有者 relay.exe 认不出是它"
    assert ("instance-start-timeout", rec["name"], "unrecognized-owner relay.exe") in home
    assert json.loads(inst.STATE.read_text(encoding="utf-8"))["tracked"][rec["id"]]["unrecognized"] is True


def test_scan_failure_is_unknown_and_never_kills_or_reports_crash(world, tmp_path, monkeypatch):
    # 端口全表读不到 (macOS 非 root / 权限): 以前当成「没人在听」, 健康的实例到期被收尸 (c10)
    monkeypatch.setattr(events, "_ALLOWED_PAYLOAD", set(events._ALLOWED_PAYLOAD) | {"instance", "exit_code"})
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path, start_timeout_s=5)
    ext = mk(tmp_path, "ext-svc", ports=[18090])
    world.add(600, "python.exe", cwd=ext["cwd"])
    world.listen = [(18090, 600, "127.0.0.1")]
    world.scan_ok = False
    inst.start(rec["id"])
    emitted: list = []
    inst._tick(emit=emitted.append)
    assert status_of(rec["id"])["state"] == "running"                     # 退路: 逐个问自己追踪的树
    world.listen = [x for x in world.listen if x[1] != 101]               # 它不听了, 但全表仍读不到
    s = status_of(rec["id"])
    assert s["state"] == "unknown" and s["state_label"] == "状态未知（读不到端口表）"
    assert inst.start(rec["id"])["reason"] == "unknown"
    assert status_of(ext["id"])["state"] == "unknown"                     # 外部实例: 不说「未运行」
    assert inst.start_group("core")["skipped"] == [{"id": rec["id"], "reason": "unknown"},
                                                   {"id": ext["id"], "reason": "unknown"}]
    # 启动中碰上读不到: 到期也不杀, 期限往后推
    slow = mk(tmp_path, "slow-svc", ports=[18092], start_timeout_s=5)
    fake_launcher(world, monkeypatch, listen=False)
    inst.start(slow["id"], force=True)
    inst._TRACK[slow["id"]]["deadline"] = time.time() - 1
    inst._tick(emit=emitted.append)
    inst._join_workers(5)
    assert world.stops == [] and status_of(slow["id"])["state"] == "starting"
    assert inst._TRACK[slow["id"]]["deadline"] > time.time()
    assert emitted == []


def test_deps_ready_when_scan_unknown_via_loopback_probe_or_tracked_tree(world, tmp_path, monkeypatch):
    # 端口全表读不到时, 以前依赖一律「没就绪」: 页面上运行中的依赖打 ✗、普通启动被拒、
    # 等依赖 15 分钟后失败、重启把健康的实例停了就不再起 (c10)
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_DEPS_POLL_S", 0.05)
    db = mk(tmp_path, "demo-db", ports=[18888])
    web = mk(tmp_path, "demo-web", ports=[18080],
             deps=[{"name": "依赖A", "port": 18888}, {"name": "依赖B", "port": 18889}])
    world.scan_ok = False
    assert inst.start(db["id"])["ok"]
    inst._tick()
    assert status_of(db["id"])["state"] == "running"                     # tokmon 拉起的依赖: 问它自己的树
    world.probe_up = {18889}                                             # 外部起的依赖: 回环试连连得上
    s = status_of(web["id"])
    assert [d["ready"] for d in s["deps"]] == [True, True] and s["deps_ready"] is True
    assert inst.start(web["id"])["state"] == "starting"                   # 不再被「依赖未就绪」挡住
    inst._tick()
    assert inst.restart(web["id"])["ok"]                                  # 重启: 停完照常再起, 不卡在「等依赖」
    inst._join_workers(5)
    assert [x[0] for x in launched] == [db["id"], web["id"], web["id"]]
    world.probe_up = set()
    assert [d["ready"] for d in status_of(web["id"])["deps"]] == [True, False]   # 真没人听的照实说没就绪


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_real_loopback_probe():
    port = _free_port()
    assert inst._loopback_listening(port) is False
    inst._PROBE_CACHE.clear()                                   # 结果缓存 15 秒 (别每轮询都握手); 这里要看真变化
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        assert inst._loopback_listening(port) is True
    finally:
        s.close()


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_real_observation_fallbacks(monkeypatch):
    import psutil
    s = socket.socket()
    try:
        port = _free_port()
        s.bind(("127.0.0.1", port))
        s.listen(1)
        assert port in inst._proc_listen_ports(os.getpid())              # 全表读不到时逐个问自己的进程
    finally:
        s.close()
    assert inst._cmdline(os.getpid()) and inst._cmdline(2 ** 22 + 7) == []

    def denied(kind="inet"):
        raise psutil.AccessDenied(0)
    monkeypatch.setattr(inst.psutil, "net_connections", denied)
    assert inst._scan_listeners() is None                                # 读不到 ≠ 没人在听


def test_crash_record_superseded_by_later_run_stays_gone_after_restart(world, tmp_path, monkeypatch):
    monkeypatch.setattr(events, "_ALLOWED_PAYLOAD", set(events._ALLOWED_PAYLOAD) | {"instance", "exit_code"})
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    inst.start(rec["id"])
    inst._tick()
    world.kill([100, 101], code=3)
    inst._tick()
    assert status_of(rec["id"])["state"] == "crashed"
    world.add(4242, "python.exe", cwd=rec["cwd"])                         # 你后来从终端按老办法起了它
    world.listen = [(18080, 4242, "127.0.0.1")]
    inst._tick()                                                          # 页面没开也行: 监督线程就会落盘
    assert rec["id"] not in json.loads(inst.STATE.read_text(encoding="utf-8"))["last"]
    world.kill([4242])                                                    # 终端关了
    restart_tokmon()
    assert status_of(rec["id"])["state"] == "stopped"                     # 旧的「意外退出」不再冒出来 (c11)


def test_status_build_racing_an_action_is_not_cached(world, tmp_path, monkeypatch):
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    monkeypatch.setattr(inst, "_STATUS_TTL", 60.0)
    entered, release, got = threading.Event(), threading.Event(), {}

    def slow_scan():
        snap = list(world.listen)                                         # 端口表是点启动之前读到的
        if threading.current_thread().name == "poller":                   # 另一个标签页的轮询: 扫描慢
            entered.set()
            release.wait(5)
        return snap

    monkeypatch.setattr(inst, "_scan_listeners", slow_scan)
    t = threading.Thread(target=lambda: got.update(s=inst.status_all()), name="poller")
    t.start()
    assert entered.wait(5)
    assert inst.start(rec["id"])["state"] == "starting"                    # 构建进行中点了启动
    release.set()
    t.join(5)
    assert [x["state"] for x in got["s"]["instances"]] == ["stopped"]      # 那一帧是点之前的 ...
    assert status_of(rec["id"])["state"] == "starting"                     # ... 但没进缓存 (c12)


def test_self_without_declared_ports_uses_serve_port_for_tunnel_and_link(world, monkeypatch):
    selfrec = inst.save_instance({"name": "tokmon", "kind": "self"})["instance"]
    world.cf = [(900, ["cloudflared", "tunnel", "--url", "http://localhost:18765", "--metrics", "127.0.0.1:18766"])]
    monkeypatch.setattr(inst, "_http_get_json", lambda *a, **k: {"hostname": "demo-self.trycloudflare.com"})
    s = status_of(selfrec["id"], 18765)
    assert s["tunnel"] == {"url": "https://demo-self.trycloudflare.com", "kind": "quick", "pid": 900}   # c13
    assert s["open_url"] == "http://localhost:18765/" and s["ports"] == []


def test_portless_service_label_admits_external_runs_are_invisible(world, tmp_path, monkeypatch):
    monkeypatch.setattr(events, "_ALLOWED_PAYLOAD", set(events._ALLOWED_PAYLOAD) | {"instance", "exit_code"})
    launched = fake_launcher(world, monkeypatch)
    rec = mk(tmp_path, "demo-worker", ports=[], command="python worker.py")
    s = status_of(rec["id"])
    assert s["state"] == "stopped" and s["state_label"] == "未运行（没有端口，外部启动的认不出）"   # s1
    # 认不出它是不是已在别处跑着: 单点启动 / 重启先问你, 你确认 (force) 了才起
    confirm = {"ok": False, "reason": "portless-confirm", "detail": "没有端口，认不出它是不是已经在别处跑着；确定要再起一份吗？",
               "owner": None, "deps": None}
    assert inst.start(rec["id"]) == confirm
    assert inst.restart(rec["id"]) == confirm
    assert launched == []
    assert inst.start(rec["id"], force=True)["state"] == "starting"
    inst._TRACK[rec["id"]]["started_at"] -= 10
    inst._tick()
    s = status_of(rec["id"])
    assert s["state"] == "running" and s["state_label"] == "运行中"
    assert inst.restart(rec["id"])["ok"]                                  # tokmon 追踪着的那份: 停了再起同一份, 不用再问
    inst._join_workers(5)
    assert len(launched) == 2
    inst._TRACK[rec["id"]]["started_at"] -= 10
    inst._tick()
    world.kill([110, 111], code=1)
    inst._tick(emit=lambda ev: None)
    s = status_of(rec["id"])                                              # 崩了之后你可能从别处又起了它: 同样别说得太死
    assert s["state"] == "crashed" and s["state_label"] == "意外退出（没有端口，外部启动的认不出）"


def test_portless_service_is_never_boot_auto_or_batch_started(world, tmp_path, monkeypatch, home):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_BOOT_DELAY_S", 0.0)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.0)
    monkeypatch.setattr(inst, "_EMIT", lambda ev: None)
    d = tmp_path / "work" / "demo-worker"
    d.mkdir(parents=True)
    assert inst.save_instance({"name": "demo-worker", "cwd": str(d), "command": "python worker.py", "boot": "auto"}) == \
        {"ok": False, "reason": "invalid", "errors": {"boot": "portless-auto"}}
    w = mk(tmp_path, "demo-worker", ports=[], command="python worker.py", boot="manual")
    assert inst.patch_instance(w["id"], {"boot": "auto"})["errors"] == {"boot": "portless-auto"}
    assert inst.patch_instance(w["id"], {"boot": "ask"})["errors"] == {"boot": "portless-auto"}   # 提醒了却批量起不了: 也不许
    # 旧清单里残留的「无端口 + auto」: 读盘不判死 (不丢你的记录), 开机拉起跳过并审计一笔
    doc = json.loads(inst.MANIFEST.read_text(encoding="utf-8"))
    doc["instances"].append(dict(doc["instances"][0], id="old-worker", name="old-worker", boot="auto"))
    doc["instances"].append(dict(doc["instances"][0], id="old-asker", name="old-asker", boot="ask"))
    inst.MANIFEST.write_text(json.dumps(doc), encoding="utf-8")
    assert inst.status_all()["manifest_error"] is None
    assert inst._boot_sequence() == "done"
    assert launched == []
    assert ("instance-boot-start", "old-worker", "portless") in home
    assert ("instance-boot-start", "old-asker", "portless") in home
    assert inst.status_all()["boot"]["pending"] == []                   # 旧记录里的「无端口 + 提醒」也不进提醒 (提醒了也批量起不了)
    r = inst.start_group("core")
    assert r["queued"] == [] and r["skipped"] == [{"id": w["id"], "reason": "portless"},
                                                  {"id": "old-worker", "reason": "portless"},
                                                  {"id": "old-asker", "reason": "portless"}]
    inst._join_workers(5)
    assert launched == []


# ---------------------------------------------------------------- 单一主人 (c7 / c15)

_HOLDER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from tokmon import instances as i
i.set_home(sys.argv[2])
print("owner" if i.claim_owner(int(sys.argv[3])) else "busy", flush=True)
sys.stdin.readline()
"""


@contextlib.contextmanager
def other_tokmon(tmp_path, port=18765):
    """另起一个真的 python 进程当「另一个 tokmon」: 用同一个 home 抢主人锁, 拿着不放, 直到关它的 stdin。"""
    import subprocess
    fake_home = tmp_path / "holder-userhome"
    env = dict(os.environ, USERPROFILE=str(fake_home), HOME=str(fake_home))
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, repo, str(inst._HOME), str(port)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, **kw)
    try:
        yield p, p.stdout.readline().decode("utf-8", "replace").strip()
    finally:
        try:
            p.stdin.close()
            p.wait(15)
        except Exception:
            p.kill()
            p.wait(5)


def test_claim_owner_is_idempotent_and_publishes_card(home):
    assert inst.claim_owner(18765) and inst.claim_owner() and inst.claim_owner(18765)
    card = json.loads((inst._HOME / "owner.json").read_text(encoding="utf-8"))
    assert card["pid"] == os.getpid() and card["port"] == 18765
    assert inst.status_all()["owner"] == {"is_owner": True, "pid": os.getpid(), "port": 18765}
    inst._release_owner()
    assert not (inst._HOME / "owner.json").exists()
    assert inst.status_all()["owner"] == {"is_owner": False, "pid": None, "port": None}
    assert inst.save_instance({"name": "x"})["reason"] == "not-owner"     # 没接管 = 不接受变更


def test_second_tokmon_is_read_only_and_never_advances_state(world, tmp_path, monkeypatch):
    monkeypatch.setattr(events, "_ALLOWED_PAYLOAD", set(events._ALLOWED_PAYLOAD) | {"instance", "exit_code"})
    rec = mk(tmp_path)
    selfrec = inst.save_instance({"name": "tokmon", "kind": "self", "ports": [18765]})["instance"]
    world.add(300, "cmd.exe", cwd=rec["cwd"], ct=7000.0)
    world.add(301, "python.exe", cwd="C:/Users/u/elsewhere", ct=7000.5, parent=300)
    world.listen = [(18080, 301, "127.0.0.1")]
    inst._reset_for_tests()                            # 本进程放掉锁, 让「另一个 tokmon」先拿
    with other_tokmon(tmp_path) as (holder, said):
        assert said == "owner"
        world.add(holder.pid, "python.exe", cwd="C:/Users/u/tokmon")
        world.listen.append((18765, holder.pid, "127.0.0.1"))
        me = status_of(selfrec["id"], 18999)                              # 本服务卡: 主人那个 tokmon 在听它的端口不是冲突
        assert me["state"] == "self" and me["pid"] == holder.pid and me["conflicts"] == []
        emitted: list = []
        inst.start_supervisor(boot=True, serve_port=18999, emit=emitted.append)
        assert inst._SUP_THREAD is None and inst._BOOT_THREAD is None     # 抢不到锁: 不监督、不做开机拉起
        assert inst._CLAIM_THREAD is not None                             # ... 但定期再抢: 主人退出后由这边接管
        st = inst.status_all(18999)
        assert st["owner"] == {"is_owner": False, "pid": holder.pid, "port": 18765}
        want = f"另一个 tokmon（pid {holder.pid} · 端口 18765）在管理实例，请在它的页面操作"
        for r in (inst.start(rec["id"]), inst.stop(rec["id"]), inst.restart(rec["id"]),
                  inst.start_group("core"), inst.boot_ack("dismiss"), inst.save_instance({"name": "x"}),
                  inst.patch_instance(rec["id"], {"boot": "ask"}), inst.delete_instance(rec["id"])):
            assert r["ok"] is False and r["reason"] == "not-owner" and r["detail"] == want, r
        assert inst.get_instance(rec["id"])["id"] == rec["id"]            # 只读的照常
        assert inst.log_tail(rec["id"])["ok"]
        # 主人写的 state.json: 非主人照着显示 (认得出主人拉起的实例), 且一改就重读
        tracked = {"pid": 300, "create_time": 7000.0, "started_at": 7000.0, "phase": "running"}
        gone = {"pid": 310, "create_time": 7100.0, "started_at": 7100.0, "phase": "running"}
        inst.STATE.write_text(json.dumps({"tracked": {rec["id"]: tracked, "gone-svc": gone}}), encoding="utf-8")
        s = status_of(rec["id"])
        assert s["state"] == "running" and s["started_by"] == "tokmon"
        inst.STATE.write_text(json.dumps({"tracked": {rec["id"]: dict(tracked, phase="stopping"), "gone-svc": gone}}),
                              encoding="utf-8")
        assert status_of(rec["id"])["state"] == "stopping"
        # 主人那边停掉了它: 非主人既不当崩溃报, 也绝不改写主人的状态文件
        before = inst.STATE.read_bytes()
        world.kill([300, 301], code=0)
        with inst._LOCK:                               # 纵深: 就算非主人内存里有追踪条目, 一拍也不推进、不报崩溃
            inst._TRACK[rec["id"]] = {"popen": None, "pid": 300, "create_time": 7000.0, "started_at": 7000.0,
                                      "phase": "running", "stop_requested": False, "deadline": 0.0}
        inst._tick(emit=emitted.append)
        status_of(rec["id"])
        with inst._LOCK:
            inst._state()["boot_pending"] = ["x"]
            inst._save_state()                         # 非主人写状态是空操作
            inst._TRACK.clear()
        assert emitted == [] and inst.STATE.read_bytes() == before
    # 那个 tokmon 退出了: 锁随进程释放, 这里可以接管
    assert inst.claim_owner()
    assert inst.status_all()["owner"]["is_owner"] is True


def test_non_owner_takes_over_once_the_owner_tokmon_exits(world, tmp_path, monkeypatch):
    # 以前只在启动时抢一次锁: 主人退出后这边永远只读, 页面还说「另一个 tokmon 在管」, 没有谁在监督 (R4)
    monkeypatch.setattr(inst, "_CLAIM_RETRY_S", 0.1)
    inst._reset_for_tests()
    with other_tokmon(tmp_path) as (_holder, said):
        assert said == "owner"
        inst.start_supervisor(boot=True, serve_port=18999)
        time.sleep(0.4)
        assert inst.status_all()["owner"]["is_owner"] is False and inst._SUP_THREAD is None   # 主人还在: 抢不到
    assert _wait(lambda: inst.status_all()["owner"]["is_owner"], 10)
    assert inst._SUP_THREAD is not None and inst._SUP_THREAD.is_alive()   # 接手监督 ...
    assert inst._BOOT_THREAD is None                                      # ... 但不补做开机拉起
    card = json.loads((inst._HOME / "owner.json").read_text(encoding="utf-8"))
    assert card["pid"] == os.getpid() and card["port"] == 18999
    assert inst.save_instance({"name": "x"})["reason"] == "invalid"       # 变更不再被 not-owner 挡住


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_owner_card_of_an_exited_tokmon_reads_stale(home, monkeypatch):
    import psutil
    inst._release_owner()
    card = inst._HOME / "owner.json"
    card.write_text(json.dumps({"pid": 4321, "port": 18765, "since": time.time()}), encoding="utf-8")
    monkeypatch.setattr(inst, "_pid_exists", lambda pid: False)
    assert inst.status_all()["owner"] == {"is_owner": False, "pid": None, "port": None, "stale": True}
    r = inst.save_instance({"name": "x"})
    assert r["reason"] == "not-owner" and "已退出" in r["detail"]         # 不再说「另一个 tokmon 在管」
    # pid 被复用: 进程在, 但创建得比名片上的接管时刻还晚 -> 写名片的那个早没了
    parent = psutil.Process().parent()
    if parent is not None:
        monkeypatch.setattr(inst, "_pid_exists", lambda pid: True)
        card.write_text(json.dumps({"pid": parent.pid, "port": 18765, "since": parent.create_time() - 100}),
                        encoding="utf-8")
        assert inst.status_all()["owner"]["stale"] is True
        card.write_text(json.dumps({"pid": parent.pid, "port": 18765, "since": parent.create_time() + 5}),
                        encoding="utf-8")
        assert inst.status_all()["owner"] == {"is_owner": False, "pid": parent.pid, "port": 18765}


# ---------------------------------------------------------------- 启动 / 监督

def test_start_health_check_then_running_by_tokmon(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    assert inst.start("nope")["reason"] == "not-found"
    r = inst.start(rec["id"])
    assert r == {"ok": True, "state": "starting", "pid": 100}
    assert launched == [(rec["id"], "user")]
    assert status_of(rec["id"])["state"] == "starting"
    assert inst.start(rec["id"])["reason"] == "busy"
    inst._tick()
    s = status_of(rec["id"])
    assert s["state"] == "running" and s["started_by"] == "tokmon" and s["pid"] == 101
    assert inst.start(rec["id"])["reason"] == "already-running"
    tracked = json.loads(inst.STATE.read_text(encoding="utf-8"))["tracked"]
    assert tracked[rec["id"]]["pid"] == 100


def test_start_exits_during_starting_is_failed(world, tmp_path, monkeypatch):
    fake_launcher(world, monkeypatch, listen=False)
    rec = mk(tmp_path)
    inst.start(rec["id"])
    world.kill([100, 101], code=3)
    inst._tick()
    s = status_of(rec["id"])
    assert s["state"] == "failed" and s["exit_code"] == 3 and s["state_label"] == "启动失败"


def test_start_timeout_kills_tree_and_fails(world, tmp_path, monkeypatch, home):
    fake_launcher(world, monkeypatch, listen=False)
    rec = mk(tmp_path, start_timeout_s=5)
    inst.start(rec["id"])
    inst._tick()
    assert status_of(rec["id"])["state"] == "starting"
    inst._TRACK[rec["id"]]["deadline"] = time.time() - 1
    inst._tick()
    inst._join_workers(5)
    s = status_of(rec["id"])
    assert s["state"] == "failed" and "启动超时" in s["last_error"]
    assert world.stops[-1]["pids"] == [100, 101] and world.stops[-1]["grace"] == 0
    assert ("instance-start-timeout", rec["name"], "forced") in home


def test_launch_failure_reported(world, tmp_path, monkeypatch):
    def boom(rec, actor):
        raise OSError("no such program")
    monkeypatch.setattr(inst, "_launch", boom)
    rec = mk(tmp_path)
    r = inst.start(rec["id"])
    assert r["ok"] is False and r["reason"] == "launch-failed" and "no such program" in r["detail"]
    assert status_of(rec["id"])["state"] == "failed"


def test_child_env_drops_inherited_session_vars_but_keeps_explicit(monkeypatch):
    # tokmon 若是在 Claude Code 会话里起的: 会话变量不漏进长期服务, 也别让 cmd 不在 cwd 里找 run.bat
    for k, v in {"NoDefaultCurrentDirectoryInExePath": "1", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s-1",
                 "CLAUDE_CODE_ENTRYPOINT": "cli", "MC_REMOTE": "1", "MC_REMOTE_HOSTS": "demo.example",
                 "DEMO_KEEP": "yes"}.items():
        monkeypatch.setenv(k, v)
    env = inst._child_env({"env": {"CLAUDE_CODE_ENTRYPOINT": "mine"}})
    up = {k.upper(): v for k, v in env.items()}
    for k in ("NODEFAULTCURRENTDIRECTORYINEXEPATH", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "MC_REMOTE",
              "MC_REMOTE_HOSTS"):
        assert k not in up, k
    assert up["CLAUDE_CODE_ENTRYPOINT"] == "mine"                   # 实例自己显式写的照样生效
    assert up["DEMO_KEEP"] == "yes" and up["PYTHONUNBUFFERED"] == "1" and "PATH" in up


def test_force_start_and_wait_deps(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_DEPS_POLL_S", 0.05)
    a = mk(tmp_path, "dep-a", ports=[18080], deps=[{"name": "auth-svc", "port": 18888}])
    b = mk(tmp_path, "dep-b", ports=[18090], deps=[{"name": "auth-svc", "port": 18888}])
    assert inst.start(a["id"], force=True)["state"] == "starting"             # 你确认了 -> 不等依赖
    r = inst.start(b["id"], wait_deps=True)
    assert r == {"ok": True, "state": "waiting_deps", "pid": None}
    assert status_of(b["id"])["state"] == "waiting_deps"
    time.sleep(0.2)
    assert [x[0] for x in launched] == [a["id"]]                              # 依赖没好 -> 不拉
    world.add(60, "dep-a.exe", cwd="C:/Users/u/apps")
    world.listen.append((18888, 60, "127.0.0.1"))
    deadline = time.time() + 5
    while time.time() < deadline and len(launched) < 2:
        time.sleep(0.05)
    assert [x[0] for x in launched] == [a["id"], b["id"]]
    assert status_of(b["id"])["state"] == "starting"


def test_stop_cancels_waiting_deps(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_DEPS_POLL_S", 0.05)
    rec = mk(tmp_path, deps=[{"name": "auth-svc", "port": 18888}])
    inst.start(rec["id"], wait_deps=True)
    assert inst.stop(rec["id"])["ok"] is True
    world.listen.append((18888, 60, "127.0.0.1"))
    time.sleep(0.3)
    assert launched == [] and status_of(rec["id"])["state"] == "stopped"


def test_crash_emits_process_crashed_once(world, tmp_path, monkeypatch):
    monkeypatch.setattr(events, "_ALLOWED_PAYLOAD", set(events._ALLOWED_PAYLOAD) | {"instance", "exit_code"})
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path, project="demo")
    emitted: list = []
    inst.start(rec["id"])
    inst._tick(emit=emitted.append)
    started_at = inst._TRACK[rec["id"]]["started_at"]
    assert status_of(rec["id"])["state"] == "running"
    world.kill([100, 101], code=1)
    inst._tick(emit=emitted.append)
    inst._tick(emit=emitted.append)
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev.type == "PROCESS_CRASHED" and ev.pillar == "instances" and ev.severity == "warning"
    assert ev.project == "demo" and ev.dedup_key == f"PROCESS_CRASHED:{rec['id']}:{started_at}"
    assert ev.payload["instance"] == rec["name"] and ev.payload["exit_code"] == 1
    assert ev.payload["state_label"] == "意外退出"
    s = status_of(rec["id"])
    assert s["state"] == "crashed" and s["exit_code"] == 1 and s["state_label"] == "意外退出"
    assert inst._TRACK == {}                                                   # 绝不自动重启


def test_requested_stop_is_not_a_crash(world, tmp_path, monkeypatch, home):
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    emitted: list = []
    inst.start(rec["id"])
    inst._tick(emit=emitted.append)
    world.gate = threading.Event()
    r = inst.stop(rec["id"])
    assert r == {"ok": True, "state": "stopping"}
    assert status_of(rec["id"])["state"] == "stopping"
    assert inst.stop(rec["id"])["reason"] == "busy"
    world.kill([100, 101], code=0)                     # 停止途中根进程先退了: 监督线程不能当成崩溃
    inst._tick(emit=emitted.append)
    world.gate.set()
    inst._join_workers(5)
    inst._tick(emit=emitted.append)
    assert emitted == []
    assert world.stops[0]["pids"] == [100, 101] and world.stops[0]["plan"]["mode"] == "tokmon"
    assert world.stops[0]["plan"]["attach"][0] == 100                     # 先挂 cmd 根的控制台
    assert world.stops[0]["plan"]["targets"] == [101]                     # Ctrl+C 要送到的是端口 owner
    s = status_of(rec["id"])
    assert s["state"] == "stopped" and s["exit_code"] == 0
    assert ("instance-stopped", rec["name"], "graceful") in home
    assert rec["id"] not in json.loads(inst.STATE.read_text(encoding="utf-8"))["tracked"]


def test_stop_external_kills_owner_tree_never_ancestors(world, tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "_IS_WIN", True)
    rec = mk(tmp_path)
    world.add(498, "pwsh.exe", cwd=rec["cwd"])                  # 你的终端 shell: 绝不能碰
    world.add(499, "cmd.exe", cwd=rec["cwd"], parent=498)       # .bat 的 cmd: 可同控制台, 但不杀
    world.add(500, "python.exe", cwd=rec["cwd"], parent=499)
    world.add(501, "python.exe", cwd=rec["cwd"], parent=500)
    world.listen = [(18080, 500, "127.0.0.1")]
    assert status_of(rec["id"])["started_by"] == "external"
    assert inst.stop(rec["id"])["ok"]
    inst._join_workers(5)
    plan = world.stops[0]["plan"]
    assert world.stops[0]["pids"] == [500, 501] and plan["mode"] == "external"
    # 先挂 owner 自己的控制台, 再挂它上方的 .bat cmd; Ctrl+C 只在 owner 真在那个控制台上时才发
    assert plan["attach"][:2] == [500, 499] and plan["targets"] == [500]
    assert 499 in plan["allowed"]
    assert 498 not in plan["allowed"] and 498 not in plan["attach"] and 498 in world.procs and 499 in world.procs
    assert status_of(rec["id"])["state"] == "stopped"


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_ppid_rejects_reused_parent_pid(monkeypatch):
    import psutil
    parent = psutil.Process().parent()
    assert inst._ppid(os.getpid()) == (parent.pid if parent else None)
    assert inst._ppid(2 ** 22 + 7) is None

    class Reused:
        """原父进程早没了, 它的 pid 被一个更晚创建的 cmd.exe 复用: ppid() 还指着它, parent() 核对 create_time 后给 None。"""

        def __init__(self, pid):
            self.pid = pid

        def ppid(self):
            return 4242

        def parent(self):
            return None

    monkeypatch.setattr(inst.psutil, "Process", Reused)
    assert inst._ppid(500) is None                     # 绝不把无关的 cmd 当祖先、挂到它的控制台上发 Ctrl+C


class _P:
    """假 psutil.Process: 记录 kill 顺序。"""

    def __init__(self, pid, log, deny=False):
        self.pid, self.log, self.deny, self.alive = pid, log, deny, True

    def is_running(self):
        return self.alive

    def kill(self):
        if self.deny:
            raise inst.psutil.AccessDenied(self.pid)
        self.log.append(self.pid)
        self.alive = False


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_execute_stop_paths(monkeypatch):
    def wait_procs(procs, timeout=None):
        return [p for p in procs if not p.alive], [p for p in procs if p.alive]
    monkeypatch.setattr(inst.psutil, "wait_procs", wait_procs)

    # 优雅: 发出 Ctrl+C 后全都自己退了
    log: list = []
    procs = [_P(100, log), _P(101, log)]

    def sent(plan):
        for p in plan["procs"]:
            p.alive = False
        return True
    monkeypatch.setattr(inst, "_send_interrupt", sent)
    assert inst._execute_stop({"procs": procs, "root": 100}, 5) == "graceful" and log == []

    # 发不出去 (比如控制台上有你的 shell): 直接定点强杀, 先子后根
    monkeypatch.setattr(inst, "_send_interrupt", lambda plan: False)
    log = []
    procs = [_P(100, log), _P(101, log), _P(102, log)]
    assert inst._execute_stop({"procs": procs, "root": 100}, 5) == "forced"
    assert log[-1] == 100 and sorted(log) == [100, 101, 102]

    # grace=0 (启动超时收尸): 不发 Ctrl+C
    monkeypatch.setattr(inst, "_send_interrupt", lambda plan: pytest.fail("grace=0 不该先发 Ctrl+C"))
    assert inst._execute_stop({"procs": [_P(5, [])], "root": 5}, 0) == "forced"

    # 杀不动 (权限)
    assert inst._execute_stop({"procs": [_P(7, [], deny=True)], "root": None}, 0) == "access-denied"
    # 早就没了
    gone = _P(9, [])
    gone.alive = False
    assert inst._execute_stop({"procs": [gone], "root": 9}, 5) == "gone"


def test_forced_stop_catches_child_respawned_during_grace(world, tmp_path, monkeypatch, home):
    # .bat 守护循环: Ctrl+C 打死了 python, cmd 答不了「终止批处理 (Y/N)」(stdin 是空的) 又拉起一个新的 —— 快照里没有它
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    inst.start(rec["id"])
    inst._tick()
    world.respawn[100] = (18080, rec["cwd"])

    def ctrl_c(plan):
        world.kill([101])                               # python 收到 Ctrl+C 退了, cmd 立刻补一个 700
        return True

    world.real_stop(monkeypatch, ctrl_c)
    assert inst.stop(rec["id"])["ok"]
    inst._join_workers(5)
    assert world.spawned == [700]                       # 强杀前先挂起 cmd: 杀 700 时它没机会再补一个
    assert not {100, 700} & set(world.procs)
    assert not [x for x in world.listen if x[0] == 18080]
    last = json.loads(inst.STATE.read_text(encoding="utf-8"))["last"][rec["id"]]
    assert last["outcome"] == "forced" and last["error"] is None
    assert status_of(rec["id"])["state"] == "stopped"
    assert ("instance-stopped", rec["name"], "forced") in home


@pytest.mark.parametrize("delay,respawns", [
    (0, 4),     # 补拉的立刻在听: 快照强杀 1 次 + 补扫 3 轮, 每杀一个它就补一个
    (1, 1),     # 补拉的要过一次扫描才在听 (真实服务起来要时间): 旧实现杀完立刻看一眼、看不到就报「停好了」(c1)
])
def test_stop_reports_incomplete_when_outside_loop_keeps_respawning(world, tmp_path, monkeypatch, home, delay, respawns):
    rec = mk(tmp_path)
    world.add(498, "node.exe", cwd="C:/Users/u/tools")          # 树外的守护者 (进程管理器之类): 是祖先, 绝不碰
    world.add(500, "python.exe", cwd=rec["cwd"], parent=498)
    world.listen = [(18080, 500, "127.0.0.1")]
    world.respawn[498] = (18080, rec["cwd"])
    world.respawn_delay = delay
    world.real_stop(monkeypatch, lambda plan: False)             # Ctrl+C 发不出去 -> 直接定点强杀
    assert inst.stop(rec["id"])["ok"]
    inst._join_workers(5)
    assert 498 in world.procs
    assert len(world.spawned) == respawns
    last = json.loads(inst.STATE.read_text(encoding="utf-8"))["last"][rec["id"]]
    assert last["outcome"] == "incomplete" and last["error"] == "停止后仍有进程在监听 18080"
    assert ("instance-stopped", rec["name"], "incomplete") in home
    s = status_of(rec["id"])
    assert s["state"] == "running" and s["started_by"] == "external"   # 状态照实: 它还在
    assert s["last_error"] == "停止后仍有进程在监听 18080"                # 为什么还在, 就挂在卡片上说


def test_restart_after_incomplete_stop_keeps_stop_record_and_does_not_start(world, tmp_path, monkeypatch, home):
    # 重启碰上外面的守护循环: 停不干净就别再起一份, 也别拿「重启后没能启动: already-running」盖掉停止记录 (R2)
    launched = fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    world.add(498, "node.exe", cwd="C:/Users/u/tools")
    world.add(500, "python.exe", cwd=rec["cwd"], parent=498)
    world.listen = [(18080, 500, "127.0.0.1")]
    world.respawn[498] = (18080, rec["cwd"])
    world.real_stop(monkeypatch, lambda plan: False)
    assert inst.restart(rec["id"]) == {"ok": True, "state": "stopping"}
    inst._join_workers(5)
    assert launched == []
    last = json.loads(inst.STATE.read_text(encoding="utf-8"))["last"][rec["id"]]
    assert last["state"] == "stopped" and last["outcome"] == "incomplete"
    assert last["error"] == "停止后仍有进程在监听 18080，没有重新启动"
    s = status_of(rec["id"])
    assert s["state"] == "running" and s["started_by"] == "external"
    assert s["last_error"] == "停止后仍有进程在监听 18080，没有重新启动"      # 解释留在卡片上, 没被取代掉
    assert ("instance-stopped", rec["name"], "incomplete") in home


def test_stop_sweep_kills_orphan_listener_but_not_other_instances_tree(world, tmp_path, monkeypatch):
    fake_launcher(world, monkeypatch)
    a = mk(tmp_path, "svc-a", ports=[18080, 18081])
    inst.start(a["id"])
    inst._tick()
    bdir = os.path.join(a["cwd"], "nested-b")
    os.makedirs(bdir)
    b = mk(tmp_path, "svc-b", cwd=bdir, ports=[18081])           # 另一个实例: 目录嵌在 a 下面, tokmon 正在追踪它
    assert inst.start(b["id"])["ok"]
    world.listen = [x for x in world.listen if x[0] != 18081]    # b 还没开始听 (a 定停止计划时看不见它)
    world.add(150, "python.exe", cwd=os.path.join(a["cwd"], "worker"))   # 父子链断了的孤儿: 不在 a 的树里

    def ctrl_c(plan):
        world.kill([100, 101], code=0)                  # a 的树优雅退出了 ...
        world.listen += [(18080, 150, "127.0.0.1"), (18081, 111, "127.0.0.1")]   # ... 孤儿和 b 此刻在听
        return True

    world.real_stop(monkeypatch, ctrl_c)
    assert inst.stop(a["id"])["ok"]
    inst._join_workers(5)
    assert 150 not in world.procs                       # 快照外、仍在 a 的端口上听的「它」: 一并收掉
    assert {110, 111} <= set(world.procs)               # 别的实例正在追踪的树: 绝不碰
    last = json.loads(inst.STATE.read_text(encoding="utf-8"))["last"][a["id"]]
    assert last["outcome"] == "forced"


def test_rename_refused_while_busy(world, tmp_path, monkeypatch):
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    inst.start(rec["id"])                                                   # starting = 在途
    data = dict(inst.get_instance(rec["id"]), id="new-id")
    assert inst.save_instance(data, original_id=rec["id"])["reason"] == "busy"
    assert inst.save_instance(dict(data, id=rec["id"], description="改描述可以"), original_id=rec["id"])["ok"]


def test_restart_stops_then_starts(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    inst.start(rec["id"])
    inst._tick()
    assert inst.restart(rec["id"]) == {"ok": True, "state": "stopping"}
    assert inst.start(rec["id"])["reason"] == "busy"
    inst._join_workers(5)
    assert len(launched) == 2 and len(world.stops) == 1
    assert status_of(rec["id"])["state"] == "starting"


def test_reset_waits_for_inflight_workers_before_clearing(world, tmp_path, monkeypatch):
    # 旧实现: 复位先清空线程列表, 夹具随后的 join 什么也等不到; 卡住的停止线程事后醒来写进别的 home
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    inst.start(rec["id"])
    inst._tick()
    world.gate = threading.Event()
    assert inst.stop(rec["id"])["ok"]
    workers = [w for w in inst._WORKERS if w.is_alive()]
    assert workers
    threading.Timer(0.3, world.gate.set).start()
    state_file = inst.STATE
    inst._reset_for_tests()
    assert not [w for w in workers if w.is_alive()]
    assert rec["id"] not in json.loads(state_file.read_text(encoding="utf-8"))["tracked"]   # 收尾写在复位之前、写在本测试的 home


def test_reset_also_stops_a_deps_waiter_born_while_it_joins(world, tmp_path, monkeypatch):
    # 测试在「重启 (依赖没好) 的停止阶段」失败: 夹具放行后重启线程停完才去等依赖, 这个等待生在复位叫停之后 ——
    # 以前它拿到的取消事件没人设、也不看 _STOP_EV, 熬过 join 后醒来写进下一个测试的 home (c26)
    fake_launcher(world, monkeypatch)
    spawned: list = []
    real_spawn = inst._spawn

    def spy(target, *args):
        t = real_spawn(target, *args)
        spawned.append((target.__name__, t))
        return t

    monkeypatch.setattr(inst, "_spawn", spy)
    rec = mk(tmp_path, deps=[{"name": "依赖A", "port": 18888}])
    assert inst.start(rec["id"], force=True)["ok"]
    inst._tick()
    world.gate = threading.Event()
    assert inst.restart(rec["id"])["ok"]
    threading.Timer(0.3, world.gate.set).start()
    t0 = time.monotonic()
    inst._reset_for_tests()
    assert "_wait_deps_then_launch" in [n for n, _ in spawned]            # 等依赖线程确实是复位途中才生出来的
    assert not [t for _, t in spawned if t.is_alive()]
    assert time.monotonic() - t0 < 4


def test_restored_tracking_after_tokmon_restart(world, tmp_path, monkeypatch):
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    gone = mk(tmp_path, "gone-svc", ports=[18090])
    old = mk(tmp_path, "old-boot", ports=[18091])
    slow = mk(tmp_path, "slow-svc", ports=[18092])
    world.add(300, "cmd.exe", cwd=rec["cwd"], ct=7000.0)
    world.add(301, "python.exe", cwd="C:/Users/u/elsewhere", ct=7000.5, parent=300)
    world.add(330, "cmd.exe", cwd=slow["cwd"], ct=7200.0)
    world.listen = [(18080, 301, "127.0.0.1")]
    deadline = time.time() + 30
    inst.STATE.parent.mkdir(parents=True, exist_ok=True)
    inst.STATE.write_text(json.dumps({"tracked": {
        rec["id"]: {"pid": 300, "create_time": 7000.0, "started_at": 7000.0},          # 旧格式: 没存阶段 -> 按在跑
        gone["id"]: {"pid": 310, "create_time": 7100.0, "started_at": 7100.0},
        old["id"]: {"pid": 320, "create_time": 10.0, "started_at": 10.0},
        slow["id"]: {"pid": 330, "create_time": 7200.0, "started_at": 7200.0, "phase": "starting",
                     "deadline": deadline, "free_at_launch": [18092]},
    }}), encoding="utf-8")
    restart_tokmon()
    s = status_of(rec["id"])
    assert s["state"] == "running" and s["started_by"] == "tokmon" and s["pid"] == 301
    # tokmon 不在时没了的: 关机 / 注销 / Fast Startup 都这样 (开机时间还可能不变) —— 如实「未运行 + 原因未知」, 绝不是崩溃
    for g in (status_of(gone["id"]), status_of(old["id"])):
        assert g["state"] == "stopped" and g["exit_code"] is None
        assert g["last_error"] == "tokmon 不在时退出（关机 / 注销 / 或崩溃，原因未知）"
    # 重启前还在「启动中」的: 阶段和期限都照旧, 不会被当成已经在跑 (c8)
    assert status_of(slow["id"])["state"] == "starting"
    assert inst._TRACK[slow["id"]]["deadline"] == deadline
    assert set(json.loads(inst.STATE.read_text(encoding="utf-8"))["tracked"]) == {rec["id"], slow["id"]}
    emitted: list = []
    inst._tick(emit=emitted.append)
    assert emitted == []                                              # 没有 PROCESS_CRASHED
    inst._TRACK[slow["id"]]["deadline"] = time.time() - 1             # 期限到了还没在听: 照常按超时收尸
    inst._tick(emit=emitted.append)
    inst._join_workers(5)
    s = status_of(slow["id"])
    assert s["state"] == "failed" and "启动超时" in s["last_error"] and 330 not in world.procs


def test_stop_in_flight_when_tokmon_restarts_is_never_reported_as_crash(world, tmp_path, monkeypatch):
    # 你点了停止, 服务还在优雅收尾, tokmon 就重启了: 以前恢复成「在跑」且忘了是你要停的, 它一退就报 PROCESS_CRASHED (R3)
    monkeypatch.setattr(events, "_ALLOWED_PAYLOAD", set(events._ALLOWED_PAYLOAD) | {"instance", "exit_code"})
    fake_launcher(world, monkeypatch)
    rec = mk(tmp_path)
    inst.start(rec["id"])
    inst._tick()
    world.gate = threading.Event()
    assert inst.stop(rec["id"])["ok"]
    disk = json.loads(inst.STATE.read_text(encoding="utf-8"))["tracked"][rec["id"]]
    assert disk["phase"] == "stopping" and disk["stop_requested"] is True    # 「是你要停的」落盘了
    world.gate.set()
    inst._join_workers(5)
    # 上一个 tokmon 停到一半就退了, 盘上留着两条 stopping: 一条的进程还在收尾, 一条的已经退了 (旧格式只有阶段)
    slow = mk(tmp_path, "slow-stop", ports=[18090])
    done = mk(tmp_path, "done-stop", ports=[18091])
    world.add(300, "cmd.exe", cwd=slow["cwd"], ct=7000.0)
    world.add(301, "python.exe", cwd=slow["cwd"], ct=7000.5, parent=300)
    world.listen = [(18090, 301, "127.0.0.1")]
    inst.STATE.write_text(json.dumps({"tracked": {
        slow["id"]: {"pid": 300, "create_time": 7000.0, "started_at": 7000.0, "phase": "stopping",
                     "stop_requested": True},
        done["id"]: {"pid": 310, "create_time": 7100.0, "started_at": 7100.0, "phase": "stopping"},
    }}), encoding="utf-8")
    restart_tokmon()
    emitted: list = []
    s = status_of(slow["id"])
    assert s["state"] == "running" and s["started_by"] == "tokmon"
    assert inst._TRACK[slow["id"]]["stop_requested"] is True
    d = status_of(done["id"])
    assert d["state"] == "stopped" and d["last_error"] is None             # 那次停止成了, 不是「原因未知」
    world.kill([300, 301], code=0)                                         # 它自己收完尾退了
    inst._tick(emit=emitted.append)
    assert emitted == []
    s = status_of(slow["id"])
    assert s["state"] == "stopped" and s["last_error"] is None
    assert slow["id"] not in inst._TRACK
    assert json.loads(inst.STATE.read_text(encoding="utf-8"))["last"][slow["id"]]["state"] == "stopped"


def test_start_group_skips_running_and_queues_rest(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.0)
    a = mk(tmp_path, "core-a", ports=[18080])
    b = mk(tmp_path, "core-b", ports=[18081])
    c = mk(tmp_path, "spare-c", ports=[18082], group="backup")
    world.add(900, "python.exe", cwd=a["cwd"])
    world.listen = [(18080, 900, "127.0.0.1")]
    r = inst.start_group("core")
    assert r["ok"] and r["queued"] == [b["id"]] and r["skipped"] == [{"id": a["id"], "reason": "already-running"}]
    inst._join_workers(5)
    assert [x[0] for x in launched] == [b["id"]]
    assert c["id"] not in [x[0] for x in launched]
    assert inst.start_group("everything")["reason"] == "bad-group"


def _wait_launches(launched, n, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and len(launched) < n:
        time.sleep(0.02)
    return len(launched) >= n


def test_group_start_staggers_launches_behind_shared_dep(world, tmp_path, monkeypatch):
    # 两个核心实例等同一个依赖: 依赖一好, 旧实现两个等依赖线程在同一拍一起冲; 现在一个一个来、按清单顺序、隔 _STAGGER_S
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.4)
    monkeypatch.setattr(inst, "_DEPS_POLL_S", 0.05)
    dep = [{"name": "auth-svc", "port": 18888}]
    a = mk(tmp_path, "grp-a", ports=[18080], deps=dep)
    b = mk(tmp_path, "grp-b", ports=[18081], deps=dep)
    assert inst.start_group("core")["queued"] == [a["id"], b["id"]]
    time.sleep(0.3)
    assert status_of(a["id"])["state"] == "waiting_deps"
    assert status_of(b["id"])["state"] == "stopped"                  # a 真正起来之前不轮到 b
    world.add(60, "dep-a.exe", cwd="C:/Users/u/apps")
    world.listen.append((18888, 60, "127.0.0.1"))
    assert _wait_launches(launched, 2)
    assert [x[0] for x in launched] == [a["id"], b["id"]]
    assert world.launch_times[1][1] - world.launch_times[0][1] >= 0.35


def test_boot_sequence_staggers_real_launches(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.4)
    monkeypatch.setattr(inst, "_DEPS_POLL_S", 0.05)
    monkeypatch.setattr(inst, "_BOOT_DELAY_S", 0.0)
    dep = [{"name": "auth-svc", "port": 18888}]
    a = mk(tmp_path, "boot-a", ports=[18080], deps=dep, boot="auto")
    b = mk(tmp_path, "boot-b", ports=[18081], deps=dep, boot="auto")
    t = threading.Thread(target=inst._boot_sequence, daemon=True)
    t.start()
    time.sleep(0.3)
    assert launched == []
    world.add(60, "dep-a.exe", cwd="C:/Users/u/apps")
    world.listen.append((18888, 60, "127.0.0.1"))
    assert _wait_launches(launched, 2)
    t.join(5)
    assert launched == [(a["id"], "boot"), (b["id"], "boot")]
    assert world.launch_times[1][1] - world.launch_times[0][1] >= 0.35


def test_manual_start_is_immediate_but_pushes_batch_slot(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.4)
    x = mk(tmp_path, "man-x", ports=[18080], group="backup")
    z = mk(tmp_path, "man-z", ports=[18082], group="backup")
    y = mk(tmp_path, "grp-y", ports=[18081])
    assert inst.start(x["id"])["state"] == "starting"
    assert inst.start(z["id"])["state"] == "starting"                # 你连点两个: 都立刻起, 不排队
    assert world.launch_times[1][1] - world.launch_times[0][1] < 0.2
    assert inst.start_group("core")["queued"] == [y["id"]]
    inst._join_workers(5)
    assert [i for i, _ in launched] == [x["id"], z["id"], y["id"]]
    assert world.launch_times[2][1] - world.launch_times[1][1] >= 0.35   # 批量的那个仍离上一次真实拉起 ≥ _STAGGER_S


class _SteppedBackClock:
    """inst 看到的 time 模块: 墙钟被往回拨了一小时 (开机对时常见), 单调时钟照旧。"""

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return time.time() - 3600


def test_stagger_gate_ignores_a_backward_wall_clock_step(world, tmp_path, monkeypatch):
    # 旧实现用墙钟算闸门: 拉起一个后墙钟回拨 Δ, 下一个批量拉起要等 Δ + 5s (开机时一小时都拉不起来) (R1)
    fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.3)
    x = mk(tmp_path, "man-x", ports=[18080])
    y = mk(tmp_path, "grp-y", ports=[18081])
    assert inst.start(x["id"])["state"] == "starting"
    monkeypatch.setattr(inst, "time", _SteppedBackClock())
    res = inst._start(y["id"], force=False, wait_deps=False, actor="boot", internal=False, stagger=True)
    assert res["reason"] == "stagger" and 0 < res["wait"] <= 0.3
    time.sleep(0.35)
    assert inst._start(y["id"], force=False, wait_deps=False, actor="boot", internal=False, stagger=True)["ok"]


# ---------------------------------------------------------------- 开机拉起

def test_boot_sequence_once_per_tokmon_process_not_per_kernel_boot(world, tmp_path, monkeypatch, home):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(events, "_ALLOWED_PAYLOAD", set(events._ALLOWED_PAYLOAD) | {"instance", "exit_code"})
    monkeypatch.setattr(inst, "_BOOT_DELAY_S", 0.0)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.0)
    auto = mk(tmp_path, "boot-auto", ports=[18080], boot="auto")
    ask = mk(tmp_path, "boot-ask", ports=[18081], boot="ask")
    mk(tmp_path, "boot-manual", ports=[18082], boot="manual")
    emitted: list = []
    inst.start_supervisor(boot=True, emit=emitted.append)
    first = inst._BOOT_THREAD
    first.join(5)
    assert launched == [(auto["id"], "boot")]
    assert [e.type for e in emitted] == ["INSTANCE_BOOT_PENDING"]
    ev = emitted[0]
    st = inst.status_all()
    handled = st["boot"]["handled"]
    assert ev.pillar == "instances" and ev.severity == "warning" and ev.payload["count"] == 1
    assert ev.dedup_key == f"INSTANCE_BOOT_PENDING:{handled}"         # 按「这次开机拉起」的时刻去重, 不按内核开机时间
    assert ("instance-boot-start", auto["name"], "starting") in home
    assert st["boot"] == {"handled": handled, "pending": [ask["id"]]}  # 不再有 boot_time
    assert status_of(ask["id"])["boot_pending"] is True

    # 同一个 tokmon 进程里再调: 不再做第二遍
    inst.start_supervisor(boot=True, emit=emitted.append)
    assert inst._BOOT_THREAD is first and len(launched) == 1 and len(emitted) == 1

    # Fast Startup 关机再开 / 注销重登: 内核开机时间不变, 会话里的进程全没了, 开机任务新起一个 tokmon -> 照样再拉一遍
    inst._reset_for_tests()
    world.procs.clear()
    world.listen = []
    time.sleep(0.05)
    inst.start_supervisor(boot=True, emit=emitted.append)
    inst._BOOT_THREAD.join(5)
    assert launched == [(auto["id"], "boot"), (auto["id"], "boot")]
    assert [e.type for e in emitted] == ["INSTANCE_BOOT_PENDING", "INSTANCE_BOOT_PENDING"]   # 上次拉起的没了 ≠ 崩溃
    assert emitted[1].dedup_key != emitted[0].dedup_key                # 铃铛按新 key 会再响
    assert inst.status_all()["boot"]["pending"] == [ask["id"]]


def test_boot_skips_degraded_auto_instance_and_audits_why(world, tmp_path, monkeypatch, home):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_BOOT_DELAY_S", 0.0)
    auto = mk(tmp_path, "boot-auto", ports=[18080], boot="auto")
    inst.start(auto["id"])
    inst._tick()
    world.listen = []                                  # 进程还在, 端口不听了
    assert inst._boot_sequence() == "done"
    assert len(launched) == 1                          # 没再起一份
    assert ("instance-boot-start", auto["name"], "degraded") in home


def test_boot_ack_start_and_dismiss(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    monkeypatch.setattr(inst, "_STAGGER_S", 0.0)
    ask = mk(tmp_path, "boot-ask", ports=[18081], boot="ask")
    with inst._LOCK:
        inst._state()["boot_pending"] = [ask["id"]]
    r = inst.boot_ack("start")
    assert r["ok"] and r["queued"] == [ask["id"]]
    inst._join_workers(5)
    assert launched == [(ask["id"], "user")]
    assert inst.status_all()["boot"]["pending"] == []
    with inst._LOCK:
        inst._state()["boot_pending"] = [ask["id"]]
    assert inst.boot_ack("dismiss") == {"ok": True, "dismissed": [ask["id"]]}
    assert inst.boot_ack("maybe")["reason"] == "bad-action"


def test_pending_cleared_once_instance_runs(world, tmp_path):
    ask = mk(tmp_path, "boot-ask", ports=[18081], boot="ask")
    with inst._LOCK:
        inst._state()["boot_pending"] = [ask["id"]]
    assert inst.status_all()["boot"]["pending"] == [ask["id"]]
    world.add(70, "node.exe", cwd=ask["cwd"])
    world.listen = [(18081, 70, "127.0.0.1")]
    assert inst.status_all()["boot"]["pending"] == []          # 显示层立刻不再提醒
    inst._tick()
    assert json.loads(inst.STATE.read_text(encoding="utf-8"))["boot_pending"] == []


# ---------------------------------------------------------------- cloudflared 隧道

def test_tunnel_quick_url_via_metrics(world, tmp_path, monkeypatch):
    rec = mk(tmp_path)
    world.add(10, "python.exe", cwd=rec["cwd"])
    world.add(900, "cloudflared.exe", cwd="C:/Users/u")
    world.cf = [(900, ["cloudflared.exe", "tunnel", "--url", "http://localhost:18080"])]
    world.listen = [(18080, 10, "127.0.0.1"), (19241, 900, "127.0.0.1"), (19242, 900, "10.0.0.5")]
    calls: list = []

    def fake_get(host, port, path, timeout=0.5):
        calls.append((host, port, path))
        return {"hostname": "demo-quick.trycloudflare.com"} if port == 19241 else None

    monkeypatch.setattr(inst, "_http_get_json", fake_get)
    s = status_of(rec["id"])
    assert s["tunnel"] == {"url": "https://demo-quick.trycloudflare.com", "kind": "quick", "pid": 900}
    assert calls == [("127.0.0.1", 19241, "/quicktunnel")]           # 只问 loopback, 非本机地址绝不连
    status_of(rec["id"])
    assert len(calls) == 1                                           # 30s 缓存


def test_tunnel_named_and_explicit_metrics(world, tmp_path, monkeypatch):
    rec = mk(tmp_path, ports=[18080, 18081])
    world.cf = [(901, ["cloudflared", "tunnel", "--metrics", "localhost:19300", "--url=localhost:18081"])]
    calls: list = []
    monkeypatch.setattr(inst, "_http_get_json",
                        lambda host, port, path, timeout=0.5: calls.append((host, port)) or {"hostname": ""})
    s = status_of(rec["id"])
    assert s["tunnel"] == {"url": None, "kind": "named", "pid": 901}
    assert calls == [("127.0.0.1", 19300)]
    assert inst._parse_cf(["cloudflared", "--metrics", "10.1.2.3:2000", "--url", "https://127.0.0.1"]) == (443, None)


def test_tunnel_ignores_unrelated_cloudflared(world, tmp_path, monkeypatch):
    rec = mk(tmp_path)
    world.cf = [(902, ["cloudflared", "tunnel", "--url", "http://localhost:18999"])]
    monkeypatch.setattr(inst, "_http_get_json", lambda *a, **k: pytest.fail("不该去问无关隧道"))
    assert status_of(rec["id"])["tunnel"] is None


# ---------------------------------------------------------------- 日志 / 草稿

def test_log_tail_redacts_strips_ansi_and_clamps(home, tmp_path):
    rec = mk(tmp_path)
    inst.LOGS.mkdir(parents=True, exist_ok=True)
    body = ("".join(f"n{i}\n" for i in range(600)) + "\x1b[32mgreen\x1b[0m\nprogress 10%\rprogress 100%\r\n"
            "auth token=abcdef1234567890\n" + "中文行\n")
    raw = body.encode("utf-8")
    (inst.LOGS / f"{rec['id']}.log").write_bytes(raw)
    r = inst.log_tail(rec["id"], 5000)                     # 上限 500 行
    assert r["ok"] and r["exists"] and len(r["lines"]) == 500 and r["lines"][0] == "n104"
    assert r["size"] == len(raw)
    tail4 = inst.log_tail(rec["id"], 4)["lines"]
    assert tail4[:2] == ["green", "progress 100%"] and tail4[3] == "中文行"
    assert "abcdef1234567890" not in tail4[2] and "***" in tail4[2]
    assert inst.log_tail(rec["id"], 0)["lines"] == ["中文行"]
    assert inst.log_tail("../../etc/passwd")["reason"] == "not-found"
    assert inst.log_tail("nope")["reason"] == "not-found"
    other = mk(tmp_path, "no-log", ports=[18099])
    assert inst.log_tail(other["id"]) == {"ok": True, "lines": [], "size": 0, "exists": False}


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_draft_from_pid_identity_checks(home):
    import psutil
    me = psutil.Process(os.getpid())
    r = inst.draft_from_pid(os.getpid(), me.create_time())
    assert r["ok"] and r["draft"]["kind"] == "self" and r["draft"]["boot"] == "manual"
    assert r["draft"]["cwd"] and r["draft"]["command"] and not r["draft"]["name"].lower().endswith(".exe")
    assert inst.draft_from_pid(os.getpid(), me.create_time() - 50)["reason"] == "pid-reused"
    assert inst.draft_from_pid(os.getpid(), None)["reason"] == "identity-required"
    assert inst.draft_from_pid(os.getpid(), float("nan"))["reason"] == "identity-required"
    assert inst.draft_from_pid(2 ** 22 + 7, 1.0)["reason"] in ("gone", "access-denied")


def test_draft_command_quotes_cmd_metacharacters(monkeypatch):
    monkeypatch.setattr(inst, "_IS_WIN", True)
    cmd, warns = inst._draft_command(["C:/tools/app.exe", "--db", "db?sslmode=require&timeout=10^x",
                                      "--filter=size>100", "a|b", "(x)", "plain", "with space", "x&y\\"])
    assert cmd == ('C:/tools/app.exe --db "db?sslmode=require&timeout=10^x" "--filter=size>100" "a|b" "(x)" '
                   'plain "with space" "x&y\\\\"')                  # 结尾反斜杠翻倍, 免得吞掉收尾引号
    assert warns == []
    _cmd, warns = inst._draft_command(["app", "--pct", "50%"])
    assert warns == ["命令里有 %，cmd 会展开成环境变量，保存前请核对"]
    assert any("双引号" in w for w in inst._draft_command(["app", 'say "hi"'])[1])
    monkeypatch.setattr(inst, "_IS_WIN", False)                      # POSIX: 经 /bin/sh -c, 用 shell 引号
    assert inst._draft_command(["app", "a b", "x&y"]) == ("app 'a b' 'x&y'", [])


def test_draft_from_pid_builds_cmd_safe_command_and_warns(home, monkeypatch):
    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 5000.0

        def name(self):
            return "app.exe"

        def cmdline(self):
            return ["C:/tools/app.exe", "--url", "http://h/x?a=1&b=2", "--pct", "50%"]

        def cwd(self):
            return "C:/Users/u/proj"

    monkeypatch.setattr(inst, "_HAS_PSUTIL", True)
    monkeypatch.setattr(inst, "_IS_WIN", True)
    monkeypatch.setattr(inst.psutil, "Process", FakeProc)
    monkeypatch.setattr(inst, "_scan_listeners", lambda: [(18080, 4242, "127.0.0.1")])
    r = inst.draft_from_pid(4242, 5000.0)
    assert r["ok"] and r["draft"]["command"] == 'C:/tools/app.exe --url "http://h/x?a=1&b=2" --pct 50%'
    assert r["draft"]["name"] == "app" and r["draft"]["project"] == "proj" and r["draft"]["ports"] == [18080]
    assert "%" in r["warning"]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe 的解析规则")
def test_real_draft_command_survives_cmd_roundtrip(tmp_path):
    import subprocess
    args = ["db?sslmode=require&timeout=10^x", "--filter=size>100", "a|b", "(x)", "with space", "x&y\\", "plain"]
    argv = [sys.executable, "-c", "import sys, json; print(json.dumps(sys.argv[1:]))"] + args
    cmd, warns = inst._draft_command(argv)
    assert warns == []
    r = subprocess.run(f'{inst._comspec()} /d /s /c "{cmd}"', cwd=tmp_path, capture_output=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    assert json.loads(r.stdout.decode("utf-8")) == args, r.stderr
    assert list(tmp_path.iterdir()) == []                          # 没有被 > 重定向出来的文件


# ---------------------------------------------------------------- 真实集成: 拉起 -> 跑起来 -> 优雅停止

_SERVER = r'''
import http.server, pathlib, sys
port, marker = int(sys.argv[1]), sys.argv[2]

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass

srv = http.server.HTTPServer(("127.0.0.1", port), H)
print("demo server listening", port, flush=True)
try:
    srv.serve_forever(poll_interval=0.2)
except KeyboardInterrupt:
    pathlib.Path(marker).write_text("graceful", encoding="utf-8")
    print("got interrupt, bye", flush=True)
'''


def _free_port() -> int:
    """测试专用段 18000-19999 里挑一个空闲端口 (离你本机常驻服务的端口远远的)。"""
    import random
    for port in random.sample(range(18000, 20000), 200):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("18000-19999 里找不到空闲端口")


def _wait(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.15)
    return pred()


def _kill_spawned(roots, port=None, work=None):
    """只收拾本测试自己拉起的进程 (拉起时脱离了 job、没有窗口, 漏一个就是隐形孤儿):
    先冻住 (挂起) 根 —— cmd 拉起 python 只要几十毫秒, 先点名再杀根会漏掉点名之后才生出来的那个 (c27);
    再反复点名子孙 (每个也先冻住) 直到连着两次没有新的; 先子后根杀掉;
    最后补一刀: 仍监听测试端口、且 cwd 在测试目录下的。绝不碰 pytest 自己。"""
    import psutil
    me = os.getpid()
    found: dict = {}                                    # pid -> 句柄, 按发现顺序 (自上而下)

    def freeze(p):
        if p.pid == me or p.pid in found:
            return False
        found[p.pid] = p
        try:
            p.suspend()
        except psutil.Error:
            pass
        return True

    for h in roots:
        freeze(h)
    quiet = 0
    while quiet < 2:                                    # 冻住那一刻正在创建的子进程可能下一次点名才露面
        new = False
        for p in list(found.values()):
            try:
                kids = p.children(recursive=True) if p.is_running() else []
            except psutil.Error:
                kids = []
            for c in kids:
                new = freeze(c) or new
        quiet = 0 if new else quiet + 1
        if not new and quiet < 2:
            time.sleep(0.05)
    victims: list = list(reversed(list(found.values())))    # 先子后根
    for p in victims:
        try:
            if p.is_running():
                p.kill()
        except psutil.Error:
            pass
    if port and work:
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.Error, OSError):
            conns = []
        base = os.path.normcase(os.path.abspath(str(work)))
        for c in conns:
            if c.status != psutil.CONN_LISTEN or not c.laddr or c.laddr.port != port or not c.pid or c.pid == me:
                continue
            try:
                p = psutil.Process(c.pid)
                if os.path.normcase(os.path.abspath(p.cwd())).startswith(base):
                    extra = [p] + p.children(recursive=True)
                    for q in extra:
                        freeze(q)
                    for q in reversed(extra):
                        victims.append(q)
                        try:
                            q.kill()
                        except psutil.Error:
                            pass
            except psutil.Error:
                pass
    psutil.wait_procs(victims, timeout=3)


_SLEEPER = r'''
import pathlib, sys, time
try:
    time.sleep(60)
except KeyboardInterrupt:
    pathlib.Path(sys.argv[1]).write_text("interrupted", encoding="utf-8")
'''


class _LaunchingRoot:
    """假的 cmd 根: 第一次被点名时 python 还没生出来, 只要它没被冻住, 点名之后马上就生一个 (真实里 cmd 拉起 python 要几十毫秒)。"""

    def __init__(self, pid):
        self.pid, self.kids, self.frozen, self.alive = pid, [], False, True

    def suspend(self):
        self.frozen = True

    def is_running(self):
        return self.alive

    def children(self, recursive=False):
        out = list(self.kids)
        if self.alive and not self.frozen and not self.kids:
            self.kids.append(_LaunchingRoot(self.pid + 1))
        return out

    def kill(self):
        self.alive = False


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_kill_spawned_freezes_the_root_before_listing_children(monkeypatch):
    # 旧清理: 点一次名 (空) -> 杀根 -> 点名之后才生出来的 python 成了孤儿, 占着端口一直跑 (c27)
    import psutil
    monkeypatch.setattr(psutil, "wait_procs", lambda procs, timeout=None: ([], []))
    root = _LaunchingRoot(2 ** 22 + 100)
    _kill_spawned([root])
    assert not root.alive and not [k for k in root.kids if k.alive]


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_kill_spawned_reaches_grandchildren_given_only_the_root(tmp_path):
    # 集成测试在「跑起来」之前失败时手里只有 cmd 根: 清理必须现查子孙, 否则 python 孙进程成了隐形孤儿
    import subprocess

    import psutil
    (tmp_path / "sleeper.py").write_text(_SLEEPER, encoding="utf-8")
    if sys.platform == "win32":
        p = subprocess.Popen(f'{inst._comspec()} /d /s /c ""{sys.executable}" sleeper.py m.txt"', cwd=tmp_path,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        p = subprocess.Popen(["/bin/sh", "-c", f'"{sys.executable}" sleeper.py m.txt; true'], cwd=tmp_path,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    root = psutil.Process(p.pid)
    kid = _wait(lambda: next((c for c in root.children() if c.name().lower().startswith("python")), None), 15)
    try:
        assert kid
        _kill_spawned([root])
        assert not kid.is_running() and not root.is_running()
    finally:
        _kill_spawned([root] + ([kid] if kid else []))


@pytest.mark.skipif(sys.platform != "win32" or not inst.available(), reason="Windows 控制台机制")
def test_real_ctrl_c_helper_only_fires_on_console_holding_target(tmp_path):
    # 父 cmd 活在另一个控制台 (如你的窗口 / 以 pause 结尾的 .bat), owner 在自己的控制台: Ctrl+C 必须送到 owner 那边
    import ctypes
    import subprocess

    import psutil
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)       # 同 _popen_windows: 让子进程收得到 Ctrl+C
    (tmp_path / "sleeper.py").write_text(_SLEEPER, encoding="utf-8")
    roots: list = []

    def spawn(marker):
        p = subprocess.Popen(f'{inst._comspec()} /d /s /c ""{sys.executable}" -u sleeper.py {marker}"', cwd=tmp_path,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)     # 各自一个独立 (隐藏) 控制台
        root = psutil.Process(p.pid)
        roots.append(root)
        return root, _wait(lambda: next((c for c in root.children() if c.name().lower().startswith("python")), None), 15)

    try:
        a_root, a_py = spawn("a.txt")
        b_root, b_py = spawn("b.txt")
        assert a_py and b_py
        allowed = {a_root.pid, a_py.pid, b_root.pid, b_py.pid}
        # 只挂得上 A, 而要停的 B 不在 A 的控制台上: 不发 (旧 helper 会在 A 上发出去)
        assert inst._win_ctrl_c([a_root.pid], allowed, [b_py.pid]) == "attach-failed"
        # 先试 A (没有目标) -> 换 B 的控制台 -> 发
        assert inst._win_ctrl_c([a_root.pid, b_root.pid], allowed, [b_py.pid]) == "sent"
        assert _wait(lambda: (tmp_path / "b.txt").exists(), 10)
        time.sleep(0.5)
        assert not (tmp_path / "a.txt").exists() and a_py.is_running()
    finally:
        _kill_spawned(roots)


@pytest.mark.skipif(sys.platform != "win32" or not inst.available(), reason="Windows 控制台机制")
def test_real_ctrl_c_helper_never_fires_on_a_console_holding_a_forbidden_pid(tmp_path):
    # 同一个控制台上挂着「绝不能收到 Ctrl+C」的进程 (真实里是 tokmon 自己, 比如它在终端里跑、实例跟它同窗):
    # 就算其余进程都在允许名单里, helper 也不发 —— 否则 Ctrl+C 打到 tokmon 身上
    import ctypes
    import subprocess

    import psutil
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    (tmp_path / "sleeper.py").write_text(_SLEEPER, encoding="utf-8")
    py = f'"{sys.executable}" -u sleeper.py'
    p = subprocess.Popen(f'{inst._comspec()} /d /s /c "start "" /b {py} x.txt & {py} y.txt"', cwd=tmp_path,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)     # 两个 python 共用 cmd 的 (隐藏) 控制台
    root = psutil.Process(p.pid)
    kids: list = []
    try:
        kids = _wait(lambda: (lambda ks: ks if len(ks) == 2 else None)(
            [c for c in root.children() if c.name().lower().startswith("python")]), 15) or []
        assert kids, root.children()
        target, bystander = kids
        allowed = {root.pid, target.pid, bystander.pid}
        # helper 本体: 控制台上全是允许的进程, 只因 bystander 被列为 forbidden -> 当作共用控制台, 不发
        r = subprocess.run([sys.executable, "-c", inst._WIN_CTRL_C_HELPER, str(root.pid),
                            ",".join(map(str, sorted(allowed))), str(target.pid), str(bystander.pid)],
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=30, capture_output=True)
        assert r.returncode == 4 and b"shared-console" in r.stdout, r
        assert inst._win_ctrl_c([root.pid], allowed, [target.pid], forbidden=[bystander.pid]) == "shared-console"
        time.sleep(0.5)
        assert not (tmp_path / "x.txt").exists() and not (tmp_path / "y.txt").exists()
        # 对照: 同一个控制台、不列 forbidden 时是发得出去的 (上面没发, 是 forbidden 挡住的)。
        # start /b 起的那个按 cmd 的规矩忽略 Ctrl+C, 前台那个 (y) 收得到
        assert inst._win_ctrl_c([root.pid], allowed, [target.pid]) == "sent"
        assert _wait(lambda: (tmp_path / "y.txt").exists(), 10)
    finally:
        _kill_spawned([root] + kids)                   # start /b 那个不理 Ctrl+C, cmd 退了它就成孤儿: 手里的句柄一并收掉


@pytest.mark.skipif(sys.platform != "win32" or not inst.available(), reason="cmd.exe 行为")
def test_real_bare_bat_runs_even_if_tokmon_env_disables_cwd_search(home, tmp_path, monkeypatch):
    monkeypatch.setenv("NoDefaultCurrentDirectoryInExePath", "1")   # 在 Claude Code 会话里起 tokmon 时就带着它
    work = tmp_path / "bat proj"
    work.mkdir()
    (work / "run.bat").write_bytes(b"@echo off\r\necho ran> marker.txt\r\n")
    h = inst._launch({"id": "demo-bat", "cwd": str(work), "command": "run.bat", "env": {}}, "user")
    try:
        h["popen"].wait(30)
    finally:
        if h["popen"].poll() is None:
            h["popen"].kill()
    log = (inst.LOGS / "demo-bat.log").read_bytes().decode("utf-8", "replace")
    assert (work / "marker.txt").exists(), log


@pytest.mark.skipif(not inst.available(), reason="needs psutil")
def test_real_start_then_graceful_stop(home, tmp_path, monkeypatch):
    import psutil
    work = tmp_path / "demo proj"                        # 目录带空格: 顺带验证引号
    work.mkdir()
    (work / "server.py").write_text(_SERVER, encoding="utf-8")
    port = _free_port()
    monkeypatch.setattr(inst, "_SUP_INTERVAL_S", 0.2)
    monkeypatch.setattr(inst, "_LOG_ROTATE_BYTES", 16)
    r = inst.save_instance({"name": "Demo API", "cwd": str(work), "ports": [port], "stop_grace_s": 8,
                            "start_timeout_s": 20,
                            "command": f'"{sys.executable}" -u server.py {port} marker.txt'})
    iid = r["instance"]["id"]
    inst.LOGS.mkdir(parents=True, exist_ok=True)
    (inst.LOGS / f"{iid}.log").write_text("old log line that is long enough to rotate\n", encoding="utf-8")
    emitted: list = []
    inst.start_supervisor(emit=emitted.append)
    handles: list = []
    roots: list = []
    try:
        res = inst.start(iid)
        assert res["ok"] and res["state"] == "starting", res
        try:
            roots = [psutil.Process(res["pid"])]
            handles = list(roots)
        except psutil.NoSuchProcess:
            pass
        s = _wait(lambda: (lambda x: x if x["state"] == "running" else None)(status_of(iid)), 15)
        assert s, (status_of(iid), inst.log_tail(iid))
        assert s["started_by"] == "tokmon" and s["listening"][0]["mine"] is True
        if roots:
            handles += roots[0].children(recursive=True)
        assert (inst.LOGS / f"{iid}.log.1").exists()                          # 启动前轮转了旧日志
        assert _wait(lambda: any("demo server listening" in ln for ln in inst.log_tail(iid)["lines"]), 5)
        assert any("由 tokmon 启动" in ln for ln in inst.log_tail(iid)["lines"])

        assert inst.stop(iid) == {"ok": True, "state": "stopping"}
        s = _wait(lambda: (lambda x: x if x["state"] == "stopped" else None)(status_of(iid)), 15)
        assert s, status_of(iid)
        assert (work / "marker.txt").exists(), inst.log_tail(iid)            # 走的是优雅路径 (Ctrl+C / SIGINT)
        assert json.loads(inst.STATE.read_text(encoding="utf-8"))["last"][iid]["outcome"] == "graceful"
        assert not [h for h in handles if h.is_running()]
        assert not [e for e in emitted if e.type == "PROCESS_CRASHED"]
    finally:
        # 无论在哪一步失败: 现查整棵树 + 测试端口上 cwd 在测试目录下的监听者, 一并收掉 (不只是 cmd 根)
        _kill_spawned(roots + [h for h in handles if h not in roots], port, work)



def test_restart_of_a_tracked_portless_that_just_died_relaunches_without_asking(world, tmp_path, monkeypatch):
    """第三轮复查: tokmon 追踪着的无端口实例刚退出 (supervisor 还没来得及看到), 你点「重启」—— 这是替换它, 不是双开, 不该要确认。"""
    launched = fake_launcher(world, monkeypatch)
    w = mk(tmp_path, "demo-worker", ports=[], command="python worker.py")
    assert inst.start(w["id"], force=True)["ok"]                     # 第一次: 你已确认过
    root = inst._TRACK[w["id"]]["pid"]
    inst._TRACK[w["id"]]["phase"] = "running"                       # 早已跑稳 (无端口的 3 秒观察期过了)
    world.procs.pop(root + 1, None)
    world.procs.pop(root, None)                                     # 进程没了, 但还没过 supervisor 的一拍
    r = inst.restart(w["id"])
    assert r["ok"] and r["state"] == "starting", r
    inst._join_workers(5)
    assert len(launched) == 2


# ---------------- 终轮静态复查 ----------------

def test_tiebreak_winner_missing_its_own_token_yields_when_the_parent_names_a_rival(world, tmp_path):
    # A = run_a.bat (里面跑 python app.py), B = python app.py --safe。你双击了 run_a.bat:
    # 进程自己 `python app.py` 对上 B 的两个词却缺了 B 的 --safe, 上一层 cmd /c run_a.bat 明说是 A —— 不能判给 B
    a = mk(tmp_path, "svc-a", ports=[18431], command="run_a.bat")
    b = mk(tmp_path, "svc-b", ports=[18431], cwd=a["cwd"], command="python app.py --safe")
    world.add(499, "cmd.exe", cwd=a["cwd"], cmdline=["cmd.exe", "/c", "run_a.bat"])
    world.add(500, "python.exe", cwd=a["cwd"], parent=499, cmdline=["python", "app.py"])
    world.listen = [(18431, 500, "127.0.0.1")]
    assert status_of(b["id"])["state"] == "port_busy"
    assert inst.stop(b["id"])["ok"] is False and 500 in world.procs


def test_restart_refuses_when_port_table_unreadable_and_not_tracked(world, tmp_path, monkeypatch):
    launched = fake_launcher(world, monkeypatch)
    ext = mk(tmp_path, "ext-svc", ports=[18090])
    world.add(600, "python.exe", cwd=ext["cwd"])
    world.listen = [(18090, 600, "127.0.0.1")]
    world.scan_ok = False                                           # 它可能正在外面跑着, 只是看不见
    r = inst.restart(ext["id"])
    assert r["ok"] is False and r["reason"] == "unknown"            # 跟「停止」一样: 不替你再起一份
    inst._join_workers(5)
    assert launched == []


def test_patch_without_boot_does_not_trip_the_portless_rule_on_old_records(world, tmp_path):
    w = mk(tmp_path, "demo-worker", ports=[], command="python worker.py", boot="manual")
    doc = json.loads(inst.MANIFEST.read_text(encoding="utf-8"))
    doc["instances"][0]["boot"] = "ask"                             # 旧版本留下的「无端口 + 提醒」
    inst.MANIFEST.write_text(json.dumps(doc), encoding="utf-8")
    assert inst.patch_instance(w["id"], {"group": "backup"})["ok"]  # 只是挪到备用: 不该因为没动过的开机策略被拒
    assert inst.patch_instance(w["id"], {"boot": "ask"})["errors"] == {"boot": "portless-auto"}
