"""
WorkspaceStore —— Workspace Core 的**持久化唯一边界**
============================================================

【为什么需要一个 store，而不是让 Gateway 直接写 SQL】

    与 `core/task_store.py` 同样的理由：

        · SQL 只出现在一个地方，schema 变化不会散落各处
        · Gateway 只表达「领域操作」，不表达「表结构」
        · 领域对象（Resource / ExplicitLock）与行之间只有一个转换点

【🔒 本文件的两条硬约束】

    ① **只有 Explicit Lock 落盘。**
       `Effective Lock` / `Effective Controller` **没有任何列** ——
       它们由 `ResourceTree` 在运行时派生。
       这直接满足任务书 §48「不要维护两份会漂移的事实」。

    ② **所有写操作都要求调用方传入 `conn`。**
       store 本身**不开事务** —— 事务边界由 Gateway 掌握。
       因为「检查 + 修改」必须原子，而那需要
       「读资源树」与「写回」在**同一个事务**里。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from core.workspace_core import (
    ExplicitLock,
    LockKind,
    Resource,
    ResourceState,
    ResourceTree,
    ResourceType,
    normalize_path,
)


def _now() -> str:
    """统一的 UTC 时间戳（与项目其他模块保持一致的 ISO 形式）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_resource_id() -> str:
    """
    生成一个**稳定**的 resource_id。

    🔒 rename / move 之后这个 id **不变** ——
       它是身份，不是位置（任务书 §29）。
    """
    return f"RES-{uuid.uuid4().hex[:12]}"


