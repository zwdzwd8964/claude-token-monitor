"""命令行入口: `python -m tokmon <report|watch|serve|doctor|backtest|autostart|pricing> [options]`。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, autostart, pricing
from .discovery import default_base
from .doctor import run_console_doctor, run_doctor
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
    ps.add_argument("--boot", action="store_true",
                    help="开机自启任务用: 本次开机只做一次开机拉起 (auto 实例拉起 / ask 实例提醒)")

    pa = sub.add_parser("autostart", help="tokmon 开机自启 (Windows 任务计划程序, 被拒时退到注册表 Run)")
    pa.add_argument("action", choices=["install", "uninstall", "status"],
                    help="install=登记 / uninstall=撤销 / status=查看")
    pa.add_argument("--port", type=int, default=8765, help="开机后 serve 用的端口 (默认 8765)")
    # 不继承 common (--scope 这些对自启无意义), 只要 --claude-dir: 平时用非默认目录, 开机那次也得用它
    pa.add_argument("--claude-dir", type=Path, default=None,
                    help="开机后 serve 用的 Claude projects 目录 (默认不指定 = ~/.claude/projects)")

    pp = sub.add_parser("pricing", help="价目表: 看 tokmon 用的单价 / 导出价目快照给别的程序读")
    pp.add_argument("--json", action="store_true", help="输出 JSON 价目快照 (默认是给人看的表)")
    pp.add_argument("--out", type=Path, default=None, help="把 JSON 快照写到这个文件 (隐含 --json)")
    return p


def _run_pricing(as_json: bool, out: Path | None) -> int:
    snap = pricing.snapshot(__version__)
    if out is not None:
        out = Path(out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, out)           # 先写临时文件再换名: 读的一方不会读到半个文件
        print(f"价目快照已写到 {out} (官方价目 {snap['fetched']}, tokmon {__version__})")
        return 0
    if as_json:
        # 纯 ASCII: Windows 管道默认按本地代码页 (GBK) 编码, 读的一方按 UTF-8 解就乱; \uXXXX 两边都不会错
        print(json.dumps(snap, ensure_ascii=True, indent=2))
        return 0
    print(f"tokmon {__version__} 价目 · USD / 百万 token · 来源 {snap['source']} (抓取 {snap['fetched']})")
    print(f"{'模型':20} {'输入':>7} {'5m 写':>7} {'1h 写':>7} {'缓存读':>7} {'输出':>7}  备注")
    for key, e in snap["models"].items():
        notes = []
        if "long_prompt" in e:
            lp = e["long_prompt"]
            notes.append(f"prompt > {lp['over_tokens']:,}: {lp['input']:g} / {lp['cache_write_5m']:g} / "
                         f"{lp['cache_write_1h']:g} / {lp['cache_read']:g} / {lp['output']:g}")
        if "fast" in e:
            notes.append(f"fast: {e['fast']['input']:g} / {e['fast']['output']:g}")
        if e.get("us_geo_mult"):
            notes.append(f"us × {e['us_geo_mult']:g}")
        print(f"{key:20} {e['input']:>7g} {e['cache_write_5m']:>7g} {e['cache_write_1h']:>7g} "
              f"{e['cache_read']:>7g} {e['output']:>7g}  {' · '.join(notes)}")
    print(f"web search ${snap['web_search_usd_per_request'] * 1000:g} / 1000 次 · 认不出的新版本按同家族最新型号估算 (标为未确认)")
    return 0


# 开机自启走 pythonw: 没有控制台, sys.stdout/stderr 是 None, 一 write 就炸 (http.server 的访问日志直接写 stderr)。
# 所以这两种情况把输出落到文件里, 顺便让你事后能查「开机那次到底起没起来」。
# 与注册表 Run 启动脚本兜底写的是同一个文件 (那个脚本在 tokmon 起来之前失败也往这儿记)。
_SERVE_LOG = autostart._SERVE_LOG
_SERVE_LOG_MAX = 5 * 1024 * 1024


def _redirect_serve_output(boot: bool) -> None:
    orig = sys.stdout
    try:
        _SERVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        if _SERVE_LOG.exists() and _SERVE_LOG.stat().st_size > _SERVE_LOG_MAX:
            os.replace(_SERVE_LOG, _SERVE_LOG.with_name(_SERVE_LOG.name + ".1"))
    except OSError:
        pass                            # 轮转失败 (比如另一个 serve 正开着它): 接着追加, 别丢日志
    try:
        f = open(_SERVE_LOG, "a", encoding="utf-8", errors="backslashreplace", buffering=1)
    except OSError:
        if sys.stdout is not None and sys.stderr is not None:
            return                      # 有控制台就留在控制台, 总比丢了强
        f = open(os.devnull, "w", encoding="utf-8")
    if orig is not None:
        try:
            print(f"输出写到 {_SERVE_LOG}", file=orig, flush=True)
        except (OSError, ValueError):
            pass
    sys.stdout = sys.stderr = f
    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} tokmon serve 启动 (pid {os.getpid()}"
          f"{', --boot' if boot else ''}) ===")


def _run_autostart(action: str, port: int, claude_dir: Path | None = None) -> int:
    if action == "status":
        st = autostart.status(fresh=True)
        if not st.get("supported"):
            print(st.get("detail") or "开机自启: 当前系统不支持")
        elif st.get("installed"):
            how = {"task": "任务计划程序", "run-key": "注册表 Run"}.get(st.get("method"), st.get("method"))
            print(f"开机自启: 已安装 ({how}) · {st.get('detail')} · 撤销: python -m tokmon autostart uninstall")
        else:
            print(f"开机自启: 未安装 · {st.get('detail')} · 安装: python -m tokmon autostart install")
        return 0
    if action == "install":
        cd = str(claude_dir) if claude_dir else None
        if cd and not Path(cd).expanduser().exists():
            # 照样登记: 开机那次 serve 缺目录也会起 (实例照常, Token 页没数据), 只是提醒你可能写错了
            print(f"注意: Claude 数据目录现在不存在: {cd}")
        r = autostart.install(port=port, claude_dir=cd)
        if r.get("ok"):
            bc = autostart.boot_command(port, claude_dir=cd)
            print(f"已登记开机自启 · {r.get('detail')} · 命令: {bc['exe']} {' '.join(bc['args'])} · "
                  f"日志 {_SERVE_LOG} · 撤销: python -m tokmon autostart uninstall")
            return 0
        print(f"开机自启登记失败 ({r.get('reason')}): {r.get('detail')}")
        return 1
    r = autostart.uninstall()
    if r.get("ok"):
        what = "、".join({"task": "任务计划程序", "run-key": "注册表 Run"}.get(m, m) for m in r.get("removed") or [])
        print(f"已移除开机自启 ({what})" if what else "本来就没装开机自启")
        return 0
    print(f"移除开机自启失败 ({r.get('reason')}): {r.get('detail')}")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "autostart":         # 与 Claude 数据目录无关: 在目录检查之前处理
        return _run_autostart(args.action, args.port, args.claude_dir)
    if args.cmd == "pricing":           # 同上: 只读价目表
        return _run_pricing(args.json, args.out)
    if args.cmd == "serve" and (args.boot or sys.stdout is None or sys.stderr is None):
        _redirect_serve_output(args.boot)   # 先重定向, 下面「找不到目录」这类错误也能落进日志
    base = args.claude_dir or default_base()
    include_kinds = SCOPE_KINDS[args.scope]

    if not Path(base).exists():
        if not (args.cmd == "serve" and args.boot):
            print(f"找不到 Claude 数据目录: {base}")
            return 1
        # 开机那次不能因为它就整个不起: 实例管理 / 开机拉起根本不需要 Claude 数据, 退出了就什么都没了。
        # 照样起 serve, Token / 会话这些页没数据 (降级)。警告由 run_serve 自己写进 serve.log (它对直接调用者
        # 也要做同样的判断), 这里不再说一遍 —— 两边都说, 日志里同一件事就是两行。

    if args.cmd == "report":
        records = load_records(base, include_kinds, args.vscode_only)
        run_report(records, args.since, args.scope, args.vscode_only)
    elif args.cmd == "watch":
        run_watch(base, args.interval, include_kinds, args.scope, args.vscode_only)
    elif args.cmd == "doctor":
        run_doctor(base)               # 成本/测量层契约
        print()
        run_inference_doctor(base)     # 推断层契约 (M4.5: activity/events 的假设)
        print()
        run_console_doctor(base)       # 控制台: 归属 / 项目修正 / 每次运行的 token 对账 (CONSOLES_PLAN S1)
    elif args.cmd == "backtest":
        run_backtests(base, days=args.days)   # L2: classify_state 准确率回测 (三把尺)
    elif args.cmd == "serve":
        # 没起来 (端口绑不上 / 远程配置被拒) -> 退出码 1: `tokmon serve || ...` 与任务计划程序的「上次运行结果」要看得出失败
        if run_serve(base, args.host, args.port, boot=args.boot) is False:
            return 1
    return 0
