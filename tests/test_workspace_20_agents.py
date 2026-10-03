"""
20 Agent 并行验证（任务书 §19 / §20 / §21）
============================================================

【这一组测试到底在验证什么】

    不是「20 个线程没崩」，而是：

        20 个 Agent identity 同时压在 Workspace Core 上时，
        **§20 列出的核心不变量还成立吗？**

            Identity     resource_id 唯一
            Revision     单调递增，不回退
            Lock         同一 File 最多一个 Effective Holder
            Responsibility  显式边界不被父级错误吞掉
            Isolation    不同 Workspace 不互相污染
            Path         无 Workspace escape
            Atomicity    无半成功状态
            Audit/Event  成功变化有可追溯历史
            No silent overwrite  不能绕过 Lock + Revision

【🔒 关于「不要全局串行」怎么**可测地**证明】

    任务书 §21 错误 1 明确禁止为了通过测试而给 Gateway 加全局大锁。

    但「没有全局锁」不能靠「跑得快」来证明（那会 flaky）。
    本文件用三层**直接**证据：

        ① `TestNoGlobalSerialization` —— 直接测锁注册表：
           20 个不同资源 → 20 把独立的锁，
           而且可以**同时**处在临界区（用 barrier 证明，不是靠计时）。
        ② 锁 key 观察 —— 记录 Gateway 实际用过的锁 key，
           断言不同资源用的是**不同** key（全局锁只会有一个 key）。
        ③ 结构性断言 —— Gateway 没有任何单一共享锁字段。

【⚠️ 关于 SQLite single-writer（任务书 §21 错误 2）】

    SQLite 是单写者 ⇒ 写事务在**基础设施层**会排队。
    这是**已知的、如实记录的**瓶颈，
    **不等于** Workspace 的责任模型，也**不**会让别的 Agent
    得到 `LOCKED_BY_OTHER_AGENT`。

    所以本文件**不**声称「20 个写操作真的在物理上同时落盘」——
    那是假的。本文件声称的是：
        **Workspace 的并发控制是资源级的，
          SQLite 的写序列化是另一层的事。**
"""

from __future__ import annotations

import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.workspace_concurrency import ResourceLockRegistry
from tests.workspace_harness import WorkspaceHarness, check_workspace_invariants

AGENT_COUNT = 20


def agents(n: int = AGENT_COUNT, prefix: str = "Agent") -> list[str]:
    """Agent-01 … Agent-20（只是 Workspace 层的 actor，不是真实 LLM）。"""
    return [f"{prefix}-{i:02d}" for i in range(1, n + 1)]


# ============================================================
# 统计记录器 —— 任务书 §25 要求记录的量
# ============================================================
class Stats:
    def __init__(self) -> None:
        self.passed = 0
        self.rejected = 0
        self.stale = 0
        self.exceptions: list = []
        self.acquired_keys: Counter = Counter()
        self.started_at = 0.0
        self.finished_at = 0.0
        self._guard = threading.Lock()

    def record(self, result) -> None:
        with self._guard:
            if result is None:
                self.rejected += 1
            elif result.success:
                self.passed += 1
            else:
                self.rejected += 1
                if result.error_code == "STALE_REVISION":
                    self.stale += 1

    def record_exception(self, exc: BaseException) -> None:
        with self._guard:
            self.exceptions.append(exc)

    def summary(self) -> str:
        elapsed = self.finished_at - self.started_at
        return (
            f"passed={self.passed} rejected={self.rejected} "
            f"stale={self.stale} exceptions={len(self.exceptions)} "
            f"elapsed={elapsed:.2f}s"
        )


class ObservingRegistry(ResourceLockRegistry):
    """
    记录**实际用到过哪些锁 key** 的注册表。

    🔒 这是「没有全局锁」的直接证据：
       如果 Gateway 用了全局大锁，20 个不同资源也只会出现 **1 个 key**。
    """

    def __init__(self) -> None:
        super().__init__()
        self.seen_keys: Counter = Counter()

    def hold(self, workspace_id: str, *resource_keys: str):
        for k in resource_keys:
            if k:
                self.seen_keys[(workspace_id, k)] += 1
        return super().hold(workspace_id, *resource_keys)


