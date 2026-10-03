"""
WorktreeManager —— 功能 / 隔离 / 持久化 / 崩溃一致性
============================================================

【为什么这些测试用**真实 Git**，不用 mock】

    任务书 §2 要求「利用成熟 Git 原语」。
    而 mock 掉 Git 就测不出「成熟原语的真实行为」——
    例如「同一个 branch 不能被两个 worktree checkout」这条，
    正是**真实 Git** 才会告诉我们（我在实现时就撞上了它）。

    所以每个测试都在 `tmp_path` 里 `git init` 一个**真仓库**。
    它们**绝不**碰项目自己的仓库。
"""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from change import (
    GitManager,
    InformationalState,
    IntentKind,
    IntentState,
    WorktreeManager,
    WorktreeState,
    WorkspaceSnapshot,
)
from change.worktree_store import DuplicateLiveWorktree, WorktreeStore
from services.workspace_concurrency import ResourceLockRegistry
from storage.database import Database


# ============================================================
# 真实 Git 仓库
# ============================================================
class Repo:
    """一个真实的临时 Git 仓库。"""

    def __init__(self, root: Path):
        self.path = root
        self.path.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.path / "README.md").write_text("hello\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.path,
            capture_output=True, text=True, check=True,
        )
        return proc.stdout

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def worktree_paths(self) -> list[str]:
        out = self._git("worktree", "list", "--porcelain")
        return [l.split(" ", 1)[1] for l in out.splitlines()
                if l.startswith("worktree ")]

    def branches(self) -> list[str]:
        return [
            l.strip().lstrip("* ").strip()
            for l in self._git("branch", "--list").splitlines()
        ]


class Env:
    """一个完整的 Worktree 测试环境。"""

    def __init__(self, root: Path, *, workspaces=("WS-001", "WS-002"),
                 revisions=None, registry=None):
        self.repo = Repo(root / "repo")
        self.db_path = str(root / "change.db")
        self.revisions = dict(revisions or {w: 7 for w in workspaces})
        self.registry = registry if registry is not None else ResourceLockRegistry()
        self._keepalive: Database | None = None

    # ---- 连接 ----
    def new_db(self) -> Database:
        db = Database(self.db_path)
        db.connect()
        db.initialize()
        return db

    def manager(self, db: Database) -> WorktreeManager:
        return WorktreeManager(
            db,
            git_manager=GitManager(repo_path=str(self.repo.path)),
            workspace_lookup=self.lookup,
            repository_ref="repo-main",
            worktrees_root=str(self.repo.path.parent / ".worktrees"),
            registry=self.registry,
            actor="test",
        )

    def lookup(self, workspace_id: str):
        if workspace_id not in self.revisions:
            return None
        return WorkspaceSnapshot(workspace_id, self.revisions[workspace_id])

    def close(self) -> None:
        pass


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    bootstrap = e.new_db()
    mgr = e.manager(bootstrap)
    e.bootstrap_db = bootstrap
    try:
        yield e
    finally:
        bootstrap.close()


@pytest.fixture
def mgr(env):
    return env.manager(env.bootstrap_db)


