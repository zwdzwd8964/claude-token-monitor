# tokmon — Claude Code Token 监控器 (v0 雏形)

监控你在**所有 VSCode session** 里用 Claude Code vibe coding 产生的 token 用量与等价成本。

数据直接来自 Claude Code 自己写的会话 transcript（`~/.claude/projects/**/*.jsonl`），
每条 assistant 消息都带完整 `usage` 字段——**无需任何网络代理、无需改 Claude Code 配置**，
纯本地只读解析。

> 📍 这是雏形 v0。它该往哪进化、怎么进化，见 [NORTH_STAR.md](NORTH_STAR.md)。
>
> 🛰️ tokmon 是更大平台 **Claude Mission Control**（AI coding 运维驾驶舱）的「成本支柱」。
> 平台级方向（监控→事件→通知 三层架构）见 [MISSION_CONTROL.md](MISSION_CONTROL.md)。

## 快速开始

```powershell
# 1. (可选但推荐) 装 rich, 获得漂亮表格与实时 TUI
pip install rich

# 2. 实时盯盘 (你最想要的核心体验)
python -m tokmon watch

# 3. 按需报表
python -m tokmon report --since 7d
python -m tokmon report --since today --scope main
python -m tokmon report --since all

# 4. 数据体检 (升级 Claude Code 后跑一次, 确认格式没变 / 数字可信)
python -m tokmon doctor

# 5. 本地监控台 (早期预览, 只监听本机, 不外发)
python -m tokmon serve            # 主页 http://127.0.0.1:8765/  → /tokens (Token 看板) + /processes (进程监控)
```

不装 rich 也能跑——自动降级为纯文本输出。

## 命令

| 命令 | 说明 |
|------|------|
| `python -m tokmon watch` | 实时盯盘 TUI：今天/近 7 天/全部 的 token+成本，正在跑的项目高亮，每 5s 刷新 |
| `python -m tokmon report` | 按天/项目/模型/来源 的汇总报表 |
| `python -m tokmon doctor` | 数据契约体检：识别率/字段覆盖/去重健康/未知模型——守护「依赖 Claude Code 内部格式」这个最大风险 |
| `python -m tokmon serve` | 本地监控台（早期预览，只监听 `127.0.0.1`、默认不外发）。主页分流到两个**并行同级**的视图，`--host`/`--port` 可调 |

### 监控台的两个页面（`tokmon serve`）

主页 `/` 是入口，下面两个监控并行同级、互不耦合：

- **`/tokens` — Token 看板**：KPI + 按天/项目/模型/来源，带 **「vs 上一周期」自基线对比**（回答「跟我的预期差多少」）。数据来自 token 监控内核（`parser/pricing/aggregate`）。
- **`/processes` — 进程 / 端口监控**（纯标准库 + `psutil`，**纯只读**）：
  - 本机进程资源（CPU / 内存 / 运行时长 / 命令行），内存∪CPU 各取 Top N；
  - **localhost 监听端口 → 占用进程**（loopback / all-interfaces 标记）；
  - **活动网络连接**（只数真正 ESTABLISHED 的 TCP + 已连 UDP，不含 TIME_WAIT 等），并按**远端 IP 聚合**看「谁在连哪里」；
  - **本机 `cloudflared` 隧道进程**（状态 / 资源 / 监听口；隧道走 QUIC，边缘链路在 socket 层不可见，如实说明）；
  - **本地服务健康探测**（opt-in，点按钮才跑）：对本机可经 `127.0.0.1`/`::1` 触达的端口先 TCP 连一次、开着再发 HTTP HEAD，显示 存活 / 状态码 / 响应时间。**主动连接但只碰本机、不外发**；HEAD 在 HTTP 层只读幂等，对非 HTTP 服务仅是一次会被其日志记录的异常连接（不改数据）。
  - **只读、不外发**：只观测、绝不 kill/改任何东西。被动快照零网络请求；命令行做尽力脱敏（token/key/URI 口令等）。缺 `psutil` 时该页友好提示安装，**不影响 Token 看板**。

> 数据层独立：进程监控在 `tokmon/procmon.py`，**不碰 token 监控内核**。

公共参数（写在子命令后即可）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--scope {all,main}` | `all` | `all`=含子智能体/workflow；`main`=仅你直接交互的主会话 |
| `--vscode-only` | 关 | 只统计位于 `.vscode` 之下的会话（排除其它目录的会话） |
| `--since` | `7d` (report) | 时间窗口：`today` / `all` / `24h` / `7d` / `2w` |
| `--interval` | `5` (watch) | TUI 刷新间隔秒数 |
| `--claude-dir` | `~/.claude/projects` | 数据目录覆盖 |

## 项目 = `.vscode` 下的子文件夹

『监控单位』是 **`.vscode` 文件夹下的每个直接子文件夹**（即你的每个 VSCode 工作区）。
项目名从 transcript 里的真实 `cwd` 还原，因此：

- 空格被保留（`edgar api`、`news feed 0622`，不会被编码成 `-`）。
- 更深的工作目录会归并回它所属的子文件夹：
  `…\.vscode\API\auto refresh strategy\prod_20260604` → 归入项目 **API**（更深的路径存为 subpath，备未来 drill-down）。
- 不在 `.vscode` 下的会话（如你在别处跑的）回退用目录名，可用 `--vscode-only` 过滤掉。

> 这条逻辑全在 `tokmon/project.py` 的纯函数 `workspace_identity()` 里，有单测覆盖。

## 它统计了什么

每条 assistant 消息的 `usage`：
- `input` / `output` tokens
- 缓存写入（区分 5m / 1h TTL，倍率不同）/ 缓存读取
- web search / web fetch 次数
- 按模型家族定价（Opus $5/$25、Sonnet $3/$15、Haiku $1/$5、Fable $10/$50 per 1M），
  缓存写 ×1.25(5m)/×2(1h)、缓存读 ×0.1

> ⚠️ 若你是 Max/Pro 订阅用户，显示的 `$` 是**等价用量价值**，不是真实账单扣费。

## 它还没做什么

见 [NORTH_STAR.md](NORTH_STAR.md) 的「非目标」与「路线图」。一句话：v0 只做**离线/轮询式的本地统计与展示**，
没有持久化、没有预算告警、没有 Web 看板、没有真正的文件监听（用的是定时重扫）。

## 结构

```
tokmon/
  discovery.py  发现并分类 session 文件 (main/subagent/workflow)
  project.py    cwd -> .vscode 子文件夹 的项目身份识别 (纯函数, 有单测)
  parser.py     JSONL -> UsageRecord, 去重 + 文件级缓存
  pricing.py    模型定价表 + 成本计算  ← 改价只动这里
  records.py    UsageRecord 数据模型
  aggregate.py  按天/项目/模型/来源聚合
  report.py     按需 CLI 报表
  tui.py        实时盯盘 TUI
  doctor.py     数据契约体检 (格式漂移 / 去重 / 未知模型)
  util.py       格式化 / 时间窗口解析
  cli.py        命令行入口
tests/
  test_tokmon.py  project/pricing/parser 单测 (python tests\test_tokmon.py 即可跑)
```
