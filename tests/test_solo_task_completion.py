"""
Solo Task 完成语义 + Reopen / Iteration V1（ADR-0056）
=======================================================

【验证什么】

    Solo Task 允许承接方自己 VERIFY / COMPLETE；
    Human 没有“接受”动作；
    COMPLETED 不是终态，Task Creator 可以 Reopen 开启下一 Iteration；
    Team Task 的 `producer ≠ acceptor` 不受影响。

【反向测试】

    · Team Task 的承接方仍然不能自验；
    · 非 Creator 不能 Reopen；
    · 非 COMPLETED 不能 Reopen；
    · 绕过 `reopen()` 直接 `transition_to(IN_PROGRESS)` 必须失败。
"""

from __future__ import annotations

import pytest

from communication.message import Message, MessageType
from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef, TaskVerification
from core.task_store import TaskStore
from services import HandlerAction, TaskExecutionService
from services.task_execution_service import ExecutionEvent


@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


@pytest.fixture
def execution(store) -> TaskExecutionService:
    return TaskExecutionService(store=store)


def solo_task(store: TaskStore, *, creator: str = "agent_a") -> Task:
    task = Task.create(
        task_id="T-SOLO",
        creator=creator,
        description="一个人完成的小任务",
        workspace_id="WS-SOLO",
        participants=[creator],
        mode=TaskMode.SOLO,
    )
    task.claim_producer(creator)
    store.save(task)
    return task


def message(task_id: str, sender: str, message_type: str) -> Message:
    """
    Solo 没有第二个 Agent 可以互发消息；这里用 front_agent 作为收件人。

    🔒 执行服务只读 `from_agent` 做角色判断，`to_agent` 不参与
       Task 状态机。接收方是谁不影响本测试验证的规则。
    """
    return Message(
        from_agent=sender,
        to_agent="front_agent",
        message_type=message_type,
        payload={"task_id": task_id},
    )


def run_to_completed(
    execution: TaskExecutionService, task: Task, actor: str
) -> Task:
    start = message(task.task_id, actor, MessageType.START)
    assert execution.apply_start(task, incoming=start).ok

    submit = message(task.task_id, actor, MessageType.SUBMIT)
    assert execution.apply_submit(
        task,
        result=TaskResultRef(result_ref="solo-result-v1"),
        incoming=submit,
    ).ok

    verify = message(task.task_id, actor, MessageType.VERIFY)
    assert execution.apply_verify(
        task,
        verification=TaskVerification(passed=True),
        incoming=verify,
    ).ok

    complete = message(task.task_id, actor, MessageType.COMPLETE)
    assert execution.apply_complete(task, incoming=complete).ok
    return task


# ============================================================
# 1. Task Core：mode / iteration
# ============================================================
class TestTaskModeCore:
    def test_default_mode_is_team(self):
        task = Task.create(creator="agent_a")
        assert task.mode == TaskMode.TEAM
        assert task.iteration == 1

    def test_solo_mode_round_trip(self):
        task = Task.create(creator="agent_a", mode=TaskMode.SOLO)
        data = task.to_dict()
        assert data["mode"] == TaskMode.SOLO
        assert data["iteration"] == 1

        restored = Task.from_dict(data)
        assert restored.mode == TaskMode.SOLO
        assert restored.iteration == 1

    def test_unknown_mode_rejected(self):
        with pytest.raises(Exception, match="未知的任务模式"):
            Task.create(creator="agent_a", mode="PAIR")

    def test_iteration_must_start_at_one(self):
        with pytest.raises(Exception, match="iteration"):
            Task(task_id="T", creator="a", iteration=0)


