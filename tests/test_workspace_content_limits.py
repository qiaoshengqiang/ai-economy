"""
Workspace 单次写入/编辑的**结构上限**
====================================

真实 run `demand_795139c7`：

    A 连续 3 次 `finish_reason=length`，仍然试图一次写完整文件；
    文字压力提醒没有改变写法。

这里验证的是**写入前的硬拒绝**：

    ① 超限 `workspace_write.content` → `content_too_large`，
       且 `write_fn` **根本没被调用**；
    ② 超限 `workspace_edit.new_string` → `content_too_large`，
       且 `edit_fn` **根本没被调用**；
    ③ 未配置上限时保持旧行为（实验调用方不受影响）；
    ④ Demand Entry 的 dispatcher 真的把统一写入上限接上了。
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
from actions.workspace_edit import (                           # noqa: E402
    ACTION_WORKSPACE_EDIT,
    WorkspaceEditExecutor,
)
from actions.workspace_write import (                          # noqa: E402
    ACTION_WORKSPACE_WRITE,
    WorkspaceWriteExecutor,
)


def _write_intent(content: str, mode: str = "create") -> ActionIntent:
    return ActionIntent(
        action_type=ACTION_WORKSPACE_WRITE,
        parameters={"path": "src/main.js", "content": content,
                    "write_mode": mode},
        agent_id="agent_a")


def _edit_intent(new_string: str) -> ActionIntent:
    return ActionIntent(
        action_type=ACTION_WORKSPACE_EDIT,
        parameters={"path": "src/main.js", "old_string": "// TODO",
                    "new_string": new_string},
        agent_id="agent_a")


# ------------------------------------------------------------
# workspace_write
# ------------------------------------------------------------
def test_oversized_write_is_rejected_before_write_fn_is_called():
    calls = []

    def write_fn(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True}

    ex = WorkspaceWriteExecutor(write_fn, max_content_chars=100)
    result = ex.execute(_write_intent("x" * 101))

    assert result.success is False
    assert result.metadata["code"] == "content_too_large"
    assert result.metadata["chars"] == 101
    assert result.metadata["max_chars"] == 100
    assert result.output is None
    assert calls == []                      # 🔒 没有写入发生
    assert "没有写入" in result.error


def test_content_at_the_limit_is_allowed():
    calls = []

    def write_fn(*args, **kwargs):
        calls.append(1)
        return {"ok": True, "path": "src/main.js", "mode": "create",
                "revision": 1, "chars": 100}

    ex = WorkspaceWriteExecutor(write_fn, max_content_chars=100)
    result = ex.execute(_write_intent("x" * 100))

    assert result.success is True
    assert calls == [1]


def test_append_chunk_is_limited_not_the_resulting_file():
    """append 只需输出新增片段；上限应作用于这一片，而不是旧文件。"""
    calls = []

    def write_fn(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "path": "src/main.js", "mode": "append",
                "revision": 2, "appended_chars": 100}

    ex = WorkspaceWriteExecutor(write_fn, max_content_chars=100)
    ok = ex.execute(_write_intent("y" * 100, mode="append"))
    too_big = ex.execute(_write_intent("y" * 101, mode="append"))

    assert ok.success is True
    assert too_big.success is False
    assert too_big.metadata["code"] == "content_too_large"
    assert len(calls) == 1


def test_no_limit_keeps_old_behavior():
    def write_fn(*args, **kwargs):
        return {"ok": True, "path": "big.md", "mode": "create",
                "revision": 1, "chars": 50_000}

    ex = WorkspaceWriteExecutor(write_fn)
    result = ex.execute(_write_intent("x" * 50_000))
    assert result.success is True


def test_invalid_limit_is_rejected_at_construction():
    with pytest.raises(ValueError):
        WorkspaceWriteExecutor(lambda *a, **k: {"ok": True},
                               max_content_chars=0)


# ------------------------------------------------------------
# workspace_edit
# ------------------------------------------------------------
def test_oversized_edit_is_rejected_before_edit_fn_is_called():
    calls = []

    def edit_fn(*args, **kwargs):
        calls.append(1)
        return {"ok": True}

    ex = WorkspaceEditExecutor(edit_fn, max_new_string_chars=100)
    result = ex.execute(_edit_intent("z" * 101))

    assert result.success is False
    assert result.metadata["code"] == "content_too_large"
    assert result.metadata["chars"] == 101
    assert result.metadata["max_chars"] == 100
    assert calls == []
    assert "没有修改" in result.error


def test_edit_at_the_limit_is_allowed():
    calls = []

    def edit_fn(*args, **kwargs):
        calls.append(1)
        return {"ok": True, "path": "src/main.js", "revision": 2,
                "matched": 1, "replaced": 1}

    ex = WorkspaceEditExecutor(edit_fn, max_new_string_chars=100)
    result = ex.execute(_edit_intent("z" * 100))

    assert result.success is True
    assert calls == [1]


# ------------------------------------------------------------
# Demand Entry 接线
# ------------------------------------------------------------
def test_demand_dispatcher_wires_the_structural_limit():
    from core.task import Task
    from demand.actions import (
        MAX_SINGLE_WRITE_CHARS, build_task_dispatcher,
    )

    task = Task.create(
        creator="agent_a", description="d",
        workspace_id="WS-LIMIT", task_id="t_limit",
        participants=["agent_a"])
    runtime = SimpleNamespace(
        workspace_gateway=object(), communication=object())
    dispatcher = build_task_dispatcher(
        runtime, task=task, bridge=SimpleNamespace())

    write_ex = dispatcher._executors["workspace_write"]
    edit_ex = dispatcher._executors["workspace_edit"]
    read_ex = dispatcher._executors["workspace_read"]
    assert read_ex.stat_fn is not None, "Demand 读动作应启用 revision 探针缓存"
    assert write_ex.max_content_chars == MAX_SINGLE_WRITE_CHARS
    assert edit_ex.max_new_string_chars == MAX_SINGLE_WRITE_CHARS
    assert MAX_SINGLE_WRITE_CHARS == 64_000

    result = dispatcher.dispatch(
        _write_intent("x" * (MAX_SINGLE_WRITE_CHARS + 1)))
    assert result.success is False
    assert result.metadata["code"] == "content_too_large"
