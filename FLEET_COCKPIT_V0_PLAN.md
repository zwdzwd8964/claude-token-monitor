# Fleet Cockpit v0 · SDK-spawn + 手机远程答题（AskUserQuestion）

> 给主程序的实现交接。设计与约束，非最终代码。上层方向见 [MISSION_CONTROL.md](MISSION_CONTROL.md) §9「舰队驾驶舱」。
>
> **动机（真实痛点）**：用户给 Claude Code 大任务、让它有问题用 AskUserQuestion 选择题问；但它常在一大段 MCP 运算+思考后卡在提问上，
> 用户**得不到及时通知**、**也没法在手机上答那些选择题**。
>
> **已敲定的决定链：**
> 1. 手机答题是**硬需求** → **提前启动 Fleet Cockpit v0**（本切片），不等"先固信"走完。
> 2. 答题往返通道 = **Telegram 双向**（inline keyboard 按钮=选项，回复文字=Other，零入站端口）。
> 3. 范围 = **一条端到端竖切**，Fleet Cockpit 其余部分（N agent/多机/融合视图/左栏成产品）**不碰**。

---

## 0. 可行性依据（已核实，官方文档）

来源：code.claude.com/docs 的 hooks / agent-sdk（user-input / agent-loop / sessions）页。

- **交互式 `claude` Chat（Type A）**：AskUserQuestion 是普通工具，PreToolUse hook 能**检测**（payload 含题目+选项），但 hook **不能提供工具结果** → **答案塞不回去**（同 nudge/stop 的"无干净 API"）。
- **SDK 起的 agent（Type B）**：`canUseTool` 回调在 agent loop 内，命中 `tool_name == "AskUserQuestion"` 时可返回
  `PermissionResultAllow(updated_input={"questions": [...], "answers": {...}})` → **把答案喂回、当场解阻塞**。这是官方支持的路径。
- **∴ 手机答题只能对 Type B 成立**——这正是本切片存在的理由。

---

## 1. 诚实的账（把关结论）

**不欠新推断债**：canUseTool 是 SDK **确定性**告知"AskUserQuestion 触发了"——ground truth，不是从 transcript 猜。故本切片**不违背**"先固信再拓宽"的初衷（那针对的是"在没校准的推断上行动"）。

**代价 = 打开至今最深的控制面**：从"观测+审批一个 permission"升级为"mission control **亲自 spawn 并驾驶** agent"。P7 全程罩住，答一题 = 一条 `COMMAND_ISSUED`。

**新现实 = 两类会话**（/sessions 必须区分）：
- **Type A** 手敲交互式 Chat：只能**观测 + 通知**（QUESTION_PENDING 经 PreToolUse hook 或尾部检测），答案塞不回。
- **Type B** 经 mission control SDK 起：**可完整驾驶**，含手机远程答题。

**M4.5 推断 doctor（L2/L3/L4）是观测侧，对占多数的 Type A 仍完全有用 → 并行继续，不砍。**

**依赖影响（要正视）**：本切片引入第一个重外部依赖 **`claude-agent-sdk`**（Python，与 tokmon 同栈）。它**只活在 spawn/控制模块**里，**不碰只读内核与三支柱**（serve 的"纯标准库、可离线"对 Type A 监控依然成立）。控制层与只读采集在代码上分离（§8 P7）。

---

## 2. v0 竖切（只证一件事：起一个任务、手机答它的题）

```
你(网页/Telegram 下任务)                          你的手机(Telegram)
        │                                              ▲   │答案
        ▼                                              │题 │(点按钮/回文字)
┌─ mission control (控制层, 新 spawn 模块) ────────────┼───┼────────┐
│  1. SDK 起 Type B agent (headless+streaming, 本机)   │   │        │
│  2. agent 调 AskUserQuestion → canUseTool 命中 ──────┘   │        │
│     · 发真实 QUESTION_PENDING 事件 (ground truth)        │        │
│     · 经 Telegram 出站推题目+inline keyboard ───────────┘        │
│     · 异步阻塞等你答 (有界, 见 §4 失败安全)                       │
│  3. Telegram getUpdates 收到你的选择 ◄──────────────────────────┘ │
│     · 校验(chat_id + 待答 nonce) → 组 answers dict                 │
│  4. canUseTool 返回 PermissionResultAllow(updated_input=answers)  │
│     → agent 当场解阻塞, 继续跑; 落审计 + COMMAND_ISSUED           │
└──────────────────────────────────────────────────────────────────┘
```

**Spawn 忠实度（关键，否则是玩具替身）**：Type B agent 要尽量复刻你平时 Chat 的环境——同 **tools / MCP servers / 模型 / CLAUDE.md / 权限设定 / cwd**。用 `ClaudeAgentOptions` 对齐；差异要在 /sessions 上标出来，别假装等价。

---

