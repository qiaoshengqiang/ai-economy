"""
Action Seam —— 一次 Cognitive Run 的完整通道
============================================================

    Stimulus → Cognitive Context → LLM → Agent Decision
             → ActionIntent → Runtime → Executor → ActionResult
             →（读取类动作）→ 结果当场回喂 → LLM → 再决定一次
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

【🔴 2026-10-10：读取结果**当场**回喂（用户决定「形状 2」）】

    实测缺口（运行 1010-1009，真实数据）：

        `workspace_read` 的 `ActionResult.output.content` 里
        完整存着 2521 字符的正文，而 Agent **下一轮看到的只有**
        `action=workspace_read … chars=2521` —— **正文一个字都没有**。
        ⇒ 「读」这件事在结构上成功了，语义上是**空转**。

    ✅ 现在：Agent 在一次认知里发起读取之后，环境把**真实返回**
       作为下一条 user 消息回喂，它**在同一次认知里接着决定做什么**。

    🔒 **最多一次**（硬上限 2 次 LLM 调用）——
       不做循环。理由：每次调用都要花钱（AIC），一个没有上限的
       tool-loop 会把成本变成不可预测的东西。

    🔒 **不是 tool calling** —— 只是普通消息。
       `actions/__init__.py` 明列「❌ DSH Tool Bridge / Tool Registry」，
       走原生 `tools` 会撞那条边界。

    🔒 **默认关闭**（`follow_up_on_reads=False`）：
       它会让「读」的那一轮**多一次完整 LLM 调用**，
       这种成本必须由**调用方**明说要不要付。
       实验侧（`production._turn`）显式打开。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from actions.executor import ActionDispatcher
from actions.intent import (
    ActionIntent, ActionResult, CognitiveRunResult, CognitiveSegment,
    Decision, new_decision_id,
)
from actions.parse import decision_from_text
from actions.read_payload import (
    PER_PAYLOAD_MAX_CHARS, TOTAL_MAX_CHARS, render_read_results,
)
from core.logging_setup import get_logger

logger = get_logger(__name__)


def run_cognition(
    agent: Any,
    messages: Any = None,
    *,
    dispatcher: Optional[ActionDispatcher] = None,
    execute: bool = True,
    write_service: Optional[Any] = None,
    follow_up_on_reads: bool = False,
    follow_up_per_payload_max: int = PER_PAYLOAD_MAX_CHARS,
    follow_up_total_max: int = TOTAL_MAX_CHARS,
    image_resolver: Optional[Callable[[str, str], Optional[str]]] = None,
    repair_malformed: bool = False,
    work_loop: bool = False,
    max_work_steps: int = 2,
    initial_phase: str = "decision",
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
        follow_up_on_reads
                      🔴 True 时：Agent 这一轮发起的**读取类**动作
                      （`workspace_read` / `shared_context_read` / …）
                      如果真的读到了东西，就把**真实返回**作为下一条
                      user 消息回喂，让它**在同一次认知里**接着决定。

                      🔒 **最多一次**（2 次 LLM 调用封顶）。
                      🔒 默认 False —— 它多花一次调用，由调用方决定。
        follow_up_per_payload_max / follow_up_total_max
                      回喂内容的**字符上限**。默认沿用 `actions/read_payload`
                      的 8,000 / 12,000；实验可以调小，避免把单轮上下文
                      推到推理模型的长 reasoning 区间。

                      🔒 截断仍然会**如实写明**完整长度，不静默少给。
        image_resolver
                      🔴 可选：`(run_id, relative_path) -> data URL`。
                      传了才会把 `run_tool` 返回里的截图作为
                      `image_url` content part 附在 follow-up user 消息里。
                      `actions/` 不读文件 —— 文件解析由调用方注入。
        repair_malformed
                      🔴 True 时：模型输出看起来像结构化信封、但 JSON
                      解析失败（截断/写坏）⇒ **重试一次**，要求它输出
                      一个更短、只有一个动作的信封。

                      🔒 只修**格式**，不改 Agent 的决定。
                      🔒 每段**最多一次**，默认 False（会多花一次调用）。
        work_loop     🔴 True 时启用**最小工作循环**：
                      decision → work（最多 N 步，phase=work）
                      → decision checkpoint（phase=decision）。
                      工作阶段看到的是真实读取/工具返回；没有可回喂载荷
                      时不会空转。默认 False（向后兼容）。
        max_work_steps
                      工作阶段最多几次 LLM 调用（硬上限，防止失控 loop）。
        initial_phase 第一段（seg0）的 phase：默认 `decision`；
                      接收人类命令的首次理解可以传 `intake`。

    返回：CognitiveRunResult（含 decision / intent / result / segments）
    """
    agent_id = _agent_id_of(agent)

    # ---- 第 1 段 ------------------------------------------------------
    seg0 = _run_one_segment(agent, agent_id, messages, generate_kwargs,
                            phase=initial_phase,
                            repair_malformed=repair_malformed)
    segments: List[CognitiveSegment] = [seg0]

    # 🔴 Agent 对**下一步**思考模式的决定：立刻应用到同一个 Agent 的 Brain。
    #    这样紧接着的读取回喂（seg1）就会用新的思考模式；
    #    不写 `thinking` 就保持当前状态。
    _apply_thinking(agent, seg0.decision, current_phase=initial_phase)

    # ---- 执行第 1 段的动作 --------------------------------------------
    intents: List[ActionIntent] = list(seg0.action_intents)
    results: List[Optional[ActionResult]] = _dispatch_all(
        agent_id, seg0.action_intents, dispatcher=dispatcher,
        execute=execute, truncated=seg0.is_truncated)

    # ---- 可选：工作循环 / 读取回喂 ------------------------------------
    #
    # 🔒 判据是**「真的读到了载荷」**，不是「有没有发起读取」——
    #    读取失败 / 读到空文件时**不**触发 work/follow-up：
    #    那会白花一次调用，而且模型看到的还是一句「什么都没有」。
    follow_up_used = False
    base = _as_message_list(messages)

    if work_loop and base is not None:
        # decision → work（最多 N 步）→ decision checkpoint
        last_seg = seg0
        last_results = results
        last_prompt = ""
        work_steps = 0
        for _ in range(max(0, int(max_work_steps))):
            prompt = render_read_results(
                list(zip(last_seg.action_intents, last_results)),
                per_payload_max=int(follow_up_per_payload_max),
                total_max=int(follow_up_total_max),
                image_resolver=image_resolver)
            if not prompt:
                break
            work_messages = base + [
                {"role": "assistant",
                 "content": str(getattr(last_seg, "content", "") or "")},
                {"role": "user", "content": prompt},
            ]
            seg = _run_one_segment(
                agent, agent_id, work_messages, generate_kwargs,
                index=len(segments), phase="work",
                repair_malformed=repair_malformed)
            segments.append(seg)
            _apply_thinking(agent, seg.decision, current_phase="work")
            seg_results = _dispatch_all(
                agent_id, seg.action_intents, dispatcher=dispatcher,
                execute=execute, truncated=seg.is_truncated)
            intents.extend(seg.action_intents)
            results.extend(seg_results)
            last_seg, last_results, last_prompt = seg, seg_results, prompt
            work_steps += 1
            follow_up_used = True

        if work_steps:
            # 回到 decision：让 Agent 决定继续工作还是结束。
            checkpoint_text = (
                "\n\n========== 工作阶段结束 ==========\n"
                "上面是工作阶段最后一步的真实返回。\n"
                "高优先级：检查一次私有工作记忆（抽象结构 / 分支 / "
                "重大问题 / 重大发现）。即使没有变化，也可以做一次空更新。\n"
                "如果计划 / 分工 / 完成清单 / 要共享的抽象信息有变化，"
                "可以用 public_working_memory_update 更新公共工作记忆"
                "（expected_revision 用你看到的 revision）。\n"
                "现在回到决策：继续工作、结束、或交付。")
            # 🔒 `last_prompt` 可能是字符串，也可能是带图片的 content parts。
            #    后者不能直接 `list + str`；把 checkpoint 作为新的 text part 追加。
            if isinstance(last_prompt, list):
                checkpoint_prompt = [
                    *last_prompt,
                    {"type": "text", "text": checkpoint_text},
                ]
            else:
                checkpoint_prompt = str(last_prompt) + checkpoint_text
            checkpoint_messages = base + [
                {"role": "assistant",
                 "content": str(getattr(last_seg, "content", "") or "")},
                {"role": "user", "content": checkpoint_prompt},
            ]
            seg = _run_one_segment(
                agent, agent_id, checkpoint_messages, generate_kwargs,
                index=len(segments), phase="decision",
                repair_malformed=repair_malformed)
            segments.append(seg)
            _apply_thinking(agent, seg.decision, current_phase="decision")
            intents.extend(seg.action_intents)
            results.extend(_dispatch_all(
                agent_id, seg.action_intents, dispatcher=dispatcher,
                execute=execute, truncated=seg.is_truncated))

    elif follow_up_on_reads:
        prompt2 = render_read_results(
            list(zip(seg0.action_intents, results)),
            per_payload_max=int(follow_up_per_payload_max),
            total_max=int(follow_up_total_max),
            image_resolver=image_resolver)
        if prompt2 and base is not None:
            follow_up_used = True
            follow_up_messages = base + [
                # 🔒 把模型**自己那段原文**放回去 —— 否则它不知道自己
                #    上一句说了什么，就会把这次读取当成一个新任务的开头。
                {"role": "assistant",
                 "content": str(getattr(seg0, "content", "") or "")},
                {"role": "user", "content": prompt2},
            ]
            seg1 = _run_one_segment(agent, agent_id, follow_up_messages,
                                   generate_kwargs, index=1, phase="work",
                                   repair_malformed=repair_malformed)
            segments.append(seg1)
            _apply_thinking(agent, seg1.decision, current_phase="work")
            intents.extend(seg1.action_intents)
            results.extend(_dispatch_all(
                agent_id, seg1.action_intents, dispatcher=dispatcher,
                execute=execute, truncated=seg1.is_truncated))
        elif prompt2:
            logger.warning(
                "有几个读取结果可以回喂，但 messages 的形状认不出来"
                "（%s）—— 跳过 follow-up，不让它冒把原始 prompt 弄坏的风险。",
                type(messages).__name__)

    # ---- Reflection（§十：**可选**，0..N 个）---------------------------
    # 🔒 从**所有段**收 —— 第二段里提的反思与第一段一样真实。
    proposals: list = []
    for seg in segments:
        proposals.extend(_reflections_of(agent_id, seg.content))
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
        # 🔒 `decision` 保持**第一段**的语义不变（向后兼容）——
        #    它是「Agent 看到 prompt 时决定做什么」。
        #    最后那一版在 `segments[-1].decision`。
        decision=seg0.decision,
        action_intents=intents,
        action_results=results,
        reflection_proposals=proposals,
        write_outcomes=outcomes,
        # §七：把 Provider 输出契约**带出** Cognition，不留在里面被吃掉。
        # 🔒 这三个保持**第一段**的值（向后兼容）；
        #    每一段自己的状态在 `segments[i]` 里。
        output_status=seg0.output_status,
        cognitive_status=seg0.cognitive_status,
        finish_reason=seg0.finish_reason,
        segments=segments,
        follow_up_used=follow_up_used,
        thinking=_last_thinking(segments),
    )


