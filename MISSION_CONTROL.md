# Claude Mission Control · 平台北极星

> 这是**整个监控平台**的方向与约束。它统管多个支柱（token 成本、进程健康、对话活动……），
> 每个支柱有自己的子宪章：成本支柱见 [NORTH_STAR.md](NORTH_STAR.md)（tokmon），进程支柱见 `tokmon/procmon.py` 顶部说明。
> 任何人（含未来的 Claude session）在扩展这个平台前，先读这里，确保进化朝同一个北极星走，且不破坏已立住的地基。

---

## 1. 北极星 (The North Star)

**当 Claude Code 在替我 agentic coding 时，让我随时知道它到底在『推进 / 卡住 / 烧钱 / 乱改』，
并且只在重要时刻、用恰当的方式提醒我——有用，且不烦。**

agentic coding 工具最大的痛点不是不够聪明，而是：

> 它在跑的时候，人不知道它到底是在推进、卡住、烧 token，还是在乱改。

衡量是否成功的唯一问题：

> 在任意时刻，我能否在 5 秒内回答——
> 「我所有正在跑的 Claude Code，现在分别是什么状态？有没有哪个需要我现在介入？」
> 而当我**没在看**屏幕时，重要的事会不会**主动、恰当地**找到我，无关紧要的事会不会**安静地**不打扰我？

一切功能、重构、取舍，都回到这个问题。**它若不让这个问题的答案更快/更准/更可信，或让"打扰"更精准，就先别做。**

这让平台从一个 **token tracker** 升级为真正的 **AI coding ops dashboard**。

---

## 2. 设计原则 (Principles)

按优先级排序，冲突时上位优先。前 4 条继承自 tokmon 北极星，后 4 条是平台新增：

1. **真实优先 (Ground truth first)。** 状态/成本/风险都必须可被信任。宁可显示「未知」也不要悄悄报错，
   宁可少一个事件，也不要一个会误导自己的判断。误报会摧毁信任，让人开始无视通知——那等于系统死了。
2. **监控永不*自动*干预；干预只在你显式下达时发生 (No auto-intervention; only audited, user-issued control)。**
   三个监控支柱**永远纯只读**——绝不自动替 Claude Code 做决定、不自动点 permission、不自动 kill/改进程或文件。
   *控制*是**单独一层**（控制层 / control plane，见 §8 P7）：它只执行**你本人显式下达**的、allow-list 内的指令，
   每条二次确认 + 鉴权 + 全审计，绝不自动动作。监控器/控制层挂掉都不能影响正在 coding 的你。
   （这是对早期「绝不干预」的**有意改写**：从"系统不动手"变成"系统从不*自动*动手，只有你、显式、经审计地动手"。）
3. **本地优先；外发显式 opt-in 且内容最小化 (Local-first; minimal opt-in egress)。** 默认一切在本机。
   通知是**唯一**会外发的能力，且默认关闭、显式开启、内容最小化（见 §6）。这是对 tokmon「本地不外发」的**受控破例**，必须慎之又慎。
4. **渐进可降级 (Graceful degradation)。** 缺某数据源 → 那个支柱降级而非全局崩；通知通道挂了 → 不影响监控；
   任何单点异常不拖垮全局。
5. **判断与打扰分离 (Decide-what-happened ≠ decide-whether-to-bother)。** 这是平台的**灵魂**：
   监控层只负责判断「发生了什么」（产出事件）；通知层只负责决定「该不该打扰你、以及怎么打扰」。
   两者通过统一事件层解耦。不分离，后面必成屎山。
6. **有用且不烦 (Useful, not annoying)。** 默认静默。不是什么都通知。靠**严重度 + 静默时段 + 去抖**
   让重要的事浮上来、噪音沉下去。一个让人想关掉的通知系统，是负价值。
7. **支柱独立 (Pillars stay pure)。** token / 进程 / 对话活动各有自己的内核，互不污染。
   Mission Control 是**编排者**，消费各支柱的输出，不把它们搅成一锅（参照 tokmon I4/I6）。
8. **小步进化，每步可用且不烦 (Always shippable, always quiet-by-default)。** 每个版本都是能独立交付价值的完整切片，
   且默认不打扰——先让它「有用且不烦」，再谈丰富。

---

## 3. 三层架构 (The Three Layers · 平台脊柱)

数据单向流动：**监控层 → 事件总线 → 通知层**。这是不可逆的方向，也是 §2.5 解耦原则的落地。

