"""Agent Inbox V1：持久化 Inbox Core 的契约测试。"""

from __future__ import annotations

import pytest

from communication import (
    CommunicationManager,
    CommunicationRecorder,
    InboxOutcome,
    LocalTransport,
    MessageStatus,
    ReceiveStatus,
)
from tests.communication_helpers import FakeAgent
from work.comm_bridge import run_coro
from core.identity import utc_now_iso


@pytest.fixture
def wired(db):
    journal = CommunicationRecorder(db)
    manager = CommunicationManager(LocalTransport(), recorder=journal)
    agent_a = FakeAgent("agent_a", "Agent A")
    agent_b = FakeAgent("agent_b", "Agent B")
    manager.register(agent_a)
    manager.register(agent_b)
    return manager, journal, agent_a, agent_b


def test_initialize_inbox_sets_cutover_and_pending(wired):
    manager, journal, _agent_a, _agent_b = wired
    journal.initialize_inbox(
        "agent_b",
        enabled=True,
        cutover_at="2000-01-01T00:00:00+00:00",
    )

    run_coro(manager.send("agent_a", "agent_b", text="hello"))

    pending = journal.list_pending("agent_b")
    assert len(pending) == 1
    assert pending[0]["from_agent"] == "agent_a"
    assert pending[0]["status"] == MessageStatus.DELIVERED

    stats = journal.inbox_stats("agent_b")
    assert stats["initialized"] is True
    assert stats["enabled"] is True
    assert stats["pending"] == 1
    assert stats["total"] == 1


def test_mark_ignored_keeps_processed_fact_false(wired):
    manager, journal, _agent_a, _agent_b = wired
    journal.initialize_inbox(
        "agent_b",
        cutover_at="2000-01-01T00:00:00+00:00",
    )
    message = run_coro(manager.send("agent_a", "agent_b", text="legacy"))

    assert journal.mark_ignored(
        message.message_id, reason="legacy_inbox_cutover") is True

    row = journal.get_message(message.message_id)
    assert row["status"] == MessageStatus.IGNORED
    assert int(row["processed_once"] or 0) == 0
    assert journal.list_pending("agent_b") == []

    stats = journal.inbox_stats("agent_b")
    assert stats["pending"] == 0
    assert stats["ignored"] == 1

    events = journal.events_for(message.message_id)
    assert any(item["status"] == MessageStatus.IGNORED for item in events)

    # 即使消息还留在内存队列里，也不能被消费。
    inbound = run_coro(manager.receive("agent_b", timeout=0.01))
    assert inbound.status == ReceiveStatus.DUPLICATE


def test_initialize_ignores_only_messages_before_cutover(wired):
    manager, journal, _agent_a, _agent_b = wired
    old = run_coro(manager.send("agent_a", "agent_b", text="old"))
    journal.initialize_inbox(
        "agent_b",
        cutover_at=utc_now_iso(),
    )
    new = run_coro(manager.send("agent_a", "agent_b", text="new"))

    assert journal.get_message(old.message_id)["status"] == (
        MessageStatus.IGNORED)
    assert journal.get_message(new.message_id)["status"] == (
        MessageStatus.DELIVERED)
    pending = journal.list_pending("agent_b")
    assert [item["message_id"] for item in pending] == [new.message_id]


def test_inbox_state_can_be_enabled_and_observed(wired):
    _manager, journal, _agent_a, _agent_b = wired
    journal.initialize_inbox(
        "agent_b",
        cutover_at="2000-01-01T00:00:00+00:00",
    )
    assert journal.get_inbox_state("agent_b")["enabled"] is False

    journal.set_inbox_enabled("agent_b", True)
    state = journal.get_inbox_state("agent_b")
    assert state["enabled"] is True
    assert journal.list_inbox_states()

    stats = journal.inbox_stats("agent_b")
    assert stats["enabled"] is True
    assert stats["initialized"] is True


def test_uninitialized_inbox_reports_legacy(wired):
    manager, journal, _agent_a, _agent_b = wired
    run_coro(manager.send("agent_a", "agent_b", text="before"))

    stats = journal.inbox_stats("agent_b")
    assert stats["initialized"] is False
    assert stats["enabled"] is False
    assert stats["pending"] == 0
    assert stats["legacy"] == 1
    assert journal.list_pending("agent_b") == []


def test_inbox_outcome_contract():
    assert InboxOutcome.processed().action == "processed"
    assert InboxOutcome.deferred(next_checkpoint="task_end").action == (
        "deferred")
    assert InboxOutcome.replied("hi").reply == "hi"
    with pytest.raises(ValueError):
        InboxOutcome(action="unknown")


def test_controller_inbox_views_read_recorder(wired):
    from types import SimpleNamespace
    from work.controller import AIEOSController

    manager, journal, _agent_a, _agent_b = wired
    journal.initialize_inbox(
        "agent_b",
        cutover_at="2000-01-01T00:00:00+00:00",
    )
    run_coro(manager.send("agent_a", "agent_b", text="hello"))

    class _Directory:
        def all(self):
            return [
                SimpleNamespace(agent_id="agent_a"),
                SimpleNamespace(agent_id="agent_b"),
            ]

    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
        communication=SimpleNamespace(recorder=journal),
        resolved_config=SimpleNamespace(inbox_enabled=False),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        stats = controller.inbox_stats()
        assert stats["enabled"] is False
        assert stats["agents"]["agent_b"]["pending"] == 1

        pending = controller.inbox_pending("agent_b", limit=10)
        assert pending["agent_id"] == "agent_b"
        assert len(pending["messages"]) == 1
        assert pending["messages"][0]["payload"]["text"] == "hello"

        with pytest.raises(ValueError):
            controller.inbox_pending("no_such_agent")
    finally:
        controller.close()


def test_inbox_state_table_is_part_of_schema(wired):
    _manager, journal, _agent_a, _agent_b = wired
    columns = journal.db.columns_of("communication_inbox_state")
    assert columns == [
        "agent_id", "enabled", "cutover_at", "last_seen_at", "updated_at"]
