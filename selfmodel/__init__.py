"""
Self Model V1 —— Agent 对「自己」的动态工作模型。

    from selfmodel import SelfModelStore, Scope, Evidence, SelfClaim, Strategy

🔒 边界（§四 / §十八）：
    Self Model ≠ Memory ≠ World Model ≠ Other-Agent Model
              ≠ Public Manifest ≠ Identity / Ledger / Capability

🔒 它**不是**第二权威源：AIC 余额、Capability、Authorization、
   Workspace 权限的权威仍然是它们各自的系统。
"""

from selfmodel.models import (
    EVIDENCE_EXPERIENCE, EVIDENCE_FEEDBACK, EVIDENCE_HUMAN_INPUT,
    EVIDENCE_OBSERVATION, EVIDENCE_REFLECTION, EVIDENCE_SYSTEM_FACT,
    KIND_CLAIM, KIND_FACT, STATUS_ACTIVE, STATUS_RETIRED,
    Evidence, Scope, SelfClaim, SelfModelView, Strategy,
)
from selfmodel.store import SelfModelError, SelfModelStore

__all__ = [
    "SelfModelStore", "SelfModelError", "SelfClaim", "Strategy",
    "SelfModelView", "Scope", "Evidence",
    "KIND_FACT", "KIND_CLAIM", "STATUS_ACTIVE", "STATUS_RETIRED",
    "EVIDENCE_EXPERIENCE", "EVIDENCE_FEEDBACK", "EVIDENCE_OBSERVATION",
    "EVIDENCE_REFLECTION", "EVIDENCE_HUMAN_INPUT", "EVIDENCE_SYSTEM_FACT",
]
