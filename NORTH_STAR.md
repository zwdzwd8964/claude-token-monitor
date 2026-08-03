# 北极星文档 · tokmon 演进指导

> 这份文档不是需求清单，而是**方向与约束**。任何人（包括未来的你、或某个 Claude session）
> 在改这个项目前，先读这里，确保每一步进化都朝同一个北极星走，且不破坏已立住的地基。
>
> **定位（2026-06 起）：** tokmon 现在是更大平台 **Claude Mission Control** 的**「成本支柱」**。
> 平台级方向、三层架构（监控→事件→通知）、事件契约与通知/隐私基线见 [MISSION_CONTROL.md](MISSION_CONTROL.md)。
> 本文件**保持克制、专注 token 成本**这一件事（§10），不承载平台的其它使命——那是上层文档的事。

---

## 1. 北极星 (The North Star)

**让我对『自己用 Claude Code vibe coding 烧了多少 token / 等价多少钱』这件事，
从「完全没感觉」变成「随手可见、心里有数、能复盘」。**

衡量是否成功的唯一问题：

> 在任意时刻，我能否在 5 秒内回答——
> 「我今天/这周烧了多少 token？哪个项目最贵？跟我的预期差多少？」

一切功能、重构、取舍，都回到这个问题。**它若不让这个问题的答案更快/更准/更可信，就先别做。**

---

## 2. 设计原则 (Principles)

按优先级排序，冲突时上位优先：

1. **真实优先 (Ground truth first)。** 数字必须可被信任。宁可显示「未知单价」也不要悄悄估错；
   宁可少一个炫酷功能，也不要一个会误导自己的数字。
2. **零侵入 (Zero intrusion)。** 永远只读 `~/.claude/projects`，绝不修改 Claude Code 的行为、配置或文件，
   绝不拦截网络。监控器挂掉不能影响正在 coding 的你。
3. **本地、私密、可离线 (Local-first)。** token 用量是隐私。默认一切在本机完成，不外发。
   任何「上报/同步/云」必须是显式 opt-in。
4. **核心数据层 vs 展示层分离。** `discovery/parser/pricing/aggregate` 是稳定内核；
   `report/tui/(未来的 web/export)` 都只是它的消费者。新展示形态不该需要改内核。
5. **渐进可降级 (Graceful degradation)。** 缺 rich → 纯文本；缺某字段 → 跳过那条而非崩溃；
   数据目录不存在 → 友好报错。任何单点异常不拖垮全局。
6. **小步进化，每步可用 (Always shippable)。** 每个版本都是一个能独立交付价值的完整切片，
   而不是「做了一半的大功能」。

---

## 3. 当前状态 · v0 (Where we are)

**已立住的地基（v0 已交付）：**

- **数据源已摸清并跑通**：`~/.claude/projects/<project>/**/*.jsonl`，每条 assistant 消息含 `usage`。
- **三层来源分类**：main（主会话）/ subagent（子智能体）/ workflow（多智能体 workflow），可分别统计。
- **精确解析**：input/output、缓存写（5m/1h 分开）、缓存读、web search/fetch 次数。
- **去重**：按 `(message_id, request_id)` 全局去重，避免同一条消息重复计数。
- **定价**：按模型家族单价 + 缓存倍率，算出等价美元；未知模型标 `*`。
- **两种形态**：`watch`（实时盯盘 TUI）+ `report`（按需报表）。
- **性能基础**：文件级 `(mtime,size)` 缓存，watch 轮询只读变动文件。

**v0.2 增量（已交付）：**

- **项目身份改用真实 `cwd`**：监控单位明确定义为 **`.vscode` 下的直接子文件夹**
  （`tokmon/project.py` 纯函数 `workspace_identity()`）。空格保留；更深 cwd 归并回所属子文件夹
  （余下存 `subpath`）；非 `.vscode` 会话回退 basename 并可用 `--vscode-only` 过滤。
- **单元测试**（`tests/test_tokmon.py`，13 例）：钉住 project/pricing/parser 去重，无依赖可跑。

**v0.3 增量（已交付）：**

- **`tokmon doctor` 数据契约体检**（`tokmon/doctor.py`）：识别率 / 关键字段覆盖 / 去重健康
  / 跨来源碰撞 / 未知模型。守护「依赖未公开格式」这个最大风险（见 §5）。
