"""
CommitStore —— `commit_requests` 的持久化边界
==============================================

SQL 只出现在这里。`OutputCommitService` 只表达领域操作。

🔒 唯一索引保证：

    (task_id, iteration, snapshot_tree, output_repo, target_ref)

    同一个 Task 的同一个快照，不会因为重试 / 重复触发
    变成两个 commit request。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from commit.models import CommitRequest, CommitStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CommitStore:
    def __init__(self, db) -> None:
        self.db = db

    # ------------------------------------------------------------
    def create_or_get(
        self,
        *,
        task_id: str,
        iteration: int,
        snapshot_seq: int,
        snapshot_commit: str,
        snapshot_tree: str,
        mode: str,
        status: str,
        output_repo: str,
        target_ref: str,
        base_commit: str = "",
    ) -> CommitRequest:
        existing = self.find_by_key(
            task_id=task_id,
            iteration=iteration,
            snapshot_tree=snapshot_tree,
            output_repo=output_repo,
            target_ref=target_ref,
        )
        if existing is not None:
            return existing

        import uuid

        request_id = "commit_" + uuid.uuid4().hex[:16]
        now = _now()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO commit_requests ("
                    "  request_id, task_id, iteration, snapshot_seq,"
                    "  snapshot_commit, snapshot_tree, mode, status,"
                    "  output_repo, target_ref, base_commit, commit_sha,"
                    "  merge_sha, authorized_by, authorized_at, error,"
                    "  created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request_id, task_id, int(iteration),
                        int(snapshot_seq), snapshot_commit, snapshot_tree,
                        mode, status, output_repo, target_ref,
                        base_commit or None, None, None, None, None, None,
                        now, now,
                    ),
                )
        except sqlite3.IntegrityError:
            # 并发触发：另一个入口已经创建了同一条请求。
            existing = self.find_by_key(
                task_id=task_id,
                iteration=iteration,
                snapshot_tree=snapshot_tree,
                output_repo=output_repo,
                target_ref=target_ref,
            )
            if existing is not None:
                return existing
            raise

        created = self.find(request_id)
        if created is None:
            raise RuntimeError("commit request 写入后无法读取")
        return created

    # ------------------------------------------------------------
    def find(self, request_id: str) -> Optional[CommitRequest]:
        row = self.db.fetchone(
            "SELECT * FROM commit_requests WHERE request_id = ?",
            (str(request_id),),
        )
        return self._row(row) if row is not None else None

    def find_by_key(
        self,
        *,
        task_id: str,
        iteration: int,
        snapshot_tree: str,
        output_repo: str,
        target_ref: str,
    ) -> Optional[CommitRequest]:
        row = self.db.fetchone(
            "SELECT * FROM commit_requests WHERE task_id = ?"
            " AND iteration = ? AND snapshot_tree = ?"
            " AND output_repo = ? AND target_ref = ?",
            (
                str(task_id), int(iteration), str(snapshot_tree),
                str(output_repo), str(target_ref),
            ),
        )
        return self._row(row) if row is not None else None

    def find_pending(
        self,
        task_id: str,
        *,
        status: str = CommitStatus.AWAITING_HUMAN.value,
    ) -> List[CommitRequest]:
        rows = self.db.fetchall(
            "SELECT * FROM commit_requests WHERE task_id = ?"
            " AND status = ? ORDER BY created_at DESC, request_id DESC",
            (str(task_id), str(status)),
        )
        return [self._row(row) for row in rows]

    def list_pending_all(self, limit: int = 20) -> List[CommitRequest]:
        rows = self.db.fetchall(
            "SELECT * FROM commit_requests WHERE status = ?"
            " ORDER BY created_at DESC, request_id DESC LIMIT ?",
            (CommitStatus.AWAITING_HUMAN.value, max(1, int(limit))),
        )
        return [self._row(row) for row in rows]

    def list_for_task(self, task_id: str) -> List[CommitRequest]:
        rows = self.db.fetchall(
            "SELECT * FROM commit_requests WHERE task_id = ?"
            " ORDER BY created_at DESC, request_id DESC",
            (str(task_id),),
        )
        return [self._row(row) for row in rows]

    def list_recent(self, limit: int = 50) -> List[CommitRequest]:
        rows = self.db.fetchall(
            "SELECT * FROM commit_requests"
            " ORDER BY created_at DESC, request_id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [self._row(row) for row in rows]

    # ------------------------------------------------------------
    def update(self, request_id: str, **fields: Any) -> CommitRequest:
        allowed = {
            "status", "base_commit", "commit_sha", "merge_sha",
            "authorized_by", "authorized_at", "error",
        }
        updates = {
            key: value for key, value in fields.items()
            if key in allowed
        }
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [str(request_id)]
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE commit_requests SET {assignments}"
                " WHERE request_id = ?",
                tuple(values),
            )
        updated = self.find(request_id)
        if updated is None:
            raise RuntimeError(f"commit request 不存在：{request_id}")
        return updated

    # ------------------------------------------------------------
    @staticmethod
    def _row(row) -> CommitRequest:
        return CommitRequest(
            request_id=str(row["request_id"]),
            task_id=str(row["task_id"]),
            iteration=int(row["iteration"] or 0),
            snapshot_seq=int(row["snapshot_seq"] or 0),
            snapshot_commit=str(row["snapshot_commit"] or ""),
            snapshot_tree=str(row["snapshot_tree"] or ""),
            mode=str(row["mode"] or ""),
            status=str(row["status"] or ""),
            output_repo=str(row["output_repo"] or ""),
            target_ref=str(row["target_ref"] or ""),
            base_commit=str(row["base_commit"] or ""),
            commit_sha=str(row["commit_sha"] or ""),
            merge_sha=str(row["merge_sha"] or ""),
            authorized_by=str(row["authorized_by"] or ""),
            authorized_at=str(row["authorized_at"] or ""),
            error=str(row["error"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )


__all__ = ["CommitStore"]
