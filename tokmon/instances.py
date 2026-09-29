"""实例层 (v0.22): 你登记的本机服务的「期望状态」+ 执行你显式下达的启停。

它是什么: `~/.tokmon/instances/instances.json` 里登记的本机服务 (dev server / 看板 / 引擎…) 清单,
加上对它们的识别 (谁在监听声明的端口、是不是它) 与你点了才发生的 启动 / 停止 / 重启, 以及你为每个实例声明的开机策略。
它是 /processes 上的第二个支柱; 监控层 procmon 仍然纯只读, 实例逻辑全在这里。

P7 控制层守则在本模块的落点:
- ① allow-list: 只对清单里的实例 (按 id) 动作; 跑的就是你登记的那条命令, 不接受任意指令。
- ② 你显式触发: 启停只来自你的点击, 或你自己声明的开机策略 (auto / ask); 系统从不自作主张。
- ③ 鉴权: 控制令牌 + 仅本机 Host —— 由 serve 层把守 (本模块不碰 HTTP)。
- ④ 全审计: 用户动作由 serve 审计; 开机拉起 / 停止结果 / 启动超时收尸 由本模块经 `_audit` 钩子审计。
- ⑤ 失败安全: 崩溃只通知, **绝不自动重启**; 端口冲突只报告, **绝不杀认不出的占用者**; 杀前按 create_time 复核身份,
  外部启动的实例只杀端口 owner 及其子孙, 绝不碰祖先 (祖先可能是你的终端)。
单一主人: 同一个 ~/.tokmon 上只有拿到 owner.lock (OS 独占锁) 的那个 tokmon 跑监督 / 开机拉起、接受启停与编辑、
写 state.json; 第二个 tokmon (别的端口 / 开发副本) 只读 —— 否则两边各推各的, 你的一次停止会被另一边当成崩溃报出去。
状态照实: 端口表读不到就说「未知」, 进程在而端口没在听就说出来, 认不准归属就谁都不认 —— 绝不为了好看报「运行中」。
零外发: 只碰 loopback (cloudflared 本机 metrics)。渐进降级: 缺 psutil -> available() False, 只给清单不给状态。
隐私: 读接口里命令一律脱敏 (procmon._redact), env 只给键不给值; 清单在 ~/.tokmon, 永不进仓库。
"""

from __future__ import annotations

import copy
import http.client
import json
import locale
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover - 由 serve 层降级提示
    psutil = None
    _HAS_PSUTIL = False

from . import procmon
from .events import Event, bus

_IS_WIN = sys.platform == "win32"

# ---- 路径 (测试用 set_home 整体挪走) ----
_HOME = Path.home() / ".tokmon" / "instances"
MANIFEST = _HOME / "instances.json"
STATE = _HOME / "state.json"
LOGS = _HOME / "logs"

# ---- 节奏 / 上限 (模块常量, 测试可 monkeypatch) ----
_STATUS_TTL = 1.5          # status_all 短缓存: 多标签页 / 页面 4s 轮询共享一帧
_SUP_INTERVAL_S = 2.0      # 监督线程节拍
_STAGGER_S = 5.0           # 批量拉起之间错开 (免得一起抢 CPU/端口, 也让通知一个个来)
_BOOT_DELAY_S = 5.0        # 开机拉起前再等一会 (网络/依赖刚起)
_DEPS_POLL_S = 3.0
_DEPS_WAIT_S = 900.0       # 等依赖最多 15 分钟
_PORTLESS_UP_S = 3.0       # 无端口服务: 活过 3s 就算起来了
_UNKNOWN_EXTEND_S = 10.0   # 端口表读不到时, 启动期限每拍往后推这么多 (读不到 ≠ 没在听, 绝不因此收尸)
_KILL_WAIT_S = 3.0
_SWEEP_ROUNDS = 3          # 停止后补扫轮数: 宽限期里新冒出来的 / 快照外仍在声明端口上监听的「它」
_SWEEP_GAP_S = 0.5         # 补扫前 / 各轮之间等这么久 (≤1s): 给「被杀后又被谁拉起来」的进程一点时间开始监听
_CLAIM_RETRY_S = 5.0       # 非主人每隔这么久再抢一次主人锁: 原来的主人退出了, 这边接手监督
_PROBE_TIMEOUT_S = 0.3     # 端口表读不到时, 对本机回环试连依赖端口的超时
_LOG_ROTATE_BYTES = 5 * 1024 * 1024
_CT_TOL = 1.0              # create_time 身份容差 (秒)
_TUNNEL_TTL = 30.0
_CF_TTL = 10.0
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

GROUPS = ("core", "backup")
KINDS = ("service", "self")
BOOTS = ("auto", "ask", "manual")
_PATCHABLE = ("boot", "group", "name", "description", "project")
_BUSY_PHASES = ("starting", "stopping", "waiting_deps")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_LABELS = {
    "running": "运行中", "starting": "启动中", "stopping": "停止中", "stopped": "未运行",
    "failed": "启动失败", "crashed": "意外退出", "port_busy": "端口被占用",
    "waiting_deps": "等依赖就绪", "self": "本服务",
    "degraded": "进程在 · 端口没在监听", "unknown": "状态未知（读不到端口表）",
}
_PORTLESS_HEDGE = "（没有端口，外部启动的认不出）"
_PORTLESS_STOPPED_LABEL = "未运行" + _PORTLESS_HEDGE
_PORTLESS_CONFIRM = "没有端口，认不出它是不是已经在别处跑着；确定要再起一份吗？"
_GONE_WHILE_AWAY = "tokmon 不在时退出（关机 / 注销 / 或崩溃，原因未知）"
# 端口转发 / 代理进程 (docker compose / WSL 发布的端口, LISTEN 记在它们名下): 实例拉起时端口是空的、根进程还活着,
# 这时声明端口被它们监听 -> 算作实例在服务; 但它们永远不是实例的进程, 绝不杀
_PROXY_NAMES = frozenset({"com.docker.backend.exe", "com.docker.proxy.exe", "docker-proxy",
                          "vpnkit.exe", "wslrelay.exe", "wslhost.exe"})

# ---- 运行期状态 (全部由 _LOCK 保护) ----
_LOCK = threading.RLock()
_TRACK: dict[str, dict] = {}       # id -> {popen, pid, create_time, started_at, phase, stop_requested, deadline, ...}
_RESTARTING: set = set()           # 重启线程在途 (停完到再起之间也算忙, 防别人插队拉起)
_SEEN_RUNNING: dict[str, float] = {}   # 最近一次看到它在跑的时刻: 比它早的 crashed/failed 记录视为已被取代
_STATE: dict | None = None
_LOADED = False
_MCACHE: dict = {}
_SCACHE: dict = {}
_TUNNEL_CACHE: dict = {}
_CF_CACHE: dict = {}
_PRESERVED_NOTE: str | None = None
_EMIT = None
_SUP_THREAD: threading.Thread | None = None
_BOOT_THREAD: threading.Thread | None = None
_CLAIM_THREAD: threading.Thread | None = None   # 非主人: 定期再抢主人锁的线程
_STOP_EV = threading.Event()       # 测试复位时让所有后台线程收手
_WORKERS: list = []
# 最近一次真实拉起的时刻 (monotonic): 批量拉起 (开机 / 全部拉起) 的真实启动之间至少隔 _STAGGER_S。
# 用单调时钟: 开机对时把墙钟往回拨一小时, 闸门不能因此卡一小时 (只在进程内比较, 不显示不落盘)
_LAST_LAUNCH = float("-inf")
_GEN = 0                           # 失效代数: 构建状态期间有人 _invalidate, 这次结果就不进缓存 (免得旧帧再活 1.5s)
_STATE_SIG = None                  # 非主人: 上次读 state.json 时的 (mtime, size) —— 主人写了就重读
# 主人锁: 描述符一直开着 = 一直持锁, 进程退出由 OS 自动放。os.open 默认不可继承 (PEP 446), 实例子进程不会把锁带走。
_OWNER_FD: int | None = None
_OWNER_PORT: int | None = None
_OWNER_SINCE: float | None = None


def available() -> bool:
    return _HAS_PSUTIL


def set_home(path) -> None:
    """测试用: 把清单/状态/日志整体挪到别处。顺带清掉与旧目录绑定的缓存, 放掉旧目录上的主人锁。"""
    global _HOME, MANIFEST, STATE, LOGS, _STATE, _LOADED, _PRESERVED_NOTE, _STATE_SIG
    with _LOCK:
        _release_owner()
        _STATE_SIG = None
        _HOME = Path(path)
        MANIFEST = _HOME / "instances.json"
        STATE = _HOME / "state.json"
        LOGS = _HOME / "logs"
        _MCACHE.clear()
        _SCACHE.clear()
        _STATE = None
        _LOADED = False
        _PRESERVED_NOTE = None


def _default_audit(kind: str, target: str, outcome: str) -> None:
    """默认审计: 进 control 的审计 + COMMAND_ISSUED。惰性 import, 免得 instances <-> control 成环。"""
    try:
        from . import control
        control.plane.audit_action(kind, target, outcome)
    except Exception:
        pass


_audit = _default_audit


def _emit(ev: Event, emit=None) -> None:
    fn = emit or _EMIT or bus.emit
    try:
        fn(ev)
    except Exception:
        pass


def _invalidate() -> None:
    global _GEN
    with _LOCK:
        _GEN += 1
        _SCACHE.clear()


# ---- 单一主人 (OS 级独占锁) ----

def _os_lock(fd) -> bool:
    """非阻塞独占锁; 被别的进程拿着 -> False。按真实平台选实现 (测试会改 _IS_WIN, 这里不跟)。"""
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _is_owner() -> bool:
    return _OWNER_FD is not None


