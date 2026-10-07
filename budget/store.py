"""
TaskBudgetStore —— Budget 的持久化（**只有 4 列**）
========================================================

    task_id · budget_aic · currency · mode · created_at

🔒 **没有 spent_aic 列，也没有 remaining_aic 列。**

    这不是「暂时没加」，而是设计：
    一旦存下来，Ledger 一变它们就过期，而**没有任何机制**会来对齐。
    与 `wallets` 表刻意没有 balance 列是同一条纪律。
"""

from __future__ import annotations

import sqlite3
from typing import Any, List, Optional

from core.identity import utc_now_iso
from budget.models import (
    MODE_OBSERVE_ONLY, VALID_MODES, TaskBudgetError, TaskProductionBudget,
)

TABLE_TASK_BUDGETS = "task_production_budgets"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE_TASK_BUDGETS} (
    task_id     TEXT PRIMARY KEY,
    budget_aic  REAL NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'AIC',
    mode        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _row_to_budget(row: sqlite3.Row) -> TaskProductionBudget:
    return TaskProductionBudget(
        task_id=row["task_id"], budget_aic=float(row["budget_aic"]),
        currency=row["currency"], mode=row["mode"],
        created_at=row["created_at"],
        # 🔒 spent / remaining 留空 —— 它们由 Service 现算，不来自这里
        spent_aic=None, remaining_aic=None,
    )


class TaskBudgetStore:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.db.transaction() as conn:
            conn.executescript(_SCHEMA)

    def set_budget(
        self,
        task_id: str,
        budget_aic: float,
        *,
        mode: str = MODE_OBSERVE_ONLY,
        currency: str = "AIC",
    ) -> TaskProductionBudget:
        """
        设定（或覆盖）一个 Task 的预算。

        🔒 签名里**没有** spent / remaining ——
           它们不是可写事实，所以连入口都不给。
        """
        if not str(task_id or "").strip():
            raise TaskBudgetError("task_id 不能为空")
        if mode not in VALID_MODES:
            raise TaskBudgetError(
                f"未知 budget mode {mode!r}；合法取值：{list(VALID_MODES)}")
        value = float(budget_aic)
        if value != value:
            raise TaskBudgetError("budget_aic 不能是 NaN")
        if value < 0:
            raise TaskBudgetError(f"budget_aic 不能为负：{value}")

        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_TASK_BUDGETS}"
                " (task_id, budget_aic, currency, mode, created_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET"
                "   budget_aic=excluded.budget_aic,"
                "   currency=excluded.currency,"
                "   mode=excluded.mode",
                (task_id, value, currency, mode, now),
            )
        got = self.get(task_id)
        assert got is not None
        return got

    def get(self, task_id: str) -> Optional[TaskProductionBudget]:
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_TASK_BUDGETS} WHERE task_id=?", (task_id,))
        return _row_to_budget(row) if row else None

    def list_budgets(self) -> List[TaskProductionBudget]:
        return [_row_to_budget(r) for r in self.db.fetchall(
            f"SELECT * FROM {TABLE_TASK_BUDGETS} ORDER BY created_at")]
