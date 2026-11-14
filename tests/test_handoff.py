"""Phase 2：Front → Worker handoff 消息。"""

from __future__ import annotations

from types import SimpleNamespace

from communication import CommunicationManager, LocalTransport
from communication.recorder import CommunicationRecorder
from storage.database import Database
from work.handoff import HandoffRuntime


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
