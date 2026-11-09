"""
Demand Entry V1 的动作组合
============================

🔒 这里只做“注册 + 绑定”，不新增业务规则：
    - task_create 只在 intake 阶段可用；
    - task 阶段注册 workspace / message / acceptance / context / tool；
    - 工作记忆 / 短期 / 长期 / 私有空间 / 公共资料 / 资源责任
      与 production.py 的实验动作集对齐。

工作记忆是 **Agent 自己的抽象记录**；这里只提供读写入口，
不自动写、不自动总结、不把 search 结果塞进记忆。
"""

from __future__ import annotations

from typing import Any

from core.write_limits import MAX_SINGLE_WRITE_CHARS
from actions import (
    ActionDispatcher,
    AssignAcceptanceExecutor,
    LongTermMemoryConsolidateExecutor,
    LongTermMemoryMarkUsedExecutor,
    LongTermMemoryReadExecutor,
    LongTermMemorySearchExecutor,
    MessageSendExecutor,
    PrivateDeleteExecutor,
    PrivateListExecutor,
    PrivateReadExecutor,
    PrivateWorkingMemoryReadExecutor,
    PrivateWorkingMemoryUpdateExecutor,
    PrivateWriteExecutor,
    PublicQueryExecutor,
    PublicWorkingMemoryReadExecutor,
    PublicWorkingMemoryUpdateExecutor,
    ResourceCreateExecutor,
    ResourceHandoffExecutor,
    ResourceReleaseExecutor,
    RunToolExecutor,
    SharedContextReadExecutor,
    ShortTermMemoryReadExecutor,
    ShortTermMemoryReinforceExecutor,
    ShortTermMemoryUpdateExecutor,
    TaskCreateExecutor,
    WorkspaceEditExecutor,
    WorkspacePlanCheckpointExecutor,
    WorkspacePlanUpdateExecutor,
    WorkspaceReadExecutor,
    WorkspaceSearchExecutor,
    WorkspaceWriteExecutor,
    DraftAppendExecutor,
    DraftDeleteExecutor,
    DraftListExecutor,
    DraftReadExecutor,
    DraftWriteExecutor,
    WorkspacePromoteDraftsExecutor,
)
from services.artifacts import (
    edit_artifact, read_artifact, search_artifacts, stat_artifact,
    write_artifact,
)
from services.assignment_ops import assign_acceptance
from services.context_ops import read_shared_context
from services.messaging import send_message_as
from services.private_workspace import (
    delete_private, list_private, read_private, write_private,
)
from services.public_reference import PublicReference
from services.resource_control import (
    create_directory, handoff_resource, release_resource,
)
from demand.submit_check import (
    check_submit_deliverable, format_submit_check_failure,
)


def build_intake_dispatcher(create_fn: Any) -> ActionDispatcher:
    return ActionDispatcher([TaskCreateExecutor(create_fn)])


#: 🔒 单次写入/编辑结构上限（字符数）。
#:
#: 真实 run `demand_795139c7`：Agent 3 次 `finish_reason=length`，
#: 仍然试图一次写完整文件。文字提醒没有改变行为 —— 这里把它变成
#: **写入前的硬拒绝**：超限的 Action 不会落盘，Agent 必须拆成
#: create + append / workspace_edit。
#:
#: 具体数值在 core/write_limits.py —— Work 层共用同一个上限。


