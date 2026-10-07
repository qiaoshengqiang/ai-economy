"""
Action Seam —— 一次 Cognitive Run 的完整通道
============================================================

    Stimulus → Cognitive Context → LLM → Agent Decision
             → ActionIntent → Runtime → Executor → ActionResult

【§十六：ActionResult 必须能被上层看见】

    本模块返回 `CognitiveRunResult`，里面**同时**有
    decision / action_intent / action_result。
    **不允许** Executor 执行完之后 result 被 Runtime 吃掉。

【§十八：向后兼容】

    普通 chat（LLM 只回文本，没有结构信封）：
        decision.kind == "reply"，intent == None，**不调用 Executor**，
        返回值里的 content 就是原文。
    → 既有的「只回复」路径完全不受影响。

【§二 / §五：Decision Owner = Agent】

    本模块**不决定**要不要行动。
    它只做三件事：
        ① 问 LLM（Agent 的认知过程）
        ② 把 LLM 的**结构化输出**翻译成 Decision（不猜，见 parse.py）
        ③ 如果 Agent 决定了 Action，就交给 Dispatcher 执行
    「要不要行动、行动什么」永远来自 Agent 的决定。
"""

from __future__ import annotations

from typing import Any, Optional

from actions.executor import ActionDispatcher
from actions.intent import (
    ActionIntent, ActionResult, CognitiveRunResult, Decision,
    new_decision_id,
)
from actions.parse import decision_from_text


def run_cognition(
    agent: Any,
    messages: Any = None,
    *,
    dispatcher: Optional[ActionDispatcher] = None,
    execute: bool = True,
    **generate_kwargs: Any,
) -> CognitiveRunResult:
    """
    跑一次 Agent Cognition，并（在 Agent 决定时）执行它的 Action。

    参数：
        agent         任何有 `.generate(messages, ...)` 的对象
                      （通常是 BaseAgent；有 `.agent_id` 更好）
        dispatcher    执行器路由。**不传就只产生 Intent 不执行** ——
                      这样调用方可以只观察 Agent 想做什么。
        execute       False 表示「只形成 Decision，不执行」

    返回：CognitiveRunResult（含 decision / intent / result）
    """
    agent_id = _agent_id_of(agent)

    response = agent.generate(messages=messages, **generate_kwargs)
    content = getattr(response, "content", None)

    # 🔒 只认显式信封。没有信封 → 普通回复，不产生 Action。
    decision = decision_from_text(agent_id, content)

    if not (execute and decision.is_action and dispatcher is not None):
        return CognitiveRunResult(
            decision=decision,
            action_intent=decision.intent,
            action_result=None,
        )

    result: ActionResult = dispatcher.dispatch(decision.intent)
    return CognitiveRunResult(
        decision=decision,
        action_intent=decision.intent,
        action_result=result,
    )


def reply_decision(agent_id: str, text: str) -> Decision:
    """
    构造一个「只回复」的 Decision —— 给不走 LLM 的调用方用。

    🔒 它**不**产生 ActionIntent。存在的意义是让
       「没有 Action」也走同一条数据结构，而不是特例。
    """
    return Decision(decision_id=new_decision_id(), agent_id=agent_id,
                    kind="reply", reasoning=text or "")


def _agent_id_of(agent: Any) -> str:
    for attr in ("agent_id", "id", "name"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""
