"""
Test: Task Core（Step 4A）
============================================================

这个文件验证 Task 领域对象与状态机。

【分七组】

    1. 身份           task_id 与 current_trace_id 是两回事
    2. 状态机         合法转换 / 非法转换 / 终态
    3. budget vs agreed_price   两个概念不能混
    4. 报价历史       完整报价链
    5. Workspace 引用 只是标识，没有权限
    6. 边界           Task 不碰钱、不碰 LLM、不碰文件
    7. 序列化         与协议形状一致

【最重要的一条纪律】

    这个文件**不许** import llm / communication / wallet / ledger。
    因为 Task Core 必须能完全脱离它们运行。
    这不是洁癖 —— 如果 Task 依赖 LLM，那么「创建一个任务」就得先有模型，
    而 Task 只是业务状态，和推理无关。
"""

from __future__ import annotations

import pytest

from core.money import Money
from core.task import (
    SELF_TRANSITIONS,
    TRANSITIONS,
    BudgetExceededError,
    InvalidTaskTransition,
    Offer,
    OfferType,
    Task,
    TaskStatus,
    TaskValidationError,
)


@pytest.fixture
def task():
    """一个刚创建的任务：100 AIC 预算，关联 WS-001。"""
    return Task.create(
        creator="agent_a",
        description="写一个 CSV 分析程序",
        budget=Money(100),
        workspace_id="WS-001",
    )


@pytest.fixture
def negotiating_task(task):
    """一个已经走到 NEGOTIATING 的任务。"""
    task.offer()
    task.negotiate()
    return task


@pytest.fixture
def accepted_task(negotiating_task):
    """一个已经成交（25 AIC）的任务。"""
    negotiating_task.accept(Money(25), contractor="agent_b")
    return negotiating_task


# ============================================================
# 1. 身份
# ============================================================
class TestIdentity:
    def test_task_has_stable_id(self, task):
        assert task.task_id
        assert task.task_id.startswith("task_")

    def test_ids_are_unique(self):
        ids = {Task.create(creator="a").task_id for _ in range(100)}
        assert len(ids) == 100

    def test_task_id_and_trace_id_are_different_fields(self, task):
        """
        【核心】task_id 是任务身份，current_trace_id 是这一次执行。

        两者必须能不同 —— 因为同一个任务可能重跑多次。
        """
        assert task.task_id != task.current_trace_id
        assert task.current_trace_id.startswith("trc_")

    def test_can_start_a_new_trace_for_the_same_task(self, task):
        """
        同一个任务换一次 trace（模拟失败重跑），task_id 不变。
        """
        original_task_id = task.task_id
        first_trace = task.current_trace_id

        task.current_trace_id = "trc_second_attempt"

        assert task.task_id == original_task_id          # 身份没变
        assert task.current_trace_id != first_trace      # 链路换了

    def test_explicit_trace_id_accepted(self):
        task = Task.create(creator="a", trace_id="trc_chosen")
        assert task.current_trace_id == "trc_chosen"

    def test_explicit_task_id_accepted(self):
        task = Task.create(creator="a", task_id="TASK-001")
        assert task.task_id == "TASK-001"

    @pytest.mark.parametrize("bad", ["", "   ", None, 123])
    def test_invalid_task_id_rejected(self, bad):
        with pytest.raises(TaskValidationError):
            Task(task_id=bad, creator="agent_a")

    @pytest.mark.parametrize("bad", ["", "   ", None, 123])
    def test_invalid_creator_rejected(self, bad):
        with pytest.raises(TaskValidationError):
            Task(task_id="task_x", creator=bad)

    def test_sequential_tasks_have_different_traces(self):
        """两个独立任务不该共用 trace。"""
        first = Task.create(creator="a")
        second = Task.create(creator="a")

        assert first.task_id != second.task_id
        assert first.current_trace_id != second.current_trace_id


