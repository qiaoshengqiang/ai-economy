"""
Work 层最小 prompt
===================

🔒 刻意**不注入**：

    · Wallet / Ledger / AIC / 余额 / 价格 / 议价 / 支付
    · 身份经济 / 声誉 / 排名
    · Agent 间消息 / inbox / 收件人
    · 生活层邀请协议

✅ Public Working Memory 是团队**抽象架构白板**：目录、文件归属、
   类/方法接口、依赖、进度。每个 worker 的 prompt 都直接注入它，
   避免反复读 Shared Context；它不存具体实现或长任务描述。

只给当前角色真正需要的：

    目标 · DoD · 公共工作记忆 · Workspace 事实 ·
    任务级记忆 · 草稿 · 动作 schema
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from work.roles import WorkContext, WorkRole


#: 动作参数 schema。
#:
#: 🔒 之前只列动作名，模型会猜 `target` / `pattern` / `command`，
#:    导致大量“动作被拒绝 → 重来”的浪费。这里把字段名写死。
ACTION_SCHEMA = """
- workspace_read:
  {"path":"<relative path>", "offset":1, "limit":12000}
  Path field is `path` (never `target`).
- workspace_search:
  {"query":"<text>", "path":"", "glob":"", "max_results":20}
  Query field is `query`; `glob` is the file pattern.
- workspace_write:
  {"path":"<relative path>", "content":"<full content>",
   "write_mode":"create|replace|append"}
  write_mode defaults to create. Use create for new files, replace for
  existing files, append for additions. Large files may be written in one go.
- workspace_edit:
  {"path":"<relative path>", "old_string":"<unique old fragment>",
   "new_string":"<new fragment>", "replace_all":false}
- run_tool:
  {"tool":"python|pytest|node|playwright", "args":[...],
   "source":"task|private",
   "path":"<optional: only copy this path; omit = whole Workspace>",
   "timeout_seconds":60}
  There is no `command` field. Each arg is <= 2000 chars; write long
  scripts to a file first, then run the file.
- tool_help:
  {"tool":"python|pytest|node|playwright"}
  Use this before guessing a tool's usage, args, limits, or test-file
  naming. Omit `tool` to list the catalog. Docs are the single source.
- private_working_memory_read / private_working_memory_update:
  Same shape as working memory, but scoped to this Agent + Task.
- short_term_memory_read / short_term_memory_update / short_term_memory_reinforce
- long_term_memory_search / long_term_memory_read / long_term_memory_mark_used
- private_write / private_read / private_list / private_delete:
  Private space of the current Agent only.
- resource_create / resource_handoff / resource_release:
  {"path":"<relative path>", "to":"<agent_id>", "reason":"..."}
- draft_write / draft_append / draft_read / draft_list / draft_delete
- workspace_promote_drafts:
  {"items":[{"draft_id":"...", "path":"...",
             "write_mode":"create|replace|append"}]}
