"""省钱 S1 · 上下文体检 (context_view): 用真的 transcript 走一遍内核解析, 再算每个会话主线程最后一轮的上下文 /
每轮 $ / 缓存有效期 / 上下文税 / 重建与命中的账。"""

import json
from statistics import median

import pytest

from tokmon import context_view, parser
from tokmon.pricing import CACHE_READ_MULT, CACHE_WRITE_1H_MULT, CACHE_WRITE_5M_MULT, cost_usd, rates_for
from test_trace import SID, T0, asst, iso, write_jsonl


def u(inp=10, out=20, cread=0, c5m=0, c1h=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cread,
            "cache_creation_input_tokens": c5m + c1h,
            "cache_creation": {"ephemeral_5m_input_tokens": c5m, "ephemeral_1h_input_tokens": c1h}}


def say(t, i, usage, model="claude-opus-5", **kw):
    return asst(t, f"m{i}", f"r{i}", {"type": "text", "text": "x"}, usage, model=model, **kw)


def recs(tmp_path, lines, sub=None):
    proj = tmp_path / "c--Users-u--vscode-demo"
    write_jsonl(proj / f"{SID}.jsonl", lines)
    if sub:
        write_jsonl(proj / SID / "subagents" / "agent-a1.jsonl", sub)
    parser._file_cache.clear()
    return parser.load_records(tmp_path, {"main", "subagent", "workflow"}, False)


def test_last_main_turn_context_ttl_and_money(tmp_path):
    lines = [say(0, 0, u(inp=5000, c1h=40_000)),                         # 第一轮: 写 1 小时缓存
             say(60, 1, u(inp=200, cread=45_000, c1h=10_000)),
             say(120, 2, u(inp=300, cread=55_000, c1h=120_000)),
             say(180, 3, u(inp=100, cread=175_000, c1h=5_000)),
             say(240, 4, u(inp=50, cread=180_000, out=900)),
             say(300, 5, u(inp=40, cread=182_000, c1h=2_000, out=400))]
    sub = [say(310, 9, u(inp=1, cread=900_000), agent="a1")]              # 子 agent 的上下文各算各的
    cx = context_view.session_contexts(recs(tmp_path, lines, sub))[SID]
    assert cx["ctx"] == 40 + 182_000 + 2_000                              # 最后一轮主线程: 新输入 + 缓存读 + 缓存写
    assert cx["ttl"] == 3600 and cx["known"] and cx["t"] == pytest.approx(T0 + 300)
    costs = [cost_usd("claude-opus-5", l["message"]["usage"]["input_tokens"], l["message"]["usage"]["output_tokens"],
                      0, l["message"]["usage"]["cache_creation"]["ephemeral_1h_input_tokens"],
                      l["message"]["usage"]["cache_read_input_tokens"])[0] for l in lines[-5:]]
    assert cx["per_turn"] == pytest.approx(round(median(costs), 4))       # 最近 5 轮的中位数
    rate = rates_for("claude-opus-5")[0]
    assert cx["tax_per_turn"] == pytest.approx(round((184_040 - 150_000) * rate * CACHE_READ_MULT / 1e6, 4))
    assert cx["rebuild"] == pytest.approx(round(184_040 * rate * CACHE_WRITE_1H_MULT / 1e6, 4))
    assert cx["hit"] == pytest.approx(round(184_040 * rate * CACHE_READ_MULT / 1e6, 4))
    assert cx["rebuild"] / cx["hit"] == pytest.approx(20, rel=1e-3)       # 1 小时缓存: 重写是命中的 20 倍


def test_ttl_follows_latest_write_kind_and_placeholders_are_skipped(tmp_path):
    lines = [say(0, 0, u(inp=5000, c1h=30_000)),
             say(60, 1, u(inp=100, cread=35_000, c5m=8_000)),               # 最近一次写的是 5 分钟缓存
             say(90, 2, u(inp=10, cread=43_000)),                            # 只读不写: 往前找
             say(95, 3, u(inp=0, out=0), model="<synthetic>")]              # 0 token 的占位消息不算「最后一轮」
    cx = context_view.session_contexts(recs(tmp_path, lines))[SID]
    assert cx["ttl"] == 300 and cx["ctx"] == 43_010 and cx["tax_per_turn"] == 0
    rate = rates_for("claude-opus-5")[0]
    assert cx["rebuild"] == pytest.approx(round(43_010 * rate * CACHE_WRITE_5M_MULT / 1e6, 4))


def test_no_cache_writes_means_unknown_ttl(tmp_path):
    cx = context_view.session_contexts(recs(tmp_path, [say(0, 0, u(inp=9000))]))[SID]
    assert cx["ttl"] is None and cx["ctx"] == 9000


def test_unknown_model_is_flagged(tmp_path):
    cx = context_view.session_contexts(recs(tmp_path, [say(0, 0, u(inp=9000, c1h=1000), model="mystery-9")]))[SID]
    assert cx["known"] is False and cx["rebuild"] == 0 and cx["per_turn"] == 0


# ------------------------------------------------------------------ 省钱 S3: 「上下文过 30 万」事件源 (纯逻辑)

def test_context_large_fires_once_per_crossing_and_rearms():
    from tokmon.event_sources import context_source as cs
    now = 1_000_000.0
    st: dict = {}
    c = lambda ctx, age=5: {"ctx": ctx, "t": now - age, "project": "demo"}
    assert cs.derive({"s": c(250_000)}, now, st, emit=True) == []
    (ev,) = cs.derive({"s": c(310_000)}, now, st, emit=True)             # 越过 30 万: 一条
    assert ev.type == "CONTEXT_LARGE" and ev.severity == "info" and ev.payload == {"count": 310_000}
    assert ev.session == "s" and ev.project == "demo" and ev.timestamp == now - 5
    assert cs.derive({"s": c(420_000)}, now, st, emit=True) == []        # 还在线上: 不再发
    assert cs.derive({"s": c(260_000)}, now, st, emit=True) == []        # 掉到 30 万以下但没到 24 万: 不重置
    assert cs.derive({"s": c(330_000)}, now, st, emit=True) == []
    assert cs.derive({"s": c(90_000)}, now, st, emit=True) == []         # 压缩到 24 万以下: 重置
    assert len(cs.derive({"s": c(301_000)}, now, st, emit=True)) == 1     # 再越过: 算新的一次


def test_context_large_only_for_running_sessions_and_silent_when_seeding():
    from tokmon.event_sources import context_source as cs
    now = 1_000_000.0
    st: dict = {}
    assert cs.derive({"old": {"ctx": 500_000, "t": now - 3600, "project": "p"}}, now, st, emit=True) == []   # 放着没动的不吵
    st2: dict = {}
    assert cs.derive({"s": {"ctx": 500_000, "t": now, "project": "p"}}, now, st2, emit=False) == []          # 冷启动: 只记状态
    assert st2 == {"s": True}