# ================================================================
# 内部
# ================================================================
#: 信封写坏后的**一次**修复提示。
#:
#: 🔒 它只说格式，不说「你应该做什么」——
#:    决定仍然是 Agent 的，我们只是要求它**把决定写成可执行的样子**。
REPAIR_INSTRUCTION = (
    "你的上一条输出没有形成可执行的结构化信封（JSON 被截断或格式不合法），"
    "因此没有任何动作被执行。\n"
    "请**直接重发**一个更短的信封，最多一个动作：\n"
    '{"decision":"act","reasoning":"<一句话>",'
    '"action":{"type":"<动作类型>","parameters":{...}}}\n'
    "不要把长文档、长报告或嵌套很深的 payload 塞进 JSON —— "
    "先发最小合法参数，后续再补。"
)


def _run_one_segment(agent: Any, agent_id: str, messages: Any,
                     generate_kwargs: dict, *, index: int = 0,
                     phase: str = "decision",
                     repair_malformed: bool = False,
                     ) -> CognitiveSegment:
    """
    **一次** LLM 调用 + 解析。不含执行动作（执行由调用方做）。

    🔒 段与段之间**唯一**的差别就是这个 `index` ——
       第二段走的是**完全同一条**解析路径（同一个信封 parser）。
       所以「第二段能不能执行动作」不需要另一套规则。

    🔴 `repair_malformed=True` 时：信封解析失败会**重试一次**，
       要求模型输出更短的、只有一个动作的信封。
       只修格式，不替 Agent 决定做什么。
    """
    call_kwargs = dict(generate_kwargs)
    call_kwargs.pop("phase", None)
    response = agent.generate(messages=messages, phase=phase, **call_kwargs)
    content = getattr(response, "content", None)
    total_tokens = getattr(response, "total_tokens", None)

    # 🔒 只认显式信封。没有信封 → 普通回复，不产生 Action。
    decision = decision_from_text(agent_id, content)

    # 🔴 信封坏了 ≠ Agent 决定只回复。
    #    必须显式报警，否则「模型想行动但输出不可用」会被
    #    报告成「Agent 没有行动」，进而误判 Agent 的意愿。
    envelope_error = getattr(decision, "envelope_error", "")
    repair_attempted = False
    repair_succeeded = False
    if envelope_error:
        logger.warning(
            "Cognition 信封解析失败（%s）：agent=%s seg=%d "
            "dropped=%d finish_reason=%s output_status=%s。"
            "模型输出**看起来像结构化信封**，但没有可用动作 —— "
            "这不是「Agent 决定只回复」。",
            envelope_error, agent_id, index,
            getattr(decision, "dropped_actions", 0),
            getattr(response, "finish_reason", None),
            getattr(response, "output_status", None),
        )

        # ---- 可选：一次格式修复 ------------------------------------
        base = _as_message_list(messages) if repair_malformed else None
        if base is not None:
            repair_attempted = True
            repair_messages = base + [
                {"role": "assistant", "content": str(content or "")},
                {"role": "user", "content": REPAIR_INSTRUCTION},
            ]
            repaired = agent.generate(messages=repair_messages,
                                      phase=phase,
                                      **call_kwargs)
            repaired_content = getattr(repaired, "content", None)
            repaired_decision = decision_from_text(agent_id,
                                                   repaired_content)
            repaired_tokens = getattr(repaired, "total_tokens", None)
            if total_tokens is None and repaired_tokens is None:
                total_tokens = None
            else:
                total_tokens = (total_tokens or 0) + (repaired_tokens or 0)

            content = repaired_content
            decision = repaired_decision
            # 🔒 状态以**最后一次**调用为准：第一次已经确定不可用。
            response = repaired
            repair_succeeded = (
                bool(str(repaired_content or "").strip())
                and not getattr(decision, "envelope_error", ""))
            if repair_succeeded:
                logger.info(
                    "信封修复成功：agent=%s seg=%d actions=%d",
                    agent_id, index, len(decision.intents))
            else:
                logger.warning(
                    "信封修复失败（%s）：agent=%s seg=%d "
                    "finish_reason=%s output_status=%s",
                    getattr(decision, "envelope_error", ""),
                    agent_id, index,
                    getattr(response, "finish_reason", None),
                    getattr(response, "output_status", None),
                )

    thinking_error = getattr(decision, "thinking_error", "")
    if thinking_error:
        logger.warning(
            "Cognition thinking 指令非法（%s）：agent=%s seg=%d —— "
            "已忽略该指令；同一信封里的动作不受影响。",
            thinking_error, agent_id, index)

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
    #
    #   🔒 鸭子类型取值，不 import `llm/` —— `actions/` 是叶子层。
    output_status = getattr(response, "output_status", None)
    if output_status == "EMPTY_CONTENT":
        logger.warning(
            "Cognition 拿到 EMPTY_CONTENT：agent=%s seg=%d —— 这次调用"
            "没有可用的生产内容。它**不是**「Agent 决定不作答」，"
            "两者必须分开统计。",
            agent_id, index,
        )

    # --- 认知输出语义状态（截断检测）----------------------------------
    #
    #   🔴 **「模型返回了内容」不等于「模型完成了认知」。**
    #   实测（Run 2）：11 turns 的 `finish_reason="length"` 被记成
    #   `VALID_CONTENT`。真实语义是**撞到 token 上限被截断**。
    cognitive_status = getattr(response, "cognitive_status", None)
    finish_reason = getattr(response, "finish_reason", None)

    if cognitive_status == "TRUNCATED":
        logger.warning(
            "Cognition 输出被**截断**：agent=%s seg=%d finish_reason=%s "
            "output_status=%s。模型输出达到 token 上限，本轮认知不完整 —— "
            "即使解析出了合法 Action，也**不执行**。"
            "这不是 provider 失败，provider 调用本身是成功的。",
            agent_id, index, finish_reason, output_status,
        )

    return CognitiveSegment(
        index=index,
        phase=phase,
        temperature=getattr(getattr(agent, "brain", None),
                            "last_temperature", None),
        content=str(content or ""),
        decision=decision,
        action_intents=list(decision.intents),
        output_status=output_status,
        cognitive_status=cognitive_status,
        finish_reason=finish_reason,
        # 🔴 逐段记 tokens —— `last_response` 只保留最后一次调用，
        #    只看它会把 follow-up 那轮的成本算成**只有第二段**。
        #    修复调用发生时，这里记**两次调用之和**。
        tokens=total_tokens,
        repair_attempted=repair_attempted,
        repair_succeeded=repair_succeeded,
    )


