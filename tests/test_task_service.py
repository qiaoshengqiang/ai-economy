"""
Test: TaskService —— 消息进入业务领域的适配层（Step 4B）
============================================================

【这个文件验证什么】

    Step 4B 第一次把三个系统缝在一起：

        Task  +  Capability  +  Communication

    而 TaskService 就是那个缝合点。

【两种职责分开测】

    委派侧  candidates_for()          —— 找候选，不选人
    接收侧  handle_offer() 等          —— 把消息翻译成领域操作

【最重要的三条断言】

    1. 处理成功之后任务状态是 OFFERED，**不是** ACCEPTED
       （能力匹配 ≠ 自动接单）
    2. 报价历史里记下了 message_id，任务对齐了 message 的 trace_id
       （链路可追踪）
    3. 整个过程不产生任何账目
       （Capability ≠ Economy）
"""

from __future__ import annotations

import pytest

from core.agent_directory import LocalAgentDirectory
from core.money import Money
from core.task import OfferType, Task, TaskStatus
from core.task_store import TaskStore

from communication import Message, MessageType

from services import HandlerResult, TaskService


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def directory() -> LocalAgentDirectory:
    d = LocalAgentDirectory()
    d.register_agent("agent_a", ["task_decomposition", "outsourcing"])
    d.register_agent("agent_b", ["python_coding", "data_analysis", "csv_processing"])
    return d


@pytest.fixture
def service(db, directory) -> TaskService:
    return TaskService(store=TaskStore(db), directory=directory)


@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


def make_offer(sender="agent_a", recipient="agent_b", **overrides) -> Message:
    """构造一条合规的 TASK_OFFER。"""
    payload = {
        "task_id": "TASK-001",
        "workspace_id": "WS-001",
        "description": "写一个 CSV 分析程序",
        "budget": {"amount": 100, "currency": "AIC"},
        "required_capabilities": ["python_coding", "data_analysis"],
        "acceptance": {"type": "tests"},
    }
    payload.update(overrides.pop("payload", {}))
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.TASK_OFFER,
        trace_id=overrides.pop("trace_id", "trc_offer_1"),
        payload=payload,
        **overrides,
    )


def make_counter(price=25, task_id="TASK-001", sender="agent_b",
                 recipient="agent_a", **overrides) -> Message:
    payload = {"task_id": task_id, "price": {"amount": price, "currency": "AIC"}}
    payload.update(overrides.pop("payload", {}))
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.COUNTER_OFFER,
        payload=payload,
        **overrides,
    )


# ============================================================
# 1. 委派侧：找候选（不选人）
# ============================================================
class TestCandidateDiscovery:
    def test_finds_capable_agent(self, service):
        task = Task.create(
            creator="agent_a", budget=Money(100),
            required_capabilities=["python_coding", "data_analysis"],
        )

        candidates = service.candidates_for(task)

        assert [e.agent_id for e in candidates] == ["agent_b"]

    def test_returns_all_matches_not_one(self, service, directory):
        """
        【关键】返回**全部**候选，不是「一个最佳人选」。

        选谁是发起方 Agent 的决定。
        """
        directory.register_agent("agent_c", ["python_coding", "data_analysis"])

        task = Task.create(
            creator="agent_a", required_capabilities=["python_coding"],
        )
        candidates = service.candidates_for(task)

        assert [e.agent_id for e in candidates] == ["agent_b", "agent_c"]

    def test_no_match(self, service):
        task = Task.create(creator="agent_a", required_capabilities=["rust"])
        assert service.candidates_for(task) == []

    def test_task_without_requirements_matches_all(self, service):
        task = Task.create(creator="agent_a")
        assert len(service.candidates_for(task)) == 2

    def test_requires_directory(self, db):
        """没配目录时给出明确的错误，而不是静默返回空。"""
        service = TaskService(store=TaskStore(db), directory=None)
        task = Task.create(creator="a", required_capabilities=["python"])

        with pytest.raises(RuntimeError, match="AgentDirectory"):
            service.candidates_for(task)

    def test_can_be_done_by(self, service):
        task = Task.create(
            creator="agent_a",
            required_capabilities=["python_coding", "data_analysis"],
        )

        assert service.can_be_done_by(task, "agent_b") is True
        assert service.can_be_done_by(task, "agent_a") is False
        assert service.can_be_done_by(task, "agent_不存在") is False

    def test_explain_candidates(self, service):
        """解释匹配过程 —— 但不排序、不推荐。"""
        task = Task.create(
            creator="agent_a",
            required_capabilities=["python_coding", "data_analysis"],
        )

        info = service.explain_candidates(task)

        assert info["required"] == ["data_analysis", "python_coding"]
        assert [c["agent_id"] for c in info["candidates"]] == ["agent_b"]
        assert [e["agent_id"] for e in info["excluded"]] == ["agent_a"]
        assert info["excluded"][0]["missing"] == ["data_analysis", "python_coding"]

    def test_discovery_does_not_modify_task(self, service):
        task = Task.create(
            creator="agent_a", required_capabilities=["python_coding"],
        )
        before = task.status

        service.candidates_for(task)

        assert task.status == before