## 3. Telegram ↔ AskUserQuestion 映射（grounded 机制）

AskUserQuestion：1–4 题，每题 2–4 选项，可 multiSelect，且永远有 "Other" 自由文本。

- **单选题**：一条消息 = 题干 + N 个 inline 按钮；`callback_data` 编码 `会话id:题index:选项index:nonce`。点一下即答。
- **多选题**：按钮点了 toggle（回显 ✓），加一个「✅ 确认」按钮收尾。
- **Other**：提示"或直接回复文字"；你回的文本消息 → 该题答案。
- **多题**：顺序发多条，全部集齐再回填。
- **回填格式**：`answers` 以**题干文本为 key**、值为所选 label（多选为 list）：
  `updated_input={"questions": <原样回传>, "answers": {"如何格式化输出?": "摘要", ...}}`。

---

## 4. P7 + 失败安全（控制层守则，全满足）

- **① allow-list**：v0 只允许"回答一个待答 AskUserQuestion"这一种指令。**不做**任意消息注入 / nudge / stop。
- **② 你触发**：只有你**显式点按钮/回文字**才产生答案；**永不自动选、永不默认答**。
- **③ 鉴权**：校验 Telegram update 来自你的 `chat_id` + 密钥；callback 必须匹配一个**在飞的待答 nonce**（防重放/伪造答题）。
- **④ 审计**：每次回答落审计（哪题/选了什么/何时）+ 发 `COMMAND_ISSUED`。
- **⑤ 失败安全（最关键，选错比不选更糟）**：
  - **绝不伪造答案**。够不到你 = 不动。
  - **SDK 约束**：canUseTool **不能无限等**（有 session timeout）。故用**有界窗口**；把 agent 泊在「等答」态（/sessions 可见）+ Telegram 通知"任务已泊，回来答"。
  - **推荐"泊车 & 恢复"**：窗口内没答 → 让 agent **优雅停**（回一个中性"用户暂不可达，请先停下/总结现状"的结果，而非瞎选一项）→ 任务泊住；你稍后答 → **resume 会话**（sessions.md 支持）把答案作为上下文续跑。是否 v0 就做 resume，还是 v0 先只"泊+停"、v0.1 再 resume，是实现取舍。

---

## 5. 新事件登记（§4 事件契约）

`QUESTION_PENDING` — 类别**状态**，默认 **Critical**（同 PERMISSION_NEEDED："你不答它就不动"）。
载荷（§6）：`session / project / 题目数`；**题目+选项文本属"丰富上下文"**——远程答题必须外发它们，故这是一次**显式 opt-in**（默认最小载荷只报"有 N 题待答"，开了富上下文才把题面送手机）。

> Type A 也复用这个事件：PreToolUse(AskUserQuestion) hook 或尾部"未配对 AskUserQuestion tool_use"→ 发 QUESTION_PENDING（仅通知，不可答）。

---

## 6. 通道重逢：Telegram 回到台前（翻转 M4.5(b) 的 Pushover-only）

M4.5(b) 当时选 Pushover/Email 只因"没 Telegram 账号 + 只需通知出"。但**答题需要能收** → Pushover/Email 办不到。
本切片让 **Telegram 双向**成为**答题通道**，它同时也能 notify-out。**结论**：Telegram = 控制入 + 通知出的主通道；Pushover 退为**可选的纯通知补充**（比如你只想要 Critical 震动、不想在那条会话里答题时）。

---

## 7. v0 非目标（留在大赌注里，别蔓延）

- ❌ N 个并行 agent 编排 / 多机汇总 / 云端 agent。
- ❌ 三栏融合视图、左栏成产品。
- ❌ 对 Type A 交互式 Chat 硬塞答案（无干净 API，不 hack）。
- ❌ 任意远程 steer（发新指令/改提示）——v0 只答已问出的题。

---

## 8. 落地次序 + 测试

1. **SDK spawn 骨架**：能起一个忠实复刻环境的 Type B agent、跑通一个简单任务（无问题）。
2. **canUseTool 拦截 + 本机回填**：先在 localhost 用写死答案验证"拦得住、喂得回、agent 解阻塞"这条最硬的闭环。
3. **Telegram 往返**：inline keyboard 出 + getUpdates 收 + nonce 校验 + answers 组装。
4. **P7/失败安全 + 泊车**：鉴权、审计、COMMAND_ISSUED、有界窗口、泊+停（resume 视情况）。
5. **QUESTION_PENDING 事件 + /sessions 两类会话标识**。

测试：canUseTool 回填、Telegram 回调解析、nonce 防重放、失败安全（超时不伪造）、answers 组装（单选/多选/Other）——纯函数部分合成用例钉死。
沿用 design → implement+单测 → 对抗式 review（重点查 P7 鉴权/伪造、失败安全不瞎答）→ 修确认项。
