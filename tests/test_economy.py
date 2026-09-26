"""
Test: Economy（经济系统）
============================================================

验证目标：
    1. 系统能创建两个 Agent
    2. 两个 Agent 各自有独立身份 / 钱包 / 工作空间
    3. 账本审计（对账）永远平衡
    4. Agent 之间无法互相篡改余额
    5. 系统就绪判断正确
"""

from __future__ import annotations

import pytest

from core.economy import AgentNotFoundError, Economy
from core.wallet import WalletError


# ============================================================
# 创建
# ============================================================
def test_create_two_agents(funded_agents):
    economy, agent_a, agent_b = funded_agents

    assert agent_a.agent_id == "agent_a"
    assert agent_b.agent_id == "agent_b"
    assert agent_a.name == "Agent A"
    assert agent_b.name == "Agent B"


def test_agents_have_independent_identity(funded_agents):
    _, agent_a, agent_b = funded_agents

    assert agent_a.identity is not agent_b.identity
    assert agent_a.identity.agent_id != agent_b.identity.agent_id
    assert agent_a.identity.created_at  # 都有时间戳


def test_agents_have_independent_wallets(funded_agents):
    _, agent_a, agent_b = funded_agents

    assert agent_a.wallet.wallet_id != agent_b.wallet.wallet_id
    assert agent_a.wallet.agent_id == "agent_a"
    assert agent_b.wallet.agent_id == "agent_b"


def test_agents_have_independent_workspaces(funded_agents):
    """
    【核心隔离测试】

    A 的工作空间和 B 的工作空间必须是不同的真实目录，
    且互不包含。
    """
    _, agent_a, agent_b = funded_agents

    root_a = agent_a.workspace.root
    root_b = agent_b.workspace.root

    assert root_a != root_b
    assert root_a.exists()
    assert root_b.exists()

    # A 的目录不能在 B 的目录里面（反之亦然）
    assert root_b not in root_a.parents
    assert root_a not in root_b.parents


def test_create_agent_is_idempotent(economy):
    """
    重复创建同一个 Agent 不应该重复发钱。

    这是个很容易踩的坑：程序跑第二遍，Agent 就白拿了 100 AI。
    """
    first = economy.create_agent("agent_a")
    balance_after_first = first.balance

    second = economy.create_agent("agent_a")

    assert second.balance == balance_after_first
    assert economy.ledger.count() == 1   # 只有一笔铸币


def test_agent_not_found(economy):
    with pytest.raises(AgentNotFoundError):
        economy.agent("agent_不存在")


def test_all_agents(funded_agents):
    economy, _, _ = funded_agents

    agents = economy.all_agents()
    assert len(agents) == 2
    assert {a.agent_id for a in agents} == {"agent_a", "agent_b"}


# ============================================================
# 隔离：A 不能碰 B 的钱
# ============================================================
def test_agent_cannot_spend_other_agents_money(funded_agents):
    """
    Agent A 只能通过自己的钱包花钱。
    它没有 B 钱包的「写权限」—— B 的余额不会因为 A 而改变，
    除非通过一笔真实的转账。
    """
    _, agent_a, agent_b = funded_agents

    a_wallet = agent_a.wallet
    b_wallet = agent_b.wallet

    # A 花自己的钱，B 的余额必须不变
    a_wallet.transfer_to(b_wallet, 10)

    assert agent_a.balance == 90
    assert agent_b.balance == 110

    # A 想要「直接给 B 设置余额」是不存在的方法
    assert not hasattr(a_wallet, "set_balance")
    assert not hasattr(b_wallet, "set_balance")


def test_workspace_blocks_cross_agent_access(funded_agents):
    """
    【安全边界测试】

    Agent A 的工作空间必须拒绝访问 Agent B 的目录。
    这是把「工作空间隔离」从口号变成代码的关键测试。
    """
    from core.workspace import WorkspaceViolationError

    _, agent_a, agent_b = funded_agents

    # A 试图用相对路径跑出去摸 B 的文件
    with pytest.raises(WorkspaceViolationError):
        agent_a.workspace.write_text("../../agent_b/memory/steal.txt", "入侵数据")

    # A 试图用绝对路径摸 B 的目录
    with pytest.raises(WorkspaceViolationError):
        agent_a.workspace.read_text(str(agent_b.workspace.root / "secret.txt"))


def test_workspace_allows_own_files(funded_agents):
    _, agent_a, _ = funded_agents

    agent_a.workspace.write_text("output/hello.txt", "你好")

    assert agent_a.workspace.read_text("output/hello.txt") == "你好"
    assert "output/hello.txt" in agent_a.workspace.list_files()


