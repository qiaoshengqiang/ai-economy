"""
Test: Task 乐观锁（Step 4D.1 / 目标 B）
============================================================

【这个文件验证什么】

    4D.1 之前：

        TaskStore.save() 是**无条件 UPSERT** —— 谁后写谁覆盖。

            A 读 task（v=5）→ 改成 SUBMITTED
            B 读 task（v=5）→ 改成 VERIFIED
            B 先写成功；A 后写 → 把 VERIFIED 覆盖回 SUBMITTED

        两边都以为自己成功了。**不报错**，只让状态悄悄变错。

    4D.1 之后：

        save() 是 compare-and-set：

            UPDATE tasks SET ... WHERE task_id=? AND version=?

        影响行数为 0 → 抛 ConcurrentTaskModification。

【最重要的几条断言】

    · 版本号读出来、写回去、正常递增
    · stale write **必须失败**，且绝不静默覆盖
    · 冲突不自动重试、不自动重放
    · 冲突不属于 Task Domain State（没有 CONFLICT 状态）
"""

from __future__ import annotations

import pytest

from core.money import Money
from core.task import Task, TaskStatus
from core.task_store import ConcurrentTaskModification, TaskStore


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


def new_task(store, task_id="T-1") -> Task:
    task = Task.create(
        task_id=task_id,
        creator="agent_a",
        description="写个分析程序",
        budget=Money(100),
        participants=["agent_a", "agent_b"],
    )
    store.save(task)
    return task


# ============================================================
# B-1. 版本号的基本行为
# ============================================================
class TestVersionBasics:
    def test_new_task_starts_at_zero(self):
        task = Task.create(task_id="T", creator="agent_a", budget=Money(1))

        assert task.version == 0, "还没保存过的任务是 0"

    def test_first_save_advances_to_one(self, store):
        task = new_task(store)

        assert task.version == 1

    def test_save_increments(self, store):
        task = new_task(store)
        task.offer()
        store.save(task)

        assert task.version == 2

    def test_version_is_read_back(self, store):
        """
        【这一条是实现时踩到的坑】

            如果 get() 不读 version，读回来的永远是 0，
            那么任何一次 get() → 改 → save() 都会被误判成冲突 ——
            包括完全没有并发的情况。
        """
        task = new_task(store)
        task.offer()
        store.save(task)

        reread = store.get("T-1")

        assert reread.version == task.version

    def test_version_survives_roundtrip(self, store):
        task = new_task(store)
        task.offer()
        store.save(task)

        restored = Task.from_dict(store.get("T-1").to_dict())

        assert restored.version == task.version

    def test_read_modify_write_without_concurrency_works(self, store):
        """没有并发时，标准的「读-改-写」必须正常工作。"""
        new_task(store)

        for _ in range(5):
            task = store.get("T-1")
            task.note = f"第 {task.version} 次"
            store.save(task)

        assert store.get("T-1").version == 6

    def test_version_must_be_int(self):
        with pytest.raises(Exception):
            Task(task_id="T", creator="a", version="5")

    def test_version_must_not_be_negative(self):
        with pytest.raises(Exception):
            Task(task_id="T", creator="a", version=-1)

    def test_version_rejects_bool(self):
        """True 是 int 的子类，但显然不是合法的版本号。"""
        with pytest.raises(Exception):
            Task(task_id="T", creator="a", version=True)


