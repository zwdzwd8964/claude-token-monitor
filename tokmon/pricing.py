"""模型定价表与成本计算。

单价来源: Anthropic claude-api 参考 (cached 2026-06)。单位 = USD / 1M tokens。
缓存与 server-tool 计费规则也在此集中, 改价只需动这一个文件。

注意: 若你用的是 Max/Pro 订阅, 这里算出的 $ 是『等价用量价值』, 不是真实扣费。
"""

from __future__ import annotations

# 缓存相对 base input 单价的倍率 (来自 prompt-caching 规则)
CACHE_WRITE_5M_MULT = 1.25   # ephemeral 5m 写入
CACHE_WRITE_1H_MULT = 2.0    # ephemeral 1h 写入
CACHE_READ_MULT = 0.10       # 缓存命中读取

# 按模型家族定价 (input, output) per 1M tokens —— 覆盖各 4.x 小版本。
FAMILY_PRICING: dict[str, tuple[float, float]] = {
    "fable": (10.0, 50.0),
    "mythos": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

# 精确 id 覆盖 (留作未来某个具体版本单独调价用)
EXACT_PRICING: dict[str, tuple[float, float]] = {}

# Server 工具计费
WEB_SEARCH_USD_PER_REQUEST = 10.0 / 1000  # $10 / 1000 次搜索
WEB_FETCH_USD_PER_REQUEST = 0.0           # 按 token 计费, 无单独的 per-request 费


def rates_for(model: str | None) -> tuple[float, float, bool]:
    """返回 (input_rate, output_rate, known)。未知模型返回 (0, 0, False)。"""
    if not model:
        return (0.0, 0.0, False)
    m = model.lower()
    if m in EXACT_PRICING:
        i, o = EXACT_PRICING[m]
        return (i, o, True)
    for fam, (i, o) in FAMILY_PRICING.items():
        if fam in m:
            return (i, o, True)
    return (0.0, 0.0, False)


def cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_5m: int,
    cache_1h: int,
    cache_read: int,
    web_search: int = 0,
    web_fetch: int = 0,
) -> tuple[float, bool]:
    """计算单条 usage 的成本。返回 (usd, known_price)。"""
    in_rate, out_rate, known = rates_for(model)
    token_cost = (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_5m * in_rate * CACHE_WRITE_5M_MULT
        + cache_1h * in_rate * CACHE_WRITE_1H_MULT
        + cache_read * in_rate * CACHE_READ_MULT
    ) / 1_000_000
    tool_cost = (
        web_search * WEB_SEARCH_USD_PER_REQUEST
        + web_fetch * WEB_FETCH_USD_PER_REQUEST
    )
    return token_cost + tool_cost, known