```
┌─ Layer 1 监控层 (Observation) ──────────────────────────────┐
│  各支柱产出"原始信号":                                        │
│   · 成本: token 用量 / burn rate / 预算占比   (tokmon 内核)   │
│   · 进程: 进程健康 / 端口 / cloudflared        (procmon)      │
│   · 对话活动: session 活跃/空闲/卡住 / 错误 / permission /    │
│            重复编辑 / 大 diff / 敏感文件触碰  (新支柱, 见 §5) │
└───────────────────────────┬─────────────────────────────────┘
                            │  raw signals
┌─ Layer 2 事件总线 (Event Bus) ──────▼────────────────────────┐
│  把信号判定成"有类型的事件"(§4), 带严重度/来源/最小载荷。      │
│  这是解耦层: 监控层只往这里发事件, 从不直接调通知通道。        │
└───────────────────────────┬─────────────────────────────────┘
                            │  typed events
┌─ Layer 3 通知层 (Notification) ─────▼────────────────────────┐
│  规则 + 严重度 + 静默时段 + 去抖 -> 决定该不该打扰、怎么打扰。  │
│  通道是可插拔的 output: Telegram / Pushover / Email / ...     │
│  也驱动 UI: 左=sessions 状态, 中=事件 timeline, 右=通知规则。 │
└──────────────────────────────────────────────────────────────┘
```

**产品形态（Mission Control 三栏 UI 的目标）：**
左栏当前 sessions（active / idle / 卡住 / 进程健康 / token 占比 / 最后输出时间）；
中栏事件 timeline（按时间的状态/错误/重试/告警流）；右栏通知规则（idle>10min→push、token>80%→push、crash→urgent…）。

---

## 4. 事件契约 (Event Contract) · v1

事件是平台的**通用货币**。通道、UI、规则都消费事件，不消费原始信号。下面是 v1 事件集（**可演进的契约**，
新增/改名要在本节登记，像 tokmon §5 的数据契约一样郑重）。每个事件至少带：
`type` / `severity` / `pillar`(来源支柱) / `session` / `project` / `timestamp` / **最小载荷**（§6）。

| 类别 | 事件 type | 默认严重度 | 说明 |
|------|-----------|-----------|------|
| **状态** | `SESSION_STARTED` | Info | 新 session 开始 |
| | `TASK_COMPLETED` | Info | 一轮任务完成（回到等待输入） |
| | `TASK_FAILED` | Warning | 任务以错误收尾 |
| | `SESSION_IDLE` | Warning | 空闲超过阈值（如 10min 无新动作） |
| | `SESSION_STUCK` | Warning→Critical | 标称"运行中"却长时间无输出（疑似卡住） |
| | `PERMISSION_NEEDED` | Critical | 在等你点 permission，**进度被你阻塞** |
| | `QUESTION_PENDING` | Critical（**当前 Warning**） | 在等你答 AskUserQuestion 选择题，**进度被你阻塞**（PERMISSION_NEEDED 的亲兄弟；Type B 可远程答，Type A 仅通知，见 §9 Fleet Cockpit v0）· ✅ 0.15.1：Claude Code 进程自报 waiting 持续 60s 发一次 |
| | `LONG_RUNNING_TASK` | Info→Warning | 单任务超长 |
| | `POSSIBLE_LOOP` | Critical | 疑似循环（反复同类动作） |
| **成本** | `TOKEN_BUDGET_WARNING` | Warning(70%)→Critical(90/95%) | 接近/越过预算阈值 |
| | `BURN_RATE_SPIKE` | Warning | 每分钟 token burn 异常飙升 |
| | `DAILY_BUDGET_EXCEEDED` | Warning | 某项目/今日累计越线 |
| | `CONTEXT_LARGE` | Info | 正在跑的会话主线程上下文刚过 30 万（每越过一次一条；浏览器铃铛勾了才弹）· ✅ 0.21.0 |
| **质量/风险** | `DESTRUCTIVE_OP` | Info（**当前**） | rm -rf / git reset --hard / 强推 / 整棵树还原 / MCP 删除类 / DROP（只标不拦）· ✅ 0.15.0 |
| | `ERROR_SPIKE` | Critical（**当前 Info**） | 连续多次失败（同一时间线严格相邻 ≥ 3）· ✅ 0.15.0 |
| | `REPEATED_FILE_EDIT` | Warning（**当前 Info**） | 反复编辑同一文件（改 → 验证失败且失败点了它的名 ≥ 3 轮）· ✅ 0.15.0 |
| | `LARGE_DIFF` | Info→Warning（**当前 Info**） | 单次调用增删 ≥ 1200 行 / 一个任务动 ≥ 200 个文件 · ✅ 0.15.0 |
| | `SENSITIVE_FILE_TOUCH` | Warning | 改 package / config / env / 密钥类文件 |
| | `SENSITIVE_DIR_TOUCH` | Warning→Critical | 触碰敏感目录 |
| **进程** | `PROCESS_CRASHED` | Critical | 被监控进程消失/崩溃 |
| | `CLOUDFLARED_DOWN` | Warning | 隧道进程不在了 |

