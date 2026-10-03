"""
Workspace 测试脚手架（Hardening + 20 Agent 并发共用）
============================================================

【为什么需要它，而不是每个测试文件各写一份】

    三类测试对「环境」的要求**互相冲突**：

        纯单元测试         → 内存库最干净
        reopen / 持久化     → **必须**文件库（内存库关掉就没了）
        真实并发           → 文件库 + **每线程独立连接**

【🔒 为什么并发必须「每线程一个 Database」】

    这是本项目**已记录的技术债 #2**：

        `Database._tx_depth` 是**实例状态** ——
        跨线程共享会被交错修改 → 事务边界错乱。

    第一次写并发测试时我共用一个内存 `Database`，
    在全量测试（有负载）下**偶发失败**：某线程抛异常、
    结果留在 `None`。那就是这条技术债的现场。

    正确做法（本脚手架做的事）：

        · 用**文件**数据库（`:memory:` 每条连接是独立的库）
        · 每个线程 `new_db()` 拿**自己**的连接
        · 但**共享** `ResourceLockRegistry` ——
          资源级互斥是**进程内**概念，必须共享才起作用
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Optional

from services.workspace_concurrency import ResourceLockRegistry
from services.workspace_gateway import WorkspaceGateway
from storage.database import Database, create_memory_database


class WorkspaceHarness:
    """
    一个可复用的 Workspace 测试环境。

    两种模式：
        `WorkspaceHarness.in_memory()`  —— 单元测试（快）
        `WorkspaceHarness.on_file(p)`   —— 持久化 / 并发（真）
    """

    def __init__(
        self,
        db_path: Optional[str],
        participants: dict[str, set[str]] | None = None,
        agents: Iterable[str] | None = None,
    ) -> None:
        self.db_path = db_path
        self.participants: dict[str, set[str]] = {
            k: set(v) for k, v in (participants or {"W1": {"A", "B", "C"}}).items()
        }
        # 🔒 共享 —— 资源级互斥必须对所有线程可见
        self.locks = ResourceLockRegistry()
        # Agent **存在性**（身份层）与 Participant（任务层）是两件事。
        # 默认：所有出现过的名字都算存在。
        self.agents: set[str] = set(agents) if agents is not None else None  # type: ignore[assignment]

    # ----------------------------------------------------------
    # 构造
    # ----------------------------------------------------------
    @classmethod
    def in_memory(cls, **kw) -> "WorkspaceHarness":
        h = cls(None, **kw)
        h._keepalive = create_memory_database()      # 内存库要保持引用
        return h

    @classmethod
    def on_file(cls, path, **kw) -> "WorkspaceHarness":
        return cls(str(path), **kw)

    # ----------------------------------------------------------
    # 连接
    # ----------------------------------------------------------
    def new_db(self) -> Database:
        """**当前线程**专属的连接（并发测试的关键）。"""
        if self.db_path is None:
            # 内存模式：所有 gateway() 共用同一个 Database。
            # ⚠️ 这**不安全**用于多线程 —— 只给单线程测试用。
            self._keepalive.initialize()
            return self._keepalive
        db = Database(self.db_path)
        db.connect()
        db.initialize()
        return db

    def gateway(self, db: Optional[Database] = None) -> WorkspaceGateway:
        db = db or self.new_db()
        return WorkspaceGateway(
            db,
            lock_registry=self.locks,
            is_participant=lambda w, a: a in self.participants.get(w, set()),
            agent_exists=(
                (lambda a: a in self.agents) if self.agents is not None else None
            ),
        )

    @contextmanager
    def session(self):
        """
        在当前线程开一个 gateway，用完**关掉自己的连接**。

        ⚠️ 内存模式下不关 —— 否则后面的测试就没库了。
        """
        db = self.new_db()
        try:
            yield WorkspaceGateway(
                db,
                lock_registry=self.locks,
                is_participant=lambda w, a: a in self.participants.get(w, set()),
                agent_exists=(
                    (lambda a: a in self.agents) if self.agents is not None else None
                ),
            )
        finally:
            if self.db_path is not None:
                db.close()

    @contextmanager
    def connection(self):
        """裸连接（断言数据库状态用）。"""
        db = self.new_db()
        try:
            yield db
        finally:
            if self.db_path is not None:
                db.close()

    # ----------------------------------------------------------
    # 便捷
    # ----------------------------------------------------------
    def invite(self, workspace_id: str, *agents: str) -> None:
        self.participants.setdefault(workspace_id, set()).update(agents)

    def bootstrap(self, workspace_id: str = "W1", owner: str = "A") -> None:
        with self.session() as gw:
            gw.ensure_workspace(workspace_id, owner)

    def close(self) -> None:
        pass


# ============================================================
# 不变量检查器 —— 20 Agent 测试与 hardening 共用
# ============================================================
class InvariantViolation(AssertionError):
    """系统不变量被破坏。"""


def check_workspace_invariants(db, workspace_id: str) -> None:
    """
    检查任务书 §20 列出的**核心不变量**。

    这些必须在**任何**操作序列之后都成立：

        Identity      resource_id 唯一
        Revision      revision 单调递增（列 == 表里最大值，无空洞、无重复）
        Lock          同一 File 最多一个 Effective Lock Holder
        Path          不存在非法 / 重复 path
        Atomicity     不出现半成功状态
        Audit/Event   关键成功操作有可追踪历史
    """
    problems: list[str] = []

    # ---- Identity：resource_id 唯一（主键保证，这里防御性检查）----
    rows = db.fetchall(
        "SELECT resource_id, path, name, parent_id, revision, state, "
        "resource_type, workspace_id FROM workspace_resources "
        "WHERE workspace_id = ?", (workspace_id,))
    ids = [r["resource_id"] for r in rows]
    if len(ids) != len(set(ids)):
        problems.append(f"resource_id 重复：{ids}")

    # ---- Path：唯一 + 合法 ----
    paths = [r["path"] for r in rows]
    if len(paths) != len(set(paths)):
        dup = [p for p in set(paths) if paths.count(p) > 1]
        problems.append(f"path 重复：{dup}")
    for p in paths:
        if not p.startswith("/") or "//" in p or ".." in p.split("/"):
            problems.append(f"非法 path：{p!r}")

    # ---- 每个非根资源必须有存在的父（且父是目录）----
    by_id = {r["resource_id"]: r for r in rows}
    for r in rows:
        if r["parent_id"] is None:
            if r["path"] != "/":
                problems.append(f"非根 path 却没有 parent：{r['path']}")
            continue
        parent = by_id.get(r["parent_id"])
        if parent is None:
            problems.append(f"parent 不存在（孤儿）：{r['path']}")
        elif parent["resource_type"] != "directory":
            problems.append(f"parent 不是目录：{r['path']}")

    # ---- 🔒 Revision：列 == 表里最大值，无空洞、无重复 ----
    for r in rows:
        rev_rows = db.fetchall(
            "SELECT revision_number FROM workspace_revisions "
            "WHERE resource_id = ? ORDER BY revision_number",
            (r["resource_id"],))
        nums = [x["revision_number"] for x in rev_rows]

        if not nums:
            problems.append(f"{r['path']} 完全没有 revision 行")
            continue
        if len(nums) != len(set(nums)):
            problems.append(f"{r['path']} 有重复 revision_number：{nums}")
        if nums != list(range(1, len(nums) + 1)):
            problems.append(f"{r['path']} revision 不连续（有空洞）：{nums}")
        if max(nums) != r["revision"]:
            problems.append(
                f"{r['path']} revision 列={r['revision']} "
                f"但表里最大={max(nums)}"
            )

    # ---- 🔒 Lock：同一资源最多一条显式锁（主键保证）----
    locks = db.fetchall(
        "SELECT l.resource_id, l.kind, COUNT(*) AS n "
        "FROM workspace_explicit_locks l "
        "JOIN workspace_resources r ON r.resource_id = l.resource_id "
        "WHERE r.workspace_id = ? GROUP BY l.resource_id", (workspace_id,))
    for lk in locks:
        if lk["n"] != 1:
            problems.append(f"锁重复：{lk['resource_id']} × {lk['n']}")
        target = by_id.get(lk["resource_id"])
        if target is None:
            problems.append(f"锁指向不存在的资源：{lk['resource_id']}")
        elif target["resource_type"] == "directory" and lk["kind"] != "directory":
            problems.append(f"目录上有 File Lock：{target['path']}")
        elif target["resource_type"] == "file" and lk["kind"] != "file":
            problems.append(f"文件上有 Directory Lock：{target['path']}")

    # ---- Audit / Event：operation_id 有迹可循 ----
    orphan_audits = db.fetchall(
        "SELECT audit_id FROM workspace_audits "
        "WHERE workspace_id = ? AND (operation_id IS NULL OR operation_id = '')",
        (workspace_id,))
    if orphan_audits:
        problems.append(f"{len(orphan_audits)} 条 Audit 没有 operation_id")

    if problems:
        raise InvariantViolation(
            f"Workspace {workspace_id} 不变量被破坏：\n  - "
            + "\n  - ".join(problems)
        )
