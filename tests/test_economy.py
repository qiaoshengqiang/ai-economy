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

    这是个很容易踩的坑：程序跑第二遍，Agent 就白拿了 100 AIC。
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
    assert status["currency"] == "AIC"
    assert status["version"] == "0.1"
    assert status["audit"]["balanced"] is True
    assert len(status["agents"]) == 2


# ============================================================
# 经济规则
# ============================================================
def test_can_afford(economy):
    """
    余额够不够的判断。

    显式指定初始余额，这样测试不受 config/config.yaml 影响。
    """
    economy.create_agent("agent_a", initial_balance=100)

    assert economy.can_afford("agent_a", 50) is True
    assert economy.can_afford("agent_a", 100) is True
    assert economy.can_afford("agent_a", 100.01) is False
    assert economy.can_afford("nobody", 1) is False


def test_assert_solvent_rejects_broke_agent(economy):
    from core.economy import EconomyError

    economy.create_agent("agent_poor", mint=False)

    with pytest.raises(EconomyError):
        economy.assert_solvent("agent_poor")


def test_config_controls_initial_balance(economy, config):
    """
    初始余额必须来自配置，而不是硬编码。

    这里验证「配置里写了多少，Agent 就拿多少」这条通路是通的。
    注意断言的是【配置里的值】，不是某个固定数字 ——
    这样你改 config/config.yaml 也不会让测试失败。
    """
    expected = float(config.get("economy.initial_balance", 0))
    assert expected > 0, "config/config.yaml 里的 initial_balance 应该是正数"

    agent = economy.create_agent("agent_from_config")
    assert agent.balance == expected


def test_explicit_initial_balance_overrides_config(economy, config):
    """
    显式传入的 initial_balance 应该覆盖配置文件。
    """
    configured = float(config.get("economy.initial_balance", 0))
    override = configured + 137

    agent = economy.create_agent("agent_rich", initial_balance=override)

    assert agent.balance == override
    assert agent.balance != configured


def test_tokens_per_ai_coin_from_config(economy, config):
    """
    【兑换比例】

    1 AIC = 多少 token 这个参数必须来自配置，不能硬编码进代码。

    注意这里【不断言具体数字】—— 因为它是你随时可以调整的经济模型参数。
    如果断言 == 10000，那你把它改成 5000 做实验时测试就会假报警。
    正确的验证方式是：Economy 读到的值 == 配置里写的值。
    """
    configured = config.get("economy.tokens_per_ai_coin")

    assert configured is not None, "config/config.yaml 缺少 tokens_per_ai_coin"
    assert int(configured) > 0, "兑换比例必须是正数"

    # 核心断言：Economy 用的就是配置里的那个值（而不是代码里写死的）
    assert economy.tokens_per_ai_coin == int(configured)


def test_exchange_rate_is_not_hardcoded_in_source(config):
    """
    【架构保护】兑换比例不能作为默认值写死在 Python 代码里。

    它只应该出现在 config/config.yaml 里。

    这条测试防的是这个具体写法：

        config.get("economy.tokens_per_ai_coin", 10000)     # ← 禁止
                                     ^^^^^^^^^^^^^^^^^^
        一旦配置里漏了这个键，系统就悄悄用代码里的 10000，
        你再也说不清它到底用的是哪个数。经济系统不能有这种模糊地带。

    正确的写法是不给默认值，缺了就直接报错：

        require_positive_int(config, "economy.tokens_per_ai_coin")

    ── 实现说明（踩了三个坑，所以最后用 AST 双重检查）──

        坑 1：简单子串查找。
              配置成 5000 时会把代码里的 25000 误判成违规 —— 假警报。
        坑 2：正则 + 数字边界，仍会误判。
              文档字符串里举例「1000000 :g 会变成 1e+06」也被当成违规。
        坑 3：只扫数字字面量，在配置值很小时失效。
              配置成 1 时，代码里任何一个 1 都会被匹配 —— 还是假警报。

        假警报的后果很严重：它会让人习惯性忽略测试，
        那还不如没有测试。

        所以现在分两层检查：
          第一层【结构检查】永远执行 ——
              找 get(...) 这类调用，看有没有为这个参数提供了默认值。
              这个判断和配置值是多少无关，任何值都适用。
          第二层【数值检查】只在配置值足够特别时执行 ——
              用 ast 找真正的整数字面量，忽略注释和字符串。
              配置值太小时（比如 1、10）容易和循环上限之类撞车，
              这时跳过第二层，并在测试里说清楚为什么跳过。
    """
    import ast
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    configured = int(config.get("economy.tokens_per_ai_coin"))

    KEY = "tokens_per_ai_coin"

    # 低于这个值就认为「不够特别」，不做数值检查
    # （否则 1、10 这种会和代码里大量无关的数字撞车）
    DISTINCTIVE_MIN = 1000

    structural_violations = []
    literal_violations = []

    for source in (project_root / "core").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue

            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue

            # ---- 第一层：结构检查（任何配置值下都执行）----
            # 调用里带了这个键，还额外提供了参数 → 那就是默认值
            if KEY in first.value and len(node.args) > 1:
                default = ast.unparse(node.args[1])
                structural_violations.append(
                    f"  {source.name}:{node.lineno}  "
                    f"{first.value!r} 带了默认值 {default}"
                )

            # ---- 第二层：数值检查（只扫描真正的数字字面量）----
            if configured >= DISTINCTIVE_MIN:
                for sub in ast.walk(tree):
                    if not isinstance(sub, ast.Constant):
                        continue
                    if isinstance(sub.value, bool):
                        continue
                    if isinstance(sub.value, int) and sub.value == configured:
                        literal_violations.append(
                            f"  {source.name}:{sub.lineno}  字面量 {configured}"
                        )
                break  # 每个文件只扫一次字面量，避免重复

    assert not structural_violations, (
        f"core/ 里给兑换比例 {KEY} 提供了硬编码的默认值。\n"
        f"这会导致你无法确定系统用的是配置里的值还是代码里的值。\n"
        f"请改成不给默认值（缺失就报错）。\n"
        f"违规位置：\n" + "\n".join(structural_violations)
    )

    if configured >= DISTINCTIVE_MIN:
        assert not literal_violations, (
            f"core/ 里出现了硬编码的兑换比例 {configured}。\n"
            f"它只应该出现在 config/config.yaml 里。\n"
            f"违规位置：\n" + "\n".join(literal_violations)
        )


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