# ============================================================
# B-2. 冲突检测（本文件的核心）
# ============================================================
class TestConflictDetection:
    def test_stale_save_is_rejected(self, store):
        new_task(store)

        writer_a = store.get("T-1")
        writer_b = store.get("T-1")
        assert writer_a.version == writer_b.version

        # A 先写
        writer_a.note = "A 的修改"
        store.save(writer_a)

        # B 拿着过期版本写 —— 必须失败
        writer_b.note = "B 的修改"
        with pytest.raises(ConcurrentTaskModification):
            store.save(writer_b)

    def test_conflict_reports_both_versions(self, store):
        new_task(store)
        writer_a = store.get("T-1")
        writer_b = store.get("T-1")

        writer_a.note = "A"; store.save(writer_a)

        with pytest.raises(ConcurrentTaskModification) as info:
            store.save(writer_b)

        assert info.value.task_id == "T-1"
        assert info.value.expected_version == writer_b.version
        assert info.value.actual_version == writer_a.version
        assert info.value.actual_version != info.value.expected_version

    def test_no_silent_overwrite(self, store):
        """
        【这是本阶段最重要的一条断言】

            旧写入**不能**覆盖新写入。
        """
        new_task(store)

        writer_a = store.get("T-1")
        writer_b = store.get("T-1")

        writer_a.status  # 保持 A 手上的状态
        writer_a.note = "A 的修改"
        store.save(writer_a)

        writer_b.note = "B 的修改"
        with pytest.raises(ConcurrentTaskModification):
            store.save(writer_b)

        final = store.get("T-1")
        assert final.note == "A 的修改", "A 的修改必须完好无损"
        assert final.note != "B 的修改", "B 不能覆盖 A"

    def test_state_not_corrupted_after_conflict(self, store):
        """冲突之后任务状态仍然一致。"""
        new_task(store)
        writer_a = store.get("T-1")
        writer_b = store.get("T-1")

        writer_a.offer()
        writer_a.accept(Money(25), contractor="agent_b")
        store.save(writer_a)

        with pytest.raises(ConcurrentTaskModification):
            store.save(writer_b)

        final = store.get("T-1")
        assert final.status == TaskStatus.ACCEPTED
        assert final.agreed_price == Money(25)

    def test_failed_save_writes_nothing(self, store):
        """冲突时**一个字段都不能写进去**。"""
        task = new_task(store)
        task.offer()
        store.save(task)

        stale = store.get("T-1")
        winner = store.get("T-1")
        winner.note = "winner"
        store.save(winner)

        stale.note = "loser"
        stale.contractor = "agent_x"
        stale.description = "被改掉了"
        with pytest.raises(ConcurrentTaskModification):
            store.save(stale)

        final = store.get("T-1")
        assert final.note == "winner"
        assert final.contractor is None
        assert final.description == "写个分析程序"

    def test_exception_message_is_actionable(self, store):
        new_task(store)
        a = store.get("T-1"); b = store.get("T-1")
        a.note = "x"; store.save(a)

        with pytest.raises(ConcurrentTaskModification) as info:
            store.save(b)

        text = str(info.value)
        assert "并发" in text
        assert "重新读取" in text or "过期" in text

    def test_conflict_is_not_a_task_state(self):
        """
        【并发冲突不属于 Task Domain State】

            不新增 CONFLICT / PROCESSING / RETRY 状态。
        """
        for forbidden in ("CONFLICT", "CONFLICTED", "PROCESSING",
                          "LOCKED", "STALE", "RETRY"):
            assert not TaskStatus.is_known(forbidden), (
                f"TaskStatus 里出现了 {forbidden} —— "
                f"并发冲突是持久化层的问题，不是任务生命周期的一个阶段"
            )

    def test_conflict_is_not_swallowed(self, store):
        """它必须是一个**异常**，不能变成一个返回值被忽略。"""
        assert issubclass(ConcurrentTaskModification, Exception)

    def test_no_automatic_retry(self, store):
        """
        【本阶段明确不自动重试 / 不自动重放】

            save() 冲突时直接抛异常 ——
            不会偷偷重新读取、不会自动重放状态变化。

            理由：状态变化有语义，自动重放可能产生
            重复状态变化、重复消息，或者替 Agent 做了决定。

            【怎么测「没有自动重试」】
                冲突之后，数据库里的版本**不应该**被悄悄改动。
                如果 save() 内部重读了再写，版本就会继续涨。
        """
        new_task(store)
        a = store.get("T-1"); b = store.get("T-1")
        a.note = "A"; store.save(a)

        version_before = store.get("T-1").version
        for _ in range(3):
            with pytest.raises(ConcurrentTaskModification):
                store.save(b)
        version_after = store.get("T-1").version

        assert version_after == version_before, (
            "冲突不该产生任何写入（也就不会推进版本号）"
        )

    def test_retry_after_reread_succeeds(self, store):
        """
        正确的恢复方式是调用方**显式重新读取** —— 而不是自动重放。
        """
        new_task(store)
        a = store.get("T-1"); b = store.get("T-1")
        a.note = "A"; store.save(a)

        with pytest.raises(ConcurrentTaskModification):
            store.save(b)

        # 显式重新读取
        fresh = store.get("T-1")
        fresh.note = "B（基于最新版本）"
        store.save(fresh)

        assert store.get("T-1").note == "B（基于最新版本）"


