# 控制台接入 · 迁移规格

> **【状态章 · 2026-10-29】S0 已验收（0.23.0）· S1（0.24.0）· S2（0.25.0）· S3（0.26.0）已交付，待你验收。**
> 还差两件要你在场的：S2 判据 ③（铃铛在真机上响一次）、S3 判据 ①（额度读数和控制台页面上显示的对一次）。你在问卷里选了：
> **1A** 在 tokmon 里按 S1 → S3 做 · **2A** 用配置文件列出控制台 · **3A** 先按官方价目修 `pricing.py`，控制台改读它。
> 开工时你又定了几条：S0 也按官方价算 fast mode 和 us 推理；价目快照默认输出到 stdout，`--out` 写文件；`budget.json` 不动，在停顿点提醒你；
> 超过 260 字符的 transcript 路径（31 个文件，现在读不到）放到 S1 修；S0 之后停一次给你看，S1→S3 连着做；每一刀都提交并推送。
> S0 和原文不一样的地方，见 §1 表下面的「S0 落地备注」。
> S1 开工时你又定了三条：B 加白名单 `<runs_dir>/*/agents/*.json`（只取 id / session_id / role / kind 四个键）；A 做到运行级 + 锦标赛精确；
> E 的自测探针会话标「控制台 E · 自测」。S1 实测和原文不一样的地方，见「S1 落地备注」。
>
> 来源：2026-10-29 的会话分析。问题一：「每个项目配一个控制台」值不值。问题二：控制台的数据能不能进 tokmon。数字都是本机实测。
> **仓库是公开的**：控制台一律用代号 A–E，真实路径和端口只放在 `~/.tokmon/consoles.json`（本机文件，不进版本库，格式见附录 C）；
> 文中只写比例和计数，不写真实花费和项目名（沿用 [SESSIONS_COCKPIT_PLAN.md](SESSIONS_COCKPIT_PLAN.md) 的纪律）。

## 开工清单（给接手的会话）

1. 先读 [MISSION_CONTROL.md](MISSION_CONTROL.md) §8（P1–P7）、[NORTH_STAR.md](NORTH_STAR.md) §4（I1–I6），再读本文件。
2. `python -m pytest -q`，记下开工前的基线。
3. 打开 `~/.tokmon/consoles.json`，确认 5 个控制台的目录都还在。
4. 从 S0 开始，每刀发一个版本，CHANGELOG 里写清判据是怎么验的。

## 0. 为什么

从 2026-10-20 起，你给每个项目配了一个「控制台」：用 Claude Agent SDK 派 agent，结构是中控 + worker / verifier / scout，带工单、额度护栏和证据报告。
5 个控制台是一代一代 fork 出来的：

| 代号 | 代 | 方言 | session_id → 运行 / 任务 / 角色 的关联键 |
|---|---|---|---|
| A | 1 | conductor | 只能靠 cwd（`runs/<运行>/workspace`），最细到运行级；锦标赛 entry / 裁判目录精确到 agent |
| B | 2 | missiond | `<运行>/agents/<aid>.json` 的 session_id（白名单只取四个键），会话级；没有 json 时按 worktree 目录兜底到运行级 |
| C | 3 | office（dispatch） | 每次运行的 `state.json` 里 `agents[].spec.session_id` |
| D | 4 | office | 同 C |
| E | 5 | office（档案驱动，项目专属内容都在 `profile.yaml`） | 会话台账 `state/sessions.jsonl` + `state.json` |

**tokmon 已经能看到这些 agent 的 token，但认不出它们是什么。** 控制台派出的 agent 都是 `entrypoint=sdk-py` 的 Claude Code 会话，
transcript 照常写进 `~/.claude/projects`（目前 198 个主会话文件，约占主会话 token 的 13%）。缺的是下面四样：

1. **归属**：分不出一个会话属于哪个控制台、哪次运行、哪个任务、什么角色。
2. **假项目**：在临时目录里跑的会话被当成了项目（`/sessions` 里出现 `s-tall__whole__sonnet` 这类名字）。
3. **状态判断错误**：worker 在等中控时显示成「等你」。真正需要你处理的事反而看不见：阻塞工单、额度停泊、预算告警。
   这正好卡在北极星那句「有没有哪个需要我现在介入」上。
