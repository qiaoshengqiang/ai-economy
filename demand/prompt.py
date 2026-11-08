"""
Demand Entry V1 的 Prompt 构造
================================

只陈述环境事实 + 可用动作，不替 Agent 决定团队/分工/产物。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

MAX_DIRECTORY_CHARS = 4_000
MAX_WORKSPACE_CHARS = 6_000
MAX_CONTEXT_CHARS = 5_000
MAX_INBOX_CHARS = 6_000


def render_directory(runtime: Any) -> str:
    lines: List[str] = []
    try:
        entries = sorted(runtime.directory.all(), key=lambda e: e.agent_id)
    except Exception as exc:                                  # noqa: BLE001
        return f"（Agent Directory 读取失败：{type(exc).__name__}: {exc}）"
    for entry in entries:
        agent = None
        try:
            agent = runtime.economy.agent(entry.agent_id)
        except Exception:                                     # noqa: BLE001
            pass
        name = getattr(agent, "name", "") or entry.agent_id
        caps = entry.capabilities.names if entry.capabilities else ()
        cap_text = ", ".join(sorted(caps)) if caps else "(none)"
        lines.append(
            f"- {entry.agent_id} ({name}): capabilities=[{cap_text}], "
            f"available={entry.available}")
    text = "\n".join(lines) if lines else "（目录为空）"
    return _clip(text, MAX_DIRECTORY_CHARS)


def _render_workspace(runtime: Any, workspace_id: str,
                      agent_id: str) -> str:
    gateway = runtime.workspace_gateway
    lines: List[str] = []

    def walk(path: str, depth: int) -> None:
        if len(lines) >= 120:
            return
        result = gateway.list(workspace_id, path, agent_id)
        if not result.success:
            return
        for entry in (result.result or {}).get("entries", []) or []:
            if len(lines) >= 120:
                return
            name = str(entry.get("name") or "")
            if not name:
                continue
            child = f"{path.rstrip('/')}/{name}" if path != "/" else f"/{name}"
            kind = str(entry.get("type") or "")
            if kind == "directory":
                lines.append(f"- {child}/")
                if depth < 3:
                    walk(child, depth + 1)
            else:
                rev = entry.get("revision")
                r = f" (R{rev})" if rev is not None else ""
                lines.append(f"- {child}{r}")

    try:
        walk("/", 0)
    except Exception as exc:                                 # noqa: BLE001
        return f"（Workspace 读取失败：{type(exc).__name__}: {exc}）"
    text = "\n".join(lines) if lines else "（Workspace 目前为空）"
    return _clip(text, MAX_WORKSPACE_CHARS)


def _render_context(runtime: Any, task_id: str) -> str:
    try:
        current = runtime.alignment.current(task_id)
    except Exception as exc:                                  # noqa: BLE001
        return f"（Shared Context 读取失败：{type(exc).__name__}: {exc}）"
    if current is None:
        return "（这个 Task 还没有 Shared Context）"
    try:
        payload = {
            "revision": current.revision,
            "context": current.context.to_dict(),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:                                  # noqa: BLE001
        return f"（Shared Context 序列化失败：{type(exc).__name__}: {exc}）"
    return _clip(text, MAX_CONTEXT_CHARS)


def _render_inbox(messages: Iterable[Any]) -> str:
    items = list(messages)
    if not items:
        return "（这一轮你收到 0 条消息。）"
    lines = [f"这一轮你收到 {len(items)} 条消息：", ""]
    for m in items:
        text = ""
        try:
            text = str(getattr(m, "text", "") or "")
        except Exception:                                     # noqa: BLE001
            text = ""
        if not text:
            payload = getattr(m, "payload", None) or {}
            text = str(payload.get("text") or payload)[:500]
        lines.append(
            f"[{getattr(m, 'message_type', 'TEXT')}] "
            f"{getattr(m, 'from_agent', '?')} → "
            f"{getattr(m, 'to_agent', '?')}:")
        lines.append(text)
        lines.append("")
    return _clip("\n".join(lines).rstrip(), MAX_INBOX_CHARS)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（截断：完整 {len(text)} 字符；这里只给前 {limit}）"


def _safe(render: Any, fallback: str) -> str:
    try:
        return str(render())
    except Exception as exc:                                  # noqa: BLE001
        return f"{fallback}：{type(exc).__name__}: {exc}"


def _render_budget(runtime: Any, agent_id: str, *,
                   cap: Optional[int],
                   feedback: Optional[Dict[str, Any]]) -> str:
    """
    渲染**认知预算事实**。

    🔒 这是观测，不是指令：Agent 自己决定下一轮要不要调整。
    🔒 必须同时给出 requested / actual / clipped —— 否则 Agent
       无法知道“我请求了 32k，实际只拿到 16k”。
    """
    lines: List[str] = []
    if cap is not None:
        lines.append(f"- 本次运行的硬上限（Human）：{int(cap)} tokens")
    else:
        lines.append("- 本次运行没有额外的 Human 硬上限（Provider 结构上限仍生效）")
    try:
        agent = runtime.economy.agent(agent_id)
        brain = getattr(agent, "brain", None)
    except Exception:                                         # noqa: BLE001
        brain = None
    prefs = getattr(brain, "cognitive_budget_preferences", None)
    if callable(prefs):
        try:
            state = prefs() or {}
            lines.append(
                f"- config 默认偏好：{state.get('config_default')} tokens")
            per_phase = state.get("per_phase") or {}
            if per_phase:
                phase_text = ", ".join(
                    f"{k}={v}" for k, v in sorted(per_phase.items()))
                lines.append(f"- 当前分相偏好：{phase_text}")
            else:
                lines.append("- 当前分相偏好：（没有覆盖，使用 config 默认）")
        except Exception as exc:                              # noqa: BLE001
            lines.append(f"- 预算偏好读取失败：{type(exc).__name__}: {exc}")
    if feedback:
        lines.append(
            f"- 上一轮：turn={feedback.get('turn')} "
            f"phase={feedback.get('phase')} "
            f"finish_reason={feedback.get('finish_reason')} "
            f"output_status={feedback.get('output_status')}")
        lines.append(
            f"  requested={feedback.get('requested')} "
            f"actual={feedback.get('actual')} "
            f"clipped={feedback.get('clipped') or 'none'} "
            f"truncated={bool(feedback.get('truncated'))} "
            f"truncated_segments={feedback.get('truncated_segments') or 0} "
            f"segments={feedback.get('segments')}")
        if feedback.get("truncated"):
            lines.append(
                "  ⚠️ 上一轮有段被截断：该段即使解析出合法 Action 也不会执行。")
        if feedback.get("clipped"):
            lines.append(
                "  ⚠️ 上一轮请求的预算被裁剪；actual 才是真正可用的上限。")
    else:
        lines.append("- 上一轮：这是你这个 Agent 的第一次 turn。")
    lines.append(
        "- 你可以在信封顶层用 `cognitive_budget` 调整下一轮偏好："
        "`32768`（当前相）或 "
        "`{\"work\":32768,\"decision\":8192}`（分相）。")
    return "\n".join(lines)


def _envelope_example() -> str:
    return (
        '{"decision":"act","reasoning":"一句话说明你为什么这样做",'
        '"action":{"type":"动作类型","parameters":{...}}}'
    )


def build_intake_prompt(*, run: Any, runtime: Any, front_agent: str,
                        budget_cap: Optional[int] = None,
                        budget_feedback: Optional[Dict[str, Any]] = None) -> str:
    directory = render_directory(runtime)
    short_term = _safe(
        lambda: runtime.private_short_term_memory.render(front_agent),
        "（短期记忆读取失败）")
    long_term = _safe(
        lambda: runtime.private_long_term_memory.render_top(front_agent),
        "（长期记忆读取失败）")
    budget = _render_budget(
        runtime, front_agent, cap=budget_cap, feedback=budget_feedback)
    return f"""你是 {front_agent}，AI Economy OS 里的一名 Agent。这是**正式运行**，不是演示。

