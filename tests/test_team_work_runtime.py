"""
TeamWorkRuntime —— Leader 计划 / 并行 / QA / 汇总
==================================================

验证用户定义的团队工作方式：

    Leader 计划架构 → 分配 items → 多 Agent 并行 →
    (可选) QA → Leader 汇总收尾

重点：

    · 没有 QA 时 Leader 完成；
    · 有 QA 时 QA 通过才能完成；
    · QA 不通过时 Task 不能 COMPLETED；
    · 成员只拿到自己的 item；
    · Work 层没有消息 / 经济动作。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from actions import ActionIntent
from core.task import Task, TaskMode, TaskStatus
from core.task_store import TaskStore
from work.actions import FORBIDDEN_WORK_ACTION_TYPES, WORK_ACTION_TYPES
from work.plan import parse_work_plan
from work.team_runtime import TeamWorkRuntime


# ============================================================
# fakes
# ============================================================
class _Gateway:
    def list(self, ws_id, path, agent_id):
        return SimpleNamespace(success=True, result={"entries": []})


class _ScriptedAgent:
    has_brain = True
    workspace = None

    def __init__(self, *, think=None, actions=None):
        self._think = think
        self._actions = list(actions or [])

    def think(self, prompt):
        if callable(self._think):
            return self._think(prompt)
        return self._think or ""

    def act(self, *, messages, dispatcher, **kwargs):
        action = None
        if self._actions:
            action = self._actions.pop(0)
        if action is None:
            return SimpleNamespace(
                segments=[SimpleNamespace(tokens=3, content="", is_truncated=False)],
                action_intents=[], action_results=[])
        if callable(action):
            return action(dispatcher)
        intent = ActionIntent(
            action_type=action["type"],
            parameters=action.get("parameters") or {},
            agent_id=action.get("agent_id", ""),
        )
        result = dispatcher.dispatch(intent)
        return SimpleNamespace(
            segments=[SimpleNamespace(tokens=3, content="", is_truncated=False)],
            action_intents=[intent], action_results=[result])


def _done(item_id, agent_id):
    return {
        "type": "work_item_done",
        "agent_id": agent_id,
        "parameters": {"item_id": item_id, "summary": f"{item_id} done"},
    }


def _verdict(passed):
    return {
        "type": "work_qa_verdict",
        "agent_id": "agent_c",
        "parameters": {"passed": passed, "feedback": "" if passed else "需要修"},
    }


def _complete():
    return {
        "type": "work_complete",
        "agent_id": "agent_a",
        "parameters": {
            "result_ref": "team-result-v1",
            "summary": "团队完成并汇总",
        },
    }


class _TeamRuntime:
    def __init__(self, db, task, agents):
        self.tasks = TaskStore(db)
        self.tasks.save(task)
        self.economy = SimpleNamespace(agent=lambda aid: agents.get(aid))
        self.workspace_gateway = _Gateway()
        self.alignment = None
        self.private_working_memory = SimpleNamespace(
            render=lambda aid, tid: "")
        self.private_short_term_memory = SimpleNamespace(
            render=lambda aid: "")
        self.private_long_term_memory = SimpleNamespace(
            render_top=lambda aid: "")
        self.tool_runner = None


def _task(participants=("agent_a", "agent_b", "agent_c")):
    task = Task.create(
        task_id="T-TEAM-RUN",
        creator="agent_a",
        description="做一个两模块项目",
        workspace_id="",
        participants=list(participants),
        mode=TaskMode.TEAM,
    )
    task.claim_producer("agent_a")
    return task


def _plan(qa=None):
    return json.dumps({
        "items": [
            {"item_id": "item_1", "assigned_to": "agent_b",
             "goal": "模块 B"},
            {"item_id": "item_2", "assigned_to": "agent_c",
             "goal": "模块 C"},
        ],
        "qa": qa,
        "directories": {},
    })


def _plan_single():
    return json.dumps({
        "items": [
            {"item_id": "item_1", "assigned_to": "agent_b",
             "goal": "模块 B"},
        ],
        "qa": None,
        "directories": {},
    })


def _blocked(item_id, agent_id, reason="卡住了"):
    return {
        "type": "work_item_blocked",
        "agent_id": agent_id,
        "parameters": {"item_id": item_id, "reason": reason},
    }


def _plan_update_reassign(item_id, assigned_to, revision=1):
    return {
        "type": "work_plan_update",
        "agent_id": "agent_a",
        "parameters": {
            "expected_revision": revision,
            "reason": "改派 blocked item",
            "operations": [{
                "op": "reassign",
                "item_id": item_id,
                "assigned_to": assigned_to,
            }],
        },
    }


# ============================================================
# 1. Plan parser
# ============================================================
class TestWorkPlan:
    def test_parse_plan(self):
        plan, error = parse_work_plan(
            _plan(qa="agent_c"),
            participants=["agent_a", "agent_b", "agent_c"])
        assert error == ""
        assert plan is not None
        assert [item.agent_id for item in plan.items] == ["agent_b", "agent_c"]
        assert plan.qa == "agent_c"

    def test_reject_unknown_assignee(self):
        plan, error = parse_work_plan(
            json.dumps({"items": [
                {"assigned_to": "ghost", "goal": "x"}]}),
            participants=["agent_a"])
        assert plan is None
        assert "unknown_agent" in error

    def test_reject_unknown_qa(self):
        plan, error = parse_work_plan(
            _plan(qa="ghost"),
            participants=["agent_a", "agent_b", "agent_c"])
        assert plan is None
        assert "qa_unknown_agent" in error

    def test_reject_duplicate_item_id(self):
        plan, error = parse_work_plan(
            json.dumps({"items": [
                {"item_id": "x", "assigned_to": "agent_a", "goal": "1"},
                {"item_id": "x", "assigned_to": "agent_b", "goal": "2"},
            ]}),
            participants=["agent_a", "agent_b"])
        assert plan is None
        assert "duplicate_item_id" in error


# ============================================================
# 2. Replan operations
# ============================================================
class TestReplan:
    def _plan(self):
        plan, error = parse_work_plan(
            _plan(), participants=["agent_a", "agent_b", "agent_c"])
        assert error == ""
        return plan

    def test_reassign(self):
        from work.replan import apply_plan_operations

        plan = self._plan()
        new, applied = apply_plan_operations(
            plan,
            [{"op": "reassign", "item_id": "item_1",
              "assigned_to": "agent_c"}],
            participants=["agent_a", "agent_b", "agent_c"],
            leader="agent_a",
            item_status={"item_1": "BLOCKED"},
        )
        assert new.revision == plan.revision + 1
        assert new.items[0].agent_id == "agent_c"
        assert applied[0]["op"] == "reassign"

    def test_add_and_cancel(self):
        from work.replan import apply_plan_operations

        plan = self._plan()
        new, _ = apply_plan_operations(
            plan,
            [
                {"op": "add", "item": {
                    "item_id": "item_3",
                    "assigned_to": "agent_b",
                    "goal": "补测试"}},
                {"op": "cancel", "item_id": "item_2"},
            ],
            participants=["agent_a", "agent_b", "agent_c"],
            leader="agent_a",
            item_status={"item_1": "DONE"},
        )
        assert [item.item_id for item in new.items] == ["item_1", "item_3"]

    def test_cannot_reassign_done_item(self):
        from work.replan import ReplanError, apply_plan_operations

        with pytest.raises(ReplanError, match="item_already_done"):
            apply_plan_operations(
                self._plan(),
                [{"op": "reassign", "item_id": "item_1",
                  "assigned_to": "agent_c"}],
                participants=["agent_a", "agent_b", "agent_c"],
                leader="agent_a",
                item_status={"item_1": "DONE"},
            )

    def test_cannot_set_leader_as_qa(self):
        from work.replan import ReplanError, apply_plan_operations

        with pytest.raises(ReplanError, match="qa_is_leader"):
            apply_plan_operations(
                self._plan(),
                [{"op": "update", "item_id": "item_1", "goal": "x"}],
                participants=["agent_a", "agent_b", "agent_c"],
                leader="agent_a",
                qa="agent_a",
                qa_provided=True,
            )


# ============================================================
# 3. Team runtime
# ============================================================
class TestTeamRuntime:
    def test_workers_parallel_and_leader_completes(self, db):
        barrier = threading.Barrier(2, timeout=5)

        def worker_action(dispatcher, item_id, agent_id):
            barrier.wait()

            def act(_dispatcher):
                intent = ActionIntent(
                    action_type="work_item_done",
                    parameters={"item_id": item_id,
                                "summary": f"{item_id} done"},
                    agent_id=agent_id,
                )
                result = _dispatcher.dispatch(intent)
                return SimpleNamespace(
                    segments=[SimpleNamespace(
                        tokens=3, content="", is_truncated=False)],
                    action_intents=[intent], action_results=[result])
            return act(dispatcher)

        agents = {
            "agent_a": _ScriptedAgent(
                think=_plan(), actions=[_complete()]),
            "agent_b": _ScriptedAgent(
                actions=[lambda d: worker_action(d, "item_1", "agent_b")]),
            "agent_c": _ScriptedAgent(
                actions=[lambda d: worker_action(d, "item_2", "agent_c")]),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)

        result = TeamWorkRuntime(
            runtime, task, max_worker_turns=2,
            max_integration_turns=2,
        ).run()

        assert result.ok is True, result.error
        assert runtime.tasks.get(task.task_id).status == TaskStatus.COMPLETED
        assert runtime.tasks.count_executions(task.task_id) == 4
        assert any(event["event"] == "team_item_done"
                   for event in result.events)
        assert result.summary == "团队完成并汇总"

    def test_initial_plan_skips_leader_planning_call(self, db):
        """Front/Controller 已经让 Leader 规划过时，runtime 不再重复规划。"""
        from work.plan import parse_work_plan

        plan, error = parse_work_plan(
            _plan(), participants=["agent_a", "agent_b", "agent_c"])
        assert error == ""
        calls = {"think": 0}

        def think(_prompt):
            calls["think"] += 1
            return ""

        agents = {
            "agent_a": _ScriptedAgent(think=think, actions=[_complete()]),
            "agent_b": _ScriptedAgent(actions=[_done("item_1", "agent_b")]),
            "agent_c": _ScriptedAgent(actions=[_done("item_2", "agent_c")]),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)
        result = TeamWorkRuntime(
            runtime, task, initial_plan=plan,
            max_worker_turns=2, max_integration_turns=2,
        ).run()

        assert result.ok is True, result.error
        assert calls["think"] == 0

    def test_qa_must_pass(self, db):
        agents = {
            "agent_a": _ScriptedAgent(
                think=_plan(qa="agent_c"), actions=[_complete()]),
            "agent_b": _ScriptedAgent(actions=[_done("item_1", "agent_b")]),
            "agent_c": _ScriptedAgent(actions=[
                _done("item_2", "agent_c"), _verdict(True)]),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)

        result = TeamWorkRuntime(
            runtime, task, max_worker_turns=2,
            max_integration_turns=2,
        ).run()

        assert result.ok is True, result.error
        assert runtime.tasks.get(task.task_id).status == TaskStatus.COMPLETED

    def test_qa_failure_blocks_completion(self, db):
        agents = {
            "agent_a": _ScriptedAgent(
                think=_plan(qa="agent_c"), actions=[_complete()]),
            "agent_b": _ScriptedAgent(actions=[_done("item_1", "agent_b")]),
            "agent_c": _ScriptedAgent(
                actions=[_done("item_2", "agent_c"),
                         _verdict(False), _verdict(False)]),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)

        result = TeamWorkRuntime(
            runtime, task, max_worker_turns=2,
            max_integration_turns=1,
        ).run()

        assert result.ok is False
        assert runtime.tasks.get(task.task_id).status != TaskStatus.COMPLETED

    def test_blocked_item_replans_and_completes(self, db):
        """
        item blocked → Leader reassign → 新成员完成 → Leader 收尾。
        """
        agents = {
            "agent_a": _ScriptedAgent(
                think=_plan_single(),
                actions=[
                    _plan_update_reassign("item_1", "agent_c"),
                    _complete(),
                ]),
            "agent_b": _ScriptedAgent(
                actions=[_blocked("item_1", "agent_b")]),
            "agent_c": _ScriptedAgent(
                actions=[_done("item_1", "agent_c")]),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)

        result = TeamWorkRuntime(
            runtime, task, max_worker_turns=1,
            max_integration_turns=1, max_replans=2,
        ).run()

        assert result.ok is True, result.error
        assert runtime.tasks.get(task.task_id).status == TaskStatus.COMPLETED
        assert any(event["event"] == "team_plan_updated"
                   for event in result.events)
        assert any(event["event"] == "team_replan_applied"
                   for event in result.events)
        assert len(result.plan_history) >= 2
        assert result.plan_history[-1]["revision"] == 2

    def test_stale_plan_revision_blocks_completion(self, db):
        agents = {
            "agent_a": _ScriptedAgent(
                think=_plan_single(),
                actions=[
                    _plan_update_reassign("item_1", "agent_c", revision=0),
                    _complete(),
                ]),
            "agent_b": _ScriptedAgent(
                actions=[_blocked("item_1", "agent_b")]),
            "agent_c": _ScriptedAgent(
                actions=[_done("item_1", "agent_c")]),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)

        result = TeamWorkRuntime(
            runtime, task, max_worker_turns=1,
            max_integration_turns=1, max_replans=1,
        ).run()

        assert result.ok is False
        assert runtime.tasks.get(task.task_id).status != TaskStatus.COMPLETED
        assert any(event["event"] == "team_replan_no_change"
                   for event in result.events)

    def test_invalid_plan_fails_without_completing(self, db):
        agents = {
            "agent_a": _ScriptedAgent(think="not json", actions=[]),
            "agent_b": _ScriptedAgent(),
            "agent_c": _ScriptedAgent(),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)

        result = TeamWorkRuntime(runtime, task).run()

        assert result.ok is False
        assert "计划" in result.error
        assert runtime.tasks.get(task.task_id).status != TaskStatus.COMPLETED

    def test_worker_item_ownership(self, db):
        """成员不能替别人标记完成。"""
        agents = {
            "agent_a": _ScriptedAgent(
                think=_plan(), actions=[_complete()]),
            "agent_b": _ScriptedAgent(actions=[
                _done("item_2", "agent_b")]),  # item_2 属于 agent_c
            "agent_c": _ScriptedAgent(actions=[_done("item_2", "agent_c")]),
        }
        task = _task()
        runtime = _TeamRuntime(db, task, agents)

        result = TeamWorkRuntime(
            runtime, task, max_worker_turns=1,
            max_integration_turns=2,
        ).run()

        assert any(
            event["event"] == "team_worker_finished"
            and event.get("agent_id") == "agent_b"
            for event in result.events)


class _FakePublicWM:
    def __init__(self):
        self.revision = 0
        self.items = {}

    def read(self, task_id, agent_id):
        return SimpleNamespace(
            revision=self.revision, items=dict(self.items))

    def update(self, task_id, agent_id, *, expected_revision,
               items=None, remove_keys=None):
        assert expected_revision == self.revision
        for key in remove_keys or []:
            self.items.pop(key, None)
        for key, value in (items or {}).items():
            self.items[key] = value
        self.revision += 1
        return SimpleNamespace(revision=self.revision, items=dict(self.items))


class TestPublicWorkingMemoryIsAbstract:
    def test_plan_publishes_architecture_not_task_copy(self, db):
        from work.plan import WorkItem, WorkPlan

        task = _task()
        runtime = _TeamRuntime(db, task, {})
        runtime.public_working_memory = _FakePublicWM()

        plan = WorkPlan(
            revision=1,
            items=[
                WorkItem(
                    item_id="item_1",
                    agent_id="agent_b",
                    goal="这是一段很长很长的任务目标" * 20,
                    paths=["game/src/a.js", "game/src/b.js"],
                    acceptance=["很长很长的验收标准" * 20],
                    notes="很长很长的实现说明" * 20,
                ),
            ],
            directories={"game": "agent_b", "game/src": "agent_b"},
            qa="agent_c",
        )
        team = TeamWorkRuntime(runtime, task)
        team._publish_plan_to_public_memory(plan)

        items = runtime.public_working_memory.items
        assert items["team.architecture"]["value"]["files"] == {
            "game/src/a.js": "agent_b",
            "game/src/b.js": "agent_b",
        }
        assert items["item.item_1"]["value"] == {
            "owner": "agent_b",
            "paths": ["game/src/a.js", "game/src/b.js"],
        }
        # 不复制 goal / acceptance / notes
        dumped = json.dumps(items, ensure_ascii=False)
        assert "很长很长的任务目标" not in dumped
        assert "很长很长的验收标准" not in dumped
        assert "很长很长的实现说明" not in dumped
        assert len(dumped) < 1500


# ============================================================
# 3. 动作边界
# ============================================================
def test_work_action_set_has_no_messages_or_economy():
    for banned in ("message_send", "task_create", "assign_acceptance"):
        assert banned not in WORK_ACTION_TYPES
    assert "public_working_memory_read" in WORK_ACTION_TYPES
    assert "public_working_memory_update" in WORK_ACTION_TYPES
    assert "work_item_done" in WORK_ACTION_TYPES
    assert "work_qa_verdict" in WORK_ACTION_TYPES
    assert "work_plan_update" in WORK_ACTION_TYPES
    assert "work_complete" in WORK_ACTION_TYPES