class WorkspaceStore:
    """Workspace Core 的表访问层（无事务、无业务判断）。"""

    def __init__(self, db) -> None:
        self.db = db

    # ==========================================================
    # 读
    # ==========================================================
    def get_resource(self, resource_id: str) -> Optional[Resource]:
        row = self.db.fetchone(
            "SELECT * FROM workspace_resources WHERE resource_id = ?",
            (resource_id,),
        )
        return self._to_resource(row) if row else None

    def find_by_path(
        self, workspace_id: str, path: str
    ) -> Optional[Resource]:
        row = self.db.fetchone(
            "SELECT * FROM workspace_resources "
            "WHERE workspace_id = ? AND path = ?",
            (workspace_id, normalize_path(path)),
        )
        return self._to_resource(row) if row else None

    def children_of(self, parent_id: Optional[str]) -> list[Resource]:
        if parent_id is None:
            return []
        rows = self.db.fetchall(
            "SELECT * FROM workspace_resources WHERE parent_id = ? "
            "ORDER BY name",
            (parent_id,),
        )
        return [self._to_resource(r) for r in rows]

    def all_resources(self, workspace_id: str) -> list[Resource]:
        rows = self.db.fetchall(
            "SELECT * FROM workspace_resources WHERE workspace_id = ?",
            (workspace_id,),
        )
        return [self._to_resource(r) for r in rows]

    def all_explicit_locks(self, workspace_id: str) -> list[ExplicitLock]:
        """
        取一个 workspace 下的**全部显式锁**。

        ⚠️ 通过 JOIN 限定在 workspace 内 ——
           不能把别的 workspace 的锁带进来，
           否则跨 workspace 的资源树会互相污染。
        """
        rows = self.db.fetchall(
            "SELECT l.* FROM workspace_explicit_locks l "
            "JOIN workspace_resources r ON r.resource_id = l.resource_id "
            "WHERE r.workspace_id = ?",
            (workspace_id,),
        )
        return [self._to_lock(r) for r in rows]

    def load_tree(self, workspace_id: str) -> ResourceTree:
        """
        读出**一次快照**并构造 `ResourceTree`。

        🔒 Gateway 必须在**同一个事务**里调用它，
           这样解析用的数据与被修改的数据同源。
        """
        return ResourceTree(
            self.all_resources(workspace_id),
            self.all_explicit_locks(workspace_id),
        )

    def explicit_lock_of(self, resource_id: str) -> Optional[ExplicitLock]:
        row = self.db.fetchone(
            "SELECT * FROM workspace_explicit_locks WHERE resource_id = ?",
            (resource_id,),
        )
        return self._to_lock(row) if row else None

    # ==========================================================
    # 写（全部要求调用方已开事务）
    # ==========================================================
    def create_resource(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        parent: Optional[Resource],
        name: str,
        resource_type: ResourceType,
        path: str,
        created_by: str,
        content: Optional[str] = None,
    ) -> Resource:
        """
        插入一个新资源。

        🔒 **不写 explicit lock**（任务书 §11）——
           新资源隐式继承父目录责任。
           创建者已经持有父目录的 Directory Lock，
           再记一条指向自己的显式锁是冗余。
        """
        resource_id = _new_resource_id()
        now = _now()
        conn.execute(
            "INSERT INTO workspace_resources ("
            "  resource_id, workspace_id, parent_id, name, path,"
            "  resource_type, state, revision, content,"
            "  created_by, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                resource_id,
                workspace_id,
                parent.resource_id if parent else None,
                name,
                normalize_path(path),
                resource_type.value,
                ResourceState.ACTIVE.value,
                1,
                content,
                created_by,
                now,
                now,
            ),
        )
        return Resource(
            resource_id=resource_id,
            workspace_id=workspace_id,
            parent_id=parent.resource_id if parent else None,
            name=name,
            path=normalize_path(path),
            resource_type=resource_type,
            state=ResourceState.ACTIVE,
            revision=1,
            content=content,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )

    def update_resource(
        self,
        conn: sqlite3.Connection,
        resource: Resource,
        *,
        content: Optional[str] = None,
        path: Optional[str] = None,
        name: Optional[str] = None,
        parent_id: Optional[str] = None,
        state: Optional[ResourceState] = None,
        revision: Optional[int] = None,
    ) -> Resource:
        """
        更新资源（**调用方必须已经确认过 revision**）。

        ⚠️ 这个函数**不做**乐观锁检查 —— 那是 Gateway 的职责
           （`base_revision == current` 必须在同一个事务里校验）。
           这里只负责「把已经决定好的新状态写下去」。
        """
        now = _now()
        new_content = resource.content if content is None else content
        new_path = normalize_path(path) if path is not None else resource.path
        new_name = name if name is not None else resource.name
        new_parent = parent_id if parent_id is not None else resource.parent_id
        new_state = state if state is not None else resource.state
        new_revision = revision if revision is not None else resource.revision

        conn.execute(
            "UPDATE workspace_resources SET "
            "  parent_id = ?, name = ?, path = ?, state = ?,"
            "  revision = ?, content = ?, updated_at = ? "
            "WHERE resource_id = ?",
            (
                new_parent,
                new_name,
                new_path,
                new_state.value,
                new_revision,
                new_content,
                now,
                resource.resource_id,
            ),
        )
        return Resource(
            resource_id=resource.resource_id,
            workspace_id=resource.workspace_id,
            parent_id=new_parent,
            name=new_name,
            path=new_path,
            resource_type=resource.resource_type,
            state=new_state,
            revision=new_revision,
            content=new_content,
            created_by=resource.created_by,
            created_at=resource.created_at,
            updated_at=now,
        )

    def set_explicit_lock(
        self,
        conn: sqlite3.Connection,
        *,
        resource_id: str,
        holder: str,
        kind: LockKind,
        acquired_by: str,
    ) -> ExplicitLock:
        """
        建立 / 覆盖一条显式锁。

        🔒 `resource_id` 是主键 ⇒ **同一资源最多一条** ⇒
           同一 File 不可能有两个 Explicit Holder。
        """
        now = _now()
        conn.execute(
            "INSERT INTO workspace_explicit_locks "
            "  (resource_id, holder, kind, acquired_at, acquired_by) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(resource_id) DO UPDATE SET "
            "  holder = excluded.holder,"
            "  kind = excluded.kind,"
            "  acquired_at = excluded.acquired_at,"
            "  acquired_by = excluded.acquired_by",
            (resource_id, holder, kind.value, now, acquired_by),
        )
        return ExplicitLock(
            resource_id=resource_id,
            holder=holder,
            kind=kind,
            acquired_at=now,
            acquired_by=acquired_by,
        )

    def clear_explicit_lock(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> None:
        """移除显式锁（用于 `release()`）—— 之后 Effective Lock 重新派生。"""
        conn.execute(
            "DELETE FROM workspace_explicit_locks WHERE resource_id = ?",
            (resource_id,),
        )

    # ----------------------------------------------------------
    # Revision / Audit / Event
    # ----------------------------------------------------------
    def add_revision(
        self,
        conn: sqlite3.Connection,
        *,
        resource_id: str,
        workspace_id: str,
        revision_number: int,
        created_by: str,
        change_type: str,
        change_ref: Optional[str] = None,
    ) -> str:
        revision_id = f"REV-{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO workspace_revisions ("
            "  revision_id, resource_id, workspace_id, revision_number,"
            "  created_by, created_at, change_type, change_ref"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (
                revision_id, resource_id, workspace_id, revision_number,
                created_by, _now(), change_type, change_ref,
            ),
        )
        return revision_id

    def add_audit(
        self,
        conn: sqlite3.Connection,
        *,
        operation_id: str,
        workspace_id: str,
        action: str,
        actor: str,
        result: str,
        resource_id: Optional[str] = None,
        error_code: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        """
        写一条 Audit。

        🔒 **失败的尝试也要写**（任务书 §20 / §30）——
           它回答「谁做了什么、结果如何」。
        """
        audit_id = f"AUD-{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO workspace_audits ("
            "  audit_id, operation_id, workspace_id, action, actor,"
            "  resource_id, result, error_code, detail, task_id, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                audit_id, operation_id, workspace_id, action, actor,
                resource_id, result, error_code,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                task_id, _now(),
            ),
        )
        return audit_id

    def add_event(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        event_type: str,
        actor: str,
        resource_id: Optional[str] = None,
        revision: Optional[int] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        写一条 Workspace Event。

        🔒 **只记录成功的状态变化** —— 没有 `*_FAILED`。
        🔒 `detail` **不含文件内容**（任务书 §20）——
           Event 是「发生了什么」的通知，不是内容分发渠道。
        """
        event_id = f"EVT-{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO workspace_events ("
            "  event_id, workspace_id, event_type, resource_id,"
            "  revision, actor, detail, created_at"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (
                event_id, workspace_id, event_type, resource_id,
                revision, actor,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                _now(),
            ),
        )
        return event_id

    # ----------------------------------------------------------
    # 查询（测试与未来 Runtime 订阅用）
    # ----------------------------------------------------------
    def audits_for(
        self, workspace_id: str, *, resource_id: Optional[str] = None
    ) -> list[sqlite3.Row]:
        if resource_id:
            return self.db.fetchall(
                "SELECT * FROM workspace_audits "
                "WHERE workspace_id = ? AND resource_id = ? "
                "ORDER BY created_at, audit_id",
                (workspace_id, resource_id),
            )
        return self.db.fetchall(
            "SELECT * FROM workspace_audits WHERE workspace_id = ? "
            "ORDER BY created_at, audit_id",
            (workspace_id,),
        )

    def events_for(
        self, workspace_id: str, *, resource_id: Optional[str] = None
    ) -> list[sqlite3.Row]:
        if resource_id:
            return self.db.fetchall(
                "SELECT * FROM workspace_events "
                "WHERE workspace_id = ? AND resource_id = ? "
                "ORDER BY created_at, event_id",
                (workspace_id, resource_id),
            )
        return self.db.fetchall(
            "SELECT * FROM workspace_events WHERE workspace_id = ? "
            "ORDER BY created_at, event_id",
            (workspace_id,),
        )

    def revisions_for(self, resource_id: str) -> list[sqlite3.Row]:
        return self.db.fetchall(
            "SELECT * FROM workspace_revisions WHERE resource_id = ? "
            "ORDER BY revision_number",
            (resource_id,),
        )

    # ==========================================================
    # 行 → 领域对象
    # ==========================================================
    @staticmethod
    def _to_resource(row: sqlite3.Row) -> Resource:
        return Resource(
            resource_id=row["resource_id"],
            workspace_id=row["workspace_id"],
            parent_id=row["parent_id"],
            name=row["name"],
            path=row["path"],
            resource_type=ResourceType(row["resource_type"]),
            state=ResourceState(row["state"]),
            revision=row["revision"],
            content=row["content"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_lock(row: sqlite3.Row) -> ExplicitLock:
        return ExplicitLock(
            resource_id=row["resource_id"],
            holder=row["holder"],
            kind=LockKind(row["kind"]),
            acquired_at=row["acquired_at"],
            acquired_by=row["acquired_by"],
        )
