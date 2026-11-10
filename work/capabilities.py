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


#: 每个动作的参数形状。只描述字段，不描述用法顺序。
#:
#: 🔒 这是 protocol facts：从真实 dispatcher 的 action_types 渲染，
#:    不是「建议 Agent 用哪个」。
ACTION_PARAMS = {
    # Workspace
    "workspace_read": (
        '- workspace_read:\n'
        '  {"path":"<relative path>", "offset":1, "limit":12000}'
    ),
    "workspace_search": (
        '- workspace_search:\n'
        '  {"query":"<text>", "path":"", "glob":"", "max_results":20, '
        '"case_sensitive":true, "context_lines":0}'
    ),
    "workspace_write": (
        '- workspace_write:\n'
        '  {"path":"<relative path>", "content":"<full content>", '
        '"write_mode":"create|replace|append"}'
    ),
    "workspace_edit": (
        '- workspace_edit:\n'
        '  {"path":"<relative path>", "old_string":"<unique old fragment>", '
        '"new_string":"<new fragment>", "replace_all":false, '
        '"expected_revision":<optional int>}'
    ),
    # Drafts
    "draft_write": (
        '- draft_write:\n'
        '  {"content":"<text>", "label":"<optional label>"}'
    ),
    "draft_append": (
        '- draft_append:\n'
        '  {"draft_id":"<id>", "content":"<text>"}'
    ),
    "draft_read": (
        '- draft_read:\n'
        '  {"draft_id":"<id>", "offset":0, "limit":12000}'
    ),
    "draft_list": '- draft_list:\n  {}',
    "draft_delete": '- draft_delete:\n  {"draft_id":"<id>"}',
    "workspace_promote_drafts": (
        '- workspace_promote_drafts:\n'
        '  {"items":[{"draft_id":"<id>", "path":"<relative path>", '
        '"write_mode":"create|replace|append"}]}'
    ),
    # Execution
    "run_tool": (
        '- run_tool:\n'
        '  {"tool":"python|pytest|node|playwright", "args":[...], '
        '"source":"task|private", "path":"<optional copy path>", '
        '"timeout_seconds":60}\n'
        '  There is no `command` field. Each arg is <= 2000 chars; write long '
        'scripts to a file first, then run the file.'
    ),
    "tool_help": (
        '- tool_help:\n'
        '  {"tool":"python|pytest|node|playwright"}\n'
        '  Public tool library only; action parameters are listed in this '
        'section.'
    ),
    # Resources
    "resource_create": '- resource_create:\n  {"path":"<relative path>"}',
    "resource_handoff": (
        '- resource_handoff:\n'
        '  {"path":"<relative path>", "to":"<agent_id>", "reason":"<text>", '
        '"task_id":"<optional>"}'
    ),
    "resource_release": '- resource_release:\n  {"path":"<relative path>"}',
    # Private working memory
    "private_working_memory_read": '- private_working_memory_read:\n  {}',
    "private_working_memory_update": (
        '- private_working_memory_update:\n'
        '  {"expected_revision":<int>, "nodes":{"<node_id>":'
        '{"kind":"goal|branch|task|finding|issue|note", "title":"<text>", '
        '"content":<json>, "parent_id":"<optional node_id>"}}, '
        '"remove_nodes":["<node_id>"]}'
    ),
    # Short-term memory
    "short_term_memory_read": '- short_term_memory_read:\n  {}',
    "short_term_memory_update": (
        '- short_term_memory_update:\n'
        '  {"expected_revision":<int>, "entry_id":"<id>", '
        '"summary":"<text>", "architecture":"<text>", "notable":"<text>", '
        '"importance":<int>, "remove":false}'
    ),
    "short_term_memory_reinforce": (
        '- short_term_memory_reinforce:\n'
        '  {"expected_revision":<int>, "entry_id":"<id>", "reason":"<text>"}'
    ),
    # Long-term memory
    "long_term_memory_search": (
        '- long_term_memory_search:\n  {"query":"<text>"}'
    ),
    "long_term_memory_read": (
        '- long_term_memory_read:\n  {"entry_id":"<id>"}'
    ),
    "long_term_memory_mark_used": (
        '- long_term_memory_mark_used:\n  {"entry_ids":["<id>"]}'
    ),
    "long_term_memory_consolidate": (
        '- long_term_memory_consolidate:\n'
        '  {"expected_revision":<int>, "summary":"<text>", '
        '"tasks":[...], "events":[...], "short_term_entry_ids":["<id>"]}'
    ),
    # Private workspace
    "private_write": (
        '- private_write:\n  {"path":"<relative path>", "content":"<text>"}'
    ),
    "private_read": '- private_read:\n  {"path":"<relative path>"}',
    "private_list": '- private_list:\n  {}',
    "private_delete": '- private_delete:\n  {"path":"<relative path>"}',
    # Work lifecycle
    "work_complete": (
        '- work_complete:\n'
        '  {"result_ref":"<logical name, not a path>", "summary":"<text>", '
        '"result_type":"artifact", "metadata":{}, "feedback":"<text>"}'
    ),
    # Team-only
    "public_working_memory_read": '- public_working_memory_read:\n  {}',
    "public_working_memory_update": (
        '- public_working_memory_update:\n'
        '  {"expected_revision":<int>, "items":{"<key>":{"kind":"...", '
        '"value":<json>, "parent_key":"<optional>"}}, '
        '"remove_keys":["<key>"]} or {"expected_revision":<int>, '
        '"content":"<single note>"}'
    ),
    "work_item_done": (
        '- work_item_done:\n'
        '  {"item_id":"<id>", "summary":"<text>", "paths":["<path>"], '
        '"note":"<optional>"}'
    ),
    "work_item_blocked": (
        '- work_item_blocked:\n'
        '  {"item_id":"<id>", "reason":"<text>"}'
    ),
    "work_qa_verdict": (
        '- work_qa_verdict:\n'
        '  {"passed":true, "feedback":"<text>"}'
    ),
    "work_plan_update": (
        '- work_plan_update:\n'
        '  {"expected_revision":<int>, "operations":[...], '
        '"qa":<optional>, "reason":"<text>"}'
    ),
}


def describe_action_params(action_types: Iterable[str]) -> str:
    """只渲染当前 dispatcher 真正注册的动作参数。"""
    lines: List[str] = []
    for name in sorted({str(n) for n in action_types if str(n).strip()}):
        block = ACTION_PARAMS.get(name)
        if block:
            lines.append(block)
        else:
            # 守卫会阻止这种情况；这里仍然如实说「没有参数说明」。
            lines.append(f"- {name}: (no parameter documentation)")
    return "\n".join(lines) or "(none)"


def describe_actions(action_types: Iterable[str]) -> str:
    """把动作集合渲染成一行一个的中性事实清单。"""
    lines: List[str] = []
    for name in sorted({str(n) for n in action_types if str(n).strip()}):
        summary = ACTION_SUMMARIES.get(name, "").strip()
        lines.append(f"- {name}: {summary}" if summary else f"- {name}")
    return "\n".join(lines) or "(none)"
