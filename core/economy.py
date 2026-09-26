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

    LLM 说："我决定支付 Agent B 20 AI"
        ↓
    Economy 检查：你有 20 AI 吗？有合同吗？托管里有吗？
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
from core.config import AgentConfig, Config, load_agent_config, load_global_config
from core.identity import AgentIdentity, utc_now_iso
from core.ledger import Ledger
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
        self.currency: str = str(self.config.get("economy.currency", "AI"))
        self.tokens_per_ai_coin: int = int(
            self.config.get("economy.tokens_per_ai_coin", 10000)
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
            logger.debug("Agent %s 已存在，跳过创建", agent_id)
            return existing

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
        if mint:
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