# ============================================================
# 2. 状态机
# ============================================================
class TestStateMachine:
    def test_new_task_starts_created(self, task):
        assert task.status == TaskStatus.CREATED

    def test_happy_path(self, task):
        """完整正常路径：CREATED → … → COMPLETED。"""
        assert task.status == TaskStatus.CREATED

        task.offer()
        assert task.status == TaskStatus.OFFERED

        task.negotiate()
        assert task.status == TaskStatus.NEGOTIATING

        task.accept(Money(25))
        assert task.status == TaskStatus.ACCEPTED

        task.start()
        assert task.status == TaskStatus.IN_PROGRESS

        task.submit()
        assert task.status == TaskStatus.SUBMITTED

        task.verify(passed=True)
        assert task.status == TaskStatus.VERIFIED

        task.complete()
        assert task.status == TaskStatus.COMPLETED
        assert task.is_terminal is True

    def test_negotiating_can_repeat(self, negotiating_task):
        """
        NEGOTIATING → NEGOTIATING 是唯一允许的自转换。

        因为一轮谈判里可以来回还价很多次。
        """
        for _ in range(5):
            negotiating_task.negotiate()

        assert negotiating_task.status == TaskStatus.NEGOTIATING

    def test_only_negotiating_allows_self_transition(self):
        assert SELF_TRANSITIONS == frozenset({TaskStatus.NEGOTIATING})

    def test_verify_failure_sends_back_to_in_progress(self, accepted_task):
        """
        验收不通过 → 打回重做（回到 IN_PROGRESS）。

        这比直接把任务判死更符合实际：交付不合格通常是可以修的。
        """
        accepted_task.start()
        accepted_task.submit()

        accepted_task.verify(passed=False)

        assert accepted_task.status == TaskStatus.IN_PROGRESS
        assert accepted_task.is_terminal is False

    def test_can_rework_then_succeed(self, accepted_task):
        accepted_task.start()
        accepted_task.submit()
        accepted_task.verify(passed=False)      # 返工
        accepted_task.submit()                  # 再交
        accepted_task.verify(passed=True)       # 通过

        assert accepted_task.status == TaskStatus.VERIFIED

    def test_status_cannot_be_assigned_directly(self, task):
        """
        【关键】状态没有 setter —— 只能走状态机。

        这保证了「状态机不会被绕过」。
        """
        with pytest.raises(AttributeError):
            task.status = TaskStatus.COMPLETED

    def test_direct_assignment_leaves_status_unchanged(self, task):
        try:
            task.status = TaskStatus.COMPLETED
        except AttributeError:
            pass

        assert task.status == TaskStatus.CREATED


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "target",
        [
            TaskStatus.IN_PROGRESS,
            TaskStatus.SUBMITTED,
            TaskStatus.VERIFIED,
            TaskStatus.COMPLETED,
            TaskStatus.ACCEPTED,
            TaskStatus.NEGOTIATING,
            TaskStatus.REJECTED,
            TaskStatus.EXPIRED,
        ],
    )
    def test_created_cannot_jump_forward(self, task, target):
        """CREATED 只能去 OFFERED 或 CANCELLED —— 不能跳步。"""
        with pytest.raises(InvalidTaskTransition):
            task.transition_to(target)

    def test_cannot_skip_from_offered_to_completed(self, task):
        task.offer()

        with pytest.raises(InvalidTaskTransition):
            task.transition_to(TaskStatus.COMPLETED)

    def test_cannot_go_backwards(self, accepted_task):
        """ACCEPTED 不能退回 OFFERED。"""
        with pytest.raises(InvalidTaskTransition):
            accepted_task.transition_to(TaskStatus.OFFERED)

    def test_self_transition_rejected_for_most_states(self, task):
        """除了 NEGOTIATING，其它状态不能转到自己。"""
        with pytest.raises(InvalidTaskTransition):
            task.transition_to(TaskStatus.CREATED)

    def test_unknown_status_rejected(self, task):
        with pytest.raises(TaskValidationError):
            task.transition_to("NOT_A_REAL_STATUS")

    def test_error_message_explains_allowed_transitions(self, task):
        """报错要能直接告诉你「该去哪儿」。"""
        with pytest.raises(InvalidTaskTransition) as exc:
            task.transition_to(TaskStatus.COMPLETED)

        message = str(exc.value)
        assert TaskStatus.CREATED in message
        assert TaskStatus.COMPLETED in message
        assert "OFFERED" in message        # 提示了合法目标

    def test_can_transition_to_is_pure_query(self, task):
        """can_transition_to 只是查询，不该改状态。"""
        assert task.can_transition_to(TaskStatus.OFFERED) is True
        assert task.can_transition_to(TaskStatus.COMPLETED) is False
        assert task.status == TaskStatus.CREATED


