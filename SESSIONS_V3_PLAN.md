# `/sessions` V3 · 表格 + 分面过滤 + 回复时钟 — 设计

> 给主程序的实现交接。**设计与约束，不是代码。**
> 承接 [SESSIONS_FILTER_PLAN.md](SESSIONS_FILTER_PLAN.md)（V1，**已交付**）与 [SESSIONS_FILTER_V2_PLAN.md](SESSIONS_FILTER_V2_PLAN.md)（V2，**已写但从未实施**）。
> **本文件取代 V2**，吸收它仍然正确的部分（§2.1 状态轴 / §2.2 分面计数 / §2.3 不许有隐形过滤器 / §2.5 表格），并处理审计新查出的**内核缺陷**。
>
> **已敲定的决定（用户，2026-07-14）：**
> 1. **D1 · 状态降级为「列 + 分面过滤器」**，不再是分组轴。
> 2. **D2 · 「已关闭」默认过滤掉，但必须大声**：可见、可点、持久化、进 fbar、一键清除。
> 3. **D3 · 公网 cloudflared 隧道已关闭**（PID 100908 → `127.0.0.1:8794`，2026-07-14 kill）。V2 §6 的安全发现**当前已解除**。
> 4. **D4 · 默认全局按「Claude 最后回复」降序，不分组**（用户授权我定，理由见 §7）。
> 5. **D5 · 新增后端字段 `last_reply_epoch`**（用户授权我定，理由与边界见 §10）——**这打破了 V1/V2 的「零后端改动」范围规则，本文件必须为此负责。**

---

## 0. 前置事实（不再复议，只据此设计）

### 0.1 V2 从未实施

`tokmon/serve.py` 的 mtime 是 **15:46**；`SESSIONS_FILTER_V2_PLAN.md` 写于 **20:58**。**文档比代码晚。** V2 §2.1–§2.5 **每一条都未建**：`SESS_PAGE`（`serve.py:1127-1397`）里今天仍然有 `#hideidle`（`serve.py:1194`）、pill 仍读全量 `d.counts`（`serve.py:1298`）、`groupOf` 仍只吃 `state`（`serve.py:1262`）。

**所以 V2 不是「上一版实现」，它是「一份没落地的诊断」。** V3 **取代**它：V2 的诊断（三个根因）全部成立且已并入本文；V2 的方案在 V3 里被实现，并被审计新查出的 F7–F12 修正。

### 0.2 隧道（D3）

cloudflared PID 100908 → `127.0.0.1:8794`，已于 2026-07-14 kill。V2 §6「看板裸奔公网」**当前已解除**。

> ⚠️ **但 `MC_REMOTE`（[REMOTE_CONTROL_PLAN.md](REMOTE_CONTROL_PLAN.md) §6：读页也要 token）仍未实现。** `/sessions` `/tokens` `/processes` `/api/*` 至今**没有任何鉴权**（`_ctl_guard` 只护控制端点）。**今天谁再起一次隧道，洞就原样回来。**
> **本设计不引入任何新的出站或入站面**（纯前端 + 纯只读内核改动），但**不解除这个前置条件**：**在 `MC_REMOTE` 落地前，别再把 `tokmon serve` 挂上公网隧道。**

### 0.3 留给「卡住检测器」的缝（一行，本文不设计它）

`amb` 组 + `last_reply_epoch` + `last_user_epoch` + `step_epoch` + `tail_complete` + `last_reply_absent` 恰好是它规则层筛子的入参；表格的**展开行**是它未来 `[诊断]` 按钮的落点。**它有自己的文档（STUCK_DETECTOR_PLAN.md），本文不欠它设计债。**
🔴 **规则层在 `last_reply_absent == 'out_of_window'` 时必须弃权（P6）** —— 这两个字段（`tail_complete` / `last_reply_absent`）恰恰是那个筛子唯一能防止「因为读不到就误判卡住」的字段。
🔴 **但欠它一句警告**：`_tail_cache` 里的巨型文本从 V3 起是**被钳制过的**（§11.3）。它要做 LLM 深挖时**必须自己重读文件**，不能吃缓存里的 objs。

---

## 1. 审计判决（一次说清）

**用户的抱怨「有时候 claude 回了我你也没记录」——是真的。时间戳 160/160 全对，一条没丢。丢的是行本身。**

> **机制：Claude 回复你之后第 600 秒，那一行从 DOM 里被删掉。**
> `serve.py:1194` 默认勾选 `<input id="hideidle" checked>`；`serve.py:1269` 执行 `if(F.hideIdle && s.state==='AWAITING_USER' && s.idle) return false;`。
> 而 `idle` 只在 `AWAITING_USER` 分支被赋值（`activity.py:191`、`activity.py:204`）→ **空闲 ⊂ 等你** → 这个复选框**唯一能藏的东西，就是「Claude 回完了，在等你」**。

**现场快照（`:8795/api/sessions` 实测）：**

| 事实 | 数字 |
|---|---|
| 总会话 | **160** = 活跃 4 + 久未返回 4 + 等你(AWAITING_USER) 71 + 已关闭 81 |
| 71 个「等你」里，`idle`（>10min）的 | **70** ← **默认被删掉的就是这 70 行** |
| 这 70 个里，`claude.exe` 还活着的 | **23** |
| 默认实际渲染的行数 | **90**，其中 **81 行是已关闭**，中位年龄 **231.2h（9.6 天）**，最老 45 天 |

**一句话：这一页把「谁在等我」这个它唯一存在的理由，藏掉了 71 分之 70；同时把 9.6 天前的尸体摆在你脸上。**

### 收敛成一句根因

审计的 12 条现象（F1–F12）收敛为**一句话**：

> **这一页有三处「同一件事的第二份定义」，每一处都在悄悄撒谎。**
>
> - **「Claude 回没回我」有 3 份定义**：状态机（`activity.py:144-194`）一份、`_recent_events` 的 `task_completed`（`activity.py:297`）一份、UI 的「最后活动」（其实是 `_last_message`，`activity.py:267-271`，**你一说话它就动**）又一份。
> - **「过滤器」有 2 套机制**：`FILTERS`（可见、可点、持久化、进 fbar）一套；`#hideidle`（隐形、不可点、不持久、不进 fbar）另一套。
> - **「数字」有 2 个来源**：pill 读全量 `d.counts`（`serve.py:1298`），行读 `applyFilters` 的结果。两者从不相等。

违反的是同一条：

> **原则 1 · 真实优先。** 「数字必须可被信任。宁可显示「未知单价」也不要悄悄估错；宁可少一个炫酷功能，也不要一个会误导自己的数字。」（NORTH_STAR.md:30-31）

**所以 V3 的组织原则不是「加功能」，而是：每件事只留一份定义，并让第二份定义在结构上无法存在。**

---

## 2. F1–F12 逐条落点（每一条都指名到 file:line）

| # | 现象 | 落点 | 节 |
|---|---|---|---|
| **F1** | `#hideidle` 默认勾选，600s 后删掉「Claude 刚回复」的行 | **删除控件与机制**：`serve.py:1194`（控件）、`:1269`（过滤分支）、`:1297`（`render` 读 checkbox）、`:1382-1383`（监听器）。`git grep hideidle` → **0 命中** | §9 |
| **F2** | 空闲 pill 无 `.clk`，是唯一点不动的 pill（`serve.py:1309` vs 委托 `:1388`） | 六枚状态 pill **由同一个数组渲染** → 不可能有一枚缺 `.clk` | §5.2 |
| **F3** | `hideIdle` 不在 `FILTERS`（`serve.py:1255`）、不被 `saveFilters` 持久化（`:1261`） | 机制删除；新的默认过滤**就是 `FILTERS.states` 上的一个值**，天然持久化 | §9 |
| **F4** | `bits`（`serve.py:1327-1330`）漏了 `hideIdle` **和文本搜索** → 隐形过滤器 | **`AXES` 轴登记表**：`applyFilters` / `bits` / `facetCounts` 遍历同一张表 | §8 |
| **F5** | `shownNote` 只在 `!bits.length` 时渲染（`serve.py:1369`）→ 开任何过滤器就抹掉唯一的交代 | 「显示 N/160」**无条件常驻** | §8.3 |
| **F6** | pill 读全量 `d.counts`（`serve.py:1298`）、chip 读全量 `d.sessions`（`serve.py:1316`）→ 承诺 81 给 0 | **分面计数** + 运行时不变量 **S4**（点 N 出 N） | §8.2 |
| **F7** | 尾读在巨型记录上失败 → `objs=[]` → UNKNOWN → `_DEAD_STATES`（`event_sources/activity_source.py:29`）→ **事件总线一起哑掉**（`activity.py:236-247`） | **边界锚定 + 指数增窗 + 硬顶 + 内存钳制 + 升窗节流**。🔴 **`_read_tail` 的两个调用点必须同改：`activity.py:425`、`inference_doctor.py:85`** | §11 |
| **F8** | `_current_step` 按**种类**优先（`activity.py:359` `if think: return think`），从不比时间戳 → 7/160 行显示陈旧思考 | **最近的块赢**（记录逆序 × 块逆序，第一个非空 text/thinking）+ `step_epoch`。🔴 **必然打红 `tests/test_tokmon.py:616`，必须改写** | §12 |
| **F9** | 无 focus/visibility 重取（只有 `setInterval(tickAll,30000)`，`serve.py:1381`） | `visibilitychange` / `focus` / `online` → 去抖重取 | §13 |
| **F10** | 年龄由服务端算好（`serve.py:1361` 渲染 `fmtAgo(s.last_activity_age_s)`），`last_activity_epoch` 在 `serve.py` 里**一次都没被引用**；`fmtAgo` 还向下取整（`:1211`）→ 20 分钟前的帧自称「5s 前」 | **年龄一律客户端从 epoch 现算 + 自己会走 + 陈旧横幅**。🔴 **绝不新增 `last_reply_age_s`** | §13 |
| **F11** | `last_activity_epoch` = `_last_message()`（`activity.py:267-271`）= assistant **或** user 的最新一条 → **你一提问它就前移**；16/160 会话正踩着 | **`last_reply_epoch` + `last_user_epoch` 两列并排，较新的那格加粗** | §10 / §6 |
| **F12** | 无表格；排序服务端硬编码 `key=(_STATE_ORDER[state], last_activity_age_s)`（`activity.py:506-507`），前端从不重排 | 前端**全局排序**，可点列头。**服务端排序保留不动**；`/api/sessions` 契约改为「**顺序未定义，消费者自己排**」 | §6 / §7 |

### F13（设计期新增实测 —— 三份候选设计和三个评审全都猜错了这一条）

审计推断「巨型记录 = 巨型 thinking 块」。**我全量重扫了语料，不是。**

```
超过 256KB 的记录: 82 条
  按 type:  user 81 · attachment 1 · assistant 0      ← 一条 assistant 都没有
  最大单条: 11,935,057 B, type="user", content=['document','text']   (一份粘贴进来的文档)
  巨型块的字节归属: image 20.1MB · text 18.8MB · tool_result 5.3MB · document 11.9MB · thinking 0
```

> 🔴 **内存钳制如果只钳 assistant 的 `thinking`/`text`，它一个字节都没钳到。**
> 而「绝不碰 `user`/`tool_result`」（为了保护 `_open_bg_tasks`）**同样一个字节都没钳到**。
> **两种直觉都错。正确的做法见 §11.3：按 block 类型钳制，并在钳制前把被钳区域里的判断结果先算完。**

---

## 3. 用户的五个决定（照录，含理由）

**D1 · 状态降级为「列 + 分面过滤器」。** 状态不再是分组轴：它是表格的**第 1 列**（彩色徽章、可点列头排序）+ 页顶的**一行 6 枚分面 pill**。
> 用户原话：「分组是没有过滤器时代的遗物；有了过滤器就不需要了。**用过滤器实现的筛选是可见的，用排序实现的优先级是隐形的。**」

**D2 · 「已关闭」默认过滤掉，但必须大声。** 81 行、中位 9.6 天，是噪音，该默认藏。
> **但它必须是一等公民的过滤器：可见、可点、持久化、进 fbar、一键清除。**
> **和 `hideIdle` 的区别就是这整件事的全部意义。在这里重蹈覆辙 = 致命。** → §9 的解法是：**它根本不是一个新过滤器。**

**D3 · 隧道已关闭。** → §0.2。

**D4 · 排序（用户授权我定，已采纳）：默认全局按「Claude 最后回复」降序，不分组。** 列头可点排序；「按状态分组」开关存在但**默认 OFF**；全部持久化。
> 理由：**分组会给排序套一个笼子。** 用户明确说过「按上次响应的时间排序对我来说很重要」，而分组会让「3 小时前回复的活跃会话」永远排在「10 秒前回复的等你会话」上面——那是另一个问题的答案。

**D5 · 新增后端字段（用户授权我定）。** `last_reply_epoch` + `last_user_epoch` + `step_epoch` + `tail_complete` + `last_reply_absent`。
> **这打破了 V1（`SESSIONS_FILTER_PLAN.md:154`）和 V2（`:172`）都写过的「零后端改动」。** 辩护、边界、以及「哪些函数不许碰」，见 §10.1。

---

## 4. 优先级

