# Claude Mission Control

> 当 Claude Code 在替我 agentic coding 时，让我随时知道它在
> **推进 / 卡住 / 烧钱 / 乱改**，并且只在重要时刻、用恰当的方式提醒我——**有用，且不烦。**

一个**纯本地、只读**的 AI coding 运维驾驶舱。数据来自 Claude Code 自己写的会话 transcript
（`~/.claude/projects/**/*.jsonl`）——**不装代理、不 hook、不改 Claude Code 的任何配置**。

它由 `tokmon`（token 成本监控）长成：那部分仍是平台的**成本支柱**，宪章见 [NORTH_STAR.md](NORTH_STAR.md)；
平台级方向见 [MISSION_CONTROL.md](MISSION_CONTROL.md)。

---

## 现在是什么状态（2026-09-22 实测）

| | |
|---|---|
| 代码 / 测试 | 约 11k 行 · **282 个测试全过**（`python -m pytest -q`；含 Node 跑的页面冒烟测试） |
| 工作流回放 | `/workflow`：近 7 天 98 个任务可回放，token 与 /tokens **逐 token 对账 98/98**；S2 学习层：流程条、数据依赖、名词说明、脚本对照（43/43 次 workflow 的阶段都对上了脚本）；S3 统计：工具 / skill 排行、MCP 健康、跨任务对照，**每个数字点开都是同样条数的明细**（真实数据 323/323） |
| 状态推断可信度 | 回测 **97.0% 准确率 / 1343 个评估点**（`python -m tokmon backtest`） |
| 数据契约 | `tokmon doctor` 全绿（覆盖 ≈100%、0 跨来源碰撞） |
| 手机环 | ⏸ 已搁置（2026-09-21 你的决定）：通道就绪但从未用真账号验证过 |

全景评估与逐条侧批见 [docs/RECAP_2026-09-20.md](docs/RECAP_2026-09-20.md)（或浏览器打开
[docs/recap.html](docs/recap.html)）。

---

## 快速开始

```powershell
pip install -r requirements.txt      # rich / psutil 都是可选, 不装也能跑

python -m tokmon serve               # 驾驶舱: http://127.0.0.1:8765/
python -m tokmon watch               # 终端实时盯盘
python -m tokmon report --since 7d   # 按需报表
python -m tokmon doctor              # 体检 (升级 Claude Code 后先跑这个)
```

## 命令

| 命令 | 说明 |
|------|------|
| `serve` | 本地驾驶舱（纯标准库 `http.server`，零依赖可离线，默认只监听 `127.0.0.1`） |
| `watch` | 实时盯盘 TUI：今天/近 7 天/全部的 token + 成本，每 5s 刷新 |
| `report` | 按天 / 项目 / 模型 / 来源的汇总报表 |
| `doctor` | **两半体检**：成本契约（解析/去重/定价）+ 推断契约（状态判断的载重假设） |
| `backtest` | 用 transcript 的**未来当真值**回测状态推断准确率，三把尺 + 混淆矩阵 |

公共参数（写在子命令后）：`--scope {all,main}` · `--vscode-only` · `--since` · `--interval` · `--claude-dir`

## 驾驶舱的页面

| 页面 | 支柱 / 层 | 内容 |
|---|---|---|
| `/sessions` | 对话活动 | 每个会话在 **推进 / 处理中 / 久未返回 / 等你 / 读不出**，含当前步骤与真实思考片段 |
| `/tokens` | 成本 | KPI + 「vs 上一周期」自基线对比 + 日/周预算设置；项目名可点进工作流回放 |
| `/workflow` | 工作流回放 + 统计 | **一次提问是怎么被完成的**：结构化摘要（耗时三段 / token / 异常 / 关键时刻）+ 调用树与时间轴并排 + 明细抽屉（完整输入输出，已脱敏）。skill / MCP / 子 agent / workflow 的层级都在里面。**统计**（`?view=stats`）：哪些工具 / skill 最花时间、哪个 MCP 老出错、同一个阶段在不同任务里差多少——数字点开是明细，明细点开回到回放 |
| `/processes` | 进程 | 进程 / 监听端口 / 活动连接 / cloudflared 隧道 / 健康探测（纯只读 + 命令行脱敏） |
| `/notify` | 通知层 | ⏸ 已从导航隐藏（URL 仍可用）。推送与抑制双记的 feed + 通道配置 |
| `/control` | 控制层 | ⏸ 已从导航隐藏（URL 仍可用）。远程批 permission · 终止进程 · 释放端口 · steer · 答选择题 |
| `/billing` | 厂商账单 | Anthropic / OpenAI 官方 usage API（Google 无官方 API，诚实标「不可得」） |
| `/doctor` `/backtest` | 信任基建 | 体检与回测的 Web 视图 |
| `/` | 入口 | 分流到上面各页 |