"""


def _render_workspace(runtime: Any, workspace_id: str,
                      agent_id: str) -> str:
    """渲染 Workspace 的相对路径树（最多 80 行）。"""
    if runtime is None or not workspace_id:
        return "(no Workspace)"
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
    return "\n".join(lines) if lines else "(empty Workspace)"


def _render_brief(brief: Optional[Dict[str, Any]]) -> str:
    if not brief:
        return "(no extra Brief; use the Task description)"
    return json.dumps(brief, ensure_ascii=False, indent=2)


def _render_public_memory(context: WorkContext) -> str:
    facts = str(context.public_memory_facts or "").strip()
    return facts or "（公共工作记忆为空）"


def _render_last_turn(last: Optional[Dict[str, Any]]) -> str:
    if not last:
        return ""
    lines = [
        "",
        "========== PREVIOUS TURN FACTS ==========",
        f"- status: {last.get('status_before')} -> {last.get('status_after')}",
        f"- actions: {last.get('action_types') or []}",
        f"- tokens: {last.get('tokens', 0)}",
    ]
    if last.get("decision_kind"):
        line = f"- decision: {last['decision_kind']}"
        if last.get("envelope_error"):
            line += f" (envelope_error={last['envelope_error']})"
        lines.append(line)
    if last.get("thinking"):
        lines.append(
            "- thinking: "
            + json.dumps(last["thinking"], ensure_ascii=False))
    budget = last.get("cognitive_budget")
    if isinstance(budget, dict):
        lines.append(
            "- cognitive_budget: "
            f"requested={budget.get('requested')} "
            f"actual={budget.get('effective')} "
            f"clipped={budget.get('clipped') or '-'}")
    rewrites = last.get("rewrites")
    if isinstance(rewrites, dict) and rewrites:
        detail = ", ".join(
            f"{path} x {count}"
            for path, count in sorted(rewrites.items()))
        lines.append(
            f"- repeated full-file rewrites: {detail}; "
            "prefer workspace_edit for small changes.")
    summary = str(last.get("round_summary") or "")
    if summary:
        lines.append(
            "\n========== PREVIOUS ROUND SUMMARY (FULL) ==========\n"
            + summary)
    feedback = last.get("action_feedback")
    if isinstance(feedback, list) and feedback:
        lines.append("- previous action results:")
        for item in feedback[:10]:
            if not isinstance(item, dict):
                continue
            status = "ok" if item.get("ok") else (
                "not executed (truncated)"
                if not item.get("executed") else "failed")
            line = f"  - {item.get('action') or '?'}"
            if item.get("target"):
                line += f" {item['target']}"
            line += f": {status}"
            if item.get("code"):
                line += f" ({item['code']})"
            error = str(item.get("error") or "")
            if error:
                line += f" - {error[:240]}"
            lines.append(line)
    if last.get("truncated"):
        lines.append(
            "- previous output was truncated: raise cognitive_budget to "
            "65536 next time, or split the work / use create + append.")
    if last.get("error"):
        lines.append(f"- error: {last['error']}")
    return "\n".join(lines)


def _render_tool_catalog(tools: Any) -> str:
    lines = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        desc = str(tool.get("description") or "").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines) or "（没有工具目录）"


def _render_tool_docs(tool_docs: Optional[Dict[str, str]]) -> str:
    """把本轮之前查到的工具文档原样注入。

    🔒 注入的是**文档原文**，不是摘要：工具约束（spec 命名、BASE_URL、
       smoke gate、超时、输出上限）一旦被转述就会失真。
    """
    if not tool_docs:
        return ""
    blocks = ["========== TOOL DOCS ALREADY READ (AUTHORITATIVE) =========="]
    for name in sorted(tool_docs):
        content = str(tool_docs.get(name) or "").strip()
        if not content:
            continue
        blocks.extend(["", f"### {name}", content])
    if len(blocks) == 1:
        return ""
    blocks.extend([
        "",
        "These are the real constraints from the tool docs. Follow them "
        "exactly; do not rely on a paraphrase or on memory.",
    ])
    return "\n".join(blocks)


def build_cognition_prompt(
    context: WorkContext,
    *,
    task_description: str,
    runtime: Any,
    tool_catalog: Any = (),
    max_steps: int = 2,
    tool_docs: Optional[Dict[str, str]] = None,
) -> str:
    """Task cognition: understand environment, requirements, design, risks."""
    workspace = _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    criteria = "\n".join(
        f"- {item}" for item in context.acceptance_criteria
    ) or "(no explicit DoD; use the Human task text)"
    return f"""You are {context.agent_id}. This is the TASK COGNITION phase.
Do not start production yet.

Goal: before acting, understand the Human request, the current environment,
available tools, existing code/dependencies, technical approach, architecture,
risks, and unknowns. Produce a complete cognition report. The report will be
injected into EVERY later decision and action as fixed context.

Use English for this report and for all internal reasoning.

========== HUMAN TASK TEXT ==========
{task_description}

========== DEFINITION OF DONE ==========
{criteria}

========== BRIEF ==========
{_render_brief(context.brief)}

========== ENVIRONMENT FACTS ==========
workspace_id: {context.workspace_id}
{workspace}

========== AVAILABLE TOOLS ==========
{_render_tool_catalog(tool_catalog)}
{_render_tool_docs(tool_docs)}
========== EVIDENCE BEFORE THE REPORT ==========
The catalog above only gives tool names. Tool docs carry hard constraints
(spec-file naming, BASE_URL, smoke gate, timeouts, output limits) that a
plan built from general knowledge will get wrong.
- Call `tool_help` for every tool you plan to use in this task.
- You may put several tool_help calls in one `actions` list.
- If a tool's docs are already listed above, do not call it again.
- Do not write the report from general knowledge alone when a tool is
  involved in the plan.

