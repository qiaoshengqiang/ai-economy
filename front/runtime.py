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
   它只输出 Decision / Brief；Task 由承接的 Agent 创建。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from front.contracts import FrontDecision, FrontIntent, FrontResult
from front.parser import parse_front_decision


#: Front 调用的 system prompt —— 只描述入口职责。
#:
#: 🔴 不写 Agent 身份 / 经济 / Constitution：那些属于接单与议价，
#:    不属于「把 Human 的话路由到哪里」。
FRONT_SYSTEM_PROMPT = (
    "You are the Human-facing entry point of an agent system. "
    "Decide the next action from the Human input, the recent conversation, "
    "and the task list. Reply to the Human in the language they used. "
    "Do not discuss wallet, price, or identity."
)


class FrontAgentRuntime:
    """一个可测试的 Front Agent（LLM 调用通过 `think` 注入）。"""

    def __init__(
        self,
        agent_id: str,
        think: Callable[[str], str],
    ) -> None:
        if not callable(think):
            raise ValueError("FrontAgentRuntime 需要可调用的 think(prompt)")
        self.agent_id = str(agent_id)
        self._think = think

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
        context: str = "",
    ) -> FrontDecision:
        """
        识别 Human 意图。

        `active_tasks` / `completed_tasks` 是 **只读引用**：
        `{"task_id", "summary", "status"}`。Front 不修改它们。

        `conversation` 是最近 Human / Front 消息的只读快照：
        `{"role", "text"}`。它只用于判断「再做一个」这类上下文，
        不是任务需求。
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

        last_error = ""
        for attempt in range(1, 3):
            prompt = self._prompt(
                human_text, active, completed, conversation=history,
                context=context, previous_error=last_error)
            try:
                raw = self._think(prompt)
            except Exception as exc:                          # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            decision, error = parse_front_decision(
                raw,
                human_text=human_text,
                active_task_ids=active_ids,
                completed_task_ids=completed_ids,
            )
            if decision is not None:
                return decision
            last_error = error

        return FrontDecision(
            intent=FrontIntent.CLARIFY,
            question=(
                "我需要再确认一下：你是想让我直接回答，"
                "还是要开始 / 继续一项工作？"),
            confidence=0.0,
            reason=f"front_parse_failed:{last_error}",
        )

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
        context: str = "",
        previous_error: str = "",
    ) -> str:
        error_text = ""
        if previous_error:
            error_text = (
                "\n上一轮输出不合法：" + previous_error +
                "。请只输出一个合法 JSON 对象。\n")

        return f"""You are {self.agent_id}, the only entry point between the Human and the Agent Economy.
You are chatting with the Human: every reply/question must sound natural and friendly.
Reply to the Human in the language they used. Internal reasoning may be English.
Do not expose Brief, Creator, Task, acceptance chain, routing, or scope unless the
Human explicitly asks how the system works.

========== RECENT CONVERSATION (READ-ONLY) ==========
{_render_conversation(conversation)}

========== CURRENT HUMAN INPUT ==========
{human_text}

========== ACTIVE TASKS (READ-ONLY) ==========
{_render_tasks(active)}

========== COMPLETED TASKS (READ-ONLY) ==========
{_render_tasks(completed)}

========== OTHER CONTEXT ==========
{context or "(none)"}
{error_text}
========== RESPONSIBILITY AND BOUNDARIES ==========
You are the Human's entry point, not the task planner, producer, or acceptor.
Use the current input, recent conversation, task list, and task state to decide
what should happen next, and choose one Decision within your boundaries.

Boundaries:
- Never create a Task. On handoff, you decide whether task_text is the original
  Human text or a faithful restatement.
- Preserve the Human's intended task: do not add, remove, or weaken key
  requirements. When unsure, keep the original text or clarify.
- You may decide the reply_style (format), but not the technical solution.
- Do not replace the Work Agent in design, decomposition, or acceptance.
- If information is insufficient and the choice matters, use clarify.

Consider:
- Is this a new work request, or talk about / modification / continuation of
  existing work?
- Does the recent conversation change what this input means?
- The Task list is known facts, not a matching table; similar wording does not
  automatically mean the same intent.
- If the Human wants a new deliverable instance, handoff may be right even when
  it resembles an earlier Task.
- If the Human wants changes to an existing delivery, consider rework; if they
  only ask for status/result, consider direct_reply / follow_up; if they ask to
  see artifacts, consider artifacts.
- If there is no unique correct answer, choose the path that best matches the
  Human's current intent and reasonable cost.

status INTERRUPTED means the Task was interrupted and is not running now:
do not say "still processing"; when the Human wants to continue, use follow_up
and the system will resume it.

Available intents:

1. direct_reply
   No work is needed; answer directly.
   {{"intent":"direct_reply","reply":"...","reason":"..."}}

2. handoff
   This is a new work request. You decide how to hand it off:
   - if the original text is already clear, use it verbatim as task_text;
   - if you judge a compression/restatement is needed, write a task_text that
     preserves the original intent.
   An optional brief may carry goal / deliverables / constraints / acceptance.

   {{"intent":"handoff",
     "task_text":"text handed to the Work Agent",
     "brief":{{"goal":"...","deliverables":["..."],
              "constraints":["..."],"acceptance_expectations":["..."]}},
     "reply":"short confirmation for the Human",
     "reply_style":"final reply format, e.g. friendly, concise, conclusion first",
     "reason":"..."}}

   Critical: preserve what the Human wants to accomplish. Do not add, remove,
   or weaken key requirements. If unsure, keep the original text or clarify.
   reply_style only describes how to reply to the Human, not the task.

3. rework
   The Human wants changes to a COMPLETED Task. Reference its task_id.
   {{"intent":"rework","task_id":"...","reply":"short confirmation",
     "reason":"..."}}
   The reply is for the Human, e.g. "Understood, I will have the original
   Agent update it." Do not write internal instructions.

4. follow_up
   The Human follows up on an ACTIVE / INTERRUPTED Task.
   {{"intent":"follow_up","task_id":"...","reply":"natural status update",
     "reason":"..."}}
   Example: "This is already in progress; I will tell you when it is done."
   or "This task was interrupted; I am continuing it now."

5. cancel
   The Human explicitly asks to stop / cancel an ACTIVE Task.
   {{"intent":"cancel","task_id":"...","reply":"confirmation for the Human",
     "reason":"..."}}
   Only reference an active task_id. Never claim "already stopped" in a
   direct_reply: stopping must go through this intent.

6. artifacts
   The Human asks to see a Task's artifacts / code / files / result.
   {{"intent":"artifacts","task_id":"...","reply":"short confirmation",
     "reason":"..."}}
   Only reference a task_id shown above. If "the previous one" is ambiguous,
   choose the best match from the task lists; if still unclear, clarify.

7. clarify
   The intent is unclear; ask the Human.
   {{"intent":"clarify","question":"...","reason":"..."}}

No wallet / price / identity discussion; those are not Front decisions.
The Human has no "accept task" action. Completed Tasks are only reopened through
later Human input; do not emit an accept intent.

Return JSON only, no markdown fences.
"""


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
