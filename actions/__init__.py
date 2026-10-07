"""
Minimal Action Seam —— Agent 的认知第一次有了行动出口。

    from actions import (
        ActionIntent, ActionResult, Decision, CognitiveRunResult,
        ActionDispatcher, ReplyExecutor, NoopExecutor, run_cognition,
    )

🔒 边界（本轮**刻意不做**）：
    ❌ Authorization / Capability / Gateway / Sandbox / Executor Framework
    ❌ DSH Tool Bridge / Tool Registry / Process.Execute / Host 控制
    ❌ Action → Self Model 回流（Experience / Reflection 是下一阶段）
    ❌ 复用 core/capability.py（语义不同，见 §二十二）
"""

from actions.executor import (
    ActionDispatcher, ActionExecutor, NoopExecutor, ReplyExecutor,
)
from actions.intent import (
    ACTION_NOOP, ACTION_OBSERVE, ACTION_REPLY, BUILTIN_ACTION_TYPES,
    DECISION_ACT, DECISION_REPLY, VALID_DECISION_KINDS,
    ActionIntent, ActionResult, CognitiveRunResult, Decision,
)
from actions.parse import (
    ENVELOPE_INSTRUCTION, build_intent, decision_from_text,
    extract_envelope,
)
from actions.run import reply_decision, run_cognition

__all__ = [
    "ActionIntent", "ActionResult", "Decision", "CognitiveRunResult",
    "ActionDispatcher", "ActionExecutor", "ReplyExecutor", "NoopExecutor",
    "run_cognition", "reply_decision", "decision_from_text",
    "extract_envelope", "build_intent", "ENVELOPE_INSTRUCTION",
    "ACTION_REPLY", "ACTION_NOOP", "ACTION_OBSERVE",
    "BUILTIN_ACTION_TYPES", "DECISION_ACT", "DECISION_REPLY",
    "VALID_DECISION_KINDS",
]
