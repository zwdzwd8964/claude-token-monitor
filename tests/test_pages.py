"""页面文件 (会话驾驶舱 S0): 页面都从 tokmon/pages/*.html 载入, serve.py 只留路由与数据。"""

from pathlib import Path

import pytest

from tokmon import serve

PAGES = Path(serve.__file__).parent / "pages"
LOADED = [("HOME", "home.html"), ("SESS_PAGE", "sessions.html"), ("PROC_PAGE", "processes.html"),
          ("NOTIFY_PAGE", "notify.html"), ("CONTROL_PAGE", "control.html"), ("DOCTOR_PAGE", "doctor.html"),
          ("BACKTEST_PAGE", "backtest.html"), ("BILLING_PAGE", "billing.html"), ("WORKFLOW_PAGE", "workflow.html"),
          ("PAGE", "tokens.html"), ("LOGIN_PAGE", "login.html")]


@pytest.mark.parametrize("const,fn", LOADED)
def test_every_page_loads_from_its_file(const, fn):
    page = getattr(serve, const)
    assert (PAGES / fn).is_file()
    assert "页面文件缺失" not in page and page.lstrip().lower().startswith("<!doctype html>")
    assert "__BASE__" not in page and "__NAV__" not in page          # 皮肤与导航都换上了


def test_pages_with_header_get_the_shared_nav():
    for const, fn in LOADED:
        if const in ("LOGIN_PAGE", "HOME"):
            continue                                                 # 登录页没有导航; 主页是入口磁贴
        assert '<details class="more">' in getattr(serve, const), const


def test_serve_does_not_inline_pages_again():
    src = Path(serve.__file__).read_text(encoding="utf-8").lower()
    assert '"""<!doctype html>' not in src                           # 新页面放 tokmon/pages/, 别再内联进 serve.py
