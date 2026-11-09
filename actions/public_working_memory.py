"""
Public Working Memory Actions
==============================

    public_working_memory_read    → 读当前公共工作记忆
    public_working_memory_update  → 基于 revision 更新条目

🔒 与 Workspace / Shared Context 的动作同构：
    · 只执行，不决策；
    · task_id 由组合根绑定，Agent 不能指定别的任务；
    · agent_id 只取自 intent（运行时填的），不能伪造；
    · 失败返回结构化 ActionResult，不抛异常给 Agent。

🔒 更新是**显式**动作。系统不会在工作循环后自动写入 ——
   “要不要记录、记录什么”仍然由 Agent 判断。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from actions.intent import ActionResult, ActionIntent

ACTION_PUBLIC_WORKING_MEMORY_READ = "public_working_memory_read"
ACTION_PUBLIC_WORKING_MEMORY_UPDATE = "public_working_memory_update"


def _memory_dict(memory: Any) -> Dict[str, Any]:
    if hasattr(memory, "to_dict"):
        return memory.to_dict()
    if isinstance(memory, dict):
        return memory
    return {}


class PublicWorkingMemoryReadExecutor:
    action_types = frozenset({ACTION_PUBLIC_WORKING_MEMORY_READ})

    def __init__(self, read_fn: Callable[..., Any]):
        self.read_fn = read_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        try:
            memory = self.read_fn(intent.agent_id)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "working_memory_read_failed"})
        return ActionResult(
            success=True, output=_memory_dict(memory), error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


class PublicWorkingMemoryUpdateExecutor:
    action_types = frozenset({ACTION_PUBLIC_WORKING_MEMORY_UPDATE})

    #: 允许的参数字段。未知字段**必须失败**，不能静默忽略 ——
    #: 真实 run 里 Agent 用 `content` 写协作板，结果更新成功但 items 为空。
    _ALLOWED_PARAMS = frozenset({
        "expected_revision", "items", "remove_keys", "content",
    })

    def __init__(self, update_fn: Callable[..., Any]):
        self.update_fn = update_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        unknown = sorted(set(params) - self._ALLOWED_PARAMS)
        if unknown:
            return ActionResult(
                success=False, output=None,
                error=(f"public_working_memory_update 不认识的参数：{unknown}；"
                       "请使用 expected_revision / items / remove_keys / "
                       "content（content 是单条 note 的简写）"),
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "unknown_parameters",
                          "unknown": unknown})
        expected = params.get("expected_revision")
        if expected is None:
            return ActionResult(
                success=False, output=None,
                error="public_working_memory_update 需要 expected_revision"
                      "（先读当前 revision，再基于它更新）",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_expected_revision"})
        items = params.get("items")
        if items is not None and not isinstance(items, dict):
            return ActionResult(
                success=False, output=None,
                error="items 必须是对象：key -> {kind, value, parent_key?}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "invalid_items"})
        remove = params.get("remove_keys")
        if remove is not None and not isinstance(remove, list):
            return ActionResult(
                success=False, output=None,
                error="remove_keys 必须是字符串列表",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "invalid_remove_keys"})
        content = params.get("content")
        if content is not None:
            if items is not None:
                return ActionResult(
                    success=False, output=None,
                    error="content 和 items 只能二选一；"
                          "content 是单条 note 的简写",
                    action_id=intent.intent_id,
                    action_type=intent.action_type,
                    metadata={"code": "content_and_items"})
            if not isinstance(content, str):
                return ActionResult(
                    success=False, output=None,
                    error="content 必须是字符串",
                    action_id=intent.intent_id,
                    action_type=intent.action_type,
                    metadata={"code": "invalid_content"})
            items = {"note": {"kind": "note", "value": content}}
        if not items and not remove:
            return ActionResult(
                success=False, output=None,
                error="这次更新没有任何条目（items/content/remove_keys 都为空）",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "empty_update"})
        try:
            memory = self.update_fn(
                intent.agent_id,
                expected_revision=int(expected),
                items=items,
                remove_keys=remove)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "working_memory_update_failed"})
        return ActionResult(
            success=True, output=_memory_dict(memory), error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


__all__ = [
    "ACTION_PUBLIC_WORKING_MEMORY_READ",
    "ACTION_PUBLIC_WORKING_MEMORY_UPDATE",
    "PublicWorkingMemoryReadExecutor",
    "PublicWorkingMemoryUpdateExecutor",
]
