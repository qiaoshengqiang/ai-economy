"""Controller 接线：life 策略读取、委托闹钟权限、Worker handler 注册。"""

from __future__ import annotations

from types import SimpleNamespace

from work.controller import AIEOSController


def _runtime(life: dict | None):
    resolved_agent = SimpleNamespace(
        raw={"life": life} if life is not None else {},
        system_prompt="你是小伊。",
    )
    front_agent = SimpleNamespace(
        raw={}, system_prompt="你是小艾。")
    return SimpleNamespace(
        directory=SimpleNamespace(all=lambda: [
            SimpleNamespace(agent_id="agent_a",
                            capabilities=SimpleNamespace(names=())),
            SimpleNamespace(agent_id="agent_b",
                            capabilities=SimpleNamespace(names=())),
        ]),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
        resolved_config=SimpleNamespace(
            agent=lambda aid: (
                resolved_agent if aid == "agent_b" else front_agent)),
        communication=SimpleNamespace(recorder=None),
    )


def _controller(runtime):
    return AIEOSController(runtime, front_agent="agent_a")


def test_life_policy_is_read_from_agent_raw_config():
    controller = _controller(_runtime({
        "enabled": True,
        "wake_on_message": True,
        "accept_delegated_alarms": True,
    }))
    try:
        policy = controller._life_policy("agent_b")
        assert policy.enabled is True
        assert policy.wake_on_message is True
        assert policy.accept_delegated_alarms is True
        assert controller._life_policy("agent_a").enabled is False
    finally:
        controller.close()


def test_delegated_alarm_requires_opt_in():
    controller = _controller(_runtime({
        "enabled": True,
        "accept_delegated_alarms": True,
    }))
    try:
        ok, reason = controller._delegated_alarm_allowed("agent_b")
        assert ok is True
        assert reason == ""

        controller._life_policy_cache.clear()
        controller.rt.resolved_config = SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(
                raw={"life": {"enabled": True}},
                system_prompt=""))
        ok, reason = controller._delegated_alarm_allowed("agent_b")
        assert ok is False
        assert "没有接受" in reason
    finally:
        controller.close()


def test_life_wake_starts_and_registers_worker_alarm_handler():
    controller = _controller(_runtime({
        "enabled": True,
        "wake_on_message": True,
        "wake_on_alarm": True,
    }))
    try:
        assert controller._life_wake is not None
        controller._register_life_alarm_handlers()
        assert controller._alarm_dispatcher.handler_for("agent_b") is not None
        # Front 仍然使用自己的 FrontWakeupHandler，不在这里注册。
        assert controller._alarm_dispatcher.handler_for("agent_a") is None
    finally:
        controller.close()


def test_front_relays_life_reply_without_creating_job():
    controller = _controller(_runtime({"enabled": True}))
    try:
        controller._on_front_inbox_message(SimpleNamespace(
            from_agent="agent_b",
            payload={
                "kind": "agent_message",
                "life": True,
                "text": "你好小艾",
            },
        ))
        assert any(
            "回复" in item["text"] and "你好小艾" in item["text"]
            for item in controller._messages)
        assert controller._jobs == {}
    finally:
        controller.close()


def test_life_wake_is_not_started_without_opt_in():
    controller = _controller(_runtime({"enabled": False}))
    try:
        assert controller._life_wake is None
        controller._register_life_alarm_handlers()
        assert controller._alarm_dispatcher.handler_for("agent_b") is None
    finally:
        controller.close()
