"""
Minimal Action Seam —— Agent 的认知第一次有了行动出口。

    from actions import (
        ActionIntent, ActionResult, Decision, CognitiveRunResult,
        ActionDispatcher, ReplyExecutor, NoopExecutor, run_cognition,
        WorkspaceWriteExecutor, ACTION_WORKSPACE_WRITE,
    )

🔒 边界（本轮**刻意不做**）：
    ❌ Authorization / Capability / Sandbox / Executor Framework
    ❌ DSH Tool Bridge / Tool Registry / Process.Execute / Host 控制
    ❌ Action → Self Model 回流（必须经过 Experience + Reflection）
    ❌ 复用 core/capability.py（语义不同，见 §二十二）

⚠️ `WorkspaceWriteExecutor` **不 import `services/`** —— 它只拿一个
   注入的 `write_fn`。所以 `actions/` 仍是叶子层
   （由 `tests/test_action_seam.py` 的两个 AST 守卫 + `actions/workspace_write.py`
    的源码守卫共同保证）。
"""

from actions.executor import (
    ActionDispatcher, ActionExecutor, NoopExecutor, ReplyExecutor,
)
from actions.intent import (
    ACTION_NOOP, ACTION_OBSERVE, ACTION_REPLY, BUILTIN_ACTION_TYPES,
    DECISION_ACT, DECISION_REPLY, VALID_DECISION_KINDS,
    ActionIntent, ActionResult, CognitiveRunResult, Decision,
    new_decision_id, new_intent_id,
)
from actions.parse import (
    ENVELOPE_INSTRUCTION, build_intent, decision_from_text,
    extract_envelope,
)
from actions.run import reply_decision, run_cognition
from actions.workspace_write import (
    ACTION_WORKSPACE_WRITE, WorkspaceWriteExecutor,
)

__all__ = [
    "ActionIntent", "ActionResult", "Decision", "CognitiveRunResult",
    "ActionDispatcher", "ActionExecutor", "ReplyExecutor", "NoopExecutor",
    "run_cognition", "reply_decision", "decision_from_text",
    "extract_envelope", "build_intent", "ENVELOPE_INSTRUCTION",
    "ACTION_REPLY", "ACTION_NOOP", "ACTION_OBSERVE",
    "ACTION_WORKSPACE_WRITE", "WorkspaceWriteExecutor",
    "BUILTIN_ACTION_TYPES", "DECISION_ACT", "DECISION_REPLY",
    "VALID_DECISION_KINDS", "new_intent_id", "new_decision_id",
]
