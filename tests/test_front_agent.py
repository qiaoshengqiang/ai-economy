"""
Front Agent V1：意图 / Brief / Result 契约
==========================================

重点验证：

    · Front 只在 direct_reply / handoff / rework / follow_up / clarify 之间判断；
    · rework 只能引用已完成的 Task；
    · follow_up 只能引用进行中的 Task；
    · 解析/LLM 失败 → 可重试错误，不伪装成 clarify，也不擅自建 Task；
    · Front 可以使用私有空间 / 草稿做多步准备；源码里没有创建 Task 的能力。
"""

from __future__ import annotations

import inspect
import json

import pytest

from front import (
    FrontAgentRuntime, FrontBrief, FrontDecision, FrontIntent, FrontResult,
    FrontUnavailableError, parse_front_actions, parse_front_decision,
    parse_front_decisions,
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


def test_rework_on_unknown_task_is_retryable_error():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "rework", "task_id": "task_missing"}))
    with pytest.raises(FrontUnavailableError):
        runtime.decide(
            "改一下", completed_tasks=[{"task_id": "task_1"}])


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


def test_cancel_on_non_active_task_is_retryable_error():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "cancel", "task_id": "task_missing"}))
    with pytest.raises(FrontUnavailableError):
        runtime.decide(
            "停掉它", active_tasks=[{"task_id": "task_2"}])


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


def test_artifacts_on_unknown_task_is_retryable_error():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "artifacts", "task_id": "task_missing"}))
    with pytest.raises(FrontUnavailableError):
        runtime.decide(
            "看看这个任务的产物",
            completed_tasks=[{"task_id": "task_1"}])


def test_clarify():
    runtime = FrontAgentRuntime(
        "front", _think_returning(
            {"intent": "clarify", "question": "你要的是哪个文件？"}))
    decision = runtime.decide("那个东西")
    assert decision.intent == FrontIntent.CLARIFY
    assert "哪个文件" in decision.question


def test_malformed_output_is_retryable_error():
    runtime = FrontAgentRuntime("front", _think_returning("not json"))
    with pytest.raises(FrontUnavailableError, match="无法解析"):
        runtime.decide("帮我做点事")


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
    assert "你是 front，面向人类的入口" in prompt
    assert "Front 动作（可选，多步）" in prompt
    assert "artifact_read" in prompt
    assert "不返回" in prompt
    assert "private_read" in prompt
    assert "brief.files" in prompt
    assert "task_text 是 Work Agent 看到的内容" in prompt
    assert "preferred_agent 是路由元数据" in prompt
    assert "最近对话" in prompt
    assert "没有创建 Task 的动作" in prompt
    assert "INTERRUPTED" in prompt
    assert "follow_up 可以恢复它" in prompt
    assert "artifacts" in prompt
    assert "一个回合内多个动作" in prompt
    # 旧版的强行为约束不应再出现。
    assert "Do not replace the Work Agent" not in prompt
    assert "not the task planner" not in prompt
    assert "Do not expose" not in prompt
    assert "No wallet" not in prompt


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
    assert "人类: 之前那个台球网页做好了吗" in prompt
    assert "Front Agent: 已经做好了，可以直接打开看" in prompt


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
    assert "agent_b：status=WORKING" in prompt
    assert "agent_e：status=IDLE" in prompt
    assert "preferred_agent 为 WORKING → 拒绝" in prompt
    assert "不会自动替换" in prompt
    assert "inspect —— 只读 Task 事实" in prompt


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
    assert "inspect —— 只读 Task 事实" in prompts[0]


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

    with pytest.raises(FrontUnavailableError, match="inspect_limit"):
        runtime.decide(
            "看看",
            active_tasks=[{"task_id": "task_1",
                           "status": "IN_PROGRESS"}],
        )

    assert len(calls) == 2, "最多 inspect 2 次"


def test_inspect_without_capability_is_retryable_error():
    runtime = FrontAgentRuntime(
        "front",
        _think_returning({"intent": "inspect", "task_id": "task_1"}),
    )
    with pytest.raises(FrontUnavailableError, match="inspect_unavailable"):
        runtime.decide(
            "看看",
            active_tasks=[{"task_id": "task_1",
                           "status": "IN_PROGRESS"}],
        )


def test_prompt_separates_task_text_from_routing_metadata():
    think = _think_returning({"intent": "direct_reply", "reply": "好"})
    runtime = FrontAgentRuntime("front", think)
    runtime.decide("让 agent_e 写一个笑话", agents=[
        {"agent_id": "agent_e", "status": "IDLE",
         "role": "WORKER", "can_work": True},
    ])

    prompt = think.prompt
    assert "task_text 是 Work Agent 看到的内容" in prompt
    assert "preferred_agent 是路由元数据" in prompt
    assert "use it verbatim" not in prompt


