"""
Task Production Budget V1 —— 为一个 Task 准备的**生产资源边界**
================================================================

【🔴 五个概念不得混为一谈（任务书 §一）】

    Token                   底层认知资源消耗
    AIC Cost                实际生产资源的**经济计价**
    Agent Wallet            Agent **自有**资产
    Task Production Budget  **为某个 Task** 提供的生产资金/资源上限
    Task Price              Agent-to-Agent **生产能力交换**价格

【❌ 明令禁止】

    **AIC = Token Quota**

    不许实现「1 AIC 等于最多可调用 X tokens」这种强绑定作为系统硬约束。
    AIC/token 汇率只是实验参数，不是系统结构。

【✅ 它是什么】

    一个 Task 级配置事实：这个 Task 被允许投入多少经济资源。

    它不是账本，不是钱包，不改 Agent 的资产语义。

        Ledger   → 实际发生了什么经济变化（唯一事实源）
        Budget   → 这个 Task 的**资金边界/配置**
        spent    → **派生**自 Ledger / LLM Cost，不是自己累加的
        remaining→ budget - spent，**派生**

【本轮只启用 OBSERVE_ONLY（§三 / §四）】

    Budget 会记录、会进入 Cognitive Context、会显示已消耗与剩余，
    但**不阻断** LLM Runtime —— 即使超预算，Runtime 仍可继续生产。

    目的是先观察：Agent 在经济事实存在、但**没有硬经济压力**时的正常行为。
"""

from budget.models import (
    MODE_HARD_LIMIT,
    MODE_OBSERVE_ONLY,
    VALID_MODES,
    BudgetMode,
    TaskBudgetError,
    TaskProductionBudget,
)
from budget.service import TaskBudgetService
from budget.store import TaskBudgetStore

__all__ = [
    "TaskBudgetStore", "TaskBudgetService", "TaskProductionBudget",
    "TaskBudgetError", "BudgetMode", "MODE_OBSERVE_ONLY", "MODE_HARD_LIMIT",
    "VALID_MODES",
]