# ============================================================
# B-3. force（显式覆盖）
# ============================================================
class TestForceOverwrite:
    def test_force_allows_overwrite(self, store):
        new_task(store)
        a = store.get("T-1"); b = store.get("T-1")
        a.note = "A"; store.save(a)

        b.note = "B（显式覆盖）"
        store.save(b, force=True)

        assert store.get("T-1").note == "B（显式覆盖）"

    def test_force_is_opt_in(self, store):
        """必须显式传 —— 默认行为永远是「冲突就报错」。"""
        import inspect

        signature = inspect.signature(TaskStore.save)
        assert signature.parameters["force"].default is False
        assert signature.parameters["force"].kind is inspect.Parameter.KEYWORD_ONLY


# ============================================================
# B-4. 新任务 / 老任务
# ============================================================
class TestInsertVsUpdate:
    def test_insert_then_update(self, store):
        task = new_task(store)
        assert task.version == 1

        task.offer()
        store.save(task)
        assert task.version == 2

        assert store.count() == 1, "不该产生第二行"

    def test_two_different_tasks_do_not_conflict(self, store):
        a = new_task(store, "T-A")
        b = new_task(store, "T-B")
        a.note = "a"; store.save(a)
        b.note = "b"; store.save(b)

        assert store.get("T-A").note == "a"
        assert store.get("T-B").note == "b"

    def test_same_object_can_be_saved_repeatedly(self, store):
        """保存成功后 version 会推进，所以同一个对象可以连续保存。"""
        task = new_task(store)

        for _ in range(4):
            task.note = f"v{task.version}"
            store.save(task)

        assert store.get("T-1").version == 5

    def test_rejected_type(self, store):
        with pytest.raises(TypeError):
            store.save("这不是 Task")


# ============================================================
# B-5. 与 4D 执行路径的配合
# ============================================================
class TestExecutionPathUsesLocking:
    def test_state_transitions_advance_version(self, store):
        """每次执行事件都会推进版本号 —— 说明真的走了 compare-and-set。"""
        from communication import Message, MessageType
        from services import TaskExecutionService

        task = new_task(store)
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        store.save(task)

        before = store.get("T-1").version
        execution = TaskExecutionService(store)
        message = Message(from_agent="agent_b", to_agent="agent_a",
                          message_type=MessageType.START,
                          payload={"task_id": "T-1"})

        execution.apply_start(store.get("T-1"), incoming=message)

        assert store.get("T-1").version > before

    def test_stale_task_object_is_rejected_in_4d_flow(self, store):
        """
        4D 流程里拿着过期 Task 去推进 → 必须被拦住。

        （真实场景：两个 worker 同时想推进同一个任务。）
        """
        from communication import Message, MessageType
        from services import TaskExecutionService

        task = new_task(store)
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        store.save(task)

        stale = store.get("T-1")
        execution = TaskExecutionService(store)

        # 别人先推进了
        fresh = store.get("T-1")
        fresh.start()
        store.save(fresh)

        # 我拿着过期的那份想提交。
        #
        # 【只 start()，不再 submit()】
        #     apply_submit() 自己会做 IN_PROGRESS → SUBMITTED 的转移。
        #     这里手工 submit() 的话就变成「从 SUBMITTED 转 SUBMITTED」，
        #     会被状态机拦住 —— 而拦住的原因和并发无关，测试就跑偏了。
        stale.start()
        message = Message(from_agent="agent_b", to_agent="agent_a",
                          message_type=MessageType.SUBMIT,
                          payload={"task_id": "T-1"})

        from core.task_execution import TaskResultRef
        with pytest.raises(ConcurrentTaskModification):
            execution.apply_submit(stale, TaskResultRef(result_ref="v1"),
                                   incoming=message)
