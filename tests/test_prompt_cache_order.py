"""Prompt cache layout：稳定指令在前，易变上下文在后。"""

from __future__ import annotations

from front.runtime import FrontAgentRuntime
from work.prompt import build_cognition_prompt, build_work_prompt
from work.roles import WorkContext, WorkRole


def test_front_prompt_keeps_stable_instructions_before_volatile_context():
    runtime = FrontAgentRuntime(
        "agent_a", lambda _prompt: "", agent_name="小艾",
        action_types=("artifact_read", "private_read", "web_search"),
    )
    prompt = runtime._prompt(
        "你好",
        active=[],
        completed=[],
        conversation=[{"role": "人类", "text": "之前"}],
        agents=[],
        inspection=[],
        action_history=[],
        context="",
        project_context="",
        memory_facts="短期记忆：偏好简洁",
    )

    stable_action = prompt.index("========== Front 动作")
    stable_judgment = prompt.index("========== 任务判断事实")
    stable_intent = prompt.index("========== 意图")
    current_time = prompt.index("当前时间：")
    conversation = prompt.index("========== 最近对话")
    agent_facts = prompt.index("========== Agent 事实")

    memory = prompt.index("========== 记忆（只读")
    assert stable_judgment < memory
    assert stable_action < memory
    assert stable_intent < memory
    assert memory < conversation
    assert conversation < current_time
    assert agent_facts < current_time


def test_work_prompt_keeps_action_schema_before_task_state():
    context = WorkContext(
        task_id="task_x",
        agent_id="agent_b",
        agent_name="小博",
        role=WorkRole.SOLO,
        iteration=2,
        brief={"task_text": "做一个页面"},
        workspace_id="WS",
        acceptance_criteria=["可打开"],
        memory_facts="记忆",
        draft_facts="草稿",
    )
    prompt = build_work_prompt(
        context,
        task_description="做一个页面",
        available_actions=("workspace_read", "workspace_write"),
    )

    assert prompt.index("========== 动作与参数") < prompt.index(
        "========== 当前 Task")
    assert prompt.index("========== 输出格式") < prompt.index(
        "========== 当前 Task")
    assert prompt.index("========== 当前 Task") < prompt.index(
        "========== 任务 ==========")
    assert prompt.index("========== 任务 ==========") < prompt.index(
        "========== Workspace 事实")


def test_cognition_prompt_keeps_protocol_before_task_state():
    context = WorkContext(
        task_id="task_x",
        agent_id="agent_b",
        agent_name="小博",
        role=WorkRole.SOLO,
        iteration=1,
        brief={"task_text": "做一个页面"},
        workspace_id="WS",
        acceptance_criteria=["可打开"],
    )
    prompt = build_cognition_prompt(
        context,
        task_description="做一个页面",
        runtime=None,
        available_actions=("workspace_read",),
        next_actions=("workspace_write",),
    )
    assert prompt.index("========== 协议") < prompt.index(
        "========== 人类任务原文")
    assert prompt.index("========== 本阶段可用动作") < prompt.index(
        "========== 人类任务原文")
    assert prompt.index("========== 人类任务原文") < prompt.index(
        "========== 环境事实")
