"""
Task Message Handlers —— 按消息类型隔离的业务处理
============================================================

【为什么要按消息类型分开】

    在重构之前，四种消息的处理都挤在 TaskService 里，
    每个方法 96~133 行，各自重复做：
        解析 payload → 校验 → 找任务 → 角色校验 → 业务规则 → 改状态 → 造结果

    改一种消息的规则时，很容易碰到另一种消息的代码。

    现在每种消息一个 Handler：

        OfferHandler          TASK_OFFER
        CounterOfferHandler   COUNTER_OFFER
        AcceptHandler         ACCEPT
        RejectHandler         REJECT

    一个 Handler 只处理一种消息，也只有它自己会因为这个消息类型的
    规则变化而需要修改。

【为什么用注册表而不是 if / elif】

    【这是本次重构最重要的「未来扩展点」】

        Step 4D 要加 START / SUBMIT / VERIFY / COMPLETE。
        如果 TaskService 里写的是：

            if message_type == "TASK_OFFER": ...
            elif message_type == "ACCEPT": ...
            elif message_type == "SUBMIT": ...       ← 每次都要改这里

        那么每加一个消息类型都要动 TaskService，它迟早又变成 God Object。

    有了注册表，4D 只需要：

        registry.register(SubmitHandler(negotiation))

    TaskService 一行都不用改。

【Handler 的职责边界】

    ✅ 编排：解析 → 校验 → 落地 → 造结果
    ❌ 不解析 payload     → 交给 TaskMessageMapper
    ❌ 不实现业务规则     → 交给 TaskNegotiationService
    ❌ 不直接改数据库     → 交给 TaskStore
    ❌ 不做经济决定       → 交给 DecisionPolicy

    Handler 是**薄薄的一层编排**，不是业务逻辑的仓库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.task import TaskStatus

from communication.message import Message, MessageType

from services.task_message_mapper import TaskMessageMapper
from services.task_negotiation_service import TaskNegotiationService
from services.task_result import HandlerResult


logger = get_logger(__name__)


# ============================================================
# 抽象 Handler
# ============================================================
class TaskMessageHandler(ABC):
    """
    处理**一种** Task 业务消息。

    【契约】
        · `message_type` 是它负责的消息类型
        · `handle(message)` 必须返回 HandlerResult
        · 它**只返回**要发什么，不自己发消息
          （这样它可以在没有通信层的情况下被测试）
    """

    #: 这个 handler 负责的消息类型
    message_type: str = ""

    def __init__(
        self,
        negotiation: TaskNegotiationService,
        mapper: Optional[TaskMessageMapper] = None,
    ):
        self.negotiation = negotiation
        self.mapper = mapper or negotiation.mapper

    # ------------------------------------------------------------
    def accepts(self, message: Message) -> bool:
        return message.message_type == self.message_type

    def check_type(self, message: Message) -> None:
        """类型不对是**编程错误**，不是业务错误 —— 直接抛。"""
        if not isinstance(message, Message):
            raise TypeError(
                f"{type(self).__name__} 需要 Message，"
                f"收到 {type(message).__name__}"
            )
        if not self.accepts(message):
            raise ValueError(
                f"{type(self).__name__} 负责 {self.message_type}，"
                f"收到的是 {message.message_type}"
            )

    @abstractmethod
    def handle(self, message: Message) -> HandlerResult:
        """处理一条消息。**不产生通信副作用。**"""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


# ============================================================
# TASK_OFFER
# ============================================================
class OfferHandler(TaskMessageHandler):
    """
    处理收到的 TASK_OFFER。

    【做了几件事】
        1. 解析 payload（Mapper）
        2. 校验协议形状 + 货币（NegotiationService）
        3. 在本地建立 / 关联任务
        4. 记进报价历史，状态 → OFFERED

    【刻意没做的一件事】
        ❌ 不自动接受任务。
           处理完的状态是 OFFERED，表示「收到了邀约」，
           而不是 ACCEPTED。接不接由 Agent 自己决定。
    """

    message_type = MessageType.TASK_OFFER

    def handle(self, message: Message) -> HandlerResult:
        self.check_type(message)

        intent = self.mapper.parse_offer(message)
        errors: List[str] = list(intent.errors)

        # 货币是业务规则（协议只规定「金额带 currency」）
        if intent.budget is not None:
            errors.extend(
                self.negotiation.validate_currency(intent.budget, "budget")
            )

        if errors:
            return self.negotiation.reject_message(
                message, errors, [], intent.task_id
            )

        assert intent.task_id and intent.budget and intent.description

        return self.negotiation.open_offer(
            incoming=message,
            task_id=intent.task_id,
            description=intent.description,
            budget=intent.budget,
            workspace_id=intent.workspace_id,
            required_capabilities=intent.required_capabilities,
            acceptance=intent.acceptance,
            expires_at=intent.expires_at,
        )


# ============================================================
# COUNTER_OFFER
# ============================================================
class CounterOfferHandler(TaskMessageHandler):
    """
    处理收到的 COUNTER_OFFER（还价）。

    【业务规则】
        price.currency == AIC
        price <= TASK_OFFER.budget
        amount >= 0（Money 自己保证）

    第二条是**业务规则**，不是通信规则 ——
    通信层继续只负责把消息送到。
    """

    message_type = MessageType.COUNTER_OFFER

    def handle(self, message: Message) -> HandlerResult:
        self.check_type(message)

        intent = self.mapper.parse_counter_offer(message)
        errors: List[str] = list(intent.errors)

        if intent.price is not None:
            errors.extend(
                self.negotiation.validate_currency(intent.price, "price")
            )

        if errors:
            return self.negotiation.reject_message(
                message, errors, [], intent.task_id
            )

        assert intent.task_id and intent.price
        task = self.negotiation.store.get(intent.task_id)
        if task is None:
            return self.negotiation.reject_message(
                message,
                [f"本地没有任务 {intent.task_id} —— 还价必须先有一个已知的任务"],
                [],
                intent.task_id,
            )

        # ---- 幂等（必须排在角色校验之前）----
        if self.negotiation.already_recorded(task, message):
            return self.negotiation.already_processed(task, message)

        # ---- 角色 / 轮次 ----
        # 【行动方是消息的发送者】它在决定要不要还价；
        # 我们是收件人，只是被通知。
        role_errors = self.negotiation.validate_actor(
            task, message.from_agent, message
        )
        if role_errors:
            return self.negotiation.reject_message(
                message, role_errors, [], intent.task_id
            )

        # ---- 业务规则：还价不能超过预算 ----
        price_errors = self.negotiation.validate_counter_price(
            task, intent.price
        )
        if price_errors:
            return self.negotiation.reject_message(
                message, price_errors, [], intent.task_id
            )

        return self.negotiation.apply_counter_offer(
            task, intent.price, incoming=message
        )


# ============================================================
# ACCEPT
# ============================================================
class AcceptHandler(TaskMessageHandler):
    """
    处理收到的 ACCEPT。

    【它只意味着「合同成立」】
        ✅ Task 变成 ACCEPTED，agreed_price 被记录，生成成交凭证
        ❌ 不转账、不写账本、不创建 Workspace、不调模型

    【价格规则】
        ACCEPT.price 必须等于**被接受的那条 Offer** 的价格：
            reply_to → TASK_OFFER      ⇒ price = TASK_OFFER.budget
            reply_to → COUNTER_OFFER   ⇒ price = COUNTER_OFFER.price
    """

    message_type = MessageType.ACCEPT

    def handle(self, message: Message) -> HandlerResult:
        self.check_type(message)

        intent = self.mapper.parse_accept(message)
        if not intent.ok:
            return self.negotiation.reject_message(
                message, list(intent.errors), [], intent.task_id
            )

        assert intent.task_id and intent.price

        # ---- 幂等：这条 ACCEPT 之前处理过吗？（必须排在验证之前）----
        #
        # 【为什么顺序很重要】
        #     发送侧接受之后，任务在本地已经是 ACCEPTED 了。
        #     对方收到 ACCEPT 再走一遍时，会先撞上「任务已是终态」的校验，
        #     被误判成一次非法操作。
        #
        #     但它其实只是**同一条成交消息的接收侧**，应该被识别为重复，
        #     而不是错误。所以幂等判断必须放在角色/轮次验证之前。
        task = self.negotiation.store.get(intent.task_id)
        if task is None:
            return self.negotiation.reject_message(
                message, [f"本地没有任务 {intent.task_id}"], [], intent.task_id
            )

        duplicate = self.negotiation.already_accepted(task, intent.price)
        if duplicate is not None:
            return duplicate

        if self.negotiation.already_recorded(task, message):
            return self.negotiation.already_processed(task, message)

        # ---- 角色 / 轮次 ----
        role_errors = self.negotiation.validate_actor(
            task, message.from_agent, message
        )
        if role_errors:
            return self.negotiation.reject_message(
                message, role_errors, [], intent.task_id
            )

        # ---- 找到被接受的那条报价 ----
        accepted_offer = (
            task.find_offer_by_message(message.reply_to)
            if message.reply_to else None
        )
        if accepted_offer is None:
            return self.negotiation.reject_message(
                message,
                [
                    f"ACCEPT 必须通过 reply_to 指向被接受的那条报价，"
                    f"但 {message.reply_to!r} 不在这个任务的报价历史里。\n"
                    f"（协议 §18：成交必须能反查到具体是哪一条报价）"
                ],
                [],
                intent.task_id,
            )

        # ---- 价格必须等于被接受报价的价格（协议 §14）----
        expected = accepted_offer.amount
        if intent.price != expected:
            return self.negotiation.reject_message(
                message,
                [
                    f"成交价 {intent.price} 与被接受的报价 {expected} 不一致。\n"
                    f"（协议 §14：ACCEPT.price 必须等于 reply_to 指向那条报价的"
                    f"价格。reply_to 指向 {accepted_offer.offer_type}）"
                ],
                [],
                intent.task_id,
            )

        return self.negotiation.apply_accept(
            task,
            intent.price,
            incoming=message,
            accepted_offer_message_id=accepted_offer.message_id,
            accepted_offer_type=accepted_offer.offer_type,
        )


# ============================================================
# REJECT
# ============================================================
class RejectHandler(TaskMessageHandler):
    """
    处理收到的 REJECT。

    【REJECT 是什么】
        「我理解这个业务请求，但不愿意承接。」
        它不是协议错误，也不是通信失败 —— 是**正常的业务行为**。

    【和「协议错误」的区别】
        业务拒绝：价格不合适 → REJECT，任务变成 REJECTED
        协议违规：还价超过预算 → 那条消息**无效**，任务状态不变
        （协议错误走 reject_message，回 ERROR，不动任务状态）
    """

    message_type = MessageType.REJECT

    def handle(self, message: Message) -> HandlerResult:
        self.check_type(message)

        intent = self.mapper.parse_reject(message)
        if not intent.ok:
            return self.negotiation.reject_message(
                message, list(intent.errors), [], intent.task_id
            )

        assert intent.task_id

        task = self.negotiation.store.get(intent.task_id)
        if task is None:
            return self.negotiation.reject_message(
                message, [f"本地没有任务 {intent.task_id}"], [], intent.task_id
            )

        # ---- 幂等：已经拒绝过了吗？（必须排在验证之前）----
        duplicate = self.negotiation.already_rejected(task)
        if duplicate is not None:
            return duplicate

        if self.negotiation.already_recorded(task, message):
            return self.negotiation.already_processed(task, message)

        # ---- 角色 / 轮次 ----
        role_errors = self.negotiation.validate_actor(
            task, message.from_agent, message
        )
        if role_errors:
            return self.negotiation.reject_message(
                message, role_errors, [], intent.task_id
            )

        return self.negotiation.apply_reject(
            task, intent.reason, incoming=message
        )


# ============================================================
# 注册表（未来 4D 的扩展点）
# ============================================================
class TaskHandlerRegistry:
    """
    消息类型 → Handler 的映射。

    【这是 Step 4D 唯一需要碰的地方】

        registry.register(SubmitHandler(negotiation))
        registry.register(VerifyHandler(negotiation))

    门面和组合根都不用改。

    【为什么不用 if / elif】
        见本文件开头的说明：每加一个类型都要改 TaskService，
        它迟早又会变成 God Object。
    """

    def __init__(self, handlers=()):
        self._handlers: Dict[str, TaskMessageHandler] = {}
        for handler in handlers:
            self.register(handler)

    def register(
        self, handler: TaskMessageHandler, replace: bool = False
    ) -> TaskMessageHandler:
        if not isinstance(handler, TaskMessageHandler):
            raise TypeError(
                f"register() 需要 TaskMessageHandler，"
                f"收到 {type(handler).__name__}"
            )
        if not handler.message_type:
            raise ValueError(
                f"{type(handler).__name__} 没有声明 message_type"
            )

        if handler.message_type in self._handlers and not replace:
            raise ValueError(
                f"{handler.message_type} 已经有处理器了："
                f"{type(self._handlers[handler.message_type]).__name__}。\n"
                f"要替换请传 replace=True。"
            )

        self._handlers[handler.message_type] = handler
        logger.debug(
            "已注册处理器：%s → %s",
            handler.message_type, type(handler).__name__,
        )
        return handler

    def get(self, message_type: str) -> Optional[TaskMessageHandler]:
        return self._handlers.get(str(message_type))

    def require(self, message_type: str) -> TaskMessageHandler:
        handler = self.get(message_type)
        if handler is None:
            raise KeyError(
                f"没有处理 {message_type} 的 handler。\n"
                f"已注册的：{self.types()}"
            )
        return handler

    def types(self) -> List[str]:
        return sorted(self._handlers)

    def __contains__(self, message_type: str) -> bool:
        return str(message_type) in self._handlers

    def __len__(self) -> int:
        return len(self._handlers)

    def describe(self) -> Dict[str, Any]:
        return {
            "registry": type(self).__name__,
            "handlers": {
                message_type: type(handler).__name__
                for message_type, handler in sorted(self._handlers.items())
            },
        }

    def __repr__(self) -> str:
        return f"TaskHandlerRegistry({self.types()})"


def default_handler_registry(
    negotiation: TaskNegotiationService,
    mapper: Optional[TaskMessageMapper] = None,
) -> TaskHandlerRegistry:
    """
    构造 Step 4C 默认的四个处理器。

    【Step 4D 要加新类型时，改的是这里 + 组合根】
        比如：
            registry.register(SubmitHandler(negotiation))

        门面（TaskService）不需要任何改动 —— 这就是本次重构要保住的扩展点。
    """
    return TaskHandlerRegistry([
        OfferHandler(negotiation, mapper),
        CounterOfferHandler(negotiation, mapper),
        AcceptHandler(negotiation, mapper),
        RejectHandler(negotiation, mapper),
    ])
