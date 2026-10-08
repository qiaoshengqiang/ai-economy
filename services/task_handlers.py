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

from core.task_execution import ExecutionEvent

from services.task_application_service import TaskApplicationService
from services.task_execution_service import TaskExecutionService
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
        service: "TaskApplicationService",
        mapper: Optional[TaskMessageMapper] = None,
    ):
        """
        【为什么参数叫 service 而不是 negotiation】
            Step 4D 引入了第二个应用服务（执行）。
            两类 handler 的骨架完全一样 —— 都需要
            「一个能落地状态的 service」+「一个能解析的 mapper」。

            所以基类只认抽象的 service，具体是哪一个
            由两个语义化的中间基类声明（见下）。
        """
        self.service = service
        self.mapper = mapper or service.mapper

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


class NegotiationMessageHandler(TaskMessageHandler):
    """
    议价消息的 handler。

    【它存在的意义】
        声明「我的 service 一定是 TaskNegotiationService」，
        并把它暴露成语义化的 self.negotiation ——
        这样四个议价 handler 里的代码读起来是
        「问协商服务」，而不是笼统的「问 service」。
    """

    def __init__(
        self,
        negotiation: "TaskNegotiationService",
        mapper: Optional[TaskMessageMapper] = None,
    ):
        super().__init__(negotiation, mapper)
        self.negotiation = negotiation


# ============================================================
# TASK_OFFER
# ============================================================
class OfferHandler(NegotiationMessageHandler):
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
class CounterOfferHandler(NegotiationMessageHandler):
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
class AcceptHandler(NegotiationMessageHandler):
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
            # 【接收侧】我们收到的是**对方**发来的 ACCEPT，
            # 所以「做出接受这个动作的人」是消息的发送者。
            accept_author=message.from_agent,
        )


# ============================================================
# REJECT
# ============================================================
class RejectHandler(NegotiationMessageHandler):
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
# 执行阶段（Step 4D）
#
# 【四个 handler 的骨架完全一样】
#
#     校验消息类型 → 解析 → 取任务 → 幂等 → 业务校验 → 落地
#
# 所以公共骨架放在 ExecutionMessageHandler 里，
# 每个具体 handler 只写「自己特有的那两件事」：
#
#     parse()        这条消息解析成什么（用 Mapper）
#     extra_errors() 这条消息特有的业务校验
#     apply()        怎么落地（用 ExecutionService）
#
# 【这就是上一轮重构买来的东西】
#     加四种消息类型，TaskService 一行没改；
#     每个新 handler 只有十几行。
# ============================================================
class ExecutionMessageHandler(TaskMessageHandler):
    """
    执行阶段消息的 handler 骨架。

    【它不做什么 —— 和议价 handler 一样】
        ❌ 不解析 payload      → Mapper
        ❌ 不实现业务规则      → TaskExecutionService
        ❌ 不直接改数据库      → TaskStore
        ❌ 不发消息            → 只返回 HandlerResult
        ❌ 不执行代码、不调 LLM、不碰钱、不碰 Workspace
    """

    #: 这个 handler 对应哪个执行事件（见 core.task_execution.ExecutionEvent）
    event_type: str = ""

    def __init__(
        self,
        execution: TaskExecutionService,
        mapper: Optional[TaskMessageMapper] = None,
    ):
        super().__init__(execution, mapper)
        self.execution = execution

    # ------------------------------------------------------------
    # 子类要实现的三个钩子
    # ------------------------------------------------------------
    @abstractmethod
    def parse(self, message: Message):
        """把消息解析成 intent（用 Mapper）。"""

    def extra_errors(self, task: Any, intent: Any) -> List[str]:
        """
        这条消息**特有**的业务校验。

        默认没有。需要的话由子类覆盖
        （比如 SUBMIT 要求 result_ref 必填）。
        """
        return []

    @abstractmethod
    def apply(self, task: Any, intent: Any, message: Message) -> HandlerResult:
        """落地（用 ExecutionService）。"""

    # ------------------------------------------------------------
    def handle(self, message: Message) -> HandlerResult:
        self.check_type(message)

        # ---- 1) 解析 ----
        intent = self.parse(message)
        if not intent.ok:
            return self.execution.reject_message(
                message, list(intent.errors), [], intent.task_id
            )

        # ---- 2) 取任务 ----
        task, error = self.execution.load_task(message, intent.task_id)
        if error is not None:
            return error

        # ---- 3) 幂等（必须排在状态 / 角色校验之前）----
        #
        # 【顺序为什么重要】
        #     发送侧落地之后任务状态已经变了。
        #     对方收到同一条消息再走一遍时，会先撞上「状态不允许」，
        #     被误判成一次非法操作 ——
        #     但它其实只是**同一条消息的接收侧**，应该识别为重复。
        if self.execution.already_recorded(task, message):
            return self.execution.already_processed(task, message)

        # ---- 4) 业务校验 ----
        #
        # 【两类错误一起报】
        #     特有的（result_ref 缺失）+ 通用的（状态 / 角色）
        #     一次全告诉对方，免得它改一条跑一次。
        errors = self.extra_errors(task, intent)
        errors.extend(
            self.execution.validate(
                task, message.from_agent, self.event_type, message
            )
        )
        if errors:
            return self.execution.reject_message(
                message, errors, [], intent.task_id
            )

        # ---- 5) 落地 ----
        return self.apply(task, intent, message)


