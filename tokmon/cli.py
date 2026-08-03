"""命令行入口: `python -m tokmon <report|watch> [options]`。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .discovery import default_base
from .doctor import run_doctor
from .inference_backtest import run_backtests
from .inference_doctor import run_inference_doctor
from .parser import load_records
from .report import run_report
from .serve import run_serve
from .tui import run_watch

SCOPE_KINDS = {
    "all": {"main", "subagent", "workflow"},
    "main": {"main"},
}


def build_parser() -> argparse.ArgumentParser:
    # 公共参数放进 parent, 让两个子命令都接受 (可写在子命令前或后)。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--claude-dir", type=Path, default=None,
                        help="Claude projects 目录 (默认 ~/.claude/projects)")
    common.add_argument("--scope", choices=list(SCOPE_KINDS), default="all",
                        help="统计范围: all=含子智能体/workflow (默认), main=仅主会话")
    common.add_argument("--vscode-only", action="store_true",
                        help="只统计位于 .vscode 之下的会话 (排除其它目录的会话)")

    p = argparse.ArgumentParser(
        prog="tokmon",
        description="监控 Claude Code 在所有 VSCode session 里产生的 token 用量。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("report", parents=[common], help="按需输出汇总报表")
    pr.add_argument("--since", default="7d",
                    help="时间窗口: today / all / 24h / 7d / 2w (默认 7d)")

    pw = sub.add_parser("watch", parents=[common], help="实时盯盘 TUI")
    pw.add_argument("--interval", type=int, default=5, help="刷新间隔秒数 (默认 5)")

    sub.add_parser("doctor", parents=[common],
                   help="数据契约体检: 识别率/字段覆盖/去重/未知模型 (升级 Claude Code 后跑一次)")

    pb = sub.add_parser("backtest", parents=[common],
                        help="推断准确率回测 (L2): 用 transcript 的未来当真值, 量 classify_state 准不准 (三把尺)")
    pb.add_argument("--days", type=int, default=7, help="回测窗口天数 (默认 7)")

    ps = sub.add_parser("serve", parents=[common],
                        help="本地 Web 看板 (早期预览, 只监听本机)")
    ps.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1, 只本机)")
    ps.add_argument("--port", type=int, default=8765, help="端口 (默认 8765)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = args.claude_dir or default_base()
    include_kinds = SCOPE_KINDS[args.scope]

    if not Path(base).exists():
        print(f"找不到 Claude 数据目录: {base}")
        return 1

    if args.cmd == "report":
        records = load_records(base, include_kinds, args.vscode_only)
        run_report(records, args.since, args.scope, args.vscode_only)
    elif args.cmd == "watch":
        run_watch(base, args.interval, include_kinds, args.scope, args.vscode_only)
    elif args.cmd == "doctor":
        run_doctor(base)               # 成本/测量层契约
        print()
        run_inference_doctor(base)     # 推断层契约 (M4.5: activity/events 的假设)
    elif args.cmd == "backtest":
        run_backtests(base, days=args.days)   # L2: classify_state 准确率回测 (三把尺)
    elif args.cmd == "serve":
        run_serve(base, args.host, args.port)
    return 0
