"""/tokens 页的数据立方 (display layer, 不碰内核)。

一次请求返回一份压缩的聚合: 按 (UTC 小时, 本地日期, 本地小时, 项目, 模型, 来源, 会话) 分组,
每组带 token 分项 + 等价成本 + **成本拆分** (input / output / 缓存写 / 缓存读 / 工具)。
前端拿到后所有切片、联动筛选都在本地算, 点一下立刻出结果, 不再请求服务器。

诚实约定 (NORTH_STAR §2.1):
- 每组的 `c` 就是内核 `UsageRecord.cost_usd` 的和 —— 与 /api/summary 同源同口径;
  拆分直接用 `pricing.cost_parts()` (与 `pricing.cost_usd` 同一段算式, 逐请求分档 / fast / us 都在里面),
  所以各分项之和 == `c` (浮点误差内)。定价仍只在 pricing.py (I3), 这里不复制任何单价、倍率。
- 未知单价的消息单独计数 (`un` 条 / `ut` token), 页面据此打 `*` 与斜线纹, 而不是悄悄按 0 混进去。
- 本地日期/小时取自 `UsageRecord.timestamp` (parser 已转本地时区), 与 build_summary 的 by_day 一致。
- 省钱 (CONTEXT_COST_PLAN S2) 的三个量都是**已有成本的一部分**, 不是另加的钱:
  `tx` 上下文税 = 这一轮缓存读里超过 TAX_LINE 的那部分按缓存读价的钱 (<= `ccr`);
  `rb` 重建多花 = 主线程空了超过缓存有效期后, 这一轮缓存写的钱 - 同样多 token 按缓存读价的钱 (<= `ccw`); `rn` 次数。
  重建要看上一轮 (可能在窗口外), 所以在**全部已加载记录**上按会话排好顺序判, 再汇到窗口里。
"""

from __future__ import annotations

from datetime import datetime

from .aggregate import group_by
from .context_view import TAX_LINE, TTL_1H, TTL_5M, context_tokens
from .pricing import (
    CACHE_WRITE_1H_MULT,
    CACHE_WRITE_5M_MULT,
    WEB_SEARCH_USD_PER_REQUEST,
    rates,
)
from .pricing import cost_parts as _price_parts

# 行 = 7 个键 + 18 个量。列名随响应下发 (`cols`), 前端按名取, 不靠位置。
KEY_COLS = ["e", "d", "h", "p", "m", "s", "x"]
VAL_COLS = ["n", "in", "out", "cr", "cw", "ws", "wf", "c", "ci", "co", "ccw", "ccr", "ct", "un", "ut", "tx", "rb", "rn"]
COLS = KEY_COLS + VAL_COLS
_COST_COLS = {"c", "ci", "co", "ccw", "ccr", "ct", "tx", "rb"}
REBUILD_MIN_CTX = 20_000      # 太小的上下文不算重建 (与回测口径一致)
SOURCES = ["main", "subagent", "workflow"]      # 固定顺序 -> 前端配色稳定


def cost_parts(rec) -> tuple[float, float, float, float, float]:
    """把一条记录的等价成本拆成 (input, output, 缓存写, 缓存读, 工具)。就是 pricing.cost_usd 那段算式。"""
    return _price_parts(rec.model, rec.input_tokens, rec.output_tokens, rec.cache_5m, rec.cache_1h, rec.cache_read,
                        rec.web_search, rec.web_fetch, rec.speed, rec.geo)[:5]


def _quote(rec):
    """这条记录 (一次请求) 实际适用的单价: 分档按它自己的上下文, fast / us 按它自己的 usage。"""
    return rates(rec.model, context_tokens(rec), rec.speed, rec.geo)


def savings_marks(records) -> dict:
    """全部记录 -> {id(记录): (上下文税 $, 重建多花 $, 重建次数)}; 两项都是 0 的不登记。

    重建只在主线程上判: 同一会话按时间排好, 这一轮与上一轮隔了超过缓存有效期 (看上一轮为止最近一次写的是哪种),
    且这一轮缓存写 >= 上下文的一半 (整段重写了)。子 agent 各自的第一轮本来就要写缓存, 不算重建。"""
    marks: dict = {}
    for r in records:
        if r.cache_read > TAX_LINE:
            cr_rate = _quote(r).cache_read
            if cr_rate:
                marks[id(r)] = [(r.cache_read - TAX_LINE) * cr_rate / 1_000_000, 0.0, 0]
    seqs: dict = {}
    for r in records:
        if r.source_kind == "main":
            seqs.setdefault(r.session_id, []).append(r)
    for rs in seqs.values():
        rs.sort(key=lambda r: r.timestamp)
        ttl = None
        for a, b in zip(rs, rs[1:]):
            if a.cache_1h or a.cache_5m:
                ttl = TTL_1H if a.cache_1h >= a.cache_5m else TTL_5M
            ctx = context_tokens(b)
            if ttl is None or ctx < REBUILD_MIN_CTX or b.cache_write < 0.5 * ctx:
                continue
            if (b.timestamp - a.timestamp).total_seconds() <= ttl:
                continue
            q = _quote(b)
            extra = (b.cache_5m * q.cache_write_5m + b.cache_1h * q.cache_write_1h
                     - b.cache_write * q.cache_read) / 1_000_000
            m = marks.setdefault(id(b), [0.0, 0.0, 0])
            m[1] += max(0.0, extra)
            m[2] += 1
    return marks


