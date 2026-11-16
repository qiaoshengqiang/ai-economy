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
from actions.intent import ActionResult
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
from work.prompt import (
    WORK_SYSTEM_PROMPT, build_cognition_prompt, build_lead_plan_prompt,
    build_work_prompt,
)
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
# 1.5 Work 动作参数适配
# ============================================================
class TestWorkActionAdapters:
    def test_read_offset_zero_is_normalized_to_one(self):
        from actions.workspace_read import WorkspaceReadExecutor
        from work.adapters import WorkWorkspaceReadExecutor

        seen = {}

        def read_fn(*_args, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "path": "a.js", "content": "x",
                    "revision": 1}

        adapter = WorkWorkspaceReadExecutor(
            WorkspaceReadExecutor(read_fn))
        intent = ActionIntent(
            action_type="workspace_read",
            parameters={"path": "a.js", "offset": 0, "limit": 10},
            agent_id="agent_a")
        result = adapter.execute(intent)

        assert result.success is True
        assert seen["offset"] == 1

    def test_missing_write_mode_defaults_to_create(self):
        from actions.workspace_write import WorkspaceWriteExecutor
        from work.adapters import WorkWorkspaceWriteExecutor

        seen = {}

        def write_fn(*args, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "path": "a.js", "mode": "create",
                    "revision": 1, "chars": 1}

        adapter = WorkWorkspaceWriteExecutor(
            WorkspaceWriteExecutor(write_fn))
        intent = ActionIntent(
            action_type="workspace_write",
            parameters={"path": "a.js", "content": "x"},
            agent_id="agent_a")
        result = adapter.execute(intent)

        assert result.success is True
        assert seen["write_mode"] == "create"

    def test_target_alias_is_accepted(self):
        from actions.workspace_write import WorkspaceWriteExecutor
        from work.adapters import WorkWorkspaceWriteExecutor

        seen = {}

        def write_fn(*args, **kwargs):
            seen["path"] = args[3]
            return {"ok": True, "path": "a.js", "mode": "create",
                    "revision": 1, "chars": 1}

        adapter = WorkWorkspaceWriteExecutor(
            WorkspaceWriteExecutor(write_fn))
        intent = ActionIntent(
            action_type="workspace_write",
            parameters={"target": "a.js", "content": "x",
                        "write_mode": "create"},
            agent_id="agent_a")
        result = adapter.execute(intent)

        assert result.success is True
        assert seen["path"] == "a.js"


def test_work_image_resolver_reads_run_screenshot(tmp_path):
    from types import SimpleNamespace
    from work.images import make_image_resolver

    run_root = tmp_path / "tool_runs"
    work = run_root / "run_1" / "work"
    work.mkdir(parents=True)
    (work / "shot.png").write_bytes(b"png-bytes")

    runtime = SimpleNamespace(
        tool_runner=SimpleNamespace(run_root=run_root))
    resolver = make_image_resolver(runtime)

    url = resolver("run_1", "shot.png")
    assert url is not None
    assert url.startswith("data:image/png;base64,")
    assert resolver("run_1", "missing.png") is None
    assert resolver("../run_1", "shot.png") is None


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


