"""
Demand Entry V1 回归测试
=========================

覆盖不依赖真实 LLM 的部分：

- task_create 执行器：creator 只来自 intent，不能伪造；
- TaskCreationService：参与者/DoD/producer 校验，创建 Root Task；
- RunScheduler：串行轮询、终态、墙钟、轮数、无进展；
- CLI 参数。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from actions import ACTION_TASK_CREATE, ActionIntent, TaskCreateExecutor
from demand.models import DemandRun, TurnOutcome
from demand.scheduler import RunScheduler
from demand.task_creation import TaskCreationError, TaskCreationService


# ============================================================
# fakes
# ============================================================
class _Dir:
    def __init__(self, ids):
        self.ids = set(ids)

    def get(self, agent_id):
        if agent_id not in self.ids:
            return None
        return SimpleNamespace(agent_id=agent_id, available=True)

    def all(self):
        return [SimpleNamespace(agent_id=a, available=True,
                                capabilities=SimpleNamespace(names=[]))
                for a in sorted(self.ids)]


class _Gateway:
    def __init__(self):
        self.handoffs = []

    def ensure_workspace(self, ws_id, owner):
        return SimpleNamespace(success=True, error=None)

    def handoff(self, ws_id, path, *, from_agent, to_agent, actor,
                reason="", task_id=None):
        self.handoffs.append((ws_id, path, from_agent, to_agent, actor))
        return SimpleNamespace(success=True, error=None)

    def list(self, ws_id, path, agent_id):
        return SimpleNamespace(success=True, result={"entries": []})


class _Alignment:
    def __init__(self):
        self.changes = []

    def require_current(self, task_id):
        return SimpleNamespace(revision=1)

    def current(self, task_id):
        return SimpleNamespace(
            revision=1,
            context=SimpleNamespace(
                objective="", acceptance_criteria=(), assignees_for=lambda *a: (),
                to_dict=lambda: {"objective": ""}))

    def propose(self, task_id, **kwargs):
        self.changes.append(kwargs)
        return SimpleNamespace(change_id=f"chg_{len(self.changes)}")

    def accept(self, task_id, change_id, **kwargs):
        return None


class _Budget:
    def __init__(self):
        self.set = []

    def set_budget(self, task_id, amount):
        self.set.append((task_id, amount))


class _Runtime:
    def __init__(self, ids=("agent_a", "agent_b", "agent_c")):
        self.directory = _Dir(ids)
        self.workspace_gateway = _Gateway()
        self.alignment = _Alignment()
        self.budget_service = _Budget()
        self.tasks = {}
        self.experiment_run_id = "run_test"

    def create_task(self, task):
        self.tasks[task.task_id] = task
        return task


def _run(**over):
    data = dict(
        run_id="demand_test",
        demand="do it",
        budget_aic=10.0,
        wall_limit_seconds=600.0,
        max_turns=20,
        max_agents=5,
        front_agent="agent_a",
        run_dir="/tmp/demand_test",
    )
    data.update(over)
    return DemandRun(**data)


# ============================================================
# task_create executor
# ============================================================
class TestTaskCreateExecutor:
    def test_creator_comes_from_intent_not_parameters(self):
        seen = {}

        def create_fn(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "task_id": "task_x",
                    "workspace_id": "WS-X", "participants": ["agent_a"],
                    "status": "CREATED"}

        ex = TaskCreateExecutor(create_fn)
        intent = ActionIntent(
            action_type=ACTION_TASK_CREATE, agent_id="agent_a",
            parameters={"creator": "evil", "description": "d",
                        "participants": ["agent_a"], "producer": "agent_a",
                        "acceptance_criteria": ["ok"]})
        result = ex.execute(intent)
        assert result.success is True
        assert seen["creator"] == "agent_a"
        assert result.output["task_id"] == "task_x"

    def test_failure_is_structured(self):
        ex = TaskCreateExecutor(
            lambda **kw: {"ok": False, "code": "bad", "message": "no"})
        result = ex.execute(ActionIntent(
            action_type=ACTION_TASK_CREATE, agent_id="agent_a",
            parameters={}))
        assert result.success is False
        assert result.metadata["code"] == "bad"


# ============================================================
# TaskCreationService validation / create
# ============================================================
class TestTaskCreationService:
    def _svc(self, runtime=None):
        return TaskCreationService(runtime or _Runtime(), _run(), max_agents=5)

    def test_requires_creator_in_participants(self):
        with pytest.raises(TaskCreationError, match="creator"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_b"], acceptance_criteria=["ok"],
                producer="agent_b", acceptance_assignee=None)

    def test_requires_at_least_two_participants(self):
        with pytest.raises(TaskCreationError, match="2 个参与者"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a"], acceptance_criteria=["ok"],
                producer="agent_a", acceptance_assignee=None)

    def test_requires_producer(self):
        with pytest.raises(TaskCreationError, match="producer 必填"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b"],
                acceptance_criteria=["ok"], producer=None,
                acceptance_assignee=None)

    def test_acceptance_assignee_cannot_be_creator(self):
        with pytest.raises(TaskCreationError, match="不能把验收责任派给自己"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b"],
                acceptance_criteria=["ok"], producer="agent_a",
                acceptance_assignee="agent_a")

    def test_acceptance_assignee_cannot_be_producer(self):
        with pytest.raises(TaskCreationError, match="不能是 producer"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b"],
                acceptance_criteria=["ok"], producer="agent_b",
                acceptance_assignee="agent_b")

    def test_requires_dod(self):
        with pytest.raises(TaskCreationError, match="acceptance_criteria"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b"],
                acceptance_criteria=[], producer="agent_a",
                acceptance_assignee=None)

    def test_create_root_task_and_handoff_to_producer(self):
        rt = _Runtime()
        run = _run()
        svc = TaskCreationService(rt, run, max_agents=5)
        out = svc.create(
            creator="agent_a", description="build it",
            participants=["agent_a", "agent_b"],
            acceptance_criteria=["file exists"],
            producer="agent_b", acceptance_assignee=None)
        assert out["ok"] is True
        task = rt.tasks[out["task_id"]]
        assert task.creator == "agent_a"
        assert task.contractor == "agent_b"
        assert set(task.participants) == {"agent_a", "agent_b"}
        assert task.budget is None  # 人类预算是 TaskBudget，不是商业 price
        assert out["workspace_id"] == "WS-DEMAND-demand_test"
        assert rt.workspace_gateway.handoffs  # root 交给 producer
        assert rt.budget_service.set

    def test_create_twice_is_rejected(self):
        rt = _Runtime()
        run = _run()
        svc = TaskCreationService(rt, run, max_agents=5)
        first = svc.create(
            creator="agent_a", description="build it",
            participants=["agent_a", "agent_b"],
            acceptance_criteria=["file exists"],
            producer="agent_a", acceptance_assignee=None)
        assert first["ok"] is True
        second = svc.create(
            creator="agent_a", description="again",
            participants=["agent_a", "agent_b"],
            acceptance_criteria=["file exists"],
            producer="agent_a", acceptance_assignee=None)
        assert second["ok"] is False
        assert second["code"] == "task_already_created"


# ============================================================
# RunScheduler
# ============================================================
class TestRunScheduler:
    def test_terminal_at_start_makes_no_turns(self):
        calls = []
        s = RunScheduler(
            participants=["a", "b"],
            run_turn=lambda a: calls.append(a) or TurnOutcome(a),
            task_status=lambda: "COMPLETED",
            max_turns=10)
        result = s.run()
        assert result.exit_reason == "COMPLETED"
        assert calls == []

    def test_round_robin_and_completed(self):
        calls = []
        status = {"v": "IN_PROGRESS"}

        def turn(a):
            calls.append(a)
            if len(calls) == 3:
                status["v"] = "COMPLETED"
            return TurnOutcome(a, ok=True, actions=1,
                               status_before="IN_PROGRESS",
                               status_after=status["v"])

        s = RunScheduler(participants=["a", "b"], run_turn=turn,
                         task_status=lambda: status["v"], max_turns=10)
        result = s.run()
        assert result.exit_reason == "COMPLETED"
        assert calls == ["a", "b", "a"]

    def test_max_turns(self):
        s = RunScheduler(participants=["a"], run_turn=lambda a: TurnOutcome(a, actions=1),
                         task_status=lambda: "IN_PROGRESS", max_turns=2)
        result = s.run()
        assert result.exit_reason == "MAX_TURNS"
        assert result.turns == 2

    def test_time_limit(self):
        s = RunScheduler(participants=["a"], run_turn=lambda a: TurnOutcome(a),
                         task_status=lambda: "IN_PROGRESS",
                         max_turns=10, deadline=time.monotonic() - 1)
        assert s.run().exit_reason == "TIME_LIMIT"

    def test_no_progress(self):
        s = RunScheduler(participants=["a"], run_turn=lambda a: TurnOutcome(a),
                         task_status=lambda: "IN_PROGRESS",
                         max_turns=20, no_progress_cycles=2)
        assert s.run().exit_reason == "NO_PROGRESS"


# ============================================================
# CLI
# ============================================================
def test_cli_parse_file_and_defaults():
    from demand.cli import build_parser

    args = build_parser().parse_args(["--file", "x.txt", "--minutes", "5"])
    assert args.file == "x.txt"
    assert args.minutes == 5
    assert args.front_agent == "agent_a"
    assert args.max_turns == 30


def test_demand_task_dispatcher_includes_memory_and_search():
    """Demand 的任务阶段必须暴露生产层已有的 Agent 能力。"""
    from demand.actions import build_task_dispatcher
    from core.task import Task
    from core.task_workspace import TaskWorkspaceRef

    class _Bridge:
        def run(self, coro):
            return {"ok": True}

    task = Task.create(
        creator="agent_a", description="x", workspace_id="WS-X",
        participants=["agent_a", "agent_b"])
    runtime = SimpleNamespace(
        workspace_gateway=SimpleNamespace(),
        communication=SimpleNamespace(),
        alignment=SimpleNamespace(),
        economy=SimpleNamespace(),
        tool_runner=SimpleNamespace(),
        public_working_memory=SimpleNamespace(),
        private_working_memory=SimpleNamespace(),
        private_short_term_memory=SimpleNamespace(),
        private_long_term_memory=SimpleNamespace(),
    )
    dispatcher = build_task_dispatcher(runtime, task=task, bridge=_Bridge())
    expected = {
        "workspace_read", "workspace_search", "workspace_write",
        "workspace_edit", "message_send", "public_query",
        "private_write", "private_read", "private_list", "private_delete",
        "run_tool", "resource_create", "resource_handoff",
        "resource_release", "assign_acceptance", "shared_context_read",
        "public_working_memory_read", "public_working_memory_update",
        "private_working_memory_read", "private_working_memory_update",
        "short_term_memory_read", "short_term_memory_update",
        "short_term_memory_reinforce",
        "long_term_memory_search", "long_term_memory_read",
        "long_term_memory_mark_used", "long_term_memory_consolidate",
    }
    got = set(dispatcher.action_types)
    assert expected <= got, sorted(expected - got)
