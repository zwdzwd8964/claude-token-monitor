"""解析 JSONL transcript -> UsageRecord, 并做跨文件去重。

项目身份从每个文件里的真实 `cwd` 还原 (见 project.py); cwd 缺失时回退到目录名解码。
性能: 每个文件按 (mtime, size) 缓存解析结果, 未变动的文件在 watch 轮询时不重复读盘。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .discovery import discover, friendly_project
from .pricing import cost_usd
from .project import workspace_identity
from .records import UsageRecord

# path(str) -> (signature, [UsageRecord])
_file_cache: dict[str, tuple[tuple[float, int], list[UsageRecord]]] = {}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()  # 转本地时区, 便于按『天』归桶
    except ValueError:
        return None


def _read_records(path: Path, raw_dir: str, kind: str) -> list[UsageRecord]:
    # 一次扫描: 跟踪『当前 cwd』(会话中途 cd 会变), 给每条 assistant 用量记录
    # 捕获它自己生效时的 cwd —— 逐记录归属, 避免整文件被第一个 cwd 错算。
    current_cwd: str | None = None
    payloads: list[tuple[dict, str | None]] = []
    try:
        f = open(path, "r", encoding="utf-8")
    except OSError:
        return []
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("cwd"):
                current_cwd = d["cwd"]  # assistant 行也自带 cwd, 此处会先更新再入队
            if d.get("type") == "assistant" and (d.get("message") or {}).get("usage"):
                payloads.append((d, current_cwd))

    records: list[UsageRecord] = []
    for d, rec_cwd in payloads:
        # 逐记录解析项目身份: 优先该记录生效的真实 cwd, 否则回退编码目录名。
        wid = workspace_identity(rec_cwd)
        if wid is not None:
            project, subpath, under = wid.project, wid.subpath, wid.under_anchor
        else:
            project, subpath, under = friendly_project(raw_dir), "", False
        cwd = rec_cwd or ""

        msg = d.get("message") or {}
        usage = msg.get("usage") or {}
        ts = _parse_ts(d.get("timestamp"))
        if ts is None:
            continue

        model = msg.get("model") or "unknown"
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)

        cc = usage.get("cache_creation") or {}
        cache_5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
        cache_1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
        if not cc:
            cache_5m = int(usage.get("cache_creation_input_tokens") or 0)

        stu = usage.get("server_tool_use") or {}
        web_search = int(stu.get("web_search_requests") or 0)
        web_fetch = int(stu.get("web_fetch_requests") or 0)

        cost, known = cost_usd(
            model, input_tokens, output_tokens,
            cache_5m, cache_1h, cache_read, web_search, web_fetch,
        )
        records.append(UsageRecord(
            timestamp=ts,
            project=project,
            session_id=d.get("sessionId") or path.stem,
            model=model,
            source_kind=kind,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_5m=cache_5m,
            cache_1h=cache_1h,
            cache_read=cache_read,
            web_search=web_search,
            web_fetch=web_fetch,
            cost_usd=cost,
            known_price=known,
            message_id=str(msg.get("id") or ""),
            request_id=str(d.get("requestId") or ""),
            cwd=cwd,
            subpath=subpath,
            under_vscode=under,
        ))
    return records


def parse_file(path: Path, raw_dir: str, kind: str) -> list[UsageRecord]:
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return []
    sig = (st.st_mtime, st.st_size)
    cached = _file_cache.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    records = _read_records(path, raw_dir, kind)
    _file_cache[key] = (sig, records)
    return records


def load_records(
    base: Path,
    include_kinds: set[str] | None = None,
    vscode_only: bool = False,
) -> list[UsageRecord]:
    """扫描全部 session 文件, 去重后返回 UsageRecord 列表。

    去重键 = (message_id, request_id), 避免同一条 assistant 消息被重复计数。
    vscode_only=True 时只保留位于 .vscode 之下的会话。
    """
    seen: dict[tuple[str, str], UsageRecord] = {}
    for path, raw_dir, kind in discover(base):
        if include_kinds is not None and kind not in include_kinds:
            continue
        for rec in parse_file(path, raw_dir, kind):
            seen[rec.dedup_key] = rec
    records = list(seen.values())
    if vscode_only:
        records = [r for r in records if r.under_vscode]
    return records