========== READ-ONLY ACTIONS AVAILABLE ==========
- workspace_read: {{"path":"...","offset":1,"limit":200}}
- workspace_search: {{"query":"...","path":"","glob":"","max_results":20}}
- tool_help: {{"tool":"python|pytest|node|playwright"}}
- private_read / private_list / private_working_memory_read / draft_read / draft_list

This phase is read-only: do not write files and do not call work_complete.
If you need more facts, emit one or more actions in one JSON (use the
`actions` list), at most {max_steps} steps.
When you have enough facts, output the COMPLETE REPORT as plain text,
not a JSON action.

Suggested report sections:
## 1. Requirements (including implicit requirements)
## 2. Environment facts (project structure / existing resources / dependencies / tools)
## 3. Technical approach and architecture (alternatives, trade-offs)
## 4. Risks, unknowns, assumptions to verify
## 5. Execution plan (modules / files / order)
## 6. Verification strategy

Be concrete and actionable. Later decisions use this report directly.
"""


def build_work_prompt(
    context: WorkContext,
    *,
    task_description: str,
    runtime: Any = None,
    last_turn: Optional[Dict[str, Any]] = None,
    extra_instruction: str = "",
    tool_docs: Optional[Dict[str, str]] = None,
) -> str:
    """Build one Work turn prompt. Internal reasoning should be English."""
    role = context.role
    if role == WorkRole.SOLO:
        role_text = (
            "You are the Solo worker for this Task. Complete the work "
            "yourself and call work_complete when done.")
    elif role == WorkRole.LEAD:
        role_text = (
            "You are the Team Lead. Plan, assign, integrate, and deliver; "
            "do not push work back to the Human.")
    elif role == WorkRole.QA:
        role_text = (
            "You are QA. Check the delivery against the Definition of Done "
            "and return work_qa_verdict.")
    else:
        role_text = (
            "You are a Team Member. Complete only your assigned items; "
            "use work_item_done / work_item_blocked. Do not accept others' work.")

    criteria = "\n".join(
        f"- {item}" for item in context.acceptance_criteria
    ) or "(no explicit DoD; use the task description)"
    reply_style = str(
        (context.brief or {}).get("reply_style") or "").strip()
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    memory = context.memory_facts or "(no task-level memory)"
    drafts = context.draft_facts or "(no drafts)"

    completion_hint = (
        "When done, call:\n"
        '{"decision":"act","reasoning":"...",'
        '"action":{"type":"work_complete","parameters":{'
        '"result_ref":"<logical name, not a path>",'
        '"summary":"one-line summary of the delivery"}}}'
        if role == WorkRole.SOLO
        else "Call work_complete only after the assigned work is truly done."
    )

    return f"""You are {context.agent_id}. You are working on Task {context.task_id}
(iteration {context.iteration}).

========== ROLE ==========
{role_text}

========== TASK ==========
{task_description}

========== BRIEF ==========
{_render_brief(context.brief)}

========== DEFINITION OF DONE ==========
{criteria}

========== REPLY FORMAT FROM FRONT ==========
{reply_style or "(none; concise, friendly, conclusion first, then artifacts)"}

========== TASK COGNITION (STARTUP SNAPSHOT) ==========
{str(context.cognition_report or "").strip()
 or "(no cognition report; use the task text and current environment)"}

> This is a startup snapshot. The environment may have changed. Re-read or
> search when needed; do not assume the snapshot is still current.

========== WORKSPACE FACTS ==========
workspace_id: {context.workspace_id}
{workspace}

========== TASK-LEVEL MEMORY (PRIVATE WORKING MEMORY) ==========
{memory}

========== CURRENT DRAFTS ==========
{drafts}
{_render_last_turn(last_turn)}
{extra_instruction}
{_render_tool_docs(tool_docs)}
========== DECISION / WORK ==========
Each call is in the decision or work phase:
- decision: decide the next step;
- work: continue after reads/tool results.
The action set is the same; you may set thinking / cognitive_budget per phase.

========== OPTIONAL: TUNE THE NEXT CALL ==========
Put these at the TOP LEVEL of the JSON (not inside action.parameters):
- "thinking": {{"enabled": true, "effort": "high"}}
  Applies to the current phase; DeepSeek supports low / high / max.
  Use enabled:false to disable. You may pass a per-phase map.