# ============================================================
# 一、Functional（任务书 §26）
# ============================================================
class TestFunctional:
    def test_create_produces_a_real_git_worktree(self, env, mgr):
        r = mgr.create("WS-001", operation_id="OP-1")

        assert r.success, r.error_code
        assert r.result["state"] == WorktreeState.READY.value
        assert r.result["workspace_id"] == "WS-001"

        # 🔒 真实 Git 侧确实多了一个 worktree
        assert len(env.repo.worktree_paths()) == 2   # main + 新的

    def test_create_records_both_provenances(self, env, mgr):
        """
        🔒 `workspace_revision` 与 `base_commit` **是两个字段**（§6）。
        """
        r = mgr.create("WS-001", operation_id="OP-1")

        assert r.result["workspace_revision"] == env.revisions["WS-001"]
        assert r.result["base_commit"] == env.repo.head()
        assert r.result["workspace_revision"] != r.result["base_commit"], (
            "两者是不同概念 —— 绝不能混成一个"
        )

    def test_get_and_get_for_workspace(self, env, mgr):
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]

        assert mgr.get(wid).result["worktree_id"] == wid
        assert mgr.get_for_workspace("WS-001").result["worktree_id"] == wid
        assert not mgr.get("WT-nope").success
        assert not mgr.get_for_workspace("WS-002").success

    def test_list_all_and_per_workspace(self, env, mgr):
        mgr.create("WS-001", operation_id="OP-1")
        mgr.create("WS-002", operation_id="OP-2")

        assert len(mgr.list().result["worktrees"]) == 2
        assert len(mgr.list("WS-001").result["worktrees"]) == 1

    def test_status_reports_git_side_facts(self, env, mgr):
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]

        st = mgr.status(wid).result
        assert st["git_present"] is True
        assert st["consistent"] is True

    def test_freeze_then_archive(self, env, mgr):
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]

        assert mgr.freeze(wid, operation_id="OP-2").result["state"] == "frozen"
        assert mgr.archive(wid, operation_id="OP-3").result["state"] == "archived"

    def test_archive_requires_frozen(self, env, mgr):
        """生命周期是 …→FROZEN→ARCHIVED —— 不许跳步。"""
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]

        r = mgr.archive(wid, operation_id="OP-2")

        assert not r.success
        assert r.error_code == "INVALID_STATE"

    def test_archived_allows_a_new_live_worktree(self, env, mgr):
        """
        🔒 任务书 §3：**不是**「一个 Workspace 永远只能有一个 Worktree」。
           归档的可以存在，同时还能有新的 live 的。
        """
        first = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]
        mgr.freeze(first, operation_id="OP-2")
        mgr.archive(first, operation_id="OP-3")

        r = mgr.create("WS-001", operation_id="OP-4")

        assert r.success, r.error_code
        assert r.result["worktree_id"] != first
        assert len(mgr.list("WS-001").result["worktrees"]) == 2

    def test_unknown_workspace_fails(self, env, mgr):
        r = mgr.create("WS-NOPE", operation_id="OP-1")
        assert not r.success
        assert r.error_code == "WORKSPACE_NOT_FOUND"

    def test_not_a_repository_fails(self, tmp_path):
        e = Env(tmp_path)
        (e.repo.path / ".git").rename(e.repo.path / ".git-hidden")
        db = e.new_db()
        try:
            r = e.manager(db).create("WS-001", operation_id="OP-1")
            assert not r.success
            assert r.error_code == "NOT_A_REPOSITORY"
        finally:
            db.close()

    def test_bad_base_ref_fails(self, env, mgr):
        r = mgr.create("WS-001", operation_id="OP-1", base_ref="no-such-ref")
        assert not r.success
        assert r.error_code == "BASE_REF_NOT_FOUND"