| 级 | 内容 | 为什么是这一级 |
|---|---|---|
| **P0-0** | **F7 尾读修复 + 内存钳制 + 升窗节流 + `_read_tail` 的两个调用点同步改（`activity.py:425` / `inference_doctor.py:85`）** | 🔴 **硬门：它必须先于 P0-1 落地。** 见 §10.5 |
| **P0-1** | 谓词统一（`_turn_phase`/`_is_turn_end`/`_is_reply`）+ 5 个新行字段 | D5。依赖 P0-0 |
| **P0-2** | **删除 `hideIdle`**（F1/F2/F3） | 用户抱怨的正主 |
| **P0-3** | `AXES` 轴登记表（F4/F5）+ 分面计数（F6）+ `groupOf(session)` 六组 | 数字不许撒谎 |
| **P0-4** | 「已关闭」默认过滤 = `FILTERS.states` 上的一个预设（D2） | 不许长成第二个 hideIdle |
| **P0-5** | 表格视图 + 全局排序（D1/D4/F12） | 用户要的东西 |
| **P0-6** | 新鲜度：客户端年龄 + 陈旧横幅 + focus 重取（F9/F10）。🔴 **年龄滴答与陈旧横幅必须同一个 commit** | 见 §13 的红线 |
| **P0-7** | **Python 单测**（≥24 例；`activity.py` 的每一条改动都必须有测试） | §17 |
| **P1-1** | F8 `current_step` + `step_epoch` + **改写** `tests/test_tokmon.py:616` | 100% 的 💭 行在撒谎，但它撒的是小谎 |
| **P1-2** | `_recent_events` 谓词统一（`activity.py:297`）——⚠️ **这会改变事件总线的输出**，见 §10.4 | P6：今天它在流式片段上误发 `TASK_COMPLETED` |
| **P1-3** | `inference_doctor` 新增「回复 & 尾读」lens | 让潜伏的 F7 可被观测 |
| **P1-4** | localStorage 迁移 + 一次性横幅；展开行；卡片视图；按状态分组开关 | |
| **P1-5** | 自检不变量 S1–S7（**渲染成页面上的可见横幅**，不是 `console.warn`） | §15 |
| **P2-1** | 列显隐（model / branch） | |
| **P2-2** | 虚拟化 —— **只在单次 `render()` > 150ms 时才做** | §6.4 |
| **P2-3** | **JS 测试设施（棘轮，见 §17.2）** | |

---

## 5. 状态轴：从「分组」降级为「列 + 分面」

### 5.1 六组严格互斥划分 · `groupOf(session)`

**入参是整个 `session`，不是 `state`。** `idle` 是 `AWAITING_USER` 分支里的独立字段（`activity.py:191` / `:204`），光看 `state` 分不出 wait/idle。**这是全文最容易漏改的一行**（现状 `serve.py:1262` 是 `groupOf(state)`，V2 §2.1 已经警告过，V2 没人实现，所以警告还热着）。

| 组 | 判定 | 标签 | 徽章色 |
|---|---|---|---|
| `live` | `state ∈ {WORKING, PROCESSING}` | **运行中** | 绿 |
| `amb` | `state == AMBIGUOUS_PENDING` | **久未返回** | 琥珀 |
| `wait` | `state == AWAITING_USER && !idle` | **等你回话** | 蓝 |
| `idle` | `state == AWAITING_USER && idle` | **等你回话 · 已久**（>10min） | 蓝（淡） |
| `closed` | `state == CLOSED` | **已关闭** | 灰 |
| `unk` | `state == UNKNOWN` | **读不出** | 灰（描边） |

```js
function groupOf(s){
  const st = s.state;
  if (st === 'WORKING' || st === 'PROCESSING') return 'live';
  if (st === 'AMBIGUOUS_PENDING')              return 'amb';
  if (st === 'AWAITING_USER')                  return s.idle ? 'idle' : 'wait';
  if (st === 'CLOSED')                         return 'closed';
  if (st === 'UNKNOWN')                        return 'unk';
  return null;                                  // 🔴 后端出现了前端不认识的 state
}
```

🔴 **`return null` 不是 bug，是设计。** 现状 `CLS[state] || 'unk'`（`serve.py:1262`）会把「后端新增了一个状态」**伪装成「读不出」**——一次真实的漂移被静默降级成一个已知类别。V3 里它进一个只在 `N>0` 时才出现的红色 pill：`⚠ 未覆盖 N`，**且这些行照常显示，绝不吞**（S1，§15）。

> 🔴 **未覆盖的行不受 `states` 轴管辖**：它永远显示，只由 `⚠ 未覆盖 N` 红 pill 单列计数；点那枚 pill 才筛它。
> （落到代码上就是 §8.1 `states` 轴的 `pass` 里那句 `g === null || …`。**默认的 `PRESET_UNFINISHED` 不含任何未来状态的名字——如果 `pass` 写成 `F.states.includes(groupOf(s))`，`[].includes(null)` 为 `false`，一次真实的后端漂移会被这一页默认删除，那正是 F1 的形状。**）

### 5.2 改名是有意的：「空闲」是反的

旧标签「空闲」在语义上**是反的**。它实际的意思是「**Claude 已经回复了你，超过 10 分钟没人接话**」——**这是你欠 Claude 一句话**，不是「它闲着没事」。**160 行里有 70 行顶着一个意思恰好相反的标签**，而「藏起来无所谓」的心理起点，正是这个词。

**新标签直说它是什么：「等你回话 · 已久」。** 这是全页面最便宜的一次认知负担削减。

### 5.3 「后台在跑」是标记，不是状态

`background`（`classify_state` 已把它折进 `WORKING`，`activity.py:77-82`）**永远不进状态轴** —— 一个 WORKING 会话可以同时在跑后台任务，它**不互斥**。它是状态单元格里的一枚角标 `⚙ 后台`。

**放弃了什么**：不能「只看后台在跑的」。
**为什么值**：往互斥轴里塞一个非互斥标记，**正是 V2 诊断出的根因 1**。为了 3 行会话重蹈那个覆辙，不划算。

### 5.4 服务端排序不动

`activity.py:506-507` 的 `key=(_STATE_ORDER[state], last_activity_age_s)` **保持原样**——它从此只是一个稳定的初始顺序，前端总是自己重排。
**`/api/sessions` 的契约变为：顺序未定义，消费者自己排。** 少改一处后端。

---

## 6. 表格（data-frame）视图

**默认视图 = 表格。** 默认要看 79 行，卡片扫不动；表格才可能兑现「5 秒内回答」。卡片视图保留为切换项（`VIEW: 'table' | 'card'`，持久化）。

### 6.1 列（六列，每一列都要为「推进 / 卡住 / 等我」出力）

`table-layout: fixed` + `<colgroup>`；外层 `.wrap{overflow-x:auto}`（复用 `PROC_PAGE` 已有的模式，`serve.py:955`/`1004`）。`main{max-width}` 从 1100px 提到 **1400px**（`serve.py:1137`）。

| # | 列 | 宽 | 内容 | 排序键 |
|---|---|---|---|---|
| 0 | ▸ | 24px | 展开 | — |
| 1 | **状态** | 100px | 彩色徽章（复用 `.sb`，`serve.py:1163-1168`）+ `⚙ 后台` / `⎋ 被打断` 角标 | `state`（组序） |
| 2 | **项目** | 150px | `project`（`subpath` 灰色小字），`title=` 全名 | `proj` |
| 3 | **标题** | 1fr(≥200px) | `title` → 回退 `last_text` → 回退 `session_id[:8]` | `title` |
| 4 | **Claude 回复** ⭐ | 104px | `fmtAgo(now − last_reply_epoch)`，`title=` 绝对时刻 | `reply` **默认** |
| 5 | **我说话** ⭐ | 104px | `fmtAgo(now − last_user_epoch)`，`title=` 绝对时刻 | `user` |
| 6 | **当前步骤** ⭐ | 1.4fr(≥240px) | `▶工具` / `💭思考` / `💬叙述` + 文本（单行省略号）+ **`step_epoch` 的年龄** | `step` |

**不进列**：`model` / `git_branch` / `cwd` / `session_id` / `file` / `最后活动`。它们进**展开区**，且**仍在文本搜索范围内**（`text` 轴不变）。
**放弃的东西**：不能按模型排序。可接受——真要按模型看，用文本搜索。

### 6.2 🎯 第 4/5 列并排，较新的那格加粗

> **两个时间单元格里，较新的那个加粗高亮，较旧的那个压暗。**

一眼扫下去：**一列亮 = 球在 Claude 那边**（它欠你一个回复 → 在推进 或 卡住）；**另一列亮 = 球在你这边**（它回完了 → 等你）。

**为什么第 5 列必须是 `last_user_epoch`，而不是现成的 `last_activity_epoch`：** 后者是 `_last_message()`（`activity.py:267-271`）= assistant **或** user 的最新一条，**它包含 `tool_use` 的 assistant 行和 `tool_result` 的 user 行** —— 它在每一个工具步上都在跳。它**恒 ≥ `last_reply`**，所以「两者之差」**只能告诉你有人后说了话，告诉不了你是谁**。
**只有 `last_user_epoch`（真人开口，§10.3）能把「球在谁那边」画出来。** 零列宽成本，且顺手把「卡住检测器」规则层的两个入参交付了。

**为什么不做一个「球权」列**：它就是「谁最后说话」，而加粗已经把它画出来了，**零列宽**。**放弃了对「球权」单独排序的能力** —— 状态列已经覆盖了这个语义（`wait`/`idle` ⟺ 球在你这边）。

**为什么第 6 列必须带 `step_epoch`：** `▶ Bash · 40m 前` 出现在一行**运行中**的徽章旁边——**这就是「卡住」的信号本身**，而且是唯一不需要点一下就能看到的那个。没有它，一个 40 分钟前冻住的 `live` 行和一个正在飞的 `live` 行**逐字节完全相同**。

### 6.3 展开行

点行（或 `▸`）→ 其下插入 `<tr class="exp"><td colspan="7">`，内容 = **今天的卡片正文，一字不改**：完整 `state_label`（含「久未返回=无法从 transcript 区分…」的对冲原话）、完整 `current_step`、`recent_events` chips（复用 `evChip`，`serve.py:1279`）、`model` / `git_branch` / `cwd` / `session_id` / `file`、三个时间的**绝对值**、以及 `last_reply_absent` 的人话解释（§10.2）。

**两种视图不是两套渲染，是同一份数据的两级详略**（V2 §2.5 的正确直觉，保留）。卡片视图 = 「全部行都处于展开态」的表格。同一条管线：`applyFilters → sortRows → 两个渲染器之一`。

🔴 **展开态存在 `EXPANDED: Set<session_id>`（JS 变量），30s 重渲染后从它恢复。绝不存 DOM。**
这是 V1 §3.1「chips 选中态被重建冲掉」那个坑的**同型复发**：**DOM 是投影，不是存储。** 排序状态、滚动位置同样必须扛过重渲染。
**实现时必须真的等满 30 秒验一次**，别点一下就说好了。
**不持久化到 localStorage** —— 展开是瞬时意图，存下来只会在明天复活一堆陈旧的展开行。

### 6.4 性能契约（160 行 → 1600 行）

- **不截断、不分页、不虚拟化。全部渲染。** 1600 行 × ~400B ≈ 640KB，一次 `innerHTML` 约 30–60ms，每 30s 一次。可接受。
- 🔴 **「只渲染前 400 行 + 点击加载更多」是禁止的**：那是在用户和答案之间插一次点击，而且它是一个**隐形过滤器**的变种。**这个页面已经因为这个死过一次了。**
- 🔴 **5s 的年龄 tick 绝不能整表重排。** 年龄单元格写成 `<span class="age" data-epoch="…">`，tick 只做
  `document.querySelectorAll('[data-epoch]').forEach(el => el.textContent = fmtAgo(nowS() - +el.dataset.epoch))`。
  1600 次 `textContent` 写入 ≈ 几毫秒，**零 layout thrash**。**这就是「年龄从 epoch 客户端算」不只是诚实性需求、也是性能需求的原因。**
- **何时该虚拟化**：单次 `render()` > **150ms** 时。阈值写死在这里，别拍脑袋。

### 6.5 不复用 `tableOf`

`serve.py:620-633` 的 `tableOf` **不是通用表格渲染器** —— 表头写死成 `名称/Tokens/Input/Output/Cache R/W/Cost/Msgs`，是 token 看板的形状。审计说它「sessions 未使用」是对的，**并且应该继续不使用**。硬凑复用只会把两个页面焊死。

---

## 7. 排序模型

```js
const SORTS = {
  reply: s => s.last_reply_epoch,        // 默认
  user:  s => s.last_user_epoch,
  step:  s => s.step_epoch,
  act:   s => s.last_activity_epoch,     // 旧行为的等价物, 保留为可选键
  state: s => GRP_RANK[groupOf(s)] ?? 99,  // 🔴 未覆盖 (groupOf → null) 排最后。别写成 GRP_RANK[groupOf(s)]:
                                           //    那给出的是 undefined, 靠 `undefined == null` 碰巧走对 null 沉底分支
  proj:  s => (s.project || '').toLowerCase(),
  title: s => (s.title   || '').toLowerCase(),
};
let SORT = { key: 'reply', dir: 'desc' };   // D4
let GROUPBY = false;                        // 「按状态分组」默认 OFF
```

| 项 | 决定 |
|---|---|
| **默认** | `reply` **降序**、**全局、不分组**（D4）。打开页面第一眼 = **刚回完你的会话在最上面** |
| **方向** | 点列头切键；再点同一列头翻转。**不做「第三次点击复位」**——多一个隐藏态就多一个说谎的机会 |
| **首次点击方向** | 时间键 → `desc`；文本键 → `asc`；`state` → `asc`（要动手的在上） |
| **null 处理** | 🔴 **null 永远排在最后，两个方向都是** |
| **平局链** | 主键 → `last_activity_epoch` desc → `session_id` asc（**全序**） |
| **持久化** | `SORT` + `GROUPBY` 进 localStorage |

### 🔴 null 永远沉底 —— 这是一条原则 1 的排序规则，不是审美

```js
if (a == null && b == null) return tie();
if (a == null) return 1;                 // 先于 dir 判断
if (b == null) return -1;
```

`last_reply_epoch == null` 的含义是「**不知道**」，不是「很久以前」，也不是「刚刚」。
**最常见的实现者错误是 `(x.last_reply_epoch || 0)`** —— `|| 0` 让「未知」冒充 1970 年：升序时它**浮到第一行，伪装成「最该看的那个」**。
**未知只能沉底，不能参与比较。** 行里显示 `—`，页脚计数（S5，§15）。

### 全序不是优化，是硬要求

页面每 30s 重渲染。若比较器对相等元素不稳定，等值行会在每一帧之间**互相换位**——**画面在动，但什么都没发生**。这是一种低烈度的、持续的说谎，而且 5 秒扫描直接报废。`session_id` 兜底保证全序：**只要数据没变，画面就一个像素都不动。**

### 「按状态分组」开关（默认 OFF）

