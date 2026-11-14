"""Task 卡片归档 / 删除。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef
from core.task_store import TaskStore
from work.controller import AIEOSController
from work.lifecycle import WorkLifecycle
from storage.database import Database


class _Directory:
    def all(self):
        return [SimpleNamespace(agent_id="agent_a", available=True)]


def _controller(db, tmp_path):
    store = TaskStore(db)
    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=store,
    )
    controller = AIEOSController(
        runtime,
        front_agent="agent_a",
        task_root=tmp_path / "tasks",
        settings_path=tmp_path / "ui_settings.json",
    )
    return controller, store


def _completed_task(store, task_id="task_done"):
    task = Task.create(
        task_id=task_id,
        creator="agent_a",
        description="已完成任务",
        participants=["agent_a"],
        mode=TaskMode.SOLO,
    )
    store.save(task)
    lifecycle = WorkLifecycle(store)
    lifecycle.start(task, actor="agent_a")
    task = store.get(task_id)
    lifecycle.complete_solo(
        task, actor="agent_a",
        result=TaskResultRef(result_ref="done", summary="done"))
    return store.get(task_id)


def test_archive_hides_from_default_filter(db, tmp_path):
    controller, store = _controller(db, tmp_path)
    task = _completed_task(store)
    try:
        result = controller.archive_task(task.task_id)
        assert result["archived"] is True

        card = next(
            item for item in controller.state()["tasks"]
            if item["task_id"] == task.task_id)
        assert card["archived"] is True

        controller.unarchive_task(task.task_id)
        card = next(
            item for item in controller.state()["tasks"]
            if item["task_id"] == task.task_id)
        assert card["archived"] is False
    finally:
        controller.close()


def test_delete_only_terminal_and_removes_task_domain(db, tmp_path):
    controller, store = _controller(db, tmp_path)
    task = _completed_task(store)
    task_dir = tmp_path / "tasks" / task.task_id
    task_dir.mkdir(parents=True)
    (task_dir / "workspace").mkdir()
    (task_dir / "workspace" / "a.txt").write_text("x", encoding="utf-8")
    try:
        result = controller.delete_task(task.task_id)
        assert result["deleted"] is True
        assert store.get(task.task_id) is None
        assert not task_dir.exists()
        assert all(
            item["task_id"] != task.task_id
            for item in controller.state()["tasks"])
    finally:
        controller.close()

    open_task = Task.create(
        task_id="task_open",
        creator="agent_a",
        participants=["agent_a"],
        mode=TaskMode.SOLO,
    )
    store.save(open_task)
    controller, _store = _controller(db, tmp_path)
    try:
        with pytest.raises(ValueError):
            controller.delete_task(open_task.task_id)
    finally:
        controller.close()
