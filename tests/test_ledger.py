"""
Test: Ledger（交易账本）
============================================================

验证目标：
    1. 每笔交易都有完整字段（时间、发送者、接收者、金额、原因）
    2. 账本是 append-only，余额由求和得来
    3. 铸币是全系统唯一创造货币的入口
    4. 非法金额被拒绝
    5. 审计等式成立：钱包之和 = 货币总量
"""

from __future__ import annotations

import pytest

from core.ledger import (
    LedgerError,
    SYSTEM_TREASURY,
    TX_GENESIS_MINT,
    TX_TRANSFER,
)


# ============================================================
# 基本记账
# ============================================================
def test_empty_ledger(ledger):
    assert ledger.count() == 0
    assert ledger.total_supply() == 0
    assert ledger.balance_of("nobody") == 0


def test_record_and_read_back(ledger):
    tx = ledger.record(
        tx_type=TX_GENESIS_MINT,
        amount=100,
        from_agent=None,
        to_agent="agent_a",
        reason="genesis_mint",
    )

    assert tx.tx_id.startswith("tx_")
    assert tx.tx_type == TX_GENESIS_MINT
    assert tx.amount == 100
    assert tx.from_agent is None
    assert tx.to_agent == "agent_a"
    assert tx.created_at  # 非空

    assert ledger.balance_of("agent_a") == 100


def test_transaction_has_all_required_fields(ledger):
    """
    需求文档要求的字段一个都不能少：
    tx_id / from / to / amount / asset / reason / task_id / timestamp
    """
    tx = ledger.record(
        tx_type=TX_TRANSFER,
        amount=10,
        from_agent="agent_a",
        to_agent="agent_b",
        reason="task_payment",
        task_id="task_001",
    )

    data = tx.to_dict()

    for field in (
        "tx_id", "from", "to", "amount", "asset",
        "reason", "task_id", "timestamp",
    ):
        assert field in data, f"交易记录缺少字段：{field}"

    assert data["from"] == "agent_a"
    assert data["to"] == "agent_b"
    assert data["amount"] == 10
    assert data["asset"] == "AIC"
    assert data["reason"] == "task_payment"
    assert data["task_id"] == "task_001"
    assert data["timestamp"]


# ============================================================
# 余额计算
# ============================================================
def test_balance_is_sum_of_transactions(ledger):
    ledger.record(TX_GENESIS_MINT, 100, None, "agent_a", reason="mint")
    ledger.record(TX_TRANSFER, 30, "agent_a", "agent_b", reason="payment")
    ledger.record(TX_TRANSFER, 5, "agent_b", "agent_a", reason="refund")

    assert ledger.balance_of("agent_a") == 75   # 100 - 30 + 5
    assert ledger.balance_of("agent_b") == 25   # 30 - 5


def test_ledger_is_append_only_by_design(ledger):
    """
    账本没有提供修改或删除交易的方法。

    这条测试用「检查接口」的方式来保护架构：
    如果以后有人加了 update_transaction / delete_transaction，
    这条测试会失败，提醒他这违反了经济系统的原则。

    正确的纠错方式是「再记一笔反向交易」（冲正），
    而不是抹掉历史。
    """
    forbidden = (
        "update_transaction",
        "delete_transaction",
        "set_balance",
        "edit_amount",
    )
    for method_name in forbidden:
        assert not hasattr(ledger, method_name), (
            f"Ledger 不应该有 {method_name} 方法 —— "
            f"账本必须是 append-only 的。"
            f"要纠正错误请记一笔反向交易。"
        )


# ============================================================
# 货币总量
# ============================================================
def test_only_mint_creates_money(ledger):
    """
    转账不改变货币总量，只有铸币会。
    """
    assert ledger.total_supply() == 0

    ledger.record(TX_GENESIS_MINT, 100, None, "agent_a")
    assert ledger.total_supply() == 100

    ledger.record(TX_TRANSFER, 40, "agent_a", "agent_b")
    assert ledger.total_supply() == 100   # 转账不增加总量

    ledger.record(TX_GENESIS_MINT, 100, None, "agent_b")
    assert ledger.total_supply() == 200


# ============================================================
# 拒绝非法输入
# ============================================================
def test_reject_negative_amount(ledger):
    with pytest.raises(LedgerError):
        ledger.record(TX_TRANSFER, -10, "agent_a", "agent_b")


def test_reject_non_numeric_amount(ledger):
    with pytest.raises(LedgerError):
        ledger.record(TX_TRANSFER, "一百块", "agent_a", "agent_b")  # type: ignore[arg-type]


