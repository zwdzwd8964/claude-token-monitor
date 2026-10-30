"""发现并分类 session 文件。

目录约定 (Claude Code):
  <base>/<encoded-cwd>/<uuid>.jsonl                                  -> main
  <base>/<encoded-cwd>/<uuid>/subagents/agent-*.jsonl               -> subagent
  <base>/<encoded-cwd>/<uuid>/subagents/workflows/wf_*/agent-*.jsonl -> workflow

<encoded-cwd> 是被编码过的 cwd, 仅作 fallback。真正的项目身份从文件内的 `cwd`
字段还原 (见 project.py)。
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

_LONG = 248                    # Windows 不开长路径支持时: 文件路径 >= 260、目录 >= 248 字符就打不开 / stat 不出来
_WIN_PREFIX = "\\\\?\\"


def fs_path(p) -> str:
    r"""给系统调用 (open / os.stat / os.path.isfile) 用的路径: Windows 上太长的路径加 \\?\ 前缀。

    Claude Code 的 scratchpad 会话目录名很长, transcript 路径能到 269 字符; 不加前缀时 open / stat 直接失败,
    这些会话就被悄悄漏掉了 (2026-10-29 实测 31 个文件)。**只在系统调用处包一层**: 缓存键、row["file"]、
    relative_to 仍然用原路径, 前缀不外泄。"""
    s = os.fspath(p)
    if os.name != "nt" or len(s) < _LONG or s.startswith(_WIN_PREFIX):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):                         # UNC: \\server\share -> \\?\UNC\server\share
        return _WIN_PREFIX + "UNC" + s[1:]
    return _WIN_PREFIX + s


def _entries(d) -> list:
    try:
        with os.scandir(fs_path(d)) as it:
            return list(it)
    except OSError:
        return []                                   # 列不出的目录不拖垮整次扫描


def iter_files(d, pattern: str = "*.jsonl"):
    """递归列出 d 下名字匹配 pattern 的文件 —— 和 Path.rglob 同一个顺序 (先本层的文件, 再按目录项顺序深入),
    但每一层目录都按 fs_path 列, 超过 MAX_PATH 的目录也列得出来。产出的路径不带前缀 (缓存键 / 页面上的路径不变)。"""
    d = Path(d)
    entries = _entries(d)
    for e in entries:
        try:
            if e.is_file() and fnmatch.fnmatch(e.name, pattern):
                yield d / e.name
        except OSError:
            continue
    for e in entries:
        try:
            if e.is_dir():
                yield from iter_files(d / e.name, pattern)
        except OSError:
            continue


def list_files(d, pattern: str = "*") -> list:
    """d 这一层里名字匹配 pattern 的文件 (不递归; 长路径安全; 按名字排序)。"""
    d = Path(d)
    out = []
    for e in _entries(d):
        try:
            if e.is_file() and fnmatch.fnmatch(e.name, pattern):
                out.append(d / e.name)
        except OSError:
            continue
    return sorted(out)


def is_dir(d) -> bool:
    return os.path.isdir(fs_path(d))


def default_base() -> Path:
    return Path.home() / ".claude" / "projects"


def friendly_project(raw_dir: str) -> str:
    """编码目录名的兜底友好名 (cwd 缺失时才用)。c--Users-zwdzw--vscode-API -> vscode-API。"""
    return raw_dir.split("--")[-1] or raw_dir


def classify(path: Path, base: Path) -> tuple[str, str]:
    """返回 (raw_dir, source_kind)。raw_dir = base 下的第一层目录名。"""
    rel = path.relative_to(base)
    parts = rel.parts
    raw_dir = parts[0] if parts else path.parent.name
    s = "/".join(parts)
    if "/subagents/workflows/" in s:
        kind = "workflow"
    elif "/subagents/" in s:
        kind = "subagent"
    else:
        kind = "main"
    return raw_dir, kind


def discover(base: Path, newer_than: float | None = None) -> list[tuple[Path, str, str]]:
    """返回 [(path, raw_dir, source_kind), ...]。跳过 workflow journal。

    newer_than (epoch 秒, 可选): 只返回 mtime >= 它的文件。**只有调用方自己有时间窗时才传**。

    为什么按 mtime 筛是安全的: transcript 只追加, 文件的 mtime 必然 >= 它最后一条记录的时间戳。
    所以 mtime < cutoff 的文件, 里面不可能有 >= cutoff 的记录 —— 跳过它不改变窗口内的任何结果。
    调用方应当再减一点余量 (时钟漂移 / 拷贝文件保留 mtime), 别拿 cutoff 卡着边界传。
    """
    base = Path(base)
    if not base.exists():
        return []
    out: list[tuple[Path, str, str]] = []
    for path in iter_files(base, "*.jsonl"):
        if path.name == "journal.jsonl":
            continue  # workflow 编排日志, 不含 assistant usage
        if newer_than is not None:
            try:
                if os.stat(fs_path(path)).st_mtime < newer_than:
                    continue
            except OSError:
                pass        # stat 不出来 (竞态删除/权限) -> 不敢跳, 照旧交给解析层处理
        raw_dir, kind = classify(path, base)
        out.append((path, raw_dir, kind))
    return out
