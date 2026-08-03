# CHANGELOG

遵循北极星：每个版本都是一个能独立交付价值的完整切片。

## 0.3.0 — `tokmon doctor` 数据契约体检

**动机**：tokmon 依赖 Claude Code 未公开的 JSONL 内部格式（北极星 §5 标记的最大系统性风险）。
需要一个工具在格式漂移**悄悄算错前**把问题暴露出来，并让人**敢信**这些数字。

**变更**：
- 新增 `tokmon/doctor.py` + `tokmon doctor` 命令。全量扫描后体检：
  - **识别率**：assistant 消息 / 含 usage / 可解析时间戳 的比例。
  - **关键字段覆盖**：cwd（项目归属）/ message.id+requestId（去重）/ cache_creation 拆分。
  - **去重健康**：空去重键风险；**跨来源碰撞**（main 与 subagent/workflow 是否共用同一去重键）。
  - **未知模型**：不在定价表里的 model，按 token 量排序（提示去 `pricing.py` 补价）。
  - 带状态图标（✓/⚠/✗）与结论，对高去重率等给出解释而非裸数字。
- 新增 `test_doctor_scan_counts_and_warns`（共 17 例）。

**用 doctor 实测到的结论**（真实数据）：
- ✓ 数据契约健康：assistant/usage/timestamp/cwd/message.id/cache 拆分覆盖 ≈100%。
- **0 跨来源碰撞** → 退役北极星技术债 #3（来源拆分歧义经验证不存在）。
- 73% 去重合并率属正常：会话续接/分叉会重复记录历史（一条消息可出现 20 次/跨 5 个文件），
  去重是必要的，否则会多算约 3.7 倍。

## 0.2.0 — 项目身份对齐到 `.vscode` 子文件夹

**动机**：核心目标明确为「监控 `.vscode` 下每个子文件夹的 token 用量」。
v0 用编码目录名 `split('--')[-1]` 猜项目名，有两处硬伤：把空格变成 `-`、无法把更深的 cwd 归并回所属项目。

**变更**：
- 新增 `tokmon/project.py`：纯函数 `workspace_identity(cwd)`，从真实 `cwd` 还原项目身份。
  - 项目 = `.vscode` 下的**直接子文件夹**（大小写不敏感匹配 `.vscode`）。
  - 更深 cwd 归并回所属子文件夹，余下路径存 `subpath`（备未来 drill-down）。
  - 空格等原样保留；不在 `.vscode` 下的会话回退到 basename。
- `UsageRecord` 新增 `cwd / subpath / under_vscode` 字段。
- `parser` 改为单次扫描捕获文件级 `cwd`，按真实路径定项目，目录名仅作 fallback。
- 新增 `--vscode-only` 过滤（report / watch）。
- watch 的「正在跑」改用最新一条用量记录推断（移除对文件 mtime 的依赖）。
- 新增 `tests/test_tokmon.py`（13 例，无依赖可跑）：project / pricing / parser 去重。

**对抗式 review 修复**（多智能体 review 发现, 均补了单测）：
- **逐记录 cwd 归属**（HIGH）：之前整文件用第一个 cwd, 会话中途 `cd` 会把 token 算错项目。
  改为按每条 assistant 消息自己的 cwd 归属。实测修正了 home 会话约 33.7M tokens 的项目归属、
  ~26.8M 的 subpath 归属。
- **嵌套 `.vscode` 取最近一个**（MEDIUM）：避免外层 `.vscode` 漏进 subpath。
- **盘符/驱动器相对路径**（LOW）：`_split_path` 剥离 `c:` / `c:rest` 前缀。

**验证**：16 个单测全过；`--vscode-only` 现干净显示 4 个真正的 .vscode 子文件夹
（`API` / `edgar api` / `evolution-test` / `news feed 0622`）；home 开发会话正确拆为
`projects` / `claude-token-monitor` / `zwdzw`。

## 0.1.0 — 雏形

- 数据来源：`~/.claude/projects/**/*.jsonl`（只读）。
- main/subagent/workflow 三层来源分类；按 `(message_id, request_id)` 去重。
- 按模型家族定价 + 缓存 5m/1h/读 倍率。
- 两种形态：`watch`（实时 TUI）+ `report`（按需报表）。