4. **缺真实额度**：控制台的 `quota` 事件里有订阅 5 小时和 7 天窗口的真实利用率，tokmon 现在只有折算出来的等价 $。

**分析时还顺带发现：`pricing.py` 落后于官方价目**，当前这几款模型的等价 $ 都算高了，主会话合计高约 1.6 倍（附录 A）。所以 S0 先修价目。

接入以后，问题一留下的几个问题就能直接从 tokmon 里得到答案：
- 一个项目的钱分别花在哪：搭控制台、控制台干活、交互写产品，各占多少（问题一的建议是控制台自身开发不超过项目花费的 20%）。
- 一次运行、一个任务、每个角色各花了多少，verifier 占多大比例。
- 现在有没有工单在等我，哪个控制台因为额度停住了。

**这一轮解决不了的**：控制台的 Messages API 后端（A 的 `--backend api`）、项目代码里直接调用的 API、GPU 云、Codex。
这些用量都不进 transcript，归 `/billing` 管，或者另立规格。

## 1. 四刀

| 切片 | 内容 | 完成判据 |
|---|---|---|
| **S0 价目对齐官方**（0.23.0） | `pricing.py` 从「按家族定价」改成「按模型 id 定价」，五列：输入 / 5 分钟缓存写 / 1 小时缓存写 / 缓存读 / 输出，用最长前缀匹配。缓存读倍率按模型区分：Fable 5.1 是 0.025 倍，Opus 5.5 和 Sonnet 5.5 是 0.05 倍，其余 0.1 倍。Haiku 5.5 看单次请求的 prompt 长度（输入 + 缓存读 + 缓存写）：超过 10 万走高价档。不认识的新版本退回家族价，并标 `known=False`。文件头写明价目来源 URL 和抓取日期。新增 `python -m tokmon pricing --json`，导出价目快照给控制台读。`cost_usd` 的签名不变 | ① 每个模型一条单测，每一列都等于附录 B；② 全量重扫后的合计倍数和附录 A 一致（±1%），写进 CHANGELOG；③ doctor 的「未知价模型」除了 `<synthetic>` 以外为空 |
| **S1 归属**（0.24.0） | 新增支柱 `consoles.py`，读配置、会话台账、各运行的 `state.json`，并增量尾读 `events.jsonl`，把三种方言统一成一张标签表：`session_id → {控制台, 运行, 任务, 角色, agent, 项目, 粒度=会话级/运行级}`。内核只加一个字段 `entrypoint`。`/tokens` 加「来源」维度（交互 / 控制台:<代号> / 未认领 sdk），可以按控制台 → 运行 → 任务 → 角色逐级展开，并且跟着图上的筛选走。`/sessions` 把控制台会话折叠到它所属运行的那一行下面，展开后看各个角色。修掉假项目，两条规则：控制台标签自带 project；Claude Code scratchpad 目录（`Temp/claude/<编码后的 cwd>/<父会话 id>/scratchpad/…`）里的会话归给父会话所在的项目 | ① `sdk-py` token 的认领率 ≥95%（按项目估算，上限是 99% 以上），没认领的会话在 doctor 里列出来；② C、D、E 的每次运行，transcript 合计和控制台 `usage` 合计相差 ≤1%；③ `/sessions` 里不再出现临时目录假项目；④ `/sessions` 和 `/tokens` 的刷新耗时不变慢（数字写进 CHANGELOG）；⑤ 三种方言都有合成夹具 |
| **S2 等你**（0.25.0） | 每次运行一台规范化的状态机，状态有：运行中 / 等你（有没答的阻塞工单）/ 额度停泊（停到 HH:MM）/ 预算告警（带级别）/ 空闲 / 已结束（done / partial / failed 各多少）。worker 会话的「等你」改成「等中控」或「额度停泊」，状态来源标「控制台」。新事件见 §2。运行那一行给出控制台地址的链接，链接里不带口令 | ① 拿历史 events 回放：每张工单从 opened 到对应的 answered 之间都显示「等你」，answered 之后一个刷新周期内消失，所有历史工单都对得上；② 「阻塞」怎么判定，先对照各代控制台的源码核实，再回填 §3.2（各代字段不一样）；③ 铃铛在真机上验证一次；④ worker 会话不再误报「等你」（抽查 + 计数） |
| **S3 额度与对账**（0.26.0） | `/sessions` 页顶加一条额度条：5 小时和 7 天窗口的利用率、重置时刻、读数是多久以前的、来自哪个控制台，超过 1 小时的读数置灰。doctor 加「控制台契约」一节，按控制台列出：文件是否可读、信封合规率、类型映射覆盖率（没认出的类型计数）、关联覆盖率、每次运行的 token 对账、控制台自记的 $ 和 tokmon 算的 $ 差多少 | ① 同一时刻，额度读数和控制台页面上显示的一致；② doctor 能复现附录 A 的对账结果；③ 没认出的事件类型数为 0，或者已经登记进 §3.2 |