def test_prompt_says_assignee_is_routing_not_task_text():
    think = _think_returning({"intent": "direct_reply", "reply": "好"})
    runtime = FrontAgentRuntime("front", think)
    runtime.decide("让 agent_e 写一个笑话")

    prompt = think.prompt
    assert "task_text 是 Work Agent 看到的内容" in prompt
    assert "preferred_agent 是路由元数据" in prompt
    assert "路由事实：" in prompt
    assert "keep the original text" not in prompt


# ============================================================
# Front actions / files / raw output / retry
# ============================================================
def test_parse_front_actions_shape():
    actions, error = parse_front_actions(
        '{"actions":[{"type":"private_write",'
        '"parameters":{"path":"a.txt","content":"hi"}}]}')
    assert error == ""
    assert actions == [{
        "type": "private_write",
        "parameters": {"path": "a.txt", "content": "hi"},
    }]

    actions, error = parse_front_actions('{"intent":"direct_reply"}')
    assert actions is None
    assert error == ""


def test_handoff_parses_files():
    runtime = FrontAgentRuntime(
        "front", _think_returning({
            "intent": "handoff",
            "task_text": "落盘 Front 给的代码",
            "brief": {
                "goal": "写 index.html",
                "files": [
                    {"path": "index.html", "content": "<html>hi</html>"},
                    {"path": "app.js", "from_private": "app.js"},
                    {"path": "style.css", "draft_id": "draft_1"},
                ],
            },
            "reply": "好",
        }))
    decision = runtime.decide("把这段代码写下来")

    assert decision.brief.files == [
        {"path": "index.html", "content": "<html>hi</html>"},
        {"path": "app.js", "from_private": "app.js"},
        {"path": "style.css", "draft_id": "draft_1"},
    ]
    assert decision.brief.to_dict()["files"] == decision.brief.files


def test_raw_output_is_preserved_for_audit():
    raw = json.dumps({"intent": "direct_reply", "reply": "好"})
    runtime = FrontAgentRuntime("front", _think_returning(raw))
    decision = runtime.decide("你好")

    assert decision.raw_output == raw
    assert decision.to_dict()["raw_output"] == raw


def test_decision_to_dict_exposes_top_level_task_text():
    raw = json.dumps({
        "intent": "handoff",
        "task_text": "TOP-LEVEL-TEXT",
        "brief": {"goal": "g"},
    })
    runtime = FrontAgentRuntime("front", _think_returning(raw))
    decision = runtime.decide("做一个东西")

    assert decision.to_dict()["task_text"] == "TOP-LEVEL-TEXT"


def test_action_loop_feeds_real_results_back():
    responses = [
        {"actions": [{
            "type": "private_write",
            "parameters": {"path": "snake.html", "content": "<html>"},
        }]},
        {"intent": "direct_reply", "reply": "写好了"},
    ]
    prompts = []
    seen = []

    def think(prompt):
        prompts.append(prompt)
        return json.dumps(responses.pop(0), ensure_ascii=False)

    def execute(actions):
        seen.append(actions)
        return "private_write ok path=snake.html chars=6"

    runtime = FrontAgentRuntime(
        "front", think, execute_actions=execute,
        action_types=["private_write"])
    decision = runtime.decide("准备一个页面")

    assert decision.intent == FrontIntent.DIRECT_REPLY
    assert seen == [[{
        "type": "private_write",
        "parameters": {"path": "snake.html", "content": "<html>"},
    }]]
    assert len(prompts) == 2
    assert "private_write ok path=snake.html" in prompts[1]
    assert runtime.last_action_history[0]["actions"] == seen[0]