打开后按 `GRP_RANK` 切成 6 段，**所选排序在每段内部生效**，组头显示该组的分面计数。

```js
const GRP_RANK = { amb:0, idle:1, wait:2, live:3, unk:4, closed:5 };  // 按「这行有多想让你动手」排
```

**默认关的理由（D4）：** 分组会**囚禁**用户真正要的那个全局排序。用户要的是「谁最近回了我」，分组把它变成「在每个状态桶里，谁最近回了我」——**那是另一个问题。要分组的人自己开。**

---

## 8. `AXES` 轴登记表：让「隐形过滤器」在结构上无法存在

### 8.1 一张表，三个消费者

**F4 的教训不是「有人忘了往 `bits` 里加两项」，而是「`applyFilters` 和 `bits` 是两份手写的清单」。** 只要它们是两份，就总有一天会分叉。

```js
const AXES = [
  { id:'projects', active:F => F.projects.length > 0,
    pass:(s,F) => F.projects.includes(s.project || ''),
    label:F => '项目=' + F.projects.map(p => p || '(无项目)').join(', '),
    facetable:true,  valuesOf:s => [s.project || ''] },      // 划分型: 每行恰好落一个取值
  { id:'states',   active:F => F.states.length > 0 && F.states.length < 6,
    pass:(s,F) => { const g = groupOf(s); return g === null || F.states.includes(g); },  // 🔴 未覆盖的行永不被状态轴排除
    label:F => fmtStatesLabel(F.states),                     // §9.3 的语法糖只在这里
    facetable:true,  valuesOf:s => [groupOf(s)] },           // 划分型; groupOf 为 null 的行单列计入「⚠ 未覆盖 N」
  { id:'ageMax',   active:F => F.ageMax != null,
    pass:(s,F) => { const a = ageOf(s); return a != null && a <= F.ageMax; },
    label:F => '最后活动（你或 Claude）在 ' + AGE_LABEL[F.ageMax] + ' 内',   // ← 文案必须写全, 见下
    facetable:false, valuesOf:null },                        // 🔴 嵌套窗 (近1h ⊂ 近24h ⊂ 近7d), 不是划分
  { id:'text',     active:F => !!F.text,
    pass:(s,F) => TEXT_FIELDS.some(k => matches(s[k], F.text)),
    label:F => '搜索=「' + F.text + '」',                      // ← F4: 文本搜索从此不再隐形
    facetable:false, valuesOf:null },                        // 🔴 根本没有离散取值集
];

const applyFilters = (rows, F) => rows.filter(s => AXES.every(a => !a.active(F) || a.pass(s, F)));
const bits         = F        => AXES.filter(a => a.active(F)).map(a => a.label(F));
```

> 🔴 **`applyFilters` / `bits` / `facetCounts` 三者遍历的是同一张表。**
> **往 `AXES` 加一个轴 = 它同时开始收窄结果、同时出现在 fbar、同时被计入分面。**
> **一个能收窄结果却不出现在 fbar 的过滤器，从此在结构上无法被写出来。**

🔴 **`facetable` 不是装饰，它是 S4 的作用域。** 只有**划分型**轴（每行恰好落一个取值：`states` / `projects`）才满足 §15 的 Σ 恒等式；`ageMax` 的取值是**嵌套窗**（Σ 三窗远大于总数），`text` 根本没有取值集。**S4 只遍历 `facetable: true` 的轴**——否则一个正确的实现会被自己的不变量红条常年挂在页面上（那是用一次 P6 误报去防 F6）。

🔴 **「最近」在 V3 里有两份度量，这是有意的，声明在此：** `ageMax` 轴以 `last_activity_epoch`（你**或** Claude 的最后一条）度量，而默认排序键是 `last_reply_epoch`（**只有** Claude）。**它们回答两个不同的问题**（「这个会话还热着吗」 vs 「谁最近回了我」），所以两份度量都留。**代价是 fbar 文案必须写全**：`最后活动（你或 Claude）在 1h 内`，**不许简写成「1h 内」**——简写会让人以为它筛的是「Claude 1h 内回过」。

再叠一条运行时断言防漂移（**S3**，§15）：`Object.keys(FILTERS)` 的集合 === `AXES` 的 id 集合。**结构防写，断言防漂移，两个都要。**

### 8.2 分面计数（数字不许撒谎）

**语义**（V2 §2.2，保留）：chip/pill 上的数字 = **在其它轴的过滤之下，点它会真的出多少条**（标准分面：**轴内 OR、跨轴 AND**）。

🔴 **写全，别留「再按取值分组计数」这样的一行注释。** 分面计数是 **F6 的正主**——一句注释带过，五个实现者会写出五份，`2/36` 的分母尤其。**唯一的 helper 长这样，不许手写第二份：**

```js
const facetCounts = (rows, F, axisId) => {                       // -> Map<value, count>
  const ax = AXES.find(a => a.id === axisId);
  const base = rows.filter(s => AXES.every(a => a.id === axisId || !a.active(F) || a.pass(s, F)));
  const m = new Map();
  for (const s of base) for (const v of ax.valuesOf(s)) m.set(v, (m.get(v)||0) + 1);
  return m;
};
// chip/pill 的 "2/36": 分子 = facetCounts(d.sessions, F, axisId).get(v)||0
//                      分母 = facetCounts(d.sessions, {空 F}, axisId).get(v)||0   ← 同一个 helper, 不许手写第二份
```

（`valuesOf` 在 §8.1 的 `AXES` 上：`states` → `[groupOf(s)]`、`projects` → `[s.project||'']`。`facetable:false` 的轴没有 `valuesOf`，也没有分面计数——它们不出 chip/pill。）

**硬保证（= 验收判据）：**
> **该轴未选任何项时，点一个显示 N 的 chip/pill，出来的行数必须恰好是 N。**

**病态情形（V1 §3.2 只答对了一半）：** 一个被选中的 chip 显示 `0`，有**两种完全不同的原因**，**必须分开说**：
- **本帧全局就没有这个项目的会话** → 「项目 X 在本帧无会话」（V1 已做对）
- **被其它轴筛没了**（全局 36，分面 0） → **这是完全不同的一句话**

**所以 chip 的数字直接渲染成 `2/36`（分面 / 全局）。** 从源头掐死 V2 那两张截图。

`d.counts` 从此**只用于交叉校验**（S2，§15），**不再驱动任何 pill**。

### 8.3 常驻交代

- **总数 pill：`共 160（当前显示 79）`**，且 `shownNote` **无条件渲染**（修 F5：`serve.py:1369` 现在只在 `!bits.length` 时才出现——一开任何过滤器，唯一那句「有东西被藏了」反而消失）。
- **V1 已经做对的两条诚实提示，全部保留**：选中项目本帧无会话 → 不静默变「全部」（V1 §3.2）；时间窗排除了 `epoch == null` 的会话 → fbar 明写「另有 N 个会话时间读不出，已被时间窗排除」（V1 §3.3）。
- **空态文案必须精确**（今天的「无匹配会话」是个死胡同）：

| 情形 | 文案 |
|---|---|
| 有 filter，0 行 | **当前过滤条件下没有会话。** 160 个会话全部被 `项目=X · 状态=运行中` 排除了。`[清除全部过滤]` |
| 无 filter，`total===0` | **没有发现任何 session**（`~/.claude/projects` 下没有 main transcript）。 |
| 文本搜索 0 命中 | **「foo」没有匹配任何会话**（已在 79 个会话中搜索标题/项目/步骤/工具/分支）。`[清空搜索]` |

**永远不允许出现一个不带「总数 + 清除按钮」的空态。**

---

## 9. 「已关闭」默认过滤（D2）—— 结构上不可能变成第二个 `hideIdle`

### 9.1 `hideIdle` 的七宗罪，其实是同一宗

| | 罪状 | 位置 |
|---|---|---|
| H1 | 活在 `FILTERS` 之外 | `serve.py:1255` |
| H2 | 不被 `saveFilters` 持久化 | `serve.py:1261` |
| H3 | 默认开 | `serve.py:1194` |
| H4 | 不进 `bits` | `serve.py:1327-1330` |
| H5 | 一开别的过滤器，`shownNote` 就消失 | `serve.py:1369` |
| H6 | pill 没有 `.clk`，点不动 | `serve.py:1309` vs `:1388` |
| H7 | 计数在全量上算 | `serve.py:1298` |

**七条的本质是一条：它享有特权——它是第二套过滤机制。**

### 9.2 🔴 因此：不新增 `hideClosed` 布尔量。默认过滤**就是一次普通的状态选择**

```js
const PRESET_UNFINISHED = ['live','amb','wait','idle','unk'];   // 就是「除 closed 外的全部」
// 首次访问 (localStorage 无记录) → FILTERS.states = [...PRESET_UNFINISHED]
```

于是它**自动地、无需任何专门代码**获得 D2 要求的全部四项属性：

| D2 的要求 | 靠什么保证 |
|---|---|
| **可见** | 它是 `states` 轴的一个选择 → §8.1 的 `AXES` 表**自动**把它渲进 fbar |
| **可点** | 6 枚 pill 由同一个数组渲染 → `已关闭` pill **必然**有 `.clk`，只是显示为「未选中」 |
| **计数诚实** | 分面计数照常算 → pill 显示 **81** → 点它 → **真的 81 行**（S4 按构造成立） |
| **持久化** | `saveFilters` 存的就是 `FILTERS` |
| **一键清除** | fbar 的「清除全部过滤」本来就清 `FILTERS.states` |
| **永远不会变成 hideIdle** | **因为它压根不是一个独立的过滤器** —— 没有第二套机制可以跟 UI 脱节 |

**被拒绝的替代方案：** 加一个 `FILTERS.hideClosed = true` 布尔量。**拒绝的理由就是它的形状和 `hideIdle` 一模一样**——一个平行的、需要人记得在四个地方同步的特权开关。**我们刚被这个形状咬过。**
而且如果它是独立布尔，会立刻和 §8.2 打架：「只看未结束」为 ON 时，`已关闭` pill 显示 81 → 点它 → **0 行**。**那就是 F6 的截图，一字不差地重演。**

### 9.3 语法糖（是糖，不是机制）

当且仅当选中集合恰好 == `PRESET_UNFINISHED` 时，fbar 渲染为：

```
过滤中：只看未结束（已排除 81 个已关闭） · 显示 79/160  [+ 显示已关闭]  [清除全部过滤]
```

控件区放两个预设按钮：`[只看未结束]` `[全部]` —— **它们只是往 `FILTERS.states` 里写值。**

> **糖是糖，轴是轴。轴是唯一真相源。糖崩了最多是文案难看，筛选逻辑不会撒谎。**

### 9.4 为什么默认藏 CLOSED 是正当的（而藏 idle 不是）

81 个 CLOSED 的**中位年龄 231.2 小时（9.6 天）**，最老 45 天；`CLOSED` 的判定是「**进程已退出**」（`activity.py:89-95`，靠 procmon 活性，是三种判定里**唯一不靠 transcript 推断**的一种）——它们**在任何意义上都不可能需要你现在介入**。
而 `idle` 的含义是「**Claude 刚回了你，你还没接话**」——**那正是你最该看见的东西。**
**一个是历史，一个是待办。旧代码把它们搞反了。**

### 9.5 首屏成绩单

默认 **79 行**（160 − 81），**71 个「等你回话 / 等你回话·已久」全部在列**（今天：1 个）。

> **这就是 V3 的全部意义：把用户唯一在问的那个问题的答案，从 1/71 变成 71/71。**

---

## 10. 后端契约（D5）：`last_reply_epoch` 与它的四个同伴

### 10.1 凭什么改内核？—— 这是本文最可争议的一条，正面回答

**(a) V1/V2 的「零后端改动」不是宪章。**
V1 的 `SESSIONS_FILTER_PLAN.md:154` 和 V2 的 `:172` 里，「`git diff` 只应碰 `serve.py`」写在**「完成判据」**一节；V1 自己解释得很清楚：「**这也是它能『插队』的原因：风险面极小**」（`SESSIONS_FILTER_PLAN.md:29`）。**那是一句关于插队优先级的话，是那两个小切片自己给自己设的审慎上限——不是 NORTH_STAR 或 MISSION_CONTROL 里的任何一条不变量。**

**(b) `activity.py` 不在 I6 说的那个「内核」里。**

> **原则 4：核心数据层 vs 展示层分离。**「`discovery/parser/pricing/aggregate` 是稳定内核；`report/tui/(未来的 web/export)` 都只是它的消费者。」（NORTH_STAR.md:36-37）
> **I6：展示层不反向污染内核。**「TUI/report 的需求不能逼内核引入**展示相关的状态**。」（NORTH_STAR.md:90）

**内核被逐一点名了。`activity.py` 不在其中**——它自己第一行就声明「**不 import** token 内核(parser/aggregate/pricing/records)」（`activity.py:8`）。它是**另一根支柱自己的内核**：

> **P4 支柱独立。**「token / **进程** / **对话活动**各自的内核互不引用；平台只在事件总线层汇合。」（MISSION_CONTROL.md:179）

**I6 禁止的是「让 A 支柱的内核被 B 的展示需求污染」。改对话活动支柱自己的内核，从来不是 I6 禁止的事。**

**(c) 🔴 红线，一句话钉死，且本设计不越线：**

> **内核可以多产出一个「事实」，但绝不能产出一个「视图意见」。**
>
> **允许**：`last_reply_epoch`（transcript 里的一个时刻，是事实）。
> **永远禁止**：`sort_key` / `hidden` / `default_filtered` / `row_class` / `is_important` / **`last_reply_age_s`**。

**`last_reply_age_s` 被明确拒绝**——「服务端在采样那一刻算好的年龄」**正是 F10 的病根**（`serve.py:1361`）。**新字段只发 epoch，年龄一律由客户端从 epoch 现算。** 造一个新的同款器官 = 给下一个实现者递枪。
同理：`last_reply_absent` **只发原因码**（`'never'|'out_of_window'|…`），**中文措辞归 UI 所有**。这是本切片里 I6 真正有风险的那一处，我们守住它。

