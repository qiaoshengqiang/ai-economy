"""
Test: TaskStore —— Task 持久化（Step 4A）
============================================================

验证 Task 能可靠地存进 SQLite 并读回来。

【重点验证三件事】

    1. 往返无损         存进去再读出来，字段一个都不少
    2. 两个「不能合并」的概念在数据库层面也是分开的
         task_id / current_trace_id
         budget / agreed_price
    3. 报价历史 append-only 且能完整重建
"""

from __future__ import annotations

import pytest

from core.money import Money
from core.task import OfferType, Task, TaskStatus
from core.task_store import TaskStore

from storage.database import create_memory_database
from storage.models import TABLE_TASK_OFFERS, TABLE_TASKS


@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


@pytest.fixture
def saved_task(store):
    task = Task.create(
        creator="agent_a",
        description="写一个 CSV 分析程序",
        budget=Money(100),
        workspace_id="WS-001",
        acceptance={"type": "tests"},
    )
    store.save(task)
    return task


# ============================================================
# 1. 基本存取
# ============================================================
class TestBasicPersistence:
    def test_save_and_get(self, store, saved_task):
        loaded = store.get(saved_task.task_id)

        assert loaded is not None
        assert loaded.task_id == saved_task.task_id
        assert loaded.creator == "agent_a"
        assert loaded.description == "写一个 CSV 分析程序"

    def test_get_unknown_returns_none(self, store):
        assert store.get("task_不存在") is None

    def test_require_unknown_raises(self, store):
        with pytest.raises(KeyError):
            store.require("task_不存在")

    def test_exists(self, store, saved_task):
        assert store.exists(saved_task.task_id) is True
        assert store.exists("task_不存在") is False

    def test_count(self, store, saved_task):
        assert store.count() == 1

    def test_save_twice_does_not_duplicate(self, store, saved_task):
        """UPSERT —— 同一个任务反复保存也只有一行。"""
        store.save(saved_task)
        store.save(saved_task)

        assert store.count() == 1

    def test_save_requires_task(self, store):
        with pytest.raises(TypeError):
            store.save("不是任务")

    def test_status_persisted(self, store, saved_task):
        saved_task.offer()
        saved_task.negotiate()
        store.save(saved_task)

        assert store.get(saved_task.task_id).status == TaskStatus.NEGOTIATING

    def test_status_survives_multiple_saves(self, store, saved_task):
        """每次状态变化都保存，读回来是最新的。"""
        for action in (saved_task.offer, saved_task.negotiate):
            action()
            store.save(saved_task)

        assert store.get(saved_task.task_id).status == TaskStatus.NEGOTIATING