**S0 落地备注（0.23.0）**：
- 模型 id 先归一化（去日期、`[1m]`、Bedrock / Vertex 前后缀），再**精确**匹配，没有用「最长前缀」：前缀匹配会让 `claude-opus-5` 吞掉将来的 `claude-opus-5-6`，把它错当成已知价。
- 加了 fast mode 和 us 推理的计价，`UsageRecord` 多了 `speed` / `geo` 两个字段（都取自 transcript 原值）。S1 在内核里只加 `entrypoint` 这一条约定不变。
- 影响面比 §4 写的大：tokens_view（成本拆分、省钱标记）、context_view（上下文体检）、`/tokens` 和 `/sessions` 页上「未知单价按 $0」的文案、
  doctor、report，都改成用每条请求实际适用的单价。`/workflow` 的 $ 改成按「计价分类」加总，trace 的分类钩子由 serve 注入（P4）。
- 判据 ①②③ 的实测结果见 CHANGELOG 0.23.0。

**S1 追加**：所有读 transcript 的地方都要能读超过 260 字符的路径（Windows 长路径前缀）。现在有 31 个 scratchpad 会话文件读不到，它们本来就是 S1 要认领的对象。

**S1 落地备注（0.24.0）**：
- 新支柱 `tokmon/consoles.py`（只 import stdlib + project）；内核只加了 `UsageRecord.entrypoint`。scratchpad 里的会话在内核里就归父会话的项目（`project.scratchpad_parent` 纯函数 + `load_records` 末尾一遍，不改缓存里的记录）；控制台标签改项目在展示层做（`/api/summary` 与 `/tokens` 同一份）。
- `/tokens` 的新维度叫「归属」，不叫「来源」：「来源」已经是 main / subagent / workflow。归属 / 运行 / 角色按会话查表（不加宽每一行），任务按记录切（行多了一个键 `t`）。
- `/sessions` 的行带 `console` 标签；控制台会话按运行折叠，运行那一行给控制台自己报的状态、角色构成、累计 $、控制台链接（不带口令）。「等你」的判断没动，是 S2。
- 顺手修了：sdk 会话没有人类提问行，任务简报算出「没有任务」却不进缓存，每次刷新都重排队、「还在算」的计数永远下不去。
- 规格里的 `控制台 E · 自测` 会话归 E 的项目，运行一栏写「自测」，粒度「只认到控制台」，不切任务。
- 临时目录里还有一类假项目规格没写到：pytest 的临时目录、演练沙盒里起的 sdk 会话，路径里没有线索指向真项目 → 内核里归到「临时目录」（`project.in_temp_dir`），不再按叶子目录名造项目。
- 判据 ② 对 C 的最近一次运行**不满足**（+2.9%）：分项看是 C 自己的账本输出 token 少记约 62 倍、漏记 31 条响应；D、E 全部 ≤0.4%。钱以 transcript 为准，不受影响；doctor 标 ⚠ 写明原因。
- 读「会被 rename 覆盖保存」的文件时连 `os.stat` 都不做（它在 Windows 上也会短暂开句柄），签名从目录列表拿。

**S1 判据④ 的基线**（2026-10-29，S0 代码，另起一份 serve，热缓存，各 5 次取中位数）：`/api/tokens/cube?since=7d` 7.2 秒，`since=today` 9.9 秒，`/api/sessions` 0.14 秒；
冷启动后第一次取 7 天立方 358 秒。热缓存的数字抖得厉害（同一接口 5.4–13.3 秒，后台各 pump 在同时扫描），S1 对比时用同样的方法、取中位数。

