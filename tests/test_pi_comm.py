"""Phase 4：Pi Inbox / Outbox。"""

from __future__ import annotations

import time
from types import SimpleNamespace

from communication import CommunicationManager, LocalTransport
from communication.recorder import CommunicationRecorder
from storage.database import Database
from work.comm_bridge import run_coro
from work.pi_comm import PiInboxBridge, PiOutboxBridge


def _setup():
    db = Database(":memory:")
    db.initialize()
    recorder = CommunicationRecorder(db=db, store_payload=True)
    communication = CommunicationManager(LocalTransport(), recorder=recorder)
    for agent_id in ("agent_b", "agent_c"):
        communication.register(
            SimpleNamespace(agent_id=agent_id, name=agent_id),
            capabilities=(),
        )
    return communication, recorder


def test_pi_inbox_capture_and_drain(tmp_path):
    communication, _recorder = _setup()
    bridge = PiInboxBridge(
        communication, agent_id="agent_b", aieos_dir=tmp_path)

    bridge.capture(SimpleNamespace(
        message_id="msg_1",
        from_agent="agent_c",
        to_agent="agent_b",
        message_type="TEXT",
        conversation_id="conv_1",
        created_at="now",
        payload={"kind": "agent_message", "task_id": "task_1",
                 "text": "我改了 api.py"},
    ))

    text = bridge.drain_for_prompt()
    assert "msg_1" in text
    assert "agent_c" in text
    assert "我改了 api.py" in text
    assert not bridge.inbox_md.exists()
    assert "我改了 api.py" in bridge.archive_md.read_text(encoding="utf-8")


def test_pi_inbox_bridge_receives_real_message(tmp_path):
    communication, _recorder = _setup()
    bridge = PiInboxBridge(
        communication, agent_id="agent_b", aieos_dir=tmp_path)
    assert bridge.start()

    try:
        run_coro(communication.send(
            "agent_c", "agent_b", text="inbox-real-check",
            message_type="TEXT", conversation_id="conv-real"))
        deadline = time.time() + 3.0
        text = ""
        while time.time() < deadline and "inbox-real-check" not in text:
            time.sleep(0.05)
            text = bridge.drain_for_prompt()
        assert "inbox-real-check" in text
    finally:
        bridge.stop()


def test_pi_inbox_peek_then_consume_keeps_new_messages(tmp_path):
    communication, _recorder = _setup()
    bridge = PiInboxBridge(
        communication, agent_id="agent_b", aieos_dir=tmp_path)

    bridge.capture(SimpleNamespace(
        message_id="msg_1", from_agent="agent_c", to_agent="agent_b",
        message_type="TEXT", conversation_id="conv_1", created_at="now",
        payload={"kind": "agent_message", "text": "第一条"},
    ))
    first = bridge.peek_for_prompt()
    assert "第一条" in first
    assert bridge.inbox_md.exists(), "peek 不能消费"

    bridge.capture(SimpleNamespace(
        message_id="msg_2", from_agent="agent_c", to_agent="agent_b",
        message_type="TEXT", conversation_id="conv_1", created_at="now",
        payload={"kind": "agent_message", "text": "第二条"},
    ))
    assert bridge.consume_text(first) is True
    remaining = bridge.peek_for_prompt()
    assert "第一条" not in remaining
    assert "第二条" in remaining
    assert "第一条" in bridge.archive_md.read_text(encoding="utf-8")
    assert "第二条" not in bridge.archive_md.read_text(encoding="utf-8")


def test_pi_inbox_ignores_non_text_and_empty_messages(tmp_path):
    communication, _recorder = _setup()
    bridge = PiInboxBridge(
        communication, agent_id="agent_b", aieos_dir=tmp_path)

    # Task 生命周期 / ERROR 不是 Agent 可读消息。
    bridge.capture(SimpleNamespace(
        message_id="msg_error", from_agent="agent_a", to_agent="agent_b",
        message_type="ERROR", conversation_id="conv_1", created_at="now",
        payload={"errors": ["缺少 price"]},
    ))
    bridge.capture(SimpleNamespace(
        message_id="msg_offer", from_agent="agent_a", to_agent="agent_b",
        message_type="TASK_OFFER", conversation_id="conv_1",
        created_at="now", payload={"kind": "handoff_offer"},
    ))
    # TEXT 但内容为空也不应该写。
    bridge.capture(SimpleNamespace(
        message_id="msg_empty", from_agent="agent_a", to_agent="agent_b",
        message_type="TEXT", conversation_id="conv_1", created_at="now",
        payload={"text": "   "},
    ))

    assert not bridge.inbox_md.exists()
    assert bridge.peek_for_prompt() == ""

    bridge.capture(SimpleNamespace(
        message_id="msg_ok", from_agent="agent_a", to_agent="agent_b",
        message_type="TEXT", conversation_id="conv_1", created_at="now",
        payload={"text": "真正的消息"},
    ))
    assert "真正的消息" in bridge.peek_for_prompt()


def test_pi_outbox_sends_agent_message(tmp_path):
    communication, recorder = _setup()
    bridge = PiOutboxBridge(
        communication,
        actor="agent_b",
        task_id="task_1",
        aieos_dir=tmp_path,
    )
    bridge.pending.mkdir(parents=True)
    (bridge.pending / "msg-1.json").write_text(
        '{"to":"agent_c","text":"请检查 shared/types.ts"}',
        encoding="utf-8",
    )

    assert bridge.drain_and_send() == 1
    rows = recorder.conversation("conv_task_1_agent_b_agent_c")
    assert len(rows) == 1
    assert rows[0]["message_type"] == "TEXT"
    assert rows[0]["from_agent"] == "agent_b"
    assert rows[0]["to_agent"] == "agent_c"
    assert '"kind": "agent_message"' in rows[0]["payload"]
    assert not (bridge.pending / "msg-1.json").exists()
    assert (bridge.sent / "msg-1.json").exists()