# ============================================================
# 2. 接收侧：TASK_OFFER
# ============================================================
class TestHandleOffer:
    def test_creates_local_task(self, service, store):
        result = service.handle_offer(make_offer())

        assert result.ok is True
        assert result.action == "created"
        assert result.task_id == "TASK-001"
        assert store.exists("TASK-001")

    def test_task_starts_as_offered_not_accepted(self, service, store):
        """
        【Step 4B 最关键的一条断言】

        能力匹配到 B，不代表 B 接单。
        Handler 处理完之后状态是 OFFERED —— 「收到了邀约」。
        接不接由 Agent 自己判断。
        """
        service.handle_offer(make_offer())

        task = store.get("TASK-001")
        assert task.status == TaskStatus.OFFERED
        assert task.status != TaskStatus.ACCEPTED

    def test_handler_returns_no_reply_on_success(self, service):
        """
        成功时不抢答。

        「收到」不等于「接受」—— 回一条确认容易被误读成业务答复。
        """
        result = service.handle_offer(make_offer())

        assert result.reply_type is None
        assert result.to_reply(make_offer()) is None

    def test_task_creator_is_the_sender(self, service, store):
        service.handle_offer(make_offer(sender="agent_a"))

        assert store.get("TASK-001").creator == "agent_a"

    def test_budget_recorded(self, service, store):
        service.handle_offer(make_offer())

        assert store.get("TASK-001").budget == Money(100)

    def test_description_recorded(self, service, store):
        service.handle_offer(make_offer())

        assert store.get("TASK-001").description == "写一个 CSV 分析程序"

    def test_workspace_reference_recorded(self, service, store):
        service.handle_offer(make_offer())

        assert store.get("TASK-001").workspace_id == "WS-001"

    def test_required_capabilities_recorded(self, service, store):
        service.handle_offer(make_offer())

        task = store.get("TASK-001")
        assert task.required_capabilities.names == ["data_analysis", "python_coding"]

    def test_participants_include_both(self, service, store):
        service.handle_offer(make_offer())

        assert store.get("TASK-001").participants == ["agent_a", "agent_b"]

    def test_linked_when_task_already_exists(self, service, store):
        """任务已经存在时是「关联」，不是「新建」。"""
        store.save(Task.create(task_id="TASK-001", creator="agent_a",
                               budget=Money(100)))

        result = service.handle_offer(make_offer())

        assert result.action == "linked"
        assert store.count() == 1

    def test_handle_offer_requires_message(self, service):
        with pytest.raises(TypeError):
            service.handle_offer({"not": "a message"})

    def test_handle_offer_rejects_wrong_type(self, service):
        wrong = Message(from_agent="a", to_agent="b",
                        message_type=MessageType.TEXT, payload={"text": "hi"})

        with pytest.raises(ValueError, match="TASK_OFFER"):
            service.handle_offer(wrong)


