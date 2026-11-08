"""
PrivateWorkingMemoryStore —— 私有工作记忆持久化
================================================

    agent_private_working_memory            当前版本（CAS + 快照）
    agent_private_working_memory_revisions  历史版本（append-only）

🔒 每个 (agent_id, task_id) 一份；只有该 Agent 的 action 会写它。
🔒 任务结项后 `finalize()` 删除当前行；历史保留。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from core.private_working_memory import (
    ConcurrentPrivateWorkingMemoryModification,
    PrivateWorkingMemory,
    apply_update,
)
from storage.database import Database
from storage.models import (
    TABLE_PRIVATE_WORKING_MEMORY,
    TABLE_PRIVATE_WORKING_MEMORY_REVISIONS,
)


class PrivateWorkingMemoryStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------
    def get(self, agent_id: str, task_id: str
            ) -> Optional[PrivateWorkingMemory]:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_WORKING_MEMORY} "
                "WHERE agent_id = ? AND task_id = ?",
                (agent_id, task_id)).fetchone()
        if row is None:
            return None
        return PrivateWorkingMemory.from_dict(
            agent_id, task_id, json.loads(row["payload"]))

    # ------------------------------------------------------------
    def update(
        self,
        agent_id: str,
        task_id: str,
        *,
        expected_revision: int,
        nodes: Optional[Mapping[str, Mapping[str, Any]]] = None,
        remove_nodes: Optional[List[str]] = None,
    ) -> PrivateWorkingMemory:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT revision, payload FROM {TABLE_PRIVATE_WORKING_MEMORY} "
                "WHERE agent_id = ? AND task_id = ?",
                (agent_id, task_id)).fetchone()
            current = (PrivateWorkingMemory.from_dict(
                agent_id, task_id, json.loads(row["payload"]))
                if row else None)
            actual = current.revision if current else 0
            if int(expected_revision) != actual:
                raise ConcurrentPrivateWorkingMemoryModification(
                    agent_id, task_id, int(expected_revision), actual)

            next_memory = apply_update(
                current,
                agent_id=agent_id, task_id=task_id,
                expected_revision=int(expected_revision),
                nodes=nodes, remove_nodes=remove_nodes)
            payload = json.dumps(next_memory.to_dict(), ensure_ascii=False,
                                 sort_keys=True)

            if current is None:
                conn.execute(
                    f"INSERT INTO {TABLE_PRIVATE_WORKING_MEMORY} "
                    "(agent_id, task_id, revision, payload, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (agent_id, task_id, next_memory.revision, payload,
                     next_memory.updated_at))
            else:
                cur = conn.execute(
                    f"UPDATE {TABLE_PRIVATE_WORKING_MEMORY} "
                    "SET revision = ?, payload = ?, updated_at = ? "
                    "WHERE agent_id = ? AND task_id = ? AND revision = ?",
                    (next_memory.revision, payload, next_memory.updated_at,
                     agent_id, task_id, int(expected_revision)))
                if cur.rowcount != 1:
                    raise ConcurrentPrivateWorkingMemoryModification(
                        agent_id, task_id, int(expected_revision), actual)

            conn.execute(
                f"INSERT INTO {TABLE_PRIVATE_WORKING_MEMORY_REVISIONS} "
                "(agent_id, task_id, revision, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, task_id, next_memory.revision, payload,
                 next_memory.updated_at))
            return next_memory

    # ------------------------------------------------------------
    def history(self, agent_id: str, task_id: str, limit: int = 20
                ) -> List[PrivateWorkingMemory]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_WORKING_MEMORY_REVISIONS} "
                "WHERE agent_id = ? AND task_id = ? "
                "ORDER BY revision DESC LIMIT ?",
                (agent_id, task_id, int(limit))).fetchall()
        return [PrivateWorkingMemory.from_dict(
            agent_id, task_id, json.loads(r["payload"])) for r in rows]

    # ------------------------------------------------------------
    def finalize(self, agent_id: str, task_id: str
                 ) -> Optional[PrivateWorkingMemory]:
        """
        任务结项：返回最终快照并删除当前行。

        🔒 历史 revisions 保留（append-only）。
        🔒 返回的 snapshot 是未来「迁移到短期记忆」的输入；
           本次不实现短期记忆存储。
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT payload FROM {TABLE_PRIVATE_WORKING_MEMORY} "
                "WHERE agent_id = ? AND task_id = ?",
                (agent_id, task_id)).fetchone()
            if row is None:
                return None
            memory = PrivateWorkingMemory.from_dict(
                agent_id, task_id, json.loads(row["payload"]))
            conn.execute(
                f"DELETE FROM {TABLE_PRIVATE_WORKING_MEMORY} "
                "WHERE agent_id = ? AND task_id = ?",
                (agent_id, task_id))
            return memory


__all__ = ["PrivateWorkingMemoryStore"]