# ============================================================
# 缺失 / 非法配置
# ============================================================
class TestRequiredConfig:
    """
    核心经济参数缺失或非法时，必须【立刻报错】，而不是悄悄用默认值。

    为什么这件事重要？
        如果 config.get("...", 10000) 这样给了默认值，
        那么配置里写 5000、代码里写 10000 的时候，
        你永远说不清系统实际用的是哪个数。
        经济系统里这种模糊地带是危险的 —— 宁可启动失败。
    """

    def _config_without_ratio(self):
        """造一个缺少兑换比例的配置。"""
        from core.config import Config

        return Config(
            {
                "system": {"name": "test"},
                "economy": {"currency": "AIC", "initial_balance": 100},
            }
        )

    def test_missing_ratio_raises(self):
        from core.config import ConfigError, require_positive_int

        config = self._config_without_ratio()

        with pytest.raises(ConfigError) as excinfo:
            require_positive_int(config, "economy.tokens_per_ai_coin")

        message = str(excinfo.value)
        assert "tokens_per_ai_coin" in message
        assert "缺少" in message
        # 错误提示里不能出现任何具体数字 ——
        # 否则又会给人「这里有个默认值可以抄」的错觉
        assert "10000" not in message

    def test_zero_ratio_raises(self):
        from core.config import Config, ConfigError, require_positive_int

        config = Config({"economy": {"tokens_per_ai_coin": 0}})

        with pytest.raises(ConfigError) as excinfo:
            require_positive_int(config, "economy.tokens_per_ai_coin")

        assert "大于 0" in str(excinfo.value)

    def test_negative_ratio_raises(self):
        from core.config import Config, ConfigError, require_positive_int

        config = Config({"economy": {"tokens_per_ai_coin": -5}})

        with pytest.raises(ConfigError):
            require_positive_int(config, "economy.tokens_per_ai_coin")

    def test_non_numeric_ratio_raises(self):
        from core.config import Config, ConfigError, require_positive_int

        config = Config({"economy": {"tokens_per_ai_coin": "一万"}})

        with pytest.raises(ConfigError) as excinfo:
            require_positive_int(config, "economy.tokens_per_ai_coin")

        assert "必须是整数" in str(excinfo.value)

    def test_economy_refuses_to_start_without_ratio(self):
        """
        Economy 启动时就应该拦住，不能带着残缺的经济模型跑起来。
        """
        from core.config import Config, ConfigError
        from core.economy import Economy
        from storage.database import create_memory_database

        broken = Config(
            {
                "system": {"name": "test", "version": "0.1"},
                "runtime": {"database_path": ":memory:"},
                "economy": {"currency": "AIC", "initial_balance": 100},
            }
        )

        database = create_memory_database()
        try:
            with pytest.raises(ConfigError):
                Economy(db=database, config=broken)
        finally:
            database.close()

    def test_valid_ratio_is_accepted(self):
        from core.config import Config, require_positive_int

        config = Config({"economy": {"tokens_per_ai_coin": 7777}})

        assert require_positive_int(config, "economy.tokens_per_ai_coin") == 7777

    def test_shipped_config_has_the_ratio(self):
        """
        仓库里自带的 config/config.yaml 必须有这个参数，
        否则新克隆的人一启动就报错。
        """
        from core.config import load_global_config

        config = load_global_config()
        value = config.get("economy.tokens_per_ai_coin")

        assert value is not None, "config/config.yaml 缺少 tokens_per_ai_coin"
        assert int(value) > 0