def _dispatcher(*, include_public_memory=True):
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
        include_public_memory=include_public_memory,
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
        assert "tool_help" in registered

    def test_every_registered_action_has_a_capability_summary(self):
        """动作清单从 dispatcher 读取；每个动作都必须有一句中性摘要。"""
        from work.capabilities import ACTION_SUMMARIES

        registered = set(_dispatcher().action_types)
        missing = registered - set(ACTION_SUMMARIES)
        assert not missing, f"缺少能力摘要：{sorted(missing)}"

    def test_every_registered_action_has_parameter_schema(self):
        from work.capabilities import ACTION_PARAMS

        registered = set(_dispatcher().action_types)
        missing = registered - set(ACTION_PARAMS)
        assert not missing, f"缺少参数说明：{sorted(missing)}"

    def test_action_params_render_only_requested_actions(self):
        from work.capabilities import describe_action_params

        solo = describe_action_params(
            {"workspace_write", "run_tool", "work_complete"})
        assert "workspace_write" in solo
        assert "work_complete" in solo
        assert "work_item_done" not in solo
        assert "public_working_memory_update" not in solo

    def test_solo_dispatcher_excludes_public_working_memory(self):
        registered = set(_dispatcher(
            include_public_memory=False).action_types)
        assert "public_working_memory_read" not in registered
        assert "public_working_memory_update" not in registered
        assert "workspace_read" in registered


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
            "inbox",
        ):
            assert banned not in prompt, banned
        assert "work_complete" in prompt
        assert "PUBLIC WORKING MEMORY" not in prompt
        assert "hello.txt exists" in prompt

    def test_prompt_separates_name_and_agent_id(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", agent_name="小艾",
            role=WorkRole.SOLO)
        prompt = build_work_prompt(
            context, task_description="写 hello.txt", runtime=None)

        assert "你的名字是：小艾" in prompt
        assert "你的 Agent ID 是：agent_a" in prompt
        assert "小艾（agent_a）" not in prompt

    def test_work_prompt_labels_human_task_and_front_notes(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO,
            brief={
                "goal": "把格子改成正方形",
                "human_text": "格子看起来有点变形",
                "task_text": "把格子改成正方形",
                "front_notes": "建议保持竖屏；这只是建议。",
            })
        prompt = build_work_prompt(
            context, task_description="把格子改成正方形", runtime=None)

        assert "Human 对 Front 的原话（不一定是需求）" in prompt
        assert "Front 给 Worker 的任务说明" in prompt
        assert "Front 的建议（非硬要求）" in prompt
        assert "格子看起来有点变形" in prompt
        assert "把格子改成正方形" in prompt
        assert "保持竖屏" in prompt

    def test_team_prompt_shows_participant_names_with_agent_ids(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", agent_name="小艾",
            agent_names={"agent_b": "小博", "agent_c": "小希"},
            role=WorkRole.LEAD)
        prompt = build_lead_plan_prompt(
            context,
            task_description="做一个网页",
            participants=["agent_b", "agent_c"],
        )

        assert "小博（Agent ID: agent_b）" in prompt
        assert "小希（Agent ID: agent_c）" in prompt
        assert "小博（agent_b）" not in prompt

    def test_prompt_falls_back_to_agent_id_when_name_missing(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO)
        prompt = build_work_prompt(
            context, task_description="写 hello.txt", runtime=None)

        assert "你的名字是：agent_a" in prompt
        assert "你的 Agent ID 是：agent_a" in prompt

    def test_cognition_prompt_includes_environment_and_tools(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO,
            workspace_id="WS-X",
            acceptance_criteria=["页面可打开"])
        prompt = build_cognition_prompt(
            context,
            task_description="做一个鹈鹕骑自行车网页",
            runtime=None,
            tool_catalog=[{
                "name": "playwright",
                "description": "浏览器测试",
            }],
            max_steps=2,
            available_actions={"workspace_read", "tool_help"},
            next_actions={"workspace_write", "workspace_edit", "run_tool",
                          "tool_help"},
        )

        assert "任务认知" in prompt
        assert "做一个鹈鹕骑自行车网页" in prompt
        assert "playwright" in prompt
        assert "workspace_read" in prompt
        assert "只读" in prompt
        assert "Work 阶段动作" in prompt
        assert "workspace_write" in prompt
        assert "Work 动作参数" in prompt
        assert '"write_mode"' in prompt
        assert "只支持公共工具库" in prompt
        assert "工具文档" in prompt
        assert "## 3. Technical approach" not in prompt
        assert '"actions":[{"type":"tool_help"' in prompt
        assert "普通 JSON 语法" in prompt
        assert "工具调用标记" in prompt

    def test_all_registered_solo_actions_are_visible(self):
        """能力清单来自 dispatcher：注册了什么，prompt 里就必须有什么。"""
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO)
        actions = _dispatcher(include_public_memory=False).action_types
        prompt = build_work_prompt(
            context, task_description="d", runtime=None,
            available_actions=actions)

        for name in actions:
            assert name in prompt, f"prompt 缺少已注册动作：{name}"

    def test_work_prompt_reinjects_tool_docs(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO)
        prompt = build_work_prompt(
            context, task_description="用 playwright 测试", runtime=None,
            tool_docs={"playwright": "SPEC-MUST-BE-*.spec.js"})

        assert "工具文档（已读取" in prompt
        assert "SPEC-MUST-BE-*.spec.js" in prompt
        assert "工具文档是权威来源" in prompt

    def test_prompt_exposes_thinking_but_not_budget_control(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO)
        prompt = build_work_prompt(
            context, task_description="做一个分模块游戏", runtime=None,
            last_turn={
                "status_before": "IN_PROGRESS",
                "status_after": "IN_PROGRESS",
                "action_types": ["workspace_write"],
                "tokens": 100,
                "truncated": True,
                "thinking": {"*": {"enabled": True, "effort": "high"}},
                "cognitive_budget": {
                    "requested": 65536,
                    "effective": 32768,
                    "clipped": "above_max",
                },
                "rewrites": {"index.html": 3},
                "summary_actions_ignored": ["workspace_write"],
                "round_summary": "ROUND-1 完整总结：已写 config.js。",
                "action_feedback": [
                    {"action": "workspace_edit",
                     "target": "app/service.py", "ok": False,
                     "executed": True, "code": "old_string_not_found",
                     "error": "old_string 在文件里没有找到"},
                    {"action": "workspace_write",
                     "target": "js/game.js", "ok": True,
                     "executed": True, "code": "", "error": ""},
                ],
            })

        assert "decision" in prompt and "work" in prompt
        assert "thinking" in prompt
        assert "low / high / max" in prompt
        assert "cognitive_budget" not in prompt
        assert "65536" not in prompt
        assert "输出预算：effective=32768 tokens（Work 固定）" in prompt
        assert "index.html x 3" in prompt
        assert "workspace_edit" in prompt
        assert "该输出中的动作没有执行" in prompt
        assert '"actions"' in prompt
        assert "动作按顺序执行" in prompt
        assert "summary 阶段返回的动作被忽略" in prompt
        assert "workspace_write" in prompt
        assert "上一轮总结（完整）" in prompt
        assert "ROUND-1 完整总结：已写 config.js。" in prompt
        assert "上一轮动作结果" in prompt
        assert "workspace_edit app/service.py：失败" in prompt
        assert "old_string_not_found" in prompt

    def test_prompt_has_work_actions(self):
        context = WorkContext(
            task_id="T-1", agent_id="agent_a", role=WorkRole.SOLO)
        prompt = build_work_prompt(
            context, task_description="d", runtime=None,
            available_actions=_dispatcher(
                include_public_memory=False).action_types)
        for action in ("workspace_read", "workspace_edit", "run_tool",
                       "tool_help", "draft_write",
                       "private_working_memory_update"):
            assert action in prompt
        assert "public_working_memory_update" not in prompt
        assert "工具文档是权威来源" in prompt

    def test_worker_prompt_includes_item_notes_and_public_memory(self):
        from work.plan import WorkItem
        from work.prompt import build_team_worker_prompt

        context = WorkContext(
            task_id="T-1", agent_id="agent_b", role=WorkRole.MEMBER,
            public_memory_facts="[plan] contract: use Game.CONFIG")
        item = WorkItem(
            item_id="item_1", agent_id="agent_b", goal="写 player.js",
            notes="接口契约：Game.Player.update(dt, input)")
        prompt = build_team_worker_prompt(
            context,
            task_description="打飞机游戏",
            items=[item],
            item_state={},
            runtime=None,
        )
        assert "Game.Player.update(dt, input)" in prompt
        assert "Game.CONFIG" in prompt
        assert "公共工作记忆里已有契约时，直接以它为团队契约" in prompt


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
        self.last_kwargs = {}

    def act(self, *, messages, dispatcher, **kwargs):
        self.last_kwargs = dict(kwargs)
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
            tokens=11, content="done", is_truncated=False,
            model_reasoning="MODEL-THINK",
            latency_ms=42.5,
            action_results=[action_result])
        return SimpleNamespace(
            segments=[segment],
            action_intents=[intent],
            action_results=[action_result],
            summary="solo 总结",
        )


