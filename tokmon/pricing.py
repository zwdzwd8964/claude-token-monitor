"""模型定价表与成本计算。

单价来源: Anthropic 官方价目页 https://platform.claude.com/docs/en/about-claude/pricing
(抓取 2026-10-29)。单位 = USD / 1M tokens。改价、加模型只动这一个文件 (I3)。

注意: 若你用的是 Max/Pro 订阅, 这里算出的 $ 是『等价用量价值』, 不是真实扣费。

计价规则 (都来自上面那页):
- 按模型 id 定价, 五列: 输入 / 5 分钟缓存写 / 1 小时缓存写 / 缓存读 / 输出。
  缓存写 = 输入价 × 1.25 (5 分钟) / × 2 (1 小时), 所有模型一样; **缓存读倍率按模型不同**
  (Fable 5.1 / Mythos 5.1 是 0.025×, Opus 5.5 / Sonnet 5.5 是 0.05×, 其余 0.1×), 所以表里直接写五列, 不再用全局倍率。
- 模型 id 先归一化 (去日期后缀 / `[1m]` / Bedrock、Vertex 的前后缀) 再**精确**查表。
  不用前缀匹配: `claude-opus-5` 会吞掉将来的 `claude-opus-5-6`, 把它错当成已知价。
- Haiku 5.5 按**单次请求**的 prompt 长度分档: prompt = 新输入 + 缓存读 + 缓存写, 超过 10 万整条请求走高价档。
- fast mode (usage.speed == "fast"): 输入 / 输出换成 fast 价, 缓存倍率叠加在 fast 的输入价上。
- 美国本土推理 (usage.inference_geo == "us"): Claude 4.6 及以后的模型所有 token 类别 × 1.1。
- 不在表里的型号 (新版本, 或官方页已不再列出的老型号如 claude-3-7-sonnet / claude-3-opus): 按同家族最新型号估算,
  并标 known=False (页面打 *, doctor 列出来) —— 老型号这样估会偏差很大, 但官方页上没有它们的价, 宁可标「未确认」也不凭记忆写死。
  认不出家族的按 0 计。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple

PRICING_SOURCE_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICING_FETCHED = "2026-10-29"

# 缓存写相对输入价的倍率 (所有模型相同)。缓存读的倍率按模型不同, 已经写进下面的表里。
CACHE_WRITE_5M_MULT = 1.25   # ephemeral 5m 写入
CACHE_WRITE_1H_MULT = 2.0    # ephemeral 1h 写入


class Rates(NamedTuple):
    """一个模型的五列单价 (USD / 1M tokens)。"""
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


class Quote(NamedTuple):
    """某次请求实际适用的单价 + 这个单价有多可信。"""
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float
    known: bool      # 单价来自官方表, 且计价条件 (speed / geo) 都认得
    basis: str       # "table" 官方表 | "family" 按同家族最新型号估算 | "none" 认不出, 按 0


def _r(i: float, cw5: float, cw1: float, cr: float, o: float) -> Rates:
    return Rates(float(i), float(cw5), float(cw1), float(cr), float(o))


# 官方价目 (附录 B)。键 = 归一化后的模型 id。
MODEL_PRICING: dict[str, Rates] = {
    "claude-fable-5-1":  _r(10, 12.50, 20, 0.25, 50),
    "claude-mythos-5-1": _r(10, 12.50, 20, 0.25, 50),
    "claude-fable-5":    _r(10, 12.50, 20, 1, 50),
    "claude-mythos-5":   _r(10, 12.50, 20, 1, 50),
    "claude-opus-5-5":   _r(4, 5, 8, 0.20, 20),
    "claude-opus-5":     _r(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-8":   _r(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-7":   _r(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-6":   _r(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-5":   _r(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-1":   _r(15, 18.75, 30, 1.50, 75),
    "claude-opus-4":     _r(15, 18.75, 30, 1.50, 75),
    "claude-sonnet-5-5": _r(2, 2.50, 4, 0.10, 10),
    "claude-sonnet-5":   _r(2, 2.50, 4, 0.20, 10),
    "claude-sonnet-4-6": _r(3, 3.75, 6, 0.30, 15),
    "claude-sonnet-4-5": _r(3, 3.75, 6, 0.30, 15),
    "claude-sonnet-4":   _r(3, 3.75, 6, 0.30, 15),
    "claude-haiku-5-5":  _r(0.10, 0.125, 0.20, 0.01, 0.50),     # prompt <= 10 万; 超过走 LONG_PROMPT_PRICING
    "claude-haiku-4-5":  _r(1, 1.25, 2, 0.10, 5),
    "claude-haiku-3-5":  _r(0.80, 1, 1.60, 0.08, 4),
}

# 旧命名 -> 表里的键
MODEL_ALIASES: dict[str, str] = {
    "claude-3-5-haiku": "claude-haiku-3-5",
}

# 按单次请求 prompt 长度分档的模型: 键 -> (阈值, 超过阈值时的五列单价)。prompt = 新输入 + 缓存读 + 缓存写。
LONG_PROMPT_PRICING: dict[str, tuple[int, Rates]] = {
    "claude-haiku-5-5": (100_000, _r(0.50, 0.625, 1, 0.05, 2.50)),
}

# fast mode: 键 -> (输入, 输出)。缓存倍率叠加在 fast 输入价上 (缓存读用该模型自己的倍率)。
FAST_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (8.0, 40.0),
    "claude-opus-5":   (10.0, 50.0),
    "claude-opus-4-8": (10.0, 50.0),
}

# inference_geo == "us" 的加价: 只对 Claude 4.6 及以后的模型 (含 Haiku 5.5 的高价档)。
US_GEO_MULT = 1.1
US_GEO_MODELS = frozenset(k for k in MODEL_PRICING if k not in {
    "claude-opus-4-5", "claude-opus-4-1", "claude-opus-4",
    "claude-sonnet-4-5", "claude-sonnet-4",
    "claude-haiku-4-5", "claude-haiku-3-5",
})

# 认不出的新版本按同家族最新型号估算 (known=False)。
FAMILY_FALLBACK: dict[str, str] = {
    "fable": "claude-fable-5-1",
    "mythos": "claude-mythos-5-1",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

# 不改变单价的计价条件取值 (doctor 也用这两组, 判断哪些记录带了非标准条件)
STANDARD_SPEEDS = frozenset({"", "standard"})
STANDARD_GEOS = frozenset({"", "global", "not_available"})

# Server 工具计费
WEB_SEARCH_USD_PER_REQUEST = 10.0 / 1000  # $10 / 1000 次搜索
WEB_FETCH_USD_PER_REQUEST = 0.0           # 按 token 计费, 无单独的 per-request 费

_RE_SUFFIX_BRACKET = re.compile(r"\[[^\]]*\]$")            # claude-opus-5-5[1m]
_RE_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}(?:-[a-z]+)?\.)?anthropic\.")   # us. / us-gov. / global.anthropic.claude-…
_RE_BEDROCK_SUFFIX = re.compile(r"-v\d+(?::\d+)?$")         # …-v1:0
_RE_DATE_SUFFIX = re.compile(r"-\d{8}$")                    # …-20251101


def _s(v) -> str:
    """transcript 里的原值 -> 字符串 (None -> "")。非字符串的怪值也不能让计价抛错, lru_cache 也只收可哈希的参数。"""
    return "" if v is None else v if isinstance(v, str) else str(v)


def normalize_model(model: str | None) -> str:
    """模型 id -> 价目表的键 (去掉日期、上下文标记、云厂商前后缀)。认不出也原样 (小写) 返回。"""
    m = _s(model).strip().lower()
    m = _RE_SUFFIX_BRACKET.sub("", m)
    m = m.split("@", 1)[0]                                      # Vertex: claude-opus-4-1@20250805
    m = _RE_BEDROCK_PREFIX.sub("", m)
    m = _RE_BEDROCK_SUFFIX.sub("", m)
    m = _RE_DATE_SUFFIX.sub("", m)
    return MODEL_ALIASES.get(m, m)


@lru_cache(maxsize=1024)          # 模型 id 只有寥寥几种, 而每条记录要算好几次: 别每次都跑正则
def _resolve(model: str | None) -> tuple[str | None, str]:
    """-> (价目表的键, basis)。basis: table / family / none。"""
    key = normalize_model(model)
    if key in MODEL_PRICING:
        return key, "table"
    for fam, latest in FAMILY_FALLBACK.items():
        if fam in key:
            return latest, "family"
    return None, "none"


def _is_long(key: str | None, prompt_tokens: int) -> bool:
    tier = LONG_PROMPT_PRICING.get(key) if key else None
    return bool(tier) and prompt_tokens > tier[0]


@lru_cache(maxsize=4096)
def _quote(model: str | None, long: bool, speed: str | None, geo: str | None) -> Quote:
    key, basis = _resolve(model)
    if key is None:
        return Quote(0.0, 0.0, 0.0, 0.0, 0.0, False, "none")
    known = basis == "table"
    base = MODEL_PRICING[key]
    if long and key in LONG_PROMPT_PRICING:
        base = LONG_PROMPT_PRICING[key][1]
    i, cw5, cw1, cr, o = base

    sp = (speed or "").lower()
    if sp == "fast" and key in FAST_PRICING:
        cr_mult = cr / i
        i, o = FAST_PRICING[key]
        cw5, cw1, cr = i * CACHE_WRITE_5M_MULT, i * CACHE_WRITE_1H_MULT, i * cr_mult
    elif sp not in STANDARD_SPEEDS:
        known = False                       # 这个模型没有 fast 价 / 认不出的 speed: 按标准价, 但不敢说准

    g = (geo or "").lower()
    if g == "us" and key in US_GEO_MODELS:
        i, cw5, cw1, cr, o = (x * US_GEO_MULT for x in (i, cw5, cw1, cr, o))
    elif g not in STANDARD_GEOS:
        known = False                       # 老模型没有 us 加价规则 / 认不出的 geo: 不乘, 也不敢说准
    return Quote(i, cw5, cw1, cr, o, known, basis)


@lru_cache(maxsize=4096)
def _tiers(model, speed, geo) -> tuple:
    """(短 prompt 的单价, 长 prompt 的单价或 None, 分档阈值或 None) —— rates() 的热路径一次缓存命中就够。"""
    model, speed, geo = _s(model), _s(speed), _s(geo)
    key, _ = _resolve(model)
    tier = LONG_PROMPT_PRICING.get(key) if key else None
    return (_quote(model, False, speed, geo), _quote(model, True, speed, geo) if tier else None,
            tier[0] if tier else None)


def rates(model: str | None, prompt_tokens: int = 0, speed: str | None = None, geo: str | None = None) -> Quote:
    """一次请求适用的五列单价。prompt_tokens = 这次请求的新输入 + 缓存读 + 缓存写 (只有分档模型用得上)。
    每条记录都要调好几次 (成本拆分 / 省钱标记 / 上下文体检): 走一层缓存, 不可哈希的怪值才先转字符串。"""
    try:
        short, long_, th = _tiers(model, speed, geo)
    except TypeError:
        short, long_, th = _tiers(_s(model), _s(speed), _s(geo))
    return long_ if th is not None and prompt_tokens > th else short


def rates_for(model: str | None) -> tuple[float, float, bool]:
    """兼容旧调用: (输入价, 输出价, known), 按标准条件、短 prompt。新代码用 rates()。"""
    q = rates(model)
    return (q.input, q.output, q.known)


def cost_parts(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_5m: int,
    cache_1h: int,
    cache_read: int,
    web_search: int = 0,
    web_fetch: int = 0,
    speed: str | None = None,
    geo: str | None = None,
) -> tuple[float, float, float, float, float, bool]:
    """**单次请求**的成本拆分 -> (输入, 输出, 缓存写, 缓存读, 工具, known)。各项之和就是 cost_usd。"""
    q = rates(model, input_tokens + cache_5m + cache_1h + cache_read, speed, geo)
    return _parts(q, input_tokens, output_tokens, cache_5m, cache_1h, cache_read, web_search, web_fetch)


def _parts(q: Quote, input_tokens, output_tokens, cache_5m, cache_1h, cache_read, web_search, web_fetch):
    ci = input_tokens * q.input / 1_000_000
    co = output_tokens * q.output / 1_000_000
    ccw = (cache_5m * q.cache_write_5m + cache_1h * q.cache_write_1h) / 1_000_000
    ccr = cache_read * q.cache_read / 1_000_000
    ct = web_search * WEB_SEARCH_USD_PER_REQUEST + web_fetch * WEB_FETCH_USD_PER_REQUEST
    return ci, co, ccw, ccr, ct, q.known


def cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_5m: int,
    cache_1h: int,
    cache_read: int,
    web_search: int = 0,
    web_fetch: int = 0,
    speed: str | None = None,
    geo: str | None = None,
) -> tuple[float, bool]:
    """计算**单次请求**的成本。返回 (usd, known_price)。

    分档按传进来的 token 判断。已经加总的多次请求请用 price_class() + cost_usd_class(), 否则会被错判成长 prompt。"""
    ci, co, ccw, ccr, ct, known = cost_parts(model, input_tokens, output_tokens, cache_5m, cache_1h, cache_read,
                                             web_search, web_fetch, speed, geo)
    return ci + co + ccw + ccr + ct, known


# ---------------------------------------------------------------- 计价分类 (给要先加总再算价的调用方)

def price_class(model: str | None, input_tokens: int, cache_5m: int, cache_1h: int, cache_read: int,
                speed: str | None = None, geo: str | None = None) -> str:
    """把影响单价的条件编进一个键: `模型|条件`。同一个键下的多次请求可以先加总 token 再算价。

    条件只写会改变单价的: long (分档模型的长 prompt)、speed=… (非标准)、geo=… (非标准)。"""
    model, speed, geo = _s(model), _s(speed).lower(), _s(geo).lower()
    key, _ = _resolve(model)
    mods = []
    if _is_long(key, input_tokens + cache_5m + cache_1h + cache_read):
        mods.append("long")
    if speed not in STANDARD_SPEEDS:
        mods.append("speed=" + speed)
    if geo not in STANDARD_GEOS:
        mods.append("geo=" + geo)
    return f"{model or 'unknown'}|{','.join(mods)}"


def cost_usd_class(cls: str, input_tokens: int, output_tokens: int, cache_5m: int, cache_1h: int,
                   cache_read: int) -> tuple[float, bool]:
    """price_class 的键 (也接受不带条件的模型名) + 加总后的 token -> (usd, known)。分档由键决定, 不看 token 总量。"""
    model, _, mods = str(cls).partition("|")
    flags = [m for m in mods.split(",") if m]
    speed = next((m[6:] for m in flags if m.startswith("speed=")), None)
    geo = next((m[4:] for m in flags if m.startswith("geo=")), None)
    q = _quote(model, "long" in flags, speed, geo)
    ci, co, ccw, ccr, ct, known = _parts(q, input_tokens, output_tokens, cache_5m, cache_1h, cache_read, 0, 0)
    return ci + co + ccw + ccr + ct, known


# ---------------------------------------------------------------- 价目快照 (pricing --json, 给控制台读)

def snapshot(version: str = "") -> dict:
    """完整价目快照: 控制台读它就不用再自己维护一份价目表 (CONSOLES_PLAN 决策 3A)。"""
    def cols(r) -> dict:
        return {"input": round(r[0], 6), "cache_write_5m": round(r[1], 6), "cache_write_1h": round(r[2], 6),
                "cache_read": round(r[3], 6), "output": round(r[4], 6)}

    models = {}
    for key, r in MODEL_PRICING.items():
        e = cols(r)
        if key in LONG_PROMPT_PRICING:
            th, lr = LONG_PROMPT_PRICING[key]
            e["long_prompt"] = {"over_tokens": th, **cols(lr)}
        if key in FAST_PRICING:
            q = _quote(key, False, "fast", None)
            e["fast"] = cols(q)
        e["us_geo_mult"] = US_GEO_MULT if key in US_GEO_MODELS else None
        models[key] = e
    return {
        "schema": 1,
        "source": PRICING_SOURCE_URL,
        "fetched": PRICING_FETCHED,
        "tokmon_version": version,
        "unit": "USD per 1M tokens",
        "models": models,
        "aliases": dict(MODEL_ALIASES),
        "family_fallback": dict(FAMILY_FALLBACK),
        "web_search_usd_per_request": WEB_SEARCH_USD_PER_REQUEST,
        "web_fetch_usd_per_request": WEB_FETCH_USD_PER_REQUEST,
        "rules": {
            "model_id": "先归一化 (小写; 去掉 [..] 后缀、@.. 后缀、Bedrock 的 <区域>.anthropic. 前缀和 -vN:M 后缀、-YYYYMMDD 日期), 再查 aliases, 再精确匹配 models 的键",
            "unknown_version": "名字里含 family_fallback 的家族名 -> 按该家族最新型号估算, 标为未确认; 都不含 -> 0",
            "prompt_tokens": "单次请求的 input + cache_read + cache_write; 超过 long_prompt.over_tokens 时整条请求按 long_prompt 计价",
            "fast": "usage.speed == 'fast' 时用 fast 那组价 (缓存倍率已叠加)",
            "us_geo": "usage.inference_geo == 'us' 时所有列 × us_geo_mult (为 null 的模型没有这条规则)",
        },
    }