# ============================================================
# 3. 校验（Task 层，不是 Communication 层）
# ============================================================
class TestOfferValidation:
    def test_missing_task_id(self, service):
        message = make_offer(payload={"task_id": None})
        result = service.handle_offer(message)

        assert result.ok is False
        assert any("task_id" in e for e in result.errors)

    def test_missing_description(self, service):
        """协议 §7.2 要求 description —— Task 层真的校验它。"""
        message = make_offer(payload={"description": None})
        result = service.handle_offer(message)

        assert result.ok is False
        assert any("description" in e for e in result.errors)

    def test_missing_budget(self, service):
        message = make_offer()
        del message.payload["budget"]

        result = service.handle_offer(message)

        assert result.ok is False
        assert any("budget" in e for e in result.errors)

    def test_bare_number_budget_rejected(self, service):
        """
        【协议 §7.1】budget 不能是裸数字 —— 没有货币语义。

        这条校验属于 Task 层。通信层继续不解释 payload。
        """
        message = make_offer(payload={"budget": 100})
        result = service.handle_offer(message)

        assert result.ok is False
        assert any("裸数字" in e for e in result.errors)

    def test_wrong_currency_rejected(self, service):
        """§19：budget.currency 必须是 AIC。"""
        message = make_offer(
            payload={"budget": {"amount": 100, "currency": "USD"}}
        )
        result = service.handle_offer(message)

        assert result.ok is False
        assert any("AIC" in e for e in result.errors)

    def test_negative_budget_rejected(self, service):
        message = make_offer(
            payload={"budget": {"amount": -5, "currency": "AIC"}}
        )
        result = service.handle_offer(message)

        assert result.ok is False

    def test_rejection_replies_with_error(self, service):
        """
        失败必须回复 —— 否则对方会一直等。
        （成功才是不回复。）
        """
        message = make_offer(payload={"task_id": None})
        result = service.handle_offer(message)

        assert result.reply_type == MessageType.ERROR
        reply = result.to_reply(message)

        assert reply is not None
        assert reply.message_type == MessageType.ERROR
        assert reply.to_agent == message.from_agent
        assert reply.payload["errors"]

    def test_rejection_does_not_create_task(self, service, store):
        message = make_offer(payload={"task_id": None})
        service.handle_offer(message)

        assert store.count() == 0

    def test_multiple_errors_reported_together(self, service):
        """一次把所有问题都说出来，而不是让人改一个跑一次。"""
        message = make_offer(payload={"task_id": None, "description": None})
        result = service.handle_offer(message)

        assert len(result.errors) >= 2