def run_parallel(fn, items) -> list:
    """
    用 `ThreadPoolExecutor(max_workers=20)` 跑 `items`，每个 item 一个任务。

    每个任务**自己开一个 Database 连接**（技术债 #2），
    但**共享** ResourceLockRegistry（进程内资源级互斥）。
    """
    with ThreadPoolExecutor(max_workers=AGENT_COUNT) as pool:
        return list(pool.map(fn, items))


@pytest.fixture
def h20(tmp_path):
    """20 个 Agent + 一个文件库。"""
    names = agents()
    h = WorkspaceHarness.on_file(
        tmp_path / "ws20.db",
        participants={"W1": set(names) | {"Owner"}},
        agents=set(names) | {"Owner"},
    )
    h.registry = ObservingRegistry()
    h.locks = h.registry
    h.bootstrap("W1", "Owner")
    return h


def gw_for(h):
    """一个绑定了观察注册表的 gateway。"""
    return h.gateway()


# ============================================================
# 零、直接证明「没有全局串行」
# ============================================================
class TestNoGlobalSerialization:
    def test_registry_is_resource_level_not_global(self):
        """
        🔒 **20 个不同资源 → 20 把独立的锁，而且可以同时进入临界区。**

        这个测试用 `Barrier` 而不是计时 ——
        如果存在一把全局锁，第 2 个线程永远等不到 barrier，
        测试会**确定性**地超时失败，而不是偶尔 flaky。

        （barrier 放在**临界区内部**：只有 20 个线程
          **同时**持有各自的锁，才能全部到达。）
        """
        reg = ResourceLockRegistry()
        n = AGENT_COUNT
        inside = threading.Barrier(n, timeout=10)
        ok = []
        errors = []

        def worker(i):
            try:
                with reg.hold("W1", f"RES-{i}"):
                    # 🔒 只有真的并行，这句才可能返回
                    inside.wait()
                    ok.append(i)
            except Exception as exc:      # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, (
            f"❌ 不同资源之间发生了串行化 —— barrier 超时：{errors}"
        )
        assert len(ok) == n, f"只有 {len(ok)}/{n} 个线程同时进入了临界区"

    def test_gateway_uses_distinct_locks_per_resource(self, h20):
        """
        🔒 Gateway 对不同资源用的是**不同**锁 key。

        如果它加了全局大锁，`seen_keys` 里只会有一个 key。
        """
        gw = gw_for(h20)
        names = agents()

        # 每个 Agent 一个自己的目录（先串行建好）
        for i, name in enumerate(names):
            assert gw.create("W1", "/", f"d{i:02d}", "directory", "Owner").success
            assert gw.handoff("W1", f"/d{i:02d}", "Owner", name, "Owner",
                              reason="分派").success
            assert gw.create("W1", f"/d{i:02d}", "f.txt", "file", name,
                             content="v").success

        h20.registry.seen_keys.clear()

        def do(i):
            with h20.session() as g:
                return g.edit("W1", f"/d{i:02d}/f.txt", 1, f"new-{i}", names[i])

        results = run_parallel(do, list(range(len(names))))

        assert all(r.success for r in results), (
            f"全部该成功：{[r.error_code for r in results if not r.success]}")

        ns_keys = {k for k in h20.registry.seen_keys if k[1].startswith("NS:")}
        res_keys = {k for k in h20.registry.seen_keys if not k[1].startswith("NS:")}

        # 20 个文件各自一把资源锁 —— 不是一把全局锁
        assert len(res_keys) == len(names), (
            f"应该是 {len(names)} 把不同的资源锁，实际 {len(res_keys)}："
            f"{sorted(k[1] for k in res_keys)}"
        )

    def test_gateway_has_no_single_shared_lock_attribute(self, h20):
        """
        结构性断言：Gateway 上**没有**一把覆盖全部操作的单一锁。

        （`_locks` / `_global_lock` / `_mutex` 这类名字不该出现，
          而 `locks` 应该是一个**按资源分键**的注册表。）
        """
        gw = gw_for(h20)

        for banned in ("_global_lock", "_gateway_lock", "_mutex", "_big_lock"):
            assert not hasattr(gw, banned), f"Gateway 上有全局锁字段 {banned}"

        assert isinstance(gw.locks, ResourceLockRegistry), (
            "Gateway 的并发控制必须走资源级注册表"
        )
        # 它是一个**可增长的按资源分键的**表，不是单把锁
        assert hasattr(gw.locks, "hold"), "应提供按资源取锁的接口"


