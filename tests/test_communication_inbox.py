"""Agent Inbox V1：持久化 Inbox Core 的契约测试。"""

from __future__ import annotations

import time

import pytest

from communication import (
    CommunicationManager,
    CommunicationRecorder,
    InboxConsumer,
    InboxOutcome,
    InboxPolicy,
    LocalTransport,
    MessageStatus,
    MessageType,
    ReceiveStatus,
)
from work.availability import AgentAvailability
from work.life_inbox_service import LifeInboxService
from tests.communication_helpers import FakeAgent
from work.comm_bridge import run_coro
from work.pi_comm import PiInboxBridge
from work.pi_inbox_service import PiInboxService
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
    assert InboxPolicy().reply_mode == "never"
    assert InboxPolicy(reply_mode="manual").reply_mode == "manual"
    with pytest.raises(ValueError):
        InboxPolicy(reply_mode="always")

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


def test_pi_inbox_bridge_persists_messages_while_idle(wired, tmp_path):
    manager, journal, _agent_a, _agent_b = wired
    bridge = PiInboxBridge(
        manager,
        agent_id="agent_b",
        inbox_dir=tmp_path / "inbox",
    )
    assert bridge.start() is True
    try:
        message = run_coro(manager.send(
            "agent_a", "agent_b", text="idle hello"))
        deadline = time.time() + 3.0
        while time.time() < deadline and not bridge.inbox_md.exists():
            time.sleep(0.05)
        assert bridge.inbox_md.exists()
        assert "idle hello" in bridge.inbox_md.read_text(encoding="utf-8")
        assert journal.get_message(message.message_id)["processed_once"] == 1

        drained = bridge.drain_for_prompt()
        assert "idle hello" in drained
        assert not bridge.inbox_md.exists()
    finally:
        bridge.stop()


def test_pi_inbox_service_starts_and_stops_agents(wired, tmp_path):
    manager, journal, _agent_a, _agent_b = wired
    service = PiInboxService(manager, agents_dir=tmp_path / "agents")
    service.sync(["agent_b"])
    try:
        assert service.bridge_for("agent_b") is not None
        assert journal.get_inbox_state("agent_b")["enabled"] is True
    finally:
        service.sync([])
        assert service.bridge_for("agent_b") is None
        assert journal.get_inbox_state("agent_b")["enabled"] is False


def test_inbox_consumer_dispatches_messages_to_handler(wired):
    manager, _journal, _agent_a, _agent_b = wired
    seen = []
    consumer = InboxConsumer(
        manager, agent_id="agent_b", handler=seen.append)
    assert consumer.start() is True
    try:
        run_coro(manager.send("agent_a", "agent_b", text="inbox"))
        deadline = time.time() + 3.0
        while time.time() < deadline and not seen:
            time.sleep(0.05)
        assert seen
        assert seen[0].payload["text"] == "inbox"
    finally:
        consumer.stop()


def test_controller_submit_dispatches_through_front_inbox(wired):
    from types import SimpleNamespace
    from work.controller import AIEOSController
    from work.human_gateway import HumanGateway

    manager, _journal, _agent_a, _agent_b = wired
    gateway = HumanGateway(manager)
    gateway.ensure_registered()

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
        communication=manager,
        resolved_config=SimpleNamespace(inbox_enabled=True),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", human_gateway=gateway)
    dispatched = []
    controller._run_job = lambda job: dispatched.append(job.job_id)
    try:
        job_id = controller.submit("hello")
        deadline = time.time() + 3.0
        while time.time() < deadline and job_id not in dispatched:
            time.sleep(0.05)
        assert dispatched == [job_id]
        assert controller._front_inbox_consumer is not None
    finally:
        controller.close()


def test_solo_runtime_drains_inbox_to_checkpoint(wired):
    from types import SimpleNamespace
    from work.runtime import BuiltinWorkRuntime

    manager, journal, _agent_a, _agent_b = wired
    journal.initialize_inbox(
        "agent_b",
        enabled=True,
        cutover_at="2000-01-01T00:00:00+00:00",
    )
    message = run_coro(manager.send(
        "agent_a", "agent_b", text="solo hello"))

    runner = BuiltinWorkRuntime.__new__(BuiltinWorkRuntime)
    runner.rt = SimpleNamespace(communication=manager)
    text = runner._drain_inbox("agent_b")

    assert "Agent 收件箱" in text
    assert "solo hello" in text
    assert journal.get_message(message.message_id)["processed_once"] == 1
    assert runner._drain_inbox("agent_b") == ""


