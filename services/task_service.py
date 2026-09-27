"""
Task Service —— 消息进入业务领域的适配层
============================================================

【这个文件在架构里的位置】

        Communication
             ↓
        Task Service          ← 本文件（适配层）
             ↓
        Task Core / TaskStore

    它的职责只有一件：

        把一条**通信消息**翻译成对**任务领域**的操作。

    消息是「字节 + 信封」，任务是「业务状态」。
    这两个世界必须有一个地方对接，本文件就是那个地方。

【为什么单独一个 services/ 包，而不是放进 communication/】

    communication/ 的职责是「把消息送到」。
    它必须能在**完全不知道 Task 存在**的情况下工作 ——
    否则「通信层」就不能被单独使用、单独测试、单独替换了。
    （有架构守卫盯着这条线：communication/ 不许 import core.task）

    所以适配层放在外面：

        services/  →  core/  +  communication/  +  storage/
        core/      →  不依赖任何上层
        communication/  →  不依赖 services/

    依赖是单向的，没有环。

【这个文件刻意不做的事】

    ❌ 不自动接受任务
       §23：能力匹配到 B，**不代表** B 会接这个任务。
       B 自己判断，然后回 ACCEPT / COUNTER_OFFER / REJECT。
       所以成功处理一条 TASK_OFFER 之后，任务状态是 OFFERED，不是 ACCEPTED。

    ❌ 不主动发消息
       本文件只**返回**应答内容，由调用方（组合根接的处理器）决定发不发。
       这样它可以在没有任何通信层的情况下被测试。

    ❌ 不决定选谁
       candidates_for() 只返回候选列表。选谁是 Task Initiator Agent 的事。
       这里没有 best_agent()、没有排序、没有打分。

    ❌ 不碰钱、不碰 Workspace
       只记录经济约定（budget / price），不转账、不授权。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.agent_directory import AgentDirectory, DirectoryEntry
from core.capability import CapabilityProfile
from core.decision_policy import (
    Decision,
    DecisionContext,
    DecisionPolicy,
    DecisionType,
)
from core.logging_setup import get_logger
from core.money import DEFAULT_CURRENCY, Money, MoneyError
from core.task import (
    InvalidResponderError,
    NotYourTurnError,
    OfferType,
    Task,
    TaskRoleError,
    TaskStatus,
    TaskValidationError,
)
from core.task_agreement import TaskAgreementRecord
from core.task_store import TaskStore

from communication.message import Message, MessageType

logger = get_logger(__name__)


# ============================================================
# 处理结果
# ============================================================
@dataclass(frozen=True)
class HandlerResult:
    """
    处理一条业务消息的结果。

    【为什么要返回一个结构，而不是直接回消息】
        因为「处理业务」和「发送消息」是两件事。
        本文件只做前者 —— 发不发、发什么类型，由调用方决定。

        这也让它可以在没有通信层的情况下被测试。
    """

    ok: bool

    #: "created"（新建了本地任务）/ "linked"（关联到已有任务）/ "rejected"
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


# ============================================================
# 校验错误信息
# ============================================================
def _money_from_payload(raw: Any, field_name: str) -> Money:
    """
    从 payload 里取一个金额。

    协议 §7.1 要求金额是 {amount, currency}，不是裸数字。
    这里给出明确的中文错误信息 —— 因为这是最常犯的协议错误。
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raise ValueError(
            f"{field_name} 必须是 {{amount, currency}} 形式的对象，"
            f"不能是裸数字 {raw!r}。\n"
            f"（裸数字没有货币语义 —— 见 PROTOCOL.md 7.1）"
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"{field_name} 必须是 {{amount, currency}} 形式的对象，"
            f"收到 {type(raw).__name__}"
        )
    try:
        return Money.from_dict(raw)
    except MoneyError as exc:
        raise ValueError(f"{field_name} 不合法：{exc}") from exc


