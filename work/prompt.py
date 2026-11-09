"""
Work 层最小 prompt
===================

🔒 刻意**不注入**：

    · Wallet / Ledger / AIC / 余额 / 价格 / 议价 / 支付
    · 身份经济 / 声誉 / 排名
    · Agent 间消息 / inbox / 收件人
    · 生活层邀请协议
    · 每任务强制的 Public Working Memory / 冲突记录

只给当前角色真正需要的：

    目标 · DoD · Workspace 事实 · 任务级记忆 · 草稿 · 动作清单
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from work.roles import WorkContext, WorkRole


def _render_workspace(runtime: Any, workspace_id: str,
                      agent_id: str) -> str:
    """渲染 Workspace 的相对路径树（最多 80 行）。"""
    if runtime is None or not workspace_id:
        return "（没有 Workspace）"
    gateway = runtime.workspace_gateway
    lines: List[str] = []

    def walk(path: str, depth: int) -> None:
        if len(lines) >= 80:
            return
        try:
            result = gateway.list(workspace_id, path, agent_id)
        except Exception:                                     # noqa: BLE001
            return
        if not getattr(result, "success", False):
            return
        payload = getattr(result, "result", None) or {}
        for entry in payload.get("entries", []) or []:
            if len(lines) >= 80:
                return
            name = str(entry.get("name") or "")
            if not name:
                continue
            child = (
                f"{path.rstrip('/')}/{name}" if path not in ("", "/")
                else name
            )
            kind = str(entry.get("type") or "")
            meta = []
            controller = entry.get("effective_controller")
            if controller:
                meta.append(f"owner={controller}")
            holder = entry.get("effective_lock_holder")
            if holder:
                meta.append(f"lock={holder}")
            suffix = f"  [{', '.join(meta)}]" if meta else ""
            if kind == "directory":
                lines.append(f"- {child}/{suffix}")
                if depth < 3:
                    walk(child, depth + 1)
            else:
                lines.append(f"- {child}{suffix}")

    walk("/", 0)
    return "\n".join(lines) if lines else "（空 Workspace）"


def _render_brief(brief: Optional[Dict[str, Any]]) -> str:
    if not brief:
        return "（没有额外 Brief；以 Task description 为准）"
    return json.dumps(brief, ensure_ascii=False, indent=2)


def _render_last_turn(last: Optional[Dict[str, Any]]) -> str:
    if not last:
        return ""
    lines = [
        "",
        "========== 上一轮事实 ==========",
        f"- status: {last.get('status_before')} → {last.get('status_after')}",
        f"- actions: {last.get('action_types') or []}",
        f"- tokens: {last.get('tokens', 0)}",
    ]
    if last.get("truncated"):
        lines.append(
            "- 上一轮输出被截断：请拆小工作单元，或先写草稿再分批落盘。")
    if last.get("error"):
        lines.append(f"- error: {last['error']}")
    return "\n".join(lines)


def build_work_prompt(
    context: WorkContext,
    *,
    task_description: str,
    runtime: Any = None,
    last_turn: Optional[Dict[str, Any]] = None,
    extra_instruction: str = "",
) -> str:
    """
    构造一个 Work 层 turn 的 prompt。

    🔒 角色只看到自己的最小上下文；没有生活层身份/经济/消息。
    """
    role = context.role
    if role == WorkRole.SOLO:
        role_text = (
            "你是这个 Task 的 Solo 承接方。自己完成全部工作；"
            "完成后调用 work_complete。"
        )
    elif role == WorkRole.LEAD:
        role_text = (
            "你是 Team Lead。负责计划、集成和最终交付；"
            "不要把工作推回给 Human。"
        )
    else:
        role_text = (
            "你是 Team Member。只完成分配给你的部分；"
            "不要替别人做验收。"
        )

    criteria = "\n".join(
        f"- {item}" for item in context.acceptance_criteria
    ) or "（Task 没有显式 DoD；以 description 为准）"
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    memory = context.memory_facts or "（没有任务级记忆）"
    drafts = context.draft_facts or "（没有草稿）"

    completion_hint = (
        "完成时调用：\n"
        '{"decision":"act","reasoning":"...",'
        '"action":{"type":"work_complete","parameters":{'
        '"result_ref":"<逻辑名，不是路径>",'
        '"summary":"一句话说明交付了什么"}}}'
        if role == WorkRole.SOLO
        else "只有在确实完成分配工作时才调用 work_complete。"
    )

    return f"""你是 {context.agent_id}。当前在 Task {context.task_id}
（iteration {context.iteration}）中工作。

========== 角色 ==========
{role_text}

========== 任务目标 ==========
{task_description}

========== Brief ==========
{_render_brief(context.brief)}

========== 完成标准（Definition of Done） ==========
{criteria}

========== Workspace 事实 ==========
workspace_id: {context.workspace_id}
{workspace}

========== 任务级记忆（Private Working Memory） ==========
{memory}

========== 当前草稿 ==========
{drafts}
{_render_last_turn(last_turn)}
{extra_instruction}
========== 可用动作 ==========
- workspace_read / workspace_search / workspace_write / workspace_edit
- draft_write / draft_append / draft_read / draft_list / draft_delete
- workspace_promote_drafts（一次把多个草稿落盘）
- run_tool（受控工具：python / pytest / node / playwright 等）
- resource_create / resource_handoff / resource_release（Workspace 锁）
- shared_context_read
- private_working_memory_read / private_working_memory_update
- short_term_memory_read / short_term_memory_update / short_term_memory_reinforce
- long_term_memory_search / long_term_memory_read / long_term_memory_mark_used
- private_write / private_read / private_list / private_delete
- work_complete

🔒 这是 Work 层：没有 Agent 间通信动作，也不讨论身份或经济。
🔒 先读再改；写入前确认路径与锁；不要一次写超长文件。
🔒 不要声称完成而没有实际产物。

{completion_hint}

========== 输出格式 ==========
只输出一个 JSON 对象，不要 markdown 围栏：
{{"decision":"act","reasoning":"一句话说明为什么这样做",
 "action":{{"type":"动作类型","parameters":{{...}}}}}}
如果只是普通回复，可以输出 {{"decision":"reply","content":"..."}}。
"""
