"""
Test: 议价 —— ACCEPT / COUNTER_OFFER / REJECT（Step 4C）
============================================================

【这个文件验证什么】

    Step 4C 把 Task 的三种经济决策正式接到了协议上。

【最重要的四条断言】

    1. ACCEPT 之后任务变成 ACCEPTED，**但一分钱都没动**
       （§20：ACCEPT 只意味着合同成立，不意味着付款）

    2. ACCEPT.price 必须等于**被接受的那条报价**的价格（§14）
       而且必须通过 reply_to 说清「接受的是哪一条」（§18）

    3. 角色 / 轮次验证真的会拦住错误的人（§16）
       不是参与方、或者自己回自己，都会被拒绝

    4. REJECT 是**业务行为**，不是协议错误（§12 / §13）
       它让任务进入 REJECTED，同样不产生任何经济后果
"""

from __future__ import annotations

import pytest

from core.agent_directory import LocalAgentDirectory
from core.decision_policy import Decision, DecisionType, RuleBasedDecisionPolicy
from core.money import Money
from core.task import OfferType, Task, TaskStatus
from core.task_agreement import TaskAgreementRecord
from core.task_store import TaskStore

from communication import Message, MessageType

from services import TaskService


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def directory() -> LocalAgentDirectory:
    d = LocalAgentDirectory()
    d.register_agent("agent_a", ["task_decomposition"])
    d.register_agent("agent_b", ["python_coding", "data_analysis"])
    return d


@pytest.fixture
def store(db) -> TaskStore:
    return TaskStore(db)


@pytest.fixture
def policies():
    return {
        # A 是发起方（买方）：接受线 50（<=50 就付）
        "agent_a": RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=40, walk_away_price=100, max_rounds=2
        ),
        # B 是承接方（卖方）：接受线 50（>=50 就干）
        "agent_b": RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=60, walk_away_price=25, max_rounds=2
        ),
    }


@pytest.fixture
def service(store, directory, policies) -> TaskService:
    return TaskService(store=store, directory=directory, policies=policies)


def make_task(store: TaskStore, task_id="T-1", budget=100):
    task = Task.create(
        task_id=task_id,
        creator="agent_a",
        description="写个分析程序",
        budget=Money(budget),
        workspace_id="WS-001",
        participants=["agent_a", "agent_b"],
    )
    task.offer()
    store.save(task)
    return task


def offer_message(task_id="T-1", budget=100, sender="agent_a",
                  recipient="agent_b", **overrides) -> Message:
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.TASK_OFFER,
        trace_id=overrides.pop("trace_id", "trc_1"),
        payload={
            "task_id": task_id,
            "description": "写个分析程序",
            "budget": {"amount": budget, "currency": "AIC"},
        },
        **overrides,
    )


def counter_message(task_id="T-1", price=60, sender="agent_b",
                    recipient="agent_a", reply_to=None,
                    trace_id="trc_1") -> Message:
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.COUNTER_OFFER,
        trace_id=trace_id, reply_to=reply_to,
        payload={"task_id": task_id, "price": {"amount": price, "currency": "AIC"}},
    )


def accept_message(task_id="T-1", price=60, sender="agent_a",
                   recipient="agent_b", reply_to=None,
                   trace_id="trc_1") -> Message:
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.ACCEPT,
        trace_id=trace_id, reply_to=reply_to,
        payload={"task_id": task_id, "price": {"amount": price, "currency": "AIC"}},
    )


def reject_message(task_id="T-1", sender="agent_b", recipient="agent_a",
                   reason="太便宜了", reply_to=None,
                   trace_id="trc_1") -> Message:
    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.REJECT,
        trace_id=trace_id, reply_to=reply_to,
        payload={"task_id": task_id, "reason": reason},
    )


