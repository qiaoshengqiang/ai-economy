"""
Delegation 领域模型
========================================================

【为什么是独立对象，而不是塞进 Task（§四）】

    Task 已经有 12 状态生命周期，而且它是**协作者之间的合约**语义。
    生产性委托要表达的是另一件事：

        「A 请 B 做一件需要实际生产投入的事，B 报价，A 决定要不要买。」

    把报价 / 结算 / 拒绝史塞进 Task，会让 Task 变成什么都装的经济容器 ——
    而 Task Core 已经被明确要求保持薄。

    所以：**Task 说「做什么」，Delegation 说「谁委托谁、成不成交」。**

【🔒 这里没有 balance 字段，也永远不该有（§十一）】

    余额的唯一权威是 Ledger。
    一个 Offer 若存下「requester 余额 = 20」，就会和 Ledger 分叉，
    而且**不会有任何机制**把两边对齐。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# ================================================================
# 状态（§五 的最小集合，一个都不多）
# ================================================================
#: Requester 提出了委托，等 Provider 报价或回应。
STATUS_OFFERED = "OFFERED"
#: Requester 已接受 Provider 的报价。
#: 🔴 **不代表钱动了** —— 见 §九：ACCEPTED ≠ 付款。
STATUS_ACCEPTED = "ACCEPTED"
#: 任一方拒绝。**没有经济交易**（§十 A / B）。
STATUS_REJECTED = "REJECTED"
#: 生产已完成，交付物已登记。**仍然没有转账**。
STATUS_COMPLETED = "COMPLETED"
#: 已结算：Ledger 上发生了真实转账。
STATUS_SETTLED = "SETTLED"
#: 委托被撤销（尚未完成）。
STATUS_CANCELLED = "CANCELLED"

ALL_STATUSES = (
    STATUS_OFFERED, STATUS_ACCEPTED, STATUS_REJECTED,
    STATUS_COMPLETED, STATUS_SETTLED, STATUS_CANCELLED,
)

#: 终态 —— 到了这里不会再变。
TERMINAL_STATUSES = (STATUS_REJECTED, STATUS_SETTLED, STATUS_CANCELLED)


class DelegationError(RuntimeError):
    """委托相关的业务错误。"""


class InvalidTransitionError(DelegationError):
    """
    非法的状态迁移。

    🔒 它是**明确的拒绝**，不是「静默什么都不做」——
       静默会把「这次操作没生效」和「这次操作成功了但没变化」混起来。
    """


def new_offer_id() -> str:
    return f"off_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class DelegationOffer:
    """
    一次生产性委托。

    🔒 名字叫 Offer 是因为它承载「报价」这件事，
       但它的生命周期**比一次报价长** —— 从提出一直活到结算。
    """

    offer_id: str
    task_id: str
    requester_agent_id: str
    provider_agent_id: str

    #: 委托内容 —— 「请你做什么」。
    description: str = ""

    #: 报价（AIC）。None = Provider 还没报价。
    #: 🔒 **由 Provider Agent 自己提出**（§六）。
    #:    Runtime 不硬编码「Editor = 5 AIC」——
    #:    角色不能自动决定价格。
    price_aic: Optional[float] = None
    currency: str = "AIC"

    status: str = STATUS_OFFERED

    #: 交付物在共享 Workspace 中的引用（不是内容副本）。
    deliverable_ref: str = ""

    #: 谁拒绝的（requester / provider），以及为什么。
    rejected_by: str = ""
    reject_reason: str = ""

    #: 结算产生的 Ledger 交易号。**唯一**的经济事实凭据。
    settled_tx_id: str = ""

    created_at: str = ""
    accepted_at: str = ""
    completed_at: str = ""
    settled_at: str = ""

    # ------------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_open(self) -> bool:
        """还在流程里（等回应 / 等完成 / 等结算）。"""
        return self.status in (STATUS_OFFERED, STATUS_ACCEPTED,
                               STATUS_COMPLETED)

    @property
    def has_price(self) -> bool:
        return self.price_aic is not None

    def counterparty_of(self, agent_id: str) -> Optional[str]:
        if agent_id == self.requester_agent_id:
            return self.provider_agent_id
        if agent_id == self.provider_agent_id:
            return self.requester_agent_id
        return None

    def role_of(self, agent_id: str) -> str:
        if agent_id == self.requester_agent_id:
            return "requester"
        if agent_id == self.provider_agent_id:
            return "provider"
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
