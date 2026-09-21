# CHANGELOG

遵循北极星：每个版本都是一个能独立交付价值的完整切片。

> **本文件曾从 0.3.0 起停更约 10 个切片**（2026-06-28 → 2026-09-20）。
> 那段历史不在这里，在 [EVOLUTION.md](EVOLUTION.md)（逐代评估 + 证据分级）与
> [docs/RECAP_2026-09-20.md](docs/RECAP_2026-09-20.md)（全景 + 侧批）。
> 下面的「未记录期」条目只做索引，不假装当时写过。

## 0.10.0 — `/workflow` 工作流回放 S1（+ 一轮多 agent 对抗式 review）

**动机**：你要一个比 /tokens 更细的追踪器——看一次提问是**怎么被完成的**：调了哪些 skill / 工具 / MCP /
子 agent / workflow，各花多少时间和 token。原则是你的原话：「能追踪、能看懂、能学到；不缺信息，也不堆信息」。
需求用 6 轮问卷敲定（23 项决定），规格见 [WORKFLOW_TAB_PLAN.md](WORKFLOW_TAB_PLAN.md)。远程控制与通知按你的决定搁置。

**交付**
- 新支柱 `tokmon/trace.py`（只读，只 import stdlib + discovery + project）：按真人提问切任务；后台任务完成通知按
  `<tool-use-id>` 真值找回启动它的任务；Agent / Workflow 按 `agentId` / `runId` 回链子树，workflow 阶段顺序取自脚本；
  skill 归属只信 Claude Code 自己打的标记。按文件增量解析 + 缓存，近 7 天列表 0.3 秒。
- 新页面 `/workflow`（独立文件 `tokmon/pages/workflow.html`）：左栏任务列表；结构化摘要（耗时拆成 机器执行 / 模型生成 / 等你、
  token 真值与等价 $、异常计数、关键时刻）；调用树与时间轴并排；明细抽屉取完整输入输出与全文（已脱敏）。
- 三级诚实：真值直接写；调用级 token 是两个估算（发起 ≈ / 结果 ≈）；重试、打转、偏慢写明「推断 / 统计」。
- 导航隐藏 /notify /control（代码保留）；/tokens 的项目名可深链进来（带时间窗）。

**对抗式 review**：4 个维度各 1 个 reviewer + 1 个 verifier（8 个 agent）。34 条发现，**31 条确认，全部修复**，每条都有回归测试：
- **时间**：旧算法把一段里首尾之间整段算成「模型生成」，空闲、事后敲的 `/model`、恢复会话时的占位消息都会撑大时长——
  真实数据里一个 18 分钟的任务被报成 40 小时。现在只有相邻活动相隔 ≤ 15 分钟才连成区间；`/model` 归下一轮。
- **对账**：按指令开头兜底回链 agent 可能挂错、漏算或跨任务重复算 → 改为证据制（指令全文 + 启动时间唯一匹配，
  明确被引用过的 agent 不许碰，多个候选就不猜）。
- **运行态**：运行中给到「拥有最后一条活动」的任务；查询带进程存活索引——**修掉了 S1 对 /sessions 的一处回归**
  （activity 的共享快照缓存曾被写入一帧不带存活信息的结果）。
- **不把缺失写成 0**：文件缺失 / 没结果 / 含估不出的块一律显示 — 或 ≈?，合计有缺失时标「≥」。
- **结果体积**：图片按尺寸估（原来「[图片]」3 个字 = 3 token）；skill 正文注入算进 Skill 调用；另存到文件的输出不再误报「大结果」。
- **安全**：transcript 里出现过的控制令牌会原样出现在回放页（本仓库测远程模式时打印过）→ 本服务知道的密钥原值精确打码；
  截断不再切出半截密钥；按字段名打码嵌套 JSON 里的 password / apiKey 等。
- **其余**：后台任务失败不再显示成成功；「打转」要求输出也没变化（改代码→重跑测试不再误报）；「偏慢」写明是统计推断并排除
  sleep / 轮询；抽屉晚到的旧响应不再覆盖新内容；自动刷新失败不再清空回放；吸顶表头修复；启动横幅恢复外发提示。

**验证**：`pytest -q` **223 passed**（177 → 223）；真实数据 **97/97 个任务、20/20 个会话 token 与 parser 逐 token 一致**
（仅有的不一致都复核为活会话写入竞态，换序后一分不差）；被报成 40 小时的任务现在是 18 分 09 秒；
139 个任务的回放数据与 69 个调用明细里控制令牌出现 0 次（原始 transcript 里有 2 次）；前端在 Node 桩 DOM 上用 12 个真实任务
跑 213 次明细抽屉，0 个运行时错误。

## 0.9.0 — 重启四件事：停止撒谎的默认值 / 补上读页鉴权 / 备好手机环 / 文档降熵

