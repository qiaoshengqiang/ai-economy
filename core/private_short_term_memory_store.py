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
    reinforce_entry,
    update_entry,
)
from storage.database import Database
from storage.models import (
    TABLE_PRIVATE_LONG_TERM_MEMORY,
    TABLE_PRIVATE_SHORT_TERM_ARCHIVE,
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
            memory = add_entry(current, entry, agent_id=agent_id)
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
    def reinforce(
        self,
        agent_id: str,
        *,
        expected_revision: int,
        entry_id: str,
        reason: str = "",
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
            memory = reinforce_entry(
                current, agent_id=agent_id, entry_id=entry_id, reason=reason)
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
    def archive_and_remove(
        self,
        agent_id: str,
        *,
        expected_revision: int,
        entry_ids: List[str],
        long_term_entry_id: str,
    ) -> PrivateShortTermMemory:
        """
        Agent 完成长期抽象后：把选中的短期条目移入 archive，并从当前短期记忆删除。
        """
        targets = set(entry_ids)
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
            keep = []
            for e in current.entries:
                if e.entry_id in targets:
                    conn.execute(
                        f"INSERT OR REPLACE INTO "
                        f"{TABLE_PRIVATE_SHORT_TERM_ARCHIVE} "
                        "(agent_id, entry_id, payload, archived_at, "
                        "long_term_entry_id) VALUES (?, ?, ?, ?, ?)",
                        (agent_id, e.entry_id,
                         json.dumps(e.to_dict(), ensure_ascii=False,
                                    sort_keys=True),
                         current.updated_at, long_term_entry_id))
                else:
                    keep.append(e)
            memory = PrivateShortTermMemory(
                agent_id=agent_id, revision=current.revision + 1,
                entries=keep, updated_at=current.updated_at)
            payload = json.dumps(memory.to_dict(), ensure_ascii=False,
                                 sort_keys=True)
            conn.execute(
                f"UPDATE {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                "SET revision = ?, payload = ?, updated_at = ? "
                "WHERE agent_id = ? AND revision = ?",
                (memory.revision, payload, memory.updated_at,
                 agent_id, current.revision))
            return memory

    def clear(self, agent_id: str) -> Dict[str, int]:
        """清空该 Agent 的当前短期记忆与归档（不可恢复）。"""
        with self.db.transaction() as conn:
            current = conn.execute(
                f"DELETE FROM {TABLE_PRIVATE_SHORT_TERM_MEMORY} "
                "WHERE agent_id = ?", (agent_id,))
            archived = conn.execute(
                f"DELETE FROM {TABLE_PRIVATE_SHORT_TERM_ARCHIVE} "
                "WHERE agent_id = ?", (agent_id,))
        return {
            "short_term": max(0, int(current.rowcount or 0)),
            "archived": max(0, int(archived.rowcount or 0)),
        }

    def archived_entries(self, agent_id: str,
                         long_term_entry_id: Optional[str] = None
                         ) -> List[Dict[str, Any]]:
        query = (f"SELECT payload FROM {TABLE_PRIVATE_SHORT_TERM_ARCHIVE} "
                 "WHERE agent_id = ?")
        args: List[Any] = [agent_id]
        if long_term_entry_id is not None:
            query += " AND long_term_entry_id = ?"
            args.append(long_term_entry_id)
        with self.db.transaction() as conn:
            rows = conn.execute(query, args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    # ------------------------------------------------------------
    def long_term_entries(self, agent_id: str) -> List[Dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_LONG_TERM_MEMORY} "
                "WHERE agent_id = ? ORDER BY created_at", (agent_id,)
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]


__all__ = ["PrivateShortTermMemoryStore"]