> 阈值（idle 多久、budget 几 %、diff 多大）都走配置，不写死。新增事件先问北极星：它能帮我回答
> 「在推进/卡住/烧钱/乱改」吗？不能，就先别加（防事件蔓延，呼应 tokmon §10）。

---

## 5. 数据来源与契约风险 (Data sources)

平台的判断力来自三类只读数据源，**全部不 hook、不拦截 Claude Code**，只读它已经写下的东西 + 看本机进程：

| 支柱 | 来源 | 推断的事件 | 风险 |
|------|------|-----------|------|
| 成本 | `~/.claude/projects/**/*.jsonl` 的 `usage` | budget/burn rate | 依赖未公开格式（tokmon §5 的头号风险） |
| 对话活动 | 同上 transcript：最后一条消息时间戳、`tool_result` 错误、permission 行、文件编辑动作 | idle/stuck/error/permission/repeated-edit/large-diff/sensitive-touch | **高**：idle/stuck/permission 检测强依赖 transcript 结构，格式漂移会让判断失灵 |
| 对话活动（0.15.1 起优先） | `~/.claude/sessions/<pid>.json`：Claude Code 自己写的 pid→会话归属 + 回合状态（busy/idle/waiting）| 在跑/等你/等授权/已关闭 | 中：未公开格式；目录不存在或字段不认识 → 退回 transcript 推断（行为同 0.15.0） |
| 进程 | psutil（procmon） | crash / cloudflared down | 中：跨平台进程语义差异 |

**护栏：**
- 对话活动检测必须**容错**：字段缺失 → 该事件降级/跳过，绝不误报成 Critical（误报比漏报更伤信任）。
- 复用 tokmon 的 `tokmon doctor` 精神：新增「活动检测」后，要有办法验证「它判定的 idle/stuck/permission 与现实一致」。
  升级 Claude Code 后，先确认活动检测没漂移，再信它的告警。
  **✅ 决定（2026-06-29）：把这条"doctor 精神"从口号兑现成交付物 —— M4.5 的「推断 doctor」（见 §9）。**
  当前的不对称值得正视：成本（**测量**）有 `doctor` 能对过账；但 activity/events/notify/control（**推断**）至今只有单测 + 对抗式 review，
  **没有 runtime 的「对真相」校验器**。当我们已经*基于推断去行动*（控制层已上线），未经验证的推断就是这座塔的软肋。
- 我们**只读 transcript + 看进程**，绝不 hook Claude Code、不注入、不拦网络（§2.2）。

---

## 6. 通知与隐私 (Notification & Privacy) · 受控破例

通知是平台**唯一**会把数据送出本机的能力，因此基线收得很紧（用户已敲定：**显式 opt-in + 内容最小化**）：

- **默认全关。** 不配置就一条都不发。开启是逐通道、逐规则的显式动作。
- **内容最小化（默认）。** 一条通知默认只含：`事件类型 / 项目名 / 关键指标(如 82%) / 严重度 / 时间`。
  **命令行、代码、diff、文件绝对路径、密钥**等敏感上下文**默认不外发**；要发需**单独显式开启**「丰富上下文」。
- **Telegram = 双向通道（已敲定）。** 通知*出*、指令*入*走同一条：本机只做**出站长轮询**（`getUpdates`），
  **不开任何入站端口、不暴露看板**——这是手机能「监控 + 下指令」却几乎零攻击面的关键。指令入站后走 §8 P7 的护栏。
- **通道是可插拔 output（按推荐顺序）：**
  - **第一版：Telegram（双向）或 Pushover（仅出）**——手机即时推送、可结构化、按项目分频道/分 topic。最贴合个人工具。
  - **第二版：Email digest**——任务完成/超预算/崩溃/每日总结。稳定跨平台，但实时性一般、易淹没，**适合摘要不适合实时**。
  - **bonus（非核心）：** iMessage / Apple Shortcuts——能玩，但 Apple 自动化稳定性与权限偏玄学，**不作主通道**。
