# S1-v2 · runner 改用 Agent SDK — 拿到「远程多选按钮」

> **【状态章 · 2026-09-20 核对】✅ 已实施。** `tokmon/runner.py` 就是本文件的 SDK 版实现，
> `/api/control/asks` + `/api/control/answer` + `/control` 页的按钮区块均在。
> 仍未做的只有 §8 的 **A3「上手机」**——其前置 `MC_REMOTE` 已于 0.9.0 补上（见 CHANGELOG）。

> **给主程序的实现交接。设计与约束，不是代码。**
> 承接 [REMOTE_CONTROL_PLAN.md](REMOTE_CONTROL_PLAN.md)（S1 后端已交付）。
> **本文件只改一件事：runner 的驱动方式。** 其余（control 的 P7 gate、审计、serve 端点、UI 骨架）基本不动。

---

## 0. 一句话

把 `tokmon/runner.py` 从 **「subprocess 调 `claude -p`」** 改成 **「Agent SDK + `canUseTool`」**，
以拿到「**Claude 提问 → 手机/网页上出现按钮 → 你点一下 → 会话继续**」这个体验。

---

## 1. 为什么（三条都是实测，不是推测）

1. **只有 `canUseTool` 能给你按钮。** 它能拦下 Claude 的 `AskUserQuestion` 调用、拿到结构化的
   `{questions, options}`、把你的选择塞回会话。（官方 Agent SDK 能力，非 research preview。）
2. **Channels 做不到，反而更糟。** 实测跑通一个 channel 后读 `system/init` 事件：
   **channel 一激活，原生 `AskUserQuestion` 直接从工具列表消失**（复现 GitHub #40644）。
   Channels 只能做「自由文本聊天桥 + permission relay」，**给不了按钮**。
3. **现在的 `claude -p` 结构上就不可能。** 实测（sonnet，$0.145）：`-p` 是**一次性回合**，
   没有可返回的交互循环——模型把多选题**打成纯文本就结束了回合**（"A) Flask B) FastAPI C) Django"），
   压根不去调任何「问用户」的工具。**这不是调参能修的，是结构性的。**

---

## 2. 已敲定的决定

| 决定 | 说明 |
|---|---|
| 用 **Python** Agent SDK | tokmon 是纯 Python，SDK 进程内集成最顺，`runner.py` 原地改造 |
| **放弃 Channels** | 另案、非本切片。它是「聊活会话」的能力，不是「按钮」的能力 |
| **接受破「零额外依赖」** | NORTH_STAR 的零依赖偏好在此**有意破例**，换「真·多选按钮」这个硬需求。记录在案 |
| 控制对象 = **平台自己驱动的会话** | 不再试图远程控制「你终端里那个活 TUI」——那条路给不了按钮（见 §1.2/1.3） |

---

## 3. 核心机制：`canUseTool` 就是 permission hook 的翻版

**🔑 最重要的一句：这个模式代码库里已经有了，照抄即可。**

`control.py` 的 `handle_permission()` / `resolve()` 就是同一套：
**挂起一个 pending → 用 `threading.Event` 阻塞 → UI 点击 → 唤醒 → 返回决定 → 超时则失败安全。**

`canUseTool` 完全照这个套：

```
Claude 调 AskUserQuestion
    ↓
canUseTool 在 runner 里被触发, 拿到 {questions, options}
    ↓
runner 挂起一个 _PendingAsk{id, session, questions} + Event, 阻塞等待
    ↓
网页轮询到待答问题 → 渲染成按钮 → 你点一个
    ↓
POST 回来 → set Event → canUseTool 醒来
    ↓
返回 allow + answers → 会话带着你的选择继续跑
```

**顺带白捡：** `canUseTool` 拦截的是**每一次工具调用**，不只是 AskUserQuestion。
所以它**同时**就是这些会话的 permission 审批口——**一套机制同时覆盖「问题」和「权限」**。

---

## 4. 改动落点

| 文件 | 改动 |
|---|---|
| `tokmon/runner.py` | **主要改动。** 驱动方式从 `subprocess.Popen(claude -p)` 换成 Agent SDK 客户端；新增 `canUseTool` 回调 + `_PendingAsk` 挂起/唤醒（照抄 `control._Pending`）；`spawn/resume/status` 对外签名**尽量不变**，让上层无感 |
| `tokmon/serve.py` | 新增 `GET /api/control/asks`（列待答问题）+ `POST /api/control/answer`（提交选择），都走现成 `_ctl_guard`；`_do_steer` 基本不动 |
| `/control` 页 | 新增「待答问题」区块：问题 + 选项按钮，点击即 POST。样式复用现有 `.sec`/`.pill` |
| `tokmon/control.py` | **不动**（`audit_action` 已够用，steer/answer 都记它） |
| 内核 / activity / procmon | **零改动** |
| `requirements.txt` | 新增 Agent SDK（唯一新依赖） |

---

## 5. P7 守则（几乎全部沿用现有）

1. **allow-list**：仍只有「给平台拥有的会话发 prompt」+「回答该会话的提问」两个动作。cwd 仍限已知项目集。
2. **你显式触发**：**答案只能来自你的点击**。
   ⚠️ **超时绝不替你选**——超时必须返回 **deny**（理由「用户未作答」），
   **绝不自动挑一个选项**，那等于伪造你的决定。
3. **鉴权**：复用 `_ctl_guard`（`X-Control-Token`）。
4. **全审计**：每次 steer / 每次作答 → `audit_action` + `COMMAND_ISSUED`。
   **§6 内容最小化：问题正文与选项文本不进事件/通知**，只记 `{会话, 项目, "answered"}`，正文只进本地审计。
