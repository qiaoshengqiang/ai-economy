"""
Work 层 V1：Task-scoped lifecycle / 动作集合 / prompt / availability
=====================================================================

验证：

    · Solo WorkLifecycle 走完 SUBMIT → VERIFY → COMPLETE；
    · Reopen 保留旧执行历史；
    · Work 动作集合**没有**消息 / 经济 / 验收指派；
    · Work prompt 不包含钱包 / 价格 / AIC / 消息动作；
    · AgentAvailability 原子 IDLE → WORKING；
    · TeamRegistry 并行上限 3、WORKING 不可加入；
    · TaskDraftStore 按 Task 持久化到私有空间。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from actions import ActionIntent
from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef
from core.task_store import TaskStore
from core.workspace import Workspace
from work.actions import (
    FORBIDDEN_WORK_ACTION_TYPES,
    WORK_ACTION_TYPES,
    build_work_dispatcher,
)
from work.availability import AgentAvailability
from work.drafts import TaskDraftStore
from work.lifecycle import WorkLifecycle, WorkLifecycleError
from work.prompt import build_work_prompt
from work.roles import WorkContext, WorkRole
from work.runtime import SoloWorkRuntime
from work.team_registry import TeamRegistry


def solo_task(store: TaskStore, *, agent: str = "agent_a") -> Task:
    task = Task.create(
        task_id="T-SOLO-WORK",
        creator=agent,
        description="写一个 hello.txt",
        workspace_id="WS-SOLO",
        participants=[agent],
        mode=TaskMode.SOLO,
    )
    task.claim_producer(agent)
    store.save(task)
    return task


# ============================================================
# 1. Lifecycle
# ============================================================
class TestWorkLifecycle:
    def test_start_submit_verify_complete(self, db):
        store = TaskStore(db)
        task = solo_task(store)
        lifecycle = WorkLifecycle(store)

        lifecycle.start(task, actor="agent_a")
        assert task.status == TaskStatus.IN_PROGRESS

        lifecycle.submit(
            task, actor="agent_a",
            result=TaskResultRef(result_ref="hello-v1"))
        assert task.status == TaskStatus.SUBMITTED

        lifecycle.verify(task, actor="agent_a", passed=True)
        assert task.status == TaskStatus.VERIFIED

        lifecycle.complete(task, actor="agent_a")
        assert task.status == TaskStatus.COMPLETED
        assert store.get(task.task_id).status == TaskStatus.COMPLETED
        assert store.count_executions(task.task_id) == 4

    def test_complete_solo_is_idempotent(self, db):
        store = TaskStore(db)
        task = solo_task(store)
        lifecycle = WorkLifecycle(store)
        lifecycle.start(task, actor="agent_a")

        first = lifecycle.complete_solo(
            task, actor="agent_a",
            result=TaskResultRef(result_ref="hello-v1"))
        second = lifecycle.complete_solo(
            task, actor="agent_a",
            result=TaskResultRef(result_ref="hello-v2"))

        assert first["ok"] is True
        assert second["already_completed"] is True
        assert store.count_executions(task.task_id) == 4

    def test_team_task_cannot_use_solo_complete(self, db):
        store = TaskStore(db)
        task = Task.create(
            task_id="T-TEAM-WORK", creator="agent_a",
            participants=["agent_a", "agent_b"], mode=TaskMode.TEAM)
        task.claim_producer("agent_b")
        task.start()
        store.save(task)

        lifecycle = WorkLifecycle(store)
        with pytest.raises(WorkLifecycleError, match="Solo"):
            lifecycle.verify(task, actor="agent_b", passed=True)

    def test_non_contractor_cannot_complete(self, db):
        store = TaskStore(db)
        task = solo_task(store)
        lifecycle = WorkLifecycle(store)
        lifecycle.start(task, actor="agent_a")

        with pytest.raises(WorkLifecycleError, match="承接方"):
            lifecycle.complete_solo(
                task, actor="agent_b",
                result=TaskResultRef(result_ref="x"))

    def test_reopen_preserves_history(self, db):
        store = TaskStore(db)
        task = solo_task(store)
        lifecycle = WorkLifecycle(store)
        lifecycle.start(task, actor="agent_a")
        lifecycle.complete_solo(
            task, actor="agent_a",
            result=TaskResultRef(result_ref="hello-v1"))
        before = store.count_executions(task.task_id)

        lifecycle.reopen(task, actor="agent_a", reason="Human 要求返工")

        assert task.status == TaskStatus.IN_PROGRESS
        assert task.iteration == 2
        assert store.count_executions(task.task_id) == before
        assert store.get(task.task_id).iteration == 2

    def test_only_creator_can_reopen(self, db):
        store = TaskStore(db)
        task = solo_task(store)
        lifecycle = WorkLifecycle(store)
        lifecycle.start(task, actor="agent_a")
        lifecycle.complete_solo(
            task, actor="agent_a",
            result=TaskResultRef(result_ref="hello-v1"))

        with pytest.raises(WorkLifecycleError, match="Creator"):
            lifecycle.reopen(task, actor="agent_b")


# ============================================================
# 2. 动作集合
# ============================================================
class _FakeGateway:
    def list(self, ws_id, path, agent_id):
        return SimpleNamespace(success=True, result={"entries": []})


class _FakeRuntime:
    def __init__(self):
        self.workspace_gateway = _FakeGateway()
        self.tool_runner = None
        self.alignment = None
        self.economy = None
        self.private_working_memory = None
        self.private_short_term_memory = None
        self.private_long_term_memory = None


def _dispatcher():
    task = Task.create(
        task_id="T-DISPATCH", creator="agent_a",
        participants=["agent_a"], mode=TaskMode.SOLO,
        workspace_id="WS-X")
    drafts = SimpleNamespace(
        write=lambda *a, **k: {"ok": False},
        append=lambda *a, **k: {"ok": False},
        read=lambda *a, **k: {"ok": False},
        list=lambda *a, **k: {"ok": True, "drafts": []},
        delete=lambda *a, **k: {"ok": False},
    )
    return build_work_dispatcher(
        _FakeRuntime(), task=task, drafts=drafts,
        promote_drafts=lambda *a, **k: {"ok": True, "results": []},
        complete_fn=lambda **k: {"ok": False},
        item_done_fn=lambda **k: {"ok": False},
        item_blocked_fn=lambda **k: {"ok": False},
        qa_verdict_fn=lambda **k: {"ok": False},
        plan_update_fn=lambda **k: {"ok": False},
    )


class TestWorkActions:
    def test_forbidden_actions_absent(self):
        dispatcher = _dispatcher()
        registered = set(dispatcher.action_types)
        assert not (registered & FORBIDDEN_WORK_ACTION_TYPES)
        for name in ("message_send", "task_create", "assign_acceptance"):
            assert name not in registered

    def test_expected_actions_present(self):
        registered = set(_dispatcher().action_types)
        assert WORK_ACTION_TYPES <= registered
        assert "work_complete" in registered
        assert "workspace_promote_drafts" in registered


# ============================================================
# 3. Prompt
# ============================================================
class TestWorkPrompt:
    def test_prompt_excludes_economy_and_messages(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO,
            workspace_id="WS-X",
            acceptance_criteria=["hello.txt exists"],
        )
        prompt = build_work_prompt(
            context, task_description="写 hello.txt",
            runtime=None)

        for banned in (
            "wallet", "Wallet", "AIC", "钱包", "价格", "议价",
            "message_send", "task_create", "assign_acceptance",
            "public_working_memory", "inbox",
        ):
            assert banned not in prompt, banned
        assert "work_complete" in prompt
        assert "hello.txt exists" in prompt

    def test_prompt_has_work_actions(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO)
        prompt = build_work_prompt(
            context, task_description="d", runtime=None)
        for action in ("workspace_read", "workspace_edit", "run_tool",
                       "draft_write", "private_working_memory_update"):
            assert action in prompt


# ============================================================
# 4. Availability / TeamRegistry
# ============================================================
class TestAvailability:
    def test_idle_to_working_is_atomic(self):
        availability = AgentAvailability()
        availability.register("agent_a")
        results = []

        def acquire():
            results.append(availability.try_acquire("agent_a"))

        threads = [threading.Thread(target=acquire) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert results.count(True) == 1
        assert availability.state("agent_a") == AgentAvailability.WORKING

    def test_release_returns_to_idle(self):
        availability = AgentAvailability()
        assert availability.try_acquire("agent_a") is True
        assert availability.release("agent_a") is True
        assert availability.is_idle("agent_a") is True

    def test_working_agent_cannot_join_second_team(self):
        availability = AgentAvailability()
        registry = TeamRegistry(availability, max_parallel_teams=3)
        ok, _, _ = registry.register(
            task_id="task_1", lead="agent_a",
            members=["agent_b"])
        assert ok is True

        ok2, error, _ = registry.register(
            task_id="task_2", lead="agent_c",
            members=["agent_a"])
        assert ok2 is False
        assert "agents_busy" in error

    def test_parallel_limit_is_three(self):
        availability = AgentAvailability()
        registry = TeamRegistry(availability, max_parallel_teams=3)
        for index in range(3):
            ok, error, _ = registry.register(
                task_id=f"task_{index}",
                lead=f"lead_{index}", members=[f"m_{index}"])
            assert ok is True, error

        ok, error, _ = registry.register(
            task_id="task_4", lead="lead_4", members=["m_4"])
        assert ok is False
        assert error == "max_parallel_teams"

    def test_release_frees_agents(self):
        availability = AgentAvailability()
        registry = TeamRegistry(availability)
        registry.register(task_id="task_1", lead="agent_a", members=["agent_b"])
        assert registry.release("task_1") is True
        assert availability.is_idle("agent_a") is True
        assert availability.is_idle("agent_b") is True
        assert registry.active_count() == 0


# ============================================================
# 5. TaskDraftStore 持久化
# ============================================================
class TestTaskDraftStore:
    def test_round_trip_survives_new_instance(self, tmp_path):
        workspace = Workspace(tmp_path / "agent_a", "agent_a")
        store = TaskDraftStore(
            task_id="task_1",
            workspace_of=lambda aid: workspace)

        written = store.write("agent_a", "hello", label="greeting")
        assert written["ok"] is True

        reloaded = TaskDraftStore(
            task_id="task_1",
            workspace_of=lambda aid: workspace)
        read = reloaded.read("agent_a", written["draft_id"])
        assert read["ok"] is True
        assert read["content"] == "hello"

    def test_tasks_are_isolated(self, tmp_path):
        workspace = Workspace(tmp_path / "agent_a", "agent_a")
        first = TaskDraftStore(
            task_id="task_1", workspace_of=lambda aid: workspace)
        second = TaskDraftStore(
            task_id="task_2", workspace_of=lambda aid: workspace)

        draft = first.write("agent_a", "task 1 draft")
        assert second.read("agent_a", draft["draft_id"])["ok"] is False
        assert second.list("agent_a")["count"] == 0


# ============================================================
# 6. SoloWorkRuntime 最小闭环
# ============================================================
class _FakeAgent:
    def __init__(self):
        self.has_brain = True
        self.workspace = None

    def act(self, *, messages, dispatcher, **kwargs):
        intent = ActionIntent(
            action_type="work_complete",
            parameters={
                "result_ref": "hello-v1",
                "summary": "已写出 hello.txt",
            },
            agent_id="agent_a",
        )
        action_result = dispatcher.dispatch(intent)
        segment = SimpleNamespace(
            tokens=11, content="done", is_truncated=False)
        return SimpleNamespace(
            segments=[segment],
            action_intents=[intent],
            action_results=[action_result],
        )


class _SoloRuntime:
    def __init__(self, db, task):
        self.tasks = TaskStore(db)
        self.tasks.save(task)
        self.economy = SimpleNamespace(agent=lambda aid: _FakeAgent())
        self.workspace_gateway = _FakeGateway()
        self.alignment = SimpleNamespace(
            current=lambda tid: SimpleNamespace(
                context=SimpleNamespace(
                    acceptance_criteria=("hello.txt exists",))))
        self.private_working_memory = SimpleNamespace(
            render=lambda aid, tid: "（空）")
        self.private_short_term_memory = SimpleNamespace(
            render=lambda aid: "")
        self.private_long_term_memory = SimpleNamespace(
            render_top=lambda aid: "")
        self.tool_runner = None
        self.experiment_run_id = "test"


class TestSoloWorkRuntime:
    def test_runtime_completes_task(self, db):
        task = Task.create(
            task_id="T-RUNTIME", creator="agent_a",
            description="写 hello.txt", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        result = SoloWorkRuntime(runtime, task, max_turns=3).run()

        assert result.ok is True, result.error
        assert result.status == TaskStatus.COMPLETED
        assert result.turns == 1
        assert runtime.tasks.get(task.task_id).status == TaskStatus.COMPLETED
        assert runtime.tasks.count_executions(task.task_id) == 4

    def test_runtime_resumes_submitted_task(self, db):
        task = Task.create(
            task_id="T-RESUME", creator="agent_a",
            description="d", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")
        task.start()
        task.submit()

        runtime = _SoloRuntime(db, task)
        result = SoloWorkRuntime(runtime, task, max_turns=3).run()

        assert result.ok is True, result.error
        assert runtime.tasks.get(task.task_id).status == TaskStatus.COMPLETED

    def test_runtime_requires_solo_task(self, db):
        task = Task.create(
            task_id="T-TEAM", creator="agent_a",
            participants=["agent_a", "agent_b"], mode=TaskMode.TEAM)
        runtime = _SoloRuntime(db, task)
        with pytest.raises(ValueError, match="SOLO"):
            SoloWorkRuntime(runtime, task)