- **去敏在源头。** 外发前复用 procmon 的脱敏思路（token/key/URI 口令等），且最小载荷本就不含这些。

---

## 7. 严重度与「不烦」契约 (Severity & don't-annoy)

让系统「有用且不烦」靠这套机制，它和事件契约同等重要：

- **三档严重度：**
  - **Info**：session started、task completed —— 默认**不推送**，只进 UI timeline / 日摘要。
  - **Warning**：token 70%、idle 10min、重复重试 —— 默认**安静推送**（可被静默时段压制）。
  - **Critical**：进程崩溃、token 95%、permission needed、疑似循环 —— **优先推送**，必要时叠加 Email。
- **静默时段 (Quiet hours)：** 例如夜间只放行 Critical，其余攒到次日摘要。
- **去抖 / 去重 (Debounce)：** 同一事件不反复轰炸；idle 每超一个台阶提醒一次而非每分钟一次。
- **默认静默 (Default to silence)：** 拿不准要不要打扰时，**不打扰**。宁可在 UI 里能看到，也不轻易推送。

---

## 8. 架构不变量 (Invariants — 别破坏这些)

进化过程中，下面必须始终成立。破坏其一 = 走偏：

- **P1 监控只读。** 只读 transcript + 看进程；绝不修改 Claude Code 的文件/配置/行为，绝不自动点 permission、不 kill 进程。
- **P2 判断与打扰分离。** 监控层只发事件到事件总线，**永远不直接调用任何通知通道**。
- **P3 事件是唯一契约。** 通道 / UI / 规则只消费事件（§4），不绕过去读原始信号。新增维度优先加成事件字段。
- **P4 支柱独立。** token / 进程 / 对话活动各自的内核互不引用；平台只在事件总线层汇合（参照 tokmon I4/I6）。
- **P5 默认静默、默认不外发。** 通知默认关、内容默认最小；要更吵/更详细都得显式 opt-in。
- **P6 误报零容忍倾向。** 拿不准就降级严重度或不发；一次 Critical 误报对信任的伤害 > 十次正确 Info。
- **P7 控制层守则 (Control plane discipline)。** 任何「下达指令」能力必须满足全部：
  ① **allow-list**：只有显式登记的指令类型可执行，其余一律拒绝；
  ② **你显式触发**：系统永不自动下指令，每条都来自你本人的明确动作；
  ③ **二次确认 + 鉴权**：执行前确认，且校验身份（手机侧令牌/密钥）；
  ④ **全审计**：每条指令落审计日志（谁/何时/做了什么/结果），并发一条 `COMMAND_ISSUED` 事件进总线；
  ⑤ **失败安全**：够不到目标（如运行中的 Claude Code 无 API）时**明确报"做不到"**，绝不伪装成功、绝不瞎试有副作用的动作。
  控制层与监控支柱在代码上**分离**：监控只读, 控制单独成层, 不让控制逻辑污染只读采集。

---

## 9. 演进路线图 (Roadmap)

每阶段 = 一个可独立交付、且**默认不烦**的切片。**按顺序做，但每步都先问北极星，并先验证"不误报"。**

> **战略位置（2026-06-29 · 本次方向评审定）：** M0→M4 的地图基本走完，剩下的多是"填充"而非"前沿"。三个判断决定接下来的次序：
> 1. **过了卢比孔河。** M4 让平台从「观测者」变成「行动者」（控制层上线）。引力开始朝**「舰队远程驾驶舱」**偏——这是机会，也是要主动按住的口子。
> 2. **信任有不对称（§5）。** 测量侧（tokmon）有 `doctor` 对过账；推断侧（activity/events/notify/control）没有。整座塔架在「可信推断」上，但**目前只有成本这层地板对过了账**。
> 3. **「已交付」≠「在承重」。** 通知 / 控制这条链路（平台真正的回报）很可能**一次都没在真实手机上闭过环**（缺 Telegram 账号，已 deferred）。
>
> **决定：先固信，再拓宽。** 下一步 = **M4.5 收口固信**（推断 doctor + 手机闭环验证）；信任锁死后下一个大赌注 = **舰队驾驶舱**；**语义层**（天花板最高、对原则 1 威胁最大）**显式推迟**到信任锁死之后。理由：在已经*基于推断行动*之后，最高杠杆的不是再加一层，而是先确认下面没有空心。

