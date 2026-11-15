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
   作为团队的共享契约；具体实现和长任务描述放在 Shared Context。

只给当前角色真正需要的：

    目标 · DoD · Workspace 事实 ·
    任务级记忆 · 草稿 · 动作 schema

🔒 **system 层同样受这条边界约束**：Work 调用必须传
   `cognitive=False` + `WORK_SYSTEM_PROMPT`。
   `Brain` 默认会把 Agent 身份 / Constitution / 经济事实 / 自我模型
   拼进 system —— 那是 Agent 自己的认知，不属于 Work。

🔒 **prompt 只陈述环境事实，不规定工作流**：
   动作清单从真实 dispatcher 派生（`work/capabilities.py`），
   不写「先做 A 再做 B」；怎么做由 Agent 自己决定。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from work.capabilities import describe_action_params, describe_actions
from work.roles import WorkContext, WorkRole


#: Work 调用（Solo / Team / QA / Leader）使用的 system prompt。
#:
#: 🔴 只描述「你在执行一个 Task」这一层。
#:    Agent 的身份 / Constitution / 经济事实 / 自我模型 **不在这里** ——
#:    那些属于 Agent 自身的认知（Front / 接单 / 议价），不属于 Work。
#:    `Brain` 的 Cognitive Context 会把这些东西拼进 system；Work 调用
#:    通过 `cognitive=False` + 本 prompt 明确关掉它。
WORK_SYSTEM_PROMPT = (
    "你是一个执行单个 Task 的 Work Agent。"
    "Task 及其 Definition of Done 决定要交付什么；"
    "动作 schema 决定如何表达动作。"
)


#: 动作参数 schema。
#:
#: 🔒 之前只列动作名，模型会猜 `target` / `pattern` / `command`，
#:    导致大量“动作被拒绝 → 重来”的浪费。这里把字段名写死。


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
    return "\n".join(lines) if lines else "（Workspace 为空）"


def render_brief_sections(brief: Optional[Mapping[str, Any]]) -> str:
    """把 Front Brief 渲染成**来源明确**的事实块。

    🔒 不把整个 JSON 直接丢给 Worker：Worker 需要知道哪些是 Human 原话、
       哪些是 Front 的任务说明、哪些只是 Front 的建议。
    """
    if not brief:
        return "（没有额外 Brief；使用任务描述）"
    parts: List[str] = []
    human = str(brief.get("human_text") or "").strip()
    task = str(brief.get("task_text") or "").strip()
    notes = str(brief.get("front_notes") or "").strip()
    if human:
        parts.append(
            "Human 对 Front 的原话（不一定是需求）：\n" + human)
    if task:
        parts.append("Front 给 Worker 的任务说明：\n" + task)
    if notes:
        parts.append("Front 的建议（非硬要求）：\n" + notes)
    files = brief.get("files") or []
    if files:
        parts.append(
            "Front 提供的文件：\n"
            + json.dumps(files, ensure_ascii=False, indent=2))
    if not parts:
        goal = str(brief.get("goal") or "").strip()
        if goal:
            parts.append("Front 给 Worker 的任务说明：\n" + goal)
    return "\n\n".join(parts) or "（Brief 为空）"


def _render_brief(brief: Optional[Dict[str, Any]]) -> str:
    return render_brief_sections(brief)


def _render_public_memory(context: WorkContext) -> str:
    facts = str(context.public_memory_facts or "").strip()
    return facts or "（公共工作记忆为空）"


