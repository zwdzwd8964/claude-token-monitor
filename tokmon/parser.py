"""解析 JSONL transcript -> UsageRecord, 并做跨文件去重。

项目身份从每个文件里的真实 `cwd` 还原 (见 project.py); cwd 缺失时回退到目录名解码。
性能: 每个文件按 (mtime, size) 缓存解析结果, 未变动的文件在 watch 轮询时不重复读盘。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .discovery import discover, friendly_project, fs_path
from .pricing import cost_usd
from .project import TEMP_PROJECT, in_temp_dir, scratchpad_parent, workspace_identity
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
    current_ep = ""                     # entrypoint (claude-vscode / sdk-py / cli …): 每行都带, 同样跟踪当前值
    payloads: list[tuple[dict, str | None, str]] = []
    try:
        f = open(fs_path(path), "r", encoding="utf-8")
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
            if d.get("entrypoint"):
                current_ep = str(d["entrypoint"])
            if d.get("type") == "assistant" and (d.get("message") or {}).get("usage"):
                payloads.append((d, current_cwd, current_ep))

    records: list[UsageRecord] = []
    for d, rec_cwd, rec_ep in payloads:
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
        # 计价条件 (fast mode / us 推理): 取值只有寥寥几种, intern 后几十万条记录共用同一个字符串
        speed = sys.intern(str(usage.get("speed") or ""))
        geo = sys.intern(str(usage.get("inference_geo") or ""))

        cost, known = cost_usd(
            model, input_tokens, output_tokens,
            cache_5m, cache_1h, cache_read, web_search, web_fetch, speed, geo,
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
            speed=speed,
            geo=geo,
            entrypoint=sys.intern(rec_ep),
        ))
    return records


def parse_file(path: Path, raw_dir: str, kind: str) -> list[UsageRecord]:
    key = str(path)
    try:
        st = os.stat(fs_path(path))
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
    newer_than: float | None = None,
) -> list[UsageRecord]:
    """扫描全部 session 文件, 去重后返回 UsageRecord 列表。

    去重键 = (message_id, request_id), 避免同一条 assistant 消息被重复计数。
    vscode_only=True 时只保留位于 .vscode 之下的会话。

    newer_than (epoch 秒, 可选): 按文件 mtime 预筛, 只读可能落在窗口内的文件 (见 discovery.discover)。
    **只有「我只要最近 N 天」的调用方才传**; 要全量口径的 (/tokens 的配色名次、savings_marks 的
    窗口外上一轮、report --since all) 必须不传, 否则会少算。
    """
    seen: dict[tuple[str, str], UsageRecord] = {}
    for path, raw_dir, kind in discover(base, newer_than=newer_than):
        if include_kinds is not None and kind not in include_kinds:
            continue
        for rec in parse_file(path, raw_dir, kind):
            seen[rec.dedup_key] = rec
    records = _scratchpad_to_parent(list(seen.values()))
    if vscode_only:
        records = [r for r in records if r.under_vscode]
    return records


def _scratchpad_to_parent(records: list[UsageRecord]) -> list[UsageRecord]:
    """在别的会话的 scratchpad 里跑的会话 -> 项目归给父会话的项目 (见 project.scratchpad_parent);
    其余在系统临时目录里跑的 (pytest 的 tmp_path、演练沙盒…) -> 归到「临时目录」, 不按叶子目录名造假项目。

    不改缓存里的记录 (`_file_cache` 共享这些对象): 要改的那几条换成副本。父会话的项目取它非 scratchpad 记录里
    最多的那个; 父会话自己也在别人的 scratchpad 里就再往上找 (最多 5 层); 父会话不在这批记录里 (被 newer_than
    筛掉 / 已删) 就按父会话的编码目录名兜底。"""
    refs: dict[str, object] = {}
    temps: set[str] = set()
    hit = False
    for r in records:
        if r.cwd and r.cwd not in refs:
            refs[r.cwd] = scratchpad_parent(r.cwd)
            if refs[r.cwd] is None and in_temp_dir(r.cwd):
                temps.add(r.cwd)
            hit = hit or refs[r.cwd] is not None or r.cwd in temps
    if not hit:
        return records
    parent_of: dict[str, object] = {}
    for r in records:
        ref = refs.get(r.cwd) if r.cwd else None
        if ref is not None:
            parent_of.setdefault(r.session_id, ref)
    need = {ref.parent_session_id for ref in parent_of.values()}     # 只数要用到的父会话 (不是每个会话都数一遍)
    own: dict[str, Counter] = defaultdict(Counter)        # 父会话 -> 它自己 (非 scratchpad) 的 (项目, 在 .vscode 下) 计数
    if need:
        for r in records:
            if r.session_id in need and refs.get(r.cwd) is None:
                own[r.session_id][(TEMP_PROJECT, False) if r.cwd in temps else (r.project, r.under_vscode)] += 1

    def resolve(ref) -> tuple[str, bool]:
        for _ in range(5):
            mine = own.get(ref.parent_session_id)
            if mine:
                return mine.most_common(1)[0][0]
            nxt = parent_of.get(ref.parent_session_id)
            if nxt is None:
                break
            ref = nxt
        return friendly_project(ref.parent_dir), False

    memo: dict = {}
    out = []
    for r in records:
        if not r.cwd:
            out.append(r)
            continue
        ref = refs[r.cwd]
        if ref is None:
            out.append(replace(r, project=TEMP_PROJECT, subpath="", under_vscode=False) if r.cwd in temps else r)
            continue
        pu = memo.get(ref.parent_session_id)
        if pu is None:
            pu = memo[ref.parent_session_id] = resolve(ref)
        out.append(replace(r, project=pu[0], subpath="", under_vscode=pu[1]))
    return out