- 用 doctor 实测：**0 跨来源碰撞** → 技术债 #3 经验证不存在；73% 去重合并率属正常（会话续接重复记录历史）。

**当前边界（明确没做）：**

- 没有持久化（每次都全量重扫内存计算）。
- watch 是**定时重扫**，不是真正的文件系统监听（够用，但不是实时推送）。
- 没有预算/阈值告警。
- 没有 Web 看板、没有数据导出。
- `report` 还没有 `--by` 单维度 / `--json` 导出。

---

## 4. 架构不变量 (Invariants — 别破坏这些)

进化过程中，下面这些必须始终成立。破坏其一 = 走偏：

- **I1 只读。** 代码里永远不出现对 `~/.claude` 的任何写操作。
- **I2 内核纯函数化。** `parser/pricing/aggregate` 不做 I/O 之外的副作用，输入相同 → 输出相同，易测试。
- **I3 定价集中。** 所有单价、倍率只存在于 `pricing.py`。改价/加模型只动这一个文件。
- **I4 UsageRecord 是唯一真相载体。** 所有展示层都消费 `list[UsageRecord]`，
  不绕过它直接读 JSONL。新增维度优先加到 `UsageRecord` 字段，而非各处临时解析。
- **I5 去重键稳定。** `(message_id, request_id)` 是去重契约，改它要有充分理由并回归验证。
- **I6 展示层不反向污染内核。** TUI/report 的需求不能逼内核引入展示相关的状态。

---

## 5. 数据源契约与风险 (Data contract)

我们依赖一个**未公开承诺的内部格式**。这是最大的系统性风险，必须正视：

| 依赖字段 | 用途 | 若 Claude Code 改格式的影响 |
|----------|------|------------------------------|
| `type == "assistant"` + `message.usage` | 识别计费消息 | 高：解析失效 |
| `message.usage.{input,output,cache_*}_tokens` | token 计数 | 高 |
| `message.usage.cache_creation.ephemeral_{5m,1h}` | 缓存 TTL 拆分 | 中：可降级为合并计 |
| `message.usage.server_tool_use.*` | 工具计费 | 低 |
| `message.id` / `requestId` | 去重 | 中：去重退化 |
| 每条 assistant 行的 `cwd` | **逐记录**项目归属 (会话中途 cd 会变) | 中：归属退化为文件级 |
| `timestamp` | 按天归桶 | 中 |
| 目录结构 `subagents/`、`workflows/` | 来源分类 | 中 |

**护栏：**
- 解析必须对缺字段**容错跳过**，不能崩。（v0 已做。）
- ✅ **`tokmon doctor`**（v0.3 已实现）：全量扫描，报告「识别率/字段覆盖/去重健康/未知模型」，
  让格式漂移**可被发现**而不是悄悄算错。升级 Claude Code 后**先跑 `tokmon doctor`**。
- 升级后若 doctor 报警（识别率掉、出现新未知模型、跨来源碰撞 >0），优先核对格式变化。

---

## 6. 演进路线图 (Roadmap)

每个阶段 = 一个可独立交付的切片。**按顺序做，但每步都先问北极星。**

### v0.1 — 把雏形磨成每天能用（信任 + 顺手）
- **目标**：让自己愿意每天开着它。
- 内容：
  - ✅ `tokmon doctor`：识别率/字段覆盖/去重健康/未知模型体检（v0.3 已做）。
  - ✅ 项目名从记录里的 `cwd` 字段精确还原（v0.2 已做，定义为 `.vscode` 子文件夹）。
  - ✅ 单元测试覆盖 `pricing` / `parser` / `project` / `doctor`（`tests/test_tokmon.py`，17 例）。
  - `report` 增加 `--by {day,project,model,source,session}` 只看某一维度；`--json` 输出。← **下一个待做**
- **完成判据**：升级 Claude Code 后能用 doctor 一眼看出是否漂移；数字我敢信。← **基本达成**

### v0.2 — 真·实时（从轮询到监听）
- **目标**：盯盘体验从「每 5s 重扫」升级为「写入即更新」。
- 内容：用 `watchdog` 监听 `~/.claude/projects` 的文件变更，增量解析新增行（记录每文件已读偏移），
  仅对变动文件做增量。watch 内存里维护增量聚合。
