"""控制台支柱 (CONSOLES_PLAN S1): 只读控制台写下的文件, 把三种方言统一成一张标签表。

你给项目配的「控制台」(用 Claude Agent SDK 派 agent 的中控) 派出的会话, 都是 entrypoint=sdk-* 的 Claude Code 会话,
transcript 照常进 ~/.claude/projects —— tokmon 算得到它们的钱, 却认不出它们是谁。这里把它们认出来:

    session_id -> Label(控制台代号, 运行, 角色, agent, 项目, 粒度)

再给每次运行一张任务时间表 + 每个 agent 的派活窗口, 展示层用 `task_of()` 把一条条记录切到任务上
(一个会话常常跨好几个任务: 中控贯穿整次运行, 工人一次次被派活)。

边界 (CONSOLES_PLAN §2 + 2026-10-29 你的选择):
- **只读白名单**: <runs_dir>/*/events.jsonl、<runs_dir>/*/state.json、E 的会话台账 (registry)、
  B 的 <runs_dir>/*/agents/*.json —— 只取 id / session_id / role / kind 四个键, 其余 (任务说明、最后一段话…) 当场丢掉。
  **绝不读** *.token、daemon.log、agents/*.jsonl、.env; 不请求控制台的 HTTP 接口。
- **不挡控制台的原子保存**: 控制台在 Windows 上靠「写临时文件再 rename 覆盖」保存 state.json / agents json;
  只要有人拿着这个文件的句柄, rename 就失败 (实测: 连 FILE_SHARE_DELETE 打开的也挡; C 不重试, 会直接抛错)。
  所以这类文件**只在它安静了 SETTLE_S 秒之后才读** (控制台刚写过 = 很可能马上又写), 一次读完立刻关;
  没安静下来就先用上一份 (新会话晚几秒认出来, 好过让控制台存档失败)。连判断「变没变」也不碰文件本身:
  os.stat 在 Windows 上也会短暂开一个句柄, 所以大小 / 修改时刻从**目录列表**里拿 (os.scandir, 不开文件)。
  台账 / events 只追加不 rename, 不受影响。
- events.jsonl 增量尾读: 按 (size, mtime) 记偏移, 只读到最后一个换行 (正在写的半行不读), 只解析用得到的几种类型。
- 支柱独立 (P4): 只 import stdlib + project。不 import 成本内核 / activity / trace / procmon, 也不读 transcript ——
  会话的 cwd / entrypoint 由调用方传进来 (A 只能按 cwd 认)。
- 认不准就不认: 宁可让会话留在「未认领」, 也不猜一个控制台给它 (P6)。

三种方言 (各代控制台的细节见 CONSOLES_PLAN §3):
- office (C / D / E): state.json 的 agents[].spec.session_id 精确到会话; E 另有会话台账 (含中控换代前的旧会话与自测探针);
  派活窗口: C 的 dispatches、D / E 的 assignments。
- missiond (B): <mission>/agents/<aid>.json 的 session_id 精确到会话 (白名单只取四个键); 没有 json 时按 worktree 目录认到运行。
- conductor (A): 从不记 session_id, 且正常收尾会删掉自己的 transcript; 按 cwd 认: workspace 下的到运行级,
  锦标赛 entry / 裁判目录精确到 agent。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

from .project import workspace_identity

TTL_S = 5.0                         # 快照最多这么久重建一次 (重建本身也只读变了的文件)
SETTLE_S = 5.0                      # 会被 rename 覆盖保存的文件: 最后一次修改之后安静这么久才去读 (见模块说明)
_CONFIG = {"path": None}            # None -> ~/.tokmon/consoles.json (测试用 set_config 指到别处)
_LOCK = threading.Lock()
_CACHE: dict = {"t": 0.0, "snap": None, "cfg_sig": None}
_FILES: dict[str, tuple] = {}       # path -> ((size, mtime), 解析结果)   state.json / agents json / 台账
_TAILS: dict[str, "_Tail"] = {}     # events.jsonl -> 尾读状态

SELF_TEST = "自测"                  # E 的自测探针会话: 运行一栏与角色一栏都写它
_A_FIXTURE = "00000000-"            # A 的合成样例运行 (界面自测用), 不算真运行
_RE_B_AGENT_DIR = re.compile(r"--(a\d+)$", re.I)
_RE_TN = re.compile(r"^T\d+$")


def set_config(path) -> None:
    """测试 / 调用方指定控制台清单的位置 (None = 默认 ~/.tokmon/consoles.json)。清空缓存。"""
    with _LOCK:
        _CONFIG["path"] = Path(path) if path else None
        _CACHE.update(t=0.0, snap=None, cfg_sig=None)
        _FILES.clear()
        _TAILS.clear()


def config_path() -> Path:
    return _CONFIG["path"] or (Path.home() / ".tokmon" / "consoles.json")


# ---------------------------------------------------------------- 数据形状

@dataclass(frozen=True)
class Label:
    console: str        # 代号 A–E
    run: str            # 运行目录名; 自测会话是「自测」; 认不到运行是 ""
    role: str           # 规范角色 (各代的 kind): conductor / core / worker / verifier / critic / seat / helper /
                        # scout / researcher / judge / competitor / 自测; 运行级认不出角色是 ""
    agent: str          # 运行内的 agent id (core / w01 / a03 / entry 的选手 …); 不知道是 ""
    project: str        # 控制台服务的项目 (按 project_root 的项目身份)
    grain: str          # "session" 精确到会话 / "run" 只认到运行 / "console" 只认到控制台


@dataclass
class RunInfo:
    console: str
    run: str
    started: float | None = None
    finished: float | None = None
    status: str = ""                                   # 控制台最近报的状态 (running / idle / done / failed …)
    task_starts: list = field(default_factory=list)    # [(开始时刻, 任务键)] 按时间排序
    task_status: dict = field(default_factory=dict)    # 任务键 -> 结束状态
    ledger: dict | None = None                         # office: state.json ledger.total (对账用, 不当真值)
    # ---- S2 状态机的料 (都是控制台自己写下的事实; 工单只记 id / 种类 / 开关时刻 / 是否阻塞, **不记题目和正文**)
    tickets: dict = field(default_factory=dict)        # 工单 id -> {"kind", "opened", "closed", "blocking"}
    park_until: float | None = None                    # 控制台报的额度停泊到几点
    parked: bool = False                               # 控制台报的状态本身就是「等额度」(不一定知道到几点)
    budget_level: str = ""                             # 控制台最近报的预算级别: ok / warn / soft_stop / hard_stop
    budget_at: float | None = None                     # B: 由 meter.warning 推出的告警是什么时候 (过了 5 小时就不作数)
    last_seen: float | None = None                     # events.jsonl 最后一次被写的时刻 (控制台还活着没有)

    def pending(self) -> list:
        """没答的阻塞工单 [(id, 种类, 开的时刻)], 按开的先后。"""
        out = [(k, t["kind"], t["opened"]) for k, t in self.tickets.items()
               if t["blocking"] and t["closed"] is None and t["opened"] is not None]
        return sorted(out, key=lambda x: x[2])

    def waiting_at(self, t: float) -> bool:
        """回放用: 时刻 t 这次运行有没有没答的阻塞工单 (S2 判据 ①)。"""
        return any(x["blocking"] and x["opened"] is not None and x["opened"] <= t
                   and (x["closed"] is None or t < x["closed"]) for x in self.tickets.values())

    def state(self, now: float) -> str:
        """规范化的运行状态: finished / waiting_you / parked / budget / running / idle (先后即优先级)。"""
        if self.finished is not None:
            return "finished"
        if self.pending():
            return "waiting_you"
        if self.parked or (self.park_until is not None and self.park_until > now):
            return "parked"
        if self.status in _RUNNING and self.last_seen is not None and now - self.last_seen > STALE_S:
            return "stale"            # 报在跑, 但事件流很久没动: 多半是控制台被关掉了 (A 被杀掉的运行就停在 running)
        if self.budget_level in ("warn", "soft_stop", "hard_stop") and not (
                self.budget_at is not None and now - self.budget_at > 5 * 3600):
            return "budget"
        if self.status in _RUNNING:
            return "running"
        return "idle"

    def outcomes(self) -> dict:
        """各任务的结束状态计数 (done / partial / failed …)。"""
        out: dict = {}
        for v in self.task_status.values():
            out[v] = out.get(v, 0) + 1
        return out


_RUNNING = frozenset({"running", "planning", "starting", "waiting", "finishing", "closing", "stopping"})
STALE_S = 2 * 3600                  # 报在跑、但事件流这么久没动 -> 「没动静」, 不再说它在跑


def _ticket_open(info: RunInfo, tid, kind, ts, blocking=True) -> None:
    if tid in (None, ""):
        return
    t = info.tickets.setdefault(str(tid), {"kind": "", "opened": None, "closed": None, "blocking": True})
    t["kind"] = str(kind or t["kind"] or "")
    t["opened"] = t["opened"] if t["opened"] is not None else ts
    t["blocking"] = bool(blocking)


def _ticket_close(info: RunInfo, tid, ts) -> None:
    t = info.tickets.get(str(tid)) if tid not in (None, "") else None
    if t is not None and t["closed"] is None:
        t["closed"] = ts if ts is not None else (t["opened"] or 0.0)


@dataclass
class Console:
    code: str
    name: str
    dialect: str
    url: str
    project: str
    under_anchor: bool = False      # 项目根在 .vscode 下 (「仅 .vscode」筛选时, 改了项目的会话跟着项目走)


class Snapshot:
    """一帧控制台数据。只读; 每 TTL_S 秒由 snapshot() 换新的一帧。"""

    def __init__(self):
        self.consoles: list[Console] = []
        self.labels: dict[str, Label] = {}              # session_id -> Label (精确认到的)
        self.runs: dict[tuple[str, str], RunInfo] = {}
        self.windows: dict[tuple[str, str, str], list] = {}   # (代号, 运行, agent) -> [(t0, t1, 任务键)] 派活窗口
        self.problems: list[str] = []                   # 读不了的文件 / 对不上的记录 (doctor 用, 不含内容)
        self._cwd_rules: list[tuple] = []               # (代号, 规范化的根, 方言, 项目, 附加数据)
        self._cwd_memo: dict[str, Label | None] = {}

    # ---- 查询

    def label(self, session_id: str | None, cwd: str | None = None, entrypoint: str | None = None) -> Label | None:
        """会话 -> 标签。先按 session_id 精确认; 认不到时只对 sdk 会话按 cwd 认 (A 的运行目录、B 的 worktree)。"""
        if session_id:
            lab = self.labels.get(session_id) or self.labels.get(session_id.lower())
            if lab is not None:
                return lab
        if not cwd or not (entrypoint or "").startswith("sdk"):
            return None
        if cwd not in self._cwd_memo:
            self._cwd_memo[cwd] = self._by_cwd(cwd)
        return self._cwd_memo[cwd]

    def task_of(self, lab: Label | None, ts: float) -> str:
        """这条记录 (时刻 ts) 属于这次运行的哪个任务; 认不到是 ""。

        先看这个 agent 的派活窗口 (工人一次次被派到不同任务), 再按运行的任务时间表: 落在最近一个已开始的任务上
        (与控制台自己记账的规则一致: 任务结束后、下一个开始前的收尾也记在上一个任务上)。"""
        if lab is None or not lab.run or lab.grain == "console":
            return ""
        for t0, t1, key in reversed(self.windows.get((lab.console, lab.run, lab.agent), ())):
            if t0 <= ts <= t1:                                    # 按开始时刻排好、互不重叠 (见 _normalize_windows)
                return key
        info = self.runs.get((lab.console, lab.run))
        if info is None or not info.task_starts:
            return ""
        i = bisect_right(info.task_starts, (ts, "\uffff"))
        return info.task_starts[i - 1][1] if i else ""

    def console(self, code: str) -> Console | None:
        return next((c for c in self.consoles if c.code == code), None)

    # ---- 按 cwd 认 (A / B)

    def _by_cwd(self, cwd: str) -> Label | None:
        n = _norm(cwd)
        for code, root, dialect, project, extra in self._cwd_rules:
            if not n.startswith(root + "/"):
                continue
            rest = n[len(root) + 1:].split("/")
            if dialect == "conductor":
                return _a_label(code, project, rest, extra)
            if dialect == "missiond":
                return _b_label(code, project, rest, extra)
        return None


# ---------------------------------------------------------------- 入口

def snapshot(force: bool = False) -> Snapshot:
    """当前一帧 (最多 TTL_S 秒前建的)。清单不存在 -> 空帧 (没配控制台)。线程安全。"""
    now = time.time()
    with _LOCK:
        snap = _CACHE["snap"]
        if snap is not None and not force and now - _CACHE["t"] < TTL_S:
            return snap
        try:
            snap = _build()
        except Exception as e:                                    # 可选支柱: 永远不让 /sessions /tokens 因它 500
            snap = Snapshot()
            snap.problems.append(f"读取控制台清单出错 ({type(e).__name__})")
        _CACHE.update(t=now, snap=snap)
        return snap


def _build() -> Snapshot:
    snap = Snapshot()
    try:
        raw = _read_shared(config_path())
        cfg = json.loads(raw.decode("utf-8-sig")) if raw is not None else {}    # 记事本存的带 BOM 也认
    except (ValueError, UnicodeDecodeError):
        snap.problems.append("consoles.json 不是合法 JSON")
        cfg = {}
    cons = cfg.get("consoles") if isinstance(cfg, dict) else None
    if cons is not None and not isinstance(cons, list):
        snap.problems.append("consoles.json: consoles 不是列表")
        cons = None
    for c in cons or []:
        if not isinstance(c, dict) or not c.get("code") or not isinstance(c.get("runs_dir"), str):
            continue
        code, dialect = str(c["code"]), str(c.get("dialect") or "")
        root = c.get("project_root")
        if root is not None and not isinstance(root, str):
            snap.problems.append(f"{code}: project_root 不是字符串")
            root = None
        wid = workspace_identity(root) if root else None
        con = Console(code, str(c.get("name") or code), dialect, str(c.get("url") or ""),
                      wid.project if wid else code, bool(wid and wid.under_anchor))
        snap.consoles.append(con)
        try:
            if dialect == "office":
                _office(snap, con, c)
            elif dialect == "missiond":
                _missiond(snap, con, c)
            elif dialect == "conductor":
                _conductor(snap, con, c)
            else:
                snap.problems.append(f"{code}: 不认识的方言 {dialect!r}")
        except Exception as e:                            # 一个控制台坏了不连累别的 (渐进降级)
            snap.problems.append(f"{code}: 读取出错 ({type(e).__name__})")
    return snap


# ---------------------------------------------------------------- office (C / D / E)

def _office(snap: Snapshot, con: Console, c: dict) -> None:
    code = con.code
    run_of_tn: dict[str, str] = {}
    agent_kind: dict[tuple[str, str], str] = {}                    # (运行, agent) -> 规范角色
    for rdir in _run_dirs(c["runs_dir"]):
        run = rdir.name
        info = snap.runs.setdefault((code, run), RunInfo(code, run))
        st = _json_file(rdir / "state.json", snap, sig=_dir_sigs(rdir, ("state.json",)).get("state.json"))
        if isinstance(st, dict):
            led = (st.get("ledger") or {}).get("total")
            info.ledger = led if isinstance(led, dict) else None
            for a in st.get("agents") or []:
                spec = (a or {}).get("spec") or {}
                aid = str(spec.get("id") or "")
                role = _office_role(spec)
                agent_kind[(run, aid)] = role
                sid = spec.get("session_id")
                if isinstance(sid, str) and sid:
                    snap.labels.setdefault(sid.lower(), Label(code, run, role, aid, con.project, "session"))
            _office_windows(snap, code, run, st)
            for src in (st.get("requests") or [], st.get("tasks") or []):
                for x in src:
                    if isinstance(x, dict) and _RE_TN.match(str(x.get("tn") or "")):
                        run_of_tn.setdefault(x["tn"], run)
            for tn in (st.get("tnums") or {}):
                if _RE_TN.match(str(tn)):
                    run_of_tn.setdefault(tn, run)
        status_ts = None                                          # 最近一条改状态的事件的时刻
        for ev in _events(rdir / "events.jsonl", _OFFICE_TYPES, snap):
            t, d = ev.get("type"), ev.get("data") or {}
            ts = _num(ev.get("ts"))
            if t == "session.started":
                info.started = info.started or ts
            elif t == "session.status" and d.get("status"):
                info.status, status_ts = str(d["status"]), ts
            elif t == "session.finished":
                info.finished = ts
                info.status = str(d.get("status") or info.status)
            elif t == "task.started" and ts is not None:
                key = str(d.get("tn") or d.get("index") or "")
                if key:
                    info.task_starts.append((ts, key))
            elif t == "task.finished":
                key = str(d.get("tn") or d.get("index") or "")
                if key:
                    info.task_status[key] = str(d.get("status") or "")
            elif t == "ticket.opened":
                # C / D 的工单没有「是否阻塞」: 只有中控能开、开了就等你 (控制台源码核实, CONSOLES_PLAN §3.2);
                # E 的工单带 blocking, 不阻塞的当场按推荐项默认采纳 (ticket.adopted), 不算等你
                _ticket_open(info, d.get("id"), d.get("kind"), ts, d.get("blocking", True) is not False)
            elif t in ("ticket.answered", "ticket.superseded", "ticket.adopted"):
                _ticket_close(info, d.get("id"), ts)
            elif t == "budget" and d.get("level"):
                info.budget_level = str(d["level"])
            elif t == "quota.park" and not d.get("injected"):
                info.park_until = _num(d.get("until"))
            elif t == "quota.resume":
                info.park_until, info.parked, status_ts = None, False, ts
            elif t == "agent.parked" and d.get("cause") == "quota" and _num(d.get("until")):
                info.park_until = max(info.park_until or 0.0, _num(d.get("until")))
            elif t == "agent.successor":
                # E 的中控换代: 新旧两个会话都属于这个 agent (state.json 只留新的那个)
                aid = str(ev.get("agent") or d.get("successor") or "")
                for k in ("from_session", "to_session"):
                    sid = d.get(k)
                    if isinstance(sid, str) and sid:
                        snap.labels.setdefault(sid.lower(), Label(code, run, agent_kind.get((run, aid), aid), aid,
                                                                  con.project, "session"))
        # state.json 是「现在」—— 但我们手里的可能是缓存的旧一份 (控制台一直在存档时, 安静了才重读, 见 SETTLE_S):
        # 只有它比最近的事件新, 才拿它盖掉事件给出的状态 / 重新打开事件里已经关掉的工单
        hit = _FILES.get(str(rdir / "state.json"))
        st_t = hit[0][1] / 1e9 if hit and hit[1] is st and st is not None else None
        fresh = st_t is not None and (status_ts is None or st_t >= status_ts)
        if isinstance(st, dict) and st.get("status") and fresh:
            info.status = str(st["status"])
        if isinstance(st, dict):
            _office_state_now(info, st, st_t, fresh)
        info.parked = info.status == "waiting_quota"
        _seen(info, rdir)
        info.task_starts.sort()
    reg = c.get("registry")
    if reg:
        _registry(snap, con, Path(reg), run_of_tn, agent_kind)


_OFFICE_TYPES = ("session.started", "session.status", "session.finished", "task.started", "task.finished",
                 "agent.successor", "ticket.opened", "ticket.answered", "ticket.superseded", "ticket.adopted",
                 "budget", "quota.park", "quota.resume", "agent.parked")


def _office_state_now(info: RunInfo, st: dict, st_t: float | None = None, fresh: bool = True) -> None:
    """state.json 是「现在」: 工单的当前状态 (开 / 答) 以它为准; 运行关了就是结束了; 额度停到几点。
    st_t: 这一份 state.json 的修改时刻 —— 比事件里关单的时刻还旧的, 不许把工单重新打开 (不然答过的工单会再响)。"""
    for tid, x in (st.get("tickets") or {}).items():
        if not isinstance(x, dict):
            continue
        opened = _num(x.get("opened"))
        _ticket_open(info, x.get("id") or tid, x.get("kind"), opened, x.get("blocking", True) is not False)
        t = info.tickets.get(str(x.get("id") or tid))
        if t is None:
            continue
        if t["opened"] is None:
            t["opened"] = opened
        if x.get("status") == "open":
            if t["closed"] is None or (st_t is not None and st_t > t["closed"]):
                t["closed"] = None                           # 事件里关过、这份更新的 state.json 又开着 (重开): 以它为准
        elif t["closed"] is None:
            t["closed"] = _num(x.get("answered")) or opened or 0.0
    if st.get("status") == "closed" and info.finished is None:
        info.finished = _num(st.get("saved_at")) or _num(st.get("heartbeat")) or info.started or 0.0
    q = st.get("quota") if isinstance(st.get("quota"), dict) else {}
    pu = _num(q.get("paused_until"))
    if fresh and pu and (info.park_until is None or pu > info.park_until):
        info.park_until = pu                                 # 旧的一份不能把 quota.resume 之后的停泊又写回来


def _office_role(spec: dict) -> str:
    if spec.get("critic"):
        return "critic"
    return str(spec.get("kind") or spec.get("shell") or "")


def _office_windows(snap: Snapshot, code: str, run: str, st: dict) -> None:
    """派活窗口: C 的 dispatches{no: {agent, task_index, started, finished}}, D/E 的 assignments{A-nnn: {agent, task|tn, started, ended}}。"""
    for sect, t1k, keyk in (("dispatches", "finished", ("task_index",)), ("assignments", "ended", ("tn", "task"))):
        for x in (st.get(sect) or {}).values():
            if not isinstance(x, dict):
                continue
            t0, t1 = _num(x.get("started")), _num(x.get(t1k))
            key = next((str(x[k]) for k in keyk if x.get(k) not in (None, "")), "")
            if t0 is None or not key or not x.get("agent"):
                continue
            snap.windows.setdefault((code, run, str(x["agent"])), []).append((t0, t1 if t1 is not None else float("inf"), key))
    _normalize_windows(snap, code, run)


def _normalize_windows(snap: Snapshot, code: str, run: str) -> None:
    """同一个 agent 的派活窗口按开始时刻排好; 没写结束 (或和下一段重叠) 的窗口截到下一段开始。
    只有最后一段可以开着 (正在做的那件活)。不规整的话, 一段没结束的旧窗口会把后面所有的都盖住。"""
    for k, ws in snap.windows.items():
        if k[0] != code or k[1] != run:
            continue
        ws.sort(key=lambda w: w[0])
        for i in range(len(ws) - 1):
            t0, t1, key = ws[i]
            nxt = ws[i + 1][0]
            if t1 > nxt:
                ws[i] = (t0, nxt, key)


def _registry(snap: Snapshot, con: Console, path: Path, run_of_tn: dict, agent_kind: dict) -> None:
    """E 的会话台账: 一行 = (会话, T<n>, agent) 第一次起进程。自测探针的行 (role=probe:… / task 不是 T<n>) 记成「自测」。"""
    rows = _registry_rows(path, snap)
    by_sid: dict[str, list] = {}
    for r in rows:
        sid = str(r.get("session_id") or "").lower()
        if sid:
            by_sid.setdefault(sid, []).append(r)
    for sid, rs in by_sid.items():
        if sid in snap.labels:
            continue
        probe = all(str(r.get("role") or "").startswith("probe:") or not _RE_TN.match(str(r.get("task") or ""))
                    for r in rs)
        if probe:
            snap.labels[sid] = Label(con.code, SELF_TEST, SELF_TEST, "", con.project, "console")
            continue
        run = next((run_of_tn[str(r.get("task"))] for r in rs if str(r.get("task")) in run_of_tn), "")
        if not run:
            snap.problems.append(f"{con.code}: 台账里有会话的任务号对不上任何运行")
            continue
        r0 = next((r for r in rs if r.get("agent")), rs[0])
        aid = str(r0.get("agent") or "")
        role = agent_kind.get((run, aid)) or str(r0.get("shell") or r0.get("role") or "")
        snap.labels[sid] = Label(con.code, run, role, aid, con.project, "session")


# ---------------------------------------------------------------- missiond (B)

def _missiond(snap: Snapshot, con: Console, c: dict) -> None:
    code = con.code
    missions = []
    for rdir in _run_dirs(c["runs_dir"]):
        run = rdir.name
        missions.append(run)
        info = snap.runs.setdefault((code, run), RunInfo(code, run))
        adir = rdir / "agents"
        if adir.is_dir():
            sigs = _dir_sigs(adir, None, ".json")              # 只有 .json (白名单); agents/*.jsonl 永远不碰
            for name in sorted(sigs):
                f = adir / name
                a = _json_file(f, snap, keep=("id", "session_id", "role", "kind"), sig=sigs[name])
                if not isinstance(a, dict):
                    continue
                sid = a.get("session_id")
                if isinstance(sid, str) and sid:
                    aid = str(a.get("id") or f.stem)
                    role = "conductor" if aid == "conductor" else str(a.get("kind") or a.get("role") or "")
                    snap.labels.setdefault(sid.lower(), Label(code, run, role, aid, con.project, "session"))
        for ev in _events(rdir / "events.jsonl", _MISSIOND_TYPES, snap):
            t, d = ev.get("type"), ev.get("data") or {}
            ts = _num(ev.get("ts"))
            if t == "mission.created":
                info.started = info.started or ts
            elif t == "mission.state" and d.get("state"):
                prev, info.status = info.status, str(d["state"])
                if d["state"] in ("closed", "failed"):
                    info.finished = ts
                # blocked = 等你 (B 唯一的运行级等你状态); 离开 blocked 就关掉这张「工单」
                if d["state"] == "blocked":
                    _ticket_open(info, f"blocked@{ts}", d.get("kind") or "blocked", ts)
                elif prev == "blocked":
                    for k, x in info.tickets.items():
                        if k.startswith("blocked@") and x["closed"] is None:
                            x["closed"] = ts
                if d["state"] == "parked":
                    info.park_until = _num(d.get("until")) or info.park_until
                elif prev == "parked":
                    info.park_until = None
            elif t == "question.asked":
                _ticket_open(info, "q:" + str(d.get("question_id") or ""), "question", ts)   # B 的提问都是阻塞的 (必须写 why_blocking)
            elif t == "question.answered":
                _ticket_close(info, "q:" + str(d.get("question_id") or ""), ts)
            elif t == "approval.requested":
                _ticket_open(info, "a:" + str(d.get("approval_id") or ""), "approval", ts)
            elif t == "approval.resolved":
                _ticket_close(info, "a:" + str(d.get("approval_id") or ""), ts)
            elif t == "agent.parked" and d.get("reason") == "quota" and _num(d.get("until")):
                info.park_until = max(info.park_until or 0.0, _num(d.get("until")))     # idle 那种停泊不是额度
            elif t == "meter.warning" and d.get("meter") in ("five_hour", "quota_share", "time_box"):
                info.budget_level, info.budget_at = "warn", ts    # ram / capacity / sessions 是机器负载, 不是预算
            elif t == "task.started" and ts is not None and d.get("n") is not None:
                info.task_starts.append((ts, str(d["n"])))
            elif t == "task.finished" and d.get("n") is not None:
                info.task_status[str(d["n"])] = str(d.get("status") or "")
        _seen(info, rdir)
        info.task_starts.sort()
    wt = c.get("worktrees_dir")
    if wt:
        snap._cwd_rules.append((code, _norm(wt), "missiond", con.project, set(missions)))


_MISSIOND_TYPES = ("mission.created", "mission.state", "task.started", "task.finished", "question.asked",
                   "question.answered", "approval.requested", "approval.resolved", "agent.parked", "meter.warning")


def _b_label(code: str, project: str, rest: list, missions: set) -> Label | None:
    """B 的 worktree: <worktrees_dir>/<mission>[--aNN]/… —— 段名去掉 --aNN 后必须和某个运行目录名完全一致。"""
    seg = rest[0] if rest else ""
    m = _RE_B_AGENT_DIR.search(seg)
    mission = seg[:m.start()] if m else seg
    hit = next((x for x in missions if x.lower() == mission), None)
    if hit is None:
        return None
    return Label(code, hit, "", m.group(1) if m else "", project, "run")


# ---------------------------------------------------------------- conductor (A)

def _conductor(snap: Snapshot, con: Console, c: dict) -> None:
    code = con.code
    tours: dict[str, dict] = {}                         # 运行 -> {tid: (entries, judge_ids)}
    for rdir in _run_dirs(c["runs_dir"]):
        run = rdir.name
        if run.startswith(_A_FIXTURE):
            continue
        info = snap.runs.setdefault((code, run), RunInfo(code, run))
        for ev in _events(rdir / "events.jsonl", ("run.started", "run.status", "run.finished", "task.started",
                                                   "task.finished", "tournament.started", "budget", "quota"), snap):
            t, d = ev.get("type"), ev.get("data") or {}
            ts = _num(ev.get("ts"))
            if t == "run.started":
                info.started = info.started or ts
            elif t == "run.status" and d.get("status"):
                info.status = str(d["status"])
            elif t == "run.finished":
                info.finished = ts
                info.status = str(d.get("status") or info.status)
            elif t == "task.started" and ts is not None and d.get("index") is not None:
                info.task_starts.append((ts, str(d["index"])))
            elif t == "task.finished" and d.get("index") is not None:
                info.task_status[str(d["index"])] = str(d.get("status") or "")
            elif t == "budget" and d.get("level"):
                info.budget_level = str(d["level"])
            elif t == "quota":                                    # A 没有停泊: 被拒 (rejected) 就等到重置
                info.park_until = _num(d.get("resets_at")) if d.get("status") == "rejected" else None
            elif t == "tournament.started" and d.get("tid"):
                ent = d.get("entries") if isinstance(d.get("entries"), dict) else {}
                tours.setdefault(run.lower(), {})[str(d["tid"]).lower()] = (
                    {str(k).lower(): str(v) for k, v in ent.items()}, [str(x) for x in d.get("judge_ids") or []])
        if not info.task_starts and info.started is not None:
            info.task_starts.append((info.started, "1"))         # 老运行没有 task.* 事件: 整次运行算任务 1
        _seen(info, rdir)
        info.task_starts.sort()
    snap._cwd_rules.append((code, _norm(c["runs_dir"]), "conductor", con.project,
                            {"tours": tours, "runs": {k[1].lower(): k[1] for k in snap.runs if k[0] == code}}))


def _a_label(code: str, project: str, rest: list, extra: dict) -> Label | None:
    """A 的 cwd: <runs_dir>/<运行>/workspace[/…] -> 运行级; …/tournaments/tN/entry_X -> 那名选手; …/tournaments/tN -> 裁判。"""
    if not rest or rest[0].startswith(_A_FIXTURE.lower()):
        return None
    run = extra["runs"].get(rest[0])
    if run is None:
        return None
    if len(rest) >= 3 and rest[1] == "tournaments":
        entries, judges = extra["tours"].get(rest[0], {}).get(rest[2], ({}, []))
        if len(rest) >= 4 and rest[3] in entries:
            return Label(code, run, "competitor", entries[rest[3]], project, "session")
        if len(rest) == 3:
            return Label(code, run, "judge", judges[0] if len(judges) == 1 else "", project,
                         "session" if len(judges) == 1 else "run")
    return Label(code, run, "", "", project, "run")


# ---------------------------------------------------------------- 文件读取 (共享删除, 读完即关)

def _seen(info: "RunInfo", rdir: Path) -> None:
    sig = _sig(rdir / "events.jsonl")                         # 只追加的文件: stat 不挡控制台
    info.last_seen = sig[1] / 1e9 if sig else None


def _run_dirs(runs_dir) -> list[Path]:
    try:
        return sorted(p for p in Path(runs_dir).iterdir() if p.is_dir())
    except OSError:
        return []


def _open_shared(path: Path):
    """只读打开, 共享读 / 写 / 删除 (Windows 上用 CreateFileW, 尽量少挡别人; 非 Windows 就是普通 open)。
    注意: 共享删除并不能让控制台的 rename 覆盖在我们拿着句柄时成功 —— 真正的保护是 SETTLE_S + 读完即关。"""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)   # GENERIC_READ, 共享读写删, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateFileW failed", str(path))
    fd = msvcrt.open_osfhandle(h, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    return os.fdopen(fd, "rb")


def _read_shared(path: Path) -> bytes | None:
    try:
        with _open_shared(path) as f:
            return f.read()
    except OSError:
        return None


def _sig(path: Path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _dir_sigs(d: Path, names: tuple | None = None, suffix: str = "") -> dict:
    """目录里文件的 (大小, 修改时刻), **从目录列表拿, 不开文件本身** (Windows 上 scandir 的数据来自 FindNextFileW;
    os.stat 则会短暂开一个句柄, 正好可能撞上控制台的 rename)。names: 只要这几个名字; suffix: 只要这个后缀的。"""
    out = {}
    try:
        with os.scandir(d) as it:
            for e in it:
                n = e.name
                if (names is not None and n not in names) or (suffix and not n.lower().endswith(suffix)):
                    continue
                try:
                    if e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        out[n] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
    except OSError:
        pass
    return out


def _json_file(path: Path, snap: Snapshot, keep: tuple | None = None, sig=None):
    """整份读 JSON (按 size+mtime 缓存)。keep: 只留这几个键 (B 的 agents json —— 其余字段读进来就丢)。
    sig: 调用方从目录列表拿到的 (大小, 修改时刻) —— 这类会被 rename 覆盖保存的文件, 读之前不碰它本身。"""
    if sig is None:
        return None
    key = str(path)
    hit = _FILES.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    if time.time() - sig[1] / 1e9 < SETTLE_S:
        return hit[1] if hit else None               # 控制台刚写过: 先用上一份, 等它安静了再读 (不挡它的 rename)
    raw = _read_shared(path)
    if raw is None:                                  # 打不开 (共享冲突 / 瞬时 IO 错误): 不缓存, 先用上一份, 下一帧再试
        snap.problems.append(f"读不了 {path.name}")
        return hit[1] if hit else None
    try:
        val = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        snap.problems.append(f"读不了 {path.name}")   # 正好读到写了一半? 不缓存坏结果, 先用上一份, 下一帧再读
        return hit[1] if hit else None
    if keep is not None and isinstance(val, dict):
        val = {k: val[k] for k in keep if k in val}
    _FILES[key] = (sig, val)
    return val


def _registry_rows(path: Path, snap: Snapshot) -> list[dict]:
    sig = _sig(path)
    if sig is None:
        return []
    key = str(path)
    hit = _FILES.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    raw = _read_shared(path)
    if raw is None:                                  # 打不开: 不缓存空台账, 先用上一份, 下一帧再试
        snap.problems.append(f"读不了 {path.name}")
        return hit[1] if hit else []
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(o, dict):
            rows.append({k: o.get(k) for k in ("session_id", "task", "role", "agent", "shell")})   # 只留认人要的键
    _FILES[key] = (sig, rows)
    return rows


class _Tail:
    __slots__ = ("sig", "offset", "events")

    def __init__(self):
        self.sig, self.offset, self.events = None, 0, []


def _events(path: Path, types: tuple, snap: Snapshot) -> list[dict]:
    """events.jsonl 里这几种类型的事件 (增量尾读; 文件变短 = 被重写 -> 从头读)。只留信封 + data。"""
    key = str(path) + "|" + ",".join(types)
    tail = _TAILS.get(key)
    if tail is None:
        tail = _TAILS[key] = _Tail()
    sig = _sig(path)
    if sig is None:
        return tail.events
    if sig == tail.sig:
        return tail.events
    if sig[0] < tail.offset:
        tail.offset, tail.events = 0, []                         # 截断 / 重写
    try:
        with _open_shared(path) as f:
            f.seek(tail.offset)
            chunk = f.read()
    except OSError:
        snap.problems.append(f"读不了 {path.parent.name}/{path.name}")
        return tail.events
    end = chunk.rfind(b"\n") + 1                                 # 只要完整的行
    marks = tuple(t.encode() for t in types)
    for line in chunk[:end].splitlines():
        if not any(m in line for m in marks):
            continue
        try:
            o = json.loads(line.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(o, dict) and o.get("type") in types:
            tail.events.append({"type": o.get("type"), "ts": o.get("ts"), "agent": o.get("agent"),
                                "data": o.get("data") if isinstance(o.get("data"), dict) else {}})
    tail.offset += end
    tail.sig = (tail.offset, sig[1]) if end == len(chunk) else None   # 有半行没读: 下一帧再看
    return tail.events


# ---------------------------------------------------------------- 小工具

def _norm(p: str) -> str:
    """路径比较用: 斜杠统一、去掉 /c/ 这类前缀差异、大小写不敏感、去尾斜杠。"""
    s = str(p).replace("\\", "/").rstrip("/").lower()
    m = re.match(r"^/([a-z])/", s)
    if m:                                                        # /c/Users/… (Git Bash 风格) -> c:/users/…
        s = m.group(1) + ":/" + s[3:]
    return s


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
