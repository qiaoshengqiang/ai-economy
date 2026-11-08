"""
PrivateShortTermMemoryStore —— 私有短期记忆持久化
==================================================

    agent_short_term_memory  当前 short-term（固定容量，JSON 快照）
    agent_long_term_memory   清理时提升的 digest（长期记忆 stub）

🔒 短期记忆按 agent_id 一份，跨 Task。
🔒 长期记忆本版只存不读，留给后续长期记忆模块。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.private_short_term_memory import (
    PrivateShortTermMemory,
    ShortTermEntry,
    add_entry,
    update_entry,
)
from storage.database import Database
from storage.models import (
    TABLE_PRIVATE_LONG_TERM_MEMORY,
    TABLE_PRIVATE_SHORT_TERM_MEMORY,
)


class PrivateShortTermMemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def get(self, agent_id: str) -> Optional[PrivateShortTermMemory]:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                "WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            return None
        return PrivateShortTermMemory.from_dict(agent_id,
                                                json.loads(row["payload"]))

    # ------------------------------------------------------------
    def add(self, agent_id: str, entry: ShortTermEntry
            ) -> PrivateShortTermMemory:
        """任务完成时追加；超过一半自动压缩，digest 进入长期记忆。"""
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                "WHERE agent_id = ?", (agent_id,)).fetchone()
            current = (PrivateShortTermMemory.from_dict(
                agent_id, json.loads(row["payload"])) if row else None)
            memory, digest = add_entry(
                current, entry, agent_id=agent_id)
            if digest is not None:
                conn.execute(
                    f"INSERT OR REPLACE INTO {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                    "(agent_id, entry_id, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (agent_id, digest.entry_id,
                     json.dumps(digest.to_dict(), ensure_ascii=False,
                                sort_keys=True),
                     digest.created_at))
            payload = json.dumps(memory.to_dict(), ensure_ascii=False,
                                 sort_keys=True)
            if current is None:
                conn.execute(
                    f"INSERT INTO {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                    "(agent_id, revision, payload, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (agent_id, memory.revision, payload, memory.updated_at))
            else:
                conn.execute(
                    f"UPDATE {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                    "SET revision = ?, payload = ?, updated_at = ? "
                    "WHERE agent_id = ? AND revision = ?",
                    (memory.revision, payload, memory.updated_at,
                     agent_id, current.revision))
            return memory

    # ------------------------------------------------------------
    def update(
        self,
        agent_id: str,
        *,
        expected_revision: int,
        entry_id: str,
        summary: Optional[str] = None,
        architecture: Optional[str] = None,
        notable: Optional[str] = None,
        importance: Optional[int] = None,
        remove: bool = False,
    ) -> PrivateShortTermMemory:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT revision, payload FROM {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                "WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                raise ValueError("还没有短期记忆")
            current = PrivateShortTermMemory.from_dict(
                agent_id, json.loads(row["payload"]))
            if int(expected_revision) != current.revision:
                raise ValueError(
                    f"短期记忆 revision 冲突：期望 {expected_revision}，"
                    f"当前 {current.revision}")
            memory = update_entry(
                current, agent_id=agent_id, entry_id=entry_id,
                summary=summary, architecture=architecture, notable=notable,
                importance=importance, remove=remove)
            payload = json.dumps(memory.to_dict(), ensure_ascii=False,
                                 sort_keys=True)
            conn.execute(
                f"UPDATE {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                "SET revision = ?, payload = ?, updated_at = ? "
                "WHERE agent_id = ? AND revision = ?",
                (memory.revision, payload, memory.updated_at,
                 agent_id, current.revision))
            return memory

    # ------------------------------------------------------------
    def long_term_entries(self, agent_id: str) -> List[Dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                "WHERE agent_id = ? ORDER BY created_at", (agent_id,)
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]


__all__ = ["PrivateShortTermMemoryStore"]
