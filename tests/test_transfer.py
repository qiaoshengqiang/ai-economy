"""
Test: Transfer（Agent 之间的转账入口）
============================================================

这个文件验证 Economy.transfer() —— 也就是让一个 Agent
向另一个 Agent 付钱的那个「OS 级执行入口」。

它为什么重要？
    以后 LLM 说「我决定支付 Agent B 20 AI」时，
    这句话最终必须落到这个方法上。
    所以这里的每一条校验规则，都是未来经济系统的护栏。

验证目标：
    1. 正常转账正确改动双方余额
    2. 每一次转账都在账本留痕
    3. 各种非法转账被拒绝（余额不足 / 自己转自己 / 负数 / 破产）
    4. 被拒绝时账本和余额都不留任何痕迹（原子性）
    5. Agent 名字/简写能正确解析
    6. 收款方破产后收钱能复活
"""

from __future__ import annotations

import pytest

from core.economy import AgentNotFoundError, EconomyError
from core.ledger import InsufficientFundsError


# ============================================================
# 正常转账
# ============================================================
def test_transfer_moves_money(funded_agents):
    economy, agent_a, agent_b = funded_agents

    economy.transfer("agent_a", "agent_b", 20)

    assert agent_a.balance == 80
    assert agent_b.balance == 120


def test_transfer_returns_transaction(funded_agents):
    economy, _, _ = funded_agents

    tx = economy.transfer("agent_a", "agent_b", 20, reason="task_payment")

    assert tx.tx_type == "TRANSFER"
    assert tx.from_agent == "agent_a"
    assert tx.to_agent == "agent_b"
    assert tx.amount == 20
    assert tx.reason == "task_payment"
    assert tx.tx_id.startswith("tx_")


def test_transfer_is_written_to_ledger(funded_agents):
    economy, _, _ = funded_agents

    tx = economy.transfer("agent_a", "agent_b", 20)

    history = economy.ledger.history(agent_id="agent_a")
    assert any(t.tx_id == tx.tx_id for t in history)


def test_transfer_can_carry_task_id(funded_agents):
    economy, _, _ = funded_agents

    economy.transfer("agent_a", "agent_b", 20, task_id="task_001")

    history = economy.ledger.history(task_id="task_001")
    assert len(history) == 1
    assert history[0].amount == 20


def test_transfer_keeps_total_supply_constant(funded_agents):
    """
    转账不创造货币。总量必须不变。
    """
    economy, _, _ = funded_agents

    before = economy.ledger.total_supply()
    economy.transfer("agent_a", "agent_b", 37.5)
    after = economy.ledger.total_supply()

    assert before == after
    assert economy.audit()["balanced"] is True


def test_transfer_back_and_forth(funded_agents):
    """来回转账后余额应该正确。"""
    economy, agent_a, agent_b = funded_agents

    economy.transfer("agent_a", "agent_b", 30)
    economy.transfer("agent_b", "agent_a", 10)
    economy.transfer("agent_a", "agent_b", 5.5)

    assert agent_a.balance == pytest.approx(100 - 30 + 10 - 5.5)
    assert agent_b.balance == pytest.approx(100 + 30 - 10 + 5.5)


def test_transfer_fractional_amount(funded_agents):
    """支持小数金额（LLM 成本会是 2.8 这种数）。"""
    economy, agent_a, agent_b = funded_agents

    economy.transfer("agent_a", "agent_b", 2.8)

    assert agent_a.balance == pytest.approx(97.2)
    assert agent_b.balance == pytest.approx(102.8)


def test_transfer_can_empty_the_wallet(funded_agents):
    """刚好把余额全部转走是允许的。"""
    economy, agent_a, agent_b = funded_agents

    economy.transfer("agent_a", "agent_b", 100)

    assert agent_a.balance == 0
    assert agent_b.balance == 200


# ============================================================
# 拒绝非法转账
# ============================================================
def test_reject_insufficient_funds(funded_agents):
    economy, agent_a, agent_b = funded_agents

    with pytest.raises(InsufficientFundsError):
        economy.transfer("agent_a", "agent_b", 100.01)


def test_reject_self_transfer(funded_agents):
    economy, _, _ = funded_agents

    with pytest.raises(EconomyError):
        economy.transfer("agent_a", "agent_a", 10)


def test_reject_negative_amount(funded_agents):
    """
    【安全关键】负数金额必须被拒绝。

    否则 `transfer(a, b, -100)` 就等于从 B 偷 100 给 A。
    """
    economy, agent_a, agent_b = funded_agents

    with pytest.raises(EconomyError):
        economy.transfer("agent_a", "agent_b", -100)

    assert agent_a.balance == 100
    assert agent_b.balance == 100


def test_reject_zero_amount(funded_agents):
    economy, _, _ = funded_agents

    with pytest.raises(EconomyError):
        economy.transfer("agent_a", "agent_b", 0)


def test_reject_non_numeric_amount(funded_agents):
    economy, _, _ = funded_agents

    with pytest.raises(EconomyError):
        economy.transfer("agent_a", "agent_b", "一百块")  # type: ignore[arg-type]


def test_reject_nan_amount(funded_agents):
    economy, _, _ = funded_agents

    with pytest.raises(EconomyError):
        economy.transfer("agent_a", "agent_b", float("nan"))


