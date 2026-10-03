"""
WorktreeStore —— Worktree 的持久化边界
============================================================

与 `core/workspace_store.py` / `core/task_store.py` 同一定位：

    SQL 只出现在这里；`WorktreeManager` 只表达**领域操作**。

【🔒 本文件里最重要的一条 SQL】

    `idx_worktrees_one_live_per_workspace`
        —— 建在 `git_worktrees(workspace_id)` 上的
           **部分唯一索引**，条件是
           `state IN ('provisioning','ready','active')`。

    它就是「同一 Workspace 最多一个 live worktree」的**全部保证**
    （任务书 §17）。没有应用层锁，没有全局锁 ——
    数据库自己保证原子性。

    ✅ 允许：一个 live + N 个 archived（历史）
    ❌ 拒绝：两个 live

【为什么 store 不开事务】

    与 workspace_store 同样的理由：「检查 + 修改」必须原子，
    而那需要把「读 state」与「写 state」放进**同一个事务**。
    事务边界由 `WorktreeManager` 掌握。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from change.worktree import (
    LIVE_STATES,
    IntentKind,
    IntentState,
    WorktreeIntent,
    WorktreeRecord,
    WorktreeState,
    new_intent_id,
    new_worktree_id,
)


class DuplicateLiveWorktree(RuntimeError):
    """
    这个 Workspace 已经有 live worktree 了。

    🔒 **由数据库的唯一约束抛出，不是应用层查出来的** ——
       这正是并发正确性的来源（任务书 §17）。
    """

    def __init__(self, workspace_id: str, existing_id: Optional[str] = None):
        self.workspace_id = workspace_id
        self.existing_id = existing_id
        super().__init__(
            f"Workspace {workspace_id} 已经有一个 live worktree"
            + (f"（{existing_id}）" if existing_id else "")
        )


class DuplicateOperation(RuntimeError):
    """
    这个 `idempotency_key` 已经有 intent 了（§10）。

    🔒 **同样是数据库唯一约束抛的** ——
       重试不会变成第二个 Git worktree。
    """

    def __init__(self, key: str, existing: Optional[WorktreeIntent] = None):
        self.key = key
        self.existing = existing
        super().__init__(f"这个操作已经执行过（idempotency_key={key}）")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorktreeStore:
    """`git_worktrees` / `worktree_intents` 的表访问层。"""

    def __init__(self, db) -> None:
        self.db = db

    # ==========================================================
    # Intent（§9 / §10）
    # ==========================================================
    def create_intent(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        kind: IntentKind,
        idempotency_key: str,
        operation_id: str,
        worktree_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> WorktreeIntent:
        """
        写一条 **PENDING** intent —— 必须在副作用**之前**调用。

        🔒 同一个 `idempotency_key` 第二次插入会被唯一索引拒绝，
           抛 `DuplicateOperation`（而不是产生第二条 intent）。
        """
        intent_id = new_intent_id()
        now = _now()
        try:
            conn.execute(
                "INSERT INTO worktree_intents ("
                "  intent_id, workspace_id, kind, state, idempotency_key,"
                "  operation_id, worktree_id, payload, last_error,"
                "  created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent_id, workspace_id, kind.value,
                    IntentState.PENDING.value, idempotency_key,
                    operation_id, worktree_id,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    None, now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateOperation(
                idempotency_key, self.find_intent_by_key(idempotency_key)
            ) from exc

        return WorktreeIntent(
            intent_id=intent_id,
            workspace_id=workspace_id,
            kind=kind,
            state=IntentState.PENDING,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            worktree_id=worktree_id,
            payload=payload,
            created_at=now,
            updated_at=now,
        )

    def resolve_intent(
        self,
        conn: sqlite3.Connection,
        intent_id: str,
        *,
        state: IntentState,
        error: Optional[str] = None,
        worktree_id: Optional[str] = None,
    ) -> None:
        """把 intent 推到终态（completed / failed / abandoned）。"""
        conn.execute(
            "UPDATE worktree_intents SET state = ?, last_error = ?,"
            "  worktree_id = COALESCE(?, worktree_id), updated_at = ?"
            " WHERE intent_id = ?",
            (state.value, error, worktree_id, _now(), intent_id),
        )

    def find_intent(self, intent_id: str) -> Optional[WorktreeIntent]:
        row = self.db.fetchone(
            "SELECT * FROM worktree_intents WHERE intent_id = ?", (intent_id,))
        return self._to_intent(row) if row else None

    def find_intent_by_key(self, key: str) -> Optional[WorktreeIntent]:
        row = self.db.fetchone(
            "SELECT * FROM worktree_intents WHERE idempotency_key = ?", (key,))
        return self._to_intent(row) if row else None

    def unresolved_intents(self) -> list[WorktreeIntent]:
        """
        所有**没有走到终态**的 intent。

        🔒 这是 startup reconciliation 的入口（§12 触发方式 1）。
        """
        rows = self.db.fetchall(
            "SELECT * FROM worktree_intents WHERE state = ? "
            "ORDER BY created_at, intent_id",
            (IntentState.PENDING.value,),
        )
        return [self._to_intent(r) for r in rows]

    def unresolved_intent_for(self, workspace_id: str) -> Optional[WorktreeIntent]:
        """这个 Workspace 有没有未解决的 intent（§12 触发方式 2）。"""
        rows = self.db.fetchall(
            "SELECT * FROM worktree_intents "
            "WHERE workspace_id = ? AND state = ? "
            "ORDER BY created_at, intent_id LIMIT 1",
            (workspace_id, IntentState.PENDING.value),
        )
        return self._to_intent(rows[0]) if rows else None

    def intents_for(self, workspace_id: str) -> list[WorktreeIntent]:
        rows = self.db.fetchall(
            "SELECT * FROM worktree_intents WHERE workspace_id = ? "
            "ORDER BY created_at, intent_id",
            (workspace_id,),
        )
        return [self._to_intent(r) for r in rows]

    # ==========================================================
    # Worktree
    # ==========================================================
    def insert_worktree(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        repository_ref: str,
        base_commit: str,
        workspace_revision: int,
        state: WorktreeState,
        physical_path: Optional[str] = None,
        git_ref: Optional[str] = None,
        task_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
        worktree_id: Optional[str] = None,
    ) -> WorktreeRecord:
        """
        插入一条 worktree 记录。

        ⚠️ **`physical_path` 只允许在本表与内部日志里出现。**
        """
        wid = worktree_id or new_worktree_id()
        now = _now()
        try:
            conn.execute(
                "INSERT INTO git_worktrees ("
                "  worktree_id, workspace_id, task_id, repository_ref,"
                "  base_commit, workspace_revision, git_ref, state,"
                "  physical_path, detail, created_at, updated_at,"
                "  last_verified_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wid, workspace_id, task_id, repository_ref,
                    base_commit, workspace_revision, git_ref, state.value,
                    physical_path,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    now, now, None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            live = self.find_live(workspace_id)
            raise DuplicateLiveWorktree(
                workspace_id, live.worktree_id if live else None
            ) from exc

        return WorktreeRecord(
            worktree_id=wid,
            workspace_id=workspace_id,
            task_id=task_id,
            repository_ref=repository_ref,
            base_commit=base_commit,
            workspace_revision=workspace_revision,
            git_ref=git_ref,
            state=state,
            physical_path=physical_path,
            created_at=now,
            updated_at=now,
            detail=detail,
        )

    def update_state(
        self,
        conn: sqlite3.Connection,
        worktree_id: str,
        *,
        state: WorktreeState,
        physical_path: Optional[str] = None,
        git_ref: Optional[str] = None,
        base_commit: Optional[str] = None,
        workspace_revision: Optional[int] = None,
        detail: Optional[dict[str, Any]] = None,
        verified: bool = False,
    ) -> None:
        """
        更新状态。

        ⚠️ 从**不一致状态**（inconsistent / orphaned）**不会**自动回到
           ready —— 必须经显式 reconciliation（§11 / §13）。
           这条规则由 `WorktreeManager` 保证，store 只负责写。
        """
        now = _now()
        conn.execute(
            "UPDATE git_worktrees SET "
            "  state = ?,"
            "  physical_path = COALESCE(?, physical_path),"
            "  git_ref = COALESCE(?, git_ref),"
            "  base_commit = COALESCE(?, base_commit),"
            "  workspace_revision = COALESCE(?, workspace_revision),"
            "  detail = COALESCE(?, detail),"
            "  updated_at = ?,"
            "  last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END "
            "WHERE worktree_id = ?",
            (
                state.value, physical_path, git_ref, base_commit,
                workspace_revision,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                now, 1 if verified else 0, now, worktree_id,
            ),
        )

    def get(self, worktree_id: str) -> Optional[WorktreeRecord]:
        row = self.db.fetchone(
            "SELECT * FROM git_worktrees WHERE worktree_id = ?", (worktree_id,))
        return self._to_record(row) if row else None

    def find_live(self, workspace_id: str) -> Optional[WorktreeRecord]:
        """这个 Workspace 的 live worktree（若有）。"""
        rows = self.db.fetchall(
            "SELECT * FROM git_worktrees WHERE workspace_id = ? "
            "AND state IN ('provisioning','ready','active') "
            "ORDER BY created_at LIMIT 1",
            (workspace_id,),
        )
        return self._to_record(rows[0]) if rows else None

    def list_for_workspace(self, workspace_id: str) -> list[WorktreeRecord]:
        rows = self.db.fetchall(
            "SELECT * FROM git_worktrees WHERE workspace_id = ? "
            "ORDER BY created_at, worktree_id",
            (workspace_id,),
        )
        return [self._to_record(r) for r in rows]

    def list_all(self) -> list[WorktreeRecord]:
        rows = self.db.fetchall(
            "SELECT * FROM git_worktrees ORDER BY created_at, worktree_id")
        return [self._to_record(r) for r in rows]

    def list_by_state(self, *states: WorktreeState) -> list[WorktreeRecord]:
        if not states:
            return []
        marks = ",".join("?" * len(states))
        rows = self.db.fetchall(
            f"SELECT * FROM git_worktrees WHERE state IN ({marks}) "
            f"ORDER BY created_at, worktree_id",
            tuple(s.value for s in states),
        )
        return [self._to_record(r) for r in rows]

    def find_by_physical_path(self, path: str) -> Optional[WorktreeRecord]:
        """
        按物理路径反查 —— **reconciliation 专用**（§11 情况 C）。

        ⚠️ 这个方法的返回值**含物理路径**，绝不能被 Agent-facing 层直接透出。
        """
        row = self.db.fetchone(
            "SELECT * FROM git_worktrees WHERE physical_path = ?", (path,))
        return self._to_record(row) if row else None

    # ==========================================================
    # 行 → 领域对象
    # ==========================================================
    @staticmethod
    def _to_record(row: sqlite3.Row) -> WorktreeRecord:
        return WorktreeRecord(
            worktree_id=row["worktree_id"],
            workspace_id=row["workspace_id"],
            task_id=row["task_id"],
            repository_ref=row["repository_ref"],
            base_commit=row["base_commit"],
            workspace_revision=row["workspace_revision"],
            git_ref=row["git_ref"],
            state=WorktreeState(row["state"]),
            physical_path=row["physical_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_verified_at=row["last_verified_at"],
            detail=json.loads(row["detail"]) if row["detail"] else None,
        )

    @staticmethod
    def _to_intent(row: sqlite3.Row) -> WorktreeIntent:
        return WorktreeIntent(
            intent_id=row["intent_id"],
            workspace_id=row["workspace_id"],
            kind=IntentKind(row["kind"]),
            state=IntentState(row["state"]),
            idempotency_key=row["idempotency_key"],
            operation_id=row["operation_id"],
            worktree_id=row["worktree_id"],
            payload=json.loads(row["payload"]) if row["payload"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )

    # ==========================================================
    # Event（§25 —— 自己的事件流，不塞进 Workspace Event）
    # ==========================================================
    def add_event(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        event_type: str,
        actor: str,
        worktree_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        写一条 Worktree Event。

        🔒 **`physical_path` 绝不能进 detail** ——
           事件可能被 subscribe 到 Agent 侧。
           （`WorktreeManager` 在构造 detail 时保证这一点。）
        """
        import uuid as _uuid
        event_id = f"WTE-{_uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO worktree_events ("
            "  event_id, workspace_id, event_type, worktree_id,"
            "  actor, detail, created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                event_id, workspace_id, event_type, worktree_id, actor,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                _now(),
            ),
        )
        return event_id

    def events_for(self, workspace_id: str) -> list[sqlite3.Row]:
        return self.db.fetchall(
            "SELECT * FROM worktree_events WHERE workspace_id = ? "
            "ORDER BY created_at, event_id",
            (workspace_id,),
        )
