"""`MC_REMOTE` 读页收口的单测 —— 纯函数 + 一条端到端的 HTTP 不变量。

盯死的护栏:
- Host 白名单 (DNS-rebinding 防护) 不被通配符/大小写/端口形态绕过;
- preflight 的**失败安全**: 配置不自洽一律拒绝启动;
- Cookie 形态 (HttpOnly / SameSite / 远程必 Secure);
- **最重要的一条**: 控制门**永不认 Cookie** —— 读门认 Cookie 是为了浏览器导航,
  控制门只认自定义头才挡得住 CSRF。两道门合并 = 控制面被打穿, 所以用一个真 HTTP 用例钉死。
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tokmon import remote, serve


# ---------------- 纯函数 ----------------

@pytest.mark.parametrize("raw,want", [
    ("127.0.0.1:8765", "127.0.0.1"),
    ("localhost", "localhost"),
    ("Example.COM:443", "example.com"),
    ("  example.com  ", "example.com"),
    ("[::1]:8765", "[::1]"),
    ("[::1]", "[::1]"),                     # 旧实现在这里会切成 "[:" 而误拒
    ("", ""),
    (None, ""),
])
def test_normalize_host(raw, want):
    assert remote.normalize_host(raw) == want


def test_local_hosts_always_allowed_even_when_remote_off():
    cfg = remote.RemoteConfig()
    for h in ("127.0.0.1:8765", "localhost", "[::1]:8765", ""):
        assert cfg.host_allowed(h)


def test_remote_host_rejected_unless_enabled_and_listed():
    off = remote.RemoteConfig(enabled=False, hosts=frozenset({"tunnel.example.com"}))
    assert not off.host_allowed("tunnel.example.com")           # 没开远程 -> 不放行

    on = remote.RemoteConfig(enabled=True, hosts=frozenset({"tunnel.example.com"}))
    assert on.host_allowed("tunnel.example.com:443")
    assert on.host_allowed("TUNNEL.EXAMPLE.COM")                # 大小写无关
    assert not on.host_allowed("evil.example.com")
    assert not on.host_allowed("tunnel.example.com.evil.net")   # 非前缀/后缀匹配, 必须整串相等


def test_no_wildcard_support():
    """白名单是**显式登记**, 不支持通配 —— 写了 '*' 也只匹配字面量 '*'。"""
    cfg = remote.RemoteConfig(enabled=True, hosts=frozenset({"*"}))
    assert not cfg.host_allowed("anything.example.com")


def test_from_env_parses_list():
    cfg = remote.from_env({"MC_REMOTE": "1", "MC_REMOTE_HOSTS": "A.example.com, b.example.com ,"})
    assert cfg.enabled
    assert cfg.hosts == frozenset({"a.example.com", "b.example.com"})


def test_from_env_default_is_local():
    cfg = remote.from_env({})
    assert not cfg.enabled and not cfg.hosts


# ---------------- preflight: 失败安全 ----------------

def test_preflight_local_default_ok():
    assert remote.preflight(remote.RemoteConfig(), "127.0.0.1", "tok") is None


def test_preflight_refuses_non_loopback_bind_without_remote():
    """绑到 0.0.0.0 却没开远程 = 局域网裸奔, 必须拒绝启动 (这是本次补的洞之一)。"""
    msg = remote.preflight(remote.RemoteConfig(), "0.0.0.0", "tok")
    assert msg and "拒绝启动" in msg


def test_preflight_refuses_remote_without_hosts():
    msg = remote.preflight(remote.RemoteConfig(enabled=True), "127.0.0.1", "tok")
    assert msg and "MC_REMOTE_HOSTS" in msg


def test_preflight_refuses_remote_without_token():
    cfg = remote.RemoteConfig(enabled=True, hosts=frozenset({"t.example.com"}))
    msg = remote.preflight(cfg, "127.0.0.1", "")
    assert msg and "令牌" in msg


def test_preflight_ok_when_fully_configured():
    cfg = remote.RemoteConfig(enabled=True, hosts=frozenset({"t.example.com"}))
    assert remote.preflight(cfg, "127.0.0.1", "tok") is None


# ---------------- Cookie ----------------

def test_cookie_roundtrip():
    c = remote.build_set_cookie("abc123", secure=True)
    assert "mc_read=abc123" in c and "HttpOnly" in c and "SameSite=Strict" in c and "Secure" in c
    assert remote.cookie_token("mc_read=abc123; other=1") == "abc123"


def test_cookie_not_secure_in_local_mode():
    """本机 http 下不能加 Secure, 否则浏览器直接不存 -> 登录永远失败。"""
    assert "Secure" not in remote.build_set_cookie("x", secure=False)


def test_cookie_token_tolerates_garbage():
    assert remote.cookie_token(None) is None
    assert remote.cookie_token("") is None
    assert remote.cookie_token("not-a-cookie") is None


def test_clear_cookie_expires_immediately():
    assert "Max-Age=0" in remote.clear_cookie(secure=False)


def test_login_throttle_warns_on_cadence():
    t = remote.LoginThrottle(delay_s=0, warn_every=3)
    assert t.on_failure() is None and t.on_failure() is None
    assert t.on_failure() is not None            # 第 3 次给出可打印的告警
    t.on_success()
    assert t.failures == 0


# ---------------- 端到端: 两道门不能合并 ----------------

@pytest.fixture
def remote_server(tmp_path, monkeypatch):
    """起一个真的 ThreadingHTTPServer, 远程模式开, Host 白名单含 testserver。"""
    token = "test-token-abc"
    monkeypatch.setattr(serve.control.plane, "token", token)
    monkeypatch.setattr(serve.control.plane, "check_token",
                        lambda t: bool(t) and t == token)
    monkeypatch.setattr(serve.activity, "snapshot",
                        lambda base, live=None: {"sessions": [], "counts": {}})

    cfg = remote.RemoteConfig(enabled=True, hosts=frozenset({"testserver.example.com"}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._make_handler(tmp_path, cfg))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    port = httpd.server_address[1]
    yield {"url": f"http://127.0.0.1:{port}", "token": token}
    httpd.shutdown()
    httpd.server_close()


def _req(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def test_page_without_token_gets_login_not_data(remote_server):
    code, text, _ = _req(remote_server["url"] + "/sessions")
    assert code == 401
    assert "需要控制令牌" in text                 # 给的是登录页
    assert "Session 状态" not in text             # **没有**泄露真实页面


def test_read_api_without_token_is_forbidden(remote_server):
    code, _, _ = _req(remote_server["url"] + "/api/sessions")
    assert code == 403


def test_login_then_cookie_opens_read_door(remote_server):
    code, _, hdrs = _req(remote_server["url"] + "/api/login", "POST",
                         body={"token": remote_server["token"]})
    assert code == 200
    cookie = hdrs.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie

    jar = cookie.split(";")[0]
    code, text, _ = _req(remote_server["url"] + "/sessions", headers={"Cookie": jar})
    assert code == 200 and "Session 状态" in text


def test_bad_login_rejected(remote_server):
    code, _, hdrs = _req(remote_server["url"] + "/api/login", "POST", body={"token": "wrong"})
    assert code == 403
    assert "Set-Cookie" not in hdrs               # 失败绝不发 Cookie


def test_cookie_does_not_open_the_control_door(remote_server):
    """**本文件最重要的一条**: 读门的 Cookie 对控制端点无效, 否则控制面会被 CSRF 打穿。"""
    _, _, hdrs = _req(remote_server["url"] + "/api/login", "POST",
                      body={"token": remote_server["token"]})
    jar = hdrs["Set-Cookie"].split(";")[0]

    code, _, _ = _req(remote_server["url"] + "/api/control/mode", "POST",
                      headers={"Cookie": jar}, body={"on": True})
    assert code == 403, "Cookie 绝不能开控制门"

    code, _, _ = _req(remote_server["url"] + "/api/control/mode", "POST",
                      headers={"X-Control-Token": remote_server["token"]}, body={"on": True})
    assert code == 200, "自定义头才是控制门的钥匙"


def test_rebinding_host_rejected_even_with_valid_token(remote_server):
    """DNS-rebinding: 令牌对了但 Host 不在白名单 -> 照拒 (两层防护不互相替代)。"""
    code, _, _ = _req(remote_server["url"] + "/api/sessions",
                      headers={"Host": "evil.example.com",
                               "X-Control-Token": remote_server["token"]})
    assert code == 403


# ---------------- 终端编码降级 (原则 5) ----------------

class _FakeOut:
    def __init__(self, encoding):
        self.encoding = encoding


def test_term_text_passthrough_on_utf8(monkeypatch):
    from tokmon import util
    monkeypatch.setattr(util.sys, "stdout", _FakeOut("utf-8"))
    assert util.term_text("✓ 一切正常") == "✓ 一切正常"


def test_term_text_downgrades_on_gbk(monkeypatch):
    """EVOLUTION §Gen0.3 实测的崩溃点: GBK 控制台写 ✓ 直接 UnicodeEncodeError。
    现在必须降级成 ASCII 近似, 而不是让已经算完的体检崩在最后一步。"""
    from tokmon import util
    monkeypatch.setattr(util.sys, "stdout", _FakeOut("gbk"))
    out = util.term_text("✓ 数据契约健康 ⚠ 有告警 ✗ 失败")
    assert "✓" not in out and "⚠" not in out and "✗" not in out
    assert "[OK]" in out and "[!]" in out and "[X]" in out
    assert "数据契约健康" in out                  # 中文在 GBK 下本来就能编, 不该被动
    out.encode("gbk")                             # 降级后必须真的能写出去


def test_term_text_never_raises(monkeypatch):
    from tokmon import util
    monkeypatch.setattr(util.sys, "stdout", _FakeOut("ascii"))
    assert isinstance(util.term_text("✓ 中文 🚀"), str)   # 连中文都编不了时也只能替换, 不能抛
