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
from actions.intent import ActionResult
from demand.models import DemandRun, TurnOutcome
from demand.runner import DemandRunner
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

    def test_workspace_plan_root_owner_must_be_producer(self):
        with pytest.raises(TaskCreationError, match="root_owner"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b", "agent_c"],
                acceptance_criteria=["ok"], producer="agent_b",
                acceptance_assignee="agent_c",
                workspace_plan={
                    "root_owner": "agent_a",
                    "files": {"index.html": "agent_a"},
                })

    def test_root_file_owner_must_be_root_owner(self):
        """A 计划写根级 index.html，但 producer/root 是 C → 必须拒绝。"""
        with pytest.raises(TaskCreationError, match="根级文件"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b", "agent_c"],
                acceptance_criteria=["ok"], producer="agent_c",
                acceptance_assignee="agent_b",
                workspace_plan={
                    "root_owner": "agent_c",
                    "files": {"index.html": "agent_a"},
                })

    def test_file_under_directory_can_have_different_owner(self):
        """src/main.js 归 A，但 src/ 归 C：允许，由 C 创建后 handoff。"""
        values = self._svc()._validate(
            creator="agent_a", description="d",
            participants=["agent_a", "agent_b", "agent_c"],
            acceptance_criteria=["ok"], producer="agent_c",
            acceptance_assignee="agent_b",
            workspace_plan={
                "root_owner": "agent_c",
                "directories": {"src": "agent_c"},
                "files": {"src/main.js": "agent_a"},
            })
        assert values["workspace_plan"]["root_owner"] == "agent_c"
        assert values["workspace_plan"]["files"]["src/main.js"] == "agent_a"

    def test_file_parent_directory_must_be_declared(self):
        with pytest.raises(TaskCreationError, match="父目录"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b", "agent_c"],
                acceptance_criteria=["ok"], producer="agent_c",
                acceptance_assignee="agent_b",
                workspace_plan={
                    "root_owner": "agent_c",
                    "files": {"src/main.js": "agent_a"},
                })

    def test_workspace_plan_is_required(self):
        with pytest.raises(TaskCreationError, match="workspace_plan"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b"],
                acceptance_criteria=["ok"], producer="agent_a",
                acceptance_assignee=None)

    def test_create_root_task_and_handoff_to_producer(self):
        rt = _Runtime()
        run = _run()
        svc = TaskCreationService(rt, run, max_agents=5)
        out = svc.create(
            creator="agent_a", description="build it",
            participants=["agent_a", "agent_b"],
            acceptance_criteria=["file exists"],
            producer="agent_b", acceptance_assignee=None,
            workspace_plan={
                "root_owner": "agent_b",
                "directories": {"src": "agent_b"},
                "files": {"index.html": "agent_b"},
            })
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
            producer="agent_a", acceptance_assignee=None,
            workspace_plan={
                "root_owner": "agent_a",
                "files": {"index.html": "agent_a"},
            })
        assert first["ok"] is True
        second = svc.create(
            creator="agent_a", description="again",
            participants=["agent_a", "agent_b"],
            acceptance_criteria=["file exists"],
            producer="agent_a", acceptance_assignee=None,
            workspace_plan={
                "root_owner": "agent_a",
                "files": {"index.html": "agent_a"},
            })
        assert second["ok"] is False
        assert second["code"] == "task_already_created"

    def test_create_solo_task(self):
        """ADR-0056：Solo 单参与者，无验收人，Creator 自己生产。"""
        rt = _Runtime()
        svc = TaskCreationService(rt, _run(), max_agents=5)
        out = svc.create(
            creator="agent_a", description="solo work",
            participants=["agent_a"],
            acceptance_criteria=["file exists"],
            producer="agent_a",
            mode="SOLO",
        )
        assert out["ok"] is True, out
        assert out["mode"] == "SOLO"
        task = rt.tasks[out["task_id"]]
        assert task.mode == "SOLO"
        assert task.contractor == "agent_a"
        assert task.participants == ["agent_a"]
        # Solo 不需要 root lock handoff
        assert rt.workspace_gateway.handoffs == []

    def test_solo_rejects_extra_participants(self):
        with pytest.raises(TaskCreationError, match="SOLO"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a", "agent_b"],
                acceptance_criteria=["ok"], producer="agent_a",
                acceptance_assignee=None, mode="SOLO")

    def test_solo_rejects_acceptance_assignee(self):
        with pytest.raises(TaskCreationError, match="acceptance_assignee"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a"],
                acceptance_criteria=["ok"], producer="agent_a",
                acceptance_assignee="agent_b", mode="SOLO")

    def test_solo_producer_must_be_creator(self):
        with pytest.raises(TaskCreationError, match="producer"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a"],
                acceptance_criteria=["ok"], producer="agent_b",
                acceptance_assignee=None, mode="SOLO")

    def test_unknown_mode_rejected(self):
        with pytest.raises(TaskCreationError, match="mode"):
            self._svc()._validate(
                creator="agent_a", description="d",
                participants=["agent_a"],
                acceptance_criteria=["ok"], producer="agent_a",
                acceptance_assignee=None, mode="PAIR")


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

    def test_actions_without_real_progress_do_not_reset_the_counter(self):
        """
        🔴 真实回归：`workspace_read` / `run_tool` 不是进展。

        旧判据是 `outcome.actions > 0`，于是反复读同一个文件
        也能把 NO_PROGRESS 一直重置 —— 两个真实 run 都这样被
        MAX_TURNS 拖死。
        """
        s = RunScheduler(
            participants=["a"],
            run_turn=lambda a: TurnOutcome(a, actions=3, progress=False),
            task_status=lambda: "IN_PROGRESS",
            max_turns=20, no_progress_cycles=2)
        assert s.run().exit_reason == "NO_PROGRESS"

    def test_truncated_turn_gets_one_grace_cycle(self):
        """认知被截断不是“选择不做”；给它一次下一轮恢复的机会。"""
        calls = {"n": 0}

        def turn(a):
            calls["n"] += 1
            return TurnOutcome(
                a, actions=0, truncated=(calls["n"] == 1))

        s = RunScheduler(
            participants=["a"], run_turn=turn,
            task_status=lambda: "IN_PROGRESS",
            max_turns=10, no_progress_cycles=1,
            min_no_progress_turns=1)
        result = s.run()
        assert result.exit_reason == "NO_PROGRESS"
        assert result.turns == 2, "截断后应至少再来一轮，而不是立刻停"

    def test_repeated_truncation_does_not_extend_forever(self):
        """每个 Agent 只享受一次截断宽限；连续截断仍会停。"""
        s = RunScheduler(
            participants=["a"],
            run_turn=lambda a: TurnOutcome(a, actions=0, truncated=True),
            task_status=lambda: "IN_PROGRESS",
            max_turns=10, no_progress_cycles=1,
            min_no_progress_turns=1)
        result = s.run()
        assert result.exit_reason == "NO_PROGRESS"
        assert result.turns == 2

    def test_no_progress_waits_for_minimum_turns(self):
        """1 人团队 3 cycles 只有 3 turn；至少 8 turn 才允许停。"""
        s = RunScheduler(
            participants=["a"],
            run_turn=lambda a: TurnOutcome(a, actions=0),
            task_status=lambda: "IN_PROGRESS",
            max_turns=20, no_progress_cycles=1,
            min_no_progress_turns=8)
        result = s.run()
        assert result.exit_reason == "NO_PROGRESS"
        assert result.turns == 8

    def test_minimum_window_applies_to_two_participants(self):
        """2 人团队 3 cycles = 6 turn；最小窗口 8 turn 才停。"""
        s = RunScheduler(
            participants=["a", "b"],
            run_turn=lambda a: TurnOutcome(a, actions=0),
            task_status=lambda: "IN_PROGRESS",
            max_turns=20, no_progress_cycles=3,
            min_no_progress_turns=8)
        result = s.run()
        assert result.exit_reason == "NO_PROGRESS"
        assert result.turns == 8

    def test_real_progress_resets_the_counter(self):
        s = RunScheduler(
            participants=["a"],
            run_turn=lambda a: TurnOutcome(
                a, actions=1, progress=True, strong_progress=True),
            task_status=lambda: "IN_PROGRESS",
            max_turns=2, no_progress_cycles=1)
        assert s.run().exit_reason == "MAX_TURNS"

    def test_messages_alone_do_not_reset_the_counter(self):
        """普通协调消息只能算弱进展，不能无限延长 run。"""
        s = RunScheduler(
            participants=["a"],
            run_turn=lambda a: TurnOutcome(
                a, actions=1, progress=True, strong_progress=False),
            task_status=lambda: "IN_PROGRESS",
            max_turns=20, no_progress_cycles=2)
        assert s.run().exit_reason == "NO_PROGRESS"


