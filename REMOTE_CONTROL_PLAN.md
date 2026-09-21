# 下一代 · 远程 steer（平台自有可控会话）— 设计草图

> **【状态章 · 2026-09-20 核对】🟡 部分实施。**
> **S1（本地 steer 内核）✅ 已交付**（见 [RUNNER_SDK_PLAN.md](RUNNER_SDK_PLAN.md)，已换成 Agent SDK 驱动）。
> **§6 的 `MC_REMOTE` 收口 ✅ 已于 0.9.0 交付**（`tokmon/remote.py`：读页鉴权 / Host 白名单 /
> hook token 改走 header / 配置不自洽拒绝启动）。**S0 的隧道、S2、S3 仍未做。**

> 给主程序的实现交接。本文件是**设计与约束**，不是最终代码。
> 上层方向：[MISSION_CONTROL.md](MISSION_CONTROL.md) §9 的「舰队驾驶舱」+ [M4.5_PLAN.md](M4.5_PLAN.md) §B/§C 一直欠的**手机环**。
> 来源：「远程监测和控制 Claude Code 对话」会话（2026-07-10）的锁定方案 + 本次对 `claude` CLI **2.1.37** 的实测核验（§3）。
>
> **三个已敲定的决定（2026-07-10）：**
> 1. **范围** = 专注远程控制（Type B steer + cloudflared 上手机）。其余 backlog（预算真机验证 / 风险事件 / M5）**不进本文件**。
> 2. **起点主线** = **S1 先建 steer**（本地先把本体建出来、零攻击面），上手机放后（见 §7）。
> 3. **隧道鉴权** = **只用内建 token + HTTPS**（不叠 Cloudflare Access）。因此 token 是唯一闸 → **补偿纪律见 §6**。

---

## 0. 现状：哪些已做、哪些没做

- **✅ 远程「监测」已做完（只锁在 localhost）**：`/sessions` 活动状态 + `/api/events` + token 看板 + 进程页，全只读。
- **✅ 控制面早已「多动词」**（都走 `_ctl_guard` token 门 + `control.plane.audit_action` 审计）：
  - permission 审批（M4，官方 `PermissionRequest` hook）
  - 进程 terminate / kill（[serve.py](tokmon/serve.py) `_do_terminate` → `procmon.terminate`）
  - free-port（`_do_free_port`）
- **❌ 没做 = steer**（给一个会话**发指令**）—— 本文件的肉。
- **❌ 没做 = 把上述能力暴露上手机**（纯 transport）。

> 认知：**你已有的东西本身就是一套 remote-control，只差一层隧道 + 一个新动词。**

---

## 1. 决策记录：为什么 Type B + cloudflared（三根轴）

把「远程控制一个 chat」拆成三根轴，方案自然清楚（详细论证在源会话，这里只留结论）：

- **轴一 · 控制什么**：审批 permission ✅ 已做；**发指令 / steer ❌ = 要的新东西**。
- **轴二 · 控制哪个会话**（决定可行性）：
  - **(A) 你终端里活着的 TUI** —— 本地**无干净注入路径**（tmux/SendKeys 脆弱注入被纪律禁用）。官方 `claude remote-control` 已覆盖它，但走 Anthropic 云中转、要 claude.ai 账号、**脱离本平台的审计与 local-first** → **不自建，想 steer 活 TUI 就直接用官方那个**。
  - **(B) 平台自己 spawn/resume 的会话** —— **干净、官方、可审计、失败安全**（§2）。这就是路线图「舰队驾驶舱」的种子。**本案只做 Type B。**
- **轴三 · 走哪条远程通道**：**cloudflared 隧道先行**（最快、复用现成 `/control` UI）；**Telegram 双向零端口**是更安全的**终态 fast-follow**，不在本案硬做。

**🔒 锁定：Type B + cloudflared。**

---

## 2. 核心机制：session_id 是句柄，进程一次性

一个「可 steer 会话」在平台眼里 = **一个持久的 `session_id`(UUID) + cwd + 状态 + 逐回合记录**。进程本身**用完即弃**：

