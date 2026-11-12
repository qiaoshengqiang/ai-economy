"""
Front 私有空间 / 草稿动作
=========================

Front 可以连续调用 LLM，并通过动作准备自己的文件；这些文件不进入
Task Workspace，只有 handoff 的 `brief.files` 才会交给 Work Agent。
"""

from __future__ import annotations

from core.workspace import Workspace
from work.drafts import TaskDraftStore
from work.front_actions import (
    FRONT_ACTION_TYPES,
    build_front_dispatcher,
    execute_front_actions,
)


def _setup(tmp_path):
    workspace = Workspace(tmp_path / "agent_a", "agent_a")
    drafts = TaskDraftStore(
        task_id="front", workspace_of=lambda _aid: workspace)
    dispatcher = build_front_dispatcher(
        workspace_of=lambda _aid: workspace, drafts=drafts)
    return workspace, dispatcher


def test_front_can_write_and_read_private_files(tmp_path):
    workspace, dispatcher = _setup(tmp_path)

    receipt = execute_front_actions(
        dispatcher,
        [
            {"type": "private_write", "parameters": {
                "path": "src/app.js", "content": "const x = 1;"}},
            {"type": "private_read", "parameters": {"path": "src/app.js"}},
        ],
        agent_id="agent_a",
    )

    assert workspace.read_text("src/app.js") == "const x = 1;"
    assert "private_write" in receipt
    assert "const x = 1;" in receipt


def test_front_can_write_and_read_drafts(tmp_path):
    _workspace, dispatcher = _setup(tmp_path)

    receipt = execute_front_actions(
        dispatcher,
        [
            {"type": "draft_write", "parameters": {
                "content": "DRAFT-CONTENT", "label": "code"}},
            {"type": "draft_list", "parameters": {}},
        ],
        agent_id="agent_a",
    )

    assert "draft_write" in receipt
    assert "draft_list" in receipt
    assert "content_chars" in receipt


def test_front_action_set_is_private_only():
    assert FRONT_ACTION_TYPES == {
        "private_read", "private_write", "private_list", "private_delete",
        "draft_read", "draft_write", "draft_append", "draft_list",
        "draft_delete",
    }
    assert "workspace_write" not in FRONT_ACTION_TYPES
    assert "run_tool" not in FRONT_ACTION_TYPES