**S3 落地备注（0.26.0）**：
- 各代的额度读数形状比 §3.2 原来写的杂（2026-10-29 盘点真数据后回填进 §3.2「额度」一行）：A 的 `windows` 里直接是小数、`resets_at` 只属于 `limit` 那个窗口；
  B 顶层小数、不报重置时刻；**C 只报重置时刻、利用率一直是空的**；D / E 是完整的 `windows.{窗口}.{utilization, resets_at}`；E 的故障注入（`injected`）不认。
  所以「带利用率的读数」和「重置时刻」分开存：页顶取所有控制台里最新的带利用率的那条；它没带重置时刻时，借别的控制台报的、**同一个窗口周期**的重置时刻（读数落在 (重置 − 窗口长, 重置] 里），借不到就写「重置时刻未知」。
- 页顶额度条只在有读数时出现；超过 1 小时置灰，过了重置时刻、或有控制台报过一个已经开始的新窗口，写「读数作废」（属于上一个窗口）。读数是整个订阅账户的，不只是控制台自己的用量，提示里写明。
- 「被拒」只认得出是哪个窗口的才算：A / C 的 `limit` 指的窗口、D 的窗口自己的 `status`；E / B 一条事件一个状态、不说是哪个窗口的，不认。
  A / C 被拒的事件不带利用率：单独记下，额度条变红加 ⚠，利用率仍是之前那条读数的（不编），到重置为止或有了更新的读数为止。
- doctor 的 $ 只比**每列** token 都对得上的运行：合计对得上、某一列差好几倍（C 的输出 token）的也不比，不然差的不全是价目。
- doctor 原来「控制台」一节里的每次运行对账挪进新的「控制台契约」一节，并补上 A（usage 事件逐次加总）和「控制台自记的 $ ÷ tokmon 的 $」。
  A 收尾的 agent 会删掉自己的 transcript：transcript **少**于账本是预期（标「·」不报警），**多**了才报 ⚠。
- 「没认出的类型」= 各方言解析器用到的 + `consoles.REGISTERED_TYPES` 登记在案不用的之外的类型。现有的全部登记进 §3.2（下表之后），真数据上为 0。
- 契约计数第一次要读完全部 events（本机约 45 MB，0.3 秒），之后按偏移增量读；只在 doctor 里算，不进 `/sessions` 的刷新路径。

**S4（可选，等控制台收敛成一个引擎以后再做）**：由控制台写一份清单 `console.json`（名字、端口、项目根、runs 和 state 目录、schema 版本），
tokmon 改成读清单，配置文件里只列控制台的根目录。

## 2. 决策

| 维度 | 决定 | 备注 |
|---|---|---|
| 接入方式 | 只读文件适配器，**不改任何控制台**，不轮询控制台的 HTTP 接口 | 控制台没开也能看历史；碰不到它的口令 |
| 发现方式 | 在 `~/.tokmon/consoles.json` 里显式列出（选项 2A） | 不自动扫描 `.vscode`；新增控制台时手工加一行 |
| 读哪些文件 | 白名单：`<runs_dir>/*/events.jsonl`、`<runs_dir>/*/state.json`、会话台账，以及 B 的 `<runs_dir>/*/agents/*.json`（只取四个键，2026-10-29 你加的） | **绝不读** `state/console.token` 这类 `*.token` 文件，也不读 `daemon.log` 和 `agents/*.jsonl`（测试断言从没打开过）。控制台用「写临时文件再 rename 覆盖」保存的文件（`state.json`、agents json），**最后一次修改后安静 5 秒才读**、一次读完立刻关：Windows 上有人拿着句柄，控制台的 rename 就会失败（实测连共享删除打开的也挡；C 不重试） |
| 钱 | token 和 $ 只有一个来源：transcript → `UsageRecord` → `pricing.py`（I3、I4） | 控制台自己记的 usage 和 $ 只用在 doctor 对账里 |
| 控制台的价目 | 由 tokmon 导出价目快照，控制台改成读它（选项 3A） | 控制台那边的改动在各自项目里做，不在本仓库 |
| 内核改动 | `UsageRecord` 只加 `entrypoint`（这是 transcript 里本来就有的事实） | 控制台标签不进内核，在展示层按 session_id 关联（P4、I6） |
| 项目归属修正 | 控制台标签和 scratchpad 父会话都可以修正项目归属，doctor 报告改了多少条 | `project.py` 原有规则不变；scratchpad 规则写成纯函数加在那里 |
| 放在哪 | 融进 `/sessions`、`/tokens`、`/doctor`，不加新页面 | 和前几轨的做法一致 |
| 新事件 | `CONSOLE_TICKET_PENDING`：warning，阻塞工单 ≥60 秒没答（和铃铛现有的 60 秒规则一致）。`CONSOLE_PARKED`（每段停泊一次）、`CONSOLE_RUN_FINISHED`：info。预算复用 `TOKEN_BUDGET_WARNING`，`scope=console`，也是 info、不带百分比。冷启动静默按运行算（一次运行第一次被看到时只记下现状） | P6 对误报零容忍：只有「等你」这一种会响 |
| 事件载荷 | 白名单新增 `console`、`run`、`ticket_kind` 三个 key | **不带工单正文和任务标题**（§6 内容最小化） |
| 隐私 | 测试只用合成夹具，不提交真实的 events | 真实 events 里有提问原文 |