# ============================================================
# 4. 链路关联（message_id / trace_id / conversation_id）
# ============================================================
class TestTraceability:
    def test_message_id_recorded_in_offer_history(self, service, store):
        """
        【§29 的核心】报价历史里记下 message_id ——
        这样就能从任务查到通信日志。
        """
        message = make_offer()
        service.handle_offer(message)

        offers = store.offers_for("TASK-001")

        assert len(offers) == 1
        assert offers[0].message_id == message.message_id
        assert offers[0].offer_type == OfferType.TASK_OFFER
        assert offers[0].amount == Money(100)

    def test_task_trace_synced_to_message_trace(self, service, store):
        """
        【缝合点】任务的 current_trace_id 对齐到消息的 trace_id。

        这样任务和通信日志说的是同一次执行。
        """
        message = make_offer(trace_id="trc_from_message")
        service.handle_offer(message)

        assert store.get("TASK-001").current_trace_id == "trc_from_message"

    def test_trace_id_and_task_id_stay_different(self, service, store):
        """对齐 trace 之后，task_id 和 trace_id 仍然是两个字段。"""
        service.handle_offer(make_offer())

        task = store.get("TASK-001")
        assert task.task_id == "TASK-001"
        assert task.current_trace_id == "trc_offer_1"
        assert task.task_id != task.current_trace_id

    def test_details_expose_all_four_ids(self, service):
        message = make_offer()
        result = service.handle_offer(message)

        assert result.details["message_id"] == message.message_id
        assert result.details["trace_id"] == message.trace_id
        assert result.details["conversation_id"] == message.conversation_id
        assert result.task_id == "TASK-001"

    def test_retry_with_new_trace_keeps_task_id(self, service, store):
        """
        【重跑场景】新执行带新 trace，但 task_id 不变。

        这正是 Step 4A 把两个字段分开的用途。
        """
        service.handle_offer(make_offer(trace_id="trc_attempt_1"))
        assert store.get("TASK-001").current_trace_id == "trc_attempt_1"

        # 第二次执行（重试）—— 同一个 task_id，新的 trace
        service.handle_offer(make_offer(trace_id="trc_attempt_2"))

        task = store.get("TASK-001")
        assert task.task_id == "TASK-001"                    # 身份不变
        assert task.current_trace_id == "trc_attempt_2"      # 链路换了

    def test_trace_change_is_reported(self, service):
        service.handle_offer(make_offer(trace_id="trc_1"))
        result = service.handle_offer(make_offer(trace_id="trc_2"))

        assert result.details["trace_synced"] is True

    def test_full_chain_task_to_offer_to_message(self, service, store):
        """
        完整的可追踪链：

            Task → Offer → message_id → (通信日志)
        """
        message = make_offer()
        service.handle_offer(message)

        task = store.get("TASK-001")
        offers = store.offers_for(task.task_id)

        # 从任务能走到 message_id
        assert offers[0].message_id == message.message_id
        # 从 message 能走回任务
        assert message.payload["task_id"] == task.task_id
        # trace 一致
        assert task.current_trace_id == message.trace_id


# ============================================================
# 5. COUNTER_OFFER（含 §19 的预算规则）
# ============================================================
class TestHandleCounterOffer:
    def test_records_counter_offer(self, service, store):
        service.handle_offer(make_offer())
        result = service.handle_counter_offer(make_counter(price=60))

        assert result.ok is True
        offers = store.offers_for("TASK-001")
        assert [o.offer_type for o in offers] == [
            OfferType.TASK_OFFER, OfferType.COUNTER_OFFER
        ]

    def test_moves_to_negotiating(self, service, store):
        service.handle_offer(make_offer())
        service.handle_counter_offer(make_counter(price=60))

        assert store.get("TASK-001").status == TaskStatus.NEGOTIATING

    def test_multiple_rounds(self, service, store):
        """
        多轮还价 —— NEGOTIATING 的自转换。

        【注意双方必须交替】
            Step 4C 加了轮次校验：不能回自己刚发的那条报价。
            所以这里是 B → A → B 轮流，不是同一个人连报三次。
        """
        service.handle_offer(make_offer())

        first = make_counter(price=60)
        service.handle_counter_offer(first)

        second = make_counter(price=40, sender="agent_a", recipient="agent_b")
        service.handle_counter_offer(second)

        third = make_counter(price=25)
        service.handle_counter_offer(third)

        offers = store.offers_for("TASK-001")
        assert [o.amount.amount for o in offers] == [100, 60, 40, 25]
        assert store.get("TASK-001").status == TaskStatus.NEGOTIATING

    def test_price_above_budget_rejected(self, service, store):
        """
        【§19 的业务规则】还价不能超过预算上限。

        注意这是 **Task 层** 的校验，不是通信层的。
        """
        service.handle_offer(make_offer())          # budget = 100
        result = service.handle_counter_offer(make_counter(price=150))

        assert result.ok is False
        assert any("预算" in e for e in result.errors)
        # 没被记录
        assert len(store.offers_for("TASK-001")) == 1

    def test_price_equal_to_budget_allowed(self, service):
        service.handle_offer(make_offer())
        result = service.handle_counter_offer(make_counter(price=100))

        assert result.ok is True

    def test_price_below_budget_allowed(self, service):
        service.handle_offer(make_offer())
        assert service.handle_counter_offer(make_counter(price=1)).ok is True

    def test_bare_number_price_rejected(self, service):
        service.handle_offer(make_offer())
        result = service.handle_counter_offer(
            make_counter(payload={"price": 25})
        )

        assert result.ok is False
        assert any("裸数字" in e for e in result.errors)

    def test_wrong_currency_rejected(self, service):
        service.handle_offer(make_offer())
        result = service.handle_counter_offer(
            make_counter(payload={"price": {"amount": 25, "currency": "USD"}})
        )

        assert result.ok is False
        assert any("AIC" in e for e in result.errors)

    def test_unknown_task_rejected(self, service):
        """还价必须先有一个已知任务。"""
        result = service.handle_counter_offer(make_counter(task_id="TASK_不存在"))

        assert result.ok is False
        assert any("没有任务" in e for e in result.errors)

    def test_requires_right_message_type(self, service):
        with pytest.raises(ValueError, match="(?i)counter_offer"):
            service.handle_counter_offer(make_offer())

    def test_message_id_recorded(self, service, store):
        service.handle_offer(make_offer())
        counter = make_counter(price=60)
        service.handle_counter_offer(counter)

        offers = store.offers_for("TASK-001")
        assert offers[1].message_id == counter.message_id

    def test_does_not_accept(self, service, store):
        """还价也不等于成交。"""
        service.handle_offer(make_offer())
        service.handle_counter_offer(make_counter(price=60))

        task = store.get("TASK-001")
        assert task.status == TaskStatus.NEGOTIATING
        assert task.agreed_price is None      # 还没谈成


