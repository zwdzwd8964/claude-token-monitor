"""实时盯盘 TUI (`tokmon watch`)。

每隔 interval 秒重扫一次 (借助 parser 的文件级缓存, 只读变动文件), 刷新仪表盘:
  - 顶部: 今天 / 近 7 天 / 全部 的 token 与成本
  - 正在跑: 最近一条用量所属的 .vscode 子项目 + 距今时间
  - 今天各项目 / 各模型明细
rich 不可用时降级为清屏纯文本循环。Ctrl-C 退出。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .aggregate import filter_days, filter_today, group_by, summarize
from .parser import load_records
from .util import fmt_age, fmt_tokens, fmt_usd

try:
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False

LIVE_WINDOW_SECONDS = 90  # 最近一条用量在此范围内视为『正在跑』


def _snapshot(base: Path, include_kinds: set[str], vscode_only: bool):
    records = load_records(base, include_kinds, vscode_only)
    today_records = filter_today(records)
    now = datetime.now().astimezone()

    live_project = None
    live_age = None
    if records:
        newest = max(records, key=lambda r: r.timestamp)
        live_age = (now - newest.timestamp).total_seconds()
        if live_age <= LIVE_WINDOW_SECONDS:
            live_project = newest.project

    return {
        "today": summarize(today_records),
        "week": summarize(filter_days(records, 7)),
        "all": summarize(records),
        "today_records": today_records,
        "live_project": live_project,
        "live_age": live_age,
    }


def _proj_table(today_records, live_project):
    groups = group_by(today_records, lambda r: r.project)
    items = sorted(groups.items(), key=lambda kv: kv[1].total_tokens, reverse=True)
    table = Table(title="今天 · 按项目", title_justify="left", expand=True)
    table.add_column("项目")
    table.add_column("Tokens", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")
    for project, a in items:
        mark = "● " if project == live_project else "  "
        style = "bold green" if project == live_project else None
        table.add_row(
            mark + str(project), fmt_tokens(a.total_tokens),
            fmt_tokens(a.output_tokens), fmt_usd(a.cost), style=style,
        )
    if not items:
        table.add_row("  (今天还没有用量)", "", "", "")
    return table


def _model_table(today_records):
    groups = group_by(today_records, lambda r: r.model)
    items = sorted(groups.items(), key=lambda kv: kv[1].total_tokens, reverse=True)
    table = Table(title="今天 · 按模型", title_justify="left", expand=True)
    table.add_column("模型")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    for model, a in items:
        table.add_row(str(model), fmt_tokens(a.total_tokens), fmt_usd(a.cost))
    if not items:
        table.add_row("(今天还没有用量)", "", "")
    return table


def _render(snap, interval, scope, vscode_only):
    t, w, a = snap["today"], snap["week"], snap["all"]
    head = Text()
    head.append("Claude Code Token Monitor\n", style="bold")
    head.append(f"今天  {fmt_tokens(t.total_tokens):>9}  ~{fmt_usd(t.cost)}\n")
    head.append(f"近7天 {fmt_tokens(w.total_tokens):>9}  ~{fmt_usd(w.cost)}\n")
    head.append(f"全部  {fmt_tokens(a.total_tokens):>9}  ~{fmt_usd(a.cost)}")
    if snap["live_project"]:
        head.append(f"\n● 正在跑: {snap['live_project']}  "
                    f"({fmt_age(snap['live_age'])})", style="bold green")
    elif snap["live_age"] is not None:
        head.append(f"\n○ 最近活动: {fmt_age(snap['live_age'])}", style="dim")

    scope_label = scope + (" · 仅 .vscode" if vscode_only else "")
    footer = Text(
        f"scope={scope_label} · 每 {interval}s 刷新 · Ctrl-C 退出", style="dim")
    return Group(
        Panel(head, border_style="cyan"),
        _proj_table(snap["today_records"], snap["live_project"]),
        _model_table(snap["today_records"]),
        footer,
    )


def run_watch(base: Path, interval: int, include_kinds: set[str],
              scope: str, vscode_only: bool = False) -> None:
    if _HAS_RICH:
        _run_rich(base, interval, include_kinds, scope, vscode_only)
    else:
        _run_plain(base, interval, include_kinds, scope, vscode_only)


def _run_rich(base, interval, include_kinds, scope, vscode_only):
    with Live(refresh_per_second=4, screen=True) as live:
        try:
            while True:
                snap = _snapshot(base, include_kinds, vscode_only)
                live.update(_render(snap, interval, scope, vscode_only))
                time.sleep(interval)
        except KeyboardInterrupt:
            pass


def _run_plain(base, interval, include_kinds, scope, vscode_only):
    try:
        while True:
            snap = _snapshot(base, include_kinds, vscode_only)
            t, w, a = snap["today"], snap["week"], snap["all"]
            print("\033[2J\033[H", end="")  # 清屏
            print("=== Claude Code Token Monitor ===")
            print(f"今天  {fmt_tokens(t.total_tokens):>9}  ~{fmt_usd(t.cost)}")
            print(f"近7天 {fmt_tokens(w.total_tokens):>9}  ~{fmt_usd(w.cost)}")
            print(f"全部  {fmt_tokens(a.total_tokens):>9}  ~{fmt_usd(a.cost)}")
            if snap["live_project"]:
                print(f"● 正在跑: {snap['live_project']} ({fmt_age(snap['live_age'])})")
            print("\n今天 · 按项目")
            groups = group_by(snap["today_records"], lambda r: r.project)
            for project, ag in sorted(groups.items(),
                                      key=lambda kv: kv[1].total_tokens, reverse=True):
                mark = "●" if project == snap["live_project"] else " "
                print(f" {mark} {str(project)[:24]:24} {fmt_tokens(ag.total_tokens):>9}"
                      f"  {fmt_usd(ag.cost)}")
            scope_label = scope + (" · 仅 .vscode" if vscode_only else "")
            print(f"\nscope={scope_label} · 每 {interval}s 刷新 · Ctrl-C 退出"
                  "  (pip install rich 获得更佳体验)")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