**(d) 「Claude 上一次把回合还给我，是什么时候」本来就该在那儿。**
它和 `last_activity_epoch`（**已经在 `activity.py:116`**）**完全同类**，是 MISSION_CONTROL.md:62-67 里 Layer 1「监控层产出原始信号」的定义域。**它今天不在 payload 里，是遗漏，不是分层。**

**(e) 决定性的一条：这次改动让内核里的定义变少，不是变多。**

全量扫描 161 个 main transcript / 46,389 条 assistant 记录：

| 判据 | 命中 |
|---|---|
| D5 原文的朴素判据 `type==assistant && stop_reason != 'tool_use'` | **2,523** |
| 状态机真正在用的判据（还排除 `max_tokens` / thinking-only / 末块 tool_use） | **1,533** |
| **两者之差** | **990 条 = 39%** |

差在哪：**981 条是「thinking-only 但 `stop_reason == end_turn`」的流式中途片段**（+ 6 条 `max_tokens` + 3 条挂起工具尾）。而 `activity.py:171-183` 的注释白纸黑字写着：**L2 实测 173/173 这类行后面都还有续接片段，0 个是真结束。**

> 🔴 **如果照 D5 字面实现，我们会在同一个文件里造出「回合结束」的第 4 份定义，并且它在 39% 的记录上和状态机公开打架。**
> **更糟的是**：`_recent_events` 的 `task_completed` 判据（`activity.py:297`：`stop_reason in ("end_turn","stop_sequence")`）**正是这份错误判据** —— **今天 UI 上那个 `✓ 完成` chip 已经在流式思考片段上乱闪了，而且它还在往事件总线里发误报的 `TASK_COMPLETED`**（见 §10.4）。

**所以 D5 不是「为了 UI 而妥协的破例」，它是「把一份早就该合并的定义合并掉」。改完之后 `activity.py` 比改之前更纯。**

**(f) 代价，说清楚：** 这个切片不再「风险面极小、天然安全」。它现在**必须**有 python 单测才能交付（§17）——**而这恰好是 V1/V2 做不到的事（纯前端）。用「必须写测试」换「不再撒谎」，值。**
补一句实话：**V3 里风险最高的部分，恰好是唯一被单测覆盖的部分。**

### 10.2 谓词：一个 phase，三个消费者，**两个函数**

在 `_classify_transcript`（`activity.py:100`）**之上**新增四个纯函数（无 I/O，I2）：

```python
def _turn_phase(o: dict) -> str:
    """一条记录处在回合的哪个阶段。**这是「回合结束了吗」在本文件里的唯一定义。**
    返回: 'other' | 'tool' | 'max_tokens' | 'thinking' | 'end'
    优先级必须与 _classify_transcript 现有分支顺序**完全一致** (:149 > :161 > :175 > :184)。"""
```

| phase | 判定（按顺序） | 含义 |
|---|---|---|
| `other` | `type != "assistant"` | 不是 Claude 的话 |
| `tool` | 末块是 `tool_use` **或** `stop_reason == "tool_use"` | 回合**未**结束（工具在飞） |
| `max_tokens` | `stop_reason == "max_tokens"` | 回合**未**结束（会自动续写） |
| `thinking` | blocks 含 `thinking` 且**不含** `text` 且**不含** `tool_use` | 回合**未**结束（流式中途片段，L2 已证） |
| `end` | 其余（含 `stop_reason` 为 `end_turn` / `stop_sequence` / **`None`**） | 回合结束 |

```python
def _is_turn_end(o) -> bool:
    """纯结构判定。**synthetic 算回合结束** —— 因为状态机就是这么判的 (activity.py:187 之后 fall-through
    到 AWAITING_USER)。这个函数与 classify_state 的 AWAITING_USER 分支**逐字节等价**, 由 T-EQ 钉住。"""
    return _turn_phase(o) == "end"

def _is_reply(o) -> bool:
    """「Claude 真的回了我一句话」。= 回合结束 且 不是合成 no-op 且 不是子会话内部回合。"""
    return (_is_turn_end(o)
            and not _is_synthetic(o)                 # 与 activity.py:187 同源
            and o.get("isSidechain") is not True)
```

> 🔴 **`_is_turn_end` 和 `_is_reply` 必须是两个函数。这不是洁癖，是一个必炸的雷。**
> 状态机对 **synthetic**（`model == "<synthetic>"` 或 text 为 `"No response requested."`，语料 68 条）**仍判 `AWAITING_USER`**（你确实该说话了）。
> 如果把 synthetic 折进 `_is_turn_end`，那么用它去守 `activity.py:184` 的分支时，synthetic 记录会**掉过整个 assistant 块**、落到 `activity.py:216` → **`UNKNOWN · 消息结构异常`** → `_DEAD_STATES` → **该会话的事件总线彻底静默**，且**在默认的「只看未结束」过滤下，它会从屏幕上直接消失。**
> **今天没有任何一个测试钉住这件事**（`synthetic` 在测试里只出现在 pricing/doctor）。**它会静默上线。** → 单测 **T7** 专门钉住这个**刻意的**差异。

**三个消费者，一份定义：**

| 消费者 | 位置 | 用哪个 | 行为变化 |
|---|---|---|---|
| 状态机 `_classify_transcript` | `activity.py:144-194` | `_turn_phase` 分支 | 🔴 **零行为变化。现有 21 个调用 `classify_state` 的单测必须全绿、一字不改。** 但**它不是「行为不变」的唯一证明**——见下 |
| `_recent_events` 的 `task_completed` | `activity.py:297` | `_is_reply(o)` | **修 bug**，见 §10.4 |
| **新** `_last_reply_epoch` | §10.3 | `_is_reply(o)` | 新字段 |

🔴 **「行为不变」的强证明已经在仓库里，别只靠 21 条 fixture。**
`tokmon/inference_backtest.py`（`run_backtests`，CLI 已接：`cli.py:49` / `:78`，即 **`tokmon backtest`**）会把 `classify_state` **全语料回放**（EVOLUTION.md 里那个 L2 ~97%）。**21 条 fixture 是设计者编的输入；backtest 跑的是 161 个真 transcript。** `_turn_phase` 重构最强的行为不变证明就在手边——

> **改动前后各跑一次 `python -m tokmon backtest`，三把尺的数字必须逐位相同。** 这才是全语料证明（A16）。

⚠️ **但它有明确的覆盖边界，写在这里免得被当成万能挡箭牌**：backtest **只跑 `classify_state`**，**不覆盖 `_recent_events` 的改动**（§10.4 那次事件总线行为变更）——那部分只能靠 **T24 / T25**。

### 10.3 五个新行字段 · 契约（把边角逐个钉死）

| 字段 | 类型 | 说明 |
|---|---|---|
| `last_reply_epoch` | `int \| None` | 尾窗内最新的一次「Claude 把回合还给你」的时刻 |
| `last_reply_absent` | `str \| None` | **原因码**；`None` ⟺ `last_reply_epoch` 非 None。取值见下 |
| `last_user_epoch` | `int \| None` | 尾窗内最新的一次「**真人开口**」的时刻 |
| `step_epoch` | `int \| None` | `current_step` 那条记录的时刻（§12） |
| `tail_complete` | `bool` | 本次尾读是否覆盖到文件第 0 字节 |

**🔴 `last_reply_absent` 的五个原因码 —— 把它们糊成一个 `—` 就是整个 D5 的失败：**

| 码 | 含义 | 为什么必须分开 |
|---|---|---|
| `never` | `tail_complete == True`，读了整个文件，确实一次完整回复都没有 | **已证实** |
| `out_of_window` | `tail_complete == False`，256KB 尾窗里没找到，更早的没读 | 🔴 **回复很可能存在！** 一个长工具循环塞满 256KB，会让一个**非常活跃**的会话在窗口里找不到回合结束。**把它标成「从未回复」再按这个排序 —— 你会把最忙的会话排到最底下，并且理直气壮。** |
| `oversize` | 末条记录超过 `_MAX_TAIL_BYTES` 硬顶（§11） | 说得出「为什么读不出」，不是笼统的「读不出」 |
| `unreadable` | 文件读不出 / 解析异常 | 此时 `state` 本就是 `UNKNOWN` |
| `bad_timestamp` | 找到了最新的那条回复，但它的 `timestamp` 读不出（naive / 缺失） | 🔴 **返回 `None`，绝不回退到更早的那条回复。** 回退 = 「它 3 小时前回的你」，而真相是「它 5 分钟前回了你，只是时间戳坏了」。**那是撒谎。** |

**`_is_reply` 的边角，逐条：**

| 情形 | 算不算回复 | 为什么 |
|---|---|---|
| `stop_reason == "tool_use"` / 末块挂着 tool_use | ❌ | 回合没完 |
| `stop_reason == "max_tokens"` | ❌ | 触顶截断，Claude 会自己续写（`activity.py:161-170`）；它不是在等你 |
| **thinking-only + `end_turn`**（语料 981 条） | ❌ | 流式中途片段（`activity.py:171-183`，L2 实测 173/173） |
| `stop_reason == None` + 有 text（语料 6 条） | ✅ | 与状态机一致（它判 AWAITING_USER）。**宁可与状态机一致地「错」，也不要两处不一致** |
| **synthetic**（语料 68 条） | ❌ | Claude Code 的 no-op，**不是 Claude 回了你**。⚠️ 状态机仍判 `AWAITING_USER` —— **这个差异是设计**（T7 钉住） |
| **`isSidechain == True`** | ❌ | ⚠️ **诚实标注：本机语料 0 条命中**（46,389 条 assistant 记录里一次都没出现；`discover()` 也只喂 `kind=="main"`）。**这一条是防御性的，今天空转。写下来是因为一旦 Claude Code 改成内联子会话，我们不想在那天才发现。** |
| **你打断了它**（`[Request interrupted by user]`，`type == "user"`） | ❌（`phase == other`） | 你把它掐了，它没回你。状态机仍判 `AWAITING_USER`——于是该行显示 **「状态=等你回话 / Claude 回复=3h 前 / 我说话=3h 前(加粗)」**。**这正是真相**，旧 UI 根本没有能力表达它 |

**「真人开口」（`last_user_epoch`）：**

```python
def _is_human_turn(o) -> bool:
    if o.get("type") != "user": return False
    c = (o.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return False                                    # 机器续接
    if _lead_text(c).strip().startswith("<task-notification>"):
        return False                                    # 子代理回执
    return True                                         # 含 [Request interrupted by user]: 打断也是你开的口
```
**打断算「我说话」** —— 你按了 ESC，球确实回到了你手上。

**落点（`activity.py`，精确到函数）：**

| 动作 | 位置 |
|---|---|
| 新增 `_is_synthetic` / `_turn_phase` / `_is_turn_end` / `_is_reply` / `_is_human_turn` | `activity.py:~98`（`_classify_transcript` 之前） |
| `_classify_transcript` 的四个分支改为 `switch(_turn_phase(last_msg))` | `activity.py:144-194`（**行为不变**） |
| `_recent_events` 的 `task_completed` 判据改用 `_is_reply` | `activity.py:297` |
| **新** `_turn_epochs(objs, tail) -> dict`（**一趟反向扫描同时拿到 reply + user + 原因码**） | `activity.py:~272`（紧挨 `_last_message`，`:267-271`） |
| `_session_row` 里 `row.update(_turn_epochs(...))` | `activity.py:448-462` |
| 🔴 **两个降级字典也必须加这 5 个键** | `activity.py:431-439`（文件读不出/空）与 `activity.py:494-501`（解析异常） |
| 🔴 **`background` 补进恒定 schema**：`_classify_transcript` 的 out 初始字典补 `"background": False`；**两个降级字典同补** | `activity.py:102-107`（初始字典，**今天没有 `background` 键**——它只在 `bg_open` 分支和 `liveness is False` 分支被写入）+ `:431-439` + `:494-501`。**零风险**（既有读法都是 `s.get("background")`），但 **§6.1 / §5.3 的 `⚙ 后台` 角标依赖它**——不补，`s.background` 在绝大多数行上是 `undefined` |
| 🔴 **`_session_row` 降级字典的 `state_label` 由 `tr.reason` 三分**（否则 §11.2 的 `oversize` 文案**无处落地**：`state_label` 只由 `classify_state` / `_classify_transcript` 产出，而它们只吃 `last_msg`，**看不到 `reason`**；`objs == []` 时 `:426` 直接走降级字典，今天文案写死「无法判断 · 文件读不出/空」） | `activity.py:431-439`。三条**各一句，不许合并**：<br>· `oversize` → **「读不出 · 末条记录超过 32MB, 尾部窗口无法定位完整记录」**<br>· `unreadable` → **「无法判断 · 文件读不出」**<br>· `objs` 空但 `complete == True` → **「无法判断 · transcript 里没有对话消息」** |

🔴 **schema 必须恒定。前端绝不能同时遇到 `undefined` 和 `null` 两种「没有」。**
（这条红线**同样适用于 `background`** —— 它今天就是一个活生生的反例，见上表。**只把红线套在 5 个新字段上、对自己要用的旧字段视而不见，就是又立一份第二定义。**）

**成本：零额外 I/O。** 它走的是 `_current_step`（`activity.py:336`）已经在走的那趟 `reversed(objs)`；`objs` 是同一份、已缓存的尾窗。
🔴 **这句话只对「5 个新字段」成立，对 F7 的升窗路径不成立**（升窗会重 seek/重 parse，且 `(mtime,size)` 缓存在追加写时每帧都 miss）——**那条路的成本与闸门写在 §11.2「升窗节流」，别把这句「零额外 I/O」当成全局结论。**

**明确不碰的函数（`activity.py` 内）：** `classify_state` 的两个 hint 叠加层（`:76-96`）、`_last_message`、`_last_assistant_model`、`_conversation_title`、`_is_bg_launch`、`snapshot` 的排序与 `counts`。**逐一列出，是为了让 review 有一个可核的边界。**

### 10.4 ⚠️ 一处诚实的例外：`_recent_events` 会改变**事件总线的输出**

**三份候选设计和三个评审全都说错了这一条，我核了代码：**

> `activity.py:284` 的 docstring 写着「**仅 UI 内的观测事实, 不是 §4 事件总线**」——**这句话是错的（陈旧了）。**
> `event_sources/activity_source.py:61` 就是 `facts = row.get("recent_events") or []`，然后 `:125-127` 把 `kind == "task_completed"` 直接翻译成 **`TASK_COMPLETED` 事件**发上总线。

