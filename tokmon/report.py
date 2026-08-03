"""按需 CLI 报表 (`tokmon report`)。rich 可用则漂亮表格, 否则纯文本降级。"""

from __future__ import annotations

from datetime import datetime

from .aggregate import Agg, filter_since, group_by, summarize
from .records import UsageRecord
from .util import fmt_tokens, fmt_usd, parse_since

try:
    from rich.console import Console
    from rich.table import Table
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False


def _rows_by(records, keyfn, label_fn=str, top: int | None = None):
    groups = group_by(records, keyfn)
    items = sorted(groups.items(), key=lambda kv: kv[1].total_tokens, reverse=True)
    if top:
        items = items[:top]
    return [(label_fn(k), a) for k, a in items]


def _print_table_rich(console, title, rows):
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("")
    table.add_column("Tokens", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cache R/W", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Msgs", justify="right")
    for label, a in rows:
        table.add_row(
            str(label),
            fmt_tokens(a.total_tokens),
            fmt_tokens(a.input_tokens),
            fmt_tokens(a.output_tokens),
            f"{fmt_tokens(a.cache_read)}/{fmt_tokens(a.cache_write)}",
            fmt_usd(a.cost) + ("*" if a.any_unpriced else ""),
            str(a.count),
        )
    console.print(table)


def _print_table_plain(title, rows):
    print(f"\n{title}")
    print(f"  {'':24} {'TOKENS':>9} {'OUTPUT':>9} {'COST':>10} {'MSGS':>6}")
    for label, a in rows:
        print(f"  {str(label)[:24]:24} {fmt_tokens(a.total_tokens):>9} "
              f"{fmt_tokens(a.output_tokens):>9} "
              f"{fmt_usd(a.cost) + ('*' if a.any_unpriced else ''):>10} {a.count:>6}")


def run_report(records: list[UsageRecord], since: str | None, scope: str,
               vscode_only: bool = False) -> None:
    cutoff = parse_since(since)
    rows = filter_since(records, cutoff)
    total = summarize(rows)

    window = since or "all"
    scope_label = scope + (" · 仅.vscode" if vscode_only else "")
    header = (f"Claude Code Token 报表  |  窗口={window}  scope={scope_label}  "
              f"|  总计 {fmt_tokens(total.total_tokens)} tokens  ~{fmt_usd(total.cost)}")

    by_day = _rows_by(rows, lambda r: r.timestamp.date(),
                      label_fn=lambda d: d.isoformat())
    by_day.sort(key=lambda x: x[0], reverse=True)
    by_project = _rows_by(rows, lambda r: r.project)
    by_model = _rows_by(rows, lambda r: r.model)
    by_source = _rows_by(rows, lambda r: r.source_kind)

    if _HAS_RICH:
        console = Console()
        console.rule(header)
        _print_table_rich(console, "按天", by_day)
        _print_table_rich(console, "按项目", by_project)
        _print_table_rich(console, "按模型", by_model)
        _print_table_rich(console, "按来源 (main/subagent/workflow)", by_source)
        if total.any_unpriced:
            console.print("[dim]* 含未知单价的模型, 该行成本可能偏低。[/dim]")
    else:
        print(header)
        _print_table_plain("按天", by_day)
        _print_table_plain("按项目", by_project)
        _print_table_plain("按模型", by_model)
        _print_table_plain("按来源", by_source)
        if total.any_unpriced:
            print("\n* 含未知单价的模型, 该行成本可能偏低。")
        print("\n(提示: pip install rich 可获得更漂亮的表格)")
