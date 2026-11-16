"""
Front Agent 自己的动作集合
==========================

Front 不是 Work Agent，但需要在自己的私有空间里准备文件/草稿：

    private_read / private_write / private_list / private_delete
    draft_read / draft_write / draft_append / draft_list / draft_delete

并且可以**只读**用户当前选中的项目目录：

    project_list / project_read

🔒 这里**没有** Task / Workspace 写入 / run_tool：
   · Front 不创建 Task；
   · Front 不写 Task Workspace；
   · project_* 只读，且只允许访问当前 `workdir`；
   · 代码 / 长文本先放私有空间或草稿，再随 handoff 的
     `brief.files` 交给 Work Agent。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from actions import (
    ActionDispatcher,
    ActionIntent,
    ActionResult,
    DraftAppendExecutor,
    DraftDeleteExecutor,
    DraftListExecutor,
    DraftReadExecutor,
    DraftWriteExecutor,
    PrivateDeleteExecutor,
    PrivateListExecutor,
    PrivateReadExecutor,
    PrivateWriteExecutor,
)
from services.private_workspace import (
    delete_private, list_private, read_private, write_private,
)
from work.project_context import list_project, read_project_file


FRONT_ACTION_TYPES = frozenset({
    "artifact_read",
    "project_list", "project_read",
    "web_search", "web_fetch",
    "private_read", "private_write", "private_list", "private_delete",
    "draft_read", "draft_write", "draft_append", "draft_list",
    "draft_delete",
    "alarm_create", "alarm_list", "alarm_cancel",
    "short_term_memory_add", "short_term_memory_update",
    "short_term_memory_reinforce",
    "long_term_memory_search", "long_term_memory_read",
    "long_term_memory_consolidate",
})


class FrontWebSearchExecutor:
    """`web_search` —— 只读外部事实，不写 Task / Workspace。"""

    action_types = frozenset({"web_search"})

    def __init__(self, search_fn: Callable[..., Any]) -> None:
        self.search_fn = search_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        query = str(params.get("query") or "").strip()
        if not query:
            return ActionResult(
                success=False,
                error="web_search 需要 query",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "bad_request"},
            )
        try:
            raw = self.search_fn(
                query,
                count=int(params.get("count") or 5),
                need_content=bool(params.get("need_content", False)),
                ori_question=str(params.get("ori_question") or ""),
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "search_failed"},
            )
        if not isinstance(raw, dict):
            raw = {"results": raw}
        return ActionResult(
            success=True,
            output=raw,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontWebFetchExecutor:
    action_types = frozenset({"web_fetch"})

    def __init__(self, fetch_fn: Callable[..., Any]) -> None:
        self.fetch_fn = fetch_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        url = str(params.get("url") or "").strip()
        if not url:
            return ActionResult(
                success=False,
                error="web_fetch 需要 url",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "bad_request"},
            )
        try:
            raw = self.fetch_fn(url)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "fetch_failed"},
            )
        return ActionResult(
            success=True,
            output=raw if isinstance(raw, dict) else {"content": str(raw)},
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontArtifactReadExecutor:
    """`artifact_read` —— 只读一个 Task Workspace 里的文件内容。"""

    action_types = frozenset({"artifact_read"})

    def __init__(self, read_fn: Callable[..., Any]) -> None:
        self.read_fn = read_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        task_id = str(params.get("task_id") or "").strip()
        path = str(params.get("path") or "").strip()
        if not task_id or not path:
            return ActionResult(
                success=False,
                error="artifact_read 需要 task_id 和 path",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "bad_request"},
            )
        try:
            raw = self.read_fn(
                task_id, path,
                offset=params.get("offset"),
                limit=params.get("limit"))
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "read_failed"},
            )
        if not isinstance(raw, dict):
            return ActionResult(
                success=False,
                error="artifact_read 返回的不是对象",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "invalid_result"},
            )
        if not raw.get("ok"):
            return ActionResult(
                success=False,
                output=raw,
                error=str(raw.get("message") or "artifact_read 失败"),
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": str(raw.get("code") or "unknown")},
            )
        return ActionResult(
            success=True,
            output=dict(raw),
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontProjectListExecutor:
    """`project_list` —— 只读当前项目目录。"""

    action_types = frozenset({"project_list"})

    def __init__(self, project_root: str) -> None:
        self.project_root = str(project_root or "")

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        try:
            raw = list_project(
                self.project_root,
                path=params.get("path") or "",
                depth=params.get("depth") or 1,
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "project_list_failed"},
            )
        return ActionResult(
            success=True,
            output=raw,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontProjectReadExecutor:
    """`project_read` —— 只读当前项目目录里的一个文本文件。"""

    action_types = frozenset({"project_read"})

    def __init__(self, project_root: str) -> None:
        self.project_root = str(project_root or "")

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "").strip()
        if not path:
            return ActionResult(
                success=False,
                error="project_read 需要 path",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "bad_request"},
            )
        try:
            raw = read_project_file(
                self.project_root,
                path,
                offset=params.get("offset"),
                limit=params.get("limit"),
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "project_read_failed"},
            )
        return ActionResult(
            success=True,
            output=raw,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontAlarmCreateExecutor:
    """`alarm_create` —— Front 给自己创建一个持久化闹钟。"""

    action_types = frozenset({"alarm_create"})

    def __init__(self, alarm_service: Any) -> None:
        self.alarm_service = alarm_service

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        if self.alarm_service is None:
            return ActionResult(
                success=False, error="alarm_service 未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "alarm_disabled"},
            )
        try:
            result = self.alarm_service.create(
                agent_id=intent.agent_id,
                reason=params.get("reason") or "",
                instruction=params.get("instruction") or "",
                task_ids=params.get("task_ids") or params.get("task_id") or [],
                schedule=params.get("schedule") or "",
                interval_seconds=params.get("interval_seconds"),
                delay_seconds=params.get("delay_seconds"),
                at=str(params.get("at") or ""),
                max_runs=params.get("max_runs"),
                expires_at=str(params.get("expires_at") or ""),
                expires_in_seconds=params.get("expires_in_seconds"),
                dedupe_key=str(params.get("dedupe_key") or ""),
                source_message_id=str(
                    params.get("source_message_id") or ""),
                conversation_id=str(params.get("conversation_id") or ""),
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "alarm_create_failed"},
            )
        return ActionResult(
            success=True, output=result,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontAlarmListExecutor:
    action_types = frozenset({"alarm_list"})

    def __init__(self, alarm_service: Any) -> None:
        self.alarm_service = alarm_service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.alarm_service is None:
            return ActionResult(
                success=False, error="alarm_service 未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "alarm_disabled"},
            )
        params = dict(getattr(intent, "parameters", None) or {})
        try:
            alarms = self.alarm_service.list(
                intent.agent_id,
                include_inactive=bool(params.get("include_inactive", False)),
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "alarm_list_failed"},
            )
        return ActionResult(
            success=True, output={"alarms": alarms},
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontAlarmCancelExecutor:
    action_types = frozenset({"alarm_cancel"})

    def __init__(self, alarm_service: Any) -> None:
        self.alarm_service = alarm_service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.alarm_service is None:
            return ActionResult(
                success=False, error="alarm_service 未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "alarm_disabled"},
            )
        params = dict(getattr(intent, "parameters", None) or {})
        alarm_id = str(params.get("alarm_id") or "").strip()
        if not alarm_id:
            return ActionResult(
                success=False, error="alarm_cancel 需要 alarm_id",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "bad_request"},
            )
        try:
            ok = self.alarm_service.cancel(intent.agent_id, alarm_id)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "alarm_cancel_failed"},
            )
        return ActionResult(
            success=bool(ok),
            output={"alarm_id": alarm_id, "cancelled": bool(ok)},
            error=None if ok else "闹钟不存在或不属于当前 Agent",
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None if ok else "not_found"},
        )


class FrontShortTermMemoryAddExecutor:
    """`short_term_memory_add` —— 记住一条跨对话的稳定事实。"""

    action_types = frozenset({"short_term_memory_add"})

    def __init__(self, service: Any) -> None:
        self.service = service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.service is None:
            return ActionResult(
                success=False, error="记忆服务未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "memory_disabled"})
        params = dict(getattr(intent, "parameters", None) or {})
        try:
            entry = self.service.add(
                intent.agent_id,
                summary=str(params.get("summary") or ""),
                architecture=str(params.get("architecture") or ""),
                notable=str(params.get("notable") or ""),
                importance=params.get("importance", 3),
            )
            memory = self.service.read(intent.agent_id)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "short_term_memory_add_failed"})
        return ActionResult(
            success=True,
            output={
                "entry": entry.to_dict(),
                "revision": int(memory.revision),
                "entry_count": len(memory.entries),
                "needs_compaction": bool(memory.needs_compaction),
            },
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontShortTermMemoryUpdateExecutor:
    action_types = frozenset({"short_term_memory_update"})

    def __init__(self, service: Any) -> None:
        self.service = service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.service is None:
            return ActionResult(
                success=False, error="记忆服务未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "memory_disabled"})
        params = dict(getattr(intent, "parameters", None) or {})
        expected = params.get("expected_revision")
        entry_id = str(params.get("entry_id") or "").strip()
        if expected is None:
            return ActionResult(
                success=False, error="需要 expected_revision",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_expected_revision"})
        if not entry_id:
            return ActionResult(
                success=False, error="需要 entry_id",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_entry_id"})
        try:
            memory = self.service.update(
                intent.agent_id,
                expected_revision=int(expected),
                entry_id=entry_id,
                summary=params.get("summary"),
                architecture=params.get("architecture"),
                notable=params.get("notable"),
                importance=params.get("importance"),
                remove=bool(params.get("remove", False)),
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "short_term_memory_update_failed"})
        return ActionResult(
            success=True, output=memory.to_dict(),
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontShortTermMemoryReinforceExecutor:
    action_types = frozenset({"short_term_memory_reinforce"})

    def __init__(self, service: Any) -> None:
        self.service = service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.service is None:
            return ActionResult(
                success=False, error="记忆服务未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "memory_disabled"})
        params = dict(getattr(intent, "parameters", None) or {})
        expected = params.get("expected_revision")
        entry_id = str(params.get("entry_id") or "").strip()
        if expected is None or not entry_id:
            return ActionResult(
                success=False,
                error="需要 expected_revision + entry_id",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_required_field"})
        try:
            memory = self.service.reinforce(
                intent.agent_id,
                expected_revision=int(expected),
                entry_id=entry_id,
                reason=str(params.get("reason") or ""),
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "short_term_memory_reinforce_failed"})
        return ActionResult(
            success=True, output=memory.to_dict(),
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontLongTermMemorySearchExecutor:
    action_types = frozenset({"long_term_memory_search"})

    def __init__(self, service: Any) -> None:
        self.service = service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.service is None:
            return ActionResult(
                success=False, error="记忆服务未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "memory_disabled"})
        params = dict(getattr(intent, "parameters", None) or {})
        try:
            entries = self.service.search(
                intent.agent_id, str(params.get("query") or ""))
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "long_term_memory_search_failed"})
        return ActionResult(
            success=True,
            output={"entries": [entry.to_dict() for entry in entries]},
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontLongTermMemoryReadExecutor:
    action_types = frozenset({"long_term_memory_read"})

    def __init__(self, service: Any) -> None:
        self.service = service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.service is None:
            return ActionResult(
                success=False, error="记忆服务未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "memory_disabled"})
        params = dict(getattr(intent, "parameters", None) or {})
        entry_id = str(params.get("entry_id") or "").strip()
        if not entry_id:
            return ActionResult(
                success=False, error="需要 entry_id",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_entry_id"})
        try:
            data = self.service.read(intent.agent_id, entry_id)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "long_term_memory_read_failed"})
        return ActionResult(
            success=True, output=data,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


class FrontLongTermMemoryConsolidateExecutor:
    action_types = frozenset({"long_term_memory_consolidate"})

    def __init__(self, service: Any) -> None:
        self.service = service

    def execute(self, intent: ActionIntent) -> ActionResult:
        if self.service is None:
            return ActionResult(
                success=False, error="记忆服务未启用",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "memory_disabled"})
        params = dict(getattr(intent, "parameters", None) or {})
        expected = params.get("expected_revision")
        summary = str(params.get("summary") or "").strip()
        if expected is None or not summary:
            return ActionResult(
                success=False,
                error="需要 expected_revision + summary",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_required_field"})
        try:
            entry, remaining = self.service.consolidate(
                intent.agent_id,
                task_id="",
                expected_revision=int(expected),
                summary=summary,
                tasks=params.get("tasks") or [],
                events=params.get("events") or [],
                short_term_entry_ids=params.get("short_term_entry_ids")
                or [],
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "long_term_memory_consolidate_failed"})
        return ActionResult(
            success=True,
            output={
                "entry": entry.to_dict(),
                "short_term_memory": remaining.to_dict(),
            },
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )


def build_front_dispatcher(
    *,
    workspace_of: Callable[[str], Any],
    drafts: Any = None,
    artifact_read_fn: Optional[Callable[..., Any]] = None,
    web_search_fn: Optional[Callable[..., Any]] = None,
    web_fetch_fn: Optional[Callable[..., Any]] = None,
    project_root: str = "",
    alarm_service: Any = None,
    short_term_memory: Any = None,
    long_term_memory: Any = None,
) -> ActionDispatcher:
    """把 Front 的私有空间 / 草稿 / 只读产物动作接成一个 dispatcher。"""

    def _ws_of(agent_id: str) -> Any:
        return workspace_of(str(agent_id))

    executors: List[Any] = [
        PrivateWriteExecutor(write_private, workspace_of=_ws_of),
        PrivateReadExecutor(read_private, workspace_of=_ws_of),
        PrivateListExecutor(list_private, workspace_of=_ws_of),
        PrivateDeleteExecutor(delete_private, workspace_of=_ws_of),
    ]
    if drafts is not None:
        executors.extend([
            DraftWriteExecutor(drafts.write),
            DraftAppendExecutor(drafts.append),
            DraftReadExecutor(drafts.read),
            DraftListExecutor(drafts.list),
            DraftDeleteExecutor(drafts.delete),
        ])
    if artifact_read_fn is not None:
        executors.append(FrontArtifactReadExecutor(artifact_read_fn))
    if str(project_root or "").strip():
        executors.extend([
            FrontProjectListExecutor(project_root),
            FrontProjectReadExecutor(project_root),
        ])
    if web_search_fn is not None:
        executors.append(FrontWebSearchExecutor(web_search_fn))
    if web_fetch_fn is not None:
        executors.append(FrontWebFetchExecutor(web_fetch_fn))
    if alarm_service is not None:
        executors.extend([
            FrontAlarmCreateExecutor(alarm_service),
            FrontAlarmListExecutor(alarm_service),
            FrontAlarmCancelExecutor(alarm_service),
        ])
    if short_term_memory is not None:
        executors.extend([
            FrontShortTermMemoryAddExecutor(short_term_memory),
            FrontShortTermMemoryUpdateExecutor(short_term_memory),
            FrontShortTermMemoryReinforceExecutor(short_term_memory),
        ])
    if long_term_memory is not None:
        executors.extend([
            FrontLongTermMemorySearchExecutor(long_term_memory),
            FrontLongTermMemoryReadExecutor(long_term_memory),
            FrontLongTermMemoryConsolidateExecutor(long_term_memory),
        ])
    return ActionDispatcher(executors)


def execute_front_actions(
    dispatcher: ActionDispatcher,
    actions: Iterable[Dict[str, Any]],
    *,
    agent_id: str,
    max_output_chars: int = 20000,
) -> str:
    """执行 Front 的动作，并返回可以回喂给模型的真实结果。"""
    blocks: List[str] = []
    for index, item in enumerate(actions or [], start=1):
        action_type = str(item.get("type") or "").strip()
        parameters = item.get("parameters")
        if not action_type or not isinstance(parameters, dict):
            blocks.append(
                f"[{index}] invalid action: {json.dumps(item, ensure_ascii=False)}")
            continue
        intent = ActionIntent(
            action_type=action_type,
            parameters=dict(parameters),
            agent_id=str(agent_id),
        )
        result = dispatcher.dispatch(intent)
        blocks.append(_render_result(index, intent, result, max_output_chars))
    return "\n".join(blocks)


def _render_result(
    index: int,
    intent: ActionIntent,
    result: ActionResult,
    max_output_chars: int,
) -> str:
    payload: Dict[str, Any] = {
        "success": bool(result.success),
        "error": result.error,
        "output": result.output,
        "metadata": result.metadata,
    }
    # 写操作不回显完整正文：模型刚写过，再喂一遍只会重复占上下文。
    safe_params = dict(intent.parameters)
    if intent.action_type in ("private_write", "draft_write", "draft_append"):
        content = safe_params.pop("content", None)
        if content is not None:
            safe_params["content_chars"] = len(str(content))
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > int(max_output_chars):
        text = (
            text[:int(max_output_chars)]
            + f"\n…（结果已截断；完整 {len(text)} 字符）")
    return (
        f"[{index}] {intent.action_type}\n"
        f"parameters: {json.dumps(safe_params, ensure_ascii=False, default=str)[:2000]}\n"
        f"result: {text}")