class TestTerminalStates:
    @pytest.mark.parametrize(
        "terminal",
        [
            TaskStatus.COMPLETED,
            TaskStatus.REJECTED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
            TaskStatus.FAILED,
        ],
    )
    def test_terminal_states_have_no_outgoing_edges(self, terminal):
        assert TRANSITIONS[terminal] == frozenset()

    @pytest.mark.parametrize(
        "terminal",
        [
            TaskStatus.COMPLETED,
            TaskStatus.REJECTED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
            TaskStatus.FAILED,
        ],
    )
    def test_cannot_leave_terminal_state(self, terminal):
        task = Task(task_id="task_x", creator="a", status=terminal)

        assert task.is_terminal is True
        with pytest.raises(InvalidTaskTransition):
            task.transition_to(TaskStatus.COMPLETED)

    def test_rejected_path(self, task):
        task.offer()
        task.reject("太贵了")

        assert task.status == TaskStatus.REJECTED
        assert task.is_terminal is True

    def test_expired_path(self, task):
        task.offer()
        task.expire()

        assert task.status == TaskStatus.EXPIRED

    def test_cancelled_from_any_open_state(self):
        """任何非终态都可以取消。"""
        for prepare in (
            lambda t: None,                              # CREATED
            lambda t: t.offer(),                         # OFFERED
            lambda t: (t.offer(), t.negotiate()),        # NEGOTIATING
            lambda t: (t.offer(), t.accept(Money(10))),  # ACCEPTED
        ):
            task = Task.create(creator="a", budget=Money(100))
            prepare(task)
            task.cancel("不需要了")
            assert task.status == TaskStatus.CANCELLED

    def test_failed_path(self, accepted_task):
        accepted_task.start()
        accepted_task.fail("环境坏了")

        assert accepted_task.status == TaskStatus.FAILED
        assert accepted_task.is_terminal is True

    def test_is_open_reflects_terminal(self, task):
        assert task.is_open is True
        task.offer()
        task.reject()
        assert task.is_open is False

    def test_all_statuses_are_reachable_from_created(self):
        """
        图的可达性检查：从 CREATED 出发能不能走到每一个状态。

        这能抓到「定义了状态但忘了写转换边」这种错误。
        """
        seen = {TaskStatus.CREATED}
        frontier = [TaskStatus.CREATED]

        while frontier:
            current = frontier.pop()
            for nxt in TRANSITIONS[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)

        unreachable = set(TaskStatus.all()) - seen
        assert not unreachable, f"这些状态从 CREATED 走不到：{unreachable}"

    def test_transitions_table_covers_every_status(self):
        """每个定义的状态都必须在转换表里有条目。"""
        for status in TaskStatus.all():
            assert status in TRANSITIONS, f"{status} 缺少转换规则"

    def test_transitions_only_point_to_known_statuses(self):
        """转换表里不能出现拼错的状态名。"""
        for source, targets in TRANSITIONS.items():
            for target in targets:
                assert TaskStatus.is_known(target), (
                    f"{source} → {target} 指向了未知状态"
                )


