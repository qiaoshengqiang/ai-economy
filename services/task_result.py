"""
Task Handler Result —— 应用层处理结果
============================================================

【为什么单独一个文件】

    它是**所有**消息处理器的返回类型：

        TaskHandlers ─┐
                      ├─→ HandlerResult
        Negotiation  ─┘

    如果把它放在 task_service.py 里，就会出现循环依赖：

        task_service → handlers → task_service   ❌

    所以把它抽成一个叶子模块 —— 谁都依赖它，它不依赖谁。

【它为什么不是「回复消息」】

    「处理业务」和「发送消息」是两件事：

        处理业务：改 Task 状态、写报价历史、生成成交凭证
        发送消息：由通信层负责

    所以处理器只**返回**「应该回什么」（reply_type / reply_payload），
    发不发、什么时候发，由调用方（组合门面或组合根）决定。

    这也让它能在完全没有通信层的情况下被测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from communication.message import Message


# ============================================================
# 处理动作（出现在 HandlerResult.action 里）
# ============================================================
class HandlerAction:
    """
    处理器做过什么。

    【为什么要枚举出来而不是随手写字符串】
        组合根、演示、测试都要根据 action 判断下一步。
        散落的字符串字面量迟早会拼错，而且拼错了不会报错。
    """

    # TASK_OFFER
    CREATED = "created"                  # 本地新建了任务
    LINKED = "linked"                    # 关联到已有任务

    # COUNTER_OFFER
    NEGOTIATING = "negotiating"          # 对方还价，进入/保持谈判
    COUNTERED = "countered"              # 我方还价（发送侧）

    # ACCEPT
    ACCEPTED = "accepted"                # 成交
    ALREADY_ACCEPTED = "already_accepted"  # 重复的成交消息（幂等）

    # REJECT
    REJECTED = "rejected"                # 拒绝（业务行为 或 校验失败）
    ALREADY_REJECTED = "already_rejected"  # 重复的拒绝（幂等）

    # ---- Step 4D：执行阶段 ----
    STARTED = "started"                  # 承接方开工（ACCEPTED → IN_PROGRESS）
    SUBMITTED = "submitted"              # 承接方提交成果（IN_PROGRESS → SUBMITTED）
    VERIFIED = "verified"                # 发起方验收通过（SUBMITTED → VERIFIED）
    REWORK = "rework"                    # 发起方验收不通过，打回重做（→ IN_PROGRESS）
    COMPLETED = "completed"              # 发起方结项（VERIFIED → COMPLETED）
    REOPENED = "reopened"                # 返工：COMPLETED → IN_PROGRESS（Iteration +1）

    # 通用
    ALREADY_PROCESSED = "already_processed"  # 这条消息之前处理过（幂等）


@dataclass(frozen=True)
class HandlerResult:
    """
    处理一条业务消息的结果。

    【为什么要返回一个结构，而不是直接回消息】
        因为「处理业务」和「发送消息」是两件事。
        处理器只做前者 —— 发不发、发什么类型，由调用方决定。

        这也让它可以在没有通信层的情况下被测试。
    """

    ok: bool

    #: 见 HandlerAction
    action: str

    task_id: Optional[str] = None

    #: 校验失败的原因。ok=True 时为空。
    errors: Tuple[str, ...] = ()

    #: 非致命的问题（比如 description 为空）。记下来但不拒绝。
    warnings: Tuple[str, ...] = ()

    #: 要给对方回的应答。None 表示**不需要回复**。
    reply_type: Optional[str] = None
    reply_payload: Optional[Dict[str, Any]] = None

    #: 【Step 4C】已经构造好的回复消息。
    #: 有它的时候 to_reply() 直接返回它 ——
    #: 因为发送侧需要「先知道这条消息的 message_id」才能把它记进报价历史，
    #: 否则同一个报价会在发送侧和接收侧各记一次。
    reply_message: Optional[Message] = None

    #: 供日志和调试用的附加信息
    details: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok

    def to_reply(self, message: Message) -> Optional[Message]:
        """
        把结果转成一条回复消息。

        返回 None 表示「不用回复」——
        正常的 TASK_OFFER 处理成功就是这种情况：
        对方已经收到了，接下来该**它**决定要不要接，我们不该抢答。
        """
        if self.reply_message is not None:
            return self.reply_message
        if self.reply_type is None or self.reply_payload is None:
            return None
        return message.reply(message_type=self.reply_type,
                             payload=self.reply_payload)

    def summary(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "task_id": self.task_id,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "replies": self.reply_type is not None,
        }

    def __repr__(self) -> str:
        return (
            f"HandlerResult(ok={self.ok}, action={self.action!r}, "
            f"task_id={self.task_id!r}, errors={list(self.errors)!r})"
        )