**动机**：2026-09-20 的全景评估（[docs/RECAP_2026-09-20.md](docs/RECAP_2026-09-20.md)）指出，
这个系统「机器造得很干净、信任基建做到可测量的 97%，但最有价值的那一下从未通过电」，
且有三处与它自己的原则相悖。本版按「杠杆/成本」而非原路线图，把其中四件事做掉。

**① `/sessions` 不再默认藏起「谁在等我」**（原则 1 真实优先 / P6 误报零容忍）
- 「隐藏空闲会话」**默认不再勾选**。实测当天数据：10 个「等你」里 9 个会被旧默认删掉
  （V3 审计当时是 71 分之 70）——这一页唯一的存在理由被它藏掉了。
- 「空闲」这个**语义反了的词**全站改名为「**等你已久**」：它的真实含义是
  「Claude 已经回复你、超过 10min 没人接话」= **你欠它一句话**，不是「它闲着」。
  pill 上也标明它是「等你」的**子集**而非并列状态。
- 代码里写下了「未来若想改回 `checked`，先回答『用户打开这一页是为了看什么』」。

**② `MC_REMOTE`：读页鉴权收口**（[REMOTE_CONTROL_PLAN.md](REMOTE_CONTROL_PLAN.md) §6，欠了两个月）
- 新增 `tokmon/remote.py`（只 import stdlib，纯函数为主）。开 `MC_REMOTE=1` 后
  **所有页面与 `/api/*` 都要令牌**；手机经 `/login` 贴一次，存 **HttpOnly + SameSite=Strict** Cookie（12h）。
- **两道门故意不合并**：读门认 Cookie（浏览器导航没别的办法），
  **控制门只认 `X-Control-Token` 头、永不认 Cookie** —— 合并就等于把控制面送给 CSRF。
  有一条真 HTTP 用例钉死这条不变量。
- **失败安全**：绑非本机却没开远程 / 开了远程却没 `MC_REMOTE_HOSTS` / 没令牌 → **拒绝启动**。
- Host 白名单是**显式登记**（不支持通配），保留 DNS-rebinding 防护；顺带修掉
  `normalize_host` 对无端口 `[::1]` 的误拒。
- hook 令牌改走 **header**（隧道边缘会把 query 写进日志）；本机 URL 保留 query 兼容旧配置，
  经隧道来的请求一律只认 header。

**③ 手机环：Pushover 出站通道**（M4.5(b)，平台唯一的价值出口）
- `notify.py` 的通道升级为**可插拔多通道**：Telegram（现只出）+ **Pushover（新）**，
  **全部默认关**，都不配就一个字节不出本机；单个通道挂掉不拖垮另一个。
- `pushover_priority`：critical → high（突破手机端免打扰），其余 → normal；
  **永不使用 emergency(2)**——那会重试到你手动 ack，正是 §7「有用且不烦」要避免的。
- `/notify` 页给出 Pushover 的最短上手路径 + 双通道状态。
- **诚实边界**：真账号才能验「手机真的震了」；但**发出去那个 POST 长什么样**已用
  本地 stub 服务器零外发地钉死（token/user/message/priority/title 逐字段断言）。

**④ 文档降熵 + 两条「开箱裂缝」**
- README **重写**：一页说清现状，并成为**唯一的文档索引**（每份计划文档标注真实状态）。
- 每份计划文档头部盖「真实状态」章：已实施 / 部分 / **从未实施**。
- `requirements.txt` 补 `pytest`（此前照着装会以为「这项目没测试」）。
- **修掉 GBK 控制台下 `doctor` / `backtest` 直接崩溃**（`UnicodeEncodeError: '✓'`，
  EVOLUTION §Gen0.3 记录在案、违反原则 5）：新增 `util.term_text` 做编码降级，
  写不出就换 `[OK]/[!]/[X]`，**绝不让展示层的编码问题杀掉已经算对的结果**。
- `__version__` 从停滞的 `0.3.0` 更正为 `0.9.0`（不是 1.0：手机环真机验证仍未完成）。

**验证**：`pytest -q` **177 passed**（130 → 177，新增 47 例：`MC_REMOTE` 29 + Pushover 15 + 编码降级 3）；
`MC_REMOTE` 两条拒绝启动路径与完整登录链路经真实 HTTP 实测；
GBK 控制台下 `doctor` 从崩溃变为正常跑完。

## 0.4.0 – 0.8.x — 未记录期（2026-06-28 → 07-20）

这段时间交付了 **serve 驾驶舱 / M1 对话活动 / M2 事件总线 / M3 通知层 / M3.5 预算告警 /
M4 控制层（远程审批）/ M4.5 推断 doctor + 准确率回测 / B1 厂商账单 / S1-v2 SDK steer**，
但 CHANGELOG 全部漏记。**不补写细节**（无法诚实重建），只给索引：

- 逐代评估 + 每代的证据分级 → [EVOLUTION.md](EVOLUTION.md)
- 各切片的设计与约束 → 见 README 的文档地图
- 当前全景与缺口 → [docs/RECAP_2026-09-20.md](docs/RECAP_2026-09-20.md)

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
