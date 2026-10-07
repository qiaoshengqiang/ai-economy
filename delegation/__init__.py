"""
Delegation —— 生产性任务委托（**AIECO 的经济边界**）
========================================================

【🔴 本轮的核心经济原则】

    **Communication is not commerce. Task delegation is commerce.**
    沟通不收费，生产性委托才进入经济。

                     Communication          Task Delegation
    ─────────────────────────────────────────────────────────
    是什么            信息交换                生产能力的委托 / 购买
    例子              「你怎么看这个人物？」   「请你完成一份心理分析并写进 Workspace」
    扣 AIC            ❌ 不扣                  ✅ 扣
    产生交易          ❌ 不产生                ✅ 产生
    Ledger entry      ❌ 没有                  ✅ 有

【⚠️ 最容易搞混的一点】

    「沟通不收费」**不是**「这次 LLM 调用不产生成本」。

        LLM API Cost            = 使用**认知资源**的成本（照记，`LLM_COST`）
        Agent-to-Agent Commerce = Agent 之间**生产能力交换**的经济交易

    两者不是同一种成本。Communication 会产生前者，**不**产生后者。

【🔒 三条不许越过的边界】

    1. `Delegation ≠ Task`
       Task 表达「要做什么」，Delegation 表达「谁委托谁、是否形成经济交换」。
       **不复制第二个 Task 状态机** —— `task_id` 只是引用。

    2. `Offer ≠ Ledger`
       Offer **不许**保存「requester 当前余额 = 20」。
       余额只能来自 Ledger，`delegation/` 里没有 balance 字段也没有余额计算。

    3. `Agent 决策 / Economy 记账`
       Agent 决定报价、接受、拒绝。
       Runtime **不自动接受**、**不按阈值放行**、**不硬编码价格**。
       Economy 只负责验证（余额、存在性、正数）与记账。

【FORBIDDEN NOW（§五 明确列出的）】

    ❌ bidding ❌ auction ❌ dynamic pricing ❌ reputation pricing
    ❌ profit optimization ❌ market matching ❌ escrow ❌ credit system
"""

from delegation.models import (
    ALL_STATUSES,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_OFFERED,
    STATUS_REJECTED,
    STATUS_SETTLED,
    DelegationError,
    DelegationOffer,
    InvalidTransitionError,
    new_offer_id,
)
from delegation.service import DelegationService
from delegation.store import DelegationStore

__all__ = [
    "DelegationStore", "DelegationService", "DelegationOffer",
    "DelegationError", "InvalidTransitionError", "new_offer_id",
    "STATUS_OFFERED", "STATUS_ACCEPTED", "STATUS_REJECTED",
    "STATUS_COMPLETED", "STATUS_SETTLED", "STATUS_CANCELLED",
    "ALL_STATUSES",
]