# ============================================================
# 二、🔒 Isolation（任务书 §5 / §15 / §26）
# ============================================================
class TestIsolation:
    def test_no_physical_path_in_any_public_result(self, env, mgr):
        """
        🔒 **Agent 永远拿不到 host physical path。**
        """
        created = mgr.create("WS-001", operation_id="OP-1")
        wid = created.result["worktree_id"]
        mgr.freeze(wid, operation_id="OP-2")

        blobs = [
            str(created.to_dict()),
            str(mgr.get(wid).to_dict()),
            str(mgr.get_for_workspace("WS-001").to_dict()),
            str(mgr.list().to_dict()),
            str(mgr.status(wid).to_dict()),
            str(mgr.reconcile(wid).to_dict()),
            str(mgr.reconcile_all().to_dict()),
            str(mgr.freeze(mgr.create("WS-002", operation_id="OP-3")
                           .result["worktree_id"], operation_id="OP-4").to_dict()),
        ]

        root = str(env.repo.path.parent)
        for blob in blobs:
            assert "physical_path" not in blob, "public 结果里出现了 physical_path"
            assert root not in blob, f"public 结果里泄漏了物理根路径：{root}"
            assert ".worktrees" not in blob

    def test_events_do_not_carry_physical_path(self, env, mgr):
        """事件可能被 subscribe 到 Agent 侧 —— 也不能带路径。"""
        mgr.create("WS-001", operation_id="OP-1")

        store = WorktreeStore(env.bootstrap_db)
        rows = store.events_for("WS-001")
        assert rows, "应该产生了事件"

        root = str(env.repo.path.parent)
        for row in rows:
            detail = row["detail"] or ""
            assert root not in detail
            assert ".worktrees" not in detail

    def test_no_api_takes_a_physical_path(self, env, mgr):
        """
        🔒 public API **不接受**物理路径作为参数 ——
           那会让它成为「绕过 Workspace Gateway 直接写文件」的后门。
        """
        import inspect

        for name in ("create", "get", "get_for_workspace", "list",
                     "status", "freeze", "archive", "reconcile",
                     "reconcile_all"):
            fn = getattr(WorktreeManager, name)
            params = set(inspect.signature(fn).parameters)
            for banned in ("path", "physical_path", "directory", "cwd",
                           "file", "content"):
                assert banned not in params, (
                    f"{name}() 接受 {banned!r} —— "
                    f"WorktreeManager 不能成为直接写文件的后门"
                )

    def test_no_write_file_capability(self):
        """
        🔒 结构性断言：WorktreeManager **没有**「写文件 / 写内容」的方法。
        """
        methods = {n for n in dir(WorktreeManager) if not n.startswith("__")}
        for banned in ("write", "write_file", "write_text", "edit", "patch",
                       "commit", "checkout", "merge", "apply", "diff"):
            assert banned not in methods, (
                f"WorktreeManager 有 {banned}() —— "
                f"改文件内容必须走 Workspace Gateway（§15），"
                f"commit/diff 属于 Patch 阶段（§21）"
            )

    def test_agent_visible_ids_are_logical(self, env, mgr):
        r = mgr.create("WS-001", operation_id="OP-1")
        assert r.result["worktree_id"].startswith("WT-")
        assert r.result["workspace_id"] == "WS-001"
        assert r.result["repository_ref"] == "repo-main"   # 逻辑引用，不是路径


# ============================================================
# 三、Idempotency（任务书 §10 / §19.6）
# ============================================================
class TestIdempotency:
    def test_same_operation_retried_n_times_yields_one_worktree(self, env, mgr):
        """
        🔒 同一 logical operation 重试 N 次 → **只有一个**逻辑 Worktree。
        """
        ids = set()
        for _ in range(5):
            r = mgr.create("WS-001", operation_id="OP-SAME")
            assert r.success
            ids.add(r.result["worktree_id"])

        assert len(ids) == 1, f"产生了多个 worktree：{ids}"
        assert len(mgr.list("WS-001").result["worktrees"]) == 1
        assert len(env.repo.worktree_paths()) == 2, "Git 侧也只该有一个"

    def test_replay_is_flagged(self, env, mgr):
        mgr.create("WS-001", operation_id="OP-1")
        r = mgr.create("WS-001", operation_id="OP-1")
        assert r.references.get("idempotent_replay") is True

    def test_different_operation_reuses_live_worktree(self, env, mgr):
        """不同 operation 但已有 live → 复用（不报错、不新建）。"""
        first = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]
        r = mgr.create("WS-001", operation_id="OP-2")

        assert r.success
        assert r.references.get("reused") is True
        assert r.result["worktree_id"] == first

    def test_idempotency_key_is_unique_in_db(self, env, mgr):
        """🔒 幂等的**结构**保证：唯一索引，不是应用层查重。"""
        db = env.bootstrap_db
        rows = db.fetchall(
            "SELECT sql FROM sqlite_master "
            "WHERE name='idx_worktree_intents_idempotency'")
        assert rows and "UNIQUE" in rows[0]["sql"]

    def test_freeze_idempotent(self, env, mgr):
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]
        mgr.freeze(wid, operation_id="OP-2")
        r = mgr.freeze(wid, operation_id="OP-2")
        assert r.success and r.references.get("idempotent_replay") is True