class _SoloRuntime:
    def __init__(self, db, task):
        self.tasks = TaskStore(db)
        self.tasks.save(task)
        self.agent = _FakeAgent()
        self.economy = SimpleNamespace(agent=lambda aid: self.agent)
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


class _NoActionThenCompleteAgent(_FakeAgent):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def act(self, *, messages, dispatcher, **kwargs):
        self.calls += 1
        if self.calls < 3:
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    tokens=3, content="我先总结一下",
                    is_truncated=False)],
                action_intents=[], action_results=[])
        intent = ActionIntent(
            action_type="work_complete",
            parameters={"result_ref": "recovered-v1",
                        "summary": "恢复后完成"},
            agent_id="agent_a")
        action_result = dispatcher.dispatch(intent)
        return SimpleNamespace(
            segments=[SimpleNamespace(
                tokens=3, content="done", is_truncated=False)],
            action_intents=[intent], action_results=[action_result])


class _CognitionBrain:
    def __init__(self):
        self.thinking_calls = []

    def set_thinking(self, enabled, effort=None, *, phase="work"):
        self.thinking_calls.append((enabled, effort, phase))


class _CognitionAgent(_FakeAgent):
    def __init__(self):
        super().__init__()
        self.brain = _CognitionBrain()
        self.prompts = []
        self.kwargs_log = []

    def act(self, *, messages, dispatcher, **kwargs):
        self.prompts.append(messages)
        self.kwargs_log.append(dict(kwargs))
        if kwargs.get("initial_phase") == "cognition":
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    tokens=5,
                    content="TASK-COGNITION-MARKER\n## 任务认知\n环境与方案",
                    is_truncated=False,
                )],
                action_intents=[],
                action_results=[],
            )
        return super().act(
            messages=messages, dispatcher=dispatcher, **kwargs)


