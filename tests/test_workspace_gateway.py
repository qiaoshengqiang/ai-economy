"""
Workspace Gateway —— 集成测试（真实行为，不只是文档守卫）
============================================================

【本文件覆盖任务书 §45 的 47 项测试要求 + §53 的 8 个验收场景】

    真实行为意味着：真的建库、真的走 Gateway、真的读回数据，
    而不是断言「文档里写了这句话」。
"""

from __future__ import annotations

import pytest

from contextlib import contextmanager

from services.workspace_concurrency import ResourceLockRegistry
from services.workspace_gateway import WorkspaceGateway
from storage.database import Database, create_memory_database


# ============================================================
# Fixture
# ============================================================
class Rig:
    """一个测试用的 Workspace 环境。"""

    def __init__(self, participants: dict[str, set[str]] | None = None):
        self.db = create_memory_database()
        self.participants = participants or {"W1": {"A", "B", "C"}}
        self.gateway = WorkspaceGateway(
            self.db,
            is_participant=lambda w, a: a in self.participants.get(w, set()),
            # 🔒 必需依赖：这里的 "所有参与者都算存在"
            agent_exists=lambda a: any(
                a in members for members in self.participants.values()
            ),
        )

    def join(self, workspace_id: str, *agents: str) -> None:
        self.participants.setdefault(workspace_id, set()).update(agents)

    def close(self) -> None:
        self.db.close()

    # ---- 便捷 ----
    def bootstrap(self, workspace_id: str = "W1", owner: str = "A") -> None:
        self.gateway.ensure_workspace(workspace_id, owner)

    def audit_actions(self, workspace_id: str = "W1") -> list[str]:
        return [r["action"] for r in self.db.fetchall(
            "SELECT action FROM workspace_audits WHERE workspace_id = ? "
            "ORDER BY created_at, audit_id", (workspace_id,))]

    def event_types(self, workspace_id: str = "W1") -> list[str]:
        return [r["event_type"] for r in self.db.fetchall(
            "SELECT event_type FROM workspace_events WHERE workspace_id = ? "
            "ORDER BY created_at, event_id", (workspace_id,))]


class FileRig:
    """
    **文件数据库**版本的测试环境 —— 专门给**真实并发**测试用。

    【⚠️ 为什么不能用 `Rig`（内存库）做并发测试】

        技术债 #2 **已在基础设施层修复** ——
        `Database` 现在每线程一个 `_ThreadSession`
        （连接 + 深度 + pid），证据见
        `tests/test_database_thread_safety.py`（20 项）。

        但**内存库结构上只能单连接**：
        SQLite 的 `:memory:` 是「一个连接一个库」，
        而 shared-cache 内存在并发写下实测不成立
        （表级锁 `SQLITE_LOCKED` 不受 busy_timeout 影响）。

        所以这是**测试环境**约束，不是对 `Database` 的不信任。

    【正确做法】

        · 用**文件**数据库（`:memory:` 每条连接都是一个独立库，
          做不到「同一个库、多个连接」）
        · **每个线程开自己的 `Database` 实例**（各自一条连接）
        · 但**共享** `ResourceLockRegistry` ——
          资源级互斥是进程内概念，必须共享才能起作用

    这样测的才是**真实的并发语义**：
    多个连接打同一个库，靠资源级锁 + SQLite 事务协调。
    """

    def __init__(self, path, participants=None):
        self.path = str(path)
        self.locks = ResourceLockRegistry()          # 🔒 共享
        self.participants = participants or {"W1": {"A"}}

        bootstrap = self.new_db()
        try:
            self.gateway(bootstrap).ensure_workspace("W1", "A")
        finally:
            bootstrap.close()

    def new_db(self):
        """**当前线程**专属的连接。"""
        db = Database(self.path)
        db.connect()
        db.initialize()
        return db

    def gateway(self, db):
        return WorkspaceGateway(
            db,
            lock_registry=self.locks,
            is_participant=lambda w, a: a in self.participants.get(w, set()),
            agent_exists=lambda a: any(
                a in members for members in self.participants.values()
            ),
        )

    @contextmanager
    def session(self):
        """在**当前线程**开一个 gateway，用完关掉自己的连接。"""
        db = self.new_db()
        try:
            yield self.gateway(db)
        finally:
            db.close()


@pytest.fixture
def file_rig(tmp_path):
    """文件库 + 每线程独立连接的测试环境（并发测试专用）。"""
    r = FileRig(tmp_path / "workspace.db")
    yield r


@pytest.fixture
def rig():
    r = Rig()
    r.bootstrap()
    yield r
    r.close()


