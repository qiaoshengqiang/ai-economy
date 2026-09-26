"""
Ledger —— 交易账本
============================================================

【这是整个经济系统的地基，请务必理解这一层】

核心设计：只存交易，不存余额。

    错误做法：
        wallets.balance = wallets.balance - 20     # 谁都能改，说不清钱哪来的

    本系统做法：
        ledger_transactions 追加一条记录
        balance = SUM(到账) - SUM(出账)

    这样带来几个重要性质：
      1. 所有经济变化都有据可查（审计友好）
      2. 余额不可能凭空出现，因为除了 GENESIS_MINT 没人能创建货币
      3. 任意时刻的余额都可以重新计算验证（对账）
      4. 以后把后端换成区块链，只需要替换这个类的实现，
         Agent 和 Wallet 的代码完全不用动

这笔账本是不可变的（append-only）：
    只允许 INSERT，绝不允许 UPDATE 某条交易的金额或 DELETE 它。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso
from storage.database import Database
from storage.models import TABLE_LEDGER

# ============================================================
# 交易类型常量
# ============================================================
# 创世铸币：整个系统里唯一能凭空产生货币的入口
TX_GENESIS_MINT = "GENESIS_MINT"
# 普通转账
TX_TRANSFER = "TRANSFER"
# 托管相关
TX_ESCROW_LOCK = "ESCROW_LOCK"
TX_ESCROW_RELEASE = "ESCROW_RELEASE"
TX_ESCROW_REFUND = "ESCROW_REFUND"
# LLM 成本
TX_LLM_COST = "LLM_COST"
# 手续费（预留）
TX_FEE = "FEE"

VALID_TX_TYPES = {
    TX_GENESIS_MINT,
    TX_TRANSFER,
    TX_ESCROW_LOCK,
    TX_ESCROW_RELEASE,
    TX_ESCROW_REFUND,
    TX_LLM_COST,
    TX_FEE,
}

# 系统账户 ID。铸币、手续费等「非 Agent」的一方用它表示。
SYSTEM_TREASURY = "SYS_TREASURY"


class LedgerError(Exception):
    """账本层的错误基类。"""


class InsufficientFundsError(LedgerError):
    """余额不足。注意：这是经济规则，必须在 Ledger 层拦下。"""


# ============================================================
# 交易数据对象
# ============================================================
@dataclass
class Transaction:
    """
    一条交易记录。

    约定：
      - amount 永远是正数，资金流向由 from_agent -> to_agent 决定。
      - from_agent 为 None 表示「货币被创造出来」（只有铸币会这样）。
      - to_agent 为 None 表示「货币被销毁」（v0.1 不会发生）。
    """

    tx_id: str
    tx_type: str
    amount: float
    from_agent: Optional[str] = None
    to_agent: Optional[str] = None
    asset: str = "AI"
    reason: Optional[str] = None
    task_id: Optional[str] = None
    contract_id: Optional[str] = None
    escrow_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    memo: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "tx_type": self.tx_type,
            "from": self.from_agent,
            "to": self.to_agent,
            "amount": self.amount,
            "asset": self.asset,
            "reason": self.reason,
            "task_id": self.task_id,
            "contract_id": self.contract_id,
            "escrow_id": self.escrow_id,
            "timestamp": self.created_at,
            "memo": self.memo,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Transaction":
        return cls(
            tx_id=row["tx_id"],
            tx_type=row["tx_type"],
            amount=float(row["amount"]),
            from_agent=row["from_agent"],
            to_agent=row["to_agent"],
            asset=row["asset"],
            reason=row["reason"],
            task_id=row["task_id"],
            contract_id=row["contract_id"],
            escrow_id=row["escrow_id"],
            created_at=row["created_at"],
            memo=row["memo"],
        )

    def __str__(self) -> str:
        arrow_from = self.from_agent or "∅(mint)"
        arrow_to = self.to_agent or "∅(burn)"
        return (
            f"[{self.tx_id}] {self.tx_type}: "
            f"{arrow_from} -> {arrow_to} : {self.amount:g} {self.asset}"
        )


def new_tx_id() -> str:
    """生成交易 ID，形如 tx_000001 —— 但要保证全局唯一。"""
    return f"tx_{uuid.uuid4().hex[:12]}"


def format_amount(value: float, precision: int = 4) -> str:
    """
    把金额格式化成人类看得懂的样子。

    为什么不能直接用 f"{value:g}" 或 f"{value:.4g}"？
        因为 Python 的 %g 在大数时会切成科学计数法：

            100000   :g     -> "100000"     还行
            100000   :.4g   -> "1e+05"      ← 余额显示成这个就没法看了
            1000000  :g     -> "1e+06"      ← 更糟

        对「钱」来说，科学计数法是绝对不能接受的显示方式。
        你能想象银行 App 显示余额 1e+05 吗？

    所以这里：先用定点小数格式化（永远不会变成科学计数法），
    再把多余的尾随零去掉。

        100000.0  -> "100000"
        75000.5   -> "75000.5"
        2.8       -> "2.8"
        0.0       -> "0"
    """
    # 定点格式化，避免科学计数法
    text = f"{float(value):.{precision}f}"

    # 去掉尾随的 0 和可能剩下的小数点
    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return text or "0"


# ============================================================
# Ledger 本体
# ============================================================
class Ledger:
    """
    交易账本。

    所有会改变「谁有多少钱」的操作，都必须经过这里。
    Agent 不能直接调 Ledger —— 它要通过 Wallet / Economy 层，
    而那一层会做权限、合同、托管等校验。

    参数：
        db:              数据库连接
        currency:        本账本记账的资产符号，默认 "AI"
        amount_precision: 金额保留小数位
    """

    def __init__(
        self,
        db: Database,
        currency: str = "AI",
        amount_precision: int = 4,
    ):
        self.db = db
        self.currency = currency
        self.amount_precision = amount_precision

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------
    def _round(self, amount: float) -> float:
        return round(float(amount), self.amount_precision)

    def _validate_amount(self, amount: float) -> float:
        """
        金额校验。这是经济系统的第一道防线：
        负数金额、非数字金额一律拒绝。
        """
        try:
            value = float(amount)
        except (TypeError, ValueError):
            raise LedgerError(f"金额必须是数字，收到：{amount!r}")

        if value != value:  # NaN 检测
            raise LedgerError("金额不能是 NaN")

        if value < 0:
            raise LedgerError(
                f"金额不能为负数（收到 {value}）。"
                "资金方向请用 from_agent / to_agent 表示，"
                "不要用负号。"
            )

        return self._round(value)

    # ------------------------------------------------------------
    # 写：追加一条交易
    # ------------------------------------------------------------
    def record(
        self,
        tx_type: str,
        amount: float,
        from_agent: Optional[str] = None,
        to_agent: Optional[str] = None,
        reason: Optional[str] = None,
        task_id: Optional[str] = None,
        contract_id: Optional[str] = None,
        escrow_id: Optional[str] = None,
        asset: Optional[str] = None,
        memo: Optional[Any] = None,
        tx_id: Optional[str] = None,
        conn: Optional[Any] = None,
    ) -> Transaction:
        """
        记录一笔交易。

        conn 参数允许把这次写入并入一个外部事务。
        比如「转账」其实要写两条记录（出账+入账视图），
        必须保证它们同生共死 —— 这时就传入同一个 conn。
        """
        if tx_type not in VALID_TX_TYPES:
            raise LedgerError(
                f"未知交易类型 {tx_type!r}。"
                f"合法取值：{sorted(VALID_TX_TYPES)}"
            )

        amount = self._validate_amount(amount)

        if from_agent is None and to_agent is None:
            raise LedgerError("交易的付款方和收款方不能同时为空")

        tx = Transaction(
            tx_id=tx_id or new_tx_id(),
            tx_type=tx_type,
            amount=amount,
            from_agent=from_agent,
            to_agent=to_agent,
            asset=asset or self.currency,
            reason=reason,
            task_id=task_id,
            contract_id=contract_id,
            escrow_id=escrow_id,
            memo=json.dumps(memo, ensure_ascii=False) if memo is not None else None,
        )

        sql = f"""
            INSERT INTO {TABLE_LEDGER} (
                tx_id, tx_type, from_agent, to_agent, amount, asset,
                reason, task_id, contract_id, escrow_id, created_at, memo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            tx.tx_id,
            tx.tx_type,
            tx.from_agent,
            tx.to_agent,
            tx.amount,
            tx.asset,
            tx.reason,
            tx.task_id,
            tx.contract_id,
            tx.escrow_id,
            tx.created_at,
            tx.memo,
        )

        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as own_conn:
                own_conn.execute(sql, params)

        return tx

    # ------------------------------------------------------------
    # 读：余额
    # ------------------------------------------------------------
    def balance_of(self, agent_id: str, asset: Optional[str] = None) -> float:
        """
        计算某个 Agent 的余额。

        注意：这里没有读任何「余额字段」，而是现算的：
            余额 = 收到的钱 - 付出的钱

        如果以后交易量变得很大（几十万条），再加余额快照表做缓存。
        v0.1 完全不需要优化。
        """
        asset = asset or self.currency

        row = self.db.fetchone(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN to_agent   = ? THEN amount ELSE 0 END), 0) AS inflow,
                COALESCE(SUM(CASE WHEN from_agent = ? THEN amount ELSE 0 END), 0) AS outflow
            FROM {TABLE_LEDGER}
            WHERE asset = ?
            """,
            (agent_id, agent_id, asset),
        )

        inflow = float(row["inflow"]) if row else 0.0
        outflow = float(row["outflow"]) if row else 0.0
        return self._round(inflow - outflow)

    def total_supply(self, asset: Optional[str] = None) -> float:
        """
        全系统货币总量。

        用途：审计。铸币之后总量必须等于「所有钱包余额之和」。
        如果对不上，说明账本被写坏了。
        """
        asset = asset or self.currency
        row = self.db.fetchone(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN from_agent IS NULL THEN amount ELSE 0 END), 0)
              - COALESCE(SUM(CASE WHEN to_agent   IS NULL THEN amount ELSE 0 END), 0)
                AS supply
            FROM {TABLE_LEDGER}
            WHERE asset = ?
            """,
            (asset,),
        )
        return self._round(float(row["supply"]) if row else 0.0)

    # ------------------------------------------------------------
    # 读：流水
    # ------------------------------------------------------------
    def history(
        self,
        agent_id: Optional[str] = None,
        limit: int = 100,
        task_id: Optional[str] = None,
    ) -> List[Transaction]:
        """
        查询交易历史。

        agent_id 为空时返回全部流水。
        """
        conditions = []
        params: list = []

        if agent_id:
            conditions.append("(from_agent = ? OR to_agent = ?)")
            params.extend([agent_id, agent_id])

        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_LEDGER}
            {where}
            ORDER BY created_at ASC, rowid ASC
            LIMIT ?
            """,
            tuple(params),
        )
        return [Transaction.from_row(row) for row in rows]

    def count(self) -> int:
        row = self.db.fetchone(f"SELECT COUNT(*) AS n FROM {TABLE_LEDGER}")
        return int(row["n"]) if row else 0

    def __repr__(self) -> str:
        return (
            f"Ledger(currency={self.currency!r}, "
            f"transactions={self.count()}, supply={self.total_supply()})"
        )