---

## 三条安全边界（这是这个项目的人格，别改坏）

**1 · 监控永远只读。** 三个支柱绝不写 `~/.claude`、不 hook、不注入、不替你点 permission。
*控制*是单独一层，只执行**你显式下达**的、allow-list 内的动作，每条二次确认 + 鉴权 + 全审计，失败一律**报「做不到」**。

**2 · 默认一个字节不出本机。** 至今只有两个**受控破例**出站，都默认全关、显式 opt-in：

| 出站 | 开关 | 内容 |
|---|---|---|
| 通知（Telegram / Pushover） | `MC_PUSHOVER_TOKEN`+`MC_PUSHOVER_USER` 或 `MC_TELEGRAM_TOKEN`+`MC_TELEGRAM_CHAT_ID` | 只有 严重度/类型/项目/极简详情，**无命令行、无路径、无 diff、无密钥** |
| 厂商账单 | `~/.tokmon/providers.json`（0600） | 带 admin key 去官方 usage API 拉聚合数字 |

**3 · 暴露到本机之外必须显式收口（`MC_REMOTE`）。** 默认只听 `127.0.0.1`。要经隧道上手机：

```powershell
$env:MC_REMOTE = "1"
$env:MC_REMOTE_HOSTS = "your-random-name.trycloudflare.com"   # 显式白名单, 不支持通配
python -m tokmon serve
```

开启后**所有页面与 `/api/*` 都要令牌**（手机上先开 `/login` 贴一次，存 HttpOnly Cookie）。
配置不自洽（绑非本机却没开远程 / 开了远程却没白名单或没令牌）→ **拒绝启动**。

> **诚实边界**：token-only 是唯一的闸。所以——隧道 URL 当秘密、**用完即关**、泄露就轮换
> `~/.tokmon/control_token`。它的入站攻击面**大于** Telegram 的零端口长轮询，后者才是更安全的终态。

---

## 监控单位 = `.vscode` 下的子文件夹

项目名从 transcript 里的真实 `cwd` **逐记录**还原（会话中途 `cd` 也不会算错项目）：
空格保留；更深的工作目录归并回所属子文件夹（余下存 `subpath`）；不在 `.vscode` 下的会话回退目录名，
可用 `--vscode-only` 过滤。逻辑全在 [tokmon/project.py](tokmon/project.py) 的纯函数里，有单测覆盖。

> ⚠️ 订阅用户（Max/Pro）看到的 `$` 是**等价用量价值**，不是真实账单扣费。这里**不做账单对账**——
> 实测订阅会话根本不进 API 平台账单，硬对就是自欺（理由见 [PROVIDER_BILLING_PLAN.md](PROVIDER_BILLING_PLAN.md) §0.3）。

---

## 文档地图（**本表是唯一索引**，别再靠猜）