def build_task_dispatcher(
    runtime: Any,
    *,
    task: Any,
    bridge: Any,
    update_plan: Any = None,
    record_checkpoint: Any = None,
    drafts: Any = None,
    promote_drafts: Any = None,
) -> ActionDispatcher:
    ws_id = task.workspace.workspace_id if task.workspace else ""
    task_id = task.task_id
    current_task_id = task.task_id
    participants = set(task.participants or [])
    public = PublicReference()

    def _is_participant(tid: str, agent_id: str) -> bool:
        return tid == task_id and agent_id in participants

    def _send(comm: Any, **kwargs: Any) -> Any:
        message_type = str(kwargs.get("message_type") or "").strip().upper()
        from_agent = str(kwargs.get("from_agent") or "")
        if (message_type == "SUBMIT"
                and task.contractor
                and from_agent == task.contractor):
            try:
                report = check_submit_deliverable(
                    runtime.workspace_gateway, ws_id, from_agent)
            except Exception as exc:                          # noqa: BLE001
                return {
                    "ok": False,
                    "to": str(kwargs.get("to_agent") or ""),
                    "message_type": "SUBMIT",
                    "code": "submit_check_error",
                    "message": (f"SUBMIT 前的 DOM/import 检查自身失败："
                                f"{type(exc).__name__}: {exc}"),
                }
            if not report.get("ok"):
                return {
                    "ok": False,
                    "to": str(kwargs.get("to_agent") or ""),
                    "message_type": "SUBMIT",
                    "code": "submit_check_failed",
                    "message": format_submit_check_failure(report),
                }
        return bridge.run(send_message_as(comm, **kwargs))

    def _edit(agent_id: str, path: str, old_string: str,
              new_string: str, *, replace_all: bool = False,
              expected_revision: Any = None) -> Any:
        return edit_artifact(
            runtime.workspace_gateway, ws_id, agent_id, path,
            old_string, new_string, replace_all=replace_all,
            expected_revision=expected_revision)

    def _search(agent_id: str, query: str, **kwargs: Any) -> Any:
        return search_artifacts(
            runtime.workspace_gateway, ws_id, agent_id, query,
            path=kwargs.get("path", ""),
            glob=kwargs.get("glob", ""),
            max_results=kwargs.get("max_results", 20),
            case_sensitive=kwargs.get("case_sensitive", True),
            context_lines=kwargs.get("context_lines", 0),
        )

    def _assign(*, by: str, to: str, reason: str = "") -> Any:
        return assign_acceptance(
            runtime.alignment, task_id, by=by, to=to, reason=reason,
            is_participant=_is_participant)

    def _read_context(*, by: str) -> Any:
        return read_shared_context(
            runtime.alignment, task_id, by=by,
            is_participant=_is_participant)

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
                workspace = runtime.economy.agent(agent_id).workspace
            except Exception:                                 # noqa: BLE001
                workspace = None
            if workspace is None:
                return {
                    "ran": False, "ok": False, "tool": str(tool),
                    "code": "no_private_workspace",
                    "message": "这个 Agent 没有私有空间",
                }
            result = runtime.tool_runner.run_from_private(
                workspace, tool=tool, args=args, source_path=path,
                timeout_seconds=timeout_seconds)
        else:
            result = runtime.tool_runner.run_from_task(
                runtime.workspace_gateway, ws_id, agent_id,
                tool=tool, args=args, source_path=path,
                timeout_seconds=timeout_seconds)
        return result.to_agent_dict()

    def _ws_of(agent_id: str) -> Any:
        try:
            return runtime.economy.agent(str(agent_id)).workspace
        except Exception:                                     # noqa: BLE001
            return None

    def _wm_read(agent_id: str) -> Any:
        return runtime.public_working_memory.read(task_id, agent_id)

    def _wm_update(agent_id: str, **kwargs: Any) -> Any:
        return runtime.public_working_memory.update(
            task_id, agent_id, **kwargs)

    def _pwm_read(agent_id: str) -> Any:
        return runtime.private_working_memory.read(agent_id, task_id)

    def _pwm_update(agent_id: str, **kwargs: Any) -> Any:
        return runtime.private_working_memory.update(
            agent_id, task_id, **kwargs)

    def _stm_read(agent_id: str) -> Any:
        return runtime.private_short_term_memory.read(agent_id)

    def _stm_update(agent_id: str, **kwargs: Any) -> Any:
        return runtime.private_short_term_memory.update(agent_id, **kwargs)

    def _stm_reinforce(agent_id: str, **kwargs: Any) -> Any:
        return runtime.private_short_term_memory.reinforce(agent_id, **kwargs)

    def _ltm_search(agent_id: str, query: str) -> Any:
        return [e.to_dict() for e in
                runtime.private_long_term_memory.search(agent_id, query)]

    def _ltm_read(agent_id: str, entry_id: str) -> Any:
        return runtime.private_long_term_memory.read(
            agent_id, entry_id, task_id=task_id)

    def _ltm_mark_used(agent_id: str, entry_ids: Any) -> Any:
        return [e.to_dict() for e in
                runtime.private_long_term_memory.mark_used(
                    agent_id, entry_ids, task_id)]

    def _ltm_consolidate(agent_id: str, **kwargs: Any) -> Any:
        return runtime.private_long_term_memory.consolidate(
            agent_id, task_id=task_id, **kwargs)

    def _resource_create(agent_id: str, path: str) -> Any:
        return create_directory(
            runtime.workspace_gateway, ws_id, agent_id, path)

    def _resource_handoff(agent_id: str, path: str, to: Any,
                          reason: str = "", task_id: Any = None) -> Any:
        return handoff_resource(
            runtime.workspace_gateway, ws_id, agent_id, path, to,
            reason=reason, task_id=task_id or current_task_id)

    def _resource_release(agent_id: str, path: str) -> Any:
        return release_resource(
            runtime.workspace_gateway, ws_id, agent_id, path)

    return ActionDispatcher([
        WorkspaceReadExecutor(
            read_artifact, gateway=runtime.workspace_gateway,
            workspace_id=ws_id, stat_fn=stat_artifact),
        WorkspaceSearchExecutor(_search),
        WorkspaceWriteExecutor(
            write_artifact, gateway=runtime.workspace_gateway,
            workspace_id=ws_id,
            max_content_chars=MAX_SINGLE_WRITE_CHARS),
        WorkspaceEditExecutor(
            _edit, max_new_string_chars=MAX_SINGLE_WRITE_CHARS),
        *([WorkspacePlanUpdateExecutor(update_plan)]
          if update_plan is not None else []),
        *([WorkspacePlanCheckpointExecutor(record_checkpoint)]
          if record_checkpoint is not None else []),
        *([DraftWriteExecutor(drafts.write),
           DraftAppendExecutor(drafts.append),
           DraftReadExecutor(drafts.read),
           DraftListExecutor(drafts.list),
           DraftDeleteExecutor(drafts.delete)]
          if drafts is not None else []),
        *([WorkspacePromoteDraftsExecutor(promote_drafts)]
          if promote_drafts is not None else []),
        MessageSendExecutor(_send, comm=runtime.communication),
        PublicQueryExecutor(public.query),
        PrivateWriteExecutor(write_private, workspace_of=_ws_of),
        PrivateReadExecutor(read_private, workspace_of=_ws_of),
        PrivateListExecutor(list_private, workspace_of=_ws_of),
        PrivateDeleteExecutor(delete_private, workspace_of=_ws_of),
        RunToolExecutor(_run_tool),
        ResourceCreateExecutor(_resource_create),
        ResourceHandoffExecutor(_resource_handoff),
        ResourceReleaseExecutor(_resource_release),
        AssignAcceptanceExecutor(_assign),
        SharedContextReadExecutor(_read_context),
        PublicWorkingMemoryReadExecutor(_wm_read),
        PublicWorkingMemoryUpdateExecutor(_wm_update),
        PrivateWorkingMemoryReadExecutor(_pwm_read),
        PrivateWorkingMemoryUpdateExecutor(_pwm_update),
        ShortTermMemoryReadExecutor(_stm_read),
        ShortTermMemoryUpdateExecutor(_stm_update),
        ShortTermMemoryReinforceExecutor(_stm_reinforce),
        LongTermMemorySearchExecutor(_ltm_search),
        LongTermMemoryReadExecutor(_ltm_read),
        LongTermMemoryMarkUsedExecutor(_ltm_mark_used),
        LongTermMemoryConsolidateExecutor(_ltm_consolidate),
    ])