**于是今天的真实情况是：** `activity.py:171-183` 的注释说「thinking-only 的 end_turn 片段此前被误判 AWAITING → derive_events 会误报 TASK_COMPLETED，**修正为在飞**」——**他们只修了状态机那条路，没修事实那条路。** 一个中途的 thinking-only + `end_turn` 片段，今天仍然会经由 `_recent_events` 变成一条 `task_completed` 事实 → 越过 `last_fact_cursor` → **发出一条误报的 `TASK_COMPLETED` 事件 → 触发通知规则。**

**结论：**
- **修它是对的**（P6：误报零容忍）。**但必须声明这是一次事件总线的行为变更，不能藏在「零总线改动」后面。**
- **变更的方向：** 少发 981 类流式片段的**误报**完成；多发 6 条 `stop_reason == None` 的**真**完成。**两个方向都是向状态机收敛。**
- **不变的东西：** 🔴 **不新增事件类型、不新增 payload 字段、不改 `dedup_key` 格式**（P3：事件是唯一契约）。`last_reply_epoch` 等 5 个新键**不进任何事件的 payload**（`derive_events` 按名取键，对多出来的键天然无感）。
- **同时把 `activity.py:284` 那句陈旧的 docstring 改对。** 一份写错的注释，就是下一个人的第二份定义。
- **单测钉住**（T24/T25，§17）。

### 10.5 🔴 硬门：F7 必须先落地

`last_reply_epoch` 从**同一份 tail `objs`** 里来。
**末条记录巨大 → `objs` 空 → `last_reply_epoch = None` —— 恰好在「Claude 刚写完一大段」的会话上失效。那正是最需要它的时刻。**

> **F7 的尾读修复必须与 `last_reply_epoch` 在同一个 commit，或在它之前。否则新字段就是一个在关键时刻返回 `None` 的摆设。**

---

## 11. F7 · 尾读修复（P0-0，第一个做）

### 11.1 三个缺陷（`activity.py:236-247`）

```python
if st.st_size > _TAIL_BYTES:                 # 262144
    f.seek(st.st_size - _TAIL_BYTES)
    data = f.read()
    nl = data.find(b"\n")
    data = data[nl + 1:] if nl != -1 else data      # ← 缺陷 (a)
    if not data.strip():
        cap = min(st.st_size, _MAX_TAIL_BYTES)      # ← 缺陷 (b)  4_000_000
        f.seek(st.st_size - cap)
        ...
```

- **(a) `nl == -1` 时保留碎片。** 256KB 窗口整个落在一条巨型记录内部、且该记录没有尾随换行（正在追加写）→ 窗口里**一个 `\n` 都没有** → `data` = 一坨半截 JSON（**非空**）→ `if not data.strip()` **为假** → **大窗口兜底永远不触发** → 全行解析失败 → `objs == []` → **UNKNOWN**。
- **(b) `cap = min(size, 4_000_000)` 盲目按字节数 seek。** 语料实测**最大单条 11,935,057 B，是 4MB 上限的 3 倍** → 从 `size - 4MB` 处 seek **落在那条记录的正中间** → 同样 0 objs → **UNKNOWN**。
- **(c)（第三条，另发现）窗口里恰好只有巨型记录的尾巴 + 一条小元数据行**（如 `{"type":"mode"}`）→ `objs = [mode]` **非空** → 走正常路径 → `_last_message()`（`activity.py:267`）返回 `None` → **UNKNOWN「transcript 里没有对话消息」**。**又一条静默丢失路径。**

**为什么这是全系统最严重的一条：** `UNKNOWN ∈ _DEAD_STATES`（`event_sources/activity_source.py:29`）→ **该会话不产生任何事件 → 事件总线也一起哑掉。**

> **这是唯一一个能在 pump 正常运行的情况下，真正弄丢一次回复的机制。**

**诚实标注：** 现场目前 **0 个 UNKNOWN**；当前「末条记录 > 256KB」的文件 **0 个**。**F7 今天是潜伏的、瞬态的**（记录写完、下一条追加上去，窗口里就又有换行了，症状自愈），**尚无证据证明它在这台机器上真的发过。** 但它的触发条件——「Claude 正在写/你刚粘了一大段」——**恰恰是我们最不能瞎的时刻**。

### 11.2 修法：边界锚定 + 指数增窗（不按字节数猜）

```python
_TAIL_BYTES     = 262_144        # 快路径不变: 100+ 会话每轮只读 256KB
_MAX_TAIL_BYTES = 33_554_432     # 32 MiB 硬顶 (最大实测记录 11.4 MiB 的 2.8 倍)
_CLAMP_AFTER    = 65_536         # 单条记录超过它才进钳制 (§11.3)
_TEXT_KEEP      = 8_192          # text/thinking 保留的字符数
```

🔴 **这三个常量必须在调用时从模块全局读取**（不能烘进默认参数）——这样单测可以 monkeypatch 成 KB 级，用**比例模型**在毫秒内复现 12MB 的场景。

```
_read_tail(path) -> TailRead(objs, complete, reason)      # reason: None | 'oversize' | 'unreadable'

1. stat + (mtime,size) 缓存命中 → 直接返回                            [不变]
2. size <= _TAIL_BYTES → 整读、解析、complete=True → 返回              [不变]
3. window = _TAIL_BYTES
   loop:
     start = max(0, size - window); seek(start); data = read()
     if start > 0:
         nl = data.find(b"\n")
         if nl == -1:            → 整个窗口落在一条记录内部 → 必须增窗   [缺陷 (a)]
             升窗 or 返回 oversize
             continue
         data = data[nl+1:]                      # 丢掉首个残行
     objs = parse_lines(data)                    # 半截尾行被现有 except:continue 天然跳过
     if _has_conversational(objs) or start == 0: break                [缺陷 (c) 的闸门]
     升窗 or 返回 oversize
   # 升窗: window *= 4 (256K → 1M → 4M → 16M → 32M);
   #       window >= _MAX_TAIL_BYTES 时 → return TailRead([], complete=False, reason='oversize')
4. complete = (start == 0)
5. 每条记录: if len(raw) > _CLAMP_AFTER: _precompute_facts(o); _clamp(o)     # §11.3
6. 落缓存 (mtime, size, TailRead)                                     [键不变]
```

- `_has_conversational(objs) = any(o.get("type") in ("assistant","user") for o in objs)` → **直接封死缺陷 (c)**：只有元数据行时会继续增窗。
- **JSONL 保证换行 = 记录分隔符**（JSON 字符串里的换行被转义成 `\n`），所以按 `\n` 锚定边界是可靠的，不需要括号配平。
- 🔴 **判据是 `not _has_conversational(objs)`，不是 `not data.strip()`。** 后者就是缺陷 (a) 本身。

**`oversize`（超过 32MB 硬顶还是拿不到完整记录）：** 该行 `UNKNOWN`，但 **`state_label` 说得出为什么**：
```
读不出 · 末条记录超过 32MB, 尾部窗口无法定位完整记录
```
**而不是**泛泛的「无法判断 · 文件读不出/空」。**让潜伏的 bug 在它真的发生时自己举手** —— 这就是 NORTH_STAR §5「让格式漂移**可被发现**，而不是悄悄算错」。

**文件结尾没有 `\n`（正在追加写）：**
- 末条 JSON **已完整**（只是 `\n` 还没 flush）→ 照常解析成功 → 用它。
- 末条 JSON **不完整**（写了一半）→ `json.loads` 失败 → 被现有的 `except: continue`（`activity.py:261-262`）跳过 → **回落到它前面那条完整记录**。状态短暂「落后一条」——**这是正确的**：那条记录还没写完，它还不是事实（P6：拿不准就别报）。
- 🔴 **绝不能让这个残片把整帧变成 `UNKNOWN`** —— 那正是缺陷 (a) 今天的行为。

**🔴 `_read_tail` 有两个调用点，都用 `if not objs:` 做降级判断——必须都改：`activity.py:425`、`inference_doctor.py:85`。**

| 调用点 | 现状 | 改后 | 不改会怎样 |
|---|---|---|---|
| `activity.py:425-426`（`_session_row`） | `objs = _read_tail(path)` / `if not objs:` | `tr = _read_tail(path)` / `if not tr.objs:` | 🔴 **静默**：一个非空 `TailRead`（NamedTuple/dataclass）**恒为真** → `if not objs` 永远 False → **降级字典分支静默失效** → `_last_message(TailRead)` 遍历的是字段而不是记录 |
| `inference_doctor.py:85-87` | **一模一样的形状** | 同上 | **响亮**：`tokmon doctor` 当场炸 |

> **三份候选设计里两份完全没提 doctor 那处；一份还把它锁在 `git diff` 之外。而 `_session_row` 那处更危险——它不响亮，它静默。**
> 🔴 **且 `_read_tail` 不再返回 `None`**：OSError → `TailRead([], complete=False, reason='unreadable')`。
> 单测 **T-RT**：`_read_tail(不存在的路径)` → `TailRead([], False, 'unreadable')`，且 `_session_row` **仍走降级字典**。

### 🔴 内存契约 + 升窗节流（升窗不是免费的，也不是一次性的）

**① 内存契约。** `_clamp` 发生在 `json.loads` **之后** → 解析一条 11.9MB 记录的**瞬时峰值**（窗口 bytes + `data.split(b"\n")` 的整份拷贝 + decode 出的 str + 对象图）≈ **3–5× 当前窗口**。32MB 硬顶 ⇒ **单会话瞬时上限约 150MB**。

> 🔴 **§11.3 的钳制只换到「不长期驻留」，没换到「不 OOM」。** 说清楚：**峰值 ≈ 3–5× 当前窗口；32MB 硬顶 ⇒ 单会话瞬时上限约 150MB；逐文件串行、不并发；解析后立刻 `del data`。**

**② 升窗节流。** 更要命的一条：`_tail_cache` 的键是 `(mtime, size)`，而**升窗被触发的唯一场景就是「文件正在被追加写」**——mtime/size **每帧都在变** → 缓存每帧都 miss → pump 每 5s **重新 seek 16MB、重新 split、重新 parse 那条 12MB 记录**，直到它写完。

> 🔴 **§10.3 的「成本：零额外 I/O」只对新字段成立，对 F7 的升窗路径不成立。** 必须有闸门：

```python
_ESCALATE_COOLDOWN = 30.0          # 秒
_escalate_cache: dict[str, tuple[int, int, float]] = {}   # path -> (size, window, t)
```
- 同一 `path` 在 `_ESCALATE_COOLDOWN`（30s）内**不重复升窗**。
- 冷却期内**复用上一帧的 `TailRead`**，并**强制 `tail_complete = False`** → `last_reply_absent = 'out_of_window'` → **诚实**（不是「从未回复」）。
- 单测 **T19b**：同一文件连追加 3 帧 → **升窗只发生 1 次**。
- 观测：**「升窗字节数/分钟」进 §15 的 doctor lens。**

### 11.3 内存钳制（不修这个，F7 的修复就是拿「说谎」换「OOM」）

`_tail_cache`（`activity.py:46`）缓存的是**解析后的 `objs`**，且 pump 每 5s 都持有它。升级路径会把一条 **12MB** 的记录原样塞进缓存。

> **§2.5 渐进可降级 / P4「任何单点异常不拖垮全局」——一个会 OOM 的监控器，比一个 UNKNOWN 的监控器更糟。**

**🔴 钳制必须按 block 类型来（见 F13 的字节普查），不是按记录类型来：**

| block | 处理 | 有谁在读它？ |
|---|---|---|
| `image.source.data` / `document.source.data`（base64） | **整个丢弃**，留 `{"_clamped":"image"}` 标记 | **没有任何消费者读它。** 32MB 的字节全在这儿 |
| `text`（user 或 assistant） | `[:8192]` + `"_clamped": True` | `first_text`→`last_text[:140]`（`activity.py:190`）、`_lead_text`→`startswith` 判断、`_current_step[:240]`（`:336`）。**全部 ≪ 8192** |
| `thinking` | `[:8192]` + `"_clamped": True` | `_current_step[:240]` |
| `tool_result.content` | **先 precompute，再 `[:8192]`** ↓ | 见下 |

### 🔴 钳制铁律（这一条不遵守 = 亲手制造一次 P6 误报）

> **任何在被钳掉的区域里做过判断的消费者，必须在钳制发生之前把判断结果算完、并存到记录上。**

**今天恰好有两个，一个都不能漏：**

1. **`_bg_ack`** —— `_open_bg_tasks`（`activity.py:415-418`）：
   ```python
   s = (rc if isinstance(rc, str) else _lead_text(rc)).lower()
   if not any(mk in s for mk in _BG_ACK_MARKERS):   # 非"已在后台启动"回执 = 收口
       closed.add(b["tool_use_id"])
   ```
   **这是在整个正文里做 substring 搜索，不是前缀检查，而且语义是反的：「找不到标记 ⇒ 判定收口」。**
   截断正文 → 标记找不到 → **一个还在飞的后台 Workflow 被静默标记为已完成** → `background = False` → M4.5 Bug1 的覆盖消失 → **一个正在跑 Workflow 的会话渲染成「等你输入」。**
   ✅ **钳制前先算 `b["_bg_ack"] = any(mk in full.lower() for mk in _BG_ACK_MARKERS)`，`_open_bg_tasks` 优先读这个键，缺键时回退原逻辑。零行为变化。**

2. **`_task_close_id`** —— `_TASK_CLOSE_RE.search(lead)`（`activity.py:409-412`）在 `<task-notification>` 的**全文**里找 `<tool-use-id>`。若那段文本被钳到 8192 而 tag 在后面 → **收口信号丢失 → 后台任务永远「在飞」→ 会话永远显示「后台运行中」。**
   ✅ **钳制 `text` 块前，若它以 `<task-notification>` 开头，先跑一次正则并把结果存到 `o["_task_close_id"]`。**

> **未来任何人再往这两个函数里加一条「在全文里找 X」的逻辑，必须同时加一个 precompute。写在这里，别让下一个人踩。**

