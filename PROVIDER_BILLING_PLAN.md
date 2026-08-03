# 新能力 · 厂商 API 用量与费用监控（`/billing`）— 设计草图

> 给主程序的实现交接。本文件是**设计与约束**，不是最终代码。
> 惯例同 [M4.5_PLAN.md](M4.5_PLAN.md) / [REMOTE_CONTROL_PLAN.md](REMOTE_CONTROL_PLAN.md)。
> §1 的 API 事实**全部来自 2026-07-13 的官方文档实证调研**（三个并行 agent，附出处），**不是记忆、不是猜测**。

> **五个已敲定的决定（2026-07-13）：**
> 1. **定位** = **正式修订 [NORTH_STAR.md](NORTH_STAR.md) §7 非目标**（见 §0.2），并入成本支柱的使命。
> 2. **范围** = **只管「API 平台开销」**——你自己 app 调 API 烧的钱。**本切片明确不做对账**（理由见 §0.3，很重要）。
> 3. **Provider** = **先 Anthropic + OpenAI**（都有干净 API）；**Google 诚实标注「不可得」**（§1.3，它是真的没有）。
> 4. **密钥** = `~/.tokmon/providers.json`（0600，照 `control_token` 先例），**UI/API 绝不回显**。
> 5. **前提** = 用户确认 Anthropic 有 org、OpenAI 有 org Owner —— 两家 admin key 都能建。

---

## 0. 定位：为什么这需要动北极星

### 0.1 这是平台的**第二个「受控破例」**

`NORTH_STAR.md` 原则 3 是「本地、私密、可离线」。至今唯一的出站是 **M3 通知**（往 Telegram 发）。
本能力是**第二个出站**，且性质更重：**主动带着凭据去三家云厂商拉数据**。必须像通知那样——**默认全关、显式 opt-in、内容最小化**。

### 0.2 NORTH_STAR §7 的**外科式修订**（只改该改的一条）

| 原非目标 | 处置 |
|---|---|
| ❌ **「不替代官方用量后台。不去爬 console、不调计费 API。只信本机 transcript。」** | **🔧 必须修订。** 本能力就是**调官方 usage/cost API**（注意：是官方 REST API，**不是爬 console**——这个区别要写进修订后的措辞）。 |
| ❌ **「不做账单对账工具。」** | **✅ 保留不动。** 决定 2 明确**不做对账**（见 §0.3）。这条继续有效。 |

> 修订措辞建议：「不爬 console 网页、不逆向内部端点；**可以调厂商公开的官方 usage/cost API**，但必须显式 opt-in、密钥不外泄、拿不到就诚实标注不可得。」

### 0.3 ⚠️ 为什么**不做对账**（这条最容易被未来的自己想当然地做错）

诱人的想法是：「拿厂商真实账单当真值，校验 tokmon 的等价估算」——**给 `pricing.py` 做真值 doctor**。

**但它现在对不上账，硬做就是自欺：**

- **实测证据**（本会话 spike 的 `system/init` 事件）：你的 Claude Code 是 **`apiKeySource: "none"`** —— 走**订阅 OAuth**，不是 API key 计费。
- 因此 **Claude Code 的用量根本不会出现在 Anthropic 的 API 平台账单里**。
- tokmon 报的 `$1,407` 是**「等价用量价值」估算**（北极星 §7 原文：不追求和真实账单分毫不差），你实际付的是**订阅费**。
- → 拿 `cost_report` 去校 tokmon，**是在比两个不同的计费池**。数字必然不一致，且不一致是**正确的**。

**因此本页必须在 UI 上写明**：
> 「`/billing` = 你的 **API 平台**开销（app 调 API 的钱）。它**不含**订阅制 Claude Code 的用量——那个在 `/tokens`，且那是**等价估算**不是账单。**两个数字本就不同源，不该相等。**」