## 3. 数据契约（控制台那一侧，2026-10-29 实测）

### 3.1 信封

5 代控制台格式一致：每行一个 JSON，`{seq, ts, type, agent, data}`。`ts` 是 epoch 秒；`agent` 是角色 id（如 `core`、`w01`），没有时为 null。
每次运行一个目录，目录名以 `YYYYMMDD-HHMMSS` 开头。**「当前状态」优先读 `state.json`**，历史和时间线才回放 `events.jsonl`。

### 3.2 类型映射（规范名 ← 各方言）

| 规范名 | office（C / D / E） | conductor（A） | missiond（B） | 用于 |
|---|---|---|---|---|
| 运行状态 | `session.status`.status：running / idle / closing / stopping / waiting_quota（E 还有 held）；**「现在」以 `state.json` 的 status 为准**（closed = 结束）；`session.restored` 是单独的事件，不是状态 | `run.status`（running / idle / closing）+ `run.finished`；被杀掉的运行没有 run.finished | `mission.state`.state：starting / planning / running / waiting / finishing / idle / **blocked（等你）/ parked（until）/ closed / failed**；running⇄waiting 只表示有没有回合在跑 | S2 |
| 任务 | `task.started` / `task.finished`（`status` 字段：done、partial 等） | 同左 | 同左 | S1 / S2 |
| 角色 | `state.json` 的 `agents[].spec.kind`（E 另有 shell；`spec.critic` 为真记成 critic） | `agent.spawned` 的 kind（锦标赛 competitor / judge） | `agents/<aid>.json` 的 kind；中控是 `conductor` | S1 |
| 用量 | `usage`（逐次响应；`reconciled` 是只补钱的行，token 全 0；`agent_cost` / `total_cost` 是累计，别加） | `usage`（逐次调用的真值；但 **A 正常收尾会删掉自己的 transcript**，只剩被杀掉的运行，所以对不上账） | 没有（只在 agents json 里有累计数） | S3 对账 |
| 额度 | `quota`：D / E 是 `data.windows.five_hour` / `seven_day` 下的 `.utilization`、`.resets_at`（0–1 的小数）；**C 只有 `limit` + `resets_at`，`utilization` 一直是空的**；E 的 `injected` 不为空是故障注入，不认；`known=false` 不认 | `quota`：`windows.five_hour` / `seven_day` 直接是小数，`resets_at` 只属于 `limit` 那个窗口；被拒（rejected）的几条 `windows` 是空的 | `quota.reading`：`data.five_hour` / `seven_day` 是小数，不报重置时刻 | S3 |
| 工单 | `ticket.opened`（id / kind / questions；E 另有 blocking / blocked_since）→ `ticket.answered`（同一个 id）；E 还有 `ticket.superseded`、不阻塞的 `ticket.adopted`；**当前状态以 `state.json` 的 `tickets[id].status` 为准** | 无 | `question.asked`（question_id / why_blocking）→ `question.answered`；`approval.requested`（approval_id / kind）→ `approval.resolved` | S2 |
| 停泊 | `quota.park`（until；E 的 `injected` 是故障注入，不认）→ `quota.resume`；`agent.parked` 只认 cause=quota；C / D 只有 `session.status`=waiting_quota；`state.json` 的 `quota.paused_until` | `quota` 的 status=rejected + resets_at（没有专门的停泊事件） | `agent.parked` 只认 reason=quota（**idle 那种是空闲收起，不是额度**）；`mission.state`=parked（until） | S2 |
| 预算 | `budget`（spent / cap / pct / level：ok / warn / soft_stop / hard_stop） | `budget`（同左） | `meter.warning` 只认 meter ∈ five_hour / quota_share / time_box（ram / capacity / sessions 是机器负载） | S2 |

