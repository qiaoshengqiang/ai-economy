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

        return f"""你是 {self.agent_id}，Human 与 Agent Economy 之间的唯一入口。
你在和 Human 聊天：所有 reply / question 都要像自然、友好的助手回复。
不要向 Human 暴露 Brief、Creator、Task、验收链、路由、范围等内部术语，
除非 Human 明确在问系统怎么工作。

========== 最近对话（只读） ==========
{_render_conversation(conversation)}

========== 当前 Human 输入 ==========
{human_text}

========== 进行中的 Task（只读） ==========
{_render_tasks(active)}

========== 已完成的 Task（只读） ==========
{_render_tasks(completed)}

========== 其他上下文 ==========
{context or "（无）"}
{error_text}
========== 你的职责与边界 ==========
你是 Human 的入口，不是任务规划者、生产者或验收者。
你要综合当前输入、最近对话、Task 列表和状态，判断 Human 这次想发生什么，
并在边界内选择一个 Decision。

你的边界：
- 不创建 Task；handoff 时由你决定 task_text 是原文还是忠实重述；
- 转交时保持 Human 想要完成的任务的原意；
- 可以决定给 Human 的回复格式（reply_style）；
- 不代替 Work Agent 做技术方案、拆分或验收；
- 信息不足且选择会影响结果时，用 clarify。

判断时综合考虑：
- 这是在提出新的工作请求，还是在谈论 / 修改 / 继续已有工作？
- 最近对话是否改变了这句话的含义？
- Task 列表是已知事实，不是匹配表；描述相似不等于同一个意图。
- 如果 Human 想要一个新的交付实例，即使与旧 Task 相似，也可以是 handoff。
- 如果 Human 想要修改已有交付，考虑 rework；只是询问状态/结果，考虑
  direct_reply / follow_up；要求查看产物，考虑 artifacts。
- 没有唯一正确答案时，选择最符合 Human 当下意图且代价合理的路径；
  拿不准就 clarify。

按你的职责和上下文自己作判断。

status 为 INTERRUPTED 表示任务之前中断了、现在没有在执行：
不要回答“正在处理中”；Human 想继续时用 follow_up，系统会恢复执行。

可选 intent：

1. direct_reply
   不需要任何工作，直接回答。
   {{"intent":"direct_reply","reply":"...","reason":"..."}}

2. handoff
   这是一个新的工作请求。你自己决定怎么转交：
   - 原文已经清楚 → `task_text` 逐字使用 Human 原文；
   - 你判断需要压缩 / 重述 → `task_text` 写一个不改变原意的版本。
   可选 `brief`：goal / deliverables / constraints / acceptance 作为结构化补充。

   {{"intent":"handoff",
     "task_text":"交给 Work Agent 的任务文本",
     "brief":{{"goal":"...","deliverables":["..."],
              "constraints":["..."],"acceptance_expectations":["..."]}},
     "reply":"给 Human 的简短确认",
     "reply_style":"最终回复格式要求，例如：友好、简洁、先结论后产物",
     "reason":"..."}}

   🔒 转交时必须保持 Human 想要完成的任务的原意：
      不新增、不删除、不弱化关键要求；不确定时保留原文或 clarify。
   🔒 reply_style 只描述**怎么回复 Human**，不是任务需求。

3. rework
   Human 要求对**已完成的 Task**返工。只能引用上面已完成的 task_id。
   {{"intent":"rework","task_id":"...","reply":"给 Human 的简短确认",
     "reason":"..."}}
   reply 是给 Human 的，例如「收到，我会让原来负责这个任务的 Agent 修改」；
   不要写「转给 Creator 的说明」这类内部指令。

4. follow_up
   Human 对**进行中 / 中断的 Task**追问、确认或要求继续。
   {{"intent":"follow_up","task_id":"...","reply":"给 Human 的自然状态说明",
     "reason":"..."}}
   reply 例如「这个需求已经在做了，完成后我会把结果告诉你」，
   或「这个任务之前中断了，我现在继续」；
   不要写「按现有 Brief 交付」「无需改动范围」这类内部话。

5. cancel
   Human 明确要求停止 / 取消一个**进行中的 Task**。
   {{"intent":"cancel","task_id":"...","reply":"给 Human 的确认","reason":"..."}}
   只能引用上面进行中的 task_id。
   🔒 不要说“已经停了”却不输出 cancel —— direct_reply 不会终止任何工作；
      停止必须走这个 intent，系统才会真的停掉执行并释放 Agent。

6. artifacts
   Human 要求查看某个 Task 的产物 / 代码 / 文件 / 结果。
   {{"intent":"artifacts","task_id":"...","reply":"给 Human 的简短确认",
     "reason":"..."}}
   只能引用上面出现过的 task_id。
   如果 Human 说的是“刚才那个”，从已完成 / 进行中的 Task 里选最匹配的；
   有多个候选且无法确定时用 clarify，不要猜。

7. clarify
   意图不明确，向 Human 追问。
   {{"intent":"clarify","question":"...","reason":"..."}}

🔒 不讨论钱包 / 价格 / 身份；这些不属于 Front 的判断。
🔒 Human 没有“接受任务”的动作。已完成的 Task 只能由 Human 后续输入
   触发 rework；不要输出 accept 一类的 intent。

只输出 JSON，不要 markdown 围栏。
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