def _render_last_turn(last: Optional[Dict[str, Any]]) -> str:
    if not last:
        return ""
    lines = [
        "",
        "========== 上一轮事实 ==========",
        f"- 状态：{last.get('status_before')} -> {last.get('status_after')}",
        f"- 动作：{last.get('action_types') or []}",
        f"- tokens：{last.get('tokens', 0)}",
    ]
    if last.get("decision_kind"):
        line = f"- 决策：{last['decision_kind']}"
        if last.get("envelope_error"):
            line += f"（envelope_error={last['envelope_error']}）"
        lines.append(line)
    if last.get("thinking"):
        lines.append(
            "- thinking："
            + json.dumps(last["thinking"], ensure_ascii=False))
    budget = last.get("cognitive_budget")
    if isinstance(budget, dict):
        lines.append(
            "- 输出预算："
            f"effective={budget.get('effective')} tokens（Work 固定）")
    read_only_streak = last.get("read_only_streak")
    if isinstance(read_only_streak, int) and read_only_streak >= 2:
        lines.append(
            f"- 进展：最近 {read_only_streak} 轮只读；没有写文件、没有修改内容，"
            "也没有执行工具。")
    rewrites = last.get("rewrites")
    if isinstance(rewrites, dict) and rewrites:
        detail = ", ".join(
            f"{path} x {count}"
            for path, count in sorted(rewrites.items()))
        lines.append(f"- 重复整文件重写：{detail}")
    summary = str(last.get("round_summary") or "")
    if summary:
        lines.append(
            "\n========== 上一轮总结（完整） ==========\n"
            + summary)
    ignored = last.get("summary_actions_ignored")
    if isinstance(ignored, list) and ignored:
        lines.append(
            "- summary 阶段返回的动作被忽略、未执行："
            + ", ".join(str(x) for x in ignored))
        lines.append(
            "  上一轮总结可能暗示这些动作已经发生；依赖它们之前请先核实。")
    feedback = last.get("action_feedback")
    if isinstance(feedback, list) and feedback:
        lines.append("- 上一轮动作结果：")
        for item in feedback[:10]:
            if not isinstance(item, dict):
                continue
            status = "成功" if item.get("ok") else (
                "未执行（输出截断）"
                if not item.get("executed") else "失败")
            line = f"  - {item.get('action') or '?'}"
            if item.get("target"):
                line += f" {item['target']}"
            line += f"：{status}"
            if item.get("code"):
                line += f"（{item['code']}）"
            error = str(item.get("error") or "")
            if error:
                line += f" - {error[:240]}"
            lines.append(line)
    if last.get("truncated"):
        lines.append(
            "- 上一轮输出被截断；该输出中的动作没有执行。")
    if last.get("error"):
        lines.append(f"- 错误：{last['error']}")
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
    blocks = ["========== 工具文档（已读取，权威） =========="]
    for name in sorted(tool_docs):
        content = str(tool_docs.get(name) or "").strip()
        if not content:
            continue
        blocks.extend(["", f"### {name}", content])
    if len(blocks) == 1:
        return ""
    blocks.extend([
        "",
        "以上是工具文档中的真实约束。请严格遵守，并以这里的内容为准。",
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
    available_actions: Iterable[str] = (),
    next_actions: Iterable[str] = (),
    resume_context: str = "",
) -> str:
    """Task cognition: environment facts + a report from the Agent."""
    workspace = _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    criteria = "\n".join(
        f"- {item}" for item in context.acceptance_criteria
    ) or "（没有显式 DoD；以人类任务原文为准）"
    return f"""{context.agent_identity}

当前是任务认知阶段。

本阶段只读；之后才会进入 Work 阶段。

请产出一份认知报告，说明你理解了什么、打算如何推进。
这份报告会注入后续每一次决策。

========== 人类任务原文 ==========
{task_description}

========== 完成标准（Definition of Done） ==========
{criteria}

========== Brief ==========
{_render_brief(context.brief)}

========== 上一次运行检查点（只读） ==========
{resume_context.strip() or "（无）"}

========== 环境事实 ==========
workspace_id: {context.workspace_id}
{workspace}

========== 本阶段可用动作（只读） ==========
{describe_actions(available_actions)}

========== Work 阶段动作（下一阶段启用） ==========
{describe_actions(next_actions)}

========== Work 动作参数 ==========
{describe_action_params(next_actions)}

========== 可用工具 ==========
{_render_tool_catalog(tool_catalog)}
{_render_tool_docs(tool_docs)}
========== 工具文档 ==========
工具目录只列名称；`tool_help` 返回工具的权威文档。

========== 协议 ==========
本阶段只读；写文件与完成 Task 在 Work 阶段进行。
最多 {max_steps} 步。每一步只能是下面两种之一：

(A) ACT —— 一个普通 JSON 对象，直接以 `{{` 开头：
{{"decision":"act","reasoning":"一句话",
 "actions":[{{"type":"tool_help","parameters":{{"tool":"playwright"}}}}]}}

(B) REPORT —— 纯文本报告。当你已经有足够事实时使用。

动作统一使用普通 JSON 语法；工具调用标记（例如 `<|DSML|>`、`invoke name=`）
由 provider 层解析，框架以这里定义的 JSON 形状为准。
只有上面的 JSON 形状会产生动作。

报告应覆盖这个 Task 真正重要的内容：需求、环境、方法、风险、计划，
以及你如何判断它已经完成。
"""


def build_work_prompt(
    context: WorkContext,
    *,
    task_description: str,
    runtime: Any = None,
    last_turn: Optional[Dict[str, Any]] = None,
    extra_instruction: str = "",
    tool_docs: Optional[Dict[str, str]] = None,
    available_actions: Iterable[str] = (),
) -> str:
    """Build one Work turn prompt."""
    role = context.role
    if role == WorkRole.SOLO:
        role_text = (
            "你是这个 Task 的 Solo worker。请自己完成工作，并在结束时调用 "
            "work_complete。")
    elif role == WorkRole.LEAD:
        role_text = (
            "你是 Team Lead。负责计划、分配、集成和交付，自己推进到收尾。")
    elif role == WorkRole.QA:
        role_text = (
            "你是 QA。对照 Definition of Done 检查交付，并返回 "
            "work_qa_verdict。")
    else:
        role_text = (
            "你是 Team Member。专注分配给你的 item；使用 work_item_done / "
            "work_item_blocked 汇报。")

    criteria = "\n".join(
        f"- {item}" for item in context.acceptance_criteria
    ) or "（没有显式 DoD；以任务描述为准）"
    reply_style = str(
        (context.brief or {}).get("reply_style") or "").strip()
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    memory = context.memory_facts or "（没有任务级记忆）"
    drafts = context.draft_facts or "（没有草稿）"

    completion_hint = (
        "完成时调用：\n"
        '{"decision":"act","reasoning":"...",'
        '"action":{"type":"work_complete","parameters":{'
        '"result_ref":"<逻辑名，不是路径>",'
        '"summary":"一句话说明交付内容"}}}'
        if role == WorkRole.SOLO
        else "只有分配的工作真正完成后，才调用 work_complete。"
    )

    return f"""{context.agent_identity}

你正在处理 Task {context.task_id}
（第 {context.iteration} 次迭代）。

========== 角色 ==========
{role_text}

========== 任务 ==========
{task_description}

========== Brief ==========
{_render_brief(context.brief)}

========== 完成标准（Definition of Done） ==========
{criteria}

========== Front 要求的回复格式 ==========
{reply_style or "（无；简洁、友好，先结论后产物）"}

========== 任务认知（启动快照） ==========
{str(context.cognition_report or "").strip()
 or "（没有认知报告；使用任务原文和当前环境）"}

> 这只是启动快照。环境可能已经变化；需要时请重新读取或搜索，
> 并以当前环境为准。

========== Workspace 事实 ==========
workspace_id: {context.workspace_id}
{workspace}

========== 任务级记忆（私有工作记忆） ==========
{memory}

========== 当前草稿 ==========
{drafts}
{_render_last_turn(last_turn)}
{extra_instruction}
{_render_tool_docs(tool_docs)}
========== 决策 / 执行 ==========
每次调用处于 decision 或 work 阶段：
- decision：决定下一步；
- work：在读取 / 工具结果之后继续。
动作集合相同；你可以按阶段设置 thinking。
如果一次要检查多个文件，可以在一个 `actions` 列表里放多个
`workspace_read`（或 `workspace_search`）动作 —— 结果会在下一次
Work 步骤一起返回。

========== 可选：调整下一次调用 ==========
把以下字段放在 JSON 的**顶层**（与 action.parameters 同级）：
- "thinking": {{"enabled": true, "effort": "high"}}
  作用于当前阶段；DeepSeek 支持 low / high / max。
  用 enabled:false 关闭。也可以传按阶段设置的映射。

========== 环境约束 ==========
- `workspace_write` / `workspace_edit` 会持久化到 Workspace。
- `run_tool` 在 Workspace 的临时副本中执行；副本里写入的文件不会回到
  Workspace。
- 这里返回的工具输出有上限（约 12,000 字符）；工具超时范围为 1..300 秒。
- Workspace 是交付物的通道。
- `workspace_edit` 需要精确且唯一的 `old_string`；内容过期会失败。

========== 本阶段注册的动作 ==========
{describe_actions(available_actions)}

========== 动作与参数 ==========
{describe_action_params(available_actions)}

这里是 Work 层：没有 Agent 间消息，也没有身份 / 经济。
工具文档是权威来源：`tool_help`。
`work_complete` 记录你的声明；真正的交付结果是 Workspace 里的内容。

`work_complete.summary` 请使用人类使用的语言（任务原文的语言），
因为 Front 会把它展示给人类。

{completion_hint}

========== 输出格式 ==========
返回一个普通 JSON 对象，直接以 `{{` 开头：
{{"decision":"act","reasoning":"一句话",
 "thinking":{{"enabled":true,"effort":"high"}},
 "action":{{"type":"动作类型","parameters":{{...}}}}}}
thinking 是可选的；省略表示保持当前设置。
普通回复：{{"decision":"reply","content":"..."}}。

要一次做多件事（例如写多个小文件），使用动作列表：
{{"decision":"act","reasoning":"...",
 "actions":[{{"type":"workspace_write","parameters":{{...}}}},
           {{"type":"workspace_write","parameters":{{...}}}}]}}
动作按顺序执行；一个失败不会阻塞后面的动作。如果整个输出被截断，
则所有动作都不会执行。
"""


# ============================================================
# Team 模式
# ============================================================
def _render_items(items: Any, context: Optional[WorkContext] = None) -> str:
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
        owner = (
            context.label_for(item.agent_id)
            if context is not None else item.agent_id)
        lines.append(
            f"- {item.item_id} @ {owner}: {item.goal}"
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
    people = "\n".join(
        f"- {context.label_for(aid)}" for aid in participants) or "（无）"
    return f"""{context.agent_identity}

你是 Team Leader。当前 Task {context.task_id}
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

输出普通 JSON，直接以 `{{` 开头：
{{"items":[
  {{"item_id":"item_1","assigned_to":"agent_b","goal":"...",
    "paths":["src/module_b"],"acceptance":["..."]}}
],
 "qa": null,
 "directories":{{"src/module_b":"agent_b"}},
 "notes":"..."}}

🔒 这个阶段专注计划；下一步进入并行执行。
🔒 这个阶段的输出聚焦团队计划。
"""


def build_team_worker_prompt(
    context: WorkContext,
    *,
    task_description: str,
    items: Any,
    item_state: Dict[str, Any],
    runtime: Any = None,
    last_turn: Optional[Dict[str, Any]] = None,
    available_actions: Iterable[str] = (),
) -> str:
    """成员 prompt：只看到自己的 item，不看到其他成员的完整上下文。"""
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    return f"""{context.agent_identity}

你是 Team Member。

========== 你的分工 ==========
{_render_items(items, context)}

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
{describe_action_params(available_actions)}
- work_item_done:
  {{"item_id":"...", "summary":"...", "paths":["..."]}}
  （说明写 summary 字段）
- work_item_blocked:
  {{"item_id":"...", "reason":"..."}}

🔒 只做分给你的 item。完成后调用 work_item_done（带 item_id）。
🔒 被阻塞时调用 work_item_blocked（带 item_id + reason）。
🔒 同一个文件读一次并记录结论；需要定位其他内容时用 workspace_search。
🔒 公共工作记忆里已有契约时，直接以它为团队契约。
🔒 Work 层专注交付本身。
🔒 验收由 QA / Leader 负责；成员完成后调用 work_item_done。

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
    available_actions: Iterable[str] = (),
) -> str:
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    return f"""{context.agent_identity}

你是 QA。

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
{describe_action_params(available_actions)}
- work_qa_verdict:
  {{"passed":true|false, "feedback":"..."}}

========== 通过前的事实核对 ==========
- 对照公共工作记忆里的 team.architecture.files，逐个确认声明的文件真实存在；
- 如果入口文件引用了其他本地文件（script/link/import），确认它们也存在；
- 成员回报是声明，不是证据；缺失文件必须 passed=false；
- 以 DoD 和实际文件为准；每一条都核实后才通过。

========== 你的职责 ==========
检查整体交付是否满足 DoD。可以 workspace_read / workspace_search /
run_tool 验证，然后调用 work_qa_verdict。

🔒 需要修改时给出可执行的 feedback。
🔒 公共工作记忆已经是团队契约，直接以它为准。
🔒 Work 层专注交付本身。
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
    available_actions: Iterable[str] = (),
) -> str:
    workspace = context.workspace_facts or _render_workspace(
        runtime, context.workspace_id, context.agent_id)
    qa_text = f"\n========== QA 反馈 ==========\n{qa_feedback}\n" if qa_feedback else ""
    replan_text = (
        "\n如果 blocked / pending item 或 QA 反馈需要调整分工，"
        "可以调用 work_plan_update（reassign / add / cancel / update）。\n"
        if include_plan_update else "")
    return f"""{context.agent_identity}

你是 Team Leader。现在做集成、接线、收尾。

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
{describe_action_params(available_actions)}
- work_complete:
  {{"result_ref":"<逻辑名，不是路径>", "summary":"..."}}
- work_plan_update（重新计划时）:
  {{"expected_revision":<当前版本>, "reason":"...",
    "operations":[...], "qa": null}}
{replan_text}
🔒 公共工作记忆是团队共享契约，直接以它为准。
🔒 Work 层专注交付本身。
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
    team_text = "\n".join(
        f"- {context.label_for(aid)}" for aid in participants) or "（无）"
    return f"""{context.agent_identity}

你是 Team Leader。当前是**重新计划阶段**。

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
{team_text}

========== 你的职责 ==========
这个阶段专注调整计划；生产在后续阶段进行。根据 blocked / pending / QA 反馈决定：

- reassign 改派给另一个成员；
- add 增加一个补丁 / 集成 item；
- cancel 取消不再需要的 item；
- update 修改 item 目标 / 验收 / 路径；
- qa 可以新增、替换或清除（由 Leader 之外的参与者担任）。

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

🔒 这个阶段只调用 work_plan_update；完成动作留到集成阶段。
🔒 Work 层专注计划本身。
"""