class TestCommunicationStatusIsSeparate:
    """
    Task 状态机与 Communication 状态机必须完全分开。

    这是 Step 4A 的硬性要求之一。
    """

    def test_no_transport_state_leaks_into_task_status(self):
        """
        【这是真正要禁止的事】

        绝不能出现「Task 里有一个 SENT 状态」这种情况 ——
        那意味着通信的传输状态混进了业务状态机。

        两个独立状态机在各自领域用同样的常用词（CREATED / FAILED）
        是可以接受的；但传输语义的词绝不能出现在 Task 里。
        """
        from communication import MessageStatus

        transport_states = {
            MessageStatus.QUEUED, MessageStatus.SENT, MessageStatus.DELIVERED,
            MessageStatus.RECEIVED, MessageStatus.PROCESSING,
            MessageStatus.PROCESSED, MessageStatus.DUPLICATE,
            MessageStatus.TIMEOUT, MessageStatus.RETRY,
        }
        task_states = set(TaskStatus.all())

        leaked = transport_states & task_states
        assert not leaked, (
            f"传输类状态混进了 Task 状态机：{leaked}\n"
            f"Task 是业务状态机，不该有传输语义的状态。"
        )

    def test_shared_status_names_are_only_generic_words(self):
        """
        两个状态机只允许在 CREATED / FAILED 这种通用词上重名。

        这条测试把「重名」这件事固定下来 ——
        如果将来有人往 Task 里加了一个叫 DELIVERED 的状态，
        上一条测试会红；而这条保证了重名的范围不会悄悄扩大。
        """
        from communication import MessageStatus

        overlap = set(TaskStatus.all()) & set(MessageStatus.all())

        assert overlap <= {"CREATED", "FAILED"}, (
            f"两个状态机的重名范围扩大了：{overlap}\n"
            f"如果这是有意为之，请先确认它不会造成混淆。"
        )

    def test_the_two_state_machines_are_different_types(self):
        """它们是不同的类，有不同的转换规则。"""
        from communication.recorder import _STATUS_TIMESTAMP_COLUMN

        # Task 用自己的转换表，和通信的完全不同
        assert TRANSITIONS is not _STATUS_TIMESTAMP_COLUMN
        assert set(TRANSITIONS) == set(TaskStatus.all())

    def test_business_states_are_absent_from_communication(self):
        """反过来，业务状态也不该出现在通信状态机里。"""
        from communication import MessageStatus

        business_states = {
            TaskStatus.OFFERED, TaskStatus.NEGOTIATING, TaskStatus.ACCEPTED,
            TaskStatus.IN_PROGRESS, TaskStatus.SUBMITTED,
            TaskStatus.VERIFIED, TaskStatus.COMPLETED,
        }
        message_states = set(MessageStatus.all())

        leaked = business_states & message_states
        assert not leaked, f"业务状态混进了通信状态机：{leaked}"

    def test_task_core_does_not_import_communication(self):
        """Task Core 连 communication 都不 import。"""
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "core" / "task.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)

        for module in modules:
            assert not module.startswith("communication"), (
                f"core/task.py 导入了 {module} —— "
                f"Task 状态机必须与通信状态机分离"
            )

    def test_message_status_does_not_drive_task_status(self, task):
        """
        通信层发生什么，都不该让 Task 状态自动跳变。

        这里没有消息参与，Task 就老老实实停在 CREATED。
        """
        assert task.status == TaskStatus.CREATED


