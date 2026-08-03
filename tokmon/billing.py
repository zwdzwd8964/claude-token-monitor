"""厂商 API 用量与费用监控 (第 4 数据源) —— `/billing` 页的数据源。

[PROVIDER_BILLING_PLAN.md] 的 B1 落地。**平台第二个「受控破例」出站**(第一个是 M3 通知):
主动带凭据去厂商云拉数据。**默认全关: 没配 key -> 一个字节都不出本机。**

**P4 / I2 铁律**: **绝不 import 成本内核** (parser/pricing/aggregate/records)。
它的数据来自**网络**、不是本机 transcript, 与内核完全不同源 —— 混进 aggregate 会同时破坏
I2(内核纯函数、无 I/O)、I4(UsageRecord 是唯一真相载体) 与 local-first。
**只 import stdlib** (urllib/json/threading/...)。零额外依赖 (照 notify.py 调 Telegram 的先例)。

**诚实纪律 (原则 1: 宁可显示未知, 也不要悄悄估错)** —— 以下均为 2026-07-13 官方文档实证:
- **Google: 没有任何官方 usage/cost API** (AI Studio/Gemini 的 `AIzaSy` key 拿不到用量也拿不到费用;
  真实费用只能走 BigQuery 账单导出) -> **永久标 unavailable + 原因**, 绝不显示伪造的 0。
- **Anthropic 无请求数** (usage API 不给 request count) -> `requests=None`, UI 显「—」, **绝不填 0**。
- **两家费用都只有日粒度** (bucket_width=1d only)。
- 💣 **Anthropic 的 cost `amount` 是「分」的十进制字符串** ("123.45" = $1.23);
  **OpenAI 是「美元」浮点** (`amount.value`)。**统一成 USD float —— 搞错就是静默的 100 倍误差。**
  `anth_amount_usd()` 有单测钉死。
- 数据会事后修正 -> **每次重取尾部窗口**, 不做只追加。
- 单家故障/限流 -> 那一家标「暂不可得 + 原因」, **不拖垮整页** (原则 5)。

**密钥**: `~/.tokmon/providers.json` (0600) 或环境变量。**绝不回显** —— 任何 API 只报「已配置/未配置」。
⚠️ 这些是 **org-admin 级**凭据 (Anthropic 无只读档: 同一把 key 能踢组织成员、停用别人的 API key)。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_KEY_PATH = Path.home() / ".tokmon" / "providers.json"
_TIMEOUT = 20.0
_REFRESH_S = 300.0        # 5 分钟一刷 (Anthropic 数据延迟约 5min; 官方要求持续轮询 <=1/min, 这里远低于)
_WINDOW_DAYS = 7
_MAX_PAGES = 20           # 分页硬上限, 防翻页失控

# Google: 实证结论 —— 拿不到。写死在这里, 免得未来的自己又去找不存在的端点。
_GOOGLE_REASON = ("Google 无任何官方 usage/cost API: AI Studio/Gemini 的 API key 既拿不到用量也拿不到费用; "
                  "真实费用只能走 BigQuery 账单导出 (需 billing admin + 建 dataset + BQ 查询成本 + 最长 5 天回填)。")


# ---------- 密钥 (绝不回显) ----------

def load_keys() -> dict:
    """环境变量优先, 其次 ~/.tokmon/providers.json。缺失 -> 空串 (= 未配置 = 零外发)。"""
    keys = {"anthropic": os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip(),
            "openai": os.environ.get("OPENAI_ADMIN_KEY", "").strip()}
    try:
        if _KEY_PATH.exists():
            d = json.loads(_KEY_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for k in ("anthropic", "openai"):
                    if not keys[k] and isinstance(d.get(k), str):
                        keys[k] = d[k].strip()
    except (OSError, ValueError):
        pass
    return keys


def save_keys(anthropic: str | None = None, openai: str | None = None) -> None:
    """写 ~/.tokmon/providers.json, 0600 原子写 (照 control_token 先例)。空串 = 清除。"""
    cur = {}
    try:
        if _KEY_PATH.exists():
            d = json.loads(_KEY_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                cur = {k: v for k, v in d.items() if isinstance(v, str)}
    except (OSError, ValueError):
        pass
    if anthropic is not None:
        cur["anthropic"] = anthropic.strip()
    if openai is not None:
        cur["openai"] = openai.strip()
    try:
        _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------- 金额归一 (💣 100 倍坑, 单测钉死) ----------

def anth_amount_usd(amount) -> float:
    """Anthropic cost_report 的 `amount` 是**最小货币单位(分)**的十进制字符串。
    "123.45" -> $1.2345。**必须除以 100** —— 不除就是静默的 100 倍误差。"""
    try:
        return float(amount) / 100.0
    except (TypeError, ValueError):
        return 0.0


def oai_amount_usd(amount) -> float:
    """OpenAI costs 的 `amount` 是 {"value": <美元浮点>, "currency": "usd"} —— 直接就是美元, 不除。"""
    if isinstance(amount, dict):
        try:
            return float(amount.get("value") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(amount)
    except (TypeError, ValueError):
        return 0.0


def anth_input_total(r: dict) -> int:
    """Anthropic 用量回包**没有 `input_tokens` 字段** —— 输入总量要自己加三类。"""
    cc = r.get("cache_creation") or {}
    return int((r.get("uncached_input_tokens") or 0)
               + (r.get("cache_read_input_tokens") or 0)
               + (cc.get("ephemeral_5m_input_tokens") or 0)
               + (cc.get("ephemeral_1h_input_tokens") or 0))


# ---------- HTTP (纯 stdlib; 只有配了 key 才会被调到) ----------

class _Fail(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _get(url: str, headers: dict, params: list) -> dict:
    """GET + JSON。params 是 (k,v) 列表 (支持重复 key 的数组参数)。失败 -> _Fail(诚实原因)。"""
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _Fail(f"鉴权/权限不足 (HTTP {e.code}) —— 需要 admin key, 且账号必须是 organization")
        if e.code == 429:
            raise _Fail("被限流 (HTTP 429), 稍后重试")
        raise _Fail(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise _Fail(f"网络不可达: {getattr(e, 'reason', '')}")
    except (ValueError, OSError) as e:
        raise _Fail(f"回包异常: {type(e).__name__}")


def _paged(url: str, headers: dict, params: list) -> list:
    """翻页收集所有 bucket (两家形状一致: data[] + has_more + next_page)。"""
    out, page, n = [], None, 0
    while n < _MAX_PAGES:
        p = list(params) + ([("page", page)] if page else [])
        d = _get(url, headers, p)
        out.extend(d.get("data") or [])
        if not d.get("has_more") or not d.get("next_page"):
            break
        page = d["next_page"]
        n += 1
    return out


# ---------- 时间窗 ----------

def _window(days: int = _WINDOW_DAYS):
    """UTC 日对齐的 [start, end) —— 两家的桶都按 UTC 天对齐。重取尾部窗口 (数据会事后修正)。"""
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    return start, end


def _day(epoch_or_iso) -> str:
    if isinstance(epoch_or_iso, (int, float)):
        return datetime.fromtimestamp(epoch_or_iso, timezone.utc).strftime("%Y-%m-%d")
    try:
        return str(epoch_or_iso)[:10]
    except Exception:
        return "?"


# ---------- 统一模型 ----------

@dataclass
class Provider:
    name: str
    configured: bool = False
    available: bool = False
    reason: str = ""
    days: list = field(default_factory=list)      # [{date, cost_usd|None, input, cached_input, output, requests|None}]
    by_model: list = field(default_factory=list)  # [{model, input, cached_input, output, requests|None, cost_usd|None}]
    total_cost_usd: float | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "configured": self.configured, "available": self.available,
                "reason": self.reason, "days": self.days, "by_model": self.by_model,
                "total_cost_usd": self.total_cost_usd}


def _acc(bucket: dict, model: str, inp: int, cached: int, out: int, req):
    m = bucket.setdefault(model, {"model": model, "input": 0, "cached_input": 0,
                                  "output": 0, "requests": None, "cost_usd": None})
    m["input"] += inp
    m["cached_input"] += cached
    m["output"] += out
    if req is not None:
        m["requests"] = (m["requests"] or 0) + req


# ---------- Anthropic ----------

_ANTH_USAGE = "https://api.anthropic.com/v1/organizations/usage_report/messages"
_ANTH_COST = "https://api.anthropic.com/v1/organizations/cost_report"


def fetch_anthropic(key: str, days: int = _WINDOW_DAYS) -> Provider:
    p = Provider("anthropic", configured=True)
    start, end = _window(days)
    h = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    try:
        # 用量: 按天 + 按模型
        buckets = _paged(_ANTH_USAGE, h, [
            ("starting_at", iso(start)), ("ending_at", iso(end)),
            ("bucket_width", "1d"), ("group_by[]", "model"), ("limit", "31"),
        ])
        by_day, by_model = {}, {}
        for b in buckets:
            d = _day(b.get("starting_at"))
            row = by_day.setdefault(d, {"date": d, "cost_usd": None, "input": 0,
                                        "cached_input": 0, "output": 0, "requests": None})
            for r in (b.get("results") or []):
                inp = anth_input_total(r)                       # 无 input_tokens, 自己加
                cached = int(r.get("cache_read_input_tokens") or 0)
                out = int(r.get("output_tokens") or 0)
                row["input"] += inp
                row["cached_input"] += cached
                row["output"] += out
                # requests 保持 None —— Anthropic 用量 API 不给请求数 (绝不填 0)
                _acc(by_model, r.get("model") or "?", inp, cached, out, None)
        # 费用: 只有 1d; group_by=description 才能拿到 model 解析字段
        cbuckets = _paged(_ANTH_COST, h, [
            ("starting_at", iso(start)), ("ending_at", iso(end)),
            ("group_by[]", "description"), ("limit", "31"),
        ])
        total = 0.0
        for b in cbuckets:
            d = _day(b.get("starting_at"))
            row = by_day.setdefault(d, {"date": d, "cost_usd": None, "input": 0,
                                       "cached_input": 0, "output": 0, "requests": None})
            for r in (b.get("results") or []):
                usd = anth_amount_usd(r.get("amount"))          # 💣 分 -> 元
                row["cost_usd"] = (row["cost_usd"] or 0.0) + usd
                total += usd
                m = r.get("model")
                if m and m in by_model:
                    by_model[m]["cost_usd"] = (by_model[m]["cost_usd"] or 0.0) + usd
        p.available = True
        p.days = sorted(by_day.values(), key=lambda x: x["date"], reverse=True)
        p.by_model = sorted(by_model.values(), key=lambda x: -(x["input"] + x["output"]))
        p.total_cost_usd = round(total, 4)
    except _Fail as e:
        p.available, p.reason = False, e.reason
    except Exception as e:                       # 降级: 单家挂了不拖垮整页
        p.available, p.reason = False, f"解析失败: {type(e).__name__}"
    return p


# ---------- OpenAI ----------

_OAI_USAGE = "https://api.openai.com/v1/organization/usage/completions"
_OAI_COST = "https://api.openai.com/v1/organization/costs"


def fetch_openai(key: str, days: int = _WINDOW_DAYS) -> Provider:
    p = Provider("openai", configured=True)
    start, end = _window(days)
    h = {"Authorization": f"Bearer {key}"}
    st, et = str(int(start.timestamp())), str(int(end.timestamp()))   # unix 秒 (与 Anthropic 不同)
    try:
        buckets = _paged(_OAI_USAGE, h, [
            ("start_time", st), ("end_time", et),
            ("bucket_width", "1d"), ("group_by", "model"), ("limit", "31"),
        ])
        by_day, by_model = {}, {}
        for b in buckets:
            d = _day(b.get("start_time"))
            row = by_day.setdefault(d, {"date": d, "cost_usd": None, "input": 0,
                                        "cached_input": 0, "output": 0, "requests": None})
            for r in (b.get("results") or []):
                inp = int(r.get("input_tokens") or 0)
                cached = int(r.get("input_cached_tokens") or 0)
                out = int(r.get("output_tokens") or 0)
                req = int(r.get("num_model_requests") or 0)     # OpenAI 有请求数
                row["input"] += inp
                row["cached_input"] += cached
                row["output"] += out
                row["requests"] = (row["requests"] or 0) + req
                _acc(by_model, r.get("model") or "?", inp, cached, out, req)
        cbuckets = _paged(_OAI_COST, h, [
            ("start_time", st), ("end_time", et),
            ("bucket_width", "1d"), ("group_by", "line_item"), ("limit", "180"),
        ])
        total = 0.0
        for b in cbuckets:
            d = _day(b.get("start_time"))
            row = by_day.setdefault(d, {"date": d, "cost_usd": None, "input": 0,
                                        "cached_input": 0, "output": 0, "requests": None})
            for r in (b.get("results") or []):
                usd = oai_amount_usd(r.get("amount"))           # 已是美元, 不除
                row["cost_usd"] = (row["cost_usd"] or 0.0) + usd
                total += usd
        p.available = True
        p.days = sorted(by_day.values(), key=lambda x: x["date"], reverse=True)
        p.by_model = sorted(by_model.values(), key=lambda x: -(x["input"] + x["output"]))
        p.total_cost_usd = round(total, 4)
    except _Fail as e:
        p.available, p.reason = False, e.reason
    except Exception as e:
        p.available, p.reason = False, f"解析失败: {type(e).__name__}"
    return p


# ---------- 快照 + pump ----------

_lock = threading.Lock()
_snap: dict = {"updated_epoch": None, "window_days": _WINDOW_DAYS, "providers": [],
               "egress": "仅本地(未配置任何 key)"}
_thread: threading.Thread | None = None
_stop = threading.Event()


def collect(days: int = _WINDOW_DAYS) -> dict:
    """采一帧。**没配 key 的 provider 绝不发起任何网络请求** (零外发)。"""
    keys = load_keys()
    provs: list[Provider] = []

    if keys["anthropic"]:
        provs.append(fetch_anthropic(keys["anthropic"], days))
    else:
        provs.append(Provider("anthropic", configured=False, available=False,
                              reason="未配置 admin key (设 ANTHROPIC_ADMIN_KEY 或 ~/.tokmon/providers.json)"))

    if keys["openai"]:
        provs.append(fetch_openai(keys["openai"], days))
    else:
        provs.append(Provider("openai", configured=False, available=False,
                              reason="未配置 admin key (设 OPENAI_ADMIN_KEY 或 ~/.tokmon/providers.json)"))

    # Google: 实证 —— 没有任何官方 usage/cost API。永久 unavailable, 绝不伪造 0。
    provs.append(Provider("google", configured=False, available=False, reason=_GOOGLE_REASON))

    any_key = bool(keys["anthropic"] or keys["openai"])
    out = {"updated_epoch": int(time.time()), "window_days": days,
           "providers": [p.to_dict() for p in provs],
           "egress": "厂商 API 出站(已配置)" if any_key else "仅本地(未配置任何 key)"}
    with _lock:
        _snap.clear()
        _snap.update(out)
    return out


def status() -> dict:
    """只读快照。**绝不回显 key** —— 只有 configured 布尔。"""
    with _lock:
        return dict(_snap)


def key_status() -> dict:
    k = load_keys()
    return {"anthropic_configured": bool(k["anthropic"]), "openai_configured": bool(k["openai"])}


def _loop(days: float):
    while not _stop.wait(_REFRESH_S):
        try:
            collect(int(days))
        except Exception:
            pass          # 单次失败不拖垮 pump


def start_pump(days: int = _WINDOW_DAYS):
    """懒启动 (由 serve 调一次)。**未配 key 时 collect 不发任何请求**, 所以起 pump 也是零外发。"""
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    try:
        collect(days)
    except Exception:
        pass
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(days,), name="mc-billing-pump", daemon=True)
    _thread.start()
    return _thread