# ============================================================
# 1. ACCEPT：直接接受 TASK_OFFER（§9 情况 A）
# ============================================================
class TestAcceptTaskOffer:
    def test_accepts_at_budget(self, service, store):
        """§9：reply_to 指向 TASK_OFFER 时，成交价 = TASK_OFFER.budget。"""
        task = make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        result = service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a",
                           reply_to=offer.message_id)
        )

        assert result.ok is True
        assert result.action == "accepted"
        final = store.get(task.task_id)
        assert final.status == TaskStatus.ACCEPTED
        assert final.agreed_price == Money(100)

    def test_price_must_match_the_accepted_offer(self, service, store):
        """§14：成交价必须等于被接受那条报价的价格。"""
        make_task(store)
        offer = offer_message(budget=100)
        service.handle_offer(offer)

        result = service.handle_accept(
            accept_message(price=60, sender="agent_b", recipient="agent_a",
                           reply_to=offer.message_id)
        )

        assert result.ok is False
        assert any("不一致" in e for e in result.errors)

    def test_must_point_at_a_known_offer(self, service, store):
        """§18：成交必须能反查到具体是哪一条报价。"""
        make_task(store)
        service.handle_offer(offer_message())

        result = service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a",
                           reply_to="msg_根本不存在")
        )

        assert result.ok is False
        assert any("reply_to" in e for e in result.errors)

    def test_without_reply_to_rejected(self, service, store):
        make_task(store)
        service.handle_offer(offer_message())

        result = service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a")
        )

        assert result.ok is False

    def test_missing_price_rejected(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        message = accept_message(sender="agent_b", recipient="agent_a",
                                 reply_to=offer.message_id)
        del message.payload["price"]

        result = service.handle_accept(message)
        assert result.ok is False
        assert any("price" in e for e in result.errors)

    def test_bare_number_price_rejected(self, service, store):
        """协议 §7.1：金额不能是裸数字。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        message = Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.ACCEPT, reply_to=offer.message_id,
            payload={"task_id": "T-1", "price": 100},
        )
        result = service.handle_accept(message)

        assert result.ok is False
        assert any("裸数字" in e for e in result.errors)


# ============================================================
# 2. ACCEPT：接受 COUNTER_OFFER（§10 情况 B）
# ============================================================
class TestAcceptCounterOffer:
    def test_accepts_at_counter_price(self, service, store):
        """§10：reply_to 指向 COUNTER_OFFER 时，成交价 = COUNTER_OFFER.price。"""
        task = make_task(store)
        offer = offer_message(budget=100)
        service.handle_offer(offer)
        counter = counter_message(price=25, reply_to=offer.message_id)
        service.handle_counter_offer(counter)

        result = service.handle_accept(
            accept_message(price=25, reply_to=counter.message_id)
        )

        assert result.ok is True
        final = store.get(task.task_id)
        assert final.status == TaskStatus.ACCEPTED
        assert final.agreed_price == Money(25)

    def test_budget_and_agreed_price_stay_different(self, service, store):
        """§7：预算 100、成交 25 —— 两个数必须分别保留。"""
        make_task(store, budget=100)
        offer = offer_message(budget=100)
        service.handle_offer(offer)
        counter = counter_message(price=25, reply_to=offer.message_id)
        service.handle_counter_offer(counter)
        service.handle_accept(accept_message(price=25, reply_to=counter.message_id))

        final = store.get("T-1")
        assert final.budget == Money(100)
        assert final.agreed_price == Money(25)
        assert final.budget != final.agreed_price

    def test_counter_can_go_through_multiple_rounds(self, service, store):
        """§11：多轮还价，状态保持 NEGOTIATING。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        first = counter_message(price=60, reply_to=offer.message_id)
        service.handle_counter_offer(first)
        assert store.get("T-1").status == TaskStatus.NEGOTIATING

        second = counter_message(price=40, sender="agent_a", recipient="agent_b",
                                 reply_to=first.message_id)
        service.handle_counter_offer(second)

        third = counter_message(price=25, reply_to=second.message_id)
        service.handle_counter_offer(third)

        assert store.get("T-1").status == TaskStatus.NEGOTIATING
        assert [o.amount.amount for o in store.offers_for("T-1")] == [100, 60, 40, 25]

    def test_each_counter_links_its_message(self, service, store):
        """§11：每条还价都要关联 message_id / reply_to / trace / conversation。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        counter = counter_message(price=60, reply_to=offer.message_id)
        service.handle_counter_offer(counter)

        offers = store.offers_for("T-1")
        assert offers[1].message_id == counter.message_id
        assert offers[1].reply_to == offer.message_id
        assert offers[1].from_agent == "agent_b"
        assert offers[1].trace_id == "trc_1"
        assert offers[1].conversation_id == counter.conversation_id


# ============================================================
# 3. 价格规则（§14）
# ============================================================
class TestPriceRules:
    def test_counter_above_budget_rejected(self, service, store):
        """§14 / §15：还价超过预算 → Task 层判定无效，状态保持合理。"""
        make_task(store, budget=100)
        offer = offer_message(budget=100)
        service.handle_offer(offer)

        before = store.get("T-1").status
        result = service.handle_counter_offer(
            counter_message(price=150, reply_to=offer.message_id)
        )

        assert result.ok is False
        assert any("预算" in e for e in result.errors)
        assert store.get("T-1").status == before     # 状态没被改坏
        assert len(store.offers_for("T-1")) == 1     # 无效报价没进历史

    def test_counter_equal_to_budget_allowed(self, service, store):
        make_task(store, budget=100)
        offer = offer_message(budget=100)
        service.handle_offer(offer)

        result = service.handle_counter_offer(
            counter_message(price=100, reply_to=offer.message_id)
        )
        assert result.ok is True

    def test_currency_must_be_aic(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        message = Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.COUNTER_OFFER, reply_to=offer.message_id,
            payload={"task_id": "T-1",
                     "price": {"amount": 60, "currency": "USD"}},
        )
        result = service.handle_counter_offer(message)

        assert result.ok is False
        assert any("AIC" in e for e in result.errors)

    def test_negative_price_rejected(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        result = service.handle_counter_offer(
            counter_message(price=-5, reply_to=offer.message_id)
        )
        assert result.ok is False


# ============================================================
# 4. REJECT（§12 / §13）
# ============================================================
class TestReject:
    def test_reject_from_offered(self, service, store):
        """§12：OFFERED → REJECTED"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        result = service.handle_reject(
            reject_message(reply_to=offer.message_id)
        )

        assert result.ok is True
        assert store.get("T-1").status == TaskStatus.REJECTED

    def test_reject_from_negotiating(self, service, store):
        """§12：NEGOTIATING → REJECTED"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        counter = counter_message(price=60, reply_to=offer.message_id)
        service.handle_counter_offer(counter)

        # 【方向很重要】B 刚还过价，所以轮到 A 回应 ——
        # 拒绝也必须由 A 发出（行动方 = 消息的发送者）
        service.handle_reject(
            reject_message(sender="agent_a", recipient="agent_b",
                           reply_to=counter.message_id)
        )

        assert store.get("T-1").status == TaskStatus.REJECTED

    def test_reject_is_not_an_error(self, service, store):
        """
        §13：拒绝是**正常业务行为**，不是协议错误，也不是通信失败。

        所以 ok=True（业务上处理成功了），动作是 rejected。
        """
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        result = service.handle_reject(reject_message(reply_to=offer.message_id))

        assert result.ok is True
        assert result.action == "rejected"
        assert result.reply_type is None        # 拒绝不需要再回一条

    def test_reject_records_nothing_economic(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        result = service.handle_reject(reject_message(reply_to=offer.message_id))

        assert result.details["payment"] is None
        assert result.details["workspace"] is None

    def test_reject_reason_recorded(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        service.handle_reject(
            reject_message(reason="预算太低", reply_to=offer.message_id)
        )

        assert "预算太低" in store.get("T-1").note

    def test_reject_enters_offer_history(self, service, store):
        """拒绝也是业务行为，要留在历史里可审计。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_reject(reject_message(reply_to=offer.message_id))

        types = [o.offer_type for o in store.offers_for("T-1")]
        assert OfferType.REJECT in types

    def test_reject_is_idempotent(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_reject(reject_message(reply_to=offer.message_id))

        again = service.handle_reject(reject_message(reply_to=offer.message_id))

        assert again.ok is True
        assert again.action == "already_rejected"


class TestBusinessRejectVsProtocolError:
    """
    §13：必须严格区分「业务拒绝」和「协议错误」。

        业务拒绝：价格不合适 → REJECT，任务变成 REJECTED
        协议错误：还价超预算 → 那条消息**无效**，任务状态不变
    """

    def test_business_reject_changes_task_state(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_reject(reject_message(reply_to=offer.message_id))

        assert store.get("T-1").status == TaskStatus.REJECTED

    def test_protocol_error_does_not_change_task_state(self, service, store):
        make_task(store, budget=100)
        offer = offer_message(budget=100)
        service.handle_offer(offer)
        status_before = store.get("T-1").status

        result = service.handle_counter_offer(
            counter_message(price=150, reply_to=offer.message_id)
        )

        assert result.ok is False
        assert store.get("T-1").status == status_before
        assert store.get("T-1").status != TaskStatus.REJECTED

    def test_protocol_error_replies_with_error_not_reject(self, service, store):
        """协议错误回 ERROR；业务拒绝回 REJECT（或什么都不回）。"""
        make_task(store, budget=100)
        offer = offer_message(budget=100)
        service.handle_offer(offer)

        protocol_error = service.handle_counter_offer(
            counter_message(price=150, reply_to=offer.message_id)
        )
        assert protocol_error.reply_type == MessageType.ERROR

    def test_business_reject_uses_reject_type(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        business_reject = service.handle_reject(
            reject_message(reply_to=offer.message_id)
        )
        assert business_reject.reply_type != MessageType.ERROR


# ============================================================
# 5. 角色 / 轮次验证（§16 / §17）
# ============================================================
class TestRoleAndTurnValidation:
    def test_non_participant_cannot_accept(self, service, store):
        """§16：Agent C 不能替一个它没参与的任务做决定。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        result = service.handle_accept(
            accept_message(price=100, sender="agent_c", recipient="agent_b",
                           reply_to=offer.message_id)
        )

        assert result.ok is False
        assert any("参与方" in e for e in result.errors)

    def test_non_participant_cannot_counter(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        result = service.handle_counter_offer(
            counter_message(price=60, sender="agent_c", recipient="agent_b",
                            reply_to=offer.message_id)
        )

        assert result.ok is False
        assert any("参与方" in e for e in result.errors)

    def test_cannot_respond_to_your_own_offer(self, service, store):
        """
        §16：不能自己回自己 ——
        否则一个人可以代表双方签合同。
        """
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        # 最后一条报价（TASK_OFFER）是 agent_a 自己发的
        # 现在让 agent_a 装作收到了它并要回应
        result = service.handle_counter_offer(
            counter_message(price=60, sender="agent_a", recipient="agent_b",
                            reply_to=offer.message_id)
        )

        assert result.ok is False
        assert any("轮次" in e for e in result.errors)

    def test_turn_alternates_correctly(self, service, store):
        """正常的轮流报价不会触发轮次校验。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        first = counter_message(price=60, sender="agent_b", recipient="agent_a",
                                reply_to=offer.message_id)
        assert service.handle_counter_offer(first).ok is True

        second = counter_message(price=40, sender="agent_a", recipient="agent_b",
                                 reply_to=first.message_id)
        assert service.handle_counter_offer(second).ok is True

    def test_cannot_respond_to_terminal_task(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_reject(reject_message(reply_to=offer.message_id))

        result = service.handle_counter_offer(
            counter_message(price=60, reply_to=offer.message_id)
        )

        assert result.ok is False
        assert any("终态" in e for e in result.errors)

    def test_validation_has_no_hardcoded_agent_names(self):
        """
        `services/` 里不许出现**任何真实 Agent 的名字**。

        🔴 这条守卫原来有两个盲区，`ARTIFACT_NAMESPACES` 就是这么漏过去的
           （它曾经住在 `services/artifacts.py`）：

             ① 只看 `ast.Compare`（比较语句）
                → 字典 / 列表 / 函数默认参数里的名字**全都漏**
             ② 只认 `"agent_"` 前缀
                → 守卫写的时候只有 agent_a…agent_e；
                  后来为小说实验加的 `novel_*` 那一批**完全没被覆盖**，
                  而且它**静默失效**（守卫一直绿）。

        现在的判据：
          · 扫描 `services/**/*.py` 的**全部字符串常量**
            （注释不进 AST，天然排除；docstring 显式排除）
          · 与 `config/config.yaml` 里**真实存在的 agent id 集合**比对
            —— 不再依赖任何前缀假设，新增 Agent 自动被覆盖
        """
        import ast
        from pathlib import Path

        from core.config_resolver import load_resolved_config
        from core.paths import GLOBAL_CONFIG_FILE
        from llm.router import adapter_specs

        services_dir = Path(__file__).resolve().parent.parent / "services"

        # 🔑 判据来自**配置里的真实 Agent 集合**，不是一个人的前缀猜测
        resolved = load_resolved_config(GLOBAL_CONFIG_FILE,
                                        adapter_specs=adapter_specs)
        agent_ids = set(resolved.agents)
        assert agent_ids, "配置里应当有 agents —— 否则这条守卫会空转"

        def _docstring_nodes(tree):
            """收集 docstring 的 Constant 节点 —— 说明文字里提到名字是允许的。"""
            out = set()
            for node in ast.walk(tree):
                for attr in ("body",):
                    body = getattr(node, attr, None)
                    if not isinstance(body, list) or not body:
                        continue
                    first = body[0]
                    if (isinstance(first, ast.Expr)
                            and isinstance(first.value, ast.Constant)
                            and isinstance(first.value.value, str)):
                        out.add(id(first.value))
            return out

        problems = []
        for source in sorted(services_dir.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            skip = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and id(node) not in skip
                        and node.value in agent_ids):
                    problems.append(f"{source.name}:{node.lineno} {node.value!r}")

        assert not problems, (
            "services/ 里出现了硬编码的 Agent 名：\n  "
            + "\n  ".join(problems)
            + "\n（生产层只提供机制，不内建某次实验的分工 / 参与者）"
        )



# ============================================================
# 6. 成交凭证（§19）
# ============================================================
class TestAgreement:
    def test_created_on_accept(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        counter = counter_message(price=25, reply_to=offer.message_id)
        service.handle_counter_offer(counter)
        service.handle_accept(accept_message(price=25, reply_to=counter.message_id))

        agreements = store.agreements_for("T-1")
        assert len(agreements) == 1

        agreement = agreements[0]
        assert agreement.initiator == "agent_a"
        assert agreement.contractor == "agent_b"
        assert agreement.agreed_price == Money(25)

    def test_records_the_accepted_offer(self, service, store):
        """§18：必须能反查到「最终接受的是哪一条报价」。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        counter = counter_message(price=25, reply_to=offer.message_id)
        service.handle_counter_offer(counter)
        service.handle_accept(accept_message(price=25, reply_to=counter.message_id))

        agreement = store.latest_agreement("T-1")
        assert agreement.accepted_offer_message_id == counter.message_id

    def test_records_trace_and_conversation(self, service, store):
        make_task(store)
        offer = offer_message(trace_id="trc_special")
        service.handle_offer(offer)
        counter = counter_message(price=25, reply_to=offer.message_id,
                                  trace_id="trc_special")
        service.handle_counter_offer(counter)
        service.handle_accept(
            accept_message(price=25, reply_to=counter.message_id,
                           trace_id="trc_special")
        )

        agreement = store.latest_agreement("T-1")
        assert agreement.trace_id == "trc_special"
        assert agreement.conversation_id is not None

    def test_is_not_a_payment(self, service, store):
        """§19 / §20：凭证明确说明它不是支付记录。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a",
                           reply_to=offer.message_id)
        )

        info = store.latest_agreement("T-1").describe()
        assert info["is_payment"] is False

    def test_immutable(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a",
                           reply_to=offer.message_id)
        )

        agreement = store.latest_agreement("T-1")
        with pytest.raises(Exception):
            agreement.agreed_price = Money(1)

    def test_duplicate_write_is_idempotent(self, service, store):
        """两边各自生成凭证时，ID 相同 → 只留一条。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a",
                           reply_to=offer.message_id)
        )

        agreement = store.latest_agreement("T-1")
        store.record_agreement(agreement)

        assert store.count_agreements() == 1

    def test_rejects_same_initiator_and_contractor(self):
        with pytest.raises(Exception):
            TaskAgreementRecord(
                task_id="T-1", initiator="agent_a", contractor="agent_a",
                agreed_price=Money(25),
            )

    def test_roundtrip(self):
        original = TaskAgreementRecord(
            task_id="T-1", initiator="agent_a", contractor="agent_b",
            agreed_price=Money(25), accepted_offer_message_id="msg_x",
            trace_id="trc_x", conversation_id="conv_x",
        )
        restored = TaskAgreementRecord.from_dict(original.to_dict())

        assert restored.agreement_id == original.agreement_id
        assert restored.agreed_price == original.agreed_price
        assert restored.accepted_offer_message_id == "msg_x"


# ============================================================
# 7. 决策编排（策略 → 执行）
# ============================================================
class TestDecisionOrchestration:
    def test_decide_does_not_change_state(self, service, store):
        """§8：决策不修改任何状态。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        counter = counter_message(price=60, reply_to=offer.message_id)
        service.handle_counter_offer(counter)

        before_status = store.get("T-1").status
        before_offers = len(store.offers_for("T-1"))

        service.decide(counter_message(price=60, reply_to=offer.message_id,
                                       sender="agent_b", recipient="agent_a"))

        assert store.get("T-1").status == before_status
        assert len(store.offers_for("T-1")) == before_offers

    def test_respond_executes_the_decision(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        # A 收到 B 的还价 60，A 的策略（接受线 50）会还价 40
        counter = counter_message(price=60, reply_to=offer.message_id)
        service.handle_counter_offer(counter)

        incoming = counter_message(price=60, sender="agent_b", recipient="agent_a",
                                   reply_to=offer.message_id)
        # 让「我收到的那条」的 message_id 与历史里那条一致
        incoming.message_id = counter.message_id

        result = service.respond(incoming)
        assert result.ok is True
        assert result.action == "countered"

    def test_respond_without_policy_raises(self, store, directory):
        """没配置策略的 Agent 收到报价时**明确报错**，而不是被塞个默认策略。"""
        service = TaskService(store=store, directory=directory, policies={})
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        with pytest.raises(Exception, match="DecisionPolicy"):
            service.decide(counter_message(price=60, reply_to=offer.message_id))

    def test_execute_rejects_wrong_type(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)

        with pytest.raises(TypeError):
            service.execute(counter_message(reply_to=offer.message_id), "不是决策")

    def test_policy_for(self, service):
        assert service.policy_for("agent_a") is not None
        assert service.policy_for("agent_b") is not None
        assert service.policy_for("agent_不存在") is None


# ============================================================
# 8. 隔离性：成交不碰钱、不碰 Workspace
# ============================================================
class TestIsolation:
    def test_accept_moves_no_money(self, db, economy, funded_agents, directory,
                                   policies):
        """
        【§20 的核心回归测试】

        ACCEPT 只意味着合同成立 —— 钱包和账本一个字节都不该动。
        """
        store = TaskStore(economy.db)
        service = TaskService(store, directory, policies)

        ledger_before = economy.ledger.count()
        balance_a = economy.agent("agent_a").balance
        balance_b = economy.agent("agent_b").balance

        task = Task.create(task_id="T-ISO", creator="agent_a",
                           budget=Money(100), participants=["agent_a", "agent_b"])
        task.offer()
        store.save(task)

        offer = offer_message("T-ISO", budget=100)
        service.handle_offer(offer)
        counter = counter_message("T-ISO", price=25, reply_to=offer.message_id)
        service.handle_counter_offer(counter)
        result = service.handle_accept(
            accept_message("T-ISO", price=25, reply_to=counter.message_id)
        )

        assert result.ok is True
        assert store.get("T-ISO").status == TaskStatus.ACCEPTED
        # 钱一分没动
        assert economy.ledger.count() == ledger_before
        assert economy.agent("agent_a").balance == balance_a
        assert economy.agent("agent_b").balance == balance_b
        assert economy.audit()["balanced"] is True

    def test_accept_grants_no_workspace(self, service, store):
        """§21 / §22：成交 ≠ 获得 Workspace 权限。"""
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a",
                           reply_to=offer.message_id)
        )

        final = store.get("T-1")
        assert final.workspace_id == "WS-001"       # 只是引用
        for forbidden in ("read_file", "write_file", "open_workspace", "grant"):
            assert not hasattr(final, forbidden)
            assert not hasattr(store, forbidden)

    def test_accept_details_say_no_side_effects(self, service, store):
        make_task(store)
        offer = offer_message()
        service.handle_offer(offer)
        result = service.handle_accept(
            accept_message(price=100, sender="agent_b", recipient="agent_a",
                           reply_to=offer.message_id)
        )

        assert result.details["payment"] is None
        assert result.details["workspace"] is None
        assert result.details["llm_calls"] == 0

    def test_reject_moves_no_money(self, db, economy, funded_agents, directory,
                                   policies):
        store = TaskStore(economy.db)
        service = TaskService(store, directory, policies)
        ledger_before = economy.ledger.count()

        task = Task.create(task_id="T-REJ", creator="agent_a",
                           budget=Money(100), participants=["agent_a", "agent_b"])
        task.offer()
        store.save(task)

        offer = offer_message("T-REJ", budget=100)
        service.handle_offer(offer)
        service.handle_reject(reject_message("T-REJ", reply_to=offer.message_id))

        assert economy.ledger.count() == ledger_before
        assert economy.audit()["balanced"] is True
