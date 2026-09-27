"""
多进程测试的子进程入口
============================================================

【为什么必须是一个独立的模块文件】

    两个原因，都会踩到：

    1. spawn 模式会**重新 import 主模块**

        macOS / Windows 上 multiprocessing 默认用 spawn：
        子进程启动时重新 import 一遍入口模块。

        如果 worker 函数定义在测试文件里，子进程就会重新执行
        整个测试文件 —— 包括 pytest 的收集逻辑，很容易出怪问题。

        放在独立模块里最干净：子进程 import 它，只拿到 worker。

    2. 从 stdin 跑不了 spawn

        直接 `python - <<'EOF'` 时，spawn 找不到入口文件：

            FileNotFoundError: .../ '<stdin>'

        因为 spawn 需要 data['init_main_from_path'] 指向一个真实文件。

【本模块的约定】

    每个 worker：

        · 在**自己的进程里**新建 Database（不接收父进程的对象）
        · 通过 multiprocessing.Queue 汇报结果
        · 用 barrier / Event 让多个进程尽量同时动手（制造真竞争）

    【为什么 worker 收的是 path 而不是 Database 对象】

        父进程的 Database 里有一个 SQLite 连接。
        把它传给子进程有两重问题：
            · spawn：根本 pick 不了（socket 不可序列化）
            · fork：能传，但那是**父进程的连接** —— 不能复用

        （Database 现在会检测 PID 变化并重建连接，
          但测试应该从「各自建自己的」开始，那才是最干净的用法。）
"""

from __future__ import annotations

import time
from typing import Any, Dict, List


# ============================================================
# 工具
# ============================================================
def open_database(path: str):
    """
    在**当前进程**里建一个全新的 Database + 全新连接。

    这是所有 worker 的第一步 —— 确保「每个进程一个连接」。
    """
    from storage.database import Database

    db = Database(path)
    db.initialize()
    return db


def record(out, **payload: Any) -> None:
    """把一条结果放进队列（统一带上 pid，方便判断是谁干的）。"""
    import os

    out.put({**payload, "pid": os.getpid()})