========== Human Demand ==========
{run.demand}

========== 运行约束（环境事实） ==========
- 生产预算：{run.budget_aic} AIC（观测；不是 Task.price）
- 墙钟上限：{run.wall_limit_seconds / 60:.1f} 分钟
- 最多参与者：{run.max_agents}
- 当前没有 Task

========== 认知预算（观测，不是指令） ==========
{budget}

========== 你的短期记忆（跨任务摘要，只有你能读） ==========
{short_term}

========== 你的长期记忆（高权重摘要，只有你能读） ==========
{long_term}

========== Agent Directory（公开事实，不是“你应该找谁”） ==========
{directory}

========== 你的选择 ==========
1. 接受这个 Demand，创建一个 **Root Task**；
2. 如果你判断无法完成，可以普通回复说明原因（不创建 Task）。

如果创建 Task，必须由你调用 `task_create`。参数：
- `description`: 任务目标（可以把 Human Demand 概括进去）
- `participants`: 参与者 agent_id 列表，**必须包含你自己**，至少 2 人
- `acceptance_criteria`: Definition of Done，至少 1 条
- `producer`（必填）：负责实际生产的 Agent；必须是 participants 之一。
  如果你自己生产，填你自己。
- `acceptance_assignee`（可选）：验收责任人；必须是 participants 之一，
  不能是 creator 自己，也不能和 producer 相同。