# ============================================================
# 3. budget vs agreed_price
# ============================================================
class TestBudgetVersusAgreedPrice:
    """
    两个概念必须分开保存。

        budget       发起方愿意付的**上限**
        agreed_price 实际**谈成**的数
    """

    def test_budget_set_at_creation(self, task):
        assert task.budget == Money(100)
        assert task.agreed_price is None

    def test_agreed_price_set_on_accept(self, accepted_task):
        assert accepted_task.agreed_price == Money(25)
        assert accepted_task.budget == Money(100)

    def test_both_are_kept_separately(self, accepted_task):
        """
        【最关键的断言】成交之后两个数必须都还在，而且不一样。
        """
        assert accepted_task.budget.amount == 100
        assert accepted_task.agreed_price.amount == 25
        assert accepted_task.budget != accepted_task.agreed_price

    def test_budget_is_not_mutated_by_accept(self, negotiating_task):
        negotiating_task.accept(Money(25))

        assert negotiating_task.budget == Money(100)   # 预算原封不动

    def test_accept_above_budget_rejected(self, negotiating_task):
        """
        成交价不能超过预算 —— 预算就是上限。

        这条规则把「budget 是约束、price 是结果」变成了代码保证。
        """
        with pytest.raises(BudgetExceededError):
            negotiating_task.accept(Money(150))

    def test_rejected_accept_leaves_state_unchanged(self, negotiating_task):
        """超预算被拒之后，任务不能处于半改状态。"""
        with pytest.raises(BudgetExceededError):
            negotiating_task.accept(Money(150))

        assert negotiating_task.status == TaskStatus.NEGOTIATING
        assert negotiating_task.agreed_price is None

    def test_accept_exactly_at_budget_ok(self, negotiating_task):
        """等于预算是允许的 —— 上限包含边界。"""
        negotiating_task.accept(Money(100))
        assert negotiating_task.agreed_price == Money(100)

    def test_accept_zero_is_allowed(self, negotiating_task):
        """0 元任务在协议上是合法的（免费协作）。"""
        negotiating_task.accept(Money(0))
        assert negotiating_task.agreed_price == Money(0)

    def test_no_budget_means_no_limit(self):
        """没有预算约束时不该报错。"""
        task = Task.create(creator="a")       # 不给 budget
        task.offer()
        task.accept(Money(999999))

        assert task.agreed_price == Money(999999)

    def test_currency_mismatch_between_budget_and_price_rejected(self):
        task = Task.create(creator="a", budget=Money(100, "AIC"))
        task.offer()

        with pytest.raises(TaskValidationError):
            task.accept(Money(25, "USD"))

    def test_accept_requires_money_not_number(self, negotiating_task):
        """
        裸数字不能当成交价 —— 那正是协议禁止的写法。
        """
        with pytest.raises(TaskValidationError):
            negotiating_task.accept(25)

    def test_budget_must_be_money(self):
        with pytest.raises(TaskValidationError):
            Task.create(creator="a", budget=100)

    def test_accept_records_contractor(self, negotiating_task):
        negotiating_task.accept(Money(25), contractor="agent_b")

        assert negotiating_task.contractor == "agent_b"

    def test_summary_shows_both(self, accepted_task):
        summary = accepted_task.summary()

        assert summary["budget"] == "100 AIC"
        assert summary["agreed_price"] == "25 AIC"