# ============================================================
# 场景一：20 Agent / 20 独立资源
# ============================================================
class TestScenario1_IndependentResources:
    def test_twenty_agents_twenty_files(self, h20):
        """
        20 Agents / 20 Files **同时 edit**。

        要求：不因 Workspace 内存在其他 Agent 而全局串行；
              最终全部成功、revision 正确、audit 正确、event 正确。
        """
        gw = gw_for(h20)
        names = agents()

        # ---- 准备：每人一个自己的目录 + 文件 ----
        for i, name in enumerate(names):
            assert gw.create("W1", "/", f"d{i:02d}", "directory", "Owner").success
            assert gw.handoff("W1", f"/d{i:02d}", "Owner", name, "Owner",
                              reason="分派").success
            assert gw.create("W1", f"/d{i:02d}", "f.txt", "file", name,
                             content="v0").success

        stats = Stats()
        start_barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(i):
            name = names[i]
            try:
                with h20.session() as g:
                    start_barrier.wait()          # 🔒 同步起跑
                    return g.edit("W1", f"/d{i:02d}/f.txt", 1, f"by-{name}", name)
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        stats.started_at = time.perf_counter()
        results = run_parallel(do, list(range(len(names))))
        stats.finished_at = time.perf_counter()
        for r in results:
            stats.record(r)

        # ---- 结果 ----
        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"
        assert stats.passed == AGENT_COUNT, (
            f"20 个独立资源全部该成功：{stats.summary()}")
        assert stats.rejected == 0 and stats.stale == 0

        # ---- revision 正确 ----
        for i, name in enumerate(names):
            got = gw.read("W1", f"/d{i:02d}/f.txt", name).result
            assert got["revision"] == 2, f"Agent-{i:02d} 的 revision={got['revision']}"
            assert got["content"] == f"by-{name}"

        # ---- audit 正确 ----
        with h20.connection() as db:
            edits = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_audits "
                "WHERE action='EDIT' AND result='SUCCESS'")[0]["n"]
            assert edits == AGENT_COUNT, f"应有 {AGENT_COUNT} 条 EDIT Audit，实际 {edits}"

            # ---- event 正确 ----
            ups = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_events "
                "WHERE event_type='RESOURCE_UPDATED'")[0]["n"]
            assert ups == AGENT_COUNT, f"应有 {AGENT_COUNT} 条 UPDATED Event，实际 {ups}"

            check_workspace_invariants(db, "W1")