class _SeenReadsAgent(_FakeAgent):
    """第一轮“读过”一个文件；用来确认第二轮拿到的是**新的**去重集合。"""

    def __init__(self):
        super().__init__()
        self.seen_sets = []
        self.work_turns = 0

    def act(self, *, messages, dispatcher, **kwargs):
        if kwargs.get("initial_phase") == "cognition":
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    tokens=1, content="cognition report",
                    is_truncated=False)],
                action_intents=[], action_results=[])
        self.work_turns += 1
        seen = kwargs.get("seen_reads")
        self.seen_sets.append(seen)
        if self.work_turns == 1:
            seen.add(("js/app.js", 1, 1, 40))
            intent = ActionIntent(
                action_type="workspace_read",
                parameters={"path": "js/app.js"}, agent_id="agent_a")
            result = ActionResult(
                success=True,
                output={"path": "js/app.js", "content": "console.log(1)",
                        "revision": 1, "chars": 13},
                action_id=intent.intent_id,
                action_type=intent.action_type)
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    tokens=1, content="read", is_truncated=False)],
                action_intents=[intent], action_results=[result])
        return super().act(
            messages=messages, dispatcher=dispatcher, **kwargs)


class _ToolDocAgent(_FakeAgent):
    """认知阶段查一次工具文档；后续 Work 轮必须还能看到这份文档原文。"""

    def __init__(self):
        super().__init__()
        self.prompts = []
        self.cognition_calls = 0

    def act(self, *, messages, dispatcher, **kwargs):
        self.prompts.append(messages)
        if kwargs.get("initial_phase") == "cognition":
            self.cognition_calls += 1
            if self.cognition_calls == 1:
                intent = ActionIntent(
                    action_type="tool_help",
                    parameters={"tool": "playwright"},
                    agent_id="agent_a")
                result = ActionResult(
                    success=True,
                    output={
                        "tool": "playwright",
                        "content": "SPEC-MUST-BE-*.spec.js\n"
                                   "USE process.env.BASE_URL",
                    },
                    action_id=intent.intent_id,
                    action_type="tool_help")
                return SimpleNamespace(
                    segments=[SimpleNamespace(
                        tokens=2, content='{"decision":"act"}',
                        is_truncated=False)],
                    action_intents=[intent], action_results=[result])
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    tokens=1, content="COGNITION-REPORT",
                    is_truncated=False)],
                action_intents=[], action_results=[])
        return super().act(
            messages=messages, dispatcher=dispatcher, **kwargs)


