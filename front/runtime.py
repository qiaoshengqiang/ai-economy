"""
FrontAgentRuntime —— Human 意图入口
====================================

    Human 输入
        ↓
    FrontAgentRuntime.decide()
        ├─ direct_reply
        ├─ handoff  → FrontBrief
        ├─ rework   → 原 Task 的 Creator
        ├─ follow_up→ 进行中 Task 的 Creator
        ├─ cancel   → 停止 / 取消 Task
        ├─ artifacts→ 导出 Task 产物到宿主机
        └─ clarify

🔒 Front Agent 永不创建 Task。
   它可以对话、使用自己的私有空间 / 草稿，并输出 Decision / Brief；
   Task 由承接的 Agent 创建。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from front.contracts import FrontDecision, FrontIntent, FrontResult
from front.parser import parse_front_actions, parse_front_decision


#: Front 调用的 system prompt —— 只描述入口职责。
#:
#: 🔴 不写 Agent 身份 / 经济 / Constitution：那些属于接单与议价，
#:    不属于「把 Human 的话路由到哪里」。
FRONT_SYSTEM_PROMPT = (
    "You are the Human-facing entry point of an agent system. "
    "You can chat, use your private workspace and drafts, and hand work to "
    "a Work Agent. Reply to the Human in the language they used."
)


class FrontUnavailableError(RuntimeError):
    """Front 本次没有产出可用决定；这是可重试的失败，不是 clarify。"""


class FrontAgentRuntime:
    """一个可测试的 Front Agent（LLM 调用通过 `think` 注入）。"""

    def __init__(
        self,
        agent_id: str,
        think: Callable[[str], str],
        *,
        inspect_fn: Optional[Callable[[str], Any]] = None,
        execute_actions: Optional[
            Callable[[List[Dict[str, Any]]], str]] = None,
        action_types: Iterable[str] = (),
        max_steps: int = 12,
        think_retries: int = 1,
    ) -> None:
        if not callable(think):
            raise ValueError("FrontAgentRuntime 需要可调用的 think(prompt)")
        self.agent_id = str(agent_id)
        self._think = think
        #: 只读 inspect 能力：task_id → 事实（dict/str）。None = 不可用。
        self._inspect_fn = inspect_fn
        #: Front 自己的动作执行器（私有空间 / 草稿）。
        self._execute_actions = execute_actions
        self.action_types = tuple(sorted({
            str(item) for item in action_types if str(item).strip()}))
        self.max_steps = max(1, int(max_steps))
        self.think_retries = max(0, int(think_retries))
        #: 最近一次 decide 的动作历史 / 原始输出（观测用）。
        self.last_action_history: List[Dict[str, Any]] = []
        self.last_raw_output: str = ""

    # ------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------
    def decide(
        self,
        human_text: str,
        *,
        active_tasks: Iterable[Dict[str, Any]] = (),
        completed_tasks: Iterable[Dict[str, Any]] = (),
        conversation: Iterable[Dict[str, Any]] = (),
        agents: Iterable[Dict[str, Any]] = (),
        context: str = "",
        max_inspect: int = 2,
    ) -> FrontDecision:
        """
        识别 Human 意图。

        `active_tasks` / `completed_tasks` 是 **只读引用**：
        `{"task_id", "summary", "status"}`。Front 不修改它们。

        `conversation` 是最近 Human / Front 消息的只读快照。

        `agents` 是系统里的 Agent 事实：`{agent_id, status, role, can_work}`。
        Front 可以据此决定是否指定 `preferred_agent`；系统不规定它怎么做。

        `inspect` 是只读能力，不是最终决定；最多 `max_inspect` 次。
        """
        active = [
            _task_ref(item) for item in active_tasks
            if _task_ref(item)
        ]
        completed = [
            _task_ref(item) for item in completed_tasks
            if _task_ref(item)
        ]
        active_ids = {item["task_id"] for item in active}
        completed_ids = {item["task_id"] for item in completed}
        history = _conversation_refs(conversation)
        agent_facts = _agent_refs(agents)

        inspection: List[Dict[str, Any]] = []
        action_history: List[Dict[str, Any]] = []
        last_error = ""
        parse_failures = 0
        inspect_count = 0
        self.last_action_history = []
        self.last_raw_output = ""
        for _step in range(self.max_steps):
            prompt = self._prompt(
                human_text, active, completed, conversation=history,
                agents=agent_facts, inspection=inspection,
                action_history=action_history,
                context=context, previous_error=last_error)
            raw = self._think_with_retry(prompt)
            self.last_raw_output = str(raw or "")

            decision, error = parse_front_decision(
                raw,
                human_text=human_text,
                active_task_ids=active_ids,
                completed_task_ids=completed_ids,
            )

            # 动作先执行：同一份输出里既写文件又给出最终 intent 也成立。
            actions, action_error = parse_front_actions(raw)
            if actions:
                if self._execute_actions is None:
                    raise FrontUnavailableError(
                        "Front actions are not available in this runtime")
                receipt = self._execute_actions(actions)
                action_history.append({
                    "step": len(action_history) + 1,
                    "actions": actions,
                    "raw_output": str(raw or ""),
                    "receipt": str(receipt or ""),
                })
                self.last_action_history = list(action_history)
                parse_failures = 0
                last_error = ""
                if decision is None:
                    continue

            if decision is not None:
                if decision.intent == FrontIntent.INSPECT:
                    if self._inspect_fn is None:
                        raise FrontUnavailableError(
                            "inspect_unavailable：Front 需要读取任务事实，"
                            "但当前没有 inspect 能力")
                    if inspect_count >= max_inspect:
                        raise FrontUnavailableError(
                            f"inspect_limit：本次最多 inspect {max_inspect} 次")
                    try:
                        facts = self._inspect_fn(decision.task_id)
                    except Exception as exc:                  # noqa: BLE001
                        facts = (
                            f"inspect_error: {type(exc).__name__}: {exc}")
                    inspection.append({
                        "task_id": decision.task_id,
                        "facts": facts,
                    })
                    inspect_count += 1
                    continue

                decision.raw_output = str(raw or "")
                self.last_action_history = list(action_history)
                return decision

            last_error = action_error or error or "front_decision_not_found"
            parse_failures += 1
            if parse_failures >= 2:
                raise FrontUnavailableError(
                    f"Front output could not be parsed: {last_error}")

        raise FrontUnavailableError(
            f"Front did not reach a final decision within "
            f"{self.max_steps} steps")

    def _think_with_retry(self, prompt: str) -> str:
        """LLM 调用失败时重试；最终失败交给上层标记可重试中断。"""
        attempts = self.think_retries + 1
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                return self._think(prompt)
            except Exception as exc:                          # noqa: BLE001
                last = exc
        raise FrontUnavailableError(
            f"Front LLM call failed after {attempts} attempt(s): "
            f"{type(last).__name__}: {last}")

    # ------------------------------------------------------------
    # 结果格式化
    # ------------------------------------------------------------
    @staticmethod
    def format_result(result: FrontResult) -> str:
        """Work Result → Human 可读文本。"""
        return result.to_human_text()

    @staticmethod
    def result_from_work(
        work_result: Any,
        *,
        artifacts: Optional[List[str]] = None,
        next_actions: Optional[List[str]] = None,
    ) -> FrontResult:
        """
        把 WorkResult（或同形状对象）转成 FrontResult。

        🔒 只读转换；不改变 Task 状态。
        """
        if hasattr(work_result, "to_dict"):
            data = work_result.to_dict()
        elif isinstance(work_result, dict):
            data = dict(work_result)
        else:
            data = {
                "task_id": getattr(work_result, "task_id", ""),
                "status": getattr(work_result, "status", ""),
                "summary": getattr(work_result, "summary", ""),
                "iteration": getattr(work_result, "iteration", 1),
                "error": getattr(work_result, "error", ""),
            }
        return FrontResult(
            task_id=str(data.get("task_id") or ""),
            status=str(data.get("status") or ""),
            summary=str(data.get("summary") or ""),
            artifacts=list(artifacts or data.get("artifacts") or []),
            next_actions=list(next_actions or data.get("next_actions") or []),
            iteration=int(data.get("iteration") or 1),
            error=str(data.get("error") or ""),
        )

    # ------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------
    def _prompt(
        self,
        human_text: str,
        active: List[Dict[str, str]],
        completed: List[Dict[str, str]],
        *,
        conversation: List[Dict[str, str]] = (),
        agents: List[Dict[str, Any]] = (),
        inspection: List[Dict[str, Any]] = (),
        action_history: List[Dict[str, Any]] = (),
        context: str = "",
        previous_error: str = "",
    ) -> str:
        error_text = ""
        if previous_error:
            error_text = (
                "\n上一次输出无法解析：" + previous_error +
                "。继续下一步；最终仍要输出一个合法的 JSON。\n")
        actions = ", ".join(self.action_types) or "(none)"
        return f"""You are {self.agent_id}, the Human-facing entry point.