def test_memory_is_private(funded_agents):
    """
    两个 Agent 的记忆文件互不干扰。
    """
    _, agent_a, agent_b = funded_agents

    agent_a.workspace.write_memory("notes.txt", "A 的秘密")
    agent_b.workspace.write_memory("notes.txt", "B 的秘密")

    assert agent_a.workspace.read_memory("notes.txt") == "A 的秘密"
    assert agent_b.workspace.read_memory("notes.txt") == "B 的秘密"
    assert agent_a.workspace.memory_dir != agent_b.workspace.memory_dir


# ============================================================
# 审计
# ============================================================
def test_audit_balanced_after_creation(funded_agents):
    economy, _, _ = funded_agents

    audit = economy.audit()

    assert audit["balanced"] is True
    assert audit["total_supply"] == 200
    assert audit["wallets_total"] == 200
    assert audit["difference"] == 0


def test_audit_balanced_after_transfers(funded_agents):
    economy, agent_a, agent_b = funded_agents

    agent_a.wallet.transfer_to(agent_b.wallet, 20)
    agent_b.wallet.transfer_to(agent_a.wallet, 5)
    agent_a.wallet.charge_llm_cost(2.8, "mock", "mock-model-v1")

    audit = economy.audit()

    assert audit["balanced"] is True
    assert audit["total_supply"] == 200   # 转账和成本支付都不改变总量

    # 从系统金库角度看：LLM 成本付给了金库
    assert audit["treasury"] == pytest.approx(2.8)


def test_treasury_accounting(funded_agents):
    """
    LLM 成本流向系统金库，所以：
        总供应量 = 所有 Agent 钱包之和 + 系统金库余额
    """
    economy, agent_a, _ = funded_agents

    agent_a.wallet.charge_llm_cost(3.5, "mock", "mock-model-v1")

    audit = economy.audit()
    wallets_plus_treasury = audit["wallets_total"] + audit["treasury"]

    assert wallets_plus_treasury == pytest.approx(audit["total_supply"])


# ============================================================
# 系统状态
# ============================================================
def test_is_ready_when_two_agents_exist(funded_agents):
    economy, _, _ = funded_agents
    assert economy.is_ready() is True


def test_not_ready_with_one_agent(economy):
    economy.create_agent("agent_a")
    assert economy.is_ready() is False


def test_status_summary(funded_agents):
    economy, _, _ = funded_agents

    status = economy.status()

    assert status["agent_count"] == 2
    assert status["currency"] == "AI"
    assert status["version"] == "0.1"
    assert status["audit"]["balanced"] is True
    assert len(status["agents"]) == 2


# ============================================================
# 经济规则
# ============================================================
def test_can_afford(economy):
    economy.create_agent("agent_a")

    assert economy.can_afford("agent_a", 50) is True
    assert economy.can_afford("agent_a", 100) is True
    assert economy.can_afford("agent_a", 100.01) is False
    assert economy.can_afford("nobody", 1) is False


def test_assert_solvent_rejects_broke_agent(economy):
    from core.economy import EconomyError

    economy.create_agent("agent_poor", mint=False)

    with pytest.raises(EconomyError):
        economy.assert_solvent("agent_poor")


def test_config_controls_initial_balance(economy):
    """
    初始余额必须来自配置，不是硬编码。
    这里显式传 250，验证它生效。
    """
    agent = economy.create_agent("agent_rich", initial_balance=250)
    assert agent.balance == 250


def test_tokens_per_ai_coin_from_config(economy, config):
    expected = config.get("economy.tokens_per_ai_coin")
    assert economy.tokens_per_ai_coin == expected
    assert economy.tokens_per_ai_coin == 10000


# ============================================================
# Agent 配置独立
# ============================================================
def test_agents_have_independent_llm_config(funded_agents):
    """
    【原则 1 验证】Agent 不等于 LLM。

    A 和 B 可以配置不同的 provider，
    而这个差别只体现在配置里，不影响 Agent 的身份和钱包。
    """
    _, agent_a, agent_b = funded_agents

    # 两者默认都是 mock（配置文件里写的）
    assert agent_a.config.provider == "mock"
    assert agent_b.config.provider == "mock"

    # 但它们的配置对象是各自独立的
    assert agent_a.config is not agent_b.config
    assert agent_a.config.agent_id != agent_b.config.agent_id


def test_agent_skills_differ(funded_agents):
    """
    A 和 B 的技能配置不同 —— 这是未来「专业化分工」的基础。
    """
    _, agent_a, agent_b = funded_agents

    assert "outsourcing" in agent_a.config.skills
    assert "csv_processing" in agent_b.config.skills
    assert agent_a.config.skills != agent_b.config.skills
