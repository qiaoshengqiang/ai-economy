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
    "private_read", "private_write", "private_list", "private_delete",
    "draft_read", "draft_write", "draft_append", "draft_list",
    "draft_delete",
})


def build_front_dispatcher(
    *,
    workspace_of: Callable[[str], Any],
    drafts: Any,
) -> ActionDispatcher:
    """把 Front 的私有空间 / 草稿动作接成一个 dispatcher。"""

    def _ws_of(agent_id: str) -> Any:
        return workspace_of(str(agent_id))

    return ActionDispatcher([
        PrivateWriteExecutor(write_private, workspace_of=_ws_of),
        PrivateReadExecutor(read_private, workspace_of=_ws_of),
        PrivateListExecutor(list_private, workspace_of=_ws_of),
        PrivateDeleteExecutor(delete_private, workspace_of=_ws_of),
        DraftWriteExecutor(drafts.write),
        DraftAppendExecutor(drafts.append),
        DraftReadExecutor(drafts.read),
        DraftListExecutor(drafts.list),
        DraftDeleteExecutor(drafts.delete),
    ])


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