```
mint uuid = 平台生成的 UUID   # ← 平台从第 1 回合就拥有句柄 (CLI 支持 --session-id, §3)
第1回合  claude -p "<prompt1>" --session-id <uuid> --output-format stream-json [--settings <hook.json>] [--max-budget-usd N]
第2回合  claude -p "<prompt2>" --resume  <uuid> --output-format stream-json [...]
...每回合都是一次性 headless 进程, 拿到上下文、流式吐给 UI、退出
```

- **失败安全天然**：没有需要照看的长命子进程；每回合原子，**某回合崩只崩那一回合**，绝不污染任何活着的 transcript（P7）。持久的只有 `session_id`。
- **比源会话的机制更干净**：源会话是「spawn → 解析返回的 session_id → resume」。**实测发现 CLI 有 `--session-id <uuid>`**（§3），所以平台**自己 mint UUID**、从头拥有句柄，不依赖解析首回合回包。
- **✅ 白捡监控**：spawned 出的是**真 · Claude Code 会话**，**默认把 transcript 写进 `~/.claude/projects/**/*.jsonl`**（§3 反证：`--resume` 可用 + `--no-session-persistence` 存在 = 默认持久化开）。于是**现有 activity / tokmon / events 三支柱零成本监控它**——它自动进 `/sessions`、`TASK_COMPLETED` 经现有总线流出、烧的 token 进现有看板。**「监控被 steer 的会话」不需要写任何新代码。**

---

## 3. 实测已核验的 CLI 表面（`claude` 2.1.37）—— 地基不是假设

> 本次用真实二进制核过 `--help`（无会话、无成本）。整个 runner 就压在这张表上。

| flag | 用途 | 对本案的意义 |
|---|---|---|
| `--session-id <uuid>` | 指定会话 UUID | **平台 mint 句柄，从头拥有**（改进 §2 机制） |
| `-p, --print` | headless 出结果即退 | steer 的执行形态 |
| `-r, --resume [id]` | 按 session id 续接 | 逐回合 steer |
| `--output-format stream-json` | 流式结构化输出 | 喂 `/control` 的流式回包 UI |
| `--input-format stream-json` + `--include-partial-messages` | 流式输入 / 分块 | 实时 UI（可选，后期打磨） |
| `--settings <file-or-json>` | 注入额外设置 | **把 spawned 会话的 `PermissionRequest` hook 指向 `/hook/permission`** → permission 与 steer **组合**（回答源会话待定项） |
| `--permission-mode <mode>` | 权限姿态（default / bypassPermissions / …） | steered 会话用 **`default` + hook 组合**，**绝不用 `bypassPermissions`/`--dangerously-skip-permissions`**（否则手机 steer 出的 agent 会无人值守地动工具） |
| `--max-budget-usd <N>` | 单次美元硬上限（仅 `--print`） | **每回合成本封顶**，防手机 steer 出的 agent 烧穿（呼应 tokmon 本命） |
| `--model <m>` / `--append-system-prompt` | 模型 / 系统提示 | 平台写死；手机只给 prompt 文本 |
| `--fork-session` | resume 时开新 id 而非复用 | resume 活会话的**备选**避让（本案默认用 live-index 联锁拒绝，§5.5） |

> **⚠ 二进制解析（实测风险）**：`claude.exe` 本机在 `C:\Users\<user>\.local\bin\claude.exe`（native 安装），**不在 PATH**（`which`/`Get-Command` 都找不到）。→ `runner` **必须稳健解析**：先查 PATH → 回退已知位置（`~/.local/bin`、`~/.claude/local`、npm global）→ **找不到就失败安全报「做不到」**，绝不假设 `claude` 在 PATH。

---

## 4. 代码落点（守 P4 / P7，顺已有的 `_do_terminate` 模板长）

| 新增 | 类比现有 | 职责 |
|---|---|---|
| `tokmon/runner.py` | procmon 之于进程控制 | steer 的**机械层**：`spawn(project, prompt, uuid)` / `resume(session_id, prompt)` / `status()` / 每会话流式缓冲。**P4：只 import stdlib(`subprocess`/`threading`/`json`/`uuid`) + `events` + 纯 `project` 工具；不碰任何采集内核。** **底层 CLI headless，不引 SDK 依赖**（守零额外依赖）。 |
| `control.plane` 加 `steer` 动词 | 现有 permission 审批 / `audit_action` | **P7 gatekeeper**：token 鉴权 + 审计 + 发 `COMMAND_ISSUED`。**策略与机械分离**——control 管纪律，runner 管干活。 |
| serve `_do_steer` + `/api/control/steer` | `_do_terminate` + `/api/control/terminate` | 照抄：查 `remote_mode` → 校验入参 → 调 `runner` → `control.plane.audit_action(...)`。走现成 `_ctl_guard`。 |
| `/control` 页加「会话 steer」块 | 现有待审批列表 | 选会话/项目 → 输指令 → 看流式回包。 |

