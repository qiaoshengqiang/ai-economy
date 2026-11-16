"""Life Turn：消息 / 闹钟唤醒后的受控小步。

🔒 守住的边界：

- Life Turn 不创建 Task、不写 Task Workspace；
- 只允许普通文本消息，不允许 Task 生命周期消息；
- 只能给自己设闹钟；
- 回复目标来自唤醒来源，模型不能冒充发送者。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from work.life_policy import LifePolicy
from work.life_turn import LifeMessage, LifeStimulus, LifeTurnRuntime


class _FakeAgent:
    name = "小伊"
    has_brain = True

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []
        self.last_usage = {"cost_ai": 0.25, "total_tokens": 42}

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=self.content,
            total_tokens=42,
            model="fake-model",
            provider="fake-provider",
        )


class _FakeAlarmService:
    def __init__(self) -> None:
        self.created = []
        self.cancelled = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return {"alarm_id": "alarm_test", "next_fire_at": "2026-11-16T10:00:00Z"}

    def list(self, agent_id):
        return [{"alarm_id": "alarm_existing", "agent_id": agent_id}]

    def cancel(self, actor_id, alarm_id):
        self.cancelled.append((actor_id, alarm_id))
        return True


def _runtime(
    content: str,
    *,
    sent=None,
    alarms=None,
    known=("agent_e", "agent_a", "agent_b"),
):
    agent = _FakeAgent(content)
    sent = sent if sent is not None else []
    alarms = alarms if alarms is not None else _FakeAlarmService()

    def send_fn(**kwargs):
        sent.append(kwargs)
        return {"ok": True, "message_id": "msg_reply"}

    runtime = LifeTurnRuntime(
        agent_lookup=lambda aid: agent if aid == "agent_e" else None,
        send_fn=send_fn,
        alarm_service=alarms,
        agent_exists_fn=lambda aid: aid in known,
        agent_unavailable_reason_fn=lambda _aid: "",
        system_prompt_fn=lambda _aid: "你是小伊。",
    )
    return runtime, agent, sent, alarms


def _stimulus(*messages, **kwargs):
    return LifeStimulus(
        agent_id="agent_e",
        wake_id="wake_test",
        kind=kwargs.pop("kind", "message"),
        conversation_id=kwargs.pop("conversation_id", "conv_test"),
        messages=list(messages),
        **kwargs,
    )


def _message(**kwargs):
    data = {
        "message_id": "msg_1",
        "from_agent": "agent_a",
        "text": "你好小伊",
        "conversation_id": "conv_test",
        "payload": {"kind": "agent_message", "text": "你好小伊"},
    }
    data.update(kwargs)
    return LifeMessage(**data)


def test_json_reply_goes_back_to_sender():
    content = json.dumps({"reply": "你好小艾", "actions": []})
    runtime, agent, sent, _ = _runtime(content)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.outcome == "replied"
    assert result.reply == "你好小艾"
    assert sent[0]["from_agent"] == "agent_e"
    assert sent[0]["to_agent"] == "agent_a"
    assert sent[0]["payload"]["life"] is True
    assert agent.calls[0]["call_purpose"] == "life"
    assert agent.calls[0]["cognitive"] is False


def test_plain_text_falls_back_to_reply_without_actions():
    runtime, _agent, sent, _ = _runtime("你好小艾，我收到啦。")

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.outcome == "replied"
    assert result.parsed is False
    assert result.reply == "你好小艾，我收到啦。"
    assert sent[0]["text"] == "你好小艾，我收到啦。"


def test_message_send_action_uses_runtime_identity():
    content = json.dumps({
        "reply": "",
        "actions": [{
            "type": "message_send",
            "to": "agent_b",
            "text": "小艾让我转告你一声。",
        }],
    })
    runtime, _agent, sent, _ = _runtime(content)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.outcome == "replied"
    assert result.actions[0]["ok"] is True
    assert sent[0]["from_agent"] == "agent_e"
    assert sent[0]["to_agent"] == "agent_b"


def test_life_turn_can_set_alarm_for_self_but_not_for_others():
    content = json.dumps({
        "reply": "",
        "actions": [
            {"type": "alarm_create", "reason": "r", "instruction": "i",
             "delay_seconds": 60, "agent_id": "agent_b"},
            {"type": "alarm_create", "reason": "r2", "instruction": "i2",
             "delay_seconds": 120},
        ],
    })
    runtime, _agent, _sent, alarms = _runtime(content)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.actions[0]["ok"] is False
    assert result.actions[0]["error"] == "cannot_schedule_other_agent"
    assert result.actions[1]["ok"] is True
    assert alarms.created[0]["agent_id"] == "agent_e"
    assert alarms.created[0]["created_by"] == "agent_e"


def test_unknown_action_is_rejected():
    content = json.dumps({
        "reply": "",
        "actions": [{"type": "workspace_write", "path": "x"}],
    })
    runtime, _agent, sent, _ = _runtime(content)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.outcome == "failed"
    assert result.actions[0]["ok"] is False
    assert result.actions[0]["error"] == "not_allowed"
    assert sent == []
    assert any("workspace_write" in e for e in result.errors)


def test_no_op_does_not_send_anything():
    runtime, _agent, sent, _ = _runtime(
        json.dumps({"reply": "", "actions": []}))

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.outcome == "no_op"
    assert result.summary == "no_op"
    assert sent == []


def test_multiple_senders_do_not_get_an_accidental_reply():
    content = json.dumps({"reply": "你们好", "actions": []})
    runtime, _agent, sent, _ = _runtime(content)

    result = runtime.run(_stimulus(
        _message(message_id="msg_1", from_agent="agent_a"),
        _message(message_id="msg_2", from_agent="agent_b"),
    ), LifePolicy(enabled=True))

    assert result.outcome == "failed"
    assert any("reply_target_missing" in e for e in result.errors)
    assert sent == []


def test_alarm_wake_replies_to_creator():
    content = json.dumps({"reply": "闹钟收到", "actions": []})
    runtime, _agent, sent, _ = _runtime(content)

    result = runtime.run(_stimulus(
        kind="alarm",
        reason="检查一下",
        instruction="看看消息",
        created_by="agent_a",
    ), LifePolicy(enabled=True))

    assert result.outcome == "replied"
    assert sent[0]["to_agent"] == "agent_a"


def test_rule_message_send_is_not_lifecycle_message():
    content = json.dumps({
        "reply": "",
        "actions": [{
            "type": "message_send",
            "to": "agent_b",
            "text": "x",
            "message_type": "TASK_OFFER",
        }],
    })
    runtime, _agent, sent, _ = _runtime(content)

    runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    # 只发 TEXT；模型不能通过 action 参数改变 message_type。
    assert sent[0]["text"] == "x"
    assert sent[0]["payload"]["kind"] == "agent_message"


def test_policy_defaults_are_disabled():
    policy = LifePolicy.from_raw(None)
    assert policy.enabled is False
    assert policy.wake_on_message is False
    assert policy.allows("message_send")
    assert not policy.allows("workspace_write")