- "cognitive_budget": 32768
  Applies to the current phase, or pass a per-phase map.
  Unit: max_tokens. Hard cap is 65536.
  Effective = min(your preference, 65536, provider cap).
  If omitted, the configured default is used.
  It affects the NEXT call, not the current output.
  Before writing a very large file, first emit a small reply with
  {{"decision":"reply","content":"preparing a large write",
    "cognitive_budget":65536}}, then write in the next turn.

========== WORKING RHYTHM ==========
- Decide the module boundaries and file list before the first write.
- Build the entry file as a skeleton, then use workspace_edit for changes;
  avoid rewriting the whole entry file repeatedly.
- Small files may go in one actions list. For large files, raise
  cognitive_budget first or use create + append.
- Verify with run_tool after each module when useful, then move on.
- Before the first use of any tool, call tool_help. If its docs are already
  in TOOL DOCS above, follow them exactly instead of reconstructing its CLI
  or constraints from memory.
- Verification should cover the DoD with the smallest sufficient evidence.
  If the same verification path fails twice, change approach or report the
  limitation instead of creating endless new test scripts.

========== ACTIONS AND PARAMETERS ==========
{ACTION_SCHEMA}
- work_complete:
  {{"result_ref":"<logical name, not a path>", "summary":"..."}}

This is the Work layer: no inter-Agent messages and no identity/economy.
Use tool_help before guessing tool usage; docs are the single source.
Read before editing; confirm path and locks before writing.
Do not claim completion without actual artifacts.

Write internal reasoning and round summaries in English.
Write the final `work_complete.summary` in the Human's language (the task
text language), because Front shows it to the Human.

{completion_hint}

========== OUTPUT FORMAT ==========
Return one JSON object, no markdown fences:
{{"decision":"act","reasoning":"one sentence",
 "thinking":{{"enabled":true,"effort":"high"}},
 "cognitive_budget":{{"work":32768}},
 "action":{{"type":"action_type","parameters":{{...}}}}}}
thinking / cognitive_budget are optional; omit them to keep current settings.
For a plain reply: {{"decision":"reply","content":"..."}}.

To do several things at once (e.g. write several small files), use an action list:
{{"decision":"act","reasoning":"...",
 "actions":[{{"type":"workspace_write","parameters":{{...}}}},
           {{"type":"workspace_write","parameters":{{...}}}}]}}
Actions run in order; one failure does not block later ones. If the whole
output is truncated, NO actions are executed.
"""


# ============================================================
# Team 模式
# ============================================================
def _render_items(items: Any) -> str:
    lines = []
    for item in items or []:
        paths = f"；paths={item.paths}" if getattr(item, "paths", None) else ""
        acceptance = (
            f"；acceptance={item.acceptance}"
            if getattr(item, "acceptance", None) else "")
        # 🔒 notes 常常就是 Leader 写的契约 / 接口约定。
        #    之前不渲染它，worker 只能反复 shared_context_read。
        notes = str(getattr(item, "notes", "") or "").strip()
        if len(notes) > 2000:
            notes = notes[:2000] + " …（已截断）"
        notes_text = f"\n  notes: {notes}" if notes else ""
        lines.append(
            f"- {item.item_id} @ {item.agent_id}: {item.goal}"
            f"{paths}{acceptance}{notes_text}")
    return "\n".join(lines) or "（无）"


def _render_item_state(state: Dict[str, Any]) -> str:
    if not state:
        return "（还没有成员回报）"
    lines = []
    for item_id in sorted(state):
        item = state[item_id]
        status = item.get("status", "")
        summary = item.get("summary") or item.get("reason") or ""
        lines.append(f"- {item_id}: {status}" + (f" — {summary}" if summary else ""))
    return "\n".join(lines)


def build_lead_plan_prompt(
    context: WorkContext,
    *,
    task_description: str,
    participants: List[str],
    workspace_facts: str = "",
) -> str:
    """
    Leader 计划阶段：只输出计划 JSON，不直接执行工作。
    """
    people = ", ".join(participants)
    return f"""你是 {context.agent_id}，Team Leader。当前 Task {context.task_id}
（iteration {context.iteration}）。

========== 任务目标 ==========
{task_description}

========== Brief ==========
{_render_brief(context.brief)}

========== 完成标准（Definition of Done） ==========
{(chr(10).join(f"- {c}" for c in context.acceptance_criteria)
  or "（以任务目标为准）")}

