"""
Task Shared Context —— Task 的共同认知层（Step 4F）

============================================================
【这是什么】
============================================================

一个 Task 一旦有多个 Agent 参与，就会出现这个问题：

    大家在数据库和协议层完全一致，
    但对「这个任务现在到底是什么、已经决定了什么、
    什么只是猜测、下一步该做什么」理解不同。

`TaskSharedContext` 就是答案：**一个 Task 当前的共同世界模型。**

============================================================
【它不是什么（四条最容易搞混的边界）】
============================================================

    ① 不是 Message History
         「有人说过」≠「现在仍然有效」
         消息不会自动进入 Context（必须经 Alignment）

    ② 不是 Task Core
         Task 的生命周期/预算/承接方**仍然只属于 Task Core**。
         这里**没有** status 字段 —— 见下面 §Domain State 的说明。

    ③ 不是 Agent Private Memory
         某个 Agent 的私下判断不能自动变成共识。
         私有认知要经过 Proposal → Alignment 才能进来。

    ④ 不是 Task Workspace
         这里说「我们**认为**这个任务是什么」，
         Workspace 说「**实际**有哪些文件」。

============================================================
【Domain State 怎么处理（§6 的硬约束）】
============================================================

Task Core 是 Task 生命周期和领域状态的**唯一权威**。
所以这个模块**刻意不存** status / agreed_price / contractor。

要展示任务当前状态时，`ContextSnapshot` 会**现场去读 Task Core**
（见 `ContextSnapshot.domain_state` 与 `domain_state_source`）——
是**引用**，不是**副本**。

    引用：Task.status 变 → Snapshot 立刻反映
    副本：两处各存一份 → 迟早漂移，而且无法回答「哪个是真的」

============================================================
【纯领域，零 I/O】
============================================================

这个模块不 import：sqlite3 / storage / communication / llm / network。
它只依赖：dataclasses / typing / core.logical_ref。

持久化在 `core/task_context_store.py`，
应用编排在 `services/task_alignment_service.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.logical_ref import validate_logical_ref

#: 第一个版本号。Revision 从 1 开始（不是 0）——
#: 这样「没有 context」和「context 的第 1 版」不会混淆。
FIRST_REVISION = 1


# ============================================================
# 时间戳
# ============================================================
def utc_now_iso() -> str:
    """当前 UTC 时间（ISO 8601，秒精度，带 Z）。"""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ============================================================
# 一、错误类型
# ============================================================
class ContextError(Exception):
    """Shared Context 相关的所有错误的基类。"""


class ContextValidationError(ContextError):
    """上下文内容本身不合法（缺字段、类型错、格式错）。"""


class InvalidContextChange(ContextValidationError):
    """
    ContextChange 不合法（类型未知、缺必填、目标不存在）。

    【为什么继承 ContextValidationError 而不是 ContextError】
        「这条变更的载荷不合法」本身就是一种校验失败 ——
        是**更具体**的那一类，不是并列的另一类。

        这样调用方可以：
            except ContextValidationError   → 一次兜住所有「内容不对」
            except InvalidContextChange     → 只关心变更载荷的问题

        反过来（并列）的话，用 ContextValidationError 兜不住
        变更载荷错误 —— 那是个容易漏的口袋。
    """


class NotAParticipantError(ContextError):
    """
    不是这个 Task 的合法参与方。

    【这是 §16 的边界】
        只有合法 Task Participant 才能提交 Change / 裁决。
        非参与 Agent 不得修改。

    【注意它不是 Permission System】
        判断依据是 Task 自己的字段（creator / contractor / participants），
        不涉及文件权限、路径权限、Workspace 权限。
    """

    def __init__(self, agent_id: str, task_id: str, action: str = "提交变更"):
        self.agent_id = agent_id
        self.task_id = task_id
        super().__init__(
            f"Agent {agent_id!r} 不是 Task {task_id!r} 的参与方，不能{action}。\n"
            f"（参与方 = creator / contractor / participants 之一。\n"
            f" 这条边界防止无关 Agent 改写别人的共同认知。）"
        )


class ConcurrentContextModification(ContextError):
    """
    乐观锁冲突：有人在你之前改了 Context。

    【和 `ConcurrentTaskModification` 是同一个模式】

        你读到 revision = 5
        B 先提交成功 → revision = 6
        你拿着 expected_revision = 5 再提交 → **必须失败**

    【绝不静默覆盖】

        不做自动 retry / merge / rebase / LLM merge。
        那会让「共同认知」变成一个自己会变的东西。

        要重新提交，调用方得**显式**基于新 revision 再来一次。
    """

    def __init__(self, task_id: str, expected_revision: int,
                 actual_revision: Optional[int] = None):
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision

        detail = (
            f"当前已是 revision {actual_revision}"
            if actual_revision is not None
            else "当前 revision 已变化"
        )
        super().__init__(
            f"Task {task_id!r} 的 Shared Context 乐观锁冲突："
            f"你基于 revision {expected_revision}，但{detail}。\n"
            f"（没有人被静默覆盖。请基于最新 revision 重新提交。）"
        )


class ContextNotFoundError(ContextError):
    """这个 Task 还没有 Shared Context（或指定的 revision 不存在）。"""


class ChangeAlreadyDecidedError(ContextError):
    """这个 Change 已经裁决过了（ACCEPTED / REJECTED 是终态）。"""


class SelfAlignmentError(ContextError):
    """
    提案人试图自己批准一个**决策**。

    【为什么 DECIDE 不允许自批】
        这是我方规范（Constitution §8.2、TASK_SHARED_CONTEXT.md §3.4）
        已经定下的对齐最低要求：

            「一个 Agent 自己说了算，不算共同决定。」

        其它类型（FACT / ASSUME / QUESTION / ASSIGN）是**声明性质**的，
        自己声明自己的观察是合理的，所以允许自批。
        但「决定」必须至少有一个**别人**确认。
    """


# ============================================================
# 二、变更类型
# ============================================================
class ContextChangeType:
    """
    ContextChange 的类型。

    【怎么分类的】
        按「这条变更是**声明**还是**决定**」来分 ——
        因为这两类需要不同的对齐强度（见 SelfAlignmentError）。

        声明类（可自批）：FACT / ASSUME / QUESTION / ASSIGN
        决定类（需他人确认）：DECIDE
        修正类：RESOLVE_QUESTION / CONFIRM_ASSUMPTION
        标量类：SET_OBJECTIVE / ADD_CONSTRAINT / ADD_ACCEPTANCE_CRITERION

    【为什么不做成 Enum】
        项目里 TaskStatus / MessageType / OfferType 都是
        「类 + 类方法返回 tuple」的风格（见 core/task.py）。
        保持一致。
    """

    SET_OBJECTIVE = "SET_OBJECTIVE"
    ADD_CONSTRAINT = "ADD_CONSTRAINT"
    ADD_ACCEPTANCE_CRITERION = "ADD_ACCEPTANCE_CRITERION"
    DECIDE = "DECIDE"
    ASSUME = "ASSUME"
    QUESTION = "QUESTION"
    ASSIGN = "ASSIGN"
    FACT = "FACT"
    RESOLVE_QUESTION = "RESOLVE_QUESTION"
    CONFIRM_ASSUMPTION = "CONFIRM_ASSUMPTION"

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return (
            cls.SET_OBJECTIVE,
            cls.ADD_CONSTRAINT,
            cls.ADD_ACCEPTANCE_CRITERION,
            cls.DECIDE,
            cls.ASSUME,
            cls.QUESTION,
            cls.ASSIGN,
            cls.FACT,
            cls.RESOLVE_QUESTION,
            cls.CONFIRM_ASSUMPTION,
        )

    #: 需要「非提案人确认」的类型（对齐最低要求）
    @classmethod
    def requires_other_aligner(cls) -> Tuple[str, ...]:
        return (cls.DECIDE,)

    #: 需要 target 指向已存在的记录 id 的类型
    @classmethod
    def targets_existing_record(cls) -> Tuple[str, ...]:
        return (cls.RESOLVE_QUESTION, cls.CONFIRM_ASSUMPTION)


class ContextChangeStatus:
    """Change 的生命周期。PROPOSED 是唯一非终态。"""

    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return (cls.PROPOSED, cls.ACCEPTED, cls.REJECTED)

    @classmethod
    def terminal(cls) -> Tuple[str, ...]:
        return (cls.ACCEPTED, cls.REJECTED)


class AssumptionStatus:
    """假设的状态。**未验证不得写成 FACT。**"""

    UNVERIFIED = "UNVERIFIED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return (cls.UNVERIFIED, cls.CONFIRMED, cls.REJECTED)


class QuestionStatus:
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    WITHDRAWN = "WITHDRAWN"

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return (cls.OPEN, cls.ANSWERED, cls.WITHDRAWN)


class AssignmentStatus:
    """
    分工状态。

    【注意：这只是「记录」，不是「调度」】
        没有 assign() 的自动派发、没有 worker pool、没有 scheduler。
        这里只回答「谁负责什么」这个事实问题。
    """

    ACTIVE = "ACTIVE"
    DONE = "DONE"
    RELEASED = "RELEASED"
    BLOCKED = "BLOCKED"

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return (cls.ACTIVE, cls.DONE, cls.RELEASED, cls.BLOCKED)


#: 「验收责任」的 `responsibility` 取值。
#:
#: 【为什么复用 AssignmentRecord，而不是新建一套角色系统】
#:
#:     验收责任本来就是「谁负责什么」的一种。已经有
#:     `AssignmentRecord` 表达这件事，而且它已经有
#:     `ASSIGN` 变更类型、对齐规则、修订历史与持久化。
#:
#:     新建一张「Acceptor 角色表」会立刻产生**第二套分工真相**：
#:     同一个事实（谁负责验收）在两处各存一份，然后靠人去同步。
#:     那正是本项目一直在消除的东西。
#:
#: 【为什么是常量，而不是枚举 / 白名单】
#:
#:     `responsibility` 是**开放文本** —— 研究任务写 "research"、
#:     写作任务写 "drafting"、翻译任务写 "translation"。
#:     设白名单会把「Agent 自主决定分工」变成「系统规定有哪些分工」，
#:     那是把 Agent Strategy 写进系统。
#:
#:     这里只给最常用的那一个起个名字，让「谁负责验收」
#:     可以被**查询**（`TaskSharedContext.acceptors()`），
#:     而不是靠人肉读字符串。
ACCEPTANCE_RESPONSIBILITY = "acceptance"


# ============================================================
# 三、记录类型（不可变）
# ============================================================
def _require_text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    """公共的文本校验。"""
    if not isinstance(value, str):
        raise ContextValidationError(
            f"{field_name} 必须是字符串，收到 {type(value).__name__}"
        )
    text = value.strip()
    if not text and not allow_empty:
        raise ContextValidationError(f"{field_name} 不能是空白")
    return text


def _freeze_tuple(values: Any, field_name: str) -> Tuple[str, ...]:
    """把一个字符串序列冻结成 tuple。"""
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise ContextValidationError(
            f"{field_name} 必须是序列，不能是单个字符串"
            f"（是不是忘了用 [ ] 包起来？）"
        )
    return tuple(_require_text(v, f"{field_name}[]") for v in values)


@dataclass(frozen=True)
class DecisionRecord:
    """
    一个**已经形成确认**的任务决策。

    【为什么不能只存一句话】
        未来 Agent 不应该只看到「之前决定用 B」，
        而应该能知道「**为什么**决定 B、**谁**确认的」。
        所以 reason 和 accepted_by 都是必填。
    """

    decision_id: str
    statement: str
    proposed_by: str
    reason: str
    accepted_by: Tuple[str, ...] = ()
    created_at: str = ""
    revision: int = 0
    supersedes: Optional[str] = None
    status: str = "ACCEPTED"

    def __post_init__(self):
        object.__setattr__(
            self, "statement",
            _require_text(self.statement, "DecisionRecord.statement"),
        )
        object.__setattr__(
            self, "reason",
            _require_text(self.reason, "DecisionRecord.reason"),
        )
        object.__setattr__(
            self, "proposed_by",
            _require_text(self.proposed_by, "DecisionRecord.proposed_by"),
        )
        object.__setattr__(
            self, "accepted_by",
            _freeze_tuple(self.accepted_by, "DecisionRecord.accepted_by"),
        )

        # ---- 【对齐最低要求】至少一个非提案人确认 ----
        if not self.accepted_by:
            raise ContextValidationError(
                "DecisionRecord 必须至少有一个确认人（accepted_by）。\n"
                "（没有确认人的「决定」只是一句提议，不是共同决定。）"
            )
        if not any(a != self.proposed_by for a in self.accepted_by):
            raise ContextValidationError(
                f"DecisionRecord 的确认人里有非提案人吗？"
                f"accepted_by={self.accepted_by}，proposed_by={self.proposed_by!r}\n"
                f"（一个 Agent 自己说了算，不算共同决定。）"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "statement": self.statement,
            "proposed_by": self.proposed_by,
            "reason": self.reason,
            "accepted_by": list(self.accepted_by),
            "created_at": self.created_at,
            "revision": self.revision,
            "supersedes": self.supersedes,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionRecord":
        return cls(
            decision_id=data["decision_id"],
            statement=data["statement"],
            proposed_by=data["proposed_by"],
            reason=data.get("reason", ""),
            accepted_by=tuple(data.get("accepted_by", ())),
            created_at=data.get("created_at", ""),
            revision=int(data.get("revision", 0)),
            supersedes=data.get("supersedes"),
            status=data.get("status", "ACCEPTED"),
        )


@dataclass(frozen=True)
class AssumptionRecord:
    """
    一个**尚未被完全验证**的假设。

    【为什么必须和 Fact 分开】
        Fact:       Python version = 3.13            （已验证）
        Assumption: 这个外部 API 支持幂等请求          （未验证）

        如果两者混在一起，一个 Agent 的猜测会悄悄变成系统事实。
        这正是多 Agent 系统最危险的一类错误。
    """

    assumption_id: str
    statement: str
    held_by: str
    reason: str = ""
    status: str = AssumptionStatus.UNVERIFIED
    created_at: str = ""
    revision: int = 0
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(
            self, "statement",
            _require_text(self.statement, "AssumptionRecord.statement"),
        )
        object.__setattr__(
            self, "held_by",
            _require_text(self.held_by, "AssumptionRecord.held_by"),
        )
        if self.status not in AssumptionStatus.all():
            raise ContextValidationError(
                f"AssumptionRecord.status 必须是 "
                f"{list(AssumptionStatus.all())} 之一，收到 {self.status!r}"
            )

    @property
    def is_confirmed(self) -> bool:
        return self.status == AssumptionStatus.CONFIRMED

    @property
    def is_open(self) -> bool:
        return self.status == AssumptionStatus.UNVERIFIED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assumption_id": self.assumption_id,
            "statement": self.statement,
            "held_by": self.held_by,
            "reason": self.reason,
            "status": self.status,
            "created_at": self.created_at,
            "revision": self.revision,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AssumptionRecord":
        return cls(
            assumption_id=data["assumption_id"],
            statement=data["statement"],
            held_by=data["held_by"],
            reason=data.get("reason", ""),
            status=data.get("status", AssumptionStatus.UNVERIFIED),
            created_at=data.get("created_at", ""),
            revision=int(data.get("revision", 0)),
            resolved_at=data.get("resolved_at"),
            resolved_by=data.get("resolved_by"),
        )


@dataclass(frozen=True)
class OpenQuestion:
    """
    「当前还不知道什么」。

    【为什么「不知道」值得单独存】
        一个显式的 Open Question 让所有人知道这里有不确定性；
        一个**缺失的** Open Question 让每个 Agent 各自猜一个答案，
        然后在某个时刻发现猜的不一样。

        「不知道」比「错误地以为自己知道」安全得多。
    """

    question_id: str
    question: str
    owner: Optional[str] = None
    status: str = QuestionStatus.OPEN
    created_at: str = ""
    revision: int = 0
    answer: Optional[str] = None
    answered_by: Optional[str] = None
    answered_at: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(
            self, "question",
            _require_text(self.question, "OpenQuestion.question"),
        )
        if self.status not in QuestionStatus.all():
            raise ContextValidationError(
                f"OpenQuestion.status 必须是 "
                f"{list(QuestionStatus.all())} 之一，收到 {self.status!r}"
            )

    @property
    def is_open(self) -> bool:
        return self.status == QuestionStatus.OPEN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "owner": self.owner,
            "status": self.status,
            "created_at": self.created_at,
            "revision": self.revision,
            "answer": self.answer,
            "answered_by": self.answered_by,
            "answered_at": self.answered_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OpenQuestion":
        return cls(
            question_id=data["question_id"],
            question=data["question"],
            owner=data.get("owner"),
            status=data.get("status", QuestionStatus.OPEN),
            created_at=data.get("created_at", ""),
            revision=int(data.get("revision", 0)),
            answer=data.get("answer"),
            answered_by=data.get("answered_by"),
            answered_at=data.get("answered_at"),
        )


@dataclass(frozen=True)
class AssignmentRecord:
    """
    「谁负责什么」。

    【🔒 这只是记录，不是 Scheduler】
        没有自动派发、没有 worker pool、没有负载均衡、
        没有「任务分给谁最合适」的推理。

        想自动调度是**未来阶段**的事，而且很可能永远不该由
        Shared Context 来干 —— 那是 TaskManager 的领域，
        而 TaskManager 是明确禁止的。
    """

    assignment_id: str
    agent_id: str
    responsibility: str
    status: str = AssignmentStatus.ACTIVE
    assigned_by: str = ""
    assigned_at: str = ""
    revision: int = 0
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(
            self, "agent_id",
            _require_text(self.agent_id, "AssignmentRecord.agent_id"),
        )
        object.__setattr__(
            self, "responsibility",
            _require_text(self.responsibility, "AssignmentRecord.responsibility"),
        )
        if self.status not in AssignmentStatus.all():
            raise ContextValidationError(
                f"AssignmentRecord.status 必须是 "
                f"{list(AssignmentStatus.all())} 之一，收到 {self.status!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "agent_id": self.agent_id,
            "responsibility": self.responsibility,
            "status": self.status,
            "assigned_by": self.assigned_by,
            "assigned_at": self.assigned_at,
            "revision": self.revision,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AssignmentRecord":
        return cls(
            assignment_id=data["assignment_id"],
            agent_id=data["agent_id"],
            responsibility=data["responsibility"],
            status=data.get("status", AssignmentStatus.ACTIVE),
            assigned_by=data.get("assigned_by", ""),
            assigned_at=data.get("assigned_at", ""),
            revision=int(data.get("revision", 0)),
            notes=data.get("notes", ""),
        )


@dataclass(frozen=True)
class SharedFact:
    """
    一个**已确认**的事实。

    【source 必填】
        否则它和 Assumption 就没区别了 ——
        「有来源」正是 Fact 之所以是 Fact 的理由。
    """

    fact_id: str
    statement: str
    source: str
    confirmed_by: str = ""
    created_at: str = ""
    revision: int = 0
    supersedes: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(
            self, "statement",
            _require_text(self.statement, "SharedFact.statement"),
        )
        object.__setattr__(
            self, "source",
            _require_text(self.source, "SharedFact.source"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "source": self.source,
            "confirmed_by": self.confirmed_by,
            "created_at": self.created_at,
            "revision": self.revision,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SharedFact":
        return cls(
            fact_id=data["fact_id"],
            statement=data["statement"],
            source=data["source"],
            confirmed_by=data.get("confirmed_by", ""),
            created_at=data.get("created_at", ""),
            revision=int(data.get("revision", 0)),
            supersedes=data.get("supersedes"),
        )


# ============================================================
# 四、TaskSharedContext —— 某一版共享上下文的完整内容
# ============================================================
@dataclass(frozen=True)
class TaskSharedContext:
    """
    Task 在**某一版**上的完整共享上下文。

    【为什么是不可变的】
        每一版都是一份**历史事实**。要改就产生新的一版
        （`ContextRevision`），而不是就地改这一份。

        「Revision 18 取代 17」不等于「17 可以删掉」——
        必须保留历史，才能回答「当时为什么那样认为」。

    【⚠️ 这里刻意没有的东西】

        ❌ status / state          → Task Core 的唯一权威（§6）
        ❌ agreed_price / contractor / budget → 同上
        ❌ 任何可以独立修改的生命周期字段

        要展示任务状态时由 `ContextSnapshot` **现场读 Task Core**。

    【通用任务模型（§8）】
        字段里没有 file / code / git / repo / worktree / test。
        它同样适用于 research / writing / translation / design /
        data_analysis / operations…… 不是 CodeSharedContext。
    """

    context_id: str
    task_id: str
    revision: int = FIRST_REVISION

    objective: str = ""
    constraints: Tuple[str, ...] = ()
    acceptance_criteria: Tuple[str, ...] = ()

    decisions: Tuple[DecisionRecord, ...] = ()
    assumptions: Tuple[AssumptionRecord, ...] = ()
    open_questions: Tuple[OpenQuestion, ...] = ()
    assignments: Tuple[AssignmentRecord, ...] = ()
    facts: Tuple[SharedFact, ...] = ()

    terminology: Mapping[str, str] = field(default_factory=dict)

    #: 相关引用。**只是逻辑名字**，不是路径、不是 URL、不是 I/O。
    artifact_refs: Tuple[str, ...] = ()

    #: 这一版为什么产生
    change_reason: str = ""
    created_by: str = ""
    created_at: str = ""
    parent_revision: Optional[int] = None

    def __post_init__(self):
        object.__setattr__(
            self, "context_id",
            _require_text(self.context_id, "TaskSharedContext.context_id"),
        )
        object.__setattr__(
            self, "task_id",
            _require_text(self.task_id, "TaskSharedContext.task_id"),
        )

        if not isinstance(self.revision, int) or self.revision < FIRST_REVISION:
            raise ContextValidationError(
                f"revision 必须是从 {FIRST_REVISION} 开始的整数，"
                f"收到 {self.revision!r}"
            )

        for name in ("constraints", "acceptance_criteria", "artifact_refs"):
            object.__setattr__(
                self, name,
                _freeze_tuple(getattr(self, name), f"TaskSharedContext.{name}"),
            )

        # 引用必须是逻辑名字（同 result_ref 的安全规则）
        refs = tuple(
            validate_logical_ref(r, field="TaskSharedContext.artifact_refs[]")
            for r in self.artifact_refs
        )
        object.__setattr__(self, "artifact_refs", refs)

        # terminology 冻结（免得下游把它当袋子用）
        if not isinstance(self.terminology, Mapping):
            raise ContextValidationError("terminology 必须是映射（dict）")
        object.__setattr__(
            self, "terminology",
            MappingProxyType(
                {
                    _require_text(k, "terminology.key"): _require_text(
                        v, "terminology.value", allow_empty=True
                    )
                    for k, v in self.terminology.items()
                }
            ),
        )

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def decision_by_id(self, decision_id: str) -> Optional[DecisionRecord]:
        return next(
            (d for d in self.decisions if d.decision_id == decision_id), None
        )

    def assumption_by_id(self, assumption_id: str) -> Optional[AssumptionRecord]:
        return next(
            (a for a in self.assumptions if a.assumption_id == assumption_id), None
        )

    def question_by_id(self, question_id: str) -> Optional[OpenQuestion]:
        return next(
            (q for q in self.open_questions if q.question_id == question_id), None
        )

    # ------------------------------------------------------------
    # 验收责任（Acceptance Responsibility）
    # ------------------------------------------------------------
    def assignees_for(self, responsibility: str) -> Tuple[str, ...]:
        """
        按 `responsibility` 取负责的 Agent（只读查询）。

        🔒 只读：不派发、不调度、不改任何东西。
        🔒 `RELEASED` 的分配**不算** —— 那是「显式解除责任」。
           `DONE` / `BLOCKED` 仍算（DONE 之后返工还要能再验收；
           BLOCKED 只是卡住，责任还在）。
        🔒 结果去重且排序 —— 相同输入永远给相同输出。
        """
        text = str(responsibility or "").strip().lower()
        if not text:
            return ()
        found = []
        for assignment in self.assignments:
            if (assignment.responsibility or "").strip().lower() != text:
                continue
            if assignment.status == AssignmentStatus.RELEASED:
                continue
            if assignment.agent_id not in found:
                found.append(assignment.agent_id)
        return tuple(sorted(found))

    def acceptors(self) -> Tuple[str, ...]:
        """
        当前承担「验收责任」的 Agent。

        🔒 不固定角色：任何 Task Participant 都可以被指派，
           也可以有 0 个 / 1 个 / 多个。
        🔒 它**不会**让生产者绕过「不能自验」——
           那条规则在
           `TaskExecutionService.validate_role()` 里，
           而且那里 **contractor 身份优先于 acceptor**：
           就算生产者也给自己派了验收责任，VERIFY 依然会被拒。
        """
        return self.assignees_for(ACCEPTANCE_RESPONSIBILITY)

    def has_acceptance_responsibility(self) -> bool:
        """
        有没有人负责验收。

        这是 Completion 的前置条件之一（见
        `services/task_execution_service.py` 的 Completion 检查）。
        """
        return bool(self.acceptors())

    def acceptance_criteria_list(self) -> Tuple[str, ...]:
        """
        Definition of Done（只读）。

        🔒 权威来源就是本对象的 `acceptance_criteria` 字段 ——
           刻意**没有**第二套 DoD 数据结构。
        """
        return tuple(c for c in self.acceptance_criteria if str(c).strip())

    def has_definition_of_done(self) -> bool:
        """有没有可验证的 Definition of Done。"""
        return bool(self.acceptance_criteria_list())

    @property
    def open_question_count(self) -> int:
        return sum(1 for q in self.open_questions if q.is_open)

    @property
    def unverified_assumption_count(self) -> int:
        return sum(1 for a in self.assumptions if a.is_open)

    @property
    def is_empty(self) -> bool:
        """除了 id / revision 之外什么都没有。"""
        return not any((
            self.objective,
            self.constraints,
            self.acceptance_criteria,
            self.decisions,
            self.assumptions,
            self.open_questions,
            self.assignments,
            self.facts,
            self.terminology,
            self.artifact_refs,
        ))

    # ------------------------------------------------------------
    # 序列化（持久化用物化全文，见 Phase B 设计决定 ④）
    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "context_id": self.context_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "decisions": [d.to_dict() for d in self.decisions],
            "assumptions": [a.to_dict() for a in self.assumptions],
            "open_questions": [q.to_dict() for q in self.open_questions],
            "assignments": [a.to_dict() for a in self.assignments],
            "facts": [f.to_dict() for f in self.facts],
            "terminology": dict(self.terminology),
            "artifact_refs": list(self.artifact_refs),
            "change_reason": self.change_reason,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "parent_revision": self.parent_revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSharedContext":
        return cls(
            context_id=data["context_id"],
            task_id=data["task_id"],
            revision=int(data.get("revision", FIRST_REVISION)),
            objective=data.get("objective", ""),
            constraints=tuple(data.get("constraints", ())),
            acceptance_criteria=tuple(data.get("acceptance_criteria", ())),
            decisions=tuple(
                DecisionRecord.from_dict(d) for d in data.get("decisions", ())
            ),
            assumptions=tuple(
                AssumptionRecord.from_dict(a) for a in data.get("assumptions", ())
            ),
            open_questions=tuple(
                OpenQuestion.from_dict(q) for q in data.get("open_questions", ())
            ),
            assignments=tuple(
                AssignmentRecord.from_dict(a) for a in data.get("assignments", ())
            ),
            facts=tuple(SharedFact.from_dict(f) for f in data.get("facts", ())),
            terminology=dict(data.get("terminology", {})),
            artifact_refs=tuple(data.get("artifact_refs", ())),
            change_reason=data.get("change_reason", ""),
            created_by=data.get("created_by", ""),
            created_at=data.get("created_at", ""),
            parent_revision=data.get("parent_revision"),
        )

    # ------------------------------------------------------------
    def with_revision(self, revision: int, **changes) -> "TaskSharedContext":
        """派生新的一版（不修改自己）。"""
        return replace(self, revision=revision, **changes)


# ============================================================
# 五、ContextChange —— Agent 的提议
# ============================================================
@dataclass(frozen=True)
class ContextChange:
    """
    Agent 对 Shared Context 提出的一次修改。

    ============================================================
    【为什么不能直接改 Context】
    ============================================================

        禁止：  Agent ──直接 UPDATE──▶ Shared Context

        必须：  Agent ──ContextChange──▶ Alignment ──▶ Revision

        这条规则的意义是：让「共同认知」永远是**被对齐过的**，
        而不是「最后一个写的人说了算」。

    ============================================================
    【Change 有独立生命周期】
    ============================================================

        PROPOSED ──accept──▶ ACCEPTED   （终态）
             └────reject──▶ REJECTED   （终态）

        被拒绝的 Change **不得改变**当前有效的 Shared Context。
        而且它会被保留下来 —— 「当时讨论过但没通过」
        本身就是有价值的历史。
    """

    change_id: str
    task_id: str
    proposed_by: str
    change_type: str

    #: 提议者基于哪一版提出的（乐观锁的 expected_revision）
    target_revision: int

    #: 变更的载荷。含义随 change_type 变化
    #:   SET_OBJECTIVE     → new_value 是字符串
    #:   ADD_*             → new_value 是字符串
    #:   DECIDE            → new_value = {"statement":..., "reason":...}
    #:   ASSUME            → new_value = {"statement":..., "reason":...}
    #:   QUESTION          → new_value = {"question":..., "owner":...}
    #:   ASSIGN            → new_value = {"agent_id":..., "responsibility":...}
    #:   FACT              → new_value = {"statement":..., "source":...}
    #:   RESOLVE_QUESTION  → target = question_id, new_value = {"answer":...}
    #:   CONFIRM_ASSUMPTION→ target = assumption_id, new_value = {"evidence":...}
    new_value: Any = None

    #: 针对哪一条已有记录（RESOLVE_QUESTION / CONFIRM_ASSUMPTION 用）
    target: Optional[str] = None

    #: 提议者认为当前值是什么（标量类变更用；可空）
    expected_value: Any = None

    #: **为什么**要改。必填 —— 没有理由的变更无法被有效对齐
    reason: str = ""

    #: 依据是什么（消息 id / 观察 / 外部来源）
    source: str = ""

    created_at: str = ""
    status: str = ContextChangeStatus.PROPOSED

    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decided_reason: str = ""

    #: 裁决后形成的 revision（ACCEPTED 时填）
    result_revision: Optional[int] = None

    def __post_init__(self):
        for name in ("change_id", "task_id", "proposed_by"):
            object.__setattr__(
                self, name,
                _require_text(getattr(self, name), f"ContextChange.{name}"),
            )

        if self.change_type not in ContextChangeType.all():
            raise InvalidContextChange(
                f"未知的 change_type：{self.change_type!r}\n"
                f"（已知类型：{list(ContextChangeType.all())}）"
            )

        if not isinstance(self.target_revision, int) or \
                self.target_revision < FIRST_REVISION:
            raise InvalidContextChange(
                f"target_revision 必须是从 {FIRST_REVISION} 开始的整数，"
                f"收到 {self.target_revision!r}"
            )

        object.__setattr__(
            self, "reason",
            _require_text(self.reason, "ContextChange.reason"),
        )

        if self.status not in ContextChangeStatus.all():
            raise InvalidContextChange(
                f"未知的 status：{self.status!r}"
            )

    # ------------------------------------------------------------
    @property
    def is_proposed(self) -> bool:
        return self.status == ContextChangeStatus.PROPOSED

    @property
    def is_accepted(self) -> bool:
        return self.status == ContextChangeStatus.ACCEPTED

    @property
    def is_rejected(self) -> bool:
        return self.status == ContextChangeStatus.REJECTED

    @property
    def requires_other_aligner(self) -> bool:
        """
        这条变更是否需要一个**非提案人**来确认。

        DECIDE 需要（「决定」必须有人附议）；
        声明类不需要（自己声明自己的观察是合理的）。
        """
        return self.change_type in ContextChangeType.requires_other_aligner()

    # ------------------------------------------------------------
    # 载荷读取（统一在这里做校验，别处不用再猜结构）
    # ------------------------------------------------------------
    def payload(self) -> Mapping[str, Any]:
        if not isinstance(self.new_value, Mapping):
            raise InvalidContextChange(
                f"{self.change_type} 的 new_value 必须是映射（dict），"
                f"收到 {type(self.new_value).__name__}"
            )
        return self.new_value

    def require_payload_field(self, name: str) -> str:
        """
        取一个必填的载荷字段。

        【为什么把 ContextValidationError 转成 InvalidContextChange】
            `_require_text` 抛的是通用的 `ContextValidationError`。
            但「这条**变更**的载荷缺字段」是一个**更具体**的问题类别 ——
            调用方想单独处理它时不该被迫去 catch 通用类型。

            （InvalidContextChange 本身继承 ContextValidationError，
              所以原来 catch 父类的调用方**完全不受影响**。）
        """
        value = self.payload().get(name)
        try:
            return _require_text(value, f"{self.change_type}.{name}")
        except ContextValidationError as exc:
            raise InvalidContextChange(str(exc)) from exc

    def scalar_text(self) -> str:
        try:
            return _require_text(self.new_value, f"{self.change_type}.new_value")
        except ContextValidationError as exc:
            raise InvalidContextChange(str(exc)) from exc

    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "change_id": self.change_id,
            "task_id": self.task_id,
            "proposed_by": self.proposed_by,
            "change_type": self.change_type,
            "target_revision": self.target_revision,
            "new_value": self.new_value,
            "target": self.target,
            "expected_value": self.expected_value,
            "reason": self.reason,
            "source": self.source,
            "created_at": self.created_at,
            "status": self.status,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decided_reason": self.decided_reason,
            "result_revision": self.result_revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextChange":
        return cls(
            change_id=data["change_id"],
            task_id=data["task_id"],
            proposed_by=data["proposed_by"],
            change_type=data["change_type"],
            target_revision=int(data["target_revision"]),
            new_value=data.get("new_value"),
            target=data.get("target"),
            expected_value=data.get("expected_value"),
            reason=data.get("reason", ""),
            source=data.get("source", ""),
            created_at=data.get("created_at", ""),
            status=data.get("status", ContextChangeStatus.PROPOSED),
            decided_at=data.get("decided_at"),
            decided_by=data.get("decided_by"),
            decided_reason=data.get("decided_reason", ""),
            result_revision=data.get("result_revision"),
        )


# ============================================================
# 六、ContextRevision —— 一次成功形成的版本
# ============================================================
@dataclass(frozen=True)
class ContextRevision:
    """
    一版 Shared Context 的**存档**。

    ============================================================
    【append-only：永不 UPDATE】
    ============================================================

        Revision 1 ─┐
        Revision 2 ─┤  全部保留
        Revision 3 ─┘

        「Revision 3 取代了 2」**不等于**「2 可以删掉」。

        为什么：
            一个 Agent 看到「Revision 7」时，必须能回答
            「这个共享世界模型来自什么」——
            包括「上一版是什么、谁改的、为什么改」。

        所以存储层对 revisions 表**只 INSERT、只 SELECT**。
        有守卫在检查没有任何 UPDATE/DELETE 语句。
    """

    task_id: str
    revision: int
    context: TaskSharedContext
    created_at: str = ""
    created_by: str = ""
    parent_revision: Optional[int] = None
    change_id: Optional[str] = None
    change_reason: str = ""

    def __post_init__(self):
        object.__setattr__(
            self, "task_id", _require_text(self.task_id, "ContextRevision.task_id")
        )
        if not isinstance(self.revision, int) or self.revision < FIRST_REVISION:
            raise ContextValidationError(
                f"revision 必须 >= {FIRST_REVISION}，收到 {self.revision!r}"
            )
        if self.revision != self.context.revision:
            raise ContextValidationError(
                f"ContextRevision.revision ({self.revision}) 与 "
                f"context.revision ({self.context.revision}) 不一致"
            )
        if self.task_id != self.context.task_id:
            raise ContextValidationError(
                f"ContextRevision.task_id ({self.task_id!r}) 与 "
                f"context.task_id ({self.context.task_id!r}) 不一致"
            )

    @property
    def is_initial(self) -> bool:
        return self.revision == FIRST_REVISION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "context": self.context.to_dict(),
            "created_at": self.created_at,
            "created_by": self.created_by,
            "parent_revision": self.parent_revision,
            "change_id": self.change_id,
            "change_reason": self.change_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextRevision":
        return cls(
            task_id=data["task_id"],
            revision=int(data["revision"]),
            context=TaskSharedContext.from_dict(data["context"]),
            created_at=data.get("created_at", ""),
            created_by=data.get("created_by", ""),
            parent_revision=data.get("parent_revision"),
            change_id=data.get("change_id"),
            change_reason=data.get("change_reason", ""),
        )


# ============================================================
# 七、ContextSnapshot —— 读取视图
# ============================================================
@dataclass(frozen=True)
class ContextSnapshot:
    """
    给某个 Agent 看的、在**某一版**上的可消费视图。

    ============================================================
    【🔒 它不是 Source of Truth】
    ============================================================

        它是从 `ContextRevision` **派生**出来的。
        没有独立的写入路径 —— 不可能出现
        「Snapshot 说 A，Context 说 B」这种情况。

    ============================================================
    【domain_state 为什么是「引用」而不是「副本」】
    ============================================================

        §6 要求 Task Core 是领域状态的唯一权威。
        所以这里的 `domain_state` 是**读的时候现场从 Task Core 取的**，
        Context 自己**不存** status。

            Task Core: status = SUBMITTED
            Context  : 不存 status，只在快照里引过来
            → status 变了，下次快照自动反映

        如果 Context 也存一份 status，就会出现两个都声称正确的值 ——
        那是明确禁止的。
    """

    task_id: str
    context_id: str
    revision: int
    taken_at: str
    context: TaskSharedContext

    #: **来自 Task Core 的引用**（不是 Context 拥有的事实）
    domain_state: Mapping[str, Any] = field(default_factory=dict)
    domain_state_source: str = "task_core"

    #: 最近的变更（给人看的摘要）
    recent_changes: Tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        object.__setattr__(
            self, "domain_state", MappingProxyType(dict(self.domain_state))
        )
        object.__setattr__(
            self,
            "recent_changes",
            tuple(MappingProxyType(dict(c)) for c in self.recent_changes),
        )

    @property
    def open_questions(self) -> Tuple[OpenQuestion, ...]:
        return tuple(q for q in self.context.open_questions if q.is_open)

    @property
    def unverified_assumptions(self) -> Tuple[AssumptionRecord, ...]:
        return tuple(a for a in self.context.assumptions if a.is_open)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "context_id": self.context_id,
            "revision": self.revision,
            "taken_at": self.taken_at,
            "context": self.context.to_dict(),
            "domain_state": dict(self.domain_state),
            "domain_state_source": self.domain_state_source,
            "recent_changes": [dict(c) for c in self.recent_changes],
        }


# ============================================================
# 八、把 Change 应用到 Context（Alignment 的核心）
# ============================================================
def apply_change(
    context: TaskSharedContext,
    change: ContextChange,
    *,
    accepted_by: str,
    new_revision: int,
    decided_at: Optional[str] = None,
    decided_reason: str = "",
) -> TaskSharedContext:
    """
    把一条**已获批准**的 change 应用到一个 context 上，产生新的一版。

    ============================================================
    【纯函数】
    ============================================================

        不改输入，不碰数据库，不读时间之外的外部状态。
        持久化由调用方（store / service）负责。

    ============================================================
    【为什么 accepted_by 是必填的】
    ============================================================

        每一次 revision 都要能回答「谁批准了这一步」。
        没有批准人的版本变化 = 静默修改，那是禁止的。

    ============================================================
    【为什么 DECIDE 要额外检查】
    ============================================================

        见 `SelfAlignmentError`：一个 Agent 自己说了算不算共同决定。
        这里只做「同意者里有没有非提案人」的检查，
        不实现投票/quorum（规范明确禁止）。
    """
    if not change.is_accepted:
        raise InvalidContextChange(
            f"只有 ACCEPTED 的 change 才能应用，"
            f"当前 status={change.status!r}"
        )

    if change.task_id != context.task_id:
        raise InvalidContextChange(
            f"change.task_id ({change.task_id!r}) 与 "
            f"context.task_id ({context.task_id!r}) 不一致"
        )

    if change.target_revision != context.revision:
        raise ConcurrentContextModification(
            task_id=context.task_id,
            expected_revision=change.target_revision,
            actual_revision=context.revision,
        )

    stamp = decided_at or utc_now_iso()
    kind = change.change_type

    # ------------------------------------------------------------
    # 标量 / 追加类
    # ------------------------------------------------------------
    if kind == ContextChangeType.SET_OBJECTIVE:
        updated = replace(context, objective=change.scalar_text())

    elif kind == ContextChangeType.ADD_CONSTRAINT:
        updated = replace(
            context, constraints=context.constraints + (change.scalar_text(),)
        )

    elif kind == ContextChangeType.ADD_ACCEPTANCE_CRITERION:
        updated = replace(
            context,
            acceptance_criteria=context.acceptance_criteria + (change.scalar_text(),),
        )

    elif kind == ContextChangeType.DECIDE:
        record = DecisionRecord(
            decision_id=change.change_id,
            statement=change.require_payload_field("statement"),
            proposed_by=change.proposed_by,
            reason=change.require_payload_field("reason")
            if change.payload().get("reason") else change.reason,
            accepted_by=(accepted_by,),
            created_at=stamp,
            revision=new_revision,
        )
        updated = replace(context, decisions=context.decisions + (record,))

    elif kind == ContextChangeType.ASSUME:
        record = AssumptionRecord(
            assumption_id=change.change_id,
            statement=change.require_payload_field("statement"),
            held_by=change.proposed_by,
            reason=change.payload().get("reason") or change.reason,
            created_at=stamp,
            revision=new_revision,
        )
        updated = replace(context, assumptions=context.assumptions + (record,))

    elif kind == ContextChangeType.QUESTION:
        record = OpenQuestion(
            question_id=change.change_id,
            question=change.require_payload_field("question"),
            owner=change.payload().get("owner"),
            created_at=stamp,
            revision=new_revision,
        )
        updated = replace(context, open_questions=context.open_questions + (record,))

    elif kind == ContextChangeType.ASSIGN:
        record = AssignmentRecord(
            assignment_id=change.change_id,
            agent_id=change.require_payload_field("agent_id"),
            responsibility=change.require_payload_field("responsibility"),
            assigned_by=change.proposed_by,
            assigned_at=stamp,
            revision=new_revision,
            notes=change.payload().get("notes", ""),
        )
        updated = replace(context, assignments=context.assignments + (record,))

    elif kind == ContextChangeType.FACT:
        record = SharedFact(
            fact_id=change.change_id,
            statement=change.require_payload_field("statement"),
            source=change.require_payload_field("source"),
            confirmed_by=accepted_by,
            created_at=stamp,
            revision=new_revision,
        )
        updated = replace(context, facts=context.facts + (record,))

    # ------------------------------------------------------------
    # 修正类（针对已有记录）
    # ------------------------------------------------------------
    elif kind == ContextChangeType.RESOLVE_QUESTION:
        if not change.target:
            raise InvalidContextChange("RESOLVE_QUESTION 必须指定 target（question_id）")
        existing = context.question_by_id(change.target)
        if existing is None:
            raise InvalidContextChange(
                f"找不到 question_id={change.target!r}"
            )
        updated_question = replace(
            existing,
            status=QuestionStatus.ANSWERED,
            answer=change.require_payload_field("answer"),
            answered_by=accepted_by,
            answered_at=stamp,
        )
        updated = replace(
            context,
            open_questions=tuple(
                updated_question if q.question_id == change.target else q
                for q in context.open_questions
            ),
        )

    elif kind == ContextChangeType.CONFIRM_ASSUMPTION:
        if not change.target:
            raise InvalidContextChange(
                "CONFIRM_ASSUMPTION 必须指定 target（assumption_id）"
            )
        existing = context.assumption_by_id(change.target)
        if existing is None:
            raise InvalidContextChange(
                f"找不到 assumption_id={change.target!r}"
            )
        updated_assumption = replace(
            existing,
            status=AssumptionStatus.CONFIRMED,
            resolved_at=stamp,
            resolved_by=accepted_by,
        )
        updated = replace(
            context,
            assumptions=tuple(
                updated_assumption if a.assumption_id == change.target else a
                for a in context.assumptions
            ),
        )

    else:  # pragma: no cover —— ContextChange 的校验已经挡住未知类型
        raise InvalidContextChange(f"未处理的 change_type：{kind!r}")

    # ------------------------------------------------------------
    # 推进版本 + 记录这一版为什么产生
    # ------------------------------------------------------------
    return replace(
        updated,
        revision=new_revision,
        parent_revision=context.revision,
        change_reason=decided_reason or change.reason,
        created_by=accepted_by,
        created_at=stamp,
    )