def _group(records, marks: dict | None = None) -> dict:
    """(epoch 小时, 本地日期, 本地小时, 项目, 模型, 来源, 会话) -> 18 个量。"""
    marks = marks or {}
    out: dict[tuple, list] = {}
    for r in records:
        ts = r.timestamp
        # 末位按「单价是否确认」再分一刀 (不下发): 同一格里确认 / 未确认的不混在一行, 前端按行算单价时分子分母都精确
        k = (int(ts.timestamp() // 3600), ts.date(), ts.hour, r.project, r.model, r.source_kind, r.session_id,
             bool(r.known_price))
        a = out.get(k)
        if a is None:
            a = out[k] = [0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0]
        ci, co, ccw, ccr, ct = cost_parts(r)
        a[0] += 1
        a[1] += r.input_tokens
        a[2] += r.output_tokens
        a[3] += r.cache_read
        a[4] += r.cache_write
        a[5] += r.web_search
        a[6] += r.web_fetch
        a[7] += r.cost_usd
        a[8] += ci
        a[9] += co
        a[10] += ccw
        a[11] += ccr
        a[12] += ct
        if not r.known_price:
            a[13] += 1
            a[14] += r.total_tokens
        m = marks.get(id(r))
        if m:
            a[15] += m[0]
            a[16] += m[1]
            a[17] += m[2]
    return out


def _rank(records, keyfn, score) -> list:
    """按 score 降序、再按名字的稳定排序键列表。"""
    g = group_by(records, keyfn)
    return sorted(g, key=lambda k: (-score(g[k]), str(k)))


def build(rows, prev_rows, all_records) -> dict:
    """纯函数: 当前窗记录 + 上一周期记录 (可为 None) + 本次加载的全部记录 -> 立方 dict。

    维度字典的顺序:
    - projects / sessions: 当前窗成本降序, 只在上一周期出现的排在后面 (索引只在本次响应内有效,
      前端按名字存筛选状态, 不存索引);
    - models: 按**本次加载的全部记录**的 token 量排名 (`rank`), 与时间窗无关 -> 换窗口/筛选不改颜色
      (「颜色跟着实体走, 不跟排名走」); 同时给出 `priced` (单价是否已知)。
    """
    marks = savings_marks(all_records)             # 重建要看窗口外的上一轮 -> 在全部记录上判
    cur = _group(rows, marks)
    prev = _group(prev_rows, marks) if prev_rows is not None else {}

    cur_cost = group_by(rows, lambda r: r.project)
    prev_cost = group_by(prev_rows or [], lambda r: r.project)
    projects = sorted(cur_cost, key=lambda k: (-cur_cost[k].cost, -cur_cost[k].total_tokens, k))
    projects += sorted((k for k in prev_cost if k not in cur_cost),
                       key=lambda k: (-prev_cost[k].cost, -prev_cost[k].total_tokens, k))
    # 项目的全局名次 (全部已加载记录的成本): 给前端做稳定配色用
    gproj = {k: i for i, k in enumerate(_rank(all_records, lambda r: r.project, lambda a: a.cost))}

    mrank = _rank(all_records, lambda r: r.model, lambda a: a.total_tokens)
    mpos = {m: i for i, m in enumerate(mrank)}
    models = sorted({k[4] for k in cur} | {k[4] for k in prev}, key=lambda m: (mpos.get(m, 1 << 30), m))

    # 「未确认」按当前窗口判; 只在上一周期出现的模型才看上一周期 (别让 10 天前的一条记录给本周的模型打 *)
    cur_models = {r.model for r in rows}
    unconfirmed = ({r.model for r in rows if not r.known_price}
                   | {r.model for r in prev_rows or [] if not r.known_price and r.model not in cur_models})
    sess_cur = group_by(rows, lambda r: r.session_id)
    sess_prev = group_by(prev_rows or [], lambda r: r.session_id)
    sessions = sorted(sess_cur, key=lambda k: (-sess_cur[k].cost, -sess_cur[k].total_tokens, k))
    sessions += sorted((k for k in sess_prev if k not in sess_cur),
                       key=lambda k: (-sess_prev[k].cost, -sess_prev[k].total_tokens, k))
    # 会话 -> 所属项目 (消息最多的那个) + 本窗内首末时间 (只在上一周期出现的取上一周期)
    span: dict[str, list] = {}
    for src in (rows, prev_rows or []):
        seen_here: dict[str, list] = {}
        for r in src:
            s = seen_here.get(r.session_id)
            t = r.timestamp.timestamp()
            if s is None:
                seen_here[r.session_id] = [t, t, {r.project: 1}]
            else:
                s[0] = min(s[0], t)
                s[1] = max(s[1], t)
                s[2][r.project] = s[2].get(r.project, 0) + 1
        for sid, s in seen_here.items():
            span.setdefault(sid, s)

    days = sorted({k[1] for k in cur} | {k[1] for k in prev})
    di = {d: i for i, d in enumerate(days)}
    pi = {p: i for i, p in enumerate(projects)}
    mi = {m: i for i, m in enumerate(models)}
    si = {s: i for i, s in enumerate(SOURCES)}
    xi = {x: i for i, x in enumerate(sessions)}

    def encode(groups: dict) -> list:
        out = []
        for k in sorted(groups, key=lambda k: (k[0], k[1], k[2], pi[k[3]], mi[k[4]], k[5], xi[k[6]], k[7])):
            src = k[5] if k[5] in si else "main"      # 未知来源归 main 兜底 (discover 只产出这三种)
            vals = groups[k]
            enc = [round(v, 10) if VAL_COLS[j] in _COST_COLS else v for j, v in enumerate(vals)]
            out.append([k[0], di[k[1]], k[2], pi[k[3]], mi[k[4]], si[src], xi[k[6]]] + enc)
        return out

    def proj_of(sid: str) -> int:
        cnt = span[sid][2]
        return pi[max(cnt, key=lambda p: (cnt[p], p))]

    return {
        "cols": COLS,
        "days": [{"d": d.isoformat(), "wd": d.weekday()} for d in days],
        "projects": [{"id": p, "g": gproj.get(p, len(gproj))} for p in projects],
        # priced: 模型本身有官方价, 且窗口里它的记录都确认了 (fast / us 在它上面没有规则的记录会让它变成未确认)
        # cr: 这个模型缓存读相对输入价的倍率 (按同家族估算的也给; 认不出家族的是 None) —— 页面按当前看到的行拼「计价」说明
        "models": [{"id": m, "priced": rates(m).known and m not in unconfirmed, "rank": mpos.get(m, len(mpos)),
                    "cr": _cr_mult(m)} for m in models],
        "sources": list(SOURCES),
        "sessions": [{"id": x, "p": proj_of(x), "t0": round(span[x][0], 3), "t1": round(span[x][1], 3)}
                     for x in sessions],
        "rows": encode(cur),
        "prev_rows": encode(prev) if prev_rows is not None else None,
        # 页面解释「缓存读 = input 单价 × 0.05」时用的倍率 —— 从 pricing.py 读, 前端不再写死一份 (I3 定价集中)。
        # 缓存读倍率按模型不同: 给出本窗里出现的、有单价的模型各自的倍率 (去重升序)。
        "mult": {"cr": sorted({x for x in (_cr_mult(m) for m in {k[4] for k in cur}) if x is not None}),
                 "cw5": CACHE_WRITE_5M_MULT, "cw1": CACHE_WRITE_1H_MULT,
                 "ws": WEB_SEARCH_USD_PER_REQUEST},
        "save": {"line": TAX_LINE},                  # 省钱卡片的参考线, 前端不另写一份
    }



def _cr_mult(model):
    """模型 -> 缓存读相对输入价的倍率 (标准条件; 分档 / fast / us 都不改变这个比)。认不出家族的 -> None。"""
    q = rates(model)
    return round(q.cache_read / q.input, 4) if q.input else None


def span_of(cutoff: datetime | None, now: datetime, records) -> dict:
    """当前窗的时间跨度 (给前端画稠密的日期/小时轴): 起点 = cutoff, 全部窗口 = 最早一条记录。"""
    lo = cutoff
    if lo is None:
        lo = min((r.timestamp for r in records), default=now)
    off = now.utcoffset()
    return {"lo": round(lo.timestamp(), 3), "hi": round(now.timestamp(), 3),
            "d0": lo.date().isoformat(), "d1": now.date().isoformat(),
            "tz_offset_s": int(off.total_seconds()) if off else 0}