def claim_owner(serve_port: int | None = None) -> bool:
    """幂等。拿实例层的主人锁 (`<home>/owner.lock` 上的 OS 独占锁, 本进程活着就一直持有) 并写 owner.json {pid, port, since}。
    只有主人跑监督 / 开机拉起、接受启停与编辑、写 state.json; 拿不到 = 另一个 tokmon 在管, 本进程只读。
    -> 本进程是不是主人。"""
    global _OWNER_FD, _OWNER_PORT, _OWNER_SINCE, _STATE, _LOADED, _STATE_SIG
    with _LOCK:
        if _OWNER_FD is None:
            try:
                _ensure_dir(_HOME)
                fd = os.open(str(_HOME / "owner.lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            except OSError:
                return False
            if not _os_lock(fd):
                os.close(fd)
                return False
            _OWNER_FD, _OWNER_SINCE = fd, time.time()
            _STATE, _LOADED, _STATE_SIG = None, False, None   # 刚接管: 从盘上重读状态, 按主人的规则对账一次
            _invalidate()
        elif serve_port is None or serve_port == _OWNER_PORT:
            return True
        if serve_port is not None:
            _OWNER_PORT = serve_port
        try:
            _atomic_write_json(_HOME / "owner.json", {"pid": os.getpid(), "port": _OWNER_PORT, "since": _OWNER_SINCE})
        except OSError:
            pass                                   # 只是给别人看的名片, 写不了不影响持锁
        return True


def _release_owner() -> None:
    """放锁 (测试复位 / 换 home 用; 真实进程退出时 OS 会放)。owner.json 是自己的才删。"""
    global _OWNER_FD, _OWNER_PORT, _OWNER_SINCE
    with _LOCK:
        fd = _OWNER_FD
        if fd is None:
            return
        try:
            doc = json.loads((_HOME / "owner.json").read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("pid") == os.getpid():
                (_HOME / "owner.json").unlink()
        except (OSError, ValueError):
            pass
        _os_unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        _OWNER_FD, _OWNER_PORT, _OWNER_SINCE = None, None, None
        _invalidate()


def _pid_exists(pid) -> bool:
    try:
        return bool(psutil.pid_exists(pid)) if _HAS_PSUTIL else True
    except Exception:
        return False


def _owner_alive(pid, since) -> bool:
    """owner.json 上的 pid 还是不是写名片的那个 tokmon: 进程在, 且创建得不比名片上的接管时刻晚 (晚 = pid 被复用了)。"""
    if not _pid_exists(pid):
        return False
    if _HAS_PSUTIL and isinstance(since, (int, float)) and not isinstance(since, bool):
        try:
            if psutil.Process(pid).create_time() > since + _CT_TOL:
                return False
        except (psutil.Error, OSError, ValueError):
            pass
    return True


def _owner_block() -> dict:
    """status 顶层的 owner: 本进程是不是主人; 不是的话主人是谁 (读 owner.json, 那个 pid 还活着才算数)。
    名片上的主人已经退出 -> stale: 锁已随它的进程释放, 接管线程几秒内会拿过来 (页面据此说「正在接管」而不是「别人在管」)。"""
    if _OWNER_FD is not None:
        return {"is_owner": True, "pid": os.getpid(), "port": _OWNER_PORT}
    pid = port = None
    stale = False
    try:
        doc = json.loads((_HOME / "owner.json").read_text(encoding="utf-8"))
        p = doc.get("pid") if isinstance(doc, dict) else None
        if isinstance(p, int) and not isinstance(p, bool) and p != os.getpid():
            if _owner_alive(p, doc.get("since")):
                pid = p
                q = doc.get("port")
                port = q if isinstance(q, int) and not isinstance(q, bool) else None
            else:
                stale = True
    except (OSError, ValueError):
        pass
    out = {"is_owner": False, "pid": pid, "port": port}
    if stale:
        out["stale"] = True
    return out


def _owner_gate() -> dict | None:
    """变更动作的第一道: 主人 -> None; 否则 not-owner 回执 (说清楚该去哪个 tokmon 操作)。"""
    if _OWNER_FD is not None:
        return None
    o = _owner_block()
    if o["pid"]:
        detail = f"另一个 tokmon（pid {o['pid']} · 端口 {o['port'] or '?'}）在管理实例，请在它的页面操作"
    elif o.get("stale") and _CLAIM_THREAD is not None and _CLAIM_THREAD.is_alive():
        detail = "原来管理实例的 tokmon 已退出，这个 tokmon 正在接管，请几秒后再试"
    elif o.get("stale"):
        detail = "原来管理实例的 tokmon 已退出，请重启 tokmon serve 接管后再操作"
    else:
        detail = "这个 tokmon 没在管理实例（没拿到实例层的锁），请重启 tokmon serve 后再操作"
    return {"ok": False, "reason": "not-owner", "detail": detail}


def _spawn(target, *args) -> threading.Thread:
    t = threading.Thread(target=target, args=args, name="tokmon-instance-worker", daemon=True)
    with _LOCK:
        _WORKERS[:] = [w for w in _WORKERS if w.is_alive()]
        _WORKERS.append(t)
    t.start()
    return t


def _join_workers(timeout: float = 30.0) -> None:
    """测试用: 等所有后台动作线程收尾。"""
    end = time.time() + timeout
    while True:
        with _LOCK:
            alive = [w for w in _WORKERS if w.is_alive()]
        if not alive or time.time() > end:
            return
        alive[0].join(max(0.0, min(0.5, end - time.time())))


# =====================================================================
# 校验
# =====================================================================

def _as_int(v):
    """int / 纯数字串 -> int; 其余 (含 bool) -> None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"[0-9]{1,9}", v.strip()):
        return int(v.strip())
    return None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].rstrip("-")


def _derive_id(name: str, taken) -> str:
    """没给 id -> 从名字派生 ascii slug; 撞了加 -2/-3; 名字里没 ascii -> inst-<n>。"""
    base = _slug(name)
    if not base:
        n = 1
        while f"inst-{n}" in taken:
            n += 1
        return f"inst-{n}"
    if base not in taken:
        return base
    n = 2
    while True:
        suf = f"-{n}"
        cand = base[:40 - len(suf)].rstrip("-") + suf
        if cand not in taken:
            return cand
        n += 1


def _v_env(v, errors) -> dict:
    if v in (None, "", {}):
        return {}
    if isinstance(v, str):                         # 表单原文 KEY=VALUE 每行一条也收
        d = {}
        for line in v.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, sep, val = line.partition("=")
            if not sep:
                errors["env"] = "bad-format"
                return {}
            d[k.strip()] = val.strip()
        v = d
    if not isinstance(v, dict):
        errors["env"] = "bad-format"
        return {}
    if len(v) > 30:
        errors["env"] = "too-many"
        return {}
    out = {}
    for k, val in v.items():
        if not isinstance(k, str) or not _ENV_KEY_RE.fullmatch(k):
            errors["env"] = "bad-format"
            return {}
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            errors["env"] = "bad-format"
            return {}
        val = str(val)
        if len(val) > 1000:
            errors["env"] = "too-long"
            return {}
        if "\x00" in val:
            errors["env"] = "bad-format"
            return {}
        out[k] = val
    return out


def _v_ports(v, errors) -> list:
    if v in (None, "", []):
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[,，\s]+", v.strip()) if x]
    elif isinstance(v, int) and not isinstance(v, bool):
        v = [v]
    if not isinstance(v, list):
        errors["ports"] = "bad-port"
        return []
    if len(v) > 5:
        errors["ports"] = "too-many"
        return []
    out: list = []
    for x in v:
        n = _as_int(x)
        if n is None or not 1 <= n <= 65535:
            errors["ports"] = "bad-port"
            return []
        if n in out:
            errors["ports"] = "dup-port"
            return []
        out.append(n)
    return out


def _v_deps(v, errors) -> list:
    if v in (None, "", []):
        return []
    if isinstance(v, str):                         # 表单原文 "名字:端口" 每行一条也收
        items = []
        for line in v.replace("：", ":").splitlines():
            line = line.strip()
            if not line:
                continue
            name, sep, port = line.rpartition(":")
            if not sep:
                errors["deps"] = "bad-format"
                return []
            items.append({"name": name.strip(), "port": port.strip()})
        v = items
    if not isinstance(v, list):
        errors["deps"] = "bad-format"
        return []
    if len(v) > 5:
        errors["deps"] = "too-many"
        return []
    out = []
    for d in v:
        if not isinstance(d, dict):
            errors["deps"] = "bad-format"
            return []
        name = d.get("name")
        if not isinstance(name, str) or not name.strip():
            errors["deps"] = "required"
            return []
        name = name.strip()
        if len(name) > 40:
            errors["deps"] = "too-long"
            return []
        if any(c in name for c in "\r\n\x00"):
            errors["deps"] = "bad-format"
            return []
        port = _as_int(d.get("port"))
        if port is None or not 1 <= port <= 65535:
            errors["deps"] = "bad-port"
            return []
        out.append({"name": name, "port": port})
    return out


def validate(data, *, taken=(), check_cwd: bool = True, from_disk: bool = False):
    """-> (规范化记录 | None, {字段: 错误码})。未知键丢弃。taken = 已占用 id (只用于派生 id 时避让)。
    check_cwd=False: 读盘 / patch 时不因目录暂时不在就把整条记录判死。
    from_disk=True: 读盘时不按新规则 (无端口不许 auto) 把旧记录判死 —— 那种残留由开机拉起跳过并审计。"""
    if not isinstance(data, dict):
        return None, {"_": "bad-format"}
    errors: dict = {}

    def text(key, maxlen, *, required=False, default="", multiline=False):
        v = data.get(key)
        if v is None:
            v = default
        if not isinstance(v, str):
            errors[key] = "bad-format"
            return default
        v = v.strip()
        if required and not v:
            errors[key] = "required"
        elif len(v) > maxlen:
            errors[key] = "too-long"
        elif "\x00" in v or (not multiline and ("\r" in v or "\n" in v)):
            errors[key] = "bad-format"
        return v

    def choice(key, options, default):
        v = data.get(key)
        if v in (None, ""):
            return default
        if v not in options:
            errors[key] = "bad-choice"
            return default
        return v

    def ranged(key, lo, hi, default):
        v = data.get(key)
        if v in (None, ""):
            return default
        n = _as_int(v)
        if n is None:
            errors[key] = "bad-format"
            return default
        if not lo <= n <= hi:
            errors[key] = "range"
            return default
        return n

    name = text("name", 40, required=True)
    project = text("project", 60)
    description = text("description", 300, multiline=True)
    group = choice("group", GROUPS, "core")
    kind = choice("kind", KINDS, "service")
    service = kind == "service"
    cwd = text("cwd", 1000, required=service)
    if service and cwd and "cwd" not in errors and check_cwd and not os.path.isdir(cwd):
        errors["cwd"] = "cwd-missing"
    command = text("command", 2000, required=service)
    env = _v_env(data.get("env"), errors)
    ports = _v_ports(data.get("ports"), errors)
    open_path = text("open_path", 200, default="/")
    if "open_path" not in errors and open_path and (
            not open_path.startswith("/") or any(c.isspace() for c in open_path)):
        errors["open_path"] = "bad-format"
    deps = _v_deps(data.get("deps"), errors)
    boot = choice("boot", BOOTS, "manual")
    if service and boot != "manual" and not ports and "ports" not in errors and not from_disk:
        # 没端口认不出外部启动的副本: 开机自动拉起可能双开 (s1); 开机「提醒」也不行 —— 提醒了你, 点「全部拉起」却只能跳过它 (第三轮复查)
        errors["boot"] = "portless-auto"
    grace = ranged("stop_grace_s", 0, 120, 10)
    timeout = ranged("start_timeout_s", 5, 600, 60)
    iid = data.get("id")
    if iid in (None, ""):
        iid = _derive_id(name, set(taken))
    elif not isinstance(iid, str) or not _ID_RE.fullmatch(iid):
        errors["id"] = "bad-format"
    if errors:
        return None, errors
    return {
        "id": iid, "name": name, "project": project, "description": description,
        "group": group, "kind": kind, "cwd": cwd, "command": command, "env": env,
        "ports": ports, "open_path": open_path, "deps": deps, "boot": boot,
        "stop_grace_s": grace, "start_timeout_s": timeout,
    }, {}


# =====================================================================
# 清单 / 状态 持久化
# =====================================================================

def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)                      # POSIX 收紧 (清单里可能有 env 密钥); Windows 基本 no-op
    except OSError:
        pass


def _atomic_write_json(path: Path, obj) -> None:
    """同目录写 tmp 再 os.replace: 断电/并发读都看不到半个文件。"""
    _ensure_dir(path.parent)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for i in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:                # Windows: 目标正被别的读者打开, 稍等重试
                time.sleep(0.05 * (i + 1))
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _read_manifest_file():
    """读盘上清单 -> (records, error|None, pristine)。pristine=False = 盘上文件没被完整读懂
    (坏 JSON / 形状不对 / 有条目被丢), 下次保存前必须先把原文件另存, 绝不静默覆盖你的数据。"""
    try:
        raw = MANIFEST.read_bytes()
    except FileNotFoundError:
        return [], None, True
    except OSError as e:
        return [], f"清单文件读不了 ({e.__class__.__name__})", False
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        return [], (f"清单文件 JSON 损坏 ({e.__class__.__name__}), 暂按空清单处理; "
                    "下次保存前会把原文件另存为 instances.json.corrupt-*"), False
    items = doc.get("instances") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return [], "清单文件格式不对 (缺 instances 列表), 暂按空清单处理; 下次保存前会另存原文件", False
    recs, seen, dropped = [], set(), 0
    for it in items:
        rec, _errs = validate(it, taken=seen, check_cwd=False, from_disk=True)
        if rec is None or rec["id"] in seen:
            dropped += 1
            continue
        seen.add(rec["id"])
        recs.append(rec)
    if dropped:
        return recs, f"清单里有 {dropped} 条记录无效, 已忽略 (下次保存前原文件会另存为 instances.json.corrupt-*)", False
    return recs, None, True


def _manifest():
    """-> (records 深拷贝, manifest_error)。按 (路径, mtime, size) 缓存解析结果。"""
    try:
        st = MANIFEST.stat()
        key = (str(MANIFEST), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(MANIFEST), None, None)
    with _LOCK:
        if _MCACHE.get("key") != key:
            recs, err, pristine = _read_manifest_file()
            _MCACHE.clear()
            _MCACHE.update(key=key, recs=recs, err=err, pristine=pristine)
        return copy.deepcopy(_MCACHE["recs"]), (_MCACHE["err"] or _PRESERVED_NOTE)


def _write_manifest(recs: list, pristine: bool) -> str | None:
    """持锁调用。盘上文件没被完整读懂 -> 先改名另存再写。-> 错误说明 | None。"""
    global _PRESERVED_NOTE
    if not pristine and MANIFEST.exists():
        bak = MANIFEST.with_name(f"instances.json.corrupt-{int(time.time())}")
        n = 1
        while bak.exists():
            bak = MANIFEST.with_name(f"instances.json.corrupt-{int(time.time())}-{n}")
            n += 1
        try:
            os.replace(MANIFEST, bak)
        except OSError as e:
            return f"原清单无法另存 ({e.__class__.__name__}), 为免覆盖你的数据, 本次不保存"
        _PRESERVED_NOTE = f"原清单文件没能完整读懂, 已另存为 {bak.name}; 当前清单从能读懂的部分重新开始"
    try:
        _atomic_write_json(MANIFEST, {"version": 1, "instances": recs})
    except OSError as e:
        return f"清单写入失败 ({e.__class__.__name__})"
    _MCACHE.clear()
    return None


def load_manifest() -> list[dict]:
    return _manifest()[0]


def _get(iid) -> dict | None:
    if not isinstance(iid, str):
        return None
    for r in load_manifest():
        if r["id"] == iid:
            return r
    return None


def get_instance(iid: str) -> dict | None:
    """完整记录 (含 env 值) —— 只给本机编辑用。"""
    return _get(iid)


def _file_sig(p: Path):
    try:
        s = p.stat()
        return (s.st_mtime_ns, s.st_size)
    except OSError:
        return None


def _state() -> dict:
    """持锁调用。状态只是缓存 (追踪的 pid / 上次结局 / 开机处理), 坏了就从空开始, 不必保全。
    非主人: 主人一写 (mtime/size 变了) 就重读 —— 页面才认得出主人拉起的实例、看得到它的启停结局。"""
    global _STATE, _STATE_SIG
    if _STATE is not None and _OWNER_FD is None and _file_sig(STATE) != _STATE_SIG:
        _STATE = None
    if _STATE is None:
        _STATE_SIG = _file_sig(STATE)
        st = {"boot_handled": None, "boot_pending": [], "tracked": {}, "last": {}}
        try:
            doc = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                if isinstance(doc.get("boot_handled"), (int, float)):
                    st["boot_handled"] = float(doc["boot_handled"])
                if isinstance(doc.get("boot_pending"), list):
                    st["boot_pending"] = [x for x in doc["boot_pending"] if isinstance(x, str)]
                for k in ("tracked", "last"):
                    if isinstance(doc.get(k), dict):
                        st[k] = {i: v for i, v in doc[k].items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass
        _STATE = st
    return _STATE


def _persisted(t) -> dict:
    """追踪条目 -> 落盘形状。阶段 / 期限 / 拉起时空着的端口都存下: tokmon 重启后「启动中」的还按原期限做健康检查 (c8),
    非主人也看得到主人正在启停哪个; stop_requested 也存: 停到一半 tokmon 重启了, 之后它退出算「已停止」而不是崩溃 (R3)。"""
    return {"pid": t.get("pid"), "create_time": t.get("create_time"), "started_at": t.get("started_at"),
            "phase": t.get("phase"), "deadline": t.get("deadline") or 0.0,
            "free_at_launch": list(t.get("free_at_launch") or []), "unrecognized": bool(t.get("unrecognized")),
            "stop_requested": bool(t.get("stop_requested"))}


def _entry_from_disk(t: dict, *, owner: bool) -> dict:
    """落盘的追踪条目 -> 内存条目。主人恢复时: 「启动中」保留原期限, 其余一律按「在跑」接着监督 ——
    停到一半的那次停止已经没人接着做了, 但记着「是你要停的」(stop_requested): 它之后退出记「已停止」, 不报崩溃;
    非主人只拿来显示, 阶段照抄。"""
    phase = t.get("phase")
    ct = t.get("create_time")
    ct = float(ct) if isinstance(ct, (int, float)) and not isinstance(ct, bool) else None
    deadline = t.get("deadline")
    deadline = float(deadline) if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) else 0.0
    stop_requested = bool(t.get("stop_requested")) or phase == "stopping"      # 旧格式没存标记: 阶段是 stopping 就算
    if owner:
        if phase != "starting":
            phase = "running"
        elif deadline <= 0:
            deadline = time.time() + 60.0         # 旧格式没存期限: 给一个默认的, 别一上来就判超时
    elif phase not in ("starting", "running", "stopping", "waiting_deps"):
        phase = "running"
    pid = t.get("pid")
    return {"popen": None, "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            "create_time": ct, "started_at": float(t.get("started_at") or ct or 0.0), "phase": phase,
            "stop_requested": stop_requested, "deadline": deadline,
            "free_at_launch": [p for p in (t.get("free_at_launch") or []) if isinstance(p, int)],
            "unrecognized": bool(t.get("unrecognized"))}


def _save_state() -> None:
    """持锁调用。只有主人写 (c7/c15); tracked 以内存里的 _TRACK 为准, 每次整体重写。"""
    if _OWNER_FD is None:
        return
    if not _LOADED:
        _ensure_loaded()                           # 先把盘上的追踪收进来, 免得一写把它们冲掉
    st = _state()
    st["tracked"] = {i: _persisted(t) for i, t in _TRACK.items()}
    try:
        _atomic_write_json(STATE, st)
    except OSError:
        pass                                       # 状态写不了不致命: 内存里仍然对


def _set_last(iid, state, exit_code=None, error=None, **extra) -> None:
    """持锁调用。at 严格晚于"最近一次看到它在跑" (Windows 上 time.time 粒度约 15ms, 同一刻会被当成已取代)。"""
    at = max(time.time(), _SEEN_RUNNING.get(iid, 0.0) + 1e-3)
    _state()["last"][iid] = {"state": state, "exit_code": exit_code, "at": at, "error": error, **extra}


def _untrack(iid, entry=None) -> None:
    """持锁调用: 忘掉运行期追踪 (entry 给定时只在还是它时才删, 防误删后来者)。"""
    if entry is None or _TRACK.get(iid) is entry:
        _TRACK.pop(iid, None)
    _state()["tracked"].pop(iid, None)


def _track_snapshot() -> dict:
    """{id: 追踪条目副本}。主人: 内存里的 _TRACK; 非主人: 主人落盘的 tracked (只读, 用来认出主人拉起的实例)。"""
    with _LOCK:
        if _OWNER_FD is not None:
            return {k: dict(v) for k, v in _TRACK.items()}
        return {i: _entry_from_disk(t, owner=False) for i, t in _state()["tracked"].items() if isinstance(t, dict)}


def _supersede(ids) -> None:
    """主人才写: 看到它又在跑了, 之前的 crashed/failed 记录作废并落盘 (c11) —— 否则 tokmon 一重启旧的「意外退出」又冒出来。"""
    if not ids or _OWNER_FD is None:
        return
    with _LOCK:
        lasts = _state()["last"]
        hit = [i for i in ids if (lasts.get(i) or {}).get("state") in ("crashed", "failed")]
        for i in hit:
            lasts.pop(i, None)
        if hit:
            _save_state()


# =====================================================================
# 观测原语 (都很小, 测试可以逐个替换)
# =====================================================================

def _scan_listeners() -> list | None:
    """一次 net_connections: [(port, pid|None, ip)], 只取 LISTEN。拿不到 (权限, 如 macOS 非 root) -> None:
    「读不到」绝不能当成「没人在听」—— 否则健康的实例会被启动超时收尸 (c10)。"""
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError):
        return None
    out = []
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr:
            out.append((c.laddr.port, c.pid or None, c.laddr.ip))
    return out


def _proc_info(pid) -> dict | None:
    """{pid, name, create_time, cwd} ; 进程没了 -> None; 单项拿不到 -> 该项 None。"""
    try:
        p = psutil.Process(pid)
    except (psutil.Error, ValueError, OSError):
        return None
    info = {"pid": pid, "name": "?", "create_time": None, "cwd": None}
    try:
        info["create_time"] = p.create_time()
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError):
        pass
    try:
        info["name"] = p.name() or "?"
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError):
        pass
    try:
        info["cwd"] = p.cwd() or None
    except (psutil.Error, OSError):
        pass
    return info


def _descendants(pid) -> list:
    try:
        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except (psutil.Error, OSError, ValueError):
        return []


def _ppid(pid) -> int | None:
    """父进程 pid。用 psutil parent(): 它会核对「父进程创建得比它还晚 = 原父进程早没了、pid 被复用」-> None,
    免得把一个无关的 cmd.exe 当成祖先、挂到它的控制台上发 Ctrl+C。"""
    try:
        p = psutil.Process(pid).parent()
    except (psutil.Error, OSError, ValueError):
        return None
    return p.pid if p is not None else None


def _cmdline(pid) -> list:
    try:
        return psutil.Process(pid).cmdline() or []
    except (psutil.Error, OSError, ValueError):
        return []


def _proc_listen_ports(pid) -> set:
    """单个进程自己的 LISTEN 端口: 全表读不到时的退路 (问自己的进程不需要额外权限)。"""
    try:
        p = psutil.Process(pid)
        fn = getattr(p, "net_connections", None) or p.connections
        return {c.laddr.port for c in fn(kind="inet") if c.status == psutil.CONN_LISTEN and c.laddr}
    except (psutil.Error, OSError, ValueError, AttributeError):
        return set()


_PROBE_CACHE: dict = {}      # port -> (探测时刻 monotonic, 结果)
_PROBE_TTL_S = 15.0          # 页面 / 铃铛几秒一轮询: 别每轮都去跟依赖握一次手 (第三轮复查)


def _loopback_listening(port, fresh: bool = False) -> bool:
    """端口表读不到时判断依赖是否就绪的退路: 对本机回环试连 (127.0.0.1, 再 ::1), 连上即关、不发任何数据。
    只连本机 —— 零外发; 不需要任何权限 (macOS 非 root 也行)。结果缓存 _PROBE_TTL_S 秒。"""
    hit = None if fresh else _PROBE_CACHE.get(port)
    if hit and time.monotonic() - hit[0] < _PROBE_TTL_S:
        return hit[1]
    up = False
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(_PROBE_TIMEOUT_S)
                if s.connect_ex((host, port)) == 0:
                    up = True
                    break
        except OSError:
            continue
    _PROBE_CACHE[port] = (time.monotonic(), up)
    return up


def _proc_handle(pid):
    """拿一个 psutil.Process 句柄 (构造时即锁定 create_time, 之后 is_running/kill 都会复核身份)。"""
    try:
        return psutil.Process(pid)
    except (psutil.Error, OSError, ValueError):
        return None


def _norm(p) -> str | None:
    return os.path.normcase(os.path.abspath(p)) if p else None


def _under(child, parent) -> bool:
    if not child or not parent:
        return False
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


class _Frame:
    """一次状态计算用的一帧观测: 一次 LISTEN 扫描 + 按需 memo 的进程信息 / 子孙 / 命令行,
    以及「各实例正在追踪的进程树」(认归属时要排除别人的进程) 和清单 (认不准时找同端口同目录的对手)。
    unknown = 这次端口表读不到: 谁在听一概不能断言。"""

    def __init__(self, track: dict | None = None, recs: list | None = None, cached_probe: bool = False):
        self.cached_probe = cached_probe            # 只有给页面看的状态帧能用回环试连的缓存; 要据此启动 / 等依赖的一律现试
        raw = _scan_listeners() if _HAS_PSUTIL else []
        self.unknown = raw is None
        self.raw = raw or []
        self.by_port: dict[int, list] = {}
        for port, pid, _ip in self.raw:
            lst = self.by_port.setdefault(port, [])
            if pid not in lst:                     # IPv4/IPv6 双栈同一进程只记一次
                lst.append(pid)
        self._info: dict = {}
        self._desc: dict = {}
        self._cmd: dict = {}
        self._plisten: dict = {}
        self._up: dict = {}
        self._track = track
        self._recs = recs
        self._trees: dict | None = None
        self._owner_pid: list | None = None

    def owners(self, port) -> list:
        return self.by_port.get(port, [])

    def info(self, pid) -> dict | None:
        if pid not in self._info:
            self._info[pid] = _proc_info(pid) if pid else None
        return self._info[pid]

    def descendants(self, pid) -> list:
        if pid not in self._desc:
            self._desc[pid] = _descendants(pid)
        return self._desc[pid]

    def cmdline(self, pid) -> list:
        if pid not in self._cmd:
            self._cmd[pid] = _cmdline(pid) if pid else []
        return self._cmd[pid]

    def listens(self, pid) -> set:
        if pid not in self._plisten:
            self._plisten[pid] = _proc_listen_ports(pid) if pid else set()
        return self._plisten[pid]

    def port_up(self, port) -> bool:
        """依赖端口有没有人在听。全表读不到时「读不到」绝不当「没就绪」(c10): 先对本机回环试连,
        连不上再逐个问各实例正在追踪的树 (tokmon 自己拉起的依赖, 页面上显示运行中, 这里就不能说它没就绪)。"""
        if not self.unknown:
            return port in self.by_port
        if port not in self._up:
            self._up[port] = _loopback_listening(port, fresh=not self.cached_probe) or any(
                port in self.listens(p) for tree in self.trees().values() for p in tree)
        return self._up[port]

    def trees(self) -> dict:
        """{id: 该实例正在追踪、根还活着的进程树}。"""
        if self._trees is None:
            track = self._track if self._track is not None else _track_snapshot()
            self._trees = {i: _tracked_tree(t, self) for i, t in track.items() if t and t.get("pid")}
        return self._trees

    def recs(self) -> list:
        if self._recs is None:
            self._recs = load_manifest()
        return self._recs

    def owner_pid(self) -> int | None:
        """非主人时: 管理实例的那个 tokmon 的 pid (它监听的也是「本服务」, 不是端口冲突)。"""
        if self._owner_pid is None:
            self._owner_pid = [None if _OWNER_FD is not None else _owner_block()["pid"]]
        return self._owner_pid[0]


def _alive(pid, ct, fr: _Frame | None = None) -> bool:
    if not pid or ct is None:
        return False
    info = fr.info(pid) if fr else _proc_info(pid)
    return bool(info) and info.get("create_time") is not None and abs(info["create_time"] - ct) <= _CT_TOL


def _root_alive(tr, fr: _Frame | None = None) -> bool:
    if not tr or not tr.get("pid"):
        return False
    p = tr.get("popen")
    if p is not None and p.poll() is not None:
        return False
    return _alive(tr["pid"], tr.get("create_time"), fr)


def _tracked_tree(tr, fr: _Frame) -> set:
    """tokmon 启动的实例的进程树 (根 + 子孙); 根已死 / 身份对不上 -> 空集。"""
    if not _root_alive(tr, fr):
        return set()
    return {tr["pid"], *fr.descendants(tr["pid"])}


def _eff_ports(rec, serve_port=None) -> list:
    """实际要看的端口: 本服务没声明端口 -> 用实际服务端口 (状态 / 隧道 / 打开链接 同一条规则, c13)。"""
    ports = list(rec["ports"])
    if rec["kind"] == "self" and not ports and serve_port:
        ports = [serve_port]
    return ports


def _tokens(s) -> list:
    """命令 / 命令行参数 -> 归一化的词: 去引号、取路径末段、去 .exe/.cmd/.bat、不分大小写。
    cmd /c "…" 的整串也拆开, 这样 .bat 起的 cmd 祖先的命令行也能拿来比。"""
    out = []
    for w in re.split(r'[\s"]+', s or ""):
        w = w.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
        for ext in (".exe", ".cmd", ".bat"):
            if w.endswith(ext):
                w = w[:-len(ext)]
        if w:
            out.append(w)
    return out


def _tiebreak(pid, cands, fr: _Frame):
    """同一端口、同一目录下几个实例都像这个进程: 由近及远 (它自己的命令行, 再父进程 … 至多 4 层祖先) 逐层拿命令行,
    跟各实例登记命令里「只有它有」的特有词比, 停在第一个分得开的层 (各实例 对上的 - 没对上的 不全相等)。
    那一层要判给某个实例: 它得分唯一最高、至少对上一个特有词, 其余每个都至少缺一个;
    没有特有词的那个 (命令是对手的子集) 只在近两层 (自己 / 父进程) 算输 —— 远处祖先 (终端 / IDE / 包装脚本) 的命令行
    什么词都可能带着, 光凭它做「0 比正」的判决会把别的变体的进程认成自己, 一点停止就杀错 (c6)。
    分不出 / 判不了 -> None (宁可谁都不认, 也不认错)。"""
    sets = {r["id"]: set(_tokens(r["command"])) for r in cands}
    common = set.intersection(*sets.values())
    own = {i: s - common for i, s in sets.items()}
    cur = pid
    for level in range(5):
        if not cur:
            break
        toks: set = set()
        for a in fr.cmdline(cur):
            toks.update(_tokens(a))
        if toks:                                   # 读不到命令行的一层不算证据
            hit = {i: len(d & toks) for i, d in own.items()}
            miss = {i: len(d - toks) for i, d in own.items()}
            score = {i: hit[i] - miss[i] for i in own}
            # 这一层要有证据才在这层定: 对上了某个特有词, 或者它的命令行就是那条命令 (含全部共有词, 缺的特有词才有意义)。
            # 光是「缺的词个数不同」不算 —— run_a.bat vs run_b.bat --safe, 两边都不沾的 python 进程本身说明不了什么, 该往上看 .bat 那层 (第三轮复查)。
            evidence = any(hit.values()) or bool(common and common <= toks)
            if evidence and len(set(score.values())) > 1:
                best = max(score.values())
                top = [i for i, v in score.items() if v == best]
                if len(top) != 1:
                    return None
                win = top[0]
                losers_out = all(miss[i] >= 1 or (level <= 1 and not own[i]) for i in own if i != win)
                if not (hit[win] >= 1 and losers_out):
                    return None
                if miss[win] >= 1 and _next_level_names_rival(cur, win, own, fr):
                    return None                    # 赢家自己的特有词也缺一个, 而上一层明说是别人 (cmd /c run_a.bat): 认不准, 不认错 (终轮静态复查)
                return win
        cur = _ppid(cur)
    return None


def _next_level_names_rival(pid, win, own, fr: "_Frame") -> bool:
    """往上找第一层读得到的命令行: 那里有没有别的实例的特有词 (赢家自己没对上的)。"""
    cur = _ppid(pid)
    for _ in range(4):
        if not cur:
            return False
        toks: set = set()
        for a in fr.cmdline(cur):
            toks.update(_tokens(a))
        if toks:
            return any(own[i] & toks and not (own[win] & toks) for i in own if i != win)
        cur = _ppid(cur)
    return False


def _claim(rec, port, pid, tree, tr, fr: _Frame):
    """这个监听者是不是「它」-> (kind, 同样像它的别的实例 id)。kind:
    mine = 它的进程; proxy = 代理进程替它听着 (算在服务, 绝不杀); other = 别的实例正在追踪的进程;
    ambiguous = 按目录好几个实例都像它、命令行也分不出; no = 不是它。"""
    if not pid:
        return "no", []
    if rec["kind"] == "self":
        return ("mine" if pid == os.getpid() or pid == fr.owner_pid() else "no"), []
    if pid == os.getpid():
        return "no", []                            # 服务型实例绝不把 tokmon 自己认作自己 (否则"停止"会杀掉本服务)
    if pid in tree:
        return "mine", []
    for oid, otree in fr.trees().items():
        if oid != rec["id"] and pid in otree:
            return "other", [oid]                  # 别的实例拉起的进程: 目录再像也不是这个 (c6)
    info = fr.info(pid) or {}
    if (tree and port in ((tr or {}).get("free_at_launch") or [])
            and (info.get("name") or "").casefold() in _PROXY_NAMES):
        return "proxy", []
    cwd = _norm(info.get("cwd"))
    if not _under(cwd, _norm(rec.get("cwd"))):
        return "no", []
    rivals = [r for r in fr.recs() if r["id"] != rec["id"] and r["kind"] == "service"
              and port in r["ports"] and _under(cwd, _norm(r.get("cwd")))]
    if not rivals:
        return "mine", []
    win = _tiebreak(pid, [rec] + rivals, fr)
    if win is None:
        return "ambiguous", [r["id"] for r in rivals]
    return ("mine" if win == rec["id"] else "no"), []


def _evaluate(rec, fr: _Frame, tr, last, now, serve_port=None) -> dict:
    """单个实例的识别 + 状态裁决 (§3.2 + 修正)。纯基于这一帧观测, 不写任何东西 (_SEEN_RUNNING 除外)。
    状态照实: 进程在但端口没在听 -> degraded; 端口表读不到 -> unknown; 归属认不准 -> 谁都不算在跑。"""
    iid = rec["id"]
    ports = _eff_ports(rec, serve_port)
    tree = _tracked_tree(tr, fr) if tr else set()
    listening, conflicts, mine_pids, proxied, foreign = [], [], [], [], []
    for port in ports:
        owners = fr.owners(port)
        if fr.unknown and tree:                    # 全表读不到: 至少把自己追踪的树逐个问一遍
            owners = [p for p in sorted(tree) if port in fr.listens(p)]
        for pid in owners:
            info = fr.info(pid) if pid else None
            name = (info or {}).get("name") or "?"
            kind, others = _claim(rec, port, pid, tree, tr, fr)
            listening.append({"port": port, "pid": pid, "name": name, "mine": kind in ("mine", "proxy")})
            if kind == "mine":
                if pid not in mine_pids:
                    mine_pids.append(pid)          # 按声明端口顺序: 第一个 = 主端口的 owner
                continue
            if kind == "proxy":
                proxied.append({"port": port, "pid": pid, "name": name})
                continue
            by = others[0] if kind == "other" and others else None
            foreign.append({"port": port, "pid": pid, "name": name, "kind": kind, "by": by})
            if kind == "ambiguous":
                conflicts.append({"kind": "ambiguous", "port": port, "owner": {"pid": pid, "name": name},
                                  "with": others})
            else:
                c = {"kind": "port-busy", "port": port, "owner": {"pid": pid, "name": name}}
                if by:
                    c["by"] = by                   # 占着的是另一个登记实例 (tokmon 拉起的): 说是哪个, 比只报进程名有用
                conflicts.append(c)
    primary = ports[0] if ports else None
    primary_foreign = [f for f in foreign if f["port"] == primary]
    phase = tr.get("phase") if tr else None
    tracked_alive = bool(tree)
    state, started_by, pid, uptime = "stopped", None, None, None
    note = error = label = None

    def _uptime_of(p):
        info = fr.info(p)
        ct = (info or {}).get("create_time")
        return max(0.0, now - ct) if ct else None

    if proxied and (tracked_alive or phase in _BUSY_PHASES):
        note = f"端口由 {proxied[0]['name']} 代理"
    if rec["kind"] == "self":
        state, pid = "self", (mine_pids[0] if mine_pids else os.getpid())
        uptime = _uptime_of(pid)
    elif phase in _BUSY_PHASES:
        state = phase
        if tracked_alive:
            started_by, pid = "tokmon", (mine_pids[0] if mine_pids else tr["pid"])
            uptime = max(0.0, now - tr["started_at"])
        elif mine_pids:
            started_by, pid = "external", mine_pids[0]
            uptime = _uptime_of(pid)
    elif mine_pids:
        state, pid = "running", mine_pids[0]
        in_tree = [p for p in mine_pids if p in tree]
        if in_tree:
            started_by, pid = "tokmon", in_tree[0]
            uptime = max(0.0, now - tr["started_at"])
        else:
            started_by = "external"
            uptime = _uptime_of(pid)
    elif tracked_alive:
        # 我们拉起的进程 (create_time 已核) 还活着, 但声明端口上没有「它」: 不假装在服务 (c8)
        started_by, pid = "tokmon", tr["pid"]
        uptime = max(0.0, now - tr["started_at"])
        if not ports or proxied:
            state = "running"                      # 无端口服务: 进程在就是在跑; 有代理替它听着: 在服务
        elif fr.unknown:
            state = "unknown"
        elif primary_foreign and not tr.get("unrecognized"):
            state = "port_busy"
        else:
            state = "degraded"
            if primary_foreign:                    # 启动超时那刻端口其实在听, 只是持有者认不出 (c9): 不杀, 如实说
                label = "进程在 · 端口持有者认不出"
                error = f"端口在监听，但持有者 {primary_foreign[0]['name']} 认不出是它"
    elif fr.unknown and ports:
        state = "unknown"
    elif primary_foreign:
        state = "port_busy"
    elif last and last.get("state") in ("crashed", "failed") and (last.get("at") or 0) > _SEEN_RUNNING.get(iid, 0):
        state = last["state"]
        if not ports:                              # 崩了之后你可能从别处又起了它, 无端口认不出: 同样别说得太死
            label = _LABELS[state] + _PORTLESS_HEDGE
    elif not ports:
        label = _PORTLESS_STOPPED_LABEL            # 无端口又不是 tokmon 拉起的: 外部在跑也认不出, 别说得太死 (s1)
    if state == "running":
        _SEEN_RUNNING[iid] = now
    deps = [{"name": d["name"], "port": d["port"], "ready": fr.port_up(d["port"])} for d in rec["deps"]]
    po = primary_foreign[0] if primary_foreign else None
    return {
        "state": state, "started_by": started_by, "pid": pid, "uptime_s": uptime,
        "listening": listening, "conflicts": conflicts, "mine_pids": mine_pids, "tree": tree,
        "proxied": proxied, "foreign": foreign, "unknown": fr.unknown,
        "deps": deps, "deps_ready": all(d["ready"] for d in deps), "note": note, "error": error, "label": label,
        "primary_owner": {"pid": po["pid"], "name": po["name"]} if po else None,
        "primary_by": po["by"] if po else None,
        "primary_ambiguous": [f for f in primary_foreign if f["kind"] == "ambiguous"],
    }


# =====================================================================
# cloudflared quick tunnel URL (只问本机 metrics, 零外发)
# =====================================================================

def _cloudflared_procs() -> list:
    out = []
    for p in psutil.process_iter(["name"]):
        try:
            if "cloudflared" not in (p.info.get("name") or "").lower():
                continue
            out.append((p.pid, p.cmdline()))
        except (psutil.Error, OSError):
            continue
    return out


def _http_get_json(host: str, port: int, path: str, timeout: float = 0.5):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)   # 直连 loopback, 不走任何代理
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        if r.status != 200:
            return None
        return json.loads(r.read(65536).decode("utf-8", "replace"))
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _arg(cl: list, flag: str):
    for i, a in enumerate(cl):
        if a == flag and i + 1 < len(cl):
            return cl[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return None


def _loopback_host(ip: str) -> str | None:
    """metrics 地址 -> 可以安全去问的本机地址; 非本机地址 -> None (绝不外连)。"""
    ip = (ip or "").strip().strip("[]").lower()
    if ip in ("", "0.0.0.0", "localhost") or ip.startswith("127."):
        return "127.0.0.1"
    if ip in ("::", "::1"):
        return "::1"
    return None


def _parse_cf(cl: list):
    """-> (隧道指向的本机端口 | None, metrics (host, port) | None)。"""
    url = _arg(cl, "--url")
    tport = None
    if url:
        u = url if "://" in url else "http://" + url
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://(\[[^\]]*\]|[^/:?#]*)(?::([0-9]{1,5}))?", u)
        if m:
            if m.group(2):
                tport = int(m.group(2))
            else:
                tport = 443 if u.lower().startswith("https://") else 80
    met = _arg(cl, "--metrics")
    metrics = None
    if met:
        host, _, port = met.rpartition(":")
        pn = _as_int(port)
        h = _loopback_host(host)
        if pn and h:
            metrics = (h, pn)
    return tport, metrics


def _quick_hostname(pid: int, host: str, mport: int):
    key = (pid, mport)
    now = time.time()
    c = _TUNNEL_CACHE.get(key)
    if c and now - c[0] < _TUNNEL_TTL:
        return c[1]
    doc = _http_get_json(host, mport, "/quicktunnel")
    hn = doc.get("hostname") if isinstance(doc, dict) else None
    hn = hn.strip() if isinstance(hn, str) else ""
    hn = hn if hn and _HOST_RE.fullmatch(hn) else None     # 空 = 命名隧道; 怪字符不往页面上放
    if len(_TUNNEL_CACHE) > 64:                            # cloudflared 反复重启会攒下旧键: 顺手清过期的
        for k in [k for k, v in list(_TUNNEL_CACHE.items()) if now - v[0] >= _TUNNEL_TTL]:
            _TUNNEL_CACHE.pop(k, None)
    _TUNNEL_CACHE[key] = (now, hn)
    return hn


def _tunnels(fr: _Frame, wanted: set) -> dict:
    """{隧道指向的端口: {url, kind, pid}}。只对指向实例端口的 cloudflared 去问 metrics。"""
    if not wanted:
        return {}
    now = time.time()
    c = _CF_CACHE.get("procs")
    if c and now - c[0] < _CF_TTL:
        procs = c[1]
    else:
        procs = _cloudflared_procs()
        _CF_CACHE["procs"] = (now, procs)
    out: dict = {}
    for pid, cl in procs:
        tport, metrics = _parse_cf(cl or [])
        if tport not in wanted:
            continue
        if metrics:
            cands = [metrics]
        else:
            cands = []
            for port, lpid, ip in fr.raw:
                h = _loopback_host(ip)
                if lpid == pid and h and (h, port) not in cands:
                    cands.append((h, port))
        hn = None
        for h, mp in cands:
            hn = _quick_hostname(pid, h, mp)
            if hn:
                break
        cur = out.get(tport)
        if cur is None or (hn and not cur["url"]):
            out[tport] = {"url": f"https://{hn}" if hn else None, "kind": "quick" if hn else "named", "pid": pid}
    return out


# =====================================================================
# 状态
# =====================================================================

def _ensure_loaded() -> None:
    """主人首次用时读状态并对账 tracked: tokmon 重启过而实例还活着 -> 仍算 tokmon 启动的 (阶段 / 期限照旧);
    tokmon 不在时没了 -> 丢掉, 记「未运行 + 原因未知」—— 绝不记成崩溃、不发事件: 关机 / 注销 / Fast Startup
    都会这样 (开机时间还可能不变), 只有监督线程亲眼看见的死亡才算崩溃 (c5/c14/c22)。非主人不对账、不写。"""
    global _LOADED
    with _LOCK:
        if _LOADED or _OWNER_FD is None:
            return
        _LOADED = True
        st = _state()
        if not _HAS_PSUTIL:
            return
        names = {r["id"] for r in load_manifest()}
        changed = False
        for iid, t in list(st["tracked"].items()):
            e = _entry_from_disk(t, owner=True) if isinstance(t, dict) else None
            if e and e["pid"] and e["create_time"] is not None and _alive(e["pid"], e["create_time"]):
                if iid not in _TRACK:
                    _TRACK[iid] = e
                continue
            del st["tracked"][iid]
            changed = True
            if e and e["pid"] and iid in names:    # 等依赖的条目 (还没进程) 直接丢, 无须说明
                if e["stop_requested"]:            # 你点了停止、没等停完 tokmon 就退了: 回来时它已经没了 = 那次停止成了
                    _set_last(iid, "stopped", None, None, outcome="gone")
                else:
                    _set_last(iid, "stopped", None, _GONE_WHILE_AWAY, outcome="gone-while-away")
        if changed:
            _save_state()


def _instance_view(rec, ev, last, extra) -> dict:
    ports = rec["ports"]
    eff = extra.get("ports") or ports
    seen = _SEEN_RUNNING.get(rec["id"], 0)
    show_last = bool(last) and (
        ((last.get("at") or 0) > seen and ev["state"] != "running")
        # 停止没停干净 (杀了又被外面拉起来): 它为什么还在跑, 就挂在它身上说
        or (last.get("outcome") == "incomplete" and ev["state"] == "running"))
    return {
        "id": rec["id"], "name": rec["name"], "project": rec["project"], "description": rec["description"],
        "group": rec["group"], "kind": rec["kind"], "boot": rec["boot"], "ports": list(ports),
        "open_path": rec["open_path"],
        "open_url": f"http://localhost:{eff[0]}{rec['open_path']}" if eff else None,
        "state": ev["state"], "state_label": extra.get("label") or ev.get("label") or _LABELS[ev["state"]],
        "started_by": ev["started_by"], "pid": ev["pid"],
        "uptime_s": round(ev["uptime_s"], 1) if ev["uptime_s"] is not None else None,
        "listening": ev["listening"], "deps": ev["deps"], "deps_ready": ev["deps_ready"],
        "tunnel": extra.get("tunnel"), "conflicts": extra.get("shared", []) + ev["conflicts"],
        "exit_code": last.get("exit_code") if show_last else None,
        "last_error": ev.get("error") or (last.get("error") if show_last else None),
        "last_at": last.get("at") if show_last else None,
        "note": ev.get("note"),
        "stop_grace_s": rec["stop_grace_s"],
        "command_display": procmon._redact(rec["command"]), "cwd": rec["cwd"], "env_keys": sorted(rec["env"]),
        "boot_pending": extra.get("pending", False),
        "can_control": rec["kind"] == "service",
        "log_exists": (LOGS / f"{rec['id']}.log").exists(),
    }


def _build_status(serve_port=None) -> dict:
    _ensure_loaded()
    recs, merr = _manifest()
    now = time.time()
    track = _track_snapshot()
    with _LOCK:
        st = _state()
        lasts = copy.deepcopy(st["last"])
        pending = list(st["boot_pending"])
        handled = st["boot_handled"]
    fr = _Frame(track=track, recs=recs, cached_probe=True) if _HAS_PSUTIL else None
    users: dict = {}
    for r in recs:
        for p in r["ports"]:
            users.setdefault(p, []).append(r["id"])
    eff = {r["id"]: _eff_ports(r, serve_port) for r in recs}
    tunnels = {}
    if fr is not None:
        try:
            tunnels = _tunnels(fr, {p for ps in eff.values() for p in ps})
        except Exception:
            tunnels = {}                           # 隧道只是锦上添花, 出错不拖垮状态
    out, live_pending, seen_running = [], [], []
    for r in recs:
        last = lasts.get(r["id"]) or {}
        if fr is None:
            ev = {"state": "stopped", "started_by": None, "pid": None, "uptime_s": None, "listening": [],
                  "conflicts": [], "deps": [{"name": d["name"], "port": d["port"], "ready": False} for d in r["deps"]],
                  "deps_ready": not r["deps"], "note": None}
            extra = {"label": "状态未知 (缺 psutil)"}
        else:
            ev = _evaluate(r, fr, track.get(r["id"]), last, now, serve_port)
            extra = {}
            if ev["state"] == "running" and last.get("state") in ("crashed", "failed"):
                seen_running.append(r["id"])
        shared = [{"kind": "shared-port", "port": p, "with": other}
                  for p in r["ports"] for other in users.get(p, []) if other != r["id"]]
        tun = None
        for p in eff[r["id"]]:
            if p in tunnels:
                tun = dict(tunnels[p])
                break
        is_pending = (r["id"] in pending and r["kind"] == "service"
                      and ev["state"] not in ("running", "starting", "stopping", "waiting_deps"))
        if is_pending:
            live_pending.append(r["id"])
        extra.update(shared=shared, tunnel=tun, pending=is_pending, ports=eff[r["id"]])
        out.append(_instance_view(r, ev, last, extra))
    _supersede(seen_running)
    return {
        "ok": True, "available": _HAS_PSUTIL, "generated_at": now,
        "manifest_error": merr,
        "boot": {"handled": handled, "pending": live_pending},
        "owner": _owner_block(),
        "instances": out,
    }


def status_all(serve_port: int | None = None) -> dict:
    now = time.time()
    with _LOCK:
        c = _SCACHE.get(serve_port)
        if c and now - c[0] < _STATUS_TTL:
            return dict(c[1])                      # 浅拷贝: serve 会往顶层塞 autostart/local, 别污染共享帧
        gen = _GEN
    res = _build_status(serve_port)
    with _LOCK:
        if _GEN == gen:                            # 构建期间有启停 / 编辑 (c12): 这一帧可能是动作之前的, 不缓存
            _SCACHE[serve_port] = (time.time(), res)
    return dict(res)


# =====================================================================
# 启动
# =====================================================================

def _fail(reason, detail=None, owner=None, deps=None) -> dict:
    return {"ok": False, "reason": reason, "detail": detail, "owner": owner, "deps": deps}


def _comspec() -> str:
    cs = os.environ.get("ComSpec") or ""
    if cs.lower().endswith("cmd.exe") and os.path.isfile(cs):
        return f'"{cs}"'                            # 用全路径: 免得按当前目录搜到同名 cmd.exe
    return "cmd.exe"


# tokmon 可能是在某个 Claude Code 会话里起的: 那个会话的变量不该漏进长期跑的服务 (会话结束了它们还留着);
# NoDefaultCurrentDirectoryInExePath 会让 cmd 不在 cwd 里找程序, 登记成 `run.bat` 的实例直接"不是内部或外部命令"。
# 只清继承来的 —— 实例自己的 env 里显式写了照样生效。
_ENV_DROP = ("NODEFAULTCURRENTDIRECTORYINEXEPATH",)
_ENV_DROP_PREFIX = ("CLAUDECODE", "CLAUDE_CODE_", "MC_REMOTE")


def _inherited_ok(key: str) -> bool:
    k = key.upper()                                # Windows 环境变量不分大小写
    return k not in _ENV_DROP and not k.startswith(_ENV_DROP_PREFIX)


def _child_env(rec) -> dict:
    env = {k: v for k, v in os.environ.items() if _inherited_ok(k)}
    env.setdefault("PYTHONUNBUFFERED", "1")        # 日志即时落盘 (你的环境变量优先)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for k, v in rec["env"].items():
        if _IS_WIN:                                # Windows 环境变量不分大小写: 先删同名异写, 免得块里出现两份
            for old in [x for x in env if x.upper() == k.upper()]:
                del env[old]
        env[k] = v
    return env


def _popen_windows(rec, env, logf):
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)   # 清掉继承来的"忽略 Ctrl+C", 否则子进程收不到 (实测)
    except Exception:
        pass
    cmdline = f'{_comspec()} /d /s /c "{rec["command"]}"'           # 必须传字符串: 传 list 内层引号会被转义 (实测)
    kw = dict(cwd=rec["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, close_fds=True)
    base = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen(cmdline, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, **kw)   # 脱离 job: tokmon 退出不带走实例
    except OSError:
        return subprocess.Popen(cmdline, creationflags=base, **kw)   # job 不许脱离 -> 退一步照常起


def _launch(rec, actor) -> dict:
    """真正拉起: 后台、无窗口, stdout+stderr -> logs/<id>.log。-> {popen, pid, create_time}; 失败抛 OSError。"""
    _ensure_dir(LOGS)
    logp = LOGS / f"{rec['id']}.log"
    try:
        if logp.stat().st_size > _LOG_ROTATE_BYTES:
            os.replace(logp, logp.with_name(logp.name + ".1"))
    except OSError:
        pass
    env = _child_env(rec)
    logf = open(logp, "ab")
    try:
        header = (f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 由 tokmon 启动 ({actor}) · "
                  f"{procmon._redact(rec['command'])} ===\n")
        logf.write(header.encode("utf-8"))
        logf.flush()
        if _IS_WIN:
            popen = _popen_windows(rec, env, logf)
        else:
            popen = subprocess.Popen(["/bin/sh", "-c", rec["command"]], cwd=rec["cwd"], env=env,
                                     stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                     close_fds=True, start_new_session=True)
    finally:
        logf.close()                               # 子进程有自己继承的句柄; 我们这份立刻关 (占着会挡日志轮转)
    try:
        ct = psutil.Process(popen.pid).create_time()
    except (psutil.Error, OSError):
        ct = time.time()
    return {"popen": popen, "pid": popen.pid, "create_time": ct}


def _do_launch(rec, actor, free=()) -> dict:
    """持锁调用。free = 拉起前这一帧里空着的声明端口 (之后被代理进程听上了, 才敢算作它在服务)。"""
    global _LAST_LAUNCH
    iid = rec["id"]
    try:
        h = _launch(rec, actor)
    except OSError as e:
        _set_last(iid, "failed", None, f"启动失败: {e}"[:300])
        _save_state()
        _invalidate()
        return _fail("launch-failed", detail=str(e)[:300])
    now = time.time()
    _LAST_LAUNCH = time.monotonic()                # 任何真实拉起都推后批量拉起的下一个名额
    _TRACK[iid] = {"popen": h["popen"], "pid": h["pid"], "create_time": h["create_time"], "started_at": now,
                   "phase": "starting", "stop_requested": False, "deadline": now + rec["start_timeout_s"],
                   "actor": actor, "free_at_launch": list(free)}
    _state()["last"].pop(iid, None)
    _save_state()
    _invalidate()
    return {"ok": True, "state": "starting", "pid": h["pid"]}


def _start(iid, *, force, wait_deps, actor, internal, stagger=False, portless_ok=False) -> dict:
    """stagger=True: 批量拉起 (开机 / 全部拉起) 用 —— 离上一次真实拉起不足 _STAGGER_S 就先不拉,
    回 {"ok":False,"reason":"stagger","wait":秒} 让调用方等一会再来 (只在内部流转, 不会回给页面)。
    闸门检查与拉起在同一把锁里, 两个批量线程不会同一刻都过闸。
    无端口、tokmon 也没在追踪的实例: 认不出它是不是已经在别处跑着, 没有 force (你确认过) 就先问 (s1);
    portless_ok = 重启里刚停掉的正是 tokmon 追踪的那份, 再起一份不是双开。"""
    if not _HAS_PSUTIL:
        return _fail("no-psutil")
    _ensure_loaded()
    with _LOCK:
        rec = _get(iid)
        if rec is None:
            return _fail("not-found")
        if rec["kind"] == "self":
            return _fail("self")
        tr = _TRACK.get(iid)
        if (tr and tr.get("phase") in _BUSY_PHASES) or (iid in _RESTARTING and not internal):
            return _fail("busy")
        fr = _Frame()
        ev = _evaluate(rec, fr, tr, None, time.time())
        if ev["state"] == "running":
            return _fail("already-running", owner=({"pid": ev["pid"], "name": (fr.info(ev["pid"]) or {}).get("name") or "?"}
                                                   if ev["pid"] else None))
        if ev["state"] == "degraded":              # 进程还在: 再起一份只会抢端口 / 双开 (c8)
            return _fail("degraded", detail="进程还在但端口没在监听，请用「重启」")
        if ev["state"] == "unknown" and ev["tree"]:
            return _fail("unknown", detail="读不到端口表，它拉起的进程还在，没法确认要不要再起一份")
        if ev["primary_ambiguous"]:
            a = ev["primary_ambiguous"][0]
            return _fail("ambiguous", owner={"pid": a["pid"], "name": a["name"]},
                         detail=f"端口 {rec['ports'][0]} 上的 {a['name']} (pid {a['pid']}) 同时像好几个实例，认不准")
        if ev["primary_owner"]:                    # 端口被别人占着: 只报告, 绝不替你杀
            o = ev["primary_owner"]
            by = ev["primary_by"]
            if by:                                 # 占着的是另一个登记实例: 说实例名, 比进程名好认
                other = next((r for r in fr.recs() if r["id"] == by), None)
                detail = f"端口 {rec['ports'][0]} 被实例「{other['name'] if other else by}」占用"
            else:
                detail = f"端口 {rec['ports'][0]} 被 {o['name']} (pid {o['pid']}) 占用"
            r = _fail("port-busy", detail=detail, owner=o)
            if by:
                r["by"] = by                       # 页面先认回执里的 by: 卡片上的冲突还没刷新也说得出是哪个实例
            return r
        if not rec["ports"] and not ev["tree"] and not (force or portless_ok):
            return _fail("portless-confirm", detail=_PORTLESS_CONFIRM)
        if not ev["deps_ready"]:
            if wait_deps:
                now = time.time()
                cancel = threading.Event()
                if _STOP_EV.is_set():
                    cancel.set()                   # 复位 / 退出进行中才来的等待: 生下来就是取消的, 绝不留到下一个 home
                entry = {"popen": None, "pid": None, "create_time": None, "started_at": now,
                         "phase": "waiting_deps", "stop_requested": False, "deadline": now + _DEPS_WAIT_S,
                         "cancel": cancel, "actor": actor, "stagger": stagger}
                _TRACK[iid] = entry
                _save_state()                      # 非主人的页面也看得到「等依赖就绪」
                _invalidate()
                _spawn(_wait_deps_then_launch, iid, entry, actor, _STOP_EV)
                return {"ok": True, "state": "waiting_deps", "pid": None}
            if not force:
                miss = "、".join(f"{d['name']} :{d['port']}" for d in ev["deps"] if not d["ready"])
                return _fail("deps-not-ready", detail=f"依赖未就绪: {miss}", deps=ev["deps"])
        if stagger:
            wait = _LAST_LAUNCH + _STAGGER_S - time.monotonic()
            if wait > 0:
                return {"ok": False, "reason": "stagger", "wait": wait}
        if tr is not None:
            _untrack(iid, tr)                      # 旧追踪的根已死 (还没被监督线程发现): 你显式重起, 直接替换
        free = [] if fr.unknown else [p for p in rec["ports"] if not fr.owners(p)]
        return _do_launch(rec, actor, free)


def start(iid: str, force: bool = False, wait_deps: bool = False, actor: str = "user") -> dict:
    if _HAS_PSUTIL:
        gate = _owner_gate()
        if gate:
            return gate
    return _start(iid, force=force, wait_deps=wait_deps, actor=actor, internal=False)


def _wait_deps_then_launch(iid, entry, actor, stop_ev=None) -> None:
    """后台: 每 3s 看一次依赖, 就绪就拉起; stop() / 删除 / 复位 可随时取消; 15 分钟不就绪 -> failed。
    stop_ev = 生它时的 _STOP_EV: 复位一叫停就收手 (取消事件漏设了也一样), 绝不醒来写进下一个测试的 home (c26)。"""
    cancel = entry["cancel"]
    stop_ev = stop_ev or _STOP_EV
    while True:
        if cancel.wait(_DEPS_POLL_S) or stop_ev.is_set():
            return
        rec = _get(iid)
        if rec is None:
            with _LOCK:
                _untrack(iid, entry)
                _save_state()
            return
        fr = _Frame()                              # 端口表读不到: 回环试连 + 问追踪的树, 不把「读不到」当「没就绪」(c10)
        miss = [d for d in rec["deps"] if not fr.port_up(d["port"])]
        if not miss:
            break
        if time.time() > entry["deadline"]:
            with _LOCK:
                if _TRACK.get(iid) is entry:
                    _TRACK.pop(iid, None)
                    names = "、".join(f"{d['name']} :{d['port']}" for d in miss)
                    _set_last(iid, "failed", None, f"等依赖超时: {int(_DEPS_WAIT_S // 60)} 分钟内没就绪 ({names})")
                    _save_state()
                    _invalidate()
            return
    while True:
        with _LOCK:
            if _TRACK.get(iid) is not entry or cancel.is_set() or stop_ev.is_set():
                return                             # 期间被取消 / 被别的动作替换 / 复位
            _TRACK.pop(iid, None)
            res = _start(iid, force=True, wait_deps=False, actor=actor, internal=True,
                         stagger=bool(entry.get("stagger")))
            if res.get("reason") == "stagger":     # 批量拉起: 同一依赖一好, 大家别同一刻一起冲
                _TRACK[iid] = entry                # 还没轮到: 放回去, 继续显示「等依赖就绪」
                wait = res["wait"]
            else:
                if not res["ok"] and res["reason"] not in ("already-running",):
                    _set_last(iid, "failed", None, f"依赖就绪后没能启动: {res.get('detail') or res['reason']}")
                if not res["ok"]:                  # 没拉起来: 盘上那条「等依赖」也得去掉
                    _save_state()
                    _invalidate()
                return
        if cancel.wait(min(wait, 1.0)) or stop_ev.is_set():
            return                                 # stop() / 删除 / 复位: 取消方已经清掉了条目


# =====================================================================
# 停止
# =====================================================================

# argv: 1 = 依次尝试挂靠的控制台 (pid) ; 2 = 允许出现在该控制台上的 pid (实例树 + 其 cmd/conhost 祖先) ;
#       3 = 要停的进程 (至少一个得在这个控制台上, 否则 Ctrl+C 发错地方: 比如 owner 自己另开了控制台, 父 cmd 是你的窗口) ;
#       4 = 绝不能收到 Ctrl+C 的进程 (tokmon 自己): 它在哪个控制台上, 那个控制台就一概不发
_WIN_CTRL_C_HELPER = r'''
import ctypes, os, sys
k = ctypes.windll.kernel32
ids = lambda s: [int(x) for x in s.split(",") if x]
attach, allowed, targets = ids(sys.argv[1]), set(ids(sys.argv[2])), set(ids(sys.argv[3]))
forbidden = set(ids(sys.argv[4])) if len(sys.argv) > 4 else set()
me, shared, N = os.getpid(), False, 1024
k.FreeConsole()
for pid in attach:
    if not k.AttachConsole(pid):
        continue
    arr = (ctypes.c_uint32 * N)(); n = k.GetConsoleProcessList(arr, N)
    on = [arr[i] for i in range(min(n, N)) if arr[i] != me]
    if n > N or forbidden.intersection(on):
        shared = True                  # 列表装不下 = 肯定不止实例在上面; tokmon 在上面 = 发了就打到 tokmon 自己
    elif targets.intersection(on):
        if [p for p in on if p not in allowed]:
            shared = True              # 控制台上有实例以外的进程 (你的 shell / claude ...): 不发
        else:
            k.SetConsoleCtrlHandler(None, True)            # 自己别被 Ctrl+C 打死
            sys.exit(0 if k.GenerateConsoleCtrlEvent(0, 0) else 3)
    k.FreeConsole()                    # 这个控制台上没有要停的进程 / 不能发: 换下一个
print("shared-console" if shared else "attach-failed")
sys.exit(4 if shared else 2)
'''


def _helper_python() -> str:
    """helper 用控制台版 python: 开机自启跑在 pythonw 下时, pythonw 的 stdout 可能是 None, print 会让 helper 误报失败。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return sys.executable


def _win_ctrl_c(attach: list, allowed, targets, forbidden=()) -> str:
    """独立 helper 进程挂到实例的控制台发 Ctrl+C (tokmon 自己绝不 AttachConsole)。
    只在「至少一个要停的进程在这个控制台上、且控制台上没有实例树以外的进程」时才发; 否则换下一个候选,
    都不行 -> 不发, 交给定点强杀。tokmon 自己的 pid 永远在 forbidden 里: 它所在的控制台绝不发 (比如在终端里跑的 tokmon)。"""
    forbidden = set(forbidden) | {os.getpid()}
    attach = [p for p in attach if p not in forbidden]
    targets = [p for p in targets if p not in forbidden]
    if not attach or not targets:
        return "attach-failed"
    try:
        r = subprocess.run([_helper_python(), "-c", _WIN_CTRL_C_HELPER, ",".join(map(str, attach)),
                            ",".join(map(str, sorted(set(allowed) - forbidden))), ",".join(map(str, targets)),
                            ",".join(map(str, sorted(forbidden)))],
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return "error"
    return {0: "sent", 2: "attach-failed", 4: "shared-console"}.get(r.returncode, "error")


def _send_interrupt(plan) -> bool:
    if _IS_WIN:
        return _win_ctrl_c(plan["attach"], plan["allowed"], plan.get("targets") or [], (os.getpid(),)) == "sent"
    try:
        root = plan.get("root")
        if plan["mode"] == "tokmon" and root:
            pg = os.getpgid(root)
            if pg == root and pg != os.getpgrp():   # 只对我们拉起时新开的会话/进程组发, 绝不波及自己的组
                os.killpg(pg, signal.SIGINT)
                return True
        sent = False
        for o in plan["owners"]:
            os.kill(o, signal.SIGINT)
            sent = True
        return sent
    except OSError:
        return False


def _stop_plan(rec, tr, fr: _Frame) -> dict:
    """要停哪些进程。tokmon 启动的: 跟踪根 + 子孙; 外部启动的: 端口 owner + 子孙 (绝不含祖先)。
    句柄构造时即锁定 create_time, 并与本帧观测再比一次 (PID 复用防护)。"""
    me = os.getpid()
    ev = _evaluate(rec, fr, tr, None, time.time())
    root = tr["pid"] if (tr and _root_alive(tr, fr)) else None
    pids: list = []
    if root:
        pids = [root] + [p for p in fr.descendants(root) if p != root]
    owners = [p for p in ev["mine_pids"] if p and p != me]
    for o in owners:
        if o not in pids:
            pids += [o] + [p for p in fr.descendants(o) if p not in pids and p != o]
    pids = [p for p in dict.fromkeys(pids) if p != me]
    procs = _handles(pids, fr)                     # 自上而下 (根在前): 强杀时倒过来 = 先子后根
    allowed = set(pids)
    # Ctrl+C 要送到的进程: 端口 owner (真正干活的); 无端口实例就是根
    targets = list(owners) or ([root] if root else [])
    # 挂靠顺序: tokmon 启动的先挂 cmd 根 (实测挂 Store 版 python 子进程会失败); 外部启动的先挂 owner 自己的控制台,
    # 再挂它上方紧挨着的 cmd/conhost (如 .bat 的窗口)。helper 会核对 owner 真在那个控制台上才发。
    attach: list = [root] if root else []
    attach += [o for o in owners if o not in attach]
    if _IS_WIN and not root:
        for o in owners:
            cur = o
            for _ in range(4):
                pp = _ppid(cur)
                info = fr.info(pp) if pp else None
                if not info or (info.get("name") or "").lower() not in ("cmd.exe", "conhost.exe"):
                    break
                allowed.add(pp)
                if pp not in attach:
                    attach.append(pp)
                cur = pp
    attach += [p for p in pids if p not in attach][:6]
    return {"procs": procs, "root": root, "owners": owners, "targets": targets, "attach": attach,
            "allowed": allowed, "mode": "tokmon" if root else "external", "rec": rec,
            # 没有可停的进程时, 说清楚是「认不准」还是「读不到」, 而不是一句「没在跑」
            "ambiguous": [c for c in ev["conflicts"] if c["kind"] == "ambiguous"], "unknown": ev["unknown"]}


def _handles(pids, fr: _Frame) -> list:
    """pid -> psutil 句柄 (构造时即锁定 create_time, 之后 is_running/kill 都会复核身份), 并与本帧观测再比一次 (PID 复用防护)。"""
    out = []
    for p in pids:
        h = _proc_handle(p)
        if h is None:
            continue
        info = fr.info(p)
        try:
            hct = h.create_time()
        except Exception:
            hct = None
        if info and info.get("create_time") is not None and hct is not None and abs(hct - info["create_time"]) > _CT_TOL:
            continue                               # 观测到的和现在的不是同一个进程
        out.append(h)
    return out


def _still(p) -> bool:
    try:
        return p.is_running()                      # psutil 用 create_time 比对: PID 被复用也会是 False
    except Exception:
        return False


def _new_children(procs, known: set) -> list:
    """还活着的成员身上新长出来的子孙 (句柄自带 create_time 核对的父子关系)。"""
    out = []
    for h in procs:
        if not _still(h):
            continue
        try:
            kids = h.children(recursive=True)
        except Exception:
            continue
        for c in kids:
            if c.pid not in known and c.pid != os.getpid():
                known.add(c.pid)
                out.append(c)
    return out


def _kill_ordered(procs, root=None) -> bool:
    """procs 自上而下排好。先全部挂起 —— 免得杀了子进程, 父进程 (.bat 循环 / watcher) 趁机再拉一个新的 ——
    再先子后根逐个强杀。-> 是否遇到权限拒绝。"""
    for p in procs:
        try:
            p.suspend()
        except Exception:
            pass
    denied = False
    for p in sorted(reversed(procs), key=lambda x: x.pid == root):
        try:
            if _still(p):
                p.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied = True
        except Exception:
            pass
    return denied


def _wait_killed(procs) -> list:
    """等强杀生效; 杀不掉的恢复运行 (绝不留下被冻住的进程)。-> 仍活着的。"""
    _gone, alive = psutil.wait_procs(procs, timeout=_KILL_WAIT_S)
    for p in alive:
        try:
            p.resume()
        except Exception:
            pass
    return alive


def _mine_listeners(rec, fr: _Frame) -> list:
    """[(port, pid)]: 声明端口上被认作「它」的监听者 —— 与状态同一套识别 (根已死, 不带追踪树)。"""
    ev = _evaluate(rec, fr, None, None, time.time())
    return [(x["port"], x["pid"]) for x in ev["listening"] if x["pid"] and x["pid"] in ev["mine_pids"]]   # 代理进程绝不算


def _stragglers(plan, fr: _Frame):
    """补扫对象 -> (pids 自上而下, 仍被「它」监听的端口 | None)。
    快照成员身上新长出的子孙 + 声明端口上仍被认作「它」的监听者 (连同子孙)。
    绝不含祖先、绝不含 tokmon 自己、绝不含别的实例正在追踪的进程树。"""
    rec = plan.get("rec")
    me = os.getpid()
    with _LOCK:
        others = [t for i, t in _TRACK.items() if i != rec["id"] and t.get("pid")]
    busy: set = set()
    for t in others:
        busy |= _tracked_tree(t, fr)
    pids = [c.pid for c in _new_children([p for p in plan["procs"] if _still(p)], {p.pid for p in plan["procs"]})]
    port = None
    for lport, pid in _mine_listeners(rec, fr):
        if pid == me or pid in busy:
            continue
        port = lport if port is None else port
        pids += [pid] + fr.descendants(pid)
    pids = [p for p in dict.fromkeys(pids) if p and p != me and p not in busy]
    return pids, port


def _sweep(plan):
    """停完之后补扫, 快照之外冒出来的「它」一并收掉: 每轮都先等 _SWEEP_GAP_S 再看 —— 被杀后又被外面拉起来的进程
    要一会儿才开始监听, 杀完立刻看一眼几乎总是空的 (c1); 最多 _SWEEP_ROUNDS 轮, 某轮 (等过之后) 什么都没有就收手。
    最后无论如何再等一下、再看一次「它」还在不在声明端口上听: 在 -> 停止没停干净, 照实报出来。
    -> (是否补杀过, 补扫后仍被「它」监听的端口 | None)。"""
    rec = plan.get("rec")
    if not rec or rec.get("kind") != "service" or not rec.get("ports"):
        return False, None                         # 无端口: 快照 + 强杀前重新点名已经覆盖
    swept = False
    for _ in range(_SWEEP_ROUNDS):
        time.sleep(_SWEEP_GAP_S)
        fr = _Frame()
        pids, _port = _stragglers(plan, fr)
        if not pids:
            break
        procs = _handles(pids, fr)
        if procs:
            swept = True
            _kill_ordered(procs)
            _wait_killed(procs)
    time.sleep(_SWEEP_GAP_S)
    _pids, port = _stragglers(plan, _Frame())
    return swept, port


def _execute_stop(plan, grace) -> str:
    """先优雅 (Ctrl+C / SIGINT) 等 grace 秒, 不退再强杀 (先挂起, 再先子后根), 最后补扫快照之外的「它」。
    -> graceful|forced|gone|incomplete|access-denied|error。incomplete: 补扫 3 轮后仍有「它」在监听,
    端口记在 plan["leftover_port"]。"""
    procs = [p for p in plan["procs"] if _still(p)]
    root = plan.get("root")
    outcome = "gone"
    if procs:
        outcome = None
        if grace > 0 and _send_interrupt(plan):
            _gone, alive = psutil.wait_procs(procs, timeout=grace)
            if not alive:
                outcome = "graceful"
            ids = {id(p) for p in alive}
            procs = [p for p in procs if id(p) in ids]   # wait_procs 回来的是无序的: 保住自上而下的顺序
        if outcome is None:
            # 宽限期里可能又生了新进程 (如 .bat 循环在 Ctrl+C 后又拉起一个): 强杀前按还活着的成员重新点一次名
            procs = procs + _new_children(procs, {p.pid for p in plan["procs"]})
            denied = _kill_ordered(procs, root)
            if _wait_killed(procs):
                return "access-denied" if denied else "error"
            outcome = "forced"
    swept, port = _sweep(plan)
    if port is not None:
        plan["leftover_port"] = port
        return "incomplete"
    return "forced" if swept else outcome


def _stop_worker(iid, rec, entry, plan, actor, restarting=False) -> str:
    """restarting=True: 重启里的停止 —— 没停干净时错误里说清楚「没有重新启动」(重启线程据此不再起一份)。"""
    try:
        outcome = _execute_stop(plan, rec["stop_grace_s"])
    except Exception:
        outcome = "error"
    code = None
    popen = entry.get("popen")
    if popen is not None:
        try:
            code = popen.wait(timeout=2)
        except Exception:
            code = popen.poll()
    with _LOCK:
        _untrack(iid, entry)
        if _get(iid) is not None:
            if outcome in ("graceful", "forced", "gone"):
                err = None
            elif outcome == "incomplete":          # 杀了又被拉起来 (比如外面还有个循环在守着它): 如实说
                err = f"停止后仍有进程在监听 {plan.get('leftover_port')}"
            else:
                err = f"停止未完成: {outcome}"
            if err and restarting:
                err += "，没有重新启动"
            _set_last(iid, "stopped", code, err, outcome=outcome)
        _save_state()
        _invalidate()
    _audit("instance-stopped", rec["name"], outcome)
    return outcome


def _mark_stopping(iid, tr) -> dict:
    """持锁调用: 把追踪条目切到 stopping (外部实例没有条目就新建一个只用于显示/占位的)。"""
    entry = tr if tr is not None else {"popen": None, "pid": None, "create_time": None,
                                       "started_at": time.time(), "deadline": 0.0}
    entry["phase"] = "stopping"
    entry["stop_requested"] = True
    _TRACK[iid] = entry
    _save_state()
    _invalidate()
    return entry


def _nothing_to_stop(plan) -> dict:
    if plan["ambiguous"]:
        a = plan["ambiguous"][0]
        return _fail("ambiguous", owner=a["owner"],
                     detail=f"端口 {a['port']} 上的 {a['owner']['name']} (pid {a['owner']['pid']}) 同时像好几个实例，认不准，不替你停")
    if plan["unknown"]:
        return _fail("unknown", detail="读不到端口表，认不出它的进程")
    return _fail("not-running")


def stop(iid: str, actor: str = "user") -> dict:
    if not _HAS_PSUTIL:
        return _fail("no-psutil")
    gate = _owner_gate()
    if gate:
        return gate
    _ensure_loaded()
    with _LOCK:
        rec = _get(iid)
        if rec is None:
            return _fail("not-found")
        if rec["kind"] == "self":
            return _fail("self")
        tr = _TRACK.get(iid)
        if (tr and tr.get("phase") == "stopping") or iid in _RESTARTING:
            return _fail("busy")
        if tr and tr.get("phase") == "waiting_deps":   # 还没进程: 取消等待即可
            tr["cancel"].set()
            _untrack(iid, tr)
            _set_last(iid, "stopped", None, None, outcome="cancelled")
            _save_state()
            _invalidate()
            return {"ok": True, "state": "stopped"}
        plan = _stop_plan(rec, tr, _Frame())
        if not plan["procs"]:
            if tr is not None:
                _untrack(iid, tr)
                _save_state()
                _invalidate()
            return _nothing_to_stop(plan)
        entry = _mark_stopping(iid, tr)
    _spawn(_stop_worker, iid, rec, entry, plan, actor)
    return {"ok": True, "state": "stopping"}


def restart(iid: str, actor: str = "user") -> dict:
    """同一个后台线程里: 在跑就先停, 再 start(wait_deps=True)。整个过程对别的启停算 busy。"""
    if not _HAS_PSUTIL:
        return _fail("no-psutil")
    gate = _owner_gate()
    if gate:
        return gate
    _ensure_loaded()
    with _LOCK:
        rec = _get(iid)
        if rec is None:
            return _fail("not-found")
        if rec["kind"] == "self":
            return _fail("self")
        tr = _TRACK.get(iid)
        if (tr and tr.get("phase") in _BUSY_PHASES) or iid in _RESTARTING:
            return _fail("busy")
        plan = _stop_plan(rec, tr, _Frame())
        entry = None
        if plan["procs"]:
            entry = _mark_stopping(iid, tr)
        elif plan["ambiguous"]:
            return _nothing_to_stop(plan)          # 认不准是谁: 既不替你停, 也不再起一份
        elif plan["unknown"] and tr is None:
            return _nothing_to_stop(plan)          # 读不到端口表: 它可能在外面跑着, 「重启」会变成再起一份 (终轮静态复查)
        elif tr is not None:
            # tokmon 一直追踪着的那份刚退出 (还没等到下一拍 supervisor): 再起一份就是替换它, 不是双开 —— 不必问 (第三轮复查)
            _untrack(iid, tr)
            _save_state()
        elif not rec["ports"]:
            # 无端口、tokmon 也没在追踪: 这次重启其实就是再起一份, 可它说不定在别处跑着 —— 跟「启动」一样先问你 (s1)
            return _fail("portless-confirm", detail=_PORTLESS_CONFIRM)
        _RESTARTING.add(iid)
        _invalidate()
    _spawn(_restart_worker, iid, rec, entry, plan, actor, tr is not None)
    return {"ok": True, "state": "stopping" if entry else "starting"}


def _restart_worker(iid, rec, entry, plan, actor, was_tracked=False) -> None:
    try:
        if entry is not None:
            outcome = _stop_worker(iid, rec, entry, plan, actor, restarting=True)
            if outcome not in ("graceful", "forced", "gone"):
                # 没停干净 (杀了又被外面拉起来 / 杀不动): 不再起一份, 也不拿「没能启动」盖掉停止记录 ——
                # 否则卡片只剩「运行中 · 外部启动」, 看不出是外面有东西在把它拉起来 (R2)
                return
        res = _start(iid, force=False, wait_deps=True, actor=actor, internal=True,
                     portless_ok=entry is not None or was_tracked)
        if not res["ok"]:
            with _LOCK:
                if _get(iid) is not None:
                    _set_last(iid, "failed", None, f"重启后没能启动: {res.get('detail') or res['reason']}")
                    _save_state()
    finally:
        with _LOCK:
            _RESTARTING.discard(iid)
            _invalidate()


# =====================================================================
# 批量 / 开机
# =====================================================================

def _queue_starts(ids, actor, audit_kind=None) -> dict:
    """筛掉不该起的 (不存在 / 本服务 / 已在跑 / 在途 / 端口被占 / 无端口认不出), 其余交给一个后台线程顺序拉起 (错开 5s)。"""
    queued, skipped = [], []
    recs = {r["id"]: r for r in load_manifest()}
    with _LOCK:
        track = dict(_TRACK)
        restarting = set(_RESTARTING)
    fr = _Frame(track=track, recs=list(recs.values()))
    now = time.time()
    for iid in ids:
        rec = recs.get(iid)
        if rec is None:
            skipped.append({"id": iid, "reason": "not-found"})
            continue
        if rec["kind"] != "service":
            skipped.append({"id": iid, "reason": "self"})
            continue
        if iid in restarting:
            skipped.append({"id": iid, "reason": "busy"})
            continue
        st = _evaluate(rec, fr, track.get(iid), None, now)["state"]
        if st in ("running", "starting", "stopping", "waiting_deps"):
            skipped.append({"id": iid, "reason": "already-running" if st == "running" else "busy"})
        elif st == "port_busy":
            skipped.append({"id": iid, "reason": "port-busy"})
        elif st in ("degraded", "unknown"):        # 进程还在 / 看不清: 无人值守的批量绝不再起一份
            skipped.append({"id": iid, "reason": st})
        elif not rec["ports"]:                     # 无端口、tokmon 没在追踪: 认不出是不是已在别处跑, 批量不替你再起一份 (s1)
            skipped.append({"id": iid, "reason": "portless"})
        else:
            queued.append(iid)
    if queued:
        _spawn(_run_starts, queued, actor, audit_kind, _STOP_EV)
    return {"queued": queued, "skipped": skipped}


def _batch_start(iid, actor, stop_ev):
    """批量拉起里的一个: 真实启动离上一次不足 _STAGGER_S 就等一会再来 (闸门在 _start 的锁里)。
    -> start 的结果; None = 被取消 (复位 / 退出)。"""
    while True:
        if stop_ev.is_set():
            return None                            # 先看叫没叫停: 复位中绝不再拉起 / 再生等依赖线程
        res = _start(iid, force=False, wait_deps=True, actor=actor, internal=False, stagger=True)
        if res.get("reason") != "stagger":
            return res
        if stop_ev.wait(min(res["wait"], 1.0)):
            return None


def _await_launch(iid, stop_ev) -> bool:
    """在等依赖的那个真正拉起 (或放弃 / 被取消) 之前不轮到下一个: 否则共享同一个依赖的实例会在依赖一好时同一刻一起冲。
    -> False = 被取消。"""
    while True:
        with _LOCK:
            tr = _TRACK.get(iid)
            if not tr or tr.get("phase") != "waiting_deps":
                return True
            if time.time() > tr["deadline"] + 2 * _DEPS_POLL_S:
                return True                        # 等依赖线程自己会记 failed; 这里只是别陪它耗死
        if stop_ev.wait(min(_DEPS_POLL_S, 0.5)):
            return False


def _run_starts(ids, actor, audit_kind, stop_ev) -> None:
    for iid in ids:
        res = _batch_start(iid, actor, stop_ev)
        if res is None:
            return
        if audit_kind:
            rec = _get(iid)
            _audit(audit_kind, rec["name"] if rec else iid, res.get("state") if res.get("ok") else res.get("reason"))
        if not _await_launch(iid, stop_ev):
            return


def start_group(group: str = "core", actor: str = "user") -> dict:
    if group not in GROUPS:
        return {"ok": False, "reason": "bad-group"}
    if not _HAS_PSUTIL:
        return _fail("no-psutil")
    gate = _owner_gate()
    if gate:
        return gate
    _ensure_loaded()
    ids = [r["id"] for r in load_manifest() if r["group"] == group and r["kind"] == "service"]
    return {"ok": True, **_queue_starts(ids, actor)}


def boot_ack(action: str) -> dict:
    """"start": 把开机待确认的实例拉起; "dismiss": 清掉提醒。两者都会清空待确认列表。"""
    if action not in ("start", "dismiss"):
        return {"ok": False, "reason": "bad-action"}
    gate = _owner_gate()
    if gate:
        return gate
    _ensure_loaded()
    with _LOCK:
        st = _state()
        ids = list(st["boot_pending"])
        st["boot_pending"] = []
        _save_state()
        _invalidate()
    if action == "dismiss":
        return {"ok": True, "dismissed": ids}
    if not _HAS_PSUTIL:
        return _fail("no-psutil")
    return {"ok": True, **_queue_starts(ids, "user")}


def _boot_sequence(stop_ev=None) -> str:
    """开机拉起: auto 的顺序拉起 (错开 5s, 各自等依赖), ask 的记为待确认并发一条提醒事件。
    每个 tokmon 进程只做一次 (start_supervisor(boot=True) 只起一次这个线程), 不再按「这次开机」去重:
    psutil.boot_time() 跨 Fast Startup 关机 / 注销重登都不变, 拿它去重会让「关机再开」什么都不拉 (c5/c14/c22)。
    触发它的开机自启任务只在登录时跑、且不重复起。有端口的实例已经在跑 (认得出) 就跳过; 无端口的认不出外部启动的副本
    (比如启动文件夹里的 .bat 已经起了一份), 所以校验不许它设 auto, 旧清单里残留的 auto 这里也跳过并审计一笔 (s1)。"""
    stop_ev = stop_ev or _STOP_EV
    if _OWNER_FD is None:
        return "not-owner"
    handled = time.time()
    with _LOCK:
        st = _state()
        st["boot_handled"] = handled               # 只用来显示「上次开机拉起于 …」和给提醒去重
        st["boot_pending"] = []
        _save_state()
        _invalidate()
    if stop_ev.wait(_BOOT_DELAY_S):
        return "cancelled"
    recs = [r for r in load_manifest() if r["kind"] == "service"]
    with _LOCK:
        track = dict(_TRACK)
    fr = _Frame(track=track)
    now = time.time()

    states = {r["id"]: _evaluate(r, fr, track.get(r["id"]), None, now)["state"]
              for r in recs if r["boot"] in ("auto", "ask")}
    busy = ("running", "starting", "stopping", "waiting_deps", "degraded", "unknown")
    auto = [r for r in recs if r["boot"] == "auto" and states[r["id"]] not in busy and r["ports"]]
    ask = [r for r in recs if r["boot"] == "ask" and states[r["id"]] not in busy and r["ports"]]
    for r in recs:                                 # 进程还在 / 看不清 / 认不出的 auto/ask 实例: 不起 / 不提醒, 但审计里留一笔为什么
        if r["boot"] not in ("auto", "ask"):
            continue
        if states[r["id"]] in ("degraded", "unknown"):
            _audit("instance-boot-start", r["name"], states[r["id"]])
        elif not r["ports"] and states[r["id"]] not in busy:
            _audit("instance-boot-start", r["name"], "portless")
    with _LOCK:
        _state()["boot_pending"] = [r["id"] for r in ask]
        _save_state()
        _invalidate()
    if ask:
        n = len(ask)
        _emit(Event.make("INSTANCE_BOOT_PENDING", pillar="instances", severity="warning",
                         timestamp=now, detected_at=now, dedup_key=f"INSTANCE_BOOT_PENDING:{handled}",
                         count=n, state_label=f"重启后有 {n} 个实例等你确认"))
    for r in auto:                                 # 一个一个来: 真实启动之间隔 _STAGGER_S, 等依赖的起了才轮到下一个
        res = _batch_start(r["id"], "boot", stop_ev)
        if res is None:
            return "cancelled"
        _audit("instance-boot-start", r["name"], res.get("state") if res.get("ok") else res.get("reason"))
        if not _await_launch(r["id"], stop_ev):
            return "cancelled"
    return "done"


# =====================================================================
# 监督线程
# =====================================================================

def _root_exit(tr, fr: _Frame):
    """-> (是否已退出, 退出码|None)。有 popen 以它为准; tokmon 重启后恢复的条目只能看 pid+create_time。"""
    p = tr.get("popen")
    if p is not None:
        code = p.poll()
        return code is not None, code
    return not _alive(tr.get("pid"), tr.get("create_time"), fr), None


def _timeout_worker(iid, rec, entry, plan) -> None:
    try:
        outcome = _execute_stop(plan, 0)           # 半启动的实例占着资源更糟: 直接收掉整棵树
    except Exception:
        outcome = "error"
    with _LOCK:
        _untrack(iid, entry)
        if _get(iid) is not None:
            _set_last(iid, "failed", None,
                      f"启动超时: {rec['start_timeout_s']}s 内端口没有开始监听 (已结束它的进程树: {outcome})")
        _save_state()
        _invalidate()
    _audit("instance-start-timeout", rec["name"], outcome)


def _unrecognized_holder(rec, ev, tr) -> str | None:
    """启动超时那一刻主端口其实在监听、只是持有者认不出是它 (换了目录的子进程 / 转发…) -> 持有者名字; 否则 None。
    只认「拉起时端口是空的」且持有者不属于别的实例: 这时它多半就是这个实例, 当成超时收尸只会冤杀 (c9)。"""
    ports = rec["ports"]
    if not ports or ports[0] not in (tr.get("free_at_launch") or []):
        return None
    fs = [f for f in ev["foreign"] if f["port"] == ports[0]]
    if not fs or any(f["kind"] == "other" for f in fs):
        return None
    return fs[0]["name"]


def _tick(emit=None) -> None:
    """一拍: starting -> running/failed, 发现崩溃, 清理开机待确认里已经在跑的, 落盘「又在跑了」对旧崩溃记录的取代。
    只有主人推进 (c7/c15): 第二个 tokmon 的监督若也在推, 你在主人那边的一次停止会被它当成崩溃报出去。"""
    if not _HAS_PSUTIL or _OWNER_FD is None:
        return
    _ensure_loaded()
    with _LOCK:
        items = [(i, t) for i, t in _TRACK.items() if t.get("phase") in ("starting", "running")]
        st = _state()
        pending = list(st["boot_pending"])
        stale = [i for i, l in st["last"].items()
                 if isinstance(l, dict) and l.get("state") in ("crashed", "failed") and i not in _TRACK]
    if not items and not pending and not stale:
        return
    recs = {r["id"]: r for r in load_manifest()}
    fr = _Frame(recs=list(recs.values()))
    now = time.time()
    audits: list = []
    for iid, tr in items:
        rec = recs.get(iid)
        if rec is None:
            continue
        exited, code = _root_exit(tr, fr)       # 崩溃只看根进程退没退, 与端口表读不读得到无关
        if tr["phase"] == "starting":
            if exited:
                with _LOCK:
                    if _TRACK.get(iid) is tr and tr["phase"] == "starting":
                        _untrack(iid, tr)
                        _set_last(iid, "failed", code, f"启动后进程退出了 (退出码 {code})")
                        _save_state()
                        _invalidate()
                continue
            ev = _evaluate(rec, fr, tr, None, now)
            if rec["ports"]:                       # 健康检查: 声明的端口真的被"它"(或替它听的代理) 监听了才算起来
                ready = bool(ev["mine_pids"] or ev["proxied"])
            else:
                ready = now - tr["started_at"] >= _PORTLESS_UP_S
            with _LOCK:
                if _TRACK.get(iid) is not tr or tr["phase"] != "starting":
                    continue
                if ready:
                    tr["phase"] = "running"
                    _save_state()
                    _invalidate()
                elif now > tr["deadline"]:
                    holder = None if fr.unknown else _unrecognized_holder(rec, ev, tr)
                    if fr.unknown:                 # 读不到 ≠ 没在听: 期限往后推, 绝不收尸 (c10)
                        tr["deadline"] = now + _UNKNOWN_EXTEND_S
                        _save_state()
                    elif holder:                   # 端口在听、只是认不出持有者: 不杀, 标出来 (degraded)
                        tr["phase"] = "running"
                        tr["unrecognized"] = True
                        _save_state()
                        _invalidate()
                        audits.append((rec["name"], f"unrecognized-owner {holder}"))
                    else:
                        plan = _stop_plan(rec, tr, fr)
                        _mark_stopping(iid, tr)
                        _spawn(_timeout_worker, iid, rec, tr, plan)
        elif exited:
            with _LOCK:
                if _TRACK.get(iid) is not tr or tr["phase"] != "running":
                    continue                       # 期间你点了停止 (阶段已是 stopping, 停止线程会记结局): 不是崩溃
                if tr.get("stop_requested"):
                    # tokmon 重启前你点了停止、停到一半 (恢复成「在跑」但记着是你要停的): 现在它退了 = 那次停止成了 (R3)
                    _untrack(iid, tr)
                    _set_last(iid, "stopped", code, None, outcome="gone")
                    _save_state()
                    _invalidate()
                    continue
                _untrack(iid, tr)
                _set_last(iid, "crashed", code, None)
                _save_state()
                _invalidate()
            _emit(Event.make("PROCESS_CRASHED", pillar="instances", severity="warning",
                             project=rec.get("project") or None, timestamp=now, detected_at=now,
                             dedup_key=f"PROCESS_CRASHED:{iid}:{tr['started_at']}",
                             instance=rec["name"], exit_code=code, state_label="意外退出"), emit)
    for name, outcome in audits:
        _audit("instance-start-timeout", name, outcome)
    if pending or stale:
        with _LOCK:
            track = dict(_TRACK)
        running = {i for i in set(pending) | set(stale)
                   if i in recs and _evaluate(recs[i], fr, track.get(i), None, now)["state"] == "running"}
        _supersede([i for i in stale if i in running])
        done = [i for i in pending if i in running]
        if done:                                   # 开机待确认的实例已经在跑了 (你手动起了 / 外部起了) -> 撤掉提醒
            with _LOCK:
                st = _state()
                st["boot_pending"] = [i for i in st["boot_pending"] if i not in done]
                _save_state()
                _invalidate()


def _sup_loop(stop_ev) -> None:
    while True:
        try:
            _tick()
        except Exception:
            pass                                   # 监督线程绝不能死
        if stop_ev.wait(_SUP_INTERVAL_S):
            return


def _start_sup_thread() -> None:
    """持锁调用。"""
    global _SUP_THREAD
    if _SUP_THREAD is None:
        _SUP_THREAD = threading.Thread(target=_sup_loop, args=(_STOP_EV,), name="tokmon-instances", daemon=True)
        _SUP_THREAD.start()


def _claim_loop(stop_ev, serve_port) -> None:
    """非主人: 每 _CLAIM_RETRY_S 秒再抢一次主人锁。原来的主人退出 (锁随进程释放) 后这边接手: 写名片、起监督 ——
    否则再也没有谁在监督, 崩溃没人报, 页面还一直说「另一个 tokmon 在管」(R4)。
    不补做开机拉起: 那是开机那个 tokmon 的事; 这时补做, 可能把你后来特意停掉的又拉起来。"""
    while not stop_ev.wait(_CLAIM_RETRY_S):
        if claim_owner(serve_port):
            with _LOCK:
                if _HAS_PSUTIL and not stop_ev.is_set():
                    _ensure_loaded()
                    _start_sup_thread()
            _invalidate()
            return


def start_supervisor(*, boot: bool = False, serve_port: int | None = None, emit=None) -> None:
    """幂等。先抢实例层的主人锁: 抢到才起监督线程 (每 2s 一拍)、boot=True 时做开机拉起 (每个进程只一次);
    抢不到 = 同一个 home 上另一个 tokmon 在管, 本进程只读 (不监督、不拉起、不写状态), 并起一个小线程定期再抢 ——
    那个 tokmon 退出后由这边接管监督。"""
    global _BOOT_THREAD, _EMIT, _CLAIM_THREAD
    with _LOCK:
        if emit is not None:
            _EMIT = emit                           # 先记下: 之后接管了, 崩溃通知照样走它
        if not claim_owner(serve_port):
            if _CLAIM_THREAD is None:
                _CLAIM_THREAD = threading.Thread(target=_claim_loop, args=(_STOP_EV, serve_port),
                                                 name="tokmon-instances-claim", daemon=True)
                _CLAIM_THREAD.start()
            return
        if not _HAS_PSUTIL:
            return
        _ensure_loaded()
        _start_sup_thread()
        if boot and _BOOT_THREAD is None:
            _BOOT_THREAD = threading.Thread(target=_boot_sequence, args=(_STOP_EV,), name="tokmon-instances-boot",
                                            daemon=True)
            _BOOT_THREAD.start()


# =====================================================================
# 登记 / 编辑
# =====================================================================

def _rename_runtime(old, new) -> None:
    """持锁调用: 改 id 时把运行期追踪 / 状态 / 日志一起挪过去。"""
    if old in _TRACK:
        _TRACK[new] = _TRACK.pop(old)
    if old in _RESTARTING:
        _RESTARTING.discard(old)
        _RESTARTING.add(new)
    if old in _SEEN_RUNNING:
        _SEEN_RUNNING[new] = _SEEN_RUNNING.pop(old)
    st = _state()
    for k in ("tracked", "last"):
        if old in st[k]:
            st[k][new] = st[k].pop(old)
    st["boot_pending"] = [new if x == old else x for x in st["boot_pending"]]
    _save_state()
    src, dst = LOGS / f"{old}.log", LOGS / f"{new}.log"
    try:
        if src.exists() and not dst.exists():
            os.rename(src, dst)                    # 正被运行中的实例写着 (Windows) 会失败: 无所谓, 旧日志留着
    except OSError:
        pass


def save_instance(data: dict, original_id: str | None = None) -> dict:
    gate = _owner_gate()
    if gate:
        return gate
    if not isinstance(data, dict):
        return {"ok": False, "reason": "invalid", "errors": {"_": "bad-format"}}
    _ensure_loaded()
    with _LOCK:
        recs, _err, pristine = _read_manifest_file()
        ids = [r["id"] for r in recs]
        if original_id is not None and original_id not in ids:
            return {"ok": False, "reason": "not-found"}
        data = dict(data)
        if original_id is not None and not data.get("id"):
            data["id"] = original_id
        taken = set(ids) - {original_id}
        rec, errors = validate(data, taken=taken)
        if errors:
            return {"ok": False, "reason": "invalid", "errors": errors}
        if rec["id"] in taken:
            return {"ok": False, "reason": "id-exists"}
        if original_id is not None and rec["id"] != original_id and (
                original_id in _RESTARTING or (_TRACK.get(original_id) or {}).get("phase") in _BUSY_PHASES):
            return {"ok": False, "reason": "busy"}  # 启停在途时改 id: 后台线程会找不到自己的条目
        if original_id is None:
            recs.append(rec)
        else:
            recs[ids.index(original_id)] = rec
        e = _write_manifest(recs, pristine)
        if e:
            return {"ok": False, "reason": "manifest-error", "detail": e}
        if original_id is not None and rec["id"] != original_id:
            _rename_runtime(original_id, rec["id"])
        _invalidate()
        return {"ok": True, "instance": copy.deepcopy(rec)}


def patch_instance(iid: str, fields: dict) -> dict:
    gate = _owner_gate()
    if gate:
        return gate
    if not isinstance(fields, dict) or not fields:
        return {"ok": False, "reason": "invalid", "errors": {"_": "required"}}
    bad = {k: "bad-format" for k in fields if k not in _PATCHABLE}
    if bad:
        return {"ok": False, "reason": "invalid", "errors": bad}
    with _LOCK:
        recs, _err, pristine = _read_manifest_file()
        idx = next((i for i, r in enumerate(recs) if r["id"] == iid), None)
        if idx is None:
            return {"ok": False, "reason": "not-found"}
        merged = {**recs[idx], **fields, "id": iid}
        rec, errors = validate(merged, taken={r["id"] for r in recs} - {iid}, check_cwd=False,
                               from_disk="boot" not in fields)   # 没动开机策略 (改分组 / 描述): 旧记录里的无端口 + 提醒不拦 (终轮静态复查)
        if errors:
            return {"ok": False, "reason": "invalid", "errors": errors}
        recs[idx] = rec
        e = _write_manifest(recs, pristine)
        if e:
            return {"ok": False, "reason": "manifest-error", "detail": e}
        _invalidate()
        return {"ok": True, "instance": copy.deepcopy(rec)}


def delete_instance(iid: str) -> dict:
    """删登记。在跑的实例不管它 (只是不再追踪) —— 删登记不等于停服务。"""
    gate = _owner_gate()
    if gate:
        return gate
    _ensure_loaded()
    with _LOCK:
        recs, _err, pristine = _read_manifest_file()
        keep = [r for r in recs if r["id"] != iid]
        if len(keep) == len(recs):
            return {"ok": False, "reason": "not-found"}
        e = _write_manifest(keep, pristine)
        if e:
            return {"ok": False, "reason": "manifest-error", "detail": e}
        tr = _TRACK.pop(iid, None)
        if tr and tr.get("cancel"):
            tr["cancel"].set()
        st = _state()
        st["tracked"].pop(iid, None)
        st["last"].pop(iid, None)
        st["boot_pending"] = [x for x in st["boot_pending"] if x != iid]
        _SEEN_RUNNING.pop(iid, None)
        _save_state()
        _invalidate()
    return {"ok": True}


def draft_from_pid(pid: int, create_time: float | None) -> dict:
    """从一个正在跑的进程生成登记草稿 (新鲜读 psutil + create_time 身份核对)。命令是原文 —— 仅本机端点可用。"""
    if not _HAS_PSUTIL:
        return {"ok": False, "reason": "no-psutil"}
    if create_time is None:
        return {"ok": False, "reason": "identity-required"}
    try:
        pid = int(pid)
        expect = float(create_time)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "identity-required"}
    if not math.isfinite(expect):                  # NaN 跟谁比都"不超差" -> 会把身份核对整个绕过去
        return {"ok": False, "reason": "identity-required"}
    try:
        p = psutil.Process(pid)
        ct = p.create_time()
        if abs(ct - expect) > _CT_TOL:
            return {"ok": False, "reason": "pid-reused"}
        name = p.name() or "?"
        cmdline = p.cmdline()
    except psutil.NoSuchProcess:
        return {"ok": False, "reason": "gone"}
    except (psutil.AccessDenied, PermissionError):
        return {"ok": False, "reason": "access-denied"}
    except (psutil.Error, OSError, ValueError):
        return {"ok": False, "reason": "gone"}
    try:
        cwd = p.cwd() or ""
    except (psutil.Error, OSError):
        cwd = ""
    cmd, warns = _draft_command(cmdline) if cmdline else (name, [])
    scan = _scan_listeners()
    ports = sorted({port for port, lpid, _ip in scan if lpid == pid} if scan is not None
                   else _proc_listen_ports(pid))[:5]                # 全表读不到: 退回只问这个进程
    if name.lower().endswith(".exe"):
        name = name[:-4]
    draft = {
        "name": name[:40], "project": os.path.basename(os.path.normpath(cwd))[:60] if cwd else "",
        "description": "", "cwd": cwd, "command": cmd[:2000], "env": {}, "ports": ports, "open_path": "/",
        "deps": [], "kind": "self" if pid == os.getpid() else "service", "boot": "manual", "group": "core",
        "stop_grace_s": 10, "start_timeout_s": 60,
    }
    if procmon._redact(cmd) != cmd:
        warns.insert(0, "命令行可能含密钥, 保存前请检查 (清单只存在本机)")
    return {"ok": True, "draft": draft, "warning": "；".join(warns) or None}


_CMD_META = frozenset("&|<>^()")


def _win_quote(arg: str) -> str:
    """按 MSVCRT 规则引一个参数 (同 list2cmdline), 另外含 cmd 元字符 & | < > ^ ( ) 的也整体加引号:
    实例经 `cmd /d /s /c "…"` 运行, 引号里这些字符对 cmd 是字面量; 不加就会被拆成两条命令 / 重定向 / 吃掉。"""
    if arg and not _CMD_META.intersection(arg):
        return subprocess.list2cmdline([arg])
    out, bs = ['"'], 0
    for c in arg:
        if c == "\\":
            bs += 1
            continue
        out.append("\\" * (bs * 2 + 1) + '"' if c == '"' else "\\" * bs + c)
        bs = 0
    out.append("\\" * (bs * 2) + '"')              # 结尾的反斜杠要翻倍, 否则会把收尾引号转义掉
    return "".join(out)


def _draft_command(argv: list):
    """进程 argv -> (可登记的命令串, [提醒])。Windows 上命令会经 cmd /s /c 再解析一遍; POSIX 经 /bin/sh -c。"""
    if not _IS_WIN:
        return shlex.join(argv), []
    warns = []
    if any("%" in a for a in argv):
        warns.append("命令里有 %，cmd 会展开成环境变量，保存前请核对")
    if any('"' in a for a in argv):
        warns.append("命令里有参数自带双引号，cmd 可能拆错，保存前请核对")
    return " ".join(_win_quote(a) for a in argv), warns


# =====================================================================
# 日志
# =====================================================================

def _decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return b.decode(locale.getpreferredencoding(False))   # cmd / 老工具的输出常是本地代码页
    except (UnicodeDecodeError, LookupError):
        return b.decode("utf-8", "replace")


def log_tail(iid: str, n: int = 200) -> dict:
    """日志尾巴: 去 ANSI 颜色、进度条只留最后一帧、逐行脱敏。"""
    try:
        n = max(1, min(500, int(n)))
    except (TypeError, ValueError):
        n = 200
    if not isinstance(iid, str) or not _ID_RE.fullmatch(iid) or _get(iid) is None:
        return {"ok": False, "reason": "not-found"}   # id 先过白名单正则: 杜绝路径穿越
    p = LOGS / f"{iid}.log"
    try:
        size = p.stat().st_size
    except OSError:
        return {"ok": True, "lines": [], "size": 0, "exists": False}
    chunk = min(size, max(64 * 1024, n * 512), 1024 * 1024)
    try:
        with open(p, "rb") as f:
            f.seek(size - chunk)
            data = f.read(chunk)
    except OSError:
        return {"ok": True, "lines": [], "size": size, "exists": True}
    raw = data.split(b"\n")
    if chunk < size and raw:
        raw = raw[1:]                              # 第一行可能被截成半截
    if raw and raw[-1] == b"":
        raw.pop()
    lines = []
    for b in raw[-n:]:
        s = _ANSI_RE.sub("", _decode(b).rstrip("\r")).rsplit("\r", 1)[-1]
        lines.append(procmon._redact(s)[:2000])
    return {"ok": True, "lines": lines, "size": size, "exists": True}


# =====================================================================
# 测试支持
# =====================================================================

def _reset_for_tests(join_timeout: float = 5.0) -> None:
    """先叫停并**等**在途的后台线程收尾, 再清空 —— 否则它们事后醒来会把状态写进下一个测试 (甚至真实) 的 home。"""
    global _STATE, _LOADED, _SUP_THREAD, _BOOT_THREAD, _CLAIM_THREAD, _EMIT, _STOP_EV, _PRESERVED_NOTE, _LAST_LAUNCH
    global _STATE_SIG
    _PROBE_CACHE.clear()
    with _LOCK:
        _STOP_EV.set()
        threads = [t for t in (_SUP_THREAD, _BOOT_THREAD, _CLAIM_THREAD) if t is not None]
    end = time.time() + join_timeout
    while True:                                    # 不持锁等: 它们收尾要拿 _LOCK
        with _LOCK:
            # 每一轮都再叫停一次: 收尾中的线程新建的等依赖条目 (比如重启停完才去等依赖) 也在内 (c26)
            for t in _TRACK.values():
                if t.get("cancel") is not None:
                    t["cancel"].set()
            alive = [w for w in _WORKERS if w.is_alive()]
        if not alive or time.time() > end:
            break
        alive[0].join(max(0.0, min(0.2, end - time.time())))
    for t in threads:
        if t is not threading.current_thread():
            t.join(max(0.0, end - time.time()))
    with _LOCK:
        _STOP_EV = threading.Event()
        _LAST_LAUNCH = float("-inf")
        _TRACK.clear()
        _RESTARTING.clear()
        _SEEN_RUNNING.clear()
        _SCACHE.clear()
        _MCACHE.clear()
        _TUNNEL_CACHE.clear()
        _CF_CACHE.clear()
        _WORKERS.clear()
        _STATE = None
        _LOADED = False
        _SUP_THREAD = None
        _BOOT_THREAD = None
        _CLAIM_THREAD = None
        _EMIT = None
        _PRESERVED_NOTE = None
        _STATE_SIG = None
        _release_owner()                           # 放锁放在收尾之后: 在途线程的最后一笔状态仍由主人写进本测试的 home
