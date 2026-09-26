"""
Test: Money —— 金额值类型（Step 4A）
============================================================

【为什么金额要单独测试】

    钱算错是这类系统里最严重、也最难发现的 bug。
    这一组测试盯的是「不同货币不能被混着算」和
    「金额不能被就地改掉」这两件事。
"""

from __future__ import annotations

import pytest

from core.money import (
    AIC,
    DEFAULT_CURRENCY,
    CurrencyMismatchError,
    InvalidAmountError,
    InvalidCurrencyError,
    Money,
    MoneyError,
)


# ============================================================
# 1. 构造与校验
# ============================================================
class TestConstruction:
    def test_basic(self):
        money = Money(100)

        assert money.amount == 100
        assert money.currency == "AIC"

    def test_explicit_currency(self):
        assert Money(25, "AIC").currency == "AIC"

    def test_int_and_float(self):
        assert Money(100).amount == 100.0
        assert Money(25.5).amount == 25.5

    def test_zero_is_allowed(self):
        assert Money(0).is_zero()

    def test_default_currency_constant(self):
        assert DEFAULT_CURRENCY == "AIC"
        assert AIC == "AIC"

    @pytest.mark.parametrize("amount", [-1, -0.01, -100])
    def test_negative_amount_rejected(self, amount):
        """金额不能是负数 —— 方向由「谁付给谁」表达，不靠符号。"""
        with pytest.raises(InvalidAmountError):
            Money(amount)

    def test_nan_rejected(self):
        with pytest.raises(InvalidAmountError):
            Money(float("nan"))

    @pytest.mark.parametrize("amount", [float("inf"), float("-inf")])
    def test_infinity_rejected(self, amount):
        with pytest.raises(InvalidAmountError):
            Money(amount)

    def test_bool_rejected(self):
        """
        True 是 int 的子类，会变成 1 —— 那是 bug 不是钱。
        """
        with pytest.raises(InvalidAmountError):
            Money(True)

    def test_non_numeric_rejected(self):
        with pytest.raises(InvalidAmountError):
            Money("100")

    @pytest.mark.parametrize("currency", ["", "   ", None, 123])
    def test_invalid_currency_rejected(self, currency):
        with pytest.raises(InvalidCurrencyError):
            Money(100, currency)

    def test_currency_is_stripped(self):
        assert Money(100, "  AIC  ").currency == "AIC"


# ============================================================
# 2. 不可变性
# ============================================================
class TestImmutability:
    def test_frozen(self):
        money = Money(100)

        with pytest.raises(Exception):
            money.amount = 999

    def test_hashable(self):
        assert Money(100) in {Money(100)}
        assert len({Money(100), Money(100), Money(25)}) == 2

    def test_arithmetic_returns_new_object(self):
        """运算不该改掉原来的金额。"""
        original = Money(100)
        result = original + Money(25)

        assert original.amount == 100        # 没被改
        assert result.amount == 125
        assert result is not original


# ============================================================
# 3. 货币必须一致（最重要的一组）
# ============================================================
class TestCurrencySafety:
    def test_comparison_across_currencies_raises(self):
        """
        【关键】100 AIC 和 100 USD 不能比大小。

        静默比较会得出一堆看似合理的结果，
        这种 bug 混进账本几乎查不出来。
        """
        with pytest.raises(CurrencyMismatchError):
            Money(100, "AIC") > Money(100, "USD")

        with pytest.raises(CurrencyMismatchError):
            Money(100, "AIC") < Money(100, "USD")

    def test_addition_across_currencies_raises(self):
        with pytest.raises(CurrencyMismatchError):
            Money(100, "AIC") + Money(100, "USD")

    def test_subtraction_across_currencies_raises(self):
        with pytest.raises(CurrencyMismatchError):
            Money(100, "AIC") - Money(100, "USD")

    def test_equality_across_currencies_is_false(self):
        """
        相等判断**不抛异常**，返回 False。

        因为 __eq__ 被用于 `in`、`==` 等场景，
        抛异常会让那些场景莫名其妙地炸掉。
        """
        assert (Money(100, "AIC") == Money(100, "USD")) is False

    def test_same_currency_works(self):
        assert Money(100, "AIC") > Money(25, "AIC")
        assert Money(100, "AIC") == Money(100, "AIC")
        assert (Money(100, "AIC") + Money(25, "AIC")).amount == 125


# ============================================================
# 4. 运算
# ============================================================
class TestArithmetic:
    def test_addition(self):
        assert Money(100) + Money(25) == Money(125)

    def test_subtraction(self):
        assert Money(100) - Money(25) == Money(75)

    def test_subtraction_below_zero_rejected(self):
        """
        结果为负通常意味着方向搞反了 —— 早报错比往下传好。
        """
        with pytest.raises(InvalidAmountError):
            Money(25) - Money(100)

    def test_subtraction_to_zero_ok(self):
        assert (Money(100) - Money(100)).is_zero()


# ============================================================
# 5. 序列化（协议形状）
# ============================================================
class TestSerialization:
    def test_to_dict_matches_protocol_shape(self):
        """必须等于 PROTOCOL.md 7.1 定义的形状。"""
        assert Money(100).to_dict() == {"amount": 100, "currency": "AIC"}

    def test_integer_amount_stays_integer(self):
        """100 不该变成 100.0 —— 协议示例里写的是 100。"""
        assert Money(100).to_dict()["amount"] == 100
        assert isinstance(Money(100).to_dict()["amount"], int)

    def test_decimal_amount_stays_float(self):
        assert Money(25.5).to_dict()["amount"] == 25.5

    def test_roundtrip(self):
        original = Money(100)
        assert Money.from_dict(original.to_dict()) == original

    def test_from_dict_missing_currency_rejected(self):
        """
        裸数字（或只有 amount）要被拒绝，而不是猜一个货币。

        这正是 PROTOCOL.md 5.2 记录的那个遗留问题：
        历史上 budget 曾经是裸数字，而且没有报错。
        """
        with pytest.raises(MoneyError):
            Money.from_dict({"amount": 100})

    def test_from_dict_missing_amount_rejected(self):
        with pytest.raises(MoneyError):
            Money.from_dict({"currency": "AIC"})

    def test_from_dict_non_mapping_rejected(self):
        """`budget: 100` 这种裸数字必须报错，且错误信息要指出问题。"""
        with pytest.raises(MoneyError) as exc:
            Money.from_dict(100)

        assert "amount" in str(exc.value)

    def test_str_is_readable(self):
        assert str(Money(100)) == "100 AIC"
        assert str(Money(25.5)) == "25.5 AIC"


# ============================================================
# 6. 与项目配置一致
# ============================================================
class TestConfigConsistency:
    def test_default_currency_matches_config(self):
        """
        【这条测试是 Step 3.7 那个教训的产物】

        Step 3.7 之前，协议示例用 AIC 而 config.yaml 用 AI，
        两边不一致，最后导致老 Agent 和新 Agent 无法转账。

        所以现在有一条测试盯着「代码里的默认货币 == 配置里的货币」。
        谁改了一边忘了另一边，这里会红。
        """
        from core.config import load_global_config

        configured = load_global_config().get("economy.currency")

        assert DEFAULT_CURRENCY == configured, (
            f"代码默认货币 {DEFAULT_CURRENCY!r} 与配置 {configured!r} 不一致。\n"
            f"两处必须统一（见 PROTOCOL.md 7.1）。"
        )

    def test_aic_is_the_only_currency_in_use(self):
        assert DEFAULT_CURRENCY == "AIC"
