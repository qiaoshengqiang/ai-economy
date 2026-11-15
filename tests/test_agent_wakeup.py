"""Agent Wakeup V1：闹钟 / 调度器 / Activity 的核心契约。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.alarm import WakeupResult
from core.alarm_store import AlarmStore
from core.identity import utc_now_iso
from services.alarm_service import AlarmService, WakeupDispatcher
from work.front_actions import FrontAlarmCreateExecutor, FrontAlarmListExecutor


class _Handler:
    def __init__(self, outcome: str = "no_notify") -> None:
        self.outcome = outcome
        self.events = []

    def can_wake(self, agent_id: str) -> bool:
        return True

    def wake(self, event):
        self.events.append(event)
        return WakeupResult(
            outcome=self.outcome,
            summary=f"{self.outcome}:{event.reason}",
            job_id="job_test",
        )


def _due_alarm(store: AlarmStore, *, agent_id: str = "agent_a", every=False):
    return store.create(
        agent_id=agent_id,
        reason="检查任务状态",
        instruction="查看 task_x",
        task_ids=["task_x"],
        schedule_kind="every" if every else "once",
        interval_seconds=60 if every else None,
        next_fire_at=utc_now_iso(),
    )


def test_create_list_cancel_alarm(db):
    store = AlarmStore(db)
    dispatcher = WakeupDispatcher()
    service = AlarmService(store, dispatcher)

    created = service.create(
        agent_id="agent_a",
        reason="检查任务状态",
        instruction="查看 task_x",
        task_ids=["task_x", "task_y"],
        schedule="every",
        interval_seconds=300,
        max_runs=3,
    )
    assert created["status"] == "ACTIVE"
    assert created["task_ids"] == ["task_x", "task_y"]
    assert created["interval_seconds"] == 300

    listed = service.list("agent_a")
    assert [item["alarm_id"] for item in listed] == [created["alarm_id"]]
    assert service.cancel("agent_a", created["alarm_id"]) is True
    assert service.list("agent_a") == []


def test_task_ids_optional_and_max_five(db):
    service = AlarmService(AlarmStore(db), WakeupDispatcher())
    created = service.create(
        agent_id="agent_a", reason="r", instruction="i",
        task_ids=[], schedule="once", delay_seconds=60)
    assert created["task_ids"] == []

    with pytest.raises(ValueError, match="最多 5 个"):
        service.create(
            agent_id="agent_a", reason="r", instruction="i",
            task_ids=["t1", "t2", "t3", "t4", "t5", "t6"],
            schedule="once", delay_seconds=60)


def test_claim_due_and_unique_run(db):
    store = AlarmStore(db)
    alarm = _due_alarm(store)
    claimed = store.claim_due("9999-01-01T00:00:00+00:00")
    assert claimed is not None
    assert claimed.status == "RUNNING"
    assert claimed.alarm_id == alarm.alarm_id

    run_id = store.begin_run(
        wakeup_id=alarm.alarm_id,
        agent_id=alarm.agent_id,
        scheduled_for=alarm.next_fire_at)
    assert run_id
    # 同一个到期时刻只允许一条 run。
    assert store.begin_run(
        wakeup_id=alarm.alarm_id,
        agent_id=alarm.agent_id,
        scheduled_for=alarm.next_fire_at) is None
    store.finish_run(run_id, outcome="no_notify", summary="ok")
    row = store.recent_runs("agent_a")[0]
    assert row["outcome"] == "no_notify"
    assert row["summary"] == "ok"


def test_dispatch_no_notify_writes_activity(db):
    store = AlarmStore(db)
    handler = _Handler()
    dispatcher = WakeupDispatcher()
    dispatcher.register("agent_a", handler)
    service = AlarmService(store, dispatcher)
    alarm = _due_alarm(store)

    service._dispatch(alarm)

    assert handler.events
    assert handler.events[0].task_ids == ["task_x"]
    row = store.recent_runs("agent_a")[0]
    assert row["outcome"] == "no_notify"
    assert row["job_id"] == "job_test"
    assert store.get(alarm.alarm_id).status == "COMPLETED"


def test_recurring_dispatch_reschedules(db):
    store = AlarmStore(db)
    handler = _Handler("replied")
    dispatcher = WakeupDispatcher()
    dispatcher.register("agent_a", handler)
    service = AlarmService(store, dispatcher)
    alarm = _due_alarm(store, every=True)

    service._dispatch(alarm)

    updated = store.get(alarm.alarm_id)
    assert updated.status == "ACTIVE"
    assert updated.fire_count == 1
    assert updated.next_fire_at >= alarm.next_fire_at


def test_guard_pauses_alarms(db):
    store = AlarmStore(db)
    handler = _Handler()
    dispatcher = WakeupDispatcher()
    dispatcher.register("agent_a", handler)
    service = AlarmService(
        store, dispatcher, guard_max_per_agent=1)
    first = _due_alarm(store)
    second = _due_alarm(store)

    service._dispatch(first)
    service._dispatch(second)

    assert store.get(second.alarm_id).status == "PAUSED"
    assert any(
        row["outcome"] == "guard_limit"
        for row in store.recent_runs("agent_a"))


def test_expire_and_recover_running(db):
    store = AlarmStore(db)
    service = AlarmService(store, WakeupDispatcher())
    created = service.create(
        agent_id="agent_a", reason="r", instruction="i",
        schedule="once", delay_seconds=60,
        expires_in_seconds=1)
    assert created["status"] == "ACTIVE"

    # 直接改到期时间，验证 expire_due 只处理 ACTIVE。
    with db.transaction() as conn:
        conn.execute(
            "UPDATE agent_alarms SET next_fire_at = ?, "
            "expires_at = ? WHERE alarm_id = ?",
            ("2000-01-01T00:00:00+00:00",
             "2000-01-02T00:00:00+00:00", created["alarm_id"]),
        )
    store.expire_due("2000-01-03T00:00:00+00:00")
    assert store.get(created["alarm_id"]).status == "COMPLETED"

    # RUNNING -> ACTIVE 恢复。
    alarm = _due_alarm(store, agent_id="agent_b")
    store.claim_due("9999-01-01T00:00:00+00:00")
    store.recover_running(now=utc_now_iso())
    assert store.get(alarm.alarm_id).status == "ACTIVE"


def test_front_alarm_actions(db):
    service = AlarmService(AlarmStore(db), WakeupDispatcher())
    agent_id = "agent_a"
    create = FrontAlarmCreateExecutor(service)
    intent = SimpleNamespace(
        intent_id="i1", action_type="alarm_create", agent_id=agent_id,
        parameters={
            "reason": "检查任务状态",
            "instruction": "查看 task_x",
            "task_ids": ["task_x"],
            "schedule": "once",
            "delay_seconds": 60,
        })
    result = create.execute(intent)
    assert result.success is True
    assert result.output["status"] == "ACTIVE"

    listed = FrontAlarmListExecutor(service).execute(SimpleNamespace(
        intent_id="i2", action_type="alarm_list", agent_id=agent_id,
        parameters={}))
    assert listed.success is True
    assert len(listed.output["alarms"]) == 1


def test_front_parser_accepts_no_notify():
    from front.parser import parse_front_decision

    decision, error = parse_front_decision(
        '{"intent":"no_notify","reason":"状态没有变化"}')
    assert error == ""
    assert decision is not None
    assert decision.intent == "no_notify"
    assert decision.reason == "状态没有变化"


def test_front_parser_recognizes_alarm_actions():
    from front.parser import parse_front_actions

    actions, error = parse_front_actions(
        '{"actions":[{"type":"alarm_create","parameters":'
        '{"reason":"r","instruction":"i","schedule":"once",'
        '"delay_seconds":60}}]}')
    assert error == ""
    assert actions is not None
    assert actions[0]["type"] == "alarm_create"


def _controller_runtime():
    return SimpleNamespace(
        directory=SimpleNamespace(all=lambda: [
            SimpleNamespace(
                agent_id="agent_a",
                capabilities=SimpleNamespace(names=())),
        ]),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )


def test_controller_front_wakeup_no_notify(monkeypatch):
    from front import FrontDecision, FrontIntent, FrontTurn
    from work.controller import AIEOSController
    from core.alarm import WakeupEvent

    class _Front:
        def decide_turn(self, _text, **_kwargs):
            return FrontTurn(decisions=[FrontDecision(
                intent=FrontIntent.NO_NOTIFY,
                reason="状态没有变化",
            )])

    controller = AIEOSController(
        _controller_runtime(), front_agent="agent_a")
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
    event = WakeupEvent(
        wakeup_id="alarm_1", agent_id="agent_a",
        reason="检查状态", instruction="查看 task_x")
    try:
        result = controller.run_front_wakeup(event)
        state = controller.state()
    finally:
        controller.close()

    assert result.outcome == "no_notify"
    assert state["jobs"][0]["source"] == "alarm"
    assert state["jobs"][0]["status"] == "NO_NOTIFY"
    assert not [
        item for item in state["messages"] if item.get("role") == "front"]


def test_controller_front_wakeup_rejects_handoff(monkeypatch):
    from front import FrontDecision, FrontIntent, FrontTurn
    from work.controller import AIEOSController
    from core.alarm import WakeupEvent

    class _Front:
        def decide_turn(self, _text, **_kwargs):
            return FrontTurn(decisions=[FrontDecision(
                intent=FrontIntent.HANDOFF,
                brief=SimpleNamespace(
                    goal="x", task_text="x", human_text="",
                    front_notes=""),
            )])

    controller = AIEOSController(
        _controller_runtime(), front_agent="agent_a")
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
    event = WakeupEvent(
        wakeup_id="alarm_2", agent_id="agent_a",
        reason="检查状态", instruction="查看 task_x")
    try:
        result = controller.run_front_wakeup(event)
    finally:
        controller.close()

    assert result.outcome == "rejected"
    assert "alarm_wakeup_cannot_do" in result.summary


class _DetailHandler:
    def can_wake(self, agent_id: str) -> bool:
        return True

    def wake(self, event):
        return WakeupResult(
            outcome="replied",
            summary="task_x 已完成",
            job_id="job_detail",
            detail={"calls": [
                {"step": 1, "raw_output": '{"intent":"x"}',
                 "decision_error": "", "action_error": "",
                 "decisions": ["direct_reply"], "actions": [],
                 "receipt": ""},
            ]},
        )


def test_wakeup_detail_persisted_but_not_in_list(db):
    store = AlarmStore(db)
    dispatcher = WakeupDispatcher()
    dispatcher.register("agent_a", _DetailHandler())
    service = AlarmService(store, dispatcher)
    alarm = _due_alarm(store)

    service._dispatch(alarm)

    row = store.recent_runs("agent_a")[0]
    # 列表只给摘要：不携带原始模型输出。
    assert "detail" not in row
    assert row["task_ids"] == ["task_x"]
    assert row["alarm_id"] == alarm.alarm_id

    detail = store.get_run(row["run_id"])
    assert detail is not None
    assert detail["detail"]["task_ids"] == ["task_x"]
    assert detail["detail"]["fire_count"] == 1
    assert detail["detail"]["calls"][0]["raw_output"] == '{"intent":"x"}'
    assert service.run_detail(row["run_id"])["run_id"] == row["run_id"]
    assert service.run_detail("wake_missing") is None


def test_wakeup_calls_keep_raw_output_and_receipt():
    from work.controller import AIEOSController

    turn = SimpleNamespace(
        attempts=[
            {"step": 1, "raw_output": "A", "decision_error": "",
             "action_error": "", "decisions": [],
             "actions": ["artifact_read"]},
            {"step": 2, "raw_output": "B", "decision_error": "",
             "action_error": "", "decisions": ["no_notify"],
             "actions": []},
        ],
        action_history=[{"raw_output": "A", "receipt": "R"}],
    )
    calls = AIEOSController._wakeup_calls(turn)
    assert [call["raw_output"] for call in calls] == ["A", "B"]
    assert calls[0]["receipt"] == "R"
    assert calls[1]["receipt"] == ""
    assert calls[1]["decisions"] == ["no_notify"]


def test_controller_activity_detail_only_returns_front_runs():
    from work.controller import AIEOSController

    runs = {
        "wake_1": {
            "run_id": "wake_1", "agent_id": "agent_a",
            "started_at": "2026-11-15T10:00:00+00:00",
            "detail": {"calls": []},
        },
        "wake_2": {
            "run_id": "wake_2", "agent_id": "agent_b",
            "started_at": "2026-11-15T10:01:00+00:00",
            "detail": {"calls": []},
        },
    }
    controller = AIEOSController(
        _controller_runtime(), front_agent="agent_a")
    controller.alarm_service = SimpleNamespace(
        run_detail=lambda run_id: runs.get(run_id),
        count_runs=lambda agent_id, since="": 1,
        stop=lambda: None,
    )
    try:
        data = controller.activity_detail("wake_1")
        assert data["activity"]["run_id"] == "wake_1"
        for bad in ("wake_2", "wake_missing", ""):
            with pytest.raises(ValueError):
                controller.activity_detail(bad)
        seen = controller.mark_activities_seen("agent_a", run_id="wake_1")
        assert seen["seen_at"] == "2026-11-15T10:00:00+00:00"
        with pytest.raises(ValueError):
            controller.mark_activities_seen("agent_a", run_id="wake_2")
    finally:
        controller.alarm_service = None
        controller.close()
