"""
Test: Decision Policy —— 低成本经济决策（Step 4C）
============================================================

【这一组测试在守什么】

    Step 4C 的核心设计理念是：

        **不能把算力浪费在谈判上。**

    一次任务可能来回十几轮报价。如果每轮都调大模型，
    谈判成本可能高于任务本身的价值。

    所以这里测的不只是「规则对不对」，还有：

        · 它**完全不调用 LLM**（整个文件不 import llm）
        · 它**没有副作用**（evaluate 不改任何状态）
        · 它**确定性**（同样输入 → 同样输出）
        · 它**不认 Agent 名字**（同一组规则对任何 Agent 都成立）
"""

from __future__ import annotations

import pytest

from core.decision_policy import (
    Decision,
    DecisionContext,
    DecisionError,
    DecisionPolicy,
    DecisionType,
    InvalidDecisionError,
    RuleBasedDecisionPolicy,
)
from core.money import Money


# ============================================================
# 夹具
# ============================================================
def context(
    offered: float,
    *,
    is_initiator: bool = False,
    budget: float | None = 100,
    negotiation_round: int = 0,
    max_rounds: int = 2,
    agent_id: str = "agent_x",
    message_type: str = "TASK_OFFER",
) -> DecisionContext:
    return DecisionContext(
        agent_id=agent_id,
        task_id="T-1",
        offered_price=Money(offered),
        is_initiator=is_initiator,
        budget=Money(budget) if budget is not None else None,
        negotiation_round=negotiation_round,
        max_rounds=max_rounds,
        message_type=message_type,
    )


@pytest.fixture
def seller() -> RuleBasedDecisionPolicy:
    """卖方（承接方）：越低越好，接受线 50，开口 60，底线 25。"""
    return RuleBasedDecisionPolicy(
        accept_price=50, opening_ask=60, walk_away_price=25, max_rounds=2
    )


@pytest.fixture
def buyer() -> RuleBasedDecisionPolicy:
    """买方（发起方）：越高越差，接受线 50（<=50 就付），开口 40。"""
    return RuleBasedDecisionPolicy(
        accept_price=50, opening_ask=40, walk_away_price=100, max_rounds=2
    )


# ============================================================
# 1. Decision 对象
# ============================================================
class TestDecision:
    def test_accept_requires_price(self):
        with pytest.raises(InvalidDecisionError):
            Decision(decision_type=DecisionType.ACCEPT)

    def test_counter_requires_price(self):
        with pytest.raises(InvalidDecisionError):
            Decision(decision_type=DecisionType.COUNTER_OFFER)

    def test_reject_must_not_carry_price(self):
        """拒绝就是不谈了 —— 带个价格会让下游不知道该不该用它。"""
        with pytest.raises(InvalidDecisionError):
            Decision(decision_type=DecisionType.REJECT, price=Money(10))

    def test_unknown_type_rejected(self):
        with pytest.raises(InvalidDecisionError):
            Decision(decision_type="MAYBE")

    def test_price_must_be_money(self):
        with pytest.raises(InvalidDecisionError):
            Decision(decision_type=DecisionType.ACCEPT, price=25)

    def test_immutable(self):
        decision = Decision(decision_type=DecisionType.REJECT)
        with pytest.raises(Exception):
            decision.decision_type = DecisionType.ACCEPT

    def test_predicates(self):
        assert Decision(decision_type=DecisionType.REJECT).rejected is True
        assert Decision(
            decision_type=DecisionType.ACCEPT, price=Money(1)
        ).accepted is True
        assert Decision(
            decision_type=DecisionType.COUNTER_OFFER, price=Money(1)
        ).countered is True

    def test_decision_cost_reserved(self):
        """
        §24：必须为未来的成本核算留字段，但当前不扣费。
        """
        decision = Decision(
            decision_type=DecisionType.ACCEPT, price=Money(25)
        )

        assert decision.decision_cost is None

    def test_serialization(self):
        decision = Decision(
            decision_type=DecisionType.COUNTER_OFFER,
            price=Money(60), reason="r", policy="p",
        )
        data = decision.to_dict()

        assert data["decision_type"] == "COUNTER_OFFER"
        assert data["price"] == {"amount": 60, "currency": "AIC"}
        assert data["decision_cost"] is None

    def test_str(self):
        assert "ACCEPT" in str(
            Decision(decision_type=DecisionType.ACCEPT, price=Money(25))
        )


