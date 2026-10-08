"""
Task Execution —— 执行 / 提交 / 验收阶段的领域值类型
============================================================

【这个文件在架构里的位置】

    core/task_agreement.py    议价阶段的产物：成交凭证
    core/task_execution.py    执行阶段的产物：成果引用 / 验收结论 / 执行记录  ← 本文件

    和 task_agreement.py 一样，这里是**纯值类型**：
    不可变、无 I/O、不知道 Message、不知道 SQLite、不知道 services。

【三个类型各自的角色】

    TaskResultRef        承接方说「我交付了什么」
                         只是一个**逻辑引用** —— 见下面那条重要边界

    TaskVerification     发起方说「我检查的结果如何」（passed + feedback）

    TaskExecutionRecord  把上面这些 + 谁在什么时候通过哪条消息做的
                         记成一条 append-only 的记录

【一条非常重要的边界：result_ref 不是权限】

    result_ref 是一个**字符串标识**，例如：

        "analysis-v2"          （一份分析结果的逻辑名字）
        "report-2024-11"       （一次交付的逻辑名字）

    它**不是**：

        ❌ filesystem path        文件系统路径
        ❌ writable directory     可写目录
        ❌ executable path        可执行文件路径
        ❌ access token           访问令牌
        ❌ Workspace token        Workspace 授权
        ❌ URL permission         URL 访问权
        ❌ Git branch / worktree  Git 引用

    换句话说：它是「我交付的东西叫什么名字」，
    而不是「你有权访问什么」。

    为什么这条边界必须写死在文档和代码里：

        如果哪天有人把 result_ref 直接当路径用（open(result_ref)），
        那 Agent B 就可以通过提交一个精心构造的 result_ref
        让 Agent A 去读任意文件 —— 一次典型的路径注入。

        所以这个类型**没有**任何「打开 / 读取 / 解析成路径」的方法，
        而且有架构守卫盯着它不许获得文件 I/O 能力。

【为什么 metadata 是只读的】

    metadata 允许携带额外信息（比如「改了几个文件」「跑了多久」），
    但它会被冻结成不可变的映射 —— 免得下游随手往里面塞东西，
    让它慢慢变成一个什么都能装的袋子。
"""

from __future__ import annotations

from core.logical_ref import validate_logical_ref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple


# ============================================================
# 执行事件类型
# ============================================================
class ExecutionEvent:
    """
    执行阶段的四种事件。

    【它和 MessageType 的关系】
        MessageType.START / SUBMIT / VERIFY / COMPLETE 是**协议**层的名字；
        ExecutionEvent.* 是**领域**层的名字。

        两者目前字符串相同，但刻意分开：
            协议改名不该动领域数据，领域改名也不该动协议。
        （Task Core 不许依赖 communication/ —— 有守卫。）
    """

    START = "START"
    SUBMIT = "SUBMIT"
    VERIFY = "VERIFY"
    COMPLETE = "COMPLETE"

    @classmethod
    def all(cls) -> tuple:
        return (cls.START, cls.SUBMIT, cls.VERIFY, cls.COMPLETE)

    @classmethod
    def is_known(cls, value: str) -> bool:
        return value in cls.all()


# ============================================================
# 成果引用
# ============================================================
#: 允许的成果类型。
#:
#: 【为什么要有这个白名单】
#:     不是为了限制 Agent，而是为了让「成果」是可查询的。
#:     如果 result_type 是自由文本，就会出现 "code" / "Code" / "源代码"
#:     三种写法表达同一个意思，之后没人能按类型查。
#:
#:     它是一个**开放的短列表**：以后要加新类型，
#:     在这里加一行即可 —— 但必须是刻意的决定，不是随手写。
RESULT_TYPES = (
    "artifact",      # 泛指的交付物（默认）
    "document",      # 文档 / 报告
    "code",          # 代码
    "data",          # 数据集 / 分析结果
    "patch",         # 补丁
    "tests",         # 测试
    "answer",        # 一个结论 / 回答
)