# ============================================================
# 2. 两个「不能合并」的概念
# ============================================================
class TestSeparatedConcepts:
    def test_task_id_and_trace_id_stored_separately(self, store, saved_task):
        """
        数据库里也是两列 —— 不是合成一个字段。
        """
        loaded = store.get(saved_task.task_id)

        assert loaded.task_id == saved_task.task_id
        assert loaded.current_trace_id == saved_task.current_trace_id
        assert loaded.task_id != loaded.current_trace_id

    def test_trace_can_change_without_task_id_changing(self, store, saved_task):
        """
        【核心场景】任务失败重跑 → 换 trace，但 task_id 不变。

        这正是两个字段必须分开的原因：
        如果合并，重跑就会变成「另一个任务」，历史无法归并。
        """
        original_task_id = saved_task.task_id

        store.update_trace(original_task_id, "trc_second_attempt")

        loaded = store.get(original_task_id)
        assert loaded.task_id == original_task_id        # 身份不变
        assert loaded.current_trace_id == "trc_second_attempt"

    def test_budget_and_agreed_price_stored_separately(self, store, saved_task):
        """数据库里是四列（各两列），不是两个数字。"""
        columns = store.db.columns_of(TABLE_TASKS)

        for column in ("budget_amount", "budget_currency",
                       "agreed_price_amount", "agreed_price_currency"):
            assert column in columns, f"缺少列 {column}"

    def test_both_amounts_survive_roundtrip(self, store, saved_task):
        saved_task.offer()
        saved_task.accept(Money(25), contractor="agent_b")
        store.save(saved_task)

        loaded = store.get(saved_task.task_id)

        assert loaded.budget == Money(100)
        assert loaded.agreed_price == Money(25)
        assert loaded.budget != loaded.agreed_price

    def test_currency_persisted(self, store, saved_task):
        loaded = store.get(saved_task.task_id)

        assert loaded.budget.currency == "AIC"

    def test_none_budget_stays_none(self, store):
        task = Task.create(creator="a")
        store.save(task)

        assert store.get(task.task_id).budget is None
        assert store.get(task.task_id).agreed_price is None

    def test_amounts_are_queryable_with_sql(self, store, saved_task):
        """
        金额拆成独立列的价值：可以直接用 SQL 聚合。

        （如果存成 JSON 就做不到。）
        """
        saved_task.offer()
        saved_task.accept(Money(25))
        store.save(saved_task)

        row = store.db.fetchone(
            f"SELECT SUM(agreed_price_amount) AS total FROM {TABLE_TASKS}"
        )

        assert row["total"] == 25


# ============================================================
# 3. 报价历史
# ============================================================
class TestOfferHistoryPersistence:
    def test_offer_saved_with_task(self, store, saved_task):
        saved_task.offer()
        saved_task.record_offer(OfferType.TASK_OFFER, Money(100))
        store.save(saved_task)

        offers = store.offers_for(saved_task.task_id)
        assert len(offers) == 1
        assert offers[0].amount == Money(100)

    def test_full_chain_roundtrip(self, store, saved_task):
        """
        完整报价链存进去再读出来，顺序和内容都要对。
        """
        saved_task.offer()
        saved_task.record_offer(OfferType.TASK_OFFER, Money(100))
        saved_task.negotiate()
        saved_task.record_offer(OfferType.COUNTER_OFFER, Money(60))
        saved_task.record_offer(OfferType.COUNTER_OFFER, Money(25))
        saved_task.accept(Money(25))
        saved_task.record_offer(OfferType.ACCEPT, Money(25))
        store.save(saved_task)

        offers = store.offers_for(saved_task.task_id)

        assert [o.offer_type for o in offers] == [
            OfferType.TASK_OFFER,
            OfferType.COUNTER_OFFER,
            OfferType.COUNTER_OFFER,
            OfferType.ACCEPT,
        ]
        assert [o.amount.amount for o in offers] == [100, 60, 25, 25]

    def test_offers_loaded_into_task_object(self, store, saved_task):
        """get() 出来的 Task 对象自带报价历史。"""
        saved_task.record_offer(OfferType.TASK_OFFER, Money(100))
        store.save(saved_task)

        assert len(store.get(saved_task.task_id).offers) == 1

    def test_record_offer_standalone(self, store, saved_task):
        """
        单独记一条报价，不碰任务主体。
        """
        before_status = store.get(saved_task.task_id).status

        offer = store.record_offer(
            saved_task.task_id, OfferType.COUNTER_OFFER, Money(60)
        )

        assert offer.offer_type == OfferType.COUNTER_OFFER
        assert store.get(saved_task.task_id).status == before_status

    def test_record_offer_requires_existing_task(self, store):
        with pytest.raises(KeyError):
            store.record_offer("task_不存在", OfferType.TASK_OFFER, Money(10))

    def test_offers_are_append_only(self, store, saved_task):
        """重复保存不会把报价记录搞乱。"""
        saved_task.record_offer(OfferType.TASK_OFFER, Money(100))
        store.save(saved_task)
        store.save(saved_task)
        store.save(saved_task)

        assert len(store.offers_for(saved_task.task_id)) == 1

    def test_two_tasks_do_not_share_offers(self, store, saved_task):
        other = Task.create(creator="agent_c", budget=Money(50))
        store.save(other)
        other.record_offer(OfferType.TASK_OFFER, Money(50))
        store.save(other)

        assert len(store.offers_for(saved_task.task_id)) == 0
        assert len(store.offers_for(other.task_id)) == 1

    def test_offer_message_id_persisted(self, store, saved_task):
        """message_id 可以关联通信消息（也可以为空）。"""
        saved_task.record_offer(
            OfferType.TASK_OFFER, Money(100), message_id="msg_abc"
        )
        store.save(saved_task)

        offers = store.offers_for(saved_task.task_id)
        assert offers[0].message_id == "msg_abc"

    def test_count_offers(self, store, saved_task):
        saved_task.record_offer(OfferType.TASK_OFFER, Money(100))
        store.save(saved_task)

        assert store.count_offers() == 1


