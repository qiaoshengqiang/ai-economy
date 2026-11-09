"""
Private Working Memory Actions
================================

    private_working_memory_read    → 读自己的私有工作记忆
    private_working_memory_update  → 基于 revision 更新节点

🔒 私有：agent_id 只取自 intent；task_id 由组合根绑定。
🔒 没有“读/写别人的私有记忆”的参数。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from actions.intent import ActionResult, ActionIntent

ACTION_PRIVATE_WORKING_MEMORY_READ = "private_working_memory_read"
ACTION_PRIVATE_WORKING_MEMORY_UPDATE = "private_working_memory_update"


def _memory_dict(memory: Any) -> Dict[str, Any]:
    if hasattr(memory, "to_dict"):
        return memory.to_dict()
    if isinstance(memory, dict):
        return memory
    return {}


class PrivateWorkingMemoryReadExecutor:
    action_types = frozenset({ACTION_PRIVATE_WORKING_MEMORY_READ})

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
                metadata={"code": "private_working_memory_read_failed"})
        return ActionResult(
            success=True, output=_memory_dict(memory), error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


class PrivateWorkingMemoryUpdateExecutor:
    action_types = frozenset({ACTION_PRIVATE_WORKING_MEMORY_UPDATE})

    _ALLOWED_PARAMS = frozenset({
        "expected_revision", "nodes", "remove_nodes",
    })

    def __init__(self, update_fn: Callable[..., Any]):
        self.update_fn = update_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        unknown = sorted(set(params) - self._ALLOWED_PARAMS)
        if unknown:
            return ActionResult(
                success=False, output=None,
                error=(f"private_working_memory_update 不认识的参数：{unknown}；"
                       "请使用 expected_revision / nodes / remove_nodes"),
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "unknown_parameters",
                          "unknown": unknown})
        expected = params.get("expected_revision")
        if expected is None:
            return ActionResult(
                success=False, output=None,
                error="private_working_memory_update 需要 expected_revision"
                      "（先读当前 revision，再基于它更新）",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_expected_revision"})
        nodes = params.get("nodes")
        if nodes is not None and not isinstance(nodes, dict):
            return ActionResult(
                success=False, output=None,
                error="nodes 必须是对象：node_id -> "
                      "{kind,title,content,parent_id}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "invalid_nodes"})
        remove = params.get("remove_nodes")
        if remove is not None and not isinstance(remove, list):
            return ActionResult(
                success=False, output=None,
                error="remove_nodes 必须是字符串列表",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "invalid_remove_nodes"})
        # 🔒 空更新是**合法**的：work checkpoint 明确允许“检查后没有变化”。
        #    但未知参数仍必须失败，避免 content 之类的字段被静默忽略。
        try:
            memory = self.update_fn(
                intent.agent_id,
                expected_revision=int(expected),
                nodes=nodes,
                remove_nodes=remove)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "private_working_memory_update_failed"})
        return ActionResult(
            success=True, output=_memory_dict(memory), error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


__all__ = [
    "ACTION_PRIVATE_WORKING_MEMORY_READ",
    "ACTION_PRIVATE_WORKING_MEMORY_UPDATE",
    "PrivateWorkingMemoryReadExecutor",
    "PrivateWorkingMemoryUpdateExecutor",
]