def test_reject_unknown_agent(funded_agents):
    economy, _, _ = funded_agents

    with pytest.raises(AgentNotFoundError):
        economy.transfer("agent_不存在", "agent_b", 10)

    with pytest.raises(AgentNotFoundError):
        economy.transfer("agent_a", "agent_不存在", 10)


def test_reject_transfer_from_insolvent_agent(economy):
    """
    破产的 Agent 不能付钱 —— 这是「禁止它继续承担
    无法支付的经济责任」的具体落实。
    """
    economy.create_agent("agent_a")
    economy.create_agent("agent_b")
    economy.create_agent("agent_broke", mint=False)

    with pytest.raises(EconomyError):
        economy.transfer("agent_broke", "agent_a", 10)


def test_rejected_transfer_leaves_no_trace(funded_agents):
    """
    【原子性】被拒绝的转账不能在账本里留下任何记录。

    这是个容易出错的地方：如果先写了交易再校验余额，
    就会留下一笔「幽灵交易」把账搞乱。
    """
    economy, agent_a, agent_b = funded_agents

    count_before = economy.ledger.count()

    with pytest.raises(InsufficientFundsError):
        economy.transfer("agent_a", "agent_b", 99999)

    assert economy.ledger.count() == count_before
    assert agent_a.balance == 100
    assert agent_b.balance == 100
    assert economy.audit()["balanced"] is True


def test_insolvent_agent_can_receive_money(economy):
    """
    破产的 Agent 不能付钱，但可以收钱。
    而且收到钱后应该从 INSOLVENT 复活成 ACTIVE。

    注意这里给 agent_a 显式指定初始余额，这样测试不受
    config/config.yaml 里 initial_balance 的影响 ——
    否则你把余额调成 7，这个测试就会因为「付不起 50」而失败。
    """
    economy.create_agent("agent_a", initial_balance=100)
    economy.create_agent("agent_broke", mint=False)

    broke = economy.agent("agent_broke")
    assert broke.is_insolvent is True
    assert broke.status == "INSOLVENT"

    # A 给破产的 agent_broke 转 50
    economy.transfer("agent_a", "agent_broke", 50)

    assert broke.balance == 50
    assert broke.is_insolvent is False
    assert broke.status == "ACTIVE"


def test_payer_becomes_insolvent_after_spending_everything(funded_agents):
    """
    把钱全部转走后，付款方应该自己变成破产状态。
    """
    economy, agent_a, agent_b = funded_agents

    assert agent_a.status == "ACTIVE"

    economy.transfer("agent_a", "agent_b", 100)

    assert agent_a.balance == 0
    assert agent_a.status == "INSOLVENT"
    assert agent_a.can_take_financial_risk is False


# ============================================================
# Agent 解析（命令行友好）
# ============================================================
def test_resolve_agent_id_by_exact_id(funded_agents):
    economy, _, _ = funded_agents

    assert economy.resolve_agent_id("agent_a") == "agent_a"
    assert economy.resolve_agent_id("agent_b") == "agent_b"


def test_resolve_agent_id_by_name(funded_agents):
    economy, _, _ = funded_agents

    assert economy.resolve_agent_id("Agent A") == "agent_a"
    assert economy.resolve_agent_id("Agent B") == "agent_b"


def test_resolve_agent_id_is_case_insensitive(funded_agents):
    economy, _, _ = funded_agents

    assert economy.resolve_agent_id("AGENT_A") == "agent_a"
    assert economy.resolve_agent_id("agent a") == "agent_a"
    assert economy.resolve_agent_id("AGENT A") == "agent_a"


def test_resolve_agent_id_by_shorthand(funded_agents):
    """简写 "a" / "b" 应该能解析。"""
    economy, _, _ = funded_agents

    assert economy.resolve_agent_id("a") == "agent_a"
    assert economy.resolve_agent_id("b") == "agent_b"


def test_resolve_agent_id_rejects_unknown(funded_agents):
    economy, _, _ = funded_agents

    with pytest.raises(AgentNotFoundError):
        economy.resolve_agent_id("agent_z")


def test_resolve_agent_id_rejects_empty(funded_agents):
    economy, _, _ = funded_agents

    with pytest.raises(AgentNotFoundError):
        economy.resolve_agent_id("")


# ============================================================
# 隔离：转账是唯一改变别人余额的路径
# ============================================================
def test_no_api_to_directly_set_balance(funded_agents):
    """
    【架构保护】检查系统里没有「直接赋值余额」的后门。

    Economy 和 Wallet 都不应该暴露 set_balance / update_balance
    这种方法。要改钱，只能记一笔交易。
    """
    economy, agent_a, _ = funded_agents

    forbidden = ("set_balance", "update_balance", "force_balance", "add_money")

    for name in forbidden:
        assert not hasattr(economy, name), f"Economy 不应该有 {name}()"
        assert not hasattr(agent_a.wallet, name), f"Wallet 不应该有 {name}()"


def test_transfer_requires_both_agents_to_exist(economy):
    """
    只有一个 Agent 时，转账必须失败（找不到收款方）。
    """
    economy.create_agent("agent_a")

    with pytest.raises(AgentNotFoundError):
        economy.transfer("agent_a", "agent_b", 10)