# ============================================================
# 4. 报价历史
# ============================================================
class TestOfferHistory:
    def test_offer_recorded(self, task):
        record = task.record_offer(OfferType.TASK_OFFER, Money(100))

        assert record.offer_type == OfferType.TASK_OFFER
        assert record.amount == Money(100)
        assert len(task.offers) == 1

    def test_full_offer_chain(self, negotiating_task):
        """
        完整报价链 —— 这正是「不能只存 agreed_price」的原因。

            只存 25 就问不出：这是压价成交还是按预算成交？还了几次价？
        """
        negotiating_task.record_offer(OfferType.COUNTER_OFFER, Money(60))
        negotiating_task.record_offer(OfferType.COUNTER_OFFER, Money(25))
        negotiating_task.accept(Money(25))
        negotiating_task.record_offer(OfferType.ACCEPT, Money(25))

        assert negotiating_task.offer_amounts() == [60.0, 25.0, 25.0]
        assert negotiating_task.latest_offer().offer_type == OfferType.ACCEPT

    def test_offer_does_not_change_status(self, task):
        """
        记录报价**不**改状态。

        因为「收到一个报价」和「任务因此进入谈判状态」
        在业务上未必同时发生。
        """
        task.record_offer(OfferType.TASK_OFFER, Money(100))

        assert task.status == TaskStatus.CREATED

    def test_offer_can_carry_message_id(self, task):
        """
        报价可以关联通信消息 —— 但**不是必需的**。

        这让 Task Core 保持对通信层的零依赖，
        同时为将来「从任务查到通信日志」留好接口。
        """
        with_id = task.record_offer(
            OfferType.TASK_OFFER, Money(100), message_id="msg_abc123"
        )
        without_id = task.record_offer(OfferType.COUNTER_OFFER, Money(60))

        assert with_id.message_id == "msg_abc123"
        assert without_id.message_id is None

    def test_offer_requires_money(self, task):
        with pytest.raises(TaskValidationError):
            task.record_offer(OfferType.TASK_OFFER, 100)

    def test_unknown_offer_type_rejected(self):
        with pytest.raises(TaskValidationError):
            Offer(offer_type="NOT_A_TYPE", amount=Money(10))

    def test_offer_is_immutable(self, task):
        record = task.record_offer(OfferType.TASK_OFFER, Money(100))

        with pytest.raises(Exception):
            record.amount = Money(999)

    def test_offer_types_match_protocol_message_types(self):
        """
        OfferType 的字符串必须和协议里的 message_type 一致。

        【为什么不直接 import communication】
            那会破坏 Task Core 的独立性（有守卫测试盯着）。
            所以改由这条测试来保证两边字符串同步。

        好处：将来把报价接到通信层时，不需要任何映射转换。
        """
        from communication import MessageType

        assert OfferType.TASK_OFFER == MessageType.TASK_OFFER
        assert OfferType.COUNTER_OFFER == MessageType.COUNTER_OFFER
        assert OfferType.ACCEPT == MessageType.ACCEPT

    def test_offer_ids_are_unique(self, task):
        ids = {
            task.record_offer(OfferType.COUNTER_OFFER, Money(i)).offer_id
            for i in range(20)
        }
        assert len(ids) == 20

    def test_offer_serialization(self, task):
        record = task.record_offer(OfferType.TASK_OFFER, Money(100))
        restored = Offer.from_dict(record.to_dict())

        assert restored.offer_id == record.offer_id
        assert restored.offer_type == record.offer_type
        assert restored.amount == record.amount


# ============================================================
# 5. Workspace 引用
# ============================================================
class TestWorkspaceReference:
    def test_task_can_reference_workspace(self, task):
        assert task.workspace_id == "WS-001"
        assert task.workspace is not None
        assert task.workspace.kind == "TASK"

    def test_workspace_is_optional(self):
        task = Task.create(creator="a")

        assert task.workspace is None
        assert task.workspace_id is None

    def test_workspace_reference_grants_nothing(self, task):
        """
        【Step 4A 的核心承诺】拿到 Task 不等于拿到 Workspace 权限。
        """
        for forbidden in (
            "open_workspace", "grant", "read_file", "write_file",
            "list_files", "access", "permission", "capability",
        ):
            assert not hasattr(task, forbidden), (
                f"Task 不该有 {forbidden!r} —— workspace_id 只是逻辑引用"
            )

    def test_different_tasks_get_different_workspaces(self):
        """
        Task Workspace 必须是 Task-specific，不能共用一个永久空间。
        """
        first = Task.create(creator="a", workspace_id="WS-001")
        second = Task.create(creator="a", workspace_id="WS-002")

        assert first.workspace_id != second.workspace_id
        assert first.workspace != second.workspace

    def test_workspace_id_survives_serialization(self, task):
        restored = Task.from_dict(task.to_dict())

        assert restored.workspace_id == "WS-001"

    def test_participants_supported(self):
        """
        一个 Task Workspace 未来可以有多个 Agent 参与。
        本步骤只保存名单，不实现权限。
        """
        task = Task.create(
            creator="agent_a",
            workspace_id="WS-001",
            participants=["agent_a", "agent_b", "agent_c"],
        )

        assert task.participants == ["agent_a", "agent_b", "agent_c"]

    def test_participants_default_empty(self):
        assert Task.create(creator="a").participants == []

    def test_participants_are_copied_not_shared(self):
        """传进来的列表被复制 —— 外部改它不能影响 Task。"""
        names = ["agent_a"]
        task = Task.create(creator="a", participants=names)

        names.append("agent_evil")

        assert task.participants == ["agent_a"]