# ============================================================
# 场景二：20 Agent / 同一 File
# ============================================================
class TestScenario2_SameFile:
    def test_twenty_agents_same_file_only_holder_wins(self, h20):
        """
        20 Agent 同时 edit **同一个文件**，但只有 1 个是 Effective Holder。

        要求：1 个成功，其余 19 个**明确** `LOCKED_BY_OTHER_AGENT`。
              拒绝必须是确定性的、有结构化 error 的。
        """
        gw = gw_for(h20)
        names = agents()
        gw.create("W1", "/", "shared.txt", "file", "Owner", content="v0")
        gw.handoff("W1", "/shared.txt", "Owner", names[0], "Owner",
                   reason="交给 Agent-01")

        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(i):
            name = names[i]
            try:
                with h20.session() as g:
                    barrier.wait()
                    return g.edit("W1", "/shared.txt", 1, f"by-{name}", name)
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, list(range(len(names))))
        for r in results:
            stats.record(r)

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"

        # 🔒 恰好一个成功
        assert stats.passed == 1, f"只有 Holder 能成功：{stats.summary()}"
        assert stats.rejected == AGENT_COUNT - 1

        # 🔒 19 个失败必须是**明确**的 LOCKED_BY_OTHER_AGENT
        codes = Counter(r.error_code for r in results if r and not r.success)
        assert codes == Counter({"LOCKED_BY_OTHER_AGENT": AGENT_COUNT - 1}), (
            f"失败码必须全部明确：{codes}")

        # 🔒 最终 revision == 2（create R1 + 一次成功 edit）
        assert gw.read("W1", "/shared.txt", names[0]).result["revision"] == 2

        with h20.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_twenty_agents_same_file_same_base_revision(self, h20):
        """
        20 个请求都来自**同一个 Holder** 且都基于 **同一个 base_revision**。

        要求：恰好 1 个成功，其余 19 个 `STALE_REVISION`，
              **final revision == 成功次数 + 1**。
        """
        gw = gw_for(h20)
        holder = agents()[0]
        gw.create("W1", "/", "hot.txt", "file", "Owner", content="v0")
        gw.handoff("W1", "/hot.txt", "Owner", holder, "Owner", reason="给 01")

        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(i):
            try:
                with h20.session() as g:
                    barrier.wait()
                    return g.edit("W1", "/hot.txt", 1, f"v{i}", holder)
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, list(range(AGENT_COUNT)))
        for r in results:
            stats.record(r)

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"
        assert stats.passed == 1, f"基于同一 base_revision 只能成功一次：{stats.summary()}"
        assert stats.stale == AGENT_COUNT - 1, (
            f"其余必须是 STALE_REVISION：{stats.summary()}")

        final = gw.read("W1", "/hot.txt", holder).result["revision"]
        assert final == stats.passed + 1, (
            f"🔒 final revision({final}) 必须严格 == 成功次数({stats.passed})+1")

        with h20.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_twenty_agents_progressive_revision_all_succeed(self, h20):
        """
        20 个请求来自同一个 Holder，但每个都**重新读取**当前 revision
        （乐观锁的正确用法）→ 全部成功，revision 严格等于成功次数 + 1。
        """
        gw = gw_for(h20)
        holder = agents()[0]
        gw.create("W1", "/", "seq.txt", "file", "Owner", content="v0")
        gw.handoff("W1", "/seq.txt", "Owner", holder, "Owner", reason="给 01")

        stats = Stats()

        def do(i):
            try:
                with h20.session() as g:
                    current = g.read("W1", "/seq.txt", holder).result["revision"]
                    return g.edit("W1", "/seq.txt", current, f"v{i}", holder)
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, list(range(AGENT_COUNT)))
        for r in results:
            stats.record(r)

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"

        final = gw.read("W1", "/seq.txt", holder).result["revision"]
        assert final == stats.passed + 1, (
            f"🔒 final revision({final}) 必须严格 == 成功次数({stats.passed})+1"
            f" —— {stats.summary()}")

        with h20.connection() as db:
            check_workspace_invariants(db, "W1")
            rid = db.fetchall(
                "SELECT resource_id FROM workspace_resources "
                "WHERE workspace_id='W1' AND path='/seq.txt'")[0]["resource_id"]
            nums = [r["revision_number"] for r in db.fetchall(
                "SELECT revision_number FROM workspace_revisions "
                "WHERE resource_id=? ORDER BY revision_number", (rid,))]
            assert nums == list(range(1, final + 1)), (
                f"revision 历史必须连续无空洞无重复：{nums}")


