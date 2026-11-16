"""分页字段契约：按行用 start_line / max_lines，按字符用 start_char / max_chars。

`offset` / `limit` 只作为执行器 / 适配器里的过渡别名存在；
Agent 能看到的 prompt 与动作 schema 不应再暴露它们，
否则同一组字段在不同动作里会代表不同单位。
"""

from __future__ import annotations

from front.runtime import FrontAgentRuntime
from work.capabilities import ACTION_PARAMS
from work.prompt import build_work_prompt
from work.roles import WorkContext, WorkRole


def _front_prompt() -> str:
    runtime = FrontAgentRuntime(
        "agent_a", lambda _prompt: "", agent_name="小艾",
        action_types=(
            "artifact_read", "project_list", "project_read",
            "private_read", "draft_read", "web_search",
        ),
    )
    return runtime._prompt(
        "你好",
        active=[],
        completed=[],
        conversation=[],
        agents=[],
        inspection=[],
        action_history=[],
        context="",
        project_context="",
        memory_facts="",
    )


def _work_prompt() -> str:
    context = WorkContext(
        task_id="task_x",
        agent_id="agent_a",
        agent_name="小艾",
        role=WorkRole.SOLO,
        iteration=1,
        brief={"task_text": "做一个页面"},
        workspace_id="WS",
        acceptance_criteria=["可打开"],
        memory_facts="",
        draft_facts="",
    )
    return build_work_prompt(
        context,
        task_description="做一个页面",
        available_actions=(
            "workspace_read", "workspace_write", "draft_read"),
    )


def test_front_prompt_does_not_expose_ambiguous_paging_fields():
    prompt = _front_prompt()

    assert '"offset"' not in prompt
    assert '"limit"' not in prompt
    assert "start_line" in prompt
    assert "start_char" in prompt


def test_work_prompt_does_not_expose_ambiguous_paging_fields():
    prompt = _work_prompt()

    assert '"offset"' not in prompt
    assert '"limit"' not in prompt
    assert '"start_line"' in prompt
    assert '"start_char"' in prompt


def test_action_schema_uses_unambiguous_paging_fields():
    read = ACTION_PARAMS["workspace_read"]
    draft = ACTION_PARAMS["draft_read"]

    assert "offset" not in read and "limit" not in read
    assert "start_line" in read and "max_lines" in read
    assert "next_start_line" in read

    assert "offset" not in draft and "limit" not in draft
    assert "start_char" in draft and "max_chars" in draft
    assert "next_start_char" in draft