🔒 验收人不能是生产者；生产者不得自验。
🔒 参与者在创建时**直接指定**（V1）；没有邀请/接受流程。
🔒 V1 只允许一个 Root Task；这个 run 再调用 `task_create` 会被拒绝。

输出格式（只输出 JSON 信封，不要 markdown 围栏）：
{_envelope_example()}

示例（只是形状，不要照抄内容）：
{{"decision":"act","reasoning":"接受 Demand，创建根任务",
  "action":{{"type":"task_create","parameters":{{
    "description":"...",
    "participants":["{front_agent}","agent_b"],
    "acceptance_criteria":["交付物存在且非空","核心功能可运行"],
    "producer":"{front_agent}",
    "acceptance_assignee":"agent_b"
  }}}}}}
"""


def build_task_prompt(
    *,
    run: Any,
    task: Any,
    runtime: Any,
    agent_id: str,
    inbox: Iterable[Any],
    budget_cap: Optional[int] = None,
    budget_feedback: Optional[Dict[str, Any]] = None,
) -> str:
    directory = render_directory(runtime)
    workspace = _render_workspace(runtime, task.workspace.workspace_id
                                 if task.workspace else "", agent_id)
    context = _render_context(runtime, task.task_id)
    public_working_memory = _safe(
        lambda: runtime.public_working_memory.render(task.task_id, agent_id),
        "（公共工作记忆读取失败）")
    private_working_memory = _safe(
        lambda: runtime.private_working_memory.render(agent_id, task.task_id),
        "（私有工作记忆读取失败）")
    short_term = _safe(
        lambda: runtime.private_short_term_memory.render(agent_id),
        "（短期记忆读取失败）")
    long_term = _safe(
        lambda: runtime.private_long_term_memory.render_top(agent_id),
        "（长期记忆读取失败）")
    inbox_text = _render_inbox(inbox)
    budget = _render_budget(
        runtime, agent_id, cap=budget_cap, feedback=budget_feedback)
    try:
        acceptors = sorted(runtime.task_execution.acceptors_of(task))
    except Exception:                                         # noqa: BLE001
        acceptors = []
    role = []
    if agent_id == task.creator:
        role.append("creator")
    if task.contractor and agent_id == task.contractor:
        role.append("contractor/producer")
    if agent_id in acceptors:
        role.append("acceptor")
    if agent_id in task.participants:
        role.append("participant")
    role_text = ", ".join(role) or "participant"

    return f"""你是 {agent_id}，这是正式运行中的一个 Task turn。

========== 你的角色事实 ==========
{role_text}
（这只是事实，不规定你必须做什么；怎么分工由你们自己决定。）

========== Task Core（唯一生命周期权威） ==========
- task_id: {task.task_id}
- status: {task.status}
- creator: {task.creator}
- contractor/producer: {task.contractor or "（还没有）"}
- participants: {", ".join(task.participants)}
- acceptance assignees: {", ".join(acceptors) or "（还没有）"}
- workspace_id: {task.workspace.workspace_id if task.workspace else "（无）"}
- description: {task.description}

========== 认知预算（观测，不是指令） ==========
{budget}

========== Task Shared Context（共同认知） ==========
{context}

========== 公共工作记忆（Task 范围，参与者可见） ==========
{public_working_memory}

========== 私有工作记忆（只有你自己可见） ==========
{private_working_memory}

========== 你的短期记忆（跨任务摘要） ==========
{short_term}

========== 你的长期记忆（高权重摘要） ==========
{long_term}

========== Workspace 事实（当前版本） ==========
{workspace}

========== 这一轮的信箱 ==========
{inbox_text}

========== Agent Directory（公开事实） ==========
{directory}

========== 你可以使用的动作 ==========
- `workspace_read`
  可给 `offset`（1-based 起始行）/ `limit`（最多行数）；显式范围最多 400 行。
- `workspace_search`
  `query` / `path`（可选）/ `glob`（可选）/ `max_results`（可选）/
  `case_sensitive` / `context_lines`；只读，返回 path/line/snippet。
- `workspace_write`
  write 必须给 `write_mode`: create / replace / append。
  create/replace 需要输出完整文件；能局部改就不要用 replace。
