"""
Test: 事务原子性（Step 4D.1 / 目标 C）
============================================================

【这个文件验证什么】

    4D 的 TaskExecutionService 是这样写的：

        self.store.record_execution(record)   # 事务 1：写执行事件
        self.store.save(task)                 # 事务 2：改任务状态

    两个事务之间崩溃 → **半成功**：

        ✅ 事件写了，❌ 状态没改
        或者反过来

    而且这种不一致**不会报错**，只会让数据慢慢变质。

【为什么 4D 没做】

    因为做不到 —— Database.transaction() 不可重入：

        with db.transaction():
            with db.transaction():     # ❌ cannot start a transaction
                ...

    而 TaskStore 的每个方法都自己开事务。
    所以「把两件事放进同一个事务」在 4D 是结构性不可行的。

    4D.1 先让事务可重入，这里才可能成立。

【本文件的断言】

    成功：状态改了 **且** 事件写了
    失败：状态没改 **且** 事件没写
    没有中间状态。
"""

from __future__ import annotations

import pytest

from core.money import Money
from core.task import Task, TaskStatus
from core.task_execution import TaskResultRef, TaskVerification
from core.task_store import TaskStore
from storage.database import Database

from communication import Message, MessageType

from services import TaskExecutionService


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


@pytest.fixture
def execution(store) -> TaskExecutionService:
    return TaskExecutionService(store)


@pytest.fixture
def policies():
    """议价策略（成交路径的测试要用）。"""
    from core.decision_policy import RuleBasedDecisionPolicy

    return {
        "agent_a": RuleBasedDecisionPolicy(accept_price=50),
        "agent_b": RuleBasedDecisionPolicy(accept_price=50),
    }


def make_task(store, task_id="T-1", started: bool = True) -> Task:
    """
    造一个已成交的任务。

    started=True  → IN_PROGRESS（默认，SUBMIT/VERIFY 的测试用）
    started=False → ACCEPTED（START 的测试用）
    """
    task = Task.create(
        task_id=task_id, creator="agent_a", description="写个分析程序",
        budget=Money(100), participants=["agent_a", "agent_b"],
    )
    task.offer()
    task.accept(Money(25), contractor="agent_b")
    if started:
        task.start()
    store.save(task)
    return task


def message(message_type=MessageType.SUBMIT, task_id="T-1"):
    return Message(from_agent="agent_b", to_agent="agent_a",
                   message_type=message_type, payload={"task_id": task_id})


# ============================================================
# C-0. 事务可重入（整个目标 C 的前提）
# ============================================================
class TestReentrantTransactions:
    """
    【为什么先测这个】

        如果事务不可重入，下面所有的原子性测试都写不出来 ——
        会直接抛 "cannot start a transaction within a transaction"。
        所以它是目标 C 的**前提**，值得单独钉住。
    """

    def test_nested_transaction_does_not_raise(self, db):
        with db.transaction() as outer:
            outer.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
            with db.transaction() as inner:
                inner.execute("INSERT INTO t VALUES (1)")

    def test_depth_is_tracked(self, db):
        assert db.transaction_depth == 0
        with db.transaction():
            assert db.transaction_depth == 1
            with db.transaction():
                assert db.transaction_depth == 2
            assert db.transaction_depth == 1
        assert db.transaction_depth == 0

    def test_in_transaction_helper(self, db):
        assert db.in_transaction() is False
        with db.transaction():
            assert db.in_transaction() is True
        assert db.in_transaction() is False

    def test_inner_failure_rolls_back_everything(self, db):
        with db.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")

        with pytest.raises(RuntimeError):
            with db.transaction() as outer:
                outer.execute("INSERT INTO t VALUES (1)")
                with db.transaction() as inner:
                    inner.execute("INSERT INTO t VALUES (2)")
                    raise RuntimeError("内层失败")

        rows = [r[0] for r in db.connect().execute("SELECT a FROM t")]
        assert rows == [], "内层失败必须让整个事务回滚"

    def test_outer_failure_rolls_back_inner_writes(self, db):
        with db.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")

        with pytest.raises(RuntimeError):
            with db.transaction() as outer:
                outer.execute("INSERT INTO t VALUES (1)")
                with db.transaction() as inner:
                    inner.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("外层失败")

        rows = [r[0] for r in db.connect().execute("SELECT a FROM t")]
        assert rows == []

    def test_success_commits_both_levels(self, db):
        with db.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")

        with db.transaction() as outer:
            outer.execute("INSERT INTO t VALUES (1)")
            with db.transaction() as inner:
                inner.execute("INSERT INTO t VALUES (2)")

        rows = sorted(r[0] for r in db.connect().execute("SELECT a FROM t"))
        assert rows == [1, 2]

    def test_depth_resets_after_failure(self, db):
        """失败之后深度必须归零 —— 否则后续所有操作都会卡在「事务中」。"""
        with pytest.raises(RuntimeError):
            with db.transaction():
                raise RuntimeError("boom")

        assert db.transaction_depth == 0
        assert db.in_transaction() is False

        # 而且后续事务仍然能用
        with db.transaction() as conn:
            conn.execute("SELECT 1")

    def test_store_methods_join_outer_transaction(self, db, store):
        """
        TaskStore 的方法自己要开事务，
        但它们必须能**加入**外层事务，而不是另起一个。
        """
        task = make_task(store)

        with db.transaction():
            assert db.transaction_depth == 1
            task.note = "在外层事务里保存"
            store.save(task)
            # save() 内部也调用了 transaction()，但深度应该回到 1
            assert db.transaction_depth == 1, (
                "TaskStore.save() 应该加入外层事务，而不是嵌套开一个新的"
            )


