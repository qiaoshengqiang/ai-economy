"""Demand Entry V1 —— 人类 Demand → Agent 自建 Task → 串行调度。

> 状态：CURRENT（V1）
> 边界：Root Task only · 直接指定参与者 · 串行 · 无谈判
"""

from demand.models import DemandRun, TurnOutcome
from demand.runner import DemandRunner

__all__ = ["DemandRun", "TurnOutcome", "DemandRunner"]
