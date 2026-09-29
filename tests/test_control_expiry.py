"""控制模式自动过期 (0.22): 开启只管 8 小时, 到期自动关并记审计; 再开 = 续期。
忘了关的控制模式 = 持令牌者一直能经隧道 steer 会话, 所以要有上限。"""

from tokmon import control


def _plane(monkeypatch, now):
    p = control.ControlPlane.__new__(control.ControlPlane)   # 手工搭: 不去读/写真实 ~/.tokmon/control_token
    p.token = "t"
    import threading
    from collections import deque
    p._lock = threading.RLock()
    p._mode_on, p._mode_until = False, None
    p._pending, p._audit, p._seq = {}, deque(maxlen=200), 0
    monkeypatch.setattr(control.time, "time", lambda: now[0])
    emitted = []
    monkeypatch.setattr(control.bus, "emit", lambda ev: emitted.append(ev) or ev)
    return p, emitted


def test_mode_expires_after_ttl_and_is_audited(monkeypatch):
    now = [1_000_000.0]
    p, emitted = _plane(monkeypatch, now)
    assert p.set_mode(True) is True
    assert p.mode_expires_in() == control.MODE_TTL_S == 8 * 3600
    now[0] += control.MODE_TTL_S - 1
    assert p.remote_mode is True
    now[0] += 2
    assert p.remote_mode is False                               # 到期即关
    assert p.status()["remote_mode"] is False and p.status()["mode_expires_in_s"] is None
    assert any(a["action"] == "control-mode" and a["outcome"] == "expired" for a in p._audit)
    assert sum(1 for e in emitted if e.type == "COMMAND_ISSUED") == 1   # 只记一次, 不是每次读都记


def test_reopening_renews_and_closing_clears(monkeypatch):
    now = [2_000_000.0]
    p, _ = _plane(monkeypatch, now)
    p.set_mode(True)
    now[0] += 7 * 3600
    p.set_mode(True)                                            # 续期
    assert p.mode_expires_in() == control.MODE_TTL_S
    p.set_mode(False)
    assert p.remote_mode is False and p.mode_expires_in() is None


def test_attribute_assignment_still_works_for_callers_and_tests(monkeypatch):
    now = [3_000_000.0]
    p, _ = _plane(monkeypatch, now)
    p.remote_mode = True                                        # 旧代码/测试直接赋值: 同样带上期限
    assert p.remote_mode is True and p.mode_expires_in() == control.MODE_TTL_S
    p.remote_mode = False
    assert p.remote_mode is False


def test_expiry_audit_records_when_it_really_expired(monkeypatch):
    now = [4_000_000.0]
    p, emitted = _plane(monkeypatch, now)
    p.set_mode(True)
    until = now[0] + control.MODE_TTL_S
    now[0] = until + 9 * 3600                                       # 一夜没人读, 第二天早上才读到
    assert p.remote_mode is False
    rec = [a for a in p._audit if a["outcome"] == "expired"][0]
    assert rec["ts"] == int(until)                                  # 审计记的是真正关掉的时刻, 不是早上读到的时刻
    assert [e.timestamp for e in emitted if e.type == "COMMAND_ISSUED"] == [until]