@dataclass(frozen=True)
class TaskResultRef:
    """
    承接方提交的成果引用。

    【不可变】
        提交之后不能被改写 —— 验收针对的是**当时那一份**成果。
        要改就重新提交一次（产生新的 TaskResultRef 和新的记录）。
    """

    #: 成果的逻辑标识。**只是名字，不是路径、不是权限。**
    result_ref: str

    #: 成果类型（见 RESULT_TYPES）
    result_type: str = "artifact"

    #: 人类可读的说明
    summary: str = ""

    #: 额外信息（会被冻结成只读）
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # ---- result_ref 必填，且必须是**逻辑名字** ----
        #
        # 【安全边界】不许把 result_ref 写成路径或 URL。
        #
        # 这不是「洁癖」，是一条安全约束：
        #     如果 result_ref 长得像路径，下游（人或代码）就会倾向于
        #     把它当路径用。一旦真的 open(result_ref)，
        #     承接方就能让发起方去读任意文件。
        #
        # 所以在数据类型这一层就挡住 —— 让它根本不像一个可访问目标。
        #
        # 【Step 4F 的改动：规则抽到了 core/logical_ref.py】
        #     同一条规则现在有两处要用（result_ref 和 Shared Context 的
        #     artifact_refs）。复制两遍的话，改了 A 忘了 B 就留下漏洞。
        #     所以抽成共用函数，这里改为调用它。
        #     异常类型仍是 ValueError（InvalidLogicalRef 继承它），
        #     错误信息里仍带 "result_ref"。有回归测试钉住这两点。
        ref = validate_logical_ref(self.result_ref, field="result_ref")

        object.__setattr__(self, "result_ref", ref)

        # ---- result_type 必须是已知类型 ----
        if self.result_type not in RESULT_TYPES:
            raise ValueError(
                f"result_type 必须是 {list(RESULT_TYPES)} 之一，"
                f"收到 {self.result_type!r}"
            )

        if not isinstance(self.summary, str):
            raise ValueError("summary 必须是字符串")

        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata 必须是映射（dict）")

        # 冻结 metadata —— 免得下游把它当成一个能装任何东西的袋子
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata))
        )

    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "result_type": self.result_type,
            "result_ref": self.result_ref,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskResultRef":
        return cls(
            result_type=data.get("result_type", "artifact"),
            result_ref=data.get("result_ref", ""),
            summary=data.get("summary", ""),
            metadata=data.get("metadata") or {},
        )

    def describe(self) -> Dict[str, Any]:
        return {
            **self.to_dict(),
            # 明确声明它不是权限 —— 让读到它的人不必猜
            "is_permission": False,
            "is_filesystem_path": False,
            "is_url": False,
        }

    def __str__(self) -> str:
        label = f"{self.result_type}:{self.result_ref}"
        return f"{label}（{self.summary}）" if self.summary else label


# ============================================================
# 验收结论
# ============================================================
@dataclass(frozen=True)
class TaskVerification:
    """
    发起方给出的验收结论。

    【第一版是人工验收】
        passed 由发起方**显式**给出 ——
        不自动跑测试、不调模型判断、不执行代码。

        这不是「暂时偷懒」，而是一个刻意选择：
        「什么算通过」是业务判断，不该由系统替 Agent 决定。
        未来的自动验收会是**另一个可选组件**，
        而不是把这个类型的语义改掉。

    【passed=False 意味着返工，不是判死】
        任务回到 IN_PROGRESS 让承接方再试一次。
        feedback 必须能说明「哪里不行」——
        否则承接方只能盲改。
    """

    passed: bool

    #: 验收说明。passed=False 时尤其重要。
    feedback: str = ""

    def __post_init__(self):
        # 【bool 的坑】isinstance(1, int) 为真，
        # 但 1 在这里不代表「通过」—— 它会变成一个说不清意思的整数。
        if not isinstance(self.passed, bool):
            raise ValueError(
                f"passed 必须是布尔值（True / False），"
                f"收到 {self.passed!r}（{type(self.passed).__name__}）。\n"
                f"接受 1 / 0 会让「没传」和「传了假」分不清。"
            )
        if not isinstance(self.feedback, str):
            raise ValueError("feedback 必须是字符串")

        # 【返工必须能说明原因】
        # 否则承接方收到一个 passed=false 却不知道改什么 ——
        # 那等于让它在黑箱里试。
        if not self.passed and not self.feedback.strip():
            raise ValueError(
                "验收不通过时必须给出 feedback ——\n"
                "承接方需要知道「哪里不行」才能返工。\n"
                "（只说『不通过』等于让它盲改。）"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "feedback": self.feedback}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskVerification":
        return cls(
            passed=data.get("passed"),
            feedback=data.get("feedback", "") or "",
        )

    def describe(self) -> Dict[str, Any]:
        return {
            **self.to_dict(),
            "outcome": "verified" if self.passed else "rework",
        }

    def __str__(self) -> str:
        return "通过" if self.passed else f"不通过（{self.feedback}）"


