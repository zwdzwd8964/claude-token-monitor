"""工作流追踪支柱: 把一次提问触发的全部动作还原成「调用树 + 时间轴」(`/workflow` 的数据源)。

[WORKFLOW_TAB_PLAN.md] 的 S1 数据层。它回答——
「这次任务是怎么完成的: 调了哪些 skill / 工具 / MCP / 子 agent / workflow, 各花了多少时间和 token?」

立场 (同 activity / procmon):
- **纯只读**: 只读 transcript JSONL 与 workflow 的 meta / 脚本文件, 绝不写 (I1)。
- **支柱独立 (P4)**: 只 import stdlib + discovery + project。**不 import** 成本内核 (parser/pricing/aggregate)
  也不 import activity / procmon —— $ 换算、脱敏、运行态都在 serve 层汇合。
- **真实优先 (原则 1)**: 三级诚实, 每个数字都知道自己是哪一级:
    · 真值: 层级、调用起止时间、API usage 汇总、skill 归属 (attributionSkill)、失败 (is_error / 后台失败通知);
    · 估算 ≈: 调用级「发起成本」(响应 output ÷ 该响应里的调用数) 与「结果体积」(按字符估; 图片按尺寸估);
    · 推断: 重试 / 原地打转 / 统计意义上的「慢」(标签里写明)。
  拿不到的就是拿不到: 文件缺失 / 没有结果 / 含无法估算的块 -> None, **绝不填 0**。
- **token 与 /tokens 同一契约**: 按 (message.id, requestId) 去重, 同一响应多行时**最后一行**为准
  (实测 5703/5703 次最后一行 output 最大; 与 parser「后写覆盖」一致) -> 任务真值能和 /tokens 对账。
- **时间只算有活动的区间**: 相邻两条活动行间隔 > IDLE_GAP 视为空闲, 不计入「模型生成」
  (对抗式 review 实测: 旧算法把 18 分钟的任务报成 40 小时)。

数据事实 (2026-09-21 探针实测, 见 WORKFLOW_TAB_PLAN §2):
- 任务边界 = user 行 `origin.kind == "human"`; 后台完成通知 = `origin.kind == "task-notification"`
  (或 `queued_command` 附件, commandMode=task-notification), 内含 `<tool-use-id>` 与 `<status>`;
- 你中途的插话 = `queued_command` 附件 (commandMode=prompt); 打断 = "[Request interrupted by user" 文本;
- Agent 结果带 `agentId` -> `<会话>/subagents/**/agent-<id>.jsonl` (+ `.meta.json`: description / workflowPhase);
- Workflow 结果带 `runId` -> `<会话>/subagents/workflows/<runId>/`, 脚本在 `<会话>/workflows/scripts/*-<runId>.js`;
- Skill 正文以 isMeta 行注入, 带 `sourceToolUseID` 指回 Skill 调用 (它才是 Skill 真正的上下文成本);
- `attributionSkill` **并不总是被打上** -> 只信标记, 不补猜;
- `<synthetic>` 模型的行是 Claude Code 的占位 (如恢复会话时的 "No response requested."), 0 token, 不是回复。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .discovery import default_base, friendly_project
from .project import workspace_identity

# ---------------------------------------------------------------- 常量

HUMAN_WAIT_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})   # 这些调用的时长 = 在等你
AGENT_TOOLS = frozenset({"Agent", "Task"})
HUGE_RESULT_CHARS = 40_000          # 约 1 万 token: 一次返回就把上下文撑大一截
SLOW_MIN_SAMPLES = 20               # 样本不足不判「慢」(不瞎猜)
SLOW_MIN_SECONDS = 1.0              # 再慢也得 >= 1s 才值得标
LOOP_MIN_REPEATS = 3                # 同一动作、同一输出 >= 3 次 -> 原地打转 (推断)
IDLE_GAP = 900.0                    # 两条活动行相隔超过 15 分钟 -> 中间算空闲, 不算模型生成
IMAGE_TOKEN_CAP = 1600              # 单张图片的 token 上限 (≈ w*h/750, 大图会被缩放)
PREVIEW_CHARS = 280
TEXT_CHARS = 600
MAX_DEPTH = 8
FALLBACK_SLACK = 2.0                # 按指令回链时, 子 agent 首行时间允许的误差 (秒)
SYNTHETIC_MODEL = "<synthetic>"

# 活动行: 只有这些行能撑起「模型生成」区间与任务的时间范围 (系统事件 / 本地命令不算)
ACT_KINDS = frozenset({"prompt", "resp", "call", "result", "say", "think", "interject", "interrupt", "notify"})
BG_FAIL = frozenset({"failed", "failure", "error", "errored"})
BG_STOPPED = frozenset({"stopped", "killed", "cancelled", "canceled", "aborted"})

# 纯噪声附件: 不进回放 (系统提醒 / 环境快照 / 工具清单增量 等)
_NOISE_ATTACHMENTS = frozenset({
    "total_tokens_reminder", "batching_reminder_sent", "environment", "prompt_snapshot",
    "deferred_tools_delta", "deferred_tools_record", "date", "remote_session_change", "instructions",
    "skill_listing", "session_context", "mcp_instructions_delta", "auto_mode", "structured_output",
    "silent_turn_reminder", "command_permissions", "agent_listing_delta", "file", "todo_reminder",
    "read_truncation_notice", "ultra_effort_enter", "model",
})

_RE_TOOL_USE_ID = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")
_RE_TASK_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_RE_SUMMARY = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.S)
_RE_STATUS = re.compile(r"<status>\s*(.*?)\s*</status>", re.S)
_RE_WAIT_CMD = re.compile(r"^\s*(sleep|start-sleep|until|while|timeout|wait)\b", re.I)
_RE_WAIT_LABEL = re.compile(r"^\s*(wait|poll|等待|轮询)", re.I)
_SECRETISH = r"[A-Za-z0-9_\-+/=.]"


# ================================================================ 纯函数

def parse_ts(s) -> float | None:
    """ISO 时间戳 -> epoch 秒。读不出 -> None (调用方跳过, 不瞎填)。"""
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def est_tokens(text: str | None) -> int:
    """按字符估 token (≈): ASCII 约 4 字符/token, 非 ASCII (中日韩等) 约 1 字符/token。**只是估算**。"""
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return int(ascii_n / 4 + (len(text) - ascii_n) + 0.999)


def image_tokens(data_b64: str | None) -> int | None:
    """从 base64 图片头读出宽高, 估 token ≈ min(1600, w*h/750)。读不出尺寸 -> None (不瞎估)。"""
    if not data_b64 or not isinstance(data_b64, str):
        return None
    chunk = data_b64[:8000]
    try:
        head = base64.b64decode(chunk + "=" * (-len(chunk) % 4), validate=False)
    except (ValueError, TypeError):
        return None
    w = h = None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            w, h = struct.unpack(">II", head[16:24])
        elif head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    break
                seg = struct.unpack(">H", head[i + 2:i + 4])[0]
                i += 2 + seg
        elif head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
            w, h = struct.unpack("<HH", head[6:10])
    except struct.error:
        return None
    if not w or not h:
        return None
    return int(min(IMAGE_TOKEN_CAP, w * h / 750) + 0.999)


def split_time(intervals) -> dict:
    """把一组带标签的时间区间投影到墙钟上, 拆成「机器执行 / 模型生成 / 等你」三段。

    intervals: [(t0, t1, label)], label ∈ {"wait", "machine", "model"}。
    同一时刻被多个区间覆盖时按优先级取一个: **wait > machine > model**
    (在等你 = 关键路径卡在你; 其次只要有工具在跑就是机器在干活; 其余活跃时间 = 模型在想/写)。
    并行区间按**并集**计, 不重复累加; 没有任何区间覆盖的空档不计入 active (那是真空闲)。
    返回 {"wait","machine","model","active","span"} (秒)。
    """
    rank = {"model": 1, "machine": 2, "wait": 3}
    names = {1: "model", 2: "machine", 3: "wait"}
    evs = []
    lo = hi = None
    for t0, t1, lab in intervals:
        if t0 is None or t1 is None or lab not in rank:
            continue
        if t1 < t0:
            t0, t1 = t1, t0
        lo = t0 if lo is None else min(lo, t0)
        hi = t1 if hi is None else max(hi, t1)
        if t1 > t0:
            evs.append((t0, 1, rank[lab]))
            evs.append((t1, -1, rank[lab]))
    out = {"wait": 0.0, "machine": 0.0, "model": 0.0, "active": 0.0,
           "span": (hi - lo) if lo is not None else 0.0}
    evs.sort()
    live = {1: 0, 2: 0, 3: 0}
    prev = None
    i = 0
    while i < len(evs):
        t = evs[i][0]
        if prev is not None and t > prev:
            top = max((r for r, n in live.items() if n > 0), default=0)
            if top:
                out[names[top]] += t - prev
        while i < len(evs) and evs[i][0] == t:
            live[evs[i][2]] += evs[i][1]
            i += 1
        prev = t
    out["active"] = out["wait"] + out["machine"] + out["model"]
    return out


def active_intervals(ts: list[float], gap: float = IDLE_GAP) -> list[tuple[float, float, str]]:
    """活动时间点 -> 「模型生成」打底区间: 相邻两点间隔 <= gap 才连成区间, 否则中间是空闲。"""
    ts = sorted(t for t in ts if t is not None)
    return [(a, b, "model") for a, b in zip(ts, ts[1:]) if 0 < b - a <= gap]


def norm_key(name: str, inp: dict | None) -> str:
    """把一次调用归一成「做的是不是同一件事」的键 (重试 / 原地打转识别用, **推断**)。"""
    inp = inp or {}

    def ws(s):
        return re.sub(r"\s+", " ", str(s or "")).strip()

    if name in ("Bash", "PowerShell"):
        return f"{name}|{ws(inp.get('command'))}"
    if name == "Read":
        return f"Read|{inp.get('file_path')}|{inp.get('offset')}|{inp.get('limit')}"
    if name == "Edit":
        return f"Edit|{inp.get('file_path')}|{ws(inp.get('old_string'))[:120]}"
    if name == "Write":
        c = str(inp.get("content") or "")
        return f"Write|{inp.get('file_path')}|{hashlib.sha1(c.encode('utf-8', 'replace')).hexdigest()[:12]}"
    if name in ("Grep", "Glob"):
        return f"{name}|{inp.get('pattern')}|{inp.get('path')}|{inp.get('glob')}"
    if name == "WebFetch":
        return f"WebFetch|{inp.get('url')}"
    if name == "WebSearch":
        return f"WebSearch|{ws(inp.get('query'))}"
    try:
        return name + "|" + json.dumps(inp, sort_keys=True, ensure_ascii=False)[:400]
    except (TypeError, ValueError):
        return name + "|" + str(inp)[:400]


def flag_repeats(calls: list[dict]) -> None:
    """在**同一条时间线**(同一个 agent)内识别重试与原地打转, 原地往 flags 里追加。**推断**。

    - retry: 与此前某次**失败**的调用是同一件事 (同 key) -> 又试了一次;
    - loop:  同一件事**且结果也一样** (没有可观察的进展) 连续第 LOOP_MIN_REPEATS 次及以后。
      结果变了 (比如改了代码再跑测试, 输出不同) 就重新计数 —— 那是正常的「改 → 重跑」循环, 不是打转。
    - 后台启动 (async) 不参与判断。
    calls: 按时间排好的 [{"key", "failed", "out", "async", "flags": [...]}]。
    """
    failed = set()
    streak: dict = {}                       # key -> (上次输出签名, 连续同输出次数)
    for c in calls:
        k = c.get("key")
        if not k or c.get("async"):
            continue
        if k in failed and "retry" not in c["flags"]:
            c["flags"].append("retry")
        out = c.get("out")
        prev = streak.get(k)
        n = prev[1] + 1 if (prev and out is not None and prev[0] == out) else 1
        streak[k] = (out, n)
        if n >= LOOP_MIN_REPEATS and "loop" not in c["flags"]:
            c["flags"].append("loop")
        if c.get("failed"):
            failed.add(k)


def p90(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * 0.9))]


def tool_category(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    if name == "Skill":
        return "skill"
    if name in AGENT_TOOLS:
        return "agent"
    if name == "Workflow":
        return "workflow"
    if name in HUMAN_WAIT_TOOLS:
        return "ask"
    return "builtin"


def skill_norm(s: str | None) -> str:
    """"anthropic-skills:docs" 与 attributionSkill "docs" 视为同一个 skill。"""
    return str(s or "").split(":")[-1].strip()


def is_deliberate_wait(name: str, inp: dict | None) -> bool:
    """故意的等待 (sleep / 轮询 / Monitor) —— 它们慢是设计使然, 不参与「慢」的判断与基线。"""
    if name == "Monitor":
        return True
    inp = inp or {}
    if name in ("Bash", "PowerShell"):
        if _RE_WAIT_CMD.match(str(inp.get("command") or "")):
            return True
        return bool(_RE_WAIT_LABEL.match(str(inp.get("description") or "")))
    return False


def short(s, n=PREVIEW_CHARS, flatten=True) -> str:
    """截断给人看的文字。**不把一个密钥样的长串切成半截**: 截断点落在字母数字串中间时, 把这串尾巴整段丢掉
    (serve 层的正则脱敏认不出半截密钥, 先截后脱会漏出片段 —— 对抗式 review 实测漏出过 73 字符的 JWT)。"""
    s = str(s or "")
    if flatten:
        s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    head = s[: n - 1]
    if re.match(_SECRETISH, s[n - 1]):
        head = re.sub(_SECRETISH + r"{8,}$", "", head)
    return head + "…"


def _norm_hash(s: str | None) -> str | None:
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    return hashlib.sha1(t.encode("utf-8", "replace")).hexdigest() if t else None


def call_label(name: str, inp: dict | None) -> tuple[str, str]:
    """(主标签, 次标签)。主标签给人读 (Bash 优先用它自带的 description), 次标签给精确值。"""
    inp = inp or {}

    def base(p):
        return str(p or "").replace("\\", "/").rstrip("/").split("/")[-1]

    if name in ("Bash", "PowerShell"):
        cmd = short(inp.get("command"), 160)
        desc = short(inp.get("description"), 90)
        return (desc or cmd or name, cmd if desc else "")
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        p = inp.get("file_path") or inp.get("notebook_path")
        return (base(p) or name, short(p, 160))
    if name == "Grep":
        return (f"\"{short(inp.get('pattern'), 60)}\"", short(inp.get("path") or inp.get("glob") or "", 120))
    if name == "Glob":
        return (short(inp.get("pattern"), 80), short(inp.get("path") or "", 120))
    if name == "WebFetch":
        return (short(re.sub(r"^https?://", "", str(inp.get("url") or "")), 90), "")
    if name == "WebSearch":
        return (short(inp.get("query"), 90), "")
    if name == "Skill":
        return (str(inp.get("skill") or inp.get("command") or "?"), short(inp.get("args"), 120))
    if name in AGENT_TOOLS:
        return (short(inp.get("description"), 90) or "子 agent", str(inp.get("subagent_type") or ""))
    if name == "Workflow":
        return (short(inp.get("name") or inp.get("description") or "workflow", 90), "")
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        q0 = qs[0].get("question") if qs and isinstance(qs[0], dict) else ""
        return (short(q0, 90) or "向你提问", f"共 {len(qs)} 题" if len(qs) > 1 else "")
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        tool = parts[2] if len(parts) > 2 else name
        args = ", ".join(f"{k}={short(v, 24)}" for k, v in list(inp.items())[:3])
        return (tool, short(args, 140))
    for v in inp.values():                      # 兜底: 第一个字符串参数
        if isinstance(v, str) and v.strip():
            return (short(v, 90), "")
    return (name, "")


def text_of(content) -> str:
    """tool_result / 消息 content -> 纯文本 (图片 / 文档记占位, **只供显示, 不用于估 token**)。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    out.append(str(b.get("text") or ""))
                elif t == "image":
                    out.append("[图片]")
                elif t == "document":
                    out.append("[文档]")
                elif t == "tool_reference":
                    out.append(f"[工具定义: {b.get('tool_name') or '?'}]")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return str(content)


def result_size(content) -> tuple[int, int | None, list[str]]:
    """tool_result 的体积: (文字字符数, 估算 token 或 None, 媒体种类)。
    图片按尺寸估; 文档 / 工具定义块估不出 -> est=None (UI 显示 ≈?), 绝不把占位符的 3 个字当成 3 token。"""
    if isinstance(content, str):
        return len(content), est_tokens(content), []
    if not isinstance(content, list):
        t = text_of(content)
        return len(t), est_tokens(t), []
    chars, est, media, unknown = 0, 0, [], False
    for b in content:
        if isinstance(b, str):
            chars += len(b)
            est += est_tokens(b)
        elif isinstance(b, dict):
            t = b.get("type")
            if t == "text":
                s = str(b.get("text") or "")
                chars += len(s)
                est += est_tokens(s)
            elif t == "image":
                media.append("image")
                it = image_tokens((b.get("source") or {}).get("data"))
                if it is None:
                    unknown = True
                else:
                    est += it
            elif t in ("document", "tool_reference"):
                media.append(t)
                unknown = True
    return chars, (None if unknown else est), media


def prompt_text(content) -> tuple[str, int]:
    """真人提问 -> (给人读的文字, 附件数)。跳过 IDE 注入的 <ide_...> / <system-reminder> 块。"""
    if isinstance(content, str):
        return content.strip(), 0
    texts, att = [], 0
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            s = str(b.get("text") or "").strip()
            if s and not re.match(r"^<(ide_|system-reminder|command-|local-command)", s):
                texts.append(s)
        elif t in ("image", "document"):
            att += 1
    return "\n".join(texts).strip(), att


def notify_fields(txt: str) -> dict:
    m = _RE_SUMMARY.search(txt)
    s = _RE_STATUS.search(txt)
    return {"tids": _RE_TOOL_USE_ID.findall(txt), "task_ids": _RE_TASK_ID.findall(txt),
            "summary": short(m.group(1) if m else "", 200), "status": (s.group(1).strip().lower() if s else "")}


# ---- workflow 脚本 meta: 字面量提取 (字符串感知的括号匹配, **不执行任何 JS**) ----

def _js_skip(src: str, i: int) -> int:
    """i 指向引号 / 注释起点时, 返回跳过它之后的位置; 否则原样返回。"""
    c = src[i]
    if c in "'\"`":
        j = i + 1
        while j < len(src):
            if src[j] == "\\":
                j += 2
                continue
            if src[j] == c:
                return j + 1
            j += 1
        return len(src)
    if src.startswith("//", i):
        j = src.find("\n", i)
        return len(src) if j < 0 else j + 1
    if src.startswith("/*", i):
        j = src.find("*/", i + 2)
        return len(src) if j < 0 else j + 2
    return i


def _js_balanced(src: str, i: int) -> int | None:
    """src[i] 是 { 或 [ : 返回与之配对的右括号之后的位置 (跳过字符串与注释)。"""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack = []
    j = i
    while j < len(src):
        k = _js_skip(src, j)
        if k != j:
            j = k
            continue
        c = src[j]
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return None


def _js_top_items(body: str, opener: str) -> list[str]:
    """数组体里顶层的 {...} 项。"""
    out, j = [], 0
    while j < len(body):
        k = _js_skip(body, j)
        if k != j:
            j = k
            continue
        if body[j] == opener:
            end = _js_balanced(body, j)
            if end is None:
                break
            out.append(body[j:end])
            j = end
        else:
            j += 1
    return out


_RE_JS_STR = r"""(['"`])((?:\\.|(?!\1).)*)\1"""


def _js_field(obj: str, name: str) -> str | None:
    """在一个对象字面量的**顶层**找 name: '字符串'。"""
    depth, j = 0, 1
    while j < len(obj) - 1:
        k = _js_skip(obj, j)
        if k != j:
            j = k
            continue
        c = obj[j]
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif depth == 0 and obj.startswith(name, j) and not (obj[j - 1].isalnum() or obj[j - 1] == "_"):
            m = re.match(r"\s*:\s*" + _RE_JS_STR, obj[j + len(name):], re.S)
            if m:
                return m.group(2)
        j += 1
    return None