def _dispatch_all(agent_id: str, intents: List[ActionIntent], *,
                  dispatcher: Optional[ActionDispatcher], execute: bool,
                  truncated: bool) -> List[Optional[ActionResult]]:
    """
    执行一段里的**全部**动作。

    🔴 **0..N 个动作**，按 Agent 声明的顺序执行。

        🔒 每个动作**独立**执行、独立留结果 —— 一个失败**不阻止**后面的。
           理由：它们都是 Agent **自己声明的**决定，我们不替它取舍
           （「第 1 个失败了就别发第 2 个消息」是**我们的判断**，不是它的）。
           失败会如实进 Experience，它自己能看到。

    🔴 **TRUNCATED 时默认不执行** —— `finish_reason=length` 说明模型
       **还没输出完**，截断点之后可能还有更完整的参数。
       「JSON 恰好能解析」**不能**证明「这一轮认知已经完成」。

       🔒 但**不发明新的「失败行动」**：动作照旧**保留**在结果里
          （那是真实发生的事实），只是结果为 `None`。
    """
    if not intents:
        return []
    if truncated:
        logger.warning(
            "跳过全部 Action 执行（认知输出被截断）：agent=%s %d 个动作 —— "
            "「JSON 能解析」不等于「这一轮认知已经完成」。",
            agent_id, len(intents),
        )
        return [None] * len(intents)
    if not execute or dispatcher is None:
        # 有意图但**没有执行**（execute=False / 没 dispatcher）——
        # 意图照样保留（那是真实发生的事实），结果如实留空。
        return [None] * len(intents)
    return [dispatcher.dispatch(it) for it in intents]


