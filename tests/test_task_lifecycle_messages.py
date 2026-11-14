"""Phase 3：Task 生命周期走 Message + TaskService handler。"""

from __future__ import annotations

from types import SimpleNamespace

from communication import CommunicationManager, LocalTransport
from communication.recorder import CommunicationRecorder
from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef
from core.task_store import TaskStore
from services.task_service import TaskService
from storage.database import Database
from work.lifecycle import WorkLifecycle


def _setup():
    db = Database(":memory:")
    db.initialize()
    store = TaskStore(db)
    recorder = CommunicationRecorder(db=db, store_payload=True)
    communication = CommunicationManager(LocalTransport(), recorder=recorder)
    communication.register(
        SimpleNamespace(agent_id="agent_a", name="Agent A"), capabilities=())
    communication.register(
        "task_service", metadata={"actor_kind": "task_service"})
    service = TaskService(store)
    lifecycle = WorkLifecycle(
        store, communication=communication, task_service=service)
    return store, recorder, lifecycle


def _solo(store: TaskStore) -> Task:
    task = Task.create(
        task_id="T-LIFECYCLE",
        creator="agent_a",
        description="写 hello.txt",
        workspace_id="WS-LIFECYCLE",
        participants=["agent_a"],
        mode=TaskMode.SOLO,
    )
    store.save(task)
    return task


def test_lifecycle_uses_messages_and_task_handlers():
    store, recorder, lifecycle = _setup()
    task = _solo(store)

    lifecycle.start(task, actor="agent_a")
    task = store.get(task.task_id)
    assert task.status == TaskStatus.IN_PROGRESS

    lifecycle.submit(
        task, actor="agent_a",
        result=TaskResultRef(result_ref="hello-v1", summary="done"))
    task = store.get(task.task_id)
    assert task.status == TaskStatus.SUBMITTED

    lifecycle.verify(task, actor="agent_a", passed=True)
    task = store.get(task.task_id)
    assert task.status == TaskStatus.VERIFIED

    lifecycle.complete(task, actor="agent_a")
    task = store.get(task.task_id)
    assert task.status == TaskStatus.COMPLETED
    assert store.count_executions(task.task_id) == 4

    rows = recorder.conversation(f"conv_task_{task.task_id}")
    assert [row["message_type"] for row in rows] == [
        "START", "SUBMIT", "VERIFY", "COMPLETE"]
    execution_ids = {
        record.message_id for record in store.executions_for(task.task_id)}
    assert execution_ids == {row["message_id"] for row in rows}


def test_complete_solo_uses_lifecycle_messages():
    store, recorder, lifecycle = _setup()
    task = _solo(store)

    lifecycle.start(task, actor="agent_a")
    task = store.get(task.task_id)
    result = lifecycle.complete_solo(
        task, actor="agent_a",
        result=TaskResultRef(result_ref="hello-v1", summary="done"))

    assert result["status"] == TaskStatus.COMPLETED
    assert store.get(task.task_id).status == TaskStatus.COMPLETED
    rows = recorder.conversation(f"conv_task_{task.task_id}")
    assert [row["message_type"] for row in rows] == [
        "START", "SUBMIT", "VERIFY", "COMPLETE"]
