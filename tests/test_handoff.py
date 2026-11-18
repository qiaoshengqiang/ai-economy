"""Phase 2：Front → Worker handoff 消息。"""

from __future__ import annotations

from types import SimpleNamespace

from communication import CommunicationManager, LocalTransport
from communication.recorder import CommunicationRecorder
from storage.database import Database
from work.handoff import HandoffRuntime, is_handoff_message


def _setup():
    db = Database(":memory:")
    db.initialize()
    recorder = CommunicationRecorder(db=db, store_payload=True)
    communication = CommunicationManager(LocalTransport(), recorder=recorder)
    for agent_id in ("agent_a", "agent_b"):
        communication.register(
            SimpleNamespace(agent_id=agent_id, name=agent_id),
            capabilities=(),
        )
    return communication, recorder


def _decision():
    return SimpleNamespace(
        brief=SimpleNamespace(
            task_text="写一个 hello.py",
            human_text="写个 hello world",
            goal="写 hello",
        ),
        role="generalist",
        work_profile="standard",
    )


def test_handoff_sends_offer_and_accept():
    communication, recorder = _setup()
    job = SimpleNamespace(job_id="job_1", workdir="")
    handoff = HandoffRuntime(
        communication,
        front_agent="agent_a",
        worker="agent_b",
        job=job,
    )

    created = handoff.negotiate(
        _decision(),
        lambda: {"ok": True, "task_id": "task_1"},
    )

    assert created["ok"] is True
    rows = recorder.conversation(handoff.conversation_id)
    assert [row["message_type"] for row in rows] == [
        "TASK_OFFER", "ACCEPT"]
    assert rows[0]["from_agent"] == "agent_a"
    assert rows[0]["to_agent"] == "agent_b"
    assert rows[1]["from_agent"] == "agent_b"
    assert rows[1]["to_agent"] == "agent_a"
    assert rows[1]["reply_to"] == rows[0]["message_id"]
    assert '"task_id": "task_1"' in rows[1]["payload"]
    # handoff offer 在 Task 创建前发出，Front 不可能知道 task_id。
    assert '"task_id"' not in rows[0]["payload"]
    assert '"kind": "handoff_offer"' in rows[0]["payload"]


def test_is_handoff_message_distinguishes_taskcore_messages():
    assert is_handoff_message(
        SimpleNamespace(payload={"kind": "handoff_offer"}))
    assert is_handoff_message(
        SimpleNamespace(payload={"kind": "handoff_accept"}))
    assert is_handoff_message(
        SimpleNamespace(payload={"kind": "handoff_reject"}))
    assert not is_handoff_message(
        SimpleNamespace(payload={"kind": "task_offer"}))
    assert not is_handoff_message(SimpleNamespace(payload={}))
    assert not is_handoff_message(SimpleNamespace(payload=None))


def test_handoff_messages_bypass_taskcore_handlers():
    from main import _wire_task_handlers

    communication, _recorder = _setup()

    class _Result:
        def to_reply(self, _message):
            return None

    class _TaskService:
        def __init__(self):
            self.calls = []

        def _handle(self, name, _message):
            self.calls.append(name)
            return _Result()

        def handle_offer(self, message):
            return self._handle("offer", message)

        def handle_counter_offer(self, message):
            return self._handle("counter", message)

        def handle_accept(self, message):
            return self._handle("accept", message)

        def handle_reject(self, message):
            return self._handle("reject", message)

        def handle_start(self, message):
            return self._handle("start", message)

        def handle_submit(self, message):
            return self._handle("submit", message)

        def handle_verify(self, message):
            return self._handle("verify", message)

        def handle_complete(self, message):
            return self._handle("complete", message)

    service = _TaskService()
    _wire_task_handlers(communication, service)
    handler = communication._type_handlers["TASK_OFFER"]

    handler(SimpleNamespace(payload={
        "kind": "handoff_offer", "task_text": "写个 hello"}))
    assert service.calls == []

    handler(SimpleNamespace(payload={
        "task_id": "task_1", "description": "写个 hello"}))
    assert service.calls == ["offer"]


def test_handoff_sends_reject_when_task_creation_fails():
    communication, recorder = _setup()
    job = SimpleNamespace(job_id="job_2", workdir="")
    handoff = HandoffRuntime(
        communication,
        front_agent="agent_a",
        worker="agent_b",
        job=job,
    )

    created = handoff.negotiate(
        _decision(),
        lambda: {"ok": False, "message": "project busy"},
    )

    assert created["ok"] is False
    rows = recorder.conversation(handoff.conversation_id)
    assert [row["message_type"] for row in rows] == [
        "TASK_OFFER", "REJECT"]
    assert '"error": "project busy"' in rows[1]["payload"]
