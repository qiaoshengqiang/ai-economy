"""
Test: Wallet（钱包）
============================================================

验证目标：
    1. 新 Agent 拿到正确的初始余额
    2. 余额是「账本算出来的」，不能直接赋值修改
    3. 转账正确扣款和入账
    4. 钱不够时必须被拒绝（不能透支）
    5. 不能给自己转账
    6. 不能凭空造钱（只有 mint 可以）
"""

from __future__ import annotations

import pytest

from core.ledger import InsufficientFundsError
from core.wallet import WalletError


# ============================================================
# 初始状态
# ============================================================
def test_new_agent_has_initial_balance(funded_agents):
    economy, agent_a, agent_b = funded_agents

    assert agent_a.balance == 100
    assert agent_b.balance == 100
    assert economy.currency == "AI"


def test_wallet_has_no_stored_balance_field(funded_agents):
    """
    核心设计验证：Wallet 不允许有可直接修改的 balance 字段。

    balance 是 property（只读），所以给 wallet.balance 赋值应该报错。
    这条测试保护的是「余额必须从账本推导」这个架构原则。
    """
    _, agent_a, _ = funded_agents
    wallet = agent_a.wallet

    with pytest.raises(AttributeError):
        wallet.balance = 999999  # type: ignore[misc]


def test_balance_after_each_mint(economy):
    """
    连续铸币应该累加，且每次都有独立交易记录。
    """
    # 先创建 Agent（会顺带创建钱包并铸币 100），
    # 再取消铸币地创建一次以验证「零余额起步」的情况。
    agent = economy.create_agent("agent_test", mint=False)
    wallet = agent.wallet

    assert wallet.balance == 0

    wallet.mint(50)
    assert wallet.balance == 50

    wallet.mint(30)
    assert wallet.balance == 80

    # 两笔铸币 = 两条交易记录
    assert economy.ledger.count() == 2


def test_cannot_create_wallet_for_unknown_agent(db, ledger):
    """
    外键约束保护：不能给一个不存在的 Agent 凭空建钱包。

    这是数据库层面的隔离 —— 钱包必须挂在真实存在的经济主体上。
    """
    import sqlite3

    from core.wallet import WalletRepository

    repo = WalletRepository(db=db, ledger=ledger)

    with pytest.raises(sqlite3.IntegrityError):
        repo.create("agent_幽灵")


# ============================================================
# 转账
# ============================================================
def test_transfer_between_wallets(funded_agents):
    _, agent_a, agent_b = funded_agents

    agent_a.wallet.transfer_to(agent_b.wallet, 20, reason="test_payment")

    assert agent_a.balance == 80
    assert agent_b.balance == 120


def test_transfer_is_recorded_in_ledger(funded_agents):
    economy, agent_a, agent_b = funded_agents

    tx = agent_a.wallet.transfer_to(agent_b.wallet, 20, reason="task_payment")

    assert tx.from_agent == "agent_a"
    assert tx.to_agent == "agent_b"
    assert tx.amount == 20
    assert tx.reason == "task_payment"

    # 账本里能查到这笔
    history = economy.ledger.history(agent_id="agent_a")
    assert any(t.tx_id == tx.tx_id for t in history)


def test_transfer_preserves_total_supply(funded_agents):
    """
    转账不创造也不销毁货币：总量必须不变。
    """
    economy, agent_a, agent_b = funded_agents

    supply_before = economy.ledger.total_supply()
    agent_a.wallet.transfer_to(agent_b.wallet, 35)

    assert economy.ledger.total_supply() == supply_before
    assert economy.audit()["balanced"] is True


# ============================================================
# 拒绝非法操作
# ============================================================
def test_cannot_overspend(funded_agents):
    """余额 100，转 200 必须被拒绝。"""
    _, agent_a, agent_b = funded_agents

    with pytest.raises(InsufficientFundsError):
        agent_a.wallet.transfer_to(agent_b.wallet, 200)

    # 关键：失败后两边的余额都不能有变化
    assert agent_a.balance == 100
    assert agent_b.balance == 100


def test_cannot_transfer_to_self(funded_agents):
    _, agent_a, _ = funded_agents

    with pytest.raises(WalletError):
        agent_a.wallet.transfer_to(agent_a.wallet, 10)


def test_cannot_transfer_negative_amount(funded_agents):
    """
    负数金额必须被拒绝。

    因为如果允许负数，Agent 就能靠「转 -50」偷别人的钱。
    """
    _, agent_a, agent_b = funded_agents

    with pytest.raises(Exception):
        agent_a.wallet.transfer_to(agent_b.wallet, -50)

    assert agent_a.balance == 100
    assert agent_b.balance == 100


def test_cannot_mint_negative(funded_agents):
    _, agent_a, _ = funded_agents

    with pytest.raises(WalletError):
        agent_a.wallet.mint(-100)

    assert agent_a.balance == 100


# ============================================================
# 破产判定
# ============================================================
def test_insolvent_when_balance_zero(economy):
    """
    余额为 0 的 Agent 应该被判定为破产。

    注意这里 mint=False —— 不发初始资金，
    模拟一个把启动资金花光的 Agent。
    """
    agent = economy.create_agent("agent_poor", mint=False)

    assert agent.balance == 0
    assert agent.is_insolvent is True
    assert agent.can_take_financial_risk is False

    agent.update_status()
    assert agent.status == "INSOLVENT"


def test_solvent_agent_is_active(funded_agents):
    _, agent_a, _ = funded_agents

    agent_a.update_status()

    assert agent_a.is_insolvent is False
    assert agent_a.can_take_financial_risk is True
    assert agent_a.status == "ACTIVE"


# ============================================================
# LLM 成本直接扣钱包
# ============================================================
def test_llm_cost_reduces_balance(funded_agents):
    """
    AI Economy 的核心差异点：
    调用模型是真实成本，会让钱包变少。
    """
    _, agent_a, _ = funded_agents

    agent_a.wallet.charge_llm_cost(
        cost_ai=2.8,
        provider="mock",
        model="mock-model-v1",
    )

    assert agent_a.balance == pytest.approx(97.2)


def test_llm_cost_zero_is_still_recorded(funded_agents):
    """
    成本为 0 的调用（比如本地模型）也必须留一条账。
    否则 llm_calls 和 ledger 就对不上了。
    """
    economy, agent_a, _ = funded_agents

    count_before = economy.ledger.count()
    agent_a.wallet.charge_llm_cost(cost_ai=0, provider="local", model="local-llm")

    assert economy.ledger.count() == count_before + 1
    assert agent_a.balance == 100
