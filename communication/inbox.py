"""Agent Inbox 的运行时处理结果。

🔒 这是 **Runtime / Handler 的契约**，不是 Message 协议的一部分。
   它不进入网络，也不写进 `communication_messages`。

V1 只定义结果形状；真正的消费者在 Phase 2 才接入。
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIONS = frozenset({
    "processed",
    "deferred",
    "ignored",
    "reply_sent",
    "failed",
})
REPLY_MODES = frozenset({"never", "manual", "auto"})


@dataclass(frozen=True)
class InboxPolicy:
    """Agent 级 Inbox 行为策略；V1 默认不自动回复。"""

    reply_mode: str = "never"
    max_auto_replies_per_conversation: int = 3
    max_cost_per_message: float = 0.0

    def __post_init__(self) -> None:
        mode = str(self.reply_mode or "never").strip().lower()
        if mode not in REPLY_MODES:
            raise ValueError(f"未知 InboxPolicy.reply_mode：{self.reply_mode!r}")
        object.__setattr__(self, "reply_mode", mode)
        if self.max_auto_replies_per_conversation < 0:
            raise ValueError("max_auto_replies_per_conversation 不能为负")
        if self.max_cost_per_message < 0:
            raise ValueError("max_cost_per_message 不能为负")


@dataclass(frozen=True)
class InboxOutcome:
    """一条 inbox 消息被 Agent Runtime 处理后的结果。"""

    action: str
    reply: str = ""
    error: str = ""
    next_checkpoint: str = ""

    def __post_init__(self) -> None:
        action = str(self.action or "").strip().lower()
        if action not in ACTIONS:
            raise ValueError(f"未知 InboxOutcome.action：{self.action!r}")
        object.__setattr__(self, "action", action)

    @classmethod
    def processed(cls) -> "InboxOutcome":
        return cls(action="processed")

    @classmethod
    def deferred(cls, *, next_checkpoint: str = "") -> "InboxOutcome":
        return cls(action="deferred", next_checkpoint=next_checkpoint)

    @classmethod
    def ignored(cls, *, reason: str = "") -> "InboxOutcome":
        return cls(action="ignored", error=str(reason or ""))

    @classmethod
    def replied(cls, text: str) -> "InboxOutcome":
        return cls(action="reply_sent", reply=str(text or ""))

    @classmethod
    def failed(cls, error: str) -> "InboxOutcome":
        return cls(action="failed", error=str(error or ""))


__all__ = ["ACTIONS", "REPLY_MODES", "InboxOutcome", "InboxPolicy"]