class _MalformedCognitionAgent(_FakeAgent):
    """第一次认知输出写坏的信封；第二次才给纯文本报告。"""

    def __init__(self, first_content=None):
        super().__init__()
        self.prompts = []
        self.cognition_calls = 0
        self.first_content = first_content or (
            '```json\n{"actions":[{"action":"tool_help",'
            '"tool":"playwright"}]}\n```')

    def act(self, *, messages, dispatcher, **kwargs):
        self.prompts.append(messages)
        if kwargs.get("initial_phase") == "cognition":
            self.cognition_calls += 1
            if self.cognition_calls == 1:
                return SimpleNamespace(
                    segments=[SimpleNamespace(
                        tokens=1, content=self.first_content,
                        is_truncated=False)],
                    action_intents=[], action_results=[])
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    tokens=1, content="COGNITION-REPORT-TEXT",
                    is_truncated=False)],
                action_intents=[], action_results=[])
        return super().act(
            messages=messages, dispatcher=dispatcher, **kwargs)


class _CountingCompleteAgent(_CognitionAgent):
    def __init__(self):
        super().__init__()
        self.work_calls = 0

    def act(self, *, messages, dispatcher, **kwargs):
        if kwargs.get("initial_phase") != "cognition":
            self.work_calls += 1
        return super().act(
            messages=messages, dispatcher=dispatcher, **kwargs)


