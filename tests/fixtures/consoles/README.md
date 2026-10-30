# 控制台合成夹具 (CONSOLES_PLAN S1)

全部是**手写的合成数据**, 不含任何真实运行的内容。三种方言各一份, 形状按 2026-10-29 各代控制台的源码与实测结构
(只取 tokmon 会读的键):

| 目录 | 方言 | 代号 | 看点 |
|---|---|---|---|
| `office/runs_c/` | office (第 3 代) | C | `state.json` 的 `agents[].spec.session_id`; 派活窗口 `dispatches`; 任务时间表来自 `task.started` |
| `office/runs_e/` + `office/state/sessions.jsonl` | office (第 5 代) | E | 会话台账 (含中控换代前的旧会话、自测探针); `agent.successor`; `assignments` 的 `tn` |
| `missiond/runs/` | missiond (第 2 代) | B | `agents/<aid>.json` 只取四个键; 按 worktree 目录兜底 |
| `conductor/runs/` | conductor (第 1 代) | A | 不记 session_id: 按 cwd 认运行; 锦标赛 entry / 裁判目录精确到 agent; `00000000-*` 样例运行不算 |

故意放进去的「不许读」文件: `*.token`、`daemon.log`、`agents/*.jsonl` —— 测试会断言 tokmon 从没打开过它们。
`events.jsonl` 的最后一行故意没有换行 (控制台正写到一半), 不该被读进来。

路径里的 `{root}` 由测试换成夹具复制到临时目录后的真实路径。