Talk naturally in the language the Human used. You can use your private
workspace and drafts, and you can hand work to a Work Agent. The Work Agent
creates the Task; you don't have a create-task action.

========== RECENT CONVERSATION (READ-ONLY) ==========
{_render_conversation(conversation)}

========== CURRENT HUMAN INPUT ==========
{human_text}

========== AGENTS (READ-ONLY FACTS) ==========
{_render_agents(agents)}

========== ACTIVE TASKS (READ-ONLY) ==========
{_render_tasks(active)}

========== COMPLETED TASKS (READ-ONLY) ==========
{_render_tasks(completed)}

========== INSPECTION RESULTS (READ-ONLY) ==========
{_render_inspection(inspection)}

========== FRONT ACTIONS (OPTIONAL, MULTI-STEP) ==========
Available actions: {actions}

You may return action-only steps before the final intent:
{{"actions":[{{"type":"private_write","parameters":{{"path":"notes.md","content":"..."}}}}]}}

Actions run for real; their results come back in the next prompt. Then continue.
Parameters:
- private_write {{path, content}}; private_read {{path}}; private_list {{}}
- private_delete {{path}}
- draft_write {{content, label}}; draft_append {{draft_id, content}}
- draft_read {{draft_id}}; draft_list {{}}; draft_delete {{draft_id}}
A handoff can carry files you prepared in `brief.files`:
[{{"path":"index.html","content":"..."}}]
[{{"path":"index.html","from_private":"snake.html"}}]
[{{"path":"index.html","draft_id":"draft_..."}}]

