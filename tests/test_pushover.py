"""Pushover 出站通道的单测 (M4.5(b) 的手机环)。

盯死的护栏:
- **默认不外发**: 没配任何通道 -> 一个字节不出本机 (§6 首要铁律);
- **永不用 emergency 优先级**: priority 2 会重试到你手动 ack, 那是"让人想关掉的通知系统";
- **通道互不拖垮**: 一个通道抛异常, 另一个照发 (原则 4 渐进降级);
- **token 绝不回显**: status() 只报"已配置/未配置"。
"""

import pytest

from tokmon import notify
from tokmon.notify import NotifyConfig, Notifier, pushover_priority


# ---------------- 优先级映射 (纯函数) ----------------

@pytest.mark.parametrize("sev,want", [
    ("critical", 1),        # 进度被你阻塞 -> 允许突破手机端免打扰
    ("warning", 0),
    ("info", 0),
    ("unknown-thing", 0),   # 读不出的严重度一律按最低, 不升级
])
def test_pushover_priority(sev, want):
    assert pushover_priority(sev) == want


def test_pushover_never_uses_emergency_priority():
    """priority 2 = 重试到手动 ack。§7「有用且不烦」明确拒绝它 —— 任何严重度都不该映射到 2。"""
    for sev in ("info", "warning", "critical", "", None):
        assert pushover_priority(sev) < 2


# ---------------- 配置 ----------------

def test_no_channel_by_default():
    cfg = NotifyConfig()
    assert not cfg.any_channel()
    assert not cfg.pushover_configured() and not cfg.telegram_configured()


def test_pushover_needs_both_halves():
    assert not NotifyConfig(pushover_token="t").pushover_configured()
    assert not NotifyConfig(pushover_user="u").pushover_configured()
    assert NotifyConfig(pushover_token="t", pushover_user="u").pushover_configured()


def test_load_config_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MC_PUSHOVER_TOKEN", "ptok")
    monkeypatch.setenv("MC_PUSHOVER_USER", "puser")
    monkeypatch.setenv("MC_TELEGRAM_TOKEN", "")
    monkeypatch.setenv("MC_TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(notify.Path, "home", staticmethod(lambda: tmp_path))  # 不读真实 notify.json
    cfg = notify.load_config()
    assert cfg.pushover_configured() and cfg.pushover_token == "ptok"


# ---------------- 投递 ----------------

def test_deliver_sends_nothing_when_unconfigured(monkeypatch):
    """没配通道时**绝不**发起任何网络调用 —— 这是默认安全的全部意义。"""
    calls = []
    monkeypatch.setattr(notify, "_pushover_send", lambda *a, **k: calls.append(a) or True)
    monkeypatch.setattr(notify, "_telegram_send", lambda *a, **k: calls.append(a) or True)
    n = Notifier(NotifyConfig())
    assert n._deliver("hi", "warning") == []
    assert calls == []


def test_deliver_uses_pushover_when_configured(monkeypatch):
    seen = {}

    def fake(token, user, text, severity="warning", timeout=8.0):
        seen.update(token=token, user=user, text=text, severity=severity)
        return True

    monkeypatch.setattr(notify, "_pushover_send", fake)
    n = Notifier(NotifyConfig(pushover_token="t", pushover_user="u"))
    out = n._deliver("[critical] 等待授权 · P", "critical")
    assert out == [("pushover", True, "pushover-已送达")]
    assert seen["severity"] == "critical" and seen["token"] == "t"


def test_one_channel_failure_does_not_block_the_other(monkeypatch):
    """Telegram 挂掉 (抛异常) 不能拖垮 Pushover —— 两条腿互为冗余。"""
    def boom(*a, **k):
        raise TimeoutError("network down")

    monkeypatch.setattr(notify, "_telegram_send", boom)
    monkeypatch.setattr(notify, "_pushover_send", lambda *a, **k: True)
    n = Notifier(NotifyConfig(telegram_token="a", telegram_chat_id="b",
                              pushover_token="t", pushover_user="u"))
    out = n._deliver("hi", "warning")
    names = {r[0]: r[1] for r in out}
    assert names == {"telegram": False, "pushover": True}
    assert "TimeoutError" in out[0][2]            # 失败原因如实记录, 不吞


def test_send_test_without_channel_is_honest():
    r = Notifier(NotifyConfig()).send_test()
    assert r["ok"] is False
    assert "MC_PUSHOVER_TOKEN" in r["detail"]     # 告诉你缺什么, 而不是假装成功


def test_status_never_echoes_secrets():
    n = Notifier(NotifyConfig(pushover_token="super-secret-token", pushover_user="secret-user",
                              telegram_token="tg-secret", telegram_chat_id="chat-secret"))
    blob = repr(n.status())
    for secret in ("super-secret-token", "secret-user", "tg-secret", "chat-secret"):
        assert secret not in blob
    assert n.status()["pushover_configured"] is True
    assert set(n.status()["channels"]) == {"telegram", "pushover"}


def test_egress_label_reflects_configured_channels():
    assert Notifier(NotifyConfig()).status()["egress"] == "仅本地(默认)"
    only_po = Notifier(NotifyConfig(pushover_token="t", pushover_user="u")).status()
    assert only_po["egress"] == "Pushover 出站"


# ---------------- 线路形状 (零外发: 打到本地 stub, 不碰 api.pushover.net) ----------------

def test_pushover_wire_shape_against_local_stub(monkeypatch):
    """真账号才能验"手机真的震了"; 但**发出去的那个 POST 长什么样**可以零外发地钉死。

    做法: 起一个本地 stub HTTP 服务, 把 _pushover_send 的 URL 指过去, 检查 body 的每个字段。
    这样"线路形状对不对"有了机器守卫, 只剩"账号通不通"需要你本人配一次。
    """
    import json as _json
    import threading
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got = {}

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.update(urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8")))
            body = _json.dumps({"status": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/1/messages.json" % httpd.server_address[1]

    real = notify.urllib.request.Request

    def to_stub(u, *a, **k):            # 只改目的地, 其余逻辑走真实代码路径
        return real(url, *a, **k)

    monkeypatch.setattr(notify.urllib.request, "Request", to_stub)
    try:
        ok = notify._pushover_send("ptok", "puser", "[critical] 等待授权 · P", "critical")
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert ok is True
    assert got["token"] == ["ptok"] and got["user"] == ["puser"]
    assert got["message"] == ["[critical] 等待授权 · P"]
    assert got["priority"] == ["1"]              # critical -> high, 且不是 "2"
    assert got["title"] == ["Claude Mission Control"]