# ============================================================
# 6. 边界：不碰钱、不碰 LLM、不碰文件
# ============================================================
class TestTaskIsolatedFromEconomy:
    def test_task_has_no_wallet_access(self, task):
        for forbidden in ("wallet", "ledger", "transfer", "pay", "settle",
                          "escrow", "charge", "debit", "credit"):
            assert not hasattr(task, forbidden), (
                f"Task 不该有 {forbidden!r} —— 它只记录经济约定，不执行结算"
            )

    def test_accept_does_not_create_ledger_entries(self, db, economy, funded_agents):
        """
        【关键】task.accept() 不会产生任何账本记录。
        """
        from core.task_store import TaskStore

        before_ledger = economy.ledger.count()
        before_a = economy.agent("agent_a").balance
        before_b = economy.agent("agent_b").balance

        task = Task.create(creator="agent_a", budget=Money(100))
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        TaskStore(db).save(task)

        assert economy.ledger.count() == before_ledger, "Task 不该产生账目"
        assert economy.agent("agent_a").balance == before_a
        assert economy.agent("agent_b").balance == before_b

    def test_full_lifecycle_does_not_touch_money(self, db, economy, funded_agents):
        """走完整个生命周期，一分钱都不该动。"""
        from core.task_store import TaskStore

        before_ledger = economy.ledger.count()

        task = Task.create(creator="agent_a", budget=Money(100))
        for step in (
            task.offer, task.negotiate,
            lambda: task.accept(Money(25), contractor="agent_b"),
            task.start, task.submit,
            lambda: task.verify(passed=True),
            task.complete,
        ):
            step()
        TaskStore(db).save(task)

        assert economy.ledger.count() == before_ledger
        assert economy.audit()["balanced"] is True

    def test_agreed_price_is_a_promise_not_a_payment(self, accepted_task):
        """
        agreed_price 只是「谈好了」的记录，不代表付过了。
        """
        assert accepted_task.agreed_price == Money(25)
        # Task 里没有任何「已支付」的标记
        assert not hasattr(accepted_task, "paid")
        assert not hasattr(accepted_task, "settled")


class TestTaskIsolatedFromLLM:
    def test_task_core_does_not_import_llm(self):
        """Task Core 不能依赖 llm/*。"""
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "core" / "task.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)

        for module in modules:
            assert not module.startswith("llm"), (
                f"core/task.py 导入了 {module} —— Task 是业务状态，不是 AI 推理"
            )

    def test_lifecycle_works_without_any_llm(self, accepted_task):
        """
        完整生命周期不需要任何模型 —— 这个测试本身就是证明
        （因为整个测试文件没有 import llm）。
        """
        accepted_task.start()
        accepted_task.submit()
        accepted_task.verify(passed=True)
        accepted_task.complete()

        assert accepted_task.status == TaskStatus.COMPLETED


class TestTaskIsolatedFromFilesystem:
    def test_task_core_does_not_do_file_io(self):
        """
        Task Core 不能读写文件。

        【注意】这不是说整个项目不能碰文件 ——
        Agent Private Workspace（core/workspace.py）当然要读写文件。
        但 Task 只是业务状态，它不该有文件能力。
        """
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "core" / "task.py"
        text = source.read_text(encoding="utf-8")
        tree = ast.parse(text)

        # 不能 import 文件相关模块
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)

        forbidden_modules = {"os", "os.path", "pathlib", "shutil", "subprocess",
                             "glob", "tempfile"}
        for module in modules:
            assert module not in forbidden_modules, (
                f"core/task.py 导入了 {module} —— Task 不该有文件能力"
            )

        # 不能调用 open()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "open", "core/task.py 调用了 open()"