========== ACTION RESULTS / STATE ==========
{_render_actions(action_history)}

========== OTHER CONTEXT ==========
{context or "(none)"}
{error_text}
========== INTENTS ==========
1. direct_reply — answer directly.
   {{"intent":"direct_reply","reply":"...","reason":"..."}}

2. handoff — a new work request.
   task_text is what the Work Agent sees; human_text keeps the original.
   preferred_agent is routing metadata; empty/omitted = system picks an idle worker.
   brief may carry goal / deliverables / constraints / acceptance_expectations
   and files.
   {{"intent":"handoff",
     "task_text":"...",
     "preferred_agent":"optional agent_id",
     "brief":{{"goal":"...","deliverables":["..."],
              "constraints":["..."],"acceptance_expectations":["..."],
              "files":[{{"path":"index.html","content":"..."}}]}},
     "reply":"...",
     "reply_style":"...","reason":"..."}}

3. rework — changes to a COMPLETED Task.
   {{"intent":"rework","task_id":"...","reply":"...","reason":"..."}}

4. follow_up — continue an ACTIVE / INTERRUPTED Task.
   {{"intent":"follow_up","task_id":"...","reply":"...","reason":"..."}}

5. cancel — stop an ACTIVE Task.
   {{"intent":"cancel","task_id":"...","reply":"...","reason":"..."}}