- `workspace_edit`
  `path` / `old_string` / `new_string`；old_string 必须唯一命中；
  可选 `replace_all` / `expected_revision`。
  仍然走锁 / revision / audit；0 或多个命中会失败。
- `message_send`
  `to` / `message_type` / `text` / `payload`
  生命周期消息类型：START / SUBMIT / VERIFY / COMPLETE
- `assign_acceptance`
  把验收责任指派给另一个 participant（不能给自己）
- `shared_context_read`
- `run_tool`
  `tool`: python / pytest / node / playwright；`args` 是字符串列表；
  `source`（可选，task/private）与 `path`（可选）决定复制哪些文件到 run 目录；
  可选 `timeout_seconds`。
- `public_query`：读公共资料区（含 SYSTEM_MAP 和 tools/*.md）。
- `private_write/read/list/delete`：你自己的私有文件空间。
- `resource_create` / `resource_handoff` / `resource_release`：
  建目录 / 交接责任 / 释放独立责任。
- `public_working_memory_read/update`：Task 范围的计划/分工/进度。
- `private_working_memory_read/update`：你自己的工作结构/发现/问题。
- `short_term_memory_read/update/reinforce`：跨任务短期记忆。
- `long_term_memory_search/read/mark_used/consolidate`：长期记忆 + 权重。
  工作记忆是**你自己的记录**；系统不自动写、不自动总结。
  `workspace_search` 只读，**不会**把结果写进任何记忆。

========== 工具与执行边界（结构事实） ==========
- `run_tool` 在**一次性 run 副本**里执行。对副本的写（下载、生成、删除）
  **不会回写 Task Workspace**。要交付文件，必须用 `workspace_write` /
  `workspace_edit` 写入 Workspace。
- Playwright 需要**先写测试文件**：在 Workspace 写 `tests/foo.spec.js`，
  再 `run_tool(tool="playwright", args=["tests/foo.spec.js"])`；
  它会先跑 smoke gate。不要用 `run_tool(python/node)` 去导入 Playwright
  （Python 环境没有这个包）。
- 工具说明在公共资料区：
  `public_query(name="tools/python.md")` ·
  `public_query(name="tools/pytest.md")` ·
  `public_query(name="tools/node.md")` ·
  `public_query(name="tools/playwright.md")`。
- 验收人如果**无法复跑验证**，应发 `VERIFY(passed=false)` 并写明缺口/证据；
  不要用重复 `workspace_read` / `run_tool` 代替验收结论。

========== 生命周期规则（协议硬约束） ==========
- 如果你不是 contractor/producer：**不要发 START / SUBMIT**。
  发 START / SUBMIT 会被状态机拒绝。
- 如果你是 contractor/producer 且 Task 还是 CREATED：先发 START。
  例：{{"to":"{task.creator}","message_type":"START",
       "payload":{{"task_id":"{task.task_id}"}}}}
- 如果你是 contractor/producer 且 Task 是 IN_PROGRESS：
  完成工作后发 SUBMIT。载荷必须包含：
  {{"to":"<任意 participant>","message_type":"SUBMIT","payload":{{
    "task_id":"{task.task_id}",
    "result":{{"result_ref":"<逻辑名，不能是路径/URL>",
              "result_type":"code",
              "summary":"<一句话>"}},
    "completion_proposal":{{
      "criteria_status":"MET",
      "evidence":[{{"result_ref":"<同一个逻辑名>",
                   "result_type":"code",
                   "summary":"<证据说明>"}}],
      "outstanding_gaps":[]
    }}
  }}}}
- 如果你不是 creator/acceptor：**不要发 VERIFY / COMPLETE**。
- 如果你是 acceptor：只在 Task 为 SUBMITTED 后发 VERIFY。
  载荷：{{"to":"<producer>","message_type":"VERIFY",
          "payload":{{"task_id":"{task.task_id}","passed":true,
                     "feedback":"<独立复跑证据>"}}}}
- VERIFY 通过后，再由 creator 或 acceptor 发 COMPLETE：
  {{"to":"<producer>","message_type":"COMPLETE",
    "payload":{{"task_id":"{task.task_id}","summary":"..."}}}}
- 如果还没有验收人，由**非 producer** 用 `assign_acceptance` 指派。
- DoD 在创建时已写入；不要凭空改 Task 状态。

========== 输出格式 ==========
只输出 JSON 信封，不要 markdown 围栏：
{_envelope_example()}
可选：在信封顶层放 `thinking`（按相设置思考强度）或
`cognitive_budget`（当前相整数 / 分相对象），只影响**下一步**。
如果没有动作，可以普通回复（不产生 Action）。
"""
