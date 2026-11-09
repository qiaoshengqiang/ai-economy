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

from core.capability import CapabilityProfile
from core.identity import new_message_id, utc_now_iso
from core.money import Money
from core.task_workspace import TaskWorkspaceRef


# ============================================================
# 任务状态
# ============================================================
class TaskMode:
    """
    任务的工作模式。

    SOLO
        一个 Agent 独立完成。允许承接方自己 VERIFY / COMPLETE。
        这是 `producer ≠ acceptor` 的显式例外。

    TEAM
        多个 Agent 协作。承接方不能自验；
        VERIFY / COMPLETE 必须由发起方或验收责任人发送。

    🔒 模式只影响**角色规则**，不改变任务生命周期：
      两种模式都走同一套状态机。
    """

    SOLO = "SOLO"
    TEAM = "TEAM"

    @classmethod
    def all(cls) -> List[str]:
        return [cls.SOLO, cls.TEAM]

    @classmethod
    def is_known(cls, mode: str) -> bool:
        return mode in cls.all()


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
        """
        真终态：走到这里就不会再变了。

        🔴 `COMPLETED` **不在**这里。
           它表示当前 Iteration 关闭，可以由 Task Creator 显式 Reopen。
           见 ADR-0056。
        """
        return [cls.REJECTED, cls.CANCELLED, cls.EXPIRED, cls.FAILED]

    @classmethod
    def settled(cls) -> List[str]:
        """
        「当前 Iteration 已关闭」的状态。

        和 terminal 的区别：
            settled   可以 Reopen（COMPLETED）
            terminal  永远不能离开
        """
        return [cls.COMPLETED]

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in cls.terminal()

    @classmethod
    def is_settled(cls, status: str) -> bool:
        return status in cls.settled()

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
        # 【为什么 CREATED 可以直接开工】
        #
        #   `CREATED → OFFERED → ACCEPTED → IN_PROGRESS` 是一条**商业**路径：
        #   它存在的前提是「这个任务要拿出去报价、谈价、成交」。
        #
        #   Root Task 不是 Commerce。Human 提供 Demand →
        #   Agent 建 Root Task → 直接进入生产，中间没有报价与成交。
        #   要求它先走 OFFERED / ACCEPTED，等于为了复用交易状态
        #   而污染生产生命周期。
        #
        #   🔒 这条边**不绕过任何校验**：进入 IN_PROGRESS 仍然必须
        #      经过 `TaskExecutionService.apply_start()` 的角色校验
        #      （见 services/task_execution_service.py 的 REQUIRED_ROLE）。
        TaskStatus.IN_PROGRESS,
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

    # 【COMPLETED 不是终态】
    #
    #   它表示**当前 Iteration 关闭**。Human 后续要求返工时，
    #   由 Task Creator 显式 Reopen，回到 IN_PROGRESS，
    #   同时 iteration +1。旧执行历史保留。
    #
    #   🔒 这条边不开放给普通 transition 调用：
    #      `Task.reopen()` 是唯一的入口，它负责 iteration 递增。
    #      见 ADR-0056。
    TaskStatus.COMPLETED: frozenset({
        TaskStatus.IN_PROGRESS,     # Reopen → 下一 Iteration
    }),

    # 真终态：没有出边
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


class TaskRoleError(TaskError):
    """
    角色 / 轮次不对。

    【Step 4C §16 的硬性要求】

        不允许错误的人做错误的操作：

            · 不是这个任务的参与方，却来 ACCEPT
            · 上一条报价是自己发的，却自己回自己
            · 任务已经结束，还想继续报价

    【注意】判断依据全部来自任务自己的字段
        （creator / contractor / participants / offer.from_agent），
        **没有任何具体 Agent 的名字** —— 所以换成任何两个 Agent 都成立。
    """


class InvalidResponderError(TaskRoleError):
    """这个 Agent 不是该任务的合法参与方。"""


