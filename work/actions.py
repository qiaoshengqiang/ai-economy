"""
Work 层动作组合
================

只注册**具体工作**需要的动作：

    Workspace · Draft · Tool Runtime · Resource Lock ·
    Private / Shared Working Memory · Short / Long-Term Memory ·
    work_complete

🔒 刻意不注册：

    · message_send           Work 层没有 Agent 间消息
    · task_create            Task 已由 Team Lead 创建
    · assign_acceptance      Team 验收走原有执行服务；Solo 自己完成
    · public_query           生活层公开信息查询
    · public_working_memory  每任务强制公共 WM 属于旧重框架
    · wallet / price / 经济  无
"""

from __future__ import annotations

from typing import Any, List, Optional

from actions import (
    ActionDispatcher,
    WorkItemBlockedExecutor,
    WorkItemDoneExecutor,
    WorkQaVerdictExecutor,
    DraftAppendExecutor,
    DraftDeleteExecutor,
    DraftListExecutor,
    DraftReadExecutor,
    DraftWriteExecutor,
    LongTermMemoryConsolidateExecutor,
    LongTermMemoryMarkUsedExecutor,
    LongTermMemoryReadExecutor,
    LongTermMemorySearchExecutor,
    PrivateDeleteExecutor,
    PrivateListExecutor,
    PrivateReadExecutor,
    PrivateWorkingMemoryReadExecutor,
    PrivateWorkingMemoryUpdateExecutor,
    PrivateWriteExecutor,
    ResourceCreateExecutor,
    ResourceHandoffExecutor,
    ResourceReleaseExecutor,
    RunToolExecutor,
    SharedContextReadExecutor,
    ShortTermMemoryReadExecutor,
    ShortTermMemoryReinforceExecutor,
    ShortTermMemoryUpdateExecutor,
    WorkCompleteExecutor,
    WorkspaceEditExecutor,
    WorkspacePromoteDraftsExecutor,
    WorkspaceReadExecutor,
    WorkspaceSearchExecutor,
    WorkspaceWriteExecutor,
)
from core.write_limits import MAX_SINGLE_WRITE_CHARS
from services.artifacts import (
    edit_artifact, read_artifact, search_artifacts, stat_artifact,
    write_artifact,
)
from services.context_ops import read_shared_context
from services.private_workspace import (
    delete_private, list_private, read_private, write_private,
)
from services.resource_control import (
    create_directory, handoff_resource, release_resource,
)

#: Work 层注册的动作类型（供 guard / 测试检查）。
WORK_ACTION_TYPES = frozenset({
    "workspace_read", "workspace_search", "workspace_write",
    "workspace_edit", "draft_write", "draft_append", "draft_read",
    "draft_list", "draft_delete", "workspace_promote_drafts",
    "run_tool", "resource_create", "resource_handoff", "resource_release",
    "shared_context_read", "private_working_memory_read",
    "private_working_memory_update", "short_term_memory_read",
    "short_term_memory_update", "short_term_memory_reinforce",
    "long_term_memory_search", "long_term_memory_read",
    "long_term_memory_mark_used", "long_term_memory_consolidate",
    "private_write", "private_read", "private_list", "private_delete",
    "work_item_done", "work_item_blocked", "work_qa_verdict",
    "work_complete",
})

#: Work 层**禁止**出现的动作（由测试直接断言）。
FORBIDDEN_WORK_ACTION_TYPES = frozenset({
    "message_send", "task_create", "assign_acceptance", "public_query",
    "public_working_memory_read", "public_working_memory_update",
})


