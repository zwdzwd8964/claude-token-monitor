"""发现并分类 session 文件。

目录约定 (Claude Code):
  <base>/<encoded-cwd>/<uuid>.jsonl                                  -> main
  <base>/<encoded-cwd>/<uuid>/subagents/agent-*.jsonl               -> subagent
  <base>/<encoded-cwd>/<uuid>/subagents/workflows/wf_*/agent-*.jsonl -> workflow

<encoded-cwd> 是被编码过的 cwd, 仅作 fallback。真正的项目身份从文件内的 `cwd`
字段还原 (见 project.py)。
"""

from __future__ import annotations

from pathlib import Path


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


def discover(base: Path) -> list[tuple[Path, str, str]]:
    """返回 [(path, raw_dir, source_kind), ...]。跳过 workflow journal。"""
    base = Path(base)
    if not base.exists():
        return []
    out: list[tuple[Path, str, str]] = []
    for path in base.rglob("*.jsonl"):
        if path.name == "journal.jsonl":
            continue  # workflow 编排日志, 不含 assistant usage
        raw_dir, kind = classify(path, base)
        out.append((path, raw_dir, kind))
    return out
