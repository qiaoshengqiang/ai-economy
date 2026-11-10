"""
Front Agent V1：意图 / Brief / Result 契约
==========================================

重点验证：

    · Front 只在 direct_reply / handoff / rework / follow_up / clarify 之间判断；
    · rework 只能引用已完成的 Task；
    · follow_up 只能引用进行中的 Task；
    · 解析失败时向 Human 澄清，不擅自建 Task；
    · Front 源码里没有创建 Task 的能力。
"""

from __future__ import annotations

import inspect
import json

from front import (
    FrontAgentRuntime, FrontBrief, FrontDecision, FrontIntent, FrontResult,
    parse_front_decision,
)
from front.runtime import FrontAgentRuntime as RuntimeClass


def _think_returning(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def think(prompt):
        think.prompt = prompt
        return text

    return think


def test_direct_reply():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "direct_reply", "reply": "你好，我在。"}))
    decision = runtime.decide("你好")
    assert decision.intent == FrontIntent.DIRECT_REPLY
    assert decision.reply == "你好，我在。"


def test_handoff_produces_brief():
    think = _think_returning({
        "intent": "handoff",
        "brief": {
            "goal": "写一个 hello.txt",
            "deliverables": ["hello.txt"],
            "constraints": ["不要用 shell"],
            "acceptance_expectations": ["文件存在且非空"],
        },
    })
    runtime = FrontAgentRuntime("front", think)
    decision = runtime.decide("帮我写个 hello.txt")

    assert decision.intent == FrontIntent.HANDOFF
    assert isinstance(decision.brief, FrontBrief)
    assert decision.brief.goal == "写一个 hello.txt"
    assert decision.brief.deliverables == ["hello.txt"]
    assert decision.brief.human_text == "帮我写个 hello.txt"


def test_rework_requires_completed_task():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "rework", "task_id": "task_1", "reply": "返工"}))
    decision = runtime.decide(
        "把刚才那个改一下",
        completed_tasks=[{"task_id": "task_1", "status": "COMPLETED"}])
    assert decision.intent == FrontIntent.REWORK
    assert decision.task_id == "task_1"


def test_rework_on_unknown_task_falls_back_to_clarify():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "rework", "task_id": "task_missing"}))
    decision = runtime.decide(
        "改一下", completed_tasks=[{"task_id": "task_1"}])
    assert decision.intent == FrontIntent.CLARIFY
    assert decision.reason.startswith("front_parse_failed")


def test_follow_up_requires_active_task():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "follow_up", "task_id": "task_2"}))
    decision = runtime.decide(
        "现在做到哪了", active_tasks=[{"task_id": "task_2"}])
    assert decision.intent == FrontIntent.FOLLOW_UP
    assert decision.task_id == "task_2"


def test_cancel_requires_active_task():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "cancel", "task_id": "task_2",
             "reply": "好的，我停掉它"}))
    decision = runtime.decide(
        "把这个任务停掉", active_tasks=[{"task_id": "task_2"}])
    assert decision.intent == FrontIntent.CANCEL
    assert decision.task_id == "task_2"


def test_cancel_on_non_active_task_falls_back_to_clarify():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "cancel", "task_id": "task_missing"}))
    decision = runtime.decide(
        "停掉它", active_tasks=[{"task_id": "task_2"}])
    assert decision.intent == FrontIntent.CLARIFY
    assert decision.reason.startswith("front_parse_failed")


def test_artifacts_require_known_task():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "artifacts", "task_id": "task_1",
             "reply": "好的，我把产物导出给你"}))
    decision = runtime.decide(
        "看看刚才的产物",
        completed_tasks=[{"task_id": "task_1"}])
    assert decision.intent == FrontIntent.ARTIFACTS
    assert decision.task_id == "task_1"


def test_artifacts_on_unknown_task_falls_back_to_clarify():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "artifacts", "task_id": "task_missing"}))
    decision = runtime.decide(
        "看看这个任务的产物",
        completed_tasks=[{"task_id": "task_1"}])
    assert decision.intent == FrontIntent.CLARIFY
    assert decision.reason.startswith("front_parse_failed")


def test_clarify():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "clarify", "question": "你要的是哪个文件？"}))
    decision = runtime.decide("那个东西")
    assert decision.intent == FrontIntent.CLARIFY
    assert "哪个文件" in decision.question


def test_malformed_output_falls_back_to_clarify():
    runtime = FrontAgentRuntime("front", _think_returning("not json"))
    decision = runtime.decide("帮我做点事")
    assert decision.intent == FrontIntent.CLARIFY
    assert decision.confidence == 0.0
    assert decision.reason.startswith("front_parse_failed")


def test_parser_accepts_natural_goal_shape():
    decision, error = parse_front_decision(
        '{"goal":"写一个报告","deliverables":["report.md"]}')
    assert error == ""
    assert decision.intent == FrontIntent.HANDOFF
    assert decision.brief.goal == "写一个报告"


def test_parser_rejects_direct_reply_without_text():
    decision, error = parse_front_decision('{"intent":"direct_reply"}')
    assert decision is None
    assert error == "direct_reply_missing_reply"


def test_front_prompt_is_human_facing_and_does_not_invent_constraints():
    captured = {}

    def think(prompt):
        captured["prompt"] = prompt
        return json.dumps({
            "intent": "follow_up",
            "task_id": "task_1",
            "reply": "这个需求已经在做了，完成后我会告诉你。",
        })

    runtime = FrontAgentRuntime("front", think)
    runtime.decide("继续做", active_tasks=[{"task_id": "task_1"}])
    prompt = captured["prompt"]
    assert "不要向 Human 暴露" in prompt
    assert "不要把常见默认" in prompt
    assert "不要写「按现有 Brief 交付」" in prompt
    assert "措辞可以不同" in prompt
    assert "与已完成的 Task 实质相同 → direct_reply" in prompt
    assert "INTERRUPTED" in prompt
    assert "不要回答“正在处理中”" in prompt
    assert "停止必须走这个 intent" in prompt
    assert "产物 / 代码 / 文件 / 结果" in prompt


def test_front_cannot_create_task():
    source = inspect.getsource(RuntimeClass)
    assert "Task.create" not in source
    assert "create_task" not in source

    runtime = FrontAgentRuntime("front", _think_returning("{}"))
    assert not hasattr(runtime, "create_task")


def test_result_formatting():
    result = FrontResult(
        task_id="task_1", status="COMPLETED", summary="写完了",
        artifacts=["hello.txt"], iteration=1)
    text = FrontAgentRuntime.format_result(result)
    assert "task_1" in text
    assert "COMPLETED" in text
    assert "hello.txt" in text


def test_result_from_work_result_dict():
    result = FrontAgentRuntime.result_from_work({
        "task_id": "task_1", "status": "COMPLETED",
        "summary": "done", "iteration": 2,
    })
    assert result.task_id == "task_1"
    assert result.iteration == 2


def test_brief_round_trip():
    brief = FrontBrief(goal="g", deliverables=["a"])
    restored = FrontBrief.from_dict(brief.to_dict())
    assert restored.goal == "g"
    assert restored.deliverables == ["a"]
    assert restored.brief_id == brief.brief_id


def test_decision_validates_required_fields():
    import pytest

    with pytest.raises(ValueError):
        FrontDecision(intent=FrontIntent.HANDOFF)
    with pytest.raises(ValueError):
        FrontDecision(intent=FrontIntent.REWORK)
