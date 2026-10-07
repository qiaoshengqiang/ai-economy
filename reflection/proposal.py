"""
Reflection Proposal —— **Agent 认知**的产物
============================================================

【🔴 §七 / §十四 / §十六：Reflection 不是 Runtime 的自动规则】

    ❌ Runtime 自动分析 Experience
    ❌ if action_failed: lower_confidence()
    ❌ if success_rate < 0.5: abandon_strategy()
    ❌ ReflectionEngine / CentralReflectionEngine /
       StrategyEvolutionEngine / SelfEvolutionEngine

    ✅ Experience → **Agent Cognition** → Reflection Proposal

    也就是说：**只有 Agent 的认知结果才产生 Proposal。**
    本模块只定义 Proposal 的**数据结构**，不产生任何 Proposal。

【🔴 §九：Proposal ≠ Self Model Update】

    ❌ LLM → 直接 update SelfModelStore
    ✅ LLM → ReflectionProposal → **Runtime Validation**
            → SelfModelWriteService → SelfModelStore
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- Proposal 种类 -------------------------------------------------------
KIND_CREATE_STRATEGY = "create_strategy"
KIND_REVISE_STRATEGY = "revise_strategy"
KIND_ABANDON_STRATEGY = "abandon_strategy"
KIND_CREATE_CLAIM = "create_claim"
KIND_REVISE_CLAIM = "revise_claim"
KIND_RETIRE_CLAIM = "retire_claim"

VALID_KINDS = {
    KIND_CREATE_STRATEGY, KIND_REVISE_STRATEGY, KIND_ABANDON_STRATEGY,
    KIND_CREATE_CLAIM, KIND_REVISE_CLAIM, KIND_RETIRE_CLAIM,
}

#: 需要 target（指向已有对象）的种类
NEEDS_TARGET = {KIND_REVISE_STRATEGY, KIND_ABANDON_STRATEGY,
                KIND_REVISE_CLAIM, KIND_RETIRE_CLAIM}


def new_proposal_id() -> str:
    return f"prop_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ReflectionProposal:
    """
    Agent 基于 Experience 提出的**对自身认知的修改建议**。

    🔒 它是**建议**，不是写入。必须经过 `SelfModelWriteService` 校验。
    """

    proposal_id: str
    agent_id: str
    kind: str
    target: str = ""
    proposed_change: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    evidence: List[str] = field(default_factory=list)
    scope: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id, "agent_id": self.agent_id,
            "kind": self.kind, "target": self.target,
            "proposed_change": dict(self.proposed_change or {}),
            "reason": self.reason, "evidence": list(self.evidence or []),
            "scope": dict(self.scope or {}), "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ValidationResult:
    """校验结果。**拒绝就是拒绝，不自动修正**（§二十二）。"""

    ok: bool
    code: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "reason": self.reason}


@dataclass(frozen=True)
class WriteOutcome:
    """一次受控写入的结果 —— **包含** validation，便于上层看见原因。"""

    committed: bool
    validation: ValidationResult
    result: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {"committed": self.committed,
                "validation": self.validation.to_dict(),
                "result": getattr(self.result, "to_dict",
                                  lambda: self.result)()
                if self.result is not None else None}
