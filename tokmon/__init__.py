"""Claude Mission Control —— 纯本地只读的 AI coding 运维驾驶舱 (由 tokmon 成本监控长成)。

数据来源: ~/.claude/projects/<project>/**/*.jsonl —— Claude Code 为每个会话写的 transcript。
三个只读支柱 (成本 / 进程 / 对话活动) -> 事件总线 -> 通知与控制。不 hook、不注入、不改 Claude Code。

入口: `python -m tokmon serve | watch | report | doctor | backtest`
方向: MISSION_CONTROL.md (平台) + NORTH_STAR.md (成本支柱); 现状与侧批: docs/RECAP_2026-09-20.md
"""

# 0.3.0 -> 0.9.0: 版本号自 2026-06-28 起就停在 0.3.0, 而代码早已走过
# serve/M1/M2/M3/M3.5/M4/M4.5/billing/steer。0.9 = 平台各层齐备, 但"手机环真机验证"未完成,
# 所以**还不是 1.0**。见 CHANGELOG.md 的「未记录期」条目。
# 0.10.0: /workflow 工作流回放 S1 (见 WORKFLOW_TAB_PLAN.md)。
# 0.11.0: S2 学习层 (阶段标注 / 数据依赖 / 名词说明 / 脚本对照)。
# 0.15.1: /sessions 状态以 Claude Code 会话注册表 (进程自报) 为准, 修重启残留/死会话/后台 Agent/等授权误判。
# 0.16.0: /tokens 重做 (一屏分析台 + 联动筛选 + 数据立方 tokens_view.py)。代码早于 0.15.1 并入, 版本号补记。
# 0.17.0: 会话驾驶舱 S1 —— /sessions 每行当前任务的等价 $ / 近 10 分钟 / token, 页顶今天已烧 + 有风险; 简报后台算不阻塞。
# 0.18.0: 会话驾驶舱 S2 —— 导航铃铛: 会话卡在你身上 >= 60 秒 -> 本机浏览器通知 (默认关, 零外发)。
# 0.19.0: 省钱 S1 —— /sessions 每行上下文多大 / 每轮多少钱 / 缓存还剩多久 (context_view.py, 展示层)。
__version__ = "0.19.0"
