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

    # --- 认知输出语义状态（截断检测）----------------------------------
    #
    #   🔴 **「模型返回了内容」不等于「模型完成了认知」。**
    #
    #   实测（Run 2）：11 turns 的 `finish_reason="length"` 被记成
    #   `VALID_CONTENT`，看起来是一次成功认知。真实语义是：
    #   **模型输出撞到 token 上限被截断，本轮认知不完整。**
    #
    #   🔒 鸭子类型取值，不 import `llm/` —— `actions/` 是叶子层。
    #      取不到就是 None（「契约未上报」），**不假装 COMPLETE**。
    #
    #   🔒 它**不是** Provider Error：
    #      provider 正常完成了 API 请求，只是模型输出到了上限。
    #      所以这里既不抛异常、也不记成失败 —— 只是如实标注。
    cognitive_status = getattr(response, "cognitive_status", None)
    finish_reason = getattr(response, "finish_reason", None)
    truncated = (cognitive_status == "TRUNCATED")

    if truncated:
        logger.warning(
            "Cognition 输出被**截断**：agent=%s finish_reason=%s "
            "output_status=%s。模型输出达到 token 上限，本轮认知不完整 —— "
            "即使解析出了合法 Action，也**不执行**（完整认知可能还在后面）。"
            "这不是 provider 失败，provider 调用本身是成功的。",
            agent_id, finish_reason, output_status,
        )

    # --- Action（可选）------------------------------------------------
    #
    #   🔴 **TRUNCATED 时默认不执行 Action。**
    #
    #      理由：`finish_reason=length` 说明模型**还没输出完**。
    #      截断点之后可能还有内容 —— 比如一个本该跟着的
    #      `"reflections"` 段，或者一个更完整的 action 参数。
    #      「JSON 恰好能解析」**不能**证明「模型已经完成了这一轮认知」。
    #
    #   🔒 但**不发明新的「失败行动」**（§6）：
    #      ActionIntent 仍然**保留**在结果里（那是真实发生的事实），
    #      只是 `action_result` 为 None。
    #      没有 ActionFailure / RETRY / ABORT / RECOVER。
    # 🔴 2026-10-10：**0..N 个动作**，按 Agent 声明的顺序执行。
    #
    #    🔒 每个动作**独立**执行、独立留结果 —— 一个失败**不阻止**后面的。
    #       理由：它们都是 Agent **自己声明的**决定，我们不替它取舍
    #       （「第 1 个失败了就别发第 2 个消息」是**我们的判断**，不是它的）。
    #       失败会如实进 Experience，它自己能看到。
    intents = list(decision.intents)
    results: List[Optional[ActionResult]] = []
    if intents and execute and dispatcher is not None:
        if truncated:
            logger.warning(
                "跳过全部 Action 执行（认知输出被截断）：agent=%s %d 个动作 —— "
                "「JSON 能解析」不等于「这一轮认知已经完成」。",
                agent_id, len(intents),
            )
            results = [None] * len(intents)
        else:
            for it in intents:
                results.append(dispatcher.dispatch(it))
    elif intents:
        # 有意图但**没有执行**（execute=False / 没 dispatcher）——
        # 意图照样保留（那是真实发生的事实），结果如实留空。
        results = [None] * len(intents)


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
        action_intents=intents,
        action_results=results,
        reflection_proposals=proposals,
        write_outcomes=outcomes,
        # §七：把 Provider 输出契约**带出** Cognition，不留在里面被吃掉
        output_status=output_status,
        # 🔴 认知输出语义状态也必须带出去 —— 否则「被截断」这个事实
        #    会在 Cognition 内部被吃掉，下一轮 Agent 看不到。
        cognitive_status=cognitive_status,
        finish_reason=finish_reason,
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