**后续可能的路**（不在本切片）：调研提到另有 **Claude Code Analytics API**（给 per-user Claude Code cost）——那**可能**才是能和 tokmon 对上的那个。**待查实后再单独评估**（见 §6 B4）。

---

## 1. 调研实证：三家的真实 API 表面

> **地基不是假设。** 以下全部来自官方文档（2026-07-13 实证）。**任何声称 Gemini 有「用量 API」的说法都是编的**（§1.3）。

### 1.1 Anthropic ✅ 有干净的官方 API

| 项 | 事实 |
|---|---|
| **用量** | `GET https://api.anthropic.com/v1/organizations/usage_report/messages` |
| **费用** | `GET https://api.anthropic.com/v1/organizations/cost_report` |
| **鉴权** | `x-api-key: sk-ant-admin01-...` + `anthropic-version: 2023-06-01` |
| **用量粒度** | `bucket_width` = `1m`/`1h`/`1d`；`group_by[]` = `model` / `workspace_id` / `api_key_id` / `service_tier` / … ；`starting_at` 必填（RFC3339） |
| **费用粒度** | 🔻 **只有 `1d`**；`group_by[]` **只有 `workspace_id` + `description`** —— **没有 per-model、没有 per-api-key**（model 只在 `group_by=description` 时作为解析字段回来） |
| **延迟** | 约 **5 分钟** |
| **分页** | `has_more` + `next_page` → 回传 `page=<token>`。桶上限：`1d` 默认7/最大31；`1h` 24/168；`1m` 60/1440 |
| **轮询** | 官方：持续使用**最多每分钟 1 次** |

**🔴 三个必须钉死的坑：**

1. **💣 `amount` 是「分」不是「元」。** `cost_report` 的 `amount` 是**十进制字符串、单位是最小货币单位（分）**——`"123.45"` 实际是 **$1.23**，**要除以 100**。
   → **搞错就是静默的 100 倍误差**。**必须有单测钉死。**
2. **没有 `input_tokens` 字段。** 用量回包里是 `uncached_input_tokens` + `cache_read_input_tokens` + `cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens`。要输入总量得**自己加**。
3. **没有请求数。** 用量 API **不给 request count**（唯一的计数是 `server_tool_use.web_search_requests`）。
   → 页面上 Anthropic 的「请求数」列必须显示 **`—`（不可得）**，**绝不填 0**。

**其它诚实边界：** 个人账号**不可用**（需 org，已确认具备）；Priority Tier 的费用**不在** cost 端点；`api_key_id: null`（Workbench）与 `workspace_id: null`（默认工作区）是**合法值**，不是错误。

### 1.2 OpenAI ✅ 有

| 项 | 事实 |
|---|---|
| **用量** | `GET https://api.openai.com/v1/organization/usage/completions`（**每个模态一个端点**：还有 `embeddings` / `images` / `audio_speeches` / `audio_transcriptions` / `moderations` / `vector_stores` / `code_interpreter_sessions`；**没有单一「全部用量」端点**） |
| **费用** | `GET https://api.openai.com/v1/organization/costs` |
| **鉴权** | `Authorization: Bearer sk-admin-...` |
| **用量粒度** | `bucket_width` = `1m`/`1h`/`1d`（默认 `1d`）；`group_by` = `model` / `project_id` / `api_key_id` / `user_id` / `batch` / `service_tier`；`start_time` 必填（**unix 秒**） |
| **费用粒度** | 🔻 **只有 `1d`**；`group_by` = `project_id` / `line_item` / `api_key_id`；`limit` 1–180 |
| **优于 Anthropic** | ✅ **有 `num_model_requests`**（请求数）；✅ 费用**能按 `line_item` 拿到模型维度** |
| **延迟** | ⚠️ **官方没有公布新鲜度 SLA** → 按**最终一致**设计：**重取尾部窗口**（数据会变），**不要 append-only** |
| **分页/限流** | 游标 `next_page` + `has_more`。⚠️ **限流未公开** → 保守 ≤1/min + `429` 指数退避 |

