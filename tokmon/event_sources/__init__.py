"""各支柱的事件适配器 (P4 结构性独立)。

每个 `*_source.py` 只 import: stdlib + tokmon.events + 自己那个 pillar 的公开 snapshot。
绝不 import 兄弟 pillar。它们只在 `bus.emit(Event(...))` 处汇合。events.py 本身不认任何 pillar。
"""