---

## 5. P7 五条逐条落地

1. **allow-list**：唯一新动词 = 「给平台拥有的会话入队一条 prompt」。**cwd 必须来自已知项目集**（`.vscode` 子文件夹 / 最近项目），手机侧**不能自由填路径**（挡住 `claude` 跑到 `C:\Windows`）；**所有 flags 由平台写死**，手机只给 `{目标会话, prompt 文本}`。
2. **你显式触发**：系统永不自动 steer；每条都来自你在 UI/手机的点击。
3. **鉴权**：现成 `X-Control-Token`（`_ctl_guard`）。隧道上它是主闸（§6）。
4. **全审计**：每次 steer → 审计日志 + `COMMAND_ISSUED` 事件。**§6 内容最小化：事件/通知只带 `{会话, 项目, "steer issued"}`，prompt 正文绝不外发**，只进本地审计。
5. **失败安全**（逐条明确报「做不到」，绝不伪装成功）：`claude` 二进制找不到 / spawn 失败 / 非零退出 / 回包非 stream-json / 超时（**杀子进程防僵尸**）/ `--max-budget-usd` 触顶 / **resume 一个正被活 TUI 占用的会话**。最后一条用 **procmon 的 live 索引做联锁**：活着的会话**拒绝 resume**，避免双写 transcript（官方明确警告 resume 活会话会交错污染）。

---

## 6. 上手机：cloudflared + 内建 token（据决定 3，无 Access）

