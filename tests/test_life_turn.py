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
from work.life_tools import LifeTools
from work.life_turn import LifeMessage, LifeStimulus, LifeTurnRuntime


class _FakeAgent:
    name = "小伊"
    has_brain = True

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []
        self.last_usage = {"cost_ai": 0.25, "total_tokens": 42}
        self.finish_reason = None

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=self.content,
            total_tokens=42,
            model="fake-model",
            provider="fake-provider",
            finish_reason=self.finish_reason,
        )


class _SequenceAgent(_FakeAgent):
    def __init__(self, contents) -> None:
        super().__init__(str(contents[0]))
        self.contents = [str(item) for item in contents]
        self.index = 0

    def generate(self, **kwargs):
        content = self.contents[min(self.index, len(self.contents) - 1)]
        self.index += 1
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=content,
            total_tokens=42,
            model="fake-model",
            provider="fake-provider",
            finish_reason=getattr(self, "finish_reason", None),
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
    content,
    *,
    sent=None,
    alarms=None,
    known=("agent_e", "agent_a", "agent_b"),
    tools=None,
):
    agent = (
        _SequenceAgent(content) if isinstance(content, (list, tuple))
        else _FakeAgent(content))
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
        tools=tools,
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


def test_default_policy_includes_new_life_actions():
    policy = LifePolicy()
    for action in (
        "task_list", "task_inspect", "task_content_list",
        "task_content_read", "private_list", "private_read",
        "private_write", "private_delete", "short_term_memory_read",
        "short_term_memory_add", "short_term_memory_update",
        "long_term_memory_search", "long_term_memory_read",
        "long_term_memory_consolidate",
    ):
        assert policy.allows(action)


def test_prompt_injects_private_memory_and_recent_tasks():
    tools = LifeTools(
        short_term_render=lambda _aid: "revision = 3\n- [stm-1] 上次任务",
        long_term_render=lambda _aid: "- [ltm-1] 长期模式",
        recent_tasks=lambda _aid, _limit: [{
            "task_id": "task_1",
            "status": "COMPLETED",
            "iteration": 1,
            "updated_at": "2026-11-18T00:00:00Z",
            "description": "写 hello world",
            "last_error": "",
        }],
        task_inspect=lambda _aid, _tid: {"ok": True},
        private_read=lambda _aid, _path, _start, _max: {"ok": True},
        short_term_add=lambda *_args: {"ok": True},
        long_term_consolidate=lambda *_args: {"ok": True},
    )
    runtime, _agent, _sent, _ = _runtime(
        json.dumps({"reply": "好", "actions": []}), tools=tools)

    prompt = runtime._prompt(_stimulus(_message()), LifePolicy(enabled=True))

    assert "你的私有短期记忆" in prompt
    assert "stm-1" in prompt
    assert "你的私有长期记忆" in prompt
    assert "ltm-1" in prompt
    assert "我的最近任务" in prompt
    assert "task_1" in prompt
    assert "写 hello world" in prompt
    assert "task_inspect" in prompt
    assert "private_read" in prompt
    assert "short_term_memory_add" in prompt
    assert "long_term_memory_consolidate" in prompt


def test_task_read_action_triggers_second_model_call():
    calls = []

    def recent_tasks(agent_id, limit):
        calls.append((agent_id, limit))
        return [{
            "task_id": "task_1",
            "status": "COMPLETED",
            "iteration": 1,
            "updated_at": "2026-11-18T00:00:00Z",
            "description": "写 hello world",
            "last_error": "",
        }]

    tools = LifeTools(recent_tasks=recent_tasks)
    content = [
        json.dumps({"reply": "", "actions": [{"type": "task_list"}]}),
        json.dumps({"reply": "上次任务是 task_1，已完成。", "actions": []}),
    ]
    runtime, agent, sent, _ = _runtime(content, tools=tools)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert calls == [("agent_e", 5), ("agent_e", 5)]
    assert len(agent.calls) == 2
    assert "只读动作结果" in agent.calls[1]["messages"]
    assert "task_1" in agent.calls[1]["messages"]
    assert result.outcome == "replied"
    assert result.reply == "上次任务是 task_1，已完成。"
    assert result.reads and result.reads[0]["ok"] is True
    assert sent[0]["text"] == "上次任务是 task_1，已完成。"