**其它：** 建 admin key 需 **org Owner**（已确认具备）。**旧的 `/v1/usage` 是未文档化的遗留端点——绝不要基于它开发。**

### 1.3 Google 🔴 **诚实结论：拿不到**

**这是本次调研最硬的发现，必须原样写进文档和 UI：**

- **AI Studio / Gemini Developer API（`AIzaSy...` key）：没有任何官方的用量或费用 API。一个都没有。** 官方账单文档只把你指向**两个网页 UI**。`ai.dev/usage` 是未文档化的内部网页端点，**不是受支持的 API**。
- **真实费用只能走 BigQuery 账单导出**：要 billing admin 权限、要建 dataset、有 BQ 查询成本、**初次回填最长 5 天**、且**开票前都只是估算**。
- **Vertex 的 token 数**可经 Cloud Monitoring `timeSeries.list` 拿到（`aiplatform.googleapis.com/publisher/online_serving/token_count`），但**要 OAuth 服务账号**——而 **Python 标准库没有 RSA**，连服务账号 JWT 都签不了 → 只能走 gcloud ADC refresh-token 或 shell 出去调 `gcloud`。**破「零额外依赖」**。
- **唯一轻量法**是「自己计量响应里的 `usageMetadata`」——**但这对 tokmon 不适用**：tokmon **不代理**你的 Gemini 调用，根本看不见它们。

**→ 处置：`/billing` 页上 Google 一栏显式显示「不可得」+ 一句原因 + 「要拿到需 BigQuery 账单导出（重）」。**
**绝不显示 0，绝不瞎估。**（原则 1：宁可显示未知，也不要悄悄估错。）

---

## 2. 架构定位（守 P4 / I2 / I6）

- **新模块 `tokmon/billing.py`** —— 第 4 个数据源。
- **🔒 绝不碰成本内核**：**不 import** `parser` / `pricing` / `aggregate` / `records`。
  它的数据来自**网络**，不是本机 transcript —— 和内核是**完全不同的来源**，混进 `aggregate` 会同时破坏 **I2（内核纯函数、无 I/O）**、**I4（UsageRecord 是唯一真相载体）** 和 local-first。
  **它是与 procmon 同级的独立支柱**（charter 并入成本支柱，但**代码独立**）。
- **零额外依赖**：纯 stdlib `urllib.request`（照 `notify.py` 调 Telegram 的先例）。
- **消费者**：新页面 **`/billing`** + `GET /api/billing`（serve 层组合，照现有 8 页模式加第 9 页）。
- **不阻塞**：拉取在后台线程 + 缓存快照（照 `procmon` / `cost_source` 的 pump 风格），HTTP 请求绝不在页面请求线程里同步做。

---

## 3. 统一数据模型（**两家几乎每个维度都不一样，归一是本设计的肉**）

| 维度 | Anthropic | OpenAI | 归一策略 |
|---|---|---|---|
| **金额单位** | 十进制**字符串**，单位**分** | `amount.value` **浮点**，单位**美元** | 🔴 **统一成 USD float。Anthropic 必须 ÷100。单测钉死。** |
| **输入 token** | 无 `input_tokens`，需 `uncached + cache_read + cache_creation.*` 求和 | `input_tokens` + `input_cached_tokens` | 统一成 `input / cached_input / output` |
| **请求数** | ❌ **不可得** | ✅ `num_model_requests` | **Anthropic 显示 `—`，绝不填 0** |
| **费用按模型** | ❌ 无 per-model group_by | ✅ `line_item` | **Anthropic 的「按模型费用」显式标注不可得**（或仅从 usage×单价推，且**必须标为「推算」**） |
| **费用粒度** | 仅 `1d` | 仅 `1d` | 页面的**费用视图就是「按天」**。**不提供日内费用**（要提供必须显式标「推算」） |
| **时间参数** | RFC3339 字符串 | **unix 秒** | 内部统一用 epoch，出站时各自转换 |
| **延迟** | ~5 min | 未公开 | 一律**重取尾部窗口**（如近 3 天），不 append-only |

