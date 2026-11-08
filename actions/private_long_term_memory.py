"""
Private Long-Term Memory Actions
==================================

    long_term_memory_search       → 主动查低权重/特定主题
    long_term_memory_read         → 读一条长期记忆 + 对应短期存档
    long_term_memory_mark_used    → 这条长期记忆对当前任务有帮助
    long_term_memory_consolidate  → Agent 把短期记忆抽象成长期记忆并归档

🔒 agent_id 只取自 intent；task_id 由组合根绑定。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from actions.intent import ActionResult, ActionIntent

ACTION_LONG_TERM_MEMORY_SEARCH = "long_term_memory_search"
ACTION_LONG_TERM_MEMORY_READ = "long_term_memory_read"
ACTION_LONG_TERM_MEMORY_MARK_USED = "long_term_memory_mark_used"
ACTION_LONG_TERM_MEMORY_CONSOLIDATE = "long_term_memory_consolidate"


class LongTermMemorySearchExecutor:
    action_types = frozenset({ACTION_LONG_TERM_MEMORY_SEARCH})

    def __init__(self, search_fn: Callable[..., Any]):
        self.search_fn = search_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        query = str((intent.parameters or {}).get("query") or "")
        try:
            entries = self.search_fn(intent.agent_id, query)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "long_term_memory_search_failed"})
        return ActionResult(
            success=True, output={"entries": entries}, error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


class LongTermMemoryReadExecutor:
    action_types = frozenset({ACTION_LONG_TERM_MEMORY_READ})

    def __init__(self, read_fn: Callable[..., Any]):
        self.read_fn = read_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        entry_id = str((intent.parameters or {}).get("entry_id") or "")
        if not entry_id:
            return ActionResult(
                success=False, output=None, error="需要 entry_id",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_entry_id"})
        try:
            data = self.read_fn(intent.agent_id, entry_id)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "long_term_memory_read_failed"})
        return ActionResult(
            success=True, output=data, error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


class LongTermMemoryMarkUsedExecutor:
    action_types = frozenset({ACTION_LONG_TERM_MEMORY_MARK_USED})

    def __init__(self, mark_fn: Callable[..., Any]):
        self.mark_fn = mark_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        ids = (intent.parameters or {}).get("entry_ids") or []
        if not isinstance(ids, list) or not ids:
            return ActionResult(
                success=False, output=None, error="entry_ids 不能为空",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_entry_ids"})
        try:
            updated = self.mark_fn(intent.agent_id, ids)
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "long_term_memory_mark_failed"})
        return ActionResult(
            success=True, output={"updated": updated}, error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


class LongTermMemoryConsolidateExecutor:
    action_types = frozenset({ACTION_LONG_TERM_MEMORY_CONSOLIDATE})

    def __init__(self, consolidate_fn: Callable[..., Any]):
        self.consolidate_fn = consolidate_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        expected = params.get("expected_revision")
        summary = str(params.get("summary") or "").strip()
        if expected is None or not summary:
            return ActionResult(
                success=False, output=None,
                error="需要 expected_revision + summary",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "missing_required_field"})
        try:
            entry, remaining = self.consolidate_fn(
                intent.agent_id,
                expected_revision=int(expected),
                summary=summary,
                tasks=params.get("tasks") or [],
                events=params.get("events") or [],
                short_term_entry_ids=params.get("short_term_entry_ids") or [],
            )
        except Exception as exc:                              # noqa: BLE001
            return ActionResult(
                success=False, output=None,
                error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": "long_term_memory_consolidate_failed"})
        return ActionResult(
            success=True,
            output={"entry": entry.to_dict(),
                    "short_term_memory": remaining.to_dict()},
            error=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"code": None})


__all__ = [
    "ACTION_LONG_TERM_MEMORY_SEARCH",
    "ACTION_LONG_TERM_MEMORY_READ",
    "ACTION_LONG_TERM_MEMORY_MARK_USED",
    "ACTION_LONG_TERM_MEMORY_CONSOLIDATE",
    "LongTermMemorySearchExecutor",
    "LongTermMemoryReadExecutor",
    "LongTermMemoryMarkUsedExecutor",
    "LongTermMemoryConsolidateExecutor",
]
