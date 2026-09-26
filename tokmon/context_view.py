"""上下文体检 (CONTEXT_COST_PLAN S1): 每个会话的上下文有多大、每轮多少钱、缓存还剩多久 / 过期后下一轮重建要多少钱。

展示层 (与 tokens_view 同层): 只消费内核 load_records() 的结果 + pricing 的单价与倍率, 内核零改动 (I4/I6)。

口径:
- 上下文 = 一轮送进模型的全部输入 = 新输入 + 缓存读 + 缓存写 (usage 真值)。只看主线程; 子 agent 的上下文各算各的。
- 缓存有效期 = 这个会话最近一次**写**缓存用的是哪种 (1 小时 / 5 分钟)。近 14 天实测几乎全是 1 小时;
  「空了超过有效期 -> 下一轮重建」回测精确率 96.7% —— 是推断, 页面上写明。
- 上下文税 = 上下文超过 TAX_LINE 的那部分按缓存读价的钱 (这部分每轮都要再读一遍)。
- 重建 ≈ 整段上下文按缓存写价的钱; 命中 ≈ 同样多 token 按缓存读价的钱。都是 ≈ (等价成本, 不是账单)。
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median

from .pricing import CACHE_READ_MULT, CACHE_WRITE_1H_MULT, CACHE_WRITE_5M_MULT, rates_for

TAX_LINE = 150_000            # 上下文税的参考线: 近 7 天, 15 万以上那部分的读取费占总成本 27.6%
BIG_CTX = 300_000             # 「大上下文」: 近 7 天, 上下文超过 30 万的回合占总成本 48%
TTL_1H, TTL_5M = 3600, 300
TAIL = 5                      # 「每轮多少钱」取最近几轮的中位数


def context_tokens(r) -> int:
    """一轮送进模型的全部输入 (真值)。"""
    return r.input_tokens + r.cache_read + r.cache_write


def session_contexts(records) -> dict:
    """内核记录 -> {会话 id: 主线程最后一轮的上下文体检}。0 token 的占位消息 (<synthetic>) 不算「最后一轮」。"""
    by: dict = defaultdict(list)
    for r in records:
        if r.source_kind == "main" and context_tokens(r) > 0:
            by[r.session_id].append(r)
    out = {}
    for sid, rs in by.items():
        rs.sort(key=lambda r: r.timestamp)
        last = rs[-1]
        ctx = context_tokens(last)
        ttl = None
        for r in reversed(rs):                           # 最近一次写缓存用的是哪种有效期
            if r.cache_1h or r.cache_5m:
                ttl = TTL_1H if r.cache_1h >= r.cache_5m else TTL_5M
                break
        rate, _, known = rates_for(last.model)
        write_mult = CACHE_WRITE_1H_MULT if ttl == TTL_1H else CACHE_WRITE_5M_MULT
        out[sid] = {
            "ctx": ctx, "t": last.timestamp.timestamp(), "model": last.model, "known": known, "ttl": ttl,
            "per_turn": round(median(r.cost_usd for r in rs[-TAIL:]), 4),
            "tax_per_turn": round(max(0, ctx - TAX_LINE) * rate * CACHE_READ_MULT / 1e6, 4),
            "rebuild": round(ctx * rate * write_mult / 1e6, 4),
            "hit": round(ctx * rate * CACHE_READ_MULT / 1e6, 4),
        }
    return out
