"""
Task Negotiation Service —— 议价业务规则与状态落地
============================================================

【它负责什么】

    · 议价业务规则（谁能回应、价格是否允许、是否重复）
    · 把一条已经校验过的业务动作**落到 Task 状态上**
    · 报价历史
    · 轮次
    · 成交凭证

【它不负责什么】

    ❌ 不解析协议 payload     → 那是 TaskMessageMapper 的事
    ❌ 不做经济决定           → 那是 DecisionPolicy 的事
    ❌ 不直接改数据库         → 那是 TaskStore 的事
    ❌ 不发消息               → 那是调用方的事

【DecisionPolicy 的关系 —— 方向不能反】

        TaskNegotiationService
                ↓  问
        DecisionPolicy
                ↓  答
        Decision

    而不是：

        DecisionPolicy → TaskNegotiationService → Wallet / Task / Message   ❌

    Policy 只**返回**决定；把它落到状态上的是本文件。
    有架构守卫盯着这条线。

【为什么规则和状态落地放在一起】

    因为它们**一起变化**：
        改一条议价规则（比如「还价不能超过预算」），
        往往同时要改「校验通过之后怎么落地」。
        它们的变化原因是同一个 —— 「议价业务怎么运作」。

    而 Mapper 的变化原因是「协议长什么样」，
    所以它们必须分开。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from core.decision_policy import (
    Decision,
    DecisionContext,
    DecisionPolicy,
)
from core.logging_setup import get_logger
from core.money import DEFAULT_CURRENCY, Money
from core.task import (
    OfferType,
    Task,
    TaskStatus,
    TaskValidationError,
)
from core.task_agreement import TaskAgreementRecord
from core.task_store import TaskStore

from communication.message import Message, MessageType

from services.task_application_service import TaskApplicationService
from services.task_message_mapper import TaskMessageMapper
from services.task_result import HandlerAction, HandlerResult

logger = get_logger(__name__)


class TaskNegotiationService(TaskApplicationService):
    """
    议价规则 + 状态落地。

    用法（通常由 TaskService 门面持有）：

        negotiation = TaskNegotiationService(store, policies)
        errors = negotiation.validate_actor(task, actor, message)
        result = negotiation.apply_accept(task, price, incoming=msg, ...)
    """

    def __init__(
        self,
        store: TaskStore,
        policies: Optional[Mapping[str, DecisionPolicy]] = None,
        mapper: Optional[TaskMessageMapper] = None,
    ):
        # store / mapper 由基类持有 —— 幂等、trace 缝合、协议违规回复
        # 都在基类里，议价和执行两个服务共用同一份实现（不复制）。
        super().__init__(store=store, mapper=mapper)

        # 每个 Agent 自己的决策策略。
        #
        # 【为什么是一本「按 agent_id 查」的字典，而不是一个全局策略】
        #     因为策略是 Agent 的**私有经济参数**：
        #         B 的最低价是 20，C 的可能是 50
        #     如果只有一个全局策略，就必然要写
        #         if agent_id == "agent_b": ...
        #     而那正是被明令禁止的。
        #
        #     策略由**组合根**按每个 Agent 的配置构造，
        #     这里只负责查表 —— 它不认识任何具体 Agent。
        self.policies: Dict[str, DecisionPolicy] = dict(policies or {})

    # ============================================================
    # 一、业务规则（校验）
    # ============================================================
    def validate_currency(self, money: Money, field_name: str) -> List[str]:
        """
        货币必须是本项目唯一的货币代码。

        【为什么这条是业务规则而不是协议规则】
            协议只规定「金额是一个带 currency 的对象」；
            「本项目只收 AIC」是经济系统自己的决定。
        """
        if money.currency != DEFAULT_CURRENCY:
            return [
                f"{field_name}.currency 必须是 {DEFAULT_CURRENCY}，"
                f"收到 {money.currency!r}"
            ]
        return []

    def validate_counter_price(self, task: Task, price: Money) -> List[str]:
        """
        还价必须落在预算之内（协议 §14 / §15）。

        【为什么它属于这里，不属于通信层】
            通信层继续只负责把消息送到 —— 它有守卫盯着，
            代码里连 budget 这个词都不许出现。
        """
        if task.budget is None:
            return []

        if price.currency != task.budget.currency:
            return [
                f"还价货币 {price.currency} 与预算货币 "
                f"{task.budget.currency} 不一致"
            ]

        if price > task.budget:
            return [
                f"还价 {price} 超过预算上限 {task.budget}。\n"
                f"预算（budget）是发起方愿意支付的上限，"
                f"报价不能超过它。"
            ]
        return []

    def validate_actor(
        self, task: Task, actor: str, message: Message
    ) -> List[str]:
        """
        谁能对任务做什么。

        【参数 actor 是「做出这个动作的人」—— 这一点很容易搞反】

            接收侧的 handler 处理的是**对方发来的**消息：
                对方是行动方     → actor = message.from_agent
                我们是收件人     → message.to_agent 是我们

            发送侧（execute）处理的是**我们收到的**消息：
                我们是行动方     → actor = message.to_agent

            所以 actor 必须由调用方显式传进来，
            不能在方法里硬取某一个字段 —— 两种上下文正好相反。
            （这个坑真的踩过：把 to_agent 当行动方，导致
             「A 收到 B 的拒绝」被误判成「A 在回自己的报价」。）

        【三条规则】

            1. 任务必须处于可协商状态（OFFERED / NEGOTIATING）
            2. 行动方必须是这个任务的合法参与方
            3. 行动方不能回应「自己刚发的那条报价」

        【为什么规则 3 是必要的】
            否则同一个人可以自己报价、自己接受 ——
            那不是协商，是一个人代表双方签合同。

        【这里没有任何具体 Agent 的名字】
            规则 2 查 task.is_participant()（读 creator / contractor /
            participants），规则 3 查 offer.from_agent。
            换成任何两个 Agent 都成立。
        """
        errors: List[str] = []

        # ---- 规则 1：任务状态 ----
        if task.is_terminal:
            errors.append(
                f"任务 {task.task_id} 已经是终态 {task.status}，不能再响应。"
            )
            return errors

        if task.status not in (TaskStatus.OFFERED, TaskStatus.NEGOTIATING,
                               TaskStatus.CREATED):
            errors.append(
                f"任务 {task.task_id} 当前状态是 {task.status}，"
                f"不接受新的报价响应。"
            )

        # ---- 规则 2：必须是合法参与方 ----
        if not task.is_participant(actor):
            known = {task.creator}
            if task.contractor:
                known.add(task.contractor)
            known.update(task.participants)

            errors.append(
                f"{actor} 不是任务 {task.task_id} 的参与方。\n"
                f"  这个任务的参与方是：{sorted(known)}\n"
                f"  没有参与这个任务的人，不能替它做决定。"
            )

        # ---- 规则 3：轮次 ----
        latest = task.latest_offer()
        if latest is not None and latest.from_agent == actor:
            errors.append(
                f"轮次不对：任务 {task.task_id} 最后一条报价"
                f"（{latest.offer_type}）就是 {actor} 自己发的，"
                f"不能自己回自己。\n"
                f"（否则一个人可以代表双方成交）"
            )

        return errors

    # ============================================================
    # 二、幂等（比角色校验更靠前的一道门）
    # ============================================================
    @staticmethod
    def already_recorded(task: Task, message: Message) -> bool:
        """
        这条消息是不是已经在报价历史里了？

        【为什么这道门必须排在角色 / 轮次校验之前】

            同一台机器上（单进程演示）两边共用一个 store：
                发送侧：我决定还价 → 先记进历史 → 再发消息
                接收侧：收到那条消息 → 又想记一次

            等接收侧处理时，历史里**最后一条报价已经是发送者自己发的**了。
            如果先跑轮次校验，就会误判成
            「他自己回自己」—— 而那其实是同一条消息。

            在真实的多机部署里，这条检查同样必要：
            消息可能因为重发而到达两次（Step 3 的日志里就有 DUPLICATE 状态）。

        【判断依据】
            报价历史里有没有同样 message_id 的记录。
            message_id 是消息的唯一标识 —— 这正是它存在的意义。
        """
        if not message.message_id:
            return False
        return task.find_offer_by_message(message.message_id) is not None

    def already_accepted(self, task: Task, price: Money) -> Optional[HandlerResult]:
        """
        任务已经成交、且价格一致 → 这是一条重复的 ACCEPT。

        【为什么它必须排在角色校验之前】
            发送侧接受之后，任务在本地已经是 ACCEPTED 了。
            对方收到 ACCEPT 再走一遍时，会先撞上「任务已是终态」的校验，
            被误判成一次非法操作 —— 但它其实只是同一条成交消息的接收侧。
        """
        if task.status != TaskStatus.ACCEPTED:
            return None

        existing = self.store.latest_agreement(task.task_id)
        if existing is None or existing.agreed_price != price:
            return None

        return HandlerResult(
            ok=True,
            action=HandlerAction.ALREADY_ACCEPTED,
            task_id=task.task_id,
            details={
                "status": task.status,
                "agreement_id": existing.agreement_id,
                "agreed_price": str(existing.agreed_price),
                "accepted_offer_message_id": existing.accepted_offer_message_id,
                "note": "这次成交之前已经记录过（幂等，不重复记账）",
            },
        )

    @staticmethod
    def already_rejected(task: Task) -> Optional[HandlerResult]:
        """任务已经是 REJECTED → 这是一条重复的拒绝。"""
        if task.status != TaskStatus.REJECTED:
            return None
        return HandlerResult(
            ok=True,
            action=HandlerAction.ALREADY_REJECTED,
            task_id=task.task_id,
            details={
                "status": task.status,
                "note": "这次拒绝之前已经记录过（幂等）",
            },
        )

    # ============================================================
    # 三、决策（纯函数：只问策略，不改任何状态）
    # ============================================================
    def policy_for(self, agent_id: str) -> Optional[DecisionPolicy]:
        """
        取出某个 Agent 自己的决策策略。

        【注意这里不是「系统的策略」】
            每个 Agent 有自己的经济参数（最低价、目标价……）。
            本方法只是查表 —— 它不认识任何具体 Agent，
            也不包含任何价格规则（不许把某个 Agent 的价格策略写进代码）。
        """
        return self.policies.get(agent_id)

    def build_context(
        self, task: Task, responder: str, message: Message
    ) -> DecisionContext:
        """
        把「任务 + 消息」翻译成策略能看的上下文。

        【为什么方向来自 is_initiator，而不是 agent_id】
            买卖双方对「好价格」的判断是相反的。
            但「我是买方还是卖方」应该由**任务关系**决定：
                任务的 creator  = 发起方 = 买方（付钱）
                另一方          = 承接方 = 卖方（干活）
            这样换成任何两个 Agent 都成立。
        """
        offered = self.offered_price_from(message)

        policy = self.policy_for(responder)
        max_rounds = getattr(policy, "max_rounds", 0)

        return DecisionContext(
            agent_id=responder,
            task_id=task.task_id,
            offered_price=offered,
            is_initiator=task.is_initiator(responder),
            budget=task.budget,
            negotiation_round=task.negotiation_round_for(responder),
            max_rounds=max_rounds,
            message_type=message.message_type,
        )

    def offered_price_from(self, message: Message) -> Money:
        """
        从消息里取出「对方这次给的价格」。

        【实现已经搬到 Mapper】
            因为「TASK_OFFER 报的是 budget、COUNTER_OFFER 报的是 price」
            是**协议语义**，不是议价规则。
            放在这里会让「按消息类型分派」散落到业务代码中。

            这个方法保留下来只是因为 build_context 读起来更顺，
            它本身不含任何判断。
        """
        return self.mapper.offered_price(message)

    def decide(self, message: Message) -> Decision:
        """
        问策略「我该怎么办」。

        【这个方法**不修改任何状态**】
            不改 Task、不改报价历史、不发消息。
            这样决策可以被单独测试、记录、重放。

            Decision Policy 回答「我要 ACCEPT 还是 COUNTER_OFFER」，
            修改状态是 TaskNegotiationService / Task Core 的事。
        """
        responder = message.to_agent
        task_id = dict(message.payload).get("task_id")

        task = self.store.get(task_id) if task_id else None
        if task is None:
            raise TaskValidationError(f"本地没有任务 {task_id!r}，无法决策")

        policy = self.policy_for(responder)
        if policy is None:
            raise TaskValidationError(
                f"{responder} 没有配置决策策略（DecisionPolicy）。\n"
                f"每个 Agent 的经济参数是它自己的，必须由组合根按配置构造。"
            )

        context = self.build_context(task, responder, message)
        return policy.evaluate(context)

    # ============================================================
    # 四、状态落地：把校验过的动作落到 Task 上
    # ============================================================
    def open_offer(
        self,
        incoming: Message,
        task_id: str,
        description: str,
        budget: Money,
        workspace_id: Optional[str] = None,
        required_capabilities: Optional[Any] = None,
        acceptance: Optional[Any] = None,
        expires_at: Optional[str] = None,
    ) -> HandlerResult:
        """
        处理一条 TASK_OFFER：建立 / 关联本地任务，记下报价，进入 OFFERED。

        【注意这里是 OFFERED 而不是 ACCEPTED】
            收到邀约 ≠ 接受邀约。接不接由 Agent 自己判断（§23）。
        """
        existing = self.store.get(task_id)
        created = existing is None
        warnings: List[str] = []

        if created:
            task = Task.create(
                task_id=task_id,
                creator=incoming.from_agent,
                description=description,
                budget=budget,
                workspace_id=workspace_id,
                required_capabilities=required_capabilities,
                acceptance=acceptance,
                expires_at=expires_at,
                # 【关键的缝合点】本地任务用**消息的** trace_id。
                # 这样从通信日志查到的链路，和任务是同一条。
                trace_id=incoming.trace_id,
                participants=[incoming.from_agent, incoming.to_agent],
            )
            trace_synced = True
        else:
            task = existing
            trace_synced = self.sync_trace(task, incoming)

            if task.budget is not None and task.budget != budget:
                warnings.append(
                    f"这条 offer 的预算 {budget} 与本地记录的 {task.budget} 不同；"
                    f"保留本地值（发起方的预算以第一次声明为准）"
                )

        self.record_offer(task, OfferType.TASK_OFFER, budget, incoming)

        if task.status == TaskStatus.CREATED:
            task.offer()

        self.store.save(task)

        logger.debug(
            "处理 TASK_OFFER：task=%s from=%s message=%s",
            task.task_id, incoming.from_agent, incoming.message_id,
        )

        return HandlerResult(
            ok=True,
            action=HandlerAction.CREATED if created else HandlerAction.LINKED,
            task_id=task.task_id,
            warnings=tuple(warnings),
            # 【不回复】对方已经送达了，接下来该我们决定要不要接。
            # 抢着回一条「收到」会被误读成业务答复。
            reply_type=None,
            reply_payload=None,
            details={
                "status": task.status,
                "budget": str(task.budget),
                "required_capabilities": task.required_capabilities.names,
                "message_id": incoming.message_id,
                "trace_id": incoming.trace_id,
                "conversation_id": incoming.conversation_id,
                "trace_synced": trace_synced,
                "offers": len(task.offers),
            },
        )

    def apply_counter_offer(
        self, task: Task, price: Money, incoming: Message
    ) -> HandlerResult:
        """
        对方的还价**有效** → 记进报价历史，状态进入 / 保持 NEGOTIATING。

        【这是接收侧的落地】（对方报的价，我们记下来）
        """
        self.sync_trace(task, incoming)
        self.record_offer(task, OfferType.COUNTER_OFFER, price, incoming)

        if task.status in (TaskStatus.OFFERED, TaskStatus.NEGOTIATING):
            task.negotiate()

        self.store.save(task)

        return HandlerResult(
            ok=True,
            action=HandlerAction.NEGOTIATING,
            task_id=task.task_id,
            reply_type=None,
            reply_payload=None,
            details={
                "status": task.status,
                "price": str(price),
                "budget": str(task.budget) if task.budget else None,
                "offers": len(task.offers),
                "round": task.negotiation_round_for(incoming.from_agent),
                "message_id": incoming.message_id,
                "reply_to": incoming.reply_to,
                "from_agent": incoming.from_agent,
            },
        )

    def counter_locally(
        self, task: Task, price: Money, incoming: Message,
        outgoing: Optional[Message] = None,
    ) -> HandlerResult:
        """
        我方还了一个价（发送侧）→ 记进本地报价历史，状态进入 NEGOTIATING。

        【和 apply_counter_offer 的区别】
            这个方法记的是**我方**的报价（from_agent 是「我们」= incoming.to_agent）；
            那个方法记的是**对方**的报价。
            两者最终都落到同一份报价历史，但方向相反。
        """
        self.sync_trace(task, incoming)

        task.record_offer(
            OfferType.COUNTER_OFFER,
            price,
            message_id=outgoing.message_id if outgoing else None,
            reply_to=incoming.message_id,
            from_agent=incoming.to_agent,
            trace_id=incoming.trace_id,
            conversation_id=incoming.conversation_id,
            note=f"我方还价（回应 {incoming.message_id}）",
        )

        if task.status in (TaskStatus.OFFERED, TaskStatus.NEGOTIATING):
            task.negotiate()

        self.store.save(task)

        return HandlerResult(
            ok=True,
            action=HandlerAction.COUNTERED,
            task_id=task.task_id,
            reply_type=MessageType.COUNTER_OFFER,
            reply_payload={
                "task_id": task.task_id,
                "price": price.to_dict(),
            },
            reply_message=outgoing,
            details={
                "status": task.status,
                "price": str(price),
                "reply_to": incoming.message_id,
                "round": task.negotiation_round_for(incoming.to_agent),
            },
        )

    def apply_reject(
        self, task: Task, reason: Optional[str], incoming: Message
    ) -> HandlerResult:
        """对方的拒绝**有效** → 任务进入 REJECTED。**不产生任何经济后果。**"""
        self.sync_trace(task, incoming)

        task.record_offer(
            OfferType.REJECT,
            task.budget if task.budget else Money(0),
            message_id=incoming.message_id,
            reply_to=incoming.reply_to,
            from_agent=incoming.from_agent,
            trace_id=incoming.trace_id,
            conversation_id=incoming.conversation_id,
            note=reason or f"来自 {incoming.from_agent} 的拒绝",
        )
        task.reject(reason)
        self.store.save(task)

        logger.debug(
            "任务 %s 被 %s 拒绝：%s", task.task_id, incoming.from_agent, reason
        )

        return HandlerResult(
            ok=True,
            action=HandlerAction.REJECTED,
            task_id=task.task_id,
            reply_type=None,
            reply_payload=None,
            details={
                "status": task.status,
                "reason": reason,
                "from_agent": incoming.from_agent,
                # 明确记录：拒绝不产生任何经济后果
                "payment": None,
                "workspace": None,
            },
        )

    def reject_locally(
        self, task: Task, reason: Optional[str], incoming: Message,
        outgoing: Optional[Message] = None,
    ) -> HandlerResult:
        """我方拒绝（发送侧）→ 任务进入 REJECTED，并返回要发出去的 REJECT。"""
        self.sync_trace(task, incoming)

        task.record_offer(
            OfferType.REJECT,
            task.budget if task.budget else Money(0),
            message_id=outgoing.message_id if outgoing else None,
            reply_to=incoming.message_id,
            from_agent=incoming.to_agent,
            trace_id=incoming.trace_id,
            conversation_id=incoming.conversation_id,
            note=reason or "我方拒绝",
        )
        task.reject(reason)
        self.store.save(task)

        return HandlerResult(
            ok=True,
            action=HandlerAction.REJECTED,
            task_id=task.task_id,
            reply_type=MessageType.REJECT,
            reply_payload={"task_id": task.task_id, "reason": reason},
            reply_message=outgoing,
            details={
                "status": task.status,
                "reason": reason,
                "payment": None,
                "workspace": None,
            },
        )

    # ============================================================
    # 五、成交（ACCEPT 的唯一落地点）
    # ============================================================
    def apply_accept(
        self,
        task: Task,
        price: Money,
        incoming: Message,
        accepted_offer_message_id: Optional[str],
        accepted_offer_type: str,
        outgoing: Optional[Message] = None,
        accept_author: Optional[str] = None,
    ) -> HandlerResult:
        """
        把「成交」落到 Task 状态 + 报价历史 + 成交凭证。

        【只有这里会让任务变成 ACCEPTED】
            不管是「我自己决定接受」（发送侧），
            还是「对方接受了我的报价」（接收侧），
            都走这一条路 —— 否则两边状态会不一致。

        【它**只**做三件事】
            1. Task: OFFERED/NEGOTIATING → ACCEPTED，记 agreed_price
            2. 报价历史里追加一条 ACCEPT（带 message_id 和 reply_to）
            3. 生成一份不可变的成交凭证（TaskAgreementRecord）

        【它刻意不做的事】
            ❌ 不转账、不写账本
            ❌ 不创建 Workspace、不授予权限
            ❌ 不调模型
            ❌ 不碰 Git
        """
        self.sync_trace(task, incoming)

        # 1) 状态：→ ACCEPTED（Task Core 会校验预算上限）
        #
        # 【承接方是谁】
        #     不是发起方的那个，就是承接方。
        #     从这条消息的双方里挑一个不等于 creator 的即可 ——
        #     不管是我在接受对方的报价，还是对方接受了我的报价，
        #     这个判断都成立，而且不需要知道任何具体 Agent 的名字。
        parties = {incoming.from_agent, incoming.to_agent}
        contractor = next(
            (agent for agent in sorted(parties) if agent != task.creator),
            None,
        )
        if contractor is None:
            raise TaskValidationError(
                f"无法从 {sorted(parties)} 里确定承接方"
                f"（任务的发起方是 {task.creator}）"
            )

        task.accept(price, contractor=contractor)

        # 2) 报价历史里记下这次成交
        #
        # 【ACCEPT 是谁做出的 —— 由调用方显式告知】
        #
        #     接收侧（对方接受了我的报价）→ accept_author = message.from_agent
        #     发送侧（我自己决定接受）    → accept_author = message.to_agent
        #
        #     【为什么不在方法里猜】
        #         两种情况下 incoming 都是「我收到的那条消息」，
        #         从消息本身看不出「我是哪一边」——
        #         只有调用方知道。硬猜就要去嗅探 message_type，
        #         那会让「按消息类型分派」散落到业务代码里。
        #
        #         （这个坑 4C 踩过一次：接收侧和发送侧的「行动方」
        #           正好相反。当时的解法也是让调用方显式传 actor。）
        author = accept_author or incoming.from_agent

        # 【ACCEPT 记录该用哪个 message_id】
        #     用**这条 ACCEPT 消息自己**的 id，而不是被接受的那条报价的 id。
        #     因为被接受的那条报价已经在历史里了，用它的 id 会被去重规则挡掉，
        #     结果就是「成交了但报价历史里没有 ACCEPT 记录」。
        #     而被接受的是哪一条，靠 reply_to 表达（协议 §18 要的就是这个关系）。
        task.record_offer(
            OfferType.ACCEPT,
            price,
            message_id=(
                outgoing.message_id if outgoing else incoming.message_id
            ),
            reply_to=accepted_offer_message_id or incoming.reply_to,
            from_agent=author,
            trace_id=incoming.trace_id,
            conversation_id=incoming.conversation_id,
            note="成交",
        )

        self.store.save(task)

        # 3) 成交凭证（不可变、append-only）
        agreement = TaskAgreementRecord(
            agreement_id=self.agreement_id_for(task, accepted_offer_message_id),
            task_id=task.task_id,
            initiator=task.creator,
            contractor=task.contractor or contractor,
            agreed_price=price,
            accepted_offer_message_id=accepted_offer_message_id,
            trace_id=incoming.trace_id,
            conversation_id=incoming.conversation_id,
        )
        self.store.record_agreement(agreement)

        logger.debug(
            "任务 %s 成交：%s @ %s（依据报价 %s）",
            task.task_id, agreement.contractor, price,
            accepted_offer_message_id,
        )

        # 【发送侧需要把 ACCEPT 发出去；接收侧不需要】
        #     判断依据就是「调用方有没有给我一条要发出去的消息」——
        #     不需要去比对 message_type。
        is_sender_side = outgoing is not None

        return HandlerResult(
            ok=True,
            action=HandlerAction.ACCEPTED,
            task_id=task.task_id,
            reply_type=MessageType.ACCEPT if is_sender_side else None,
            reply_payload=(
                {"task_id": task.task_id, "price": price.to_dict()}
                if is_sender_side else None
            ),
            reply_message=outgoing if is_sender_side else None,
            details={
                "status": task.status,
                "agreed_price": str(price),
                "budget": str(task.budget) if task.budget else None,
                "contractor": task.contractor,
                "accepted_offer_message_id": accepted_offer_message_id,
                "accepted_offer_type": accepted_offer_type,
                "agreement_id": agreement.agreement_id,
                "trace_id": agreement.trace_id,
                "conversation_id": agreement.conversation_id,
                # 明确记录这些都没发生
                "payment": None,
                "workspace": None,
                "llm_calls": 0,
            },
        )

    @staticmethod
    def agreement_id_for(
        task: Task, accepted_offer_message_id: Optional[str]
    ) -> str:
        """
        成交凭证的 ID。

        【为什么从「被接受的报价 message_id」推导，而不是随机生成】

            因为双方都会各自生成一份凭证：

                发送侧（我接受你的报价）：被接受的是**你的**报价消息
                接收侧（你接受了我的报价）：reply_to 也指向**同一条**消息

            两边都能看到同一个 message_id，所以推导出的 agreement_id 相同，
            主键冲突让重复写入变成幂等 —— 一个任务只会留下一条成交凭证。

            如果随机生成，同一个任务就会有两份内容相同但 ID 不同的凭证，
            审计时分不清是不是同一笔交易。
        """
        if accepted_offer_message_id:
            return f"agreement_{accepted_offer_message_id}"
        # 没关联消息时（比如手工构造的报价）退回到按任务推导
        return f"agreement_{task.task_id}"

    # ============================================================
    # 六、共用小工具
    # ============================================================
    def record_offer(
        self, task: Task, offer_type: str, amount: Money, message: Message
    ) -> None:
        """
        把一条消息记进报价历史（带完整关联字段）。

        【为什么所有报价都走这一个方法】
            因为关联字段（message_id / reply_to / from_agent /
            trace_id / conversation_id）必须**处处一致** ——
            少了任何一个，链路就断了。
            复制五份迟早会漂移。
        """
        task.record_offer(
            offer_type,
            amount,
            message_id=message.message_id,
            reply_to=message.reply_to,
            from_agent=message.from_agent,
            trace_id=message.trace_id,
            conversation_id=message.conversation_id,
            note=f"来自 {message.from_agent}",
        )

    # ============================================================
    def describe(self) -> Dict[str, Any]:
        return {
            "service": type(self).__name__,
            "policies": sorted(self.policies),
            "tasks": self.store.count(),
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(policies={sorted(self.policies)})"
