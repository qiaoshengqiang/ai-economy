"""
Workspace Plan 更新 —— Creator 在运行中修正交付范围
==================================================

真实 run `demand_cc61c0b5`：Creator 把交付从“index.html + src/*”
缩减为“index.html + README”，但 `run.workspace_plan` 没更新，
“计划完成度”每轮仍显示 `src/* ❌ missing`。

这里验证：

    ① `workspace_plan_update` executor 的结构化结果；
    ② 未知参数 / 非对象 plan 明确失败；
    ③ 只有 creator 能更新；
    ④ 复用 task_create 的同一套 owner / 父目录校验；
    ⑤ dispatcher 只在组合根提供 update_plan 时注册该动作。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.intent import ActionIntent                        # noqa: E402
from actions.workspace_plan import (                           # noqa: E402
    ACTION_WORKSPACE_PLAN_UPDATE,
    WorkspacePlanUpdateExecutor,
)
from demand.task_creation import TaskCreationService            # noqa: E402


def _intent(plan, **extra):
    params = {"plan": plan}
    params.update(extra)
    return ActionIntent(
        action_type=ACTION_WORKSPACE_PLAN_UPDATE,
        parameters=params, agent_id="agent_a")


def test_executor_passes_plan_and_reason_to_composition_root():
    seen = {}

    def update(agent_id, *, plan, reason):
        seen.update(agent_id=agent_id, plan=plan, reason=reason)
        return {"ok": True, "workspace_plan": plan}

    plan = {"root_owner": "agent_c", "directories": {},
            "files": {"index.html": "agent_c"}}
    result = WorkspacePlanUpdateExecutor(update).execute(
        _intent(plan, reason="缩成单文件"))

    assert result.success is True
    assert result.output["workspace_plan"] == plan
    assert seen["agent_id"] == "agent_a"
    assert seen["reason"] == "缩成单文件"


def test_executor_rejects_unknown_params_and_non_object_plan():
    ex = WorkspacePlanUpdateExecutor(lambda *a, **k: {"ok": True})
    unknown = ex.execute(ActionIntent(
        action_type=ACTION_WORKSPACE_PLAN_UPDATE,
        parameters={"plan": {}, "board": "x"}, agent_id="agent_a"))
    assert unknown.success is False
    assert unknown.metadata["code"] == "unknown_parameters"

    bad = ex.execute(ActionIntent(
        action_type=ACTION_WORKSPACE_PLAN_UPDATE,
        parameters={"plan": "not-a-dict"}, agent_id="agent_a"))
    assert bad.success is False
    assert bad.metadata["code"] == "missing_plan"


def test_executor_surfaces_callback_failure():
    def update(agent_id, *, plan, reason):
        return {"ok": False, "code": "not_creator",
                "message": "只有 creator 可以更新"}

    result = WorkspacePlanUpdateExecutor(update).execute(_intent({}))
    assert result.success is False
    assert result.metadata["code"] == "not_creator"
    assert "只有 creator" in result.error


def test_validator_reuses_owner_and_parent_rules():
    service = TaskCreationService(None, None)
    people = ["agent_a", "agent_c"]
    plan = {
        "root_owner": "agent_c",
        "directories": {},
        "files": {"index.html": "agent_c", "README.md": "agent_c"},
    }
    validated = service.validate_workspace_plan(
        plan, people=people, producer="agent_c")
    assert validated["files"] == {
        "README.md": "agent_c", "index.html": "agent_c"}

    # 根级文件 owner 必须是 root_owner
    try:
        service.validate_workspace_plan(
            {"root_owner": "agent_c", "directories": {},
             "files": {"index.html": "agent_a"}},
            people=people, producer="agent_c")
    except Exception as exc:                                  # noqa: BLE001
        assert "root_owner" in str(exc)
    else:
        raise AssertionError("根级文件 owner 不一致必须失败")

    # 文件父目录必须已声明
    try:
        service.validate_workspace_plan(
            {"root_owner": "agent_c", "directories": {},
             "files": {"src/main.js": "agent_c"}},
            people=people, producer="agent_c")
    except Exception as exc:                                  # noqa: BLE001
        assert "父目录" in str(exc)
    else:
        raise AssertionError("未声明父目录必须失败")


def test_runner_update_plan_is_creator_only():
    from demand.runner import DemandRunner

    task = SimpleNamespace(
        creator="agent_a", contractor="agent_c",
        participants=["agent_a", "agent_c"],
    )
    run = SimpleNamespace(workspace_plan=None)
    events = []
    fake = SimpleNamespace(
        _status_task=lambda: task,
        _creation=TaskCreationService(None, None),
        run=run,
        _write_run=lambda: None,
        _event=lambda *a, **k: events.append((a, k)),
    )

    plan = {"root_owner": "agent_c", "directories": {},
            "files": {"index.html": "agent_c"}}
    denied = DemandRunner._update_workspace_plan(
        fake, "agent_c", plan=plan)
    assert denied["ok"] is False
    assert denied["code"] == "not_creator"

    ok = DemandRunner._update_workspace_plan(
        fake, "agent_a", plan=plan, reason="缩成单文件")
    assert ok["ok"] is True
    assert run.workspace_plan == plan
    assert events and events[0][0] == ("workspace_plan_updated",)


def test_task_dispatcher_registers_plan_update_only_with_callback():
    from demand.actions import build_task_dispatcher
    from core.task import Task

    task = Task.create(
        creator="agent_a", description="d",
        workspace_id="WS-PLAN", task_id="t_plan",
        participants=["agent_a"])
    runtime = SimpleNamespace(
        workspace_gateway=object(), communication=object())

    without = build_task_dispatcher(
        runtime, task=task, bridge=SimpleNamespace())
    assert "workspace_plan_update" not in without.action_types

    with_cb = build_task_dispatcher(
        runtime, task=task, bridge=SimpleNamespace(),
        update_plan=lambda *a, **k: {"ok": True})
    assert "workspace_plan_update" in with_cb.action_types