class StartHandler(ExecutionMessageHandler):
    """
    START —— 承接方说「我开工了」。

        ACCEPTED → IN_PROGRESS

    【为什么开工要单独发一条消息】
        发起方需要知道承接方**确实开始做了**。
        如果只看状态推断，发起方只能靠猜 ——
        而「已成交」和「已在做」是两件不同的事
        （承接方可能一直没开始）。
    """

    message_type = MessageType.START
    event_type = ExecutionEvent.START

    def parse(self, message: Message):
        return self.mapper.parse_start(message)

    def apply(self, task, intent, message: Message) -> HandlerResult:
        return self.execution.apply_start(task, incoming=message)


class SubmitHandler(ExecutionMessageHandler):
    """
    SUBMIT —— 承接方说「我交付了这份成果」。

        IN_PROGRESS → SUBMITTED

    【成果引用是必填的】
        不能只发「我做好了」：发起方需要知道
        **验收的是哪一份成果**，否则验收结论没有对象。

    【本阶段不上传文件】
        result_ref 只是一个逻辑名字（比如 "analysis-v2"）。
        它**不是**路径、不是权限、不是 URL、不是 Git 引用。
        TaskResultRef 会拒绝长得像路径或 URL 的值 ——
        那是一道安全边界，见 core/task_execution.py。
    """

    message_type = MessageType.SUBMIT
    event_type = ExecutionEvent.SUBMIT

    def parse(self, message: Message):
        return self.mapper.parse_submit(message)

    def extra_errors(self, task, intent) -> List[str]:
        errors = list(self.execution.validate_result(intent))
        # Completion Proposal 的五条校验（规格 §3）——
        # 挂在这一条 SUBMIT 上，不新开事件。
        proposal = getattr(intent, "proposal", None)
        if proposal is not None:
            errors.extend(
                self.execution.validate_completion_proposal(task, proposal))
        return errors

    def apply(self, task, intent, message: Message) -> HandlerResult:
        return self.execution.apply_submit(
            task, intent.result, incoming=message,
            proposal=getattr(intent, "proposal", None),
        )


class VerifyHandler(ExecutionMessageHandler):
    """
    VERIFY —— 发起方说「验收通过 / 不通过」。

        passed=True   → VERIFIED
        passed=False  → IN_PROGRESS（返工）

    【只有发起方能发】
        承接方不能自己宣布「我验收自己通过」——
        干活的不能既当运动员又当裁判。
        这条规则在 ExecutionService.validate_role 里，有守卫。

    【第一版是人工验收】
        passed 由发起方显式给出。
        **不会**收到 SUBMIT 就自动跑测试或问模型。
    """

    message_type = MessageType.VERIFY
    event_type = ExecutionEvent.VERIFY

    def parse(self, message: Message):
        return self.mapper.parse_verify(message)

    def extra_errors(self, task, intent) -> List[str]:
        return self.execution.validate_verification(intent)

    def apply(self, task, intent, message: Message) -> HandlerResult:
        return self.execution.apply_verify(
            task, intent.verification, incoming=message
        )


class CompleteHandler(ExecutionMessageHandler):
    """
    COMPLETE —— 发起方说「任务正式结束」。

        VERIFIED → COMPLETED

    【它只结束生命周期】
        ❌ 不付款、不转账、不释放托管、不结算
        ❌ 不动 Wallet / Ledger

        「活干完了」和「钱给了」是两件事。
        结算是一个独立的后续阶段，它可以引用
        TaskAgreementRecord 里约定的 agreed_price。
    """

    message_type = MessageType.COMPLETE
    event_type = ExecutionEvent.COMPLETE

    def parse(self, message: Message):
        return self.mapper.parse_complete(message)

    def apply(self, task, intent, message: Message) -> HandlerResult:
        return self.execution.apply_complete(task, incoming=message)


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
    execution: Optional[TaskExecutionService] = None,
    mapper: Optional[TaskMessageMapper] = None,
) -> TaskHandlerRegistry:
    """
    构造默认的处理器集合。

    【Step 4D 加执行 handler 时改了什么】
        就只改了这个函数 —— 加了四行 register。
        TaskService（门面）**一行都没改**。

        这正是上一轮重构要保住的扩展点：加消息类型不用碰门面。

    【参数 execution 为什么是可选的】
        保留 4C 的调用方式（只传 negotiation）仍然可用 ——
        那时注册表只有四个议价 handler。
        这让已有的测试和调用方不用改。
    """
    handlers = [
        # ---- 议价（Step 4C）----
        OfferHandler(negotiation, mapper),
        CounterOfferHandler(negotiation, mapper),
        AcceptHandler(negotiation, mapper),
        RejectHandler(negotiation, mapper),
    ]

    # ---- 执行（Step 4D）----
    if execution is not None:
        handlers.extend([
            StartHandler(execution, mapper),
            SubmitHandler(execution, mapper),
            VerifyHandler(execution, mapper),
            CompleteHandler(execution, mapper),
        ])

    return TaskHandlerRegistry(handlers)
