"""核心数据模型: 一条 assistant 消息对应一个 UsageRecord。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class UsageRecord:
    timestamp: datetime          # 本地时区, tz-aware
    project: str                 # 友好项目名 (如 vscode-API)
    session_id: str
    model: str
    source_kind: str             # main | subagent | workflow
    input_tokens: int
    output_tokens: int
    cache_5m: int                # ephemeral 5m 写入
    cache_1h: int                # ephemeral 1h 写入
    cache_read: int              # 缓存命中读取
    web_search: int
    web_fetch: int
    cost_usd: float
    known_price: bool            # 模型单价是否已知
    message_id: str              # 去重用
    request_id: str              # 去重用
    cwd: str = ""                # 会话真实 cwd
    subpath: str = ""            # 项目内更深一层的相对路径 (供未来 drill-down)
    under_vscode: bool = False   # 是否位于 .vscode 之下

    @property
    def cache_write(self) -> int:
        return self.cache_5m + self.cache_1h

    @property
    def total_tokens(self) -> int:
        """全部经手 token (输入 + 输出 + 缓存写 + 缓存读)。"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_5m
            + self.cache_1h
            + self.cache_read
        )

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.message_id, self.request_id)
