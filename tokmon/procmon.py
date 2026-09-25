"""进程 / localhost / cloudflared 只读监控采集层 (`/api/processes` 的数据源)。

设计立场, 与 tokmon 内核同源:
- **纯只读**: 只 *观测* 进程与连接, 绝不 kill/改任何东西 (用户已敲定: 纯只读)。
- **本地、不外发**: 被动快照只用本机 psutil、不联网; 仅 opt-in 的 `probe_health` 在用户点按钮时,
  对本机 loopback 端口做一次性 TCP / HTTP-HEAD 探活 (主动连接但只碰本机、绝不外发, 见下方说明)。
- **渐进降级**: 缺 psutil -> 抛 ImportError, 由 serve 层友好提示; 单个进程取信息失败 -> 跳过那条, 不崩。
- **真实优先**: 「活动连接」只数真正 ESTABLISHED 的 TCP + 已连 UDP, 不把 TIME_WAIT 等垂死连接算进去。
- **隐私**: 命令行里 token/key/URI 口令等敏感值做尽力脱敏 (_redact)。脱敏是尽力而为, 不是安全边界。

它是 serve 层的数据提供者, 和 token 监控的内核 (parser/pricing/aggregate) 完全独立, 互不污染。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover - 由 serve 层降级提示
    _HAS_PSUTIL = False

# cpu_percent(None) 需要跨调用保留 Process 句柄才能算增量; ThreadingHTTPServer 并发 -> 上锁。
# 值为 (create_time, Process): 用 create_time 校验身份, PID 复用时重置基线 (而非沿用旧句柄)。
_PROC_CACHE: dict[int, tuple[float, "psutil.Process"]] = {}
_LOCK = threading.Lock()
_SYS_PRIMED = False

# 短 TTL 记忆: 自动刷新 / 多标签页并发时共享同一帧, 不重复全量扫描 (轻量, 贴合零侵入)。
# 注意: 时间戳记在扫描*完成*时, 因为一次全量扫描本身就要 ~1-2s; 记开始时间会让 TTL 立即过期。
_SNAP_TTL = 6.0         # 全量扫描在重载机器 (~900 进程) 上要数秒 (create_time/memory_info/祖先提升), 拉长 TTL 免抖动
_LAST_SNAP: dict | None = None
_LAST_SNAP_T = 0.0

# ---- 命令行脱敏 (尽力而为) ----
# 1) URI 内嵌口令 scheme://user:pass@host (psql/redis/mongo/amqp 等最常见的泄漏形态), 用户名可空。
_URI_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]*):([^\s@/]+)@")
# 2) 已知厂商密钥前缀 (定长, 几乎不误伤路径/哈希)。
_KNOWN_SECRET_RE = re.compile(
    r"\b(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}"
    r"|sk_live_[0-9A-Za-z]+|sk-[0-9A-Za-z\-]{20,}|ghp_[0-9A-Za-z]{36}|gho_[0-9A-Za-z]{36}"
    r"|github_pat_[0-9A-Za-z_]+|xox[abprs]-[0-9A-Za-z\-]+"
    r"|eyJ[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-]+)\b")
# 3) 关键字=值 / "key": "值"。(?<![a-z0-9]) 让 AWS_SECRET_ACCESS_KEY= 这种下划线前缀也命中;
#    必须有分隔符 [\s=:]+ 才匹配, 避免误伤 keyboard/monkey 这类普通词; 值支持带空格的引号串。
_SECRET_RE = re.compile(
    r"""(?ix)
    (?<![a-z0-9])
    (token|api[-_]?key|key|secret|password|passwd|pwd|authorization|auth|bearer)
    ["']?[\s=:]+
    (?: (["'])(.*?)\2 | ([^\s"']+) )
    """)
# 4) 兜底: 40+ 长不透明串 (一般 token), 阈值取高避免误伤路径/哈希。
_BLOB_RE = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")


def available() -> bool:
    return _HAS_PSUTIL


# ---- Claude Code 会话活性索引 (给 activity 支柱当"进程还活着吗"的可选提示) ----
# 每个活着的 Claude Code 会话 = 一个 claude 进程。两路证据, 精确的优先:
#   1) 会话注册表 `~/.claude/sessions/<pid>.json` —— Claude Code 自己写的: 精确 pid->sessionId, 外加进程自报的
#      回合状态 status (busy / idle / waiting / shell) 与 waitingFor。只读、只本机; pid 必须活着且早于登记时刻 (防 PID 复用)。
#   2) 旧路径兜底 (没登记的进程 / 老版本): cmdline 里的 `--resume <uuid>` 精确到会话; 新开的会话只能靠 cwd 认到"项目级"。
# 据此给三值活性 (见 LiveIndex.status) 与回合状态 (LiveIndex.session_status)。缺 psutil -> 空索引 (全 None)。
_LIVE_TTL = 3.0
_LIVE_DECAY = 3            # OR 衰减: 把最近几帧活性做并集, 一次瞬时 cmdline 漏读不至于把"活"误翻成"未知/已结束"(评审 high#2)
_LIVE_CACHE: "LiveIndex | None" = None
_LIVE_CACHE_T = 0.0
_LIVE_HISTORY: list = []   # 最近 _LIVE_DECAY 帧的原始 (resume_cwds, live_cwds, open_cwds, registry)
_LIVE_LOCK = threading.Lock()
_REG_SLACK_S = 5.0         # 进程创建时刻最多可晚于登记时刻这么多秒 (时钟抖动); 再晚 = 登记它的是先前同 pid 的死进程
_REG_STATUSES = ("busy", "idle", "waiting", "shell")   # 注册表 status 的已知取值 (claude 二进制里的枚举)


def _norm_path(p: str | None) -> str | None:
    return os.path.normcase(os.path.normpath(p)) if p else None


def _resume_uuid(arg: str, nxt: str | None):
    """从一个 cmdline 参数解析 --resume 的 uuid。支持 `--resume X` / `-r X` / `--resume=X` / `-r=X` (评审 low#5)。"""
    if arg in ("--resume", "-r"):
        return nxt if (nxt and not nxt.startswith("-")) else None
    if arg.startswith("--resume="):
        return arg[len("--resume="):] or None
    if arg.startswith("-r="):
        return arg[len("-r="):] or None
    return None


class LiveIndex:
    """一帧 Claude Code 活性快照。

    resume_cwds: {会话uuid -> 进程cwd} (cmdline --resume, 仅没登记的进程); live_cwds: 所有活 claude 进程的 cwd;
    registry: {会话uuid -> 进程自报的回合状态} (注册表, 已校验 pid); open_cwds: **没登记**的活进程的 cwd
    (它们可能承载任何同项目会话)。open_cwds=None = 没有注册表可用, 退回旧口径 (用 live_cwds)。"""

    __slots__ = ("resume_cwds", "live_cwds", "registry", "open_cwds")

    def __init__(self, resume_cwds: dict, live_cwds: set, registry: dict | None = None,
                 open_cwds: set | None = None):
        self.resume_cwds = resume_cwds
        self.live_cwds = live_cwds
        self.registry = registry or {}
        self.open_cwds = open_cwds

    def status(self, session_id: str | None, cwd: str | None):
        """三值活性: True=该会话确有活进程 (注册表 / --resume 精确命中); False=确定已结束; None=未知 (不应据此覆盖 transcript)。

        False 只在 cwd 与所有**可能承载它**的活进程 cwd 都**无祖先/后代关系**时才敢下 —— 进程在项目根、会话 cd 进子目录
        (反之亦然) 都会 exact-mismatch, 若据此判 CLOSED 会把活着的会话误杀 (评审 critical#1)。cwd 缺失 -> None。
        已在注册表登记的进程只承载它登记的那个会话, 所以不算"可能承载": 同项目里所有活进程都登记了别的会话 -> 这个会话已关闭。"""
        if session_id and (session_id in self.registry or session_id in self.resume_cwds):
            return True
        if not self.live_cwds:          # 缺 psutil 或一个活 claude 进程都没有 -> 无法证伪, 保守 None
            return None
        n = _norm_path(cwd)
        if n is None:                   # 这条消息没记 cwd -> 认不到项目, 别妄下"已结束"
            return None
        cands = self.live_cwds if self.open_cwds is None else self.open_cwds
        sep = os.sep
        related = any(n == r or n.startswith(r + sep) or r.startswith(n + sep) for r in cands)
        return None if related else False   # P6: 宁可 None 也不误判 CLOSED

    def session_status(self, session_id: str | None) -> dict | None:
        """进程自报的回合状态 {status, waiting_for, status_at, started_at} (注册表); 没登记 -> None。"""
        return self.registry.get(session_id) if session_id else None


_EMPTY_LIVE = LiveIndex({}, set())


def _registry_dir():
    """Claude Code 会话注册表目录: $CLAUDE_CONFIG_DIR/sessions, 缺省 ~/.claude/sessions。"""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(root, "sessions")


def _is_claude_proc(name: str, cmdline: list) -> bool:
    """claude 原生二进制 (claude.exe / claude), 或 npm 装的 node 跑 claude-code —— 都算可能承载会话的进程。"""
    n = (name or "").lower()
    if n in ("claude.exe", "claude"):
        return True
    return n.startswith("node") and any("claude-code" in (a or "") for a in cmdline[:4])


def _registry_entry(raw: dict, create_time: float | None) -> dict | None:
    """校验并摘取一条注册表记录。纯函数 (create_time 由调用方从 psutil 取), 便于单测。

    PID 复用防护: 登记是进程启动后才写的, 所以真进程的 create_time 必然 <= startedAt (+抖动)。
    若同 pid 的进程比登记时刻还晚出生, 这条记录属于一个已死的先前进程 -> 丢弃。
    Windows 上还有精确校验: procStart 是进程创建时刻的 FILETIME (实测与 psutil create_time 逐一相差 0.000s)。"""
    if not isinstance(raw, dict) or create_time is None:
        return None
    sid = raw.get("sessionId")
    started = raw.get("startedAt")
    if not isinstance(sid, str) or not sid or not isinstance(started, (int, float)):
        return None
    started_s = started / 1000.0
    if create_time > started_s + _REG_SLACK_S:
        return None
    ps = raw.get("procStart")
    if str(raw.get("pidDomain") or "").startswith("win32") and isinstance(ps, str) and ps.isdigit():
        if abs(create_time - (int(ps) / 1e7 - 11644473600)) > 1.0:
            return None                      # 同 pid, 但不是登记它的那个进程
    st = raw.get("status")
    at = raw.get("statusUpdatedAt")
    wf = raw.get("waitingFor")
    return {
        "status": st if st in _REG_STATUSES else None,
        "waiting_for": wf if isinstance(wf, str) and wf else None,
        "status_at": at / 1000.0 if isinstance(at, (int, float)) else None,
        "started_at": started_s,
    }


def _read_registry(claude_pids: dict) -> tuple[dict, set] | None:
    """读注册表 -> ({sessionId: entry}, 已登记的 pid 集合)。目录不存在 (老版本 / 非默认布局) -> None (退回旧口径)。

    claude_pids: 本帧扫到的 {pid: create_time}; 不在其中的 pid (非 claude 进程名) 现场向 psutil 要 create_time。"""
    d = _registry_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return None
    reg: dict = {}
    pids: set = set()
    for fn in names:
        stem, ext = os.path.splitext(fn)
        if ext != ".json" or not stem.isdigit():
            continue
        pid = int(stem)
        ct = claude_pids.get(pid)
        if ct is None:
            try:
                ct = psutil.Process(pid).create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
                continue                     # 进程已退出: 这是崩溃留下的陈旧登记, 不算
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue                         # 正在被重写 / 坏文件: 本帧跳过, OR 衰减兜住
        e = _registry_entry(raw, ct)
        if e is None:
            continue
        e["pid"] = pid
        pids.add(pid)
        cur = reg.get(raw["sessionId"])
        # 两个活进程登记同一会话 (重载交接的重叠窗口): 以状态更新得最晚的那个为准, 不看 listdir 顺序
        if cur is None or (e["status_at"] or 0, e["started_at"]) > (cur["status_at"] or 0, cur["started_at"]):
            reg[raw["sessionId"]] = e
    return reg, pids


def live_claude_index() -> LiveIndex:
    """采一帧 Claude Code 活性索引 (短 TTL 共享 + OR 衰减平滑)。只读、只本机、不外发。无 psutil -> 空索引 (status 恒 None)。"""
    global _LIVE_CACHE, _LIVE_CACHE_T
    if not _HAS_PSUTIL:
        return _EMPTY_LIVE
    now = time.time()
    with _LIVE_LOCK:
        if _LIVE_CACHE is not None and (now - _LIVE_CACHE_T) < _LIVE_TTL:
            return _LIVE_CACHE
    procs: dict = {}                         # pid -> (cwd, cmdline, create_time)
    for p in psutil.process_iter(["name", "cmdline", "cwd", "create_time"]):
        try:
            cl = p.info["cmdline"] or []
            if not _is_claude_proc(p.info["name"], cl):
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        procs[p.pid] = (_norm_path(p.info.get("cwd")), cl, p.info.get("create_time"))
    got = _read_registry({pid: v[2] for pid, v in procs.items()})
    registry, reg_pids = got if got is not None else ({}, set())
    resume_cwds: dict = {}
    live_cwds: set = set()
    open_cwds: set | None = set() if got is not None else None
    for pid, (cwd, cl, _ct) in procs.items():
        if cwd:
            live_cwds.add(cwd)
        if pid in reg_pids:
            continue                         # 已登记: 它承载哪个会话以注册表为准 (cmdline --resume 可能已过时)
        if cwd and open_cwds is not None:
            open_cwds.add(cwd)
        for i, a in enumerate(cl):
            uid = _resume_uuid(a, cl[i + 1] if i + 1 < len(cl) else None)
            if uid:
                resume_cwds[uid] = cwd
    with _LIVE_LOCK:
        _LIVE_HISTORY.append((resume_cwds, live_cwds, open_cwds, registry))
        del _LIVE_HISTORY[:-_LIVE_DECAY]
        u_resume: dict = {}
        u_live: set = set()
        u_open: set | None = set()
        u_reg: dict = {}
        for rc, lc, oc, rg in _LIVE_HISTORY:  # 并集最近几帧: 单帧漏读不塌陷 True->None (评审 high#2)
            u_resume.update(rc)
            u_live |= lc
            u_open = None if (u_open is None or oc is None) else (u_open | oc)   # 任一帧没注册表 -> 旧口径
            u_reg.update(rg)                  # 从旧到新: 回合状态以最新一帧为准
        idx = LiveIndex(u_resume, u_live, u_reg, u_open)
        _LIVE_CACHE, _LIVE_CACHE_T = idx, time.time()
        return idx


def _redact(s: str) -> str:
    if not s:
        return s
    s = _URI_CRED_RE.sub(r"\1:***@", s)
    s = _KNOWN_SECRET_RE.sub("***", s)

    def _kv(m):
        kw = m.group(1)
        if m.group(2):                       # 带引号的值: 整段(含空格)脱敏
            q = m.group(2)
            return f"{kw} {q}***{q}"
        return f"{kw} ***"
    s = _SECRET_RE.sub(_kv, s)
    s = _BLOB_RE.sub("***", s)
    return s


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1") or ip.startswith("127.")


def _cpu_for(pid: int, ct: float, proc: "psutil.Process") -> float:
    """增量 CPU%。首见某进程, 或 PID 复用(create_time 变了) -> 重置基线, 本帧返回 0.0。已上锁调用。"""
    cached = _PROC_CACHE.get(pid)
    if cached is None or cached[0] != ct:
        _PROC_CACHE[pid] = (ct, proc)
        try:
            proc.cpu_percent(None)
        except Exception:
            pass
        return 0.0
    try:
        return round(cached[1].cpu_percent(None), 1)
    except Exception:
        _PROC_CACHE.pop(pid, None)
        return 0.0


def snapshot(top: int = 60, max_conns: int = 250) -> dict:
    """采一帧只读快照。无 psutil 抛 ImportError。短 TTL 内并发调用共享同一帧。"""
    if not _HAS_PSUTIL:
        raise ImportError("需要 psutil: pip install psutil")

    global _SYS_PRIMED, _LAST_SNAP, _LAST_SNAP_T
    with _LOCK:
        now = time.time()
        if _LAST_SNAP is not None and (now - _LAST_SNAP_T) < _SNAP_TTL:
            return _LAST_SNAP

        sys_cpu = psutil.cpu_percent(None)
        if not _SYS_PRIMED:
            sys_cpu = 0.0           # 系统级首帧也只是基线
            _SYS_PRIMED = True

        procs: dict[int, dict] = {}
        live = set()
        for p in psutil.process_iter(["pid", "name", "username", "create_time"]):   # 不取 ppid: 它每进程 OpenProcess, 900 进程要 15s
            try:
                pid = p.info["pid"]
                live.add(pid)
                try:
                    cmd = " ".join(p.cmdline())      # argv[0] 已含 exe 全路径, 分类用它即可 (p.exe() 每进程都太慢)
                except Exception:
                    cmd = p.info.get("name") or ""
                try:
                    rss = p.memory_info().rss
                except Exception:
                    rss = 0
                ct = p.info.get("create_time") or now
                procs[pid] = {
                    "pid": pid,
                    "name": p.info.get("name") or "?",
                    "user": p.info.get("username") or "",
                    "create_time": ct,               # 身份锚 (kill 时 PID 复用防护) + 祖先单调性守卫
                    "cpu": _cpu_for(pid, ct, p),
                    "rss": rss,
                    "uptime": max(0, int(now - ct)),
                    "cmd": _redact(cmd)[:300],       # 展示用 (脱敏截断)
                    "_raw_cmd": cmd,                 # 分类用原始 cmdline (未脱敏未截断, 含 argv[0] 全路径), 出快照前丢弃
                    "conns": 0,            # 下面补
                    "listen_ports": [],    # 下面补
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        for dead in [pid for pid in _PROC_CACHE if pid not in live]:
            _PROC_CACHE.pop(dead, None)

        snap = _assemble(procs, now, sys_cpu, top, max_conns)
        _LAST_SNAP, _LAST_SNAP_T = snap, time.time()   # 记完成时刻 (扫描本身耗时, 否则 TTL 立即过期)
        return snap


# ---- 本地服务健康探测 (opt-in: 用户点按钮才跑, 不在每次快照里跑) ----
# 立场: 仅探本机可经 loopback 触达的端口 (127.0.0.1 / ::1)。先 TCP 连一次判定 开/关,
# 开着再在同一条连接上发 HTTP HEAD —— 单连接、不对死端口写任何字节、短超时、并发受限、用户主动触发。
# 诚实说明: 这是一次「主动连接」, 不是被动观测; HEAD 在 HTTP 层只读幂等, 但对非 HTTP 服务
# (DB/SMTP 等) 那几个字节会被它当作非法输入、记一条协议错误日志并断开 —— 不改数据, 但不是"零接触"。

def _probe_one(port: int, hosts: tuple = ("127.0.0.1", "::1"), timeout: float = 0.5) -> dict:
    t0 = time.time()
    for host in hosts:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
        except Exception:
            continue                          # 该地址族连不上, 试下一个 (IPv4/IPv6)
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.sock = s                     # 复用已开连接: 不二次连接, 也不对死端口写字节
            conn.request("HEAD", "/")         # HEAD: HTTP 层只读幂等
            r = conn.getresponse()
            ms = int((time.time() - t0) * 1000)
            conn.close()
            return {"port": port, "state": "http", "status": r.status, "ms": ms}
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            return {"port": port, "state": "open", "status": None,
                    "ms": int((time.time() - t0) * 1000)}
    return {"port": port, "state": "down", "status": None,
            "ms": int((time.time() - t0) * 1000)}


def probe_health(ports: list[int] | None = None, limit: int = 60) -> dict:
    """探测本机监听端口的存活/响应 (opt-in)。ports=None 时自动取本机可经 127.0.0.1/::1 触达的口。

    每个结果带 pid (来自探测时的快照), 让前端把结果绑定到「当时的占用进程」,
    避免端口被别的进程重绑后, 旧探测结果张冠李戴 (真实优先)。
    """
    if not _HAS_PSUTIL:
        raise ImportError("需要 psutil: pip install psutil")

    if ports is None:
        # 自动取本机可经 loopback 触达的服务: loopback 绑定 + all-interface(0.0.0.0/::) 绑定。
        snap = snapshot()
        meta: dict[int, dict] = {}
        for l in snap["listening"]:
            addr = l["addr"]
            if not (l["loopback"] or addr in ("0.0.0.0", "::", "")):
                continue
            ipv6 = addr in ("::", "::1")
            m = meta.get(l["port"])
            if m is None:
                meta[l["port"]] = {"v6": ipv6, "pid": l["pid"]}
            elif not ipv6:
                m["v6"] = False               # 同端口若也有 IPv4 绑定 -> IPv4 优先
        items = [(p, ("::1", "127.0.0.1") if m["v6"] else ("127.0.0.1", "::1"), m["pid"])
                 for p, m in meta.items()]
    else:
        items = [(p, ("127.0.0.1", "::1"), None) for p in set(ports)]

    items = sorted(items, key=lambda x: x[0])[:limit]   # 排序 + 上限, 防止一次探太多
    if not items:
        return {"probed_at_epoch": int(time.time()), "results": []}
    with ThreadPoolExecutor(max_workers=min(32, len(items))) as ex:
        results = list(ex.map(lambda it: {**_probe_one(it[0], it[1]), "pid": it[2]}, items))
    results.sort(key=lambda r: r["port"])
    return {"probed_at_epoch": int(time.time()), "results": results}


# ---- 进程分类 (只读) + 终止执行器 (受 P7 控制面把守) ----
# 只详列这三类; 其余求和为「其他」。终止只允许这三类 —— 且在 kill 时用**新鲜扫描**服务端二次校验类别 + 身份,
# 客户端送来的 PID/类别只是"建议", 绝不据此下杀 (防伪造/重放/PID 复用)。分类含糊一律落到 other(只读), P6 安全方向。
_KILLABLE = ("vscode", "cloudflare", "railway")
_ANCESTRY_MAX_HOPS = 12
_KILL_LOCK = threading.Lock()   # 串行化 kill 的全量新鲜扫描: 连点/重放不会同时开好几个数秒扫描 (自我 DoS 防护)
# 标记一律用正斜杠; 分类时把 cmdline 归一成正斜杠 (Git Bash/WSL 用 /, 原生 exe 路径用 \)。
# 只匹配**可执行映像 argv[0]**, 不匹配参数 —— 否则一条"参数里带 .vscode 路径"的无关命令(如 robocopy ...\.vscode\extensions...)
# 会被误判成 VS Code 并变得可终止 (评审发现: 直接破坏白名单)。整棵 VS Code 进程树靠 _promote_ancestry 按祖先认。
_VSCODE_EXE_MARKS = ("anthropic.claude-code", "/.vscode/extensions/", "/microsoft vs code/")
# railway 只认 railway 专属 bin 路径 (npm scope / .bin), 不用 endswith('railway') 或裸文件夹名 (那会误判普通脚本)。
_RAILWAY_SCRIPT_MARKS = ("/@railway/", "node_modules/.bin/railway")


def _cat_direct(pr: dict):
    """直接信号分类 (优先级 Cloudflare > Railway > VS Code); 认不出返回 None。只看 name + argv[0] (可执行映像),
    绝不看其余参数 —— 参数里带某路径不代表这进程属于那一类 (评审: 防"参数含 .vscode 路径"被误判成可终止)。"""
    name = (pr.get("name") or "").lower()
    toks = (pr.get("_raw_cmd") or "").lower().replace("\\", "/").split()   # 归一斜杠
    argv0 = toks[0] if toks else ""
    if "cloudflared" in name:
        return "cloudflare"
    if name == "railway.exe" or name.startswith("railway"):
        return "railway"
    if name in ("node.exe", "node"):
        script = toks[1] if len(toks) > 1 else ""      # 只认 railway 专属 bin 路径, 不认裸文件夹名
        if any(m in script for m in _RAILWAY_SCRIPT_MARKS):
            return "railway"
    if name in ("code.exe", "claude.exe"):
        return "vscode"
    if any(m in argv0 for m in _VSCODE_EXE_MARKS):     # 只看 argv[0]: 进程自身就是 VS Code/扩展的可执行体
        return "vscode"
    return None


def _categorize(procs: dict[int, dict]) -> None:
    """直接信号分类 (name + cmdline)。**不查 ppid** (它每进程 OpenProcess, 900 进程要 15s) —— 终端里生出的 dev server
    靠 _promote_ancestry 按需惰性提升 (只对监听端口 owner / 待终止目标查 ppid)。含糊一律 other (只读, P6 安全方向)。"""
    for pr in procs.values():
        pr["category"] = _cat_direct(pr) or "other"
        pr["terminable"] = pr["category"] in _KILLABLE


def _ppid_of(pid: int, cache: dict):
    """惰性取 ppid (每次 OpenProcess, 故只按需查 + memo)。"""
    if pid in cache:
        return cache[pid]
    try:
        pp = psutil.Process(pid).ppid()
    except Exception:
        pp = None
    cache[pid] = pp
    return pp


def _promote_ancestry(procs: dict[int, dict], pids, cache: dict) -> None:
    """把「其实是 VS Code 终端里生出来的」进程从 other 提升为 vscode: 沿 ppid 往上找到 VS Code 直接根。
    只对给定 pids 惰性查 ppid, 不全量扫 (保性能)。祖先跨用户 / 时间倒挂(复用假边) / 撞到 Cloudflare·Railway 即停。"""
    for pid in pids:
        pr = procs.get(pid)
        if pr is None or pr["category"] != "other":
            continue
        cur = pid
        for _ in range(_ANCESTRY_MAX_HOPS):
            pp = _ppid_of(cur, cache)
            if pp is None or pp == cur:
                break
            parent = procs.get(pp)
            if parent is None:
                break
            if parent["create_time"] > procs[cur]["create_time"] + 1.0:
                break                                   # 父晚于子 -> ppid 复用的假边
            if (parent.get("user") or "") != (pr.get("user") or ""):
                break                                   # 跨用户不认 (防串进系统/服务子树)
            if parent["category"] in ("cloudflare", "railway"):
                break                                   # 不穿过别的类
            if parent["category"] == "vscode":
                pr["category"] = "vscode"
                pr["terminable"] = True
                break
            cur = pp


def _fresh_proc_table() -> dict:
    """一帧全量进程表 (含分类), **绕过 TTL** —— 专供 kill 时服务端二次校验用, 重放请求不能骑旧帧。"""
    procs: dict[int, dict] = {}
    for p in psutil.process_iter(["pid", "name", "username", "create_time"]):
        try:
            pid = p.info["pid"]
            try:
                raw = " ".join(p.cmdline())
            except Exception:
                raw = p.info.get("name") or ""
            procs[pid] = {
                "pid": pid, "name": p.info.get("name") or "?", "user": p.info.get("username") or "",
                "create_time": p.info.get("create_time") or 0.0, "_raw_cmd": raw,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    _categorize(procs)
    return procs


def _act(pid: int, expect_ct: float, action: str, table: dict) -> dict:
    """对单个 pid 执行终止, 带 create_time 身份复核 + 自我保护。返回 {pid, outcome}。"""
    if pid == os.getpid():
        return {"pid": pid, "outcome": "refuse-self"}      # 别杀掉监控自身
    try:
        po = psutil.Process(pid)
        if abs(po.create_time() - expect_ct) > 0.1:    # 两端都是服务端 psutil 读同一 OS create_time, 收紧容差把 PID 复用窗口压到近零
            return {"pid": pid, "outcome": "pid-reused"}
        # 诚实: Windows 上 terminate() 与 kill() 都是 TerminateProcess (无真 SIGTERM)。分级只在 POSIX 上真温和。
        (po.kill if action == "kill" else po.terminate)()
        try:
            po.wait(timeout=3)
        except psutil.TimeoutExpired:
            pass
        alive = po.is_running()
        return {"pid": pid, "outcome": "still-alive" if alive else ("killed" if action == "kill" else "terminated")}
    except psutil.NoSuchProcess:
        return {"pid": pid, "outcome": "gone"}
    except (psutil.AccessDenied, PermissionError):
        return {"pid": pid, "outcome": "access-denied"}
    except Exception:
        return {"pid": pid, "outcome": "error"}


def terminate(pid: int, expect_create, action: str = "terminate") -> dict:
    """终止一个进程。**新鲜扫描 + 服务端二次校验**: 身份(create_time)对不上 -> pid-reused; 类别不在白名单 -> 拒绝。
    目标可能是终端里生出的 dev server (直接信号认不出), 故对它单独做一次惰性祖先提升再判白名单。"""
    if not _HAS_PSUTIL:
        return {"ok": False, "reason": "no-psutil"}
    if expect_create is None:
        return {"ok": False, "reason": "identity-required"}
    try:
        expect_ct = float(expect_create)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "identity-required"}
    if pid == os.getpid():
        return {"ok": False, "reason": "refuse-self"}
    with _KILL_LOCK:                                       # 串行化新鲜扫描 + 动作
        table = _fresh_proc_table()
        pr = table.get(pid)
        if pr is None:
            return {"ok": False, "reason": "gone"}
        if abs(pr["create_time"] - expect_ct) > 1.0:
            return {"ok": False, "reason": "pid-reused"}   # 你看到的那个进程已经没了, 现在这个 PID 是别人的
        _promote_ancestry(table, [pid], {})                # 目标若是 VS Code 终端里生出来的, 提升为 vscode
        if pr["category"] not in _KILLABLE:
            return {"ok": False, "reason": "not-terminable", "category": pr["category"]}
        res = _act(pid, pr["create_time"], action, table)
        return {"ok": True, "pid": pid, "name": pr["name"], "category": pr["category"],
                "action": action, "outcome": res["outcome"]}


def free_port(port: int, action: str = "terminate") -> dict:
    """释放一个端口 = 终止其监听进程 (每个 owner 走同样的白名单+身份复核 + 祖先提升)。多 owner / 认不到 owner 都如实报。"""
    if not _HAS_PSUTIL:
        return {"ok": False, "reason": "no-psutil"}
    owners = set()
    unknown = False
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN":
                if c.pid:
                    owners.add(c.pid)
                else:
                    unknown = True
    except (psutil.AccessDenied, PermissionError):
        return {"ok": False, "reason": "access-denied"}
    if not owners:
        return {"ok": False, "reason": "owner-unknown" if unknown else "no-listener"}
    with _KILL_LOCK:
        table = _fresh_proc_table()
        _promote_ancestry(table, list(owners), {})
        results = []
        for pid in owners:
            pr = table.get(pid)
            if pr is None:
                results.append({"pid": pid, "outcome": "gone"})
            elif pr["category"] not in _KILLABLE:
                results.append({"pid": pid, "outcome": "not-terminable", "category": pr["category"]})
            else:
                results.append({"pid": pid, "outcome": _act(pid, pr["create_time"], action, table)["outcome"],
                                "name": pr["name"], "category": pr["category"]})
        return {"ok": True, "port": port, "action": action, "results": results}


def _assemble(procs: dict[int, dict], now: float, sys_cpu: float, top: int, max_conns: int) -> dict:
    mem = psutil.virtual_memory()

    listening: list[dict] = []
    active: list[dict] = []        # 真·活动连接: ESTABLISHED 的 TCP + 已连 UDP
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        conns = []

    for c in conns:
        pid = c.pid
        pinfo = procs.get(pid) if pid else None
        pname = pinfo["name"] if pinfo else "?"
        proto = "udp" if c.type == socket.SOCK_DGRAM else "tcp"
        if c.status == "LISTEN" and c.laddr:
            listening.append({
                "port": c.laddr.port, "addr": c.laddr.ip,
                "loopback": _is_loopback(c.laddr.ip),
                "pid": pid, "name": pname,
                "cmd": pinfo["cmd"] if pinfo else "",
            })
            if pinfo is not None:
                pinfo["listen_ports"].append(c.laddr.port)
        elif c.raddr and ((proto == "tcp" and c.status == "ESTABLISHED") or proto == "udp"):
            # 只数真正活动的: TCP ESTABLISHED + 已连 UDP (含 cloudflared 的 QUIC 边缘)。
            # TIME_WAIT/CLOSE_WAIT/SYN_SENT 等垂死/半开状态不计入, 否则会虚高 (真实优先)。
            if pinfo is not None:
                pinfo["conns"] += 1
            active.append({
                "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                "raddr_ip": c.raddr.ip if c.raddr else "",
                "rport": c.raddr.port if c.raddr else 0,
                "proto": proto,
                "status": c.status if c.status and c.status != "NONE" else proto.upper(),
                "pid": pid, "name": pname,
            })

    # 监听端口去重 (同进程 IPv4/IPv6 双栈会重复); 每进程的 listen_ports 也去重。
    seen = set()
    uniq_listen = []
    for l in sorted(listening, key=lambda x: (not x["loopback"], x["port"])):
        k = (l["port"], l["addr"], l["pid"])
        if k not in seen:
            seen.add(k)
            uniq_listen.append(l)
    for pinfo in procs.values():
        pinfo["listen_ports"] = sorted(set(pinfo["listen_ports"]))

    # 分类 (只读) —— 必须在丢弃 _raw_cmd 之前跑
    _categorize(procs)
    # 监听端口 owner 常是终端里生出来的 dev server (直接信号认不出); 只对这几十个 pid 惰性查 ppid 做祖先提升, 不全量扫
    _promote_ancestry(procs, [l["pid"] for l in listening if l.get("pid")], {})
    cat_procs: dict = {"vscode": [], "cloudflare": [], "railway": []}
    other_count = 0
    other_rss = 0
    for pinfo in procs.values():
        c = pinfo["category"]
        if c in cat_procs:
            cat_procs[c].append(pinfo)
        else:
            other_count += 1
            other_rss += pinfo.get("rss", 0)
    for pinfo in procs.values():          # 出快照前丢弃分类用的未脱敏 cmdline (privacy)
        pinfo.pop("_raw_cmd", None)
    categories: dict = {}
    for c, lst in cat_procs.items():
        lst.sort(key=lambda x: x["rss"], reverse=True)
        categories[c] = {
            "procs": lst,
            "count": len(lst),
            "rss": sum(p["rss"] for p in lst),
            "listen_ports": sorted({port for p in lst for port in p.get("listen_ports", [])}),
        }
    categories["other"] = {"count": other_count, "rss": other_rss}   # 只求和, 不列个体 (用户不想看)

    # 监听端口行补上 owner 的类别 (前端据此决定能否显示"释放端口"按钮; 服务端 kill 时仍会二次校验)
    for l in listening:
        po = procs.get(l["pid"]) if l["pid"] else None
        l["category"] = po["category"] if po else "other"
        l["terminable"] = bool(po and po.get("terminable"))
        l["create_time"] = po["create_time"] if po else None

    cloudflared = sorted(
        (dict(pinfo) for pinfo in procs.values() if "cloudflared" in pinfo["name"].lower()),
        key=lambda x: x["pid"],
    )

    # 进程列表 = 内存 Top N ∪ CPU Top N (单看内存会漏掉高 CPU 低内存的进程)。
    by_rss = sorted(procs.values(), key=lambda x: x["rss"], reverse=True)[:top]
    by_cpu = sorted(procs.values(), key=lambda x: x["cpu"], reverse=True)[:top]
    pseen = set()
    proc_list = []
    for p in by_rss + by_cpu:
        if p["pid"] not in pseen:
            pseen.add(p["pid"])
            proc_list.append(p)

    # 按远端 IP 聚合: 一眼看清「谁在连哪里」, 而不是一长串连接表。纯本地聚合, 不做反向 DNS (那是网络外发)。
    by_remote: dict[str, dict] = {}
    for c in active:
        ip = c["raddr_ip"] or "?"
        e = by_remote.setdefault(ip, {"raddr_ip": ip, "count": 0, "procs": set(), "rports": set()})
        e["count"] += 1
        e["procs"].add(c["name"])
        if c["rport"]:
            e["rports"].add(c["rport"])
    remote_list = sorted(
        ({"raddr_ip": e["raddr_ip"], "count": e["count"],
          "procs": sorted(e["procs"]), "rports": sorted(e["rports"])[:12]}
         for e in by_remote.values()),
        key=lambda x: x["count"], reverse=True,
    )

    active.sort(key=lambda x: (x["name"] or "").lower())
    return {
        "generated_at_epoch": int(now),
        "host": {
            "cpu_percent": round(sys_cpu, 1),
            "mem_percent": round(mem.percent, 1),
            "mem_used": mem.used, "mem_total": mem.total,
            "proc_count": len(procs),
        },
        "cloudflared": cloudflared,
        "categories": categories,
        "listening": uniq_listen,
        "processes": proc_list,
        "proc_total": len(procs),
        "connections": active[:max_conns],
        "conn_total": len(active),
        "by_remote": remote_list,
    }
