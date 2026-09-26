"""
Wallet —— 钱包
============================================================

钱包是 Agent 的「经济能力」。

【关键设计】钱包自己【不存余额】。
    余额永远向 Ledger 查询。钱包只负责：
      1. 绑定「我是哪个 Agent 的钱包」
      2. 检查够不够钱（在经济规则层的拦截）
      3. 把转账意图翻译成 Ledger 交易
      4. 提供人类可读的账单

    为什么要有 Wallet 这一层，而不让 Agent 直接用 Ledger？
        因为 Wallet 是「权限边界」。
        Agent A 拿到的是自己的 Wallet 对象，它压根没有 Agent B 的 Wallet，
        所以「A 直接改 B 的余额」在代码层面就是不可能的 ——
        不是靠约定，而是靠结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso
from core.ledger import (
    SYSTEM_TREASURY,
    TX_ESCROW_LOCK,
    TX_GENESIS_MINT,
    TX_LLM_COST,
    TX_TRANSFER,
    InsufficientFundsError,
    Ledger,
    LedgerError,
    Transaction,
)
from storage.database import Database
from storage.models import TABLE_WALLETS


class WalletError(Exception):
    """钱包层错误。"""


class FrozenWalletError(WalletError):
    """钱包被冻结，不能交易。"""


@dataclass
class Wallet:
    """
    一个 Agent 的钱包。

    注意：wallet_id 和 agent_id 是分开的。
    现在是一对一，未来一个 Agent 可能有多个钱包（比如一个主营账户 + 一个托管账户）。
    """

    wallet_id: str
    agent_id: str
    currency: str
    created_at: str
    overdraft_limit: float = 0.0
    frozen: bool = False

    # 运行时注入的依赖（不参与序列化）
    _ledger: Ledger = None  # type: ignore[assignment]
    _db: Database = None  # type: ignore[assignment]

    # ------------------------------------------------------------
    # 余额
    # ------------------------------------------------------------
    @property
    def balance(self) -> float:
        """
        余额 = 向账本实时查询。

        这里刻意做成 property 而不是存字段，
        就是为了让「余额」永远是账本的推导结果，无法被直接赋值。
        想给 balance 赋值会直接报 AttributeError —— 这正是我们想要的。
        """
        return self._ledger.balance_of(self.agent_id, self.currency)

    @property
    def available(self) -> float:
        """可用余额 = 余额 + 允许透支额度。v0.1 透支额度恒为 0。"""
        return round(self.balance + self.overdraft_limit, self._ledger.amount_precision)

    @property
    def is_insolvent(self) -> bool:
        """
        是否失去偿付能力（破产）。

        v0.1 定义得极简：余额 <= 0 就算。
        注意「破产」不等于「程序崩溃」——
        这个 Agent 还活着、还有记忆，只是不能再承担付不起的经济责任。
        """
        return self.available <= 0

    # ------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------
    def _assert_can_spend(self, amount: float) -> None:
        """花钱前的统一检查。这是 Agent 无法绕过的一道关卡。"""
        if self.frozen:
            raise FrozenWalletError(
                f"钱包 {self.wallet_id} 已被冻结，无法支出 {amount} {self.currency}"
            )

        if amount < 0:
            raise WalletError(f"支出金额不能为负：{amount}")

        if amount > self.available:
            raise InsufficientFundsError(
                f"{self.agent_id} 余额不足："
                f"需要 {amount} {self.currency}，"
                f"可用 {self.available} {self.currency}"
            )

    # ------------------------------------------------------------
    # 铸币（只有系统能调用）
    # ------------------------------------------------------------
    def mint(self, amount: float, reason: str = "genesis_mint") -> Transaction:
        """
        创世铸币：给这个钱包凭空发放货币。

        【安全提示】这是整个系统里唯一能无中生有创造货币的方法。
        只有 core/economy.py 在「新 Agent 出生」时才会调用它。
        Agent 自己永远拿不到调用路径。
        """
        if amount <= 0:
            raise WalletError(f"铸币数量必须大于 0，收到 {amount}")

        return self._ledger.record(
            tx_type=TX_GENESIS_MINT,
            amount=amount,
            from_agent=None,          # None = 货币从虚无中产生
            to_agent=self.agent_id,
            reason=reason,
        )

    # ------------------------------------------------------------
    # 转账
    # ------------------------------------------------------------
    def withdraw(
        self,
        amount: float,
        reason: Optional[str] = None,
        task_id: Optional[str] = None,
        contract_id: Optional[str] = None,
    ) -> Transaction:
        """
        支出：把钱付给系统金库。

        用途：支付 LLM 成本、手续费等「付给系统而不是付给某个 Agent」的钱。
        """
        amount = self._ledger._validate_amount(amount)
        self._assert_can_spend(amount)

        return self._ledger.record(
            tx_type=TX_TRANSFER,
            amount=amount,
            from_agent=self.agent_id,
            to_agent=SYSTEM_TREASURY,
            reason=reason or "withdraw",
            task_id=task_id,
            contract_id=contract_id,
        )

    def deposit(self, amount: float, reason: Optional[str] = None) -> Transaction:
        """
        入金：从系统金库收钱。

        用途：未来做「平台补贴」或者「退款」。
        v0.1 用得少，但先留着，保证 Wallet 接口完整。
        """
        amount = self._ledger._validate_amount(amount)
        return self._ledger.record(
            tx_type=TX_TRANSFER,
            amount=amount,
            from_agent=SYSTEM_TREASURY,
            to_agent=self.agent_id,
            reason=reason or "deposit",
        )

    def transfer_to(
        self,
        recipient: "Wallet",
        amount: float,
        reason: Optional[str] = None,
        task_id: Optional[str] = None,
        contract_id: Optional[str] = None,
    ) -> Transaction:
        """
        转账给另一个钱包。

        这是 Agent 之间付钱的标准方式。
        注意：调用方必须有 recipient 的 Wallet 引用，
        也就是说必须经过 Economy 层派发，不能自己乱造。
        """
        if recipient.currency != self.currency:
            raise WalletError(
                f"币种不一致，无法转账："
                f"{self.agent_id} 用 {self.currency}，"
                f"{recipient.agent_id} 用 {recipient.currency}"
            )

        if recipient.agent_id == self.agent_id:
            raise WalletError("不能给自己转账")

        if recipient.frozen:
            raise WalletError(f"收款方 {recipient.agent_id} 的钱包已被冻结")

        amount = self._ledger._validate_amount(amount)
        self._assert_can_spend(amount)

        return self._ledger.record(
            tx_type=TX_TRANSFER,
            amount=amount,
            from_agent=self.agent_id,
            to_agent=recipient.agent_id,
            reason=reason or "transfer",
            task_id=task_id,
            contract_id=contract_id,
        )

    # ------------------------------------------------------------
    # 记录 LLM 成本
    # ------------------------------------------------------------
    def charge_llm_cost(
        self,
        cost_ai: float,
        provider: str,
        model: str,
        task_id: Optional[str] = None,
    ) -> Transaction:
        """
        支付一次模型调用的成本。

        【这就是 AI Economy 的关键差异点】
        普通 Agent 项目里，调用 LLM 是免费的；
        在这个系统里，调用 LLM 是【真实的经营成本】，会让钱包变少。

        所以一个 Agent 如果一直亏本调用模型，最终会破产。
        这才是「Agent 靠经济系统生存」的真正含义。
        """
        cost_ai = self._ledger._validate_amount(cost_ai)

        if cost_ai == 0:
            # 成本为 0（比如本地模型）也要记一笔账，
            # 这样 llm_calls 和 ledger 永远能对上。
            return self._ledger.record(
                tx_type=TX_LLM_COST,
                amount=0.0,
                from_agent=self.agent_id,
                to_agent=SYSTEM_TREASURY,
                reason=f"llm_cost:{provider}/{model}",
                task_id=task_id,
                memo={"provider": provider, "model": model, "zero_cost": True},
            )

        self._assert_can_spend(cost_ai)

        return self._ledger.record(
            tx_type=TX_LLM_COST,
            amount=cost_ai,
            from_agent=self.agent_id,
            to_agent=SYSTEM_TREASURY,
            reason=f"llm_cost:{provider}/{model}",
            task_id=task_id,
            memo={"provider": provider, "model": model},
        )

    # ------------------------------------------------------------
    # 账单
    # ------------------------------------------------------------
    def statement(self, limit: int = 50) -> List[Transaction]:
        """这个钱包的流水（收入 + 支出）。"""
        return self._ledger.history(agent_id=self.agent_id, limit=limit)

    def summary(self) -> Dict[str, Any]:
        """
        钱包概览。给启动横幅和日志用。
        """
        return {
            "agent_id": self.agent_id,
            "wallet_id": self.wallet_id,
            "currency": self.currency,
            "balance": self.balance,
            "available": self.available,
            "frozen": self.frozen,
            "insolvent": self.is_insolvent,
        }

    def __str__(self) -> str:
        flag = " [FROZEN]" if self.frozen else ""
        insolvent = " [INSOLVENT]" if self.is_insolvent else ""
        return f"{self.agent_id}: {self.balance:g} {self.currency}{flag}{insolvent}"

    def __repr__(self) -> str:
        return (
            f"Wallet(agent_id={self.agent_id!r}, "
            f"balance={self.balance}, currency={self.currency!r})"
        )


# ============================================================
# 钱包仓库：负责「从数据库读/写钱包」
# ============================================================
class WalletRepository:
    """
    钱包的持久化仓库。

    为什么单独抽出来？
        让 Wallet 这个 dataclass 保持「纯数据 + 行为」，
        而把「怎么存进 SQLite」这件事分离出去。
        以后换数据库，只需要重写这个类。
    """

    def __init__(self, db: Database, ledger: Ledger):
        self.db = db
        self.ledger = ledger

    def create(self, agent_id: str, currency: str = "AI") -> Wallet:
        """为一个 Agent 创建钱包。如果已存在就返回已有的。"""
        existing = self.get(agent_id)
        if existing is not None:
            return existing

        wallet_id = f"wallet_{agent_id}"
        created_at = utc_now_iso()

        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_WALLETS}
                    (wallet_id, agent_id, currency, created_at, overdraft_limit, frozen)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (wallet_id, agent_id, currency, created_at, 0.0, 0),
            )

        wallet = Wallet(
            wallet_id=wallet_id,
            agent_id=agent_id,
            currency=currency,
            created_at=created_at,
            overdraft_limit=0.0,
            frozen=False,
        )
        self._attach(wallet)
        return wallet

    def get(self, agent_id: str) -> Optional[Wallet]:
        """按 agent_id 取钱包，不存在返回 None。"""
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_WALLETS} WHERE agent_id = ?",
            (agent_id,),
        )
        if row is None:
            return None

        wallet = Wallet(
            wallet_id=row["wallet_id"],
            agent_id=row["agent_id"],
            currency=row["currency"],
            created_at=row["created_at"],
            overdraft_limit=float(row["overdraft_limit"]),
            frozen=bool(row["frozen"]),
        )
        self._attach(wallet)
        return wallet

    def get_or_create(self, agent_id: str, currency: str = "AI") -> Wallet:
        wallet = self.get(agent_id)
        return wallet if wallet is not None else self.create(agent_id, currency)

    def set_frozen(self, agent_id: str, frozen: bool) -> None:
        """冻结/解冻钱包。未来仲裁、风控会用到。"""
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE {TABLE_WALLETS} SET frozen = ? WHERE agent_id = ?",
                (1 if frozen else 0, agent_id),
            )

    def all_wallets(self) -> List[Wallet]:
        rows = self.db.fetchall(
            f"SELECT agent_id FROM {TABLE_WALLETS} ORDER BY agent_id"
        )
        wallets = []
        for row in rows:
            wallet = self.get(row["agent_id"])
            if wallet is not None:
                wallets.append(wallet)
        return wallets

    def _attach(self, wallet: Wallet) -> None:
        """把 Ledger 和 DB 依赖注入钱包。"""
        wallet._ledger = self.ledger
        wallet._db = self.db
