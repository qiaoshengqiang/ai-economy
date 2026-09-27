"""
Task Message Mapper —— 协议消息 ↔ 应用数据
============================================================

【它的唯一职责】

        Protocol Message Payload  ↕  Application Data

    把一条通信消息的 `payload`（一个开放的字典）解析成
    **有类型的应用数据**，反过来也能把应用数据构造成回复消息。

【它刻意不做的事】（这条边界非常重要）

    ❌ 它不判断业务是否成立。

    举例：

        Mapper 可以解析出：price = 25 AIC
        但「25 AIC 是否允许」不是它决定的。

    所以它只管**形状**：

        ✅ task_id 存在吗？是字符串吗？
        ✅ price 是 {amount, currency} 对象，还是裸数字？
        ❌ price 的货币是不是 AIC        ← 业务规则，在 NegotiationService
        ❌ price 有没有超过预算           ← 业务规则，在 NegotiationService
        ❌ 这个人有没有资格回应           ← 业务规则，在 NegotiationService

    【为什么形状校验收在这儿】
        因为「协议长什么样」和「业务允不允许」是两个变化原因：
            协议加字段       → 只改 Mapper
            议价规则变了     → 只改 NegotiationService

        混在一起的话，改协议就得动业务逻辑，改业务就得动协议解析。

【它为什么不知道 Task】

    Mapper 只认识 Message 和 Money ——
    它不知道 Task、不碰 TaskStore、不看目录。
    这样它可以在完全没有领域对象的情况下被单独测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.money import Money, MoneyError
from core.task_execution import TaskResultRef, TaskVerification

from communication.message import Message, MessageType


# ============================================================
# 解析结果
# ============================================================
@dataclass(frozen=True)
class MessageIntent:
    """
    一条消息被解析出来的**应用数据**。

    【为什么要 errors 字段】
        解析失败时不能抛异常就走 —— 调用方需要把
        「所有问题一次告诉对方」，而不是改一个跑一次。
        所以解析器把问题收集起来，和解析出来的部分一起返回。

        `.ok` 为 False 时，后面的业务规则就不该再跑了。
    """

    errors: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class OfferIntent(MessageIntent):
    """TASK_OFFER 解析结果。"""

    task_id: Optional[str] = None
    description: Optional[str] = None
    budget: Optional[Money] = None
    workspace_id: Optional[str] = None
    required_capabilities: Optional[List[str]] = None
    acceptance: Optional[Dict[str, Any]] = None
    expires_at: Optional[str] = None


@dataclass(frozen=True)
class CounterOfferIntent(MessageIntent):
    """COUNTER_OFFER 解析结果。"""

    task_id: Optional[str] = None
    price: Optional[Money] = None


@dataclass(frozen=True)
class AcceptIntent(MessageIntent):
    """ACCEPT 解析结果。"""

    task_id: Optional[str] = None
    price: Optional[Money] = None
    #: 被接受的那条报价的 message_id（协议 §18 要求必须能反查）
    accepted_offer_message_id: Optional[str] = None


@dataclass(frozen=True)
class RejectIntent(MessageIntent):
    """REJECT 解析结果。"""

    task_id: Optional[str] = None
    reason: Optional[str] = None


# ============================================================
# Step 4D：执行阶段的四种消息
# ============================================================
@dataclass(frozen=True)
class StartIntent(MessageIntent):
    """START 解析结果 —— 承接方说「我开工了」。"""

    task_id: Optional[str] = None


@dataclass(frozen=True)
class SubmitIntent(MessageIntent):
    """
    SUBMIT 解析结果 —— 承接方说「我交付了这份成果」。

    【result 是必填的】
        不能只发「我做好了」：发起方需要知道
        **验收的是哪一份成果**，否则验收结论没有对象。
    """

    task_id: Optional[str] = None
    result: Optional["TaskResultRef"] = None


@dataclass(frozen=True)
class VerifyIntent(MessageIntent):
    """
    VERIFY 解析结果 —— 发起方说「验收通过 / 不通过」。

    【passed 用 Optional[bool] 而不是 bool】
        因为 `False` 和「字段缺失」必须区分：
            字段缺失 → 协议错误（这条消息不完整）
            False    → 明确的「不通过」，要求返工
        如果用 bool 加默认值，两者就分不开了。
    """

    task_id: Optional[str] = None
    passed: Optional[bool] = None
    feedback: str = ""

    @property
    def verification(self) -> "TaskVerification":
        """转成领域类型（会校验 passed 是布尔、失败必有 feedback）。"""
        from core.task_execution import TaskVerification

        return TaskVerification(passed=self.passed, feedback=self.feedback)


@dataclass(frozen=True)
class CompleteIntent(MessageIntent):
    """COMPLETE 解析结果 —— 发起方说「任务正式结束」。"""

    task_id: Optional[str] = None
    summary: str = ""


# ============================================================
# 金额解析（协议 §7.1）
# ============================================================
def parse_money(raw: Any, field_name: str) -> Money:
    """
    从 payload 里取一个金额。

    协议 §7.1 要求金额是 {amount, currency} 形式，**不是裸数字**。

    【为什么错误信息写得这么啰嗦】
        因为「budget 写成裸数字」是这套协议里最常犯的错误
        （PROTOCOL.md 5.2 专门记录了它），
        所以报错要直接告诉对方正确写法。
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raise ValueError(
            f"{field_name} 必须是 {{amount, currency}} 形式的对象，"
            f"不能是裸数字 {raw!r}。\n"
            f"（裸数字没有货币语义 —— 见 PROTOCOL.md 7.1）"
        )
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"{field_name} 必须是 {{amount, currency}} 形式的对象，"
            f"收到 {type(raw).__name__}"
        )
    try:
        return Money.from_dict(raw)
    except MoneyError as exc:
        raise ValueError(f"{field_name} 不合法：{exc}") from exc


