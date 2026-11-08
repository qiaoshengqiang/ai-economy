"""
PublicWorkingMemoryStore —— 公共工作记忆的持久化
================================================

    task_public_working_memory            当前版本（CAS 指针 + 快照）
    task_public_working_memory_revisions  历史版本（append-only）

🔒 与 Shared Context 同一个并发模式：CAS on revision。
🔒 不判断参与者资格 —— 那是 service 层的事。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from core.public_working_memory import (
    ConcurrentWorkingMemoryModification,
    PublicWorkingMemory,
    apply_update,
)
from storage.database import Database
from storage.models import (
    TABLE_PUBLIC_WORKING_MEMORY,
    TABLE_PUBLIC_WORKING_MEMORY_REVISIONS,
)


class PublicWorkingMemoryStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------
    def get(self, task_id: str) -> Optional[PublicWorkingMemory]:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT payload FROM {TABLE_PUBLIC_WORKING_MEMORY} "
                "WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return PublicWorkingMemory.from_dict(task_id, json.loads(row["payload"]))

    # ------------------------------------------------------------
    def update(
        self,
        task_id: str,
        *,
        author: str,
        expected_revision: int,
        items: Optional[Mapping[str, Mapping[str, Any]]] = None,
        remove_keys: Optional[List[str]] = None,
    ) -> PublicWorkingMemory:
        """
        基于 `expected_revision` 写入下一版。

        🔒 没有记忆时：expected_revision 必须是 0，创建 revision 1。
        🔒 有记忆时：CAS `WHERE revision = expected_revision`；
           影响 0 行 → ConcurrentWorkingMemoryModification。
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT revision, payload FROM {TABLE_PUBLIC_WORKING_MEMORY} "
                "WHERE task_id = ?", (task_id,)).fetchone()
            current = (PublicWorkingMemory.from_dict(
                task_id, json.loads(row["payload"])) if row else None)
            actual = current.revision if current else 0

            if int(expected_revision) != actual:
                raise ConcurrentWorkingMemoryModification(
                    task_id, int(expected_revision), actual)

            next_memory = apply_update(
                current,
                task_id=task_id,
                author=author,
                expected_revision=int(expected_revision),
                items=items,
                remove_keys=remove_keys,
            )
            payload = json.dumps(next_memory.to_dict(), ensure_ascii=False,
                                 sort_keys=True)

            if current is None:
                conn.execute(
                    f"INSERT INTO {TABLE_PUBLIC_WORKING_MEMORY} "
                    "(task_id, revision, payload, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task_id, next_memory.revision, payload,
                     next_memory.updated_at, author))
            else:
                cur = conn.execute(
                    f"UPDATE {TABLE_PUBLIC_WORKING_MEMORY} "
                    "SET revision = ?, payload = ?, updated_at = ?, "
                    "updated_by = ? WHERE task_id = ? AND revision = ?",
                    (next_memory.revision, payload, next_memory.updated_at,
                     author, task_id, int(expected_revision)))
                if cur.rowcount != 1:
                    raise ConcurrentWorkingMemoryModification(
                        task_id, int(expected_revision), actual)

            conn.execute(
                f"INSERT INTO {TABLE_PUBLIC_WORKING_MEMORY_REVISIONS} "
                "(task_id, revision, payload, author, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, next_memory.revision, payload, author,
                 next_memory.updated_at))
            return next_memory

    # ------------------------------------------------------------
    def history(self, task_id: str, limit: int = 20
                ) -> List[PublicWorkingMemory]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {TABLE_PUBLIC_WORKING_MEMORY_REVISIONS} "
                "WHERE task_id = ? ORDER BY revision DESC LIMIT ?",
                (task_id, int(limit))).fetchall()
        return [PublicWorkingMemory.from_dict(task_id, json.loads(r["payload"]))
                for r in rows]


__all__ = ["PublicWorkingMemoryStore"]
