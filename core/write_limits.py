"""
写入上限（跨 Work / Demand 共用）
=================================

单次写入/编辑超过这个字符数时，动作层直接拒绝：
Agent 必须拆成 create + append / edit，而不是一次写完整文件。

放在 `core/` 是因为它既不是 Demand 规则，也不是 Work 规则，
而是**动作层的结构性上限**。
"""

#: 单次 workspace_write / workspace_edit 的最大字符数。
MAX_SINGLE_WRITE_CHARS = 12_000