def _parse_task_id(payload: Mapping[str, Any], errors: List[str],
                   hint: str = "") -> Optional[str]:
    """解析 task_id。所有消息类型都需要它。"""
    value = payload.get("task_id")
    if not isinstance(value, str) or not value.strip():
        errors.append(f"缺少 task_id{hint}")
        return None
    return value.strip()


def _parse_money_field(payload: Mapping[str, Any], field_name: str,
                       errors: List[str],
                       missing_hint: str = "") -> Optional[Money]:
    """解析一个金额字段（缺失或形状不对都记进 errors）。"""
    if field_name not in payload:
        errors.append(f"缺少 {field_name}{missing_hint}")
        return None
    try:
        return parse_money(payload[field_name], field_name)
    except ValueError as exc:
        errors.append(str(exc))
        return None


# ============================================================
# Mapper
# ============================================================
class TaskMessageMapper:
    """
    协议消息 ↔ 应用数据。

    两类方法：

        解析   parse_offer / parse_counter_offer / parse_accept / parse_reject
        构造   counter_offer_reply / accept_reply / reject_reply / error_reply

    它**没有状态**，可以随便实例化、随便替换。
    """

    # ------------------------------------------------------------
    # 解析：消息 → 应用数据
    # ------------------------------------------------------------
    def parse_offer(self, message: Message) -> OfferIntent:
        """TASK_OFFER → OfferIntent"""
        payload = dict(message.payload)
        errors: List[str] = []

        task_id = _parse_task_id(
            payload, errors, "（任务发起方必须给出任务标识）"
        )

        description = payload.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append("缺少 description（协议 §7.2 要求任务描述）")
            description = None

        budget = _parse_money_field(
            payload, "budget", errors, "（协议 §7.2 要求给出预算上限）"
        )

        return OfferIntent(
            errors=tuple(errors),
            task_id=task_id,
            description=description,
            budget=budget,
            workspace_id=payload.get("workspace_id"),
            # 【注意】这里只做「原样取出」，不校验能力名是否合法 ——
            # 那是 Task / Capability 层的事。
            required_capabilities=payload.get("required_capabilities"),
            acceptance=payload.get("acceptance"),
            expires_at=payload.get("expires_at"),
        )

    def parse_counter_offer(self, message: Message) -> CounterOfferIntent:
        """COUNTER_OFFER → CounterOfferIntent"""
        payload = dict(message.payload)
        errors: List[str] = []

        task_id = _parse_task_id(payload, errors)
        price = _parse_money_field(
            payload, "price", errors, "（还价必须给出价格）"
        )

        return CounterOfferIntent(errors=tuple(errors), task_id=task_id,
                                  price=price)

    def parse_accept(self, message: Message) -> AcceptIntent:
        """ACCEPT → AcceptIntent"""
        payload = dict(message.payload)
        errors: List[str] = []

        task_id = _parse_task_id(payload, errors)
        price = _parse_money_field(
            payload, "price", errors, "（ACCEPT 必须给出成交价）"
        )

        return AcceptIntent(
            errors=tuple(errors),
            task_id=task_id,
            price=price,
            accepted_offer_message_id=message.reply_to,
        )

    def parse_reject(self, message: Message) -> RejectIntent:
        """REJECT → RejectIntent

        拒绝**不需要**金额 —— 它只表达「我不接」。
        reason 也是可选的（人类可读）。
        """
        payload = dict(message.payload)
        errors: List[str] = []

        task_id = _parse_task_id(payload, errors)

        return RejectIntent(
            errors=tuple(errors),
            task_id=task_id,
            reason=payload.get("reason"),
        )

    # ------------------------------------------------------------
    # 解析：执行阶段的四种消息（Step 4D）
    # ------------------------------------------------------------
    def parse_start(self, message: Message) -> StartIntent:
        """START → StartIntent"""
        payload = dict(message.payload)
        errors: List[str] = []
        task_id = _parse_task_id(payload, errors)
        return StartIntent(errors=tuple(errors), task_id=task_id)

    def parse_submit(self, message: Message) -> SubmitIntent:
        """
        SUBMIT → SubmitIntent

        【result_ref 的解析】
            协议允许多种写法，按宽松到严格的顺序尝试：
                1. payload.result = {result_type, result_ref, summary, metadata}
                2. payload.result_ref = "..."  （扁平写法，更省事）
            两种都支持是因为这是个新字段，
            还没有形成唯一惯例 —— 强行只认一种只会让人踩坑。
        """
        payload = dict(message.payload)
        errors: List[str] = []

        task_id = _parse_task_id(payload, errors)
        result: Optional[TaskResultRef] = None

        raw_result = payload.get("result")
        if isinstance(raw_result, Mapping):
            try:
                result = TaskResultRef.from_dict(raw_result)
            except ValueError as exc:
                errors.append(f"result 不合法：{exc}")
        elif raw_result is not None:
            errors.append(
                "result 必须是 {{result_type, result_ref, summary, metadata}} "
                "形式的对象"
            )
        elif payload.get("result_ref") is not None:
            # 扁平写法
            try:
                result = TaskResultRef(
                    result_ref=payload.get("result_ref"),
                    result_type=payload.get("result_type", "artifact"),
                    summary=payload.get("summary", "") or "",
                    metadata=payload.get("metadata") or {},
                )
            except ValueError as exc:
                errors.append(f"result_ref 不合法：{exc}")
        # 两种都没给 → result 保持 None，
        # 由 ExecutionService.validate_result 给出业务语义的错误信息
        # （「SUBMIT 必须携带成果引用」比「缺少 result_ref」更说明问题）

        return SubmitIntent(errors=tuple(errors), task_id=task_id, result=result)

    def parse_verify(self, message: Message) -> VerifyIntent:
        """VERIFY → VerifyIntent"""
        payload = dict(message.payload)
        errors: List[str] = []

        task_id = _parse_task_id(payload, errors)

        # 【这里只看「形状」】
        #     passed 是不是布尔值是**形状**问题 → Mapper
        #     passed=False 时有没有 feedback 是**业务**问题 → ExecutionService
        passed: Optional[bool] = None
        if "passed" not in payload:
            pass  # 交给业务层报错（「必须携带 passed」更有指导性）
        elif isinstance(payload["passed"], bool):
            passed = payload["passed"]
        else:
            errors.append(
                f"passed 必须是布尔值（true / false），"
                f"收到 {payload['passed']!r}。\n"
                f"（接受 1 / 0 会让「没传」和「传了不通过」分不清。）"
            )

        feedback = payload.get("feedback", "") or ""
        if not isinstance(feedback, str):
            errors.append(f"feedback 必须是字符串，收到 {type(feedback).__name__}")
            feedback = ""

        return VerifyIntent(
            errors=tuple(errors),
            task_id=task_id,
            passed=passed,
            feedback=feedback,
        )

    def parse_complete(self, message: Message) -> CompleteIntent:
        """COMPLETE → CompleteIntent"""
        payload = dict(message.payload)
        errors: List[str] = []
        task_id = _parse_task_id(payload, errors)

        summary = payload.get("summary", "") or ""
        if not isinstance(summary, str):
            errors.append("summary 必须是字符串")
            summary = ""

        return CompleteIntent(errors=tuple(errors), task_id=task_id,
                              summary=summary)

    def offered_price(self, message: Message) -> Money:
        """
        这条消息「报出来的价格」是多少。

        【为什么这件事属于 Mapper】
            因为它是**协议语义**，不是业务规则：

                TASK_OFFER      → 报的是 budget（协议 §14：接受即按 budget 成交）
                COUNTER_OFFER   → 报的是 price

            这正是 Mapper 的职责范围 ——「这条消息在说什么」。
            业务规则（这个价格允不允许）仍然在 NegotiationService。

        【为什么不让调用方自己 if 一下】
            那样「按消息类型分派」就会散落在业务代码里。
            协议语义集中在这儿，守卫才能检查
            「services 里除了注册表没有别的按类型分支」。
        """
        if message.message_type == MessageType.TASK_OFFER:
            return self.parse_offer(message).budget  # type: ignore[return-value]
        return self.parse_counter_offer(message).price  # type: ignore[return-value]

    # ------------------------------------------------------------
    # 构造：应用数据 → 回复消息
    # ------------------------------------------------------------
    @staticmethod
    def counter_offer_reply(message: Message, task_id: str,
                            price: Money) -> Message:
        """构造一条还价回复（发给 `message` 的发送方）。"""
        return message.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload={"task_id": task_id, "price": price.to_dict()},
        )

    @staticmethod
    def accept_reply(message: Message, task_id: str, price: Money) -> Message:
        """构造一条成交回复。"""
        return message.reply(
            message_type=MessageType.ACCEPT,
            payload={"task_id": task_id, "price": price.to_dict()},
        )

    @staticmethod
    def reject_reply(message: Message, task_id: str,
                     reason: Optional[str] = None) -> Message:
        """构造一条拒绝回复。"""
        return message.reply(
            message_type=MessageType.REJECT,
            payload={"task_id": task_id, "reason": reason},
        )

    @staticmethod
    def error_reply_payload(message: Message, task_id: Optional[str],
                            errors) -> Dict[str, Any]:
        """
        构造 ERROR 回复的 payload。

        【为什么错误回复要带上原消息的类型和 ID】
            因为出错时对方可能同时发了好几条，
            只说「你错了」它不知道该改哪一条。
        """
        return {
            "task_id": task_id,
            "rejected_message_type": message.message_type,
            "rejected_message_id": message.message_id,
            "errors": list(errors),
        }

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "mapper": type(self).__name__,
            "parses": [
                "TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT",
                "START", "SUBMIT", "VERIFY", "COMPLETE",
            ],
            "decides_business_rules": False,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"