# ============================================================
# Completion Proposal
# ============================================================
#: Completion Proposal 在 SUBMIT 事件里的存放位置。
#:
#: 🔒 **不新增数据库表** —— 它随 SUBMIT 的 `result.metadata` 一起
#:    落进既有的 `task_executions.result_metadata`（TEXT/JSON）列。
#:    读回时由 `TaskExecutionService.latest_completion_proposal()` 按这个键取。
#:
#: 【为什么可以放在 metadata 里，而不是提升为一等列】
#:     Proposal 从来不被**查询**（没有「列出所有 MET 的提议」这种需求），
#:     它只在「这个任务的验收过程是怎样的」这个上下文里被读。
#:     为它加列会让 schema 变宽，而收益只是少一层 JSON 解包。
COMPLETION_PROPOSAL_KEY = "completion_proposal"


class CriteriaStatus:
    """
    Completion Proposal 对照 Definition of Done 的**结构化**达标情况。

    🔒 为什么必须是结构化值，而不是一句自然语言
        自然语言没法被校验。而「声称全部达标、同时又说还有 gap」
        这种自相矛盾**必须能被机械发现** —— 否则
        「Agent 说完成了」和「真的完成了」就没有区别。

    🔒 它**不是** Task 状态
        `MET` 只表示「提交者认为对照 DoD 全部达标」，
        **不代表已验收**。`COMPLETED` 只能由
        `VERIFY(passed=True)` + `COMPLETE` 产生。

        `Completion Proposal ≠ Completed`。
    """

    #: 对照 DoD 全部达标 —— 此时 outstanding_gaps 必须为空
    MET = "MET"

    #: 部分达标（还有 gap，但已经可以请对方验收）
    PARTIAL = "PARTIAL"

    #: 未达标（例如只是为了留下一次可追溯的提交）
    NOT_MET = "NOT_MET"

    ALL = (MET, PARTIAL, NOT_MET)

    @classmethod
    def is_known(cls, value: Any) -> bool:
        return value in cls.ALL