# ============================================================
# C-1. 成功路径
# ============================================================
class TestSuccessPath:
    def test_state_and_event_both_land(self, execution, store):
        task = make_task(store)

        execution.apply_submit(task, TaskResultRef(result_ref="v1"),
                               incoming=message())

        assert store.get("T-1").status == TaskStatus.SUBMITTED
        assert store.count_executions("T-1") == 1

    def test_same_transaction(self, execution, store):
        """
        状态变化和事件记录必须在**同一个事务**里发生。

        【怎么断言「在同一个事务里」】
            用一个探针记录事务深度：
            如果两件事都在事务里发生，深度就应该是 1（不是 0，也不是 2）。
        """
        # 【先建任务，再装探针】
        #     make_task() 自己也会调用 store.save()，
        #     如果探针先装上，就会把「建任务」那次（事务外）也算进来 ——
        #     测出来的就不是我们要看的那次了。
        task = make_task(store)

        observed = []
        original_save = store.save
        original_record = store.record_execution

        def probe_save(t, **kwargs):
            observed.append(("save", store.db.transaction_depth))
            return original_save(t, **kwargs)

        def probe_record(record):
            observed.append(("record", store.db.transaction_depth))
            return original_record(record)

        store.save = probe_save
        store.record_execution = probe_record
        try:
            execution.apply_submit(task, TaskResultRef(result_ref="v1"),
                                   incoming=message())
        finally:
            store.save = original_save
            store.record_execution = original_record

        assert observed, "探针没被调用"
        for name, depth in observed:
            assert depth >= 1, f"{name} 不在任何事务里"

    def test_every_4d_event_is_atomic(self, execution, store):
        """START / SUBMIT / VERIFY / COMPLETE 每一个都要原子。"""
        # START（需要一个 ACCEPTED 的任务 —— make_task 默认已经开工了）
        task = make_task(store, started=False)
        execution.apply_start(task, incoming=message(MessageType.START))
        assert store.count_executions("T-1") == 1

        # SUBMIT（同一个任务，从 IN_PROGRESS 开始 —— 上面 START 之后就是）
        execution.apply_submit(task, TaskResultRef(result_ref="v1"),
                               incoming=message(MessageType.SUBMIT))
        assert store.count_executions("T-1") == 2

        # VERIFY
        execution.apply_verify(task, TaskVerification(passed=True, feedback="过"),
                               incoming=message(MessageType.VERIFY))
        assert store.count_executions("T-1") == 3

        # COMPLETE
        execution.apply_complete(task, incoming=message(MessageType.COMPLETE))
        assert store.count_executions("T-1") == 4


