"""
Demand Entry V1 的动作组合
============================

🔒 这里只做“注册 + 绑定”，不新增业务规则：
    - task_create 只在 intake 阶段可用；
    - task 阶段注册 workspace / message / acceptance / context / tool。
"""

from __future__ import annotations

from typing import Any

from actions import (
    ActionDispatcher,
    AssignAcceptanceExecutor,
    MessageSendExecutor,
    RunToolExecutor,
    SharedContextReadExecutor,
    TaskCreateExecutor,
    WorkspaceEditExecutor,
    WorkspaceReadExecutor,
    WorkspaceWriteExecutor,
)
from services.artifacts import edit_artifact, read_artifact, write_artifact
from services.assignment_ops import assign_acceptance
from services.context_ops import read_shared_context
from services.messaging import send_message_as


def build_intake_dispatcher(create_fn: Any) -> ActionDispatcher:
    return ActionDispatcher([TaskCreateExecutor(create_fn)])


def build_task_dispatcher(
    runtime: Any,
    *,
    task: Any,
    bridge: Any,
) -> ActionDispatcher:
    ws_id = task.workspace.workspace_id if task.workspace else ""
    participants = set(task.participants or [])

    def _is_participant(task_id: str, agent_id: str) -> bool:
        return task_id == task.task_id and agent_id in participants

    def _send(comm: Any, **kwargs: Any) -> Any:
        return bridge.run(send_message_as(comm, **kwargs))

    def _edit(agent_id: str, path: str, old_string: str,
              new_string: str, *, replace_all: bool = False,
              expected_revision: Any = None) -> Any:
        return edit_artifact(
            runtime.workspace_gateway, ws_id, agent_id, path,
            old_string, new_string, replace_all=replace_all,
            expected_revision=expected_revision)

    def _assign(*, by: str, to: str, reason: str = "") -> Any:
        return assign_acceptance(
            runtime.alignment,
            task.task_id,
            by=by,
            to=to,
            reason=reason,
            is_participant=_is_participant,
        )

    def _read_context(*, by: str) -> Any:
        return read_shared_context(
            runtime.alignment,
            task.task_id,
            by=by,
            is_participant=_is_participant,
        )

    def _run_tool(agent_id: str, *, tool: str, args: Any, source: str,
                  path: str, timeout_seconds: Any) -> Any:
        source = str(source or "task").strip().lower()
        if source not in ("task", "private"):
            return {
                "ran": False, "ok": False, "tool": str(tool),
                "code": "bad_source",
                "message": "source 只能是 task / private",
            }
        if source == "private":
            try:
                ws = runtime.economy.agent(agent_id).workspace
            except Exception:                                 # noqa: BLE001
                ws = None
            if ws is None:
                return {
                    "ran": False, "ok": False, "tool": str(tool),
                    "code": "no_private_workspace",
                    "message": "这个 Agent 没有私有空间",
                }
            result = runtime.tool_runner.run_from_private(
                ws, tool=tool, args=args, source_path=path,
                timeout_seconds=timeout_seconds)
        else:
            result = runtime.tool_runner.run_from_task(
                runtime.workspace_gateway, ws_id, agent_id,
                tool=tool, args=args, source_path=path,
                timeout_seconds=timeout_seconds)
        return result.to_agent_dict()

    return ActionDispatcher([
        WorkspaceReadExecutor(
            read_artifact, gateway=runtime.workspace_gateway,
            workspace_id=ws_id),
        WorkspaceWriteExecutor(
            write_artifact, gateway=runtime.workspace_gateway,
            workspace_id=ws_id),
        WorkspaceEditExecutor(_edit),
        MessageSendExecutor(_send, comm=runtime.communication),
        AssignAcceptanceExecutor(_assign),
        SharedContextReadExecutor(_read_context),
        RunToolExecutor(_run_tool),
    ])