@dataclass(frozen=True)
class CompletionProposal:
    """
    Agent 提交的「完成声明」—— 一个**提议**，不是完成本身。

    【它在整条链里的位置】

        Agent 认为做完了
              ↓  本类型（提议，纯值对象）
        SUBMIT（走**现有**执行协议）
              ↓
        Acceptance Responsibility（由**别人**来验收）
              ↓
        VERIFY(passed=True / False)
              ↓
        COMPLETE → COMPLETED

    🔒 四条边界
        ① 它**不改变 Task 状态** —— 纯值对象，没有任何 store /
           状态机 / 网络访问能力。
        ② 它**不能替生产者验收** —— 验收走 VERIFY，角色校验在
           `TaskExecutionService.validate_role()` 里。
        ③ 它**不新增数据库表** —— 随 SUBMIT 的
           `result.metadata["completion_proposal"]` 落进既有的
           `task_executions` 事件链。
        ④ 它**不新增 Task 状态** —— `criteria_status` 只是提议的
           内容，不是状态机的输入。

    【为什么挂在 SUBMIT 上，而不是新开一个事件】
        SUBMIT 的语义就是「我交付了成果」。Completion Proposal 是
        对「这次交付对照 DoD 到什么程度」的结构化说明 ——
        它是**同一次交付**的一部分，不是另一个动作。
        新开一个事件会多出一套幂等与顺序问题，收益只是名字更直白。
    """

    #: 提议 ID（引用 / 审计用）
    proposal_id: str

    #: 属于哪个任务
    task_id: str

    #: 谁提的 —— 必须是 Task Participant（由执行服务校验）
    proposed_by: str

    #: 提交时间（ISO 字符串，与项目其它时间戳一致）
    submitted_at: str = ""

    #: 对照 DoD 的达标情况（见 CriteriaStatus）
    criteria_status: str = CriteriaStatus.PARTIAL

    #: 证据：成果引用（复用 TaskResultRef 的语义与全部校验）
    evidence: Tuple[TaskResultRef, ...] = ()

    #: 还没达标的项。**声称 MET 时必须为空。**
    outstanding_gaps: Tuple[str, ...] = ()

    # ------------------------------------------------------------
    def __post_init__(self):
        if not str(self.proposal_id or "").strip():
            raise ValueError("CompletionProposal 需要 proposal_id")
        if not str(self.task_id or "").strip():
            raise ValueError("CompletionProposal 需要 task_id")
        if not str(self.proposed_by or "").strip():
            raise ValueError(
                "CompletionProposal 需要 proposed_by —— "
                "「谁声称完成了」必须可追溯"
            )

        # ---- criteria_status 必须是结构化值 ----
        if not CriteriaStatus.is_known(self.criteria_status):
            raise ValueError(
                f"criteria_status 必须是 {list(CriteriaStatus.ALL)} 之一，"
                f"收到 {self.criteria_status!r}。\n"
                f"（自由文本没法校验，也就没法区分「声称」和「属实」。）"
            )

        # ---- evidence 不能为空 ----
        #
        # 与现有 SUBMIT 的语义一致：`validate_result()` 已经要求
        # result_ref 非空。一个没有任何证据的「我完成了」
        # 无法被验收方核对，只会变成一句口号。
        if not self.evidence:
            raise ValueError(
                "CompletionProposal 必须带至少一条 evidence ——\n"
                "没有证据的完成声明，验收方无从核对。"
            )

        # ---- 🔴 自相矛盾检测：声称全部达标，却还留着 gap ----
        #
        # 这是本类型存在的**核心**理由：让「Agent 说完成了」
        # 和「真的完成了」在数据层就能分开。
        if self.criteria_status == CriteriaStatus.MET and self.outstanding_gaps:
            raise ValueError(
                f"criteria_status = MET（声称对照 DoD 全部达标），"
                f"但 outstanding_gaps 非空：{list(self.outstanding_gaps)}。\n"
                f"这两件事不能同时成立 —— 要么改成 "
                f"{CriteriaStatus.PARTIAL}，要么把 gap 清掉。"
            )

    # ------------------------------------------------------------
    @property
    def claims_all_met(self) -> bool:
        return self.criteria_status == CriteriaStatus.MET

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "proposed_by": self.proposed_by,
            "submitted_at": self.submitted_at,
            "criteria_status": self.criteria_status,
            "evidence": [e.to_dict() for e in self.evidence],
            "outstanding_gaps": list(self.outstanding_gaps),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompletionProposal":
        raw_gaps = data.get("outstanding_gaps") or ()
        if isinstance(raw_gaps, str):
            raw_gaps = (raw_gaps,)
        return cls(
            proposal_id=data.get("proposal_id", "") or "",
            task_id=data.get("task_id", "") or "",
            proposed_by=data.get("proposed_by", "") or "",
            submitted_at=data.get("submitted_at", "") or "",
            criteria_status=data.get("criteria_status", CriteriaStatus.PARTIAL),
            evidence=tuple(
                TaskResultRef.from_dict(e) if isinstance(e, Mapping) else e
                for e in (data.get("evidence") or ())
            ),
            outstanding_gaps=tuple(str(g) for g in raw_gaps),
        )

    def describe(self) -> Dict[str, Any]:
        return {
            **self.to_dict(),
            "evidence_count": len(self.evidence),
            "gap_count": len(self.outstanding_gaps),
            # 明确声明它**不是**完成 —— 让读到它的人不必猜
            "is_completion": False,
            "is_proposal_only": True,
        }

    def __str__(self) -> str:
        return (
            f"CompletionProposal({self.proposal_id}) "
            f"by {self.proposed_by} "
            f"status={self.criteria_status} "
            f"evidence={len(self.evidence)} "
            f"gaps={len(self.outstanding_gaps)}"
        )