# ============================================================
# 4. 查询
# ============================================================
class TestQueries:
    def test_list_all(self, store, saved_task):
        store.save(Task.create(creator="agent_c"))

        assert len(store.list_tasks()) == 2

    def test_filter_by_status(self, store, saved_task):
        saved_task.offer()
        store.save(saved_task)
        store.save(Task.create(creator="agent_c"))

        offered = store.list_tasks(status=TaskStatus.OFFERED)
        assert len(offered) == 1
        assert offered[0].status == TaskStatus.OFFERED

    def test_filter_by_creator(self, store, saved_task):
        store.save(Task.create(creator="agent_c"))

        assert len(store.list_tasks(creator="agent_a")) == 1
        assert len(store.list_tasks(creator="agent_c")) == 1
        assert len(store.list_tasks(creator="agent_z")) == 0

    def test_filter_by_contractor(self, store, saved_task):
        saved_task.offer()
        saved_task.accept(Money(25), contractor="agent_b")
        store.save(saved_task)

        assert len(store.list_tasks(contractor="agent_b")) == 1

    def test_filter_by_workspace(self, store, saved_task):
        """
        按 workspace_id 筛选 —— 注意这**只是查标识**，
        不代表能访问那个 Workspace。
        """
        store.save(Task.create(creator="agent_c", workspace_id="WS-002"))

        assert len(store.list_tasks(workspace_id="WS-001")) == 1
        assert len(store.list_tasks(workspace_id="WS-002")) == 1

    def test_limit(self, store):
        for index in range(5):
            store.save(Task.create(creator=f"agent_{index}"))

        assert len(store.list_tasks(limit=2)) == 2

    def test_count_by_status(self, store, saved_task):
        saved_task.offer()
        store.save(saved_task)

        assert store.count(status=TaskStatus.OFFERED) == 1
        assert store.count(status=TaskStatus.CREATED) == 0

    def test_stats(self, store, saved_task):
        stats = store.stats()

        assert stats["tasks"] == 1
        assert stats["workspaces"] == 1


