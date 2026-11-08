"""
Demand Entry V1 的 Prompt 构造
================================

只陈述环境事实 + 可用动作，不替 Agent 决定团队/分工/产物。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, List

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


def _envelope_example() -> str:
    return (
        '{"decision":"act","reasoning":"一句话说明你为什么这样做",'
        '"action":{"type":"动作类型","parameters":{...}}}'
    )


def build_intake_prompt(*, run: Any, runtime: Any, front_agent: str) -> str:
    directory = render_directory(runtime)
    return f"""你是 {front_agent}，AI Economy OS 里的一名 Agent。这是**正式运行**，不是演示。

========== Human Demand ==========
{run.demand}

========== 运行约束（环境事实） ==========
- 生产预算：{run.budget_aic} AIC（观测；不是 Task.price）
- 墙钟上限：{run.wall_limit_seconds / 60:.1f} 分钟
- 最多参与者：{run.max_agents}
- 当前没有 Task

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
) -> str:
    directory = render_directory(runtime)
    workspace = _render_workspace(runtime, task.workspace.workspace_id
                                 if task.workspace else "", agent_id)
    context = _render_context(runtime, task.task_id)
    inbox_text = _render_inbox(inbox)
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

========== Task Shared Context（共同认知） ==========
{context}

========== Workspace 事实（当前版本） ==========
{workspace}

========== 这一轮的信箱 ==========
{inbox_text}

========== Agent Directory（公开事实） ==========
{directory}

========== 你可以使用的动作 ==========
- `workspace_read` / `workspace_write`
  write 必须给 `write_mode`: create / replace / append
- `message_send`
  `to` / `message_type` / `text` / `payload`
  生命周期消息类型：START / SUBMIT / VERIFY / COMPLETE
- `assign_acceptance`
  把验收责任指派给另一个 participant（不能给自己）
- `shared_context_read`
- `run_tool`
  `tool`: python / pytest / node / playwright；`args` 是字符串列表；
  Playwright 会先跑 smoke gate；截图会在同一次认知的后续消息里回喂给你。

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
如果没有动作，可以普通回复（不产生 Action）。
"""
