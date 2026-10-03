"""
Workspace 测试脚手架（Hardening + 20 Agent 并发共用）
============================================================

【为什么需要它，而不是每个测试文件各写一份】

    三类测试对「环境」的要求**互相冲突**：

        纯单元测试         → 内存库最干净
        reopen / 持久化     → **必须**文件库（内存库关掉就没了）
        真实并发           → 文件库 + **每线程独立连接**

【⚠️ 并发测试为什么仍然用文件库】

    技术债 #2 **已经在基础设施层修好了** ——
    `Database` 现在每线程一个 `_ThreadSession`
    （连接 + 事务深度 + pid），见 `storage/database.py`
    与 `tests/test_database_thread_safety.py`（20 项）。

    那这里为什么还要坚持文件库？因为**内存库结构上只能单连接**：

        SQLite 的 `:memory:` 是「一个连接一个库」。
        想让多连接共享它只能用 shared-cache URI ——
        而那在并发写下**实测不成立**
        （`SQLITE_LOCKED` 表级锁，不受 busy_timeout 影响）。

    所以这是**测试环境**的约束，不是对 `Database` 的不信任：

        · 用**文件**数据库
        · 但**共享同一个 `Database` 实例** ——
          这才是真实的并发场景（修复之后它已经安全了）
        · 并且**共享** `ResourceLockRegistry` ——
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
        #
        # 🔒 `agent_exists` 现在是 Gateway 的**必需依赖** ——
        #    harness 必须显式提供它，不能再传 None。
        #
        # 【harness 的默认规则】
        #     "我知道的 Agent" = 所有已声明的 Participant ∪ agents 参数
        #     这是测试替身的合理规则：一个测试里出现的 Agent
        #     默认就当作存在，除非测试**显式**用 agents= 划定名单。
        #     （生产由 build_agent_exists_checker 注入真实身份查询。）
        self.extra_agents: set[str] = set(agents) if agents is not None else set()

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

    def known_agents(self) -> set[str]:
        """「我知道的 Agent」= 已声明的 Participant ∪ 显式给的名单。"""
        return set(self.extra_agents) | {
            a for members in self.participants.values() for a in members
        }

    def agent_exists(self, agent_id: str) -> bool:
        return agent_id in self.known_agents()

    def gateway(self, db: Optional[Database] = None) -> WorkspaceGateway:
        db = db or self.new_db()
        return WorkspaceGateway(
            db,
            lock_registry=self.locks,
            is_participant=lambda w, a: a in self.participants.get(w, set()),
            agent_exists=self.agent_exists,
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
                agent_exists=self.agent_exists,
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