# ============================================================
# Task Service
# ============================================================
class TaskService:
    """
    任务业务服务。

    两个方向的职责：

        【委派侧】candidates_for(task)
            根据任务要求的能力，问目录「谁可能行」。
            只返回候选，不选人。

        【接收侧】handle_offer(message) / handle_counter_offer(message)
            把收到的经济消息翻译成任务领域操作。

    用法：
        service = TaskService(store, directory)

        # 委派侧
        candidates = service.candidates_for(task)     # A 自己从中挑

        # 接收侧（由组合根接到通信层的类型处理器上）
        result = service.handle_offer(message)
    """

    def __init__(
        self,
        store: TaskStore,
        directory: Optional[AgentDirectory] = None,
        policies: Optional[Mapping[str, DecisionPolicy]] = None,
    ):
        self.store = store
        self.directory = directory

        # 每个 Agent 自己的决策策略（Step 4C）。
        #
        # 【为什么是一本「按 agent_id 查」的字典，而不是一个全局策略】
        #     因为策略是 Agent 的**私有经济参数**：
        #         B 的最低价是 20，C 的可能是 50
        #     如果只有一个全局策略，就必然要写
        #         if agent_id == "agent_b": ...
        #     而那正是 §6 / §17 明令禁止的。
        #
        #     有了这本字典，策略由**组合根**按每个 Agent 的配置构造，
        #     TaskService 只负责查表 —— 它不认识任何具体 Agent。
        self.policies: Dict[str, DecisionPolicy] = dict(policies or {})

    # ============================================================
    # 委派侧：找候选（**不选人**）
    # ============================================================
    def candidates_for(self, task: Task) -> List[DirectoryEntry]:
        """
        找出有能力承接这个任务的候选 Agent。

        【它只做一件事】
            把 task.required_capabilities 交给目录，拿回列表。

        【它刻意不做的事】
            · 不排序（除了一致的 agent_id 顺序）
            · 不评分
            · 不返回「最佳人选」
            · 不自动发 TASK_OFFER

        选谁是发起方 Agent 的决定 —— 这是 AI Economy 的核心行为。
        """
        if self.directory is None:
            raise RuntimeError(
                "TaskService 没有配置 AgentDirectory，无法查找候选 Agent。\n"
                "（构造时传入 directory=...）"
            )

        return self.directory.find_capable(task.required_capabilities)

    def can_be_done_by(self, task: Task, agent_id: str) -> bool:
        """
        某个 Agent 声明的能力是否覆盖这个任务的要求。

        注意：这**不是**「它会不会接」——
        只是「它声称自己会做」。
        """
        if self.directory is None:
            raise RuntimeError("TaskService 没有配置 AgentDirectory")

        entry = self.directory.get(agent_id)
        if entry is None:
            return False
        return entry.covers(task.required_capabilities)

    def explain_candidates(self, task: Task) -> Dict[str, Any]:
        """
        解释「为什么这些人是候选、别人为什么不是」。

        这不是决策辅助 —— 它不排序、不推荐。
        它只是把一个**布尔判断**展开成可读的信息，
        方便日志和演示理解匹配过程。
        """
        if self.directory is None:
            raise RuntimeError("TaskService 没有配置 AgentDirectory")

        required = task.required_capabilities
        candidates = []
        excluded = []

        for entry in self.directory.all():
            if entry.covers(required):
                candidates.append({
                    "agent_id": entry.agent_id,
                    "capabilities": entry.capabilities.names,
                })
            else:
                excluded.append({
                    "agent_id": entry.agent_id,
                    "capabilities": entry.capabilities.names,
                    "missing": sorted(entry.missing(required)),
                })

        return {
            "required": required.names,
            "candidates": candidates,
            "excluded": excluded,
        }

    # ============================================================
    # 接收侧：处理 TASK_OFFER
    # ============================================================
    def handle_offer(self, message: Message) -> HandlerResult:
        """
        处理收到的 TASK_OFFER。

        【做了四件事】
            1. 校验 payload（协议形状 + 货币 + 金额）
            2. 在本地建立/关联这个任务
            3. 把 message_id 记进报价历史
            4. 把任务的 current_trace_id 对齐到消息的 trace_id

        【刻意没做的第五件事】
            ❌ 不自动接受任务。
               处理完的状态是 OFFERED，表示「收到了邀约」，
               而不是 ACCEPTED。接不接由 Agent 自己决定（§23）。
        """
        if not isinstance(message, Message):
            raise TypeError(f"handle_offer 需要 Message，收到 {type(message).__name__}")

        if message.message_type != MessageType.TASK_OFFER:
            raise ValueError(
                f"handle_offer 收到的是 {message.message_type}，不是 TASK_OFFER"
            )

        payload = dict(message.payload)
        errors: List[str] = []
        warnings: List[str] = []

        # ---------- 1) 校验 ----------
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append("缺少 task_id（任务发起方必须给出任务标识）")
            task_id = None

        description = payload.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append("缺少 description（协议 §7.2 要求任务描述）")

        budget: Optional[Money] = None
        if "budget" not in payload:
            errors.append("缺少 budget（协议 §7.2 要求给出预算上限）")
        else:
            try:
                budget = _money_from_payload(payload["budget"], "budget")
            except ValueError as exc:
                errors.append(str(exc))

        # 货币校验（§19：budget.currency == AIC）
        if budget is not None and budget.currency != DEFAULT_CURRENCY:
            errors.append(
                f"budget.currency 必须是 {DEFAULT_CURRENCY}，"
                f"收到 {budget.currency!r}"
            )

        if errors:
            return self._reject_offer(message, errors, warnings, task_id)

        # ---------- 2) 建立 / 关联本地任务 ----------
        assert task_id is not None and budget is not None

        existing = self.store.get(task_id)
        created = existing is None

        if created:
            task = Task.create(
                task_id=task_id,
                creator=message.from_agent,
                description=description,
                budget=budget,
                workspace_id=payload.get("workspace_id"),
                required_capabilities=payload.get("required_capabilities"),
                acceptance=payload.get("acceptance"),
                expires_at=payload.get("expires_at"),
                # 【关键的缝合点】本地任务用**消息的** trace_id。
                # 这样从通信日志查到的链路，和任务是同一条。
                trace_id=message.trace_id,
                participants=[message.from_agent, message.to_agent],
            )
            trace_synced = True
        else:
            task = existing
            trace_synced = self._sync_trace(task, message)

            if task.budget is not None and task.budget != budget:
                warnings.append(
                    f"这条 offer 的预算 {budget} 与本地记录的 {task.budget} 不同；"
                    f"保留本地值（发起方的预算以第一次声明为准）"
                )

        # ---------- 3) 记录报价历史（带 message_id）----------
        task.record_offer(
            OfferType.TASK_OFFER,
            budget,
            message_id=message.message_id,
            reply_to=message.reply_to,
            from_agent=message.from_agent,
            trace_id=message.trace_id,
            conversation_id=message.conversation_id,
            note=f"来自 {message.from_agent}",
        )

        # ---------- 4) 状态：CREATED → OFFERED ----------
        # 【注意这里是 OFFERED 而不是 ACCEPTED】
        #     收到邀约 ≠ 接受邀约。接不接由 Agent 自己判断。
        if task.status == TaskStatus.CREATED:
            task.offer()

        self.store.save(task)

        logger.debug(
            "处理 TASK_OFFER：task=%s from=%s message=%s",
            task.task_id, message.from_agent, message.message_id,
        )

        return HandlerResult(
            ok=True,
            action="created" if created else "linked",
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
                "message_id": message.message_id,
                "trace_id": message.trace_id,
                "conversation_id": message.conversation_id,
                "trace_synced": trace_synced,
                "offers": len(task.offers),
            },
        )

    # ============================================================
    # 接收侧：处理 COUNTER_OFFER
    # ============================================================
    def handle_counter_offer(self, message: Message) -> HandlerResult:
        """
        处理收到的 COUNTER_OFFER（还价）。§11

        【业务规则】§14
            price.currency == AIC
            price <= TASK_OFFER.budget
            amount >= 0（Money 自己保证）

        第二条是**业务规则**，不是通信规则 ——
        所以它属于这里，不属于 Communication 层（§15）。
        Communication 继续只负责把消息送到。
        """
        if not isinstance(message, Message):
            raise TypeError("handle_counter_offer 需要 Message")
        if message.message_type != MessageType.COUNTER_OFFER:
            raise ValueError(
                f"handle_counter_offer 收到的是 {message.message_type}"
            )

        payload = dict(message.payload)
        responder = message.to_agent
        errors: List[str] = []

        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append("缺少 task_id")
            task_id = None

        price: Optional[Money] = None
        if "price" not in payload:
            errors.append("缺少 price（还价必须给出价格）")
        else:
            try:
                price = _money_from_payload(payload["price"], "price")
            except ValueError as exc:
                errors.append(str(exc))

        if price is not None and price.currency != DEFAULT_CURRENCY:
            errors.append(
                f"price.currency 必须是 {DEFAULT_CURRENCY}，"
                f"收到 {price.currency!r}"
            )

        if errors:
            return self._reject_offer(message, errors, [], task_id)

        assert task_id is not None and price is not None

        task = self.store.get(task_id)
        if task is None:
            return self._reject_offer(
                message,
                [f"本地没有任务 {task_id} —— 还价必须先有一个已知的任务"],
                [],
                task_id,
            )

        # ---- 角色 / 轮次验证（§16 / §17）----
        # ---- 幂等：这条消息已经记过了吗？（排在角色校验之前）----
        if self._already_recorded(task, message):
            return self._already_processed(task, message)

        # 【行动方是消息的发送者】它在决定要不要还价 / 接受 / 拒绝；
        # 我们是收件人，只是被通知。
        role_errors = self._validate_response(
            task, message.from_agent, message
        )
        if role_errors:
            return self._reject_offer(message, role_errors, [], task_id)

        # ---- 业务规则：还价不能超过预算（§14）----
        if task.budget is not None:
            if price.currency != task.budget.currency:
                errors.append(
                    f"还价货币 {price.currency} 与预算货币 "
                    f"{task.budget.currency} 不一致"
                )
            elif price > task.budget:
                errors.append(
                    f"还价 {price} 超过预算上限 {task.budget}。\n"
                    f"预算（budget）是发起方愿意支付的上限，"
                    f"报价不能超过它。"
                )

        if errors:
            return self._reject_offer(message, errors, [], task_id)

        self._sync_trace(task, message)

        # ---- 记进报价历史（§11：message_id / reply_to / trace / conversation）----
        task.record_offer(
            OfferType.COUNTER_OFFER,
            price,
            message_id=message.message_id,
            reply_to=message.reply_to,
            from_agent=message.from_agent,
            trace_id=message.trace_id,
            conversation_id=message.conversation_id,
            note=f"来自 {message.from_agent}",
        )

        # OFFERED → NEGOTIATING；已经在 NEGOTIATING 就保持（§11）
        if task.status in (TaskStatus.OFFERED, TaskStatus.NEGOTIATING):
            task.negotiate()

        self.store.save(task)

        return HandlerResult(
            ok=True,
            action="negotiating",
            task_id=task.task_id,
            reply_type=None,
            reply_payload=None,
            details={
                "status": task.status,
                "price": str(price),
                "budget": str(task.budget) if task.budget else None,
                "offers": len(task.offers),
                "round": task.negotiation_round_for(message.from_agent),
                "message_id": message.message_id,
                "reply_to": message.reply_to,
                "from_agent": message.from_agent,
            },
        )

    # ============================================================
    # 接收侧：处理 ACCEPT（§9 / §10）
    # ============================================================
    def handle_accept(self, message: Message) -> HandlerResult:
        """
        处理收到的 ACCEPT。

        【它只意味着「合同成立」】（§20）
            ✅ Task 变成 ACCEPTED，agreed_price 被记录，生成成交凭证
            ❌ 不转账、不写账本、不创建 Workspace、不调模型

        【§14 的价格规则】
            ACCEPT.price 必须等于**被接受的那条 Offer** 的价格：
                reply_to → TASK_OFFER      ⇒ price = TASK_OFFER.budget
                reply_to → COUNTER_OFFER   ⇒ price = COUNTER_OFFER.price
        """
        if not isinstance(message, Message):
            raise TypeError("handle_accept 需要 Message")
        if message.message_type != MessageType.ACCEPT:
            raise ValueError(f"handle_accept 收到的是 {message.message_type}")

        payload = dict(message.payload)
        responder = message.to_agent
        errors: List[str] = []

        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append("缺少 task_id")
            task_id = None

        price: Optional[Money] = None
        if "price" not in payload:
            errors.append("缺少 price（ACCEPT 必须给出成交价）")
        else:
            try:
                price = _money_from_payload(payload["price"], "price")
            except ValueError as exc:
                errors.append(str(exc))

        if errors:
            return self._reject_offer(message, errors, [], task_id)

        assert task_id is not None and price is not None

        task = self.store.get(task_id)
        if task is None:
            return self._reject_offer(
                message, [f"本地没有任务 {task_id}"], [], task_id
            )

        # ---- 幂等：这条 ACCEPT 之前处理过吗？（必须排在验证之前）----
        #
        # 【为什么顺序很重要】
        #     发送侧接受之后，任务在本地已经是 ACCEPTED 了。
        #     对方收到 ACCEPT 再走一遍时，会先撞上「任务已是终态」的校验，
        #     被误判成一次非法操作。
        #
        #     但它其实只是**同一条成交消息的接收侧**，应该被识别为重复，
        #     而不是错误。所以幂等判断必须放在角色/轮次验证之前。
        if task.status == TaskStatus.ACCEPTED:
            existing = self.store.latest_agreement(task.task_id)
            if existing is not None and existing.agreed_price == price:
                return HandlerResult(
                    ok=True,
                    action="already_accepted",
                    task_id=task.task_id,
                    details={
                        "status": task.status,
                        "agreement_id": existing.agreement_id,
                        "agreed_price": str(existing.agreed_price),
                        "accepted_offer_message_id": (
                            existing.accepted_offer_message_id
                        ),
                        "note": "这次成交之前已经记录过（幂等，不重复记账）",
                    },
                )

        # ---- 角色 / 轮次验证 ----
        # 【行动方是消息的发送者】它在决定要不要还价 / 接受 / 拒绝；
        # 我们是收件人，只是被通知。
        role_errors = self._validate_response(
            task, message.from_agent, message
        )
        if role_errors:
            return self._reject_offer(message, role_errors, [], task_id)

        # ---- 找到被接受的那条报价（§18）----
        accepted_offer = (
            task.find_offer_by_message(message.reply_to)
            if message.reply_to else None
        )

        if accepted_offer is None:
            return self._reject_offer(
                message,
                [
                    f"ACCEPT 必须通过 reply_to 指向被接受的那条报价，"
                    f"但 {message.reply_to!r} 不在这个任务的报价历史里。\n"
                    f"（§18：成交必须能反查到具体是哪一条报价）"
                ],
                [],
                task_id,
            )

        # ---- §14：成交价必须等于被接受报价的价格 ----
        expected = accepted_offer.amount
        if price != expected:
            return self._reject_offer(
                message,
                [
                    f"成交价 {price} 与被接受的报价 {expected} 不一致。\n"
                    f"（协议 §14：ACCEPT.price 必须等于 reply_to 指向那条报价的价格。"
                    f"reply_to 指向 {accepted_offer.offer_type}）"
                ],
                [],
                task_id,
            )

        return self._accept_locally(
            task, price,
            incoming=message,
            accepted_offer_message_id=accepted_offer.message_id,
            accepted_offer_type=accepted_offer.offer_type,
        )

    # ============================================================
    # 接收侧：处理 REJECT（§12）
    # ============================================================
    def handle_reject(self, message: Message) -> HandlerResult:
        """
        处理收到的 REJECT。

        【REJECT 是什么】（§12 / §13）
            「我理解这个业务请求，但不愿意承接。」
            它不是协议错误，也不是通信失败 —— 是**正常的业务行为**。

        【和「协议错误」的区别】（§13）
            业务拒绝：价格不合适 → REJECT，任务变成 REJECTED
            协议违规：还价超过预算 → 那条消息**无效**，任务状态不变
            （协议错误走 _reject_offer 那条路，回 ERROR，不动任务状态）
        """
        if not isinstance(message, Message):
            raise TypeError("handle_reject 需要 Message")
        if message.message_type != MessageType.REJECT:
            raise ValueError(f"handle_reject 收到的是 {message.message_type}")

        payload = dict(message.payload)
        responder = message.to_agent

        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return self._reject_offer(message, ["缺少 task_id"], [], None)

        task = self.store.get(task_id)
        if task is None:
            return self._reject_offer(
                message, [f"本地没有任务 {task_id}"], [], task_id
            )

        # ---- 幂等：已经拒绝过了吗？（必须排在验证之前）----
        #
        # 【为什么顺序很重要】
        #     对方重发一条 REJECT（或发送侧已经拒绝过、接收侧又收到），
        #     任务已经是终态。如果先做验证，会撞上「不能响应终态任务」
        #     被误判成一次非法操作。
        #
        #     但它其实只是同一个业务动作的重复，应被识别为幂等。
        #     （和 handle_accept 的幂等检查同一个道理。）
        if task.status == TaskStatus.REJECTED:
            return HandlerResult(
                ok=True,
                action="already_rejected",
                task_id=task.task_id,
                details={
                    "status": task.status,
                    "note": "这次拒绝之前已经记录过（幂等）",
                },
            )

        # ---- 幂等：这条 REJECT 已经记过了吗？（排在角色校验之前）----
        if self._already_recorded(task, message):
            return self._already_processed(task, message)

        # 【行动方是消息的发送者】它在决定要不要还价 / 接受 / 拒绝；
        # 我们是收件人，只是被通知。
        role_errors = self._validate_response(
            task, message.from_agent, message
        )
        if role_errors:
            return self._reject_offer(message, role_errors, [], task_id)

        self._sync_trace(task, message)

        reason = payload.get("reason")
        task.record_offer(
            OfferType.REJECT,
            task.budget if task.budget else Money(0),
            message_id=message.message_id,
            reply_to=message.reply_to,
            from_agent=message.from_agent,
            trace_id=message.trace_id,
            conversation_id=message.conversation_id,
            note=reason or f"来自 {message.from_agent} 的拒绝",
        )
        task.reject(reason)
        self.store.save(task)

        logger.debug("任务 %s 被 %s 拒绝：%s", task_id, message.from_agent, reason)

        return HandlerResult(
            ok=True,
            action="rejected",
            task_id=task.task_id,
            reply_type=None,
            reply_payload=None,
            details={
                "status": task.status,
                "reason": reason,
                "from_agent": message.from_agent,
                # 明确记录：拒绝不产生任何经济后果
                "payment": None,
                "workspace": None,
            },
        )

    def _already_processed(
        self, task: Task, message: Message
    ) -> HandlerResult:
        """
        这条消息之前已经处理过了 —— 幂等返回，不改任何状态。

        【它不是错误】
            重复投递在分布式系统里是常态（重发、重试、网络抖动）。
            正确的反应是「认出来，什么也不做」，而不是报错。
        """
        logger.debug(
            "任务 %s 已经处理过消息 %s，跳过",
            task.task_id, message.message_id,
        )
        return HandlerResult(
            ok=True,
            action="already_processed",
            task_id=task.task_id,
            reply_type=None,
            reply_payload=None,
            details={
                "status": task.status,
                "message_id": message.message_id,
                "note": "这条消息之前已经记录过（幂等，未重复处理）",
            },
        )

    # ============================================================
    # 消息级幂等（比角色校验更靠前的一道门）
    # ============================================================
    @staticmethod
    def _already_recorded(task: Task, message: Message) -> bool:
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

    # ============================================================
    # 角色 / 轮次验证（§16 / §17）
    # ============================================================
    def _validate_response(
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

        【为什么规则 3 是必要的】（§16）
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
    # 决策：问策略（纯函数，不改任何状态）
    # ============================================================
    def policy_for(self, agent_id: str) -> Optional[DecisionPolicy]:
        """
        取出某个 Agent 自己的决策策略。

        【注意这里不是「系统的策略」】
            每个 Agent 有自己的经济参数（最低价、目标价……）。
            本方法只是查表 —— TaskService 不认识任何具体 Agent，
            也不包含任何价格规则（§31：不许把 B 的价格策略写进 TaskService）。
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
            这样换成任何两个 Agent 都成立（§17 禁止硬编码 A / B）。
        """
        offered = self._offered_price_from(message, task)

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

    @staticmethod
    def _offered_price_from(message: Message, task: Task) -> Money:
        """
        从消息里取出「对方这次给的价格」。

        【TASK_OFFER 为什么用 budget】
            协议 §14 规定：reply_to 指向 TASK_OFFER 时，接受即按 budget 成交。
            所以对策略来说，TASK_OFFER 报出来的就是这个 budget。
        """
        payload = dict(message.payload)
        if message.message_type == MessageType.TASK_OFFER:
            return _money_from_payload(payload["budget"], "budget")
        return _money_from_payload(payload["price"], "price")

    def decide(self, message: Message) -> Decision:
        """
        问策略「我该怎么办」。

        【这个方法**不修改任何状态**】
            不改 Task、不改报价历史、不发消息。
            这样决策可以被单独测试、记录、重放。

            §8：Decision Policy 回答「我要 ACCEPT 还是 COUNTER_OFFER」，
                修改状态是 TaskService / Task Core 的事。
        """
        responder = message.to_agent
        payload = dict(message.payload)
        task_id = payload.get("task_id")

        task = self.store.get(task_id) if task_id else None
        if task is None:
            raise TaskValidationError(
                f"本地没有任务 {task_id!r}，无法决策"
            )

        policy = self.policy_for(responder)
        if policy is None:
            raise TaskValidationError(
                f"{responder} 没有配置决策策略（DecisionPolicy）。\n"
                f"每个 Agent 的经济参数是它自己的，必须由组合根按配置构造。"
            )

        context = self.build_context(task, responder, message)
        return policy.evaluate(context)

    # ============================================================
    # 决策的编排：问策略 → 执行（§28）
    # ============================================================
    def respond(self, message: Message) -> HandlerResult:
        """
        收到一条需要回应的报价 → 问策略 → 执行决策。

        【这是编排，不是决策本身】
            真正的决定由 Agent 自己的 DecisionPolicy 做出；
            这里只负责「问」和「把决定变成合法的业务操作」。
        """
        decision = self.decide(message)
        logger.debug(
            "任务 %s 的决策：%s（策略 %s）",
            message.payload.get("task_id"), decision, decision.policy,
        )
        return self.execute(message, decision)

    def execute(self, message: Message, decision: Decision) -> HandlerResult:
        """
        把一个已经确定的决策落到**本地**任务上，并返回要给对方的应答。

        【这里有一个容易搞混的地方，必须说清楚】

            有两类入口，方向相反：

            ① 发送侧：我**收到**对方的报价，决定回应什么
                   respond(incoming) → execute() → 改本地状态 + 返回要发的内容

            ② 接收侧：我**收到**对方的 ACCEPT / REJECT / COUNTER_OFFER
                   handle_accept(incoming) / handle_reject(...) / ...

            execute() 属于 ①。所以它**不能**把自己要发出去的那条消息
            再喂给 handle_* —— 那些方法模拟的是"收到"，
            而发出去的消息 to_agent 是**对方**，会被误判成"对方在做决定"。

            （这个坑真的踩过：B 的还价被当成 A 发的，触发了轮次校验。）

        【返回什么】
            返回的 HandlerResult 带着 reply_type / reply_payload，
            调用方用 result.to_reply(incoming) 就能得到要发出去的消息。

        【注意顺序：先决策，再校验，最后才改状态】
            策略说的不算数 —— 它可能给出一个违反协议的决定
            （比如还价超过预算）。校验在 TaskService，执行在 Task Core。
        """
        if not isinstance(decision, Decision):
            raise TypeError(
                f"execute 需要 Decision，收到 {type(decision).__name__}"
            )

        task_id = message.payload.get("task_id")
        task = self.store.get(task_id) if task_id else None
        if task is None:
            raise TaskValidationError(f"本地没有任务 {task_id!r}，无法执行决策")

        # 【发送侧的行动方是「我们」】—— 我们收到了对方的报价，正在决定怎么回。
        actor = message.to_agent

        if not task.is_participant(actor):
            raise InvalidResponderError(
                f"{actor} 不是任务 {task.task_id} 的参与方"
            )

        if decision.accepted:
            # 我接受的是**对方刚发来的这条**报价
            accept_message = message.reply(
                message_type=MessageType.ACCEPT,
                payload={
                    "task_id": task.task_id,
                    "price": decision.price.to_dict(),
                },
            )
            return self._accept_locally(
                task, decision.price, incoming=message,
                accepted_offer_message_id=message.message_id,
                accepted_offer_type=message.message_type,
                outgoing=accept_message,
            )

        if decision.countered:
            counter_message = message.reply(
                message_type=MessageType.COUNTER_OFFER,
                payload={
                    "task_id": task.task_id,
                    "price": decision.price.to_dict(),
                },
            )
            return self._counter_locally(
                task, decision.price, incoming=message,
                outgoing=counter_message,
            )

        reject_message = message.reply(
            message_type=MessageType.REJECT,
            payload={"task_id": task.task_id, "reason": decision.reason},
        )
        return self._reject_locally(
            task, decision.reason, incoming=message, outgoing=reject_message
        )

    # ============================================================
    # 发送侧：本地记录三种决策
    # ============================================================
    def _counter_locally(
        self, task: Task, price: Money, incoming: Message,
        outgoing: Optional[Message] = None,
    ) -> HandlerResult:
        """我还了一个价 → 记进本地报价历史，状态进入 NEGOTIATING。"""
        self._sync_trace(task, incoming)

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
            action="countered",
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

    def _reject_locally(
        self, task: Task, reason: str, incoming: Message,
        outgoing: Optional[Message] = None,
    ) -> HandlerResult:
        """我拒绝 → 任务进入 REJECTED。**不产生任何经济后果。**"""
        self._sync_trace(task, incoming)

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
            action="rejected",
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
    # 本地成交（ACCEPT 的唯一落地点）
    # ============================================================
    def _accept_locally(
        self,
        task: Task,
        price: Money,
        incoming: Message,
        accepted_offer_message_id: Optional[str],
        accepted_offer_type: str,
        outgoing: Optional[Message] = None,
    ) -> HandlerResult:
        """
        把「成交」落到 Task 状态 + 报价历史 + 成交凭证。

        【只有这里会让任务变成 ACCEPTED】
            不管是「我自己决定接受」（发送侧），
            还是「对方接受了我的报价」（接收侧），
            都走这一条路 —— 否则两边状态会不一致。

        【它**只**做三件事】（§9 / §10 / §19）
            1. Task: OFFERED/NEGOTIATING → ACCEPTED，记 agreed_price
            2. 报价历史里追加一条 ACCEPT（带 message_id 和 reply_to）
            3. 生成一份不可变的成交凭证（TaskAgreementRecord）

        【它刻意不做的事】（§20 / §21 / §22）
            ❌ 不转账、不写账本
            ❌ 不创建 Workspace、不授予权限
            ❌ 不调模型
            ❌ 不碰 Git
        """
        self._sync_trace(task, incoming)

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
        # 【ACCEPT 是谁发的】
        #     发送侧：是我在接受对方的报价 → 接受方 = incoming.to_agent（我）
        #     接收侧：是对方接受了我的报价 → 接受方 = incoming.from_agent（对方）
        #     用 message_type 区分这两种情况，不需要知道任何具体 Agent 的名字。
        is_receiving_accept = incoming.message_type == MessageType.ACCEPT
        accept_author = (
            incoming.from_agent if is_receiving_accept else incoming.to_agent
        )

        # 【ACCEPT 记录该用哪个 message_id】
        #     用**这条 ACCEPT 消息自己**的 id，而不是被接受的那条报价的 id。
        #     因为被接受的那条报价已经在历史里了，用它的 id 会被去重规则挡掉，
        #     结果就是"成交了但报价历史里没有 ACCEPT 记录"。
        #     而被接受的是哪一条，靠 reply_to 表达（§18 要的就是这个关系）。
        task.record_offer(
            OfferType.ACCEPT,
            price,
            message_id=(
                outgoing.message_id if outgoing else incoming.message_id
            ),
            reply_to=accepted_offer_message_id or incoming.reply_to,
            from_agent=accept_author,
            trace_id=incoming.trace_id,
            conversation_id=incoming.conversation_id,
            note="成交",
        )

        self.store.save(task)

        # 3) 成交凭证（不可变、append-only）
        agreement = TaskAgreementRecord(
            agreement_id=self._agreement_id_for(
                task, accepted_offer_message_id
            ),
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

        # 发送侧需要把 ACCEPT 发出去；接收侧不需要（reply_type 会被忽略）
        is_sender_side = incoming.message_type != MessageType.ACCEPT

        return HandlerResult(
            ok=True,
            action="accepted",
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
    def _agreement_id_for(
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
    # 内部
    # ============================================================
    def _sync_trace(self, task: Task, message: Message) -> bool:
        """
        把任务的 current_trace_id 对齐到消息的 trace_id。

        【为什么需要这个】
            Step 4A 里 task_id 和 trace_id 是**分开**的：
                task_id          任务的稳定身份
                current_trace_id 这一次执行的链路

            收到消息时，消息带的是「这一次执行」的 trace。
            把它同步过来，两个系统就缝合了：
                任务 → 报价 → message_id → 通信日志
            这条链上的 trace_id 是同一个。

        【换 trace 是正常的】
            重试意味着新的一次执行、新的 trace，
            但 task_id 不变 —— 这正是两个字段分开的目的。

        返回是否真的发生了同步。
        """
        if task.current_trace_id == message.trace_id:
            return False

        previous = task.current_trace_id
        task.current_trace_id = message.trace_id
        logger.debug(
            "任务 %s 的 trace 从 %s 切换到 %s（收到新执行的 %s）",
            task.task_id, previous, message.trace_id, message.message_type,
        )
        return True

    def _reject_offer(
        self,
        message: Message,
        errors: Sequence[str],
        warnings: Sequence[str],
        task_id: Optional[str],
    ) -> HandlerResult:
        """
        校验失败。

        【失败时回复，成功时不回复】
            失败必须告诉对方「你的消息有问题」，否则它会一直等。
            成功则不需要抢答 —— 接不接是我们的业务决定，稍后再说。
        """
        logger.warning(
            "拒绝一条 %s（来自 %s）：%s",
            message.message_type, message.from_agent, "; ".join(errors),
        )

        return HandlerResult(
            ok=False,
            action="rejected",
            task_id=task_id,
            errors=tuple(errors),
            warnings=tuple(warnings),
            reply_type=MessageType.ERROR,
            reply_payload={
                "task_id": task_id,
                "rejected_message_type": message.message_type,
                "rejected_message_id": message.message_id,
                "errors": list(errors),
            },
            details={
                "message_id": message.message_id,
                "from_agent": message.from_agent,
            },
        )

    # ============================================================
    def describe(self) -> Dict[str, Any]:
        return {
            "service": type(self).__name__,
            "directory": (
                type(self.directory).__name__ if self.directory else None
            ),
            "tasks": self.store.count(),
        }

    def __repr__(self) -> str:
        return (
            f"TaskService(directory="
            f"{type(self.directory).__name__ if self.directory else None})"
        )
