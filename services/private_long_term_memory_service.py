"""
PrivateLongTermMemoryService —— 私有长期记忆应用层
====================================================

    · top-N 高权重读取（每任务只注入一部分）
    · search / read（低权重也可以主动查）
    · mark_used / decay_unused（权重反馈）
    · consolidate（Agent 把短期记忆抽象成长期记忆 + 归档具体短期条目）
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core.private_long_term_memory import (
    TOP_LIMIT,
    LongTermEntry,
    LongTermMemoryValidationError,
    make_entry,
    matches,
)
from core.private_short_term_memory import KEEP_AFTER_COMPACT


class PrivateLongTermMemoryService:
    def __init__(self, store: Any, short_term_store: Any):
        self.store = store
        self.short_term_store = short_term_store

    # ------------------------------------------------------------
    def all(self, agent_id: str) -> List[LongTermEntry]:
        return self.store.all(agent_id)

    def top(self, agent_id: str, limit: int = TOP_LIMIT
            ) -> List[LongTermEntry]:
        return sorted(self.all(agent_id),
                      key=lambda e: (e.weight, e.created_at),
                      reverse=True)[:int(limit)]

    def search(self, agent_id: str, query: str = "") -> List[LongTermEntry]:
        return [e for e in self.all(agent_id) if matches(e, query)]

    def read(self, agent_id: str, entry_id: str,
             task_id: str = "") -> Dict[str, Any]:
        entry = self.store.get(agent_id, entry_id)
        if entry is None:
            raise LongTermMemoryValidationError(
                f"找不到 long-term entry {entry_id!r}")
        # 🔴 通过长期记忆读了对应的短期存档 = 这条长期记忆被使用了。
        if task_id:
            self.store.mark_used(agent_id, [entry_id], task_id)
            entry = self.store.get(agent_id, entry_id) or entry
        archived = self.short_term_store.archived_entries(
            agent_id, long_term_entry_id=entry_id)
        return {"entry": entry.to_dict(), "short_term_entries": archived}

    # ------------------------------------------------------------
    def mark_used(self, agent_id: str, entry_ids: Iterable[str],
                  task_id: str) -> List[LongTermEntry]:
        return self.store.mark_used(agent_id, entry_ids, task_id)

    def decay_unused(self, agent_id: str, task_id: str) -> List[LongTermEntry]:
        return self.store.decay_unused(agent_id, task_id)

    # ------------------------------------------------------------
    def consolidate(
        self,
        agent_id: str,
        *,
        task_id: str,
        expected_revision: int,
        summary: str,
        tasks: Optional[Iterable[str]] = None,
        events: Optional[Iterable[str]] = None,
        short_term_entry_ids: Optional[Iterable[str]] = None,
    ) -> Tuple[LongTermEntry, Any]:
        """
        Agent 的抽象工作：把选中的短期条目抽象成长期条目并归档。
        """
        current = self.short_term_store.get(agent_id)
        if current is None:
            raise LongTermMemoryValidationError("还没有短期记忆")
        if int(expected_revision) != current.revision:
            raise LongTermMemoryValidationError(
                f"短期记忆 revision 冲突：期望 {expected_revision}，"
                f"当前 {current.revision}")

        ids = [str(x) for x in (short_term_entry_ids or [])]
        if not ids:
            # 默认清理最旧的溢出条目（超过 KEEP_AFTER_COMPACT 的部分）
            ordered = sorted(current.entries, key=lambda e: e.created_at)
            ids = [e.entry_id for e in ordered[
                :max(0, len(ordered) - KEEP_AFTER_COMPACT)]]
        if not ids:
            raise LongTermMemoryValidationError(
                "没有选中任何短期条目（短期记忆还没超过保留数量）")
        existing = {e.entry_id for e in current.entries}
        missing = [x for x in ids if x not in existing]
        if missing:
            raise LongTermMemoryValidationError(
                f"短期记忆里没有这些 entry：{missing}")

        entry = make_entry(
            summary=summary,
            tasks=list(
                tasks or ([task_id] if str(task_id or "").strip() else [])),
            events=events or [],
            short_term_entry_ids=ids,
            weight=1.0 + 0.1 * len(ids),
        )
        remaining = self.short_term_store.archive_and_remove(
            agent_id,
            expected_revision=current.revision,
            entry_ids=ids,
            long_term_entry_id=entry.entry_id,
        )
        self.store.upsert(agent_id, entry)
        return entry, remaining

    # ------------------------------------------------------------
    def render_top(self, agent_id: str, limit: int = TOP_LIMIT) -> str:
        entries = self.all(agent_id)
        top = self.top(agent_id, limit)
        lines = []
        if not top:
            return "（还没有长期记忆）"
        for e in top:
            lines.append(
                f"- [{e.entry_id}] weight={e.weight:.1f} "
                f"use_count={e.use_count}\n"
                f"  summary: {e.summary}")
            if e.tasks:
                lines.append(f"  tasks: {' | '.join(e.tasks)}")
            if e.events:
                lines.append(f"  events: {' | '.join(e.events)}")
            lines.append(
                f"  short_term_entries: {len(e.short_term_entry_ids)} 条")
        low = len(entries) - len(top)
        if low > 0:
            lines.append(
                f"（还有 {low} 条低权重记忆未读取；"
                "可以用 long_term_memory_search / read 主动查看。）")
        return "\n".join(lines)


__all__ = ["PrivateLongTermMemoryService"]
