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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.agent_directory import AgentDirectory, DirectoryEntry
from core.capability import CapabilityProfile
from core.logging_setup import get_logger
from core.money import DEFAULT_CURRENCY, Money, MoneyError
from core.task import OfferType, Task, TaskStatus, TaskValidationError
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
    ):
        self.store = store
        self.directory = directory

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
        处理收到的 COUNTER_OFFER（还价）。

        【校验的业务规则】§19
            price.currency == AIC
            并且 price <= TASK_OFFER.budget

        第二条是**业务规则**，不是通信规则 ——
        所以它属于这里，不属于 Communication 层。
        Communication 继续只负责把消息送到。
        """
        if not isinstance(message, Message):
            raise TypeError("handle_counter_offer 需要 Message")

        if message.message_type != MessageType.COUNTER_OFFER:
            raise ValueError(
                f"handle_counter_offer 收到的是 {message.message_type}"
            )

        payload = dict(message.payload)
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
            errors.append(
                f"本地没有任务 {task_id} —— 还价必须先有一个已知的任务"
            )
            return self._reject_offer(message, errors, [], task_id)

        # ---- 业务规则：还价不能超过预算 ----
        # 这条规则在 Task 层（BudgetExceededError 也来自 Task），
        # 不在通信层。
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

        task.record_offer(
            OfferType.COUNTER_OFFER,
            price,
            message_id=message.message_id,
            note=f"来自 {message.from_agent}",
        )

        # OFFERED → NEGOTIATING（如果已经在 NEGOTIATING 就保持）
        if task.status == TaskStatus.OFFERED:
            task.negotiate()
        elif task.status == TaskStatus.NEGOTIATING:
            task.negotiate()      # 允许的自转换：多轮还价

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
                "message_id": message.message_id,
            },
        )

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