# ============================================================
# 一、同时打开数据库并读写
# ============================================================
def read_write_worker(path: str, name: str, out) -> None:
    """
    独立进程：打开数据库 → 读 → 写 → 提交。

    验证：两个互不相识的进程都能用同一个数据库文件。
    """
    try:
        db = open_database(path)
        record(out, worker=name, step="opened",
               journal_mode=db.pragmas().get("journal_mode"),
               busy_timeout=db.pragmas().get("busy_timeout"))

        before = db.fetchone("SELECT COUNT(*) AS n FROM tasks")["n"]

        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (task_id, creator, status, created_at, updated_at, version)
                VALUES (?, 'agent_a', 'CREATED', '2026-01-01', '2026-01-01', 0)
                """,
                (f"T-{name}",),
            )

        after = db.fetchone("SELECT COUNT(*) AS n FROM tasks")["n"]
        record(out, worker=name, step="done", before=before, after=after)
        db.close()
    except Exception as exc:  # noqa: BLE001
        record(out, worker=name, step="error",
               error=f"{type(exc).__name__}: {exc}")


# ============================================================
# 二、同一 Message 的原子认领
# ============================================================
def claim_worker(path: str, message_id: str, owner: str,
                 barrier, out, lease_seconds: int = 300) -> None:
    """
    独立进程：尝试认领同一条消息。两个进程用 barrier 对齐起跑。

    【为什么需要 barrier】
        不加它的话，进程 A 可能已经跑完了 B 才启动 ——
        那测的是「先后」，不是「同时」。
        barrier 让两者在**同一瞬间**发出那条 UPDATE。
    """
    from communication.message import Message, MessageType
    from communication.recorder import CommunicationRecorder

    try:
        db = open_database(path)
        recorder = CommunicationRecorder(db, claim_lease_seconds=lease_seconds)
        message = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.SUBMIT,
            payload={"task_id": "T-CLAIM"},
            message_id=message_id,
        )

        barrier.wait(timeout=30)          # 对齐起跑
        claim = recorder.claim_message(message, owner)

        record(out, worker=owner, status=claim.status, ok=claim.claimed,
               claimed_by=claim.owner, reclaimed=claim.reclaimed)
        db.close()
    except Exception as exc:  # noqa: BLE001
        record(out, worker=owner, status="ERROR",
               error=f"{type(exc).__name__}: {exc}")


# ============================================================
# 三、同一 Task 的 CAS 竞争
# ============================================================
def cas_worker(path: str, task_id: str, name: str, barrier, out,
               hold: float = 0.0) -> None:
    """
    独立进程：读取 Task → （可选等待）→ save。

    两个进程读到同一个 version，只有一个能写成功。
    """
    from core.task_store import ConcurrentTaskModification, TaskStore

    try:
        db = open_database(path)
        store = TaskStore(db)

        # 【先读，再对齐起跑】—— 这样两者读到的是同一个 version
        task = store.get(task_id)
        version_read = task.version
        task.note = f"写入方 {name}"

        barrier.wait(timeout=30)
        if hold:
            time.sleep(hold)

        try:
            store.save(task)
            record(out, worker=name, outcome="SUCCESS",
                   version_read=version_read, version_after=task.version)
        except ConcurrentTaskModification as exc:
            record(out, worker=name, outcome="CONFLICT",
                   version_read=version_read,
                   expected=exc.expected_version, actual=exc.actual_version)

        db.close()
    except Exception as exc:  # noqa: BLE001
        record(out, worker=name, outcome="ERROR",
               error=f"{type(exc).__name__}: {exc}")


# ============================================================
# 四、不同 Task 并行
# ============================================================
def independent_task_worker(path: str, task_id: str, name: str,
                            barrier, out) -> None:
    """独立进程：更新**属于自己的**那个 Task。应该不互相阻塞。"""
    from core.task_store import TaskStore

    try:
        db = open_database(path)
        store = TaskStore(db)
        task = store.get(task_id)
        task.note = f"{name} 更新了 {task_id}"

        barrier.wait(timeout=30)
        store.save(task)
        record(out, worker=name, task_id=task_id, outcome="SUCCESS",
               version=task.version)
        db.close()
    except Exception as exc:  # noqa: BLE001
        record(out, worker=name, task_id=task_id, outcome="ERROR",
               error=f"{type(exc).__name__}: {exc}")


# ============================================================
# 五、压力：多进程反复 CAS
# ============================================================
def stress_worker(path: str, task_id: str, name: str, rounds: int,
                  barrier, out) -> None:
    """
    独立进程：反复「读 → 改 → CAS 写」，冲突就重新读再试。

    这是**正确**的冲突处理方式：显式重新读取，而不是自动重放。

    最后汇报：成功次数、冲突次数、见过的最大 version。
    """
    from core.task_store import ConcurrentTaskModification, TaskStore

    successes = 0
    conflicts = 0
    max_version = 0

    try:
        db = open_database(path)
        store = TaskStore(db)
        barrier.wait(timeout=30)

        for index in range(rounds):
            for _ in range(50):        # 有界重试，避免测试挂死
                task = store.get(task_id)
                max_version = max(max_version, task.version)
                task.note = f"{name}#{index}"
                try:
                    store.save(task)
                    successes += 1
                    max_version = max(max_version, task.version)
                    break
                except ConcurrentTaskModification:
                    conflicts += 1
            else:
                record(out, worker=name, outcome="GAVE_UP", round=index)
                break

        record(out, worker=name, outcome="DONE",
               successes=successes, conflicts=conflicts,
               max_version=max_version)
        db.close()
    except Exception as exc:  # noqa: BLE001
        record(out, worker=name, outcome="ERROR",
               error=f"{type(exc).__name__}: {exc}")


# ============================================================
# 六、崩溃后的租约恢复
# ============================================================
def crash_after_claim_worker(path: str, message_id: str, owner: str,
                             out, lease_seconds: int = 300) -> None:
    """
    独立进程：认领一条消息，然后**直接退出，不释放认领**。

    这模拟 worker 被 kill -9：

        · 没有异常处理会跑
        · 没有 finally 会跑
        · claim_id 留在数据库里

    租约机制必须在**不依赖任何清理代码**的前提下恢复它。
    """
    from communication.message import Message, MessageType
    from communication.recorder import CommunicationRecorder

    db = open_database(path)
    recorder = CommunicationRecorder(db, claim_lease_seconds=lease_seconds)
    message = Message(
        from_agent="agent_a", to_agent="agent_b",
        message_type=MessageType.SUBMIT,
        payload={"task_id": "T-CRASH"},
        message_id=message_id,
    )
    claim = recorder.claim_message(message, owner)
    record(out, worker=owner, status=claim.status, ok=claim.claimed)

    # 【刻意不做任何清理】
    #     不 mark_processed、不 mark_failed、不 close()
    #     直接让进程结束 —— 模拟崩溃。
    #     （db.close() 都不调用，连连接都不优雅关闭。）
    return


# ============================================================
# 七、事务回滚（多进程）
# ============================================================
def rollback_worker(path: str, task_id: str, name: str, out) -> None:
    """
    独立进程：在事务里写执行事件，然后**抛异常**。

    验证：真进程里的事务回滚不会留下半成功状态。
    """
    from communication.message import Message, MessageType
    from core.task_execution import TaskResultRef
    from core.task_store import TaskStore
    from services import TaskExecutionService

    try:
        db = open_database(path)
        store = TaskStore(db)
        execution = TaskExecutionService(store)

        task = store.get(task_id)
        message = Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.SUBMIT,
            payload={"task_id": task_id},
        )

        original = store.record_execution

        def boom(record):
            raise RuntimeError("模拟子进程写事件失败")

        store.record_execution = boom
        try:
            execution.apply_submit(task, TaskResultRef(result_ref="v1"),
                                   incoming=message)
        except RuntimeError:
            pass
        finally:
            store.record_execution = original

        record(out, worker=name, outcome="DONE",
               status=store.get(task_id).status,
               executions=store.count_executions(task_id))
        db.close()
    except Exception as exc:  # noqa: BLE001
        record(out, worker=name, outcome="ERROR",
               error=f"{type(exc).__name__}: {exc}")
