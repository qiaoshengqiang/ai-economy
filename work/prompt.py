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
  {"path":"<相对路径>", "offset":0, "limit":12000}
  ⚠️ 路径字段必须叫 path，不要用 target。
- workspace_search:
  {"query":"<要搜的文本>", "path":"", "glob":"", "max_results":20}
  ⚠️ 查询字段必须叫 query；glob 才是文件模式。
- workspace_write:
  {"path":"<相对路径>", "content":"<完整内容>",
   "write_mode":"create|replace|append"}
  write_mode 省略时默认 create；新文件用 create，替换已有文件用 replace，
  追加用 append。大文件可以一次写，不必强行拆成很小。
- workspace_edit:
  {"path":"<相对路径>", "old_string":"<唯一旧片段>",
   "new_string":"<新片段>", "replace_all":false}
- run_tool:
  {"tool":"python|pytest|node|playwright", "args":[...],
   "source":"task|private", "path":"<入口文件>", "timeout_seconds":60}
  ⚠️ 不要用 command 字段。
  ⚠️ args 每项 ≤ 2000 字符；长脚本先 private_write 落文件，再运行文件。
- public_working_memory_read:
  {}
- public_working_memory_update:
  {"expected_revision":<当前 revision>,
   "items":{"<key>":{"kind":"plan|assignment|checklist|progress|note|conflict",
                     "value":<任意 JSON>,
                     "parent_key":"<可选>",
                     "evidence":{"path":"<真实文件>","revision":<整数>}}},
   "remove_keys":[]}
  🔒 Public WM 只存**抽象架构**：目录、文件归属、类/方法签名、
     接口、依赖关系、进度。不要存代码、完整实现、长 acceptance
     或大段任务描述；条目越小越好。
- private_working_memory_read / private_working_memory_update:
  同上，但只属于当前 Agent + 当前 Task。
- short_term_memory_read / short_term_memory_update / short_term_memory_reinforce
- long_term_memory_search / long_term_memory_read / long_term_memory_mark_used
- private_write / private_read / private_list / private_delete:
  只操作当前 Agent 的私有空间。
- resource_create / resource_handoff / resource_release:
  {"path":"<相对路径>", "to":"<agent_id>", "reason":"..."}
- draft_write / draft_append / draft_read / draft_list / draft_delete
- workspace_promote_drafts:
  {"items":[{"draft_id":"...", "path":"...",
             "write_mode":"create|replace|append"}]}
"""


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


def _render_public_memory(context: WorkContext) -> str:
    facts = str(context.public_memory_facts or "").strip()
    return facts or "（公共工作记忆为空）"


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
            f"{path} × {count}"
            for path, count in sorted(rewrites.items()))
        lines.append(
            f"- 反复整文件重写：{detail}；"
            "优先用 workspace_edit 做局部修改。")
    summary = str(last.get("round_summary") or "")
    if summary:
        lines.append(
            "\n========== 上一轮总结（完整） ==========\n" + summary)
    feedback = last.get("action_feedback")
    if isinstance(feedback, list) and feedback:
        lines.append("- 上一轮动作结果：")
        for item in feedback[:10]:
            if not isinstance(item, dict):
                continue
            status = "ok" if item.get("ok") else (
                "未执行（截断）" if not item.get("executed") else "失败")
            line = f"  - {item.get('action') or '?'}"
            if item.get("target"):
                line += f" {item['target']}"
            line += f": {status}"
            if item.get("code"):
                line += f" ({item['code']})"
            error = str(item.get("error") or "")
            if error:
                line += f" — {error[:240]}"
            lines.append(line)
    if last.get("truncated"):
        lines.append(
            "- 上一轮输出被截断：下一次可把 cognitive_budget 提到 65536，"
            "或拆小工作单元 / 先写草稿再分批落盘。")
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
            "你是 Team Lead。负责计划、分配、集成和最终交付；"
            "不要把工作推回给 Human。"
        )
    elif role == WorkRole.QA:
        role_text = (
            "你是 QA。对照 Definition of Done 检查整体交付；"
            "用 work_qa_verdict 给出通过 / 不通过。"
        )
    else:
        role_text = (
            "你是 Team Member。只完成分配给你的部分；"
            "用 work_item_done / work_item_blocked 标记状态；"
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

========== 公共工作记忆（团队共享契约 / 计划 / 进度） ==========
{_render_public_memory(context)}

========== Workspace 事实 ==========
workspace_id: {context.workspace_id}
{workspace}

========== 任务级记忆（Private Working Memory） ==========
{memory}

========== 当前草稿 ==========
{drafts}
{_render_last_turn(last_turn)}
{extra_instruction}
========== 决策 / 工作 ==========
你每次调用处在 decision 或 work 阶段：
- decision：判断下一步做什么；
- work：执行读取 / 工具后的下一步。
动作集合相同；区别是你可以为不同阶段设置下面的
thinking / cognitive_budget。

========== 可选：调整下一次调用 ==========
在同一个 JSON 的**顶层**加入（不要放进 action.parameters）：
- "thinking": {{"enabled": true, "effort": "high"}}
  单相作用于**当前阶段**；DeepSeek 支持 effort: low / high / max；
  关闭写 enabled:false。
  也可以分相：{{"decision": {{...}}, "work": {{...}}}}。
- "cognitive_budget": 32768
  单相作用于**当前阶段**；
  或分相：{{"decision": 8192, "work": 65536}}
  单位是 max_tokens（输出上限）；当前硬上限 65536，
  实际值 = min(你的偏好, 65536, Provider 上限)。
  不设置就保持当前默认值（config 里的 max_tokens）。
  它只影响**下一次** LLM 调用，不影响当前这次输出。
  如果下一次要写很大的文件：先用一次
  {{"decision":"reply","content":"准备写大文件",
    "cognitive_budget":65536}} 只设置预算，再在下一轮写。

========== 工作节奏 ==========
- 先确定模块边界和文件清单，再开始写第一个文件；
- 入口文件先搭骨架，之后用 workspace_edit 增量修改，
  避免整文件反复重写；
- 多个小文件可以放进一次 actions 列表；大文件仍建议先设置
  cognitive_budget 再写，或拆成 create + append；
- 每完成一个模块，尽量用 run_tool 验证一次，再继续下一个。

========== 可用动作与参数 ==========
{ACTION_SCHEMA}
- work_complete:
  {{"result_ref":"<逻辑名，不是路径>", "summary":"..."}}

🔒 这是 Work 层：没有 Agent 间通信动作，也不讨论身份或经济。
🔒 先读再改；写入前确认路径与锁；大文件可以一次写入，避免无意义拆分。
🔒 不要声称完成而没有实际产物。

{completion_hint}

========== 输出格式 ==========
只输出一个 JSON 对象，不要 markdown 围栏：
{{"decision":"act","reasoning":"一句话说明为什么这样做",
 "thinking":{{"enabled":true,"effort":"high"}},
 "cognitive_budget":{{"work":32768}},
 "action":{{"type":"动作类型","parameters":{{...}}}}}}
thinking / cognitive_budget 是可选的，省略就保持当前设置。
如果只是普通回复，可以输出 {{"decision":"reply","content":"..."}}。

如果要一次做多件事（例如一次写多个小文件），用动作列表：
{{"decision":"act","reasoning":"...",
 "actions":[{{"type":"workspace_write","parameters":{{...}}}},
           {{"type":"workspace_write","parameters":{{...}}}}]}}
动作按声明顺序执行；一个失败不阻止后面的。但如果整段输出被截断，
所有动作都不会执行。
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