def test_controller_enables_solo_inbox_without_consumer(wired):
    from types import SimpleNamespace
    from work.controller import AIEOSController

    manager, journal, _agent_a, _agent_b = wired

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
        communication=manager,
        resolved_config=SimpleNamespace(inbox_enabled=True),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", pi_work_agents=[])
    try:
        assert controller._pi_inbox_service is not None
        assert controller._pi_inbox_service.active_agents == []
        assert controller._front_inbox_consumer is None
        assert journal.get_inbox_state("agent_b")["enabled"] is True
    finally:
        controller.close()


def test_life_inbox_auto_replies_to_agent_message(wired):
    from types import SimpleNamespace

    manager, journal, agent_a, agent_b = wired
    journal.initialize_inbox(
        "agent_b",
        enabled=True,
        cutover_at="2000-01-01T00:00:00+00:00",
    )
    captured = {}

    def _think(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = kwargs.get("system") or ""
        return "pong"

    agent_b.think = _think
    agent_b.last_usage = {"cost_ai": 0.0}
    availability = AgentAvailability()
    availability.register("agent_b")

    message = run_coro(manager.send(
        "agent_a", "agent_b", text="ping",
        message_type=MessageType.TEXT,
        payload={"kind": "agent_message", "text": "ping"},
    ))

    agents = {"agent_a": agent_a, "agent_b": agent_b}
    runtime = SimpleNamespace(
        communication=manager,
        economy=SimpleNamespace(agent=lambda aid: agents.get(aid)),
    )
    service = LifeInboxService(runtime, availability)
    service.configure({"agent_b": InboxPolicy(
        reply_mode="auto",
        max_auto_replies_per_conversation=3,
        max_cost_per_message=0.0,
    )})
    try:
        assert service.process_once() == 1
    finally:
        service.stop()

    assert "你的名字是：Agent B" in captured["system"]
    assert "你的 Agent ID 是：agent_b" in captured["system"]
    assert "来自：Agent A（Agent ID: agent_a）" in captured["prompt"]
    assert journal.get_message(message.message_id)["processed_once"] == 1
    inbound = run_coro(manager.receive("agent_a", timeout=0.1))
    assert inbound.message is not None
    assert inbound.message.payload["kind"] == "agent_message"
    assert inbound.message.payload["text"] == "pong"
    assert inbound.message.reply_to == message.message_id


def test_life_inbox_does_not_reply_while_working(wired):
    from types import SimpleNamespace

    manager, journal, _agent_a, agent_b = wired
    journal.initialize_inbox(
        "agent_b",
        enabled=True,
        cutover_at="2000-01-01T00:00:00+00:00",
    )
    agent_b.think = lambda *_args, **_kwargs: "pong"
    availability = AgentAvailability()
    availability.register("agent_b")
    availability.try_acquire("agent_b")

    message = run_coro(manager.send(
        "agent_a", "agent_b", text="ping",
        message_type=MessageType.TEXT,
        payload={"kind": "agent_message", "text": "ping"},
    ))
    runtime = SimpleNamespace(
        communication=manager,
        economy=SimpleNamespace(agent=lambda _aid: agent_b),
    )
    service = LifeInboxService(runtime, availability)
    service.configure({"agent_b": InboxPolicy(reply_mode="auto")})
    try:
        assert service.process_once() == 0
    finally:
        service.stop()

    assert journal.get_message(message.message_id)["processed_once"] == 0


def test_controller_reads_agent_inbox_policy():
    from types import SimpleNamespace
    from work.controller import AIEOSController

    controller = AIEOSController.__new__(AIEOSController)
    controller.rt = SimpleNamespace(
        resolved_config=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(raw={
                "inbox": {
                    "reply_mode": "auto",
                    "max_auto_replies_per_conversation": 2,
                    "max_cost_per_message": 0.5,
                }
            })
        )
    )
    policy = controller._inbox_policy_for("agent_b")
    assert policy.reply_mode == "auto"
    assert policy.max_auto_replies_per_conversation == 2
    assert policy.max_cost_per_message == 0.5
