"""
Draft Buffer + 批量 promotion
=============================

目标：生成与落盘分离；下一次调用可以一次 promote 多个文件，
而不是每个文件一个 turn。

验证：

    ① 草稿按 Agent 隔离；append/read/list/delete；
    ② promotion 一次写多个文件（真实 Workspace Gateway）；
    ③ 部分失败逐项返回，成功项仍然落盘；
    ④ draft actions 的输入校验与 observable 语义；
    ⑤ dispatcher 只在组合根提供 draft 依赖时注册。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.intent import ActionIntent                        # noqa: E402
from actions.workspace_draft import (                          # noqa: E402
    ACTION_DRAFT_READ,
    ACTION_WORKSPACE_PROMOTE_DRAFTS,
    DraftAppendExecutor,
    DraftDeleteExecutor,
    DraftListExecutor,
    DraftReadExecutor,
    DraftWriteExecutor,
    WorkspacePromoteDraftsExecutor,
)
from demand.drafts import DraftBuffer                           # noqa: E402


def test_draft_buffer_isolated_by_agent_and_supports_append_read():
    buf = DraftBuffer()
    created = buf.write("agent_a", "hello", label="greeting")
    assert created["ok"] and created["draft_id"].startswith("draft_")

    # 其他 Agent 看不到
    assert buf.get("agent_b", created["draft_id"]) is None
    assert buf.read("agent_b", created["draft_id"])["code"] == "draft_not_found"

    appended = buf.append("agent_a", created["draft_id"], " world")
    assert appended["ok"] and appended["chars"] == 11

    got = buf.read(
        "agent_a", created["draft_id"], start_char=6, max_chars=5)
    assert got["ok"] and got["content"] == "world"
    assert got["total_chars"] == 11
    assert got["start_char"] == 6
    assert got["max_chars"] == 5
    assert got["next_start_char"] is None
    assert got["truncated"] is False

    # 旧别名仍然映射到字符位置（过渡期兼容）。
    alias = buf.read("agent_a", created["draft_id"], offset=6, limit=5)
    assert alias["content"] == "world"

    listed = buf.list("agent_a")
    assert listed["count"] == 1
    assert listed["drafts"][0]["preview"] == "hello world"

    assert buf.delete("agent_a", created["draft_id"])["ok"]
    assert buf.list("agent_a")["count"] == 0


def test_draft_buffer_limits_and_empty_content():
    buf = DraftBuffer(max_draft_chars=10, max_drafts_per_agent=1)
    assert buf.write("a", "")["code"] == "empty_content"
    assert buf.write("a", "x" * 11)["code"] == "draft_too_large"
    assert buf.write("a", "12345")["ok"]
    assert buf.write("a", "67890")["code"] == "too_many_drafts"


def test_draft_actions_validate_and_call_functions():
    seen = {}

    def write(agent_id, content, *, label):
        seen.update(agent_id=agent_id, content=content, label=label)
        return {"ok": True, "draft_id": "d1", "chars": len(content)}

    result = DraftWriteExecutor(write).execute(ActionIntent(
        action_type="draft_write", agent_id="a",
        parameters={"content": "abc", "label": "x"}))
    assert result.success is True
    assert result.output["draft_id"] == "d1"
    assert seen == {"agent_id": "a", "content": "abc", "label": "x"}

    bad = DraftWriteExecutor(write).execute(ActionIntent(
        action_type="draft_write", agent_id="a",
        parameters={"content": "abc", "path": "x"}))
    assert bad.success is False
    assert bad.metadata["code"] == "unknown_parameters"


def test_draft_read_is_observable_and_list_renders():
    from actions.read_payload import OBSERVABLE_ACTIONS
    assert ACTION_DRAFT_READ in OBSERVABLE_ACTIONS


def test_draft_read_pages_by_char_and_returns_next_cursor():
    buf = DraftBuffer()
    created = buf.write("agent_a", "0123456789")
    draft_id = created["draft_id"]

    first = buf.read("agent_a", draft_id, start_char=0, max_chars=4)
    assert first["content"] == "0123"
    assert first["next_start_char"] == 4
    assert first["truncated"] is True

    second = buf.read(
        "agent_a", draft_id,
        start_char=first["next_start_char"], max_chars=4)
    assert second["content"] == "4567"
    assert second["next_start_char"] == 8


def test_draft_read_executor_uses_char_fields_and_maps_aliases():
    seen = {}

    def read(agent_id, draft_id, *, start_char, max_chars):
        seen.update(start_char=start_char, max_chars=max_chars)
        return {"ok": True, "draft_id": draft_id, "content": "x"}

    ex = DraftReadExecutor(read)
    result = ex.execute(ActionIntent(
        action_type="draft_read", agent_id="a",
        parameters={"draft_id": "d1", "start_char": 2, "max_chars": 3}))
    assert result.success is True
    assert seen == {"start_char": 2, "max_chars": 3}

    alias = ex.execute(ActionIntent(
        action_type="draft_read", agent_id="a",
        parameters={"draft_id": "d1", "offset": 4, "limit": 5}))
    assert alias.success is True
    assert seen == {"start_char": 4, "max_chars": 5}

    conflict = ex.execute(ActionIntent(
        action_type="draft_read", agent_id="a",
        parameters={"draft_id": "d1", "start_char": 1, "offset": 4}))
    assert conflict.success is False
    assert conflict.metadata["code"] == "bad_pagination"


def test_batch_promote_action_calls_once_for_multiple_files():
    calls = []

    def promote(agent_id, items):
        calls.append((agent_id, items))
        results = [{"draft_id": item["draft_id"], "path": item["path"],
                    "ok": True, "revision": 1, "chars": 5}
                   for item in items]
        return {"ok": True, "summary": "2/2 promoted", "results": results}

    ex = WorkspacePromoteDraftsExecutor(promote)
    result = ex.execute(ActionIntent(
        action_type=ACTION_WORKSPACE_PROMOTE_DRAFTS, agent_id="a",
        parameters={"items": [
            {"draft_id": "d1", "path": "src/a.js", "write_mode": "create"},
            {"draft_id": "d2", "path": "src/b.js", "write_mode": "create"},
        ]}))
    assert result.success is True
    assert len(result.output["results"]) == 2
    assert result.output["summary"] == "2/2 promoted"
    assert calls[0][0] == "a"
    assert len(calls[0][1]) == 2


def test_batch_promote_requires_mode_and_reports_partial_failure():
    ex = WorkspacePromoteDraftsExecutor(lambda *a: {"ok": True})
    bad = ex.execute(ActionIntent(
        action_type=ACTION_WORKSPACE_PROMOTE_DRAFTS, agent_id="a",
        parameters={"items": [{"draft_id": "d1", "path": "a.js"}]}))
    assert bad.success is False
    assert bad.metadata["code"] == "bad_write_mode"

    def partial(agent_id, items):
        return {"ok": False, "summary": "1/2 promoted",
                "results": [{"ok": True}, {"ok": False}]}

    result = WorkspacePromoteDraftsExecutor(partial).execute(ActionIntent(
        action_type=ACTION_WORKSPACE_PROMOTE_DRAFTS, agent_id="a",
        parameters={"items": [
            {"draft_id": "d1", "path": "a.js", "write_mode": "create"},
            {"draft_id": "d2", "path": "b.js", "write_mode": "create"},
        ]}))
    assert result.success is False
    assert result.output["summary"] == "1/2 promoted"
    assert len(result.output["results"]) == 2


def test_runner_promotes_multiple_files_in_one_call():
    from demand.runner import DemandRunner
    from tests.workspace_harness import WorkspaceHarness

    h = WorkspaceHarness.in_memory(participants={"W1": {"A"}})
    gw = h.gateway()
    gw.ensure_workspace("W1", "A")
    gw.create("W1", "/", "src", "directory", "A")
    task = SimpleNamespace(workspace=SimpleNamespace(workspace_id="W1"))
    buf = DraftBuffer()
    d1 = buf.write("A", "export const a = 1;\n")
    d2 = buf.write("A", "export const b = 2;\n")
    fake = SimpleNamespace(
        _status_task=lambda: task,
        drafts=buf,
        rt=SimpleNamespace(workspace_gateway=gw),
    )
    result = DemandRunner._promote_drafts(fake, "A", [
        {"draft_id": d1["draft_id"], "path": "src/a.js",
         "write_mode": "create"},
        {"draft_id": d2["draft_id"], "path": "src/b.js",
         "write_mode": "create"},
    ])
    assert result["ok"] is True
    assert result["summary"].startswith("2/2")
    assert gw.read("W1", "/src/a.js", "A").success
    assert gw.read("W1", "/src/b.js", "A").success
    assert gw.read("W1", "/src/a.js", "A").result["content"].startswith("export")


def test_runner_promote_partial_keeps_successful_file_and_reports_failure():
    from demand.runner import DemandRunner
    from tests.workspace_harness import WorkspaceHarness

    h = WorkspaceHarness.in_memory(participants={"W1": {"A"}})
    gw = h.gateway()
    gw.ensure_workspace("W1", "A")
    gw.create("W1", "/", "src", "directory", "A")
    task = SimpleNamespace(workspace=SimpleNamespace(workspace_id="W1"))
    buf = DraftBuffer()
    good = buf.write("A", "ok")
    fake = SimpleNamespace(
        _status_task=lambda: task,
        drafts=buf,
        rt=SimpleNamespace(workspace_gateway=gw),
    )
    result = DemandRunner._promote_drafts(fake, "A", [
        {"draft_id": good["draft_id"], "path": "src/good.js",
         "write_mode": "create"},
        {"draft_id": "missing", "path": "src/bad.js",
         "write_mode": "create"},
    ])
    assert result["ok"] is False
    assert "1/2" in result["summary"]
    assert gw.read("W1", "/src/good.js", "A").success
    assert not gw.read("W1", "/src/bad.js", "A").success


def test_dispatcher_registers_drafts_only_with_dependencies():
    from core.task import Task
    from demand.actions import build_task_dispatcher

    task = Task.create(
        creator="agent_a", description="d", workspace_id="WS",
        task_id="t", participants=["agent_a"])
    runtime = SimpleNamespace(
        workspace_gateway=object(), communication=object())
    without = build_task_dispatcher(
        runtime, task=task, bridge=SimpleNamespace())
    assert "draft_write" not in without.action_types
    assert "workspace_promote_drafts" not in without.action_types

    buf = DraftBuffer()
    with_deps = build_task_dispatcher(
        runtime, task=task, bridge=SimpleNamespace(),
        drafts=buf, promote_drafts=lambda *a: {"ok": True})
    assert "draft_write" in with_deps.action_types
    assert "workspace_promote_drafts" in with_deps.action_types
