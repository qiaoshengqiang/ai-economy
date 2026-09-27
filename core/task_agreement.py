"""
Task Agreement —— 成交凭证
============================================================

【它是什么】

    一条**不可变**的记录，说的是：

        「双方在这个时间，通过这条报价，达成了这个价格的任务协议。」

【它不是什么】（这三条边界非常重要）

    ❌ 它不是 Ledger
       Ledger 记的是「钱动了」。这里一分钱都没动。

    ❌ 它不是支付记录
       ACCEPT 只意味着**合同成立**，不意味着付款。
       以后 Payment 阶段可以**引用**这条凭证去执行转账。

    ❌ 它不是权限
       达成协议 ≠ 获得 Workspace 访问权。

【为什么需要一个独立的凭证】

    因为 Task 上的 `agreed_price` 会随着状态变化而被后来的操作覆盖或混淆，
    而「当时到底谈成了什么」必须有一份**不会再变**的记录。

    审计时你需要能回答：

        · 这笔交易是谁和谁谈的？
        · 最终价格是多少？依据是哪条报价？
        · 属于哪一次执行（trace）？哪一轮对话（conversation）？

    Task 本身 + Offer History 其实已经能推出这些 ——
    但这个凭证把它们**凝固**成一个原子事实，将来 Payment 直接引用它即可，
    不必再去重放整条历史。

【为什么 append-only】

    和账本同一个道理：**历史不允许被改写**。
    如果允许改，那就没法回答「当时到底谈成的是什么」——
    因为答案会随着每次修改而变。

    同一个 Task 理论上可以有多份协议（比如先谈成、后来取消、再谈成），
    所以这里不做「一个 Task 只能有一份」的唯一约束，
    而是保留全部，取最新的一份作为当前有效的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from core.identity import new_message_id, utc_now_iso
from core.money import Money


class AgreementError(Exception):
    """成交凭证相关错误。"""


@dataclass(frozen=True)
class TaskAgreementRecord:
    """
    一份成交凭证。**不可变。**

    用法：
        agreement = TaskAgreementRecord(
            task_id="task_x",
            initiator="agent_a",
            contractor="agent_b",
            agreed_price=Money(25),
            accepted_offer_message_id="msg_counter_3",
            trace_id="trc_x",
            conversation_id="conv_x",
        )
    """

    task_id: str
    initiator: str
    contractor: str
    agreed_price: Money

    #: 被接受的那条报价消息的 message_id。
    #:
    #: 【为什么要它】
    #:     §18 要求「能从 Task 反查到双方最终接受的是哪一条报价」。
    #:     有了这个 ID，就能一路查到 Communication Journal。
    #:
    #: 【为什么不新增一个 accepted_offer_id】
    #:     因为 message_id 已经唯一标识了那条报价消息，
    #:     再加一个 ID 只会多一个可能不一致的来源。
    accepted_offer_message_id: Optional[str] = None

    #: 这次成交发生在哪一次执行 / 哪一轮对话。
    trace_id: Optional[str] = None
    conversation_id: Optional[str] = None

    agreement_id: str = ""

    #: 成交时间
    created_at: str = ""

    def __post_init__(self) -> None:
        for name in ("task_id", "initiator", "contractor"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AgreementError(f"{name} 必须是非空字符串")

        if not isinstance(self.agreed_price, Money):
            raise AgreementError(
                f"agreed_price 必须是 Money，"
                f"收到 {type(self.agreed_price).__name__}"
            )

        if self.initiator == self.contractor:
            raise AgreementError(
                "发起方和承接方不能是同一个 Agent —— "
                "自己和自己成交没有意义"
            )

        # 自动补 ID 和时间（frozen 对象用 object.__setattr__）
        if not self.agreement_id:
            object.__setattr__(
                self, "agreement_id", new_message_id("agreement")
            )
        if not self.created_at:
            object.__setattr__(self, "created_at", utc_now_iso())

    # ------------------------------------------------------------
    @property
    def price(self) -> Money:
        """`agreed_price` 的别名，读起来更顺。"""
        return self.agreed_price

    @property
    def currency(self) -> str:
        return self.agreed_price.currency

    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "agreement_id": self.agreement_id,
            "task_id": self.task_id,
            "initiator": self.initiator,
            "contractor": self.contractor,
            "agreed_price": self.agreed_price.to_dict(),
            "accepted_offer_message_id": self.accepted_offer_message_id,
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskAgreementRecord":
        return cls(
            agreement_id=data.get("agreement_id") or "",
            task_id=data["task_id"],
            initiator=data["initiator"],
            contractor=data["contractor"],
            agreed_price=Money.from_dict(data["agreed_price"]),
            accepted_offer_message_id=data.get("accepted_offer_message_id"),
            trace_id=data.get("trace_id"),
            conversation_id=data.get("conversation_id"),
            created_at=data.get("created_at") or "",
        )

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "agreement_id": self.agreement_id,
            "task_id": self.task_id,
            "from": self.initiator,
            "to": self.contractor,
            "price": str(self.agreed_price),
            "accepted_offer": self.accepted_offer_message_id,
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            # 明确说明这不是钱
            "is_payment": False,
            "note": "这是成交凭证，不是支付记录；付款属于后续步骤",
        }

    def __str__(self) -> str:
        return (
            f"{self.initiator} ↔ {self.contractor} @ {self.agreed_price}"
            f"（{self.task_id}）"
        )

    def __repr__(self) -> str:
        return (
            f"TaskAgreementRecord(task_id={self.task_id!r}, "
            f"price={self.agreed_price}, "
            f"accepted={self.accepted_offer_message_id!r})"
        )