- **完成判据**：开着 watch coding，新消息 1~2s 内反映在面板上，CPU 几乎不动。

### v0.3 — 持久化与历史（能复盘）
- **目标**：不再每次全量重扫；能看长期趋势。
- 内容：把 `UsageRecord` 落到本地 **SQLite**（`~/.tokmon/usage.db`），
  增量 upsert（去重键作主键）。`report` 优先查库，按需回灌。
  新增 `report --trend`：按天的 token/成本折线（终端 sparkline）。
- **完成判据**：全量历史查询 < 100ms；冷启动不再卡。

### v0.4 — 预算与告警（主动而非被动）
- **目标**：从「我去看」变成「它提醒我」。
- 内容：配置文件 `~/.tokmon/config.toml` 设日/周预算与每项目预算；
  watch 面板显示预算进度条；越线时 Windows 通知（`win10toast` / `plyer`）。
- **完成判据**：当天超过设定阈值，我会被动收到提醒，而不是事后才发现。

### v1.0 — 本地 Web 看板（可视化复盘）
- **目标**：补上 TUI 不擅长的趋势/对比可视化。
- 内容：`tokmon serve` 起本地 FastAPI + 一个静态前端，复用同一套内核与 SQLite，
  画按天柱状、项目占比、模型占比、缓存命中率趋势。**只监听 localhost，默认不外发。**
- **完成判据**：浏览器里能直观看清「这个月趋势 + 哪个项目/模型最烧钱」。
- ✅ **早期预览（已交付）**：`tokmon serve`（`tokmon/serve.py`）—— **纯标准库 `http.server`**（零额外依赖、可离线），
  只监听 `127.0.0.1`、`no-store`、不外发；复用 `load_records()`+`aggregate`，**不碰内核**（`_filter_window`/`_baseline_window`
  在 serve 层，I6 守住）。已含 **「vs 上一周期」自基线对比**（同一份记录的第二次 filter+summarize）：
  KPI Δ% + 「变化最大的项目」+ 缓存命中率，**回答北极星第三问「跟我的预期差多少」**——基线是「你自己的上一等长周期」
  而非预算（预算在 v0.4）。诚实降级：`today` 比昨天同一时段、`all` 无基线不瞎编、历史不足/未知单价均显式标注（原则 1）。
  正式版的 FastAPI + SQLite 持久化仍是 v1.0 目标（依赖 v0.3）。经对抗式 review 加固：HTML 转义、乱序响应丢弃、基线侧 `*` 对称标注。

> **关于 `serve` 主页的「进程监控」兄弟页（`/processes`, `tokmon/procmon.py`）**：
> 这是用户另开的**并行同级**监控（进程 / localhost 端口 / 本机 cloudflared 隧道），**不属于本北极星**——
> 它有自己的小宪章（纯只读、本地不外发、命令行脱敏），且**不碰 token 内核**（procmon 独立于 parser/pricing/aggregate）。
> 写在这里只为说明：`serve` 现在是一个承载两个独立监控的外壳，token 看板的使命不因它而扩张（守 §10）。

### v2+ — 候选方向（按需再排）
- 多机汇总（家里/公司多台机器，显式 opt-in 同步到自建端）。
- 缓存效率分析：命中率低的项目 = prompt 设计可优化的信号。
- 「effort/模型选择」与成本的关联分析，反哺自己的用法。
- 把它做成可独立分发的 CLI（`pipx install tokmon`）。

---

## 7. 非目标 (Non-Goals — 明确不做)

写下来是为了抵御「功能蔓延」对北极星的稀释：

- ❌ **不做账单对账工具。** 我们算的是**等价用量价值**，不追求和 Anthropic 真实账单分毫不差。
  > **2026-07-13 复核：仍然有效。** `/billing` 明确**不做**对账——实测你的 Claude Code 走订阅
  > (`apiKeySource: "none"`)，其用量**根本不进 API 平台账单**，硬对就是自欺。理由见
  > [PROVIDER_BILLING_PLAN.md](PROVIDER_BILLING_PLAN.md) §0.3。
