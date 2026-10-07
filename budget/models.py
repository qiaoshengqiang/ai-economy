"""
Task Production Budget —— 领域模型
========================================================

【🔒 表里只存 4 个事实】

    task_id · budget_aic · currency · mode

`spent_aic` / `remaining_aic` **不落库**（§五 明确要求）。

    它们每次从**真实 Ledger / LLM Cost** 现算。

    如果把它们存成列，就会出现「两个都声称正确的剩余预算」——
    与 `wallets` 表刻意**没有** balance 列是同一条纪律。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


class BudgetMode:
    """
    Budget 的**执行模式**。

    🔴 本轮**只启用 OBSERVE_ONLY**（§三 / §四）。
       `HARD_LIMIT` 只预留结构 —— 不实现、不启用、
       正式生产里绝不允许它阻断 Runtime。
    """

    #: 记录 + 显示 + 进 Context，但**不阻断**任何东西。
    OBSERVE_ONLY = "OBSERVE_ONLY"
    #: 预留：未来 Economic Pressure 实验用。**本轮不启用。**
    HARD_LIMIT = "HARD_LIMIT"


MODE_OBSERVE_ONLY = BudgetMode.OBSERVE_ONLY
MODE_HARD_LIMIT = BudgetMode.HARD_LIMIT
VALID_MODES = (MODE_OBSERVE_ONLY, MODE_HARD_LIMIT)

#: 本轮正式生产使用的模式。
ACTIVE_MODE = MODE_OBSERVE_ONLY


class TaskBudgetError(RuntimeError):
    """Task Budget 的业务错误。"""


@dataclass(frozen=True)
class TaskProductionBudget:
    """
    一个 Task 的生产预算快照。

    🔒 `spent_aic` / `remaining_aic` 是**由服务计算后填进来**的只读值，
       `TaskBudgetStore.create()` **不接受**它们 —— 它们从来没有被写进过表。
    """

    task_id: str
    budget_aic: float
    currency: str = "AIC"
    mode: str = MODE_OBSERVE_ONLY
    created_at: str = ""

    #: 派生：真实已消耗（LLM cost + 已结算的 Agent↔Agent 支出）。
    #: None = **取不到**（不是 0）。0 是一个真实的消耗值。
    spent_aic: Optional[float] = None
    #: 派生：budget - spent。spent 为 None 时这里也是 None。
    remaining_aic: Optional[float] = None

    @property
    def is_exhausted(self) -> bool:
        """预算是**否已用尽**（事实，不是判断）。"""
        return (self.remaining_aic is not None
                and self.remaining_aic <= 0)

    @property
    def is_observe_only(self) -> bool:
        return self.mode == MODE_OBSERVE_ONLY

    @property
    def blocks_production(self) -> bool:
        """
        🔴 这个预算**能不能**阻断生产？

        V1 的答案永远是 **False** —— 因为唯一启用的模式是 OBSERVE_ONLY。

        ⚠️ 这个方法存在不是为了将来偷偷启用 HARD_LIMIT，
           而是为了让「预算没有阻断你」这件事**可以被断言**（有测试）。
        """
        return False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["is_exhausted"] = self.is_exhausted
        d["blocks_production"] = self.blocks_production
        return d