- **M0 · 已立住的地基（已交付）**
  - 成本支柱：tokmon（watch/report/doctor/serve，含「vs 上一周期」自基线）。
  - 进程支柱：procmon（进程/端口/连接/cloudflared/健康探测，纯只读）。
  - 一个 `tokmon serve` 外壳，主页分流 `/tokens` + `/processes`。

- **M1 · 监控层：对话活动（先看见，不通知）** ✅ **已交付**
  - `tokmon/activity.py`（**支柱独立**：只依赖 stdlib + discovery + project，不碰 token 内核/procmon）从 transcript
    **尾部**推断每个 main session 的状态：`WORKING / PROCESSING / AWAITING_USER(含 idle) / AMBIGUOUS_PENDING / UNKNOWN`。
  - `/sessions` 页（Mission Control 左栏雏形）：状态徽章 + 项目/分支/模型 + 最近活动时间 + 每会话 mini-timeline；**零通知**。
  - **诚实纪律（本切片的重点）**：时钟=最后一条*消息*时间戳（非 mtime）；挂起只看尾部位置（不做 id 配对）；
    **绝不伪造 PERMISSION_NEEDED / STUCK**——久未返回统一并列「长任务/等授权/会话已关闭」；格式漂移→该行 UNKNOWN。
  - `classify_state` 是纯函数, 13 个单测钉住每个状态与诚实边界。经对抗式 review（11 agent）确认并修复 7 项
    （含实测发现的「list 形态打断被误判为活跃」honesty bug）。
  - **完成判据**：开着 `/sessions`, 一眼看出每个 session 在推进/久未返回/等我。✅
  - **0.15.1 修正（进程自报优先）**：Claude Code 自己在 `~/.claude/sessions/<pid>.json` 写着「进程承载哪个会话 + 这一轮
    忙/空闲/等授权」。procmon 读它（校验 pid 活着、防 PID 复用），activity 有它就以它为准，transcript 只补细节：
    新增状态 `BLOCKED_ON_USER`（等你授权/回答，**进程自报，不是推断**）；「久未返回」只在拿不到自报时出现；
    同项目的活进程都登记了别的会话 → 可证已关闭。卡在你身上持续 60s → `PERMISSION_NEEDED` / `QUESTION_PENDING`（来自进程
    自报，transcript 仍不合成）；进程重启杀掉的上一轮转「等你」不算 TASK_COMPLETED。见 CHANGELOG 0.15.1。

- **M2 · 事件总线** ✅ **已交付**（按用户要求：3 个子版本竞争 → 评审留最优 + 嫁接）
  - `tokmon/events.py`（**支柱无关**：Event + 严重度表 + §6 payload allow-list + EventBus；import 任何 pillar 都不允许）
    + `tokmon/event_sources/activity_source.py`（纯 `derive_events` + **唯一只读 daemon pump**，复用 activity 快照 TTL、零额外读盘）。
  - 事件：`SESSION_STARTED / TASK_COMPLETED / TOOL_ERROR / SESSION_IDLE(台阶) / SESSION_STUCK(对冲)`。`/api/events` + `/sessions` 时间线消费它。
  - **诚实纪律**：时钟=消息ts或跨阈那刻（非 now/mtime）；**两层幂等**（源头边沿+单调台阶 / 总线 dedup_key）；
    UNKNOWN→不发；从 UNKNOWN 恢复**静默重播种**不重放；冷启动基线静默；**绝不合成 PERMISSION_NEEDED**。
  - **仍零通知、零控制**。总线**无策略**（§7 防打扰全留给 M3 消费者边），M3/控制层/成本/进程支柱**零改动**即可挂上。
  - 9 个单测钉幂等/边沿/台阶/UNKNOWN恢复；P4 import-graph 实测干净；经对抗式 review（11 agent）修复 6 项（含 UNKNOWN 恢复重放 bug）。
  - **完成判据**：新增事件类型不用改 UI/通道结构就能流起来；实测基线静默后真实 `TASK_COMPLETED` 经 `/api/events` 流出。✅