# ============================================================
# DemandRunner 的进展判定
# ============================================================
class TestDemandProgress:
    def _runner(self):
        runner = object.__new__(DemandRunner)
        runner._message_signatures = set()
        runner._recent_messages = []
        runner._turn_index = 1
        runner._pending_requests = {}
        return runner

    def _intent(self, action_type, **params):
        return ActionIntent(
            action_type=action_type, agent_id="agent_a",
            parameters=dict(params))

    def _ok(self, action_type):
        return ActionResult(
            success=True, output={}, action_id="a",
            action_type=action_type)

    def test_read_and_tool_are_not_progress(self):
        runner = self._runner()
        intents = [self._intent("workspace_read", path="index.html"),
                   self._intent("run_tool", tool="python", args=[])]
        results = [self._ok("workspace_read"), self._ok("run_tool")]
        progress, reason = runner._progress_of(
            "IN_PROGRESS", "IN_PROGRESS", intents, results, [])
        assert progress is False
        assert reason == ""

    def test_successful_write_is_progress(self):
        runner = self._runner()
        intents = [self._intent("workspace_write", path="x",
                                write_mode="create", content="hi")]
        results = [self._ok("workspace_write")]
        progress, reason = runner._progress_of(
            "IN_PROGRESS", "IN_PROGRESS", intents, results, [])
        assert progress is True
        assert "workspace_write" in reason

    def test_duplicate_message_is_not_new_progress(self):
        runner = self._runner()
        intent = self._intent(
            "message_send", to="agent_b", message_type="TEXT", text="blocked")
        result = self._ok("message_send")
        first = runner._record_messages(
            "agent_a", [intent], [result])
        runner._progress_of("IN_PROGRESS", "IN_PROGRESS",
                            [intent], [result], first)
        assert first == [id(intent)]

        second = runner._record_messages(
            "agent_a", [intent], [result])
        progress, _ = runner._progress_of(
            "IN_PROGRESS", "IN_PROGRESS", [intent], [result], second)
        assert second == []
        assert progress is False

    def test_lifecycle_message_is_not_new_message_progress(self):
        """
        非法 START 的 ActionResult 也可能是 success（已投递）。
        它不能靠 `new_message` 延长 run；只有 Task 状态变化才算进展。
        """
        runner = self._runner()
        intent = self._intent(
            "message_send", to="agent_b", message_type="START",
            text="我要开始协调")
        result = self._ok("message_send")
        new = runner._record_messages("agent_a", [intent], [result])
        progress, reason = runner._progress_of(
            "IN_PROGRESS", "IN_PROGRESS", [intent], [result], new)
        assert new == [id(intent)]
        assert progress is False, reason

    def test_status_change_is_progress(self):
        runner = self._runner()
        progress, reason = runner._progress_of(
            "IN_PROGRESS", "SUBMITTED", [], [], [])
        assert progress is True
        assert reason == "task_status"

    def test_pending_request_is_cleared_by_an_actual_action(self):
        runner = self._runner()
        msg = SimpleNamespace(
            message_id="m1", message_type="REQUEST",
            from_agent="agent_e", to_agent="agent_a",
            text="请建 parts/ 并 handoff", payload={})
        runner._collect_pending_requests("agent_a", [msg])
        assert runner._pending_requests["agent_a"], "请求没有被记录"

        intent = self._intent(
            "message_send", to="agent_e", message_type="TEXT", text="好")
        result = self._ok("message_send")
        runner._clear_answered_requests(
            "agent_a", [intent], [result], "IN_PROGRESS")
        assert runner._pending_requests["agent_a"] == []

    def test_pending_request_survives_a_read_only_turn(self):
        runner = self._runner()
        msg = SimpleNamespace(
            message_id="m2", message_type="REQUEST",
            from_agent="agent_e", to_agent="agent_a",
            text="请建 parts/ 并 handoff", payload={})
        runner._collect_pending_requests("agent_a", [msg])

        read = self._intent("workspace_read", path="index.html")
        result = self._ok("workspace_read")
        runner._clear_answered_requests(
            "agent_a", [read], [result], "IN_PROGRESS")
        assert runner._pending_requests["agent_a"], (
            "只读文件不应该把请求清掉")

    def test_blocking_report_names_the_waiter(self):
        """MAX_TURNS 不能只说“停了”，要回答“在等谁、等什么动作”。"""
        runner = object.__new__(DemandRunner)
        runner._last_progress_turn = 4
        runner._last_strong_progress_turn = 4
        runner._recent_messages = []
        runner._last_turn_by_agent = {}
        runner.rt = SimpleNamespace(
            task_execution=SimpleNamespace(
                acceptors_of=lambda task: {"agent_b"}))
        task = SimpleNamespace(
            status="SUBMITTED", creator="agent_a",
            contractor="agent_a", task_id="task_x")
        report = runner._blocking_report(task)
        assert report["waiting_for"] == "agent_b"
        assert report["waiting_action"] == "VERIFY"
        assert report["last_progress_turn"] == 4

    def _pressure_runner(self):
        runner = object.__new__(DemandRunner)
        runner._turn_index = 1
        runner._pressure_streak = {}
        runner._pressure_total = {}
        runner._pressure_last_turn = {}
        return runner

    def _segment(self, *, truncated=False, clipped=None):
        return SimpleNamespace(
            phase="work", finish_reason="length" if truncated else "stop",
            output_status="EMPTY_CONTENT" if truncated else "VALID_CONTENT",
            cognitive_status="TRUNCATED" if truncated else "COMPLETE",
            is_truncated=truncated,
            requested_cognitive_budget=32768,
            actual_max_tokens=16384 if clipped else 32768,
            budget_clipped=clipped,
        )

    def test_truncation_increments_work_unit_pressure(self):
        runner = self._pressure_runner()
        first = runner._budget_feedback(
            [self._segment(truncated=True)], "work", "agent_a")
        assert first["pressure"] is True
        assert first["pressure_kinds"] == "truncated"
        assert first["pressure_streak"] == 1
        assert first["pressure_total"] == 1

        runner._turn_index = 2
        second = runner._budget_feedback(
            [self._segment(truncated=True)], "work", "agent_a")
        assert second["pressure_streak"] == 2
        assert second["pressure_total"] == 2

        runner._turn_index = 3
        normal = runner._budget_feedback(
            [self._segment()], "work", "agent_a")
        assert normal["pressure"] is False
        assert normal["pressure_streak"] == 0
        assert normal["pressure_total"] == 2

    def test_budget_clip_is_work_unit_pressure(self):
        runner = self._pressure_runner()
        fb = runner._budget_feedback(
            [self._segment(clipped="above_caller_limit")],
            "work", "agent_a")
        assert fb["pressure"] is True
        assert "clipped" in fb["pressure_kinds"]
        assert fb["pressure_total"] == 1

    def test_below_min_is_not_work_unit_pressure(self):
        runner = self._pressure_runner()
        fb = runner._budget_feedback(
            [self._segment(clipped="below_min")], "work", "agent_a")
        assert fb["pressure"] is False

    def test_truncation_and_clip_keep_their_own_fields(self):
        """同一个 turn 里两段分别截断/裁剪时，字段不能互相错配。"""
        runner = self._pressure_runner()
        clipped = self._segment(clipped="above_caller_limit")
        truncated = self._segment(truncated=True)
        fb = runner._budget_feedback(
            [clipped, truncated], "work", "agent_a")
        assert fb["truncated"] is True
        assert fb["pressure"] is True
        assert set(fb["pressure_kinds"].split(",")) == {
            "truncated", "clipped"}
        # requested/actual/clipped 来自 clipped 段
        assert fb["requested"] == 32768
        assert fb["actual"] == 16384
        assert fb["clipped"] == "above_caller_limit"

    def test_repaired_truncation_is_work_unit_pressure(self):
        """原始调用截断、修复成功后，压力信号不能消失。"""
        runner = self._pressure_runner()
        repaired = SimpleNamespace(
            phase="decision", finish_reason="stop",
            output_status="VALID_CONTENT", cognitive_status="COMPLETE",
            is_truncated=False, repair_truncated=True,
            requested_cognitive_budget=16384,
            actual_max_tokens=16384, budget_clipped=None,
            original_budget_clipped=None,
        )
        fb = runner._budget_feedback(
            [repaired], "decision", "agent_a")
        assert fb["truncated"] is False
        assert fb["repair_truncated"] is True
        assert fb["pressure"] is True
        assert fb["pressure_kinds"] == "truncated"


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
    assert args.cognitive_budget == 32768


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
