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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional


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
        # ---- result_ref 必填 ----
        if not isinstance(self.result_ref, str) or not self.result_ref.strip():
            raise ValueError(
                "result_ref 必填，而且不能是空白。\n"
                "SUBMIT 必须说明「交付的是什么」—— 不能只说「我做好了」。"
            )

        ref = self.result_ref.strip()

        # ---- 【安全边界】不许把 result_ref 写成路径或 URL ----
        #
        # 这不是「洁癖」，是一条安全约束：
        #     如果 result_ref 长得像路径，下游（人或代码）就会倾向于
        #     把它当路径用。一旦真的 open(result_ref)，
        #     承接方就能让发起方去读任意文件。
        #
        # 所以在数据类型这一层就挡住 —— 让它根本不像一个可访问目标。
        if ref.startswith(("/", "~", "./", "../", "\\")):
            raise ValueError(
                f"result_ref 不能是文件系统路径：{ref!r}\n"
                f"（它是一个逻辑名字，比如 'analysis-v2'。\n"
                f" 允许路径会让承接方通过提交一个构造过的名字"
                f"让发起方去读任意文件。）"
            )
        if "://" in ref:
            raise ValueError(
                f"result_ref 不能是 URL：{ref!r}\n"
                f"（它是一个逻辑名字，不代表任何可访问的地址。）"
            )
        if "\x00" in ref:
            raise ValueError("result_ref 不能包含空字节")

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
