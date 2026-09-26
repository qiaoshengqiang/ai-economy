"""
Task Core —— 任务领域对象与状态机
============================================================

【这个文件是什么】

    AI Economy OS 的核心业务对象：**一个任务**。

    它回答的问题是：
        「系统现在认为这个任务处于什么状态？」

    它【不】回答：
        「我要不要接这个任务？」     ← 那是 Agent Brain 的事
        「这条消息送到了吗？」       ← 那是 Communication 的事
        「钱转了吗？」               ← 那是 Economy 的事

【四条必须守住的边界】（每一条都有架构守卫测试）

    1. ❌ 不依赖 llm/*
       Task 是业务状态，不是 AI 推理。Task 必须能在没有模型的情况下
       完整创建、报价、接单、完成 —— 测试里不许出现 DeepSeek。

    2. ❌ 不依赖 communication/*
       通信层负责「把消息送到」，Task 层负责「解释消息的业务含义」。
       两者是两个状态机，不能混：
           Communication: SENT → DELIVERED → RECEIVED → PROCESSED
           Task:          OFFERED → ACCEPTED → IN_PROGRESS → COMPLETED
       message.status 永远不该让 task.status 自动跳变。

    3. ❌ 不依赖 wallet / ledger
       Task 里会有 budget 和 agreed_price，但那只是**经济约定**。
       `task.accept(price)` 只记录「双方谈好了 25 AIC」，
       **不会真的转 25 AIC**。真实结算属于后续步骤。

    4. ❌ 不依赖文件系统
       Task 只持有一个 workspace_id（逻辑引用），
       它不能读写任何文件。文件访问属于 Workspace 层。

   —— 这四条让 Task Core 成为一个纯粹的、可独立测试的领域对象。

【本文件不做什么】

    ❌ 不碰数据库（持久化在 core/task_store.py）
    ❌ 不碰网络
    ❌ 不碰 LLM
    ❌ 不碰钱（只记录数字）
    ❌ 不创建 Workspace
    ❌ 不授予权限
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from core.identity import new_message_id, utc_now_iso
from core.money import Money
from core.task_workspace import TaskWorkspaceRef


# ============================================================
# 任务状态
# ============================================================
class TaskStatus:
    """
    任务生命周期状态。

    【正常流程】

        CREATED → OFFERED → NEGOTIATING → ACCEPTED
               → IN_PROGRESS → SUBMITTED → VERIFIED → COMPLETED

    【异常终态】

        REJECTED   对方拒绝了
        CANCELLED  任何一方主动取消
        EXPIRED    报价过期（没人接）
        FAILED     执行过程中失败

    【⚠️ 这和 Communication 的状态是两回事】

        Communication 的 SENT / DELIVERED / RECEIVED / PROCESSED
        说的是「这条消息送到没有」。

        Task 的 OFFERED / ACCEPTED / COMPLETED
        说的是「这件事谈成没有、干完没有」。

        一条 TASK_OFFER 消息可能是 DELIVERED 但任务还停在 CREATED
        （对方收到了但还没看）。反过来，消息 PROCESSED 也不代表任务被接受了。
    """

    CREATED = "CREATED"
    OFFERED = "OFFERED"
    NEGOTIATING = "NEGOTIATING"
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"

    # 异常终态
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"

    # 【一个需要知道的事实：CREATED 和 FAILED 与通信状态重名】
    #
    #     TaskStatus.CREATED   与 MessageStatus.CREATED   同名
    #     TaskStatus.FAILED    与 MessageStatus.FAILED    同名
    #
    # 这是两个**互相独立**的状态机在各自领域里用了最自然的词，
    # 就像 Order.status 和 Payment.status 都可以有 CREATED 一样。
    #
    # 它本身不是问题 —— 因为：
    #     · 两者是不同模块里的不同类
    #     · core/task.py 不 import communication（有守卫测试）
    #     · 任务状态只能通过状态机改，不能被消息状态驱动
    #
    # 但它确实意味着：`task.status == message.status` 这种写法
    # 可能"碰巧"为真。**不要写那种比较** —— 它没有任何业务含义。
    #
    # 真正危险的是「传输类状态混进任务状态」，
    # 也就是 Task 里出现 SENT / DELIVERED / PROCESSED 之类。
    # 那个由测试明确禁止（tests/test_task.py）。

    @classmethod
    def all(cls) -> List[str]:
        return [
            cls.CREATED, cls.OFFERED, cls.NEGOTIATING, cls.ACCEPTED,
            cls.IN_PROGRESS, cls.SUBMITTED, cls.VERIFIED, cls.COMPLETED,
            cls.REJECTED, cls.CANCELLED, cls.EXPIRED, cls.FAILED,
        ]

    @classmethod
    def terminal(cls) -> List[str]:
        """终态：走到这里就不会再变了。"""
        return [cls.COMPLETED, cls.REJECTED, cls.CANCELLED,
                cls.EXPIRED, cls.FAILED]

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in cls.terminal()

    @classmethod
    def is_known(cls, status: str) -> bool:
        return status in cls.all()


# ============================================================
# 状态机（合法转换表）
# ============================================================
# 【这就是状态机的定义，只有这一处】
#
# 为什么用一张表而不是散落在各个方法里的 if？
#     因为「哪些转换合法」是这个系统的核心规则，
#     它必须能一眼看全、能被测试遍历、能被守卫检查。
#     散在十个方法里的 if 做不到这些。
#
# 读法：TRANSITIONS[当前状态] = 允许转到的状态集合
TRANSITIONS: Dict[str, frozenset] = {
    TaskStatus.CREATED: frozenset({
        TaskStatus.OFFERED,
        TaskStatus.CANCELLED,
    }),

    TaskStatus.OFFERED: frozenset({
        TaskStatus.NEGOTIATING,     # 对方还价
        TaskStatus.ACCEPTED,        # 对方直接接受
        TaskStatus.REJECTED,        # 对方拒绝
        TaskStatus.EXPIRED,         # 没人接，过期
        TaskStatus.CANCELLED,
    }),

    TaskStatus.NEGOTIATING: frozenset({
        TaskStatus.NEGOTIATING,     # 【唯一允许的自转换】多轮还价
        TaskStatus.ACCEPTED,
        TaskStatus.REJECTED,
        TaskStatus.EXPIRED,
        TaskStatus.CANCELLED,
    }),

    TaskStatus.ACCEPTED: frozenset({
        TaskStatus.IN_PROGRESS,     # 开工
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    }),

    TaskStatus.IN_PROGRESS: frozenset({
        TaskStatus.SUBMITTED,       # 提交成果
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    }),

    TaskStatus.SUBMITTED: frozenset({
        TaskStatus.VERIFIED,        # 验收通过
        TaskStatus.IN_PROGRESS,     # 【返工】验收没通过，打回重做
        TaskStatus.FAILED,
    }),

    TaskStatus.VERIFIED: frozenset({
        TaskStatus.COMPLETED,       # 结项
        TaskStatus.FAILED,
    }),

    # 终态：没有出边
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.EXPIRED: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


#: 允许「自转换」（转到自己）的状态。
#: 只有 NEGOTIATING —— 因为一轮谈判里可以来回还价很多次，
#: 状态始终是 NEGOTIATING，但每次还价都是一个真实的事件。
SELF_TRANSITIONS = frozenset({TaskStatus.NEGOTIATING})


# ============================================================
# 异常
# ============================================================
class TaskError(Exception):
    """Task 相关错误的基类。"""


class InvalidTaskTransition(TaskError):
    """
    试图做一个非法的状态转换。

    【这是本文件最重要的异常】
        它保证状态机不会被绕过。看到它说明调用顺序错了。
    """


class TaskValidationError(TaskError):
    """Task 字段本身不合法。"""


class BudgetExceededError(TaskError):
    """
    成交价超过了预算上限。

    这条规则把 budget 和 agreed_price 的不同含义变成了**代码保证**：
        budget       = 我愿意付的上限
        agreed_price = 实际谈成的数
    谈成的数超过上限，是逻辑错误，必须拦住。
    """


# ============================================================
# 报价记录
# ============================================================
class OfferType:
    """
    一条报价记录的类型。

    【注意这些字符串和协议里的 message_type 是**一样的**】
        PROTOCOL.md 6.2：TASK_OFFER / COUNTER_OFFER / ACCEPT

    但我们**不 import communication** —— 那会破坏 Task Core 的独立性。
    改由测试来保证两边字符串一致（tests/test_task.py 里有一条）。

    这样将来把报价接到通信层时，不需要做任何映射转换。
    """

    TASK_OFFER = "TASK_OFFER"
    COUNTER_OFFER = "COUNTER_OFFER"
    ACCEPT = "ACCEPT"

    @classmethod
    def all(cls) -> List[str]:
        return [cls.TASK_OFFER, cls.COUNTER_OFFER, cls.ACCEPT]


@dataclass(frozen=True)
class Offer:
    """
    一条报价的历史记录。

    【为什么要有它】
        只保存 agreed_price = 25 的话，就问不出：
            「这个任务是压价成交的，还是按预算成交的？」
            「对方还了几次价？」

        保留完整报价链，将来才能分析 Agent 的议价行为。

        TASK-001
        ├── TASK_OFFER      100 AIC
        ├── COUNTER_OFFER    60 AIC
        ├── COUNTER_OFFER    40 AIC
        └── ACCEPT           25 AIC      ← agreed_price 来自这里
    """

    offer_type: str
    amount: Money
    offer_id: str = field(default_factory=lambda: new_message_id("offer"))
    created_at: str = field(default_factory=utc_now_iso)

    #: 关联的通信消息 ID。
    #: 【可以是None】—— 因为 Task Core 不依赖通信层。
    #: 将来接入通信后，这里会填上那条 TASK_OFFER 消息的 message_id，
    #: 于是就能从任务一路查到通信日志。
    message_id: Optional[str] = None

    note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.offer_type not in OfferType.all():
            raise TaskValidationError(
                f"未知的报价类型：{self.offer_type!r}，"
                f"合法值：{OfferType.all()}"
            )
        if not isinstance(self.amount, Money):
            raise TaskValidationError(
                f"报价金额必须是 Money，收到 {type(self.amount).__name__}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "offer_type": self.offer_type,
            "amount": self.amount.to_dict(),
            "message_id": self.message_id,
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Offer":
        return cls(
            offer_id=data.get("offer_id") or new_message_id("offer"),
            offer_type=data["offer_type"],
            amount=Money.from_dict(data["amount"]),
            message_id=data.get("message_id"),
            note=data.get("note"),
            created_at=data.get("created_at") or utc_now_iso(),
        )

    def __str__(self) -> str:
        return f"{self.offer_type} {self.amount}"


# ============================================================
# Task
# ============================================================
class Task:
    """
    一个业务任务。

    【状态只能通过转换方法改】
        ✅ task.offer()
        ✅ task.accept(Money(25))
        ✅ task.transition_to(TaskStatus.OFFERED)
        ❌ task.status = "COMPLETED"      ← 抛 AttributeError

        这样状态机就不可能被绕过。

    【怎么用】

        task = Task.create(
            creator="agent_a",
            description="写个 CSV 分析程序",
            budget=Money(100),
            workspace_id="WS-001",
        )

        task.offer()                       # CREATED → OFFERED
        task.record_offer(TaskStatus_offer) # 记下报价历史
        task.negotiate()                   # OFFERED → NEGOTIATING
        task.accept(Money(25))             # NEGOTIATING → ACCEPTED

        # 注意：到这里为止，一分钱都没有动。
    """

    def __init__(
        self,
        task_id: str,
        creator: str,
        description: str = "",
        status: str = TaskStatus.CREATED,
        current_trace_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        budget: Optional[Money] = None,
        agreed_price: Optional[Money] = None,
        contractor: Optional[str] = None,
        participants: Optional[List[str]] = None,
        acceptance: Optional[Mapping[str, Any]] = None,
        expires_at: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        offers: Optional[List[Offer]] = None,
        note: Optional[str] = None,
    ):
        # ---------- 校验 ----------
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskValidationError("task_id 必须是非空字符串")
        if not isinstance(creator, str) or not creator.strip():
            raise TaskValidationError("creator 必须是非空字符串")
        if not TaskStatus.is_known(status):
            raise TaskValidationError(
                f"未知的任务状态：{status!r}，合法值：{TaskStatus.all()}"
            )

        # budget 和 agreed_price 都必须是 Money 或 None
        if budget is not None and not isinstance(budget, Money):
            raise TaskValidationError(
                f"budget 必须是 Money 或 None，收到 {type(budget).__name__}"
            )
        if agreed_price is not None and not isinstance(agreed_price, Money):
            raise TaskValidationError(
                f"agreed_price 必须是 Money 或 None，"
                f"收到 {type(agreed_price).__name__}"
            )

        self.task_id = task_id.strip()
        self.creator = creator.strip()
        self.description = str(description or "")

        # 【私有字段】状态只能通过转换方法改，见下面的 status property
        self._status = status

        # task_id 是稳定身份，current_trace_id 是「这一次执行」的链路。
        # 两者刻意分开，因为同一个任务可能重跑多次（每次一个新 trace）。
        self.current_trace_id = current_trace_id or new_message_id("trc")

        # workspace_id 只是逻辑标识（TaskWorkspaceRef 里没有任何 I/O 和权限）
        self.workspace = (
            TaskWorkspaceRef(workspace_id) if workspace_id else None
        )

        # 【经济约定，不是真实账目】
        #   budget       发起方愿意付的上限
        #   agreed_price 谈成的数（还没付）
        self.budget = budget
        self.agreed_price = agreed_price

        self.contractor = contractor
        self.participants = list(participants or [])
        self.acceptance = dict(acceptance) if acceptance else None
        self.expires_at = expires_at

        now = utc_now_iso()
        self.created_at = created_at or now
        self.updated_at = updated_at or now

        # 报价历史（可以有，也可以是空的）
        self.offers: List[Offer] = list(offers or [])

        self.note = note

    # ------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------
    @classmethod
    def create(
        cls,
        creator: str,
        description: str = "",
        budget: Optional[Money] = None,
        workspace_id: Optional[str] = None,
        acceptance: Optional[Mapping[str, Any]] = None,
        expires_at: Optional[str] = None,
        task_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        participants: Optional[List[str]] = None,
        note: Optional[str] = None,
    ) -> "Task":
        """
        创建一个新任务。状态恒为 CREATED。

        【为什么新任务一定从 CREATED 开始】
            因为它还没被任何人看到，谈不上 OFFERED。
            如果允许指定初始状态，就等于绕过了状态机的第一道门。
            （从数据库重建对象走 __init__，那是另一条路径。）
        """
        return cls(
            task_id=task_id or new_message_id("task"),
            creator=creator,
            description=description,
            status=TaskStatus.CREATED,
            current_trace_id=trace_id,
            workspace_id=workspace_id,
            budget=budget,
            acceptance=acceptance,
            expires_at=expires_at,
            participants=participants,
            note=note,
        )

    # ------------------------------------------------------------
    # 状态（只读属性 —— 没有 setter）
    # ------------------------------------------------------------
    @property
    def status(self) -> str:
        """
        当前状态。

        【刻意不提供 setter】
            如果想改状态，会得到：
                AttributeError: property 'status' of 'Task' object has no setter
            这就保证了状态机不会被绕过 —— 想改状态，只能走
            transition_to() / offer() / accept() 这些会做校验的方法。
        """
        return self._status

    @property
    def is_terminal(self) -> bool:
        return TaskStatus.is_terminal(self._status)

    @property
    def is_open(self) -> bool:
        """任务是否还在进行中（没到终态）。"""
        return not self.is_terminal

    @property
    def workspace_id(self) -> Optional[str]:
        """
        便利访问器。

        【强调】拿到这个字符串 ≠ 能访问这个工作空间。
        它只是个标识（详见 core/task_workspace.py）。
        """
        return self.workspace.workspace_id if self.workspace else None

    # ------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------
    def allowed_transitions(self) -> List[str]:
        """当前状态下允许转到哪些状态。"""
        return sorted(TRANSITIONS.get(self._status, frozenset()))

    def can_transition_to(self, new_status: str) -> bool:
        """
        能不能转到这个状态。

        注意自转换：只有 NEGOTIATING 允许转到自己，
        所以 `can_transition_to(当前状态)` 对大多数状态返回 False。
        """
        if new_status not in TRANSITIONS.get(self._status, frozenset()):
            return False
        if new_status == self._status and new_status not in SELF_TRANSITIONS:
            return False
        return True

    def transition_to(self, new_status: str) -> "Task":
        """
        状态转换。非法就抛 InvalidTaskTransition。

        【报错信息要说清楚「为什么不行」】
            只说「非法转换」没用，排查的人需要知道
            「现在是哪个状态、允许去哪儿」。
        """
        if not TaskStatus.is_known(new_status):
            raise TaskValidationError(
                f"未知的任务状态：{new_status!r}，合法值：{TaskStatus.all()}"
            )

        if self.is_terminal:
            raise InvalidTaskTransition(
                f"任务 {self.task_id} 已经是终态 {self._status}，不能再转换到 {new_status}。\n"
                f"终态意味着这个任务已经结束了（完成/拒绝/取消/过期/失败）。"
            )

        if not self.can_transition_to(new_status):
            raise InvalidTaskTransition(
                f"任务 {self.task_id} 不能从 {self._status} 转换到 {new_status}。\n"
                f"  当前允许转换到：{self.allowed_transitions() or '（无，已是终态）'}\n"
                f"  提示：状态只能按状态机走，不能跳步。"
            )

        self._status = new_status
        self.updated_at = utc_now_iso()
        return self

    # ------------------------------------------------------------
    # 语义化转换方法
    #
    # 为什么除了 transition_to 还要有这些？
    #   因为它们把「业务动作」和「状态变化」绑在一起：
    #       task.accept(price) 既记录成交价，又转到 ACCEPTED
    #   如果只有 transition_to，调用方就得记得「转状态之前还要设价格」——
    #   迟早有人忘，然后系统里出现「ACCEPTED 但没有成交价」的任务。
    # ------------------------------------------------------------
    def offer(self) -> "Task":
        """把任务发出去（CREATED → OFFERED）。"""
        return self.transition_to(TaskStatus.OFFERED)

    def negotiate(self) -> "Task":
        """
        进入/继续谈判（OFFERED → NEGOTIATING，或 NEGOTIATING → NEGOTIATING）。

        允许重复调用，因为一轮谈判里可以来回还价很多次。
        """
        return self.transition_to(TaskStatus.NEGOTIATING)

    def accept(self, price: Money, contractor: Optional[str] = None) -> "Task":
        """
        接受报价（OFFERED / NEGOTIATING → ACCEPTED），并记录成交价。

        【这一步不会转钱】
            只是记录「双方谈好了这个数」。
            真实结算属于后续步骤。

        【会检查不超预算】
            agreed_price > budget 是逻辑错误 —— 预算就是上限。
        """
        if not isinstance(price, Money):
            raise TaskValidationError(
                f"成交价必须是 Money，收到 {type(price).__name__}。\n"
                f"（裸数字没有货币语义）"
            )

        # 预算校验：这是 budget 与 agreed_price 分离的直接价值
        if self.budget is not None:
            from core.money import CurrencyMismatchError

            try:
                over_budget = price > self.budget
            except CurrencyMismatchError as exc:
                raise TaskValidationError(
                    f"成交价与预算的货币不一致：{exc}"
                ) from exc

            if over_budget:
                raise BudgetExceededError(
                    f"任务 {self.task_id} 的成交价 {price} 超过预算 {self.budget}。\n"
                    f"预算（budget）是发起方愿意支付的上限，成交价不能超过它。\n"
                    f"如果确实要超，应该先重新报价（COUNTER_OFFER）并调整预算。"
                )

        # 先转状态（不合法就抛异常，不留下半改的数据）
        self.transition_to(TaskStatus.ACCEPTED)

        self.agreed_price = price
        if contractor is not None:
            self.contractor = contractor

        return self

    def reject(self, reason: Optional[str] = None) -> "Task":
        """拒绝（OFFERED / NEGOTIATING → REJECTED）。"""
        self.note = reason or self.note
        return self.transition_to(TaskStatus.REJECTED)

    def start(self) -> "Task":
        """开工（ACCEPTED → IN_PROGRESS）。"""
        return self.transition_to(TaskStatus.IN_PROGRESS)

    def submit(self) -> "Task":
        """提交成果（IN_PROGRESS → SUBMITTED）。"""
        return self.transition_to(TaskStatus.SUBMITTED)

    def verify(self, passed: bool = True) -> "Task":
        """
        验收（SUBMITTED → VERIFIED 或 SUBMITTED → IN_PROGRESS）。

        passed=False 表示验收没通过，任务**打回重做**（回到 IN_PROGRESS）。
        这比直接把任务判死更符合实际 —— 交付不合格通常是可以修的。
        """
        if passed:
            return self.transition_to(TaskStatus.VERIFIED)
        return self.transition_to(TaskStatus.IN_PROGRESS)

    def complete(self) -> "Task":
        """结项（VERIFIED → COMPLETED）。"""
        return self.transition_to(TaskStatus.COMPLETED)

    def cancel(self, reason: Optional[str] = None) -> "Task":
        """取消。任何非终态都可以取消。"""
        self.note = reason or self.note
        return self.transition_to(TaskStatus.CANCELLED)

    def expire(self) -> "Task":
        """过期（OFFERED / NEGOTIATING → EXPIRED）。"""
        return self.transition_to(TaskStatus.EXPIRED)

    def fail(self, reason: Optional[str] = None) -> "Task":
        """失败（ACCEPTED / IN_PROGRESS / SUBMITTED / VERIFIED → FAILED）。"""
        self.note = reason or self.note
        return self.transition_to(TaskStatus.FAILED)

    # ------------------------------------------------------------
    # 报价历史
    # ------------------------------------------------------------
    def record_offer(
        self,
        offer_type: str,
        amount: Money,
        message_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Offer:
        """
        记下一条报价。

        【这一步不改变任务状态】
            它只是记账式的记录。状态变化要靠 negotiate()/accept()。
            分开是因为「收到一个报价」和「任务因此进入谈判状态」
            在业务上未必同时发生（可能还需要先做别的判断）。

        message_id 可以留空 —— Task Core 不依赖通信层。
        将来接入通信后，把那条消息的 ID 填进来就能一路查到通信日志。
        """
        if not isinstance(amount, Money):
            raise TaskValidationError(
                f"报价金额必须是 Money，收到 {type(amount).__name__}"
            )

        record = Offer(
            offer_type=offer_type,
            amount=amount,
            message_id=message_id,
            note=note,
        )
        self.offers.append(record)
        self.updated_at = utc_now_iso()
        return record

    def latest_offer(self) -> Optional[Offer]:
        return self.offers[-1] if self.offers else None

    def offer_amounts(self) -> List[float]:
        """报价历史里的数值序列 —— 方便看「还了几次价」。"""
        return [offer.amount.amount for offer in self.offers]

    # ------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------
    def to_dict(self, include_offers: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "task_id": self.task_id,
            # task_id 与 current_trace_id 是两个不同职责的字段，见文件头
            "current_trace_id": self.current_trace_id,
            "creator": self.creator,
            "contractor": self.contractor,
            "participants": list(self.participants),
            "status": self._status,
            "description": self.description,
            "workspace_id": self.workspace_id,
            # 【两个不同的字段，不要合并】
            "budget": self.budget.to_dict() if self.budget else None,
            "agreed_price": (
                self.agreed_price.to_dict() if self.agreed_price else None
            ),
            "acceptance": dict(self.acceptance) if self.acceptance else None,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "note": self.note,
        }
        if include_offers:
            data["offers"] = [offer.to_dict() for offer in self.offers]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Task":
        """
        从字典重建。

        注意这里**允许指定 status** —— 因为这是从存储里恢复一个
        已经存在的任务，不是新建。新建请用 Task.create()。
        """
        return cls(
            task_id=data["task_id"],
            creator=data["creator"],
            description=data.get("description", ""),
            status=data.get("status", TaskStatus.CREATED),
            current_trace_id=data.get("current_trace_id"),
            workspace_id=data.get("workspace_id"),
            budget=(
                Money.from_dict(data["budget"]) if data.get("budget") else None
            ),
            agreed_price=(
                Money.from_dict(data["agreed_price"])
                if data.get("agreed_price") else None
            ),
            contractor=data.get("contractor"),
            participants=data.get("participants"),
            acceptance=data.get("acceptance"),
            expires_at=data.get("expires_at"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            offers=[
                Offer.from_dict(item) for item in data.get("offers", [])
            ],
            note=data.get("note"),
        )

    # ------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """给人看的一行摘要。"""
        return {
            "task_id": self.task_id,
            "status": self._status,
            "creator": self.creator,
            "contractor": self.contractor,
            "budget": str(self.budget) if self.budget else None,
            "agreed_price": (
                str(self.agreed_price) if self.agreed_price else None
            ),
            "workspace_id": self.workspace_id,
            "offers": len(self.offers),
            "tracing": self.current_trace_id,
        }

    def __repr__(self) -> str:
        return (
            f"Task(task_id={self.task_id!r}, status={self._status!r}, "
            f"budget={self.budget}, agreed_price={self.agreed_price})"
        )
