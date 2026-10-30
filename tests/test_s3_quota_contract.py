"""CONSOLES_PLAN S3「额度与对账」: 各代的额度读数 / 页顶额度条的取数 / 控制台契约计数 / doctor 的契约一节。"""

import json
import os
import time
from pathlib import Path

import pytest

from tokmon import consoles, parser, serve
from tokmon.consoles import QuotaReading, Snapshot, _quota
from tokmon.doctor import contract_lines

from test_s1_attribution import U, line, world, write  # noqa: F401  (world 是 fixture)
from test_s2_waiting import snap  # noqa: F401  (fixture)

KILLED = "20261023-000000-killed"


# ---------------------------------------------------------------- 额度读数

def test_quota_readings_from_every_dialect(snap):
    q = snap.quota
    a5, a7 = q[("A", "five_hour")], q[("A", "seven_day")]
    assert (a5.utilization, a5.resets_at, a5.run) == (0.42, 18000.0, KILLED)        # A: windows 是小数, 重置时刻只属于 limit 那个窗口
    assert (a7.utilization, a7.resets_at) == (0.61, None)
    assert (q[("B", "five_hour")].utilization, q[("B", "five_hour")].resets_at) == (0.05, None)   # B: 顶层小数, 不报重置
    assert ("C", "five_hour") not in q and 20000.0 in snap.reset_marks["five_hour"]  # C: 只有重置时刻
    assert a5.status == "allowed" and q[("A", "five_hour")].utilization == 0.42      # A 之后被拒的那次 (另一次运行): 不盖掉 0.42
    assert snap.rejected[("A", "five_hour")] == (2020.0, 9999999999.0)               # ……被拒单独记下
    e5 = q[("E", "five_hour")]
    assert (e5.utilization, e5.resets_at, e5.ts) == (0.3, 18000.0, 7600.0)          # E 之后那条故障注入的 0.99 不认
    assert q[("E", "seven_day")].utilization == 0.2


def test_quota_bar_takes_the_newest_reading_and_greys_old_ones(snap):
    now = snap.quota_now(7800.0)
    assert now["five_hour"] == {"utilization": 0.3, "resets_at": 18000.0, "ts": 7600.0, "age_s": 200, "console": "E",
                                "status": "", "rejected_at": None, "stale": False, "expired": False}
    # E 的事件一条一个状态、不说是哪个窗口的: 不认 (只有 limit 指的窗口、或窗口自己带的状态才算)
    assert now["seven_day"]["utilization"] == 0.2 and now["seven_day"]["resets_at"] == 600000.0
    later = snap.quota_now(7600.0 + consoles.QUOTA_STALE_S + 1)
    assert later["five_hour"]["stale"] and not later["five_hour"]["expired"]          # 超过 1 小时: 置灰
    gone = snap.quota_now(18000.5)
    assert gone["five_hour"]["expired"]                                               # 过了重置时刻: 读数作废
    assert Snapshot().quota_now(time.time()) == {"five_hour": None, "seven_day": None}   # 没配控制台: 没有就是没有


def test_reset_time_is_borrowed_only_from_the_same_window_period():
    s = Snapshot()
    s.quota[("B", "five_hour")] = QuotaReading("B", "r", "five_hour", 10000.0, 0.5, None)
    s.reset_marks["five_hour"] = {12000.0}
    assert s.quota_now(10010.0)["five_hour"]["resets_at"] == 12000.0                 # 10000 落在 (12000-5h, 12000] 里
    s.reset_marks["five_hour"] = {9500.0}
    q = s.quota_now(10010.0)["five_hour"]
    assert q["resets_at"] is None and not q["expired"]                               # 那是上一个窗口的重置时刻: 不借


def test_a_reading_is_void_once_a_newer_window_has_started():
    """B 不报重置时刻; C 只报了下一个窗口的重置时刻 (说明读数之后窗口已经翻过去了) -> 这条读数作废, 不能当现在的。"""
    s = Snapshot()
    _quota(s, "B", "r", 10000.0, {"five_hour": 0.95, "seven_day": 0.5, "status": "allowed"})
    _quota(s, "C", "r", 10120.0, {"status": "allowed", "limit": "five_hour", "resets_at": 10060 + 18000, "utilization": None})
    q = s.quota_now(10200.0)["five_hour"]
    assert q["resets_at"] is None and q["expired"] and not q["stale"]
    _quota(s, "C", "r", 9900.0, {"status": "allowed", "limit": "five_hour", "resets_at": 10060, "utilization": None})
    q = s.quota_now(10200.0)["five_hour"]
    assert q["resets_at"] == 10060 and q["expired"]                                  # 同一周期的也报过: 借到, 照样作废
    assert not s.quota_now(10030.0)["five_hour"]["expired"]                          # 还没到下一个窗口: 不作废


