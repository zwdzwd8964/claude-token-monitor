"""tokmon 自己的开机自启 (实例层 v0.22) —— 登录后在后台起 `tokmon serve --boot`。

为什么需要: 实例层的「开机拉起」(auto 实例自动起 / ask 实例提醒你) 由 serve 进程执行,
所以 tokmon 自己得先在开机后跑起来, 否则一切无从谈起。

做法 (只动当前用户自己的东西, 不要管理员):
- 首选 **任务计划程序**: 当前用户登录触发、延迟 30 秒 (等桌面/网络稳定)、无窗口 (pythonw)、
  不限运行时长、已在跑就不再起第二份 (IgnoreNew)。
- 被拒 (策略/权限) -> 兜底 **HKCU\\...\\Run** 注册表项。Run 项设不了工作目录, 且官方文档限命令行 260 字符,
  所以让它跑一个我们生成的小启动脚本 (~/.tokmon/autostart/tokmon-boot.pyw), 脚本里 chdir + runpy。

P7 对照: ① 只登记/撤销「启动 tokmon serve --boot」这一件事; ② 只在你显式执行 (CLI / 页面按钮) 时改动;
③ 页面入口由 serve 层把关 (令牌 + 仅本机); ④ serve 层审计; ⑤ 失败如实报告, 不伪装成功。

所有外部命令走 `_run`, 注册表走 `_reg_*` 小函数 —— 测试整体替换它们, 绝不真的去建任务/写注册表。
非 Windows: status 报 supported=False, install/uninstall 一律 unsupported (不做半吊子的 systemd/launchd)。
"""

from __future__ import annotations

import ast
import getpass
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "tokmon-serve"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 任务管理器「启动应用」里关掉某个 Run 项, 记在这里 (值首字节为奇数 = 已禁用); 没有该值 = 启用。
_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_LAUNCHER = Path.home() / ".tokmon" / "autostart" / "tokmon-boot.pyw"
# 开机那次 serve 的输出落在这 (cli 用同一个文件); 启动脚本连 tokmon 都没起来时也把原因追加到这里。
_SERVE_LOG = Path.home() / ".tokmon" / "logs" / "serve.log"
_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# schtasks 往管道写字的代码页: 正常是 OEM (中文系统 936 / 英文系统 437); 万一按 ANSI 写 (英文系统 1252 ≠ 437) 也认得出。
# 读回来一律按 OEM 解 (_run)。测试会换成模拟别的系统代码页的编解码器。
_CONSOLE_WRITE = ("oem", "mbcs")
_CONSOLE_READ = "oem"

# 页面每 4s 拉一次 /api/instances, 每次都起一个 schtasks 进程太浪费 -> 30s 记忆; install/uninstall 后立即作废。
_STATUS_TTL = 30.0
_STATUS_CACHE: tuple[float, dict] | None = None
_LOCK = threading.Lock()


def _supported() -> bool:
    return sys.platform == "win32"


def _run(args: list[str]) -> tuple[int, str]:
    """跑一个外部命令 (无窗口), 返回 (退出码, stdout+stderr 文本)。起不来 -> (-1, 原因)。"""
    try:
        r = subprocess.run(args, capture_output=True, timeout=20, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)
    raw = (r.stdout or b"") + (r.stderr or b"")
    try:
        # schtasks 往管道写的是 OEM 代码页 (中文系统上是 GBK), 不是 UTF-8
        text = raw.decode(_CONSOLE_READ if _supported() else "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return r.returncode, text.strip()


# ---- 注册表 (HKCU Run) 小函数: 测试整体替换 ----

def _reg_get() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            v, _t = winreg.QueryValueEx(k, TASK_NAME)
            return str(v)
    except OSError:
        return None


def _reg_set(command: str) -> None:
    """写 Run 值; 失败抛 OSError。"""
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, TASK_NAME, 0, winreg.REG_SZ, command)


def _reg_delete() -> bool:
    """删 Run 值: True=删掉了, False=本来就没有; 其它失败抛 OSError。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, TASK_NAME)
        return True
    except FileNotFoundError:
        return False


def _reg_run_disabled() -> bool:
    """Run 项是否在「启动应用」里被你关掉了 (那样登录时不会跑)。读不到 = 没关。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPROVED_KEY) as k:
            v, _t = winreg.QueryValueEx(k, TASK_NAME)
        return bool(v) and bool(v[0] & 1)
    except (OSError, TypeError, IndexError):
        return False