- 🔧 ~~**不替代官方用量后台。不去爬 console、不调计费 API。**~~ —— **2026-07-13 修订**
  > **改为：** **不爬 console 网页、不逆向内部端点**；但**可以调用厂商公开的官方 usage/cost API**
  > （新 `/billing` 页，见 [PROVIDER_BILLING_PLAN.md](PROVIDER_BILLING_PLAN.md)）。
  >
  > 这是平台的**第二个「受控破例」出站**（第一个是 M3 通知）：**默认全关、显式 opt-in、
  > 密钥绝不回显、拿不到就诚实标注「不可得」**（Google 就是拿不到，见该文 §1.3）。
  >
  > **本机 transcript 仍是 `/tokens` 的唯一真相来源。** 厂商账单是**另一个池子**——
  > 两者**不同源、本就不该相等**，UI 上必须写明，别让未来的自己当成 bug。
- ❌ **不做团队 SaaS。** 这是给「我自己」用的本地工具，不是多租户产品（除非北极星本身改变）。
- ❌ **不修改/优化 Claude Code 本身。** 监控就是监控，不越界。
- ❌ **不做实时限流/熔断。** 我们观测，不干预 coding 流程。

---

## 8. 演进护栏 (How to extend without rotting)

给未来动手的人（含 AI agent）的具体操作约束：

- **加一个新模型/调价** → 只改 `pricing.py` 的 `FAMILY_PRICING` / `EXACT_PRICING`。别在别处散落单价。
- **加一个统计维度**（如「按 session」「按小时」）→ 在 `aggregate.py` 加 `group_by` 的 keyfn，
  展示层调用。不要在 report/tui 里重新解析 JSONL。
- **加一种新来源类型** → 改 `discovery.classify`，并在 `SCOPE_KINDS` 里登记。
- **加一种新展示形态**（web/export/通知）→ 新建模块消费 `load_records()` 的输出，**不碰内核**。
- **每次改完**：跑 `report --since today` 和 `watch` 各一次，肉眼对账数字是否仍合理。
  有测试后（v0.1）：跑测试。
- **改了去重键 / 数据契约假设** → 必须在本文件 §5 更新，并说明回归验证方式。
- **任何「上报/同步/云」特性** → 默认关闭，opt-in，文档显著标注隐私影响（原则 3）。

---

## 9. 已知风险与技术债 (Known debt)

诚实记录，避免自欺：

1. **数据格式依赖内部约定**（§5）——最大风险。缓解：doctor + 容错 + 升级后对账。
   新增依赖：项目身份依赖记录里的 `cwd` 字段（缺失时回退编码目录名）。
2. ~~**项目名靠猜**~~ ✅ **v0.2 已解决**：改用真实 `cwd`，项目 = `.vscode` 直接子文件夹。
   **设计决策**：更深的 cwd（如 `…\.vscode\API\auto refresh strategy\prod_20260604`）
   **归并回所属子文件夹 `API`**，更深路径存 `subpath` 备 drill-down——符合「监控每个 .vscode 子文件夹」。
   对抗式 review 后已加固：**逐记录**按各消息自己的 cwd 归属（会话中途 cd 不再错算，
   实测修正了我们 home 会话约 33.7M tokens 的项目归属）；嵌套 `.vscode` 取最近一个；
   UNC / 驱动器相对路径已正确处理。均有单测覆盖。
3. ~~**同一消息可能同时出现在主会话与 subagent 文件**~~ ✅ **doctor 实测验证为非问题**：
   全量扫描 **0 条跨来源碰撞**，main/subagent/workflow 拆分可信。doctor 会持续监控
   `cross_kind_keys`，若未来 Claude Code 改成内联记录子会话而出现碰撞，会告警。
4. **每次全量重扫**——数据增长后会变慢。v0.3 的 SQLite 持久化解决。
5. **`<synthetic>` 等非真实模型计入消息数**——成本为 0 不影响钱，但 msg 数会略虚高。可在 v0.1 决定是否过滤。
6. **时区**——按本地时区归「天」。跨时区机器/出差时「今天」的边界会变。可接受，记录在案。

---

## 10. 一句话给未来的自己

> 别把它做成一个炫的 dashboard 集合。
> 它只有一个使命：**让我对自己烧的 token 有感觉、有数、能复盘。**
> 每加一个东西，先问：这让那件事更快、更准、更可信了吗？不是，就先别加。