# ============================================================
# 执行记录（append-only）
# ============================================================
@dataclass(frozen=True)
class TaskExecutionRecord:
    """
    执行阶段的一条事件记录。

    【append-only】
        它记录「发生了什么」，不会被改写或删除。
        START / SUBMIT / VERIFY / COMPLETE 各产生一条。

    【为什么需要它】（§7）
        VERIFY(false) 之后承接方要返工，然后再 SUBMIT 一次。
        所以一个任务可能有多组 SUBMIT / VERIFY。
        如果只在 Task 上存「最近一次」，返工的历史就丢了 ——
        而「上一轮为什么不通过」正是承接方最需要的信息。

    【主键就是 message_id —— 刻意不发明新 ID】
        每个事件都对应且只对应一条消息，
        所以 message_id 天然唯一，而且**让幂等变成结构性的**：
        重复投递写入同一个主键 → 数据库层面就不可能重复。

        「第几轮」不单独存 —— 它是**推导**出来的
        （数这个任务之前有几条 SUBMIT）。这样就不会出现
        「记录的轮次和实际事件数对不上」这种漂移。
    """

    #: 触发这条记录的消息（同时也是主键）
    message_id: str

    task_id: str

    #: 见 ExecutionEvent
    event_type: str

    #: ---- SUBMIT 专有 ----
    result: Optional[TaskResultRef] = None

    #: ---- VERIFY 专有 ----
    verification: Optional[TaskVerification] = None

    #: 这条消息在回应哪一条（协议 §18 的 reply_to）
    reply_to: Optional[str] = None

    #: 做出这个动作的 Agent
    from_agent: Optional[str] = None

    trace_id: Optional[str] = None
    conversation_id: Optional[str] = None

    created_at: str = ""

    #: 事件之后任务处于什么状态（方便审计时不必回放）
    status_after: Optional[str] = None

    def __post_init__(self):
        if not self.message_id:
            raise ValueError(
                "TaskExecutionRecord 需要 message_id —— 它同时是幂等键"
            )
        if not self.task_id:
            raise ValueError("TaskExecutionRecord 需要 task_id")
        if not ExecutionEvent.is_known(self.event_type):
            raise ValueError(
                f"event_type 必须是 {list(ExecutionEvent.all())} 之一，"
                f"收到 {self.event_type!r}"
            )
        if not self.created_at:
            object.__setattr__(
                self, "created_at", datetime.now(timezone.utc).isoformat()
            )

    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "result": self.result.to_dict() if self.result else None,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "reply_to": self.reply_to,
            "from_agent": self.from_agent,
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "status_after": self.status_after,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskExecutionRecord":
        raw_result = data.get("result")
        raw_verification = data.get("verification")

        return cls(
            message_id=data["message_id"],
            task_id=data["task_id"],
            event_type=data["event_type"],
            result=(
                TaskResultRef.from_dict(raw_result) if raw_result else None
            ),
            verification=(
                TaskVerification.from_dict(raw_verification)
                if raw_verification else None
            ),
            reply_to=data.get("reply_to"),
            from_agent=data.get("from_agent"),
            trace_id=data.get("trace_id"),
            conversation_id=data.get("conversation_id"),
            created_at=data.get("created_at", ""),
            status_after=data.get("status_after"),
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "task_id": self.task_id,
            "message_id": self.message_id,
            "from_agent": self.from_agent,
            "reply_to": self.reply_to,
            "status_after": self.status_after,
            "result": self.result.describe() if self.result else None,
            "verification": (
                self.verification.describe() if self.verification else None
            ),
            # 明确：这些事件不产生任何经济后果
            "is_payment": False,
        }

    def __str__(self) -> str:
        return (
            f"{self.event_type} {self.task_id} "
            f"by {self.from_agent} → {self.status_after}"
        )
