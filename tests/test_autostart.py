"""tokmon 开机自启 (autostart.py) + CLI `serve --boot` / `autostart` 的单测。

盯死的护栏:
- 任务 XML 落盘是真 UTF-16 (与声明一致, 否则 schtasks 拒), 结构对: 登录后 30s / 不限时 / 不起第二份 / 带 --boot;
- 任务计划程序被拒 -> 退到 HKCU Run, 且 Run 走启动脚本 (Run 项设不了工作目录);
- 卸载两处都撤; 非 Windows 一律 unsupported;
- 测试**绝不**真的建任务/写注册表: `_run` 与 `_reg_*` 全部替换。
"""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tokmon import autostart, cli

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """默认: 当成 Windows; 注册表是内存字典; _run 没配就炸 (防止漏网的真调用)。"""
    autostart._reset_for_tests()
    reg = {"value": None, "disabled": False, "cleared": 0}

    def reg_get():
        return reg["value"]

    def reg_set(cmd):
        if reg.get("fail_set"):
            raise PermissionError("denied")
        reg["value"] = cmd

    def reg_delete():
        had = reg["value"] is not None
        reg["value"] = None
        return had

    def reg_clear_disabled():
        reg["cleared"] += 1
        reg["disabled"] = False

    monkeypatch.setattr(autostart, "_supported", lambda: True)
    monkeypatch.setattr(autostart, "_reg_get", reg_get)
    monkeypatch.setattr(autostart, "_reg_set", reg_set)
    monkeypatch.setattr(autostart, "_reg_delete", reg_delete)
    monkeypatch.setattr(autostart, "_reg_run_disabled", lambda: reg["disabled"])
    monkeypatch.setattr(autostart, "_reg_clear_disabled", reg_clear_disabled)
    monkeypatch.setattr(autostart, "_LAUNCHER", tmp_path / "autostart" / "tokmon-boot.pyw")
    monkeypatch.setattr(autostart, "_SERVE_LOG", tmp_path / "logs" / "serve.log")   # 启动脚本兜底写的日志也不碰真家目录

    def no_run(args):
        raise AssertionError(f"unexpected real command: {args}")

    monkeypatch.setattr(autostart, "_run", no_run)
    yield reg
    autostart._reset_for_tests()


class FakeRun:
    """按子命令 (/Query /Create /Delete) 回应的假 schtasks, 记下每次调用; /Create 时顺手读走 XML 文件内容。"""

    def __init__(self, query=(1, "ERROR: The system cannot find the file specified."),
                 create=(0, "SUCCESS"), delete=(0, "SUCCESS")):
        self.resp = {"/Query": query, "/Create": create, "/Delete": delete}
        self.calls = []
        self.xml_bytes = None
        self.xml_path = None

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[1]
        if verb == "/Create":
            self.xml_path = args[args.index("/XML") + 1]
            self.xml_bytes = Path(self.xml_path).read_bytes()
        return self.resp[verb]

    def verbs(self):
        return [c[1] for c in self.calls]


def _parse(xml: str):
    return ET.fromstring(autostart._xml_bytes(xml))


# ---------------- 命令 / XML ----------------