6. artifacts — show / export a Task's files.
   {{"intent":"artifacts","task_id":"...","reply":"...","reason":"..."}}

7. clarify — ask the Human when the choice matters.
   {{"intent":"clarify","question":"...","reason":"..."}}

8. inspect — read-only task facts; at most 2 per input.
   {{"intent":"inspect","task_id":"...","reason":"..."}}

Routing facts:
- preferred_agent IDLE + can_work → assigned to it.
- preferred_agent WORKING → rejected; no automatic substitution.
- unknown / FRONT / cannot-work → rejected.
- no preferred_agent → system picks an idle worker.

A Task with status INTERRUPTED is not running now; follow_up can resume it.
Return JSON only, no markdown fences.
"""


def _render_actions(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "(none)"
    lines: List[str] = []
    for item in items:
        actions = item.get("actions") or []
        names = ", ".join(
            str(action.get("type") or "?")
            for action in actions if isinstance(action, dict))
        lines.append(f"step {item.get('step', '?')}: {names}")
        receipt = str(item.get("receipt") or "").strip()
        if receipt:
            if len(receipt) > 12000:
                receipt = receipt[:12000] + "\n…（动作结果已截断）"
            lines.append(receipt)
    return "\n".join(lines)

def _conversation_refs(
    items: Iterable[Dict[str, Any]],
    *,
    limit: int = 12,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        text = str(item.get("text") or "").strip()
        if role not in ("human", "front") or not text:
            continue
        out.append({
            "role": "Human" if role == "human" else "Front",
            "text": text[:1200],
        })
    return out[-max(0, int(limit)):]


def _render_conversation(items: List[Dict[str, str]]) -> str:
    if not items:
        return "（没有更早的对话）"
    return "\n".join(
        f"{item['role']}: {item['text']}" for item in items)


def _agent_refs(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id") or "").strip()
        if not agent_id:
            continue
        out.append({
            "agent_id": agent_id,
            "status": str(item.get("status") or ""),
            "role": str(item.get("role") or ""),
            "can_work": bool(item.get("can_work", True)),
        })
    return out


def _render_agents(agents: List[Dict[str, Any]]) -> str:
    if not agents:
        return "(none)"
    return "\n".join(
        f"- {item['agent_id']}: status={item['status'] or '?'} "
        f"role={item['role'] or 'WORKER'} can_work={item['can_work']}"
        for item in agents
    )


def _render_inspection(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "(none)"
    lines: List[str] = []
    for item in items:
        task_id = str(item.get("task_id") or "")
        facts = item.get("facts")
        if not isinstance(facts, (dict, list)):
            facts = str(facts or "")
        try:
            text = json.dumps(facts, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(facts)
        if len(text) > 6000:
            text = text[:6000] + "\n… (inspection facts truncated)"
        lines.append(f"[{task_id}]\n{text}")
    return "\n\n".join(lines)


def _task_ref(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not isinstance(item, dict):
        return None
    task_id = str(item.get("task_id") or "").strip()
    if not task_id:
        return None
    return {
        "task_id": task_id,
        "status": str(item.get("status") or ""),
        "summary": str(item.get("summary") or "")[:500],
    }


def _render_tasks(items: List[Dict[str, str]]) -> str:
    if not items:
        return "（无）"
    lines = []
    for item in items:
        summary = f" — {item['summary']}" if item.get("summary") else ""
        status = f" [{item['status']}]" if item.get("status") else ""
        lines.append(f"- {item['task_id']}{status}{summary}")
    return "\n".join(lines)