def test_reject_nan_amount(ledger):
    with pytest.raises(LedgerError):
        ledger.record(TX_TRANSFER, float("nan"), "agent_a", "agent_b")


def test_reject_unknown_tx_type(ledger):
    with pytest.raises(LedgerError):
        ledger.record("MAGIC_MONEY", 999, None, "agent_a")


def test_reject_transaction_with_no_parties(ledger):
    with pytest.raises(LedgerError):
        ledger.record(TX_TRANSFER, 10, None, None)


# ============================================================
# 查询
# ============================================================
def test_history_filters_by_agent(ledger):
    ledger.record(TX_GENESIS_MINT, 100, None, "agent_a")
    ledger.record(TX_GENESIS_MINT, 100, None, "agent_b")
    ledger.record(TX_TRANSFER, 10, "agent_a", "agent_b")

    history_a = ledger.history(agent_id="agent_a")
    # 收到铸币 + 转出 10 = 2 条
    assert len(history_a) == 2

    history_b = ledger.history(agent_id="agent_b")
    # 收到铸币 + 收到 10 = 2 条
    assert len(history_b) == 2


def test_history_filters_by_task(ledger):
    ledger.record(TX_TRANSFER, 10, "agent_a", "agent_b", task_id="task_001")
    ledger.record(TX_TRANSFER, 20, "agent_a", "agent_b", task_id="task_002")

    history = ledger.history(task_id="task_001")
    assert len(history) == 1
    assert history[0].task_id == "task_001"


def test_history_is_chronological(ledger):
    for index in range(5):
        ledger.record(TX_TRANSFER, index + 1, "agent_a", "agent_b")

    history = ledger.history(agent_id="agent_a")
    amounts = [tx.amount for tx in history]

    assert amounts == [1, 2, 3, 4, 5]


# ============================================================
# 金额精度
# ============================================================
def test_amount_precision_is_respected(db):
    from core.ledger import Ledger

    ledger = Ledger(db=db, currency="AIC", amount_precision=2)
    ledger.record(TX_GENESIS_MINT, 10.005, None, "agent_a")

    # 保留 2 位小数 -> 会被舍入
    assert ledger.balance_of("agent_a") == 10.0 or ledger.balance_of("agent_a") == 10.01
    # 更重要的是：余额的小数位不会超过配置的精度
    assert len(str(ledger.balance_of("agent_a")).split(".")[-1]) <= 2


def test_system_treasury_is_tracked(ledger):
    """系统金库也走同一本账，不是法外之地。"""
    ledger.record(TX_GENESIS_MINT, 100, None, "agent_a")
    ledger.record(TX_TRANSFER, 10, "agent_a", SYSTEM_TREASURY, reason="fee")

    assert ledger.balance_of(SYSTEM_TREASURY) == 10
    assert ledger.balance_of("agent_a") == 90


# ============================================================
# 金额显示格式
# ============================================================
class TestFormatAmount:
    """
    金额格式化的测试。

    为什么这件事值得专门测？
        因为「钱显示成 1e+05」这种事一旦发生，用户是完全看不懂的。
        Python 的 %g 格式化在大数时会自动切成科学计数法，
        这是个很容易忽略但后果很难看的坑。
    """

    def test_no_scientific_notation_for_large_numbers(self):
        from core.ledger import format_amount

        assert format_amount(100000) == "100000"
        assert format_amount(75000) == "75000"
        assert format_amount(125000) == "125000"
        assert format_amount(1000000) == "1000000"
        assert format_amount(1000000000) == "1000000000"

        for value in (100000, 75000, 125000, 1000000, 1000000000):
            assert "e" not in format_amount(value).lower()

    def test_trims_trailing_zeros(self):
        from core.ledger import format_amount

        assert format_amount(100.0) == "100"
        assert format_amount(100.5) == "100.5"
        assert format_amount(2.8) == "2.8"
        assert format_amount(0.0001) == "0.0001"

    def test_zero(self):
        from core.ledger import format_amount

        assert format_amount(0) == "0"
        assert format_amount(0.0) == "0"

    def test_rounds_to_precision(self):
        from core.ledger import format_amount

        # 超过精度的部分会被四舍五入
        assert format_amount(1.23456789, precision=4) == "1.2346"
        assert format_amount(1.23456789, precision=2) == "1.23"

    def test_small_amounts_keep_their_value(self):
        """
        小额不能被四舍五入成 0 —— 否则 LLM 成本的零头就看不出来了。
        """
        from core.ledger import format_amount

        assert format_amount(0.0001) == "0.0001"
        assert format_amount(0.5) == "0.5"
