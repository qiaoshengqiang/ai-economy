"""Human Gateway 与 UI 通信投影。"""

from __future__ import annotations

from types import SimpleNamespace

from communication import CommunicationManager, LocalTransport
from communication.recorder import CommunicationRecorder
from storage.database import Database
from work.controller import AIEOSController
from work.human_gateway import HumanGateway


class _Directory:
    def all(self):
        return [SimpleNamespace(agent_id="agent_a", available=True)]


def _setup():
    db = Database(":memory:")
    db.initialize()
    recorder = CommunicationRecorder(db=db, store_payload=True)
    communication = CommunicationManager(LocalTransport(), recorder=recorder)
    communication.register(
        SimpleNamespace(agent_id="agent_a", name="Front"), capabilities=())
    gateway = HumanGateway(communication)
    gateway.ensure_registered()
    return db, communication, gateway


def test_human_gateway_writes_journal():
    _db, _communication, gateway = _setup()

    human_id = gateway.send_human_message(
        "你好", to_agent="agent_a", job_id="job_test")
    front_id = gateway.send_front_reply(
        "你好，我是 Front", front_agent="agent_a",
        job_id="job_test", reply_to=human_id)

    messages = gateway.journal_messages(front_agent="agent_a")
    assert [item["role"] for item in messages] == ["human", "front"]
    assert messages[0]["message_id"] == human_id
    assert messages[1]["message_id"] == front_id
    assert messages[0]["text"] == "你好"
    assert messages[1]["text"] == "你好，我是 Front"


def test_controller_messages_are_mirrored_and_projected():
    _db, _communication, gateway = _setup()
    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", human_gateway=gateway)
    controller._executor = SimpleNamespace(
        submit=lambda *args, **kwargs: None,
        shutdown=lambda **kwargs: None)
    try:
        job_id = controller.submit("你好")
        job = controller._jobs[job_id]
        controller._message(
            "front", "你好，我是 Front", job_id=job_id)

        messages = controller.state()["messages"]
        roles = [item["role"] for item in messages]
        assert roles == ["human", "front"]
        assert messages[0]["message_id"] == job.human_message_id
        assert messages[1]["message_id"]
        assert len(gateway.journal_messages(front_agent="agent_a")) == 2

        # 模拟进程重启：内存消息清空后，UI 仍能从 Journal 恢复对话。
        controller._messages.clear()
        restored = controller.state()["messages"]
        assert [item["role"] for item in restored] == ["human", "front"]
        assert restored[0]["message_id"] == job.human_message_id
    finally:
        controller.close()
