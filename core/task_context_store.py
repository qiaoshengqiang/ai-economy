"""
TaskContextStore —— Shared Context 的持久化（Step 4F）

============================================================
【和 TaskStore 的关系】
============================================================

完全平行的两个 store，共用同一个 `Database`：

    TaskStore           → tasks / task_offers / task_agreements / task_executions
    TaskContextStore    → task_shared_contexts / task_context_revisions
                          / task_context_changes

============================================================
【为什么不是「一个总 Store」】
============================================================

    ❌ 把 Shared Context 塞进 TaskStore

        TaskStore 已经是 958 行、7 张表。
        再加 3 张表和一套 Alignment 语义，它就会变成
        「什么都管的 God Store」—— 而 TaskService 曾经
        因为同样的原因被拆开过（见 ADR-0006）。

    ✅ 独立 store，共用 Database

        边界清楚：谁的 schema 谁维护。
        而且应用层可以按需注入其中一个。

============================================================
【没有创建 SharedContextDatabase】
============================================================

规范 §19 明确禁止。这里复用的就是现有 `Database`：
同一个连接、同一个 `transaction()`、同一个 `BEGIN IMMEDIATE`。
只是表和 SQL 是新的。

============================================================
【并发：沿用 TaskStore 已经验证过的 CAS】
============================================================

    UPDATE task_shared_contexts
       SET current_revision = ?
     WHERE task_id = ? AND current_revision = ?     ← 关键

    影响行数 1 → 成功
    影响行数 0 → 有人先改了 → ConcurrentContextModification

和 `TaskStore.save()` 是**同一个模式**，不是另一套并发系统（§12）。

============================================================
【三张表各自的写入规则（不一样，别记混）】
============================================================

    task_shared_contexts     **会 UPDATE** —— 但只改 current_revision
                             这一列。它是「当前指针」，不是历史。
                             而且 UPDATE 带 CAS 条件。

    task_context_revisions   **只 INSERT / SELECT** —— 真正的 append-only。
                             一版存下来就永不改写：
                             「Revision 18 取代 17」不等于「17 可以删掉」。

    task_context_changes     **会 UPDATE status 一次** —— 从 PROPOSED
                             变成 ACCEPTED 或 REJECTED（终态）。
                             那条 UPDATE 带 `AND status = 'PROPOSED'` 条件，
                             所以两个进程同时裁决时只有一个成功（防 TOCTOU）。
                             但**没有 DELETE** —— 被拒绝的提议要留着。

【为什么 changes 不是纯 append-only】
    因为「提议」是有生命周期的。如果每次裁决都新插一行，
    就会出现「同一个 change_id 有多行、状态各不相同」——
    查询时要先做一次「哪行是最新的」的推理，那是自找麻烦。

    而 revisions 不同：每一版都是**已经发生的事实**，
    没有「状态」可言，所以纯 append 是对的。

【守卫】
    · revisions：不许出现任何 UPDATE / DELETE
    · changes  ：不许出现 DELETE；唯一的 UPDATE 必须是那条 status 转移
                 （且必须带 `AND status = ?` 条件）
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Mapping, Optional

from core.task_shared_context import (
    apply_change,
    ContextChange,
    ContextChangeStatus,
    ContextNotFoundError,
    ContextRevision,
    ContextValidationError,
    ConcurrentContextModification,
    FIRST_REVISION,
    TaskSharedContext,
    utc_now_iso,
)
from storage.database import Database
from storage.models import (
    TABLE_CONTEXT_CHANGES,
    TABLE_CONTEXT_REVISIONS,
    TABLE_SHARED_CONTEXTS,
    TABLE_TASKS,
)


def new_context_id() -> str:
    """一个新的 context_id。"""
    return f"ctx-{uuid.uuid4().hex[:12]}"


def new_change_id() -> str:
    """一个新的 change_id。"""
    return f"chg-{uuid.uuid4().hex[:12]}"


class TaskContextStore:
    """
    Shared Context 的仓库。

    【职责】
        只做持久化 + 乐观锁。
        不判断「谁有资格提议」——那是应用层的事。
        不决定「这条变更意味着什么」——那是领域层的事
        （`apply_change`）。
    """

    #: 被允许的列（供 describe 用）
    def __init__(self, db: Database):
        self.db = db

    # ============================================================
    # 一、创建 / 读取 Context
    # ============================================================
    def create(
        self,
        task_id: str,
        *,
        created_by: str,
        context_id: Optional[str] = None,
        objective: str = "",
        constraints: Optional[List[str]] = None,
        acceptance_criteria: Optional[List[str]] = None,
        artifact_refs: Optional[List[str]] = None,
        terminology: Optional[Mapping[str, str]] = None,
        change_reason: str = "初始化共享上下文",
        now: Optional[str] = None,
    ) -> ContextRevision:
        """
        为一个 Task 建立 Shared Context，并产生 **Revision 1**。

        【为什么 Revision 1 也要进 revisions 表】
            否则历史会出现一个「空洞」：
            读到 Revision 2 时说 parent_revision=1，
            但 1 查不到。历史必须是连续的。

        【幂等性】
            同一个 task_id 调两次会抛 `ContextValidationError` ——
            因为 `task_shared_contexts` 的主键冲突。
            这里显式先查一次，给一个**清楚**的错误，
            而不是让调用方去看 sqlite 的 IntegrityError。
        """
        stamp = now or utc_now_iso()
        cid = context_id or new_context_id()

        context = TaskSharedContext(
            context_id=cid,
            task_id=task_id,
            revision=FIRST_REVISION,
            objective=objective,
            constraints=tuple(constraints or ()),
            acceptance_criteria=tuple(acceptance_criteria or ()),
            terminology=dict(terminology or {}),
            artifact_refs=tuple(artifact_refs or ()),
            change_reason=change_reason,
            created_by=created_by,
            created_at=stamp,
            parent_revision=None,
        )

        with self.db.transaction() as conn:
            existing = conn.execute(
                f"SELECT current_revision FROM {TABLE_SHARED_CONTEXTS} "
                f"WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if existing is not None:
                raise ContextValidationError(
                    f"Task {task_id!r} 已经有 Shared Context 了"
                    f"（当前 revision {existing['current_revision']}）。\n"
                    f"（一个 Task 只有一个 Context。要改内容请走 ContextChange。）"
                )

            conn.execute(
                f"""
                INSERT INTO {TABLE_SHARED_CONTEXTS}
                    (task_id, context_id, current_revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, cid, FIRST_REVISION, stamp, stamp),
            )

            self._insert_revision(conn, context, change_id=None)

        return ContextRevision(
            task_id=task_id,
            revision=FIRST_REVISION,
            context=context,
            created_at=stamp,
            created_by=created_by,
            parent_revision=None,
            change_id=None,
            change_reason=change_reason,
        )

    def current_revision_number(self, task_id: str) -> Optional[int]:
        """当前版本号。没有 Context 时返回 None。"""
        row = self.db.fetchone(
            f"SELECT current_revision FROM {TABLE_SHARED_CONTEXTS} "
            f"WHERE task_id = ?",
            (task_id,),
        )
        return int(row["current_revision"]) if row else None

    def has_context(self, task_id: str) -> bool:
        return self.current_revision_number(task_id) is not None

    def current(self, task_id: str) -> Optional[ContextRevision]:
        """当前有效的那一版。没有 Context 时返回 None。"""
        number = self.current_revision_number(task_id)
        if number is None:
            return None
        return self.revision(task_id, number)

    def require_current(self, task_id: str) -> ContextRevision:
        """当前版本；没有就抛 `ContextNotFoundError`。"""
        found = self.current(task_id)
        if found is None:
            raise ContextNotFoundError(
                f"Task {task_id!r} 还没有 Shared Context。"
            )
        return found

    def revision(self, task_id: str, revision: int) -> Optional[ContextRevision]:
        """
        读取**任意一版**（含历史版本）。

        【为什么历史版本必须可读】
            「Revision 7 是怎么来的」只有在能读出 1..7 时才回答得了。
            如果只保留当前版本，历史就被压缩成了一句
            「反正现在是这样」—— 那就失去了可追溯性。
        """
        row = self.db.fetchone(
            f"""
            SELECT task_id, revision, context_id, payload, created_at,
                   created_by, parent_revision, change_id, change_reason
              FROM {TABLE_CONTEXT_REVISIONS}
             WHERE task_id = ? AND revision = ?
            """,
            (task_id, revision),
        )
        if row is None:
            return None
        return self._row_to_revision(row)

    def history(self, task_id: str, *, limit: Optional[int] = None) -> List[ContextRevision]:
        """按版本号升序列出全部历史版本。"""
        sql = (
            f"SELECT task_id, revision, context_id, payload, created_at, "
            f"created_by, parent_revision, change_id, change_reason "
            f"FROM {TABLE_CONTEXT_REVISIONS} WHERE task_id = ? "
            f"ORDER BY revision ASC"
        )
        params: tuple = (task_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (task_id, limit)
        return [self._row_to_revision(r) for r in self.db.fetchall(sql, params)]

    def revision_count(self, task_id: str) -> int:
        row = self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM {TABLE_CONTEXT_REVISIONS} WHERE task_id = ?",
            (task_id,),
        )
        return int(row["n"]) if row else 0

    def count_contexts(self) -> int:
        row = self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM {TABLE_SHARED_CONTEXTS}"
        )
        return int(row["n"]) if row else 0

    # ============================================================
    # 二、Change
    # ============================================================
    def record_change(self, change: ContextChange) -> ContextChange:
        """
        写入一条 **PROPOSED** 的变更。

        【只写 PROPOSED】
            接受 / 拒绝走 `decide_change()` ——
            那条路径带 CAS，这条不带。分开是为了让「提议」
            成为一个廉价的、可以自由尝试的动作。
        """
        if not change.is_proposed:
            raise ContextValidationError(
                f"record_change 只接受 PROPOSED 的变更，"
                f"收到 status={change.status!r}。"
                f"（ACCEPTED/REJECTED 走 decide_change。）"
            )

        self.db.execute(
            f"""
            INSERT INTO {TABLE_CONTEXT_CHANGES}
                (change_id, task_id, proposed_by, change_type, target_revision,
                 payload, target, expected_value, reason, source, created_at,
                 status, decided_at, decided_by, decided_reason, result_revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change.change_id,
                change.task_id,
                change.proposed_by,
                change.change_type,
                change.target_revision,
                json.dumps(change.new_value, ensure_ascii=False, sort_keys=True),
                change.target,
                (
                    json.dumps(change.expected_value, ensure_ascii=False)
                    if change.expected_value is not None else None
                ),
                change.reason,
                change.source,
                change.created_at,
                change.status,
                change.decided_at,
                change.decided_by,
                change.decided_reason,
                change.result_revision,
            ),
        )
        return change

    def get_change(self, change_id: str) -> Optional[ContextChange]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_CONTEXT_CHANGES} WHERE change_id = ?",
            (change_id,),
        )
        return self._row_to_change(row) if row else None

    def require_change(self, change_id: str) -> ContextChange:
        found = self.get_change(change_id)
        if found is None:
            raise ContextNotFoundError(f"找不到 change_id={change_id!r}")
        return found

    def changes_for(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ContextChange]:
        """
        列出某个 Task 的变更（默认含**所有**状态）。

        【为什么默认包含 REJECTED】
            「当时提过但没通过」是有价值的历史 ——
            它能防止三周后有人再提一遍同样的方案。
        """
        sql = f"SELECT * FROM {TABLE_CONTEXT_CHANGES} WHERE task_id = ?"
        params: List[Any] = [task_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at ASC, change_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._row_to_change(r) for r in self.db.fetchall(sql, tuple(params))]

    def pending_changes(self, task_id: str) -> List[ContextChange]:
        """待裁决的提议。"""
        return self.changes_for(task_id, status=ContextChangeStatus.PROPOSED)

    def count_changes(self, task_id: Optional[str] = None) -> int:
        if task_id is None:
            row = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_CONTEXT_CHANGES}"
            )
        else:
            row = self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {TABLE_CONTEXT_CHANGES} WHERE task_id = ?",
                (task_id,),
            )
        return int(row["n"]) if row else 0

    # ============================================================
    # 三、裁决 + 提交新版本（原子）
    # ============================================================
    def decide_change(
        self,
        change_id: str,
        *,
        status: str,
        decided_by: str,
        decided_reason: str = "",
        result_revision: Optional[int] = None,
        now: Optional[str] = None,
    ) -> ContextChange:
        """
        把一条 PROPOSED 的变更裁决为 ACCEPTED / REJECTED。

        【为什么带条件 UPDATE 而不是先查后写】
            先 SELECT 看 status 是不是 PROPOSED，再 UPDATE ——
            两个进程同时裁决同一个 change 时都会看到 PROPOSED，
            然后都写成功。这就是 TOCTOU。

            所以用**一条带条件的 UPDATE**：

                UPDATE ... SET status = ?
                 WHERE change_id = ? AND status = 'PROPOSED'

            影响行数 0 → 别人已经裁决过了 → 明确报错。

            这和 Phase 0 修消息认领时用的是同一个手法。
        """
        if status not in ContextChangeStatus.terminal():
            raise ContextValidationError(
                f"decide_change 的 status 必须是 "
                f"{list(ContextChangeStatus.terminal())} 之一，收到 {status!r}"
            )

        stamp = now or utc_now_iso()

        cursor = self.db.execute(
            f"""
            UPDATE {TABLE_CONTEXT_CHANGES}
               SET status = ?, decided_at = ?, decided_by = ?,
                   decided_reason = ?, result_revision = ?
             WHERE change_id = ? AND status = ?
            """,
            (
                status,
                stamp,
                decided_by,
                decided_reason,
                result_revision,
                change_id,
                ContextChangeStatus.PROPOSED,
            ),
        )

        if cursor.rowcount == 0:
            existing = self.get_change(change_id)
            if existing is None:
                raise ContextNotFoundError(f"找不到 change_id={change_id!r}")
            raise ContextValidationError(
                f"change {change_id!r} 已经被裁决过了"
                f"（当前 status={existing.status!r}）。\n"
                f"（ACCEPTED / REJECTED 是终态 —— 想改就提一条新的变更。）"
            )

        decided = self.require_change(change_id)
        if decided.status != status:  # pragma: no cover —— 防御性
            raise ContextValidationError(
                f"裁决后状态不一致：期望 {status!r}，实际 {decided.status!r}"
            )
        return decided

    def commit_revision(
        self,
        revision: ContextRevision,
        *,
        expected_revision: int,
        now: Optional[str] = None,
    ) -> ContextRevision:
        """
        提交一个新版本 —— **带乐观锁**。

        ============================================================
        【这是 4F 并发正确性的核心，只有 6 行 SQL 是关键】
        ============================================================

            ① UPDATE task_shared_contexts
                 SET current_revision = <new>
               WHERE task_id = ? AND current_revision = <expected>

               影响行数 0 → 有人在你之前推进了版本 → 冲突，中止

            ② INSERT INTO task_context_revisions   （append-only）

            两步在**同一个事务**里。

        ------------------------------------------------------------
        【为什么必须先 UPDATE 再 INSERT】
        ------------------------------------------------------------

            顺序反过来的话，冲突时那条 revision 已经写进去了 ——
            你会留下一条**孤立的历史版本**：
            它的 parent_revision 指向一个不再是「当前」的版本，
            而当前指针从没指向过它。

            先 UPDATE 拿到「锁」再 INSERT，
            冲突时整个事务回滚，什么都不留。

        ------------------------------------------------------------
        【为什么不自动重试 / 合并】
        ------------------------------------------------------------

            规范 §13 明确禁止。冲突时抛出明确的领域错误，
            由调用方**显式**基于新版本重新提议。

            自动重试会让「共同认知」变成一个自己会变的东西 ——
            而共同认知的全部价值就在于「它是什么就是什么」。
        """
        stamp = now or utc_now_iso()

        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {TABLE_SHARED_CONTEXTS}
                   SET current_revision = ?, updated_at = ?
                 WHERE task_id = ? AND current_revision = ?
                """,
                (revision.revision, stamp, revision.task_id, expected_revision),
            )

            if cursor.rowcount == 0:
                # 冲突。读一下当前值，好把错误信息写清楚。
                row = conn.execute(
                    f"SELECT current_revision FROM {TABLE_SHARED_CONTEXTS} "
                    f"WHERE task_id = ?",
                    (revision.task_id,),
                ).fetchone()

                if row is None:
                    raise ContextNotFoundError(
                        f"Task {revision.task_id!r} 还没有 Shared Context"
                    )

                raise ConcurrentContextModification(
                    task_id=revision.task_id,
                    expected_revision=expected_revision,
                    actual_revision=int(row["current_revision"]),
                )

            self._insert_revision(
                conn, revision.context, change_id=revision.change_id
            )

        return revision

    def commit_accepted_change(
        self,
        change: ContextChange,
        *,
        decided_by: str,
        new_revision: int,
        decided_reason: str = "",
        now: Optional[str] = None,
    ) -> ContextRevision:
        """
        **原子地**：批准变更 → 应用它 → 提交新版本。

        ============================================================
        【为什么这个方法在 store 里，而不是在 service 里】
        ============================================================

            最初它写在 `TaskAlignmentService.accept()` 里，那个方法
            需要 `with self.db.transaction():` —— 于是 service 不得不
            import `storage.database`。

            那会让依赖图变成：

                services → storage        ❌

            而 AGENTS.md 明确写着（并且有守卫在盯）：

                services → core + communication
                （不直接依赖 storage）

            所以把事务边界**下移到 store**：
            store 本来就在 core/ 里、本来就合法地依赖 storage
            （TaskStore 也持有 Database）。

            于是 service 完全不需要知道 Database 的存在：
            它只调 `store.commit_accepted_change(...)`。

            这既修好了依赖方向，也让「事务边界」这件事
            只出现在一个地方 —— 而不是散在两层。

        【为什么必须是一个事务】
            如果 decide 提交了而 commit_revision 失败：

                change.status = ACCEPTED
                但 current_revision 没推进

            → 半成功。而且失败者会留下一个「已批准但没生效」的变更，
              下次有人在 UI 上看到它是 ACCEPTED，会以为已经生效了。

            这正是 Step 4F 开发中**真实踩到**的问题：
            它顺带暴露了 `Database.execute()` 在事务里会提前提交的缺口
            （见 storage/database.py 里那段说明）。
        """
        stamp = now or utc_now_iso()

        with self.db.transaction():
            decided = self.decide_change(
                change.change_id,
                status=ContextChangeStatus.ACCEPTED,
                decided_by=decided_by,
                decided_reason=decided_reason,
                result_revision=new_revision,
                now=stamp,
            )

            current_context = self.require_current(change.task_id).context

            new_context = apply_change(
                current_context,
                decided,
                accepted_by=decided_by,
                new_revision=new_revision,
                decided_at=stamp,
                decided_reason=decided_reason,
            )

            revision = ContextRevision(
                task_id=change.task_id,
                revision=new_revision,
                context=new_context,
                created_at=stamp,
                created_by=decided_by,
                parent_revision=current_context.revision,
                change_id=change.change_id,
                change_reason=decided_reason or change.reason,
            )

            self.commit_revision(
                revision,
                expected_revision=change.target_revision,
                now=stamp,
            )

        return revision

    # ============================================================
    # 四、内部
    # ============================================================
    @staticmethod
    def _insert_revision(
        conn, context: TaskSharedContext, *, change_id: Optional[str]
    ) -> None:
        """
        写入一条历史版本。

        【append-only】
            这里只有 INSERT。整个模块里**不存在**
            `UPDATE task_context_revisions` 或
            `DELETE FROM task_context_revisions`。
        """
        conn.execute(
            f"""
            INSERT INTO {TABLE_CONTEXT_REVISIONS}
                (task_id, revision, context_id, payload, created_at,
                 created_by, parent_revision, change_id, change_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.task_id,
                context.revision,
                context.context_id,
                json.dumps(context.to_dict(), ensure_ascii=False, sort_keys=True),
                context.created_at,
                context.created_by,
                context.parent_revision,
                change_id,
                context.change_reason,
            ),
        )

    @staticmethod
    def _row_to_revision(row) -> ContextRevision:
        payload = json.loads(row["payload"])
        return ContextRevision(
            task_id=row["task_id"],
            revision=int(row["revision"]),
            context=TaskSharedContext.from_dict(payload),
            created_at=row["created_at"] or "",
            created_by=row["created_by"] or "",
            parent_revision=row["parent_revision"],
            change_id=row["change_id"],
            change_reason=row["change_reason"] or "",
        )

    @staticmethod
    def _row_to_change(row) -> ContextChange:
        return ContextChange(
            change_id=row["change_id"],
            task_id=row["task_id"],
            proposed_by=row["proposed_by"],
            change_type=row["change_type"],
            target_revision=int(row["target_revision"]),
            new_value=json.loads(row["payload"]) if row["payload"] else None,
            target=row["target"],
            expected_value=(
                json.loads(row["expected_value"])
                if row["expected_value"] else None
            ),
            reason=row["reason"] or "",
            source=row["source"] or "",
            created_at=row["created_at"] or "",
            status=row["status"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            decided_reason=row["decided_reason"] or "",
            result_revision=row["result_revision"],
        )

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "contexts": self.count_contexts(),
            "changes": self.count_changes(),
        }

    def __repr__(self) -> str:
        info = self.describe()
        return (
            f"<TaskContextStore contexts={info['contexts']} "
            f"changes={info['changes']}>"
        )