# ============================================================
# 场景三：20 Agent / 同一 Directory Namespace
# ============================================================
class TestScenario3_SameNamespace:
    def test_twenty_agents_same_namespace_only_owner_acts(self, h20):
        """
        20 Agent 同时在**同一个目录**里做 create / rename / delete / move。

        该目录的 Directory Lock 属于 Owner ⇒
        恰好 Owner 的操作生效，其余必须以结构化错误被拒绝，
        且 Namespace 保持一致、不 deadlock。
        """
        gw = gw_for(h20)
        names = agents()
        gw.create("W1", "/", "shared", "directory", "Owner")
        gw.create("W1", "/", "shared2", "directory", "Owner")
        for i in range(AGENT_COUNT):
            gw.create("W1", "/shared", f"old{i:02d}.txt", "file", "Owner",
                      content="v")
            gw.create("W1", "/shared2", f"m{i:02d}.txt", "file", "Owner",
                      content="v")

        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(i):
            name = names[i]
            kind = i % 4
            try:
                with h20.session() as g:
                    barrier.wait()
                    if kind == 0:
                        return g.create("W1", "/shared", f"new{i:02d}.txt",
                                        "file", name, content="x")
                    if kind == 1:
                        return g.rename("W1", f"/shared/old{i:02d}.txt",
                                        f"ren{i:02d}.txt", name)
                    if kind == 2:
                        return g.delete("W1", f"/shared/old{i:02d}.txt", name)
                    return g.move("W1", f"/shared2/m{i:02d}.txt", "/shared", name)
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, list(range(AGENT_COUNT)))
        for r in results:
            stats.record(r)

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"
        assert stats.passed == 0, (
            f"非 Owner 不该有任何成功：{stats.summary()}")
        assert stats.rejected == AGENT_COUNT

        codes = Counter(r.error_code for r in results if r and not r.success)
        assert set(codes) <= {"NOT_EFFECTIVE_LOCK_HOLDER"}, (
            f"失败码必须明确：{codes}")

        with h20.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_owner_hammers_same_namespace_no_ghost(self, h20):
        """
        同一个身份（Owner）在**同一个目录**里并发做 20 个结构操作。

        要求：Namespace 一致、不产生重复 / ghost / 非法 resource、不 deadlock。
        """
        gw = gw_for(h20)
        gw.create("W1", "/", "ns", "directory", "Owner")

        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(i):
            try:
                with h20.session() as g:
                    barrier.wait()
                    return g.create("W1", "/ns", f"c{i:02d}.txt", "file",
                                    "Owner", content=f"v{i}")
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, list(range(AGENT_COUNT)))
        for r in results:
            stats.record(r)

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"
        assert stats.passed == AGENT_COUNT, (
            f"不同名字的并发 create 都该成功：{stats.summary()}")

        with h20.connection() as db:
            check_workspace_invariants(db, "W1")
            paths = sorted(r["path"] for r in db.fetchall(
                "SELECT path FROM workspace_resources "
                "WHERE workspace_id='W1' AND path LIKE '/ns/%'"))
            expected = sorted(f"/ns/c{i:02d}.txt" for i in range(AGENT_COUNT))
            assert paths == expected, f"Namespace 不一致：{paths}"

    def test_same_namespace_contention_single_winner(self, h20):
        """
        20 个 Agent 抢**同一个名字**的 create：
        恰好一个成功（Owner），其余明确 NAME_CONFLICT。
        """
        gw = gw_for(h20)
        gw.create("W1", "/", "ns", "directory", "Owner")

        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(i):
            try:
                with h20.session() as g:
                    barrier.wait()
                    return g.create("W1", "/ns", "contested.txt", "file",
                                    "Owner", content=f"v{i}")
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, list(range(AGENT_COUNT)))
        for r in results:
            stats.record(r)

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"
        assert stats.passed == 1, f"恰好一个成功：{stats.summary()}"
        codes = Counter(r.error_code for r in results if r and not r.success)
        assert codes == Counter({"NAME_CONFLICT": AGENT_COUNT - 1}), (
            f"其余必须 NAME_CONFLICT：{codes}")

        with h20.connection() as db:
            check_workspace_invariants(db, "W1")
            n = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_resources "
                "WHERE workspace_id='W1' AND path='/ns/contested.txt'")[0]["n"]
            assert n == 1, f"不能有重复身份：{n}"


