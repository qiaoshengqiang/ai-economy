"""
Economy —— 经济系统的核心
============================================================

【这一层是做什么的】

它是整个系统的「上帝视角」，负责：

    1. 持有数据库、账本、钱包仓库（唯一真相来源）
    2. 创建 Agent 并给它们发初始资金（创世铸币）
    3. 做所有经济规则的检查（余额够不够、能不能接单）
    4. 派发钱包对象（Agent 只能拿到自己的）

【为什么必须有这一层，而不是让 Agent 自己管自己？】

因为如果 Agent 能自己创建货币、自己改余额，
那这个系统就不是经济系统，而是一群程序在互相骗。

正确的不信任结构：

    LLM 说："我决定支付 Agent B 20 AIC"
        ↓
    Economy 检查：你有 20 AIC 吗？有合同吗？托管里有吗？
        ↓
    检查通过才真正执行 Ledger 写入
        ↓
    写不进去就拒绝

LLM 只能【提出意图】，永远不能【直接改账】。
这条规则是硬性的。

【以后如何替换成区块链？】
    Economy 依赖的是 Ledger 这个接口。
    以后写一个 BlockchainLedger，实现同样的 record / balance_of 方法，
    在这里换掉就行，Agent 和 Wallet 的代码完全不用动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agents.base_agent import AgentStatus, BaseAgent, Reputation
from core.config import (
    TOKENS_PER_AI_COIN_EXAMPLE,
    AgentConfig,
    Config,
    load_agent_config,
    load_global_config,
    require_positive_int,
)
from core.identity import AgentIdentity, utc_now_iso
from core.ledger import Ledger, Transaction, TX_LLM_COST, SYSTEM_TREASURY
from core.logging_setup import get_logger
from core.paths import ensure_runtime_dirs
from core.wallet import Wallet, WalletRepository
from core.workspace import Workspace
from storage.database import Database, create_database
from storage.models import TABLE_AGENTS

logger = get_logger(__name__)


class EconomyError(Exception):
    """经济系统错误。"""


class AgentNotFoundError(EconomyError):
    """找不到这个 Agent。"""


class Economy:
    """
    经济系统。

    典型用法：

        economy = Economy.bootstrap()          # 建库、建表、加载配置
        economy.create_agent("agent_a")        # 创建 Agent A
        economy.create_agent("agent_b")        # 创建 Agent B

        print(economy.agent("agent_a").balance)   # 100
    """

    def __init__(
        self,
        db: Database,
        config: Optional[Config] = None,
        ledger: Optional[Ledger] = None,
    ):
        self.db = db
        self.config = config or load_global_config()

        # --- 经济参数（全部来自配置，没有一个硬编码）---
        self.currency: str = str(self.config.get("economy.currency", "AIC"))

        # 兑换比例是核心经济模型参数，配置里没有就直接报错。
        # 用一个函数统一取值，保证「配置 → Economy → AgentConfig」
        # 三处看到的是同一个数字、同一套校验规则。
        self.tokens_per_ai_coin: int = require_positive_int(
            self.config,
            "economy.tokens_per_ai_coin",
            example=TOKENS_PER_AI_COIN_EXAMPLE,
        )

        self.amount_precision: int = int(
            self.config.get("economy.amount_precision", 4)
        )
        self.insolvency_threshold: float = float(
            self.config.get("economy.insolvency_threshold", 0)
        )

        # --- 核心组件 ---
        self.ledger = ledger or Ledger(
            db=db,
            currency=self.currency,
            amount_precision=self.amount_precision,
        )
        self.wallets = WalletRepository(db=db, ledger=self.ledger)

        # 已加载到内存中的 Agent（数据库才是权威，这里只是缓存）
        self._agents: Dict[str, BaseAgent] = {}

    # ------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------
    @classmethod
    def bootstrap(
        cls,
        db_path: Optional[Union[str, Path]] = None,
        config: Optional[Config] = None,
    ) -> "Economy":
        """
        启动经济系统：确保目录存在、连数据库、建表、加载配置。
        """
        ensure_runtime_dirs()
        database = create_database(db_path)
        economy = cls(db=database, config=config)
        logger.debug(
            "Economy 已初始化：currency=%s, db=%s",
            economy.currency,
            database.path,
        )
        return economy

    def close(self) -> None:
        self.db.close()
        self._agents.clear()

    def __enter__(self) -> "Economy":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------
    # 创建 Agent
    # ------------------------------------------------------------
    def create_agent(
        self,
        agent_id: str,
        agent_config: Optional[AgentConfig] = None,
        initial_balance: Optional[float] = None,
        mint: bool = True,
    ) -> BaseAgent:
        """
        创建一个 Agent（经济主体）。

        这个过程会做四件事：
            1. 建立身份        → agents 表
            2. 建立钱包        → wallets 表
            3. 发初始资金      → ledger_transactions 表（创世铸币）
            4. 建立工作空间    → agents/<id>/workspace

        参数：
            initial_balance: 不给就用配置里的 economy.initial_balance
            mint:            False 表示只建身份和钱包，不发钱
                             （测试「零余额破产」时会用到）
        """
        # --- 幂等：已经存在就直接返回，不重复铸币 ---
        existing = self._load_agent(agent_id)
        if existing is not None:
            logger.debug(
                "恢复已有经济主体：%s（余额 %.4f %s），不重复初始化",
                agent_id, existing.balance, self.currency)
            return existing

        # 🔴 身份行可能缺失，但账本/钱包是持久化事实。
        #    只要这个 Agent 在账本里有过任何交易，就**不重新铸币**。
        had_history = self.ledger.has_history(agent_id)

        # --- 1. 加载配置 ---
        cfg = agent_config or load_agent_config(agent_id, self.config)

        # --- 2. 身份 ---
        identity = AgentIdentity.create(
            agent_id=agent_id,
            name=cfg.agent_name,
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_AGENTS}
                    (agent_id, name, created_at, public_key, owner,
                     home_world, reputation_score, completed_tasks, failed_tasks, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.agent_id,
                    identity.name,
                    identity.created_at,
                    identity.public_key,
                    identity.owner,
                    identity.home_world,
                    0.0,
                    0,
                    0,
                    AgentStatus.ACTIVE,
                ),
            )

        # --- 3. 钱包 ---
        wallet = self.wallets.get_or_create(agent_id, currency=self.currency)

        # --- 4. 工作空间 ---
        workspace = Workspace.for_agent(agent_id)

        # --- 5. 组装 Agent ---
        agent = BaseAgent(
            identity=identity,
            config=cfg,
            wallet=wallet,
            workspace=workspace,
            reputation=Reputation(),
        )
        self._agents[agent_id] = agent

        logger.info("已创建经济主体：%s (%s)", identity.name, identity.agent_id)

        # --- 6. 发初始资金（创世铸币）---
        # 🔒 只有**账本里没有任何记录**的新 Agent 才铸币。
        #    身份行丢失但账本还在时，恢复身份，不重新发钱。
        if mint:
            if had_history:
                logger.warning(
                    "Agent %s 的身份行缺失，但账本已有记录："
                    "恢复身份，不重新铸币（余额 %.4f %s）",
                    agent_id, wallet.balance, self.currency)
            else:
                amount = (
                    float(initial_balance)
                    if initial_balance is not None
                    else cfg.initial_balance
                )
                if amount > 0:
                    wallet.mint(amount, reason="genesis_mint")
                    logger.info(
                        "  创世铸币 %.4g %s -> %s",
                        amount,
                        self.currency,
                        identity.agent_id,
                    )

        agent.update_status()
        return agent

    # ------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------
    def restore_or_create_agent(
        self,
        agent_id: str,
        agent_config: Optional[AgentConfig] = None,
        initial_balance: Optional[float] = None,
    ) -> BaseAgent:
        """启动路径：**先恢复，只有真正的新 Agent 才初始化经济数据**。

        · 已有身份 → 从 agents/wallets/ledger 恢复，不铸币；
        · 身份缺失但账本有记录 → 恢复身份，不铸币；
        · 完全新 Agent → 创建身份 + 钱包 + 创世铸币。

        🔒 余额的唯一权威是 Ledger，不是启动参数。
        """
        existing = self._load_agent(agent_id)
        if existing is not None:
            logger.debug(
                "恢复经济主体：%s（余额 %.4f %s）",
                agent_id, existing.balance, self.currency)
            return existing
        return self.create_agent(
            agent_id,
            agent_config=agent_config,
            initial_balance=initial_balance,
            mint=True,
        )

    # ------------------------------------------------------------
    # 查询 Agent
    # ------------------------------------------------------------
    def agent(self, agent_id: str) -> BaseAgent:
        """
        取得一个 Agent。不存在则抛异常。

        注意：返回的 Agent 里包含的是【它自己的】钱包对象。
        Agent A 拿不到 Agent B 的 Wallet 实例，
        所以「A 直接改 B 的余额」在结构上就是做不到的。
        """
        agent = self._load_agent(agent_id)
        if agent is None:
            raise AgentNotFoundError(
                f"找不到 Agent：{agent_id!r}。"
                f"当前已创建的 Agent：{sorted(self._agents.keys())}"
            )
        return agent

    def has_agent(self, agent_id: str) -> bool:
        return self._load_agent(agent_id) is not None

    def all_agents(self) -> List[BaseAgent]:
        """返回所有已创建的 Agent。"""
        rows = self.db.fetchall(
            f"SELECT agent_id FROM {TABLE_AGENTS} ORDER BY agent_id"
        )
        agents = []
        for row in rows:
            agent = self._load_agent(row["agent_id"])
            if agent is not None:
                agents.append(agent)
        return agents

    def _load_agent(self, agent_id: str) -> Optional[BaseAgent]:
        """
        从内存缓存或数据库中加载 Agent。

        数据库才是权威来源；内存缓存只是为了避免重复构造。
        """
        if agent_id in self._agents:
            return self._agents[agent_id]

        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_AGENTS} WHERE agent_id = ?",
            (agent_id,),
        )
        if row is None:
            return None

        # 恢复身份
        identity = AgentIdentity.from_dict(
            {
                "agent_id": row["agent_id"],
                "name": row["name"],
                "created_at": row["created_at"],
                "public_key": row["public_key"],
                "owner": row["owner"],
                "home_world": row["home_world"],
            }
        )

        # 恢复配置
        cfg = load_agent_config(agent_id, self.config)

        # 恢复钱包
        wallet = self.wallets.get_or_create(agent_id, currency=self.currency)

        # 恢复工作空间
        workspace = Workspace.for_agent(agent_id)

        # 恢复声誉
        reputation = Reputation(
            score=float(row["reputation_score"]),
            completed_tasks=int(row["completed_tasks"]),
            failed_tasks=int(row["failed_tasks"]),
        )

        agent = BaseAgent(
            identity=identity,
            config=cfg,
            wallet=wallet,
            workspace=workspace,
            reputation=reputation,
            status=row["status"],
        )
        self._agents[agent_id] = agent
        return agent

    # ------------------------------------------------------------
    # 经济规则检查（Agent 无法绕过）
    # ------------------------------------------------------------
    def can_afford(self, agent_id: str, amount: float) -> bool:
        """
        这个 Agent 付得起这笔钱吗？

        所有「花钱」的动作在执行前都应该先问这一句。
        """
        wallet = self.wallets.get(agent_id)
        if wallet is None:
            return False
        return wallet.available >= amount

    def assert_solvent(self, agent_id: str) -> None:
        """
        断言这个 Agent 还有偿付能力。

        用来在 Agent 想「承诺一件付不起的事」时把它拦下来。
        """
        agent = self.agent(agent_id)
        if not agent.can_take_financial_risk:
            raise EconomyError(
                f"{agent.name} 当前无法承担经济责任"
                f"（状态：{agent.status}，余额：{agent.balance} {self.currency}）"
            )

    # ------------------------------------------------------------
    # 经济动作：转账（OS 级执行入口）
    # ------------------------------------------------------------
    def charge_llm_cost(
        self,
        agent_id: str,
        cost_ai: float,
        call_id: Optional[str] = None,
        memo: Optional[Any] = None,
    ) -> Optional[Any]:
        """
        把一次 LLM 认知活动的成本，从 Agent 记到系统金库（burn）。

        【§0.5 审计 A-02 / H-2】

            审计发现 `LLMCallRecorder.cost_ai` 恒为 0 ——
            于是 §0.5.3「激励塑造行为」**结构性不成立**：
            Agent 可以无限调用 LLM 而没有任何经济后果。

            本方法把那笔成本**真正落进 Ledger**，
            于是「消耗 AI Coin → 为当前行动支付机会成本」成立。

        【🔒 边界：这里不算「应该花多少」】

            本方法**只记账**。它不判断「这次调用值不值」，
            也不因为余额低就拒绝调用（那会变成 OS 替 Agent 做策略）。

            ⚠️ 但**余额不足时仍然会失败** —— 那不是策略判断，
               是 §0.5.5 的**结构约束**（付不起就是付不起）。
               与「Ledger 余额不足 → 无法完成支付」是同一条。

        为什么记到 SYS_TREASURY？
            因为这是**消耗**（burn），不是转账给另一个 Agent。
            项目已有 `SYSTEM_TREASURY` 与 `TX_LLM_COST` 两个常量，
            这里只是第一次真正用上它们。
        """
        amount = float(cost_ai or 0.0)
        if amount <= 0:
            return None

        # 余额不足 → 这里会抛出（结构约束，不是策略）
        with self.db.transaction() as conn:
            return self.ledger.record(
                TX_LLM_COST,
                amount,
                from_agent=agent_id,
                to_agent=SYSTEM_TREASURY,
                reason=f"LLM 认知成本 {call_id or ''}".strip(),
                memo=memo,
                conn=conn,
            )

    def transfer(
        self,
        from_agent_id: str,
        to_agent_id: str,
        amount: float,
        reason: str = "manual_transfer",
        task_id: Optional[str] = None,
        contract_id: Optional[str] = None,
    ) -> Transaction:
        """
        让一个 Agent 付钱给另一个 Agent。**这是唯一被允许的转账路径。**

        【为什么必须走这个方法，而不能直接调 wallet.transfer_to？】

        Wallet 层只检查「余额够不够」。
        但一次真实的经济支付还应该有更多关卡：

            1. 付款方和收款方都真实存在吗？     ← Wallet 不检查
            2. 是不是自己给自己转账？           ← Wallet 检查了
            3. 付款方还有偿付能力吗？           ← Wallet 不检查
            4. 金额是正数吗？                   ← Wallet 检查了

        这些「经济规则」属于 OS Core 的职责，不属于钱包。
        钱包只管钱，Economy 管规则。

        【未来接入 LLM 时】

            LLM 输出："我决定支付 Agent B 20 AIC"
                ↓
            Agent 解析成意图：transfer(to="agent_b", amount=20)
                ↓
            调用【这个方法】
                ↓
            校验通过才真正写账本；不通过就抛异常
                ↓
            Agent 把异常翻译成"支付被系统拒绝"

        所以这个方法就是「LLM 提出意图 → OS 验证 → 执行」里的中间那一环。
        把入口建在这里，以后接 LLM 时不需要改任何经济逻辑。

        校验通过后，钱从 from 的钱包流向 to 的钱包，Ledger 记录一笔 TRANSFER。

        参数：
            amount: 必须 > 0
            reason: 人类可读的原因，会写进账本（比如 "task_payment"）
        返回：
            写入账本的那笔 Transaction

        异常：
            AgentNotFoundError      某个 Agent 不存在
            EconomyError            自己转自己 / 金额非正 / 付款方已破产
            InsufficientFundsError  余额不足
        """
        # --- 关卡 1：两个 Agent 都必须存在 ---
        from_agent = self.agent(from_agent_id)
        to_agent = self.agent(to_agent_id)

        # --- 关卡 2：不能自己给自己转账 ---
        if from_agent_id == to_agent_id:
            raise EconomyError(
                f"不能给自己转账：{from_agent.name} 的付款方和收款方是同一个 Agent"
            )

        # --- 关卡 3：金额必须是正数 ---
        try:
            value = float(amount)
        except (TypeError, ValueError):
            raise EconomyError(f"金额必须是数字，收到：{amount!r}")

        if value != value:  # NaN
            raise EconomyError("金额不能是 NaN")

        if value <= 0:
            raise EconomyError(
                f"转账金额必须大于 0，收到 {value}。"
                f"（想做反向转账就调换付款方和收款方）"
            )

        # --- 关卡 4：付款方必须还有偿付能力 ---
        #
        # 破产的 Agent 不能继续承担经济责任。
        # 注意：这里拒绝的是「付款」，因为付款是承担责任的行为。
        # 别人给它转钱是允许的 —— 那正是它翻身的机会。
        if not from_agent.can_take_financial_risk:
            raise EconomyError(
                f"{from_agent.name} 当前无法承担经济责任，转账被拒绝。"
                f"（状态：{from_agent.status}，"
                f"余额：{from_agent.balance} {self.currency}）"
            )

        # --- 关卡 5：余额是否足够（交给钱包，规则只有一处定义）---
        tx = from_agent.wallet.transfer_to(
            to_agent.wallet,
            value,
            reason=reason,
            task_id=task_id,
            contract_id=contract_id,
        )

        # --- 转账后刷新双方状态 ---
        #
        # 为什么两边的状态都要刷新？
        #   付款方可能因为这笔钱而破产（余额归零）
        #   收款方可能因为这笔钱而复活（从 INSOLVENT 回到 ACTIVE）
        from_agent.update_status()
        to_agent.update_status()

        logger.info(
            "转账 %s -> %s : %.4g %s（%s，交易号 %s）",
            from_agent.agent_id,
            to_agent.agent_id,
            value,
            self.currency,
            reason,
            tx.tx_id,
        )

        return tx

    # ------------------------------------------------------------
    # Agent 查找（让命令行可以用名字或 ID 指定 Agent）
    # ------------------------------------------------------------
    def resolve_agent_id(self, name_or_id: str) -> str:
        """
        把用户输入解析成真正的 agent_id。

        支持三种写法（都不区分大小写）：
            "agent_a"   —— 精确的 agent_id
            "Agent A"   —— 显示名
            "a"         —— 简写（自动补全成 agent_a）

        这样命令行里 `./dev.sh transfer a b 20` 也能跑通。
        """
        key = str(name_or_id).strip().lower()

        if not key:
            raise AgentNotFoundError("没有指定 Agent")

        agents = self.all_agents()

        # 1) 精确匹配 agent_id
        for agent in agents:
            if agent.agent_id.lower() == key:
                return agent.agent_id

        # 2) 匹配显示名
        for agent in agents:
            if agent.name.lower() == key:
                return agent.agent_id

        # 3) 简写："a" -> "agent_a"（只在唯一匹配时才接受，避免歧义）
        candidates = [
            agent.agent_id
            for agent in agents
            if agent.agent_id.lower() == f"agent_{key}"
        ]
        if len(candidates) == 1:
            return candidates[0]

        available = ", ".join(
            f"{a.agent_id} ({a.name})" for a in agents
        ) or "（还没有任何 Agent）"

        raise AgentNotFoundError(
            f"找不到 Agent：{name_or_id!r}\n"
            f"可用的 Agent：{available}"
        )

    # ------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------
    def audit(self) -> Dict[str, Any]:
        """
        对账：检查「所有钱包余额之和」是否等于「账本里的货币总量」。

        如果对不上，说明账本被写坏了 —— 这是非常严重的 bug。
        所以这个方法在启动和测试时都会被调用。

        注意：这里的等式能成立，是因为「铸币」和「转账」都只改账本。
        任何绕过账本直接改余额的代码都会立刻让这个等式失败。
        """
        wallet_sum = 0.0
        for wallet in self.wallets.all_wallets():
            wallet_sum += wallet.balance

        supply = self.ledger.total_supply()

        # 系统金库里的钱不算在「Agent 钱包之和」里，
        # 所以要把它单独加回来才能和总供应量比较。
        treasury = self.ledger.balance_of("SYS_TREASURY")

        difference = round(wallet_sum + treasury - supply, self.amount_precision)
        balanced = abs(difference) < 10 ** (-self.amount_precision)

        return {
            "total_supply": supply,
            "wallets_total": round(wallet_sum, self.amount_precision),
            "treasury": treasury,
            "difference": difference,
            "balanced": balanced,
            "transactions": self.ledger.count(),
        }

    # ------------------------------------------------------------
    # 状态汇总
    # ------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """系统状态概览。启动横幅会用它。"""
        agents = self.all_agents()
        return {
            "system": self.config.get("system.name", "AI Economy OS"),
            "version": self.config.get("system.version", "0.1"),
            "currency": self.currency,
            "tokens_per_ai_coin": self.tokens_per_ai_coin,
            "database": self.db.path,
            "agent_count": len(agents),
            "agents": [agent.describe() for agent in agents],
            "ledger": {
                "transactions": self.ledger.count(),
                "total_supply": self.ledger.total_supply(),
            },
            "audit": self.audit(),
        }

    def is_ready(self) -> bool:
        """
        系统就绪判断。

        v0.1 的就绪标准：
          1. 至少有两个 Agent
          2. 每个 Agent 都有钱包
          3. 每个 Agent 都有独立的工作空间目录
          4. 账本对得上（审计通过）
        """
        agents = self.all_agents()
        if len(agents) < 2:
            return False

        for agent in agents:
            if agent.wallet is None:
                return False
            if not agent.workspace.root.exists():
                return False

        return self.audit()["balanced"]

    def __repr__(self) -> str:
        return (
            f"Economy(currency={self.currency!r}, "
            f"agents={len(self.all_agents())}, "
            f"transactions={self.ledger.count()})"
        )
