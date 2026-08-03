"""tokmon — Claude Code 跨 VSCode session 的 token 用量监控雏形 (v0).

数据来源: ~/.claude/projects/<project>/**/*.jsonl —— Claude Code 为每个会话写的
transcript 文件。每条 assistant 消息都带完整的 usage 字段, 我们解析、去重、定价、聚合。

入口: `python -m tokmon watch`  /  `python -m tokmon report`
"""

__version__ = "0.3.0"
