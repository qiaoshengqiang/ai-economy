"""
Database 线程安全（技术债 #2 的基础设施层修复）
============================================================

【这一组测试验证什么】

    任务书任务一 + 任务五：

        同一个 `Database` **实例**
            ↓
        Thread A → 独立 tx depth + 独立连接
        Thread B → 独立 tx depth + 独立连接
        Thread C → 独立 tx depth + 独立连接

    而不是「禁止测试共享 Database」这种**在测试层掩盖问题**的做法。

【🔒 怎么证明，而不用计时】

    全部用 `threading.Barrier` + 明确的 transaction state 断言 +
    最终数据库状态。**没有一处用 elapsed time 判断并发。**

    barrier 放在**事务内部**时，只有「每个线程真的有独立事务上下文」
    才能全员到达 —— 旧模型（共享连接）会**确定性**地失败：

        · 要么 `cannot start a transaction within a transaction`
        · 要么深度被别的线程改掉，于是跳过 BEGIN / 提前 COMMIT

【⚠️ 为什么这组测试用文件库，而内存库单独有一条测试说明】

    SQLite 的 `:memory:` 是**每个连接一个独立的库**。
    想让多个连接共享它只能用 shared-cache URI ——
    而那个在并发写下**实测不成立**（见
    `test_in_memory_is_single_connection_by_nature`）。
    这是 SQLite 的性质，不是实现偷懒。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from storage.database import Database


@pytest.fixture
def shared_db(tmp_path):
    """
    一个**文件**数据库，被所有线程**共享同一个实例**。

    ⚠️ 注意：这里就是「同一个 Database 实例」——
        不再是「每线程一个 Database」。
        修复的正是这个场景。
    """
    db = Database(str(tmp_path / "shared.db"))
    db.initialize()
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS items "
                     "(id INTEGER PRIMARY KEY, owner TEXT, value TEXT)")
    yield db
    db.close()


def run_threads(targets, timeout=30):
    """跑一批线程，返回 [(异常 or None)]。"""
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    return threads


# ============================================================
# 一、深度是按线程隔离的
# ============================================================
class TestTransactionDepthIsThreadLocal:
    def test_thread_a_depth_differs_from_thread_b(self, shared_db):
        """
        🔒 **任务一的核心断言**：Thread A 的 tx depth ≠ Thread B 的 tx depth。

        做法：两个线程各自开一个**读事务**，在事务内部用 barrier 汇合，
        然后把各自看到的 depth 记下来。

        · 如果深度是共享的 → 第二个线程要么 BEGIN 失败，
          要么读到错误的值（1 而不是自己刚开的那个）
        · 只有「每个线程独立连接 + 独立深度」才能两个都到 1 且都到达 barrier
        """
        db = shared_db
        barrier = threading.Barrier(2, timeout=15)
        depths: dict[str, int] = {}
        errors: list = []
        inside: list = []

        def worker(name: str):
            try:
                with db.transaction(write=False):
                    inside.append(name)
                    barrier.wait()             # 🔒 事务内部汇合
                    depths[name] = db.transaction_depth
            except Exception as exc:           # pragma: no cover
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        run_threads([lambda: worker("A"), lambda: worker("B")])

        assert not errors, f"线程里抛异常（旧模型会在这里失败）：{errors}"
        assert sorted(inside) == ["A", "B"], "两个线程必须**同时**在事务里"
        assert depths == {"A": 1, "B": 1}, (
            f"每个线程都该看到**自己**的 depth=1，得到 {depths}"
        )

    def test_nested_depth_is_per_thread(self, shared_db):
        """
        一个线程嵌套到深度 2 的同时，另一个线程保持在深度 1。

        这证明深度不是「全局计数器」——
        全局计数器不可能同时是 2 和 1。
        """
        db = shared_db
        barrier = threading.Barrier(2, timeout=15)
        seen: dict[str, int] = {}
        errors: list = []

        def shallow():
            try:
                with db.transaction(write=False):
                    barrier.wait()
                    seen["shallow"] = db.transaction_depth
            except Exception as exc:           # pragma: no cover
                errors.append(exc)

        def deep():
            try:
                with db.transaction(write=False):
                    with db.transaction(write=False):
                        barrier.wait()
                        seen["deep"] = db.transaction_depth
            except Exception as exc:           # pragma: no cover
                errors.append(exc)

        run_threads([shallow, deep])

        assert not errors, f"抛异常：{errors}"
        assert seen == {"shallow": 1, "deep": 2}, (
            f"同一次汇合里两个线程的深度必须各自独立，得到 {seen}"
        )

    def test_depth_returns_to_zero_per_thread(self, shared_db):
        """事务结束后各自回到 0，互不影响。"""
        db = shared_db
        depths_seen: dict[str, list] = {}
        errors: list = []

        def worker(name: str):
            try:
                depths_seen[name] = [db.transaction_depth]
                with db.transaction(write=False):
                    depths_seen[name].append(db.transaction_depth)
                depths_seen[name].append(db.transaction_depth)
            except Exception as exc:           # pragma: no cover
                errors.append(exc)

        run_threads([lambda: worker("A"), lambda: worker("B")])

        assert not errors, f"抛异常：{errors}"
        for name, seq in depths_seen.items():
            assert seq == [0, 1, 0], f"{name} 的深度序列异常：{seq}"

    def test_other_thread_depth_not_visible_while_in_transaction(self, shared_db):
        """
        A 在事务里时，B（事务外）看到的深度必须是 **0**，不是 1。
        """
        db = shared_db
        a_inside = threading.Event()
        b_saw: list = []
        errors: list = []

        def a():
            try:
                with db.transaction(write=False):
                    a_inside.set()
                    # 等 B 观察完 —— 用事件，不用 sleep
                    for _ in range(200):
                        if b_saw:
                            break
                        threading.Event().wait(0.01)
            except Exception as exc:           # pragma: no cover
                errors.append(exc)

        def b():
            try:
                assert a_inside.wait(timeout=10), "A 没能进入事务"
                b_saw.append(db.transaction_depth)
            except Exception as exc:           # pragma: no cover
                errors.append(exc)

        run_threads([a, b])

        assert not errors, f"抛异常：{errors}"
        assert b_saw == [0], (
            f"A 在事务里时，B 看到的深度必须是 0，得到 {b_saw}"
        )

    def test_single_thread_behaviour_unchanged(self, shared_db):
        """不破坏现有单线程调用与嵌套语义。"""
        db = shared_db

        assert db.transaction_depth == 0
        assert not db.in_transaction()

        with db.transaction():
            assert db.transaction_depth == 1
            assert db.in_transaction()
            with db.transaction():
                assert db.transaction_depth == 2
                with db.transaction():
                    assert db.transaction_depth == 3
                assert db.transaction_depth == 2
            assert db.transaction_depth == 1
        assert db.transaction_depth == 0

    def test_nested_rollback_still_rolls_back_everything(self, shared_db):
        """嵌套事务的语义不变：内层失败 → 整个事务回滚。"""
        db = shared_db

        with pytest.raises(RuntimeError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO items (owner, value) VALUES ('x','1')")
                with db.transaction() as inner:
                    inner.execute(
                        "INSERT INTO items (owner, value) VALUES ('y','2')")
                    raise RuntimeError("内层炸了")

        with db.transaction(write=False) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM items WHERE owner IN ('x','y')").fetchone()[0]
        assert rows == 0, "内层失败必须回滚整个事务"


# ============================================================
# 二、每个线程一条独立连接
# ============================================================
class TestConnectionPerThread:
    def test_each_thread_gets_its_own_connection(self, shared_db):
        db = shared_db
        db.connect()                                  # 主线程的连接
        main_conn = db.connect()
        barrier = threading.Barrier(2, timeout=15)
        conns: dict[str, tuple] = {}
        errors: list = []

        def worker(name: str):
            try:
                conn = db.connect()
                conns[name] = (id(conn), conn)
                barrier.wait()
            except Exception as exc:           # pragma: no cover
                errors.append(exc)

        run_threads([lambda: worker("A"), lambda: worker("B")])

        assert not errors, f"抛异常：{errors}"
        ids = {id(main_conn)} | {v[0] for v in conns.values()}
        assert len(ids) == 3, f"三个线程必须各有各的连接，得到 {len(ids)} 个"

    def test_same_thread_reuses_its_connection(self, shared_db):
        """同一线程反复 connect() 拿到的必须是**同一个**连接。"""
        db = shared_db
        assert db.connect() is db.connect()

    def test_connection_count_tracks_threads(self, shared_db):
        db = shared_db
        db.connect()
        before = db.open_connection_count
        assert before >= 1

        def worker():
            db.connect()

        run_threads([worker, worker, worker])

        assert db.open_connection_count >= before + 3, (
            "每个线程都该留下自己的连接"
        )

    def test_close_closes_all_thread_connections(self, tmp_path):
        db = Database(str(tmp_path / "c.db"))
        db.initialize()
        db.connect()

        def worker():
            db.connect()

        run_threads([worker, worker])
        assert db.open_connection_count >= 3

        db.close()
        assert db.open_connection_count == 0, "close() 必须关掉所有线程的连接"


# ============================================================
# 三、🔒 任务五：20 线程共享同一个 Database 实例
# ============================================================
class TestSharedDatabaseInfrastructureConcurrency:
    N = 20

    def test_20_threads_independent_write_transactions(self, shared_db):
        """
        20 线程 / **同一个 Database 实例** / 各自独立的事务作用域。

        验证：全部提交成功、最终恰好 20 行、没有 cross-thread 状态污染。
        """
        db = shared_db
        barrier = threading.Barrier(self.N, timeout=30)
        errors: list = []
        ok: list = []

        def worker(i: int):
            try:
                barrier.wait()                 # 🔒 同步起跑（BEGIN 之前）
                with db.transaction() as conn:
                    assert db.transaction_depth == 1, (
                        f"线程 {i} 进入事务后深度应为 1，"
                        f"得到 {db.transaction_depth}"
                    )
                    conn.execute(
                        "INSERT INTO items (owner, value) VALUES (?,?)",
                        (f"t{i}", f"v{i}"),
                    )
                    assert db.transaction_depth == 1
                assert db.transaction_depth == 0
                ok.append(i)
            except Exception as exc:           # pragma: no cover
                errors.append(f"t{i}: {type(exc).__name__}: {exc}")

        threads = run_threads([lambda i=i: worker(i) for i in range(self.N)])

        assert not any(t.is_alive() for t in threads), "🔒 有线程卡住（死锁）"
        assert not errors, f"抛异常：{errors[:3]}"
        assert len(ok) == self.N

        with db.transaction(write=False) as conn:
            n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            owners = {r[0] for r in conn.execute(
                "SELECT owner FROM items").fetchall()}

        assert n == self.N, f"恰好 {self.N} 行，实际 {n}"
        assert owners == {f"t{i}" for i in range(self.N)}

    def test_rollback_in_half_the_threads_leaves_no_partial_state(self, shared_db):
        """
        🔒 **无 partial transaction state**：

            偶数线程提交、奇数线程回滚 →
            数据库里**只**应该有偶数线程的行，一条不多一条不少。
        """
        db = shared_db
        barrier = threading.Barrier(self.N, timeout=30)
        errors: list = []

        def worker(i: int):
            try:
                barrier.wait()
                try:
                    with db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO items (owner, value) VALUES (?,?)",
                            (f"t{i}", f"v{i}"),
                        )
                        if i % 2 == 1:
                            raise RuntimeError(f"线程 {i} 主动回滚")
                except RuntimeError:
                    pass                        # 预期
            except Exception as exc:           # pragma: no cover
                errors.append(f"t{i}: {type(exc).__name__}: {exc}")

        threads = run_threads([lambda i=i: worker(i) for i in range(self.N)])

        assert not any(t.is_alive() for t in threads), "有线程卡住"
        assert not errors, f"抛异常：{errors[:3]}"

        with db.transaction(write=False) as conn:
            owners = {r[0] for r in conn.execute(
                "SELECT owner FROM items").fetchall()}

        expected = {f"t{i}" for i in range(0, self.N, 2)}
        assert owners == expected, (
            f"只该留下提交过的偶数线程：缺 {expected - owners}，"
            f"多 {owners - expected}"
        )

    def test_committed_row_survives_another_threads_rollback(self, shared_db):
        """
        🔒 另一个线程**已经提交**的行，不会被本线程的回滚抹掉。

        ⚠️ **为什么这里必须串行，不能重叠**

            最自然的写法是「A 开写事务 → 慢 → B 也开写事务 → 提交 → A 回滚」。
            但那在 SQLite 上**做不到**：

                写事务是**单写者**。A 的 BEGIN IMMEDIATE 拿着写锁时，
                B 的 BEGIN IMMEDIATE 只会等到 busy_timeout 超时，
                然后报 `database is locked`。

            这是**基础设施限制**（SQLite 的设计），
            不是 Workspace 语义，也不是本修复要解决的问题。

            所以这条测试验证的是**不变量**：
            「提交是永久的，别人的回滚碰不到它」。
        """
        db = shared_db

        # B 先完整提交
        with db.transaction() as conn:
            conn.execute("INSERT INTO items (owner, value) VALUES ('B','y')")

        # A 再开一个事务、写入、然后回滚
        errors: list = []

        def a():
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO items (owner, value) VALUES ('A','x')")
                    raise RuntimeError("A 回滚")
            except RuntimeError:
                pass
            except Exception as exc:           # pragma: no cover
                errors.append(f"A: {type(exc).__name__}: {exc}")

        run_threads([a])
        assert not errors, f"抛异常：{errors}"

        with db.transaction(write=False) as conn:
            owners = {r[0] for r in conn.execute(
                "SELECT owner FROM items").fetchall()}

        assert "B" in owners, "🔒 B 已提交的行不能被 A 的回滚抹掉"
        assert "A" not in owners, "A 回滚后自己的行必须消失"

    def test_rollback_does_not_undo_another_threads_direct_write(self, shared_db):
        """
        🔒 **这是旧模型下会确定性出错的场景 —— 本修复的判别性测试。**

        【复现的时序】

            Thread A: BEGIN（读事务，WAL 下不阻塞写者）
            Thread B: db.execute("INSERT ...")      ← 直接写，不开事务
                      （新模型：B 有自己的连接、自己不在事务里 → 立刻提交）
                      （旧模型：B 用的是**同一个连接**，
                        而 `in_transaction()` 读的是**共享的** `_tx_depth`
                        → B 以为自己在 A 的事务里 → **不提交**）
            Thread A: 抛异常 → ROLLBACK

            旧模型的结果：**B 的写入被 A 一起回滚了** ——
            一个线程的回滚抹掉了另一个线程的写入。

            新模型：B 的写入在自己的连接上、自己提交 → 不受 A 影响。

        【为什么用读事务做 A】

            因为 A 若是写事务就会拿写锁，B 根本插不进来
            （SQLite 单写者）。用 `write=False` 时 A 只持读锁，
            WAL 模式下**读不阻塞写**，B 才能真正执行。
        """
        db = shared_db
        a_began = threading.Event()
        b_done = threading.Event()
        errors: list = []

        def a():
            try:
                with db.transaction(write=False):
                    a_began.set()
                    assert b_done.wait(timeout=15), "B 没能在 A 的事务期间完成"
                    raise RuntimeError("A 回滚")
            except RuntimeError:
                pass
            except Exception as exc:           # pragma: no cover
                errors.append(f"A: {type(exc).__name__}: {exc}")

        def b():
            try:
                assert a_began.wait(timeout=15), "A 没进事务"
                # 🔒 直接写：不开显式事务。
                #    新模型下 B 不在事务里（自己连接、自己深度=0）
                #    → execute() 立刻提交。
                db.execute(
                    "INSERT INTO items (owner, value) VALUES ('B','direct')")
                b_done.set()
            except Exception as exc:           # pragma: no cover
                errors.append(f"B: {type(exc).__name__}: {exc}")

        threads = run_threads([a, b])
        assert not any(t.is_alive() for t in threads), "有线程卡住"
        assert not errors, f"抛异常：{errors}"

        with db.transaction(write=False) as conn:
            owners = {r[0] for r in conn.execute(
                "SELECT owner FROM items").fetchall()}

        assert "B" in owners, (
            "🔒 B 的直接写入被 A 的回滚抹掉了 —— "
            "说明 B 误以为自己处在 A 的事务里（共享连接/共享深度的旧模型）"
        )

    def test_commit_in_one_thread_is_invisible_to_another_mid_transaction(self, shared_db):
        """
        🔒 **无 cross-thread transaction state**：

            A 在事务里写了但**没提交** → B 在**自己**的事务里看不到那一行。
        """
        db = shared_db
        a_wrote = threading.Event()
        a_may_rollback = threading.Event()
        b_saw: list = []
        errors: list = []

        def a():
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO items (owner, value) VALUES ('A','x')")
                    a_wrote.set()
                    assert a_may_rollback.wait(timeout=15)
                    raise RuntimeError("回滚，别留下 A")
            except RuntimeError:
                pass
            except Exception as exc:           # pragma: no cover
                errors.append(f"A: {exc}")

        def b():
            try:
                assert a_wrote.wait(timeout=15)
                with db.transaction(write=False) as conn:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM items WHERE owner='A'"
                    ).fetchone()[0]
                    b_saw.append(n)
                a_may_rollback.set()
            except Exception as exc:           # pragma: no cover
                errors.append(f"B: {exc}")

        threads = run_threads([a, b])
        assert not any(t.is_alive() for t in threads), "有线程卡住"
        assert not errors, f"抛异常：{errors}"
        assert b_saw == [0], (
            f"A 未提交的写入不该被 B 看见，B 看到 {b_saw}"
        )

    def test_concurrent_read_transactions_overlap(self, shared_db):
        """
        并发**读事务**可以真正同时进行（用 barrier 证明，不用计时）。

        20 个线程各自开读事务 + 在事务内汇合 ——
        只有「每个线程独立连接」才能全员到达。
        """
        db = shared_db
        n = self.N
        barrier = threading.Barrier(n, timeout=30)
        inside: list = []
        errors: list = []

        def worker(i: int):
            try:
                with db.transaction(write=False) as conn:
                    conn.execute("SELECT 1").fetchone()
                    barrier.wait()             # 🔒 事务内部汇合
                    inside.append(i)
            except Exception as exc:           # pragma: no cover
                errors.append(f"t{i}: {type(exc).__name__}: {exc}")

        threads = run_threads([lambda i=i: worker(i) for i in range(n)])

        assert not any(t.is_alive() for t in threads), "有线程卡住"
        assert not errors, (
            f"❌ 读事务之间发生了串行化（barrier 超时）：{errors[:3]}"
        )
        assert len(inside) == n, f"只有 {len(inside)}/{n} 个线程同时读"

    def test_repeated_runs_are_stable(self, shared_db):
        """
        重复跑 5 轮，验证不是偶然通过。

        每一轮写 N 行、校验行数，最后总数必须精确。
        """
        db = shared_db
        rounds = 5
        n = self.N

        for rnd in range(rounds):
            barrier = threading.Barrier(n, timeout=30)
            errors: list = []

            def worker(i: int, rnd=rnd):
                try:
                    barrier.wait()
                    with db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO items (owner, value) VALUES (?,?)",
                            (f"r{rnd}t{i}", f"v{i}"),
                        )
                except Exception as exc:       # pragma: no cover
                    errors.append(f"r{rnd}t{i}: {type(exc).__name__}: {exc}")

            threads = run_threads([lambda i=i: worker(i) for i in range(n)])
            assert not any(t.is_alive() for t in threads), f"第 {rnd} 轮卡住"
            assert not errors, f"第 {rnd} 轮异常：{errors[:3]}"

        with db.transaction(write=False) as conn:
            n_rows = conn.execute(
                "SELECT COUNT(*) FROM items WHERE owner LIKE 'r%'"
            ).fetchone()[0]

        assert n_rows == rounds * n, (
            f"{rounds} 轮 × {n} 线程应写 {rounds * n} 行，实际 {n_rows}"
        )


# ============================================================
# 四、内存库的固有约束（如实记录，不掩盖）
# ============================================================
class TestInMemoryIsSingleConnectionByNature:
    """
    ⚠️ 这一组**不测并发**，而是把「内存库为什么只能单连接」
       变成一条可执行的事实记录 —— 免得以后有人以为是实现偷懒。
    """

    def test_in_memory_shares_one_session(self):
        """
        `:memory:` 下所有线程共用**同一个** session
        （因为它们必须共用同一个连接 —— 见下一条测试）。
        """
        db = Database(":memory:")
        db.initialize()
        db.connect()
        main_session = db._peek_session()
        assert main_session is not None

        sessions: list = []

        def worker():
            db.connect()
            sessions.append(db._peek_session())

        run_threads([worker, worker])

        assert all(s is main_session for s in sessions), (
            "内存库必须共用同一个 session —— "
            "多开连接会得到**不同的空库**"
        )
        assert db.open_connection_count == 1
        db.close()

    def test_memory_connections_do_not_share_data(self, tmp_path):
        """
        🔒 **为什么内存库不能每线程一个连接**：直接证明。

        两个连接打同一个 `:memory:` —— 第二个看不到第一个建的表。
        """
        import sqlite3 as s3

        c1 = s3.connect(":memory:")
        c1.execute("CREATE TABLE t (x INTEGER)")
        c1.execute("INSERT INTO t VALUES (1)")

        c2 = s3.connect(":memory:")
        with pytest.raises(s3.OperationalError, match="no such table"):
            c2.execute("SELECT * FROM t").fetchall()

        c1.close()
        c2.close()

    def test_shared_cache_memory_is_not_viable_for_concurrent_writes(self):
        """
        🔒 **shared-cache 内存库在并发写下不成立** —— 实测数据。

        标准答案是用 `file:...?mode=memory&cache=shared` 让多连接共享内存库。
        但 shared-cache 用的是**表级锁**，冲突返回 `SQLITE_LOCKED`，
        而 `SQLITE_LOCKED` **不受 busy_timeout 影响** —— 等多久都不会好。

        这条测试把那个事实固化下来：如果将来有人想「顺手改成
        shared-cache」，它会立刻看到为什么不行。

        （用宽松阈值断言「大量失败」，避免变成 flaky。）
        """
        import uuid

        uri = f"file:memdb_{uuid.uuid4().hex}?mode=memory&cache=shared"
        setup = sqlite3.connect(uri, uri=True)
        setup.execute("CREATE TABLE t (x INTEGER)")
        setup.commit()

        errors: list = []
        written: list = []

        def worker(i: int):
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=1.0)
                for k in range(5):
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("INSERT INTO t VALUES (?)", (i * 100 + k,))
                    conn.commit()
                conn.close()
                written.append(i)
            except Exception as exc:
                errors.append(type(exc).__name__)

        run_threads([lambda i=i: worker(i) for i in range(20)])

        # 不断言「全失败」（那会 flaky），只断言「确实有大量失败」——
        # 足以证明它不能作为并发方案
        assert errors, (
            "shared-cache 内存库在这种并发写下**应该**报错 —— "
            "如果这条不再失败，说明 SQLite 行为变了，需要重新评估"
        )
        total = setup.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert total < 100, (
            f"共享缓存的写入应当大量丢失（期望远小于 100），实际 {total}"
        )
        setup.close()


def test_close_thread_session_releases_connection(tmp_path):
    db = Database(tmp_path / "release.db")
    db.initialize()
    try:
        assert db.fetchone("SELECT 1")[0] == 1
        assert db.open_connection_count >= 1

        db.close_thread_session()
        assert db.open_connection_count == 0

        # 关闭后当前线程再查询会重新建立连接。
        assert db.fetchone("SELECT 1")[0] == 1
    finally:
        db.close()