- **M3 · 通知层 v1（先单向出, 有用且不烦）** ✅ **已交付**
  - `tokmon/notify.py`：总线**消费者**（只 import stdlib + events，P4）。纯策略 `decide`：严重度门控 → 静默时段(只放行 critical)
    → 去抖(同 类型+会话) → 限流；**critical 优先推送，不被去抖/限流挡掉**。
  - **不阻塞总线**：`on_event` 只做内存决策+入队，真正的 Telegram **出站发送在后台 sender 线程**。
  - **默认不外发**：没配 token 就一字节不出本机（首个 egress 的显式 opt-in）；**内容最小化**（严重度/类型/项目/极简详情，无命令行/路径/密钥）；**token 绝不回显**。
  - `/notify` 页：状态 + **推送/抑制双记的 feed**（看为什么被抑制, 方便调"不烦"）+ 发送测试 + BotFather 配置指引。`MC_TELEGRAM_TOKEN`/`MC_TELEGRAM_CHAT_ID` 开启。
  - 7 个策略单测；经对抗式 review（安全/线程/解耦三维）修复 4 项（含 critical 被限流淹没）。
  - **完成判据**：重要时刻**主动**找到我、无关紧要时**安静**；默认安全、可在 feed 里调参。✅（手机实推待你配 token 验证）
  - **0.18.0 本机浏览器通道**（会话驾驶舱 S2，见 [SESSIONS_COCKPIT_PLAN.md](SESSIONS_COCKPIT_PLAN.md)）：导航铃铛，默认关；只对进程自报的
    `PERMISSION_NEEDED` / `QUESTION_PENDING`（卡在你身上 ≥ 60 秒，每段等待一次）弹浏览器通知，**零外发**，不经 `notify.py` 的外发通道。
    09-21「通知搁置」只为这一条部分推翻；手机推送与远程控制仍搁置。真机验证过（66 秒弹出、只弹一次）。

- **M3.5 · 成本预算 / 阈值告警** ✅ **已交付**
  - `tokmon/event_sources/cost_source.py`（成本支柱适配器, 只 import token 内核 + events, P4）: 算今日/近7天滚动/每项目等价花费,
    越过 70%/90%/100% 发 `TOKEN_BUDGET_WARNING`（70%=warning, 90%+=critical → 绕过去抖/限流, M3）。
  - **幂等**: 每 tick 只发"当前最高越过台阶", dedup_key=`scope:周期:阈值`（日/周都按*天*周期, 跟滚动窗对齐）; 总线对持续重发的键 `move_to_end` 不老化 → 不重响。
  - 预算可在 `/tokens` 页**直接设**（日/周 $, token 鉴权的表单, 无 token→403）, 或手改 `~/.tokmon/budget.json`（每项目预算走文件）。进度条 70/90 变色。
  - §6: 事件/通知只带 {scope, pct} + 项目名, 绝不带 $ 明细/路径。60s pump（比活动 5s 慢, 预算不需秒级）。
  - 6 个单测; 经对抗式 review 修复 4 项（含 周窗口/dedup周期错配、inf 预算、dedup 老化重响、表单被自动刷新清空）。
  - **完成判据**: 越线主动提醒, 默认安全、UI 可设。✅（手机实推等 Telegram）

- **M4 v0 · 控制层（远程审批 + 可靠检测）** ✅ **已交付**
  - **可行性研究改写了路径**：Claude Code 有官方 **`PermissionRequest` http hook** —— 干净地远程 allow/deny permission，**无需脆弱的按键注入**。
  - `tokmon/control.py`（只 import stdlib + events + 纯 project 工具）。hook(`type:http`)→ 本服务 `/hook/permission`：
    检测时**发真实 `PERMISSION_NEEDED` 事件**（终于补上 M1 测不准的缺口）；远程模式下**阻塞**等你在 UI/手机点 allow/deny。
  - **全程 P7**：① 只做 permission 审批 ② 你显式点击才决定（超时=defer，非决定）③ token 鉴权（hook 用 query token；看板用 `X-Control-Token` 头，关掉 CSRF/本机伪造）④ 每决定进审计 + 发 `COMMAND_ISSUED` 事件 ⑤ **失败安全铁律**：token 错/超时/异常/远程关/服务没开 → 一律回退正常本地弹窗，**绝不**自动批/拒。
  - 激活：默认 `remote_mode=False`（不影响日常本地弹窗）；UI 开「远程审批模式」才路由。hook 配置**只给 JSON 你手动加**（含 token），需重启会话生效。
  - `/control` 页：远程模式开关 + 待审批(允许/拒绝) + 审计日志 + hook 安装片段。5 个单测；经对抗式 review（P7/失败安全 + 阻塞线程/鉴权 + 并发/前端三维）修复 7 项（含 decide/mode 未鉴权的伪造/CSRF 缺口）。
  - **nudge / 停止**：研究确认**无干净 API**（hook 只能注入上下文、不能执行指令）→ 按失败安全原则**不做**（不靠脆弱方式硬来）。
  - **完成判据**：能从网页远程批准/拒绝 permission，全程审计、失败回退本地。✅（手机实推等 Telegram，入站走同一端点）
  - 风险事件（`REPEATED_FILE_EDIT` / `LARGE_DIFF` / `SENSITIVE_FILE_TOUCH` / `ERROR_SPIKE`）仍属 backlog（后续阶段）。