========== 团队成员（只能从这里分配） ==========
{people}

========== Workspace 事实 ==========
{workspace_facts or "（空 Workspace）"}

========== 你的职责：只做计划 ==========
1. 设计架构 / 拆分工作；
2. 把工作拆成 1..N 个 item，每个 item 明确 assigned_to；
3. 团队会并行执行这些 item；
4. 最后你自己负责集成、接线和收尾；
5. 是否设置 QA 由你决定：`qa` 填一个**非 Leader** 的参与者 agent_id，或 null。

可选 `directories`：为成员声明可写目录，runtime 会创建并移交锁。
每个目录只能有一个 owner；不写时 runtime 会给每个成员一个默认目录。

只输出 JSON，不要 markdown 围栏：
{{"items":[
  {{"item_id":"item_1","assigned_to":"agent_b","goal":"...",
    "paths":["src/module_b"],"acceptance":["..."]}}
],
 "qa": null,
 "directories":{{"src/module_b":"agent_b"}},
 "notes":"..."}}

🔒 不要在这里开始生产；下一步才是并行执行。
🔒 不要输出经济 / 钱包 / 价格相关内容。
"""


def build_team_worker_prompt(
    context: WorkContext,
    *,
    task_description: str,
    items: Any,
    item_state: Dict[str, Any],
    runtime: Any = None,
    last_turn: Optional[Dict[str, Any]] = None,
) -> str:
    """成员 prompt：只看到自己的 item，不看到其他成员的完整上下文。"""
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    return f"""你是 {context.agent_id}，Team Member。

========== 你的分工 ==========
{_render_items(items)}

========== 当前状态 ==========
{_render_item_state(item_state)}

========== 任务背景（只读） ==========
{task_description}

========== 完成标准 ==========
{(chr(10).join(f"- {c}" for c in context.acceptance_criteria)
  or "（以分工目标为准）")}

========== 公共工作记忆（团队共享契约 / 计划 / 进度） ==========
{_render_public_memory(context)}

========== Workspace ==========
{workspace}
{_render_last_turn(last_turn)}
========== 可用动作与参数 ==========
{ACTION_SCHEMA}
- work_item_done:
  {{"item_id":"...", "summary":"...", "paths":["..."]}}
  （不要用 note 字段；说明写 summary）
- work_item_blocked:
  {{"item_id":"...", "reason":"..."}}

🔒 只做分给你的 item。完成后调用 work_item_done（带 item_id）。
🔒 被阻塞时调用 work_item_blocked（带 item_id + reason）。
🔒 同一个文件读一次并记录结论；需要定位其他内容时用 workspace_search，
   不要整文件反复重读。
🔒 公共工作记忆里已有契约时，不要再反复读 Shared Context / config。
🔒 没有 Agent 间通信动作；不要讨论身份或经济。
🔒 不要替别人验收，也不要调用 work_complete。

========== 输出格式 ==========
只输出 JSON：
{{"decision":"act","reasoning":"...",
 "action":{{"type":"动作类型","parameters":{{...}}}}}}
"""


def build_qa_prompt(
    context: WorkContext,
    *,
    task_description: str,
    item_state: Dict[str, Any],
    runtime: Any = None,
    last_turn: Optional[Dict[str, Any]] = None,
) -> str:
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    return f"""你是 {context.agent_id}，QA。

========== 任务目标 ==========
{task_description}

========== 完成标准（Definition of Done） ==========
{(chr(10).join(f"- {c}" for c in context.acceptance_criteria)
  or "（以任务目标为准）")}

========== 成员回报 ==========
{_render_item_state(item_state)}

========== 公共工作记忆（团队共享契约 / 计划 / 进度） ==========
{_render_public_memory(context)}

========== Workspace ==========
{workspace}
{_render_last_turn(last_turn)}
========== 可用动作与参数 ==========
{ACTION_SCHEMA}
- work_qa_verdict:
  {{"passed":true|false, "feedback":"..."}}

========== 通过前的事实核对 ==========
- 对照公共工作记忆里的 team.architecture.files，逐个确认声明的文件真实存在；
- 如果入口文件引用了其他本地文件（script/link/import），确认它们也存在；
- 成员回报是声明，不是证据；缺失文件必须 passed=false；
- 不要因为“大部分已完成”就通过。

