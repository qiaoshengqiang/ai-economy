"""
PrivateLongTermMemoryStore —— 私有长期记忆持久化
==================================================

    agent_long_term_memory  (agent_id, entry_id, payload, created_at)

🔒 只存不自动读；读取由 service 的 top/search 决定。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from core.private_long_term_memory import (
    LongTermEntry,
    decay,
    mark_used,
)
from storage.database import Database
from storage.models import TABLE_PRIVATE_LONG_TERM_MEMORY


class PrivateLongTermMemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def all(self, agent_id: str) -> List[LongTermEntry]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                "WHERE agent_id = ? ORDER BY created_at", (agent_id,)
            ).fetchall()
        return [LongTermEntry.from_dict(json.loads(r["payload"]))
                for r in rows]

    def get(self, agent_id: str, entry_id: str) -> Optional[LongTermEntry]:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                "WHERE agent_id = ? AND entry_id = ?",
                (agent_id, entry_id)).fetchone()
        return LongTermEntry.from_dict(json.loads(row["payload"])) if row else None

    def upsert(self, agent_id: str, entry: LongTermEntry) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                "(agent_id, entry_id, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (agent_id, entry.entry_id,
                 json.dumps(entry.to_dict(), ensure_ascii=False,
                            sort_keys=True),
                 entry.created_at))

    def delete(self, agent_id: str, entry_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"DELETE FROM {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                "WHERE agent_id = ? AND entry_id = ?",
                (agent_id, entry_id))
        return int(cur.rowcount or 0) > 0

    def clear(self, agent_id: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"DELETE FROM {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                "WHERE agent_id = ?", (agent_id,))
        return max(0, int(cur.rowcount or 0))

    def mark_used(self, agent_id: str, entry_ids: Iterable[str],
                  task_id: str) -> List[LongTermEntry]:
        wanted = {str(x) for x in entry_ids}
        updated = []
        for entry in self.all(agent_id):
            if entry.entry_id in wanted:
                item = mark_used(entry, task_id)
                self.upsert(agent_id, item)
                updated.append(item)
        return updated

    def decay_unused(self, agent_id: str, task_id: str) -> List[LongTermEntry]:
        updated = []
        for entry in self.all(agent_id):
            item = decay(entry, task_id)
            if item is not entry and item.weight != entry.weight:
                self.upsert(agent_id, item)
                updated.append(item)
        return updated


__all__ = ["PrivateLongTermMemoryStore"]
