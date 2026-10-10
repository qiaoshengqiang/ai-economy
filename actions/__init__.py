"""
Minimal Action Seam —— Agent 的认知第一次有了行动出口。

    from actions import (
        ActionIntent, ActionResult, Decision, CognitiveRunResult,
        ActionDispatcher, ReplyExecutor, NoopExecutor, run_cognition,
        WorkspaceWriteExecutor, ACTION_WORKSPACE_WRITE,
        WorkspaceReadExecutor, ACTION_WORKSPACE_READ,
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
    ActionIntent, ActionResult, CognitiveRunResult, CognitiveSegment,
    Decision, new_decision_id, new_intent_id,
)
from actions.parse import (
    ENVELOPE_INSTRUCTION, build_intent, decision_from_text,
    extract_envelope,
)
from actions.assignment import (
    ACTION_ASSIGN_ACCEPTANCE, AssignAcceptanceExecutor,
)
from actions.private_workspace import (
    ACTION_PRIVATE_DELETE, ACTION_PRIVATE_LIST, ACTION_PRIVATE_READ,
    ACTION_PRIVATE_WRITE, PrivateDeleteExecutor, PrivateListExecutor,
    PrivateReadExecutor, PrivateWriteExecutor,
)
from actions.resource_control import (
    ACTION_RESOURCE_CREATE, ACTION_RESOURCE_HANDOFF, ACTION_RESOURCE_RELEASE,
    ResourceCreateExecutor, ResourceHandoffExecutor, ResourceReleaseExecutor,
)
from actions.public_query import (
    ACTION_PUBLIC_QUERY, PublicQueryExecutor,
)
from actions.message_send import (
    ACTION_MESSAGE_SEND, MessageSendExecutor,
)
from actions.run import reply_decision, run_cognition
from actions.workspace_read import (
    ACTION_WORKSPACE_READ, WorkspaceReadExecutor,
)
from actions.workspace_write import (
    ACTION_WORKSPACE_WRITE, WorkspaceWriteExecutor,
)
from actions.shared_context_read import (
    ACTION_SHARED_CONTEXT_READ, SharedContextReadExecutor,
)
from actions.read_payload import (
    CONFIRMATION_ACTIONS, OBSERVABLE_ACTIONS, PER_PAYLOAD_MAX_CHARS,
    TOTAL_MAX_CHARS, is_observable, observable_results,
    render_read_results,
)

__all__ = [
    "ActionIntent", "ActionResult", "Decision", "CognitiveRunResult",
    "CognitiveSegment",
    "OBSERVABLE_ACTIONS", "CONFIRMATION_ACTIONS",
    "PER_PAYLOAD_MAX_CHARS", "TOTAL_MAX_CHARS",
    "is_observable", "observable_results", "render_read_results",
    "ActionDispatcher", "ActionExecutor", "ReplyExecutor", "NoopExecutor",
    "run_cognition", "reply_decision", "decision_from_text",
    "extract_envelope", "build_intent", "ENVELOPE_INSTRUCTION",
    "ACTION_REPLY", "ACTION_NOOP", "ACTION_OBSERVE",
    "ACTION_WORKSPACE_WRITE", "WorkspaceWriteExecutor",
    "ACTION_WORKSPACE_READ", "WorkspaceReadExecutor",
    "ACTION_MESSAGE_SEND", "MessageSendExecutor",
    "ACTION_PUBLIC_QUERY", "PublicQueryExecutor",
    "ACTION_ASSIGN_ACCEPTANCE", "AssignAcceptanceExecutor",
    "ACTION_SHARED_CONTEXT_READ", "SharedContextReadExecutor",
    "ACTION_RESOURCE_CREATE", "ACTION_RESOURCE_HANDOFF",
    "ACTION_RESOURCE_RELEASE",
    "ResourceCreateExecutor", "ResourceHandoffExecutor",
    "ResourceReleaseExecutor",
    "ACTION_PRIVATE_WRITE", "ACTION_PRIVATE_READ", "ACTION_PRIVATE_LIST",
    "ACTION_PRIVATE_DELETE",
    "PrivateWriteExecutor", "PrivateReadExecutor", "PrivateListExecutor",
    "PrivateDeleteExecutor",
    "BUILTIN_ACTION_TYPES", "DECISION_ACT", "DECISION_REPLY",
    "VALID_DECISION_KINDS", "new_intent_id", "new_decision_id",
]
