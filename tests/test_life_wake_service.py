"""LifeWakeService：消息 / 闹钟 → Life Turn 的唤醒决策。"""

from __future__ import annotations

import json

from core.alarm import WakeupEvent
from core.alarm_store import AlarmStore
from work.life_policy import LifePolicy
from work.life_turn import LifeStimulus, LifeTurnResult
from work.life_wake_service import LifeWakeService


class _Availability:
    def __init__(self, idle: bool = True) -> None:
        self.idle = idle
        self.acquired = 0

    def is_idle(self, _agent_id: str) -> bool:
        return self.idle

    def try_acquire(self, _agent_id: str) -> bool:
        if not self.idle:
            return False
        self.idle = False
        self.acquired += 1
        return True

    def release(self, _agent_id: str) -> None:
        self.idle = True


class _Turn:
    def __init__(self, result: LifeTurnResult) -> None:
        self.result = result
        self.calls = []

    def run(self, stimulus: LifeStimulus, policy: LifePolicy):
        self.calls.append((stimulus, policy))
        return self.result


class _Bridge:
    def __init__(self, text: str) -> None:
        self.text = text
        self.consumed = False

    def peek_for_prompt(self) -> str:
        return self.text

    def consume_text(self, expected: str) -> bool:
        assert self.text.startswith(expected)
        self.text = ""
        self.consumed = True
        return True


def _row(
    message_id: str,
    *,
    from_agent: str = "agent_a",
    text: str = "你好",
    conversation: str = "conv_1",
    payload=None,
):
    body = {"text": text, "kind": "agent_message"}
    body.update(payload or {})
    return {
        "message_id": message_id,
        "from_agent": from_agent,
        "message_type": "TEXT",
        "conversation_id": conversation,
        "trace_id": "trc_1",
        "created_at": "2026-11-16T10:00:00+00:00",
        "payload": json.dumps(body, ensure_ascii=False),
    }


def _service(
    db,
    *,
    bridge=None,
    rows=None,
    turn=None,
    availability=None,
    policy=None,
    conversation=None,
):
    policy = policy or LifePolicy(
        enabled=True, wake_on_message=True, wake_on_alarm=True,
        min_interval_seconds=0, max_turns_per_hour=20)
    availability = availability or _Availability()
    turn = turn or _Turn(LifeTurnResult(
        outcome="replied", summary="你好小艾", reply="你好小艾"))
    rows = rows if rows is not None else {}
    by_id = {
        item["message_id"]: item
        for values in rows.values() for item in values
    }
    processed = []
    service = LifeWakeService(
        policy_fn=lambda _aid: policy,
        turn_runtime=turn,
        availability=availability,
        agent_ids=["agent_e"],
        pi_agents=["agent_e"] if bridge is not None else [],
        pi_bridge_fn=lambda _aid: bridge,
        pending_messages_fn=lambda aid: rows.get(aid, []),
        message_lookup_fn=lambda mid: by_id.get(mid),
        mark_processed_fn=lambda aid, ids: processed.extend(ids),
        conversation_fn=conversation or (lambda _cid: []),
        alarm_store=AlarmStore(db),
        poll_seconds=1.0,
    )
    return service, turn, availability, processed


def _pi_text(message_id: str = "msg_1", text: str = "你好小伊") -> str:
    return (
        f"### {message_id} · 小艾（Agent ID: agent_a） · TEXT\n"
        f"{text}\n\n"
    )


def _pi_empty_error_row(message_id: str = "msg_error") -> dict:
    return {
        "message_id": message_id,
        "from_agent": "agent_a",
        "message_type": "ERROR",
        "conversation_id": "conv_1",
        "trace_id": "trc_1",
        "created_at": "2026-11-16T10:00:00+00:00",
        "payload": json.dumps({"errors": ["缺少 price"]}),
    }


def test_pi_empty_or_non_text_inbox_is_ignored_and_consumed(db):
    bridge = _Bridge(
        "### msg_error · 小艾（Agent ID: agent_a） · ERROR\n\n")
    service, turn, _availability, _ = _service(
        db, bridge=bridge, rows={"agent_e": [_pi_empty_error_row()]})

    started = service.scan_once()

    assert started == 0
    assert turn.calls == []
    assert bridge.consumed is True
    assert bridge.text == ""


def test_pi_message_wakes_idle_agent_and_consumes_after_success(db):
    bridge = _Bridge(_pi_text())
    rows = {"agent_e": [_row("msg_1")]}
    service, turn, availability, _ = _service(
        db, bridge=bridge, rows=rows)

    started = service.scan_once()

    assert started == 1
    assert len(turn.calls) == 1
    assert turn.calls[0][0].agent_id == "agent_e"
    assert turn.calls[0][0].messages[0].message_id == "msg_1"
    assert bridge.consumed is True
    assert availability.acquired == 1
    assert availability.idle is True
    runs = AlarmStore(db).recent_runs("agent_e")
    assert runs[0]["kind"] == "message"
    assert runs[0]["outcome"] == "replied"