# ============================================================
# 场景 A —— 基础闭环
# ============================================================
class TestScenarioA:
    """A 创建 Workspace → 读 → 建文件 → 编辑 → 取 Revision → Audit + Event"""

    def test_full_loop(self, rig):
        # ---- 创建 Workspace ----
        assert rig.gateway.ensure_workspace("W1", "A").success

        # ---- 读 ----
        read_root = rig.gateway.read("W1", "/", "A")
        assert read_root.success
        assert read_root.result["revision"] == 1

        # ---- 建目录 + 文件 ----
        assert rig.gateway.create("W1", "/", "src", "directory", "A").success
        created = rig.gateway.create(
            "W1", "/src", "main.py", "file", "A", content="print(1)"
        )
        assert created.success
        assert created.result["revision"] == 1, "新资源从 R1 开始"

        # ---- 编辑 ----
        edited = rig.gateway.edit("W1", "/src/main.py", 1, "print(2)", "A")
        assert edited.success
        assert edited.result["revision"] == 2, "R1 → R2"

        # ---- 取 Revision ----
        assert rig.gateway.read("W1", "/src/main.py", "A").result["revision"] == 2

        # ---- Audit + Event ----
        assert "CREATE" in rig.audit_actions()
        assert "EDIT" in rig.audit_actions()
        assert "RESOURCE_CREATED" in rig.event_types()
        assert "RESOURCE_UPDATED" in rig.event_types()

    def test_list_reports_root_controller_and_lock(self, rig):
        """
        Agent 需要知道“根目录归谁、现在谁持锁”才能判断
        自己能不能创建 tests/。root 不在 entries 里，必须单独给。
        """
        listing = rig.gateway.list("W1", "/", "A")
        assert listing.success
        assert listing.result["effective_controller"] == "A"
        assert listing.result["effective_lock_holder"] == "A"

        rig.gateway.create("W1", "/", "src", "directory", "A")
        again = rig.gateway.list("W1", "/", "A")
        entry = next(e for e in again.result["entries"]
                     if e["name"] == "src")
        assert entry["effective_controller"] == "A"
        assert entry["effective_lock_holder"] == "A"

    def test_lock_snapshot_reports_effective_holders(self, rig):
        """Demand 的锁变更事实依赖这个只读快照。"""
        rig.bootstrap()
        rig.gateway.create("W1", "/", "src", "directory", "A")
        rig.gateway.create("W1", "/src", "main.py", "file", "A",
                           content="x")

        snapshot = rig.gateway.lock_snapshot("W1")
        assert snapshot["/"] == "A"
        assert snapshot["/src"] == "A"
        assert snapshot["/src/main.py"] == "A"

        rig.gateway.handoff(
            "W1", "/src", from_agent="A", to_agent="B", actor="A",
            reason="handoff for test")
        after = rig.gateway.lock_snapshot("W1")
        assert after["/src"] == "B"
        assert after["/src/main.py"] == "B"

    def test_read_does_not_produce_event_or_audit(self, rig):
        """🔒 list()/read() **不产生 Event、不记读 Audit**（任务书 §17）。"""
        before_e = len(rig.event_types())
        before_a = len(rig.audit_actions())

        rig.gateway.list("W1", "/", "A")
        rig.gateway.read("W1", "/", "A")

        assert len(rig.event_types()) == before_e, "读不该产生 Event"
        assert len(rig.audit_actions()) == before_a, "读不该写 Audit"


    def test_event_has_no_file_content(self, rig):
        """🔒 Event **不含文件内容**（任务书 §20）。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A",
                           content="SECRET_PAYLOAD_XYZ")
        rig.gateway.edit("W1", "/a.py", 1, "SECRET_PAYLOAD_ABC", "A")

        rows = rig.db.fetchall(
            "SELECT detail FROM workspace_events WHERE workspace_id = 'W1'"
        )
        blob = " ".join((r["detail"] or "") for r in rows)

        assert "SECRET_PAYLOAD_XYZ" not in blob
        assert "SECRET_PAYLOAD_ABC" not in blob


# ============================================================
# Read / Visibility（任务书 §45 的 1–4）
# ============================================================
class TestReadVisibility:
    def test_participant_can_read_whole_workspace(self, rig):
        rig.gateway.create("W1", "/", "src", "directory", "A")
        rig.gateway.create("W1", "/src", "a.py", "file", "A", content="x")

        for agent in ("A", "B", "C"):
            assert rig.gateway.read("W1", "/src/a.py", agent).success, (
                f"{agent} 是 Participant，应该能读"
            )

    def test_non_participant_cannot_read(self, rig):
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")

        r = rig.gateway.read("W1", "/a.py", "OUTSIDER")

        assert not r.success
        assert r.error_code == "NOT_TASK_PARTICIPANT"

    def test_non_participant_cannot_list(self, rig):
        r = rig.gateway.list("W1", "/", "OUTSIDER")

        assert not r.success
        assert r.error_code == "NOT_TASK_PARTICIPANT"

    def test_read_does_not_imply_write(self, rig):
        """
        🔒 **Read 不意味着 Write**（任务书 §45 第 4 项）。

        B 是 Participant（能读），但文件锁在 A 手里 → B 不能改。
        """
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")

        assert rig.gateway.read("W1", "/a.py", "B").success, "B 可以读"

        r = rig.gateway.edit("W1", "/a.py", 1, "hacked", "B")

        assert not r.success, "B 不该能改"
        assert r.error_code == "LOCKED_BY_OTHER_AGENT"
        assert rig.gateway.read("W1", "/a.py", "A").result["content"] == "x", (
            "内容必须没变"
        )


# ============================================================
# Resource 操作（§45 的 5–9）
# ============================================================
class TestResourceOps:
    def test_create_requires_parent_directory_lock(self, rig):
        rig.gateway.create("W1", "/", "src", "directory", "A")

        # B 不是 src/ 的持有者
        r = rig.gateway.create("W1", "/src", "x.py", "file", "B")

        assert not r.success
        assert r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"

    def test_delete_file(self, rig):
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")

        r = rig.gateway.delete("W1", "/a.py", "A")

        assert r.success
        # 🔒 软删除：资源仍在，只是 state = deleted（任务书 §21）
        row = rig.db.fetchone(
            "SELECT state FROM workspace_resources "
            "WHERE workspace_id='W1' AND path='/a.py'"
        )
        assert row["state"] == "deleted"
        assert "RESOURCE_DELETED" in rig.event_types()

    def test_rename_keeps_resource_id(self, rig):
        """🔒 rename 后 **resource_id 不变**（任务书 §29）。"""
        created = rig.gateway.create(
            "W1", "/", "login_old.py", "file", "A", content="x"
        )
        rid = created.result["resource_id"]
        rev_before = created.result["revision"]

        r = rig.gateway.rename("W1", "/login_old.py", "login.py", "A")

        assert r.success
        assert r.result["resource_id"] == rid, "resource_id 必须不变"
        assert r.result["new_path"] == "/login.py"
        assert r.result["revision"] == rev_before + 1
        assert "RESOURCE_RENAMED" in rig.event_types()

    def test_move_keeps_resource_id_and_locks(self, rig):
        """
        🔒 move 后 resource_id 不变；
           **Explicit Lock 保持**（不自动换责任人）。

        ⚠️ 前置条件：被移动的资源**不能**是别人独立负责的 ——
        否则 move 会被边界规则拦下（那是正确的，见 hardening 套件）。
        这里让 A 把 login.tsx 的责任**显式化给自己**，
        这样它是一条真正的 Explicit Lock，而移动它的人就是它的持有者。
        """
        rig.gateway.create("W1", "/", "frontend", "directory", "A")
        rig.gateway.create("W1", "/frontend", "login.tsx", "file", "A",
                           content="x")
        # A → A：把继承来的责任**显式化**成一条持久化锁
        rig.gateway.handoff("W1", "/frontend/login.tsx", "A", "A", "A",
                            reason="显式化责任")
        rig.gateway.create("W1", "/", "auth", "directory", "A")

        before = rig.gateway.read("W1", "/frontend/login.tsx", "A")
        rid = before.result["resource_id"]
        assert rig.gateway.store.explicit_lock_of(rid) is not None, "要先有显式锁"

        r = rig.gateway.move("W1", "/frontend/login.tsx", "/auth", "A")

        assert r.success, r.error_code
        assert r.result["resource_id"] == rid
        assert r.result["new_path"] == "/auth/login.tsx"

        # 🔒 显式锁**仍然挂在同一个 resource_id 上**，没被 move 改写
        tree = rig.gateway.store.load_tree("W1")
        moved = tree.by_path("W1", "/auth/login.tsx")
        assert tree.explicit_lock_of(rid) is not None, "move 把显式锁弄丢了"
        assert tree.effective_lock(moved) == "A", "move 不该更换责任人"

    def test_move_rejects_into_own_subtree(self, rig):
        rig.gateway.create("W1", "/", "a", "directory", "A")
        rig.gateway.create("W1", "/a", "b", "directory", "A")

        r = rig.gateway.move("W1", "/a", "/a/b", "A")

        assert not r.success
        assert r.error_code == "INVALID_PARENT"

    def test_path_traversal_rejected(self, rig):
        r = rig.gateway.read("W1", "/../../etc/passwd", "A")

        assert not r.success
        assert r.error_code == "OUTSIDE_WORKSPACE"

    def test_no_physical_path_anywhere(self, rig):
        """🔒 接口里**不出现**宿主机物理路径（任务书 §15）。"""
        created = rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        blob = str(created.to_dict()) + str(rig.gateway.list("W1", "/", "A").to_dict())

        for leak in ("/Users/", "/tmp/", "/var/", "/home/"):
            assert leak not in blob, f"返回值里泄漏了物理路径前缀 {leak}"


# ============================================================
# Lock 语义（§45 的 10–18）
# ============================================================
class TestLockSemantics:
    def test_new_resource_has_no_explicit_lock(self, rig):
        """§45-16：新资源**不自动**生成 Explicit Lock。"""
        created = rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        rid = created.result["resource_id"]

        assert rig.gateway.store.explicit_lock_of(rid) is None

        tree = rig.gateway.store.load_tree("W1")
        assert tree.effective_lock(tree.get(rid)) == "A", "靠继承父目录"

    def test_directory_lock_does_not_exclude_file_locks(self, rig):
        """
        §45-10/11：Directory Lock 与 File Lock 可以并存，
        不同 File 可以有不同 Holder。
        """
        rig.gateway.create("W1", "/", "data", "directory", "A")
        rig.gateway.create("W1", "/data", "a.csv", "file", "A", content="1")
        rig.gateway.create("W1", "/data", "b.csv", "file", "A", content="2")
        # A 把 b.csv 交给 B
        rig.gateway.handoff("W1", "/data/b.csv", "A", "B", "A", reason="分工")

        tree = rig.gateway.store.load_tree("W1")
        a = tree.by_path("W1", "/data/a.csv")
        b = tree.by_path("W1", "/data/b.csv")

        assert tree.effective_lock(a) == "A"
        assert tree.effective_lock(b) == "B"
        assert tree.effective_lock(tree.by_path("W1", "/data")) == "A", (
            "目录锁仍然在 A，且不排斥不同 File 的不同 Holder"
        )

    def test_edit_end_does_not_release(self, rig):
        """§45-17：**Edit End ≠ Lock Release**。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        for rev in (1, 2, 3):
            assert rig.gateway.edit("W1", "/a.py", rev, f"v{rev}", "A").success

        tree = rig.gateway.store.load_tree("W1")
        assert tree.effective_lock(tree.by_path("W1", "/a.py")) == "A", (
            "编辑多次之后锁仍然是 A"
        )

    def test_explicit_child_overrides_parent(self, rig):
        """§45-13/15：Explicit Child Lock 覆盖 Parent。"""
        rig.gateway.create("W1", "/", "data", "directory", "A")
        rig.gateway.create("W1", "/data", "processed", "directory", "A")
        rig.gateway.create("W1", "/data/processed", "r.csv", "file", "A",
                           content="x")

        rig.gateway.handoff("W1", "/data/processed", "A", "B", "A", reason="B 负责")
        # ⚠️ 此时 r.csv 已经**继承 B** —— 所以 from 必须是 B，不是 A
        rig.gateway.handoff("W1", "/data/processed/r.csv", "B", "C", "B",
                            reason="C 负责这个文件")

        tree = rig.gateway.store.load_tree("W1")

        assert tree.effective_lock(tree.by_path("W1", "/data")) == "A"
        assert tree.effective_lock(tree.by_path("W1", "/data/processed")) == "B"
        assert tree.effective_lock(
            tree.by_path("W1", "/data/processed/r.csv")
        ) == "C", "🔒 Explicit 必须压过父目录"

    def test_delete_cannot_bypass_foreign_boundary(self, rig):
        """§45-31：delete 不能越过别人的 Resource Boundary。"""
        rig.gateway.create("W1", "/", "data", "directory", "A")
        rig.gateway.create("W1", "/data", "processed", "directory", "A")
        rig.gateway.handoff("W1", "/data/processed", "A", "B", "A", reason="交给 B")

        r = rig.gateway.delete("W1", "/data", "A")

        assert not r.success
        # A 不负责 processed/ 本身 → 先在自身责任检查处就被拦下
        assert r.error_code in (
            "NOT_EFFECTIVE_LOCK_HOLDER", "RESOURCE_BOUNDARY_VIOLATION"
        )
        assert rig.gateway.read("W1", "/data/processed", "B").success, (
            "B 的资源必须还在"
        )

    def test_owner_cannot_delete_others_file(self, rig):
        """
        §45-32：**A 控制目录、B 控制文件时，A 不能直接删 B 的文件**。
        """
        rig.gateway.create("W1", "/", "data", "directory", "A")
        rig.gateway.create("W1", "/data", "a.csv", "file", "A", content="x")
        rig.gateway.handoff("W1", "/data/a.csv", "A", "B", "A", reason="B 负责")

        r = rig.gateway.delete("W1", "/data/a.csv", "A")

        assert not r.success
        assert r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"