**登记在案、有意不用的类型**（S3，2026-10-29 盘点真数据；代码里是 `consoles.REGISTERED_TYPES`，doctor 把这些和用到的之外的叫「没认出的类型」）：
- **office（C / D / E）**：agent 过程流水（`agent.status / tool_call / tool_result / thinking / text`）、agent 生命周期（`agent.spawned / finished / retired / denied / audit / backstop / handoff_due / park_requested / park_cleared / released / resumed`）；
  派活（`assign.*`、`dispatch.*`——窗口从 `state.json` 读）；用量（`usage`、`usage.reported`——对账用 `state.json` 账本）；计划与验收（`plan.updated`、`verify.state`、`checks.run`、`decision.recorded`、`progress.reported`）；
  消息与通知（`message`、`board.post`、`human.message`、`notify`）；错误、策略与审计（`error`、`policy.changed`、`policy.task`、`audit.violation`、`secret.blocked`）；
  其余（`git.commit / tag`、`session.restored`、`quota.injected`、E 的 `constraints.prescanned`、`eyes.confirmed`、`finish.rejected`、`handoff.written`、`intake.foreign_session`、`job.*`、`next.*`、`report.written`、`sources.manifest`、`understand.*`）；
  E 的 `ticket.reminder`（S3 真机验收时契约一节当场点名的新类型：控制台对开了几小时的阻塞工单自己的提醒，不改工单状态——tokmon 在 60 秒时已经提醒过一次）。
- **conductor（A）**：agent 过程流水与生命周期、`board.post`、`message`、`human.message`、`error`、`file.write`、`policy.changed`、`tournament.round / finished`（锦标赛的分组从 `tournament.started` 读）。
- **missiond（B）**：`tool.*`、agent 过程流水与生命周期（含 `agent.checkpoint / interrupted / submitted`）、`subagent.*`、`phase.status`、`plan.posted`、`report.written`、`job.*`、
  `spend.reserved / settled`（控制台的钱，只对账不当真值；B 没有逐次 usage）、`daemon.*`、`pod.*`、`policy.denied`、`integrity`、`git.merge`、`operator.message / delivered`。

**阻塞的判定**（S2 对照各代源码核实后回填，2026-10-29）：
- **C、D**：工单**没有**「是否阻塞」的字段。只有中控能开工单、工具立刻返回，中控接着做不依赖答案的事；没有任何自动采纳，所以**每张开着的工单都在等你**。
  例外只有 D 的 `paid_api` 在 allow 策略下当场被策略答掉（`answered_by=policy`），等于从没开着。
- **E**：工单带 `blocking`。不阻塞的当场按推荐项默认采纳（紧跟一条 `ticket.adopted`），不算等你；阻塞的到 `ticket.answered`（计划类也可能是 `ticket.superseded`）为止。
- **B**：`question.asked` 必须写 `why_blocking`，所以每一道题都是阻塞的；`approval.requested` 也要你批；`mission.state=blocked` 是运行级的等你。
- **A**：没有工单（agent 报的 blocked 是向中控汇报，不是找你）。
- tokmon 的规范状态（每次运行一台）：结束 > 等你（有没答的阻塞工单）> 额度停泊（停到几点）> 预算告警（warn 及以上）> 运行中 > 空闲；
  另加「没动静」：报在跑、但事件流 2 小时没动（被杀掉的运行就停在 running）。

### 3.3 关联键