# ============================================================
# 场景四：20 Agent / 多 Workspace（5 × 4）
# ============================================================
class TestScenario4_MultiWorkspace:
    N_WS = 5
    PER_WS = 4

    @pytest.fixture
    def multi(self, tmp_path):
        names = agents()
        participants = {}
        for w in range(self.N_WS):
            members = names[w * self.PER_WS:(w + 1) * self.PER_WS]
            participants[f"W{w}"] = set(members)
        h = WorkspaceHarness.on_file(
            tmp_path / "multi.db",
            participants=participants,
            agents=set(names),
        )
        for w in range(self.N_WS):
            wid = f"W{w}"
            owner = names[w * self.PER_WS]
            with h.session() as g:
                g.ensure_workspace(wid, owner)
                for m in participants[wid]:
                    assert g.create(wid, "/", f"by{m}", "directory",
                                    owner).success
                    assert g.handoff(wid, f"/by{m}", owner, m, owner,
                                     reason="分派").success
        return h, participants

    def test_five_workspaces_twenty_agents(self, multi):
        """5 Workspaces × 4 Agents 同时操作 → 无跨 Workspace 污染。"""
        h, participants = multi
        names = agents()
        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(i):
            name = names[i]
            wid = f"W{i // self.PER_WS}"
            try:
                with h.session() as g:
                    barrier.wait()
                    r1 = g.create(wid, f"/by{name}", "f.txt", "file", name,
                                  content=f"{wid}-{name}")
                    r2 = g.edit(wid, f"/by{name}/f.txt", 1, f"e-{wid}", name)
                    return r1, r2
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, list(range(AGENT_COUNT)))

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"
        assert all(r is not None for r in results)

        for i, pair in enumerate(results):
            r1, r2 = pair
            assert r1.success, f"{names[i]} create 失败：{r1.error_code}"
            assert r2.success, f"{names[i]} edit 失败：{r2.error_code}"
            assert r2.result["revision"] == 2

        # ---- 每个 Workspace 各自独立 ----
        for w in range(self.N_WS):
            wid = f"W{w}"
            with h.connection() as db:
                check_workspace_invariants(db, wid)

                mine = {r["resource_id"] for r in db.fetchall(
                    "SELECT resource_id FROM workspace_resources "
                    "WHERE workspace_id=?", (wid,))}
                others = {r["resource_id"] for r in db.fetchall(
                    "SELECT resource_id FROM workspace_resources "
                    "WHERE workspace_id != ?", (wid,))}
                assert not (mine & others), (
                    f"{wid} 与别的 Workspace 共享 resource_id")

                # audit / event 引用的资源必须属于本 Workspace
                for table in ("workspace_audits", "workspace_events"):
                    for row in db.fetchall(
                        f"SELECT resource_id FROM {table} WHERE workspace_id=?",
                        (wid,)
                    ):
                        if row["resource_id"] is None:
                            continue
                        owner = db.fetchall(
                            "SELECT workspace_id FROM workspace_resources "
                            "WHERE resource_id=?", (row["resource_id"],))
                        assert owner and owner[0]["workspace_id"] == wid, (
                            f"{wid} 的 {table} 引用了别的 Workspace 的资源")

    def test_cross_workspace_participant_denied(self, multi):
        """每个 Workspace 的成员不能读别的 Workspace。"""
        h, participants = multi
        names = agents()
        w0_member = names[0]
        w1 = "W1"

        with h.session() as g:
            assert g.read(w1, "/", w0_member).error_code == "NOT_TASK_PARTICIPANT"


# ============================================================
# 场景五：20 Agent / 混合 workload（固定 seed）
# ============================================================
class TestScenario5_MixedWorkload:
    def test_mixed_operations_seeded(self, h20):
        """
        混合 workload：read / edit / create / rename / move / delete /
        handoff / release 随机组合。

        🔒 **可重复**：固定 `random.seed` + 固定操作序列 ——
           不允许测试本身产生不可解释的 nondeterminism。
        """
        gw = gw_for(h20)
        names = agents()

        # ---- 准备：基础结构 ----
        for i, name in enumerate(names):
            assert gw.create("W1", "/", f"d{i:02d}", "directory", "Owner").success
            assert gw.handoff("W1", f"/d{i:02d}", "Owner", name, "Owner",
                              reason="分派").success
            assert gw.create("W1", f"/d{i:02d}", "base.txt", "file", name,
                             content="v0").success
        assert gw.create("W1", "/", "yard", "directory", "Owner").success

        # ---- 固定 seed 生成**确定**的操作计划 ----
        rng = random.Random(20240607)
        plan = []
        for i, name in enumerate(names):
            op = ["read", "edit", "create", "rename", "move",
                  "delete", "handoff"][rng.randrange(7)]
            plan.append((i, name, op))

        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)

        def do(item):
            i, name, op = item
            try:
                with h20.session() as g:
                    barrier.wait()
                    if op == "read":
                        return g.read("W1", f"/d{i:02d}/base.txt", name)
                    if op == "edit":
                        return g.edit("W1", f"/d{i:02d}/base.txt", 1,
                                      f"e{i}", name)
                    if op == "create":
                        return g.create("W1", f"/d{i:02d}", f"c{i}.txt",
                                        "file", name, content="x")
                    if op == "rename":
                        return g.rename("W1", f"/d{i:02d}/base.txt",
                                        f"r{i}.txt", name)
                    if op == "move":
                        return g.move("W1", f"/d{i:02d}/base.txt", "/yard", name)
                    if op == "delete":
                        return g.delete("W1", f"/d{i:02d}/base.txt", name)
                    return g.handoff("W1", f"/d{i:02d}", name, "Owner", name,
                                     reason="mixed")
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        results = run_parallel(do, plan)
        for r in results:
            stats.record(r)

        assert not stats.exceptions, f"抛异常：{stats.exceptions[:3]}"
        assert all(r is not None for r in results), "每个操作都要有结构化结果"

        # ---- 每个失败都必须是**明确的**结构化错误 ----
        for r in results:
            if not r.success:
                assert r.error_code, "失败必须有结构化 error code"
                assert r.operation_id, "失败也必须带 operation_id"

        # ---- 系统不变量 ----
        with h20.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_mixed_workload_is_reproducible(self, h20):
        """
        🔒 同一个 seed ⇒ 同一份操作计划。

        这条守卫防的是「测试自己产生不可解释的随机性」：
        如果计划不可复现，失败就无法定位。
        """
        def make_plan(seed):
            rng = random.Random(seed)
            return [
                ["read", "edit", "create", "rename", "move",
                 "delete", "handoff"][rng.randrange(7)]
                for _ in range(AGENT_COUNT)
            ]

        assert make_plan(20240607) == make_plan(20240607)
        assert len(make_plan(20240607)) == AGENT_COUNT
        # 不同 seed 应该给出不同计划（否则 seed 没起作用）
        assert make_plan(20240607) != make_plan(1)


