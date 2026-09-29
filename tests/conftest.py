"""全局护栏: 任何测试都碰不到真实的 ~/.tokmon/instances、serve.log 与开机启动脚本。

0.22 开发期间出过一次: 某个测试 (或验证脚本) 在真实主目录留下了实例层的 owner.lock / owner.json。
各测试自己的 fixture 会再指向自己的临时目录; 这里只是兜底 —— 没被显式隔离的测试也只会写进本次会话的临时目录。
真实目录存在 (你本机已登记实例) 时同样管用: 前后比对其中每个文件的大小与修改时刻, 变了就算这个测试写穿了。
"""

from pathlib import Path

import pytest

from tokmon import autostart, cli, instances

_REAL_INSTANCES = Path.home() / ".tokmon" / "instances"


_LIVE_WRITES = {"state.json"}      # 本机正在跑的 tokmon (实例的主人) 自己会一直改的: 只查有没有新增, 不比内容


def _snapshot(root: Path):
    if not root.exists():
        return None
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        live = rel.parts[0] == "logs" or rel.name in _LIVE_WRITES
        st = p.stat()
        out.append((str(rel),) if live else (str(rel), st.st_size, st.st_mtime_ns))
    return sorted(out)


@pytest.fixture(autouse=True)
def _never_touch_real_tokmon_home(tmp_path_factory, monkeypatch):
    sandbox = tmp_path_factory.mktemp("tokmon-home")
    instances.set_home(sandbox / "instances")
    monkeypatch.setattr(autostart, "_SERVE_LOG", sandbox / "logs" / "serve.log")
    monkeypatch.setattr(autostart, "_LAUNCHER", sandbox / "autostart" / "tokmon-boot.pyw")
    monkeypatch.setattr(cli, "_SERVE_LOG", sandbox / "logs" / "serve.log")
    before = _snapshot(_REAL_INSTANCES)
    yield
    instances.set_home(sandbox / "parked")
    assert _snapshot(_REAL_INSTANCES) == before, "测试写穿到了真实的 ~/.tokmon/instances"
