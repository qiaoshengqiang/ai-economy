"""
DelegationService —— 「Agent 决策，Economy 记账」
========================================================

【🔒 这个服务**只**做两件事】

    ① 验证一次**已经由 Agent 决定**的委托操作是否合法
    ② 在结算时把真实转账交给 `Economy.transfer()`

    它**不做**：
        ❌ 自动接受           （§七：不要 Runtime 自动接受）
        ❌ 按价格阈值放行      （§七：`if price < threshold: accept()` 是 Strategy）
        ❌ 硬编码角色价格      （§六：`Editor = 5 AIC` 不允许）
        ❌ 自己算余额          （§十一：余额只能来自 Ledger）
        ❌ 修改 Ledger 数值    （只有 `Economy.transfer` 能产生转账）

【状态机】

    OFFERED ──provider_respond(accept=False)──> REJECTED     ← §十 A，无交易
       │
       ├──requester_respond(accept=False)─────> REJECTED     ← §十 B，无交易
       │
       └──provider_respond(accept=True, price)─> OFFERED（有价）
              │
              └──requester_respond(accept=True)─> ACCEPTED   ← §十 C：这里查余额
                     │                                        **但不转账**
                     └──complete(ref)───────────> COMPLETED
                            │
                            └──settle()────────> SETTLED     ← §十 D：这里真转账
                                                              Ledger append-only

🔴 `ACCEPTED ≠ 付款`（§九）。
   接受只是「双方同意这笔交易」，钱要到 `settle()` 才动。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso
from core.logging_setup import get_logger
from delegation.models import (
    STATUS_ACCEPTED, STATUS_CANCELLED, STATUS_COMPLETED, STATUS_OFFERED,
    STATUS_REJECTED, STATUS_SETTLED, DelegationError, DelegationOffer,
    InvalidTransitionError,
)

logger = get_logger(__name__)


class DelegationService:
    """
    🔒 `economy` 是**注入**的，`delegation/` 不 import `core/economy.py`。

        · `economy.transfer(...)`   唯一被允许的转账路径
        · `economy.can_afford(...)` 只读的偿付能力检查
        · `economy.currency`        货币单位

    这样这个模块可以脱离整个 Economy 单独测试
    （测试里注入一个记账用假 Economy 即可）。
    """

    def __init__(self, store: Any, economy: Any) -> None:
        self.store = store
        self.economy = economy

    # ================================================================
    # 提出委托 —— **Agent 决定**，不是 Runtime
    # ================================================================
    def request(
        self,
        *,
        task_id: str,
        requester_agent_id: str,
        provider_agent_id: str,
        description: str = "",
        offer_id: Optional[str] = None,
    ) -> DelegationOffer:
        """
        Requester 提出一次生产性委托。

        ⚠️ 这一步**不产生任何经济变化** —— 没有报价、没有转账。
           它只是把「我请你做这件事」变成一条可追踪的事实。

        🔒 `price_aic` 刻意留空：价格由 **Provider** 提出（§六）。
        """
        if requester_agent_id == provider_agent_id:
            raise DelegationError("不能委托自己 —— 那不是一个生产能力交换")
        if not str(description or "").strip():
            raise DelegationError("委托必须说明要做什么（description 为空）")

        return self.store.create(
            task_id=task_id,
            requester_agent_id=requester_agent_id,
            provider_agent_id=provider_agent_id,
            description=str(description).strip(),
            currency=getattr(self.economy, "currency", "AIC"),
            offer_id=offer_id,
        )

    # ================================================================
    # Provider 回应：拒绝，或**自己报价**
    # ================================================================
    def provider_respond(
        self,
        offer_id: str,
        *,
        accept: bool,
        price_aic: Optional[float] = None,
        reason: str = "",
    ) -> DelegationOffer:
        """
        Provider 的决定。

            accept=False            → REJECTED（§十 A：没有经济交易）
            accept=True + price_aic → 仍是 OFFERED，但**有价了**
                                      （等 Requester 决定买不买）

        🔒 `price_aic` 必须由调用方（Agent）给出。
           本方法**没有**任何默认价、角色价、区间价。
        """
        offer = self._must_get(offer_id)
        if offer.status != STATUS_OFFERED:
            raise InvalidTransitionError(
                f"offer {offer_id} 当前状态 {offer.status}，"
                f"Provider 不能再回应（只有 OFFERED 可以）")

        if not accept:
            return self.store.update(
                offer_id, status=STATUS_REJECTED,
                rejected_by=offer.provider_agent_id,
                reject_reason=str(reason or ""))

        price = self._check_price(price_aic)
        return self.store.update(offer_id, price_aic=price)

    # ================================================================
    # Requester 回应：接受或拒绝 Provider 的报价
    # ================================================================
    def requester_respond(
        self,
        offer_id: str,
        *,
        accept: bool,
        reason: str = "",
    ) -> DelegationOffer:
        """
        Requester 的决定。

            accept=False → REJECTED（§十 B：没有经济交易）
            accept=True  → ACCEPTED（§十 C：**这里**由 Economy 验证真实余额）

        🔴 `accept=True` **不转账**。钱要到 `settle()` 才动（§九）。

        🔒 余额检查用的是 `economy.can_afford()` —— 一个**只读**查询。
           它不修改余额，也不替 Agent 做决定；
           它只是把「你买不起」这件系统事实在成交前告诉调用方。
        """
        offer = self._must_get(offer_id)
        if offer.status != STATUS_OFFERED:
            raise InvalidTransitionError(
                f"offer {offer_id} 当前状态 {offer.status}，Requester 不能回应")
        if not offer.has_price:
            raise DelegationError(
                f"offer {offer_id} 还没有价格 —— Provider 必须先报价")

        if not accept:
            return self.store.update(
                offer_id, status=STATUS_REJECTED,
                rejected_by=offer.requester_agent_id,
                reject_reason=str(reason or ""))

        # --- 余额由 Economy / Ledger 验证（§八）---
        if not self._can_afford(offer.requester_agent_id, offer.price_aic):
            raise DelegationError(
                f"余额不足，不能接受报价："
                f"{offer.requester_agent_id} 需要 {offer.price_aic} "
                f"{offer.currency}，但 Ledger 显示它付不起。"
                f"（这是一个**系统事实**，不是策略判断）")

        return self.store.update(
            offer_id, status=STATUS_ACCEPTED, accepted_at=utc_now_iso())

    # ================================================================
    # 完成 → 结算
    # ================================================================
    def complete(
        self,
        offer_id: str,
        *,
        deliverable_ref: str = "",
    ) -> DelegationOffer:
        """
        生产已完成。`deliverable_ref` 指向共享 Workspace 里的交付物 ——
        **只存引用，不存内容副本**。

        ⚠️ 仍然**没有转账**。§九 明确：ACCEPTED 不立即把钱交给 Provider。

        🔒 §九 的限制如实记录：当前系统还没有可靠的
           deliverable validation。`complete()` 是**最小**的完成标记，
           **不假装**它验证了交付质量。
        """
        offer = self._must_get(offer_id)
        if offer.status != STATUS_ACCEPTED:
            raise InvalidTransitionError(
                f"offer {offer_id} 当前状态 {offer.status}，不能标记完成"
                f"（只有 ACCEPTED 才能 complete）")
        return self.store.update(
            offer_id, status=STATUS_COMPLETED,
            completed_at=utc_now_iso(),
            deliverable_ref=str(deliverable_ref or ""))

    def settle(self, offer_id: str) -> DelegationOffer:
        """
        结算：**这里才真正动钱**（§十 D）。

            完成 → Settlement → Requester - price / Provider + price

        🔒 唯一的经济事实来自 `Economy.transfer()`：
           它验证存在性 / 正数 / 偿付能力 / 余额，然后写 append-only Ledger。
           本方法**不碰**任何余额数字，只把 tx_id 记回来当凭据。
        """
        offer = self._must_get(offer_id)
        if offer.status != STATUS_COMPLETED:
            raise InvalidTransitionError(
                f"offer {offer_id} 当前状态 {offer.status}，不能结算"
                f"（只有 COMPLETED 才能 settle）")
        if not offer.has_price or float(offer.price_aic) <= 0:
            raise DelegationError(f"offer {offer_id} 没有有效价格，不能结算")

        # 🔒 这是**唯一**的转账入口。异常（余额不足 / 破产 / 不存在）
        #    直接向上抛 —— 不捕获、不降级、不假装成功。
        tx = self.economy.transfer(
            offer.requester_agent_id,
            offer.provider_agent_id,
            float(offer.price_aic),
            reason=f"delegation:{offer_id}",
            task_id=offer.task_id,
        )

        settled = self.store.update(
            offer_id, status=STATUS_SETTLED, settled_at=utc_now_iso(),
            settled_tx_id=str(getattr(tx, "tx_id", "")))

        logger.info(
            "Delegation 已结算 %s：%s → %s %.4f %s（tx=%s）",
            offer_id, offer.requester_agent_id, offer.provider_agent_id,
            float(offer.price_aic), offer.currency, settled.settled_tx_id)
        return settled

    def cancel(self, offer_id: str, *, reason: str = "") -> DelegationOffer:
        offer = self._must_get(offer_id)
        if offer.is_terminal:
            raise InvalidTransitionError(
                f"offer {offer_id} 已是终态 {offer.status}")
        return self.store.update(
            offer_id, status=STATUS_CANCELLED, reject_reason=str(reason or ""))

    # ================================================================
    # 只读辅助
    # ================================================================
    def pending_for(self, agent_id: str,
                    *, task_id: Optional[str] = None) -> List[DelegationOffer]:
        """
        与这个 Agent 有关、且**还在流程里**的委托。

        🔒 供 Cognitive Context 使用（§十三）：
           只提供**事实**，不提供「你应该接受 / 拒绝」。
        """
        offers = self.store.list_offers(task_id=task_id, agent_id=agent_id)
        return [o for o in offers if o.is_open]

    # ------------------------------------------------------------
    def _must_get(self, offer_id: str) -> DelegationOffer:
        offer = self.store.get(offer_id)
        if offer is None:
            raise DelegationError(f"offer {offer_id!r} 不存在")
        return offer

    @staticmethod
    def _check_price(price_aic: Any) -> float:
        """
        🔒 只校验**是不是一个合法金额**，不校验「这个价合不合理」。

            「5 AIC 太高了」是 Agent Strategy —— Runtime 不许替它判断。
        """
        if price_aic is None:
            raise DelegationError("Provider 接受委托时必须给出 price_aic")
        try:
            value = float(price_aic)
        except (TypeError, ValueError):
            raise DelegationError(f"price_aic 必须是数字，收到 {price_aic!r}")
        if value != value:
            raise DelegationError("price_aic 不能是 NaN")
        if value <= 0:
            raise DelegationError(
                f"price_aic 必须大于 0，收到 {value}"
                f"（零价或负价不是一次生产能力交换）")
        return value

    def _can_afford(self, agent_id: str, amount: Any) -> bool:
        """
        只读的偿付能力检查。

        🔒 取不到就**当作付不起**（fail-closed）——
           否则一个坏掉的 Economy 会让所有委托都被放行。
        """
        checker = getattr(self.economy, "can_afford", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(agent_id, float(amount)))
        except Exception:                                     # noqa: BLE001
            return False


def summarize(offers: List[DelegationOffer]) -> Dict[str, Any]:
    """把一批委托汇总成可报告统计（§二十 B）。"""
    counts: Dict[str, int] = {}
    for o in offers:
        counts[o.status] = counts.get(o.status, 0) + 1
    settled = [o for o in offers if o.status == STATUS_SETTLED]
    return {
        "total": len(offers),
        "by_status": counts,
        "settled_count": len(settled),
        "settled_total_aic": round(
            sum(float(o.price_aic or 0) for o in settled), 6),
    }