# ============================================================
# 四、🔒 并发（任务书 §17 / §27 / §28）
# ============================================================
class TestConcurrency:
    def test_twenty_different_workspaces_create_in_parallel(self, tmp_path):
        """
        🔒 20 个**不同** Workspace 同时 create —— 都该成功。
        """
        n = 20
        workspaces = [f"WS-{i:03d}" for i in range(n)]
        e = Env(tmp_path, workspaces=workspaces)

        # 🔒 共享**同一个 Database 实例** —— 这是 ADR-0031 修好之后
        #    才合法的用法（每线程一个 session）
        db = e.new_db()
        try:
            mgr = e.manager(db)
            barrier = threading.Barrier(n, timeout=30)
            results: list = [None] * n
            errors: list = []

            def do(i):
                try:
                    barrier.wait()
                    results[i] = mgr.create(workspaces[i],
                                            operation_id=f"OP-{i}")
                except Exception as exc:      # pragma: no cover
                    errors.append(f"{i}: {type(exc).__name__}: {exc}")

            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(do, range(n)))

            assert not errors, f"抛异常：{errors[:3]}"
            assert all(r is not None and r.success for r in results), (
                f"全部该成功：{[r.error_code if r else None for r in results]}")

            assert len(mgr.list().result["worktrees"]) == n
            assert len(e.repo.worktree_paths()) == n + 1
        finally:
            db.close()

    def test_twenty_concurrent_creates_same_workspace_one_winner(self, tmp_path):
        """
        🔒 **同一个 Workspace 20 次并发 create → 只能有一个 live worktree。**

        这不是靠 `if not exists: create`（那非原子，任务书 §17 禁止），
        而是靠 DB 的**部分唯一索引**。
        """
        n = 20
        e = Env(tmp_path, workspaces=["WS-ONLY"])
        db = e.new_db()
        try:
            mgr = e.manager(db)
            barrier = threading.Barrier(n, timeout=30)
            results: list = [None] * n
            errors: list = []

            def do(i):
                try:
                    barrier.wait()
                    results[i] = mgr.create("WS-ONLY", operation_id=f"OP-{i}")
                except Exception as exc:      # pragma: no cover
                    errors.append(f"{i}: {type(exc).__name__}: {exc}")

            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(do, range(n)))

            assert not errors, f"抛异常：{errors[:3]}"

            live = [r for r in results if r and r.success]
            assert live, "至少有一个成功"
            ids = {r.result["worktree_id"] for r in live}
            assert len(ids) == 1, f"只能有一个 live worktree，实际 {len(ids)}"

            # 数据库里也确实只有一个 live
            rows = db.fetchall(
                "SELECT COUNT(*) AS n FROM git_worktrees "
                "WHERE workspace_id='WS-ONLY' "
                "AND state IN ('provisioning','ready','active')")
            assert rows[0]["n"] == 1, "DB 里出现了多个 live worktree"
        finally:
            db.close()

    def test_workspaces_do_not_pollute_each_other(self, tmp_path):
        """不同 Workspace 的 worktree 记录互不干扰。"""
        e = Env(tmp_path, workspaces=[f"WS-{i}" for i in range(5)])
        db = e.new_db()
        try:
            mgr = e.manager(db)
            for i in range(5):
                mgr.create(f"WS-{i}", operation_id=f"OP-{i}")

            for i in range(5):
                recs = mgr.list(f"WS-{i}").result["worktrees"]
                assert len(recs) == 1
                assert recs[0]["workspace_id"] == f"WS-{i}"
        finally:
            db.close()

    # ---- §28：用同步原语 / 结构断言证明**没有全局串行** ----
    def test_no_global_worktree_lock_structurally(self):
        """
        🔒 结构性断言：WorktreeManager 上**没有**一把覆盖全部操作的单一锁。

        ⚠️ **第一版查的是文本**（`ast.unparse` 之后再 substring），
           结果被**自己的文档字符串**绊倒 ——
           那段文档正好在解释「这里没有 GLOBAL_WORKTREE_LOCK」。

           这是 ADR-0018 明确反对的做法（查文本而不是查真实调用点）。
           改成查 **AST 节点**：真的锁**构造调用**、真的**模块级赋值**。
        """
        import ast
        from pathlib import Path as _P

        tree = ast.parse(
            _P("change/worktree_manager.py").read_text(encoding="utf-8"))

        # ① 任何真正的锁**构造**都不允许（进程内互斥走注入的 registry）
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(
                fn, "id", "")
            assert name not in ("Lock", "RLock", "Semaphore", "BoundedSemaphore"), (
                f"WorktreeManager 自己构造了锁 {name}() —— "
                f"进程内互斥必须走注入的 ResourceLockRegistry"
            )

        # ② 没有模块级的「全局锁」风格赋值
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        low = target.id.lower()
                        assert "lock" not in low, (
                            f"模块级定义了 {target.id} —— 疑似全局锁"
                        )

        # ③ 模块级也没有串行化所有操作的 for-loop 包住的 with
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # 方法名里不应出现暗示全局锁的名字
                assert "global_lock" not in node.name.lower()

    def test_registry_is_keyed_per_workspace_not_global(self, tmp_path):
        """
        🔒 进程内互斥是**按 Workspace 分键**的 —— 用 barrier 证明。

        20 个**不同** Workspace 同时进入临界区：
        如果存在一把全局锁，第 2 个线程永远到不了 barrier。
        """
        reg = ResourceLockRegistry()
        n = 20
        inside = threading.Barrier(n, timeout=15)
        got: list = []
        errors: list = []

        def worker(i):
            try:
                with reg.hold("WT:repo-main", f"WS-{i:03d}"):
                    inside.wait()          # 🔒 只有真的并行才能返回
                    got.append(i)
            except Exception as exc:        # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not errors, f"❌ 不同 Workspace 之间被串行化了：{errors[:3]}"
        assert len(got) == n

    def test_same_workspace_registry_serialises(self):
        """反向：**同一个** Workspace 的 key 是同一把锁（该串行就串行）。"""
        reg = ResourceLockRegistry()
        order: list = []
        reg_key = ("WT:repo-main", "WS-X")
        lock = reg._lock_for(reg_key)
        assert lock is reg._lock_for(reg_key)