def parse_script_meta(src: str) -> dict:
    """workflow 脚本头部 `export const meta = {name, description, phases:[{title, detail}]}` -> dict。
    字符串里的 ] } 不会打断解析。解析不出就给空, 调用方回退到「按出现顺序」。"""
    out = {"name": None, "description": None, "phases": []}
    m = re.search(r"export\s+const\s+meta\s*=\s*\{", src or "")
    if not m:
        return out
    start = m.end() - 1
    end = _js_balanced(src, start)
    if end is None:
        return out
    obj = src[start:end]
    out["name"] = _js_field(obj, "name")
    out["description"] = _js_field(obj, "description")
    pm = re.search(r"\bphases\s*:\s*\[", obj)
    if pm:
        a = pm.end() - 1
        b = _js_balanced(obj, a)
        if b is not None:
            for item in _js_top_items(obj[a + 1:b - 1], "{"):
                t = _js_field(item, "title")
                if t is not None:
                    out["phases"].append({"title": t, "detail": _js_field(item, "detail") or ""})
    return out


# ================================================================ S2: 阶段标注 (推断)

STAGES = ("查看", "修改", "验证", "运行", "调研", "编排", "等你")
_VIEW_TOOLS = frozenset({"Read", "Grep", "Glob", "ToolSearch", "NotebookRead", "LS", "BashOutput", "TaskOutput"})
_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit", "Artifact", "ArtifactData"})
_RESEARCH_TOOLS = frozenset({"WebSearch", "WebFetch"})
_ORCH_TOOLS = frozenset({"Agent", "Task", "Workflow", "Skill", "SendMessage", "TaskStop", "Monitor", "StructuredOutput",
                         "SubagentHandback", "TodoWrite", "CronCreate", "CronDelete", "CronList", "RemoteTrigger",
                         "EnterWorktree", "ExitWorktree", "EnterPlanMode", "KillShell"})
_RE_VERIFY = re.compile(
    r"^(python3?\s+-m\s+(pytest|unittest|py_compile|mypy|ruff|flake8)|pytest|py\.test|uv\s+run\s+(pytest|python3?\s+-m\s+pytest|ruff|mypy)"
    r"|(npm|pnpm|yarn)\s+(run\s+)?(test|lint|typecheck|check)\b|npx\s+(jest|vitest|mocha|playwright\s+test|tsc|eslint)"
    r"|jest|vitest|mocha|go\s+(test|vet)|cargo\s+(test|check|clippy)|node\s+--(test|check)|tsc\b|eslint|ruff|flake8|mypy"
    r"|pyright|black\s+--check|dotnet\s+test|mvn\s+(test|verify)|gradle\s+test|make\s+(test|check|lint))", re.I)
_RE_VIEW = re.compile(
    r"^(cat|head|tail|less|more|sed\s+-n|grep|egrep|fgrep|rg|ag|ls|dir|find|tree|wc|stat|file|du|df|which|where|type|jq|awk"
    r"|diff|cmp|md5sum|sha\d*sum|basename|dirname|realpath|pwd|echo|printf"
    r"|git\s+(status|diff|log|show|blame|branch|remote|rev-parse|ls-files|describe|reflog|shortlog)"
    r"|get-content|get-childitem|select-string|get-item|test-path|gc|gci|sls|cat\b)", re.I)
_RE_EDIT = re.compile(
    r"^(sed\s+-i|perl\s+-pi|mv|cp|rm|rmdir|mkdir|touch|chmod|chown|ln|unzip|tar\s+-?x"
    r"|git\s+(add|commit|checkout|switch|merge|rebase|stash|reset|restore|push|pull|tag|cherry-pick|revert|mv|rm|apply|init|clone)"
    r"|new-item|remove-item|move-item|copy-item|set-content|out-file|add-content|rename-item)", re.I)
_RE_DESC_VERIFY = re.compile(r"\b(test|tests|testing|verify|verification|lint|smoke|typecheck)\b|测试|验证|冒烟|自检", re.I)
_RE_DESC_VIEW = re.compile(r"^\s*(read|list|show|inspect|view|print|display|find|search|look)\b|查看|读取|列出|显示|搜索", re.I)
_RE_MCP_RESEARCH = re.compile(r"(^|_)(docs?|documentation|search_docs|fetch_docs)(_|$)", re.I)
_RE_MCP_VIEW = re.compile(r"^(get|list|read|search|fetch|status|whoami|describe|show|query|find|check|inspect|view)"
                          r"|_(status|metrics|logs?|rate|time|requests|info)$", re.I)
_RE_MCP_EDIT = re.compile(r"^(create|set|update|delete|remove|deploy|connect|disconnect|generate|add|scale|link|retry"
                          r"|write|put|post|patch|upload|publish|rename|move|merge|close|open)", re.I)


def _bash_stage(cmd: str, desc: str = "", ps: bool = False) -> str:
    """一条命令 -> 阶段 (推断)。每个简单命令单独看 (管道两边、&& 两边都算), cd / export 之类不算数;
    heredoc 的正文、引号里的东西都不参与 (它们不是 shell 代码)。"""
    env: dict = {}
    parts = []
    for sc in shell_parse(cmd, ps)["cmds"]:
        head, args = _simple(sc["words"], env)
        writes = [w for op, w in sc["redirs"] if not _is_null(w, env)]
        if (not head or head in _NEUTRAL_HEADS) and not writes:
            continue
        parts.append((" ".join([head] + [w[0] for w in args]) if head else "", writes, head))
    if not parts:
        return "运行"
    if any(_RE_VERIFY.match(line) for line, _, _ in parts):
        return "验证"                                        # 链里有测试/检查 -> 这一步的目的是验证
    if any(_RE_EDIT.match(line) or writes or head == "tee" for line, writes, head in parts):
        return "修改"
    if all(line and _RE_VIEW.match(line) for line, _, _ in parts):
        return "查看"
    if desc and _RE_DESC_VERIFY.search(desc):
        return "验证"
    if desc and _RE_DESC_VIEW.search(desc):
        return "查看"
    return "运行"


def classify_call(name: str, inp: dict | None) -> str:
    """一次调用 -> 阶段 (**推断**, 规则见 STAGES)。判断不了的一律归「运行」, 不硬猜。"""
    inp = inp or {}
    if name in HUMAN_WAIT_TOOLS:
        return "等你"
    if name in _VIEW_TOOLS:
        return "查看"
    if name in _EDIT_TOOLS:
        return "修改"
    if name in _RESEARCH_TOOLS:
        return "调研"
    if name in _ORCH_TOOLS:
        return "编排"
    if name in ("Bash", "PowerShell"):
        return _bash_stage(str(inp.get("command") or ""), str(inp.get("description") or ""), ps=(name == "PowerShell"))
    if name.startswith("mcp__"):
        tool = name.split("__", 2)[-1]
        if _RE_MCP_RESEARCH.search(tool):
            return "调研"
        if _RE_MCP_EDIT.match(tool):
            return "修改"
        if _RE_MCP_VIEW.search(tool):
            return "查看"
        return "运行"
    return "运行"


def stage_runs(calls: list[dict], t_end: float | None = None) -> list[dict]:
    """同一条时间线上的调用 -> 阶段段落。连续同阶段合一段; 夹在两个同类阶段之间、只有 1 次调用的碎片并进去
    (比如改代码中途读了一眼文件), 但**失败、强异常、等你永远不并**。每段带时长 (到下一段开始, 空闲不算)。
    calls: 按时间排好的 [{"id","name","stage","t0","t1","fail","strong"}]。"""
    runs: list[dict] = []
    for c in calls:
        t0 = c.get("t0")
        if t0 is None:
            continue
        t1 = c.get("t1") if c.get("t1") is not None else t0
        if runs and runs[-1]["stage"] == c["stage"]:
            r = runs[-1]
            r["n"] += 1
            r["t1"] = max(r["t1"], t1)
            r["fail"] = r["fail"] or bool(c.get("fail"))
            r["strong"] = r["strong"] or bool(c.get("strong"))
            r["tools"][c["name"]] = r["tools"].get(c["name"], 0) + 1
        else:
            runs.append({"stage": c["stage"], "t0": t0, "t1": t1, "n": 1, "fail": bool(c.get("fail")),
                         "strong": bool(c.get("strong")), "tools": {c["name"]: 1}, "first": c["id"], "absorbed": 0})
    changed = True
    while changed:
        changed = False
        for i in range(1, len(runs) - 1):
            r, a, b = runs[i], runs[i - 1], runs[i + 1]
            if (r["n"] == 1 and a["stage"] == b["stage"] and r["stage"] != "等你"
                    and not r["fail"] and not r["strong"]):
                a["n"] += r["n"] + b["n"]
                a["t1"] = max(a["t1"], r["t1"], b["t1"])
                a["fail"] = a["fail"] or b["fail"]
                a["strong"] = a["strong"] or b["strong"]
                for src in (r["tools"], b["tools"]):
                    for k2, v in src.items():
                        a["tools"][k2] = a["tools"].get(k2, 0) + v
                a["absorbed"] += 1 + b["absorbed"]
                a.setdefault("absorbed_stages", []).append(r["stage"])
                del runs[i:i + 2]
                changed = True
                break
    for i, r in enumerate(runs):                            # 时长: 到下一段开始 (中间是模型在想), 空闲不算
        nxt = runs[i + 1]["t0"] if i + 1 < len(runs) else t_end
        end = r["t1"]
        if nxt is not None and 0 <= nxt - r["t1"] <= IDLE_GAP:
            end = max(end, nxt)
        r["dur"] = max(0.0, end - r["t0"])
        r.pop("strong", None)
    return runs


# ================================================================ S2: 数据依赖 (推断)

_RE_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_RE_IDTOK = re.compile(r"(?<![A-Za-z0-9_-])(" + _RE_UUID + r"|[A-Za-z0-9_-]{16,64})(?![A-Za-z0-9_-])")


def _is_id(t: str) -> bool:
    """像 ID 的串: UUID / ≥16 位十六进制 / ≥20 位且至少 4 个数字的混合串。
    (排除 claude-fable-5-1 这类名字、纯单词路径段 —— 否则到处都是假依赖)"""
    if re.fullmatch(_RE_UUID, t):
        return True
    if len(t) >= 16 and re.fullmatch(r"[0-9a-fA-F]+", t) and re.search(r"[a-fA-F]", t) and re.search(r"\d", t):
        return True
    return len(t) >= 20 and sum(ch.isdigit() for ch in t) >= 4 and sum(ch.isalpha() for ch in t) >= 4


