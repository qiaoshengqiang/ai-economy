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
        context: str = "",
    ) -> FrontDecision:
        """
        识别 Human 意图。

        `active_tasks` / `completed_tasks` 是 **只读引用**：
        `{"task_id", "summary", "status"}`。Front 不修改它们。
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

        last_error = ""
        for attempt in range(1, 3):
            prompt = self._prompt(
                human_text, active, completed, context=context,
                previous_error=last_error)
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

========== Human 输入 ==========
{human_text}

========== 进行中的 Task（只读） ==========
{_render_tasks(active)}

========== 已完成的 Task（只读） ==========
{_render_tasks(completed)}

========== 其他上下文 ==========
{context or "（无）"}
{error_text}
========== 你的职责 ==========
只判断 Human 想要什么，并输出一个 Decision。你不生产、不验收、不创建 Task。

先对照上面的 Task 列表，判断这是不是同一件事（措辞可以不同，看交付物和核心要求）：
- 与进行中的 Task 实质相同 → follow_up，不要重复创建；
- 与已完成的 Task 实质相同 → direct_reply，告诉 Human 已经完成并指出产物；
  Human 明确要求修改时才用 rework；
- 只有交付物或核心要求确实不同，才 handoff。

status 为 INTERRUPTED 表示任务之前中断了、现在没有在执行：
不要回答“正在处理中”；Human 想继续时用 follow_up，系统会恢复执行。

可选 intent：

1. direct_reply
   不需要任何工作，直接回答。
   {{"intent":"direct_reply","reply":"...","reason":"..."}}

2. handoff
   这是一个新的工作请求。只产出 Brief；Task 由承接的 Agent 创建。
   {{"intent":"handoff","brief":{{"goal":"一句话目标",
     "deliverables":["..."],"constraints":["..."],
     "acceptance_expectations":["..."],"ambiguities":["..."],
     "non_goals":["..."]}},"reason":"..."}}

   🔒 Brief 只记录 Human **明确表达**的内容：
   - constraints 只写 Human 明确说过的限制（例如「只要一个文件」）；
   - 不要把常见默认、推测或“为了安全”的限制写成负向约束；
   - non_goals 只有 Human 明确排除时才写，否则保持 []；
   - 不要为了看起来完整而编造“不需要 X / 不得 Y”。

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