def test_rejected_without_utilization_still_reaches_the_bar():
    """A / C 的被拒事件不带利用率: 利用率仍是之前那条的 (不编), 状态改成 rejected; 只管 limit 指的那个窗口。"""
    s = Snapshot()
    _quota(s, "A", "r", 100.0, {"status": "allowed_warning", "limit": "five_hour", "resets_at": 18000, "utilization": None,
                                "windows": {"five_hour": 0.85, "seven_day": 0.3}})
    _quota(s, "A", "r", 400.0, {"status": "rejected", "limit": "five_hour", "resets_at": 18000, "utilization": None,
                                "windows": {}})
    q = s.quota_now(500.0)
    assert (q["five_hour"]["status"], q["five_hour"]["utilization"], q["five_hour"]["rejected_at"]) == ("rejected", 0.85, 400.0)
    assert q["seven_day"]["status"] == "" and q["seven_day"]["rejected_at"] is None
    _quota(s, "A", "r", 600.0, {"status": "allowed", "limit": "five_hour", "resets_at": 18000, "utilization": None,
                                "windows": {"five_hour": 0.2, "seven_day": 0.3}})
    assert s.quota_now(700.0)["five_hour"]["status"] == "allowed"                    # 之后有了新读数: 被拒就过去了
    s2 = Snapshot()
    _quota(s2, "A", "r", 100.0, {"status": "allowed", "limit": "five_hour", "resets_at": 300, "windows": {"five_hour": 0.5}})
    _quota(s2, "A", "r", 200.0, {"status": "rejected", "limit": "five_hour", "resets_at": 300, "windows": {}})
    assert s2.quota_now(400.0)["five_hour"]["status"] == "allowed"                   # 被拒到重置为止, 过了重置就不算


def test_a_status_without_a_window_is_not_pinned_on_every_window():
    s = Snapshot()
    _quota(s, "E", "r", 100.0, {"status": "rejected", "known": True, "injected": None,           # E: 一条一个状态, 不说哪个窗口
                                "windows": {"five_hour": {"utilization": 0.03, "resets_at": 18000},
                                            "seven_day": {"utilization": 0.84, "resets_at": 600000}}})
    _quota(s, "D", "r", 90.0, {"status": "rejected",                                              # D: 窗口自己带状态
                               "windows": {"five_hour": {"utilization": 0.5, "resets_at": 18000, "status": None},
                                           "seven_day": {"utilization": 0.9, "resets_at": 600000, "status": "rejected"}}})
    assert s.quota[("E", "five_hour")].status == "" and s.quota[("E", "seven_day")].status == ""
    assert s.quota[("D", "five_hour")].status == "" and s.quota[("D", "seven_day")].status == "rejected"


def test_non_finite_numbers_are_ignored():
    s = Snapshot()
    _quota(s, "B", "r", float("nan"), {"five_hour": 0.5})
    _quota(s, "B", "r", 100.0, {"five_hour": float("inf"), "seven_day": 0.5})
    assert list(s.quota) == [("B", "seven_day")]
    assert s.quota_now(200.0)["seven_day"]["age_s"] == 100


def test_sessions_payload_carries_quota(world):
    out = serve._sessions_with_briefs(world["base"])
    q = out["quota"]
    assert q["five_hour"]["console"] == "E" and q["five_hour"]["utilization"] == 0.3
    assert q["five_hour"]["stale"] and q["five_hour"]["expired"]                     # 夹具的时刻都在 1970 年
    for w in ("five_hour", "seven_day"):
        assert set(q[w]) == {"utilization", "resets_at", "ts", "age_s", "console", "status", "rejected_at", "stale", "expired"}


# ---------------------------------------------------------------- 契约计数

def test_contract_counts_envelopes_and_unknown_types(snap, monkeypatch):
    opened = []
    real = consoles._open_shared
    monkeypatch.setattr(consoles, "_open_shared", lambda p: (opened.append(Path(p).name), real(p))[1])
    k = consoles.contract(snap)
    assert set(opened) == {"events.jsonl"}                                           # 只数 events.jsonl, 别的一概不开
    assert k["B"]["unknown"] == {"mystery.thing": 1}                                 # 没登记的类型: 被数出来
    assert all(not k[c]["unknown"] for c in "ACE")
    assert all(e["lines"] == e["ok"] and e["unreadable"] == 0 for e in k.values())   # 合成夹具的信封都合规
    assert k["A"]["files"] == 5 and k["C"]["files"] == 1                             # A 连界面自测的样例运行也数 (都是它写的)
    # 坏信封 + 新类型: 增量地数进来 (只读新追加的那一段)
    e = Path(consoles.config_path()).parent / "c" / "office" / "runs_e" / "20261028-143926-task" / "events.jsonl"
    before = dict(k["E"])
    with open(e, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1.0, "type": "brand.new", "data": {}}) + "\n")    # 少了 seq / agent
    opened.clear()
    k2 = consoles.contract(snap)
    assert k2["E"]["lines"] == before["lines"] + 1 and k2["E"]["ok"] == before["ok"]
    assert k2["E"]["unknown"] == {"brand.new": 1}
    assert opened == ["events.jsonl"] * 2              # 没变的文件不再打开: 只有 E (刚追加) 和 C (末尾是半行, 等它写完)