# ============================================================
# C-2. 失败路径：事件写入失败
# ============================================================
class TestEventWriteFailure:
    def test_state_rolls_back(self, execution, store, monkeypatch):
        task = make_task(store)
        assert store.get("T-1").status == TaskStatus.IN_PROGRESS

        def boom(record):
            raise RuntimeError("写执行事件失败")

        monkeypatch.setattr(store, "record_execution", boom)

        with pytest.raises(RuntimeError):
            execution.apply_submit(task, TaskResultRef(result_ref="v1"),
                                   incoming=message())

        assert store.get("T-1").status == TaskStatus.IN_PROGRESS, (
            "事件写不进去，状态就不能变 —— 否则就是半成功"
        )

    def test_no_event_left_behind(self, execution, store, monkeypatch):
        # 【只建一次任务】
        #     同一个 task_id 建两次，第二次会拿着 version=0 撞上
        #     数据库里的 version=1 —— 那是**并发冲突**，
        #     会把测试引到另一个失败原因上去，测不到事务回滚。
        task = make_task(store)

        monkeypatch.setattr(
            store, "record_execution",
            lambda record: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        with pytest.raises(RuntimeError):
            execution.apply_submit(task, TaskResultRef(result_ref="v1"),
                                   incoming=message())

        assert store.count_executions("T-1") == 0


# ============================================================
# C-3. 失败路径：状态保存失败
# ============================================================
class TestTaskSaveFailure:
    def test_event_rolls_back(self, execution, store, monkeypatch):
        """
        状态存不进去 → 事件也不能留下。

        （这是另一个方向的半成功：事件存在但任务没变。）
        """
        make_task(store)

        def boom(task, **kwargs):
            raise RuntimeError("保存任务失败")

        monkeypatch.setattr(store, "save", boom)

        with pytest.raises(RuntimeError):
            execution.apply_submit(make_task(store),
                                   TaskResultRef(result_ref="v1"),
                                   incoming=message())

        assert store.count_executions("T-1") == 0, (
            "状态没保存成功，事件就不该留下"
        )

    def test_status_unchanged(self, execution, store, monkeypatch):
        task = make_task(store)

        monkeypatch.setattr(
            store, "save",
            lambda t, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        with pytest.raises(RuntimeError):
            execution.apply_submit(task, TaskResultRef(result_ref="v1"),
                                   incoming=message())

        assert store.get("T-1").status == TaskStatus.IN_PROGRESS


# ============================================================
# C-4. 失败路径：并发冲突也必须整体回滚
# ============================================================
class TestConflictRollsBack:
    def test_conflict_leaves_no_event(self, execution, store):
        """
        乐观锁冲突 → 整个操作回滚 → 事件也不该留下。

        这条把目标 B 和目标 C 连起来了：
        冲突检测在 save() 里，而 save() 和 record_execution()
        在同一个事务里，所以冲突会连带回滚事件。
        """
        from core.task_store import ConcurrentTaskModification

        task = make_task(store)
        stale = store.get("T-1")

        # 别人先推进了
        fresh = store.get("T-1")
        fresh.submit()
        store.save(fresh)

        with pytest.raises(ConcurrentTaskModification):
            execution.apply_submit(stale, TaskResultRef(result_ref="v1"),
                                   incoming=message())

        assert store.count_executions("T-1") == 0, (
            "冲突回滚之后不该留下执行事件"
        )
        assert store.get("T-1").status == TaskStatus.SUBMITTED, (
            "数据库里应该保持别人写入的状态"
        )


# ============================================================
# C-5. 成交路径也要原子
# ============================================================
class TestAcceptIsAtomic:
    def test_task_and_agreement_together(self, db, store, policies):
        from services import TaskNegotiationService

        negotiation = TaskNegotiationService(store, policies)
        task = Task.create(task_id="T-A", creator="agent_a",
                           description="d", budget=Money(100),
                           participants=["agent_a", "agent_b"])
        task.offer()
        store.save(task)

        negotiation.apply_accept(
            store.get("T-A"), Money(25),
            incoming=Message(from_agent="agent_b", to_agent="agent_a",
                             message_type=MessageType.COUNTER_OFFER,
                             payload={"task_id": "T-A"}),
            accepted_offer_message_id="msg_x",
            accepted_offer_type="COUNTER_OFFER",
            accept_author="agent_a",
        )

        assert store.get("T-A").status == TaskStatus.ACCEPTED
        assert store.count_agreements() == 1

    def test_agreement_failure_rolls_back_task(self, db, store, policies,
                                               monkeypatch):
        """凭证写不进去 → 任务不能变成 ACCEPTED。"""
        from services import TaskNegotiationService

        negotiation = TaskNegotiationService(store, policies)
        task = Task.create(task_id="T-A", creator="agent_a",
                           description="d", budget=Money(100),
                           participants=["agent_a", "agent_b"])
        task.offer()
        store.save(task)

        def boom(agreement):
            raise RuntimeError("凭证写不了")

        monkeypatch.setattr(store, "record_agreement", boom)

        with pytest.raises(RuntimeError):
            negotiation.apply_accept(
                store.get("T-A"), Money(25),
                incoming=Message(from_agent="agent_b", to_agent="agent_a",
                                 message_type=MessageType.COUNTER_OFFER,
                                 payload={"task_id": "T-A"}),
                accepted_offer_message_id="msg_x",
                accepted_offer_type="COUNTER_OFFER",
                accept_author="agent_a",
            )

        assert store.get("T-A").status != TaskStatus.ACCEPTED, (
            "没有凭证就不算成交 —— 不能出现「任务成交了但找不到凭证」"
        )
        assert store.count_agreements() == 0


# ============================================================
# C-6. 完整 4D 回归（含返工）
# ============================================================
class TestFull4DRegression:
    def test_complete_lifecycle_after_hardening(self, db, economy,
                                                funded_agents):
        """
        Step 4D.1 不能改变 4D 的行为。

        完整跑一遍（含返工），确认：
            状态正确 / 事件正确 / attempts 正确 / rework 正确
            Wallet 不变 / Ledger 不变
        """
        store = TaskStore(economy.db)
        execution = TaskExecutionService(store)

        ledger_before = economy.ledger.count()
        a_before = economy.agent("agent_a").balance
        b_before = economy.agent("agent_b").balance

        task = Task.create(task_id="T-REG", creator="agent_a",
                           description="d", budget=Money(100),
                           participants=["agent_a", "agent_b"])
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        store.save(task)

        def m(sender, recipient, message_type):
            return Message(from_agent=sender, to_agent=recipient,
                           message_type=message_type,
                           payload={"task_id": "T-REG"})

        execution.apply_start(store.get("T-REG"),
                              incoming=m("agent_b", "agent_a", MessageType.START))
        assert store.get("T-REG").status == TaskStatus.IN_PROGRESS

        execution.apply_submit(store.get("T-REG"), TaskResultRef(result_ref="v1"),
                               incoming=m("agent_b", "agent_a", MessageType.SUBMIT))
        assert store.get("T-REG").status == TaskStatus.SUBMITTED

        execution.apply_verify(store.get("T-REG"),
                               TaskVerification(passed=False, feedback="要改"),
                               incoming=m("agent_a", "agent_b", MessageType.VERIFY))
        assert store.get("T-REG").status == TaskStatus.IN_PROGRESS

        execution.apply_submit(store.get("T-REG"), TaskResultRef(result_ref="v2"),
                               incoming=m("agent_b", "agent_a", MessageType.SUBMIT))
        execution.apply_verify(store.get("T-REG"),
                               TaskVerification(passed=True, feedback="过"),
                               incoming=m("agent_a", "agent_b", MessageType.VERIFY))
        assert store.get("T-REG").status == TaskStatus.VERIFIED

        execution.apply_complete(store.get("T-REG"),
                                 incoming=m("agent_a", "agent_b",
                                            MessageType.COMPLETE))
        assert store.get("T-REG").status == TaskStatus.COMPLETED

        # ---- 事件与计数 ----
        events = [r.event_type for r in store.executions_for("T-REG")]
        assert events == ["START", "SUBMIT", "VERIFY", "SUBMIT",
                          "VERIFY", "COMPLETE"]
        assert store.attempt_count("T-REG") == 2
        assert execution.history_summary("T-REG")["rework_count"] == 1

        # ---- 经济后果：零 ----
        assert economy.ledger.count() == ledger_before
        assert economy.agent("agent_a").balance == a_before
        assert economy.agent("agent_b").balance == b_before
        assert economy.audit()["balanced"] is True