| 代 | 首选 | 兜底 / 粒度 |
|---|---|---|
| E | 会话台账 `state/sessions.jsonl`：session_id / task（全局编号 T<n>）/ role / agent / shell。T<n> → 运行靠各运行 state.json 的 requests / tasks / tnums。台账里 role=`probe:*`、task 不是 T<n> 的是自测探针 → 「自测」 | `state.json` 的当前会话 + `agent.successor` 的 from/to（中控换代）；会话级。**`intake.foreign_session` 里的是别人的会话，不认** |
| C、D | `runs/<id>/state.json` 的 `agents[].spec.session_id`（以及 spec.id / kind） | 会话级；任务按记录时间切：C 看 `dispatches`、D/E 看 `assignments` 的派活窗口，中控按「最近一个已开始的任务」 |
| B | `<运行>/agents/<aid>.json` 的 session_id（白名单只取四个键） | 没有 json：transcript 的 cwd 在 `worktrees/<mission>[--aNN]/…` 下，段名去掉 `--aNN` 后**必须和运行目录名完全一致**（slug 本身可能以时间戳开头，不能只比开头）→ 运行级 |
| A | transcript 的 cwd 在 `<runs_dir>/<运行>/workspace/…` → 这次运行（运行级）；`…/tournaments/tN/entry_X` → 那名选手、`…/tournaments/tN` → 裁判（会话级）；`00000000-*` 是界面样例，不算 | cwd 是项目根的 `sdk-py` 会话：实测没有，不做时间窗 |

## 4. 落位（tokmon 这一侧）

| 文件 | 改动 |
|---|---|
| `tokmon/pricing.py` | S0：五列价目表、按模型的缓存读倍率、Haiku 5.5 的 10 万阶梯 |
| `tokmon/cli.py` | S0：`pricing --json` |
| `tokmon/consoles.py`（新） | 控制台支柱内核：读配置、读台账和 `state.json`、增量尾读 events（按 path + size + mtime 缓存偏移量）、统一方言，输出 `ConsoleSnapshot`（标签表、运行、工单、额度） |
| `tokmon/event_sources/console_source.py`（新） | S2：对比前后两次 snapshot 发事件。只能 import stdlib、`tokmon.events` 和控制台 snapshot（P4） |
| `tokmon/parser.py`、`tokmon/records.py` | S1：加 `entrypoint`，默认值为空字符串 |
| `tokmon/project.py` | S1：识别 scratchpad 父会话的纯函数 |
| `tokmon/tokens_view.py`、`pages/tokens.html` | S1：来源维度，控制台下钻 |
| `tokmon/activity.py`、`pages/sessions.html` | S1：按运行折叠；S2：用控制台状态覆盖；S3：额度条 |
| `tokmon/events.py` | S2：新增事件类型和载荷白名单 |
| `tokmon/doctor.py` | S1：未认领列表；S3：「控制台契约」一节 |
| `tests/fixtures/consoles/{conductor,missiond,office}/` | 合成夹具，不含任何真实内容 |

## 5. 口径与诚实边界

- $ 仍然是等价用量价值。S0 之后所有金额整体下降（主会话约为原来的 0.63 倍），**这是价目修正，不是用得少了**。
  tokmon 没有持久化、每次全量重扫，所以历史数字会自动按新价目重算。下游有一个只读 import tokmon 的复盘项目，它的数字也会跟着变，需要它自己把口径版本加 1。
- 额度读数只在有控制台运行时才有，来自 SDK 的 RateLimitEvent。读数反映的是**整个订阅账户**的窗口，不只是这个控制台的用量；控制台没在跑的时候，不去推测额度。
- A 只能做到运行级（锦标赛的选手 / 裁判除外），页面上标「运行级」；B 加了 agents json 的白名单以后是会话级。
- A 正常收尾会删掉自己的 transcript：tokmon 看不到 A 收尾了的运行花了多少（只有被杀掉的运行留下来）。
- 「搭控制台」本身的花费按 cwd 分不出来：交互会话是在项目根启动的，只是改了控制台目录下的文件。S1 不处理；如果要做，用工具调用里的写入路径来归因，另开一刀。
- 修价不只影响主会话，子 agent 和 workflow 的记录同样按新价目重算。

## 6. 明确不做

- ❌ 写控制台的任何文件；在 tokmon 里答工单、批计划、停运行。这些属于控制层（P7），要做就另立规格。
- ❌ 轮询控制台的 HTTP 接口，读控制台口令。
- ❌ 把控制台自己记的 $ 当成真值。
- ❌ 自动在 `.vscode` 下发现控制台。
- ❌ 把几个控制台收敛成一个引擎：这是问题一的建议，不在本仓库范围内。
- ❌ 统计 API 后端、GPU、Codex 的用量。

## 附录 A · 实测基线（2026-10-29）

