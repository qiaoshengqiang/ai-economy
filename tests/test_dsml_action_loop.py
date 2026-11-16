"""DSML → ActionIntent 的循环行为：shadow / execute / 校验 / fail-closed。"""

from __future__ import annotations

import json

import pytest

from actions import ActionDispatcher, ActionResult, run_cognition
from actions.intent import ActionIntent
from llm.base import LLMResponse


class _Executor:
    action_types = frozenset({"fake_action"})

    def __init__(self):
        self.calls = []

    def execute(self, intent: ActionIntent) -> ActionResult:
        self.calls.append(intent)
        return ActionResult(
            success=True, output={"ok": True},
            action_id=intent.intent_id, action_type=intent.action_type)


class _Agent:
    agent_id = "agent_a"

    def __init__(self, response: LLMResponse):
        self._response = response
        self.calls = 0

    def generate(self, messages=None, **kwargs):
        self.calls += 1
        return self._response


def _response_with_call(*, content: str = "", source: str = "dsml") -> LLMResponse:
    return LLMResponse(
        content=content,
        provider="deepseek",
        model="deepseek-flash",
        tool_calls=[{
            "type": "function",
            "function": {
                "name": "fake_action",
                "arguments": json.dumps({"k": "v"}),
            },
        }],
        tool_calls_source=source,
    )


def test_run_cognition_returns_messages_for_append_only():
    agent = _Agent(LLMResponse(
        content='{"decision":"reply","content":"ok"}',
        provider="deepseek", model="deepseek-flash"))

    run = run_cognition(agent, "go")

    assert run.messages == [{"role": "user", "content": "go"}]


def test_execute_mode_maps_validated_tool_call_to_action(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "execute")
    agent = _Agent(_response_with_call())
    ex = _Executor()

    run = run_cognition(
        agent, "go", dispatcher=ActionDispatcher([ex]))

    assert run.segments[0].tool_calls_source == "dsml"
    assert len(run.segments[0].tool_calls) == 1
    assert [i.action_type for i in run.action_intents] == ["fake_action"]
    assert ex.calls and ex.calls[0].parameters == {"k": "v"}


def test_shadow_mode_records_but_does_not_execute(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "shadow")
    agent = _Agent(_response_with_call())
    ex = _Executor()

    run = run_cognition(
        agent, "go", dispatcher=ActionDispatcher([ex]))

    assert run.segments[0].tool_calls
    assert run.action_intents == []
    assert ex.calls == []


def test_unknown_action_is_not_executed(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "execute")
    agent = _Agent(_response_with_call())
    ex = _Executor()

    class OtherExecutor(_Executor):
        action_types = frozenset({"other_action"})

    other = OtherExecutor()
    run = run_cognition(
        agent, "go", dispatcher=ActionDispatcher([other]))

    assert run.action_intents == []
    assert run.segments[0].decision is not None
    assert run.segments[0].decision.envelope_error == "dsml_unknown_action"
    assert other.calls == []


def test_bad_arguments_are_not_executed(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "execute")
    response = LLMResponse(
        content="",
        provider="deepseek",
        model="deepseek-flash",
        tool_calls=[{
            "type": "function",
            "function": {"name": "fake_action", "arguments": "{not json"},
        }],
        tool_calls_source="dsml",
    )
    agent = _Agent(response)
    ex = _Executor()

    run = run_cognition(
        agent, "go", dispatcher=ActionDispatcher([ex]))

    assert run.action_intents == []
    assert run.segments[0].decision.envelope_error == "dsml_bad_arguments"
    assert ex.calls == []


def test_content_envelope_wins_over_tool_calls(monkeypatch):
    monkeypatch.setenv("AIEOS_DSML_MODE", "execute")
    envelope = json.dumps({
        "decision": "act",
        "action": {"type": "fake_action", "parameters": {"from": "envelope"}},
    })
    agent = _Agent(_response_with_call(content=envelope))
    ex = _Executor()

    run = run_cognition(
        agent, "go", dispatcher=ActionDispatcher([ex]))

    assert [i.action_type for i in run.action_intents] == ["fake_action"]
    assert ex.calls[0].parameters == {"from": "envelope"}


def test_default_mode_is_execute(monkeypatch):
    from core.dsml import dsml_mode

    monkeypatch.delenv("AIEOS_DSML_MODE", raising=False)
    assert dsml_mode() == "execute"


def test_shadow_mode_can_be_selected(monkeypatch):
    from core.dsml import dsml_mode

    monkeypatch.setenv("AIEOS_DSML_MODE", "shadow")
    assert dsml_mode() == "shadow"