class TestSoloWorkRuntime:
    def test_completed_task_stops_loop_immediately(self, db):
        task = Task.create(
            task_id="T-STOP-AFTER-COMPLETE", creator="agent_a",
            description="写 hello.txt", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        agent = _CountingCompleteAgent()
        runtime.agent = agent
        result = SoloWorkRuntime(
            runtime, task, max_turns=10).run()

        assert result.ok is True, result.error
        assert result.status == TaskStatus.COMPLETED
        # work_complete 后必须立刻停止；不能继续跑到 max_turns。
        assert agent.work_calls == 1
        assert runtime.tasks.get(task.task_id).status == TaskStatus.COMPLETED

    def test_cognition_report_is_injected_into_work_prompt(self, db):
        task = Task.create(
            task_id="T-COGNITION", creator="agent_a",
            description="写 hello.txt", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        rounds = []
        runtime = _SoloRuntime(db, task)
        runtime.agent = _CognitionAgent()
        result = SoloWorkRuntime(
            runtime, task, max_turns=1,
            round_sink=rounds.append).run()

        assert result.ok is True, result.error
        assert runtime.agent.brain.thinking_calls == [
            (True, "max", "cognition")]
        assert rounds[0]["cognition"] is True
        assert "TASK-COGNITION-MARKER" in rounds[0]["summary"]
        work_prompt = str(runtime.agent.prompts[-1])
        assert "TASK-COGNITION-MARKER" in work_prompt

    def test_runtime_recovers_from_two_no_action_turns(self, db):
        task = Task.create(
            task_id="T-RECOVER", creator="agent_a",
            description="写 hello.txt", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        runtime.agent = _NoActionThenCompleteAgent()
        result = SoloWorkRuntime(runtime, task, max_turns=4).run()

        assert result.ok is True, result.error
        assert result.status == TaskStatus.COMPLETED
        assert runtime.agent.calls == 3
        assert runtime.tasks.get(task.task_id).status == TaskStatus.COMPLETED

    def test_malformed_action_json_is_not_used_as_cognition_report(self, db):
        """
        🔴 认知阶段把 `{"actions":[...]}` 当成报告，会让这份 JSON 注入
        后续每一轮 prompt，认知阶段等于白跑（实测发生过）。坏信封必须
        触发一次重问，只接受纯文本报告。
        """
        task = Task.create(
            task_id="T-COGNITION-REPORT", creator="agent_a",
            description="用 playwright 验证", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        rounds = []
        runtime = _SoloRuntime(db, task)
        runtime.agent = _MalformedCognitionAgent()
        result = SoloWorkRuntime(
            runtime, task, max_turns=1,
            round_sink=rounds.append).run()

        assert result.ok is True, result.error
        assert runtime.agent.cognition_calls == 2
        assert rounds[0]["summary"] == "COGNITION-REPORT-TEXT"
        assert "不是合法的动作信封" in str(
            runtime.agent.prompts[1][-1])

    def test_provider_tool_call_markup_is_not_used_as_report(self, db):
        """
        🔴 DeepSeek 有时把原生 tool-call 标记写进 content
        （`<｜｜DSML｜｜ invoke name=...>`）。它不是报告，也不是合法信封；
        当成报告会让认知阶段白跑。
        """
        task = Task.create(
            task_id="T-COGNITION-DSML", creator="agent_a",
            description="用 playwright 验证", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        rounds = []
        runtime = _SoloRuntime(db, task)
        runtime.agent = _MalformedCognitionAgent(
            first_content=(
                '<｜｜DSML｜｜ calls>\n'
                '<｜｜DSML｜｜ invoke name="tool_help">\n'
                '<｜｜DSML｜｜ parameter name="tool" string="true">'
                'playwright</｜｜DSML｜｜ parameter>\n'))
        result = SoloWorkRuntime(
            runtime, task, max_turns=1,
            round_sink=rounds.append).run()

        assert result.ok is True, result.error
        assert runtime.agent.cognition_calls == 2
        assert rounds[0]["summary"] == "COGNITION-REPORT-TEXT"
        assert "工具调用标记" in str(runtime.agent.prompts[1][-1])

    def test_tool_docs_read_in_cognition_survive_into_work_prompt(self, db):
        """
        🔴 每轮 prompt 从零重建：认知里查到的工具文档如果只留在那一轮，
        下一轮就只剩「我记得读过」。文档原文必须每轮重新注入。
        """
        task = Task.create(
            task_id="T-TOOL-DOCS", creator="agent_a",
            description="用 playwright 验证页面", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        runtime.agent = _ToolDocAgent()
        result = SoloWorkRuntime(runtime, task, max_turns=1).run()

        assert result.ok is True, result.error
        work_prompt = str(runtime.agent.prompts[-1])
        assert "工具文档（已读取" in work_prompt
        assert "SPEC-MUST-BE-*.spec.js" in work_prompt
        assert "USE process.env.BASE_URL" in work_prompt

    def test_work_calls_do_not_get_agent_identity_or_economy(self, db):
        """
        🔴 Work 的 system 层不能有 Agent 身份 / 经济 / Constitution。
        经济事实是 `main.build_runtime` 注入 `Brain` 的 Cognitive Context，
        由 `cognitive=False` 关掉；system prompt 用 Work 专用文本。
        """
        task = Task.create(
            task_id="T-WORK-SYSTEM", creator="agent_a",
            description="写 hello.txt", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        runtime.agent = _CognitionAgent()
        result = SoloWorkRuntime(
            runtime, task, max_turns=1).run()

        assert result.ok is True, result.error
        assert runtime.agent.kwargs_log
        for kwargs in runtime.agent.kwargs_log:
            assert kwargs.get("cognitive") is False
            assert kwargs.get("system") == WORK_SYSTEM_PROMPT
            assert kwargs.get("requested_cognitive_budget") == 65536
            assert kwargs.get("apply_cognitive_budget") is False
        work_calls = [
            item for item in runtime.agent.kwargs_log
            if item.get("initial_phase") != "cognition"]
        assert work_calls
        assert all(item.get("max_work_steps") == 4 for item in work_calls)
        assert "AIC" not in WORK_SYSTEM_PROMPT
        assert "Agent B" not in WORK_SYSTEM_PROMPT

    def test_resume_context_is_injected_into_cognition_and_first_work(self, db):
        task = Task.create(
            task_id="T-RESUME-CTX", creator="agent_a",
            description="继续", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        runtime.agent = _CognitionAgent()
        result = SoloWorkRuntime(
            runtime, task, max_turns=1,
            resume_context="CHECKPOINT-MARKER").run()

        assert result.ok is True, result.error
        assert "上一次运行检查点" in str(runtime.agent.prompts[0])
        assert "CHECKPOINT-MARKER" in str(runtime.agent.prompts[0])
        assert "CHECKPOINT-MARKER" in str(runtime.agent.prompts[-1])

    def test_each_turn_starts_with_a_fresh_read_dedupe_set(self, db):
        """
        🔴 每一轮的 prompt 都是**从零重建**的：上一轮回喂过的正文不在
        上下文里。跨轮沿用去重指纹，会让模型重读同一版本文件时只得到
        「内容未重复发送」—— 它永远看不到正文（实测里连续 4 轮耗在这）。
        """
        task = Task.create(
            task_id="T-SEEN-READS", creator="agent_a",
            description="读 js/app.js 再完成", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        runtime.agent = _SeenReadsAgent()
        result = SoloWorkRuntime(runtime, task, max_turns=3).run()

        assert result.ok is True, result.error
        assert len(runtime.agent.seen_sets) == 2
        assert runtime.agent.seen_sets[0] is not runtime.agent.seen_sets[1]
        assert len(runtime.agent.seen_sets[1]) == 0

    def test_runtime_emits_round_payload_with_summary(self, db):
        task = Task.create(
            task_id="T-ROUND", creator="agent_a",
            description="写 hello.txt", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        rounds = []
        runtime = _SoloRuntime(db, task)
        result = SoloWorkRuntime(
            runtime, task, max_turns=1,
            round_sink=rounds.append).run()

        assert result.ok is True, result.error
        assert len(rounds) == 2
        assert rounds[0]["cognition"] is True
        assert rounds[0]["run_id"].startswith("run_")
        assert rounds[0]["iteration"] == 1
        assert rounds[1]["run_id"] == rounds[0]["run_id"]
        assert rounds[1]["round"] == 1
        assert rounds[1]["summary"] == "solo 总结"
        assert rounds[1]["segments"][0]["content"] == "done"
        assert rounds[1]["segments"][0]["model_reasoning"] == "MODEL-THINK"
        assert rounds[1]["segments"][0]["latency_ms"] == 42.5
        assert rounds[1]["latency_ms"] == 42.5
        # 动作结果必须进轮次日志，否则详情页全部显示「未执行」
        results = rounds[1]["segments"][0]["results"]
        assert results and results[0]["success"] is True
        assert results[0]["action_type"] == "work_complete"

    def test_runtime_is_decision_first_and_has_vision(self, db):
        task = Task.create(
            task_id="T-DECISION", creator="agent_a",
            description="写 hello.txt", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        result = SoloWorkRuntime(runtime, task, max_turns=1).run()

        assert result.ok is True, result.error
        assert runtime.agent.last_kwargs["initial_phase"] == "decision"
        assert callable(runtime.agent.last_kwargs["image_resolver"])

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


def test_replace_missing_file_suggests_create():
    from actions.workspace_write import WorkspaceWriteExecutor
    from work.adapters import WorkWorkspaceWriteExecutor

    executor = WorkWorkspaceWriteExecutor(WorkspaceWriteExecutor(
        lambda *_args, **_kwargs: {
            "ok": False, "code": "not_found",
            "message": "路径不存在：/x.md",
        }))
    result = executor.execute(ActionIntent(
        action_type="workspace_write",
        parameters={"path": "x.md", "content": "x",
                    "write_mode": "replace"},
        agent_id="agent_a"))

    assert result.success is False
    assert result.metadata["code"] == "not_found"
    assert "write_mode='create'" in result.error


class _SummaryActionAgent(_FakeAgent):
    """第一轮 summary 段输出动作（会被忽略），第二轮完成。"""

    def __init__(self):
        super().__init__()
        self.turn = 0
        self.prompts = []

    def act(self, *, messages, dispatcher, **kwargs):
        self.prompts.append(messages)
        if kwargs.get("initial_phase") == "cognition":
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    phase="cognition", tokens=1, content="report",
                    is_truncated=False, decision=None,
                    action_intents=[], action_results=[])],
                action_intents=[], action_results=[], summary="report")
        self.turn += 1
        if self.turn == 1:
            intent = ActionIntent(
                action_type="workspace_write",
                parameters={"path": "x.md", "content": "x"},
                agent_id="agent_a")
            segment = SimpleNamespace(
                phase="summary", tokens=2, content="summary text",
                is_truncated=False, decision=None,
                action_intents=[intent], action_results=[],
                tool_calls=[], tool_calls_source="none")
            return SimpleNamespace(
                segments=[segment], action_intents=[], action_results=[],
                summary="summary text")
        return super().act(
            messages=messages, dispatcher=dispatcher, **kwargs)


def test_summary_actions_are_reported_to_next_turn(db):
    task = Task.create(
        task_id="T-SUMMARY-IGNORED", creator="agent_a",
        description="写 hello.txt", workspace_id="WS-X",
        participants=["agent_a"], mode=TaskMode.SOLO)
    task.claim_producer("agent_a")

    runtime = _SoloRuntime(db, task)
    runtime.agent = _SummaryActionAgent()
    result = SoloWorkRuntime(runtime, task, max_turns=2).run()

    assert result.ok is True, result.error
    next_prompt = str(runtime.agent.prompts[-1])
    assert "summary 阶段返回的动作被忽略" in next_prompt
    assert "workspace_write" in next_prompt


class _ReadOnlyAgent(_FakeAgent):
    """每一轮都只读：用来验证「连续只读」是事实并且会中断 run。"""

    def __init__(self):
        super().__init__()
        self.prompts = []
        self.calls = 0

    def act(self, *, messages, dispatcher, **kwargs):
        if kwargs.get("initial_phase") == "cognition":
            return SimpleNamespace(
                segments=[SimpleNamespace(
                    tokens=1, content="cognition report",
                    is_truncated=False)],
                action_intents=[], action_results=[])
        self.calls += 1
        self.prompts.append(messages)
        intent = ActionIntent(
            action_type="workspace_read",
            parameters={"path": "src/app.js"}, agent_id="agent_a")
        result = ActionResult(
            success=False, error="not found",
            action_id=intent.intent_id,
            action_type=intent.action_type)
        return SimpleNamespace(
            segments=[SimpleNamespace(
                tokens=1, content="read again", is_truncated=False)],
            action_intents=[intent], action_results=[result])


class TestReadOnlyProgressGuard:
    def test_read_only_streak_is_reported_then_interrupts_run(self, db):
        task = Task.create(
            task_id="T-READ-GUARD", creator="agent_a",
            description="修页面", workspace_id="WS-X",
            participants=["agent_a"], mode=TaskMode.SOLO)
        task.claim_producer("agent_a")

        runtime = _SoloRuntime(db, task)
        runtime.agent = _ReadOnlyAgent()
        result = SoloWorkRuntime(
            runtime, task, max_turns=10,
            max_read_only_turns=3).run()

        assert result.ok is False
        assert "RUN_INTERRUPTED" in result.error
        assert "连续 3 轮只读" in result.error
        assert result.turns == 3
        # 第 2 轮开始，prompt 里应出现「连续只读」的事实。
        assert "最近 2 轮只读" in str(
            runtime.agent.prompts[-1])


def test_work_prompt_renders_front_supplied_files():
    context = WorkContext(
        task_id="T-FILES", agent_id="agent_a", role=WorkRole.SOLO,
        brief={"goal": "落盘 Front 给的文件", "files": [
            {"path": "index.html", "content": "FRONT-CODE"},
        ]})
    prompt = build_work_prompt(
        context, task_description="写 index.html", runtime=None)

    assert "Front 提供的文件" in prompt
    assert "FRONT-CODE" in prompt


def test_solo_read_only_guard_default_is_eight():
    import inspect

    from work.runtime import SoloWorkRuntime

    default = inspect.signature(
        SoloWorkRuntime.__init__).parameters[
            "max_read_only_turns"].default
    assert default == 8