**其它约束：**
- 🔴 **钳制是「记录自身大小」的纯函数（`len(raw) > _CLAMP_AFTER`），与走了哪条读路径无关。** 否则同一条记录会在一帧里被钳、在另一帧里不被钳——**两条码路 = 实现者必错一条**。
- **放弃的东西，明写**：未来若有人想从 `_tail_cache` 里拿巨型文本/图片的全文——**拿不到，必须重读文件**。`_clamped` 标记让消费者知道这里被截过。**不假装无损。**

---

## 12. F8 · `current_step` 按时间取，不按种类取

**现状**（`activity.py:339-363`）：反向扫，分别收集 `think` 和 `narr`，然后 **`if think: return think`（`:359`）—— 从不比较两者的时间戳。**

**实测后果：7/160 行显示的是一段陈旧的 thinking；Claude 回复前后该行字节完全相同；100% 渲染 💭 的行显示的都是回复之前的内容。** 这是 M4.5 那次「thinking 约 29% 非空 → 优先显示真实思考」的**过度矫正**：把「优先」写成了「按种类无条件优先」。

**修法（更简单，代码更少）：**

> **反向遍历 `objs`（记录逆序 × 每条记录内 block 逆序）；遇到的第一个非空 `thinking` 或 `text` 块，就是它。不比较种类，只认最近。** 顺带产出 `step_epoch` = 该记录的 `timestamp`。

- 一条记录内 `content = [thinking, text]` → block 逆序先撞到 `text` → **text 赢**（它确实更晚产出）✅
- 末条记录是 thinking-only 的流式片段 → **thinking 赢** ✅ **不是矫正回去** —— 思考仍然会显示，只在它**真的是最新的**时候（而 `activity.py:171-183` 已经确立：thinking-only = 在飞）。于是 `💭` 的含义从「它想过什么」变成「**它正在想**」。
- **一个带时间戳的步骤，无法伪装成新鲜的**（§13）。

### 🔴 这会打红一个现有测试。必须**改写**，不许撤销。

```
tests/test_tokmon.py:616  test_m45_current_step_prefers_thinking
  单条消息 [thinking:"我在推理这一步", text:"我在叙述"]  →  今天断言返回 thinking
```
**新规则下它返回 `narration`。测试必然变红。**
**最省事、也最危险的做法是把 F8 改回去让它变绿。** → 该测试必须**改写**（改名 `test_current_step_latest_block_wins`，断言 `narration`，注释里写明 F8 与新规则），**下半段（thinking 为空 → 回退叙述）保持不变照样绿。**

**放弃的东西**：「思考比叙述更有信息量，所以优先」这个启发式。已完成回合的 thinking 不再展示。
**为什么值**：F8 证明这个启发式**在真实数据上 100% 的时候是在撒谎**。**原则 1 碾压一切「更有信息量」。**

---

## 13. 新鲜度：让「陈旧的一帧」在物理上无法冒充「新鲜的一帧」（F9/F10）

### 13.1 病根

年龄是**服务端算好的**：`serve.py:1361` 渲染 `fmtAgo(s.last_activity_age_s)`。
`last_activity_epoch` **在 `serve.py` 里一次都没被引用过**（grep 全文：**0 命中**）。
`fmtAgo` 还**向下取整**（`serve.py:1211`：`5m59s → "5m 前"`）—— **误差永远朝「看起来更新鲜」偏。**
于是一个 20 分钟前的帧，会理直气壮地写着「**5s 前**」。而 grep `visibilitychange|document.hidden|focus` → **0 命中**，只有 `setInterval(tickAll, 30000)`（`serve.py:1381`）。**切走 5 分钟再切回，你看到的是 5 分钟前的世界，且它自称「5s 前」。**

### 13.2 修法（四件事，缺一不可）

**(1) 年龄一律客户端从 epoch 现算，带时钟偏差校正。**
```js
let SKEW = 0;
SKEW = d.generated_at_epoch * 1000 - Date.now();      // 每收到一帧就校
const nowS = () => (Date.now() + SKEW) / 1000;
const ageOf = s => s.last_activity_epoch == null ? null : nowS() - s.last_activity_epoch;
```
**UI 从此不读 `last_activity_age_s`。** `applyFilters` 的时间窗轴**也改用 `ageOf`** —— 否则过滤器和显示会说两套话。
（`last_activity_age_s` 在 payload 里**保留不动**：`derive_events` 在用，`activity_source.py:60`。）

**(2) 年龄自己会走。** `setInterval(tickAges, 5000)` —— **不发请求**，只 patch `[data-epoch]` 的 `textContent`（§6.4 的性能契约）。**一帧放久了，页面会自己变老给你看。**

**(3) 帧龄横幅。** 页顶常驻 `本帧采样于 15:42:07（12s 前）`。
- `frameAge > 90s`（= 3× 刷新周期）→ **整条横幅变琥珀** `⚠ 数据已陈旧：这一帧是 612 秒前采的，正在重新拉取…`，**表格整体降到 `opacity:.55`**，并**立即触发重取**。
- `|Date.now()/1000 − d.generated_at_epoch| > 120` → 追加 `⚠ 客户端与服务端时钟相差 Ns，时间显示可能不准`。
- **拉取失败不清表、但必须招供**：今天 fetch 失败只往 `#foot` 写一行字（`serve.py:1292`），**表格原样留着静悄悄地骗你**。V3：保留上一帧 + 陈旧横幅 + 写明「上次刷新失败（TypeError: Failed to fetch），下面是 612s 前的数据」。

**(4) 回到页面就重取。** `visibilitychange`（`visibilityState === 'visible'`）+ `window.focus` + `online` → `tickAll()`，**去抖 3s**。

### 🔴 `#auto`（自动刷新）关掉时，这四件事分别怎么办 —— 明写，不许实现者随手二选一

现有 `<input id="auto" checked>` + `setAuto()`（`serve.py:1379-1380`）。V3 新增的 focus/visibility/online 重取与 5s 年龄 tick **必须声明是否受 `#auto` 管**——「我明确关了自动刷新，你还偷偷重取」**又是一个隐形行为**；反过来「关了自动刷新，年龄就冻在那儿骗我」也是。

| | `#auto` = **ON** | `#auto` = **OFF** |
|---|---|---|
| 30s 轮询 | ✅ | ❌ |
| **focus / visibilitychange / online 重取** | ✅ **去抖 3s** | 🔴 **不做**（你明确关了它，就是关了） |
| **5s 年龄 tick** | ✅ | 🔴 **照常**（**手动模式下更需要知道这帧多老**） |
| **陈旧横幅** | ✅（并**立即触发重取**） | 🔴 **照常**，但**不自动重取**；文案改为：<br>`自动刷新已关闭 · 这一帧是 612s 前采的 [立即刷新]` |

> **关掉自动刷新 = 「别替我发请求」，不等于「骗我这帧是新的」。** 年龄与陈旧横幅是**诚实性**设施，不是刷新设施——它们不归 `#auto` 管。

### 🔴 (2) 和 (3) 必须在同一个 commit

> **在一份 40 分钟前的陈旧快照上，让年龄每秒优雅地滴答 —— 你造出的是一个「活着」的假象，它比 F10 更能骗人。**
> **年龄为真**（epoch 绝对，休眠唤醒自动正确）；**状态可疑**（陈旧横幅 + 表格降透明度 + 徽章加 `?`）。
> **只做前者、把横幅留到下个 commit = 把 F10 武器化。**

`fmtAgo` 的向下取整**保留**——但代价被抵消：年龄从真实时钟现算、会自己走、且 `title` 属性里永远挂着**绝对时刻**。**精确的真相永远只在一次 hover 之外。**

---

## 14. localStorage schema + 从 V1 迁移

### 14.1 新 schema（新键，不复用）

```js
const SKEY = 'mc_sess_v3';
{
  v: 3,
  filters: { projects: [], states: ['live','amb','wait','idle','unk'], ageMax: null },  // ⚠️ text 不持久化
  sort:    { key: 'reply', dir: 'desc' },
  group:   false,
  view:    'table',
  columns: { model:false, branch:false },        // P2 预留
}
```

**`text` 刻意不持久化。** 持久化一个文本搜索是陷阱：你明天打开只看到 3 行，却完全想不起昨天搜过什么。**收益最小、「忘了自己在筛」的风险最大。**（它现在**照样进 fbar** —— **不持久 ≠ 隐形**。）

### 14.2 迁移（一次性读 `mc_sess_filters`，`serve.py:1252`）

🔴 **必须处理一次语义漂移：V1 的 `'wait'` 表示全部 `AWAITING_USER`（71 个）；V3 的 `'wait'` 只表示未超 10min 的那一半（1 个）。**

> **同一个字符串，意思变了。一个存着 `states:['wait']` 的旧 blob，能通过任何白名单校验，然后悄悄地从 71 行缩成 1 行。**
> **这是最阴的一种迁移 bug：它不报错，它只是换了个意思。**

| V1 值 | V3 结果 |
|---|---|
| `states` 含 `wait` | → **同时产生 `wait` 和 `idle`**（精确保留 V1 的原意） |
| `states` 含 `live`/`amb`/`closed`/`unk` | → 1:1 |
| `states` 里的未知名字 | → 丢弃（保留 V1 在 `serve.py:1258` 的白名单保护） |
| `states == []`（**从没选过状态**） | → **应用 `PRESET_UNFINISHED`**（新默认生效） |
| `states != []`（**明确选过**） | → **照搬，不追加 preset**（🔴 **绝不在一次迁移里，悄悄把一个明确选了「看全部」的人的视图收窄 81 行**） |
| `projects` / `ageMax` | → 照搬 |
| **`hideIdle`** | → **丢弃。它被删除了。** |

迁移后**删除 `mc_sess_filters`**（不留两份真相源），并弹一次**可关闭**的横幅：

> **过滤器已升级到 V3。**「隐藏空闲会话」已被**删除** —— 它会把「Claude 刚回复完你」的会话藏起来（这就是你觉得「有时候 claude 回了我你也没记录」的原因）。「已关闭」会话现在由一个**看得见、点得动、能一键清除**的过滤器控制。**因此你现在看到的行数变多了（79 行，之前 90 行里有 81 行是 9 天前的尸体）——这是对的。**

**加载校验：逐字段降级，不要因为一个字段坏了就丢掉整个对象。** `sort.key` 必须在 `SORTS` 里，否则回 `'reply'`；`sort.dir ∈ {asc,desc}`；`view ∈ {table,card}`；`group` 强制转布尔；`ageMax` 必须是 `AGE_LABEL` 里的合法窗（V1 已有此保护，`serve.py:1259`，**保留**）。全程 `try/catch`，坏 JSON → 全默认。

---

## 15. 自检不变量（跑在浏览器里的运行时断言）

🔴 **全部渲染成页面上的可见横幅，绝不只 `console.warn`** —— 一条没人看的 console 警告，就是一次静默失败。

| # | 不变量 | 违反时 |
|---|---|---|
| **S1** | **划分完备**：每个 session 恰好属于 6 组之一（`groupOf` 不返回 `null`）；`Σ 六组 == sessions.length` —— **未覆盖数应恒为 0，一旦 > 0 即为违反** | 🔴 红色 pill `⚠ 未覆盖 N（后端出现了前端不认识的 state）`，且**这些行照常显示，绝不吞**。<br>**结构保证（不靠自觉）**：§8.1 `states` 轴的 `pass` 是 `g === null \|\| F.states.includes(g)` —— **未覆盖的行不受状态轴管辖，默认的 `PRESET_UNFINISHED` 吞不掉它**；只有点那枚红 pill 才筛它。<br>（S4 的 Σ 式里所以要**显式加上「未覆盖数」**：**违反 S1 时分面计数仍必须守约**，因为那些行还在表里。） |
| **S2** | **前后端一致**：`live==counts.working` / `wait+idle==counts.awaiting` / `idle==counts.idle` / `amb==counts.ambiguous` / `closed==counts.closed` / `unk==counts.unknown` | 🔴 红条。这条会在**未来有人只改了 `activity.py` 而没改前端**的那一天当场炸出来 |
| **S3** | **无隐形过滤器**：`Object.keys(FILTERS)` === `AXES` 的 id 集合，且每个轴都有 `label` | **由构造保证**（§8.1）+ 断言防漂移 |
| **S4** | **分面守约（FC1）· 🔴 只对划分型轴成立**（`facetable: true`，即 `states` / `projects`：每行恰好落一个取值）：`Σ 各取值的分面计数 + 未覆盖数 == \|applyFilters(rows, F\axis)\|`。**`ageMax`（嵌套窗：近1h ⊂ 近24h ⊂ 近7d，Σ 三窗远大于总数）与 `text`（无取值集）不适用 Σ 恒等式**，S4 **只遍历 `facetable` 的轴** | 🔴 红条「计数与结果不符，这是一个 bug」。**这是「点 N 出 N」的机器可检形式——它专门抓 F6 的成因**。⚠️ **逐字实现成「对每个轴」＝ 在一个正确的实现上永久挂红条**：那是用一次 P6 误报去防 F6 |
| **S4b** | **点击契约（覆盖 `facetable:false` 的轴）**：点一个显示 N 的控件 → 出 N 行 | dev 模式**抽样断言**（不做 Σ 恒等式）。`ageMax` / `text` 的守约由这条兜 |
| **S5** | **未知不撒谎**：`last_reply_epoch == null` 的行渲染 `—`（**永不渲染数字**），且**两个排序方向都沉底** | 比较器里 `if (v == null) return 1`（两向）；页脚「另有 N 个会话读不到回复时间（其中 M 个是尾窗未覆盖，不是从未回复）」 |
| **S6** | **帧不装嫩**：`frameAge > 90s` → 琥珀条 + 表格降透明 + 自动重取 | §13 |
| **S7** | **全序**：`cmp` 对任意两行不返回 0 | dev 模式抽样断言。保证两帧顺序一致（§7） |
| **S8**（**描述性，只陈述不判断**） | 页脚常驻：**「N 个会话的最后一条是你说的话（Claude 尚未回复）」**（现场 N=16） | **P7：我们观测，不自作主张** |

### 一条我**故意不做**的断言（P6）