- **M4.5 · 收口固信（Validate & Harden）** — (a) L1 已交付 / L2·L3·L4 + (b) 待做 ｜ 详 [M4.5_PLAN.md](M4.5_PLAN.md)
  - **目标**：在再加支柱之前，确认整座塔下面没有空心——让**推断**层和**测量**层一样可信，并让平台的回报真的闭一次环。
  - **(a) 推断 doctor** ✅ **已交付**：`tokmon/inference_doctor.py` —— 给 activity/events 一个类比 `tokmon doctor` 的「对真相」校验器，
    复用 activity 实际所见的尾部，量化每条**载重假设**: 时钟(末条消息ts vs mtime, 实测 25/48 偏离→证明不能用 mtime)、行类型漂移、
    stop_reason 完整性、挂起判定(尾部位置而非 id 配对)、思考可用性、标题覆盖、UNKNOWN 率。`tokmon doctor` 现在跑**两半**(测量+推断); 新增 `/doctor` 页 + `/api/doctor`。
    **首跑即抓到 3 件真事并当场修掉**: `custom-title`(用户自定义标题, 现优先于 ai-title)、`max_tokens`(未处理的 stop_reason, 现判为"自动续写中"而非"等你")、
    **thinking 块约 29% 末段非空**(此前误以为恒空→现在 `/sessions` 优先显示**真实思考 💭**, 拿不到才回退叙述)。修完 doctor warns 3→0。补上 §5 的"doctor 精神"欠账, 兑现 P6。
    （此为四 lens 里的 **L1 漂移**——静态快照、只读尾部。决定**四 lens 全做**：**L2 准确率回测（时间旅行，用 transcript 自己的未来当真值）/ L3 事件完整性回放 / L4 打扰预算**仍待做，详 [M4.5_PLAN.md](M4.5_PLAN.md)。）
  - **(b) 手机闭环 —— 决定先用 Pushover/Email 替代 Telegram**：往 `notify.py` 加薄 **Pushover 出站适配器**（可插拔/默认关/内容最小化, 同 §6 纪律；severity→推送优先级），
    最快让手机真的震。**诚实边界**：Pushover/Email **只出不收** → 闭"通知出"那半环（验"有用且不烦"）；"**远程批 permission（控制入）**"在本切片走网页 `/control`（localhost 或 cloudflared 隧道, 权衡入站面）；手机消息里点允许/拒绝**显式留待 Telegram 双向**。live 用约一周, 用 `/notify` feed + L4 回测对账打扰率。
  - **完成判据**：四 lens 齐（L1 ✅; L2 出每状态精确率+混淆矩阵+可判/不可判拆分; L3 全量 0 违例+事件对账; L4 出"一周会震 N 次"预算表）；Pushover 通道 live、手机真震过、打扰率实测可接受；远程审批经 `/control` 验证可用（手机消息审批留待 Telegram）。

