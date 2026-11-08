"""
Private Short-Term Memory Actions
====================================

    short_term_memory_read    → 读自己的短期记忆
    short_term_memory_update  → 修正/删除一个条目（反馈维护）

🔒 只有 intent.agent_id 自己的短期记忆；没有跨 Agent 参数。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from actions.intent import ActionResult, ActionIntent

ACTION_SHORT_TERM_MEMORY_READ = "short_term_memory_read"
ACTION_SHORT_TERM_MEMORY_UPDATE = "short_term_memory_update"


def _memory_dict(memory: Any) -> Dict[str, Any]:
    if hasattr(memory, "to_dict"):
        return memory.to_dict()
    if isinstance(memory, dict):
        return memory
    return {}


class ShortTermMemoryReadExecutor:
    action_types = frozenset({ACTION_SHORT_TERM_MEMORY_READ})

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
                metadata={"code": "short_term_memory_read_failed"})
        return ActionResult(
            success=True, output=_memory_dict(memory), error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


class ShortTermMemoryUpdateExecutor:
    action_types = frozenset({ACTION_SHORT_TERM_MEMORY_UPDATE})

    def __init__(self, update_fn: Callable[..., Any]):
        self.update_fn = update_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        expected = params.get("expected_revision")
        entry_id = str(params.get("entry_id") or "").strip()
        if expected is None:
            return ActionResult(
                success=False, output=None,
                error="short_term_memory_update 需要 expected_revision",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_expected_revision"})
        if not entry_id:
            return ActionResult(
                success=False, output=None,
                error="short_term_memory_update 需要 entry_id",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_entry_id"})
        try:
            memory = self.update_fn(
                intent.agent_id,
                expected_revision=int(expected),
                entry_id=entry_id,
                summary=params.get("summary"),
                architecture=params.get("architecture"),
                notable=params.get("notable"),
                importance=params.get("importance"),
                remove=bool(params.get("remove", False)))
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "short_term_memory_update_failed"})
        return ActionResult(
            success=True, output=_memory_dict(memory), error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


__all__ = [
    "ACTION_SHORT_TERM_MEMORY_READ",
    "ACTION_SHORT_TERM_MEMORY_UPDATE",
    "ShortTermMemoryReadExecutor",
    "ShortTermMemoryUpdateExecutor",
]