def _apply_thinking(agent: Any, decision: Any, *,
                    current_phase: str = "decision") -> None:
    """
    把 Decision 里的 `thinking` 指令应用到 Agent 的 Brain。

    🔒 鸭子类型：不 import `agents/`；没有 `brain.set_thinking` 的
       调用方（测试 / 旧实现）静默跳过。
    🔒 支持 `{"*": ...}`（覆盖当前相）与分相 map。
    🔒 只改策略，不改本次已经生成的内容。
    """
    directive = getattr(decision, "thinking", None)
    if not isinstance(directive, dict):
        return
    brain = getattr(agent, "brain", None)
    setter = getattr(brain, "set_thinking", None)
    if not callable(setter):
        return
    for key, value in directive.items():
        if not isinstance(value, dict):
            continue
        phase = current_phase if key == "*" else str(key)
        enabled = bool(value.get("enabled"))
        effort = value.get("effort")
        setter(enabled, effort, phase=phase)
        logger.info(
            "思考模式更新：agent=%s phase=%s enabled=%s effort=%s（下一步生效）",
            getattr(agent, "agent_id", "?"), phase, enabled, effort or "-")


def _last_thinking(segments: List[CognitiveSegment]) -> Optional[dict]:
    """最后一个**明确表态**的 thinking 指令（None = 全程没表态）。"""
    directive = None
    for seg in segments:
        value = getattr(getattr(seg, "decision", None), "thinking", None)
        if isinstance(value, dict):
            directive = value
    return dict(directive) if directive else None


def _as_message_list(messages: Any) -> Optional[List[Any]]:
    """
    把调用方给的 `messages` 变成**可以往后追加**的列表。

    认不出来的形状返回 `None` —— 调用方据此**跳过** follow-up，
    而不是冒着把原始 prompt 弄坏的风险去猜。

    🔒 不 import `llm/` —— `actions/` 是叶子层。
       `normalize_messages` 住在 `llm/base.py`，所以这里只做**最小**适配；
       真正的归一化仍然由 Brain 那侧做（它本来就会做）。
    """
    if messages is None:
        return []
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if isinstance(messages, (list, tuple)):
        return list(messages)
    return None


def _reflections_of(agent_id: str, content: Any) -> list:
    """
    §十：**0..N** 个 Proposal。没有就是空列表 —— **绝不自动补**。

    🔒 从**这一段的信封原文**解析，不从 `Decision` 取 ——
       `Decision` 里**没有** reflection 字段（实测确认），
       第一版改成从 `Decision` 取，会让 reflections **静默失效**。
       原文保存在 `segments[i].content`，所以多段也拿得到各自的 reflection。
    """
    from actions.parse import extract_envelope
    from reflection.parse import parse_reflections

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
