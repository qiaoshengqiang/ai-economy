"""
Money —— 金额值类型
============================================================

【为什么需要一个专门的类型】

    裸数字没有货币语义：

        100          ← 100 什么？AIC？美元？100 个什么？

    而 AI Economy OS 里到处都是钱：预算、报价、成交价、账本。
    如果这些地方都用裸数字，迟早会出现
    「把 AIC 的数额和美元的成本直接相加」这种错误 ——
    而且不会有任何报错，只会算出一个错的数。

    所以钱必须是一个【带单位的类型】，
    让「不同货币不能直接比较和运算」由代码保证，而不是靠人记住。

【为什么放在 core/ 而不是 core/task.py 里】

    因为将来不只 Task 需要它：
        · 协议 payload 的 budget / price（PROTOCOL.md 7.1）
        · Ledger 的记录
        · 未来的 Escrow / 结算

    它是共享的【稳定值类型】，所以单独一个文件。

【这个文件】

    · 不做任何 I/O
    · 不依赖 llm / communication / wallet / ledger
    · 不依赖 config（货币代码由调用方传入，见下面的 DEFAULT_CURRENCY 说明）
    · 纯计算 + 校验
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Union

# 数值类型别名（int 和 float 都接受）
Number = Union[int, float]


# ============================================================
# 货币代码
# ============================================================
# 【注意】项目货币代码在 Step 3.7 统一为 AIC（= AI Coin）。
#
# 为什么要在这里放一个常量？
#     因为它是【协议级常量】，不是可调参数：
#     PROTOCOL.md 规定货币代码就是 AIC，
#     钱包、账本、协议 payload 必须一致。
#
#     但真正的权威来源仍然是 config.yaml 的 economy.currency。
#     所以有一条测试盯着这两个值必须相等
#     （tests/test_money.py::test_default_currency_matches_config）——
#     如果哪天配置改了而这里没改，测试会红。
AIC = "AIC"

# 不指定货币时用的默认值
DEFAULT_CURRENCY = AIC


# ============================================================
# 异常
# ============================================================
class MoneyError(Exception):
    """金额相关错误的基类。"""


class InvalidAmountError(MoneyError):
    """金额本身非法（负数、NaN、非数字）。"""


class InvalidCurrencyError(MoneyError):
    """货币代码非法（空、不是字符串）。"""


class CurrencyMismatchError(MoneyError):
    """
    两种不同货币被放在一起比较或运算。

    【这个异常是故意的，不是麻烦】
        静默比较 100 AIC 和 100 USD 会得到「相等」——
        这种 bug 一旦混进账本，几乎查不出来。
        所以宁可报错。
    """


# ============================================================
# 金额
# ============================================================
@dataclass(frozen=True, order=False)
class Money:
    """
    一个带货币单位的金额。

    用法：
        budget = Money(100, "AIC")
        price  = Money(25)              # 默认 AIC

        price < budget                  # True
        price == Money(25, "AIC")       # True
        price == Money(25, "USD")       # 抛 CurrencyMismatchError

    设计要点：
        · frozen —— 金额不可变。改金额应该是「换一个新对象」，
          而不是「悄悄改掉旧的那个」。钱的数额被就地修改是最危险的事之一。
        · amount 允许 int 或 float，但内部统一成 float 存
        · 不允许负数 —— 方向由账本的 from/to 表达，不靠符号
          （和 core/ledger.py 的「amount 永远是正数」约定一致）
    """

    amount: float
    currency: str = DEFAULT_CURRENCY

    # ------------------------------------------------------------
    def __post_init__(self) -> None:
        # --- 校验货币 ---
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise InvalidCurrencyError(
                f"货币代码必须是非空字符串，收到 {self.currency!r}"
            )
        # frozen=True 的对象要用 object.__setattr__ 才能改
        object.__setattr__(self, "currency", self.currency.strip())

        # --- 校验金额 ---
        if isinstance(self.amount, bool):
            # bool 是 int 的子类，True 会变成 1 —— 那是 bug 不是钱
            raise InvalidAmountError("金额不能是布尔值")
        if not isinstance(self.amount, (int, float)):
            raise InvalidAmountError(
                f"金额必须是数字，收到 {type(self.amount).__name__}"
            )

        value = float(self.amount)

        # NaN != NaN，用它自己检测
        if value != value:
            raise InvalidAmountError("金额不能是 NaN")
        if value in (float("inf"), float("-inf")):
            raise InvalidAmountError("金额不能是无穷大")
        if value < 0:
            raise InvalidAmountError(
                f"金额不能是负数，收到 {value}。\n"
                f"转账方向应该由「谁付给谁」表达，不靠金额的正负号。"
            )

        object.__setattr__(self, "amount", value)

    # ------------------------------------------------------------
    # 货币一致性检查（所有比较和运算的入口）
    # ------------------------------------------------------------
    def _check_same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"货币不一致，无法比较：{self.currency} vs {other.currency}\n"
                f"（{self} 与 {other}）"
            )

    # ------------------------------------------------------------
    # 比较
    # ------------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        # 【注意】不同货币直接判不等，而不是抛异常 ——
        # 因为 __eq__ 被用于 `in`、`==` 等场景，
        # 抛异常会让这些场景莫名其妙地炸掉。
        # 真正需要严格的地方（大小比较）才抛。
        if self.currency != other.currency:
            return False
        return self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.amount, self.currency))

    def __lt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return self.amount >= other.amount

    # ------------------------------------------------------------
    # 运算
    # ------------------------------------------------------------
    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        """
        相减。结果为负会抛 InvalidAmountError。

        为什么不允许负数结果？
            因为这通常意味着方向搞反了 —— 那是逻辑错误，
            应该早报错，而不是产生一个负的金额往下传。
        """
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    # ------------------------------------------------------------
    # 转换
    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """
        转成协议用的形状（PROTOCOL.md 7.1）：

            {"amount": 100, "currency": "AIC"}

        整数金额转回 int，避免出现 100.0 这种没必要的浮点写法。
        """
        amount: Number = self.amount
        if self.amount.is_integer():
            amount = int(self.amount)
        return {"amount": amount, "currency": self.currency}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Money":
        """
        从协议形状构造。

        缺 currency 时用默认值 —— 因为历史上 payload 里
        budget 曾经是个裸数字（PROTOCOL.md 5.2 记录了这个遗留）。
        这里给出明确错误，而不是猜。
        """
        if not isinstance(data, Mapping):
            raise MoneyError(
                f"金额必须是 {{amount, currency}} 形式的对象，"
                f"收到 {type(data).__name__}：{data!r}"
            )
        if "amount" not in data:
            raise MoneyError(f"金额缺少 amount 字段：{dict(data)}")
        if "currency" not in data:
            raise MoneyError(
                f"金额缺少 currency 字段：{dict(data)}\n"
                f"（裸数字没有货币语义，必须写明是哪种货币）"
            )
        return cls(amount=data["amount"], currency=data["currency"])

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> "Money":
        return cls(0, currency)

    # ------------------------------------------------------------
    def is_zero(self) -> bool:
        return self.amount == 0

    def __str__(self) -> str:
        # 100 而不是 100.0；25.5 保持 25.5
        text = f"{self.amount:g}"
        return f"{text} {self.currency}"

    def __repr__(self) -> str:
        return f"Money(amount={self.amount!r}, currency={self.currency!r})"
