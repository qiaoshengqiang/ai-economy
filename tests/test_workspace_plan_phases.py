"""
Workspace Plan 的阶段 / 依赖 / 检查点
=====================================

这是**通用**机制：checkpoint 只是 Creator 写的自由文本目标；
Agent 自己选择怎么检查（run_tool / read / 其他），系统不硬编码
pytest / playwright / DOM 检查。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.intent import ActionIntent                        # noqa: E402
from actions.workspace_plan_checkpoint import (                # noqa: E402
    ACTION_WORKSPACE_PLAN_CHECKPOINT,
    WorkspacePlanCheckpointExecutor,
)
from demand.task_creation import (                             # noqa: E402
    TaskCreationError,
    TaskCreationService,
)


class _Gateway:
    def __init__(self, files):
        self.files = {"/" + path.lstrip("/"): content
                      for path, content in files.items()}

    def read(self, workspace_id, path, agent_id):
        rel = "/" + str(path or "").lstrip("/")
        if rel in self.files:
            return SimpleNamespace(
                success=True,
                result={"path": rel, "revision": 1,
                        "content": self.files[rel]})
        return SimpleNamespace(success=False, result=None,
                               error={"message": "not found"})


def _service():
    return TaskCreationService(None, None)


def _people():
    return ["agent_a", "agent_c"]


def test_phases_and_placeholders_are_validated_and_normalized():
    plan = _service().validate_workspace_plan({
        "root_owner": "agent_c",
        "directories": {"src": "agent_c"},
        "files": {
            "index.html": "agent_c",
            "src/main.js": "agent_c",
            "src/world.js": "agent_c",
        },
        "phases": [
            {"name": "skeleton", "files": ["index.html", "src/main.js"],
             "checkpoint": "page can load"},
            {"name": "world", "depends_on": ["skeleton"],
             "files": ["src/world.js"], "checkpoint": "world renders"},
        ],
        "placeholders": ["src/world.js"],
    }, people=_people(), producer="agent_c")
    assert plan["phases"][0]["name"] == "skeleton"
    assert plan["phases"][1]["depends_on"] == ["skeleton"]
    assert plan["placeholders"] == ["src/world.js"]


def test_phase_references_unknown_file_is_rejected():
    with pytest.raises(TaskCreationError, match="未在.*files"):
        _service().validate_workspace_plan({
            "root_owner": "agent_c",
            "files": {"index.html": "agent_c"},
            "phases": [{"name": "s", "files": ["src/main.js"]}],
        }, people=_people(), producer="agent_c")


def test_phase_dependency_cycle_is_rejected():
    with pytest.raises(TaskCreationError, match="环"):
        _service().validate_workspace_plan({
            "root_owner": "agent_c",
            "files": {"index.html": "agent_c"},
            "phases": [
                {"name": "a", "depends_on": ["b"]},
                {"name": "b", "depends_on": ["a"]},
            ],
        }, people=_people(), producer="agent_c")


def test_placeholder_must_be_a_declared_file():
    with pytest.raises(TaskCreationError, match="placeholders"):
        _service().validate_workspace_plan({
            "root_owner": "agent_c",
            "files": {"index.html": "agent_c"},
            "placeholders": ["src/main.js"],
        }, people=_people(), producer="agent_c")


def test_checkpoint_executor_requires_phase_and_bool_passed():
    called = []

    def record(agent_id, **kwargs):
        called.append((agent_id, kwargs))
        return {"ok": True, "checkpoint": kwargs}

    ex = WorkspacePlanCheckpointExecutor(record)
    missing = ex.execute(ActionIntent(
        action_type=ACTION_WORKSPACE_PLAN_CHECKPOINT,
        parameters={"passed": True}, agent_id="agent_a"))
    assert missing.success is False
    assert missing.metadata["code"] == "missing_phase"

    bad = ex.execute(ActionIntent(
        action_type=ACTION_WORKSPACE_PLAN_CHECKPOINT,
        parameters={"phase": "skeleton", "passed": "yes"},
        agent_id="agent_a"))
    assert bad.success is False
    assert bad.metadata["code"] == "invalid_passed"
    assert called == []


def _checkpoint_runner(checkpoints=None):
    task = SimpleNamespace(
        is_participant=lambda agent_id: agent_id in ("agent_a", "agent_c"),
        workspace=SimpleNamespace(workspace_id="WS"),
        participants=["agent_a", "agent_c"],
        creator="agent_a", contractor="agent_c",
    )
    run = SimpleNamespace(
        workspace_plan={
            "root_owner": "agent_c",
            "files": {"index.html": "agent_c"},
            "phases": [{"name": "skeleton", "depends_on": [],
                        "files": ["index.html"],
                        "checkpoint": "page loads"}],
        },
        plan_checkpoints=checkpoints,
    )
    events = []
    fake = SimpleNamespace(
        _status_task=lambda: task,
        run=run,
        rt=SimpleNamespace(workspace_gateway=_Gateway(
            {"index.html": "<html></html>"})),
        _write_run=lambda: None,
        _event=lambda *a, **k: events.append((a, k)),
    )
    return fake, run, events


def test_runner_records_checkpoint_and_validates_evidence():
    from demand.runner import DemandRunner

    fake, run, events = _checkpoint_runner()
    bad = DemandRunner._record_plan_checkpoint(
        fake, "agent_a", phase="ghost", passed=True)
    assert bad["ok"] is False
    assert bad["code"] == "unknown_phase"

    missing = DemandRunner._record_plan_checkpoint(
        fake, "agent_a", phase="skeleton", passed=True,
        evidence={"path": "missing.js", "revision": 1})
    assert missing["ok"] is False
    assert missing["code"] == "evidence_not_found"

    ok = DemandRunner._record_plan_checkpoint(
        fake, "agent_a", phase="skeleton", passed=True,
        evidence={"path": "index.html", "revision": 1},
        note="read + smoke")
    assert ok["ok"] is True
    assert run.plan_checkpoints["skeleton"]["passed"] is True
    assert run.plan_checkpoints["skeleton"]["by"] == "agent_a"
    assert events and events[0][0] == ("workspace_plan_checkpoint",)


def test_plan_update_clears_changed_or_removed_phase_checkpoints():
    from demand.runner import DemandRunner

    task = SimpleNamespace(
        creator="agent_a", contractor="agent_c",
        participants=["agent_a", "agent_c"],
    )
    old_phases = [
        {"name": "skeleton", "depends_on": [], "files": [],
         "checkpoint": "page loads"},
        {"name": "world", "depends_on": ["skeleton"], "files": [],
         "checkpoint": "world renders"},
    ]
    run = SimpleNamespace(
        workspace_plan={
            "root_owner": "agent_c", "directories": {},
            "files": {"index.html": "agent_c"},
            "phases": old_phases,
        },
        plan_checkpoints={
            "skeleton": {"passed": True},
            "world": {"passed": True},
        },
    )
    fake = SimpleNamespace(
        _status_task=lambda: task,
        _creation=_service(),
        run=run,
        _write_run=lambda: None,
        _event=lambda *a, **k: None,
    )
    new_plan = {
        "root_owner": "agent_c", "directories": {},
        "files": {"index.html": "agent_c"},
        "phases": [
            old_phases[0],
            {"name": "world", "depends_on": ["skeleton"], "files": [],
             "checkpoint": "world renders differently"},
        ],
    }
    result = DemandRunner._update_workspace_plan(
        fake, "agent_a", plan=new_plan)
    assert result["ok"] is True
    assert "skeleton" in run.plan_checkpoints
    assert "world" not in run.plan_checkpoints


def test_dispatcher_registers_checkpoint_only_with_callback():
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
    assert "workspace_plan_checkpoint" not in without.action_types
    with_cb = build_task_dispatcher(
        runtime, task=task, bridge=SimpleNamespace(),
        record_checkpoint=lambda *a, **k: {"ok": True})
    assert "workspace_plan_checkpoint" in with_cb.action_types
