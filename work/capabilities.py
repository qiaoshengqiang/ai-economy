"""动作能力清单 —— 给 Agent 看的**环境事实**。

这里只回答两件事：

    · 有哪些动作；
    · 每个动作做什么。

它**不**回答「你应该用哪个」「先做 A 再做 B」——
那是 Agent 自己的策略，不属于系统。

清单从真实的 dispatcher 读取（`dispatcher.action_types`），
因此不会出现「注册了但 prompt 没写」的漂移。
有守卫检查每个注册动作都有摘要。
"""

from __future__ import annotations

from typing import Iterable, List


#: 每个动作的一句话事实。保持中性：只描述能力，不描述用法顺序。
ACTION_SUMMARIES = {
    # Workspace
    "workspace_read": "read a file from the Workspace",
    "workspace_search": "search text across Workspace files",
    "workspace_write": "create, replace, or append a Workspace file",
    "workspace_edit": "replace one unique fragment in a Workspace file",
    # Drafts
    "draft_write": "write a draft in the Agent's private draft space",
    "draft_append": "append to a draft",
    "draft_read": "read a draft",
    "draft_list": "list drafts",
    "draft_delete": "delete a draft",
    "workspace_promote_drafts": "copy drafts into the Workspace",
    # Execution
    "run_tool": "run one tool from the public tool library in a throwaway copy",
    "tool_help": "read a tool's authoritative documentation",
    # Resources
    "resource_create": "create a resource node",
    "resource_handoff": "hand a resource to another participant",
    "resource_release": "release a resource",
    # Memory
    "public_working_memory_read": "read the Task's public working memory",
    "public_working_memory_update": "update the Task's public working memory",
    "private_working_memory_read": "read this Agent's task-scoped memory",
    "private_working_memory_update": "update this Agent's task-scoped memory",
    "short_term_memory_read": "read this Agent's short-term memory",
    "short_term_memory_update": "update this Agent's short-term memory",
    "short_term_memory_reinforce": "mark a short-term memory entry as useful",
    "long_term_memory_search": "search this Agent's long-term memory",
    "long_term_memory_read": "read a long-term memory entry",
    "long_term_memory_mark_used": "mark a long-term memory entry as used",
    "long_term_memory_consolidate": "consolidate memory entries",
    # Private space
    "private_write": "write a file in this Agent's private workspace",
    "private_read": "read a file in this Agent's private workspace",
    "private_list": "list this Agent's private workspace",
    "private_delete": "delete a file in this Agent's private workspace",
    # Work lifecycle / team
    "work_item_done": "mark a work item done",
    "work_item_blocked": "mark a work item blocked",
    "work_qa_verdict": "return a QA verdict",
    "work_plan_update": "update the work plan",
    "work_complete": "declare the Task delivered",
}


def describe_actions(action_types: Iterable[str]) -> str:
    """把动作集合渲染成一行一个的中性事实清单。"""
    lines: List[str] = []
    for name in sorted({str(n) for n in action_types if str(n).strip()}):
        summary = ACTION_SUMMARIES.get(name, "").strip()
        lines.append(f"- {name}: {summary}" if summary else f"- {name}")
    return "\n".join(lines) or "(none)"
