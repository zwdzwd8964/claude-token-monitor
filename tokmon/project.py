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


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass(frozen=True)
class ScratchpadRef:
    parent_session_id: str   # 父会话 id (小写)
    parent_dir: str          # 父会话的编码 cwd = 它的 transcript 在 ~/.claude/projects 下的那一层目录名


def scratchpad_parent(cwd: str | None) -> ScratchpadRef | None:
    """cwd 在某个会话的 scratchpad 里 -> 那个父会话; 否则 None。

    Claude Code 给每个会话一个一次性目录 `<TEMP>/claude/<编码后的 cwd>/<会话 id>/scratchpad/`。
    在里面跑起来的会话 (测试套件、子进程派的 agent) 的 cwd 落在这里; 按 workspace_identity 的 basename 兜底,
    它的「项目」会变成 scratchpad 里某个叶子目录名 (假项目)。它真正属于父会话的项目。
    纯函数: 只认路径形状 `…/claude/<编码 cwd>/<uuid>/scratchpad/…`, 从右往左找, 嵌套时取最里层。"""
    if not cwd:
        return None
    parts = _split_path(cwd)
    for i in range(len(parts) - 1, 2, -1):
        if parts[i].lower() == "scratchpad" and _UUID_RE.match(parts[i - 1]) and parts[i - 3].lower() == "claude":
            return ScratchpadRef(parts[i - 1].lower(), parts[i - 2])
    return None


TEMP_PROJECT = "临时目录"


def in_temp_dir(cwd: str | None) -> bool:
    """cwd 在系统临时目录里 (…/AppData/Local/Temp/…、C:/Temp/…、/tmp/…)。

    在这里跑的会话 (pytest 的 tmp_path、各种演练沙盒) 的「项目」按 basename 兜底会变成 repo / sandbox 这类假项目;
    路径里又没有任何线索指向真项目 —— 不如老实归到一个「临时目录」里。scratchpad 与控制台标签的规则优先 (认得出来的先认)。"""
    parts = [p.lower() for p in _split_path(cwd or "")]
    for i in range(len(parts) - 1):                      # 最后一段是 temp 本身不算 (那就是在临时目录根上, 少见)
        if parts[i] in ("temp", "tmp") and (i == 0 or parts[i - 1] == "local"):
            return True
    return False


_SCRATCH_DIR_RE = re.compile(
    r"-temp-claude-(.+?)-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-scratchpad(?:-|$)", re.I)


_TEMP_DIR_RE = re.compile(r"(?:-local-temp-|^[a-z]--temp-)", re.I)


def in_temp_dir_from_dir(raw_dir: str | None) -> bool:
    """同 in_temp_dir, 但只有编码目录名 (文件读不出 / 没有 cwd 时的兜底)。"""
    return bool(_TEMP_DIR_RE.search(raw_dir or ""))


def scratchpad_parent_from_dir(raw_dir: str | None) -> ScratchpadRef | None:
    """同上, 但只有 transcript 所在的编码目录名 (文件读不出 / 没有 cwd 时的兜底)。
    `…-Temp-claude-<父会话编码 cwd>-<父会话 id>-scratchpad-…` —— 编码把 / \\ : 空格都变成了 '-', 只能按形状认。"""
    m = _SCRATCH_DIR_RE.search(raw_dir or "")
    return ScratchpadRef(m.group(2).lower(), m.group(1)) if m else None