# ============================================================
# 6. 隔离性
# ============================================================
class TestIsolation:
    def test_handler_creates_no_ledger_entries(self, db, economy, funded_agents,
                                               directory):
        """
        【Capability ≠ Economy】

        处理一整轮 offer + counter_offer，账本一笔都不该多。
        """
        service = TaskService(TaskStore(economy.db), directory)
        before_ledger = economy.ledger.count()
        before_a = economy.agent("agent_a").balance

        service.handle_offer(make_offer())
        service.handle_counter_offer(make_counter(price=60))

        assert economy.ledger.count() == before_ledger
        assert economy.agent("agent_a").balance == before_a
        assert economy.audit()["balanced"] is True

    def test_task_service_does_not_send_messages(self, service):
        """
        TaskService 只**返回**应答内容，自己不发消息。

        这样它能在完全没有通信层的情况下被测试。
        """
        for forbidden in ("send", "send_message", "transport", "broadcast"):
            assert not hasattr(service, forbidden), (
                f"TaskService 不该有 {forbidden!r} —— "
                f"发消息是 Agent / 组合根的事"
            )

    def test_task_service_does_not_grant_workspace(self, service):
        for forbidden in ("open_workspace", "grant", "permission",
                          "read_file", "write_file", "checkout"):
            assert not hasattr(service, forbidden), (
                f"TaskService 不该有 {forbidden!r}"
            )

    def test_capability_does_not_grant_workspace_permission(self, service, store):
        """
        【Capability ≠ Permission】

        B 有能力，也拿到了 workspace_id，但它**没有**获得任何权限。
        """
        service.handle_offer(make_offer())

        task = store.get("TASK-001")
        assert task.workspace_id == "WS-001"
        # 但没有任何访问能力
        assert not hasattr(task, "read_file")
        assert not hasattr(store, "open_workspace")


# ============================================================
# 7. HandlerResult
# ============================================================
class TestHandlerResult:
    def test_bool(self, service):
        ok = service.handle_offer(make_offer())
        assert bool(ok) is True

    def test_summary(self, service):
        result = service.handle_offer(make_offer())
        summary = result.summary()

        assert summary["ok"] is True
        assert summary["action"] == "created"
        assert summary["task_id"] == "TASK-001"

    def test_repr(self, service):
        assert "HandlerResult" in repr(service.handle_offer(make_offer()))

    def test_to_reply_none_when_no_reply_type(self):
        result = HandlerResult(ok=True, action="created", task_id="T-1")

        assert result.to_reply(make_offer()) is None

    def test_service_repr_and_describe(self, service):
        assert "TaskService" in repr(service)
        assert service.describe()["directory"] == "LocalAgentDirectory"