def build_work_dispatcher(
    runtime: Any,
    *,
    task: Any,
    drafts: Any,
    promote_drafts: Any,
    complete_fn: Any = None,
    item_done_fn: Any = None,
    item_blocked_fn: Any = None,
    qa_verdict_fn: Any = None,
    include_complete: bool = True,
    extra_executors: Optional[List[Any]] = None,
) -> ActionDispatcher:
    """
    绑定 Work 层的动作集合。

    全部依赖通过参数注入；本模块不创建 Workspace / Store / ToolRunner。
    """
    workspace_id = task.workspace.workspace_id if task.workspace else ""
    current_task_id = task.task_id
    participants = set(task.participants or [])

    def _is_participant(tid: str, agent_id: str) -> bool:
        return tid == current_task_id and agent_id in participants

    def _edit(agent_id: str, path: str, old_string: str,
              new_string: str, *, replace_all: bool = False,
              expected_revision: Any = None) -> Any:
        return edit_artifact(
            runtime.workspace_gateway, workspace_id, agent_id, path,
            old_string, new_string, replace_all=replace_all,
            expected_revision=expected_revision)

    def _search(agent_id: str, query: str, **kwargs: Any) -> Any:
        return search_artifacts(
            runtime.workspace_gateway, workspace_id, agent_id, query,
            path=kwargs.get("path", ""),
            glob=kwargs.get("glob", ""),
            max_results=kwargs.get("max_results", 20),
            case_sensitive=kwargs.get("case_sensitive", True),
            context_lines=kwargs.get("context_lines", 0),
        )

    def _read_context(*, by: str) -> Any:
        return read_shared_context(
            runtime.alignment, current_task_id, by=by,
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
            workspace = _ws_of(agent_id)
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
                runtime.workspace_gateway, workspace_id, agent_id,
                tool=tool, args=args, source_path=path,
                timeout_seconds=timeout_seconds)
        return result.to_agent_dict()

    def _ws_of(agent_id: str) -> Any:
        try:
            return runtime.economy.agent(str(agent_id)).workspace
        except Exception:                                     # noqa: BLE001
            return None

    def _pwm_read(agent_id: str) -> Any:
        return runtime.private_working_memory.read(agent_id, current_task_id)

    def _pwm_update(agent_id: str, **kwargs: Any) -> Any:
        return runtime.private_working_memory.update(
            agent_id, current_task_id, **kwargs)

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
            agent_id, entry_id, task_id=current_task_id)

    def _ltm_mark_used(agent_id: str, entry_ids: Any) -> Any:
        return [e.to_dict() for e in
                runtime.private_long_term_memory.mark_used(
                    agent_id, entry_ids, current_task_id)]

    def _ltm_consolidate(agent_id: str, **kwargs: Any) -> Any:
        return runtime.private_long_term_memory.consolidate(
            agent_id, task_id=current_task_id, **kwargs)

    def _resource_create(agent_id: str, path: str) -> Any:
        return create_directory(
            runtime.workspace_gateway, workspace_id, agent_id, path)

    def _resource_handoff(agent_id: str, path: str, to: Any,
                          reason: str = "", task_id: Any = None) -> Any:
        return handoff_resource(
            runtime.workspace_gateway, workspace_id, agent_id, path, to,
            reason=reason, task_id=task_id or current_task_id)

    def _resource_release(agent_id: str, path: str) -> Any:
        return release_resource(
            runtime.workspace_gateway, workspace_id, agent_id, path)

    executors: List[Any] = [
        WorkspaceReadExecutor(
            read_artifact, gateway=runtime.workspace_gateway,
            workspace_id=workspace_id, stat_fn=stat_artifact),
        WorkspaceSearchExecutor(_search),
        WorkspaceWriteExecutor(
            write_artifact, gateway=runtime.workspace_gateway,
            workspace_id=workspace_id,
            max_content_chars=MAX_SINGLE_WRITE_CHARS),
        WorkspaceEditExecutor(
            _edit, max_new_string_chars=MAX_SINGLE_WRITE_CHARS),
        DraftWriteExecutor(drafts.write),
        DraftAppendExecutor(drafts.append),
        DraftReadExecutor(drafts.read),
        DraftListExecutor(drafts.list),
        DraftDeleteExecutor(drafts.delete),
        WorkspacePromoteDraftsExecutor(promote_drafts),
        RunToolExecutor(_run_tool),
        ResourceCreateExecutor(_resource_create),
        ResourceHandoffExecutor(_resource_handoff),
        ResourceReleaseExecutor(_resource_release),
        SharedContextReadExecutor(_read_context),
        PrivateWorkingMemoryReadExecutor(_pwm_read),
        PrivateWorkingMemoryUpdateExecutor(_pwm_update),
        ShortTermMemoryReadExecutor(_stm_read),
        ShortTermMemoryUpdateExecutor(_stm_update),
        ShortTermMemoryReinforceExecutor(_stm_reinforce),
        LongTermMemorySearchExecutor(_ltm_search),
        LongTermMemoryReadExecutor(_ltm_read),
        LongTermMemoryMarkUsedExecutor(_ltm_mark_used),
        LongTermMemoryConsolidateExecutor(_ltm_consolidate),
        PrivateWriteExecutor(write_private, workspace_of=_ws_of),
        PrivateReadExecutor(read_private, workspace_of=_ws_of),
        PrivateListExecutor(list_private, workspace_of=_ws_of),
        PrivateDeleteExecutor(delete_private, workspace_of=_ws_of),
    ]
    if item_done_fn is not None:
        executors.append(WorkItemDoneExecutor(item_done_fn))
    if item_blocked_fn is not None:
        executors.append(WorkItemBlockedExecutor(item_blocked_fn))
    if qa_verdict_fn is not None:
        executors.append(WorkQaVerdictExecutor(qa_verdict_fn))
    if include_complete:
        if complete_fn is None:
            raise ValueError(
                "build_work_dispatcher(include_complete=True) 需要 complete_fn")
        executors.append(WorkCompleteExecutor(complete_fn))
    if extra_executors:
        executors.extend(extra_executors)
    return ActionDispatcher(executors)
