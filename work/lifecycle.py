"""
Work Task Lifecycle —— 没有消息机制的执行生命周期
===================================================

Work 层不再使用 Agent 间消息。但 Task 的历史仍然必须可审计，
所以这里直接调用 Task Core 的领域方法，并把事件写入
`task_executions`（append-only）。

    Work 层动作          Task Core          执行记录
    ------------         ------------       ------------
    start                CREATED → IN_PROGRESS       START
    submit               IN_PROGRESS → SUBMITTED     SUBMIT
    verify               SUBMITTED → VERIFIED       VERIFY
    complete             VERIFIED → COMPLETED        COMPLETE
    complete_solo        以上四步（Solo）             三条记录
    reopen               COMPLETED → IN_PROGRESS      —

🔒 边界：

    · `task_executions.message_id` 是合成键（`work_<uuid>`），
      不依赖 Communication 层，也不代表真的发过消息；
    · Task Core 仍是唯一生命周期权威；
    · 不碰 Wallet / Ledger / Workspace。
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from core.task import Task, TaskMode, TaskStatus
from core.task_execution import (
    ExecutionEvent,
    TaskExecutionRecord,
    TaskResultRef,
    TaskVerification,
)
from core.task_store import TaskStore


class WorkLifecycleError(Exception):
    """Work 层生命周期动作被拒绝。"""


class WorkLifecycle:
    """Work 层的 Task 生命周期操作（无消息）。"""

    def __init__(self, store: TaskStore):
        self.store = store

    # ------------------------------------------------------------
    # 基本动作
    # ------------------------------------------------------------
    def start(self, task: Task, *, actor: str) -> Task:
        """CREATED / ACCEPTED → IN_PROGRESS。"""
        if task.status == TaskStatus.IN_PROGRESS:
            return task
        if task.status not in (TaskStatus.CREATED, TaskStatus.ACCEPTED):
            raise WorkLifecycleError(
                f"任务 {task.task_id} 当前是 {task.status}，不能 START。")

        if task.status == TaskStatus.CREATED:
            if not task.is_participant(actor):
                raise WorkLifecycleError(
                    f"{actor} 不是任务 {task.task_id} 的参与方。")
            task.claim_producer(actor)

        task.start()
        self.store.save(task)
        self._record(task, ExecutionEvent.START, actor=actor)
        return task

    def submit(
        self,
        task: Task,
        *,
        actor: str,
        result: TaskResultRef,
    ) -> Task:
        """IN_PROGRESS → SUBMITTED。"""
        self._require_status(task, TaskStatus.IN_PROGRESS, "SUBMIT")
        self._require_contractor(task, actor, "SUBMIT")
        task.submit()
        self.store.save(task)
        self._record(task, ExecutionEvent.SUBMIT, actor=actor, result=result)
        return task

    def verify(
        self,
        task: Task,
        *,
        actor: str,
        passed: bool = True,
        feedback: str = "",
    ) -> Task:
        """
        SUBMITTED → VERIFIED / IN_PROGRESS。

        🔒 Work 层的 VERIFY 只允许 Solo 承接方自己发。
           Team Task 的 VERIFY 仍走 `TaskExecutionService` 的角色规则。
        """
        if task.mode != TaskMode.SOLO:
            raise WorkLifecycleError(
                "WorkLifecycle.verify 只用于 Solo Task；"
                "Team Task 继续使用 TaskExecutionService。")
        self._require_status(task, TaskStatus.SUBMITTED, "VERIFY")
        self._require_contractor(task, actor, "VERIFY")

        verification = TaskVerification(passed=passed, feedback=feedback)
        task.verify(passed=passed)
        self.store.save(task)
        self._record(
            task, ExecutionEvent.VERIFY, actor=actor,
            verification=verification)
        return task

    def complete(self, task: Task, *, actor: str) -> Task:
        """VERIFIED → COMPLETED。"""
        self._require_status(task, TaskStatus.VERIFIED, "COMPLETE")
        self._require_contractor(task, actor, "COMPLETE")
        task.complete()
        self.store.save(task)
        self._record(task, ExecutionEvent.COMPLETE, actor=actor)
        return task

    # ------------------------------------------------------------
    # Solo 一键完成
    # ------------------------------------------------------------
    def complete_solo(
        self,
        task: Task,
        *,
        actor: str,
        result: TaskResultRef,
        feedback: str = "",
    ) -> Dict[str, Any]:
        """
        把一个 Solo Task 推进到 COMPLETED。

        按当前状态补齐剩余步骤：

            IN_PROGRESS → SUBMIT → VERIFY → COMPLETE
            SUBMITTED   →          VERIFY → COMPLETE
            VERIFIED    →                   COMPLETE
            COMPLETED   → 幂等返回

        🔒 每一步仍然走 Task Core 的状态机；不存在跳步。
        """
        if task.mode != TaskMode.SOLO:
            raise WorkLifecycleError(
                f"任务 {task.task_id} 是 {task.mode}，"
                f"不能用 Solo 自完成路径。")
        self._require_contractor(task, actor, "COMPLETE")

        if task.status == TaskStatus.COMPLETED:
            return {
                "ok": True,
                "already_completed": True,
                "status": task.status,
                "iteration": task.iteration,
            }

        if task.status not in (
            TaskStatus.IN_PROGRESS,
            TaskStatus.SUBMITTED,
            TaskStatus.VERIFIED,
        ):
            raise WorkLifecycleError(
                f"任务 {task.task_id} 当前是 {task.status}，不能完成。")

        if task.status == TaskStatus.IN_PROGRESS:
            self.submit(task, actor=actor, result=result)
        if task.status == TaskStatus.SUBMITTED:
            self.verify(
                task, actor=actor, passed=True,
                feedback=feedback or "Solo 自验收：按 DoD 完成")
        if task.status == TaskStatus.VERIFIED:
            self.complete(task, actor=actor)

        return {
            "ok": True,
            "already_completed": False,
            "status": task.status,
            "iteration": task.iteration,
            "summary": result.summary,
        }

    # ------------------------------------------------------------
    # Reopen
    # ------------------------------------------------------------
    def reopen(self, task: Task, *, actor: str, reason: str = "") -> Task:
        """COMPLETED → IN_PROGRESS（Iteration +1）。"""
        if task.status != TaskStatus.COMPLETED:
            raise WorkLifecycleError(
                f"只有 COMPLETED 的任务可以 Reopen；"
                f"任务 {task.task_id} 当前是 {task.status}。")
        if actor != task.creator:
            raise WorkLifecycleError(
                f"{actor} 不能 Reopen 任务 {task.task_id}；"
                f"只有 Task Creator（{task.creator}）可以。")
        task.reopen(reason=reason or None)
        self.store.save(task)
        return task

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------
    @staticmethod
    def _require_status(task: Task, expected: str, action: str) -> None:
        if task.status != expected:
            raise WorkLifecycleError(
                f"{action} 需要任务处于 {expected}，"
                f"但 {task.task_id} 当前是 {task.status}。")

    @staticmethod
    def _require_contractor(task: Task, actor: str, action: str) -> None:
        if not task.contractor:
            raise WorkLifecycleError(
                f"任务 {task.task_id} 还没有承接方，不能 {action}。")
        if actor != task.contractor:
            raise WorkLifecycleError(
                f"{action} 只能由承接方 {task.contractor} 发送，"
                f"收到 {actor}。")

    def _record(
        self,
        task: Task,
        event_type: str,
        *,
        actor: str,
        result: Optional[TaskResultRef] = None,
        verification: Optional[TaskVerification] = None,
    ) -> TaskExecutionRecord:
        record = TaskExecutionRecord(
            # 合成键：Work 层没有消息，但执行历史仍然要 append-only。
            message_id=f"work_{uuid.uuid4().hex}",
            task_id=task.task_id,
            event_type=event_type,
            result=result,
            verification=verification,
            from_agent=actor,
            trace_id=task.current_trace_id,
            conversation_id=f"work:{task.task_id}:{task.iteration}",
            status_after=task.status,
        )
        return self.store.record_execution(record)
