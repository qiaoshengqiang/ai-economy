"""
Reflection —— Agent 基于 Experience 对自己的认知提出修改建议。

    from reflection import ReflectionProposal, SelfModelWriteService

🔒 边界：
    · Reflection **属于 Agent Cognition**，不是 Runtime 的自动规则
    · 本项目**没有** ReflectionEngine / CentralReflectionEngine /
      StrategyEvolutionEngine / SelfEvolutionEngine
    · Proposal ≠ 写入 —— 必须经过 SelfModelWriteService 校验
"""

from reflection.proposal import (
    KIND_ABANDON_STRATEGY, KIND_CREATE_CLAIM, KIND_CREATE_STRATEGY,
    KIND_RETIRE_CLAIM, KIND_REVISE_CLAIM, KIND_REVISE_STRATEGY,
    NEEDS_TARGET, VALID_KINDS, ReflectionProposal, ValidationResult,
    WriteOutcome, new_proposal_id,
)
from reflection.write_service import SelfModelWriteService

__all__ = [
    "ReflectionProposal", "ValidationResult", "WriteOutcome",
    "SelfModelWriteService", "new_proposal_id",
    "KIND_CREATE_STRATEGY", "KIND_REVISE_STRATEGY", "KIND_ABANDON_STRATEGY",
    "KIND_CREATE_CLAIM", "KIND_REVISE_CLAIM", "KIND_RETIRE_CLAIM",
    "VALID_KINDS", "NEEDS_TARGET",
]