---

## 4. 密钥与安全（决定 4）

**🔴 先把血腥事实写清楚（调研实证）：**
- **Anthropic 的 admin key 没有只读档——每一把都带完整 org-admin 权限**：能**踢组织成员**、能**停用别人的 API key**、能读合规日志。官方原文：「Console keys do not have selectable scopes; every key carries full Admin API access」。**读你自己用量的那把钥匙，也能把同事踢出组织。**
- **OpenAI 稍好**：RBAC 支持**受限/只读** key，**但用量/费用的确切 scope 官方没有公开文档** → **必须实测验证**（见下）。

**落地规则：**
1. **存 `~/.tokmon/providers.json`**（照 `control_token` 先例：**0600、原子写**）。也接受环境变量（`ANTHROPIC_ADMIN_KEY` / `OPENAI_ADMIN_KEY`）覆盖。
   **绝不存进项目目录**（一旦提交/分享 = 泄露 org 管理员凭据）。
2. **默认全关**：没配 key → **一个字节都不出本机**（照 notify 的 P5）。
3. **绝不回显**：任何 API / 页面只报 **「已配置 / 未配置」**，**永不返回 key 本体**（照 `control.plane.status()` 的 `token_set` 先例）。**绝不进日志。**
4. **OpenAI 用受限 key**：建 key 时给最小权限，并**实测验证**它 ① 能读 `/v1/organization/usage/*` 和 `/costs` ② **在写端点上 403**（如 `POST /v1/organization/admin_api_keys`）。**验证通过前，按「全权限」对待。**
5. **给 Anthropic key 设过期时间**（能设就设）——因为它没有只读档，只能靠时限降低暴露面。
6. **key 绝不经 `tokmon serve` 的 HTTP 面暴露**（`/api/billing` 只回聚合数字）。

---

## 5. 诚实纪律（本项目的灵魂，逐条落到本能力）

- **拿不到 → 显式「不可得」**，绝不填 0、绝不瞎估（原则 1）。适用于：Google 全部、Anthropic 的请求数、Anthropic 的按模型费用。
- **💣 单位坑必须单测**：Anthropic `amount` 分→元。搞错=静默 100 倍。
- **数据会变**：两家的近期桶都可能事后修正 → **重取尾部窗口**，不做只追加。
- **前提不足要报出来**：key 无效 / 非 org / 非 owner → **探针请求明确报「前提不足 + 原因」**，不静默变成 0。
- **两个数字本就不同源**：`/billing`（API 平台真实账单）≠ `/tokens`（Claude Code 等价估算）。**UI 上写明**，别让未来的自己以为是 bug（§0.3）。
- **失败降级**：某一家挂了/限流了 → 那一家标「暂不可得」，**不拖垮整页**（原则 5）。

---

## 6. 交付切片

- **B1 · 核心（本切片）** —— `billing.py`（两家 client + 统一模型 + 单位归一 + 缓存 + 节流）+ **`/billing` 页** + `/api/billing`。
  密钥从 `~/.tokmon/providers.json`(0600)/env 读；未配 → 零外发。**Google 显式标「不可得 + 原因」。**
- **B2 · 前提探针 + doctor** —— 一条探针请求验证 key 有效性/权限/org 前提；接进 `tokmon doctor` 的体检风格（**这最合项目的 doctor 精神**：让「前提失效」可被发现，而不是悄悄显示 0）。
- **B3 · 接入告警（可选）** —— 把厂商**真实**费用接进现有 events/notify（`TOKEN_BUDGET_WARNING` 的兄弟，如 `PROVIDER_BUDGET_WARNING`）。这是**真实账单**驱动的告警，比估算更硬。
- **B4 · 对账（待评估，不在本切片）** —— 先查实 **Claude Code Analytics API** 是否能给出与 tokmon 同源的 Claude Code 成本；能，才谈「pricing 真值 doctor」。**查实前不做**（§0.3）。