# ============================================================
# 跨场景：统计汇总（任务书 §25 要求记录的量）
# ============================================================
class TestStatistics:
    def test_twenty_agents_summary(self, h20, capsys):
        """
        跑一轮代表性的 20 Agent workload，并把任务书 §25 要求的
        统计量打印出来（passed / rejected / stale / exceptions /
        deadlock / final revision / final state）。
        """
        gw = gw_for(h20)
        names = agents()
        for i, name in enumerate(names):
            gw.create("W1", "/", f"d{i:02d}", "directory", "Owner")
            gw.handoff("W1", f"/d{i:02d}", "Owner", name, "Owner", reason="分派")
            gw.create("W1", f"/d{i:02d}", "f.txt", "file", name, content="v0")

        stats = Stats()
        barrier = threading.Barrier(AGENT_COUNT, timeout=20)
        threads: list = []

        def do(i):
            try:
                with h20.session() as g:
                    barrier.wait()
                    return g.edit("W1", f"/d{i:02d}/f.txt", 1, f"e{i}", names[i])
            except Exception as exc:              # pragma: no cover
                stats.record_exception(exc)
                return None

        stats.started_at = time.perf_counter()
        results = run_parallel(do, list(range(AGENT_COUNT)))
        stats.finished_at = time.perf_counter()
        for r in results:
            stats.record(r)

        deadlocks = 0     # ThreadPoolExecutor.map 全部返回 ⇒ 没有死锁
        with h20.connection() as db:
            resources = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_resources "
                "WHERE workspace_id='W1'")[0]["n"]
            revs = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_revisions "
                "WHERE workspace_id='W1'")[0]["n"]
            audits = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_audits "
                "WHERE workspace_id='W1'")[0]["n"]
            events = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_events "
                "WHERE workspace_id='W1'")[0]["n"]

        report = (
            f"\n  ┌─ 20 Agent 并行统计 ────────────────────────────\n"
            f"  │ Agents      = {AGENT_COUNT}\n"
            f"  │ Workspaces  = 1\n"
            f"  │ Operations  = {AGENT_COUNT}\n"
            f"  │ Passed      = {stats.passed}\n"
            f"  │ Rejected    = {stats.rejected}\n"
            f"  │ Stale       = {stats.stale}\n"
            f"  │ Exceptions  = {len(stats.exceptions)}\n"
            f"  │ Deadlocks   = {deadlocks}\n"
            f"  │ Resources   = {resources}\n"
            f"  │ Revisions   = {revs}\n"
            f"  │ Audits      = {audits}\n"
            f"  │ Events      = {events}\n"
            f"  │ elapsed     = {stats.finished_at - stats.started_at:.3f}s\n"
            f"  └────────────────────────────────────────────────"
        )
        print(report)

        assert stats.passed == AGENT_COUNT
        assert stats.exceptions == []
        with h20.connection() as db:
            check_workspace_invariants(db, "W1")
