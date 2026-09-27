"""
Test: 真实多进程 / 多连接并发（Workspace Phase 0 / 任务 A）
============================================================

【这个文件要证明什么】

    Step 4D.1 的并发设计**只在单进程单连接下验证过**。
    这个文件用**真正的 OS 进程**去验证它：

        Process A                      Process B
        Database(path)                 Database(path)
        独立 sqlite3 连接               独立 sqlite3 连接
              ↘                        ↙
                同一个数据库文件

【为什么必须是真进程，不能用线程】

    `threading.Thread` 共享同一个解释器、同一个 GIL、同一个连接对象。
    它验证不了：

        · 连接是不是真的各自独立
        · WAL 在**跨进程**下工作不工作
        · 条件 UPDATE 的原子性是不是数据库层面的（而不是 GIL 保护的）
        · fork 之后连接能不能复用

【一个环境事实】

    macOS / Windows 上 multiprocessing 默认用 **spawn**：
    子进程是全新的解释器，不可能继承父进程的连接。

    Linux（以及显式 `get_context("fork")`）用 **fork**：
    子进程会继承父进程的内存 —— 包括已经建立的连接。

    后者才是危险的那条路，所以本文件**显式测了 fork**。
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3

import pytest

from storage.database import Database
from tests import multiprocess_helpers as helpers


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def db_file(tmp_path):
    """一个建好表的真实数据库文件（子进程要能独立打开它）。"""
    path = tmp_path / "multiprocess.db"
    db = Database(str(path))
    db.initialize()
    db.close()
    return str(path)


def drain(queue) -> list:
    """把队列里的结果全取出来。"""
    items = []
    while not queue.empty():
        items.append(queue.get())
    return items


def run_processes(targets, timeout=60):
    """
    启动一批进程，等它们全部结束，返回结果列表。

    【为什么用 spawn 而不是 fork 作为默认】
        spawn 更接近「真实的另一个进程」——
        它连解释器都是新的。fork 会被单独测（见 TestForkSafety）。
    """
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    processes = [
        ctx.Process(target=target, args=(*args, out))
        for target, args in targets
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=timeout)
        assert not process.is_alive(), f"子进程 {process.pid} 超时未结束"

    results = drain(out)
    for process in processes:
        out.close()
    return results


# ============================================================
# 一、两个独立进程同时打开同一个数据库
# ============================================================
class TestMultipleProcessesOpenDatabase:
    def test_two_processes_read_and_write(self, db_file):
        """
        两个互不相识的进程都能打开、读、写、提交同一个库。

        【重点】每个进程在自己的进程里 `Database(path)` ——
                没有共享 Database 对象，也没有共享连接。
        """
        results = run_processes([
            (helpers.read_write_worker, (db_file, "A")),
            (helpers.read_write_worker, (db_file, "B")),
        ])

        # 【每个 worker 会汇报两条】opened + done
        #     所以 2 个进程 → 4 条消息。按 step 过滤。
        errors = [r for r in results if r.get("step") == "error"]
        assert not errors, errors

        opened = [r for r in results if r.get("step") == "opened"]
        done = [r for r in results if r.get("step") == "done"]
        assert len(opened) == 2 and len(done) == 2, results

        # 必须是两个**不同的**进程
        assert len({r["pid"] for r in results}) == 2

        # 两个都成功写入，而且各自都看到了对方的写入（before/after 相差 1）
        for item in done:
            assert item["after"] == item["before"] + 1, item
        # 数据库一开始是空的，所以两个进程看到的行数是 0 和 1
        # （谁先谁后不一定）—— 这说明它们确实在看同一份数据。
        assert {item["before"] for item in done} == {0, 1}, done

        # 两行都写进去了
        db = Database(db_file)
        rows = db.fetchall("SELECT task_id FROM tasks ORDER BY task_id")
        assert {row["task_id"] for row in rows} == {"T-A", "T-B"}
        db.close()

    def test_wal_mode_is_on_in_children(self, db_file):
        """WAL 在子进程的连接上也生效（不只是父进程）。"""
        results = run_processes([
            (helpers.read_write_worker, (db_file, "A")),
        ])

        assert results[0]["journal_mode"] == "wal", results

    def test_busy_timeout_is_set_in_children(self, db_file):
        """busy_timeout 在子进程连接上也显式设了（不是靠 Python 默认碰巧）。"""
        results = run_processes([
            (helpers.read_write_worker, (db_file, "A")),
        ])

        assert results[0]["busy_timeout"] == 5000, results


# ============================================================
# 二、同一 Message 的原子认领（真进程）
# ============================================================
class TestMessageClaimAcrossProcesses:
    def test_only_one_process_claims(self, db_file):
        """
        【这是任务 A 最重要的一条】

            两个进程在 barrier 上对齐，**同时**发出认领语句。

            数据库必须只让其中一个 rowcount = 1。
        """
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)

        results = run_two(ctx, barrier, [
            (helpers.claim_worker, (db_file, "msg_race", "worker-A")),
            (helpers.claim_worker, (db_file, "msg_race", "worker-B")),
        ])

        claimed = [r for r in results if r.get("ok")]
        busy = [r for r in results if r.get("status") == "BUSY"]

        assert len(claimed) == 1, f"必须只有一个成功认领：{results}"
        assert len(busy) == 1, f"另一个必须是 BUSY：{results}"
        assert claimed[0]["pid"] != busy[0]["pid"]

    def test_claim_is_persisted(self, db_file):
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        run_two(ctx, barrier, [
            (helpers.claim_worker, (db_file, "msg_race2", "worker-A")),
            (helpers.claim_worker, (db_file, "msg_race2", "worker-B")),
        ])

        db = Database(db_file)
        row = db.fetchone(
            "SELECT claim_id, processed_once FROM communication_messages "
            "WHERE message_id = 'msg_race2'"
        )
        assert row is not None
        assert row["claim_id"] in {"worker-A", "worker-B"}
        assert row["processed_once"] == 0, "只是认领，不等于处理完成"
        db.close()

    def test_no_duplicate_claims_in_database(self, db_file):
        """数据库里只有一行、只有一个 claim —— 没有重复记录。"""
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(4)
        run_two(ctx, barrier, [
            (helpers.claim_worker, (db_file, "msg_four", f"worker-{i}"))
            for i in range(4)
        ])

        db = Database(db_file)
        count = db.fetchone(
            "SELECT COUNT(*) AS n FROM communication_messages "
            "WHERE message_id = 'msg_four'"
        )["n"]
        assert count == 1, "同一条消息只能有一行"
        db.close()


# ============================================================
# 三、同一 Task 的 CAS 冲突（真进程）
# ============================================================
class TestTaskCasAcrossProcesses:
    @pytest.fixture
    def task_id(self, db_file):
        from core.money import Money
        from core.task import Task
        from core.task_store import TaskStore

        db = Database(db_file)
        store = TaskStore(db)
        task = Task.create(task_id="T-CAS", creator="agent_a",
                           description="CAS 竞争", budget=Money(100),
                           participants=["agent_a", "agent_b"])
        store.save(task)
        db.close()
        return "T-CAS"

    def test_one_success_one_conflict(self, db_file, task_id):
        """
        两个进程读到同一个 version，只有一个能写成功。

        【并且】最终 version 只 +1（不是 +2）——
               证明失败者**没有**偷偷写进去。
        """
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)

        results = run_two(ctx, barrier, [
            (helpers.cas_worker, (db_file, task_id, "A")),
            (helpers.cas_worker, (db_file, task_id, "B")),
        ])

        outcomes = sorted(r["outcome"] for r in results)
        assert outcomes == ["CONFLICT", "SUCCESS"], results

        winner = next(r for r in results if r["outcome"] == "SUCCESS")
        loser = next(r for r in results if r["outcome"] == "CONFLICT")

        assert loser["expected"] == winner["version_read"], (
            "失败者拿的版本应该就是成功者读到的那个版本"
        )
        assert loser["actual"] == winner["version_after"], (
            "失败者应该被告知数据库里现在是哪个版本"
        )

        db = Database(db_file)
        final = db.fetchone("SELECT version FROM tasks WHERE task_id = ?",
                            (task_id,))
        assert final["version"] == winner["version_after"], (
            f"最终 version 应该只 +1，实际 {final['version']}"
        )
        db.close()

    def test_loser_does_not_overwrite_winner(self, db_file, task_id):
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        results = run_two(ctx, barrier, [
            (helpers.cas_worker, (db_file, task_id, "A")),
            (helpers.cas_worker, (db_file, task_id, "B")),
        ])

        winner = next(r for r in results if r["outcome"] == "SUCCESS")
        loser = next(r for r in results if r["outcome"] == "CONFLICT")

        db = Database(db_file)
        note = db.fetchone("SELECT note FROM tasks WHERE task_id = ?",
                           (task_id,))["note"]
        assert note == f"写入方 {winner['worker']}", (
            f"数据库里应该是赢家的数据，实际 {note!r}"
        )
        assert note != f"写入方 {loser['worker']}", "输家不能覆盖赢家"
        db.close()


# ============================================================
# 四、不同 Task 可以并行
# ============================================================
class TestIndependentTasksRunInParallel:
    def test_unrelated_tasks_both_succeed(self, db_file):
        """
        【不要只证明「并发会失败」】

            两个进程更新**不同**的 Task，必须都成功。

            这验证并发保护没有把整个 Task 系统锁死 ——
            CAS 是行级的，不是全局的。
        """
        from core.money import Money
        from core.task import Task
        from core.task_store import TaskStore

        db = Database(db_file)
        store = TaskStore(db)
        for task_id in ("T-X", "T-Y"):
            store.save(Task.create(task_id=task_id, creator="agent_a",
                                   budget=Money(100),
                                   participants=["agent_a", "agent_b"]))
        db.close()

        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        results = run_two(ctx, barrier, [
            (helpers.independent_task_worker, (db_file, "T-X", "A")),
            (helpers.independent_task_worker, (db_file, "T-Y", "B")),
        ])

        assert all(r["outcome"] == "SUCCESS" for r in results), results

        db = Database(db_file)
        for task_id, worker in (("T-X", "A"), ("T-Y", "B")):
            note = db.fetchone("SELECT note FROM tasks WHERE task_id = ?",
                               (task_id,))["note"]
            assert note == f"{worker} 更新了 {task_id}"
        db.close()


# ============================================================
# 五、压力：多进程反复 CAS
# ============================================================
class TestStress:
    def test_no_corruption_no_impossible_version(self, db_file):
        """
        4 个进程各做 15 轮 CAS。

        验证：

            · 没有人拿到 impossible version（超过总成功次数）
            · 没有静默覆盖（最终 note 是某一次真实写入）
            · 数据库完好（integrity_check 通过）
        """
        from core.money import Money
        from core.task import Task
        from core.task_store import TaskStore

        db = Database(db_file)
        store = TaskStore(db)
        store.save(Task.create(task_id="T-STRESS", creator="agent_a",
                               budget=Money(100),
                               participants=["agent_a", "agent_b"]))
        db.close()

        workers = 4
        rounds = 15
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(workers)
        results = run_two(ctx, barrier, [
            (helpers.stress_worker, (db_file, "T-STRESS", f"W{i}", rounds))
            for i in range(workers)
        ])

        assert all(r["outcome"] == "DONE" for r in results), results

        total_successes = sum(r["successes"] for r in results)
        assert total_successes == workers * rounds
        assert sum(r["conflicts"] for r in results) >= 0

        db = Database(db_file)
        row = db.fetchone("SELECT version, note FROM tasks WHERE task_id = ?",
                          ("T-STRESS",))
        # version 从 1 开始（首次 save），之后每次成功 +1
        assert row["version"] == 1 + total_successes, (
            f"version 应该正好等于总成功次数 +1："
            f"{row['version']} vs {1 + total_successes}"
        )
        assert row["note"].startswith("W"), row["note"]

        integrity = db.fetchone("PRAGMA integrity_check")[0]
        assert integrity == "ok", f"数据库损坏：{integrity}"
        db.close()

    def test_no_silent_overwrite_under_stress(self, db_file):
        """压力之后 note 一定是某个真实写入的值，不是半截数据。"""
        from core.money import Money
        from core.task import Task
        from core.task_store import TaskStore

        db = Database(db_file)
        store = TaskStore(db)
        store.save(Task.create(task_id="T-S2", creator="agent_a",
                               budget=Money(100),
                               participants=["agent_a", "agent_b"]))
        db.close()

        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(3)
        run_two(ctx, barrier, [
            (helpers.stress_worker, (db_file, "T-S2", f"W{i}", 10))
            for i in range(3)
        ])

        db = Database(db_file)
        note = db.fetchone("SELECT note FROM tasks WHERE task_id = ?",
                           ("T-S2",))["note"]
        # 格式必须是 W<数字>#<数字>，不能是半截或被截断
        assert note.startswith("W") and "#" in note, note
        left, right = note.split("#")
        assert left[1:].isdigit() and right.isdigit(), note
        db.close()


# ============================================================
# 六、多进程事务回滚
# ============================================================
class TestTransactionRollbackAcrossProcesses:
    def test_rollback_leaves_no_half_success(self, db_file):
        """
        真进程里制造失败 → 状态和事件都要回滚。
        """
        from core.money import Money
        from core.task import Task, TaskStatus
        from core.task_store import TaskStore

        db = Database(db_file)
        store = TaskStore(db)
        task = Task.create(task_id="T-RB", creator="agent_a",
                           description="回滚", budget=Money(100),
                           participants=["agent_a", "agent_b"])
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        task.start()
        store.save(task)
        db.close()

        results = run_processes([
            (helpers.rollback_worker, (db_file, "T-RB", "W1")),
        ])

        assert results[0]["outcome"] == "DONE", results
        assert results[0]["status"] == TaskStatus.IN_PROGRESS
        assert results[0]["executions"] == 0

        # 父进程再看一眼，确认数据库里也是干净的
        db = Database(db_file)
        store = TaskStore(db)
        assert store.get("T-RB").status == TaskStatus.IN_PROGRESS
        assert store.count_executions("T-RB") == 0
        db.close()


# ============================================================
# 七、fork 安全性（这条是 Workspace Phase 0 新修的）
# ============================================================
class TestForkSafety:
    """
    【为什么单独测 fork】

        spawn 下子进程是全新解释器，**不可能**继承连接 ——
        所以 spawn 的测试证明不了「继承的连接到子进程会被丢弃」。

        fork 才会继承。Linux 上 fork 是**默认**行为，
        所以这条路径必须单独验证。

        （macOS 上要显式 get_context("fork") 才走这条路。）
    """

    def test_fork_child_gets_its_own_connection(self, db_file):
        if not hasattr(os, "fork"):
            pytest.skip("这个平台没有 fork")

        ctx = mp.get_context("fork")
        out = ctx.Queue()

        # 父进程先建立连接
        parent_db = Database(db_file)
        parent_db.initialize()
        parent_db.connect().execute("SELECT 1")
        parent_pid = os.getpid()
        assert parent_db.owner_pid == parent_pid

        process = ctx.Process(target=_fork_probe, args=(parent_db, out))
        process.start()
        process.join(timeout=30)
        assert not process.is_alive()

        results = drain(out)
        out.close()
        parent_db.close()

        assert len(results) == 1
        probe = results[0]

        assert probe["child_pid"] != parent_pid
        assert probe["owner_pid"] == probe["child_pid"], (
            "子进程必须使用**自己**建立的连接，而不是继承父进程的"
        )
        assert probe["read_ok"] is True, probe

    def test_fork_child_can_write(self, db_file):
        if not hasattr(os, "fork"):
            pytest.skip("这个平台没有 fork")

        ctx = mp.get_context("fork")
        out = ctx.Queue()
        parent_db = Database(db_file)
        parent_db.initialize()
        parent_db.connect()

        process = ctx.Process(target=_fork_write_probe, args=(parent_db, out))
        process.start()
        process.join(timeout=30)
        results = drain(out)
        out.close()
        parent_db.close()

        assert results[0]["write_ok"] is True, results

        db = Database(db_file)
        count = db.fetchone("SELECT COUNT(*) AS n FROM tasks WHERE task_id = 'T-FORK'")["n"]
        assert count == 1
        db.close()


# ---- fork 探针（必须在模块级，spawn/fork 都要能 import 到）----
def _fork_probe(db, out):
    import os

    probe = {"child_pid": os.getpid()}
    try:
        db.connect()
        probe["owner_pid"] = db.owner_pid
        probe["read_ok"] = db.fetchone("SELECT COUNT(*) AS n FROM tasks") is not None
    except Exception as exc:  # noqa: BLE001
        probe["error"] = f"{type(exc).__name__}: {exc}"
    out.put(probe)


def _fork_write_probe(db, out):
    probe = {}
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, creator, status, created_at, "
                "updated_at, version) VALUES ('T-FORK','agent_a','CREATED',"
                "'2026-01-01','2026-01-01',0)"
            )
        probe["write_ok"] = True
    except Exception as exc:  # noqa: BLE001
        probe["write_ok"] = False
        probe["error"] = f"{type(exc).__name__}: {exc}"
    out.put(probe)


# ============================================================
# 工具：跑 N 个带 barrier 的进程
# ============================================================
def run_two(ctx, barrier, targets, timeout=60):
    """
    启动一批共享同一个 barrier 的进程，收集结果。

    【为什么 targets 是 (函数, 位置参数)】
        barrier 由这里统一注入（放在参数最后），
        免得每个测试都自己拼一遍。
    """
    out = ctx.Queue()
    processes = [
        ctx.Process(target=target, args=(*args, barrier, out))
        for target, args in targets
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=timeout)
        assert not process.is_alive(), f"子进程 {process.pid} 超时未结束"

    results = drain(out)
    out.close()
    return results