| 文档 | 性质 | 状态 |
|---|---|---|
| [MISSION_CONTROL.md](MISSION_CONTROL.md) | 平台宪章（北极星 / 事件契约 / 8 条不变量） | ✅ 现行 |
| [NORTH_STAR.md](NORTH_STAR.md) | 成本支柱宪章 | ✅ 现行 |
| [docs/RECAP_2026-09-20.md](docs/RECAP_2026-09-20.md) | **最新全景 + 侧批 + 重启计划** | ✅ 现行 |
| [EVOLUTION.md](EVOLUTION.md) | 逐代进化史 + 证据分级（2026-07-01） | 📜 历史快照 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更 | ✅ 已恢复更新 |
| [docs/atlas.html](docs/atlas.html) | 系统图谱（离线 Mermaid） | 📜 停在 07-01，不含 billing/steer |
| [WORKFLOW_TAB_PLAN.md](WORKFLOW_TAB_PLAN.md) | **`/workflow` 工作流追踪器一页规格** | ✅ S1–S3 全部验收（0.10.0 → 0.12.0） |
| [RUNNER_SDK_PLAN.md](RUNNER_SDK_PLAN.md) | steer 改用 Agent SDK | ✅ 已实施 |
| [SESSIONS_FILTER_PLAN.md](SESSIONS_FILTER_PLAN.md) | `/sessions` 过滤器 V1 | ✅ 已实施 |
| [REMOTE_CONTROL_PLAN.md](REMOTE_CONTROL_PLAN.md) | 远程 steer + 上手机 | 🟡 S1/S0 已实施，S2/S3 未做 |
| [M4.5_PLAN.md](M4.5_PLAN.md) | 信任加固四 lens + 手机环 | 🟡 L1/L2 + 通道已做，L3/L4 未做 |
| [FLEET_COCKPIT_V0_PLAN.md](FLEET_COCKPIT_V0_PLAN.md) | 手机答 AskUserQuestion | 🟡 网页答题已做，Telegram 双向未做 |
| [PROVIDER_BILLING_PLAN.md](PROVIDER_BILLING_PLAN.md) | `/billing` | 🟡 代码已交付，等你填 admin key |
| [SESSIONS_V3_PLAN.md](SESSIONS_V3_PLAN.md) | `/sessions` 表格 + 分面过滤 | ❌ **未实施**（今天只取了其中最小诚实切片） |
| [SESSIONS_FILTER_V2_PLAN.md](SESSIONS_FILTER_V2_PLAN.md) | 被 V3 取代 | ❌ **从未实施，仅存档** |
| [docs/HOOK_EVOLUTION_BRIEF.md](docs/HOOK_EVOLUTION_BRIEF.md) · [docs/HOOK_ISOLATED_TEST_GUIDE.md](docs/HOOK_ISOLATED_TEST_GUIDE.md) | hook 诊断与操作指引 | 📜 参考 |

## 已知缺口（诚实记账）

- **手机环未用真账号验证过**（已搁置）——通道、策略、线路形状都有测试守卫，只剩「你配一次账号」。
- **`/workflow` 的「等你」只认 AskUserQuestion / ExitPlanMode**：permission 弹窗的等待在 transcript 里和工具执行分不开，算在机器执行里。
- **无持久化**：每次全量重扫 transcript（数据长大后会变慢，SQLite 仍是 backlog）。
- **`report` 没有 `--by` / `--json`**——NORTH_STAR 里标着「下一个待做」，被整个平台化跳过了。
- **推断 doctor 的 L3（事件完整性回放）/ L4（打扰预算）未做。**
- **风险事件（`REPEATED_FILE_EDIT` / `LARGE_DIFF` / `SENSITIVE_FILE_TOUCH`）未做**——
  「推进 / 卡住 / 烧钱」都有了，**「它在干危险事吗」是唯一还空着的一格**。

## 结构

```
tokmon/
  # 成本支柱 (内核: 纯函数, 不被展示层污染)
  discovery / parser / pricing / aggregate / records / project / util
  report / tui                 CLI 两种形态
  doctor                       数据契约体检
  # 平台
  activity                     对话活动支柱 (classify_state 纯函数)
  procmon                      进程支柱 (只读 + 脱敏)
  events                       事件总线 (支柱无关, payload allow-list)
  event_sources/               activity_source (5s) · cost_source (60s)
  notify                       通知层 (总线消费者; Telegram + Pushover, 默认全关)
  control                      控制层 (permission 审批, 全程失败安全)
  runner                       steer (Agent SDK + canUseTool, 唯一重依赖)
  billing                      厂商账单 (第 4 数据源, 绝不 import 成本内核)
  remote                       MC_REMOTE 读页收口 (读门 / 控制门故意分离)
  trace                        工作流追踪支柱 (/workflow 数据层: 任务切分 / 调用树 / 耗时三段 / token 对账 / 统计与追溯索引)
  pages/workflow.html          /workflow 页面 (独立文件, 不再内联进 serve.py)
  inference_doctor / inference_backtest   推断层的体检与回测
  serve                        驾驶舱外壳
tests/                         282 例: pytest -q (tests/js/workflow_smoke.js: 页面 JS 的 Node 冒烟脚手架)
```