def test_llm_failure_retries_once_then_succeeds():
    calls = []

    def think(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("network down")
        return json.dumps({"intent": "direct_reply", "reply": "恢复了"})

    runtime = FrontAgentRuntime("front", think, think_retries=1)
    decision = runtime.decide("你好")

    assert decision.intent == FrontIntent.DIRECT_REPLY
    assert len(calls) == 2


def test_llm_failure_after_retry_is_retryable_error():
    def think(prompt):
        raise RuntimeError("network down")

    runtime = FrontAgentRuntime("front", think, think_retries=1)
    with pytest.raises(FrontUnavailableError, match="LLM call failed"):
        runtime.decide("你好")


def test_action_step_with_reply_is_not_direct_reply():
    text = json.dumps({
        "reply": "我先写文件",
        "actions": [{
            "type": "private_write",
            "parameters": {"path": "a.txt", "content": "hi"},
        }],
    })
    decision, _error = parse_front_decision(text)
    actions, _action_error = parse_front_actions(text)

    assert decision is None
    assert actions and actions[0]["type"] == "private_write"


def test_action_and_final_intent_in_one_output():
    text = json.dumps({
        "actions": [{
            "type": "private_write",
            "parameters": {"path": "code.js", "content": "const x = 1;"},
        }],
        "intent": "handoff",
        "task_text": "落盘 code.js",
        "brief": {
            "goal": "落盘 code.js",
            "files": [{"path": "code.js", "from_private": "code.js"}],
        },
        "reply": "好",
    })
    seen = []

    runtime = FrontAgentRuntime(
        "front", _think_returning(text),
        execute_actions=lambda actions: seen.append(actions) or "ok",
        action_types=["private_write"])
    decision = runtime.decide("把代码交给后台")

    assert decision.intent == FrontIntent.HANDOFF
    assert decision.brief.files == [
        {"path": "code.js", "from_private": "code.js"}]
    assert len(seen) == 1
    assert runtime.last_action_history



def test_parse_multiple_final_decisions():
    text = json.dumps({"decisions": [
        {"intent": "cancel", "task_id": "task_a", "reply": "A 已停"},
        {"intent": "cancel", "task_id": "task_b", "reply": "B 已停"},
    ]})
    decisions, error = parse_front_decisions(
        text, active_task_ids={"task_a", "task_b"})

    assert error == ""
    assert [item.task_id for item in decisions] == ["task_a", "task_b"]
    assert [item.reply for item in decisions] == ["A 已停", "B 已停"]


def test_decide_turn_returns_multiple_decisions():
    runtime = FrontAgentRuntime(
        "front", _think_returning({"decisions": [
            {"intent": "cancel", "task_id": "task_a", "reply": "A"},
            {"intent": "cancel", "task_id": "task_b", "reply": "B"},
        ]}))
    turn = runtime.decide_turn(
        "两个都停掉", active_tasks=[
            {"task_id": "task_a"}, {"task_id": "task_b"}])

    assert [item.task_id for item in turn.decisions] == [
        "task_a", "task_b"]
    assert turn.raw_output


def test_action_written_as_intent_is_still_an_action_step():
    text = json.dumps({
        "intent": "artifact_read",
        "task_id": "task_x",
        "path": "index.html",
        "offset": 1,
        "limit": 10,
        "reason": "show first lines",
    })
    actions, error = parse_front_actions(text)
    decision, _decision_error = parse_front_decision(text)

    assert error == ""
    assert decision is None
    assert actions == [{
        "type": "artifact_read",
        "parameters": {
            "task_id": "task_x", "path": "index.html",
            "offset": 1, "limit": 10,
        },
    }]


def test_handoff_parses_worker_role():
    runtime = FrontAgentRuntime(
        "front", _think_returning({
            "intent": "handoff",
            "task_text": "做一个赛车游戏",
            "role": "game_developer",
            "reply": "好",
        }),
        roles=[{
            "id": "game_developer",
            "name": "游戏开发者",
            "description": "小游戏与 Canvas",
        }],
    )
    decision = runtime.decide("做个赛车游戏")

    assert decision.role == "game_developer"
    assert decision.to_dict()["role"] == "game_developer"


def test_prompt_lists_worker_roles():
    think = _think_returning({"intent": "direct_reply", "reply": "好"})
    runtime = FrontAgentRuntime(
        "front", think,
        roles=[
            {"id": "game_developer", "name": "游戏开发者",
             "description": "小游戏与 Canvas"},
            {"id": "frontend_engineer", "name": "前端工程师",
             "description": "网页与交互"},
        ],
    )
    runtime.decide("你好")

    prompt = think.prompt
    assert "Worker 角色" in prompt
    assert "game_developer" in prompt
    assert "小游戏与 Canvas" in prompt
    assert "frontend_engineer" in prompt


def test_handoff_parses_work_profile():
    runtime = FrontAgentRuntime(
        "front", _think_returning({
            "intent": "handoff",
            "task_text": "做一个赛车游戏",
            "role": "game_developer",
            "work_profile": "creative_product",
            "reply": "好",
        }))
    decision = runtime.decide("做一个赛车游戏")

    assert decision.work_profile == "creative_product"
    assert decision.to_dict()["work_profile"] == "creative_product"


def test_rework_parses_rework_scope():
    runtime = FrontAgentRuntime(
        "front", _think_returning({
            "intent": "rework",
            "task_id": "task_1",
            "rework_scope": "redesign",
            "reply": "好",
        }))
    decision = runtime.decide(
        "重新设计", completed_tasks=[{"task_id": "task_1",
                                      "status": "COMPLETED"}])

    assert decision.rework_scope == "redesign"
    assert decision.to_dict()["rework_scope"] == "redesign"
