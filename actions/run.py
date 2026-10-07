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
from core.logging_setup import get_logger

logger = get_logger(__name__)


def run_cognition(
    agent: Any,
    messages: Any = None,
    *,
    dispatcher: Optional[ActionDispatcher] = None,
    execute: bool = True,
    write_service: Optional[Any] = None,
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
        write_service SelfModelWriteService。传了才会把 Agent 提出的
                      Reflection Proposal 走**受控写入**。
                      🔒 不传 = 只观察 Agent 想改什么，不落库。

    返回：CognitiveRunResult（含 decision / intent / result）
    """
    agent_id = _agent_id_of(agent)

    response = agent.generate(messages=messages, **generate_kwargs)
    content = getattr(response, "content", None)

    # 🔒 只认显式信封。没有信封 → 普通回复，不产生 Action。
    decision = decision_from_text(agent_id, content)

    # ============================================================
    # §七：EMPTY_CONTENT **不许悄悄成功**
    # ============================================================
    #
    #   一次「API 成功但 content 为空」的调用，到这里会变成
    #   `Decision(kind="reply", reasoning="")` ——
    #   与「Agent 主动决定不作答」**长得一模一样**。
    #
    #   实测（commit 6322092）：`novel_world_builder` 连续 4 轮如此，
    #   而日志里看起来只是「这个 Agent 没动手」。那是错的。
    #
    #   ✅ 修法**不是**新增一个 Runtime / Stage 状态（§七 明确禁止扩张），
    #      而是让这次 Cognition 的**结果**如实带上契约状态。
    #      调用方据此可以说出「这次没拿到内容」，
    #      而不是猜「Agent 是不是不想干」。
    #
    #   🔒 鸭子类型取值，不 import `llm/` —— `actions/` 是叶子层。
    #      取不到就是 None（「契约未上报」），**不假装成功**。
    output_status = getattr(response, "output_status", None)
    if output_status == "EMPTY_CONTENT":
        logger.warning(
            "Cognition 拿到 EMPTY_CONTENT：agent=%s —— 这次调用"
            "没有可用的生产内容。它**不是**「Agent 决定不作答」，"
            "两者必须分开统计。",
            agent_id,
        )

    # --- Action（可选）------------------------------------------------
    result: Optional[ActionResult] = None
    if execute and decision.is_action and dispatcher is not None:
        result = dispatcher.dispatch(decision.intent)

    # --- Reflection（§十：**可选**，0..N 个）---------------------------
    proposals = _reflections_of(agent_id, response)
    outcomes = []
    if proposals and write_service is not None:
        for p in proposals:
            try:
                outcomes.append(write_service.apply(p))
            except Exception as exc:                      # noqa: BLE001
                # §十七：Reflection 写入失败**不能**破坏主要 Cognition
                logger.error("Reflection 写入异常 %s: %s", p.kind, exc)
                outcomes.append(None)

    return CognitiveRunResult(
        decision=decision,
        action_intent=decision.intent,
        action_result=result,
        reflection_proposals=proposals,
        write_outcomes=outcomes,
        # §七：把 Provider 输出契约**带出** Cognition，不留在里面被吃掉
        output_status=output_status,
    )


def _reflections_of(agent_id: str, response: Any) -> list:
    """
    §十：**0..N** 个 Proposal。没有就是空列表 —— **绝不自动补**。

    🔒 这里只解析 LLM 的输出结构；「要不要反思」由 Agent 决定。
    """
    from actions.parse import extract_envelope
    from reflection.parse import parse_reflections

    content = getattr(response, "content", None)
    envelope = extract_envelope(content)
    if envelope is None:
        return []
    return parse_reflections(agent_id, envelope)


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