# ============================================================
# 2. Solo：承接方可以自己 VERIFY / COMPLETE
# ============================================================
class TestSoloSelfCompletion:
    def test_solo_contractor_can_verify(self, execution, store):
        task = run_to_completed(execution, solo_task(store), "agent_a")
        assert task.status == TaskStatus.COMPLETED

    def test_solo_contractor_can_complete_role_check(self, execution, store):
        task = solo_task(store)
        task.start()
        task.submit()
        task.verify(passed=True)
        store.save(task)

        assert execution.validate_role(
            task, "agent_a", ExecutionEvent.COMPLETE) == []

    def test_solo_full_flow_persists(self, execution, store):
        run_to_completed(execution, solo_task(store), "agent_a")
        loaded = store.get("T-SOLO")
        assert loaded.status == TaskStatus.COMPLETED
        assert loaded.mode == TaskMode.SOLO
        assert loaded.iteration == 1

    def test_team_contractor_still_cannot_verify(self, execution, store):
        """反向：Team Task 的生产者不能自验。"""
        task = Task.create(
            task_id="T-TEAM",
            creator="agent_a",
            description="团队任务",
            participants=["agent_a", "agent_b"],
            mode=TaskMode.TEAM,
        )
        task.claim_producer("agent_b")
        task.start()
        task.submit()
        store.save(task)

        errors = execution.validate_role(
            task, "agent_b", ExecutionEvent.VERIFY)
        assert errors
        assert "承接方" in errors[0]

    def test_team_contractor_cannot_complete(self, execution, store):
        task = Task.create(
            task_id="T-TEAM-2",
            creator="agent_a",
            participants=["agent_a", "agent_b"],
            mode=TaskMode.TEAM,
        )
        task.claim_producer("agent_b")
        task.start()
        task.submit()
        task.verify(passed=True)
        store.save(task)

        errors = execution.validate_role(
            task, "agent_b", ExecutionEvent.COMPLETE)
        assert errors


# ============================================================
# 3. Reopen / Iteration
# ============================================================
class TestReopen:
    def test_creator_can_reopen(self, execution, store):
        task = run_to_completed(execution, solo_task(store), "agent_a")
        result = execution.apply_reopen(
            task, actor="agent_a", reason="Human 要求返工")

        assert result.ok is True
        assert result.action == HandlerAction.REOPENED
        assert result.details["iteration"] == 2

        loaded = store.get("T-SOLO")
        assert loaded.status == TaskStatus.IN_PROGRESS
        assert loaded.iteration == 2
        assert loaded.note == "Human 要求返工"

    def test_non_creator_cannot_reopen(self, execution, store):
        task = run_to_completed(execution, solo_task(store), "agent_a")
        result = execution.apply_reopen(
            task, actor="agent_b", reason="不是我的任务")

        assert result.ok is False
        assert any("Creator" in error for error in result.errors)
        assert store.get("T-SOLO").status == TaskStatus.COMPLETED

    def test_cannot_reopen_unfinished_task(self, execution, store):
        task = solo_task(store)
        task.start()
        store.save(task)

        result = execution.apply_reopen(task, actor="agent_a")
        assert result.ok is False
        assert any("COMPLETED" in error for error in result.errors)

    def test_reopen_preserves_old_execution_history(self, execution, store):
        task = run_to_completed(execution, solo_task(store), "agent_a")
        before = store.count_executions("T-SOLO")
        assert before > 0

        execution.apply_reopen(task, actor="agent_a", reason="第二轮")

        after = store.count_executions("T-SOLO")
        assert after == before, "Reopen 不能删除旧执行记录"

    def test_reopened_task_can_complete_again(self, execution, store):
        task = run_to_completed(execution, solo_task(store), "agent_a")
        execution.apply_reopen(task, actor="agent_a", reason="返工")

        # 第二轮：重新提交 / 验证 / 完成
        submit = message(task.task_id, "agent_a", MessageType.SUBMIT)
        assert execution.apply_submit(
            task,
            result=TaskResultRef(result_ref="solo-result-v2"),
            incoming=submit,
        ).ok
        verify = message(task.task_id, "agent_a", MessageType.VERIFY)
        assert execution.apply_verify(
            task,
            verification=TaskVerification(passed=True),
            incoming=verify,
        ).ok
        complete = message(task.task_id, "agent_a", MessageType.COMPLETE)
        assert execution.apply_complete(task, incoming=complete).ok

        loaded = store.get("T-SOLO")
        assert loaded.status == TaskStatus.COMPLETED
        assert loaded.iteration == 2

    def test_reopen_is_not_a_new_task(self, execution, store):
        task = run_to_completed(execution, solo_task(store), "agent_a")
        execution.apply_reopen(task, actor="agent_a")
        assert store.count() == 1
        assert store.get("T-SOLO") is not None