class NotYourTurnError(TaskRoleError):
    """轮次不对（比如上一条报价就是你自己发的）。"""


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

    #: Step 4C 加的 —— 拒绝也要进报价历史。
    #: 【为什么拒绝也要记】
    #:     §12：REJECT 是正常的**业务行为**（「我理解，但我不接」），
    #:     不是协议错误。既然是业务行为，就该留在历史里可审计 ——
    #:     否则以后看不出「这个任务被谁拒过、什么时候拒的」。
    REJECT = "REJECT"

    @classmethod
    def all(cls) -> List[str]:
        return [cls.TASK_OFFER, cls.COUNTER_OFFER, cls.ACCEPT, cls.REJECT]


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
    #: 接入通信后，这里会填上那条报价消息的 message_id，
    #: 于是就能从任务一路查到通信日志。
    message_id: Optional[str] = None

    # ---- 以下四个字段是 Step 4C 加的，为了让报价历史自解释 ----
    #
    # 【为什么 reply_to 必须有】
    #     它指出「这条报价是在回应哪一条」。
    #     有了它，ACCEPT 就能反查到「最终接受的是哪一条报价」（§18），
    #     不需要再新增一个 accepted_offer_id（§18 明确要求别加重复 ID）。
    #
    # 【为什么 from_agent 必须有】
    #     角色 / 轮次验证要用它：「谁刚报过价」决定了「现在该谁说话」（§16）。
    #     不许用 `if agent_id == "agent_b"` 这种写法判断轮次。
    reply_to: Optional[str] = None
    from_agent: Optional[str] = None

    # 【为什么连 trace_id / conversation_id 也存进来】
    #     §11 明确要求：每一条新的 Counter Offer
    #     「必须保留 trace_id / 必须保留 conversation_id」。
    #     存在这里之后，报价历史是**自解释**的 ——
    #     不查通信日志也能还原完整链路。
    trace_id: Optional[str] = None
    conversation_id: Optional[str] = None

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
            "reply_to": self.reply_to,
            "from_agent": self.from_agent,
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
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
            reply_to=data.get("reply_to"),
            from_agent=data.get("from_agent"),
            trace_id=data.get("trace_id"),
            conversation_id=data.get("conversation_id"),
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
        required_capabilities: Optional[Any] = None,
        expires_at: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        offers: Optional[List[Offer]] = None,
        note: Optional[str] = None,
        mode: str = TaskMode.TEAM,
        iteration: int = 1,
        version: int = 0,
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
        if not TaskMode.is_known(mode):
            raise TaskValidationError(
                f"未知的任务模式：{mode!r}，合法值：{TaskMode.all()}"
            )
        if not isinstance(iteration, int) or isinstance(iteration, bool):
            raise TaskValidationError(
                f"iteration 必须是整数，收到 {iteration!r}"
            )
        if iteration < 1:
            raise TaskValidationError(f"iteration 必须从 1 开始：{iteration}")

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

        # 乐观锁版本号（Step 4D.1）。
        #
        # 【为什么 Task 上要有它】
        #     TaskStore.save() 需要知道「我手上这份是从哪一版读出来的」，
        #     才能做 compare-and-set。所以版本号必须随 Task 一起流转 ——
        #     读出来是多少，写回去时就拿它当条件。
        #
        # 【它会被 Task Core 自动推进吗？不会】
        #     只有**成功保存**之后，TaskStore 才会把手里这份的
        #     version 推进到新值。领域方法（start/submit/...）不碰它 ——
        #     因为「保存了几次」是持久化的事实，不是领域概念。
        #
        # 【为什么放在 core/task.py 而不是别处】
        #     它是一个跟随 Task 走的持久化标记。
        #     放在 Task 上是让它能自然流转，而不是让 Task 变聪明。
        if not isinstance(version, int) or isinstance(version, bool):
            raise TaskValidationError(
                f"version 必须是整数，收到 {version!r}"
            )
        if version < 0:
            raise TaskValidationError(f"version 不能为负：{version}")
        self.version = int(version)

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

        # 【工作模式与迭代】
        #   mode       SOLO 允许承接方自验；TEAM 保持 producer ≠ acceptor
        #   iteration  COMPLETED 后 Reopen 会 +1，旧记录不删除
        self.mode = mode
        self.iteration = int(iteration)

        # 【任务需要什么能力】—— 这只是**声明**。
        #
        # 注意 Task 到此为止：它描述「需要什么」，
        # 但**不负责**「去找谁会」。寻找候选 Agent 是 AgentDirectory 的事，
        # 而决定用谁是 Task Initiator Agent 的事。
        #
        # 这条边界很重要：如果 Task 自己会 discover agents，
        # 那它就从「业务对象」变成了「调度器」。
        self.required_capabilities = CapabilityProfile.of(required_capabilities)

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
        required_capabilities: Optional[Any] = None,
        expires_at: Optional[str] = None,
        task_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        participants: Optional[List[str]] = None,
        note: Optional[str] = None,
        mode: str = TaskMode.TEAM,
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
            required_capabilities=required_capabilities,
            expires_at=expires_at,
            participants=participants,
            note=note,
            mode=mode,
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
        """真终态：REJECTED / CANCELLED / EXPIRED / FAILED。"""
        return TaskStatus.is_terminal(self._status)

    @property
    def is_settled(self) -> bool:
        """
        当前 Iteration 是否已经关闭（COMPLETED）。

        和 is_terminal 不同：settled 可以通过 `reopen()` 重新打开。
        """
        return TaskStatus.is_settled(self._status)

    @property
    def is_closed(self) -> bool:
        """任务当前是否没有活跃 Iteration（settled 或 terminal）。"""
        return self.is_terminal or self.is_settled

    @property
    def is_open(self) -> bool:
        """任务是否还有活跃 Iteration。"""
        return not self.is_closed

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

    def transition_to(
        self, new_status: str, *, _reopen: bool = False
    ) -> "Task":
        """
        状态转换。非法就抛 InvalidTaskTransition。

        【报错信息要说清楚「为什么不行」】
            只说「非法转换」没用，排查的人需要知道
            「现在是哪个状态、允许去哪儿」。

        【`_reopen` 是内部参数】
            COMPLETED → IN_PROGRESS 只能通过 `reopen()` 走：
            因为 Reopen 还需要递增 iteration，普通 transition 会漏掉它。
        """
        if not TaskStatus.is_known(new_status):
            raise TaskValidationError(
                f"未知的任务状态：{new_status!r}，合法值：{TaskStatus.all()}"
            )

        if (
            self._status == TaskStatus.COMPLETED
            and new_status == TaskStatus.IN_PROGRESS
            and not _reopen
        ):
            raise InvalidTaskTransition(
                f"任务 {self.task_id} 已 COMPLETED；重新打开必须使用 "
                f"Task.reopen()，以便开启新的 Iteration 并保留历史。"
            )

        if self.is_terminal:
            raise InvalidTaskTransition(
                f"任务 {self.task_id} 已经是终态 {self._status}，不能再转换到 {new_status}。\n"
                f"终态意味着这个任务已经结束了（拒绝/取消/过期/失败）。"
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

    def claim_producer(self, agent_id: str) -> "Task":
        """
        承接这个任务：把 `agent_id` 指定为承接方（contractor）。

        【为什么需要这个方法】

            `CREATED → IN_PROGRESS` 是一条**非商业**路径
            （Root Task 不是 Commerce，见 `TRANSITIONS`）。

            商业路径里 contractor 是 `accept(price, contractor)` 产生的 ——
            谈成了，谁接活就定了。但 Root Task 没有报价与成交，
            所以必须有人**显式成为承接方**：
            「谁在干活」是后续 SUBMIT 的依据，也是
            **「生产者不能自验」**的依据。

        【它做什么 / 不做什么】
            ✅ 只设置 `contractor`
            ❌ **不改状态** —— 状态由 `start()` 按状态机推进
            ❌ 不做角色判断 —— 那是
               `TaskExecutionService.validate_role()` 的职责
               （领域层只保证「这个事实是合法的」，不保证「这个人有权做」）

        【两条领域约束】
            ① 只在 CREATED / ACCEPTED 阶段可承接
               （已经开始生产之后再换人，历史就没法解释了）
            ② 不能把已经确定的 contractor 换成别人
        """
        if not agent_id:
            raise TaskValidationError("承接方不能是空字符串")

        if self._status not in (TaskStatus.CREATED, TaskStatus.ACCEPTED):
            raise TaskValidationError(
                f"任务 {self.task_id} 当前状态是 {self._status}，不能承接。\n"
                f"  只有 CREATED / ACCEPTED 阶段可以确定承接方 —— "
                f"开工之后再换人，之前的记录就没法解释了。"
            )

        if self.contractor and self.contractor != agent_id:
            raise TaskValidationError(
                f"任务 {self.task_id} 已经有承接方 {self.contractor}，"
                f"不能改成 {agent_id}。\n"
                f"  （要换人必须先取消/失败再重建任务。）"
            )

        self.contractor = agent_id
        self.updated_at = utc_now_iso()
        return self

    def start(self) -> "Task":
        """
        开工（CREATED / ACCEPTED → IN_PROGRESS）。

        【CREATED 也能开工】
            Root Task 不走商业前置（OFFERED / ACCEPTED）。
            调用方（执行服务）会先确保承接方已确定 ——
            见 `claim_producer()` 与
            `TaskExecutionService.apply_start()`。
        """
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

    def reopen(self, reason: Optional[str] = None) -> "Task":
        """
        重新打开已完成的 Task（COMPLETED → IN_PROGRESS）。

        【它做什么】
            · iteration +1（开启下一轮）
            · 状态回到 IN_PROGRESS，可以重新 SUBMIT / VERIFY / COMPLETE
            · 旧执行历史、旧 SUBMIT、旧 VERIFY 全部保留（append-only）

        【它不做什么】
            · 不新建 Task
            · 不删除/覆盖旧记录
            · 不自动释放 Workspace 锁（Lock ≠ Task 状态）
            · 不更改 Workspace

        🔒 这是 COMPLETED → IN_PROGRESS 的唯一入口：
           `transition_to` 会拒绝绕过 `reopen()` 的直接转换。
        """
        if self._status != TaskStatus.COMPLETED:
            raise InvalidTaskTransition(
                f"只有 COMPLETED 的任务可以 Reopen；"
                f"任务 {self.task_id} 当前是 {self._status}。"
            )
        if reason:
            self.note = reason
        self.transition_to(TaskStatus.IN_PROGRESS, _reopen=True)
        self.iteration += 1
        return self

    def cancel(self, reason: Optional[str] = None) -> "Task":
        """取消。任何活跃状态都可以取消（COMPLETED 需先 Reopen）。"""
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
    # 能力要求
    # ------------------------------------------------------------
    def requires(self, capability: Any) -> bool:
        """这个任务是否要求某个能力。"""
        return self.required_capabilities.supports(capability)

    def has_capability_requirements(self) -> bool:
        return not self.required_capabilities.is_empty()

    # ------------------------------------------------------------
    # 报价历史
    # ------------------------------------------------------------
    def record_offer(
        self,
        offer_type: str,
        amount: Money,
        message_id: Optional[str] = None,
        note: Optional[str] = None,
        reply_to: Optional[str] = None,
        from_agent: Optional[str] = None,
        trace_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
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

        # 【幂等：同一个 message_id 只记一次】
        #
        #     为什么需要？
        #         同一条报价消息可能被两处处理：
        #             · 发送侧：我决定报价，记下「我报了 X」
        #             · 接收侧：对方收到，也记下「他报了 X」
        #         在单进程演示里两边共用同一个 store，
        #         于是同一条报价会被记两次 —— 而重复的报价会让
        #         「我已经还过几次价」算错，进而让谈判提前终止。
        #
        #     报价的唯一标识就是它那条消息的 message_id，
        #     所以按它去重是天然正确的。
        if message_id is not None:
            existing = self.find_offer_by_message(message_id)
            if existing is not None:
                return existing

        record = Offer(
            offer_type=offer_type,
            amount=amount,
            message_id=message_id,
            reply_to=reply_to,
            from_agent=from_agent,
            trace_id=trace_id,
            conversation_id=conversation_id,
            note=note,
        )
        self.offers.append(record)
        self.updated_at = utc_now_iso()
        return record

    def latest_offer(self) -> Optional[Offer]:
        return self.offers[-1] if self.offers else None

    def find_offer_by_message(self, message_id: str) -> Optional[Offer]:
        """
        按 message_id 找一条报价。

        【用途】ACCEPT 必须能反查到「被接受的是哪一条报价」（§18）。
        """
        for offer in reversed(self.offers):
            if offer.message_id == message_id:
                return offer
        return None

    def offer_by_type(self, offer_type: str) -> List[Offer]:
        return [o for o in self.offers if o.offer_type == offer_type]

    # ------------------------------------------------------------
    # 参与方与轮次
    # ------------------------------------------------------------
    def is_participant(self, agent_id: str) -> bool:
        """
        这个 Agent 是不是这个任务的合法参与方。

        【用途】§16：防止「Agent C 在没有成为合法参与方的情况下直接 ACCEPT」。

        【注意这里没有任何具体 Agent 的名字】
            判断依据是任务自己的字段（creator / contractor / participants），
            所以换成任何两个 Agent 都一样成立。
        """
        if not agent_id:
            return False
        return agent_id in {
            self.creator,
            self.contractor,
            *self.participants,
        }

    def counterparty_of(self, agent_id: str) -> Optional[str]:
        """
        对方的 agent_id。

        发起方的对方是承接方，反之亦然。
        """
        # 【注意不能直接 return self.contractor】
        #     承接方可能在成交前还是 None，
        #     这时要往下走去找 participants，而不是返回 None 就结束。
        if agent_id == self.creator and self.contractor:
            return self.contractor
        if self.contractor and agent_id == self.contractor:
            return self.creator
        # 还不是正式参与方时，从参与者名单里找一个别人
        for participant in self.participants:
            if participant != agent_id:
                return participant
        return None

    def is_initiator(self, agent_id: str) -> bool:
        """这个 Agent 是不是任务的发起方（付钱的那一方）。"""
        return agent_id == self.creator

    def negotiation_round_for(self, agent_id: str) -> int:
        """
        这个 Agent 已经还过几次价。

        【用途】防止无限还价（§30：超过 max_rounds 不再 COUNTER）。
        """
        return sum(
            1 for offer in self.offers
            if offer.offer_type == OfferType.COUNTER_OFFER
            and offer.from_agent == agent_id
        )

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
            # SOLO / TEAM；只影响角色规则，不影响状态机
            "mode": self.mode,
            # 当前 Iteration；Reopen 时 +1
            "iteration": self.iteration,
            "description": self.description,
            "workspace_id": self.workspace_id,
            # 【两个不同的字段，不要合并】
            "budget": self.budget.to_dict() if self.budget else None,
            "agreed_price": (
                self.agreed_price.to_dict() if self.agreed_price else None
            ),
            "acceptance": dict(self.acceptance) if self.acceptance else None,
            # 只是「需要什么能力」的声明，不是「找到了谁」
            "required_capabilities": self.required_capabilities.to_list(),
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "note": self.note,
            # 乐观锁版本号：随 Task 一起流转，save() 拿它当条件
            "version": self.version,
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
            required_capabilities=data.get("required_capabilities"),
            expires_at=data.get("expires_at"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            offers=[
                Offer.from_dict(item) for item in data.get("offers", [])
            ],
            note=data.get("note"),
            mode=data.get("mode", TaskMode.TEAM),
            iteration=data.get("iteration", 1),
            version=data.get("version", 0),
        )

    # ------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """给人看的一行摘要。"""
        return {
            "task_id": self.task_id,
            "status": self._status,
            "mode": self.mode,
            "iteration": self.iteration,
            "creator": self.creator,
            "contractor": self.contractor,
            "budget": str(self.budget) if self.budget else None,
            "agreed_price": (
                str(self.agreed_price) if self.agreed_price else None
            ),
            "workspace_id": self.workspace_id,
            "required_capabilities": self.required_capabilities.names,
            "offers": len(self.offers),
            "tracing": self.current_trace_id,
        }

    def __repr__(self) -> str:
        return (
            f"Task(task_id={self.task_id!r}, status={self._status!r}, "
            f"budget={self.budget}, agreed_price={self.agreed_price})"
        )
