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
    "workspace_read": "读取 Workspace 中的文件",
    "workspace_search": "在 Workspace 文件中搜索文本",
    "workspace_write": "创建、替换或追加 Workspace 文件",
    "workspace_edit": "替换 Workspace 文件中一个唯一片段",
    # Drafts
    "draft_write": "在 Agent 私有草稿空间写草稿",
    "draft_append": "向草稿追加内容",
    "draft_read": "读取草稿",
    "draft_list": "列出草稿",
    "draft_delete": "删除草稿",
    "workspace_promote_drafts": "把草稿复制到 Workspace",
    # Execution
    "run_tool": "在临时副本中运行公共工具库里的一个工具",
    "tool_help": "读取工具的权威文档",
    # Resources
    "resource_create": "创建资源节点",
    "resource_handoff": "把资源交给另一个参与者",
    "resource_release": "释放资源",
    # Memory
    "public_working_memory_read": "读取 Task 的公共工作记忆",
    "public_working_memory_update": "更新 Task 的公共工作记忆",
    "private_working_memory_read": "读取本 Agent 的任务级记忆",
    "private_working_memory_update": "更新本 Agent 的任务级记忆",
    "short_term_memory_read": "读取本 Agent 的短期记忆",
    "short_term_memory_update": "更新本 Agent 的短期记忆",
    "short_term_memory_reinforce": "把一条短期记忆标记为有用",
    "long_term_memory_search": "搜索本 Agent 的长期记忆",
    "long_term_memory_read": "读取一条长期记忆",
    "long_term_memory_mark_used": "把一条长期记忆标记为已使用",
    "long_term_memory_consolidate": "合并记忆条目",
    # Private space
    "private_write": "在本 Agent 的私有 workspace 写文件",
    "private_read": "读取本 Agent 私有 workspace 中的文件",
    "private_list": "列出本 Agent 的私有 workspace",
    "private_delete": "删除本 Agent 私有 workspace 中的文件",
    # Work lifecycle / team
    "work_item_done": "标记工作项完成",
    "work_item_blocked": "标记工作项被阻塞",
    "work_qa_verdict": "返回 QA 结论",
    "work_plan_update": "更新工作计划",
    "work_complete": "声明 Task 已交付",
}


#: 每个动作的参数形状。只描述字段，不描述用法顺序。
#:
#: 🔒 这是 protocol facts：从真实 dispatcher 的 action_types 渲染，
#:    不是「建议 Agent 用哪个」。
ACTION_PARAMS = {
    # Workspace
    "workspace_read": (
        '- workspace_read:\n'
        '  {"path":"<relative path>", "start_line":1, "max_lines":200}\n'
        '  start_line / max_lines 都是**行号**，不是字符；单次最多 400 行；\n'
        '  继续下一页用返回的 next_start_line。'
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
        '  {"draft_id":"<id>", "start_char":0, "max_chars":12000}\n'
        '  start_char / max_chars 都是**字符位置**，不是行号；\n'
        '  继续下一页用返回的 next_start_char。'
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
        '  没有 `command` 字段。每个 arg 不超过 2000 字符；长脚本先写入文件，'
        '再运行该文件。'
    ),
    "tool_help": (
        '- tool_help:\n'
        '  {"tool":"python|pytest|node|playwright"}\n'
        '  只支持公共工具库；动作参数列在本节中。'
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