# ============================================================
# 2. DecisionContext
# ============================================================
class TestDecisionContext:
    def test_side_is_derived_from_task_role_not_agent_id(self):
        """
        【§17】买方还是卖方，由**任务关系**决定，不是由 agent_id 决定。

        所以同一个 agent_id 在不同任务里可能是买方也可能是卖方。
        """
        as_buyer = context(50, is_initiator=True, agent_id="whoever")
        as_seller = context(50, is_initiator=False, agent_id="whoever")

        assert as_buyer.side == "buyer"
        assert as_seller.side == "seller"

    def test_rounds_exhausted(self):
        assert context(10, negotiation_round=1, max_rounds=2).rounds_exhausted is False
        assert context(10, negotiation_round=2, max_rounds=2).rounds_exhausted is True

    def test_price_must_be_money(self):
        with pytest.raises(DecisionError):
            DecisionContext(
                agent_id="a", task_id="t", offered_price=100,
                is_initiator=False,
            )

    def test_serialization(self):
        data = context(60, is_initiator=False).to_dict()

        assert data["side"] == "seller"
        assert data["offered_price"] == {"amount": 60, "currency": "AIC"}

    def test_future_cost_fields_reserved(self):
        """§34：不能让接口阻止未来加入成本信息。"""
        ctx = context(60)

        assert ctx.estimated_task_cost is None
        assert ctx.current_workload is None
        assert ctx.negotiation_cost is None


# ============================================================
# 3. RuleBasedDecisionPolicy 的规则
# ============================================================
class TestRuleBasedPolicy:
    def test_interface_is_abstract(self):
        with pytest.raises(TypeError):
            DecisionPolicy()

    def test_accepts_when_offer_meets_line_after_round(self, seller):
        """第二轮起，报价达到接受线就接受。"""
        decision = seller.evaluate(
            context(60, is_initiator=False, negotiation_round=1)
        )

        assert decision.accepted is True
        assert decision.price == Money(60)

    def test_does_not_accept_the_first_offer_by_default(self, seller):
        """
        【默认不接受第一个报价】

            因为 TASK_OFFER 里的 budget 是**上限**，不是报价。
            卖方收到 100 的预算，应该先报出自己的价格（60），
            而不是直接按 100 成交。
        """
        decision = seller.evaluate(
            context(100, is_initiator=False, negotiation_round=0)
        )

        assert decision.countered is True
        assert decision.price == Money(60)

    def test_accept_first_offer_when_configured(self):
        policy = RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=60, walk_away_price=25,
            max_rounds=2, accept_first_offer=True,
        )
        decision = policy.evaluate(
            context(80, is_initiator=False, negotiation_round=0)
        )

        assert decision.accepted is True
        assert decision.price == Money(80)

    def test_rejects_below_walk_away(self, seller):
        """低于底线直接拒绝，不浪费时间讨价还价。"""
        decision = seller.evaluate(
            context(10, is_initiator=False, negotiation_round=0)
        )

        assert decision.rejected is True
        assert "底线" in decision.reason

    def test_rejects_when_rounds_exhausted(self, seller):
        """§30：超过 max_rounds 不再无限还价。"""
        decision = seller.evaluate(
            context(40, is_initiator=False, negotiation_round=2, max_rounds=2)
        )

        assert decision.rejected is True
        assert "还价" in decision.reason

    def test_buyer_direction_is_reversed(self, buyer):
        """买方：报价越低越好。"""
        assert buyer.evaluate(
            context(30, is_initiator=True, negotiation_round=1)
        ).accepted is True

        # 报价太高（超过买方的底线 100 不可能，用 walk_away 判断）
        high = RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=40, walk_away_price=45, max_rounds=2
        )
        decision = high.evaluate(context(60, is_initiator=True))
        assert decision.rejected is True

    def test_concession_converges_to_walk_away(self, seller):
        """还价从开口价线性让步到底线，不会原地打转。"""
        assert seller.current_ask(0) == Money(60)
        assert seller.current_ask(1) == Money(25)     # max_rounds=2 → 最后一轮到底线

    def test_three_round_concession(self):
        policy = RuleBasedDecisionPolicy(
            accept_price=10, opening_ask=100, walk_away_price=40, max_rounds=3
        )
        asks = [policy.current_ask(i) for i in range(3)]

        assert asks[0] == Money(100)
        assert asks[1] == Money(70)
        assert asks[2] == Money(40)

    def test_counter_is_clamped_to_budget(self):
        """§14：还价不能超过预算。"""
        policy = RuleBasedDecisionPolicy(
            accept_price=10, opening_ask=200, walk_away_price=20, max_rounds=2
        )
        decision = policy.evaluate(
            context(50, is_initiator=False, budget=80, negotiation_round=0)
        )

        assert decision.countered is True
        assert decision.price == Money(80)      # 被预算夹住了
        assert decision.price <= Money(80)

    def test_currency_mismatch_rejects(self, seller):
        ctx = DecisionContext(
            agent_id="a", task_id="t",
            offered_price=Money(60, "USD"),
            is_initiator=False,
        )
        decision = seller.evaluate(ctx)

        assert decision.rejected is True
        assert "货币" in decision.reason

    def test_policy_name_recorded(self, seller):
        decision = seller.evaluate(context(10, is_initiator=False))

        assert decision.policy == "RuleBasedDecisionPolicy"