本想加：「若 `groupOf ∈ {wait,idle}` 但 `last_user_epoch > last_reply_epoch` → 状态机与 transcript 矛盾 → ⚠」。

**否决。** `AWAITING_USER` 可以合法地由 `[Request interrupted by user]`（一条 **user** 记录）产生（`activity.py:200-205`）—— 那种情况下 `last_user > last_reply` **是设计如此**，这条断言会在**每一个被打断的会话上误报**。

> **P6 · 误报零容忍倾向。「拿不准就降级严重度或不发；一次 Critical 误报对信任的伤害 > 十次正确 Info。」（MISSION_CONTROL.md:181）**

**所以它降级成 S8 的纯描述**（谁最后说话 → §6.2 的加粗）。

### `inference_doctor` 新 lens（P1-3，`tokmon/inference_doctor.py`）

MISSION_CONTROL.md 的护栏原话是「**让格式漂移可被发现**」。**F7 今天是潜伏的——潜伏的 bug 必须是可观测的，否则我们只是在等它。** 新增 6 个计数：

- `last_reply_epoch` 非 `None` 的会话占比（**掉下来 = 判据漂移了**）
- `tail_complete == False` 的会话数
- **尾读升级触发次数**（F7 从潜伏变成现实的那一刻，这个数会从 0 跳起来）
- 🔴 **升窗字节数 / 分钟**（§11.2 的升窗节流的观测口：**这个数飙起来 = 节流没生效，pump 正在每 5s 重 parse 一条 12MB 记录**）
- **`oversize` 被拒读的记录数**
- **被 `_clamp` 钳过的记录数**（F13 的观测口）

---

## 16. 改动范围

| 文件 | 改动 | 依据 |
|---|---|---|
| `tokmon/activity.py` | ① `_read_tail` → 边界锚定 + 指数增窗 + `TailRead` + `_precompute_facts` + `_clamp`（F7/F13）② 新纯函数 `_is_synthetic`/`_turn_phase`/`_is_turn_end`/`_is_reply`/`_is_human_turn`/`_turn_epochs`③ `_classify_transcript` 四个分支改由 `_turn_phase` 守卫（**行为不变**）④ `_recent_events:297` 改用 `_is_reply` + **改对陈旧 docstring**（`:284`）⑤ `_current_step` 改为「最近的块赢」+ `step_epoch`（F8）⑥ `_session_row` **与两个降级字典**各加 5 键⑦ `_open_bg_tasks` 读 `_bg_ack`/`_task_close_id` | D5 / F7 / F8 / F13 |
| `tokmon/serve.py` 的 `SESS_PAGE`（`1127-1397`） | 整个 sessions UI：`AXES` 登记表、6 组分面、表格 + 排序 + 展开、卡片切换、新鲜度、迁移、不变量横幅。**删除 `hideidle`**。<br>🔴 **`#timeline` / `loadEvents` / `renderTimeline` / `EVT_LABEL` / `evDetail` / `evCursor` / `evDropped`（M2 事件流，`serve.py:1204-1206` + `:1212-1250`）原样保留，不在本切片范围内；`tickAll()` 仍同时驱动 `load()` 与 `loadEvents()`。**「重写 `SESS_PAGE`」的实现者极可能连带删掉它们——**那正是「悄悄少了一块」的形状** | D1/D2/D4 · F1–F6 · F9–F12 |
| `tokmon/inference_doctor.py` | 🔴 **`:85` 的 `_read_tail` 调用点必须跟着改**（**不改就直接炸**；另一个调用点是 `activity.py:425`，见 §11.2）+ 新 lens | F7 / A7 |
| `tests/test_tokmon.py` | **+24 例**；**改写 `:616`**；**现有 21 个 `classify_state` 单测一字不改、必须全绿** | §17 |
| **`tokmon/inference_backtest.py`** | **零改动**，但 🔴 **必须作为回归 gate 跑**（`tokmon backtest`，`cli.py:49`/`:78`）：改动前后**三把尺的数字逐位相同** = `_turn_phase` 重构的**全语料**行为不变证明 | §10.2 / **A16** |
| **`parser` / `pricing` / `aggregate` / `discovery` / `records` / `project`** | **零改动** ✅ | **原则 4 / I4：token 内核纹丝不动** |
| **`events.py` / `notify.py` / `control.py` / `procmon.py`** | **零改动** ✅ | **P3/P4** |
| **`event_sources/activity_source.py`** | **零代码改动**，但 ⚠️ **它的输出会变**（`TASK_COMPLETED` 少发误报、多发 6 条真完成，§10.4）——**必须单测钉住，且写进 EVOLUTION.md** | P6 |
| `/api/sessions` 端点签名 | **零改动**（仍是无参 GET，仍返回全量快照；每行多 5 个键，顺序未定义） | |
| **文档（`EVOLUTION.md` / `CHANGELOG.md` / 本文件）** | 🔴 **必须改。** 事件总线的行为变更（§10.4）+ F7/F8/F13 写进 `EVOLUTION.md`；`CHANGELOG.md` 记一条 | **逐字执行「`git diff` 只碰 4 个文件」⇒ 事件总线的行为变更不会被记录——正是本文自己骂的「静默」。见 A14** |

---

## 17. 验证

### 17.1 Python 单测（`tests/test_tokmon.py`）—— **这是 V3 与 V1/V2 的实质区别**

V1（`SESSIONS_FILTER_PLAN.md:133-144`）和 V2（§4）都诚实记录了「本片无单测」，**并且没有伪造覆盖率——那是对的，因为它们是纯前端。**

> 🔴 **但 V3 的 `activity.py` 改动是纯 Python、纯函数、且仓库里已经有 928 行、86 例的测试设施在等着。**
> **所以 V3 没有借口。`activity.py` 的每一条改动都必须有测试。JS 的缺口不能给 Python 的缺口当挡箭牌。**

沿用现有 house style（`_iso` / `_amsg` / `_umsg`，`tests/test_tokmon.py:218-228`；无依赖自带 runner + `tempfile`）。

**🔴 红灯优先（red-first）—— 集合是：`T10 / T14 / T15 / T16 / T18 / T20 / T21 / T23 / T24 / T25`。**

> **这些必须在当前 `activity.py` 上先跑出红，才算证明 bug 真实存在**，而不是我们脑补的。F7 现场 0 个 UNKNOWN——它是潜伏的，**只有先让测试红，才能证明它是真的。**
>
> 🔴 **`T1–T8` 不算 red-first。** 它们测的是 `_is_turn_end` / `_turn_phase` —— **今天不存在的函数**：它们「红」是因为 `ImportError`，**不是因为复现了 bug**。那是新纯函数的**规格测试**，不是回归证明。**别拿 ImportError 冒充红灯。**
> 🔴 **`T19`（缓存用例）也不是 bug 复现**，不进 red-first 集合。

（对照：真正能对**现有代码**先红的，就是上面那 10 条 —— T14/T15/T16/T18（`_read_tail` 三缺陷 + 无尾随换行）、T20（`_open_bg_tasks` 钳制误报）、T21/T23（`_current_step` 按种类取）、T10（`_session_row` 的 F11）、T24（`_recent_events`）、T25（`derive_events` 误发 `TASK_COMPLETED`）。）

**A · 回合谓词（纯函数）**

| # | 用例 | 断言 |
|---|---|---|
| T1 | `stop_reason == "tool_use"` | `_is_turn_end` False |
| T2 | 末块是 `tool_use`、`stop_reason` 缺失 | False（钉住「末块优先」这一子句） |
| T3 | **thinking-only + `end_turn`** | **False** ← 钉住语料里那 **981** 条，防止有人「顺手」改回朴素判据 |
| T4 | `stop_reason == "max_tokens"` | False |
| T5 | `end_turn` / `stop_sequence` / **`None`** + text | True（三例） |
| **T7** | **synthetic**（`model=="<synthetic>"`；及 `"No response requested."`） | 🔴 **`_is_turn_end` True 而 `_is_reply` False**；且 `classify_state` 仍判 **`AWAITING_USER`** ← **钉死 §10.2 那个必炸的雷** |
| T8 | `isSidechain == True` | `_is_reply` False（防御性；今天语料 0 条） |
| **T-EQ** | **等价性属性测试**：对 ~12 种 assistant 记录 × 2 个 age，断言 `_is_turn_end(o) == (classify_state(o, now, cfg)['state'] == 'AWAITING_USER')` | **让状态机与谓词永远不能各自漂移** |
| **T26** | **现有 21 个调用 `classify_state` 的单测** | 🔴 **必须全绿，一个都不许改** ← `_turn_phase` 重构「行为不变」的第一道证明（fixture 级） |
| **T27** | **`tokmon backtest` 全语料回放**（`tokmon/inference_backtest.py`，**零改动，但必须作为回归 gate 跑**） | 🔴 **改动前后三把尺的数字逐位相同** ← `_turn_phase` 重构「行为不变」的**全语料**证明（161 个真 transcript，强于 21 条 fixture）。注：**只跑 `classify_state`，不覆盖 `_recent_events` 的改动**（那部分归 T24/T25） |

**B · 两个 epoch**

| # | 用例 | 断言 |
|---|---|---|
| T9 | 打断（`[Request interrupted by user]`） | `_is_reply` False；`last_reply_epoch` **停在前一次真回复**；`last_user_epoch` == 打断那条 |
| **T10** | **`objs = [assistant 回复@T1, user 提问@T2]`** | 🔴 **`last_reply_epoch == T1`、`last_user_epoch == T2`、`last_activity_epoch == T2`** ← **F11 的回归测试：证明「用户一说话它就前移」这个 bug 死了** |
| T11 | 尾窗里全是 tool_use，且 `tail_complete=True` | `last_reply_epoch is None` 且 `last_reply_absent == 'never'`（**不是 0，不是 `last_activity`**） |
| **T12** | 尾窗里没有 reply 但 `tail_complete=False` | 🔴 **`last_reply_absent == 'out_of_window'`，不是 `'never'`** ← §10.3 的成败所在 |
| T13 | 最新那条 reply 的 ts 是 naive | **`last_reply_absent == 'bad_timestamp'`，`last_reply_epoch is None`，绝不回退到更早的 reply** |
| T-U1 | `tool_result` / `<task-notification>` | 不算「我说话」 |

**C · 尾读（真文件，`tempfile`；常量 monkeypatch 成 KB 级）**

> 🔴 **所有尾读 / 巨型记录 / 钳制用例一律在 `tempfile.TemporaryDirectory()` 里造 `<tmp>/projects/<slug>/<uuid>.jsonl`，用 `activity.snapshot(base=tmp)` / `_read_tail(tmp_path)` 驱动。绝不往 `~/.claude/projects` 写任何测试文件**（**I1：只读，唯一可写的是 `~/.tokmon/`**）。
> `snapshot(base=…)` / `discover(base)` **都吃 `base` 参数**——**没有任何理由往真语料里写文件。**

| # | 用例 | 断言 |
|---|---|---|
| **T-RT** | `_read_tail(不存在的路径)` | 🔴 **返回 `TailRead([], False, 'unreadable')`，不返回 `None`**；且 `_session_row` **仍走降级字典**（钉住 §11.2 的 `if not tr.objs:` 真值陷阱） |
| **T14** | **末条记录 400KB（> `_TAIL_BYTES`）、无尾随换行** | **旧代码 0 objs → UNKNOWN（红）**；新代码解析成功、`state == AWAITING_USER`、`last_reply_epoch` 正确 ← **缺陷 (a)** |
| **T15** | **单条 > 旧的 4MB 顶** | **旧代码 `[]`（红）**；新代码读到 ← **缺陷 (b)** |
| **T16** | **窗口里只有 `{"type":"mode"}`** | 必须**继续增窗**直到拿到 assistant/user ← **缺陷 (c)** |
| T17 | 超过 `_MAX_TAIL_BYTES` 硬顶 | `reason == 'oversize'`；行 `state_label` 含「**末条记录超过 32MB**」，**不是**泛泛的「读不出」 |
| T18 | 结尾无 `\n`、末条 JSON 完整 / 截断 | 完整 → 解析；截断 → **回落到前一条完整记录，不是 UNKNOWN** |
| T19 | `(mtime,size)` 变化 | 缓存失效、新记录可见（防升级路径污染缓存）。**注：这是缓存用例，不是 bug 复现 → 不在 red-first 集合里** |
| **T19b** | **同一文件连追加 3 帧**（每帧 mtime/size 都变，即升窗的真实场景） | 🔴 **升窗只发生 1 次**（`_ESCALATE_COOLDOWN` = 30s 生效）；冷却期内复用上一帧 `TailRead` 且 **`tail_complete == False`** → `last_reply_absent == 'out_of_window'` ← **§11.2 的升窗节流。没有它，pump 每 5s 重 parse 一次 12MB 记录** |
| **T20** | **`_clamp` + `_precompute_facts`** | 🔴 (a) 1MB `text` / `image.data` 被钳；(b) `_current_step` 前 240 字**与未钳时逐字符相同**；(c) **一个 `launched in background` 标记在第 50,000 字符处的 `tool_result` 被钳后，`_open_bg_tasks` 仍判它「在飞」** ← **F13 / 钳制铁律。旧的天真钳制会在这里制造一次 P6 误报** |
| **T-slow** | **真的造一个 11,935,057 B 的单条记录** | 解析成功、`last_reply_epoch` 正确、**耗时 < 5s**。`TOKMON_SLOW=1` 守门。**这一条是唯一按真实体量验证硬顶的用例，不许用 mock 糊弄** |

**D · 当前步骤 / 一致性**

| # | 用例 | 断言 |
|---|---|---|
| **T21** | `[assistant(thinking)@T1, assistant(text)@T2]` | **返回 text** ← **F8 回归测试**（旧代码返回 thinking） |
| T22 | `[assistant(text)@T1, assistant(thinking-only)@T2]` | 返回 **thinking** ← 防止**过度矫正**（最新是思考时，思考照样显示） |
| T23 | 单条记录内 `content=[thinking, text]` | 返回 **text**；`step_epoch == 该记录的 ts` ← **`tests/test_tokmon.py:616` 就地改写成这一条** |
| **T24** | `_recent_events`：thinking-only + `end_turn` | **不**产生 `task_completed` ← 修掉 `✓ 完成` chip 乱闪 |
| **T25** | `derive_events` 喂入含 T24 那条尾巴的行 | 🔴 **不发出 `TASK_COMPLETED` 事件**（旧代码会发 → **先红**）；且**不新增任何事件类型 / payload 字段**（P3） |