def id_tokens(text: str | None, limit: int = 120) -> list[str]:
    out, seen = [], set()
    if not text or not any(ch.isdigit() for ch in text[:200000]):
        return out
    for m in _RE_IDTOK.finditer(text[:200000]):
        t = m.group(1)
        if t in seen or not _is_id(t):
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def input_ids(inp, path: str = "", out: dict | None = None) -> dict:
    """调用输入里的 ID 串 -> 所在字段路径 (如 project_id / variables.SERVICE_ID)。"""
    out = {} if out is None else out
    if isinstance(inp, dict):
        for k, v in inp.items():
            input_ids(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(inp, list):
        for i, v in enumerate(inp[:50]):
            input_ids(v, f"{path}[{i}]", out)
    elif isinstance(inp, str):
        for t in id_tokens(inp, limit=20):
            out.setdefault(t, path or "input")
    return out


# ================================================================ S2: 名词说明 (官方原文)

def parse_listing(names: list | None, lines) -> dict:
    """skill / agent 清单: 按「- 名字: 说明」逐行对上名字 (名字本身可能带冒号, 如 plugin:skill)。"""
    out = {}
    text_lines = lines.splitlines() if isinstance(lines, str) else [str(x) for x in (lines or [])]
    for n in names or []:
        n = str(n)
        pre = f"- {n}:"
        for ln in text_lines:
            if ln.strip().startswith(pre):
                out[n] = ln.strip()[len(pre):].strip()[:600]
                break
    return out


# ================================================================ S2: 脚本对照

def script_map(src: str) -> dict:
    """workflow 脚本 -> {phases: {标题: 行号}, phase_how: {标题: 来源}, agents: [{line, label_line, label, rx}]}。
    只做字面量扫描, **不执行 JS**。阶段有三种写法, 按优先级认: phase('X') 调用 > agent() 参数里的 phase: 'X'
    > 头部 meta.phases 的声明。agent 标签三种写法: 字面量 / 模板 `investigate:${lens.key}` / 拼接 'read:' + x
    —— 后两种转成正则, 用来对上回放里 agent 的 description。"""
    src = src or ""
    starts = [0] + [i + 1 for i, ch in enumerate(src) if ch == "\n"]

    def lineno(pos):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    phases: dict = {}
    how: dict = {}
    for m in re.finditer(r"\bphase\(\s*" + _RE_JS_STR, src):
        if m.group(2) not in phases:
            phases[m.group(2)], how[m.group(2)] = lineno(m.start()), "phase()"
    for m in re.finditer(r"\bphase\s*:\s*" + _RE_JS_STR, src):
        if m.group(2) not in phases:
            phases[m.group(2)], how[m.group(2)] = lineno(m.start()), "opts"
    mm = re.search(r"export\s+const\s+meta\s*=\s*\{", src)
    if mm:
        end = _js_balanced(src, mm.end() - 1) or mm.end()
        for m in re.finditer(r"\btitle\s*:\s*" + _RE_JS_STR, src[mm.end():end]):
            if m.group(2) not in phases:
                phases[m.group(2)], how[m.group(2)] = lineno(mm.end() + m.start()), "meta"
    agents = []
    for m in re.finditer(r"\bagent\(", src):
        a = m.end() - 1
        b = _js_balanced(src, a) or min(len(src), a + 3000)
        body = src[a:b]
        lm = re.search(r"\blabel\s*:\s*" + _RE_JS_STR + r"(\s*\+)?", body, re.S)
        tpl, rx = None, None
        if lm:
            tpl = lm.group(2)
            if lm.group(3):                                   # 'read:' + x  -> 前缀
                rx = "^" + re.escape(tpl) + ".+$"
                tpl = tpl + "…"
            elif "${" in tpl:
                parts = re.split(r"\$\{[^}]*\}", tpl)
                rx = "^" + ".+?".join(re.escape(x) for x in parts) + "$"
        agents.append({"line": lineno(m.start()), "label_line": lineno(a + lm.start()) if lm else lineno(m.start()),
                       "label": tpl, "rx": rx})
    return {"phases": phases, "phase_how": how, "agents": agents, "lines": len(starts)}


def match_agent_line(desc: str | None, agents: list[dict]) -> int | None:
    """回放里 agent 的 description -> 启动它的 agent() 调用所在行。静态标签优先于模板; 对不上 -> None (不猜)。"""
    if not desc:
        return None
    for a in agents:
        if a["label"] is not None and not a["rx"] and a["label"] == desc:
            return a["label_line"]
    for a in agents:
        if a["rx"] and re.match(a["rx"], desc, re.S):
            return a["label_line"]
    return None


# ================================================================ S4: 改动清单 (这次任务到底改了什么)
#
# 两级来源, 界面上分开标:
#   真值 —— Edit / Write / NotebookEdit / MultiEdit: 工具参数本身就是改动, 文件与行数都算得准;
#   推断 —— Bash / PowerShell: 从命令里认 (sed -i / 重定向 / mv / cp / rm / touch / tee / git checkout ...);
#           认得出这一步是「修改」却认不出改的是哪个文件 (内联脚本、git reset --hard、解压...) -> 如实记成
#           「改了，但不知道改了哪个」, 绝不瞎猜文件名。

_RE_SED_EXPR = re.compile(r"^[0-9,$~+\s]*[a-z]?[/#|,@!]")           # s/a/b/ · 3,5d · y#a#b#
_SCRIPT_HEADS = frozenset({"python", "python3", "py", "node", "ruby", "perl", "sh", "bash", "zsh", "pwsh",
                           "powershell", "deno", "bun", "irb", "osascript"})
_DELETE_HEADS = frozenset({"rm", "rmdir", "del", "erase", "unlink", "remove-item", "ri", "rd"})
_MOVE_HEADS = frozenset({"mv", "move-item", "rename-item", "mi", "rni", "ren"})
_COPY_HEADS = frozenset({"cp", "copy-item", "copy", "cpi"})
_TOUCH_HEADS = frozenset({"touch", "new-item", "ni"})
_PS_WRITE_HEADS = frozenset({"set-content", "out-file", "add-content", "sc", "ac"})
_WIPE_HEADS = frozenset({"unzip", "tar", "7z", "patch", "rsync", "robocopy", "xcopy"})
_GIT_WIPES = frozenset({"clean", "merge", "rebase", "cherry-pick", "revert", "pull", "am"})
_GIT_BENIGN = frozenset({"add", "commit", "push", "tag", "fetch", "init", "config", "remote", "branch", "worktree",
                         "status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "describe", "reflog"})
_GIT_GLOBAL_VALUE = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_BENIGN_HEADS = frozenset({"chmod", "chown", "mkdir", "md", "icacls", "attrib"})
_NEUTRAL_HEADS = frozenset({"cd", "pushd", "popd", "export", "set", "unset", "true", ":", "source", "."})
_WRAPPERS = frozenset({"sudo", "nohup", "time", "exec", "env", "command", "builtin", "nice"})
_NULL_SINKS = frozenset({"/dev/null", "nul", "$null", "nul:"})
_WHOLE_TREE = frozenset({".", "..", "./", "../", "~", "*", ":/"})
_RE_PATHY = re.compile(r"^[\w.~:/\\@+\- ]+$")              # 路径长这样; 带引号 / 括号 / 通配符的一律不认
_RE_ALNUM = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
_RE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_RE_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
# 带值的 flag: 下一个词是这个 flag 的值, 不是路径 (`New-Item -ItemType Directory` 的 Directory、`touch -d 2020-01-01`)
_VALUE_FLAGS = {
    "touch": {"-d", "--date", "-r", "--reference", "-t"},
    "cp": {"-t", "--target-directory", "-s", "--suffix"}, "mv": {"-t", "--target-directory", "-s", "--suffix"},
}
_PS_VALUE_FLAGS = frozenset({"-itemtype", "-type", "-name", "-value", "-erroraction", "-ea", "-filter", "-include",
                             "-exclude", "-encoding", "-warningaction", "-wa", "-credential", "-stream"})
_PS_TARGET_FLAGS = ("-path", "-filepath", "-literalpath", "-lp", "-destination")


def _lines(s) -> int:
    s = str(s or "")
    return 0 if not s else s.count("\n") + 1


def shell_parse(cmd: str, ps: bool = False) -> dict:
    """一条命令行 -> 一串「简单命令」。只做我们需要的那一小块词法, 但要做**对**:

    - 引号: 单引号原样; 双引号里只有 `\" \\ \$ \`` 是转义 (PowerShell 里转义符是反引号, 反斜杠是普通字符);
      引号段和紧挨着的裸字符拼成**同一个词** (`"$SP"/a.py` 是一个词, 不是 `$SP` 和 `/a.py` 两个);
    - `#` 只在词首、引号外才是注释, 注释到行尾 (`rm -rf dist  # remove stale build` 不会多出三个「文件」);
    - 控制符 `&& || ; | &` 与换行切命令; 引号里的不算;
    - 重定向 `> >> 2> &>` 记成 (op, 目标词); `>&2` / `2>&1` 是复制文件描述符, 不是文件;
    - heredoc `<<WORD` / `<<'WORD'` / `<<-WORD`: 正文跳过 (它不是 shell 代码), **终止符之后接着解析**;
      找不到终止符 -> unterminated (调用方记成「说不清」);
    - 变量与命令替换 (`$x` `${x}` `$(..)` 反引号) 让这个词成为 dynamic: 值在跑之前不知道, 绝不当成字面路径。

    返回 {"cmds": [{"words": [(文字, dynamic)], "redirs": [(op, 词)], "heredoc": bool}], "unterminated": bool}。"""
    s = str(cmd or "")
    n = len(s)
    cmds: list = []
    st = {"words": [], "redirs": [], "heredoc": False}
    buf: list = []
    flags = {"dyn": False, "in": False, "redir": None, "unterminated": False}
    pending_docs: list = []                                  # 这一行上等着跳正文的 heredoc: (终止符, 允许前导 tab)

    def end_word():
        if not flags["in"]:
            return
        w = ("".join(buf), flags["dyn"])
        op = flags["redir"]
        if op is None:
            st["words"].append(w)
        elif op in ("<<", "<<-"):
            pending_docs.append((w[0], op == "<<-"))
            st["heredoc"] = True
        elif op in (">", ">>"):
            st["redirs"].append((op, w))
        flags["redir"] = None                                # `<` 输入与 `<<<` here-string 的目标不是写入, 丢掉
        buf.clear()
        flags["dyn"] = False
        flags["in"] = False

    def end_cmd():
        end_word()
        if st["words"] or st["redirs"]:
            cmds.append({"words": list(st["words"]), "redirs": list(st["redirs"]), "heredoc": st["heredoc"]})
        st["words"], st["redirs"], st["heredoc"] = [], [], False

    def skip_docs(pos: int) -> int:
        for word, tabs in pending_docs:
            while True:
                if pos >= n:
                    flags["unterminated"] = True
                    return n
                j = s.find("\n", pos)
                end = n if j < 0 else j
                line = s[pos:end].rstrip("\r")
                pos = end + 1
                if (line.lstrip("\t") if tabs else line).strip() == word:
                    break
        pending_docs.clear()
        return pos

    esc = "`" if ps else "\\"
    i = 0
    while i < n:
        c = s[i]
        if c == esc and i + 1 < n:                           # 裸转义 (行尾 = 续行)
            if s[i + 1] == "\n":
                i += 2
                continue
            buf.append(s[i + 1])
            flags["in"] = True
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            buf.append(s[i + 1:j])
            flags["in"] = True
            i = j + 1
            continue
        if c == '"':
            i += 1
            flags["in"] = True
            while i < n and s[i] != '"':
                ch = s[i]
                if ch == esc and i + 1 < n and (ps or s[i + 1] in '"\\$`'):
                    buf.append(s[i + 1])
                    i += 2
                    continue
                if ch == "$" or (ch == "`" and not ps):
                    flags["dyn"] = True
                buf.append(ch)
                i += 1
            i += 1
            continue
        if c == "$" or (c == "`" and not ps):                # 变量 / 命令替换
            flags["dyn"] = True
            flags["in"] = True
            if s.startswith("$(", i):                        # $( ... ): 整段吞进这个词, 不在里面切命令
                depth, j = 0, i + 1
                while j < n:
                    if s[j] == "(":
                        depth += 1
                    elif s[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                buf.append(s[i:j + 1])
                i = j + 1
                continue
            if c == "`":
                j = s.find("`", i + 1)
                j = n - 1 if j < 0 else j
                buf.append(s[i:j + 1])
                i = j + 1
                continue
            buf.append(c)
            i += 1
            continue
        if c == "#" and not flags["in"]:                     # 注释: 词首、引号外 -> 到行尾
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in " \t\r":
            end_word()
            i += 1
            continue
        if c == "\n":
            end_cmd()
            i += 1
            if pending_docs:
                i = skip_docs(i)
            continue
        if s.startswith("&&", i) or s.startswith("||", i):
            end_cmd()
            i += 2
            continue
        if c in ";|":
            end_cmd()
            i += 1
            continue
        if c == "&":
            if s.startswith("&>", i):                        # &> file / &>> file
                end_word()
                flags["redir"] = ">>" if s.startswith("&>>", i) else ">"
                i += 3 if s.startswith("&>>", i) else 2
                continue
            end_cmd()
            i += 1
            continue
        if c in "<>":
            if flags["in"] and "".join(buf).isdigit() and not flags["dyn"]:
                buf.clear()                                  # 2> / 1>>: 前面那个数字是文件描述符, 不是词
                flags["in"] = False
            else:
                end_word()
            if c == "<":
                for op in ("<<<", "<<-", "<<", "<"):
                    if s.startswith(op, i):
                        flags["redir"] = op
                        i += len(op)
                        break
                continue
            op = ">>" if s.startswith(">>", i) else ">"
            i += len(op)
            if i < n and s[i] == "&":                        # >&2 / 2>&1: 复制文件描述符
                j = i + 1
                while j < n and (s[j].isdigit() or s[j] == "-"):
                    j += 1
                i = j
                continue
            if i < n and s[i] == "|":                        # >| 强制覆盖
                i += 1
            flags["redir"] = op
            continue
        buf.append(c)
        flags["in"] = True
        i += 1
    end_cmd()
    if pending_docs:
        flags["unterminated"] = True
    return {"cmds": cmds, "unterminated": flags["unterminated"]}


def _simple(words: list, env: dict) -> tuple[str, list]:
    """一个简单命令 -> (命令名, 参数词)。去掉前缀的 `VAR=值` (字面量的记进 env, 供后面展开)
    与 sudo / nohup / `timeout N` 之类的包装。"""
    i = 0
    while i < len(words):
        t, dyn = words[i]
        m = _RE_ASSIGN.match(t)
        if m:
            if dyn:
                env.pop(m.group(1), None)
            else:
                env[m.group(1)] = m.group(2)
            i += 1
            continue
        low = t.lower()
        if low in _WRAPPERS:
            i += 1
            continue
        if low == "timeout":
            i += 1
            while i < len(words) and words[i][0].startswith("-"):
                i += 1
            if i < len(words) and re.match(r"^\d+(\.\d+)?[smhd]?$", words[i][0]):
                i += 1
            continue
        break
    if i >= len(words):
        return "", []
    head = words[i][0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (head[:-4] if head.endswith(".exe") else head), words[i + 1:]


def _expand(word: tuple, env: dict) -> tuple[str, bool]:
    """同一条命令里 `SP="C:/x"` 赋过字面量的变量, 后面 `"$SP"/a.py` 就能展开成真路径; 展开不了的仍是 dynamic。"""
    t, dyn = word
    if not dyn:
        return t, False
    if "`" in t or "$(" in t:
        return t, True
    s2 = _RE_VAR.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), t)
    return s2, ("$" in s2 or "`" in s2)


def _is_null(word: tuple, env: dict) -> bool:
    t, _ = _expand(word, env)
    return t.lower() in _NULL_SINKS or t.lower().startswith("/dev/")


def _as_path(word: tuple, env: dict) -> str | None:
    """一个词能不能当成**字面路径**: 变量展开不出来的、通配符、整棵树 (. / .. / ~)、长得不像路径的 -> None。
    None 的意思是「认不准」—— 调用方记成「说不清」, 绝不把它当文件名。"""
    t, dyn = _expand(word, env)
    t = t.strip()
    if dyn or not t:
        return None
    if any(c in t for c in "*?[") or t.rstrip("/") in _WHOLE_TREE or t in _WHOLE_TREE:
        return None
    if not _RE_PATHY.match(t) or not _RE_ALNUM.search(t):
        return None
    if t.lower() in _NULL_SINKS or t.lower().startswith("/dev/"):
        return None
    return t


def _arg_paths(args: list, env: dict, value_flags=frozenset()) -> tuple[list[str], bool]:
    """参数词 -> (认得出的字面路径, 有没有认不准的)。flag 跳过; 带值的 flag 连它的值一起跳过。"""
    out, vague, skip = [], False, False
    for w in args:
        t = w[0]
        if skip:
            skip = False
            continue
        if t.startswith("-") and len(t) > 1:
            skip = t.lower() in value_flags
            continue
        pth = _as_path(w, env)
        if pth is None:
            vague = True
        else:
            out.append(pth)
    return out, vague


def _sed_paths(head: str, args: list, env: dict) -> list[str]:
    """`sed -i` / `perl -pi` 的文件参数。按 CLI 本身的语法认: 有 `-e` / `-f` 时表达式跟在它们后面;
    没有时第一个非 flag 参数就是表达式; 其余才是文件。perl 的 `-pe` / `-pie` 这种 flag 串以 e 结尾, 下一个词是脚本。"""
    def takes_script(t: str) -> bool:
        if t in ("-e", "--expression", "-f", "--file"):
            return True
        return head == "perl" and t.startswith("-") and not t.startswith("--") and t.endswith("e")
    has_script_flag = any(takes_script(w[0]) for w in args)
    out, skip, script_seen = [], False, False
    for w in args:
        t = w[0]
        if skip:
            skip = False
            continue
        if t.startswith("-") and len(t) > 1:
            skip = takes_script(t)
            continue
        if not has_script_flag and not script_seen:
            script_seen = True                               # 第一个非 flag 参数是表达式
            continue
        pth = _as_path(w, env)
        if pth is not None:
            out.append(pth)
    return out


def _ps_target(args: list, env: dict) -> list[str]:
    """PowerShell 写文件类: -Path / -LiteralPath / -FilePath 的值, 没有就取第一个位置参数 (-Value 后面是内容)。"""
    low = [w[0].lower() for w in args]
    for flag in _PS_TARGET_FLAGS:
        if flag in low:
            i = low.index(flag)
            pth = _as_path(args[i + 1], env) if i + 1 < len(args) else None
            return [pth] if pth else []
    skip = False
    for w in args:
        t = w[0]
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            skip = t.lower() in _PS_VALUE_FLAGS
            continue
        pth = _as_path(w, env)
        return [pth] if pth else []
    return []


def _git_sub(args: list) -> tuple[str, list]:
    """git 的子命令与它的参数 (跳过 -C 路径 / -c k=v 这类全局参数)。"""
    i = 0
    while i < len(args):
        t = args[i][0]
        if t in _GIT_GLOBAL_VALUE:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t.lower(), args[i + 1:]
    return "", []


def redirect_targets(cmd: str, ps: bool = False) -> list[str]:
    """命令里写文件的重定向目标 (字面量的)。给测试与排查用; 真正的判断在 _bash_changes 里。"""
    return [w[0] for sc in shell_parse(cmd, ps)["cmds"] for op, w in sc["redirs"] if op in (">", ">>") and not w[1]]


def _bash_changes(cmd: str, ps: bool = False) -> tuple[list, bool, bool]:
    """命令 -> (改动条目, 有没有认不出目标的改动, 是不是每一段都看懂了)。全部是推断。"""
    P = shell_parse(str(cmd or ""), ps)
    items: list = []
    unknown, understood = P["unterminated"], True
    env: dict = {}
    for sc in P["cmds"]:
        head, args = _simple(sc["words"], env)
        wrote = False
        for op, w in sc["redirs"]:                          # cmd > file / >> file
            if _is_null(w, env):
                continue
            wrote = True
            pth = _as_path(w, env)
            if pth is None:
                unknown = True                               # 写到了一个认不出的地方 (变量 / 怪字符) -> 说不清
            else:
                items.append({"path": pth, "op": "write"})
        if not head or head in _NEUTRAL_HEADS:
            continue
        low = [w[0].lower() for w in args]
        if head in ("sed", "perl") and any(re.match(r"^-[A-Za-z]*i", t) or t == "--in-place" for t in low):
            paths = _sed_paths(head, args, env)
            items += [{"path": x, "op": "edit"} for x in paths]
            unknown = unknown or not paths
        elif head in _DELETE_HEADS:
            paths, vague = _arg_paths(args, env, _PS_VALUE_FLAGS if head in ("remove-item", "ri") else frozenset())
            items += [{"path": x, "op": "delete"} for x in paths]
            unknown = unknown or vague or not paths
        elif head in _MOVE_HEADS:
            paths, vague = _arg_paths(args, env, _VALUE_FLAGS.get(head, _PS_VALUE_FLAGS))
            items += [{"path": x, "op": "move"} for x in paths]
            unknown = unknown or vague or len(paths) < 2
        elif head in _COPY_HEADS:
            paths, vague = _arg_paths(args, env, _VALUE_FLAGS.get(head, _PS_VALUE_FLAGS))
            items += [{"path": paths[-1], "op": "write"}] if paths else []
            unknown = unknown or not paths or (vague and len(paths) < 2)
        elif head in _TOUCH_HEADS:
            if head in ("new-item", "ni") and any(t in ("-itemtype", "-type") for t in low) and "directory" in low:
                continue                                     # New-Item -ItemType Directory = mkdir, 不动文件内容
            paths, vague = _arg_paths(args, env, _VALUE_FLAGS.get(head, _PS_VALUE_FLAGS))
            items += [{"path": x, "op": "write"} for x in paths]
            unknown = unknown or vague or not paths
        elif head == "tee":
            paths, vague = _arg_paths(args, env)
            items += [{"path": x, "op": "write"} for x in paths]
            unknown = unknown or vague or not paths
        elif head in _PS_WRITE_HEADS:
            tgt = _ps_target(args, env)
            items += [{"path": x, "op": "write"} for x in tgt]
            unknown = unknown or not tgt
        elif head == "git":
            gsub, gargs = _git_sub(args)
            gl = [w[0] for w in gargs]
            gflags = {t for t in gl if t.startswith("-")}
            pos = [w for w in gargs if not w[0].startswith("-")]
            if gsub in ("checkout", "restore", "switch"):
                if "--" in gl:
                    paths, vague = _arg_paths(gargs[gl.index("--") + 1:], env)
                    items += [{"path": x, "op": "revert"} for x in paths]
                    unknown = unknown or vague or not paths
                elif gsub != "restore" and gflags & {"-b", "-B", "-c", "-C", "--create", "--orphan"} and len(pos) <= 1:
                    pass                                     # 从当前 HEAD 建新分支: 不动任何文件
                elif gsub == "restore" and pos:
                    paths, vague = _arg_paths(gargs, env, frozenset({"-s", "--source"}))
                    items += [{"path": x, "op": "revert"} for x in paths]
                    unknown = unknown or vague or not paths
                else:
                    unknown = True                           # 切分支 / 整体还原: 工作区变了, 但不知道哪些文件
            elif gsub == "reset":
                unknown = unknown or "--hard" in gflags
            elif gsub == "stash":
                if not pos or pos[0][0].lower() not in ("list", "show"):
                    unknown = True                           # stash / pop / apply 会动工作区; list / show 只读
            elif gsub == "apply":
                if not gflags & {"--check", "--stat", "--summary", "--numstat"}:
                    unknown = True
            elif gsub in _GIT_WIPES:
                unknown = True
            elif gsub == "rm":
                paths, vague = _arg_paths(gargs, env)
                items += [{"path": x, "op": "delete"} for x in paths]
                unknown = unknown or vague or not paths
            elif gsub == "mv":
                paths, vague = _arg_paths(gargs, env)
                items += [{"path": x, "op": "move"} for x in paths]
                unknown = unknown or vague or len(paths) < 2
            elif gsub not in _GIT_BENIGN:
                understood = False
        elif head in _WIPE_HEADS:
            unknown = True                                   # 解压 / 打补丁 / 同步: 动了一批文件, 清单给不出
        elif head in _SCRIPT_HEADS:
            if sc["heredoc"]:
                unknown = True                               # 内联脚本: 它自己可能写文件
            else:
                understood = False
        elif head in _BENIGN_HEADS:
            pass                                             # 建目录 / 改权限: 不动文件内容
        elif not sc["redirs"] and not _RE_VIEW.match(" ".join([head] + [w[0] for w in args])):
            understood = False                               # 只读命令 / 只往 /dev/null 写的: 看懂了, 没改文件
    return items, unknown, understood


def patch_stats(tur: dict) -> dict | None:
    """Edit / Write 的返回里带着**实际打上去的补丁** (structuredPatch): 数它的 +/- 行 —— 比按请求算更准,
    Write 覆盖掉多少行也只有这里知道。userModified 说明这个文件在 Claude 读过之后被人改过。"""
    sp = tur.get("structuredPatch")
    if not isinstance(sp, list) or not sp:
        return None
    add = dele = 0
    for h in sp:
        for ln in (h.get("lines") or []) if isinstance(h, dict) else []:
            t = str(ln)[:1]
            if t == "+":
                add += 1
            elif t == "-":
                dele += 1
    return {"path": tur.get("filePath") or tur.get("file_path"), "add": add, "del": dele,
            "user_modified": bool(tur.get("userModified"))}


def file_changes(name: str, inp: dict | None, stage: str | None = None) -> dict | None:
    """一次调用改了哪些文件 -> {"src": "true"|"inferred", "items": [...], "unknown": bool}; 没改 -> None。

    items 里每条: {"path", "op", "add", "del"}; add / del 是**这次请求的**行数变化:
    Edit 用新旧文本的行数 (真值), Write 只知道写了多少行 (覆盖前多大**不知道**), 其余为 None。"""
    inp = inp or {}
    if name in ("Edit", "MultiEdit"):
        path = inp.get("file_path")
        edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        add = dele = 0
        approx = False
        for e in edits:
            if not isinstance(e, dict):
                continue
            add += _lines(e.get("new_string"))
            dele += _lines(e.get("old_string"))
            approx = approx or bool(e.get("replace_all"))
        if not path:
            return None
        return {"src": "true", "unknown": False,
                "items": [{"path": str(path), "op": "edit", "add": add, "del": dele, "approx": approx}]}
    if name == "Write":
        path = inp.get("file_path")
        if not path:
            return None
        return {"src": "true", "unknown": False,
                "items": [{"path": str(path), "op": "write", "add": _lines(inp.get("content")), "del": None}]}
    if name == "NotebookEdit":
        path = inp.get("notebook_path") or inp.get("file_path")
        if not path:
            return None
        op = {"insert": "write", "delete": "delete"}.get(str(inp.get("edit_mode") or ""), "edit")
        return {"src": "true", "unknown": False, "items": [{"path": str(path), "op": op, "add": None, "del": None}]}
    if name in ("Bash", "PowerShell"):
        items, unknown, understood = _bash_changes(inp.get("command"), ps=(name == "PowerShell"))
        if stage == "修改" and not items and not understood:
            unknown = True                                   # 看得出在改, 但命令没看懂 -> 如实记一笔
        if not items and not unknown:
            return None
        seen, uniq = set(), []
        for it in items:                                     # 同一条命令里同一个文件只记一次
            pth = it["path"]
            if (pth.lower() in _NULL_SINKS or pth.lower().startswith("/dev/")
                    or not _RE_PATHY.match(pth) or not re.search(r"[A-Za-z0-9一-鿿]", pth)):
                continue                                     # /dev/*、只有斜杠的、带引号括号的: 都不是文件
            k = (it["path"], it["op"])
            if k not in seen:
                seen.add(k)
                uniq.append({"path": it["path"], "op": it["op"], "add": None, "del": None})
        return {"src": "inferred", "unknown": unknown, "items": uniq}
    return None


_RE_SCRATCH = re.compile(r"(^|/)(temp|tmp)/claude/[^/]+/[^/]+/scratchpad/", re.I)   # 本次会话的一次性脚本目录


_RE_TEMPDIR = re.compile(r"^(/tmp/|/var/tmp/|[a-z]:/users/[^/]+/appdata/local/temp/)", re.I)
_RE_AUTOMEM = re.compile(r"/\.claude/projects/[^/]+/memory/", re.I)


def _norm_path(p: str, roots: list[str]) -> tuple[str, bool]:
    """显示用的路径 + 算不算界外。绝对路径能落进某个 root 就转成相对的; 落不进 -> 界外 (真值比较, 不猜)。
    不算界外、但**路径照写**的两类: 系统临时目录 (mktemp 的一次性文件)、Claude 自己的自动记忆 —— 它们不是谁的项目,
    实测若算进去会让近 4 成任务都挂「越界」。`~/.claude` 下别的 (设置 / agent / skill) 照算, 改它们会改变 Claude 的行为。"""
    s = str(p or "").replace("\\", "/").strip().strip("\"'")
    while s.startswith("./"):
        s = s[2:]
    if re.match(r"^/[A-Za-z]/", s) and any(":/" in r for r in roots):
        s = s[1] + ":" + s[2:]                               # Git Bash 写法 /c/Users/... 就是 C:/Users/...
    low = s.lower().rstrip("/")
    for r in roots:
        if low == r:
            return ".", False
        if low.startswith(r + "/"):
            return s[len(r) + 1:], False
    absolute = bool(re.match(r"^([A-Za-z]:/|/|\\\\)", s))
    if _RE_SCRATCH.search(s):                                # Claude 自己的临时脚本: 路径又长又没意义, 只留尾巴
        tail = s.split("/scratchpad/", 1)[-1]
        return "临时脚本/" + tail, False
    if absolute and (_RE_TEMPDIR.search(s) or _RE_AUTOMEM.search(s)):
        return s, False                                      # 系统临时目录 / Claude 自动记忆: 路径照写, 不算界外
    return s, absolute                                       # 相对路径在当时的工作目录下, 不算界外


def build_ledger(changes: list[dict], calls: list[dict], cwds) -> dict | None:
    """任务里的所有改动 -> 改动清单。changes 由 _call_node 按时间顺序登记。

    每个文件: 改了几次 / 增删多少行 / 来源(真值·推断) / 第一次与最后一次 / 是哪几次调用 (逐条可跳回放)。
    「改完验证了没」= 最后一次改动之后有没有「验证」阶段的调用 (阶段是推断; 你在页面外手动跑的测试看不见)。"""
    if not changes:
        return None
    roots = sorted({str(c).replace("\\", "/").rstrip("/").lower() for c in (cwds or set()) if c}, key=len, reverse=True)
    files: dict = {}
    unknown_calls: list = []
    for ch in changes:
        if ch.get("unknown"):
            unknown_calls.append({"node": ch["id"], "t": ch.get("t")})
        for it in ch.get("items") or []:
            path, outside = _norm_path(it["path"], roots)
            if not path:
                continue
            f = files.get(path)
            if f is None:
                f = files[path] = {"path": path, "n": 0, "add": 0, "del": 0, "add_known": False, "del_known": False,
                                   "add_partial": False, "del_partial": False,
                                   "approx": False, "outside": outside, "src": set(), "ops": {}, "t0": ch.get("t"),
                                   "t1": ch.get("t"), "calls": []}
            f["src"].add(ch.get("src") or "inferred")
            f["ops"][it["op"]] = f["ops"].get(it["op"], 0) + 1
            if it.get("approx"):                             # replace_all: 替换了几处不知道 -> 行数只是下限
                f["approx"] = True
                f["add_partial"] = f["del_partial"] = True
            if it.get("add") is None:                        # 这次改动算不出行数 (命令 / notebook): 合计只是下限
                f["add_partial"] = True
            else:
                f["add"] += it["add"]
                f["add_known"] = True
            if it.get("del") is None:
                f["del_partial"] = True
            else:
                f["del"] += it["del"]
                f["del_known"] = True
            if ch.get("t") is not None:
                f["t0"] = min(f["t0"] or ch["t"], ch["t"])
                f["t1"] = max(f["t1"] or ch["t"], ch["t"])
            if ch["id"] not in f["calls"]:
                f["calls"].append(ch["id"])
    if not files and not unknown_calls:
        return None
    rows = []
    for f in files.values():
        src = f.pop("src")
        f["src"] = "both" if len(src) > 1 else (list(src)[0] if src else "inferred")
        f["add"] = f["add"] if f.pop("add_known") else None
        f["del"] = f["del"] if f.pop("del_known") else None
        f["n"] = len(f["calls"])                             # 「N 次」= 改到它的调用数 (展开后列出的正好 N 条)
        rows.append(f)
    rows.sort(key=lambda r: (-r["n"], -(r["t1"] or 0), r["path"]))
    ts = [ch.get("t") for ch in changes if ch.get("t") is not None]
    last_t = max(ts) if ts else None
    # 「验证」= 阶段标注为验证的调用 (推断)。给两个数: 最后一次验证是什么、它之后又改了多少 ——
    # 比一句「没验证」准确: 跑完测试又补了个文档, 和改完根本没跑过测试, 不是一回事。
    vers = [c for c in calls if (c.get("meta") or {}).get("stage") == "验证" and c.get("t0") is not None]
    verify = None
    after_files: set = set()
    n_after = 0
    if vers:
        c = max(vers, key=lambda c: c["t0"])
        verify = {"node": c["id"], "t": c["t0"], "ok": not ({"fail", "bg-fail"} & set(c.get("flags") or [])),
                  "label": short(c.get("label"), 80)}
        for ch in changes:
            if ch.get("t") is not None and ch["t"] > c["t0"]:
                n_after += 1
                after_files |= {_norm_path(it["path"], roots)[0] for it in ch.get("items") or []}
    else:
        n_after, after_files = len(changes), set(files)
    return {"files": rows, "unknown_calls": unknown_calls, "last_change_t": last_t, "verify": verify,
            "after_verify": {"changes": n_after, "files": len(after_files)},
            "totals": {"files": len(rows), "unknown": len(unknown_calls),
                       "changes": len({ch["id"] for ch in changes if ch.get("items") or ch.get("unknown")}),
                       "add": sum(r["add"] or 0 for r in rows), "del": sum(r["del"] or 0 for r in rows),
                       "add_partial": any(r["add"] is None or r["add_partial"] for r in rows),
                       "del_partial": any(r["del"] is None or r["del_partial"] for r in rows),
                       "inferred": sum(1 for r in rows if r["src"] != "true"),
                       "outside": sum(1 for r in rows if r["outside"])}}


# ================================================================ S5: 风险标记 (规则 = 推断, 只标不拦)
#
# 三类 (问卷定的): 打转类 (同一文件改了又改都没过 / 连续失败) · 大改动与越界 · 破坏性操作。
# 纪律 (MISSION_CONTROL 硬不变量「误报零容忍」): 每条规则先只做「提示」, 在真实数据上审过精确率才允许升「警示」;
# 规则原文跟着标记一起下发, 界面上必须能看到「凭什么这么判」。全程只读: 只标, 不拦、不回滚、不自动干预。

THRASH_CYCLES = 3            # 「改同一个文件 -> 验证失败」这样的来回, 到几轮算打转
SPIKE_FAILS = 3              # 同一条时间线上**严格相邻**地连着失败几次算连续失败 (中间成功即清零; 4 次时近 30 天只有 0.7%)
LARGE_EDIT_LINES = 1200      # 单次改动的增删行数合计, 超过算大改动 (校准: 约 5% 的任务)
LARGE_TASK_FILES = 200       # 一个任务动过的文件数, 超过算大改动 (校准: 约 6% 的任务)

_SQL_HEADS = frozenset({"psql", "mysql", "sqlite3", "sqlcmd", "mongo", "mongosh", "redis-cli", "duckdb"})
_RE_SQL_DESTRUCTIVE = re.compile(r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", re.I)
_RE_MCP_DESTRUCTIVE = re.compile(r"^(remove|delete|destroy|drop|purge|wipe)_|_(remove|delete|destroy)$", re.I)


def _flagset(words: list) -> tuple[str, set]:
    """参数词 -> (短 flag 字母串, 长 flag 集合)。`-fdn` -> "fdn"。"""
    toks = [w[0] for w in words]
    return ("".join(t[1:] for t in toks if t.startswith("-") and not t.startswith("--")),
            {t.lower() for t in toks if t.startswith("--")})


def destructive_kind(name: str, inp: dict | None) -> str | None:
    """这次调用是不是破坏性操作 -> 人话说明; 不是 -> None。只认**明确**的那几种, 认不准的不标;
    预演 (`git clean -n` / `--dry-run` / PowerShell `-WhatIf`) 一个字节都不删, 不算。"""
    inp = inp or {}
    if name in ("Bash", "PowerShell"):
        ps = name == "PowerShell"
        env: dict = {}
        for sc in shell_parse(str(inp.get("command") or ""), ps)["cmds"]:
            head, args = _simple(sc["words"], env)
            if not head:
                continue
            short, long = _flagset(args)
            low = {w[0].lower() for w in args}
            if head in _SQL_HEADS and _RE_SQL_DESTRUCTIVE.search(" ".join(w[0] for w in args)):
                return "删库 (DROP / TRUNCATE)"                # 只认真的数据库客户端: 别的命令里出现这几个词不算
            if head in ("remove-item", "ri") or (ps and head in ("rm", "del", "rd", "rmdir")):
                if (low & {"-recurse", "-r"}) and "-force" in low and "-whatif" not in low:
                    return "PowerShell 递归强删"
                continue
            if head in ("rm", "del"):                         # 递归 + 强制 才算 (rm a.txt 不算)
                if ("r" in short.lower() or "--recursive" in long) and ("f" in short or "--force" in long):
                    return "rm -rf"
                continue
            if head == "git":
                gsub, gargs = _git_sub(args)
                gshort, glong = _flagset(gargs)
                dry = "n" in gshort or "--dry-run" in glong
                if gsub == "reset" and "--hard" in glong:
                    return "git reset --hard"
                if gsub == "clean" and ("f" in gshort or "--force" in glong) and not dry:
                    return "git clean"
                if gsub == "push" and ("f" in gshort or "--force" in glong) and not dry:
                    return "强推 (git push --force)"         # --force-with-lease 不在里面, 不算
                if gsub in ("checkout", "restore"):
                    pos = [w[0] for w in gargs if not w[0].startswith("-")]
                    if pos and all(x.rstrip("/") in (".", "*", ":") or x == ":/" for x in pos):
                        return "整棵树还原 (git checkout/restore .)"
    elif name.startswith("mcp__"):
        tool = name.split("__", 2)[-1]
        if _RE_MCP_DESTRUCTIVE.search(tool):
            return f"MCP 删除类调用 ({tool})"
    return None


def _spikes(calls: list[dict], need: int = SPIKE_FAILS) -> list[list[str]]:
    """同一条时间线上连着失败 need 次及以上 -> 每一串的调用 id。跨 agent 不算一串 (各干各的)。"""
    by_tl: dict = defaultdict(list)
    for c in calls:
        if c.get("t0") is not None:
            by_tl[(c.get("meta") or {}).get("tl") or "main"].append(c)
    out = []
    for cs in by_tl.values():
        run: list = []
        for c in sorted(cs, key=lambda c: c["t0"]):
            if FAIL_FLAGS & set(c.get("flags") or []):
                run.append(c["id"])
            else:
                if len(run) >= need:
                    out.append(run)
                run = []
        if len(run) >= need:
            out.append(run)
    return out


def _thrash(ledger: dict, changes: list[dict], calls: list[dict], roots: list[str],
            err_text: dict | None = None) -> list[tuple]:
    """「改了这个文件 -> 验证失败」的来回 >= THRASH_CYCLES 轮 -> (文件, 轮数, 相关调用)。

    - **按时间线分开算**: workflow 里几个 agent 各自在自己的 worktree 改同名文件, 不是一个人在来回;
    - **失败要冲着这个文件**: 失败的输出里点了这个文件的名字才算它的一轮 (整条时间线的失败不能摊给每个被改的文件——
      否则只改了文档也会被判「改了 3 轮没过」)。输出里点不出任何文件的失败, 不算给谁 (宁可漏标);
    - 只算改 / 写 (删、移、还原不是在「修」这个文件); 被工具拦下 (`<tool_use_error>`) 不是验证结果。"""
    err_text = err_text or {}
    fails_by_tl: dict = defaultdict(list)
    for c in calls:
        if ((c.get("meta") or {}).get("stage") == "验证" and (FAIL_FLAGS & set(c.get("flags") or []))
                and c.get("t0") is not None):
            txt = str(err_text.get(c["id"]) or "")
            if txt.lstrip().startswith("<tool_use_error>"):
                continue
            fails_by_tl[(c.get("meta") or {}).get("tl") or "main"].append((c["t0"], txt.replace("\\", "/").lower()))
    if not fails_by_tl:
        return []
    per_file: dict = defaultdict(list)
    for ch in changes:
        if ch.get("t") is None:
            continue
        for it in ch.get("items") or []:
            if it.get("op") in ("edit", "write"):
                per_file[(ch.get("tl") or "main", _norm_path(it["path"], roots)[0])].append((ch["t"], ch["id"]))
    out = []
    for (tl, path), evs in per_file.items():
        name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not name or "." not in name:
            continue                                         # 目录 / 没扩展名的: 点名判断不可靠, 不判
        mine = [(t, "fail", None) for t, txt in fails_by_tl.get(tl, []) if name in txt]
        seq = sorted([(t, "chg", cid) for t, cid in evs] + mine)
        cycles, armed, nodes = 0, False, []
        for _t, kind, cid in seq:
            if kind == "chg":
                armed = True
                nodes.append(cid)
            elif armed:                                      # 改完之后, 冲着它的验证失败 = 一轮
                cycles += 1
                armed = False
        if cycles >= THRASH_CYCLES:
            out.append((path, cycles, list(dict.fromkeys(nodes))))
    return sorted(out, key=lambda x: -x[1])


def risk_markers(ledger: dict | None, calls: list[dict], changes: list[dict], cwds,
                 err_text: dict | None = None) -> list[dict]:
    """一个任务的风险标记。每条都带: 规则原文 (why) + 证据 (相关调用 / 文件) + 级别 (先一律「提示」)。"""
    roots = sorted({str(c).replace("\\", "/").rstrip("/").lower() for c in (cwds or set()) if c}, key=len, reverse=True)
    out: list = []

    dest: dict = defaultdict(list)
    for c in calls:
        k = (c.get("meta") or {}).get("destructive")
        if k:
            dest[k].append(c["id"])
    for kind, ids in sorted(dest.items(), key=lambda kv: -len(kv[1])):
        out.append({"rule": "destructive", "level": "info", "label": f"破坏性操作：{kind}", "n": len(ids),
                    "why": "命令里出现了会丢东西的操作（rm -rf / git reset --hard / git clean / 强推 / 整棵树还原 / "
                           "MCP 删除类调用 / DROP 表）。只标出来，不拦你。",
                    "refs": [{"kind": "call", "id": i} for i in ids]})

    for run in _spikes(calls):
        out.append({"rule": "error-spike", "level": "info", "label": f"连着失败 {len(run)} 次", "n": len(run),
                    "why": f"同一条时间线上严格相邻地连续 {SPIKE_FAILS} 次及以上调用失败，中间没有成功过。可能是卡住了。",
                    "refs": [{"kind": "call", "id": i} for i in run]})

    for path, cycles, nodes in _thrash(ledger or {}, changes, calls, roots, err_text):
        out.append({"rule": "thrash", "level": "info", "label": f"{path} 改了 {cycles} 轮都没通过", "n": cycles,
                    "why": f"「改这个文件 → 验证失败、而且失败输出里点了它的名」的来回达到 {THRASH_CYCLES} 轮及以上"
                           "（推断：验证是按阶段标注判的）。失败里没点名的不算给它；一口气分几次写一个大文件不算。",
                    "refs": [{"kind": "call", "id": i} for i in nodes]})

    big = [c for c in calls if (c.get("meta") or {}).get("edit_lines", 0) >= LARGE_EDIT_LINES]
    if big:
        out.append({"rule": "large-edit", "level": "info", "label": f"单次大改动 {len(big)} 次", "n": len(big),
                    "why": f"一次调用增删合计 ≥ {LARGE_EDIT_LINES} 行（按实际落盘的补丁算；算不出行数的不参与）。",
                    "refs": [{"kind": "call", "id": c["id"]} for c in big]})
    if ledger and ledger["totals"]["files"] >= LARGE_TASK_FILES:
        out.append({"rule": "large-task", "level": "info", "label": f"这次任务动了 {ledger['totals']['files']} 个文件",
                    "n": ledger["totals"]["files"],
                    "why": f"一个任务动过的文件数 ≥ {LARGE_TASK_FILES} 个。这条是按任务判的，没有单独的一步可跳。",
                    "refs": []})
    if ledger and ledger["totals"]["outside"]:
        outs = [f for f in ledger["files"] if f["outside"]]
        paths = [f["path"] for f in outs][:6]
        out.append({"rule": "outside", "level": "info", "label": f"改了工作目录以外的 {ledger['totals']['outside']} 个文件",
                    "n": ledger["totals"]["outside"],
                    "why": "路径不在这个任务的工作目录下。不算的：Claude 自己的临时脚本、自动记忆、系统临时目录；"
                           "算的：别的仓库、~/.claude 下的设置 / agent / skill。跨仓库干活会正常出现这一条。",
                    "detail": "、".join(paths),
                    "refs": [{"kind": "call", "id": i} for i in dict.fromkeys(c for f in outs for c in f["calls"])]})
    return out


def fmt_dur(s: float) -> str:
    s = max(0, int(round(s)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    mm = int(round(s / 60))
    return f"{mm // 60}h{mm % 60:02d}m"


# ================================================================ 单文件扫描 (增量 + 缓存)

class FileTrace:
    """一个 JSONL 文件解析后的精简行序列。按 (mtime,size) 缓存; 文件只增长时**只读新增部分**。"""

    __slots__ = ("path", "sig", "offset", "rows", "resp", "resp_calls", "resp_model", "first_ts", "last_ts",
                 "session_id", "agent_id", "cwd", "branch", "first_user", "first_user_hash", "meta_body",
                 "gloss", "_last_model", "_last_effort")

    def __init__(self, path: Path):
        self.path = path
        self.sig = None
        self.offset = 0
        self.rows: list[dict] = []
        self.resp: dict = {}                     # (msg_id, req_id) -> usage 元组 (最后一行为准)
        self.resp_calls = defaultdict(int)       # 该响应里 tool_use 的个数 (发起成本均摊用)
        self.resp_model: dict = {}
        self.first_ts = None
        self.last_ts = None
        self.session_id = None
        self.agent_id = None
        self.cwd = None
        self.branch = None
        self.first_user = None                   # agent 文件的首条指令 (显示用, 截断)
        self.first_user_hash = None              # 首条指令全文的归一哈希 (按指令回链用)
        self.meta_body: dict = {}                # sourceToolUseID -> (字符数, 估算 token): Skill 正文注入
        self.gloss: dict = {"skill": {}, "agent": {}, "mcp": {}}   # 名词说明 (官方原文, 来自清单附件)
        self._last_model = None
        self._last_effort = None


_FILES: dict[str, FileTrace] = {}
_LOCK = threading.RLock()


def usage_tuple(u: dict) -> tuple:
    """(input, output, cache_5m, cache_1h, cache_read) —— 与 parser 同一口径 (含 cache_creation 回退)。"""
    cc = u.get("cache_creation") or {}
    c5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    c1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    if not cc:
        c5 = int(u.get("cache_creation_input_tokens") or 0)
    return (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), c5, c1,
            int(u.get("cache_read_input_tokens") or 0))


def _scan_line(ft: FileTrace, o: dict, off: int) -> None:
    """一行 -> 若干精简行。每行在**发布时**就带好序号 i (读者不加锁遍历也看不到半成品)。"""
    rows = ft.rows

    def emit(row):
        row["i"] = len(rows)
        rows.append(row)

    t = o.get("type")
    ts = parse_ts(o.get("timestamp"))
    if ts is not None:
        ft.first_ts = ts if ft.first_ts is None else min(ft.first_ts, ts)
        ft.last_ts = ts if ft.last_ts is None else max(ft.last_ts, ts)
    if o.get("sessionId") and not ft.session_id:
        ft.session_id = o["sessionId"]
    if o.get("agentId") and not ft.agent_id:
        ft.agent_id = o["agentId"]
    if o.get("cwd"):
        ft.cwd = o["cwd"]
    if o.get("gitBranch"):
        ft.branch = o["gitBranch"]
    skill = skill_norm(o.get("attributionSkill")) or None
    msg = o.get("message") or {}
    content = msg.get("content")

    if t == "assistant":
        model = msg.get("model")
        synthetic = model == SYNTHETIC_MODEL
        key = (str(msg.get("id") or ""), str(o.get("requestId") or ""))
        u = msg.get("usage")
        if isinstance(u, dict) and (key[0] or key[1]):
            first = key not in ft.resp
            ft.resp[key] = usage_tuple(u)                       # 先存 usage (最后一行为准), 再发布行
            ft.resp_model[key] = model or "unknown"
            if first:
                emit({"k": "resp", "ts": ts, "key": key, "skill": skill, "cwd": ft.cwd, "synthetic": synthetic})
        if model and not synthetic:
            if ft._last_model and model != ft._last_model:
                emit({"k": "event", "ts": ts, "ev": "model", "label": f"模型切换 → {model}", "skill": skill})
            ft._last_model = model
        eff = o.get("perTurnEffort") or o.get("effort")
        if eff:
            if ft._last_effort and eff != ft._last_effort:
                emit({"k": "event", "ts": ts, "ev": "effort", "label": f"effort {ft._last_effort} → {eff}",
                      "skill": skill})
            ft._last_effort = eff
        if synthetic:
            return                                               # 占位消息: 不是回复, 不进回放
        if isinstance(content, list):
            for bi, b in enumerate(content):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    name = str(b.get("name") or "?")
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    label, sub = call_label(name, inp)
                    try:
                        in_chars = len(json.dumps(inp, ensure_ascii=False))
                    except (TypeError, ValueError):
                        in_chars = 0
                    ft.resp_calls[key] += 1
                    row = {"k": "call", "ts": ts, "id": b.get("id"), "name": name, "label": label, "sub": sub,
                           "key": norm_key(name, inp), "resp": key, "skill": skill, "in_chars": in_chars,
                           "off": off, "bg": bool(inp.get("run_in_background")),
                           "wait": is_deliberate_wait(name, inp), "stage": classify_call(name, inp)}
                    chg = file_changes(name, inp, row["stage"])
                    if chg:
                        row["chg"] = chg                        # 改动清单 (真值 / 推断, 见 file_changes)
                    dk = destructive_kind(name, inp)
                    if dk:
                        row["destructive"] = dk                 # 破坏性操作 (规则, 见 destructive_kind)
                    if name.startswith("mcp__"):
                        row["in_ids"] = input_ids(inp)           # 数据依赖 (推断): 参数里的 ID -> 字段路径
                    if name in AGENT_TOOLS:
                        row["prompt_hash"] = _norm_hash(inp.get("prompt"))
                    emit(row)
                elif bt == "text":
                    s = str(b.get("text") or "").strip()
                    if s:
                        emit({"k": "say", "ts": ts, "text": short(s, TEXT_CHARS), "long": len(s) > TEXT_CHARS,
                              "skill": skill, "off": off, "bi": bi})
                elif bt == "thinking":
                    s = str(b.get("thinking") or "").strip()
                    if s:                                        # 实测仅约 13% 非空; 空的不显示, 不伪造
                        emit({"k": "think", "ts": ts, "text": short(s, TEXT_CHARS), "long": len(s) > TEXT_CHARS,
                              "skill": skill, "off": off, "bi": bi})
        return

    if t == "user":
        if o.get("isMeta"):
            src = o.get("sourceToolUseID")
            if src:                                              # Skill 正文注入: 它才是 Skill 的真实上下文成本
                body = text_of(content)
                ft.meta_body[src] = (len(body), est_tokens(body))
            return
        origin = (o.get("origin") or {}).get("kind")
        if o.get("isCompactSummary"):
            emit({"k": "event", "ts": ts, "ev": "compact-summary", "label": "上下文压缩后续写"})
            return
        is_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        if is_result:
            tur = o.get("toolUseResult")
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                rc = b.get("content")
                chars, est, media = result_size(rc)
                txt = text_of(rc)
                extra, interrupted = {}, False
                if isinstance(tur, dict):
                    for k2 in ("agentId", "runId", "workflowName", "status", "isAsync", "taskId",
                               "persistedOutputPath", "backgroundTaskId", "summary", "scriptPath", "transcriptDir"):
                        v = tur.get(k2)
                        if v not in (None, "", False):
                            extra[k2] = short(v, 200) if k2 == "summary" else v
                    interrupted = bool(tur.get("interrupted"))
                patch = patch_stats(tur) if isinstance(tur, dict) else None
                emit({"k": "result", "ts": ts, "tid": b.get("tool_use_id"),
                      "err": bool(b.get("is_error")) or interrupted, "interrupted": interrupted,
                      "chars": chars, "est": est, "media": media, "preview": short(txt, PREVIEW_CHARS),
                      "extra": extra, "off": off, "ids": id_tokens(txt), "patch": patch,
                      "etext": (txt[:1500] + "\n" + txt[-1500:]) if (b.get("is_error") or interrupted) else None})
            return
        if ft.first_user is None and origin is None and not (isinstance(content, str) and content.startswith("<")):
            full = prompt_text(content)[0]
            ft.first_user = short(full, 2000, flatten=False)       # agent 文件的首条指令
            ft.first_user_hash = _norm_hash(full)
        if origin == "task-notification":
            emit({"k": "notify", "ts": ts, "queued": False, **notify_fields(text_of(content))})
            return
        if origin == "human":
            text, att = prompt_text(content)
            emit({"k": "prompt", "ts": ts, "uuid": o.get("uuid"), "text": short(text, 2000, flatten=False),
                  "att": att, "cwd": o.get("cwd") or ft.cwd, "branch": o.get("gitBranch") or ft.branch})
            return
        txt = text_of(content)                                   # 其余 user 行: 打断 / 本地命令
        if "[Request interrupted by user" in txt:
            emit({"k": "interrupt", "ts": ts})
        elif txt.lstrip().startswith("<command-name>"):
            m = re.search(r"<command-name>\s*(.*?)\s*</command-name>", txt, re.S)
            a = re.search(r"<command-args>\s*(.*?)\s*</command-args>", txt, re.S)
            emit({"k": "event", "ts": ts, "ev": "command",
                  "label": short(f"命令 {m.group(1) if m else ''} {a.group(1) if a else ''}".strip(), 120)})
        return

    if t == "attachment":
        a = o.get("attachment") if isinstance(o.get("attachment"), dict) else {}
        at = a.get("type")
        if at == "skill_listing":                                # 名词说明: 只收进词典, 不进回放
            ft.gloss["skill"].update(parse_listing(a.get("names"), a.get("content")))
            return
        if at == "agent_listing_delta":
            ft.gloss["agent"].update(parse_listing(a.get("addedTypes"), a.get("addedLines")))
            return
        if at == "mcp_instructions_delta":
            for blk in a.get("addedBlocks") or []:
                m = re.match(r"^## (\S+)\n(.*)", str(blk), re.S)
                if m:
                    ft.gloss["mcp"][m.group(1)] = m.group(2).strip()[:600]
            return
        if not at or at in _NOISE_ATTACHMENTS:
            return
        ats = ts if ts is not None else parse_ts(a.get("timestamp"))
        if at == "queued_command":
            p = a.get("prompt")
            ptxt = p if isinstance(p, str) else text_of(p)
            if isinstance(p, str) and p.startswith("[{"):        # 有时被序列化成 Python repr 字符串
                found = re.findall(r"'text':\s*'(.*?)'\}", p, re.S)
                ptxt = " ".join(found) if found else p
            if a.get("commandMode") == "task-notification":
                emit({"k": "notify", "ts": ats, "queued": True, **notify_fields(ptxt)})
            else:
                emit({"k": "interject", "ts": ats, "text": short(ptxt, 400)})
            return
        if at == "edited_text_file":
            emit({"k": "event", "ts": ats, "ev": "edited",
                  "label": "文件被外部修改: " + short(a.get("filename") or a.get("path") or "", 100)})
            return
        if at == "invoked_skills":
            emit({"k": "event", "ts": ats, "ev": "skills", "label": "加载 skill 内容"})
            return
        if "hook" in at.lower() or a.get("hookEvent") or a.get("hookName"):
            emit({"k": "event", "ts": ats, "ev": "hook",
                  "label": "hook: " + short(a.get("hookEvent") or a.get("hookName") or at, 80)})
        return

    if t == "system":
        st = o.get("subtype")
        if st == "compact_boundary":
            emit({"k": "event", "ts": ts, "ev": "compact", "label": "上下文压缩"})
        elif st == "api_error":
            emit({"k": "event", "ts": ts, "ev": "api_error", "label": "API 错误 (自动重试)"})


def load_file(path: Path) -> FileTrace | None:
    """读取 (或增量续读) 一个 JSONL。只有以换行结尾的完整行才前移 offset —— 正在写的半行下次再读。
    单写者 (_LOCK 内), 读者不加锁: 行在发布时已完整 (带序号 i; resp 行发布前 usage 已入表)。"""
    path = Path(path)
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime, st.st_size)
    with _LOCK:
        ft = _FILES.get(key)
        if ft is not None and ft.sig == sig:
            return ft
        if ft is None or st.st_size < ft.offset:
            ft = FileTrace(path)                              # 新文件 / 被截断重写 -> 全量
        try:
            with open(path, "rb") as fh:
                fh.seek(ft.offset)
                off = ft.offset
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break                                 # 半行: 不前移, 等写完
                    line_off = off
                    off += len(raw)
                    s = raw.strip()
                    if not s:
                        continue
                    try:
                        o = json.loads(s.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if isinstance(o, dict):
                        _scan_line(ft, o, line_off)
                ft.offset = off
        except OSError:
            return ft if ft.rows else None
        ft.sig = sig
        _FILES[key] = ft
        return ft


def read_line_at(path: Path, off: int) -> dict | None:
    """按字节偏移读回原始行 (明细面板取完整原文用, 不常驻内存)。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(off)
            raw = fh.readline()
        o = json.loads(raw.decode("utf-8", "replace"))
        return o if isinstance(o, dict) else None
    except (OSError, ValueError):
        return None


# ================================================================ 会话目录索引

def session_dir(main_path: Path) -> Path:
    return Path(main_path).with_suffix("")


_DIR_TTL = 10.0                                   # 目录索引短缓存: 一次列表会对同一会话构建多个任务
_AGENT_IDX: dict = {}                             # sdir -> (t, {agentId: path})
_META: dict = {}                                  # meta.json 路径 -> (mtime, dict)
_SCRIPTS: dict = {}                               # sdir -> (t, [script paths])


def _agent_files(sdir: Path) -> dict[str, Path]:
    """<会话>/subagents/**/agent-<id>.jsonl -> {agentId: path} (返回副本, 调用方可以 setdefault)。"""
    now = time.time()
    hit = _AGENT_IDX.get(str(sdir))
    if hit and now - hit[0] < _DIR_TTL:
        return dict(hit[1])
    out = {}
    sub = sdir / "subagents"
    if sub.is_dir():
        for p in sub.rglob("agent-*.jsonl"):
            out[p.stem[len("agent-"):]] = p
    _AGENT_IDX[str(sdir)] = (now, out)
    return dict(out)


def agent_meta(path: Path) -> dict:
    mp = path.with_name(path.stem + ".meta.json")
    try:
        mt = mp.stat().st_mtime
    except OSError:
        return {}
    hit = _META.get(str(mp))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        d = json.loads(mp.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    _META[str(mp)] = (mt, d)
    return d


def _inside(path, root: Path) -> Path | None:
    """transcript 里记下的路径只在 ~/.claude/projects 之内才读 (不跟着任意路径走)。"""
    try:
        rp = Path(path).resolve()
        rp.relative_to(root.resolve())
        return rp
    except (TypeError, ValueError, OSError):
        return None


def _workflow_script(sdir: Path, run_id: str) -> Path | None:
    """本会话目录下找; 找不到再去「同一会话 id 的兄弟项目目录」找 —— Claude Code 按**启动 workflow 那一刻的 cwd**
    选项目目录存脚本 (会话中途 cd 进子目录 / 在 worktree 里启动时, 脚本就不在会话自己的目录下)。"""
    now = time.time()
    hit = _SCRIPTS.get(str(sdir))
    if not hit or now - hit[0] >= _DIR_TTL:
        found = []
        for d in [sdir] + [q for q in sdir.parent.parent.glob(f"*/{sdir.name}") if q != sdir]:
            sd = d / "workflows" / "scripts"
            if sd.is_dir():
                found.extend(sorted(sd.iterdir()))
        hit = (now, found)
        _SCRIPTS[str(sdir)] = hit
    return next((p for p in hit[1] if run_id in p.name and p.suffix == ".js"), None)


# ================================================================ 任务切分

def session_tasks(main_path: Path) -> tuple[list[dict], list[dict]]:
    """把一个主会话切成任务。返回 (tasks, 开场行)。

    任务 = 一次真人提问 + 这一轮的全部行 + 后台完成后被通知唤起的「延续段」
    (按通知里的 <tool-use-id> / <task-id> **真值**找回启动它的任务; 找不到才挂到当前任务, 标 linked=False)。
    - 两轮之间敲的本地命令 (/model 等) 归到**下一个**任务: 它作用于下一轮, 也不该把上一轮的时长撑到几小时后;
    - 状态为 stopped/killed 的后台通知 (多见于恢复会话时「上次没跑完」) 不开延续段, 只作为启动调用上的备注;
    - 拥有全文件最后一条活动行的任务标 tail=True (它才可能是「正在跑」的那个)。
    """
    ft = load_file(main_path)
    if ft is None:
        return [], []
    tasks: list[dict] = []
    call_owner: dict[str, dict] = {}
    bg_owner: dict[str, dict] = {}
    cur = seg = None
    pre: list[dict] = []
    held: list[dict] = []                 # 暂存的本地命令: 等下一条非命令行决定归属
    tail_owner = None

    def flush_held(into_rows):
        into_rows.extend(held)
        held.clear()

    for r in list(ft.rows):
        k = r["k"]
        if k == "event" and r.get("ev") == "command":
            held.append(r)
            continue
        if k == "prompt":
            cur = {"id": r["uuid"] or f"{Path(main_path).stem}:{len(tasks)}", "prompt": r, "segments": [],
                   "main": Path(main_path), "session_id": ft.session_id or Path(main_path).stem, "bg_notes": []}
            seg = {"kind": "main", "rows": [r]}
            flush_held(seg["rows"])
            cur["segments"].append(seg)
            tasks.append(cur)
            tail_owner = cur
            continue
        if k == "notify" and not r.get("queued"):
            owner = next((call_owner[t] for t in r.get("tids") or [] if t in call_owner), None) \
                or next((bg_owner[t] for t in r.get("task_ids") or [] if t in bg_owner), None)
            linked = owner is not None
            if r.get("status") in BG_STOPPED and owner is not None:
                owner["bg_notes"].append(r)                  # 不开延续段; 后续行照常归当前任务
                continue
            owner = owner or cur
            if owner is None:
                pre.extend(held)
                held.clear()
                pre.append(r)
                continue
            if cur is not None:
                flush_held(cur["segments"][-1]["rows"])      # 通知之前敲的命令留在当时的任务里
            seg = {"kind": "continuation", "rows": [r], "linked": linked}
            owner["segments"].append(seg)
            cur = owner
            tail_owner = cur
            continue
        if cur is None:
            pre.extend(held)
            held.clear()
            pre.append(r)
            continue
        flush_held(seg["rows"])
        seg["rows"].append(r)
        if k in ACT_KINDS and not r.get("synthetic"):
            tail_owner = cur
        if k == "call" and r.get("id"):
            call_owner[r["id"]] = cur
        elif k == "result":
            ex = r.get("extra") or {}
            for bid in (ex.get("backgroundTaskId"), ex.get("taskId")):
                if bid:
                    bg_owner[str(bid)] = cur
    if held:                                                 # 文件末尾的命令: 没有下一轮了, 留在当前任务
        flush_held(seg["rows"] if seg is not None else pre)
    if tail_owner is not None:
        tail_owner["tail"] = True
    return tasks, pre


# ================================================================ 构树

class _Ctx:
    """一次构树的共享上下文。"""

    def __init__(self, main_path: Path, now: float, running: bool):
        self.main = Path(main_path)
        self.sdir = session_dir(main_path)
        self.agents = _agent_files(self.sdir)
        self.now = now
        self.running = running
        self.usage: dict = {}                # resp key -> (usage 元组, model)  —— 整个任务的去重登记簿
        self.synthetic: set = set()          # 占位消息的 resp key (0 token, 不算 API 响应次数)
        self.call_loc: dict = {}             # tool_use_id -> (path, call_off, result_off)
        self.text_loc: dict = {}             # say/think 节点 id -> (path, off, block_index, kind)
        self.intervals: list = []            # (t0, t1, label) 三段耗时用
        self.calls: list[dict] = []          # 全任务的调用节点
        self.visited: set = set()            # 已展开的 agentId (防环 / 防重复计数)
        self.cwds: set = set()               # 任务里各响应生效时的 cwd (项目归属对齐 /tokens)
        self.claimed = _claimed_agents(self)  # 本会话里被 agentId **明确**引用过的 agent (兜底回链不许碰)
        self.gloss: dict = {"skill": {}, "agent": {}, "mcp": {}}
        self.script_loc: dict = {}           # runId -> (脚本路径或 None, run transcript 目录)
        self.errs: dict = {}                 # 失败调用 id -> 返回开头 (统计页的「最近错误」用; 不进回放树)
        self.err_text: dict = {}             # 失败调用 id -> 返回的头和尾 (打转规则判断失败是不是冲着某个文件)
        self.changes: list = []              # 按时间登记的改动 (改动清单用)


_CLAIMED: dict = {}                               # 会话目录 -> (t, 被明确引用的 agentId 集合)


def _claimed_agents(ctx: _Ctx) -> set:
    """本会话里被某个结果**明确**写了 agentId 的 agent。同一会话在一次列表刷新里要算几十遍, 缓存 _DIR_TTL 秒。"""
    now = time.time()
    hit = _CLAIMED.get(str(ctx.sdir))
    if hit and now - hit[0] < _DIR_TTL:
        return hit[1]
    ids = set()
    for p in [ctx.main] + list(ctx.agents.values()):
        ft = load_file(p)
        if ft is None:
            continue
        for r in list(ft.rows):
            if r["k"] == "result":
                aid = (r.get("extra") or {}).get("agentId")
                if aid:
                    ids.add(str(aid))
    _CLAIMED[str(ctx.sdir)] = (now, ids)
    return ids


def usage_sum(keys, ctx: _Ctx) -> dict | None:
    """一组去重后的响应 key -> token 真值 (含按模型拆分, $ 由 serve 层按模型换算)。"""
    if keys is None:
        return None
    by_model: dict = defaultdict(lambda: [0, 0, 0, 0, 0])
    n = 0
    for k in keys:
        u = ctx.usage.get(k)
        if not u or not u[0]:
            continue
        if k not in ctx.synthetic:
            n += 1
        acc = by_model[u[1]]
        for i in range(5):
            acc[i] += u[0][i]
    tot = [sum(v[i] for v in by_model.values()) for i in range(5)]
    return {"total": sum(tot), "input": tot[0], "output": tot[1], "cache_write": tot[2] + tot[3],
            "cache_5m": tot[2], "cache_1h": tot[3], "cache_read": tot[4],
            "by_model": {m: list(v) for m, v in by_model.items()}, "responses": n}


def _timeline(rows: list[dict], ft: FileTrace, ctx: _Ctx, depth: int) -> tuple[list, set, list]:
    """一条时间线 (主线程某段 / 某个 agent 文件) -> (子节点, 本线及其全部子树的 resp keys)。

    - skill 分组: `Skill` 调用开一个 skill 节点; 之后行的 attributionSkill 等于它就留在里面, 否则收口。
      没有标记的行**不猜**归属。你的插话 / 打断 / 后台通知会先收口, 保证树按时间顺序。
    - Agent / Workflow 调用展开成子树 (agentId / runId 真值链接), 子树 keys 并入本线。
    - 「模型生成」打底区间只由活动行按 IDLE_GAP 连出 (空闲不算)。
    - S2: 每个调用标阶段 (推断), 连成阶段段落; MCP 调用找数据来源 (推断: 参数里的 ID 出现在此前某个返回里)。
    返回 (子节点, keys, 阶段段落)。
    """
    keys: set = set()
    call_rows = {r["id"]: r for r in rows if r["k"] == "call" and r.get("id")}
    seen_ids: list = []                                      # [(来源调用 id, ID 集合)] —— 只收**已经返回**的结果
    results = {r["tid"]: r for r in rows if r["k"] == "result" and r.get("tid")}
    last_resp_pos = max((j for j, r in enumerate(rows) if r["k"] == "resp" and not r.get("synthetic")), default=-1)
    top: list[dict] = []
    sk_node = None
    call_nodes: list[dict] = []
    act_ts: list[float] = []

    def into():
        return sk_node["children"] if sk_node is not None else top

    for j, r in enumerate(rows):
        k = r["k"]
        if k in ACT_KINDS and r.get("ts") is not None and not r.get("synthetic") \
                and not (k == "notify" and r.get("status") in BG_STOPPED):
            act_ts.append(r["ts"])
        if k == "result":
            if r.get("ids") and r.get("tid") in call_rows:
                seen_ids.append((r["tid"], set(r["ids"])))
            continue
        if k == "prompt":
            continue
        sk = r.get("skill")
        if k in ("call", "say", "think", "resp") and not r.get("synthetic"):
            if k == "call" and r["name"] == "Skill":
                sk_node = {"id": f"skill:{r['id']}", "kind": "skill", "label": skill_norm(r["label"]),
                           "children": [], "keys": set(), "t0": r["ts"], "t1": r["ts"], "tagged": False}
                top.append(sk_node)
            elif sk_node is not None and sk != sk_node["label"]:
                sk_node = None
            if sk and sk_node is None:
                sk_node = {"id": f"skill:{ft.agent_id or 'main'}:{r.get('i')}", "kind": "skill", "label": sk,
                           "continued": True, "children": [], "keys": set(), "t0": r["ts"], "t1": r["ts"],
                           "tagged": True}
                top.append(sk_node)
            if sk_node is not None and sk == sk_node["label"]:
                sk_node["tagged"] = True
        if k == "resp":
            key = r["key"]
            keys.add(key)
            ctx.usage[key] = (ft.resp.get(key), ft.resp_model.get(key, "unknown"))
            if r.get("synthetic"):
                ctx.synthetic.add(key)
            elif r.get("cwd"):
                ctx.cwds.add(r["cwd"])
            if sk_node is not None and not r.get("synthetic"):
                sk_node["keys"].add(key)
            continue
        if k == "call":
            stale = j < last_resp_pos                        # 时间线已经往下走了 -> 这个没结果的调用不会再回来
            deps = []
            if r.get("in_ids"):                              # 数据依赖 (推断): 目标只看 MCP, 来源可以是任何调用
                used = set()
                for tok, path in r["in_ids"].items():
                    for src_id, ids in reversed(seen_ids):
                        if tok in ids and src_id != r["id"]:
                            if src_id not in used:
                                src = call_rows[src_id]
                                deps.append({"from": src_id, "from_name": src["name"], "from_label": src["label"],
                                             "key": path, "value": tok})
                                used.add(src_id)
                            break
                    if len(deps) >= 3:
                        break
            node = _call_node(r, results.get(r["id"]), ft, ctx, depth, stale)
            if deps:
                node["meta"]["deps"] = deps
            sub = node.pop("_keys", set())
            keys |= sub
            call_nodes.append(node)
            into().append(node)
            if sk_node is not None:
                sk_node["keys"] |= sub
            continue
        rid = f"{ft.agent_id or 'main'}:{r.get('i')}"       # 文件内行序号 -> 稳定唯一
        if k in ("say", "think"):
            nid = f"{k}:{rid}"
            into().append({"id": nid, "kind": k, "label": r["text"], "long": r.get("long", False),
                           "t0": r["ts"], "t1": r["ts"]})
            ctx.text_loc[nid] = (str(ft.path), r.get("off"), r.get("bi"), k)
        elif k == "event":
            into().append({"id": f"ev:{rid}", "kind": "event", "ev": r.get("ev"),
                           "label": r["label"], "t0": r["ts"], "t1": r["ts"]})
        elif k in ("interject", "interrupt", "notify"):
            sk_node = None                                    # 先收口: 之后的行另起 (延续) skill 节点, 保持时间顺序
            if k == "interject":
                top.append({"id": f"user:{rid}", "kind": "user", "ev": "interject", "label": "你追加了一条消息",
                            "text": r.get("text", ""), "t0": r["ts"], "t1": r["ts"]})
            elif k == "interrupt":
                top.append({"id": f"user:{rid}", "kind": "user", "ev": "interrupt", "label": "你打断了",
                            "t0": r["ts"], "t1": r["ts"]})
            else:
                st = r.get("status") or ""
                if st in BG_FAIL:
                    label, ev, flags = "后台任务失败", "notify-fail", ["bg-fail"]
                elif st in BG_STOPPED:
                    label, ev, flags = "后台任务被停止", "notify-stopped", ["bg-stopped"]
                else:
                    label, ev, flags = "后台任务完成", "notify", []
                top.append({"id": f"ev:{rid}", "kind": "event", "ev": ev, "tids": r.get("tids") or [],
                            "label": label + (f": {r['summary']}" if r.get("summary") else ""), "flags": flags,
                            "t0": r["ts"], "t1": r["ts"]})

    for n in top:                                            # skill 节点收尾: 时间范围 + token 真值
        if n["kind"] == "skill":
            ts = [t for c in n["children"] for t in (c.get("t0"), c.get("t1")) if t]
            if ts:
                n["t0"], n["t1"] = min(ts), max(ts)
            kset = n.pop("keys")
            n["tokens"] = usage_sum(kset, ctx) if n["tagged"] else None
            if not n["tagged"]:
                n["note"] = "之后的行没有 skill 归属标记 (Claude Code 未标注), 影响范围无法确定"
    flag_repeats([{"key": c["meta"]["key"], "failed": "fail" in c["flags"], "flags": c["flags"],
                   "out": c["meta"].get("out_sig"), "async": c["meta"]["async"]} for c in call_nodes])
    ctx.intervals.extend(active_intervals(act_ts))
    strong = {"fail", "bg-fail", "huge", "loop"}
    stages = stage_runs([{"id": c["id"], "name": c["name"], "stage": c["meta"].get("stage") or "运行",
                          "t0": c.get("t0"), "t1": c.get("t1"),
                          "fail": bool({"fail", "bg-fail"} & set(c["flags"])),
                          "strong": bool(strong & set(c["flags"]))} for c in call_nodes],
                        t_end=max(act_ts) if act_ts else None)
    return top, keys, stages


def _find_agent_by_prompt(r: dict, res: dict | None, ctx: _Ctx) -> tuple[str | None, str]:
    """Agent 结果里没有 agentId 时的**证据制**回退。返回 (agentId 或 None, 原因)。

    只有同时满足才回链: 该 agent 没被任何结果明确引用过; 它的首条指令**全文**(归一后) 与调用的 prompt 相同;
    它的首行时间落在 [调用时间, 结果时间或现在] 内 (± FALLBACK_SLACK)。多个候选 -> 不猜, 标 ambiguous。"""
    h = r.get("prompt_hash")
    if not h:
        return None, "unlinked"
    lo = (r["ts"] or 0) - FALLBACK_SLACK
    hi = ((res or {}).get("ts") or ctx.now) + FALLBACK_SLACK
    hits = []
    for aid, p in ctx.agents.items():
        if aid in ctx.visited or aid in ctx.claimed:
            continue
        ft = load_file(p)
        if ft is None or ft.first_user_hash != h or ft.first_ts is None:
            continue
        if lo <= ft.first_ts <= hi:
            hits.append(aid)
    if len(hits) == 1:
        return hits[0], "linked-by-prompt"
    return None, ("ambiguous" if hits else "unlinked")


def _call_node(r: dict, res: dict | None, ft: FileTrace, ctx: _Ctx, depth: int, stale: bool) -> dict:
    name = r["name"]
    cat = tool_category(name)
    t0 = r["ts"]
    t1 = res["ts"] if res else None
    flags: list[str] = []
    extra = (res or {}).get("extra") or {}
    is_async = bool(extra.get("isAsync")) or extra.get("status") == "async_launched" or bool(r.get("bg"))
    if res is None:
        if ctx.running and not stale:
            t1 = ctx.now
            flags.append("running")                          # 还在跑
        else:
            flags.append("no-result")                        # 没等到结果 (被打断 / 会话结束 / 已被越过)
    elif res.get("err"):
        flags.append("fail")
        ctx.errs[r["id"]] = res.get("preview") or ""
        ctx.err_text[r["id"]] = res.get("etext") or res.get("preview") or ""
    resp_u = ft.resp.get(r["resp"])
    n_par = max(1, ft.resp_calls.get(r["resp"], 1))
    # 结果体积: 没有结果 -> None (不是 0); 含图片/文档等估不出的块 -> None; Skill 加上注入的正文
    if res is None:
        r_chars, r_est, media = None, None, []
    else:
        r_chars, r_est, media = res.get("chars", 0), res.get("est"), list(res.get("media") or [])
    body = ft.meta_body.get(r["id"]) if name == "Skill" else None
    if body and r_chars is not None:
        r_chars += body[0]
        r_est = None if r_est is None else r_est + body[1]
    if r_chars is not None and r_chars >= HUGE_RESULT_CHARS:
        flags.append("huge")
    elif extra.get("persistedOutputPath"):
        flags.append("persisted")                            # 已另存: 上下文里只有预览, 不是「撑大上下文」
    node = {
        "id": r["id"], "kind": "call", "cat": cat, "name": name, "label": r["label"], "sub": r.get("sub", ""),
        "t0": t0, "t1": t1, "flags": flags,
        "issue_est": int((resp_u[1] if resp_u else 0) / n_par + 0.5), "result_est": r_est,
        "meta": {"key": r["key"], "result_chars": r_chars, "in_chars": r.get("in_chars", 0),
                 "async": is_async, "parallel": n_par, "wait": bool(r.get("wait")), "stage": r.get("stage"),
                 "media": media, "skill_body": bool(body),
                 "out_sig": (hashlib.sha1((res.get("preview") or "").encode("utf-8", "replace")).hexdigest()[:12]
                             + f":{res.get('chars', 0)}") if res else None},
        "children": [],
    }
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        node["meta"]["server"] = parts[1] if len(parts) > 1 else "?"
    node["meta"]["tl"] = ft.agent_id or "main"               # 属于哪条时间线 (连续失败按时间线算)
    if r.get("destructive"):
        node["meta"]["destructive"] = r["destructive"]
        flags.append("destructive")
    ctx.call_loc[r["id"]] = (str(ft.path), r.get("off"), (res or {}).get("off"))
    if r.get("chg"):
        chg = dict(r["chg"])
        ln = sum((it.get("add") or 0) + (it.get("del") or 0) for it in chg["items"])
        pt = (res or {}).get("patch")
        if pt and chg["src"] == "true" and chg["items"]:      # 实际落盘的补丁 > 按请求算的行数
            it = dict(chg["items"][0])
            it.update(add=pt["add"], **{"del": pt["del"]})
            it.pop("approx", None)
            it["patched"] = True
            if pt.get("user_modified"):
                it["user_modified"] = True
            chg["items"] = [it]
        ln = sum((it.get("add") or 0) + (it.get("del") or 0) for it in chg["items"])
        if ln:
            node["meta"]["edit_lines"] = ln
            if ln >= LARGE_EDIT_LINES:
                flags.append("big-change")
        ctx.changes.append({"id": r["id"], "t": t0, "tl": node["meta"]["tl"], **chg})
    # 三段耗时: agent / workflow 调用本身不计 (同步的会把子 agent 的模型时间误算成机器执行),
    # 它们的时间由子 agent 自己的区间承担。
    if t1 is not None and cat not in ("agent", "workflow"):
        ctx.intervals.append((t0, t1, "wait" if name in HUMAN_WAIT_TOOLS else "machine"))
    ctx.calls.append(node)
    keys: set = set()
    if cat == "agent":
        aid = str(extra.get("agentId") or "")
        how = "explicit"
        if not aid:
            aid, how = _find_agent_by_prompt(r, res, ctx)
        if aid:
            sub = _agent_node(aid, ctx, depth + 1, fallback_label=r["label"])
            if sub is not None:
                if how != "explicit":
                    sub["flags"].append(how)                  # 回链方式如实标注
                node["children"].append(sub)
                keys |= sub.pop("_keys", set())
        else:
            node["meta"]["unlinked"] = True
            if how == "ambiguous":
                flags.append("ambiguous")
    elif cat == "workflow" and extra.get("runId"):
        wf = _workflow_node(str(extra["runId"]), str(extra.get("workflowName") or r["label"]), ctx, depth + 1,
                            summary=extra.get("summary"), script_path=extra.get("scriptPath"),
                            transcript_dir=extra.get("transcriptDir"))
        node["children"].append(wf)
        keys |= wf.pop("_keys", set())
    node["_keys"] = keys
    return node


def _agent_node(agent_id: str, ctx: _Ctx, depth: int, fallback_label: str = "",
                meta: dict | None = None) -> dict | None:
    if agent_id in ctx.visited or depth > MAX_DEPTH:
        return None
    ctx.visited.add(agent_id)
    path = ctx.agents.get(agent_id)
    meta = meta if meta is not None else (agent_meta(path) if path else {})
    label = meta.get("description") or fallback_label or f"agent {agent_id[:8]}"
    info = {"type": meta.get("agentType"), "phase": meta.get("workflowPhase"), "depth": meta.get("spawnDepth")}
    if path is None:
        return {"id": f"agent:{agent_id}", "kind": "agent", "label": label, "agent_id": agent_id,
                "flags": ["missing"], "tokens": None, "t0": None, "t1": None, "children": [], "meta": info,
                "_keys": set()}
    ft = load_file(path)
    if ft is None:
        return None
    children, keys, stages = _timeline(list(ft.rows), ft, ctx, depth)   # 其中已按活动行连出「模型生成」区间
    for kind, d in ft.gloss.items():
        ctx.gloss[kind].update(d)
    info["instruction"] = short(ft.first_user, 400, flatten=False) if ft.first_user else ""
    return {"id": f"agent:{agent_id}", "kind": "agent", "label": label, "agent_id": agent_id,
            "t0": ft.first_ts, "t1": ft.last_ts, "children": children, "flags": [], "meta": info,
            "stages": stages, "tokens": usage_sum(keys, ctx), "_keys": keys}


def _workflow_node(run_id: str, name: str, ctx: _Ctx, depth: int, summary: str | None = None,
                   script_path: str | None = None, transcript_dir: str | None = None) -> dict:
    base = ctx.sdir.parent.parent
    rdir = ctx.sdir / "subagents" / "workflows" / run_id
    td = _inside(transcript_dir, base) if transcript_dir else None
    if td is not None and td.is_dir() and not rdir.is_dir():
        rdir = td                                            # 返回里记下的 transcript 目录 (真值)
    script = None
    sp = _inside(script_path, base) if script_path else None
    if sp is not None and sp.is_file():
        script = sp                                          # 返回里记下的脚本路径 (真值)
    if script is None:
        script = _workflow_script(ctx.sdir, run_id)
    ctx.script_loc[run_id] = (str(script) if script else None, str(rdir))
    smeta = {"name": None, "description": None, "phases": []}
    if script is not None:
        try:
            smeta = parse_script_meta(script.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    agents: list[dict] = []
    if rdir.is_dir():
        for p in sorted(rdir.glob("agent-*.jsonl")):
            aid = p.stem[len("agent-"):]
            ctx.agents.setdefault(aid, p)
            a = _agent_node(aid, ctx, depth + 1, meta=agent_meta(p))
            if a is not None:
                agents.append(a)
    order = [p["title"] for p in smeta["phases"]]            # phase 顺序以脚本为准 (真值), 其余按首次出现
    detail = {p["title"]: p.get("detail", "") for p in smeta["phases"]}
    groups: dict[str, list] = {}
    for a in sorted(agents, key=lambda a: a.get("t0") or 0):
        groups.setdefault((a.get("meta") or {}).get("phase") or "(未分阶段)", []).append(a)
    keys: set = set()
    phases = []
    for ph in [p for p in order if p in groups] + [p for p in groups if p not in order]:
        members = groups[ph]
        pk: set = set()
        missing = 0
        for a in members:
            pk |= a.pop("_keys", set())
            missing += "missing" in (a.get("flags") or [])
        keys |= pk
        t0s = [a["t0"] for a in members if a.get("t0")]
        t1s = [a["t1"] for a in members if a.get("t1")]
        phases.append({"id": f"wf:{run_id}:{ph}", "kind": "phase", "label": ph, "detail": detail.get(ph, ""),
                       "t0": min(t0s) if t0s else None, "t1": max(t1s) if t1s else None, "children": members,
                       "tokens": usage_sum(pk, ctx) if missing < len(members) else None,
                       "flags": ["partial"] if 0 < missing < len(members) else [],
                       "meta": {"agents": len(members)}})
    t0s = [p["t0"] for p in phases if p.get("t0")]
    t1s = [p["t1"] for p in phases if p.get("t1")]
    exists = rdir.is_dir()
    return {"id": f"wf:{run_id}", "kind": "workflow", "label": smeta.get("name") or name, "run_id": run_id,
            "detail": smeta.get("description") or summary or "", "t0": min(t0s) if t0s else None,
            "t1": max(t1s) if t1s else None, "children": phases,
            "tokens": usage_sum(keys, ctx) if exists else None,          # 目录都不在 -> 拿不到, 不是 0
            "flags": [] if exists else ["missing"],
            "meta": {"agents": len(agents), "phases": len(phases), "script": script.name if script else None},
            "_keys": keys}


def walk(nodes):
    for n in nodes:
        yield n
        if n.get("children"):
            yield from walk(n["children"])


def _is_activity_node(n: dict) -> bool:
    return n["kind"] != "event" and not (n["kind"] == "call" and "no-result" in (n.get("flags") or []))


def build_task(task: dict, now: float | None = None, running: bool = False,
               baseline: dict | None = None) -> dict:
    """把一个切好的任务构建成 {summary, tree, call_loc, text_loc, keys}。纯内存计算 (文件已在缓存里)。"""
    now = now or time.time()
    ft = load_file(task["main"])
    ctx = _Ctx(task["main"], now, running)
    children: list[dict] = []
    task_keys: set = set()
    main_stages: list[dict] = []
    for kind, d in (ft.gloss if ft else {}).items():
        ctx.gloss[kind].update(d)
    for seg in task["segments"]:
        rows = seg["rows"]
        nodes, keys, stages = _timeline(rows, ft, ctx, 0)
        task_keys |= keys
        if seg["kind"] == "continuation" and stages:
            stages[0]["after_bg"] = True                     # 后台完成后的延续: 流程条上画一道分隔
        main_stages.extend(stages)
        tss = [r["ts"] for r in rows if r.get("ts") is not None and r["k"] in ACT_KINDS and not r.get("synthetic")]
        if seg["kind"] == "continuation":
            children.append({"id": f"seg:{rows[0].get('i')}", "kind": "segment", "label": "后台完成后的延续",
                             "linked": seg.get("linked", False), "t0": min(tss) if tss else None,
                             "t1": max(tss) if tss else None, "children": nodes})
        else:
            children.extend(nodes)
    total = usage_sum(task_keys, ctx)
    calls_by_id = {c["id"]: c for c in ctx.calls}
    # 后台任务的结局落回启动它的调用上: 失败 -> bg-fail (计入失败); 被停止 -> bg-stopped (备注)
    for n in walk(children):
        if n["kind"] == "event" and n.get("ev") in ("notify-fail", "notify-stopped"):
            for tid in n.get("tids") or []:
                c = calls_by_id.get(tid)
                if c is not None:
                    f = "bg-fail" if n["ev"] == "notify-fail" else "bg-stopped"
                    if f not in c["flags"]:
                        c["flags"].append(f)
    for r in task.get("bg_notes") or []:
        for tid in r.get("tids") or []:
            c = calls_by_id.get(tid)
            if c is not None and "bg-stopped" not in c["flags"]:
                c["flags"].append("bg-stopped")
    if baseline:                                             # 慢 (统计推断): 历史基线, 故意等待不算, 样本不足不判
        for c in ctx.calls:
            b = baseline.get(c["name"])
            d = (c["t1"] - c["t0"]) if (c.get("t1") is not None and c.get("t0") is not None) else None
            if (b and b["n"] >= SLOW_MIN_SAMPLES and b["p90"] is not None and d is not None
                    and c["name"] not in HUMAN_WAIT_TOOLS and not c["meta"]["async"] and not c["meta"]["wait"]
                    and "running" not in c["flags"] and d > b["p90"] and d >= SLOW_MIN_SECONDS):
                c["flags"].append("slow")
                c["meta"]["p90"] = round(b["p90"], 2)
    split = split_time(ctx.intervals)
    nodes = list(walk(children))
    t0 = task["prompt"]["ts"]
    ts_all = [t for n in nodes if _is_activity_node(n) for t in (n.get("t0"), n.get("t1")) if t]
    if t0:
        ts_all.append(t0)
    t1 = max(ts_all) if ts_all else None
    if t0 and t1:
        split["span"] = t1 - t0                              # 跨度 = 任务真实起止; active 只算有活动的区间, 差值就是空闲
    cats: dict = defaultdict(int)
    flags: dict = defaultdict(int)
    for c in ctx.calls:
        cats[c["cat"]] += 1
        for f in c["flags"]:
            flags[f] += 1
    partial = sum(1 for n in nodes if n["kind"] in ("agent", "workflow") and "missing" in (n.get("flags") or [])) \
        + sum(1 for c in ctx.calls if c["meta"].get("unlinked"))
    projects = set()
    for cwd in ctx.cwds | {task["prompt"].get("cwd")}:
        wid = workspace_identity(cwd) if cwd else None
        if wid is not None:
            projects.add(wid.project)
    summary = {
        "id": task["id"], "session_id": task["session_id"],
        "prompt": short(task["prompt"]["text"], 400, flatten=False),
        "attachments": task["prompt"].get("att", 0), "t0": t0, "t1": t1, "running": running,
        "time": split, "tokens": total, "partial": partial, "calls": len(ctx.calls), "cats": dict(cats),
        "skills": sorted({n["label"] for n in nodes if n["kind"] == "skill"}),
        "workflows": [{"label": n["label"], "phases": n["meta"]["phases"], "agents": n["meta"]["agents"]}
                      for n in nodes if n["kind"] == "workflow"],
        "agents": sum(1 for n in nodes if n["kind"] == "agent"),
        "mcp_servers": sorted({c["meta"].get("server") for c in ctx.calls if c["cat"] == "mcp"} - {None}),
        "flags": dict(flags), "moments": moments(t0, nodes, ctx.calls),
        "continuations": sum(1 for s in task["segments"] if s["kind"] == "continuation"),
        "projects": sorted(projects),
        "stages": main_stages,
        "glossary": _used_glossary(ctx, nodes),
        "changes": build_ledger(ctx.changes, ctx.calls, ctx.cwds | {task["prompt"].get("cwd")}),
    }
    summary["risks"] = risk_markers(summary["changes"], ctx.calls, ctx.changes,
                                    ctx.cwds | {task["prompt"].get("cwd")}, ctx.err_text)
    for c in ctx.calls:
        c["meta"].pop("out_sig", None)                       # 只在判打转时用
    root = {"id": f"task:{task['id']}", "kind": "task", "label": short(task["prompt"]["text"], 120),
            "t0": t0, "t1": t1, "tokens": total, "children": children}
    return {"summary": summary, "tree": root, "call_loc": ctx.call_loc, "text_loc": ctx.text_loc,
            "script_loc": ctx.script_loc, "keys": task_keys, "errs": ctx.errs}


def _used_glossary(ctx: _Ctx, nodes: list[dict]) -> dict:
    """只下发这个任务里用到的名词说明 (skill / agent 类型 / MCP server), 原文。"""
    skills = {n["label"] for n in nodes if n["kind"] == "skill"}
    agents = {(n.get("meta") or {}).get("type") for n in nodes if n["kind"] == "agent"}
    agents |= {c.get("sub") for c in nodes if c["kind"] == "call" and c.get("cat") == "agent"}
    servers = {(c.get("meta") or {}).get("server") for c in nodes if c["kind"] == "call" and c.get("cat") == "mcp"}
    g = ctx.gloss
    return {"skill": {k: v for k, v in g["skill"].items() if skill_norm(k) in skills or k in skills},
            "agent": {k: v for k, v in g["agent"].items() if k in agents},
            "mcp": {k: v for k, v in g["mcp"].items() if k in servers}}


def moments(t0: float | None, nodes: list[dict], calls: list[dict]) -> list[dict]:
    """关键时刻 (确定性, 最多 8 条): 首次失败 / 后台失败 / 启动 workflow / 等你 / 打断与插话 / 上下文压缩 / 最后一次回复。"""
    out = []
    first_fail = next((c for c in sorted(calls, key=lambda c: c["t0"] or 0) if "fail" in c["flags"]), None)
    if first_fail:
        out.append({"t": first_fail["t0"], "kind": "fail",
                    "label": short(f"首次失败: {first_fail['name']} · {first_fail['label']}", 120),
                    "ref": first_fail["id"]})
    bg_fail = next((n for n in nodes if n["kind"] == "event" and n.get("ev") == "notify-fail"), None)
    if bg_fail:
        out.append({"t": bg_fail["t0"], "kind": "fail", "label": short(bg_fail["label"], 120), "ref": bg_fail["id"]})
    for c in calls:
        if c["cat"] == "workflow":
            lab = c["children"][0]["label"] if c.get("children") else c["label"]
            out.append({"t": c["t0"], "kind": "workflow", "label": short(f"启动 workflow: {lab}", 120), "ref": c["id"]})
        elif c["cat"] == "ask" and c.get("t1") and "running" not in c["flags"]:
            out.append({"t": c["t0"], "kind": "wait", "label": f"向你提问, 等了 {fmt_dur(c['t1'] - c['t0'])}",
                        "ref": c["id"]})
    seen_user = set()
    for n in nodes:
        if n["kind"] == "user":
            k = (n["label"], int(n["t0"] or 0))
            if k in seen_user:
                continue
            seen_user.add(k)
            out.append({"t": n["t0"], "kind": "user", "label": n["label"], "ref": n["id"]})
        elif n["kind"] == "event" and n.get("ev") == "compact":
            out.append({"t": n["t0"], "kind": "event", "label": "上下文压缩", "ref": n["id"]})
    says = [n for n in nodes if n["kind"] == "say" and n["id"].startswith("say:main:") and n.get("t0")]
    if says:
        last = max(says, key=lambda n: n["t0"])
        out.append({"t": last["t0"], "kind": "done", "label": "最后一次回复", "ref": last["id"]})
    out.sort(key=lambda m: m["t"] or 0)
    if len(out) > 8:                                     # 不堆信息: 保留失败 / 等你 / 收尾, 其余按时间补足
        keep = [m for m in out if m["kind"] in ("fail", "wait", "done")][:8]
        rest = [m for m in out if m not in keep]
        out = sorted(keep + rest[: 8 - len(keep)], key=lambda m: m["t"] or 0)
    for m in out:
        m["offset"] = (m["t"] - t0) if (m["t"] and t0) else None
    return out


# ================================================================ 公共 API (serve 层调用)

def main_files(base: Path) -> list[Path]:
    base = Path(base)
    if not base.exists():
        return []
    out = []
    for proj in base.iterdir():
        if proj.is_dir():
            out.extend(p for p in proj.glob("*.jsonl") if p.is_file())
    return out


LIST_FILES_CAP = 400                              # 列表行里最多带这么多个文件名 (按改动次数排); 多的在 files_more 里如实说
_TASK_INDEX: dict[str, Path] = {}                 # task id -> 主会话文件
_BASELINE = {"t": 0.0, "data": None}
_BUILT: dict = {}                                 # task id -> (time, built)  明细面板短缓存


def _project_of(task: dict, mp: Path) -> tuple[str, str]:
    wid = workspace_identity(task["prompt"].get("cwd"))
    if wid is not None:
        return wid.project, wid.subpath
    return friendly_project(mp.parent.name), ""


def _iter_built(base: Path, since: float | None, project: str | None, running_sessions: set):
    """窗口内的每个任务 -> (task, built, project, subpath)。列表与统计共用同一套筛选口径:
    时间按提问时刻; project 按「任务里任一响应生效时的项目」匹配 —— 与 /tokens 逐记录按 cwd 归项目的口径一致。"""
    now = time.time()
    bl = baseline(base, ttl=300)
    for mp in list(main_files(base)):
        try:
            if since is not None and mp.stat().st_mtime < since:
                continue                                   # 整个文件都比窗口旧
        except OSError:
            continue
        tasks, _ = session_tasks(mp)
        for t in tasks:
            _TASK_INDEX[t["id"]] = mp
            if since is not None and (t["prompt"]["ts"] or 0) < since:
                continue
            proj, subpath = _project_of(t, mp)
            running = bool(t.get("tail")) and t["session_id"] in running_sessions
            built = build_task(t, now=now, running=running, baseline=bl)
            if project and proj != project and project not in built["summary"]["projects"]:
                continue
            yield t, built, proj, subpath


def list_tasks(base: Path | None = None, since: float | None = None, project: str | None = None,
               running_sessions: set | None = None) -> list[dict]:
    """任务列表 (左栏): 每个任务一行轻量摘要。运行中置顶, 其余按时间倒序。"""
    base = Path(base or default_base())
    out = []
    for t, built, proj, subpath in _iter_built(base, since, project, running_sessions or set()):
        s = built["summary"]
        s.update(project=proj, subpath=subpath, branch=t["prompt"].get("branch"))
        L = s.get("changes")                                   # 左栏要能按文件反查: 带上文件名与次数 (压缩成两元组)
        s["chg"] = None if not L else {**L["totals"], "verified": bool(L["verify"]),
                                       "after_verify": L["after_verify"]["changes"]}
        s["files"] = [] if not L else [[f["path"], f["n"]] for f in L["files"][:LIST_FILES_CAP]]
        s["files_more"] = 0 if not L else max(0, len(L["files"]) - LIST_FILES_CAP)
        s["risks_n"] = len(s.get("risks") or [])
        out.append({k: s[k] for k in ("id", "session_id", "project", "projects", "subpath", "branch", "prompt",
                                       "t0", "t1", "running", "time", "tokens", "partial", "calls", "cats",
                                       "skills", "flags", "agents", "workflows", "mcp_servers", "continuations",
                                       "chg", "files", "files_more", "risks_n")})
    out.sort(key=lambda s: (not s["running"], -(s["t0"] or 0)))
    return out


# ================================================================ S3: 统计 (排行 / MCP 健康 / 跨任务对照)
#
# 完成判据: 排行里的每一个数字都能点开, 列出的明细条数与数字**完全相等**, 每一条都能跳回回放里的那一步。
# 做法: 统计时顺手建追溯索引 {键: [明细]}; 每个数字 = 某个键下 (可选按标记筛过的) 明细条数。
# 页面拿到的每一份统计带一个 stamp, 取明细时带上它 -> 用的是算出那些数字的同一份索引。

STATS_TTL = 30.0                                  # 同一窗口 + 项目 30 秒内复用
STAMP_KEEP = 4                                    # 最近几份统计的索引留着给明细用 (全部时间一份约 10MB)
STAMP_MAX_AGE = 900.0
_STATS: dict = {}                                 # (base, since 分钟桶, project, 是否带价) -> stamp
_STAMPS: dict = {}                                # stamp -> (t, 结果, 索引); dict 的插入序就是新旧序
_STATS_LOCK = threading.Lock()
_STAMP_SEQ = [0]
_ROLE_RX = re.compile(r"^\s*([^\s:：/]{1,24})\s*[:：](?!//)")          # review:auth -> review; 不吃 https://
FAIL_FLAGS = frozenset(("fail", "bg-fail"))
COUNT_FLAGS = ("retry", "loop", "slow", "huge")


def role_of(label, agent_type=None) -> tuple:
    """子 agent 的「角色」-> (角色, 来源)。标签冒号前的短前缀 (review:auth -> review; 约定, 所以是推断);
    没有前缀就用 agent 类型 (meta 里的真值); 都没有 -> (None, None)。
    workflow-subagent 是所有 workflow 子 agent 共用的类型, 不算角色。"""
    m = _ROLE_RX.match(str(label or ""))
    if m:
        return m.group(1).lower(), "prefix"
    if agent_type and agent_type != "workflow-subagent":
        return str(agent_type), "type"
    return None, None


def tool_key(n: dict) -> str:
    """排行里的「工具」: 内置工具按名字; MCP 按 server 合成一行 (单个工具在 MCP 健康里看)。"""
    if n.get("cat") == "mcp":
        return "mcp·" + str((n.get("meta") or {}).get("server") or "?")
    return str(n.get("name") or "?")


def _ref(dim: str, name) -> str:
    """追溯索引的键 = 维度 + 名字的哈希: 原始名字不进 URL; 重新统计后同一个对象的键不变。"""
    return f"{dim}:{hashlib.sha1(str(name).encode('utf-8', 'replace')).hexdigest()[:12]}"


def _call_dur(n: dict) -> float | None:
    """调用耗时 = 发出到返回 (后台启动只算启动那一下, 与回放一致); 还在跑 / 没结果 -> None。"""
    if n.get("t0") is None or n.get("t1") is None or "running" in (n.get("flags") or []):
        return None
    return max(0.0, n["t1"] - n["t0"])


def _span(n: dict) -> float | None:
    return max(0.0, n["t1"] - n["t0"]) if (n.get("t0") is not None and n.get("t1") is not None) else None


def _median(vals: list) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


def _bm_add(acc: dict, bm: dict | None) -> None:
    for m, v in (bm or {}).items():
        a = acc.setdefault(m, [0, 0, 0, 0, 0])
        for i in range(5):
            a[i] += v[i]


def _usage_of(n: dict) -> tuple:
    """结构节点的 token 真值 -> (合计, 按模型); 拿不到 -> (None, None)。"""
    tk = n.get("tokens")
    if not tk:
        return None, None
    return tk.get("total"), tk.get("by_model")


def compute_stats(built_iter, price=None) -> tuple[dict, dict]:
    """(task, built, project, subpath) 的迭代 -> (统计结果, 追溯索引)。纯内存, 不读盘。

    price: 可选的 by_model -> (美元, 含未知单价) 换算函数, 由 serve 层注入 (本模块不依赖计价)。
    索引里三种明细: call (一次调用) / occ (一个对象在某个任务里的一次出现) / task (一个任务)。"""
    def cost(bm):
        if price is None or not bm:
            return None, False
        try:
            return price(bm)
        except Exception:
            return None, False

    idx: dict = defaultdict(list)
    tasks: dict = {}
    tools: dict = {}
    mcp: dict = {}
    skill_occ: dict = {}                               # 名字 -> {task: 累加器}  (同一任务里的几段合成一次出现)
    tool_occ: dict = {}                                # 工具键 -> {task: 累加器}
    phase_occ: dict = defaultdict(list)
    role_occ: dict = defaultdict(list)
    role_src: dict = {}
    ov = {"tasks": 0, "calls": 0, "active": 0.0, "fail": 0, "partial": 0, "running": 0,
          "flags": {f: 0 for f in COUNT_FLAGS}}
    ov_tok, ov_bm = 0, {}
    chg = {"tasks": 0, "changes": 0, "add": 0, "del": 0, "inferred": 0, "unknown": 0, "outside": 0,
           "add_partial": False, "del_partial": False, "verified_clean": 0, "changed_after": 0, "never_verified": 0}
    chg_files: set = set()
    chg_outside: set = set()
    risk: dict = {}                                   # rule -> {"tasks", "calls", "hits", "level"}

    for _t, built, proj, _sub in built_iter:
        sm = built["summary"]
        tid = sm["id"]
        errs = built.get("errs") or {}
        tasks[tid] = {"prompt": short(sm.get("prompt"), 90), "t0": sm.get("t0"), "project": proj}
        ov["tasks"] += 1
        ov["active"] += (sm.get("time") or {}).get("active") or 0
        ov["partial"] += 1 if sm.get("partial") else 0
        ov["running"] += 1 if sm.get("running") else 0
        tk = sm.get("tokens") or {}
        ov_tok += tk.get("total") or 0
        _bm_add(ov_bm, tk.get("by_model"))
        call_refs: dict = {}                             # 本任务: 调用 id -> 明细
        skill_calls: list = []                           # [(skill 名, 子树调用 id 集合)]  调用明细建完再解析
        task_fail = 0
        stack = [(built["tree"], None, ())]              # (节点, 所在 workflow 名, 外层 skill 名)
        while stack:
            n, wf, sks = stack.pop()
            k = n["kind"]
            kid_wf, kid_sks = wf, sks
            if k == "call":
                key = tool_key(n)
                d = _call_dur(n)
                fl = [f for f in COUNT_FLAGS if f in (n.get("flags") or [])]
                failed = bool(FAIL_FLAGS & set(n.get("flags") or []))
                ref = {"kind": "call", "task": tid, "node": n["id"], "t0": n.get("t0"), "name": n["name"],
                       "cat": n.get("cat"), "label": short(n.get("label"), 120), "sub": short(n.get("sub"), 120),
                       "flags": fl, "failed": failed, "dur": d}
                call_refs[n["id"]] = ref
                ov["calls"] += 1
                ov["fail"] += failed
                task_fail += failed
                for f in fl:
                    ov["flags"][f] += 1
                idx["all"].append(ref)
                row = tools.get(key)
                if row is None:
                    row = tools[key] = {"name": key, "cat": n.get("cat"), "calls": 0, "tasks": set(), "durs": [],
                                        "time": 0.0, "fail": 0, "result_est": 0, "result_unknown": 0,
                                        "nested": n.get("cat") in ("agent", "workflow"),
                                        "ref": _ref("tool", key), "ref_occ": _ref("tool-occ", key),
                                        **{f: 0 for f in COUNT_FLAGS}}
                row["calls"] += 1
                row["tasks"].add(tid)
                row["fail"] += failed
                for f in fl:
                    row[f] += 1
                if d is not None:
                    row["durs"].append(d)
                    row["time"] += d
                if n.get("result_est") is None:
                    row["result_unknown"] += 1
                else:
                    row["result_est"] += n["result_est"]
                idx[row["ref"]].append(ref)
                acc = tool_occ.setdefault(key, {}).get(tid)
                if acc is None:
                    acc = tool_occ[key][tid] = {"kind": "occ", "task": tid, "node": n["id"], "t0": n.get("t0"),
                                                "dur": None, "tokens": None, "calls": 0, "fail": 0}
                    idx[row["ref_occ"]].append(acc)
                acc["calls"] += 1
                acc["fail"] += failed
                if d is not None:
                    acc["dur"] = (acc["dur"] or 0.0) + d
                if n.get("t0") is not None and (acc["t0"] is None or n["t0"] < acc["t0"]):
                    acc["t0"], acc["node"] = n["t0"], n["id"]       # 跳转落在这个任务里第一次用它的地方
                if n.get("cat") == "mcp":
                    srv = str((n.get("meta") or {}).get("server") or "?")
                    tname = str(n["name"]).split("__", 2)[-1]
                    m = mcp.get(srv)
                    if m is None:
                        m = mcp[srv] = {"server": srv, "calls": 0, "tasks": set(), "durs": [], "fail": 0,
                                        "tools": {}, "errors": [], "ref": _ref("mcp", srv),
                                        "ref_occ": row["ref_occ"]}
                    m["calls"] += 1
                    m["tasks"].add(tid)
                    m["fail"] += failed
                    if d is not None:
                        m["durs"].append(d)
                    tt = m["tools"].get(tname)
                    if tt is None:
                        tt = m["tools"][tname] = {"tool": tname, "calls": 0, "fail": 0, "durs": [],
                                                  "ref": _ref("mcptool", f"{srv}\x00{tname}")}
                    tt["calls"] += 1
                    tt["fail"] += failed
                    if d is not None:
                        tt["durs"].append(d)
                    idx[m["ref"]].append(ref)
                    idx[tt["ref"]].append(ref)
                    if failed:
                        m["errors"].append({"task": tid, "node": n["id"], "t0": n.get("t0"), "tool": tname,
                                            "excerpt": short(errs.get(n["id"]), 200)})
            elif k == "skill":
                ids = [c["id"] for c in walk(n.get("children") or []) if c["kind"] == "call"]
                skill_calls.append((n["label"], ids))
                acc = skill_occ.setdefault(n["label"], {}).get(tid)
                if acc is None:
                    acc = skill_occ[n["label"]][tid] = {"kind": "occ", "task": tid, "node": n["id"], "t0": n.get("t0"),
                                                        "dur": None, "tokens": None, "bm": {}, "untagged": False,
                                                        "calls": 0, "fail": 0, "_seen": set()}
                if n["label"] not in sks:                    # 外层没有同名 skill 时才累加时长 / token (防嵌套重复)
                    tot, bm = _usage_of(n)
                    if tot is None:                          # 没有归属标记: 它管到哪不知道 -> 时长和 token 都拿不到
                        acc["untagged"] = True
                    else:
                        acc["tokens"] = (acc["tokens"] or 0) + tot
                        _bm_add(acc["bm"], bm)
                        sp = _span(n)
                        if sp is not None:
                            acc["dur"] = (acc["dur"] or 0.0) + sp
                if n.get("t0") is not None and (acc["t0"] is None or n["t0"] < acc["t0"]):
                    acc["t0"], acc["node"] = n["t0"], n["id"]
                kid_sks = sks + (n["label"],)
            elif k == "workflow":
                kid_wf = n.get("label")
            elif k in ("phase", "agent"):
                calls = [c for c in walk(n.get("children") or []) if c["kind"] == "call"]
                tot, bm = _usage_of(n)
                o = {"kind": "occ", "task": tid, "node": n["id"], "t0": n.get("t0"), "dur": _span(n),
                     "tokens": tot, "bm": bm, "calls": len(calls),
                     "fail": sum(1 for c in calls if FAIL_FLAGS & set(c.get("flags") or []))}
                if k == "phase":
                    o["sub"] = short(wf, 80) if wf else ""
                    phase_occ[n["label"]].append(o)
                else:
                    role, src = role_of(n.get("label"), (n.get("meta") or {}).get("type"))
                    if role:
                        o["sub"] = short(n.get("label"), 100)
                        role_occ[role].append(o)
                        role_src.setdefault(role, src)
            for c in reversed(n.get("children") or []):
                stack.append((c, kid_wf, kid_sks))
        for name, ids in skill_calls:                    # skill 里的调用: 同一任务里按调用 id 去重
            acc = skill_occ[name][tid]
            for cid in ids:
                r = call_refs.get(cid)
                if r is not None and cid not in acc["_seen"]:
                    acc["_seen"].add(cid)
                    idx[_ref("skill-call", name)].append(r)
                    acc["calls"] += 1
                    acc["fail"] += r["failed"]
        L = sm.get("changes")                            # 改动清单: 每个数字同样挂进追溯索引
        if L:
            t_ref = {"kind": "task", "task": tid, "node": f"task:{tid}", "t0": sm.get("t0"),
                     "dur": (sm.get("time") or {}).get("active"), "tokens": tk.get("total"),
                     "calls": sm.get("calls") or 0, "fail": task_fail,
                     "sub": (f"{L['totals']['files']} 个文件 · {L['totals']['changes']} 次改动" if L["totals"]["files"]
                             else f"{L['totals']['unknown']} 次改动认不出文件")}
            chg["tasks"] += 1
            idx["chg-tasks"].append(t_ref)
            if not L["verify"]:
                chg["never_verified"] += 1
                idx["chg-noverify"].append(t_ref)
            elif L["after_verify"]["changes"]:
                chg["changed_after"] += 1
                idx["chg-after"].append(t_ref)
            else:
                chg["verified_clean"] += 1
            for k in ("add", "del", "unknown"):
                chg[k] += L["totals"][k]
            chg["add_partial"] = chg["add_partial"] or L["totals"]["add_partial"]
            chg["del_partial"] = chg["del_partial"] or L["totals"]["del_partial"]
            chg_files |= {(proj, f["path"]) for f in L["files"]}          # 跨项目的同名文件不合并
            chg_outside |= {(proj, f["path"]) for f in L["files"] if f["outside"]}
            seen_chg: set = set()
            for f in L["files"]:
                for cid in f["calls"]:
                    r = call_refs.get(cid)
                    if r is None or cid in seen_chg:
                        continue
                    seen_chg.add(cid)
                    idx["chg-calls"].append(r)
                    if r["name"] in ("Bash", "PowerShell"):
                        chg["inferred"] += 1
                        idx["chg-inferred"].append(r)
            for u in L["unknown_calls"]:
                r = call_refs.get(u["node"])
                if r is not None:
                    idx["chg-unknown"].append(r)
                    if u["node"] not in seen_chg:
                        seen_chg.add(u["node"])
                        idx["chg-calls"].append(r)
                        idx["chg-inferred"].append(r)      # 认不出文件的也是 Bash 认出来的改动, 一样要能点开
                        chg["inferred"] += 1
        for rule in {x["rule"] for x in (sm.get("risks") or [])}:
            marks = [x for x in sm["risks"] if x["rule"] == rule]
            row = risk.setdefault(rule, {"rule": rule, "tasks": 0, "calls": 0, "hits": 0,
                                         "level": marks[0]["level"], "why": marks[0]["why"],
                                         "ref_tasks": _ref("risk-t", rule), "ref_calls": _ref("risk-c", rule)})
            row["tasks"] += 1
            row["hits"] += len(marks)
            idx[row["ref_tasks"]].append({"kind": "task", "task": tid, "node": f"task:{tid}", "t0": sm.get("t0"),
                                          "dur": (sm.get("time") or {}).get("active"), "tokens": tk.get("total"),
                                          "calls": sm.get("calls") or 0, "fail": 0,
                                          "sub": "、".join(short(m["label"], 40) for m in marks[:3])})
            seen_r: set = set()
            for m in marks:
                for rf in m.get("refs") or []:
                    r2 = call_refs.get(rf.get("id")) if rf.get("kind") == "call" else None
                    if r2 is not None and rf["id"] not in seen_r:
                        seen_r.add(rf["id"])
                        idx[row["ref_calls"]].append(r2)
                        row["calls"] += 1
        idx["tasks"].append({"kind": "task", "task": tid, "node": f"task:{tid}", "t0": sm.get("t0"),
                             "dur": (sm.get("time") or {}).get("active"), "tokens": tk.get("total"),
                             "calls": len(call_refs), "fail": task_fail,
                             "flags": (["partial"] if sm.get("partial") else []) + (["running"] if sm.get("running") else [])})

    # ---- 收尾: 排行行 + 分布
    tool_rows = []
    for r in tools.values():
        durs = r.pop("durs")
        r.update(tasks=len(r["tasks"]), timed=len(durs), p50=_median(durs), p90=p90(durs))
        tool_rows.append(r)
    tool_rows.sort(key=lambda r: (-r["time"], -r["calls"]))

    skill_rows = []
    for name, per in skill_occ.items():
        occs = []
        bm_all: dict = {}
        for acc in per.values():
            acc.pop("_seen", None)
            _bm_add(bm_all, acc["bm"])
            acc["cost"], acc["unpriced"] = cost(acc.pop("bm"))
            occs.append(acc)
        ref_occ = _ref("skill-occ", name)
        idx[ref_occ] = occs
        c, unp = cost(bm_all)
        skill_rows.append({"name": name, "tasks": len(occs), "calls": sum(o["calls"] for o in occs),
                           "fail": sum(o["fail"] for o in occs),
                           "tokens": sum(o["tokens"] or 0 for o in occs), "cost": c, "unpriced": unp,
                           "untagged": sum(1 for o in occs if o["untagged"]),
                           "time": sum(o["dur"] or 0 for o in occs),
                           "ref_occ": ref_occ, "ref_calls": _ref("skill-call", name)})
    skill_rows.sort(key=lambda r: (-r["time"], -r["tasks"]))

    mcp_rows = []
    for m in mcp.values():
        durs = m.pop("durs")
        m.update(tasks=len(m["tasks"]), p50=_median(durs), p90=p90(durs))
        tl = []
        for tt in m["tools"].values():
            tt["p50"] = _median(tt.pop("durs"))
            tl.append(tt)
        m["tools"] = sorted(tl, key=lambda x: (-x["fail"], -x["calls"]))
        m["errors"] = sorted(m["errors"], key=lambda e: -(e["t0"] or 0))[:5]
        mcp_rows.append(m)
    mcp_rows.sort(key=lambda m: (-m["fail"], -m["calls"]))

    def dist(dim: str, groups: dict, src: dict | None = None) -> list:
        rows = []
        for name, occs in groups.items():
            for o in occs:
                if "bm" in o:
                    o["cost"], o["unpriced"] = cost(o.pop("bm"))
            ref = _ref("cmp-" + dim, name)
            idx[ref] = occs
            durs = [o for o in occs if o.get("dur") is not None]
            toks = [o for o in occs if o.get("tokens") is not None]
            rows.append({"name": name, "n": len(occs), "tasks": len({o["task"] for o in occs}),
                         "fail_occ": sum(1 for o in occs if o.get("fail")),
                         "dur_median": _median([o["dur"] for o in durs]),
                         "tok_median": _median([o["tokens"] for o in toks]),
                         "calls_median": _median([o["calls"] for o in occs]),
                         "slowest": max(durs, key=lambda o: o["dur"]) if durs else None,
                         "priciest": max(toks, key=lambda o: o["tokens"]) if toks else None,
                         "src": (src or {}).get(name), "ref": ref, "points": occs})
        rows.sort(key=lambda r: (-r["tasks"], -r["n"], str(r["name"])))
        return rows

    compare = {"skill": dist("skill", {k: list(v.values()) for k, v in skill_occ.items()}),
               "phase": dist("phase", phase_occ),
               "role": dist("role", role_occ, role_src),
               "tool": dist("tool", {k: list(v.values()) for k, v in tool_occ.items()})}
    c, unp = cost(ov_bm)
    ov.update(tokens=ov_tok, cost=c, unpriced=unp)
    chg["files"] = len(chg_files)
    chg["outside"] = len(chg_outside)                    # 与「个文件被改过」同一个去重口径
    chg["changes"] = len(idx["chg-calls"])               # 以「几次调用改了文件」为准 (一次调用改多个文件只算一次)
    risk_rows = sorted(risk.values(), key=lambda r: (-r["tasks"], -r["hits"]))
    return {"overview": ov, "tasks": tasks, "tools": tool_rows, "skills": skill_rows, "mcp": mcp_rows,
            "compare": compare, "changes": chg, "risks": risk_rows}, dict(idx)


def _new_stamp() -> str:
    _STAMP_SEQ[0] += 1
    return f"{int(time.time() * 1000):x}{_STAMP_SEQ[0]:03d}"


def stats(base: Path | None = None, since: float | None = None, project: str | None = None,
          running_sessions: set | None = None, price=None, fresh: bool = False) -> dict:
    """统计子页的数据 (带 stamp)。同一窗口 + 项目 STATS_TTL 秒内复用; fresh=True 跳过缓存重算。"""
    base = Path(base or default_base())
    key = (str(base), None if since is None else int(since // 60), project or "", price is not None)
    now = time.time()
    with _STATS_LOCK:
        st = _STATS.get(key)
        hit = _STAMPS.get(st) if st else None
        if hit and not fresh and now - hit[0] < STATS_TTL:
            return hit[1]
    res, idx = compute_stats(_iter_built(base, since, project, running_sessions or set()), price=price)
    with _STATS_LOCK:
        stamp = _new_stamp()
        res["stamp"] = stamp
        res["computed_at"] = time.time()
        _STAMPS[stamp] = (res["computed_at"], res, idx)
        _STATS[key] = stamp
        for old in [k for k, v in _STAMPS.items() if res["computed_at"] - v[0] > STAMP_MAX_AGE]:
            _STAMPS.pop(old, None)
        while len(_STAMPS) > STAMP_KEEP:
            _STAMPS.pop(next(iter(_STAMPS)))
        for k in [k for k, v in _STATS.items() if v not in _STAMPS]:
            _STATS.pop(k, None)                      # 窗口 -> stamp 的登记跟着清 (「近 7 天」每分钟换一个桶, 不能无限长)
    return res


def _drill_match(x: dict, flag: str) -> bool:
    if flag == "fail":
        return bool(x.get("failed")) if x["kind"] == "call" else bool(x.get("fail"))
    return flag in (x.get("flags") or [])


def stats_refs(stamp: str, ref: str, flag: str | None = None, sort: str = "time",
               offset: int = 0, limit: int = 100) -> dict | None:
    """某个统计数字背后的明细 (追溯抽屉)。stamp 已过期 -> None (调用方重新统计后再取)。
    flag: fail / retry / loop / slow / huge / partial / running; sort: time / dur / tokens / calls; 分页。"""
    with _STATS_LOCK:
        hit = _STAMPS.get(stamp)
    if hit is None:
        return None
    res, idx = hit[1], hit[2]
    items = list(idx.get(ref) or [])
    if flag:
        items = [x for x in items if _drill_match(x, flag)]
    if sort in ("dur", "tokens", "calls"):
        items.sort(key=lambda x: (x.get(sort) is None, -(x.get(sort) or 0), -(x.get("t0") or 0)))
    else:
        items.sort(key=lambda x: -(x.get("t0") or 0))
    offset = max(0, int(offset or 0))
    limit = max(1, min(500, int(limit or 100)))
    page = items[offset:offset + limit]
    return {"stamp": stamp, "total": len(items), "offset": offset, "items": page,
            "tasks": {x["task"]: res["tasks"].get(x["task"]) for x in page}}


def get_task(task_id: str, base: Path | None = None, running_sessions: set | None = None) -> dict | None:
    base = Path(base or default_base())
    mp = _TASK_INDEX.get(task_id)
    if mp is None:
        list_tasks(base)                                   # 冷启动: 建一次索引
        mp = _TASK_INDEX.get(task_id)
        if mp is None:
            return None
    tasks, _ = session_tasks(mp)
    for t in tasks:
        if t["id"] != task_id:
            continue
        running = bool(t.get("tail")) and t["session_id"] in (running_sessions or set())
        built = build_task(t, running=running, baseline=baseline(base, ttl=300))
        proj, subpath = _project_of(t, mp)
        built["summary"].update(project=proj, subpath=subpath, branch=t["prompt"].get("branch"),
                                prompt_full=t["prompt"]["text"])
        _BUILT[task_id] = (time.time(), built)
        return built
    return None


def _built(task_id: str, base: Path | None) -> dict | None:
    """明细接口用的构建结果: 优先 10s 内的缓存, 过期就重建 (避免对活任务拿旧偏移)。"""
    hit = _BUILT.get(task_id)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    return get_task(task_id, base)


def get_call_detail(task_id: str, call_id: str, base: Path | None = None) -> dict | None:
    """一个调用的完整输入 / 输出原文 (脱敏在 serve 层做)。"""
    built = _built(task_id, base)
    if not built:
        return None
    loc = built["call_loc"].get(call_id)
    if not loc:
        return None
    path, coff, roff = loc
    out = {"name": None, "input": None, "output": None, "is_error": None, "extra": None}
    if coff is not None:
        o = read_line_at(Path(path), coff) or {}
        for b in (o.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == call_id:
                out["input"], out["name"] = b.get("input"), b.get("name")
    if roff is not None:
        o = read_line_at(Path(path), roff) or {}
        for b in (o.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == call_id:
                out["output"] = text_of(b.get("content"))
                out["is_error"] = bool(b.get("is_error"))
        tur = o.get("toolUseResult")
        if isinstance(tur, dict):
            out["extra"] = {k: tur[k] for k in ("stderr", "interrupted", "returnCodeInterpretation",
                                                 "persistedOutputPath", "gitOperation") if k in tur}
    return out


def get_text_detail(task_id: str, node_id: str, base: Path | None = None) -> dict | None:
    """Claude 说的话 / 思考片段的**全文** (保留换行; 脱敏在 serve 层做)。"""
    built = _built(task_id, base)
    if not built:
        return None
    loc = built["text_loc"].get(node_id)
    if not loc:
        return None
    path, off, bi, kind = loc
    if off is None:
        return None
    o = read_line_at(Path(path), off) or {}
    content = (o.get("message") or {}).get("content") or []
    if isinstance(content, list) and isinstance(bi, int) and 0 <= bi < len(content):
        b = content[bi]
        if isinstance(b, dict):
            return {"kind": kind, "text": str(b.get("thinking" if kind == "think" else "text") or "")}
    return None


def get_script(task_id: str, run_id: str, base: Path | None = None) -> dict | None:
    """workflow 的编排脚本 + 它和回放的对应关系 (阶段 -> phase 所在行, agent -> 启动它的 agent() 行)。
    脚本位置取构建时登记的 (优先 Workflow 返回里的 scriptPath 真值), 不按请求参数拼路径。脱敏在 serve 层做。"""
    built = _built(task_id, base)
    if not built:
        return None
    loc = (built.get("script_loc") or {}).get(run_id)
    if not loc or not loc[0]:
        return None
    try:
        src = Path(loc[0]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    smap = script_map(src)
    agents = {}
    rdir = Path(loc[1])
    if rdir.is_dir():
        for p in sorted(rdir.glob("agent-*.jsonl")):
            agents[p.stem[len("agent-"):]] = match_agent_line(agent_meta(p).get("description"), smap["agents"])
    return {"name": Path(loc[0]).name, "text": src, "lines": smap["lines"], "phases": smap["phases"],
            "phase_how": smap["phase_how"], "agents": agents,
            "agent_calls": [{"line": a["label_line"], "label": a["label"]} for a in smap["agents"]]}


def baseline(base: Path | None = None, ttl: float = 300) -> dict:
    """每个工具的历史耗时基线 {tool: {"n", "p90"}} —— 全部文件、全部历史; 故意等待与后台启动不算; TTL 内复用。"""
    now = time.time()
    if _BASELINE["data"] is not None and now - _BASELINE["t"] < ttl:
        return _BASELINE["data"]
    base = Path(base or default_base())
    durs: dict = defaultdict(list)
    if base.exists():
        for p in list(base.rglob("*.jsonl")):
            if p.name == "journal.jsonl":
                continue
            ft = load_file(p)
            if ft is None:
                continue
            open_calls = {}
            for r in list(ft.rows):
                if r["k"] == "call":
                    open_calls[r["id"]] = r
                elif r["k"] == "result" and r.get("tid") in open_calls:
                    c = open_calls.pop(r["tid"])
                    ex = r.get("extra") or {}
                    if c.get("bg") or c.get("wait") or ex.get("isAsync") or ex.get("status") == "async_launched":
                        continue
                    if c["ts"] is not None and r["ts"] is not None:
                        durs[c["name"]].append(max(0.0, r["ts"] - c["ts"]))
    data = {name: {"n": len(v), "p90": p90(v)} for name, v in durs.items()}
    _BASELINE.update(t=now, data=data)
    return data


def warm(base: Path | None = None) -> None:
    """后台预热: 把全部 transcript 读进缓存 + 算好耗时基线 (冷启动约 6-10s, 之后增量)。"""
    try:
        baseline(base, ttl=0)
    except Exception:           # 预热失败不影响服务 (原则 5)
        pass


def start_warmer(base: Path | None = None) -> None:
    threading.Thread(target=warm, args=(base,), name="mc-trace-warm", daemon=True).start()
