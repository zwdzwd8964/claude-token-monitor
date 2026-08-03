"""项目身份识别 (v0.2 核心)。

『监控单位』= `.vscode` 下的**直接子文件夹**。从 transcript 里的真实 `cwd` 还原,
而不是从被编码的目录名猜测——后者会把空格变成 '-', 也无法把更深的 cwd 归并回所属项目。

规则:
  c:\\Users\\zwdzw\\.vscode\\API                              -> project=API,  subpath=""
  c:\\Users\\zwdzw\\.vscode\\API\\auto refresh\\prod_20260604 -> project=API,  subpath="auto refresh/prod_20260604"
  c:\\Users\\zwdzw\\.vscode\\edgar api                        -> project="edgar api" (空格保留)
  C:\\Users\\zwdzw                                            -> project=zwdzw, under_anchor=False (不在 .vscode 下)

纯函数, 无 I/O —— 满足北极星架构不变量 I2, 便于测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_ANCHOR = ".vscode"

_DRIVE_RE = re.compile(r"^[A-Za-z]:(.*)$")  # Windows 盘符前缀, 含驱动器相对路径 c:rest


@dataclass(frozen=True)
class WorkspaceId:
    project: str          # 监控单位: anchor 下的直接子文件夹 (或 fallback 到 basename)
    under_anchor: bool    # 是否位于 anchor(.vscode) 之下
    subpath: str          # 项目内更深一层的相对路径, 可能为空
    cwd: str              # 原始 cwd


def _split_path(p: str) -> list[str]:
    """同时兼容 \\ 与 /, 去掉空段, 并剥离 Windows 盘符前缀 (c: / c:rest)。"""
    parts = [c for c in p.replace("\\", "/").split("/") if c]
    if parts:
        m = _DRIVE_RE.match(parts[0])
        if m:
            rest = m.group(1)
            parts = ([rest] if rest else []) + parts[1:]
    return parts


def workspace_identity(cwd: str | None, anchor: str = DEFAULT_ANCHOR) -> WorkspaceId | None:
    """从 cwd 还原项目身份。cwd 缺失返回 None (调用方走目录名 fallback)。"""
    if not cwd:
        return None
    parts = _split_path(cwd)
    if not parts:
        return None
    anchor_lower = anchor.lower()
    # 取**最后/最近**的 .vscode: 监控单位 = 会话实际所在的那个 .vscode 的直接子文件夹,
    # 嵌套 .vscode 时不会把外层 .vscode 漏进 subpath。
    idxs = [i for i, c in enumerate(parts) if c.lower() == anchor_lower]
    idx = idxs[-1] if idxs else None

    if idx is not None and idx + 1 < len(parts):
        # anchor 之后还有子文件夹: 它就是项目, 再深的归入 subpath
        return WorkspaceId(
            project=parts[idx + 1],
            under_anchor=True,
            subpath="/".join(parts[idx + 2:]),
            cwd=cwd,
        )
    # anchor 不在路径中, 或 anchor 是最后一段 -> fallback 到 basename
    return WorkspaceId(
        project=parts[-1],
        under_anchor=idx is not None,
        subpath="",
        cwd=cwd,
    )