def test_boot_command_prefers_pythonw(monkeypatch, tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(autostart.sys, "executable", str(exe))
    bc = autostart.boot_command(18765)
    assert bc["exe"] == str(exe)                     # 没有 pythonw -> 用 python 本身
    (tmp_path / "pythonw.exe").write_bytes(b"")
    bc = autostart.boot_command(18765, host="127.0.0.1")
    assert bc["exe"] == str(tmp_path / "pythonw.exe")
    assert bc["args"] == ["-m", "tokmon", "serve", "--boot", "--host", "127.0.0.1", "--port", "18765"]
    assert Path(bc["cwd"]) == Path(autostart.__file__).resolve().parent.parent
    assert (Path(bc["cwd"]) / "tokmon" / "__main__.py").exists()


def test_task_xml_well_formed_and_settings():
    root = _parse(autostart.task_xml(18765, user="DEMO\\u"))
    trig = root.find(f"{NS}Triggers/{NS}LogonTrigger")
    assert trig.findtext(f"{NS}Delay") == "PT30S"
    assert trig.findtext(f"{NS}UserId") == "DEMO\\u"
    pr = root.find(f"{NS}Principals/{NS}Principal")
    assert pr.findtext(f"{NS}LogonType") == "InteractiveToken"
    assert pr.findtext(f"{NS}RunLevel") == "LeastPrivilege"
    st = root.find(f"{NS}Settings")
    assert st.findtext(f"{NS}ExecutionTimeLimit") == "PT0S"
    assert st.findtext(f"{NS}MultipleInstancesPolicy") == "IgnoreNew"
    assert st.findtext(f"{NS}DisallowStartIfOnBatteries") == "false"
    assert st.findtext(f"{NS}StopIfGoingOnBatteries") == "false"
    assert st.findtext(f"{NS}Hidden") == "true"
    assert st.findtext(f"{NS}StartWhenAvailable") == "true"
    assert st.findtext(f"{NS}AllowHardTerminate") == "true"
    assert st.findtext(f"{NS}Priority") == "5"       # 不用默认 7 (低于正常, 会被拉起的实例继承)
    ex = root.find(f"{NS}Actions/{NS}Exec")
    args = ex.findtext(f"{NS}Arguments")
    assert "--boot" in args and "--port 18765" in args and args.startswith("-m tokmon serve")
    bc = autostart.boot_command(18765)
    assert ex.findtext(f"{NS}Command") == bc["exe"]
    assert ex.findtext(f"{NS}WorkingDirectory") == bc["cwd"]


def test_task_xml_escapes_everything(monkeypatch):
    monkeypatch.setattr(autostart, "boot_command", lambda port=8765, host="127.0.0.1", claude_dir=None: {
        "exe": "C:/Users/u/A&B <x>/pythonw.exe",
        "args": ["-m", "tokmon", "serve", "--boot", "--host", host, "--port", str(port)],
        "cwd": "C:/Users/u/R&D>",
    })
    root = _parse(autostart.task_xml(18765, user="D&M\\<u>"))
    ex = root.find(f"{NS}Actions/{NS}Exec")
    assert ex.findtext(f"{NS}Command") == "C:/Users/u/A&B <x>/pythonw.exe"
    assert ex.findtext(f"{NS}WorkingDirectory") == "C:/Users/u/R&D>"
    assert root.findtext(f"{NS}Triggers/{NS}LogonTrigger/{NS}UserId") == "D&M\\<u>"


def test_task_xml_user_from_env_or_omitted(monkeypatch):
    monkeypatch.setattr(autostart.getpass, "getuser", lambda: "u")
    monkeypatch.setenv("USERDOMAIN", "DEMO")
    root = _parse(autostart.task_xml(18765))
    assert root.findtext(f"{NS}Triggers/{NS}LogonTrigger/{NS}UserId") == "DEMO\\u"
    monkeypatch.delenv("USERDOMAIN")
    root = _parse(autostart.task_xml(18765))
    assert root.find(f"{NS}Triggers/{NS}LogonTrigger/{NS}UserId") is None
    assert root.find(f"{NS}Principals/{NS}Principal/{NS}UserId") is None
    assert root.findtext(f"{NS}Triggers/{NS}LogonTrigger/{NS}Delay") == "PT30S"


def test_xml_bytes_utf16_le_bom_matches_declaration():
    b = autostart._xml_bytes(autostart.task_xml(18765, user="DEMO\\u"))
    assert b[:2] == b"\xff\xfe"
    text = b[2:].decode("utf-16-le")
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")


# ---------------- install ----------------

def test_install_uses_schtasks_xml(_isolate):
    reg = _isolate
    reg["value"] = "stale"                            # 以前兜底写过 Run -> 装成任务后要清掉, 免得起两份
    fr = FakeRun()
    autostart._run = fr
    r = autostart.install(port=18765)
    assert r["ok"] is True and r["method"] == "task" and r["reason"] is None
    create = [c for c in fr.calls if c[1] == "/Create"][0]
    assert create[:4] == ["schtasks", "/Create", "/TN", autostart.TASK_NAME] and create[-1] == "/F"
    assert fr.xml_bytes[:2] == b"\xff\xfe"
    root = ET.fromstring(fr.xml_bytes)
    assert "--port 18765" in root.findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments")
    assert not os.path.exists(fr.xml_path)           # 临时 XML 用完即删
    assert reg["value"] is None


def test_install_falls_back_to_run_key(_isolate):
    reg = _isolate
    reg["disabled"] = True
    fr = FakeRun(create=(1, "ERROR: Access is denied."))
    autostart._run = fr
    r = autostart.install(port=18765)
    assert r["ok"] is True and r["method"] == "run-key"
    assert "Access is denied" in r["detail"]
    assert reg["value"] == autostart.run_key_command(18765)
    assert str(autostart._LAUNCHER) in reg["value"]
    assert reg["cleared"] == 1                        # 你显式要装 -> 「启动应用」里的禁用标记也撤掉
    src = autostart._LAUNCHER.read_text(encoding="utf-8")
    assert "--boot" in src and "'18765'" in src
    compile(src, "tokmon-boot.pyw", "exec")
    assert not os.path.exists(fr.xml_path)


def test_install_both_fail(_isolate):
    _isolate["fail_set"] = True
    autostart._run = FakeRun(create=(1, "ERROR: Access is denied."))
    r = autostart.install(port=18765)
    assert r["ok"] is False and r["reason"] == "failed" and r["method"] is None
    assert "Access is denied" in r["detail"]


@pytest.mark.parametrize("port", [0, 70000, "x", None])
def test_install_rejects_bad_port(port):
    r = autostart.install(port=port)                  # 没配 _run: 若真去跑 schtasks 会直接炸
    assert r["ok"] is False and r["reason"] == "bad-port"


def test_run_key_command_is_short():
    # 官方文档: Run 项命令行不超过 260 字符 -> 只放 exe + 脚本路径
    cmd = autostart.run_key_command(18765)
    assert cmd == subprocess.list2cmdline([autostart.boot_command(18765)["exe"], str(autostart._LAUNCHER)])


def _isolated_env(tmp_path):
    """跑启动脚本的子进程: 家目录指到临时目录 (tokmon 导入时可能碰 ~/.tokmon), 不带 PYTHONPATH。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(PYTHONIOENCODING="utf-8", USERPROFILE=str(home), HOME=str(home))
    return env


def _run_script(tmp_path, src):
    script = tmp_path / "boot.py"
    script.write_text(src, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], cwd=str(tmp_path), env=_isolated_env(tmp_path),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def test_launcher_actually_starts_tokmon(tmp_path):
    """启动脚本在任意工作目录下都能把 tokmon 跑起来 (chdir + sys.path + runpy, 相对 import 不断)。"""
    src = autostart._launcher_source(18765, "127.0.0.1")
    argv = ["tokmon"] + autostart.boot_command(18765)["args"][2:]
    assert repr(argv) in src
    src = src.replace(repr(argv), repr(["tokmon", "autostart", "--help"]))
    r = _run_script(tmp_path, src)
    assert r.returncode == 0, r.stderr
    assert "install" in r.stdout and "uninstall" in r.stdout
    assert not autostart._SERVE_LOG.exists()          # 正常退出 (SystemExit) 不算失败, 不往日志里记


def test_launcher_passes_tokmon_exit_code(tmp_path):
    src = autostart._launcher_source(18765, "127.0.0.1")
    argv = ["tokmon"] + autostart.boot_command(18765)["args"][2:]
    r = _run_script(tmp_path, src.replace(repr(argv), repr(["tokmon", "autostart", "bogus"])))
    assert r.returncode == 2                          # argparse 报错的退出码原样透传, 不被兜底吞成 1
    assert not autostart._SERVE_LOG.exists()


def test_launcher_failure_before_tokmon_lands_in_serve_log(monkeypatch, tmp_path):
    """c23: 仓库搬走后启动脚本死在 os.chdir(ROOT) —— 那时 tokmon 还没接管输出, pythonw 下一声不吭。
    现在原因 (含 traceback) 追加进 serve.log, 退出码 1。"""
    moved = tmp_path / "old-location" / "proj"
    monkeypatch.setattr(autostart, "_root", lambda: moved)
    src = autostart._launcher_source(18765, "127.0.0.1")
    assert repr(str(autostart._SERVE_LOG)) in src
    r = _run_script(tmp_path, src)
    assert r.returncode == 1
    text = autostart._SERVE_LOG.read_text(encoding="utf-8")
    assert "开机自启脚本没能启动 tokmon" in text and str(moved) in text
    assert "FileNotFoundError" in text and "Traceback" in text
    assert "Traceback" not in r.stderr                # 只记一次 (不再抛给 pythonw 的默认 excepthook)
    r = _run_script(tmp_path, src)                    # 追加, 不覆盖
    assert autostart._SERVE_LOG.read_text(encoding="utf-8").count("没能启动") == 2


# ---------------- --claude-dir (c24) ----------------

def test_boot_command_claude_dir(monkeypatch, tmp_path):
    base = ["-m", "tokmon", "serve", "--boot", "--host", "127.0.0.1", "--port", "18765"]
    assert autostart.boot_command(18765)["args"] == base
    assert autostart.boot_command(18765, claude_dir=None)["args"] == base
    assert autostart.boot_command(18765, claude_dir="  ")["args"] == base
    d = tmp_path / "my claude" / "projects"
    assert autostart.boot_command(18765, claude_dir=str(d))["args"] == base + ["--claude-dir", str(d)]
    # 开机时工作目录是仓库根 -> 相对路径必须在登记时就落成绝对路径
    monkeypatch.chdir(tmp_path)
    args = autostart.boot_command(18765, claude_dir="rel/projects")["args"]
    assert args[-2:] == ["--claude-dir", os.path.abspath("rel/projects")]
    assert os.path.isabs(args[-1])


def test_task_xml_and_launcher_carry_claude_dir(tmp_path):
    d = str(tmp_path / "R&D claude" / "projects")
    root = _parse(autostart.task_xml(18765, user="DEMO\\u", claude_dir=d))
    args = root.findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments")
    assert args == subprocess.list2cmdline(autostart.boot_command(18765, claude_dir=d)["args"])
    assert f'--claude-dir "{d}"' in args              # 带空格 -> 引起来; & 经 XML 转义后原样回来
    assert "--claude-dir" not in autostart.task_xml(18765, user="DEMO\\u")
    src = autostart._launcher_source(18765, "127.0.0.1", claude_dir=d)
    assert repr(["tokmon", "serve", "--boot", "--host", "127.0.0.1", "--port", "18765", "--claude-dir", d]) in src
    compile(src, "tokmon-boot.pyw", "exec")


def test_install_passes_claude_dir_to_task_and_run_key(_isolate, tmp_path):
    d = str(tmp_path / "claude" / "projects")
    fr = FakeRun()
    autostart._run = fr
    assert autostart.install(port=18765, claude_dir=d)["ok"] is True
    args = ET.fromstring(fr.xml_bytes).findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments")
    assert args.endswith(subprocess.list2cmdline(["--claude-dir", d]))
    # 任务计划程序被拒 -> Run 兜底的启动脚本里也得有它
    autostart._run = FakeRun(create=(1, "ERROR: Access is denied."))
    r = autostart.install(port=18765, claude_dir=d)
    assert r["ok"] is True and r["method"] == "run-key"
    src = autostart._LAUNCHER.read_text(encoding="utf-8")
    assert f"'--claude-dir', {d!r}" in src
    assert _isolate["value"] == autostart.run_key_command(18765, claude_dir=d)


def test_install_rejects_bad_claude_dir():
    r = autostart.install(port=18765, claude_dir='C:/Users/u/a"b')   # 没配 _run: 真去跑会炸
    assert r["ok"] is False and r["reason"] == "bad-claude-dir"
    assert autostart.install(port=18765, claude_dir="C:/Users/u/a\nb")["reason"] == "bad-claude-dir"


# ---------------- uninstall ----------------

def test_uninstall_removes_both(_isolate):
    reg = _isolate
    reg["value"] = "x"
    autostart._LAUNCHER.parent.mkdir(parents=True)
    autostart._LAUNCHER.write_text("#", encoding="utf-8")
    fr = FakeRun(query=(0, "tokmon-serve Ready"))
    autostart._run = fr
    r = autostart.uninstall()
    assert r["ok"] is True and r["removed"] == ["task", "run-key"]
    assert fr.verbs() == ["/Query", "/Delete"]
    assert fr.calls[1] == ["schtasks", "/Delete", "/TN", autostart.TASK_NAME, "/F"]
    assert reg["value"] is None
    assert not autostart._LAUNCHER.exists()


def test_uninstall_nothing_installed():
    fr = FakeRun()
    autostart._run = fr
    r = autostart.uninstall()
    assert r["ok"] is True and r["removed"] == []
    assert fr.verbs() == ["/Query"]                   # 没有任务就不去删


def test_uninstall_reports_delete_failure(_isolate):
    _isolate["value"] = "x"
    autostart._run = FakeRun(query=(0, "ok"), delete=(1, "ERROR: Access is denied."))
    r = autostart.uninstall()
    assert r["ok"] is False and r["reason"] == "failed"
    assert r["removed"] == ["run-key"] and "Access is denied" in r["detail"]


# ---------------- status ----------------

def _query_xml(port=18765, command=None, enabled=True):
    xml = autostart.task_xml(port, user="DEMO\\u")
    if command is not None:
        bc = autostart.boot_command(port)
        xml = xml.replace(f"<Command>{bc['exe']}</Command>", f"<Command>{command}</Command>")
    if not enabled:
        xml = xml.replace("<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>")
    return xml


def test_real_run_wrapper(monkeypatch):
    monkeypatch.undo()                                # 用真的 _run, 但只跑无害的 python 自己
    rc, out = autostart._run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert (rc, out) == (3, "hi")
    rc, out = autostart._run(["tokmon-no-such-exe-for-test"])
    assert rc == -1 and out


def test_status_task_installed():
    # 真 schtasks 的输出: 声明 UTF-16 但其实是 OEM 文本, 行尾 \r\r\n
    autostart._run = FakeRun(query=(0, _query_xml(18765).replace("\n", "\r\r\n")))
    st = autostart.status()
    assert st == {"supported": True, "installed": True, "method": "task", "detail": st["detail"]}
    assert "18765" in st["detail"]


def test_status_task_disabled_or_python_gone():
    autostart._run = FakeRun(query=(0, _query_xml(enabled=False)))
    st = autostart.status(fresh=True)
    assert st["installed"] is False and st["method"] == "task" and "禁用" in st["detail"]
    autostart._run = FakeRun(query=(0, _query_xml(command="C:/nonexistent-tokmon-test/pythonw.exe")))
    st = autostart.status(fresh=True)
    assert st["installed"] is False and "不存在" in st["detail"]


def test_status_unparseable_query_still_counts_as_installed():
    autostart._run = FakeRun(query=(0, "garbage"))
    st = autostart.status(fresh=True)
    assert st["installed"] is True and st["method"] == "task"


def test_status_run_key(_isolate):
    autostart._write_launcher(18765, "127.0.0.1")
    _isolate["value"] = autostart.run_key_command(18765)
    autostart._run = FakeRun()
    st = autostart.status(fresh=True)
    assert st["installed"] is True and st["method"] == "run-key"
    _isolate["disabled"] = True                       # 你在「启动应用」里关掉了它
    st = autostart.status(fresh=True)
    assert st["installed"] is False and st["method"] == "run-key"
    _isolate["disabled"] = False
    autostart._LAUNCHER.unlink()                      # 启动脚本没了 -> 登录时会静默失败
    st = autostart.status(fresh=True)
    assert st["installed"] is False and "不见了" in st["detail"]


def _with_wd(xml, wd):
    cur = f"<WorkingDirectory>{autostart.boot_command(18765)['cwd']}</WorkingDirectory>"
    assert cur in xml
    return xml.replace(cur, "" if wd is None else f"<WorkingDirectory>{wd}</WorkingDirectory>")


def test_status_task_repo_moved(tmp_path):
    """c23: 仓库搬走 / 改名后任务里的工作目录失效 —— 登录时任务计划程序起不了, 不能再报「已安装」。"""
    clone = tmp_path / "clone"
    (clone / "tokmon").mkdir(parents=True)
    (clone / "tokmon" / "__main__.py").write_text("", encoding="utf-8")
    for wd in ("C:/old-location/proj",                # 不存在
               str(tmp_path),                         # 存在但不是 tokmon 仓库
               str(clone),                            # 另一份像样的仓库, 但不是当前这份
               None):                                 # 没登记工作目录 = 在 system32 里跑 -m tokmon
        autostart._run = FakeRun(query=(0, _with_wd(_query_xml(18765), wd).replace("\n", "\r\r\n")))
        st = autostart.status(fresh=True)
        assert st["installed"] is False and st["method"] == "task", wd
        assert "仓库位置变了 · 请重新安装" in st["detail"], wd


def test_status_task_repo_same_path_other_spelling():
    root = autostart.boot_command(18765)["cwd"]
    spellings = [root.replace("\\", "/"), root + os.sep]
    if sys.platform == "win32":
        spellings.append(root.upper())                # Windows 路径大小写不敏感
    for wd in spellings:
        autostart._run = FakeRun(query=(0, _with_wd(_query_xml(18765), wd)))
        st = autostart.status(fresh=True)
        assert st["installed"] is True, wd


def test_status_task_garbled_workdir_not_judged():
    # OEM 解码把非 ASCII 路径弄成 ? (解不开的字节成 \ufffd) / 带 %VAR% -> 判断不了就不瞎报「搬走了」
    for wd in ("C:/Users/u/??/proj", "%USERPROFILE%/proj", "C:/Users/u/\ufffd\ufffd/proj"):
        autostart._run = FakeRun(query=(0, _with_wd(_query_xml(18765), wd)))
        assert autostart.status(fresh=True)["installed"] is True, wd


def test_judgeable_non_ascii_is_judged_lossy_is_not():
    """c23 残留: 非 ASCII 本身不是「弄花了」—— 只有 ? / %VAR% / \ufffd 才跳过判断。"""
    for p in ("C:/Users/张三/proj", "D:/Projets/José", "C:/Users/u/proj"):
        assert autostart._judgeable(p), p
    for p in ("", "C:/Users/u/??/proj", "%USERPROFILE%/proj", "C:/a\ufffdb"):
        assert not autostart._judgeable(p), p


def _via_console(text):
    """模拟 schtasks 按本机 OEM 代码页写管道、_run 再按 OEM 解回来 (非 Windows 没有 oem 编码 -> 原样)。"""
    try:
        return text.encode("oem", errors="replace").decode("oem", errors="replace")
    except LookupError:
        return text


def _fake_repo(path):
    (path / "tokmon").mkdir(parents=True)
    (path / "tokmon" / "__main__.py").write_text("", encoding="utf-8")
    return path


def test_status_task_non_ascii_repo_moved(tmp_path):
    """c23 残留: 仓库装在中文 / 带重音的目录下, 搬走后任务计划程序起不了 —— 以前因为「非 ASCII 不判断」照报「已安装」。"""
    clone = _fake_repo(tmp_path / "副本-José" / "claude-token-monitor")
    for wd in (str(tmp_path / "旧位置" / "claude-token-monitor"),       # 不存在
               str(tmp_path / "old-José" / "claude-token-monitor"),     # 不存在
               str(clone)):                                             # 存在、像样, 但不是当前这份
        xml = _with_wd(_query_xml(18765), wd).replace("\n", "\r\r\n")
        for label, text in (("原样", xml), ("经本机 OEM", _via_console(xml))):
            autostart._run = FakeRun(query=(0, text))
            st = autostart.status(fresh=True)
            seen = _via_console(wd) if label != "原样" else wd
            if "?" in seen:                                             # 本机代码页里没这些字 -> 成了 ?, 判断不了
                assert st["installed"] is True, (wd, label)
                continue
            assert st["installed"] is False and st["method"] == "task", (wd, label)
            assert "仓库位置变了 · 请重新安装" in st["detail"], (wd, label)


def test_status_task_non_ascii_repo_in_place(monkeypatch, tmp_path):
    """仓库原地没动: 路径里有中文 / 重音 / 本机代码页只能「近似」的字 (936 里 â->a) —— 经本机 OEM 读回来也得认成这份仓库。
    只去掉 ASCII 限制、不按同样方式弄花再比, 这里会被误报成「搬走了」, 而且重装也好不了。"""
    root = _fake_repo(tmp_path / "用户-José-caf\u00e2" / "claude-token-monitor")
    monkeypatch.setattr(autostart, "_root", lambda: root)
    xml = _query_xml(18765).replace("\n", "\r\r\n")
    assert f"<WorkingDirectory>{root}</WorkingDirectory>" in xml
    for text in (xml, _via_console(xml)):
        autostart._run = FakeRun(query=(0, text))
        st = autostart.status(fresh=True)
        assert st["installed"] is True and st["method"] == "task", st


_TEST_CPS = {"tokmon_test_cp437": 437, "tokmon_test_cp1252": 1252}


@pytest.fixture
def us_console(monkeypatch):
    """把「本机」模拟成英文系统: OEM 437 / ANSI 1252 (Windows 自己的代码页转换, 含近似字), 与跑测试的机器无关。"""
    import codecs
    if not hasattr(codecs, "code_page_encode"):
        pytest.skip("需要 Windows 的代码页编解码")

    def search(name):
        cp = _TEST_CPS.get(name)
        if cp is None:
            return None
        return codecs.CodecInfo(
            name=name,
            encode=lambda s, errors="strict", cp=cp: codecs.code_page_encode(cp, s, errors),
            decode=lambda b, errors="strict", cp=cp: codecs.code_page_decode(cp, bytes(b), errors, True))

    # 生产里按 OEM 写、按 ANSI 写两种都要认; 下面把它们换成模拟英文系统的 437 / 1252
    assert autostart._CONSOLE_WRITE == ("oem", "mbcs") and autostart._CONSOLE_READ == "oem"
    codecs.register(search)
    monkeypatch.setattr(autostart, "_CONSOLE_WRITE", ("tokmon_test_cp437", "tokmon_test_cp1252"))
    monkeypatch.setattr(autostart, "_CONSOLE_READ", "tokmon_test_cp437")
    yield lambda text, write: text.encode(write, errors="replace").decode("tokmon_test_cp437", errors="replace")
    codecs.unregister(search)


def test_status_task_best_fit_views_on_other_codepage(us_console, monkeypatch, tmp_path):
    """英文系统上 Ł 被近似成 L (没有 ?), 若按 ANSI 写 é 会读成 Θ —— 原地没动的仓库都得认得出; 真搬走了照样报。"""
    root = _fake_repo(tmp_path / "Jos\u00e9-\u0141ukasz" / "claude-token-monitor")
    monkeypatch.setattr(autostart, "_root", lambda: root)
    xml = _query_xml(18765).replace("\n", "\r\r\n")
    moved = _with_wd(_query_xml(18765), str(tmp_path / "old-Jos\u00e9-\u0141ukasz" / "claude-token-monitor"))
    for write in ("tokmon_test_cp437", "tokmon_test_cp1252"):
        seen = us_console(str(root), write)
        assert seen != str(root) and autostart._judgeable(seen), (write, seen)   # 真被弄花了, 且没有 ? 可跳过
        autostart._run = FakeRun(query=(0, us_console(xml, write)))
        assert autostart.status(fresh=True)["installed"] is True, write
        autostart._run = FakeRun(query=(0, us_console(moved, write)))
        st = autostart.status(fresh=True)
        assert st["installed"] is False and "仓库位置变了" in st["detail"], write


def test_status_task_non_ascii_python_gone(tmp_path):
    """任务里的 Python 路径同理: 装在中文 / 重音目录下的 Python 不在了, 也要报。"""
    gone = str(tmp_path / "蟒蛇-Jos\u00e9" / "pythonw.exe")
    autostart._run = FakeRun(query=(0, _query_xml(command=gone)))
    st = autostart.status(fresh=True)
    assert st["installed"] is False and "Python 已不存在" in st["detail"]


def test_status_task_python_garbled_by_codepage_not_gone(us_console, monkeypatch, tmp_path):
    """当前这个 Python 只是被代码页近似弄花 (Ł->L, 读回来的路径并不存在) —— 不算没了。"""
    exe = tmp_path / "\u0141ukasz" / "python.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(autostart.sys, "executable", str(exe))
    seen = us_console(str(exe), "tokmon_test_cp437")
    assert seen != str(exe) and not os.path.exists(seen)
    autostart._run = FakeRun(query=(0, _query_xml(command=seen)))
    assert autostart.status(fresh=True)["installed"] is True


def test_status_run_key_repo_moved_or_python_gone(_isolate, monkeypatch, tmp_path):
    """c23: Run 兜底 —— 启动脚本里的 ROOT 过期 (会死在 chdir) / Run 值里的 Python 没了, 都不能报「已安装」。"""
    real_root = autostart._root()
    monkeypatch.setattr(autostart, "_root", lambda: tmp_path / "old-location" / "proj")
    autostart._write_launcher(18765, "127.0.0.1")     # 在「旧位置」时装的
    monkeypatch.setattr(autostart, "_root", lambda: real_root)   # 仓库搬到了现在的位置
    _isolate["value"] = autostart.run_key_command(18765)
    autostart._run = FakeRun()
    st = autostart.status(fresh=True)
    assert st["installed"] is False and st["method"] == "run-key"
    assert "仓库位置变了 · 请重新安装" in st["detail"]
    autostart._write_launcher(18765, "127.0.0.1")     # 重新安装 -> 好了
    assert autostart.status(fresh=True)["installed"] is True
    # Run 值里的 pythonw 没了 (比如重装过 Python)
    _isolate["value"] = subprocess.list2cmdline(["C:/nonexistent-tokmon-test/pythonw.exe", str(autostart._LAUNCHER)])
    st = autostart.status(fresh=True)
    assert st["installed"] is False and "Python 已不存在" in st["detail"] and "pythonw.exe" in st["detail"]
    # 启动脚本被改得认不出 ROOT
    _isolate["value"] = autostart.run_key_command(18765)
    autostart._LAUNCHER.write_text("#", encoding="utf-8")
    st = autostart.status(fresh=True)
    assert st["installed"] is False and "认不出" in st["detail"]


def test_status_foreign_run_value_not_judged(_isolate):
    # 同名 Run 值但不是我们生成的 (没指向启动脚本): 认不出就按原样报, 不去拆它的命令行
    _isolate["value"] = "C:/Program Files/Some Tool/tool.exe --flag"
    autostart._run = FakeRun()
    st = autostart.status(fresh=True)
    assert st["installed"] is True and st["method"] == "run-key"


def test_status_not_installed():
    autostart._run = FakeRun()
    st = autostart.status(fresh=True)
    assert st["installed"] is False and st["method"] is None and st["supported"] is True
    assert set(st) == {"supported", "installed", "method", "detail"}


def test_status_cached_and_invalidated_by_install():
    fr = FakeRun()
    autostart._run = fr
    autostart.status()
    autostart.status()
    assert fr.verbs().count("/Query") == 1           # 页面每 4s 拉一次, 不能每次都起 schtasks
    autostart.status(fresh=True)
    assert fr.verbs().count("/Query") == 2
    autostart.install(port=18765)
    autostart.status()
    assert fr.verbs().count("/Query") == 3


def test_non_windows_unsupported(monkeypatch):
    monkeypatch.undo()                                # 撤掉 _supported 桩, 走真实的 sys.platform 判断
    monkeypatch.setattr(autostart, "_run", lambda a: (_ for _ in ()).throw(AssertionError(a)))
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    autostart._reset_for_tests()
    st = autostart.status()
    assert st["supported"] is False and st["installed"] is False and st["method"] is None
    assert autostart.install(18765)["reason"] == "unsupported"
    assert autostart.install(18765)["ok"] is False
    assert autostart.uninstall() == {"ok": False, "removed": [], "reason": "unsupported",
                                     "detail": autostart.uninstall()["detail"]}


# ---------------- CLI ----------------

def test_cli_parse_serve_boot():
    p = cli.build_parser()
    assert p.parse_args(["serve", "--boot"]).boot is True
    assert p.parse_args(["serve"]).boot is False
    a = p.parse_args(["autostart", "install", "--port", "18765"])
    assert a.cmd == "autostart" and a.action == "install" and a.port == 18765
    with pytest.raises(SystemExit):
        p.parse_args(["autostart", "bogus"])


def test_cli_autostart_status_needs_no_claude_dir(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "default_base", lambda: tmp_path / "missing")
    calls = []
    monkeypatch.setattr(cli.autostart, "status",
                        lambda fresh=False: calls.append(fresh) or
                        {"supported": True, "installed": False, "method": None, "detail": "都没有登记"})
    assert cli.main(["autostart", "status"]) == 0
    out = capsys.readouterr().out
    assert "开机自启" in out and "未安装" in out and "找不到" not in out
    assert calls == [True]


def test_cli_autostart_status_unsupported(monkeypatch, capsys):
    monkeypatch.setattr(autostart, "_supported", lambda: False)
    assert cli.main(["autostart", "status"]) == 0
    assert "只支持 Windows" in capsys.readouterr().out
    assert cli.main(["autostart", "install"]) == 1


def test_cli_autostart_install_and_uninstall(monkeypatch, capsys):
    got = {}
    monkeypatch.setattr(cli.autostart, "install",
                        lambda port=8765, host="127.0.0.1", claude_dir=None:
                        got.update(port=port, claude_dir=claude_dir) or
                        {"ok": True, "method": "task", "reason": None, "detail": "任务计划程序"})
    assert cli.main(["autostart", "install", "--port", "18765"]) == 0
    assert got == {"port": 18765, "claude_dir": None}   # 没给 --claude-dir 就不登记 (开机用默认目录)
    out = capsys.readouterr().out
    assert "已登记开机自启" in out and "uninstall" in out and "--boot" in out
    assert "--claude-dir" not in out
    monkeypatch.setattr(cli.autostart, "install",
                        lambda port=8765, host="127.0.0.1", claude_dir=None: {"ok": False, "method": None,
                                                                              "reason": "failed", "detail": "x"})
    assert cli.main(["autostart", "install"]) == 1
    monkeypatch.setattr(cli.autostart, "uninstall",
                        lambda: {"ok": True, "removed": ["task", "run-key"], "reason": None, "detail": ""})
    assert cli.main(["autostart", "uninstall"]) == 0
    assert "任务计划程序" in capsys.readouterr().out


def test_cli_autostart_install_claude_dir(monkeypatch, tmp_path, capsys):
    """c24: 平时 `serve --claude-dir X` 的人装自启要能把 X 一起登记, 否则开机那次去找默认目录。"""
    p = cli.build_parser()
    assert p.parse_args(["autostart", "install", "--claude-dir", "D:/c"]).claude_dir == Path("D:/c")
    assert p.parse_args(["autostart", "status"]).claude_dir is None
    got = {}
    monkeypatch.setattr(cli.autostart, "install",
                        lambda port=8765, host="127.0.0.1", claude_dir=None:
                        got.update(port=port, claude_dir=claude_dir) or
                        {"ok": True, "method": "task", "reason": None, "detail": "任务计划程序"})
    d = tmp_path / "claude" / "projects"
    d.mkdir(parents=True)
    assert cli.main(["autostart", "install", "--port", "18765", "--claude-dir", str(d)]) == 0
    assert got == {"port": 18765, "claude_dir": str(d)}
    out = capsys.readouterr().out
    assert f"--claude-dir {d}" in out and "注意" not in out
    missing = tmp_path / "missing"
    assert cli.main(["autostart", "install", "--claude-dir", str(missing)]) == 0   # 照装, 只提醒
    assert got["claude_dir"] == str(missing)
    assert "不存在" in capsys.readouterr().out


def _run_main_capturing_redirect(argv):
    """跑 main 并拿回它重定向出去的 stdout (再还原, 关掉日志文件)。"""
    orig_out, orig_err = sys.stdout, sys.stderr
    try:
        rc = cli.main(argv)
        redirected = sys.stdout
        assert sys.stderr is redirected
    finally:
        now = sys.stdout
        sys.stdout, sys.stderr = orig_out, orig_err
    if now is not orig_out:
        now.close()
    return rc, now


def test_cli_serve_boot_redirects_and_passes_boot(monkeypatch, tmp_path):
    log = tmp_path / "logs" / "serve.log"
    monkeypatch.setattr(cli, "_SERVE_LOG", log)
    seen = {}

    def fake_run_serve(base, host, port, boot=False):
        seen.update(base=base, host=host, port=port, boot=boot)
        print("fake serve up")
        sys.stderr.write("stderr line\n")             # http.server 的访问日志就是这么写的

    monkeypatch.setattr(cli, "run_serve", fake_run_serve)
    claude = tmp_path / "claude"
    claude.mkdir()
    rc, f = _run_main_capturing_redirect(["serve", "--boot", "--port", "18765", "--claude-dir", str(claude)])
    assert rc == 0
    assert seen == {"base": claude, "host": "127.0.0.1", "port": 18765, "boot": True}
    text = log.read_text(encoding="utf-8")
    assert "tokmon serve 启动" in text and "--boot" in text
    assert "fake serve up" in text and "stderr line" in text


def test_cli_serve_boot_missing_dir_still_starts(monkeypatch, tmp_path):
    """开机那次 Claude 目录不在 (比如自定义目录没登记进任务): serve 照样起 —— 实例 / 开机拉起不依赖它。
    警告只由 run_serve 写一次 (test_instances_api 钉住那一行); cli 不再重复, 免得日志里同一件事两行。"""
    log = tmp_path / "logs" / "serve.log"
    monkeypatch.setattr(cli, "_SERVE_LOG", log)
    seen = {}

    def fake_run_serve(base, host, port, boot=False):
        seen.update(base=base, boot=boot)
        print(f"找不到 Claude 数据目录: {base} (run_serve 的警告)")

    monkeypatch.setattr(cli, "run_serve", fake_run_serve)
    missing = tmp_path / "missing"
    rc, _f = _run_main_capturing_redirect(["serve", "--boot", "--claude-dir", str(missing)])
    assert rc == 0
    assert seen == {"base": missing, "boot": True}
    text = log.read_text(encoding="utf-8")
    assert text.count("找不到 Claude 数据目录") == 1


def test_cli_missing_dir_without_boot_still_refuses(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "run_serve", lambda *a, **k: pytest.fail("should not start"))
    missing = str(tmp_path / "missing")
    assert cli.main(["serve", "--claude-dir", missing]) == 1       # 手动 serve: 目录写错了就直说, 不带病起
    assert cli.main(["report", "--claude-dir", missing]) == 1
    out = capsys.readouterr().out
    assert "找不到 Claude 数据目录" in out and "照常继续" not in out


def test_cli_serve_pythonw_redirects_without_boot(monkeypatch, tmp_path):
    log = tmp_path / "logs" / "serve.log"
    monkeypatch.setattr(cli, "_SERVE_LOG", log)
    monkeypatch.setattr(cli, "run_serve", lambda base, host, port, boot=False: print(f"boot={boot}"))
    claude = tmp_path / "claude"
    claude.mkdir()
    orig_out, orig_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = None                    # pythonw 下就是这样
    try:
        rc = cli.main(["serve", "--claude-dir", str(claude)])
        f = sys.stdout
    finally:
        sys.stdout, sys.stderr = orig_out, orig_err
    f.close()
    assert rc == 0
    text = log.read_text(encoding="utf-8")
    assert "boot=False" in text and "--boot" not in text


def test_cli_serve_plain_keeps_console(monkeypatch, tmp_path):
    log = tmp_path / "logs" / "serve.log"
    monkeypatch.setattr(cli, "_SERVE_LOG", log)
    seen = {}
    monkeypatch.setattr(cli, "run_serve", lambda base, host, port, boot=False: seen.update(boot=boot))
    claude = tmp_path / "claude"
    claude.mkdir()
    before = sys.stdout
    assert cli.main(["serve", "--claude-dir", str(claude)]) == 0
    assert sys.stdout is before and seen == {"boot": False}
    assert not log.exists()


def test_cli_serve_log_rotates(monkeypatch, tmp_path):
    log = tmp_path / "logs" / "serve.log"
    log.parent.mkdir()
    log.write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(cli, "_SERVE_LOG", log)
    monkeypatch.setattr(cli, "_SERVE_LOG_MAX", 10)
    monkeypatch.setattr(cli, "run_serve", lambda *a, **k: None)
    claude = tmp_path / "claude"
    claude.mkdir()
    _run_main_capturing_redirect(["serve", "--boot", "--claude-dir", str(claude)])
    assert (tmp_path / "logs" / "serve.log.1").read_text(encoding="utf-8") == "x" * 100
    assert "x" * 100 not in log.read_text(encoding="utf-8")
