"""S0 价目对齐官方 (CONSOLES_PLAN): 每个模型每一列 = 附录 B; 分档 / fast / us / 归一化 / 家族兜底 / 加总后算价。"""

import json
import math

import pytest

from tokmon import cli, parser, pricing, serve, trace
from tokmon.pricing import cost_parts, cost_usd, cost_usd_class, normalize_model, price_class, rates

from test_trace import SID, asst, human, iso, write_jsonl

M = 1_000_000

# 附录 B (官方页 2026-10-29): 模型 -> (输入, 5m 写, 1h 写, 缓存读, 输出)。这里故意再抄一份, 就是用来对表的。
APPENDIX_B = {
    "claude-fable-5-1":  (10, 12.50, 20, 0.25, 50),
    "claude-mythos-5-1": (10, 12.50, 20, 0.25, 50),
    "claude-fable-5":    (10, 12.50, 20, 1, 50),
    "claude-mythos-5":   (10, 12.50, 20, 1, 50),
    "claude-opus-5-5":   (4, 5, 8, 0.20, 20),
    "claude-opus-5":     (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-8":   (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-7":   (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-6":   (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-5":   (5, 6.25, 10, 0.50, 25),
    "claude-sonnet-5-5": (2, 2.50, 4, 0.10, 10),
    "claude-sonnet-5":   (2, 2.50, 4, 0.20, 10),
    "claude-sonnet-4-6": (3, 3.75, 6, 0.30, 15),
    "claude-sonnet-4-5": (3, 3.75, 6, 0.30, 15),
    "claude-haiku-5-5":  (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5":  (1, 1.25, 2, 0.10, 5),
}
HAIKU_55_LONG = (0.50, 0.625, 1, 0.05, 2.50)


def _per_column(model, **kw):
    """每一列各 1M token (分开算, 保证 Haiku 5.5 都在短 prompt 档以外的列不受影响)。"""
    return (cost_usd(model, M, 0, 0, 0, 0, **kw)[0], cost_usd(model, 0, 0, M, 0, 0, **kw)[0],
            cost_usd(model, 0, 0, 0, M, 0, **kw)[0], cost_usd(model, 0, 0, 0, 0, M, **kw)[0],
            cost_usd(model, 0, M, 0, 0, 0, **kw)[0])


@pytest.mark.parametrize("model", sorted(APPENDIX_B))
def test_every_model_every_column_matches_appendix_b(model):
    want = APPENDIX_B[model]
    q = rates(model)
    assert q.known and q.basis == "table"
    assert (q.input, q.cache_write_5m, q.cache_write_1h, q.cache_read, q.output) == pytest.approx(want)
    if model == "claude-haiku-5-5":
        # 1M token 的单列已经超过 10 万 -> 走高价档; 短档用 1 万 token 验
        got = tuple(c * 100 for c in (cost_usd(model, 10_000, 0, 0, 0, 0)[0], cost_usd(model, 0, 0, 10_000, 0, 0)[0],
                                      cost_usd(model, 0, 0, 0, 10_000, 0)[0], cost_usd(model, 0, 0, 0, 0, 10_000)[0],
                                      cost_usd(model, 0, 10_000, 0, 0, 0)[0]))
        assert got == pytest.approx(want)
        # 前四列各 1M 本身就超过 10 万 -> 高价档; 输出不算 prompt, 要配一个长 prompt 才进高价档
        assert _per_column(model)[:4] == pytest.approx(HAIKU_55_LONG[:4])
        assert _per_column(model)[4] == pytest.approx(want[4])
        long_out = cost_usd(model, 0, M, 0, 0, 200_000)[0] - 200_000 * HAIKU_55_LONG[3] / M
        assert long_out == pytest.approx(HAIKU_55_LONG[4])
    else:
        assert _per_column(model) == pytest.approx(want)


def test_cache_read_multiplier_differs_by_model():
    ratio = {m: rates(m).cache_read / rates(m).input for m in ("claude-fable-5-1", "claude-opus-5-5",
                                                               "claude-sonnet-5-5", "claude-opus-5", "claude-haiku-4-5")}
    assert ratio == pytest.approx({"claude-fable-5-1": 0.025, "claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05,
                                   "claude-opus-5": 0.1, "claude-haiku-4-5": 0.1})


def test_haiku_55_tier_is_per_request_prompt_including_cache():
    # prompt = 新输入 + 缓存读 + 缓存写; 输出不算。恰好 10 万还是短档, 多 1 个就整条请求走高价档
    short, _ = cost_usd("claude-haiku-5-5", 40_000, 5_000, 10_000, 20_000, 30_000)      # 100_000
    long_, _ = cost_usd("claude-haiku-5-5", 40_001, 5_000, 10_000, 20_000, 30_000)      # 100_001
    lo = (40_000 * 0.10 + 5_000 * 0.50 + 10_000 * 0.125 + 20_000 * 0.20 + 30_000 * 0.01) / M
    hi = (40_001 * 0.50 + 5_000 * 2.50 + 10_000 * 0.625 + 20_000 * 1 + 30_000 * 0.05) / M
    assert short == pytest.approx(lo) and long_ == pytest.approx(hi)
    assert cost_usd("claude-haiku-5-5", 10, 500_000, 0, 0, 0)[0] == pytest.approx((10 * 0.10 + 500_000 * 0.50) / M)


def test_model_id_normalization_and_no_prefix_swallowing():
    assert normalize_model("claude-opus-4-5-20251101") == "claude-opus-4-5"
    assert normalize_model("claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert normalize_model("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == "claude-sonnet-4-5"
    assert normalize_model("anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert normalize_model("claude-opus-4-1@20250805") == "claude-opus-4-1"
    assert normalize_model("claude-3-5-haiku-20241022") == "claude-haiku-3-5"
    assert rates("Claude-Opus-5-5").basis == "table"
    # 精确匹配: claude-opus-5 不会吞掉将来的 claude-opus-5-6 (前缀匹配会把它错当成已知的 Opus 5)
    q = rates("claude-opus-5-6")
    assert q.basis == "family" and q.known is False
    assert (q.input, q.output) == (4.0, 20.0)                   # 按同家族最新型号 (Opus 5.5) 估算
    assert rates("claude-opus-5").input == 5.0


def test_bedrock_region_prefixes_with_hyphen():
    # us-gov. 这类带连字符的推理配置前缀也要剥掉, 否则 Haiku 4.5 会落到家族兜底 (Haiku 5.5 的价, 低 10 倍)
    assert normalize_model("us-gov.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert normalize_model("global.anthropic.claude-opus-5-5") == "claude-opus-5-5"
    assert rates("us-gov.anthropic.claude-haiku-4-5-20251001-v1:0")[:5] == pytest.approx(APPENDIX_B["claude-haiku-4-5"])


def test_odd_raw_values_never_raise():
    # transcript 原值不一定是字符串: 计价不能因此抛错 (trace 钩子直接把 usage 原值传进来, 抛错会让整个文件加载失败)
    for v in (1, 0, True, {"x": 1}, ["fast"]):
        k = price_class("claude-opus-5-5", 10, 0, 0, 0, v, v)
        assert k.startswith("claude-opus-5-5|")
        assert cost_usd_class(k, 10, 10, 0, 0, 0)[0] >= 0
        q = rates("claude-opus-5-5", speed=v, geo=v)
        assert q.input > 0
    assert rates(12345).basis == "none" and cost_usd(12345, 10, 10, 0, 0, 0) == (0.0, False)


def test_unknown_family_is_zero_and_unknown():
    for m in ("<synthetic>", "gpt-5", "", None):
        cost, known = cost_usd(m, M, M, M, M, M)
        assert cost == 0.0 and known is False
        assert rates(m).basis == "none"


def test_family_fallback_costs_but_is_flagged():
    cost, known = cost_usd("claude-sonnet-6", M, 0, 0, 0, 0)
    assert known is False and cost == pytest.approx(2.0)        # Sonnet 5.5 的输入价, 但不敢说准


def test_fast_mode_prices_stack_cache_multipliers():
    q = rates("claude-opus-5-5", speed="fast")
    assert (q.input, q.cache_write_5m, q.cache_write_1h, q.cache_read, q.output) == pytest.approx((8, 10, 16, 0.4, 40))
    assert q.known
    assert rates("claude-opus-4-8", speed="fast")[:5] == pytest.approx((10, 12.5, 20, 1.0, 50))
    # 没有 fast 价的模型 / 认不出的 speed: 照标准价, 但标为未确认
    s = rates("claude-sonnet-5-5", speed="fast")
    assert s[:5] == pytest.approx(APPENDIX_B["claude-sonnet-5-5"]) and s.known is False
    assert rates("claude-opus-5-5", speed="turbo").known is False
    assert rates("claude-opus-5-5", speed="standard").known is True


def test_us_geo_multiplies_every_column_for_46_and_later():
    q = rates("claude-opus-5-5", geo="us")
    assert q[:5] == pytest.approx(tuple(x * 1.1 for x in APPENDIX_B["claude-opus-5-5"])) and q.known
    hl = rates("claude-haiku-5-5", prompt_tokens=200_000, geo="us")          # 高价档也乘
    assert hl[:5] == pytest.approx(tuple(x * 1.1 for x in HAIKU_55_LONG))
    fu = rates("claude-opus-5-5", speed="fast", geo="us")                    # fast 与 us 叠加
    assert fu.input == pytest.approx(8 * 1.1) and fu.output == pytest.approx(40 * 1.1)
    old = rates("claude-opus-4-5", geo="us")                                 # 4.5 没有这条规则: 不乘, 标未确认
    assert old[:5] == pytest.approx(APPENDIX_B["claude-opus-4-5"]) and old.known is False
    for g in ("global", "not_available", "", None):
        assert rates("claude-opus-5-5", geo=g).known is True
    assert rates("claude-opus-5-5", geo="eu").known is False


def test_cost_parts_sum_to_cost_usd():
    args = ("claude-haiku-5-5", 70_000, 3_000, 20_000, 15_000, 5_000, 2, 1)
    for kw in ({}, {"geo": "us"}, {"speed": "fast"}):
        *parts, known = cost_parts(*args, **kw)
        full, known2 = cost_usd(*args, **kw)
        assert math.isclose(sum(parts), full, rel_tol=1e-12) and known == known2


def test_price_class_keeps_per_request_tier_when_summing():
    # 两次短请求 (各 6 万) 加总后是 12 万: 直接拿总数算会被错判成长 prompt, 按计价分类加总就不会
    reqs = [(60_000, 100, 0, 0, 0), (55_000, 200, 0, 0, 0), (150_000, 300, 0, 0, 0)]
    per_request = sum(cost_usd("claude-haiku-5-5", *r)[0] for r in reqs)
    by_class: dict = {}
    for i, o, c5, c1, cr in reqs:
        k = price_class("claude-haiku-5-5", i, c5, c1, cr)
        acc = by_class.setdefault(k, [0, 0, 0, 0, 0])
        for j, v in enumerate((i, o, c5, c1, cr)):
            acc[j] += v
    assert set(by_class) == {"claude-haiku-5-5|", "claude-haiku-5-5|long"}
    summed = sum(cost_usd_class(k, *v)[0] for k, v in by_class.items())
    assert summed == pytest.approx(per_request)
    # 旧做法 (按模型加总再算): 两次短请求合起来过了 10 万, 被整体按高价档算 -> 正好贵 5 倍
    shorts = sum(cost_usd("claude-haiku-5-5", *r)[0] for r in reqs[:2])
    naive = cost_usd("claude-haiku-5-5", 115_000, 300, 0, 0, 0)[0]
    assert naive == pytest.approx(shorts * 5)


def test_price_class_encodes_only_price_changing_conditions():
    assert price_class("claude-opus-5-5", 10, 0, 0, 0, "standard", "global") == "claude-opus-5-5|"
    assert price_class("claude-opus-5-5", 10, 0, 0, 0, "fast", "us") == "claude-opus-5-5|speed=fast,geo=us"
    assert price_class("claude-opus-5-5", 10, 0, 0, 500_000) == "claude-opus-5-5|"        # 不分档的模型没有 long
    c, k = cost_usd_class("claude-opus-5-5|speed=fast,geo=us", M, 0, 0, 0, 0)
    assert c == pytest.approx(8 * 1.1) and k
    assert cost_usd_class("claude-opus-5", M, 0, 0, 0, 0) == (pytest.approx(5.0), True)    # 不带条件的纯模型名也接受
    assert cost_usd_class("<synthetic>|", 5, 5, 0, 0, 0) == (0.0, False)


def test_rates_for_compat_wrapper():
    assert pricing.rates_for("claude-opus-5-5") == (4.0, 20.0, True)
    assert pricing.rates_for("<synthetic>") == (0.0, 0.0, False)


# ---------------------------------------------------------------- 价目快照 (pricing --json)

def test_snapshot_is_complete_and_serializable():
    snap = pricing.snapshot("9.9.9")
    assert snap["schema"] == 1 and snap["fetched"] == pricing.PRICING_FETCHED and snap["tokmon_version"] == "9.9.9"
    assert snap["source"].startswith("https://platform.claude.com/")
    for m, want in APPENDIX_B.items():
        e = snap["models"][m]
        assert (e["input"], e["cache_write_5m"], e["cache_write_1h"], e["cache_read"], e["output"]) == pytest.approx(want)
    assert snap["models"]["claude-haiku-5-5"]["long_prompt"]["over_tokens"] == 100_000
    assert snap["models"]["claude-opus-5-5"]["fast"]["cache_read"] == pytest.approx(0.4)
    assert snap["models"]["claude-opus-4-5"]["us_geo_mult"] is None
    json.dumps(snap)


def test_cli_pricing_json_is_ascii_and_out_writes_file(tmp_path, capsys):
    assert cli.main(["pricing", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()                                        # Windows 管道按本地代码页编码, 纯 ASCII 才不会乱
    assert json.loads(out)["models"]["claude-opus-5-5"]["input"] == 4.0
    dest = tmp_path / "sub" / "pricing.json"
    assert cli.main(["pricing", "--out", str(dest)]) == 0
    assert json.loads(dest.read_text(encoding="utf-8"))["fetched"] == pricing.PRICING_FETCHED
    assert not (tmp_path / "sub" / "pricing.json.tmp").exists()
    assert cli.main(["pricing"]) == 0 and "claude-haiku-5-5" in capsys.readouterr().out


# ---------------------------------------------------------------- 接到内核与 trace

def _write_session(tmp_path, lines):
    proj = tmp_path / "projects" / "c--Users-u--vscode-demo"
    write_jsonl(proj / f"{SID}.jsonl", lines)
    return proj / f"{SID}.jsonl"


def _u(inp, out, cread, speed=None, geo=None):
    d = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cread, "cache_creation_input_tokens": 0,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    if speed:
        d["speed"] = speed
    if geo:
        d["inference_geo"] = geo
    return d


def test_parser_carries_speed_and_geo_into_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "_file_cache", {})
    _write_session(tmp_path, [
        asst(1, "m1", "r1", {"type": "text", "text": "a"}, _u(M, 0, 0, "standard", "global"), model="claude-opus-5-5"),
        asst(2, "m2", "r2", {"type": "text", "text": "b"}, _u(M, 0, 0, "fast", "global"), model="claude-opus-5-5"),
        asst(3, "m3", "r3", {"type": "text", "text": "c"}, _u(M, 0, 0, "standard", "us"), model="claude-opus-5-5"),
        asst(4, "m4", "r4", {"type": "text", "text": "d"}, _u(M, 0, 0), model="claude-opus-5-5"),
    ])
    recs = {r.message_id: r for r in parser.load_records(tmp_path / "projects")}
    assert (recs["m1"].speed, recs["m1"].geo) == ("standard", "global")
    assert recs["m1"].cost_usd == pytest.approx(4.0)
    assert recs["m2"].cost_usd == pytest.approx(8.0) and recs["m2"].known_price
    assert recs["m3"].cost_usd == pytest.approx(4.4)
    assert (recs["m4"].speed, recs["m4"].geo, recs["m4"].cost_usd) == ("", "", pytest.approx(4.0))


def test_serve_injects_price_class_so_workflow_cost_keeps_haiku_tier(tmp_path, monkeypatch):
    for name in ("_FILES", "_TASK_INDEX", "_AGENT_IDX", "_SCRIPTS", "_META", "_BUILT", "_CLAIMED", "_STATS", "_STAMPS"):
        monkeypatch.setattr(trace, name, {})
    assert trace.price_class is not None                       # serve 在 import 时注入 (本测试文件 import 了 serve)
    main = _write_session(tmp_path, [
        human(0, "P1", "看一下"),
        asst(1, "m1", "r1", {"type": "text", "text": "a"}, _u(60_000, 100, 0), model="claude-haiku-5-5"),
        asst(2, "m2", "r2", {"type": "text", "text": "b"}, _u(55_000, 200, 0), model="claude-haiku-5-5"),
        asst(3, "m3", "r3", {"type": "text", "text": "c"}, _u(1_000, 300, 149_000), model="claude-haiku-5-5"),
    ])
    tasks, _ = trace.session_tasks(main)
    tok = trace.build_task(tasks[0])["summary"]["tokens"]
    assert set(tok["by_model"]) == {"claude-haiku-5-5|", "claude-haiku-5-5|long"}
    want = (cost_usd("claude-haiku-5-5", 60_000, 100, 0, 0, 0)[0] + cost_usd("claude-haiku-5-5", 55_000, 200, 0, 0, 0)[0]
            + cost_usd("claude-haiku-5-5", 1_000, 300, 0, 0, 149_000)[0])
    cost, unpriced = serve._wf_price(tok["by_model"])
    assert cost == pytest.approx(round(want, 4)) and unpriced is False
    assert iso(0)                                              # (夹具时间轴与 test_trace 共用)


# ---------------------------------------------------------------- 审查补的回归 (多视角静态审查 2026-10-29)

# 官方页另列、附录 B 没有的几行 (老型号, 仍在官方表上)
OFFICIAL_EXTRA = {
    "claude-opus-4-1":  (15, 18.75, 30, 1.50, 75),
    "claude-opus-4":    (15, 18.75, 30, 1.50, 75),
    "claude-sonnet-4":  (3, 3.75, 6, 0.30, 15),
    "claude-haiku-3-5": (0.80, 1, 1.60, 0.08, 4),
}


@pytest.mark.parametrize("model", sorted(OFFICIAL_EXTRA))
def test_official_rows_outside_appendix_b(model):
    q = rates(model)
    assert q.known and q[:5] == pytest.approx(OFFICIAL_EXTRA[model])


def _ur(ts, **kw):
    from tokmon.records import UsageRecord
    d = dict(project="p", session_id="s", model="claude-opus-5-5", source_kind="main", input_tokens=0, output_tokens=0,
             cache_5m=0, cache_1h=0, cache_read=0, web_search=0, web_fetch=0, cost_usd=0.0, known_price=True,
             message_id=f"m{ts.timestamp()}", request_id="r")
    d.update(kw)
    return UsageRecord(timestamp=ts, **d)


def test_context_view_uses_the_models_own_cache_rates():
    from datetime import datetime, timedelta, timezone
    from tokmon import context_view
    t0 = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
    rs = [_ur(t0, input_tokens=5000, cache_1h=40_000),
          _ur(t0 + timedelta(minutes=1), input_tokens=40, cache_read=182_000, cache_1h=2_000, message_id="b")]
    cx = context_view.session_contexts(rs)["s"]
    assert cx["ctx"] == 184_040 and cx["known"] and cx["ttl"] == 3600
    assert cx["hit"] == pytest.approx(round(184_040 * 0.20 / M, 4))           # Opus 5.5 缓存读 0.05×
    assert cx["rebuild"] == pytest.approx(round(184_040 * 8 / M, 4))
    assert cx["tax_per_turn"] == pytest.approx(round((184_040 - 150_000) * 0.20 / M, 4))
    assert cx["rebuild"] / cx["hit"] == pytest.approx(40, rel=1e-2)            # 以前全按 0.1× 算是 20


def test_savings_marks_use_per_request_rates():
    from datetime import datetime, timezone
    from tokmon import tokens_view
    t0 = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
    a = _ur(t0, cache_read=200_000)                                           # Opus 5.5: 0.05×
    h = _ur(t0, model="claude-haiku-5-5", cache_read=200_000, message_id="h")  # 20 万 prompt -> 高价档 0.05/M
    marks = tokens_view.savings_marks([a, h])
    assert marks[id(a)][0] == pytest.approx(50_000 * 0.20 / M)
    assert marks[id(h)][0] == pytest.approx(50_000 * 0.05 / M)


def test_cube_splits_confirmed_rows_and_flags_models():
    from datetime import datetime, timezone
    from tokmon import tokens_view
    t0 = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
    ok = _ur(t0, input_tokens=1000, cost_usd=0.004)
    odd = _ur(t0, input_tokens=1000, cost_usd=0.004, known_price=False, speed="turbo", message_id="odd")
    fam = _ur(t0, model="claude-opus-9", input_tokens=10, cache_read=500, cost_usd=0.0001, known_price=False,
              message_id="fam")
    cb = tokens_view.build([ok, odd, fam], None, [ok, odd, fam])
    rows = [dict(zip(cb["cols"], r)) for r in cb["rows"]]
    mods = {m["id"]: m for m in cb["models"]}
    opus = [r for r in rows if cb["models"][r["m"]]["id"] == "claude-opus-5-5"]
    assert len(opus) == 2 and sorted(r["un"] for r in opus) == [0, 1]       # 确认 / 未确认分两行, 不混
    assert mods["claude-opus-5-5"]["priced"] is False                         # 有一条条件没规则的记录 -> 打 *
    assert mods["claude-opus-5-5"]["cr"] == 0.05 and mods["claude-opus-9"]["cr"] == 0.05   # 估算的也给倍率
    assert mods["claude-opus-9"]["priced"] is False
    assert cb["mult"]["cr"] == [0.05]


def test_doctor_separates_estimated_unknown_and_condition_cases(tmp_path):
    from tokmon.doctor import build_lines, scan
    proj = tmp_path / "c--Users-u--vscode-demo"
    write_jsonl(proj / f"{SID}.jsonl", [
        asst(1, "m1", "r1", {"type": "text", "text": "a"}, _u(100, 10, 0, "fast"), model="claude-opus-5-5"),
        asst(2, "m2", "r2", {"type": "text", "text": "b"}, _u(100, 10, 0, "fast"), model="claude-sonnet-5-5"),
        asst(3, "m3", "r3", {"type": "text", "text": "c"}, _u(100, 10, 0), model="claude-opus-9"),
        asst(4, "m4", "r4", {"type": "text", "text": "d"}, _u(100, 10, 0), model="weird-model"),
    ])
    rep = scan(tmp_path)
    assert rep.price_conditions == {"speed=fast": 1}                          # Opus 5.5 的 fast: 已按官方加价计
    assert rep.unconfirmed_conditions == {("claude-sonnet-5-5", "speed=fast"): (1, 110)}
    assert rep.unknown_basis == {"claude-opus-9": "family", "weird-model": "none"}
    lines, warns = build_lines(rep)
    text = "\n".join(lines)
    assert "按同家族最新型号估算" in text and "认不出的模型 · 按 0 计" in text and "计价条件没有官方规则" in text
    assert warns >= 3


def test_spend_today_star_ignores_zero_token_placeholders(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    proj = tmp_path / "c--Users-u--vscode-demo"

    def line(mid, model, u):
        d = asst(0, mid, "r" + mid, {"type": "text", "text": "x"}, u, model=model)
        d["timestamp"] = now_iso
        return d

    write_jsonl(proj / f"{SID}.jsonl", [line("a", "claude-opus-5-5", _u(100, 10, 0)),
                                         line("b", "<synthetic>", _u(0, 0, 0))])
    monkeypatch.setattr(parser, "_file_cache", {})
    monkeypatch.setattr(serve, "_RECS_VIEW", {"t": 0.0, "key": None, "v": None})
    assert serve._spend_today(tmp_path)["unpriced"] is False                  # 只有 0 token 占位消息: 金额不受影响
    write_jsonl(proj / "other.jsonl", [line("c", "weird-model", _u(100, 10, 0))])
    monkeypatch.setattr(serve, "_RECS_VIEW", {"t": 0.0, "key": None, "v": None})
    assert serve._spend_today(tmp_path)["unpriced"] is True


def test_baseline_only_unconfirmed_does_not_star_current_model():
    from datetime import datetime, timezone
    from tokmon import tokens_view
    t0 = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
    old = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
    cur = _ur(t0, model="claude-sonnet-5-5", input_tokens=10, cost_usd=0.00002)
    odd_prev = _ur(old, model="claude-sonnet-5-5", input_tokens=10, cost_usd=0.00002, known_price=False, speed="fast",
                   message_id="p1")
    gone = _ur(old, model="claude-opus-4-5", input_tokens=10, cost_usd=0.00005, known_price=False, geo="us",
               message_id="p2")
    cb = tokens_view.build([cur], [odd_prev, gone], [cur, odd_prev, gone])
    mods = {m["id"]: m for m in cb["models"]}
    assert mods["claude-sonnet-5-5"]["priced"] is True          # 本窗的记录都确认了: 上一周期那条不该给它打 *
    assert mods["claude-opus-4-5"]["priced"] is False           # 只在上一周期出现的模型, 按上一周期判