def test_task_read_action_denied_is_visible_to_model():
    tools = LifeTools(
        task_inspect=lambda _aid, _tid: {
            "ok": False, "code": "not_participant",
            "message": "这个任务不存在，或你不在参与方中",
        })
    content = [
        json.dumps({"reply": "", "actions": [
            {"type": "task_inspect", "task_id": "task_x"}]}),
        json.dumps({"reply": "我看不到这个任务。", "actions": []}),
    ]
    runtime, agent, _sent, _ = _runtime(content, tools=tools)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.reads[0]["ok"] is False
    assert "not_participant" in agent.calls[1]["messages"]
    assert result.reply == "我看不到这个任务。"


def test_private_write_action_uses_tools():
    writes = []

    def private_write(agent_id, path, content):
        writes.append((agent_id, path, content))
        return {"ok": True, "path": path, "chars": len(content)}

    tools = LifeTools(private_write=private_write)
    content = json.dumps({
        "reply": "",
        "actions": [{
            "type": "private_write",
            "path": "notes/todo.md",
            "content": "remember this",
        }],
    })
    runtime, _agent, _sent, _ = _runtime(content, tools=tools)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert writes == [("agent_e", "notes/todo.md", "remember this")]
    assert result.actions[0]["ok"] is True


def test_read_action_respects_policy_allowlist():
    tools = LifeTools(
        recent_tasks=lambda _aid, _limit: [{"task_id": "task_1"}])
    content = [
        json.dumps({"reply": "", "actions": [{"type": "task_list"}]}),
        json.dumps({"reply": "没有权限。", "actions": []}),
    ]
    policy = LifePolicy(enabled=True, allowed_actions=("message_send",))
    runtime, agent, _sent, _ = _runtime(content, tools=tools)

    result = runtime.run(_stimulus(_message()), policy)

    assert result.reads[0]["ok"] is False
    assert result.reads[0]["error"] == "not_allowed"
    assert "not_allowed" in agent.calls[1]["messages"]


def test_read_action_with_bad_params_does_not_crash():
    tools = LifeTools(
        recent_tasks=lambda _aid, _limit: [{"task_id": "task_1"}])
    content = [
        json.dumps({"reply": "", "actions": [
            {"type": "task_list", "limit": "not-a-number"}]}),
        json.dumps({"reply": "参数不对。", "actions": []}),
    ]
    runtime, agent, _sent, _ = _runtime(content, tools=tools)

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.reads[0]["ok"] is False
    assert "ValueError" in result.reads[0]["error"]
    assert result.reply == "参数不对。"
    assert len(agent.calls) == 2


def test_truncated_json_reply_is_extracted():
    content = (
        '{"reply":"小欧回复：max_turns=24 耗尽。\\n'
        '第二轮：playwright 断言失败'
    )
    runtime, agent, sent, _ = _runtime(content)
    agent.finish_reason = "length"

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.outcome == "replied"
    assert "max_turns=24" in result.reply
    assert "输出上限" in result.reply
    assert not result.reply.startswith('{"reply"')
    assert "output_truncated" in result.errors
    assert "max_turns=24" in sent[0]["text"]


def test_truncated_without_reply_gives_clear_error():
    runtime, agent, sent, _ = _runtime('{"actions":[')
    agent.finish_reason = "length"

    result = runtime.run(_stimulus(_message()), LifePolicy(enabled=True))

    assert result.outcome == "replied"
    assert "Life 回答被截断" in result.reply
    assert result.reply != '{"actions":['
    assert "output_truncated" in result.errors
    assert "Life 回答被截断" in sent[0]["text"]
