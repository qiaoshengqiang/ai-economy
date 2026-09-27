"""
TaskAlignmentService —— Shared Context 的对齐机制（Step 4F）

============================================================
【这一层干什么】
============================================================

它把三个东西接起来：

    Task          （参与方是谁 —— 复用 Task.is_participant）
    TaskContextStore （持久化 + 乐观锁）
    apply_change  （纯领域的「变更怎么落到 context 上」）

并回答一个业务问题：

    「这个 Agent 现在能不能做这个动作？」

============================================================
【对齐的最小流程】
============================================================

    Agent A
      │
      │  propose()
      ▼
    ContextChange (PROPOSED)          ← 只是提议，Context 没变
      │
      │  accept()  ← 必须由参与方裁决
      ▼
    apply_change() → 新 Revision      ← 只有到这一步 Context 才变
      │
      ▼
    current_revision += 1

    reject() → Change 变 REJECTED，**Context 一点没动**

============================================================
【为什么不实现投票 / quorum】
============================================================

规范 §9 明确禁止。这里只有一个**对齐最低要求**：

    DECIDE 类型的变更，批准人不能是提案人自己。

理由：「一个 Agent 自己说了算，不算共同决定」——
这是 Constitution §8.2 已经定下的原则（Said ≠ Confirmed）。

声明类（FACT / ASSUME / QUESTION / ASSIGN）允许自批：
「我观察到 X」「我假设 Y」本来就是个人声明，不需要别人附议。

============================================================
【为什么这一层不直接 UPDATE Shared Context】
============================================================

    禁止：  service ──UPDATE──▶ task_shared_contexts 的内容
    允许：  service ──commit_revision──▶ 推进 current_revision

「内容」永远只通过 `apply_change()` 产生新版本。
`task_shared_contexts` 表里**只有一个版本号**，没有内容列 ——
所以「绕过 Alignment 直接改内容」在结构上就做不到。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from core.task import Task
from core.task_context_store import TaskContextStore, new_change_id
from core.task_shared_context import (
    ContextChange,
    ContextChangeStatus,
    ContextChangeType,
    ContextNotFoundError,
    ContextRevision,
    ContextSnapshot,
    ContextValidationError,
    ConcurrentContextModification,
    InvalidContextChange,
    NotAParticipantError,
    SelfAlignmentError,
    TaskSharedContext,
    apply_change,
    utc_now_iso,
)
from core.task_store import TaskStore


class TaskAlignmentService:
    """
    Shared Context 的应用服务。

    【它不知道的事】
        · 不 import communication（消息不会自动变成事实 —— §11）
        · 不 import llm（不做「LLM 猜有个决定」的事 —— §11）
        · 不碰钱包/账本（4F 不涉及经济结算）
        · 不碰文件系统（artifact_refs 只是逻辑引用 —— §15）
    """

    def __init__(
        self,
        tasks: TaskStore,
        contexts: TaskContextStore,
    ):
        # 【注意：这里**没有** Database】
        #     事务边界在 TaskContextStore 里。
        #     service 只管业务规则（谁能做什么），不管事务。
        #
        #     这样依赖方向保持为：
        #         services → core + communication
        #     而不是 services → storage。
        self.tasks = tasks
        self.contexts = contexts

    # ============================================================
    # 一、创建
    # ============================================================
    def create_context(
        self,
        task_id: str,
        *,
        by: str,
        objective: str = "",
        constraints: Optional[List[str]] = None,
        acceptance_criteria: Optional[List[str]] = None,
        artifact_refs: Optional[List[str]] = None,
        terminology: Optional[Mapping[str, str]] = None,
        now: Optional[str] = None,
    ) -> ContextRevision:
        """
        为一个 Task 建立共享上下文（Revision 1）。

        【谁可以建】
            必须是 Task 的参与方。非参与方不得为别人的任务
            建立共同认知 —— 那是 §16 的边界。
        """
        task = self._require_task(task_id)
        self._require_participant(task, by, action="创建共享上下文")

        stamp = now or utc_now_iso()

        return self.contexts.create(
            task_id,
            created_by=by,
            objective=objective,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            artifact_refs=artifact_refs,
            terminology=terminology,
            now=stamp,
        )

    # ============================================================
    # 二、读取
    # ============================================================
    def current(self, task_id: str) -> Optional[ContextRevision]:
        """当前有效版本。没有 context 时返回 None。"""
        return self.contexts.current(task_id)

    def require_current(self, task_id: str) -> ContextRevision:
        return self.contexts.require_current(task_id)

    def history(self, task_id: str) -> List[ContextRevision]:
        """全部历史版本（升序）。"""
        return self.contexts.history(task_id)

    def changes(
        self, task_id: str, *, status: Optional[str] = None
    ) -> List[ContextChange]:
        return self.contexts.changes_for(task_id, status=status)

    def pending(self, task_id: str) -> List[ContextChange]:
        return self.contexts.pending_changes(task_id)

    # ============================================================
    # 三、提议
    # ============================================================
    def propose(
        self,
        task_id: str,
        *,
        proposed_by: str,
        change_type: str,
        new_value: Any,
        expected_revision: int,
        reason: str,
        target: Optional[str] = None,
        expected_value: Any = None,
        source: str = "",
        now: Optional[str] = None,
    ) -> ContextChange:
        """
        提出一条变更。**这一步不改变 Context。**

        ------------------------------------------------------------
        【expected_revision 是必填的，而且会被立刻校验】
        ------------------------------------------------------------

            如果提案基于的版本已经不是当前版本，
            说明上下文在你思考期间变了。

            这时**直接拒绝提议**（而不是先存下来再在 accept 时炸）——
            因为一条基于旧版本的提议，其内容很可能已经不再适用。
            让它在提议阶段就失败，比让它进入待裁决队列更诚实。

            注意：accept() 时还会**再校验一次**（因为提议之后
            别的东西可能又推进了版本）。两次检查都需要：
                提议时  → 早失败，快速反馈
                裁决时  → 真正的 CAS，防静默覆盖
        """
        task = self._require_task(task_id)
        self._require_participant(task, proposed_by, action="提议变更")

        # 必须已经有 context
        current = self.contexts.require_current(task_id)

        if expected_revision != current.revision:
            raise ConcurrentContextModification(
                task_id=task_id,
                expected_revision=expected_revision,
                actual_revision=current.revision,
            )

        change = ContextChange(
            change_id=new_change_id(),
            task_id=task_id,
            proposed_by=proposed_by,
            change_type=change_type,
            target_revision=expected_revision,
            new_value=new_value,
            target=target,
            expected_value=expected_value,
            reason=reason,
            source=source,
            created_at=now or utc_now_iso(),
            status=ContextChangeStatus.PROPOSED,
        )

        # 提前校验载荷形状 —— 别等到 accept 才发现结构不对
        self._validate_payload(change, current.context)

        self.contexts.record_change(change)
        return change

    # ============================================================
    # 四、裁决 —— 接受（会推进版本）
    # ============================================================
    def accept(
        self,
        task_id: str,
        change_id: str,
        *,
        decided_by: str,
        reason: str = "",
        now: Optional[str] = None,
    ) -> ContextRevision:
        """
        接受一条变更 → 产生**新的 Revision**。

        ------------------------------------------------------------
        【整个过程在一个事务里】
        ------------------------------------------------------------

            decide_change(ACCEPTED)        ← 带条件 UPDATE（防重复裁决）
            commit_revision(...)           ← 带 CAS（防静默覆盖）

            两步要么都成功，要么都回滚。
            不会出现「change 说 ACCEPTED 但版本没推进」这种半成功。

        【顺序很重要】
            先决定 change 的状态，再提交版本。
            反过来的话，如果版本提交成功而 decide 失败（比如
            已被裁决），就会留下一个「凭空出现的 revision」。
        """
        task = self._require_task(task_id)
        self._require_participant(task, decided_by, action="裁决变更")

        change = self.contexts.require_change(change_id)
        self._require_same_task(change, task_id)

        if not change.is_proposed:
            raise ContextValidationError(
                f"change {change_id!r} 已经是 {change.status!r}（终态），"
                f"不能再裁决。"
            )

        # ---- 对齐最低要求：DECIDE 不能自批 ----
        if change.requires_other_aligner and decided_by == change.proposed_by:
            raise SelfAlignmentError(
                f"{decided_by!r} 不能批准自己提出的 {change.change_type} 变更。\n"
                f"（「决定」需要至少一个**别人**确认 —— "
                f"否则那只是一个提议，不是共同决定。\n"
                f" 声明类变更（FACT/ASSUME/QUESTION/ASSIGN）不受此限。）"
            )

        current = self.contexts.require_current(task_id)

        # ---- 真正的 CAS ----
        #     提议之后可能又有别人推进了版本，所以这里必须再查一次。
        if change.target_revision != current.revision:
            raise ConcurrentContextModification(
                task_id=task_id,
                expected_revision=change.target_revision,
                actual_revision=current.revision,
            )

        # ---- 裁决 + 应用 + 提交，原子地（事务在 store 里）----
        return self.contexts.commit_accepted_change(
            change,
            decided_by=decided_by,
            new_revision=current.revision + 1,
            decided_reason=reason,
            now=now,
        )

    # ============================================================
    # 五、裁决 —— 拒绝（**Context 完全不变**）
    # ============================================================
    def reject(
        self,
        task_id: str,
        change_id: str,
        *,
        decided_by: str,
        reason: str = "",
        now: Optional[str] = None,
    ) -> ContextChange:
        """
        拒绝一条变更。

        【关键语义】
            被拒绝的 Change **不得改变**当前有效 Shared Context。

            这一点是结构性的：这个函数**根本不碰**
            `task_shared_contexts.current_revision`，
            也不写 `task_context_revisions`。

        【为什么拒绝也要有理由】
            被否掉的方案也是知识。三周后有人再提同样的东西时，
            「上次为什么否掉」能省一轮来回。
        """
        task = self._require_task(task_id)
        self._require_participant(task, decided_by, action="裁决变更")

        change = self.contexts.require_change(change_id)
        self._require_same_task(change, task_id)

        if not change.is_proposed:
            raise ContextValidationError(
                f"change {change_id!r} 已经是 {change.status!r}（终态），"
                f"不能再裁决。"
            )

        before = self.contexts.current_revision_number(task_id)

        decided = self.contexts.decide_change(
            change_id,
            status=ContextChangeStatus.REJECTED,
            decided_by=decided_by,
            decided_reason=reason,
            result_revision=None,
            now=now or utc_now_iso(),
        )

        # ---- 防御性断言：拒绝绝不允许动版本号 ----
        after = self.contexts.current_revision_number(task_id)
        if after != before:  # pragma: no cover —— 触发就说明有 bug
            raise ContextValidationError(
                f"内部错误：拒绝一个 change 不应该改变 revision"
                f"（{before} → {after}）"
            )

        return decided

    # ============================================================
    # 六、Snapshot（读取视图）
    # ============================================================
    def snapshot(
        self,
        task_id: str,
        *,
        revision: Optional[int] = None,
        recent_change_limit: int = 5,
        now: Optional[str] = None,
    ) -> ContextSnapshot:
        """
        生成一个可消费的读取视图。

        ------------------------------------------------------------
        【domain_state 是**现场读 Task Core** 的】
        ------------------------------------------------------------

            §6：Task Core 是领域状态的唯一权威。

            所以这里不会从 Context 里取 status（它根本没有），
            而是**现在**去 TaskStore 读一次。

            这样：
                Task.status 变了 → 下一次 snapshot 立刻反映
            而不是：
                Context 里存了一份旧 status，和 Task Core 打架

            `domain_state_source = "task_core"` 就是用来标明这一点的。
        """
        task = self._require_task(task_id)

        if revision is None:
            found = self.contexts.require_current(task_id)
        else:
            found = self.contexts.revision(task_id, revision)
            if found is None:
                raise ContextNotFoundError(
                    f"Task {task_id!r} 没有 revision {revision}"
                )

        recent = self.contexts.changes_for(task_id)[-recent_change_limit:]
        recent_summaries = tuple(
            {
                "change_id": c.change_id,
                "change_type": c.change_type,
                "proposed_by": c.proposed_by,
                "status": c.status,
                "reason": c.reason,
                "decided_by": c.decided_by,
                "result_revision": c.result_revision,
            }
            for c in recent
        )

        return ContextSnapshot(
            task_id=task_id,
            context_id=found.context.context_id,
            revision=found.revision,
            taken_at=now or utc_now_iso(),
            context=found.context,
            domain_state=self._domain_state(task),
            domain_state_source="task_core",
            recent_changes=recent_summaries,
        )

    # ============================================================
    # 七、内部
    # ============================================================
    def _require_task(self, task_id: str) -> Task:
        return self.tasks.require(task_id)

    @staticmethod
    def _require_participant(task: Task, agent_id: str, *, action: str) -> None:
        """
        §16：只有合法 Task Participant 才能提交 / 裁决。

        【复用 Task.is_participant()】
            判断依据是 Task 自己的字段（creator / contractor /
            participants）—— 没有硬编码任何 Agent 名字，
            所以换成任何两个 Agent 都一样成立。

        【这不是 Permission System】
            不涉及文件权限 / 路径权限 / Workspace 权限。
            那些属于后续阶段。
        """
        if not task.is_participant(agent_id):
            raise NotAParticipantError(agent_id, task.task_id, action)

    @staticmethod
    def _require_same_task(change: ContextChange, task_id: str) -> None:
        if change.task_id != task_id:
            raise ContextValidationError(
                f"change {change.change_id!r} 属于 Task {change.task_id!r}，"
                f"不是 {task_id!r}"
            )

    @staticmethod
    def _domain_state(task: Task) -> Dict[str, Any]:
        """
        **引用** Task Core 的领域状态（不是副本）。

        这里刻意只挑「共同认知需要知道的」字段：
        任务处于什么阶段、谁发起、谁承接、约束是什么。

        注意这些字段**不会被写进 Context** ——
        每次 snapshot 都重新读，所以永远不会漂移。
        """
        return {
            "status": task.status,
            "creator": task.creator,
            "contractor": task.contractor,
            "participants": list(task.participants),
            "is_terminal": task.is_terminal,
            "is_open": task.is_open,
            "agreed_price": (
                task.agreed_price.to_dict() if task.agreed_price else None
            ),
            "budget": task.budget.to_dict() if task.budget else None,
            "task_version": task.version,
        }

    @staticmethod
    def _validate_payload(change: ContextChange, context: TaskSharedContext) -> None:
        """
        提前检查载荷形状。

        【为什么在 propose 时就查】
            等到 accept 才炸的话，一条结构错误的提议会躺在
            待裁决队列里，直到有人去批准它才暴露问题。
            早失败更诚实。
        """
        kind = change.change_type

        if kind == ContextChangeType.SET_OBJECTIVE:
            change.scalar_text()

        elif kind in (
            ContextChangeType.ADD_CONSTRAINT,
            ContextChangeType.ADD_ACCEPTANCE_CRITERION,
        ):
            change.scalar_text()

        elif kind == ContextChangeType.DECIDE:
            change.require_payload_field("statement")

        elif kind == ContextChangeType.ASSUME:
            change.require_payload_field("statement")

        elif kind == ContextChangeType.QUESTION:
            change.require_payload_field("question")

        elif kind == ContextChangeType.ASSIGN:
            change.require_payload_field("agent_id")
            change.require_payload_field("responsibility")

        elif kind == ContextChangeType.FACT:
            change.require_payload_field("statement")
            change.require_payload_field("source")

        elif kind == ContextChangeType.RESOLVE_QUESTION:
            if not change.target:
                raise InvalidContextChange(
                    "RESOLVE_QUESTION 必须指定 target（question_id）"
                )
            if context.question_by_id(change.target) is None:
                raise InvalidContextChange(
                    f"找不到 question_id={change.target!r}"
                )
            change.require_payload_field("answer")

        elif kind == ContextChangeType.CONFIRM_ASSUMPTION:
            if not change.target:
                raise InvalidContextChange(
                    "CONFIRM_ASSUMPTION 必须指定 target（assumption_id）"
                )
            if context.assumption_by_id(change.target) is None:
                raise InvalidContextChange(
                    f"找不到 assumption_id={change.target!r}"
                )

    def describe(self) -> Dict[str, Any]:
        return {
            "contexts": self.contexts.count_contexts(),
            "changes": self.contexts.count_changes(),
        }

    def __repr__(self) -> str:
        return f"<TaskAlignmentService {self.describe()}>"