| 项 | 实测 |
|---|---|
| `sdk-py` 主会话 | 198 个文件，约占主会话 token 的 13% |
| 控制台自记 token vs transcript | C、D、E 相差 ≤0.6%；A 自记的约为 transcript 的 **9.3 倍**——S1 查明不是记多了，是 A 正常收尾时删掉了自己的 transcript（只剩一次被杀掉的运行，那一次逐 agent 对得上 ±5%）；B 不记 usage。S1 复测（按会话精确归属）：D、E 每次运行 ≤0.4%，C 最近一次运行 +2.9%（C 的账本输出 token 少记、漏记 31 条响应，见 CHANGELOG 0.24.0） |
| 修价影响（主会话，按 transcript 逐条重算） | 合计 ×0.63 · Opus 5.5 ×0.56 · Fable 5.1 ×0.55 · Sonnet 5.5 ×0.56 · Haiku 5.5 ×0.10 · Opus 5 / 4.x ×1.00；控制台会话 ×0.56，交互会话 ×0.64 |
| 控制台价目表 vs 官方 | 基础价是对的；但缓存写一律按 5 分钟的价格算，而 Claude Code 几乎只写 1 小时缓存（见 [CONTEXT_COST_PLAN.md](CONTEXT_COST_PLAN.md)），所以 Opus 5.5 的缓存写少算 37.5%；Sonnet 5.5 的缓存读按 0.1 倍算（官方是 0.05 倍）；没有 Haiku 5.5 的 10 万阶梯 |
| 问题一（交互项目 vs 控制台项目，比例） | 你每发一条指令，agent 干活的时长约为原来的 3 倍；每条指令让 agent 停下来等你的时长约为原来的 1/3；每生成 1 个 token 要读入的上下文从 275 降到 151（控制台自动化部分）；控制台时期的交互花费里，约 72% 用在搭建和修补控制台上（按最贵的 22 题人工归类，旧价目口径） |

## 附录 B · 官方价目（USD / 百万 token，2026-10-29 抓取 platform.claude.com/docs/en/about-claude/pricing）

| 模型 | 输入 | 5 分钟缓存写 | 1 小时缓存写 | 缓存读 | 输出 |
|---|---|---|---|---|---|
| Fable 5.1 / Mythos 5.1 | 10 | 12.50 | 20 | 0.25 | 50 |
| Fable 5 / Mythos 5 | 10 | 12.50 | 20 | 1 | 50 |
| Opus 5.5 | 4 | 5 | 8 | 0.20 | 20 |
| Opus 5 / 4.8 / 4.7 / 4.6 / 4.5 | 5 | 6.25 | 10 | 0.50 | 25 |
| Sonnet 5.5 | 2 | 2.50 | 4 | 0.10 | 10 |
| Sonnet 5 | 2 | 2.50 | 4 | 0.20 | 10 |
| Sonnet 4.6 / 4.5 | 3 | 3.75 | 6 | 0.30 | 15 |
| Haiku 5.5（prompt ≤10 万） | 0.10 | 0.125 | 0.20 | 0.01 | 0.50 |
| Haiku 5.5（prompt >10 万） | 0.50 | 0.625 | 1 | 0.05 | 2.50 |
| Haiku 4.5 | 1 | 1.25 | 2 | 0.10 | 5 |

Web 搜索每 1000 次 $10（不变）。Haiku 5.5 的 prompt 长度按单次请求的全部输入计算，缓存读和缓存写也算在内。
官方页会变：S0 落地前**再抓一次**，以当时的页面为准。

## 附录 C · `~/.tokmon/consoles.json` 的格式

本机已经按下面的格式写好了 5 条（真实路径只在那份文件里）。字段：

```json
{
  "version": 1,
  "consoles": [
    {
      "code": "E",
      "name": "<给自己看的名字>",
      "dialect": "office",
      "generation": 5,
      "project_root": "<控制台服务的项目根目录>",
      "runs_dir": "<runs 目录>",
      "state_dir": "<state 目录，可省略>",
      "registry": "<会话台账 sessions.jsonl，可省略>",
      "worktrees_dir": "<只有 missiond 用>",
      "join": "registry | state_json | cwd_worktree | cwd_run",
      "url": "http://127.0.0.1:<端口>/"
    }
  ]
}
```

`dialect` 的取值：`office`（C / D / E）、`missiond`（B）、`conductor`（A）。`join` 是首选的关联方式，兜底规则见 §3.3。
`url` 只用来在页面上生成链接，tokmon 不会去请求它。