def test_type_registry_is_disjoint_from_what_we_use():
    for dialect, used in consoles.USED_TYPES.items():
        assert not (used & consoles.REGISTERED_TYPES[dialect]), dialect


# ---------------------------------------------------------------- doctor 的契约一节

@pytest.fixture
def a_world(world):
    """world + A 那次被杀掉的运行留下的 transcript (按目录认到运行级)。"""
    root = world["root"]
    cwd = str(root / "conductor" / "runs" / KILLED / "workspace")
    write(world["base"] / "c--a-killed" / f"{U('a1')}.jsonl",
          [line(U("a1"), "a1a", 1020, cwd, "sdk-py"), line(U("a1"), "a1b", 1030, cwd, "sdk-py")])
    return world


def _contract(world, records=None):
    recs = records if records is not None else parser.load_records(world["base"])
    lines, warns = contract_lines(recs, consoles.snapshot(force=True))
    return "\n".join(lines), warns, recs


def test_doctor_contract_section(a_world):
    text, warns, recs = _contract(a_world)
    assert "✓ A conductor" in text and "没认出 0" in text
    assert "⚠ B missiond" in text and "mystery.thing ×1" in text                     # 没认出的类型: 点名, 并告诉你去登记
    assert "A 不记 session_id" in text and "运行 4 次, 接上 transcript 的 1 次" in text
    assert "控制台记下的会话" in text and "B 不记 usage" in text
    assert f"✓ {KILLED}" in text and "token transcript÷控制台 ×1.0000" in text       # A 的 usage 事件加总 = transcript
    assert "$ 控制台÷tokmon ×" in text and "价目不同" in text
    assert "⚠ 20261026-013942-session" in text and warns >= 2                         # C 的账本对不上 + B 的新类型
    # A: transcript 少了 (收尾的 agent 删了自己的) 是预期 -> 不报警; 多了才是账的问题
    a_recs = [r for r in recs if r.session_id == U("a1")]
    others = [r for r in recs if r.session_id != U("a1")]
    assert f"✓ {KILLED}" in text and "$ 控制台÷tokmon ×833.33 (每列 token 都对得上的运行)" in text   # 夹具账本的 $ 是随手写的
    text, _w, _ = _contract(a_world, others + a_recs[:1])
    assert f"· {KILLED}" in text and "只剩一部分" in text and f"⚠ {KILLED}" not in text
    assert "$ 不比: token 先对不上" in text                                            # 少了一半 transcript 的 $ 不拿来比价目
    assert "都对得上的运行" not in text.split("A conductor")[1].split("B missiond")[0]   # A 头一行也不拿它算 $
    text, _w, _ = _contract(a_world, others + a_recs + a_recs[:1])
    assert f"⚠ {KILLED}" in text


def test_missing_runs_dir_is_reported_not_healthy(tmp_path):
    p = tmp_path / "consoles.json"
    p.write_text(json.dumps({"consoles": [{"code": "Z", "dialect": "office", "runs_dir": str(tmp_path / "gone")}]}),
                 encoding="utf-8")
    consoles.set_config(p)
    snap = consoles.snapshot(force=True)
    assert any("runs_dir 不存在" in x for x in snap.problems)
    assert consoles.contract(snap)["Z"]["missing"]
    lines, warns = contract_lines([], snap)
    text = "\n".join(lines)
    assert "⚠ Z office" in text and "runs_dir 不存在或列不出" in text and warns >= 1
    assert str(tmp_path) not in text                                                 # 不打印路径


def test_doctor_sections_say_they_did_not_run(a_world, monkeypatch):
    def boom(*a, **k):
        raise OSError("x")
    monkeypatch.setattr(serve, "load_records", boom)
    d = serve._doctor_status(a_world["base"])
    for k in ("consoles", "contract"):
        assert d[k]["warns"] == 1 and "没跑" in d[k]["lines"][0]                       # 不能显示「✓ 健康」


def test_doctor_api_has_the_contract_section(a_world):
    d = serve._doctor_status(a_world["base"])
    assert set(d) == {"cost", "inference", "consoles", "contract"}
    assert any("mystery.thing" in ln for ln in d["contract"]["lines"]) and d["contract"]["warns"] >= 1
    assert not any("对账 (只对账" in ln for ln in d["consoles"]["lines"])        # 对账只在契约一节, 不重复


def test_doctor_page_has_the_contract_section():
    page = (Path(serve.__file__).parent / "pages" / "doctor.html").read_text(encoding="utf-8")
    assert "控制台契约" in page and "d.contract?.warns" in page