# ============================================================
# 4. 确定性、无副作用、不调模型
# ============================================================
class TestPolicyProperties:
    def test_deterministic(self, seller):
        """同样输入必须给同样输出 —— 否则测试不稳定，也没法重放。"""
        ctx = context(40, is_initiator=False, negotiation_round=0)

        first = seller.evaluate(ctx)
        second = seller.evaluate(ctx)

        assert first == second

    def test_no_side_effects(self, seller):
        """evaluate 不改上下文、不改策略本身。"""
        ctx = context(40, is_initiator=False, negotiation_round=0)
        before = ctx.to_dict()

        seller.evaluate(ctx)

        assert ctx.to_dict() == before
        assert seller.accept_price == Money(50)
        assert seller.max_rounds == 2

    def test_does_not_import_llm(self):
        """整个决策模块不许依赖 llm —— 这是本步骤的核心约束。"""
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "core" / "decision_policy.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)

        for module in modules:
            assert not module.startswith("llm"), (
                f"core/decision_policy.py 导入了 {module}"
            )

    def test_describe_says_no_llm(self, seller):
        info = seller.describe()

        assert info["calls_llm"] is False
        assert info["accept_price"] == "50 AIC"

    def test_policy_has_no_state_changing_methods(self, seller):
        """策略上不该有「改状态」或「发消息」的方法。"""
        for forbidden in ("apply", "execute", "mutate", "send", "transfer",
                          "grant", "save", "write"):
            assert not hasattr(seller, forbidden), (
                f"策略不该有 {forbidden!r} —— 它只返回决策，不执行"
            )


# ============================================================
# 5. 从配置构造（不硬编码 Agent）
# ============================================================
class TestFromConfig:
    def test_from_config(self):
        policy = RuleBasedDecisionPolicy.from_config({
            "accept_price": 50,
            "opening_ask": 60,
            "walk_away_price": 25,
            "max_rounds": 2,
        })

        assert policy.accept_price == Money(50)
        assert policy.opening_ask == Money(60)
        assert policy.walk_away_price == Money(25)
        assert policy.max_rounds == 2

    def test_from_config_requires_accept_price(self):
        with pytest.raises(DecisionError, match="accept_price"):
            RuleBasedDecisionPolicy.from_config({})

    def test_from_config_defaults(self):
        policy = RuleBasedDecisionPolicy.from_config({"accept_price": 50})

        assert policy.opening_ask == Money(50)       # 没给就用接受线
        assert policy.walk_away_price == Money(50)
        assert policy.max_rounds == 3

    def test_two_agents_same_class_different_params(self):
        """
        【§6 的核心】同一个策略类，不同 Agent 传不同参数即可 ——
        代码里不需要（也不允许）出现 Agent 的名字。
        """
        b = RuleBasedDecisionPolicy.from_config({"accept_price": 20})
        c = RuleBasedDecisionPolicy.from_config({"accept_price": 50})

        assert b.accept_price == Money(20)
        assert c.accept_price == Money(50)
        assert type(b) is type(c)

    def test_currency_from_config(self):
        policy = RuleBasedDecisionPolicy.from_config(
            {"accept_price": 20, "currency": "AIC"}
        )
        assert policy.accept_price.currency == "AIC"

    def test_invalid_max_rounds(self):
        with pytest.raises(DecisionError):
            RuleBasedDecisionPolicy(accept_price=10, max_rounds=0)

    def test_max_rounds_must_be_int(self):
        with pytest.raises(DecisionError):
            RuleBasedDecisionPolicy(accept_price=10, max_rounds=2.5)

    def test_accepts_money_object(self):
        policy = RuleBasedDecisionPolicy(accept_price=Money(20, "AIC"))

        assert policy.accept_price == Money(20)


# ============================================================
# 6. 现实中真实存在的两套参数
# ============================================================
class TestRealConfigurations:
    def test_buyer_and_seller_converge(self):
        """
        把 demo 里那两个策略各跑几轮，确认它们最终能谈拢。

        这是「规则不是空转」的端到端证明。
        """
        buyer = RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=40, walk_away_price=100, max_rounds=2
        )
        seller = RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=60, walk_away_price=25, max_rounds=2
        )

        offer = 100.0
        buyer_rounds = seller_rounds = 0
        transcript = []

        for _ in range(10):
            # 卖方回应
            decision = seller.evaluate(context(
                offer, is_initiator=False, negotiation_round=seller_rounds
            ))
            transcript.append(("seller", decision))
            if decision.accepted or decision.rejected:
                break
            offer = decision.price.amount
            seller_rounds += 1

            # 买方回应
            decision = buyer.evaluate(context(
                offer, is_initiator=True, negotiation_round=buyer_rounds
            ))
            transcript.append(("buyer", decision))
            if decision.accepted or decision.rejected:
                break
            offer = decision.price.amount
            buyer_rounds += 1

        kinds = [d.decision_type for _, d in transcript]
        assert kinds[-1] == DecisionType.ACCEPT, (
            f"两个策略应该能谈拢，实际：{kinds}"
        )