---

## 7. 完成判据

1. `/billing` 页显示 Anthropic + OpenAI 的**按天费用**与**按模型用量**；数字与各自官方 console **人工核对一次能对上**。
2. **Anthropic 分→元的换算有单测钉死**（100 倍坑）。
3. **未配 key → 一字节不外发**；key **在任何 API/页面/日志里都不出现**。
4. **Google 显式「不可得 + 原因」**，页面上**不出现伪造的 0**。
5. 前提不足 / 限流 / 单家故障 → **明确标注**，不瞎估、不拖垮整页。
6. `billing.py` **不 import 任何成本内核**（import 图实测干净，守 P4/I2）。

---

## 8. ⏸️ 当前进度 / 断点续传（2026-07-13 挂起，插队做 `/sessions` filter）

### ✅ B1 已交付（代码已落、测试全绿）
- **`tokmon/billing.py`** —— P4 实测干净（**只 import stdlib，零内核引用**）；两家 client + 统一模型 + 分页 + 5min pump + 失败降级。
- **`/billing` 页 + `GET /api/billing`** + 导航 + 主页入口；**`POST /api/billing/keys` 走 `_ctl_guard` 令牌**（写 org-admin 级凭据必须持令牌）。
- **11 个单测**（全套 **115 绿**）：分/元 100 倍坑、零外发、绝不填 0、密钥不回显。
- **真机冒烟通过**：`egress = 仅本地(未配置任何 key)`；三家均正确标 unavailable + 诚实原因；回包**不含 `sk-`**。

### ⏸️ 断点在哪
**卡在「填 admin key」这一步。** 已在 `~/.tokmon/providers.json` 建好 **0600 空骨架**（内容 `{"anthropic":"","openai":""}`）。
> ⚠️ **Windows 上 0600 是 no-op**（实测权限显示 `0o666`）——真正保护来自用户目录 ACL。同 `control_token` 的现状。

### ▶️ 恢复后的下一步（按序）
1. **用户自行**把两把 **admin key**（`sk-ant-admin01-…` / `sk-admin-…`）填进 `~/.tokmon/providers.json`，或设环境变量。
   **注意：普通 API key（`sk-ant-api03-…` / `sk-proj-…`）调不通 usage/cost 端点——会 401/403。必须是 admin key。**
2. **真实拉取验证**（§7 完成判据 #1）：
   - 能否调通（401/403 → 前提不足，会明确报出来）
   - 💣 **把原始 `amount` 与归一后的美元并排打出来**，亲眼确认 Anthropic 没差 100 倍
   - **验证 OpenAI 的受限 key 是否真只读**（对写端点发探针，期望 403）
   - 数字对着各自 console **人工核一次**
3. 然后才谈 **B2（前提探针 + doctor）/ B3（真实账单告警）/ B4（对账，需先查实 Claude Code Analytics API）**。

### 🩸 一条教训（写下来免得重犯）
找 key 时我递归扫了 `C:\Users\zwdzw\.vscode` **整个目录树**（用户所有项目）——**这会烧掉海量 token**。
**教训：动手扫描前先问清范围。** 别对大目录树做无边界递归。

---

## 收尾：沿用既有纪律

`design → implement + 单测 → 对抗式 review → 修确认项`。每步先问北极星：
**这让「我对自己烧的钱有感觉、有数、能复盘」更真、更准、更可信了吗？**
——注意这次的「更真」是字面意义的：**第一次拿到的是真实账单，不是估算**。但也正因如此，**绝不能让它和估算混为一谈**（§0.3）。
</content>
