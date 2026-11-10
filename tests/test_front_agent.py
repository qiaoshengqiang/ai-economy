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


def test_handoff_can_pass_raw_text():
    runtime = FrontAgentRuntime(
        "front", _think_returning({
            "intent": "handoff",
            "reply": "好，我开始做。",
            "reply_style": "友好、简洁",
        }))
    decision = runtime.decide("帮我写个 hello.txt")

    assert decision.intent == FrontIntent.HANDOFF
    assert decision.brief.human_text == "帮我写个 hello.txt"
    assert decision.brief.task_text == "帮我写个 hello.txt"
    assert decision.brief.goal == "帮我写个 hello.txt"


def test_handoff_can_pass_faithful_brief():
    think = _think_returning({
        "intent": "handoff",
        "reply": "好，我开始做。",
        "reply_style": "友好、简洁，先结论后产物",
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
    # task_text 是 Front 选择交给 Work 的版本
    assert decision.brief.task_text == "写一个 hello.txt"
    # 原文永远保留，供 Work / 审计对照
    assert decision.brief.human_text == "帮我写个 hello.txt"
    assert decision.brief.deliverables == ["hello.txt"]
    assert decision.brief.constraints == ["不要用 shell"]
    assert decision.brief.acceptance_expectations == ["文件存在且非空"]
    assert decision.brief.reply_style == "友好、简洁，先结论后产物"


def test_handoff_task_text_can_override_brief_goal():
    runtime = FrontAgentRuntime(
        "front", _think_returning({
            "intent": "handoff",
            "task_text": "做一个打印 hello world 的 Python 程序",
            "brief": {"goal": "写个程序"},
            "reply": "好",
        }))
    decision = runtime.decide("随便写个程序吧")

    assert decision.brief.task_text == "做一个打印 hello world 的 Python 程序"
    assert decision.brief.human_text == "随便写个程序吧"


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
    assert "Do not expose Brief" in prompt
    assert "You decide how to hand it off" in prompt
    assert "task_text" in prompt
    assert "Preserve the Human's intended task" in prompt
    assert "reply_style only describes" in prompt
    assert "RECENT CONVERSATION" in prompt
    assert "You are the Human's entry point" in prompt
    assert "Task list is known facts, not a matching table" in prompt
    assert "Do not use keyword lists" not in prompt
    assert "INTERRUPTED" in prompt
    assert "still processing" in prompt
    assert "stopping must go through this intent" in prompt
    assert "artifacts / code / files / result" in prompt


def test_front_prompt_includes_recent_conversation():
    captured = {}

    def think(prompt):
        captured["prompt"] = prompt
        return json.dumps({
            "intent": "handoff",
            "reply": "好，我再做一个新的。",
        })

    runtime = FrontAgentRuntime("front", think)
    runtime.decide(
        "再做一个网页，点击开始后出现不断滚动的台球",
        completed_tasks=[{"task_id": "task_1",
                          "status": "COMPLETED",
                          "summary": "滚动台球网页"}],
        conversation=[
            {"role": "human", "text": "之前那个台球网页做好了吗"},
            {"role": "front", "text": "已经做好了，可以直接打开看"},
        ],
    )
    prompt = captured["prompt"]
    assert "Human: 之前那个台球网页做好了吗" in prompt
    assert "Front: 已经做好了，可以直接打开看" in prompt


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


# ============================================================
# Agent 事实 / preferred_agent / inspect
# ============================================================
def test_handoff_parses_preferred_agent():
    runtime = FrontAgentRuntime(
        "front", _think_returning({
            "intent": "handoff",
            "task_text": "写一个笑话",
            "preferred_agent": "agent_e",
            "reply": "好",
        }))
    decision = runtime.decide("让 agent_e 写一个笑话")

    assert decision.preferred_agent == "agent_e"
    assert decision.brief.task_text == "写一个笑话"


def test_prompt_exposes_agent_facts_and_routing_rules():
    think = _think_returning({"intent": "direct_reply", "reply": "好"})
    runtime = FrontAgentRuntime("front", think)
    runtime.decide("你好", agents=[
        {"agent_id": "agent_b", "status": "WORKING",
         "role": "WORKER", "can_work": True},
        {"agent_id": "agent_e", "status": "IDLE",
         "role": "WORKER", "can_work": True},
    ])

    prompt = think.prompt
    assert "agent_b: status=WORKING" in prompt
    assert "agent_e: status=IDLE" in prompt
    assert "preferred_agent that is WORKING" in prompt
    assert "handoff is rejected" in prompt
    assert "inspect_task(task_id)" in prompt


def test_inspect_loop_fetches_facts_then_decides():
    responses = [
        {"intent": "inspect", "task_id": "task_1", "reason": "看看进度"},
        {"intent": "direct_reply", "reply": "它还在跑。"},
    ]
    prompts = []

    def think(prompt):
        prompts.append(prompt)
        return json.dumps(responses.pop(0), ensure_ascii=False)

    calls = []

    def inspect_fn(task_id):
        calls.append(task_id)
        return {"task": {"task_id": task_id, "status": "IN_PROGRESS"}}

    runtime = FrontAgentRuntime("front", think, inspect_fn=inspect_fn)
    decision = runtime.decide(
        "那个任务怎么样了",
        active_tasks=[{"task_id": "task_1", "status": "IN_PROGRESS",
                       "summary": "写页面"}],
    )

    assert decision.intent == FrontIntent.DIRECT_REPLY
    assert calls == ["task_1"]
    assert len(prompts) == 2
    assert "IN_PROGRESS" in prompts[1]
    assert "inspect_task(task_id)" in prompts[0]


def test_inspect_is_bounded():
    responses = [
        {"intent": "inspect", "task_id": "task_1"},
        {"intent": "inspect", "task_id": "task_1"},
        {"intent": "inspect", "task_id": "task_1"},
        {"intent": "direct_reply", "reply": "好"},
    ]

    def think(prompt):
        return json.dumps(responses.pop(0), ensure_ascii=False)

    calls = []
    runtime = FrontAgentRuntime(
        "front", think, inspect_fn=lambda tid: calls.append(tid) or {"x": 1})

    decision = runtime.decide(
        "看看",
        active_tasks=[{"task_id": "task_1", "status": "IN_PROGRESS"}],
    )

    assert len(calls) == 2, "最多 inspect 2 次"
    assert decision.intent == FrontIntent.CLARIFY
    assert "inspect_limit" in decision.reason


def test_inspect_without_capability_falls_back_to_clarify():
    runtime = FrontAgentRuntime(
        "front",
        _think_returning({"intent": "inspect", "task_id": "task_1"}),
    )
    decision = runtime.decide(
        "看看",
        active_tasks=[{"task_id": "task_1", "status": "IN_PROGRESS"}],
    )

    assert decision.intent == FrontIntent.CLARIFY
    assert "inspect_unavailable" in decision.reason