**现状**：只有 control 端点有 token；`/sessions`/`/tokens`/`/processes` 读页**无鉴权**；`_host_ok()`（[serve.py:251](tokmon/serve.py#L251)）只放 localhost。直接开隧道 = 把这些全暴露到公网 URL。

**上隧道前必做的收口：**
- 新增 **`MC_REMOTE=1` 模式**：开启后**所有端点（含读页）都要 token**；**无 token 配置则拒绝远程启动**（失败安全）。
- 隧道域名**显式加进 host 白名单**（非通配），保留 `_host_ok` 的 DNS-rebinding 防护。
- hook 的 `?token=` 从 query **改走 header**（隧道/边缘可能把 query 写进日志）。

**因为选了 token-only，token 成为唯一闸 → 补偿纪律（写进文档，别省）：**
- **隧道 URL 当秘密**：用 `trycloudflare` 随机域名，别贴进任何公开处；
- **token 高熵 + 可轮换**：复用现成 `secrets.token_urlsafe(24)`，泄露即换；
- **仅在需要时开隧道**：用完即关，**不常驻公网**（把暴露窗口压到最小）；
- **诚实边界**：token-only 的入站攻击面 **> Telegram 零端口 getUpdates**。它换来的是**最快闭环 + 复用现成 UI**。**Telegram 双向仍是更安全的终态**，值得作为 fast-follow（那时 steer 指令走出站长轮询回执，零入站端口）。

---

## 7. 交付切片 + 落地次序（据决定 2：S1 起）

- **S1（起点）· runner 内核 + 本地 steer** —— **localhost only，零新攻击面**。`runner.py` + `control.steer` + `_do_steer` + `/api/control/steer` + `/control` UI。桌面浏览器就能 spawn/resume + steer 一个平台会话、看流式、全审计。**在最安全处先把「spawn/steer 真 Claude 进程」这个最 risky 的新机械钉死。**
- **S0 · 上手机（纯 transport，零新控制）** —— cloudflared quick tunnel + `MC_REMOTE` 收口（内建 token）。**闭上 M4.5 欠的手机环**：远程监测 + 远程批 permission，真的震手机。
- **S2 · steer 上手机** —— S1 + S0 合流，隧道上远程 steer。**这才是你要的「远程控制一个 chat」本体。**
- **S3（later）· 组合 + fleet 打磨** —— permission 组合（spawned 会话的 hook 指回 `/hook/permission`）验证；`/sessions` 长出真正的舰队驾驶舱列（每会话 steer 入口 + token + 进程融合）。

> **次序说明**：选 S1 先 = **先在 localhost（零攻击面）钉死新机械，再谈暴露上手机**。代价：M4.5 欠的手机环**晚一步闭**（可接受——手机环无新控制风险，随时能补）。

---

## 8. 动手前实测钉死的 —— ✅ S1 spike 已跑（2026-07-10 · claude 2.1.37 · 真实 spawn 花费 $0.003）

**核心地基全部实测通过**（一次 haiku 调用，prompt 走 stdin）：
- **✅ headless spawn 通 + `--session-id` mint 的 UUID 被认下**：`claude -p --session-id <uuid> --output-format stream-json --verbose --strict-mcp-config [--model M --max-budget-usd N]`。
- **✅ 白捡监控坐实**：spawned 会话把 transcript 写到 `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`（按 cwd 归属）→ 现有 activity/tokmon/events 零成本可监控。
- **✅ stream-json 三事件形状已拿**（runner 据此解析）：`system/init`（session_id/cwd/tools/model/`mcp_servers`）→ `assistant`（`message.content[].text` 流式）→ `result`（`is_error` / `result`=最终文本 / `total_cost_usd` / `permission_denials[]` / `num_turns`）。**`result` 白给每回合成本 + permission 拒绝记录，正好喂审计。**
- **✅ `--strict-mcp-config` 切掉继承 MCP**（init 里 `mcp_servers:[]`，不再挂在别人的 OAuth 上）。
- **✅ `--settings` 注入 hook**（permission 组合的路径，S3 用）。

**spike 逼出两条设计没料到的硬约束（runner 必须处理，已写进 `runner.py`）：**
1. **🔴 env 必须净化（THE 大发现）**：从 Claude Code 会话里 spawn 的子 claude 继承 `CLAUDECODE=1` / `CLAUDE_CODE_ENTRYPOINT` / `CLAUDE_CODE_CHILD_SESSION` / `CLAUDE_CODE_SESSION_ID` → **子进程侦测到「在另一个 claude 里」直接 hang 死（零输出、不自解）**。runner **必须 strip 掉所有 `CLAUDE_CODE*` + `CLAUDECODE` 再 spawn**。
2. **🟠 prompt 走 stdin**：`-p` 在管道 stdin 时**优先读 stdin**（空 stdin 会把 prompt 顶空）。runner 用 Python `subprocess` 把 prompt 从 stdin 喂进去（Python utf-8 编码器**不加 BOM**，天然规避 PS5.1 `-Encoding utf8` 的 BOM 污染坑）。

**仍待钉（进后续 slice 时验）：**
- **⚠ 二进制解析**：`claude.exe` 实测在 `~/.local/bin`、非 PATH → runner 已做 PATH→已知位置回退→找不到失败安全。
- **⚠ live-index 按 session 联锁**：procmon 有进程视图，但「会话 ↔ 进程」映射需验证（拒绝 resume 活会话）。留待 S1 的 resume 路径 / S2。

---

## 9. 完成判据

1. **S1**：桌面能 spawn 一个平台会话、逐回合 steer、看流式回包、每步进审计 + `COMMAND_ISSUED`；**所有失败路径明确报「做不到」**；单测钉 `runner` 纯函数 + `control.steer` 的 P7 gate；经对抗式 review（P4/P7/失败安全 + 安全/线程/解耦三维）。
2. **白捡监控成立**：被 steer 的会话**自动出现在 `/sessions`**、烧的 token 进看板——零新监控代码。
3. **成本封顶为真**：每回合 `--max-budget-usd` 生效，实测一次触顶被拦。
4. **（S0/S2）**：隧道上远程 steer 真跑通一次；`MC_REMOTE` 收口生效、**无 token 拒启验证过**。

---

## 收尾：沿用既有纪律

`design → implement + 单测 → 对抗式 review → 修确认项`。每步先问北极星：
**这让「我知道它在推进 / 卡住 / 烧钱 / 乱改，并能在重要时刻远程处置」更真、或那次处置更安全了吗？** 不是，就先别加。
</content>