### 17.2 前端：**诚实记录 —— 仍然没有 JS 单测**

**仓库没有 JS 测试设施**（V1 §5、V2 §4 都如实记过，没有伪造覆盖率）。**V3 也不假装有。**

**补偿（比 V1/V2 更硬，且可核）：**
1. `groupOf` / `applyFilters` / `facetCounts` / `cmp` / `migrate` **全部是纯函数**，不碰 DOM、不改入参，集中在一个代码块里。
2. **§15 的 S1–S7 是跑在你真实 160 会话语料上、每 30 秒执行一次的断言。** 对这一类 bug（计数不守约、划分不完备、隐形过滤器）来说，**它比单测更强**——单测跑的是我编的 fixture，S1–S4 跑的是你的真实数据。**尤其 S4，它就是不让 F6 复活的保险丝。**
3. 🔴 **写下一条棘轮，别再无限期推迟：**
   > **下一次再有人要改这一页的过滤 / 排序 / 分面逻辑 —— 先引入 JS 测试设施，再动手。**
   V1 说「未来再决定」，V2 也说「未来再决定」。**到此为止。条件写死，不再由心情决定。**

### 17.3 真机冒烟（必须真做，不许口头通过）

- 打开 → **数 71 个「等你回话 / 等你回话·已久」全在**
- **等满 30 秒**，确认：项目 chips 不丢选中（V1 §3.1）、**展开的行不自己合上**（§6.3，V3 的新坑）、排序不跳、滚动位置不丢
- 点 `已关闭 (81)` → **数出 81 行**
- 合盖 10 分钟 → 开盖 → 看陈旧横幅 → 看它自动重拉

---

## 18. 实现者会搞错的 8 件事（按「造成的谎言有多安静」排序）

**前 6 条的共同点：测试全绿、review 全过、用户看不出来。**

1. **隐形过滤器重生** —— 留着 `hideIdle`，或在 `render()` 里读一个 checkbox（正是 `serve.py:1297` 今天的形状）。
2. **计数还在全量上算** —— 不筛时一切正常，**只有同时筛两个轴才露馅** ⇒ 能轻松通过随手冒烟。（S4 是它的保险丝。）
3. **`_is_turn_end` 写成两份**（一份在状态机里、一份给新字段）⇒ 下一个新 `stop_reason` 到来时，徽章说「等你回话」、`last_reply_epoch` 说「从没回复过」，**两个都振振有词**。
4. **看到 `tests/test_tokmon.py:616` 变红，就把 F8 改回去。** 那个测试**必须改写，不是删除、更不是撤销 F8**。
5. **`(x.last_reply_epoch || 0)`** —— `|| 0` 让「未知」冒充 1970 年：升序时它浮到第一行，**伪装成「最该看的那个」**。
6. **年龄滴答了，但陈数据横幅留到了下个 commit** —— 合盖 40 分钟，唤醒，年龄优雅地跳动，徽章写着「运行中」。**这是 F10 的武器化版本。**
7. **`_read_tail` 的两个调用点只改了一个。** `inference_doctor.py:85` 没改 → `tokmon doctor` 当场炸（这条**响亮**，但它会让人在赶工时干脆回退整个 F7）。**更危险的是另一处：`activity.py:425` 的 `if not objs:`** —— 一个非空 `TailRead` **恒为真** → 降级分支**静默失效**，`_last_message()` 去遍历字段而不是记录。**它不响亮，它静默。**（§11.2）
8. **内存钳制钳错了地方** —— 语料里最大的那条 **11.9MB 记录是 `type:"user"` 的 `document` 块，不是 thinking**（F13）；而钳 `tool_result` 又会**静默杀死 `_open_bg_tasks` 的后台标记**（§11.3 的钳制铁律）。

---

## 19. 风险 · 我们放弃了什么（诚实清单）

| 放弃 / 风险 | 换到了什么 · 缓解 |
|---|---|
| **全局排序 = 排序不再暗示优先级。** 一旦你点了 `[显示已关闭]`，一个 2 分钟前刚被关闭的会话会排在一个 3 小时前回复的**活跃**会话上面 | 这是 D4 的真实代价，用户已知情。缓解：**状态列的彩色徽章**始终在第 1 列 + 「按状态分组」开关随时可开。**用排序实现的优先级是隐形的——所以我们把优先级交还给过滤器和徽章。** |
| **「已关闭」默认藏 81 行 —— 这是本设计里最接近 `hideIdle` 的一处。** 若 procmon 的活性判定误把一个活会话认成 CLOSED，它会被默认藏起来 | **计算过的风险。** 缓解：四项属性（可见/可点/持久/可清）+ fbar 常驻写明「已排除 81 个已关闭」+ 分面计数诚实（点它出 81 行）。**并且 `CLOSED` 的依据是「进程已退出」，是三种判定里唯一不靠 transcript 推断的一种——它是最硬的那个。** |
| 放弃「思考优先于叙述」的启发式（F8）：**已完成回合的 thinking 不再展示** | 它在真实数据上 **100% 的时候在撒谎**。原则 1 碾压「更有信息量」。 |
| 放弃「只看后台在跑」的过滤 | 不往互斥的状态轴里塞一个非互斥标记（= V2 诊断的**根因 1**）。3 行会话，不值。 |
| 放弃按模型排序（`model` 下放展开区） | 换 1400px 里能扫得动的 6 列。它**仍在文本搜索范围内**。 |
| 放弃「球权」独立列 | 加粗较新的那一格已经把它画出来了，**零列宽**。 |
| 放弃分页 / 截断 / 「加载更多」 | **不在用户和答案之间加任何一次点击。** 真慢了就虚拟化（阈值 150ms 已写死）。 |
| 放弃 `_tail_cache` 里巨型文本/图片的全文（钳制后拿不到，要全文必须重读文件） | 换**不长期驻留**。🔴 **不是「换不 OOM」——说准确点**：`_clamp` 在 `json.loads` **之后**，瞬时峰值仍 ≈ 3–5× 当前窗口（32MB 硬顶 ⇒ 单会话瞬时上限约 150MB，见 §11.2 的内存契约）。**留给「卡住检测器」的明确警告（§0.3）。** |
| **升窗不是免费的**：追加写时 `(mtime,size)` 缓存每帧 miss ⇒ 若无闸门，pump 每 5s 重 parse 一条 12MB 记录 | **换 `_ESCALATE_COOLDOWN`（30s）节流**（§11.2）；冷却期内诚实降级为 `tail_complete=False` / `out_of_window`，**不假装读到了**。观测口：doctor lens 的「升窗字节数/分钟」。 |
| 放弃文本搜索的持久化 | 换「不会明天打开只看到 3 行却想不起为什么」。**它照样进 fbar。** |
| **事件总线的输出会变**（`TASK_COMPLETED` 少发 981 类误报、多发 6 条真完成） | **这是一次 P6 修复，不是回归。** 但它是本切片唯一改变既有事件输出的地方——**必须单测钉住（T25）、写进 EVOLUTION.md，不许静默。** |
| 本切片不再「风险面极小、天然安全」（V1 的原话） | **换「不再撒谎」。** 代价是它**必须**有 python 单测——**而 V3 里风险最高的那部分，恰好是唯一被单测覆盖的那部分。** |
| **`MC_REMOTE` 仍未建**（D3） | 隧道关了，洞暂时不在。**再开一次隧道，洞原样回来。**（§0.2） |

---

## 20. 完成判据（可证伪、肉眼可查）

| # | 判据 |
|---|---|
| **A1** | **默认打开 `/sessions`，71 个「等你回话 / 等你回话·已久」的会话全部可见**（今天：1 个）。检验：状态列里这两组的行数 == 两个 pill 数字之和。 |
| **A2** | 默认 = **表格视图、按「Claude 回复」降序、不分组**。第一行就是**最近真正回复过你**的会话。fbar 明写：`过滤中：只看未结束（已排除 81 个已关闭） · 显示 79/160 · [+ 显示已关闭] [清除全部过滤]`。 |
| **A3** | 🎯 **点任意一枚显示 N 的 pill / chip（该轴未选其它项时），出来的行数恰好是 N。** 逐个点 6 枚状态 pill 验证，**对 `已关闭 (81)` 也必须成立**（这是 D2 的检验）。 |
| **A4** | 🎯 **让一个会话里的 Claude 刚回复完 → 该行立刻出现在最上面；10 分钟后它仍在**（只是组从「等你回话」变成「等你回话 · 已久」）。**它永不消失。** ← **直接验用户的原始抱怨。** |
| **A5** | 🎯 **在一个已回复的会话里发一条 prompt、不等回复** → 「我说话」更新并加粗，**「Claude 回复」纹丝不动，排序位置不上升。** ← **F11。** |
| **A6** | **合盖 10 分钟再打开** → 页面 (a) **立刻重拉**，(b) 数据回来之前显示「已陈旧 600s」琥珀条 + 表格降透明，**绝不显示「5s 前」**。 |
| **A7** | 六组计数之和 + 未覆盖 == 总数（160），且与服务端 `counts` 一致；否则页顶红条。（**可人为在 `activity.py` 里加一个状态，验它真的会炸。**） |
| **A8** | `last_reply_epoch` 为 null 的行显示 `—`，**升序降序都沉底**；hover 出五个原因码之一的人话；页脚写明「另有 N 个会话读不到回复时间（其中 M 个是**尾窗未覆盖**，不是从未回复）」。 |
| **A9** | 构造一个「末条记录 12MB」的 transcript → 该会话**不是**「读不出」，状态与两个时间都正确。（跑 T-slow）<br>🔴 **它必须造在 `tempfile.TemporaryDirectory()` 里的 `<tmp>/projects/<slug>/<uuid>.jsonl`，用 `activity.snapshot(base=tmp)` 驱动。绝不往 `~/.claude/projects` 写任何测试文件（I1：只读，唯一可写的是 `~/.tokmon/`）。** |
| **A10** | **不存在任何不在 fbar 里的过滤器**（含文本搜索）。**`git grep hideidle` → 0 命中。** |
| **A11** | `python tests/test_tokmon.py` **全绿**，新增 **≥24 例**；**现有 21 个 `classify_state` 用例一字未改**；`tests/test_tokmon.py:616` **被改写（不是删除）**；🔴 **red-first 集合 `T10 / T14 / T15 / T16 / T18 / T20 / T21 / T23 / T24 / T25` 在改代码前必须是红的**（§17.1）。**`T1–T8` 不算数——它们只会 `ImportError`，那不是复现 bug。** |
| **A12** | `tokmon doctor` 能跑（**`_read_tail` 的两个调用点 `activity.py:425` 与 `inference_doctor.py:85` 都已跟着改**），且新 lens 报得出「尾读升级次数 / **升窗字节数每分钟** / oversize / 钳制数」。 |
| **A13** | 1600 行下：单次 `render()` < 150ms；**5s 的年龄 tick 不触发整表重排**（Performance 面板量）。 |
| **A14** | **代码 diff** 只碰 `activity.py` / `serve.py`(SESS_PAGE) / `inference_doctor.py` / `tests/test_tokmon.py`；**文档 diff 必须包含 `EVOLUTION.md`**（事件总线输出变更 + F7/F8/F13）**与 `CHANGELOG.md`**，以及本文件。**`parser` / `pricing` / `aggregate` / `discovery` / `records` / `project` / `events` / `notify` / `control` / `procmon` / `inference_backtest` 零改动**（用 import-graph 实测核，M2 已有这个习惯）。<br>🔴 **别把 A14 读成「只许碰 4 个文件」** —— 那会让事件总线的行为变更**不被记录**，正是本文自己骂的「静默」。 |
| **A15** | V1 老用户（`localStorage` 存着 `states:['wait']`）升级后**看到的行数不减少**（迁移成 `['wait','idle']`），且看到一次可关闭的升级说明横幅。 |
| **A16** | 🔴 **改动前后各跑一次 `python -m tokmon backtest`（`tokmon/inference_backtest.py`，零改动），三把尺的数字逐位相同** —— 这是 `_turn_phase` 重构「**行为不变**」的**全语料证明**（161 个真 transcript，强于 21 条 fixture）。<br>注：**backtest 只跑 `classify_state`，不覆盖 `_recent_events` 的改动**——那部分只能靠 **T24 / T25**。 |

---

## 收尾

**实现顺序有硬依赖，不许打乱：**

> **① F7 尾读 + 钳制 + 升窗节流（含 `_read_tail` 的两个调用点：`activity.py:425` / `inference_doctor.py:85`） → ② 谓词统一 + 5 个新字段 → ③ F8 `current_step`（含改写 `tests:616`）→ ④ 前端表格 / 分面 / 新鲜度**

（②依赖①：新字段来自同一份 tail objs，**不先修①，新字段会在最需要它的会话上恰好是 `None`**。）

沿用既有纪律：`design → implement → **python 单测（这次真有）** → 真机冒烟 → 对抗式 review → 修确认项`。

先问北极星：

> **「我所有正在跑的 Claude Code，现在分别是什么状态？有没有哪个需要我现在介入？」**（MISSION_CONTROL.md:21）

- **V1 让它更快了**（能筛了）。
- **V1 同时让它更不可信**（数字撒谎）；**V2 诊断出来了，但从没实现。**
- **V3 要回答的是一个更难堪的问题：这一页最核心的一件事——「Claude 回没回我」——它在三个地方给出三个不同的答案，并且默认把其中一个答案的结果从屏幕上删掉了。**

**所以 V3 的成功判据不是「加了表格和排序」。**

> **是：这一页从此只有一份「Claude 回了我」的定义 —— 它写在内核里、被单测钉住、由页面上的运行时断言持续监视；而凡是会收窄你视野的东西，都必须写在你脸上。**
>
> **一个把 71 分之 70 的答案藏起来的看板，快不快已经无所谓了。**