- **然后 · 舰队驾驶舱（Fleet Cockpit）— 信任锁死后的下一个大赌注**
  - **目标**：顺着 M4 控制层往下，围绕**N 个并行 agent**重构——worktree / 后台任务 / 多机 / 云端 agent。
    左栏从「一个面板」升级为**产品本身**；多机汇总从 v2 候选升为中心；手机成为 hub。
    原 **M5**（三栏 UI sessions | timeline | 规则、每会话 token+进程**融合**、按项目分频道、规则可视化编辑）并入本赌注的**单机切片**。
    可重新评估当年"nudge / steer 无干净 API → 不做"的结论（Claude Code 的 SDK / headless 表面已变大）。
  - **⚠ 北极星警示**：这会把平台从"观测 + 最小审批"推向"**舰队远程操控**"，是对当前北极星的**有意拓宽**，要作为显式选择来做、别滑过去（与 §10「除非北极星本身改变」呼应）。
  - **完成判据**：「我有 6 个 agent 跨 3 台机器 / 2 个云环境，告诉我哪个需要我」能在 5 秒内回答。
  - **▶ v0（提前启动 · 因"手机答题"硬需求）** ⬅ 详 [FLEET_COCKPIT_V0_PLAN.md](FLEET_COCKPIT_V0_PLAN.md)
    - **痛点**：大任务里 Claude 用 AskUserQuestion 提问后卡住，用户既没及时通知、也没法手机上答选择题。
    - **核实**：手机答题**只能对 SDK 起的 agent（Type B）** 成立——`canUseTool` 命中 `AskUserQuestion` 可回填 `answers` 解阻塞；
      手敲交互式 Chat（Type A）无干净 API（同 nudge/stop），仅能检测+通知。
    - **竖切**：mission control 用 SDK **忠实复刻环境**起一个 Type B agent → `canUseTool` 拦 AskUserQuestion → 发 `QUESTION_PENDING`(§4)
      → 经 **Telegram 双向**（inline keyboard=选项，回文字=Other）推题、收答、回填 → 全程 **P7 + 失败安全**（超时不伪造、泊+停、绝不自动选）。
    - **诚实账**：**不欠推断债**（canUseTool 是 ground truth，非猜）；代价是打开**至今最深的控制面** + 引入首个重依赖 `claude-agent-sdk`（**仅在 spawn/控制模块**，不碰只读内核/三支柱）。
      产生**两类会话**：Type A 观测+通知、Type B 可驾驶。**M4.5 推断 doctor 是观测侧、对 Type A 仍全用 → 并行不砍。**
    - **通道重逢**：答题需"能收" → **Telegram 双向回到台前**（翻转 M4.5(b) 的 Pushover-only：Telegram = 控制入+通知出主通道，Pushover 退为可选纯通知）。
    - **v0 非目标**：N agent / 多机 / 融合视图 / 任意远程 steer —— 全留大赌注，v0 只"答已问出的题"。

- **暂缓 · 语义层（Semantic）— 天花板最高，但对原则 1 威胁最大**
  - 把 activity 从**结构性**状态（WORKING / IDLE）升到**语义**：不只是"WORKING"，而是"重构 auth 模块 20min、刚在同一文件第 3 次测试失败"——
    在语义层回答"推进 / 卡住 / 乱改"。但它需要 LLM 读 transcript（成本 / 隐私 / 可能外发 / 非确定性 / 误判 → 误报 → 信任崩）。
  - **决定（2026-06-29）：显式推迟到信任锁死（M4.5）之后再碰。** 若做，优先「本地小模型 / 廉价模型摘要」路线以守 local-first（§2.3）。

- **backlog · 风险事件（"乱改"象限——四象限里最弱的一格）**
  - `REPEATED_FILE_EDIT` / `LARGE_DIFF` / `SENSITIVE_FILE_TOUCH` / `ERROR_SPIKE`——事件流之上的质量 / 风险源。
    **✅ 0.15.0（改动与风险 S3）落地了其中三类 + `DESTRUCTIVE_OP`**，规则与 `/workflow` 回放同一套（`trace.session_brief`）；
    精确率审完之前**一律 info**（notify 不推 info → 通知仍关），payload 只有规则名 / 计数 / 种类，不带路径与命令（§6）。
    `SENSITIVE_FILE_TOUCH` 仍在 backlog（见 [CHANGE_RISK_PLAN.md](CHANGE_RISK_PLAN.md) §6）。
    推进（events）、卡住（activity）、烧钱（tokmon）都有了，**"它在干危险 / 蠢事吗"是唯一还空着的一格**。可走填充路线，也可作为语义层的入口。

---

## 10. 非目标 (Non-Goals)

写下来抵御功能蔓延对北极星的稀释：

- ❌ **不*自动*干预 coding。** 系统永不自动点 permission、不自动改/回滚代码、不自动 kill。
  *控制*只在你**显式下达**且经 §8 P7 护栏（确认/鉴权/审计）时发生——我们观测，不**自作主张**接管。
- ❌ **不什么都通知。** 默认静默；噪音通知是负价值。
- ❌ **不默认外发敏感内容。** 命令行/代码/diff/路径默认不出本机。
- ❌ **不 hook / 不拦截 Claude Code。** 只读 transcript + 看进程。
- ❌ **不把 Apple 自动化当核心通道。** iMessage/Shortcuts 仅 bonus。
- ❌ **不做团队 SaaS / 多租户。** 这是给「我自己」的本地 ops 工具（除非北极星本身改变）。

---

## 11. 一句话给未来的自己

> 它在替我跑的时候，让我知道它在**推进 / 卡住 / 烧钱 / 乱改**——
> 并且只在**重要时刻**、用**恰当方式**提醒我，其余时候**安静**。
> 每加一个事件、一条通知、一个通道，先问：这让那件事更清楚、或那次打扰更精准了吗？
> 不是，就先别加。**有用，且不烦。**
