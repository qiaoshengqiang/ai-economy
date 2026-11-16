"""委托闹钟：`agent_id`（被唤醒者）≠ `created_by`（创建者）。

Front 可以给其他 Agent 设置闹钟，但：

- 目标必须显式开启 `life.accept_delegated_alarms`；
- 只有 Front 可以委托；
- 目标本人和创建者都可以取消；
- 目标自己的闹钟对别人不可见。
"""

from __future__ import annotations

from core.alarm_store import AlarmStore
from services.alarm_service import AlarmService, WakeupDispatcher
from work.front_actions import (
    FrontAlarmCreateExecutor,
    FrontAlarmListExecutor,
)


def _service(db) -> AlarmService:
    return AlarmService(AlarmStore(db), WakeupDispatcher())


class _Intent:
    def __init__(self, parameters: dict, *, agent_id: str = "agent_a"):
        self.intent_id = "intent_test"
        self.action_type = "alarm_create"
        self.agent_id = agent_id
        self.parameters = parameters


def test_store_records_created_by_separately_from_subject(db):
    service = _service(db)
    alarm = service.create(
        agent_id="agent_e",
        created_by="agent_a",
        reason="提醒小伊",
        instruction="看看项目进度",
        schedule="once",
        delay_seconds=60,
    )
    assert alarm["agent_id"] == "agent_e"
    assert alarm["created_by"] == "agent_a"


def test_front_can_delegate_alarm(db):
    service = _service(db)
    executor = FrontAlarmCreateExecutor(
        service,
        front_agent="agent_a",
        delegated_alarm_allowed_fn=lambda target: (
            target == "agent_e", "not accepted"),
    )
    result = executor.execute(_Intent({
        "agent_id": "agent_e",
        "reason": "提醒小伊",
        "instruction": "十分钟后看看消息",
        "delay_seconds": 600,
    }))

    assert result.success
    assert result.output["agent_id"] == "agent_e"
    assert result.output["created_by"] == "agent_a"


def test_worker_cannot_delegate_alarm(db):
    service = _service(db)
    executor = FrontAlarmCreateExecutor(
        service, front_agent="agent_a")
    result = executor.execute(_Intent({
        "agent_id": "agent_e",
        "reason": "r",
        "instruction": "i",
        "delay_seconds": 60,
    }, agent_id="agent_b"))

    assert not result.success
    assert result.metadata["code"] == "delegated_alarm_forbidden"
    assert service.list("agent_e") == []


def test_target_policy_can_reject_delegated_alarm(db):
    service = _service(db)
    executor = FrontAlarmCreateExecutor(
        service,
        front_agent="agent_a",
        delegated_alarm_allowed_fn=lambda _target: (
            False, "agent_e 没有接受委托闹钟"),
    )
    result = executor.execute(_Intent({
        "agent_id": "agent_e",
        "reason": "r",
        "instruction": "i",
        "delay_seconds": 60,
    }))

    assert not result.success
    assert result.metadata["code"] == "delegated_alarm_not_accepted"
    assert "没有接受" in (result.error or "")
    assert service.list("agent_e") == []


def test_subject_and_creator_can_cancel_but_unrelated_cannot(db):
    service = _service(db)
    alarm = service.create(
        agent_id="agent_e",
        created_by="agent_a",
        reason="r",
        instruction="i",
        schedule="once",
        delay_seconds=60,
    )
    alarm_id = alarm["alarm_id"]

    assert service.cancel("agent_b", alarm_id) is False
    assert service.list("agent_e")

    assert service.cancel("agent_a", alarm_id) is True
    assert service.list("agent_e") == []

    # 目标本人也可以拒绝别人给自己设的闹钟。
    second = service.create(
        agent_id="agent_e",
        created_by="agent_a",
        reason="r2",
        instruction="i2",
        schedule="once",
        delay_seconds=60,
    )
    assert service.cancel("agent_e", second["alarm_id"]) is True


def test_delegated_list_only_shows_alarms_created_by_actor(db):
    service = _service(db)
    service.create(
        agent_id="agent_e", created_by="agent_a",
        reason="from front", instruction="i",
        schedule="once", delay_seconds=60,
    )
    service.create(
        agent_id="agent_e", created_by="agent_e",
        reason="self", instruction="i",
        schedule="once", delay_seconds=120,
    )

    front_items = service.list_delegated(
        "agent_a", agent_id="agent_e")
    assert [item["reason"] for item in front_items] == ["from front"]

    all_items = service.list("agent_e")
    assert {item["reason"] for item in all_items} == {"from front", "self"}


def test_front_alarm_list_can_filter_delegated(db):
    service = _service(db)
    service.create(
        agent_id="agent_e", created_by="agent_a",
        reason="from front", instruction="i",
        schedule="once", delay_seconds=60,
    )
    executor = FrontAlarmListExecutor(service, front_agent="agent_a")
    result = executor.execute(_Intent({
        "agent_id": "agent_e",
    }))

    assert result.success
    assert [item["reason"] for item in result.output["alarms"]] == [
        "from front"]


def test_unknown_target_is_rejected_by_policy(db):
    service = _service(db)
    executor = FrontAlarmCreateExecutor(
        service,
        front_agent="agent_a",
        delegated_alarm_allowed_fn=lambda _target: (
            False, "未知 Agent"),
    )
    result = executor.execute(_Intent({
        "agent_id": "missing_agent",
        "reason": "r",
        "instruction": "i",
        "delay_seconds": 60,
    }, agent_id="agent_a"))
    assert not result.success
    assert result.metadata["code"] == "delegated_alarm_not_accepted"