========== 你的职责 ==========
检查整体交付是否满足 DoD。可以 workspace_read / workspace_search /
run_tool 验证，然后调用 work_qa_verdict。

🔒 不通过必须给出可执行的 feedback。
🔒 不要反复读 Shared Context；公共工作记忆已经是团队契约。
🔒 没有 Agent 间通信动作；不要讨论身份或经济。
"""


def build_lead_integration_prompt(
    context: WorkContext,
    *,
    task_description: str,
    item_state: Dict[str, Any],
    qa_feedback: str = "",
    runtime: Any = None,
    last_turn: Optional[Dict[str, Any]] = None,
    include_plan_update: bool = False,
) -> str:
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    qa_text = f"\n========== QA 反馈 ==========\n{qa_feedback}\n" if qa_feedback else ""
    replan_text = (
        "\n如果 blocked / pending item 或 QA 反馈需要调整分工，"
        "可以调用 work_plan_update（reassign / add / cancel / update）。\n"
        if include_plan_update else "")
    return f"""你是 {context.agent_id}，Team Leader。现在做集成、接线、收尾。

========== 任务目标 ==========
{task_description}

========== 成员回报 ==========
{_render_item_state(item_state)}
{qa_text}
========== 公共工作记忆（团队共享契约 / 计划 / 进度） ==========
{_render_public_memory(context)}

========== Workspace ==========
{workspace}
{_render_last_turn(last_turn)}
========== 你的职责 ==========
1. 检查成员产物与接口；
2. 补齐缺失的集成 / 接线 / 收尾；
3. 处理 blocked item 或调整方案；
4. 满足 DoD 后调用 work_complete。

========== 可用动作与参数 ==========
{ACTION_SCHEMA}
- work_complete:
  {{"result_ref":"<逻辑名，不是路径>", "summary":"..."}}
- work_plan_update（重新计划时）:
  {{"expected_revision":<当前版本>, "reason":"...",
    "operations":[...], "qa": null}}
{replan_text}
🔒 公共工作记忆是团队共享契约；不要反复读 Shared Context。
🔒 没有 Agent 间通信动作；不要讨论身份或经济。
🔒 如果设置了 QA，只有 QA 通过后才能完成。
"""


def build_leader_replan_prompt(
    context: WorkContext,
    *,
    task_description: str,
    item_state: Dict[str, Any],
    plan: Any,
    participants: List[str],
    qa_feedback: str = "",
    runtime: Any = None,
) -> str:
    """
    Leader 在两轮之间重新计划。

    🔒 这个阶段只调用 work_plan_update，不直接生产。
    """
    blocked = [
        item_id for item_id, state in (item_state or {}).items()
        if str(state.get("status") or "") in ("BLOCKED", "PENDING")
    ]
    feedback = (
        f"\n========== 需要处理 ==========\n"
        f"- blocked/pending items: {blocked}\n"
        + (f"- QA feedback: {qa_feedback}\n" if qa_feedback else "")
    )
    return f"""你是 {context.agent_id}，Team Leader。当前是**重新计划阶段**。

========== 任务目标 ==========
{task_description}

========== 当前计划（revision {getattr(plan, "revision", 1)}） ==========
{json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)}

========== 成员回报 ==========
{_render_item_state(item_state)}
{feedback}
========== 公共工作记忆 ==========
{_render_public_memory(context)}

========== 团队成员 ==========
{", ".join(participants)}

========== 你的职责 ==========
只调整计划，不直接生产。根据 blocked / pending / QA 反馈决定：

- reassign 改派给另一个成员；
- add 增加一个补丁 / 集成 item；
- cancel 取消不再需要的 item；
- update 修改 item 目标 / 验收 / 路径；
- qa 可以新增、替换或清除（不能是 Leader 自己）。

只输出一个动作：
{{"decision":"act","reasoning":"...","action":{{
  "type":"work_plan_update",
  "parameters":{{
    "expected_revision":{getattr(plan, "revision", 1)},
    "reason":"...",
    "operations":[
      {{"op":"reassign","item_id":"item_2","assigned_to":"agent_d"}},
      {{"op":"add","item":{{"item_id":"item_3",
        "assigned_to":"agent_b","goal":"补集成测试"}}}}
    ],
    "qa": null
  }}
}}}}

🔒 不要在这里调用 work_complete。
🔒 没有 Agent 间通信动作；不要讨论身份或经济。
"""
