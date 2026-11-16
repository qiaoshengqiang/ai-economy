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


def _setup(tmp_path, artifact_read_fn=None, project_root=""):
    workspace = Workspace(tmp_path / "agent_a", "agent_a")
    drafts = TaskDraftStore(
        task_id="front", workspace_of=lambda _aid: workspace)
    dispatcher = build_front_dispatcher(
        workspace_of=lambda _aid: workspace, drafts=drafts,
        artifact_read_fn=artifact_read_fn, project_root=project_root)
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


def test_front_action_set_is_scoped():
    assert "artifact_read" in FRONT_ACTION_TYPES
    assert {"project_list", "project_read"} <= FRONT_ACTION_TYPES
    assert {
        "private_read", "private_write", "private_list", "private_delete",
        "draft_read", "draft_write", "draft_append", "draft_list",
        "draft_delete",
    } <= FRONT_ACTION_TYPES
    assert "workspace_write" not in FRONT_ACTION_TYPES
    assert "project_write" not in FRONT_ACTION_TYPES
    assert "run_tool" not in FRONT_ACTION_TYPES


def test_front_can_read_task_artifact(tmp_path):
    calls = []

    def artifact_read(task_id, path, *, start_line=None, max_lines=None):
        calls.append((task_id, path, start_line, max_lines))
        return {
            "ok": True, "task_id": task_id, "path": path,
            "content": "line1\nline2", "chars": 11,
            "total_lines": 2, "start_line": 1, "max_lines": 2,
            "next_start_line": None,
        }

    _workspace, dispatcher = _setup(
        tmp_path, artifact_read_fn=artifact_read)
    receipt = execute_front_actions(
        dispatcher,
        [{"type": "artifact_read", "parameters": {
            "task_id": "task_x", "path": "index.html",
            "start_line": 1, "max_lines": 2}}],
        agent_id="agent_a",
    )

    assert calls == [("task_x", "index.html", 1, 2)]
    assert "line1" in receipt
    assert "artifact_read" in receipt


def test_front_artifact_read_rejects_conflicting_paging_aliases(tmp_path):
    def artifact_read(task_id, path, *, start_line=None, max_lines=None):
        raise AssertionError("冲突参数不应该触发真实读取")

    _workspace, dispatcher = _setup(
        tmp_path, artifact_read_fn=artifact_read)
    receipt = execute_front_actions(
        dispatcher,
        [{"type": "artifact_read", "parameters": {
            "task_id": "task_x", "path": "index.html",
            "start_line": 1, "offset": 2}}],
        agent_id="agent_a",
    )

    assert "bad_pagination" in receipt
    assert "不要同时使用 start_line 和 offset" in receipt


def test_front_can_list_and_read_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text(
        "<html><head><title>Demo</title></head></html>", encoding="utf-8")

    _workspace, dispatcher = _setup(
        tmp_path, project_root=str(project))
    receipt = execute_front_actions(
        dispatcher,
        [
            {"type": "project_list", "parameters": {}},
            {"type": "project_read", "parameters": {
                "path": "index.html", "start_line": 1, "max_lines": 5}},
        ],
        agent_id="agent_a",
    )

    assert "project_list" in receipt
    assert "project_read" in receipt
    assert "index.html" in receipt
    assert "<title>Demo</title>" in receipt


def test_front_project_read_rejects_conflicting_paging_aliases(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text("<html></html>", encoding="utf-8")

    _workspace, dispatcher = _setup(
        tmp_path, project_root=str(project))
    receipt = execute_front_actions(
        dispatcher,
        [{"type": "project_read", "parameters": {
            "path": "index.html", "start_line": 1, "offset": 2}}],
        agent_id="agent_a",
    )

    assert "bad_pagination" in receipt


def test_project_actions_are_unavailable_without_workdir(tmp_path):
    from front.parser import FRONT_ACTION_NAMES

    _workspace, dispatcher = _setup(tmp_path)
    assert "project_list" not in dispatcher.action_types
    assert "project_read" not in dispatcher.action_types
    # 动作名集合仍然完整；是否可用由 dispatcher 决定。
    assert "project_list" in FRONT_ACTION_NAMES


def test_front_action_names_match_executor():
    from front.parser import FRONT_ACTION_NAMES

    assert FRONT_ACTION_NAMES == FRONT_ACTION_TYPES
