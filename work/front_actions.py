"""
Front Agent 自己的动作集合
==========================

Front 不是 Work Agent，但需要在自己的私有空间里准备文件/草稿：

    private_read / private_write / private_list / private_delete
    draft_read / draft_write / draft_append / draft_list / draft_delete

🔒 这里**没有** Task / Workspace / run_tool：
   · Front 不创建 Task；
   · Front 不写 Task Workspace；
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


FRONT_ACTION_TYPES = frozenset({
    "artifact_read",
    "private_read", "private_write", "private_list", "private_delete",
    "draft_read", "draft_write", "draft_append", "draft_list",
    "draft_delete",
})


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


def build_front_dispatcher(
    *,
    workspace_of: Callable[[str], Any],
    drafts: Any,
    artifact_read_fn: Optional[Callable[..., Any]] = None,
) -> ActionDispatcher:
    """把 Front 的私有空间 / 草稿 / 只读产物动作接成一个 dispatcher。"""

    def _ws_of(agent_id: str) -> Any:
        return workspace_of(str(agent_id))

    executors: List[Any] = [
        PrivateWriteExecutor(write_private, workspace_of=_ws_of),
        PrivateReadExecutor(read_private, workspace_of=_ws_of),
        PrivateListExecutor(list_private, workspace_of=_ws_of),
        PrivateDeleteExecutor(delete_private, workspace_of=_ws_of),
        DraftWriteExecutor(drafts.write),
        DraftAppendExecutor(drafts.append),
        DraftReadExecutor(drafts.read),
        DraftListExecutor(drafts.list),
        DraftDeleteExecutor(drafts.delete),
    ]
    if artifact_read_fn is not None:
        executors.append(FrontArtifactReadExecutor(artifact_read_fn))
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