def _reg_clear_disabled() -> None:
    """你显式要装 -> 顺手撤掉「启动应用」里的禁用标记, 否则装了也不跑。尽力而为。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, TASK_NAME)
    except OSError:
        pass


# ---- 要登记的命令 ----

def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _abs_dir(claude_dir) -> str | None:
    """--claude-dir 落成绝对路径: 开机时工作目录是仓库根, 相对路径会被解释到那儿去。空 = 不指定。"""
    if claude_dir is None or str(claude_dir).strip() == "":
        return None
    return os.path.abspath(os.path.expanduser(str(claude_dir).strip()))


def boot_command(port: int = 8765, host: str = "127.0.0.1", claude_dir: str | None = None) -> dict:
    """开机要跑的命令。优先 pythonw (没有控制台窗口); 工作目录 = 仓库根, 让 `-m tokmon` 找得到包。
    claude_dir: 你平时用 `serve --claude-dir X` 时要一起登记, 否则开机那次去找默认目录。"""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    args = ["-m", "tokmon", "serve", "--boot", "--host", str(host), "--port", str(int(port))]
    cd = _abs_dir(claude_dir)
    if cd:
        args += ["--claude-dir", cd]
    return {
        "exe": str(w if w.exists() else exe),
        "args": args,
        "cwd": str(_root()),
    }


def _logon_user() -> str | None:
    dom = os.environ.get("USERDOMAIN")
    try:
        user = getpass.getuser()
    except Exception:
        user = None
    return f"{dom}\\{user}" if dom and user else None


def task_xml(port: int = 8765, host: str = "127.0.0.1", user: str | None = None,
             claude_dir: str | None = None) -> str:
    """任务计划程序 XML。声明 UTF-16: 落盘时必须真的是 UTF-16 (声明与文件编码不符 schtasks 会拒)。"""
    bc = boot_command(port, host, claude_dir)
    user = user if user is not None else _logon_user()
    uid = f"\n      <UserId>{escape(user)}</UserId>" if user else ""    # 不知道是谁就不写 (由注册者兜底)
    desc = ("Claude Mission Control (tokmon): 登录后在后台启动本机看板, 并按你给实例设的开机策略拉起服务。"
            "由 python -m tokmon autostart install|uninstall 管理。")
    # Priority 5 (= 正常): 任务默认 7 是「低于正常」, 且子进程会继承 -> tokmon 拉起的实例也会全被降优先级。
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(desc)}</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>{uid}
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{uid}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(bc["exe"])}</Command>
      <Arguments>{escape(subprocess.list2cmdline(bc["args"]))}</Arguments>
      <WorkingDirectory>{escape(bc["cwd"])}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _xml_bytes(xml: str) -> bytes:
    """UTF-16 LE + BOM, 与 XML 声明一致。"""
    return b"\xff\xfe" + xml.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le")


def _launcher_source(port: int, host: str, claude_dir: str | None = None) -> str:
    bc = boot_command(port, host, claude_dir)
    root = bc["cwd"]
    argv = ["tokmon"] + bc["args"][2:]          # 去掉 "-m tokmon"
    # pythonw 没有控制台: 仓库搬走 (chdir 失败) / 包坏了 (import 失败) 都死在 tokmon 接管输出之前 ->
    # 自己把原因追加进 serve.log, 别静默消失。SystemExit 不是 Exception, tokmon 正常退出码照样透传。
    return (
        "# 由 `python -m tokmon autostart install` 生成: 注册表 Run 兜底用 (Run 项设不了工作目录)。\n"
        "# 删掉本文件 = 这条开机自启失效; 撤销请用 `python -m tokmon autostart uninstall`。\n"
        "import os, runpy, sys\n"
        f"ROOT = {root!r}\n"
        f"LOG = {str(_SERVE_LOG)!r}\n"
        f"sys.argv = {argv!r}\n"
        "try:\n"
        "    os.chdir(ROOT)\n"
        "    sys.path.insert(0, ROOT)\n"
        "    runpy.run_module('tokmon', run_name='__main__', alter_sys=True)\n"
        "except Exception:\n"
        "    import time, traceback\n"
        "    try:\n"
        "        os.makedirs(os.path.dirname(LOG), exist_ok=True)\n"
        "        with open(LOG, 'a', encoding='utf-8', errors='backslashreplace') as f:\n"
        "            f.write('=== %s 开机自启脚本没能启动 tokmon (ROOT=%s) · 仓库搬过就重新安装 ===\\n'\n"
        "                    % (time.strftime('%Y-%m-%d %H:%M:%S'), ROOT))\n"
        "            traceback.print_exc(file=f)\n"
        "    except Exception:\n"
        "        pass\n"
        "    sys.exit(1)\n"
    )


def run_key_command(port: int = 8765, host: str = "127.0.0.1", claude_dir: str | None = None) -> str:
    """Run 值: pythonw + 启动脚本 (短, 远低于 260 字符上限)。端口 / claude_dir 都在脚本里。"""
    return subprocess.list2cmdline([boot_command(port, host, claude_dir)["exe"], str(_LAUNCHER)])


# ---- 状态 ----

def _judgeable(path: str) -> bool:
    """schtasks 输出是 OEM 解码的: 代码页里没有的字被换成 ? (解不开的字节成了 \\ufffd), %VAR% 要展开 -> 这些不做存在性判断, 免得误报。
    非 ASCII 本身不算: 中文系统 OEM=936, 张三 / José 这类路径原样回来, 不判断的话仓库搬走了还报「已安装」。"""
    return bool(path) and not any(c in path for c in "?%\ufffd")


def _console_views(path: str) -> set[str]:
    """path 经 schtasks 写进管道、再被 _run 解回来可能的样子: 原样, 或按代码页弄花后的样子。
    errors="replace" 走的正是 Windows 默认转换 —— 没有的字先换「近似字」(936 里 â->a, 437 里 Ł->L), 不一定是 ? ->
    读回来的路径没有 ? 却和真路径对不上; 把真路径按同样的方式弄花再比, 才不会把原地没动的仓库报成「搬走了」(重装也好不了)。"""
    views = {path}
    for enc in _CONSOLE_WRITE:
        try:
            views.add(path.encode(enc, errors="replace").decode(_CONSOLE_READ, errors="replace"))
        except (LookupError, UnicodeError):          # 非 Windows 没有 oem / mbcs
            pass
    return views


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except (TypeError, ValueError):
        return False


def _is_this_repo(path: str, via_console: bool = False) -> bool:
    """path 就是当前这份仓库 (大小写 / 斜杠不敏感) 且里面真有 tokmon/__main__.py —— 否则登录时 `-m tokmon` 起不来。
    via_console: path 读自 schtasks 输出 -> 当前仓库根按同样方式弄花后对得上也算 (对上了就是同一个目录, 去仓库根里找 __main__.py)。"""
    root = str(_root())
    views = _console_views(root) if via_console else {root}
    return (any(_same_path(path, v) for v in views)
            and os.path.isfile(os.path.join(root, "tokmon", "__main__.py")))


def _python_gone(cmd: str) -> bool:
    """任务登记的 Python 已不存在。读回来的路径可能被代码页弄花: 对得上当前这个 Python 弄花后的样子就不算没了。"""
    if not _judgeable(cmd) or os.path.exists(cmd):
        return False
    return not any(_same_path(cmd, v) for v in _console_views(boot_command()["exe"]))


_REPO_MOVED = "仓库位置变了 · 请重新安装"


def _describe_task(text: str) -> tuple[bool, str]:
    """解析 `schtasks /Query /XML` 的输出 -> (真的会在登录时跑, 说明)。解析失败按「在」处理, 不瞎报。"""
    try:
        body = text[text.index("<Task"):]
        root = ET.fromstring(body)
    except (ValueError, ET.ParseError):
        return True, f"任务计划程序「{TASK_NAME}」· 登录后 30 秒后台启动"
    enabled = root.findtext(f"{_TASK_NS}Settings/{_TASK_NS}Enabled")
    if (enabled or "").strip().lower() == "false":
        return False, f"任务计划程序里有「{TASK_NAME}」, 但被禁用了 · 重新安装会启用它"
    exec_ = f"{_TASK_NS}Actions/{_TASK_NS}Exec/{_TASK_NS}"
    cmd = (root.findtext(exec_ + "Command") or "").strip().strip('"')
    args = root.findtext(exec_ + "Arguments") or ""
    wd = (root.findtext(exec_ + "WorkingDirectory") or "").strip().strip('"')
    if _python_gone(cmd):
        return False, f"任务「{TASK_NAME}」指向的 Python 已不存在 ({Path(cmd).name}) · 请重新安装"
    # 仓库搬走 / 改名: 工作目录不存在时任务计划程序根本起不了动作 (目录名无效), 存在但不是这份仓库 `-m tokmon` 也找不到包。
    # 没登记工作目录 = 在 system32 里跑 `-m tokmon`, 同样起不来。
    if not wd or (_judgeable(wd) and not _is_this_repo(wd, via_console=True)):
        return False, f"任务「{TASK_NAME}」登记的{_REPO_MOVED}"
    m = re.search(r"--port\s+(\d+)", args)
    port = f" · 端口 {m.group(1)}" if m else ""
    return True, f"任务计划程序「{TASK_NAME}」· 登录后 30 秒后台启动{port}"


def _first_arg(cmd: str) -> str:
    """Run 值 (list2cmdline 拼的) 的第一个参数 = 要跑的 exe。"""
    cmd = cmd.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 0 else cmd[1:]
    return cmd.split(None, 1)[0] if cmd else ""


def _launcher_root() -> str | None:
    """从启动脚本里读回登记时的仓库根 (ROOT = '...'); 读不到 / 认不出 -> None。"""
    try:
        text = _LAUNCHER.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"^ROOT = (.+)$", text, re.M)
    if not m:
        return None
    try:
        v = ast.literal_eval(m.group(1).strip())
    except (ValueError, SyntaxError):
        return None
    return v if isinstance(v, str) else None


def _run_key_problem(val: str) -> str | None:
    """Run 值是我们登记的那条, 但登录时注定跑不起来 -> 原因。看着没问题, 或不是我们生成的 (认不出就不瞎报) -> None。"""
    if str(_LAUNCHER).casefold() not in val.casefold():
        return None
    exe = _first_arg(val)
    if exe and "%" not in exe and not os.path.exists(exe):          # 注册表是 Unicode 读的, 不存在 OEM 弄花的问题
        return f"注册表 Run 登记的 Python 已不存在 ({Path(exe).name}) · 请重新安装"
    if not _LAUNCHER.exists():                                      # 比如删过 ~/.tokmon
        return "注册表 Run 已登记, 但它要跑的启动脚本不见了 · 重新安装即可"
    root = _launcher_root()
    if root is None:
        return "注册表 Run 已登记, 但启动脚本认不出 (不是这版 tokmon 生成的) · 重新安装即可"
    if not _is_this_repo(root):                                     # 脚本会死在 os.chdir(ROOT)
        return f"注册表 Run 已登记, 但{_REPO_MOVED}"
    return None


def _compute_status() -> dict:
    if not _supported():
        return {"supported": False, "installed": False, "method": None, "detail": "开机自启只支持 Windows"}
    rc, out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML", "ONE"])
    task_ok, task_detail = _describe_task(out) if rc == 0 else (False, "")
    run_val = _reg_get()
    # Run 值登记着, 但登录时注定跑不起来 (Python / 启动脚本 / 仓库位置不对了) -> 不能报「已安装」
    run_problem = _run_key_problem(run_val) if run_val else None
    run_off = bool(run_val) and not run_problem and _reg_run_disabled()
    if task_ok:
        extra = " · 注意: 注册表 Run 里也登记了一份 (重新安装会清掉)" if run_val else ""
        return {"supported": True, "installed": True, "method": "task", "detail": task_detail + extra}
    if run_val and not run_off and not run_problem:
        return {"supported": True, "installed": True, "method": "run-key",
                "detail": f"注册表 HKCU Run「{TASK_NAME}」· 登录时后台启动"}
    if run_problem:
        return {"supported": True, "installed": False, "method": "run-key", "detail": run_problem}
    if run_val:
        return {"supported": True, "installed": False, "method": "run-key",
                "detail": "注册表 Run 已登记, 但在「启动应用」里被关掉了 · 重新安装会打开它"}
    if rc == 0:
        return {"supported": True, "installed": False, "method": "task", "detail": task_detail}
    return {"supported": True, "installed": False, "method": None,
            "detail": "任务计划程序和注册表 Run 里都没有登记"}


def status(fresh: bool = False) -> dict:
    """{"supported","installed","method": "task"|"run-key"|None,"detail"}。installed = 按我们能查到的, 登录时真的会跑。"""
    global _STATUS_CACHE
    with _LOCK:
        now = time.monotonic()
        if not fresh and _STATUS_CACHE and now - _STATUS_CACHE[0] < _STATUS_TTL:
            return dict(_STATUS_CACHE[1])
        st = _compute_status()
        _STATUS_CACHE = (time.monotonic(), st)
        return dict(st)


def _invalidate() -> None:
    global _STATUS_CACHE
    with _LOCK:
        _STATUS_CACHE = None


# ---- 安装 / 卸载 ----

def _write_launcher(port: int, host: str, claude_dir: str | None = None) -> None:
    _LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LAUNCHER.with_name(_LAUNCHER.name + ".tmp")
    tmp.write_text(_launcher_source(port, host, claude_dir), encoding="utf-8")
    os.replace(tmp, _LAUNCHER)


def install(port: int = 8765, host: str = "127.0.0.1", claude_dir: str | None = None) -> dict:
    """先任务计划程序; 被拒再退到 HKCU Run。-> {"ok","method","reason","detail"}
    claude_dir: 非默认的 Claude 数据目录 (serve 的 --claude-dir), 给了就原样登记进开机命令。"""
    if not _supported():
        return {"ok": False, "method": None, "reason": "unsupported", "detail": "开机自启只支持 Windows"}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "method": None, "reason": "bad-port", "detail": f"端口无效: {port!r}"}
    if not 1 <= port <= 65535:
        return {"ok": False, "method": None, "reason": "bad-port", "detail": f"端口无效: {port}"}
    # 引号 / 换行进了命令行就拆不回原样 (Windows 路径里本来也不可能有)
    if claude_dir is not None and any(c in str(claude_dir) for c in '"\r\n\x00'):
        return {"ok": False, "method": None, "reason": "bad-claude-dir", "detail": "Claude 目录路径含非法字符"}
    _invalidate()
    xml_path = None
    try:
        fd, xml_path = tempfile.mkstemp(prefix="tokmon-task-", suffix=".xml")
        with os.fdopen(fd, "wb") as f:
            f.write(_xml_bytes(task_xml(port, host, claude_dir=claude_dir)))
        rc, out = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
    except OSError as e:
        rc, out = -1, str(e)
    finally:
        if xml_path:
            try:
                os.remove(xml_path)
            except OSError:
                pass
    if rc == 0:
        # 以前兜底写过 Run 项 -> 清掉, 否则登录时会起两份 (第二份抢不到端口, 白报错)
        try:
            _reg_delete()
        except OSError:
            pass
        _invalidate()
        return {"ok": True, "method": "task", "reason": None,
                "detail": f"任务计划程序「{TASK_NAME}」: 登录后 30 秒后台运行 serve --boot (端口 {port})"}
    task_err = out or f"schtasks 退出码 {rc}"
    try:
        _write_launcher(port, host, claude_dir)
        _reg_set(run_key_command(port, host, claude_dir))
        _reg_clear_disabled()
    except OSError as e:
        _invalidate()
        return {"ok": False, "method": None, "reason": "failed",
                "detail": f"任务计划程序失败 ({task_err}); 注册表 Run 也失败 ({e})"}
    _invalidate()
    return {"ok": True, "method": "run-key", "reason": None,
            "detail": f"任务计划程序不可用 ({task_err}) -> 已改用注册表 HKCU Run「{TASK_NAME}」(端口 {port})"}


def uninstall() -> dict:
    """任务和 Run 项都撤 (谁在删谁)。-> {"ok","removed":[...],"reason","detail"}"""
    if not _supported():
        return {"ok": False, "removed": [], "reason": "unsupported", "detail": "开机自启只支持 Windows"}
    _invalidate()
    removed, errors = [], []
    rc, _out = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    if rc == 0:
        rc, out = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if rc == 0:
            removed.append("task")
        else:
            errors.append(f"删除任务失败: {out or rc}")
    try:
        if _reg_delete():
            removed.append("run-key")
    except OSError as e:
        errors.append(f"删除注册表 Run 失败: {e}")
    try:
        _LAUNCHER.unlink()
    except OSError:
        pass
    _invalidate()
    if errors:
        return {"ok": False, "removed": removed, "reason": "failed", "detail": "; ".join(errors)}
    return {"ok": True, "removed": removed, "reason": None,
            "detail": "已移除开机自启" if removed else "本来就没装开机自启"}


def _reset_for_tests() -> None:
    _invalidate()
