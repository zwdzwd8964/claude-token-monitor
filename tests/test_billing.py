"""billing (厂商用量/费用支柱) 纯函数 + 诚实纪律单测。**不发任何真实网络请求**。

盯死的护栏:
- 💣 Anthropic 的 amount 是「分」, OpenAI 是「美元」 -> 归一。搞错 = 静默 100 倍误差。
- 零外发: 没配 key -> 一个字节都不出本机。
- 绝不填 0: Anthropic 无请求数 -> None; Google 不可得 -> unavailable + 原因。
"""

import pytest

from tokmon import billing


# ---- 💣 金额归一: 100 倍坑 ----

def test_anthropic_amount_is_cents_not_dollars():
    """官方: cost_report 的 amount 是**最小货币单位(分)**的字符串。"123.45" = $1.2345。"""
    assert billing.anth_amount_usd("123.45") == pytest.approx(1.2345)
    assert billing.anth_amount_usd("100") == pytest.approx(1.00)


def test_openai_amount_is_dollars_not_cents():
    """官方: costs 的 amount = {"value": <美元浮点>} —— 直接是美元, 不能再除 100。"""
    assert billing.oai_amount_usd({"value": 0.06, "currency": "usd"}) == pytest.approx(0.06)


def test_two_providers_normalize_differently_100x():
    """同一个名义数字, 两家含义差 100 倍 —— 这正是必须归一的理由。"""
    anth = billing.anth_amount_usd("123.45")            # -> $1.2345
    oai = billing.oai_amount_usd({"value": 123.45})     # -> $123.45
    assert oai == pytest.approx(anth * 100)


def test_amount_garbage_is_zero_not_crash():
    assert billing.anth_amount_usd(None) == 0.0
    assert billing.anth_amount_usd("abc") == 0.0
    assert billing.oai_amount_usd(None) == 0.0


# ---- Anthropic 没有 input_tokens 字段 ----

def test_anth_input_total_sums_three_kinds():
    r = {"uncached_input_tokens": 1500, "cache_read_input_tokens": 200,
         "cache_creation": {"ephemeral_5m_input_tokens": 500, "ephemeral_1h_input_tokens": 1000}}
    assert billing.anth_input_total(r) == 1500 + 200 + 500 + 1000
    assert billing.anth_input_total({}) == 0          # 缺字段不崩


# ---- 零外发: 没配 key 绝不发请求 ----

def test_no_key_means_zero_egress(monkeypatch):
    monkeypatch.setattr(billing, "load_keys", lambda: {"anthropic": "", "openai": ""})

    def boom(*a, **k):
        raise AssertionError("未配置 key 时不得发起任何网络请求 (零外发被破坏)")

    monkeypatch.setattr(billing.urllib.request, "urlopen", boom)
    snap = billing.collect()
    assert snap["egress"].startswith("仅本地")
    for p in snap["providers"]:
        assert p["available"] is False
    anth = next(p for p in snap["providers"] if p["name"] == "anthropic")
    assert anth["configured"] is False and "未配置" in anth["reason"]


# ---- Google: 实证不可得, 绝不伪造 0 ----

def test_google_always_unavailable_with_reason(monkeypatch):
    monkeypatch.setattr(billing, "load_keys", lambda: {"anthropic": "", "openai": ""})
    monkeypatch.setattr(billing.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no egress")))
    g = next(p for p in billing.collect()["providers"] if p["name"] == "google")
    assert g["available"] is False
    assert g["total_cost_usd"] is None          # 不是 0 —— 是「不可得」
    assert "没有" in g["reason"] or "无任何" in g["reason"]


# ---- 解析: 用真实回包形状 (取自官方文档) ----

_ANTH_USAGE_BUCKETS = [{
    "starting_at": "2026-07-12T00:00:00Z",
    "results": [{
        "model": "claude-opus-4-6",
        "uncached_input_tokens": 1500, "cache_read_input_tokens": 200,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 300},
        "output_tokens": 500,
    }],
}]
_ANTH_COST_BUCKETS = [{
    "starting_at": "2026-07-12T00:00:00Z",
    "results": [{"amount": "250.00", "currency": "USD", "cost_type": "tokens",
                 "description": "Claude Opus Usage - Input Tokens", "model": "claude-opus-4-6"}],
}]


def test_fetch_anthropic_parses_and_never_fakes_requests(monkeypatch):
    calls = []

    def fake_paged(url, h, params):
        calls.append(url)
        return _ANTH_COST_BUCKETS if "cost_report" in url else _ANTH_USAGE_BUCKETS

    monkeypatch.setattr(billing, "_paged", fake_paged)
    p = billing.fetch_anthropic("sk-ant-admin01-x")
    assert p.available is True
    d = p.days[0]
    assert d["input"] == 1500 + 200 + 300          # 三类相加 (无 input_tokens 字段)
    assert d["output"] == 500
    assert d["requests"] is None                    # 💡 Anthropic 无请求数 -> None, 绝不填 0
    assert d["cost_usd"] == pytest.approx(2.50)     # 💣 "250.00" 分 -> $2.50
    assert p.total_cost_usd == pytest.approx(2.50)
    assert p.by_model[0]["model"] == "claude-opus-4-6"


_OAI_USAGE_BUCKETS = [{
    "start_time": 1783036800,
    "results": [{"model": "gpt-4o", "input_tokens": 150000, "output_tokens": 75000,
                 "input_cached_tokens": 25000, "num_model_requests": 500}],
}]
_OAI_COST_BUCKETS = [{
    "start_time": 1783036800,
    "results": [{"amount": {"value": 12.34, "currency": "usd"}, "line_item": "gpt-4o, inputs"}],
}]


def test_fetch_openai_parses_requests_and_dollar_amount(monkeypatch):
    def fake_paged(url, h, params):
        return _OAI_COST_BUCKETS if url.endswith("/costs") else _OAI_USAGE_BUCKETS

    monkeypatch.setattr(billing, "_paged", fake_paged)
    p = billing.fetch_openai("sk-admin-x")
    assert p.available is True
    d = p.days[0]
    assert d["input"] == 150000 and d["output"] == 75000
    assert d["requests"] == 500                      # OpenAI 有请求数
    assert d["cost_usd"] == pytest.approx(12.34)     # 美元, 不除 100
    assert p.by_model[0]["requests"] == 500


# ---- 失败降级: 单家挂了不拖垮, 且给诚实原因 ----

def test_auth_failure_degrades_with_honest_reason(monkeypatch):
    def boom(url, h, params):
        raise billing._Fail("鉴权/权限不足 (HTTP 403) —— 需要 admin key, 且账号必须是 organization")

    monkeypatch.setattr(billing, "_paged", boom)
    p = billing.fetch_anthropic("bad-key")
    assert p.available is False
    assert "admin key" in p.reason
    assert p.total_cost_usd is None                  # 不是 0 —— 是拿不到


# ---- 密钥绝不回显 ----

def test_key_status_never_echoes_key(monkeypatch):
    monkeypatch.setattr(billing, "load_keys",
                        lambda: {"anthropic": "sk-ant-admin01-SECRET", "openai": ""})
    s = billing.key_status()
    assert s == {"anthropic_configured": True, "openai_configured": False}
    assert "SECRET" not in repr(s)