# ============================================================
# 5. 完整生命周期往返
# ============================================================
class TestLifecycleRoundtrip:
    def test_every_state_survives_persistence(self, store):
        """
        逐个状态走一遍，每次都存一次、读一次，
        确认数据库里存的状态和内存里一致。
        """
        task = Task.create(creator="agent_a", budget=Money(100))
        store.save(task)
        assert store.get(task.task_id).status == TaskStatus.CREATED

        for action, expected in (
            (task.offer, TaskStatus.OFFERED),
            (task.negotiate, TaskStatus.NEGOTIATING),
            (lambda: task.accept(Money(25)), TaskStatus.ACCEPTED),
            (task.start, TaskStatus.IN_PROGRESS),
            (task.submit, TaskStatus.SUBMITTED),
            (lambda: task.verify(passed=True), TaskStatus.VERIFIED),
            (task.complete, TaskStatus.COMPLETED),
        ):
            action()
            store.save(task)
            assert store.get(task.task_id).status == expected

    def test_rework_roundtrip(self, store):
        """返工（SUBMITTED → IN_PROGRESS）也能正确持久化。"""
        task = Task.create(creator="a", budget=Money(100))
        store.save(task)

        task.offer()
        task.accept(Money(25))
        task.start()
        task.submit()
        task.verify(passed=False)
        store.save(task)

        assert store.get(task.task_id).status == TaskStatus.IN_PROGRESS

    def test_loaded_task_can_continue_the_machine(self, store, saved_task):
        """
        从数据库读回来的任务，状态机还能继续走。
        """
        saved_task.offer()
        store.save(saved_task)

        loaded = store.get(saved_task.task_id)
        loaded.negotiate()
        loaded.accept(Money(25))
        store.save(loaded)

        assert store.get(loaded.task_id).status == TaskStatus.ACCEPTED

    def test_loaded_task_can_change_trace(self, store, saved_task):
        """读回来之后换 trace，再存回去。"""
        loaded = store.get(saved_task.task_id)
        loaded.current_trace_id = "trc_retry"
        store.save(loaded)

        assert store.get(saved_task.task_id).current_trace_id == "trc_retry"


# ============================================================
# 6. 边界：TaskStore 不碰钱
# ============================================================
class TestStoreDoesNotTouchMoney:
    def test_saving_task_creates_no_ledger_entries(self, store, economy,
                                                   funded_agents):
        """
        TaskStore 用的虽然是同一个数据库，但**不产生任何账目**。
        """
        from core.task_store import TaskStore as _TaskStore

        eco_store = _TaskStore(economy.db)
        before = economy.ledger.count()

        task = Task.create(creator="agent_a", budget=Money(100))
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        task.record_offer(OfferType.ACCEPT, Money(25))
        eco_store.save(task)

        assert economy.ledger.count() == before
        assert economy.audit()["balanced"] is True

    def test_task_tables_are_separate_from_ledger(self, store):
        """tasks 表和账本表是独立的。"""
        tables = store.db.table_names()

        assert TABLE_TASKS in tables
        assert TABLE_TASK_OFFERS in tables
        assert "ledger_transactions" in tables
        assert "wallets" in tables

    def test_no_workspace_permission_tables_created(self, store):
        """
        Step 4A 不建 Workspace 权限相关的表。

        建空表会让人误以为系统已经有那个能力。
        """
        tables = set(store.db.table_names())

        for forbidden in (
            "workspace_members", "workspace_permissions",
            "workspace_files", "workspace_capabilities",
            "task_contracts", "task_escrows",
        ):
            assert forbidden not in tables, (
                f"Step 4A 不该创建 {forbidden} 表 —— 那属于后续步骤"
            )


# ============================================================
# 7. 杂项
# ============================================================
class TestMisc:
    def test_repr(self, store, saved_task):
        assert "TaskStore" in repr(store)

    def test_describe(self, store, saved_task):
        info = store.describe()

        assert info["tasks"] == 1
        assert info["offers"] == 0

    def test_store_is_bound_to_db(self, store):
        assert store.db is not None

    def test_participants_persisted(self, store):
        task = Task.create(
            creator="agent_a", participants=["agent_a", "agent_b"]
        )
        store.save(task)

        assert store.get(task.task_id).participants == ["agent_a", "agent_b"]

    def test_acceptance_persisted(self, store, saved_task):
        loaded = store.get(saved_task.task_id)

        assert loaded.acceptance == {"type": "tests"}

    def test_expires_at_persisted(self, store):
        task = Task.create(creator="a", expires_at="2026-09-26T10:00:00+00:00")
        store.save(task)

        assert store.get(task.task_id).expires_at == "2026-09-26T10:00:00+00:00"

    def test_note_persisted(self, store):
        task = Task.create(creator="a", note="内部备注")
        store.save(task)

        assert store.get(task.task_id).note == "内部备注"