# ============================================================
# Handoff / Release（§45 的 19–24）
# ============================================================
class TestHandoffRelease:
    def test_directory_handoff_sets_controller_and_lock(self, rig):
        rig.gateway.create("W1", "/", "frontend", "directory", "A")

        r = rig.gateway.handoff("W1", "/frontend", "A", "B", "A", reason="B 接手")

        assert r.success
        tree = rig.gateway.store.load_tree("W1")
        node = tree.by_path("W1", "/frontend")

        assert tree.effective_lock(node) == "B"
        assert tree.effective_controller(node) == "B"

    def test_handoff_audit_and_event(self, rig):
        """§45-21/22：Handoff 必须产生 Audit + Event。"""
        rig.gateway.create("W1", "/", "frontend", "directory", "A")
        rig.gateway.handoff("W1", "/frontend", "A", "B", "A", reason="交接原因")

        assert "HANDOFF" in rig.audit_actions()
        assert "RESPONSIBILITY_HANDOFF" in rig.event_types()

        row = rig.db.fetchone(
            "SELECT detail FROM workspace_audits WHERE action='HANDOFF'"
        )
        assert "交接原因" in row["detail"], "reason 必须进 Audit"

    def test_parent_handoff_does_not_swallow_explicit_child(self, rig):
        """
        §45-23 / 场景 F：**父级 Handoff 不吞回 Explicit Child Lock**。
        """
        rig.gateway.create("W1", "/", "configs", "directory", "A")
        rig.gateway.create("W1", "/configs", "app.yaml", "file", "A", content="a")
        rig.gateway.create("W1", "/configs", "production.yaml", "file", "A",
                           content="p")
        rig.gateway.handoff("W1", "/configs/production.yaml", "A", "C", "A",
                            reason="C 单独负责")

        # A 把整个 configs/ 交给 B
        rig.gateway.handoff("W1", "/configs", "A", "B", "A", reason="整体交接")

        tree = rig.gateway.store.load_tree("W1")

        assert tree.effective_lock(tree.by_path("W1", "/configs/app.yaml")) == "B", (
            "隐式后代要跟随"
        )
        assert tree.effective_lock(
            tree.by_path("W1", "/configs/production.yaml")
        ) == "C", "🔒 显式责任不能被吞回"

    def test_release_recomputes_effective_lock(self, rig):
        """§45-24：Release 后**重新按父级规则解析**。"""
        rig.gateway.create("W1", "/", "data", "directory", "A")
        rig.gateway.create("W1", "/data", "processed", "directory", "A")
        rig.gateway.create("W1", "/data/processed", "result.csv", "file", "A",
                           content="x")
        rig.gateway.handoff("W1", "/data/processed", "A", "B", "A", reason="B")
        # ⚠️ r.csv 已继承 B，所以 from = B
        rig.gateway.handoff("W1", "/data/processed/result.csv", "B", "C", "B",
                            reason="C")

        before = rig.gateway.store.load_tree("W1")
        assert before.effective_lock(
            before.by_path("W1", "/data/processed/result.csv")
        ) == "C"

        r = rig.gateway.release("W1", "/data/processed/result.csv", "C")

        assert r.success
        assert r.result["effective_lock_after"] == "B", "应该回落到父目录的 B"

    def test_receiver_cannot_snatch(self, rig):
        """🔒 接收方**不能直接抢**（没有 lock() API，handoff 必须 from 正确）。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")

        r = rig.gateway.handoff("W1", "/a.py", "B", "C", "B", reason="抢")

        assert not r.success
        assert r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"

    def test_root_release_rejected(self, rig):
        """§25：根的 release 语义未定 → 明确拒绝，**不随意分配**。"""
        r = rig.gateway.release("W1", "/", "A")

        assert not r.success
        assert r.error_code == "RESOURCE_BOUNDARY_VIOLATION"

    def test_force_unlock_is_governance(self, rig):
        """§26 / ADR-0021：force_unlock 需要高权限 + 原因 + 审计。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        # 先做一次 handoff，让 a.py 拥有**显式**锁（见下面那条测试的说明）
        rig.gateway.handoff("W1", "/a.py", "A", "A", "A", reason="建立显式责任")

        # 没有授权
        r1 = rig.gateway.force_unlock("W1", "/a.py", "A", reason="x")
        assert r1.error_code == "GOVERNANCE_REQUIRED"

        # 有授权但没原因
        r2 = rig.gateway.force_unlock("W1", "/a.py", "A", reason="",
                                      authorized=True)
        assert r2.error_code == "INVALID_ARGUMENT"

        # 正常
        r3 = rig.gateway.force_unlock("W1", "/a.py", "HUMAN", reason="Agent 失联",
                                      authorized=True)
        assert r3.success
        assert r3.result["previous_owner"] == "A"
        assert "FORCE_UNLOCK" in rig.audit_actions()

    def test_force_unlock_requires_explicit_responsibility(self, rig):
        """
        ⚠️ **记录一个从实现中浮现的语义边界**（任务书 §52 要求记录 Open Question）。

            `force_unlock` 作用于**显式**责任。

            一个**只靠继承**获得责任的文件（例如 A 持有 `data/`，
            于是 `data/a.csv` 的 Effective Lock = A）**没有自己的显式锁**，
            所以「解锁 a.csv」没有可撤销的对象 ——
            真正该解锁的是 `data/` 本身。

            🔒 本轮**不自行发明**「解锁继承责任」的语义
               （那需要新概念，例如负数显式锁 —— 属于扩大模型）。
               这里明确返回一个可解释的错误，并把它记为 Open Question。
        """
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")

        r = rig.gateway.force_unlock("W1", "/a.py", "HUMAN",
                                     reason="Agent 失联", authorized=True)

        assert not r.success
        assert r.error_code == "RESOURCE_NOT_FOUND", (
            "该资源没有显式责任可撤销 —— 返回明确错误，而不是悄悄改父目录的锁"
        )

        # 要让一个普通资源拥有显式锁，必须经 handoff 产生
        # （⚠️ 必须在强解锁**根**之前做 —— 解锁根之后整棵树就没有责任源头了）
        rig.gateway.create("W1", "/", "data", "directory", "A")
        rig.gateway.handoff("W1", "/data", "A", "A", "A", reason="建立显式责任")

        ok = rig.gateway.force_unlock("W1", "/data", "HUMAN",
                                      reason="Agent 失联", authorized=True)
        assert ok.success, "有显式锁的资源才可以被强解锁"
        assert ok.result["previous_owner"] == "A"

        # 根目录是唯一**天生**持有显式锁的资源（Workspace 的责任源头）
        assert rig.gateway.force_unlock(
            "W1", "/", "HUMAN", reason="Agent 失联", authorized=True
        ).success, "根目录的显式锁也可以被强解锁"


