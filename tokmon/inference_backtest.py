"""推断准确率回测 · L2 (M4.5)。时间旅行: 用 transcript 自己的"未来"当 oracle, 量 `classify_state` 准不准。

零人工标注: 在过去某截断点 i, 看活分类器当时会判什么 (P), 再从下一条消息 (successor) 推真值 (TRUTH), 对账。

**三个子代 = 三把尺 (oracle 的严/宽), 供你分别评测**:
  - strict  : 只在水落石出时判真值; 灰区(successor 太远/中等空档)一律计「不可判」。报出来的准确率最铁硬, 覆盖最低。
  - medium  : 居中。
  - lenient : 尽量判; 灰区按良性解释标注。覆盖最高, 但假设最多。

**狗粮纪律 (原则 1)**: 判不了真值的样本单独计「不可判」, **绝不**掺进准确率分母。一把没校准的尺比没有尺更危险。

读取纪律 (与 L1 的关键区别): L2 需要更全的历史 (successor 当 oracle), 故走**独立的窗口读取路径** (近 K 天, 内存有界),
不复用 activity._read_tail。位于支柱之上, 只只读 import activity (跑 classify_state); 任何支柱都不 import 本模块 (P4)。

纯函数 (_truth / _pred_label / _successor_kind / score) 便于单测, 用"构造时就知道答案"的合成消息钉死。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import activity
from .discovery import discover
from .util import term_text

_EPS = 1.0                 # 评估时刻取 successor 到达前最后一刻
_STUCK = None              # 运行时从 cfg 取
_INF = float("inf")

# 真值标签
TRUTHS = ("working", "awaiting", "idle", "long_task", "ended", "unjudgeable")
# 预测标签 (classify_state 状态 -> 可对比)
PREDS = ("working", "awaiting", "idle", "ambiguous", "unknown")


@dataclass(frozen=True)
class Oracle:
    name: str
    flight_max: float          # 续接/assistant successor 的空档超过它 -> 太远, 不可判
    no_succ_gray_ended: bool    # 末条消息、中等年龄、无 successor: True=判已结束, False=不可判
    desc: str


ORACLES = (
    Oracle("strict", 1800.0, False, "灰区一律不可判; 准确率最铁硬, 覆盖最低"),
    Oracle("medium", 7200.0, False, "居中"),
    Oracle("lenient", _INF, True, "尽量判; 覆盖最高, 假设最多"),
)
_ENDED_AGE = 6 * 3600       # 末条消息超过它没动静 -> 一律判已结束


@dataclass
class BacktestReport:
    base: str
    oracle: str
    days: int
    sessions: int = 0
    points: int = 0                 # 总评估点 (已排除回合内中途行)
    judgeable: int = 0
    unjudgeable: int = 0
    intra_skipped: int = 0          # 跳过的回合内中途行 (活分类器的读取路径结构上看不到)
    matrix: dict = field(default_factory=dict)   # (pred, truth) -> count, 仅 judgeable
    over_optimism: int = 0          # 危险格: 说在干活, 实则等待/已结束
    false_completion: int = 0       # 危险格: 说完事了, 实则还在跑
    correct: int = 0


def _lead_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return (b.get("text") or "").strip()
    return ""


def _successor_kind(succ: dict) -> str:
    """successor 是什么 -> 反推 cur 当时在干嘛。"""
    if succ.get("type") == "assistant":
        return "assistant"            # 助手继续产出 -> cur 当时在飞
    content = (succ.get("message") or {}).get("content")
    lead = _lead_text(content)
    if lead.startswith(activity._INTERRUPT):
        return "interrupt"            # 用户打断了一次活动 -> cur 当时在飞
    if lead.startswith("<task-notification>"):
        return "background"           # 后台子任务的生命周期 ping, 时序与前台回合解耦 -> 见证不了 cur, 不可判 (L2 review 修)
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return "continuation"         # 工具结果回来 -> cur 当时在飞
    return "human_prompt"             # 人类新提问 -> cur 当时在等用户


def _truth(succ, gap, file_age, idle_thr, stuck_s, oracle: Oracle) -> str:
    """纯函数: 从 successor 推真值。succ=None 表示 cur 是文件末条。"""
    if succ is None:
        if file_age < stuck_s:
            return "unjudgeable"      # 很新 -> 此刻真的可能在飞, 判不了
        if file_age >= _ENDED_AGE:
            return "ended"            # 老 -> 已结束/被弃
        return "ended" if oracle.no_succ_gray_ended else "unjudgeable"   # 中等年龄: 灰区
    kind = _successor_kind(succ)
    if kind == "background":
        return "unjudgeable"         # 后台 task-notification 的时序与前台回合解耦, 无法见证 cur 的状态
    if kind == "human_prompt":
        return "idle" if gap > idle_thr else "awaiting"     # 等用户; 空档大 -> idle (awaiting 稳定, 不受 flight_max 限)
    if kind == "interrupt":
        return "working"             # 用户停掉一次在跑的活
    # continuation / assistant successor = cur 当时在飞
    if gap > oracle.flight_max:
        return "unjudgeable"         # successor 太远, 不能当"连续在飞"的证据 (会话可能曾休眠后恢复)
    if gap > stuck_s:
        return "long_task"           # 大空档但在 flight_max 内 -> 长任务在飞
    return "working"


def _is_intra_turn(cur: dict, succ: dict) -> bool:
    """cur 是回合内中途行吗 (后接同一 message.id 的 assistant 续接片段)。活分类器 _last_message 结构上看不到这种行。"""
    if cur.get("type") != "assistant" or succ.get("type") != "assistant":
        return False
    cid = (cur.get("message") or {}).get("id")
    return cid is not None and cid == (succ.get("message") or {}).get("id")


def _pred_label(st: dict) -> str:
    s = st.get("state")
    if s in ("WORKING", "PROCESSING"):
        return "working"
    if s == "AWAITING_USER":
        return "idle" if st.get("idle") else "awaiting"
    if s == "AMBIGUOUS_PENDING":
        return "ambiguous"
    return "unknown"


def _is_correct(pred: str, truth: str) -> bool:
    if pred == "ambiguous":
        return truth in ("long_task", "ended")     # 对冲: 落在它并列的可能里就算对, 不苛责没说死
    return pred == truth


def _danger(pred: str, truth: str):
    if pred == "working" and truth in ("awaiting", "idle", "ended"):
        return "over"          # 过度乐观: 说在干活, 实则等待/已结束 (最伤——让你以为在推进)
    if pred in ("awaiting", "idle") and truth == "working":
        return "false_done"    # 假完成: 说完事了, 实则还在跑 -> 会触发错误"任务完成"通知
    return None


def _read_messages(path: Path, days: int, now: float, max_bytes: int = 8_000_000):
    """独立窗口读取: 近 days 天的有序 (ts, obj) 消息列表 (只 assistant|user)。内存有界 (只读尾部 max_bytes)。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    cutoff = now - days * 86400
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                data = f.read()
                data = data[data.find(b"\n") + 1:]
            else:
                data = f.read()
    except OSError:
        return []
    msgs = []
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        if o.get("type") not in ("assistant", "user"):
            continue
        ts = activity._parse_ts(o.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        msgs.append((ts, o))
    msgs.sort(key=lambda x: x[0])
    return msgs


def backtest(base: Path, oracle: Oracle, days: int = 7, per_session: int = 60,
             cfg: "activity.ActivityConfig | None" = None) -> BacktestReport:
    cfg = cfg or activity.ActivityConfig()
    rep = BacktestReport(base=str(base), oracle=oracle.name, days=days)
    now = time.time()
    matrix: Counter = Counter()
    for path, _raw, kind in discover(base):
        if kind != "main":
            continue
        msgs = _read_messages(path, days, now)
        if len(msgs) < 2:
            continue
        rep.sessions += 1
        m = len(msgs)
        # 截断点: 抽样以控成本 (含末条的无-successor 点)
        if m <= per_session:
            idxs = range(m)
        else:
            step = m / per_session
            idxs = sorted({int(k * step) for k in range(per_session)} | {m - 1})
        for i in idxs:
            cur = msgs[i][1]
            if i + 1 < m:
                succ = msgs[i + 1][1]
                # 跳过回合内中途行: 同一 message.id 的连续 assistant 行 (thinking/text/tool_use 被拆成多行)。
                # 活分类器的 _last_message 只会落在回合最后一行, 结构上永远看不到中途行 —— 在这些点评估
                # 等于测一个生产环境读不到的输入, 会虚高"假完成"危险格。镜像 _last_message: 只在回合末行判。
                if _is_intra_turn(cur, succ):
                    rep.intra_skipped += 1
                    continue
                gap = msgs[i + 1][0] - msgs[i][0]
                now_eval = msgs[i + 1][0] - _EPS
                file_age = 0.0
            else:
                succ = None
                gap = 0.0
                now_eval = now
                file_age = now - msgs[i][0]
            truth = _truth(succ, gap, file_age, cfg.idle_after_s, cfg.stuck_after_s, oracle)
            rep.points += 1
            if truth == "unjudgeable":
                rep.unjudgeable += 1
                continue                     # 狗粮: 不进分母
            pred = _pred_label(activity.classify_state(cur, now_eval, cfg))
            rep.judgeable += 1
            matrix[(pred, truth)] += 1
            if _is_correct(pred, truth):
                rep.correct += 1
            d = _danger(pred, truth)
            if d == "over":
                rep.over_optimism += 1
            elif d == "false_done":
                rep.false_completion += 1
    rep.matrix = {f"{p}|{t}": c for (p, t), c in matrix.items()}
    return rep


# ---- 渲染 (纯函数) ----

def build_backtest_lines(rep: BacktestReport) -> list[str]:
    out = []
    acc = 100.0 * rep.correct / rep.judgeable if rep.judgeable else 0.0
    cov = 100.0 * rep.judgeable / rep.points if rep.points else 0.0
    out.append(f"oracle={rep.oracle}   会话 {rep.sessions}   评估点 {rep.points}   近 {rep.days} 天")
    out.append(f"  准确率 {acc:.1f}%  (judgeable {rep.judgeable})   覆盖 {cov:.0f}%   "
               f"不可判 {rep.unjudgeable} (不进分母)   回合内中途行已跳过 {rep.intra_skipped}")
    di = "✓" if (rep.over_optimism + rep.false_completion) == 0 else "⚠"
    out.append(f"  {di} 危险格: 过度乐观(说在干活实则等待/已结束) {rep.over_optimism}   "
               f"假完成(说完了实则在跑) {rep.false_completion}")
    # 混淆矩阵
    mat: dict = {}
    for k, c in rep.matrix.items():
        p, t = k.split("|", 1)
        mat[(p, t)] = c
    truths = [t for t in TRUTHS if t != "unjudgeable" and any(pt[1] == t for pt in mat)]
    if mat:
        out.append("  混淆矩阵 (行=预测, 列=真值; 对角线=对):")
        head = "    pred\\truth   " + "".join(f"{t[:8]:>9}" for t in truths)
        out.append(head)
        for p in PREDS:
            if not any(pt[0] == p for pt in mat):
                continue
            row = "".join(f"{mat.get((p, t), 0):>9}" for t in truths)
            out.append(f"    {p:11}{row}")
    return out


def run_backtests(base: Path, days: int = 7) -> None:
    try:
        from rich.console import Console
        console = Console()
        emit = lambda ln="": console.print(term_text(str(ln)))   # noqa: E731 - 编码降级后再交给 rich
        console.rule("推断准确率回测 (L2 · 三把尺, 你分别评测)")
    except ImportError:
        emit = lambda ln="": print(term_text(str(ln)))           # noqa: E731
        print("推断准确率回测 (L2 · 三把尺)")
        print("=" * 40)
    for oracle in ORACLES:
        emit("")
        emit(f"── 子代: {oracle.name}  ({oracle.desc}) ──")
        rep = backtest(base, oracle, days=days)
        for ln in build_backtest_lines(rep):
            emit(ln)
    emit("")
    emit("说明: 三把尺差在「灰区怎么算」。挑你最信的那把——准确率高且'不可判'诚实、危险格少, 就是好尺。")