def test_busy_agent_is_not_interrupted(db):
    bridge = _Bridge(_pi_text())
    service, turn, _availability, _ = _service(
        db,
        bridge=bridge,
        rows={"agent_e": [_row("msg_1")]},
        availability=_Availability(idle=False),
    )

    started = service.scan_once()

    assert started == 0
    assert turn.calls == []
    assert bridge.consumed is False


def test_no_op_consumes_message_and_records_no_notify(db):
    bridge = _Bridge(_pi_text())
    service, _turn, _availability, _ = _service(
        db,
        bridge=bridge,
        rows={"agent_e": [_row("msg_1")]},
        turn=_Turn(LifeTurnResult(outcome="no_op", summary="no_op")),
    )

    started = service.scan_once()

    assert started == 1
    assert bridge.consumed is True
    runs = AlarmStore(db).recent_runs("agent_e")
    assert runs[0]["outcome"] == "no_notify"


def test_failure_keeps_message_for_retry(db):
    bridge = _Bridge(_pi_text())
    service, turn, _availability, _ = _service(
        db,
        bridge=bridge,
        rows={"agent_e": [_row("msg_1")]},
        turn=_Turn(LifeTurnResult(outcome="failed", summary="boom")),
    )

    started = service.scan_once()

    assert started == 1
    assert bridge.consumed is False
    assert bridge.text
    runs = AlarmStore(db).recent_runs("agent_e")
    assert runs[0]["outcome"] == "failed"


def test_rate_limit_stops_message_wakeups(db):
    bridge = _Bridge(_pi_text())
    service, turn, _availability, _ = _service(
        db,
        bridge=bridge,
        rows={"agent_e": [_row("msg_1")]},
        policy=LifePolicy(
            enabled=True, wake_on_message=True,
            max_turns_per_hour=1, min_interval_seconds=0),
    )
    store = AlarmStore(db)
    store.begin_run(
        wakeup_id="old_wake", agent_id="agent_e",
        scheduled_for="2026-11-16T10:00:00+00:00", kind="message")

    started = service.scan_once()

    assert started == 0
    assert turn.calls == []
    assert bridge.consumed is False


def test_conversation_limit_consumes_and_records_throttled(db):
    bridge = _Bridge(_pi_text())
    service, turn, _availability, _ = _service(
        db,
        bridge=bridge,
        rows={"agent_e": [_row("msg_1")]},
        policy=LifePolicy(
            enabled=True, wake_on_message=True,
            max_turns_per_hour=20, max_conversation_turns=1,
            min_interval_seconds=0),
        conversation=lambda _cid: [
            _row("msg_old", payload={"life": True, "auto": True}),
        ],
    )

    started = service.scan_once()

    assert started == 0
    assert turn.calls == []
    assert bridge.consumed is True
    runs = AlarmStore(db).recent_runs("agent_e")
    assert runs[0]["outcome"] == "throttled"


def test_builtin_pending_message_is_marked_processed_after_success(db):
    rows = {"agent_e": [_row("msg_1")]}
    service, turn, _availability, processed = _service(
        db, rows=rows)

    started = service.scan_once()

    assert started == 1
    assert turn.calls[0][0].messages[0].from_agent == "agent_a"
    assert processed == ["msg_1"]
    assert rows["agent_e"]


def test_alarm_wakes_idle_agent(db):
    service, turn, _availability, _ = _service(db)
    event = WakeupEvent(
        wakeup_id="alarm_1",
        agent_id="agent_e",
        reason="检查一下",
        instruction="看看消息",
        created_by="agent_a",
        scheduled_for="2026-11-16T10:00:00+00:00",
    )

    result = service.run_alarm(event)

    assert result.outcome == "replied"
    assert len(turn.calls) == 1
    assert turn.calls[0][0].kind == "alarm"
    assert turn.calls[0][0].created_by == "agent_a"


def test_alarm_is_skipped_when_agent_busy(db):
    service, turn, _availability, _ = _service(
        db, availability=_Availability(idle=False))
    event = WakeupEvent(wakeup_id="alarm_1", agent_id="agent_e")

    result = service.run_alarm(event)

    assert result.outcome == "skipped_busy"
    assert turn.calls == []


def test_disabled_life_rejects_alarm(db):
    service, turn, _availability, _ = _service(
        db, policy=LifePolicy(enabled=False))
    event = WakeupEvent(wakeup_id="alarm_1", agent_id="agent_e")

    result = service.run_alarm(event)

    assert result.outcome == "rejected"
    assert result.summary == "life_disabled"
    assert turn.calls == []


def test_system_sender_does_not_wake_builtin_agent(db):
    rows = {"agent_e": [_row("msg_1", from_agent="system")]}
    service, turn, _availability, _ = _service(db, rows=rows)

    started = service.scan_once()

    assert started == 0
    assert turn.calls == []