# ============================================================
# Revision（§45 的 25–30 · 场景 E）
# ============================================================
class TestRevision:
    def test_revision_increments(self, rig):
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="0")

        for expected in (2, 3, 4):
            r = rig.gateway.edit("W1", "/a.py", expected - 1, f"v{expected}", "A")
            assert r.success
            assert r.result["revision"] == expected

    def test_stale_revision_rejected_and_not_applied(self, rig):
        """§45-28/29 + 场景 E：stale 被拒绝**且不修改资源**。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="R1")

        # A 先更新到 R2
        rig.gateway.edit("W1", "/a.py", 1, "R2", "A")

        # B（假设也持有锁）基于 R1 提交 —— 先让 B 拿到锁以隔离 revision 变量
        rig.gateway.handoff("W1", "/a.py", "A", "B", "A", reason="换人")

        current = rig.gateway.read("W1", "/a.py", "B").result["revision"]
        # 🔒 **责任变化不产生新 Revision**（任务书 §27 的 Responsibility 流程
        #    没有 Create Revision 步骤）—— 所以 handoff 之后仍是 R2。
        assert current == 2, (
            "handoff 是责任变化，不该让内容 revision 递增"
        )

        r = rig.gateway.edit("W1", "/a.py", 1, "STALE_WRITE", "B")

        assert not r.success
        assert r.error_code == "STALE_REVISION"
        assert r.error["current_revision"] == current
        assert r.error["provided_revision"] == 1
        assert rig.gateway.read("W1", "/a.py", "B").result["content"] == "R2", (
            "🔒 失败的操作**不能**修改资源"
        )

    def test_stale_attempt_is_audited(self, rig):
        """§45-30：失败的尝试**可以审计**。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        rig.gateway.edit("W1", "/a.py", 1, "y", "A")
        rig.gateway.edit("W1", "/a.py", 1, "z", "A")   # stale

        actions = rig.audit_actions()

        assert "EDIT_ATTEMPT" in actions, "失败尝试要有 Audit"

        row = rig.db.fetchone(
            "SELECT result, error_code FROM workspace_audits "
            "WHERE action='EDIT_ATTEMPT'"
        )
        assert row["result"] == "FAILED"
        assert row["error_code"] == "STALE_REVISION"

    def test_failed_mutation_produces_no_success_event(self, rig):
        """§45-36：**失败的 mutation 不产生错误的成功 Event**。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        before = len(rig.event_types())

        rig.gateway.edit("W1", "/a.py", 999, "y", "A")     # stale

        assert len(rig.event_types()) == before, "失败不该产生 Event"

    def test_revisions_are_recorded(self, rig):
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        rid = rig.gateway.read("W1", "/a.py", "A").result["resource_id"]
        rig.gateway.edit("W1", "/a.py", 1, "y", "A")
        rig.gateway.edit("W1", "/a.py", 2, "z", "A")

        rows = rig.gateway.store.revisions_for(rid)

        assert [r["revision_number"] for r in rows] == [1, 2, 3]
        assert [r["change_type"] for r in rows] == ["CREATE", "EDIT", "EDIT"]


# ============================================================
# Audit / Event（§45 的 35–41）
# ============================================================
class TestAuditEvent:
    def test_success_mutation_has_audit(self, rig):
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")

        assert "CREATE" in rig.audit_actions()

    def test_all_event_types(self, rig):
        """§45-37..41：五类资源事件 + 责任事件。"""
        rig.gateway.create("W1", "/", "d", "directory", "A")
        rig.gateway.create("W1", "/d", "a.py", "file", "A", content="x")
        rig.gateway.edit("W1", "/d/a.py", 1, "y", "A")
        rig.gateway.rename("W1", "/d/a.py", "b.py", "A")
        rig.gateway.create("W1", "/d", "sub", "directory", "A")
        rig.gateway.move("W1", "/d/sub", "/", "A")
        rig.gateway.handoff("W1", "/d", "A", "B", "A", reason="x")
        rig.gateway.create("W1", "/d", "c.py", "file", "B", content="z")
        rig.gateway.delete("W1", "/d/c.py", "B")

        types = set(rig.event_types())

        for expected in ("RESOURCE_CREATED", "RESOURCE_UPDATED",
                         "RESOURCE_RENAMED", "RESOURCE_MOVED",
                         "RESOURCE_DELETED", "RESPONSIBILITY_HANDOFF"):
            assert expected in types, f"缺少事件 {expected}"

    def test_audit_is_distinct_from_event(self, rig):
        """
        🔒 Audit（含失败）与 Event（只成功）**是两个不同的东西**。
        """
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="x")
        rig.gateway.edit("W1", "/a.py", 1, "y", "A")
        rig.gateway.edit("W1", "/a.py", 1, "z", "A")   # 失败

        audits = rig.audit_actions()
        events = rig.event_types()

        assert "EDIT_ATTEMPT" in audits
        assert not any("FAILED" in e or "ATTEMPT" in e for e in events), (
            "Event 里不该有失败事件"
        )


# ============================================================
# 并发（§45 的 42–47）—— **真实线程 + barrier**
# ============================================================
class TestConcurrency:
    def test_different_workspaces_are_isolated(self):
        """
        §45-47 / 场景 H：**一个 Workspace 的竞争不会破坏另一个**。
        """
        r = Rig(participants={"W1": {"A"}, "W2": {"B"}})
        try:
            r.gateway.ensure_workspace("W1", "A")
            r.gateway.ensure_workspace("W2", "B")
            r.gateway.create("W1", "/", "a.py", "file", "A", content="w1")
            r.gateway.create("W2", "/", "b.py", "file", "B", content="w2")

            # W1 上制造一次 stale 冲突
            r.gateway.edit("W1", "/a.py", 1, "w1-v2", "A")
            bad = r.gateway.edit("W1", "/a.py", 1, "w1-stale", "A")
            assert bad.error_code == "STALE_REVISION"

            # W2 完全不受影响
            ok = r.gateway.edit("W2", "/b.py", 1, "w2-v2", "B")
            assert ok.success
            assert r.gateway.read("W2", "/b.py", "B").result["content"] == "w2-v2"
        finally:
            r.close()

    def test_different_files_in_same_workspace_are_parallel(self, rig):
        """§45-43 / 场景 C：同 Workspace 不同 File **可以并行**。"""
        rig.gateway.create("W1", "/", "a.py", "file", "A", content="a")
        rig.gateway.create("W1", "/", "b.py", "file", "A", content="b")
        rig.gateway.handoff("W1", "/b.py", "A", "B", "A", reason="分工")

        assert rig.gateway.edit("W1", "/a.py", 1, "a2", "A").success
        assert rig.gateway.edit("W1", "/b.py", 1, "b2", "B").success

    def test_two_threads_race_same_file(self, file_rig):
        """
        🔒 **§45-44 / 场景 D：真实测试两个并发操作竞争同一 File。**

        【测试是真的并发吗】
            是的 —— 两个线程各自跑完整业务逻辑，
            在 `barrier.wait()` 处**同步起跑**，
            然后同时尝试 `edit` 同一个文件。
            **不是**串行调用两次假装并发。

        【为什么 loser 一定是 STALE_REVISION 或 LOCKED_BY_OTHER_AGENT】
            两个线程都用 `base_revision = 已读到的 R`：
                · 谁先提交 → 成功，资源变成 R+1
                · 谁后提交 → base_revision 已过期 → STALE_REVISION
            如果再加一个「锁换人」的维度，也可能得到
            LOCKED_BY_OTHER_AGENT。两者都是**明确且可解释**的失败。
        """
        import threading

        with file_rig.session() as gw:
            gw.create("W1", "/", "a.py", "file", "A", content="v0")

        barrier = threading.Barrier(2)
        results: list = [None, None]
        errors: list = [None, None]

        def attempt(slot: int, payload: str) -> None:
            try:
                # 🔒 **每个线程自己的连接**（技术债 #2：不能共享 Database）
                with file_rig.session() as gw:
                    barrier.wait(timeout=5)          # 🔒 同步起跑
                    results[slot] = gw.edit(
                        "W1", "/a.py", 1, payload, "A"
                    )
            except Exception as exc:                 # pragma: no cover
                errors[slot] = exc

        threads = [
            threading.Thread(target=attempt, args=(0, "winner-A")),
            threading.Thread(target=attempt, args=(1, "winner-B")),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15)

        assert not any(errors), f"线程里抛异常：{errors}"
        assert all(res is not None for res in results), "两个线程都要有结果"

        successes = [res for res in results if res.success]
        failures = [res for res in results if not res.success]

        assert len(successes) == 1, (
            f"🔒 同一 File 必须**恰好一个**成功，实际 {len(successes)}"
        )
        assert len(failures) == 1
        assert failures[0].error_code in (
            "STALE_REVISION", "LOCKED_BY_OTHER_AGENT"
        ), f"失败必须明确，得到 {failures[0].error_code}"

        with file_rig.session() as gw:
            final = gw.read("W1", "/a.py", "A")
            assert final.result["revision"] == 2
            assert final.result["content"] in ("winner-A", "winner-B")

    def test_race_produces_exactly_two_revisions(self, file_rig):
        """
        并发竞争之后，**恰好**留下 2 条 EDIT 之后的 revision ——
        败者**不能**留下任何写入痕迹。
        """
        import threading

        with file_rig.session() as gw:
            gw.create("W1", "/", "a.py", "file", "A", content="v0")
            rid = gw.read("W1", "/a.py", "A").result["resource_id"]

        barrier = threading.Barrier(2)
        errors: list = []

        def attempt(payload: str) -> None:
            try:
                with file_rig.session() as gw:
                    barrier.wait(timeout=5)
                    gw.edit("W1", "/a.py", 1, payload, "A")
            except Exception as exc:                 # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(f"p{i}",))
                   for i in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15)

        assert not errors, f"线程里抛异常：{errors}"

        with file_rig.session() as gw:
            rows = gw.store.revisions_for(rid)

        assert [x["revision_number"] for x in rows] == [1, 2], (
            f"应该是 CREATE(R1) + 恰好一次 EDIT(R2)，"
            f"实际 {[x['revision_number'] for x in rows]}"
        )

    def test_concurrent_creates_in_same_directory_are_consistent(self, file_rig):
        """
        §45-45：同一 Directory 的**冲突结构操作安全** ——
        两个并发 create 之后，Namespace 必须一致（两个都在，或明确失败一个）。
        """
        import threading

        barrier = threading.Barrier(4)
        results: list = [None] * 4
        errors: list = []

        def create(slot: int) -> None:
            try:
                with file_rig.session() as gw:
                    barrier.wait(timeout=5)
                    results[slot] = gw.create(
                        "W1", "/", f"f{slot}.py", "file", "A", content="x"
                    )
            except Exception as exc:                 # pragma: no cover
                errors.append((slot, exc))

        threads = [threading.Thread(target=create, args=(i,)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15)

        assert not errors, f"线程里抛异常：{errors}"
        assert all(res is not None and res.success for res in results), (
            f"不同名字的并发 create 应该全部成功："
            f"{[x.error_code if x else None for x in results]}"
        )

        with file_rig.session() as gw:
            listing = gw.list("W1", "/", "A")

        names = sorted(e["name"] for e in listing.result["entries"])
        assert names == ["f0.py", "f1.py", "f2.py", "f3.py"], (
            f"Namespace 必须一致，实际 {names}"
        )


# ============================================================
# 场景 B —— Handoff 之后的分工
# ============================================================
class TestScenarioB:
    def test_handoff_then_split_work(self, rig):
        rig.gateway.create("W1", "/", "frontend", "directory", "A")
        rig.gateway.create("W1", "/", "backend", "directory", "A")
        rig.gateway.create("W1", "/frontend", "page.tsx", "file", "A",
                           content="v0")

        # A 把 frontend/ 交给 B
        rig.gateway.handoff("W1", "/frontend", "A", "B", "A", reason="B 负责前端")

        # B 能读整个 Workspace
        assert rig.gateway.read("W1", "/backend", "B").success
        assert rig.gateway.read("W1", "/frontend/page.tsx", "B").success

        # B 能在 frontend/ 里工作
        assert rig.gateway.create("W1", "/frontend", "nav.tsx", "file", "B",
                                  content="n").success

        # B **不能**动 A 仍然负责的 backend/
        bad = rig.gateway.create("W1", "/backend", "evil.py", "file", "B")
        assert not bad.success
        assert bad.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"


# ============================================================
# 场景 G —— rename / move 的 id 稳定性
# ============================================================
class TestScenarioG:
    def test_id_stable_across_rename_and_move(self, rig):
        rig.gateway.create("W1", "/", "src", "directory", "A")
        rig.gateway.create("W1", "/", "lib", "directory", "A")
        created = rig.gateway.create("W1", "/src", "old.py", "file", "A",
                                     content="x")
        rid = created.result["resource_id"]

        rig.gateway.rename("W1", "/src/old.py", "new.py", "A")
        assert rig.gateway.read("W1", "/src/new.py", "A").result["resource_id"] == rid

        rig.gateway.move("W1", "/src/new.py", "/lib", "A")
        assert rig.gateway.read("W1", "/lib/new.py", "A").result["resource_id"] == rid

        # 历史 Revision 也仍然挂在同一个 id 上
        numbers = [r["revision_number"]
                   for r in rig.gateway.store.revisions_for(rid)]
        assert numbers == [1, 2, 3], f"三次操作的 revision 都要在，实际 {numbers}"