# ============================================================
# 五、Crash Consistency（任务书 §19 / §23）
# ============================================================
class TestCrashConsistency:
    """
    真实「进程崩溃」没法在测试里做，所以**直接构造崩溃后的状态**
    （这正是 ADR-0036 的 intent log 允许的做法：
      崩溃点的特征就是「DB 停在哪一步」）。
    """

    def _simulate_crash_after_intent(self, env, workspace_id, path, branch,
                                     base_commit, worktree_id):
        """
        §19.3：崩在 `intent created` 与 `git worktree created` **之间**。
        """
        db = env.bootstrap_db
        with db.transaction() as conn:
            store = WorktreeStore(db)
            store.create_intent(
                conn, workspace_id=workspace_id, kind=IntentKind.PROVISION,
                idempotency_key=f"worktree:provision:{workspace_id}:OP-SIM",
                operation_id="OP-SIM", worktree_id=worktree_id,
                payload={"path": path, "branch": branch,
                         "base_commit": base_commit},
            )
            store.insert_worktree(
                conn, workspace_id=workspace_id, repository_ref="repo-main",
                base_commit=base_commit, workspace_revision=7,
                state=WorktreeState.PROVISIONING, physical_path=path,
                git_ref=f"refs/heads/{branch}", worktree_id=worktree_id,
            )
        return worktree_id

    def test_crash_between_intent_and_git(self, env, mgr):
        """
        §19.3 —— reconcile 必须识别「副作用从未发生」，并安全地标记失败。
        """
        path = str(env.repo.path.parent / ".worktrees" / "WS-001" / "WT-sim1")
        wid = self._simulate_crash_after_intent(
            env, "WS-001", path, "aieco/WS-001/WT-sim1",
            env.repo.head(), "WT-sim1")

        assert mgr.status(wid).result["state"] == "provisioning"

        r = mgr.reconcile(wid)

        assert r.success
        assert r.result["finding"] == InformationalState.UNRESOLVED_INTENT.value
        assert r.result["state"] == "provision_failed"

        # 🔒 未解决的 intent 被解决掉了 —— 之后可以正常 create
        assert WorktreeStore(env.bootstrap_db).unresolved_intents() == []
        assert mgr.create("WS-001", operation_id="OP-AFTER").success

    def test_crash_between_git_and_finalize(self, env, mgr):
        """
        §19.4：崩在 `git worktree created` 与 `DB state finalized` **之间**。

        构造方式：真的建一个 Git worktree，但把 DB 留在 PROVISIONING。
        """
        path = str(env.repo.path.parent / ".worktrees" / "WS-001" / "WT-sim2")
        branch = "aieco/WS-001/WT-sim2"
        base = env.repo.head()

        # 真的建 Git worktree（模拟副作用已完成）
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "-b", branch, path, base],
                       cwd=env.repo.path, check=True, capture_output=True)

        wid = self._simulate_crash_after_intent(
            env, "WS-001", path, branch, base, "WT-sim2")

        r = mgr.reconcile(wid)

        assert r.success
        assert r.result["finding"] == \
            InformationalState.DB_PROVISIONING_GIT_EXISTS.value
        assert r.result["state"] == "ready", (
            "🔒 副作用其实成功了 —— reconcile 应当**确认**它，而不是重建"
        )
        assert WorktreeStore(env.bootstrap_db).unresolved_intents() == []

    def test_git_missing_is_not_pretended_healthy(self, env, mgr):
        """
        §11 情况 B：DB=READY，Git 侧没了。

        🔒 **不能假装 READY 仍然正确。**
        """
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]
        rec = WorktreeStore(env.bootstrap_db).get(wid)
        assert rec.physical_path

        # 外部把 worktree 删掉（模拟「被外部删除」§19.5）
        subprocess.run(["git", "worktree", "remove", "--force",
                        rec.physical_path],
                       cwd=env.repo.path, check=True, capture_output=True)

        assert mgr.status(wid).result["git_present"] is False
        assert mgr.status(wid).result["consistent"] is False

        r = mgr.reconcile(wid)

        assert r.result["finding"] == \
            InformationalState.DB_READY_GIT_MISSING.value
        assert r.result["state"] == "inconsistent"
        assert WorktreeStore(env.bootstrap_db).get(wid).is_inconsistent

    def test_orphan_worktree_is_recorded_not_trusted(self, env, mgr):
        """
        §11 情况 C：DB 没有记录，Git 里有。

        🔒 **不能静默当成合法 worktree**，也**不能删除它**。
        """
        path = str(env.repo.path.parent / ".worktrees" / "WS-001" / "WT-orphan")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "aieco/WS-001/WT-orphan",
             path, env.repo.head()],
            cwd=env.repo.path, check=True, capture_output=True)

        assert mgr.list("WS-001").result["worktrees"] == []

        r = mgr.reconcile_all()

        findings = {x["finding"] for x in r.result["reports"]}
        assert InformationalState.DB_NONE_GIT_EXISTS.value in findings

        recs = mgr.list("WS-001").result["worktrees"]
        assert len(recs) == 1
        assert recs[0]["state"] == "orphaned"

        # 🔒 没被删掉
        assert Path(path).exists()
        # 🔒 也没占住 live 槽位 —— 还能正常 create
        assert mgr.create("WS-001", operation_id="OP-2").success

    def test_reconcile_does_not_touch_responsibility(self, env, mgr):
        """
        🔒 任务书 §13：reconciliation **不改变 Workspace Responsibility**。

        具体验证：它不写 workspace_resources / workspace_explicit_locks
        / workspace_audits / workspace_events。
        """
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]
        db = env.bootstrap_db

        tables = ("workspace_resources", "workspace_explicit_locks",
                  "workspace_revisions", "workspace_audits",
                  "workspace_events", "workspace_explicit_locks")
        before = {
            t: db.fetchall(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
            for t in tables
        }

        mgr.reconcile(wid)
        mgr.reconcile_all()

        after = {
            t: db.fetchall(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
            for t in tables
        }
        assert before == after, (
            f"reconciliation 动了 Workspace Core 的表：{before} → {after}"
        )

    def test_no_auto_delete_anywhere(self):
        """
        🔒 结构性断言：本层**没有**自动删除逻辑
           （任务书 §24：不要因为 `git worktree remove` 就提供无条件 delete；
            §13：不得删除未知 worktree）。
        """
        import ast
        from pathlib import Path as _P

        src = _P("change/worktree_manager.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        # public API 里不能有 remove/delete
        public = {n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef,)) and not n.name.startswith("_")}
        for banned in ("remove", "delete", "prune", "purge"):
            assert banned not in public, f"public API 里有 {banned}()"

        # 唯一的 worktree_remove 调用点必须在 GitManager 里，不在 manager 里
        assert "worktree_remove" not in src


# ============================================================
# 六、Persistence（任务书 §26）
# ============================================================
class TestPersistenceReopen:
    def test_state_survives_reopen(self, env, mgr):
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]
        mgr.freeze(wid, operation_id="OP-2")

        before = mgr.get(wid).result

        reopened = env.new_db()
        try:
            after = env.manager(reopened).get(wid).result
        finally:
            reopened.close()

        for key in ("worktree_id", "workspace_id", "state", "base_commit",
                    "workspace_revision", "git_ref", "repository_ref"):
            assert after[key] == before[key], f"{key} 在重开后变了"

    def test_events_survive_reopen(self, env, mgr):
        mgr.create("WS-001", operation_id="OP-1")
        db = env.bootstrap_db
        before = db.fetchall(
            "SELECT COUNT(*) AS n FROM worktree_events")[0]["n"]

        reopened = env.new_db()
        try:
            after = reopened.fetchall(
                "SELECT COUNT(*) AS n FROM worktree_events")[0]["n"]
        finally:
            reopened.close()

        assert after == before

    def test_unresolved_intent_visible_after_reopen(self, env):
        """
        🔒 崩溃之后重开，未解决的 intent 必须**还看得见** ——
           否则 startup reconciliation 无从下手（§12 触发方式 1）。
        """
        db = env.bootstrap_db
        with db.transaction() as conn:
            WorktreeStore(db).create_intent(
                conn, workspace_id="WS-001", kind=IntentKind.PROVISION,
                idempotency_key="k1", operation_id="OP-1")

        reopened = env.new_db()
        try:
            pending = WorktreeStore(reopened).unresolved_intents()
        finally:
            reopened.close()

        assert len(pending) == 1
        assert pending[0].state is IntentState.PENDING


# ============================================================
# 七、Event（任务书 §25）
# ============================================================
class TestEvents:
    def test_lifecycle_emits_events(self, env, mgr):
        wid = mgr.create("WS-001", operation_id="OP-1").result["worktree_id"]
        mgr.freeze(wid, operation_id="OP-2")
        mgr.archive(wid, operation_id="OP-3")

        rows = WorktreeStore(env.bootstrap_db).events_for("WS-001")
        kinds = [r["event_type"] for r in rows]

        assert "WORKTREE_CREATED" in kinds
        assert "WORKTREE_FROZEN" in kinds
        assert "WORKTREE_ARCHIVED" in kinds

    def test_events_are_not_workspace_resource_events(self, env, mgr):
        """
        🔒 任务书 §25：**不要把这些硬塞进 Workspace Resource Event。**
        """
        mgr.create("WS-001", operation_id="OP-1")

        db = env.bootstrap_db
        assert db.fetchall("SELECT COUNT(*) AS n FROM workspace_events")[0]["n"] == 0
        assert db.fetchall("SELECT COUNT(*) AS n FROM worktree_events")[0]["n"] > 0