# ============================================================
# 7. 序列化
# ============================================================
class TestSerialization:
    def test_to_dict_has_expected_fields(self, task):
        data = task.to_dict()

        for field in (
            "task_id", "current_trace_id", "creator", "contractor",
            "participants", "status", "description", "workspace_id",
            "budget", "agreed_price", "acceptance", "expires_at",
            "created_at", "updated_at",
        ):
            assert field in data, f"缺少字段 {field}"

    def test_budget_and_agreed_price_are_separate_keys(self, accepted_task):
        """两个概念在序列化后仍然是两个键。"""
        data = accepted_task.to_dict()

        assert data["budget"] == {"amount": 100, "currency": "AIC"}
        assert data["agreed_price"] == {"amount": 25, "currency": "AIC"}

    def test_roundtrip_preserves_everything(self, accepted_task):
        accepted_task.record_offer(OfferType.COUNTER_OFFER, Money(60))
        restored = Task.from_dict(accepted_task.to_dict())

        assert restored.task_id == accepted_task.task_id
        assert restored.status == accepted_task.status
        assert restored.budget == accepted_task.budget
        assert restored.agreed_price == accepted_task.agreed_price
        assert restored.workspace_id == accepted_task.workspace_id
        assert restored.current_trace_id == accepted_task.current_trace_id
        assert len(restored.offers) == len(accepted_task.offers)

    def test_status_is_preserved(self):
        """从字典恢复时可以带状态 —— 这是存储重建的路径。"""
        task = Task.from_dict({
            "task_id": "task_x", "creator": "a", "status": "IN_PROGRESS",
        })

        assert task.status == TaskStatus.IN_PROGRESS

    def test_unknown_status_rejected_on_restore(self):
        with pytest.raises(TaskValidationError):
            Task.from_dict({
                "task_id": "task_x", "creator": "a", "status": "BOGUS",
            })

    def test_none_budget_serializes_as_none(self):
        assert Task.create(creator="a").to_dict()["budget"] is None

    def test_acceptance_preserved(self):
        task = Task.create(creator="a", acceptance={"type": "tests"})
        restored = Task.from_dict(task.to_dict())

        assert restored.acceptance == {"type": "tests"}

    def test_expires_at_preserved(self):
        task = Task.create(
            creator="a", expires_at="2026-09-26T10:00:00+00:00"
        )
        restored = Task.from_dict(task.to_dict())

        assert restored.expires_at == "2026-09-26T10:00:00+00:00"

    def test_acceptance_is_copied(self):
        original = {"type": "tests", "rules": ["a"]}
        task = Task.create(creator="a", acceptance=original)

        original["type"] = "manual"

        assert task.acceptance["type"] == "tests"


# ============================================================
# 8. 其他
# ============================================================
class TestTimestamps:
    def test_created_and_updated_set(self, task):
        assert task.created_at
        assert task.updated_at

    def test_update_advances_timestamp(self, task):
        before = task.updated_at
        task.offer()

        assert task.updated_at >= before

    def test_created_at_not_changed_by_transitions(self, task):
        original = task.created_at
        task.offer()
        task.negotiate()

        assert task.created_at == original


class TestRepr:
    def test_repr_is_informative(self, task):
        text = repr(task)

        assert task.task_id in text
        assert "CREATED" in text
        assert "100" in text

    def test_summary(self, accepted_task):
        summary = accepted_task.summary()

        assert summary["status"] == TaskStatus.ACCEPTED
        assert summary["budget"] == "100 AIC"
        assert summary["agreed_price"] == "25 AIC"
        assert summary["workspace_id"] == "WS-001"
        assert summary["tracing"] == accepted_task.current_trace_id
