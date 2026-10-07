"""
TaskBudgetService —— 把 Budget 与**真实 Ledger** 接起来
========================================================

【🔒 spent_aic 从哪来（§五 / §六）】

    必须是**真实**的经济数据，**禁止实验器自己累加**：

        spent = Σ llm_calls.cost_ai           (task_id = 本 Task)
              + Σ ledger_transactions.amount  (task_id = 本 Task
                                               AND tx_type='TRANSFER')

    第一项是真实的 LLM 认知成本；第二项是真实的 Agent↔Agent 结算支出。

    🔒 **不新建第二套交易账本。** 两个来源都是系统已经在写的事实表。

【⚠️ 一个如实的限制】

    `llm_calls.task_id` 这一列存在，但**只有调用方显式传 task_id 时才有值**。
    (`Brain.generate(task_id=...)` 支持它，组合根也把它透传到 recorder/
    ledger memo。)

    如果生产运行没有传，spent 就会是 0 —— 那是**事实**，
    不是「没有花钱」。所以本服务区分：

        spent_aic = 0.0    → 真的查到 0
        spent_aic = None   → **查不到**（例如账本是坏的）

    并把「有多少次调用没有 task_id」一并报出来，供审计。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from budget.models import (
    MODE_OBSERVE_ONLY, TaskBudgetError, TaskProductionBudget,
)

#: 一个 Task 的结算支出类型（Agent → Agent）。
TRANSFER = "TRANSFER"


class TaskBudgetService:
    """
    🔒 依赖全部是**注入的只读回调** —— 本模块不 import Ledger / Economy。

    参数：
        llm_cost_of_task(task_id) -> float | None
        settled_cost_of_task(task_id) -> float | None
        untagged_llm_calls() -> int | None
            有多少次 LLM 调用**没有** task_id（审计用）。
    """

    def __init__(
        self,
        store: Any,
        *,
        llm_cost_of_task: Optional[Callable[[str], Optional[float]]] = None,
        settled_cost_of_task: Optional[Callable[[str], Optional[float]]] = None,
        untagged_llm_calls: Optional[Callable[[], Optional[int]]] = None,
    ) -> None:
        self.store = store
        self.llm_cost_of_task = llm_cost_of_task
        self.settled_cost_of_task = settled_cost_of_task
        self.untagged_llm_calls = untagged_llm_calls

    # ------------------------------------------------------------
    def set_budget(self, task_id: str, budget_aic: float, **kw: Any):
        return self.store.set_budget(task_id, budget_aic, **kw)

    # ------------------------------------------------------------
    def snapshot(self, task_id: str) -> Optional[TaskProductionBudget]:
        """
        现算一次快照。**每次调用都重新问账本** —— 不缓存。

        返回 None 表示这个 Task 还没有设预算。
        """
        base = self.store.get(task_id)
        if base is None:
            return None

        llm = self._safe(self.llm_cost_of_task, task_id)
        settled = self._safe(self.settled_cost_of_task, task_id)

        if llm is None and settled is None:
            spent = None            # 查不到 ≠ 0
        else:
            spent = round((llm or 0.0) + (settled or 0.0), 10)

        remaining = None if spent is None else round(
            float(base.budget_aic) - spent, 10)

        return TaskProductionBudget(
            task_id=base.task_id, budget_aic=base.budget_aic,
            currency=base.currency, mode=base.mode,
            created_at=base.created_at,
            spent_aic=spent, remaining_aic=remaining)

    # ------------------------------------------------------------
    def audit(self, task_id: str) -> Dict[str, Any]:
        """给实验报告用的可观测信息（§十四 / §十五）。"""
        snap = self.snapshot(task_id)
        if snap is None:
            return {"task_id": task_id, "budget": "NOT SET"}
        out = snap.to_dict()
        out["untagged_llm_calls"] = self._safe_int(self.untagged_llm_calls)
        out["warning"] = (
            "llm_calls.task_id 为空 → spent 可能低估。"
            "生产运行必须把 task_id 传给 Brain.generate()。"
            if (out.get("untagged_llm_calls") or 0) > 0 else None)
        return out

    # ------------------------------------------------------------
    @staticmethod
    def _safe(fn: Optional[Callable], *args: Any) -> Optional[float]:
        """
        🔒 取不到就是 None，**不返回 0.0 顶替** ——
           0 是一个真实的消耗值；把「不知道」写成 0
           会让 Agent 以为自己还有全部预算。
        """
        if fn is None:
            return None
        try:
            value = fn(*args)
        except Exception:                                     # noqa: BLE001
            return None
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(fn: Optional[Callable]) -> Optional[int]:
        if fn is None:
            return None
        try:
            return int(fn())
        except Exception:                                     # noqa: BLE001
            return None