5. **失败安全**：SDK 起不来 / 会话崩 / 超时 / 答案 id 对不上 → **明确报「做不到」**，绝不伪装成功。

---

## 6. 两个真实技术风险（提前想清楚，别踩）

### 6.1 async ↔ 线程 的桥（**最主要的实现难点**）

Agent SDK 是 **asyncio**；`tokmon serve` 是 **ThreadingHTTPServer（同步线程）**。
`canUseTool` 在事件循环里被 await，而「你点按钮」是从另一个 HTTP 线程进来的。

**建议做法**：SDK 事件循环跑在一个专用后台线程；`canUseTool` 里 `await` 一个 `asyncio.Event`；
HTTP 线程用 `loop.call_soon_threadsafe(...)` 去 set 它。
**别**在 HTTP 线程里直接碰 asyncio 对象——那是竞态温床。

### 6.2 「白捡监控」要重新确认

现在 `/sessions`、token 看板能看到 spawned 会话，是因为 CLI headless **实测**会把 transcript 写进
`~/.claude/projects`。**SDK 驱动的会话是否同样落盘，未验证**（大概率会，同一引擎）。
→ 列入 §7 必测项。若不落盘，需补一层薄的回包落盘，否则监控白捡的性质就没了。

---

## 7. 动手前必须实测钉死的（**先做这三条，再写主体代码**）

> 纪律同 REMOTE_CONTROL_PLAN §8：地基先钉死，别把 200 行建在假设上。

1. **🔴 `canUseTool` 到底收不收得到 `AskUserQuestion`。**
   这是整个方案的命门。写一个最小脚本：SDK 起一个会话，注册 `canUseTool`，
   给一个必然引发提问的 prompt，打印回调收到的 `tool_name` 和 `input_data`。
   **收到了 → 全案成立；收不到 → 立刻停下来找我重新评估。**
2. **确认 Python SDK 的真实 API。** 包名、`canUseTool` 的确切签名与返回类型
   （下面 §7.1 是调研得到的形状，**以你装上的 SDK 实际为准**，不要盲信本文档）。
3. **确认 SDK 会话是否写 `~/.claude/projects`**（30 秒：起一个会话，看目录里有没有新 jsonl）。

### 7.1 调研得到的 API 形状（待你用实物核对）

- 回调：`async def can_use_tool(tool_name: str, input_data: dict, context) -> PermissionResultAllow | PermissionResultDeny`
- `tool_name == "AskUserQuestion"` 时，`input_data` 里有 `questions` 数组，每项含
  `question` / `header` / `options[{label, description}]` / `multiSelect`
- 回答方式：`PermissionResultAllow(updated_input={"questions": ..., "answers": {问题文本: 选中的label}})`
- SDK 另有 `disallowedTools` 可禁用工具（CLI 没有）

---

## 8. 交付切片

- **A0 · 地基 spike**（§7 三条）。**不通过就停，别继续。**
- **A1 · runner 换 SDK**：驱动方式替换 + `canUseTool` 拦截 AskUserQuestion + 挂起/唤醒 + 超时失败安全。
  先只做 **localhost**，命令行/单测验证，不碰 UI。
- **A2 · 端点 + UI**：`GET /api/control/asks` + `POST /api/control/answer` + `/control` 页的按钮区块。
  桌面浏览器上跑通「Claude 问 → 出按钮 → 点 → 会话继续」。
- **A3 · 上手机**：套现有 cloudflared 隧道（**注意：读页鉴权 `MC_REMOTE` 仍未做，见 §10 警告**）。

---

## 9. 完成判据

1. **在网页上，Claude 的提问显示为可点按钮；点一下，会话带着你的选择继续。** ← 核心
2. **超时不替你选**：不作答则明确 deny + 审计留痕，绝不自动选项。
3. 被驱动的会话仍出现在 `/sessions`、token 仍进看板（白捡监控成立）。
4. 每次 steer / 作答都有审计 + `COMMAND_ISSUED`；**问题正文不进事件**。
5. 所有失败路径明确报「做不到」；单测覆盖 `canUseTool` 的纯决策部分 + 超时失败安全。
6. 全套测试仍绿（当前基线 115）。

---

## 10. ⚠️ 别碰 / 其他在途线程（避免串台）

**本次只做本文件的事。** 仓库里另有三条独立线程，**不要顺手一起做**：

| 线程 | 状态 | 文档 |
|---|---|---|
| `/sessions` 过滤器 v2 | 诊断+计划已出，待实现 | [SESSIONS_FILTER_V2_PLAN.md](SESSIONS_FILTER_V2_PLAN.md) |
| 厂商账单 B1 | 代码已交付，⏸ 卡在用户填 admin key | [PROVIDER_BILLING_PLAN.md](PROVIDER_BILLING_PLAN.md) §8 |
| Channels（聊活会话） | 已调研，**本次明确不做** | REMOTE_CONTROL_PLAN.md |

**🔴 一个高优先级安全欠账（不属本切片，但请知悉）：**
看板已经挂在公网 cloudflared 隧道上，而 `/sessions`、`/tokens`、`/processes` 这些**读页没有任何鉴权**——
拿到 URL 的人能看到全部会话标题、当前步骤、思考片段与花费。
对应的 `MC_REMOTE` 收口在 REMOTE_CONTROL_PLAN §6，**尚未实施**。A3 上手机前必须先补，或先关隧道。

---

## 收尾纪律

`地基 spike → 实现 + 单测 → 对抗式 review → 修确认项`。
每步先问北极星：**这让「重要时刻它能找到我、而我能从手机上处置」更真了吗？**
—— 这一版的意义就是把「处置」从「只能打字」变成「能点按钮」。
