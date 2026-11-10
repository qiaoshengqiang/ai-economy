"""
Minimal Action Seam —— 数据结构
============================================================

    Agent Decision → ActionIntent → Runtime → Executor → ActionResult

【🔴 四条不能违反的边界】

① **ActionIntent 是「请求」，不是「权限」（§七）**

       ActionIntent ≠ Authorization ≠ Capability

   Agent 说「我想写入 outline.md」**只表示一个请求**，
   **不表示**它已经拥有 `File.Write` 权限。

② **Decision 属于 Agent，不属于 Runtime（§五 / §二）**

   本模块**没有** DecisionEngine / CentralDecisionEngine。
   `Decision` 只是「Agent 本次 Cognition 产生的结果」的**数据表示**，
   不是替所有 Agent 做决定的服务。

   🔒 有守卫测试：Runtime 不能因为 Strategy / confidence / cost
      变化就把 Intent A 换成 Intent B（§二十）。

③ **Action 是结构化结果，不是从自然语言里猜出来的（§十四）**

   ❌ 不要 regex 猜「它是不是想写文件」。
   ✅ 只有**显式**的结构化信封（见 parse.py）才会产生 ActionIntent。
   没有信封 → 就是普通回复，**不影响既有行为**（§十八）。

④ **Executor 只做机械执行（§九）**

   它不能：选 Strategy / 决定要不要行动 / 改 Self Model /
          判断经济价值 / 判断任务优先级。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 第一版支持的 Action 类型（§十）。
#: 🔒 刻意**只有**低风险的两种 + 一个测试用的。
#:    §十一：本轮**不**实现裸 host filesystem write ——
#:    未来的 Digital Production World 必须走
#:    Action → Capability → Gateway → Workspace → Filesystem，
#:    那些层现在还不存在。
ACTION_REPLY = "reply"
ACTION_NOOP = "noop"
ACTION_OBSERVE = "observe"

#: 第一版内置类型。**测试可以注入别的**（§二十一：可替换）。
BUILTIN_ACTION_TYPES = {ACTION_REPLY, ACTION_NOOP, ACTION_OBSERVE}

#: Decision 的种类
DECISION_REPLY = "reply"      # 只回复，不发起 Action
DECISION_ACT = "act"          # 发起一个 ActionIntent
VALID_DECISION_KINDS = {DECISION_REPLY, DECISION_ACT}


def new_intent_id() -> str:
    return f"intent_{uuid.uuid4().hex[:12]}"


def new_decision_id() -> str:
    return f"decision_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ActionIntent:
    """
    **Agent 请求 Runtime 执行某个行为的结构化表达**（§六）。

    🔒 它是**请求**，不是权限 —— 见模块 docstring ①。
    """

    action_type: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    intent_id: str = ""
    agent_id: str = ""
    reason: str = ""
    decision_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.action_type or not str(self.action_type).strip():
            raise ValueError("action_type 不能为空")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id, "agent_id": self.agent_id,
            "action_type": self.action_type,
            "parameters": dict(self.parameters or {}),
            "reason": self.reason, "decision_id": self.decision_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ActionResult:
    """
    执行结果（§八）。

    §十七：**失败是 ActionResult，不是 Runtime 崩溃。**
        执行失败 → `success=False, error=...`
        但程序性错误（bug）**不**应该被无差别吞掉 ——
        区分在 Executor 的实现里。
    """

    success: bool
    output: Any = None
    error: Optional[str] = None
    action_id: str = ""
    action_type: str = ""
    duration_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id, "action_type": self.action_type,
            "success": self.success, "output": self.output,
            "error": self.error, "duration_ms": self.duration_ms,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class Decision:
    """
    **Agent 本次 Cognition 产生的决定结果**（§五）。

    🔒 这**不是**一个决策服务。它是一个**数据类**。
       本项目**没有** DecisionEngine / CentralDecisionEngine，
       将来也不应该有 —— Decision Owner 是 Agent。
    """

    decision_id: str
    agent_id: str
    kind: str
    reasoning: str = ""
    #: 🔴 一次决策可以有 **0..N** 个动作（2026-10-10）。
    #:
    #:    【为什么要多个】
    #:        API 完全支持一次输出多个动作，而我们**有这个需求** ——
    #:        例如「写完正文的**同时**通知下一个人」。
    #:        旧形状只有单数，于是 Agent 只能把它拆成两次决策，
    #:        而「下一次」需要有人唤醒它 ⇒ 拆完就断在那里。
    #:
    #:    🔒 **向后兼容**：信封里给 `action`（单数）也接受，
    #:       会被归一成单元素列表。
    intents: List[ActionIntent] = field(default_factory=list)
    #: 信封里写坏、被**跳过**的动作个数。
    #: 🔒 不静默丢弃 —— 跳过了几个，就如实记几个。
    dropped_actions: int = 0
    #: 信封解析失败的原因（空字符串 = 没有失败）。
    #:
    #: 🔴 为什么需要它：LLM 输出被截断/写坏时，`decision_from_text`
    #:    会降级成 `kind="reply"` —— 与「Agent 主动决定只回复」
    #:    **长得一模一样**。实测（5-Agent 空间站 run）：
    #:    生产者明明在输出 `{"decision":"act", ... SUBMIT ...}`，
    #:    但 JSON 在 payload 中间断掉 → 被记成 reply → 没人 SUBMIT →
    #:    整个 Task 卡在 IN_PROGRESS，而报告看起来像「Agent 不想结项」。
    #:
    #:    `unparsable_envelope` —— 看起来像信封，但 JSON 解析失败
    #:    `no_valid_actions`    —— 信封能解析，但没有一个动作可用
    envelope_error: str = ""
    #: 🔴 Agent 对思考模式的决定（可选，**按 phase**）。
    #:
    #:    形状：{"decision": {"enabled": bool, "effort": ...},
    #:           "work":     {"enabled": bool, "effort": ...}}
    #:
    #:    · `None` = 这次没有表态，保持当前状态；
    #:    · 单相 `{"enabled": ..., "effort": ...}` 会被归一成
    #:      `{"*": ...}`，表示覆盖**产生这条决策的当前相**。
    #:
    #:    🔒 这是 Agent 的决定，不是 Runtime 的强制策略；
    #:       “决策优先低思考”只应作为默认值，不能 clamp。
    thinking: Optional[Dict[str, Dict[str, Any]]] = None
    #: `thinking` 字段写了但形状不合法时的原因（空字符串 = 没写/合法）。
    thinking_error: str = ""
    #: 形状不合法时的原始 `thinking` 值（诊断用；合法/未写时为 None）。
    #: 🔒 它只用于日志定位，不参与策略。
    thinking_raw: Any = None
    #: 🔴 Agent 对**认知预算**的决定（可选，按 phase）。
    #:
    #:    形状：{"work": 32768} / {"decision": 8192, "work": 16384}
    #:    单相整数会被归一成 `{"*": 32768}`，覆盖当前相。
    #:
    #:    🔒 这是 Agent 的**偏好**，不是硬上限；Human / 系统仍可 cap，
    #:      Provider 还会做 MIN / MAX 的结构裁剪。
    cognitive_budget: Optional[Dict[str, int]] = None
    #: `cognitive_budget` 写了但形状不合法时的原因（空字符串 = 没写/合法）。
    cognitive_budget_error: str = ""
    created_at: str = ""

    @property
    def intent(self) -> Optional[ActionIntent]:
        """向后兼容：**第一个**动作（老调用方只认一个）。"""
        return self.intents[0] if self.intents else None
    created_at: str = ""

    @property
    def is_action(self) -> bool:
        return self.kind == DECISION_ACT and bool(self.intents)

    @property
    def is_reply_only(self) -> bool:
        return self.kind == DECISION_REPLY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id, "agent_id": self.agent_id,
            "kind": self.kind, "reasoning": self.reasoning,
            "intents": [i.to_dict() for i in self.intents],
            "intent": self.intent.to_dict() if self.intent else None,
            "dropped_actions": self.dropped_actions,
            "envelope_error": self.envelope_error,
            "thinking": dict(self.thinking) if self.thinking else None,
            "thinking_error": self.thinking_error,
            "cognitive_budget": (dict(self.cognitive_budget)
                                 if self.cognitive_budget else None),
            "cognitive_budget_error": self.cognitive_budget_error,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CognitiveSegment:
    """
    一次认知里的**一段** —— 一次 LLM 调用 + 它决定的动作。

    【为什么需要它】

        用户 2026-10-10 决定「形状 2」：Agent 读取环境之后，
        **读取到的内容当场回喂给模型**，它在**同一次认知里**接着决定。

        ⇒ 一次认知因此可能有 **2 段**（硬上限，见 `run_cognition`）。
          只记「最后那一段」会丢掉第一段里**它为什么去读**的判断 ——
          而观察实验要看的正是那个。

    🔒 每段各自的契约状态（`output_status` / `cognitive_status`）
       独立记录：第二段被截断 ≠ 第一段被截断。
    """

    #: 第几段（从 0 开始）。
    index: int = 0

    #: 这一段属于哪一相：`decision` / `work`。
    #: 🔒 它只用于观测与策略选择，不改变消息内容。
    phase: str = "decision"

    #: 这一段**实际**使用的 temperature（来自 Agent 的 Brain 回执）。
    #: 🔒 `None` = 调用方没有暴露，不代表“没有温度”。
    temperature: Optional[float] = None

    #: 这一段请求的认知预算 / 实际 max_tokens / 裁剪原因。
    #: 🔒 全部是 Provider 回执的事实；`None` = 没上报。
    requested_cognitive_budget: Optional[int] = None
    actual_max_tokens: Optional[int] = None
    budget_clipped: Optional[str] = None

    #: 这一段模型返回的原文。🔒 保留原文 —— 信封解析失败时它是唯一证据。
    content: str = ""

    #: Provider 返回的**隐藏推理**（DeepSeek `reasoning_content`）。
    #:
    #: 🔴 它和 `content` 是两回事：`content` 是模型给世界的输出，
    #:    这里是模型给自己的思考。UI 的 "thinking" 只应显示这一格。
    #:    为空 = provider 没返回 / 该模型不输出推理。
    model_reasoning: str = ""

    #: Provider 层恢复出来的结构化工具调用（OpenAI 形状）。
    #: 🔒 只用于观测/映射；是不是执行由 Work 循环决定。
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls_source: str = "none"

    #: 这一段解析出来的 Decision。
    decision: Optional[Decision] = None

    action_intents: List[ActionIntent] = field(default_factory=list)
    action_results: List[Optional[ActionResult]] = field(default_factory=list)

    output_status: Optional[str] = None
    cognitive_status: Optional[str] = None
    finish_reason: Optional[str] = None

    #: 🔴 这一段自己花掉的 tokens。
    #:
    #:     【为什么必须逐段记】
    #:         `BaseAgent.last_response` 只保留**最后一次**调用 ——
    #:         发生 follow-up 时，「这一轮花了多少」会被算成**只有第二段**。
    #:         而每一段都是真实的付费调用。
    #:
    #:     🔒 权威成本仍然在 `llm_calls` 表（每次调用一行）；
    #:        这个字段是给**逐轮显示**用的汇总依据。
    tokens: Optional[int] = None

    #: 这一段是否因为信封解析失败而**重试过一次**（修复调用）。
    repair_attempted: bool = False
    #: 修复调用是否成功解析出可用动作。
    repair_succeeded: bool = False
    #: 🔴 原始调用是否撞到输出上限（即使后来修复成功）。
    #:
    #:    `is_truncated` 表示**最终结果**被截断、动作不执行；
    #:    `repair_truncated` 表示原始调用截断、但修复后动作可以执行。
    #:    两者都应当计入“工作单元压力”，但语义不能混。
    repair_truncated: bool = False
    #: 原始调用（修复前）的 finish_reason / budget_clipped。
    original_finish_reason: Optional[str] = None
    original_budget_clipped: Optional[str] = None

    @property
    def is_truncated(self) -> bool:
        return self.cognitive_status == "TRUNCATED"

    @property
    def reasoning(self) -> str:
        return str(getattr(self.decision, "reasoning", "") or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "phase": self.phase,
            "temperature": self.temperature,
            "requested_cognitive_budget": self.requested_cognitive_budget,
            "actual_max_tokens": self.actual_max_tokens,
            "budget_clipped": self.budget_clipped,
            "content_chars": len(self.content or ""),
            "model_reasoning": self.model_reasoning,
            "tool_calls": list(self.tool_calls or []),
            "tool_calls_source": self.tool_calls_source,
            "decision": self.decision.to_dict() if self.decision else None,
            "action_intents": [i.to_dict() for i in self.action_intents],
            "action_results": [(r.to_dict() if r else None)
                               for r in self.action_results],
            "output_status": self.output_status,
            "cognitive_status": self.cognitive_status,
            "finish_reason": self.finish_reason,
            "is_truncated": self.is_truncated,
            "repair_attempted": self.repair_attempted,
            "repair_succeeded": self.repair_succeeded,
            "repair_truncated": self.repair_truncated,
            "original_finish_reason": self.original_finish_reason,
            "original_budget_clipped": self.original_budget_clipped,
        }


@dataclass(frozen=True)
class CognitiveRunResult:
    """
    一次 Agent Cognitive Run 的完整结果（§十六）。

    🔒 **Action Result 必须能被上层 Cognition 看见** ——
       不能让 Executor 执行完之后 result 被 Runtime 吃掉。
    """

    decision: Optional[Decision] = None
    #: 🔴 **0..N** 个动作及其结果（2026-10-10）。顺序 = Agent 声明的顺序。
    #:    🔒 `action_results[i]` 可能为 `None` —— 表示第 i 个动作**没被执行**
    #:       （例如认知被截断）。「有意图但没执行」是真实事实，不假装。
    #:
    #:    🔴 **跨段拼接**：发生 follow-up 时，这里是**两段动作按真实顺序拼起来**
    #:       的全部结果（先第一段、再第二段）。这样任何
    #:       「遍历 `zip(action_intents, action_results)`」的调用方
    #:       **不用改**就能看到全部动作。
    action_intents: List[ActionIntent] = field(default_factory=list)
    action_results: List[Optional[ActionResult]] = field(default_factory=list)
    #: §九 / §十：Agent 本次 Cognition 产生的 **0..N** 个 Reflection Proposal。
    #: 🔒 **可选** —— 空列表是合法且常见的（Agent 决定「不值得反思」）。
    reflection_proposals: List[Any] = field(default_factory=list)
    #: 每个 proposal 的受控写入结果（含被拒绝的原因）
    write_outcomes: List[Any] = field(default_factory=list)

    #: 🔴 **Provider 输出契约状态**（`VALID_CONTENT` / `EMPTY_CONTENT`）。
    #:
    #: 【为什么 Cognition 必须携带它】
    #:
    #:     没有它的时候，一次「API 成功但 content 为空」的调用在这里
    #:     会变成 `Decision(kind="reply", reasoning="")` ——
    #:     和「Agent 主动决定不作答」**长得一模一样**。
    #:     真实实验里我第一遍就是这么读的，结论是错的。
    #:
    #: 🔒 刻意做成 **None 表示「契约未上报」**，而不是本地默认成
    #:    `VALID_CONTENT`：
    #:        · `actions/` 是叶子层，不 import `llm/`（保持依赖方向干净）
    #:        · 所以这里存的是**鸭子类型**拿到的字符串原值
    #:        · 认不出来时如实说「不知道」，不假装成功
    output_status: Optional[str] = None

    #: 🔴 **认知输出语义状态**（`COMPLETE` / `TRUNCATED` / `INCOMPLETE` /
    #: `UNKNOWN`）—— 与 `output_status` **正交**。
    #:
    #:     output_status    = 语法契约（能不能解析出结构）
    #:     cognitive_status = 语义完成度（这一轮认知想完了没有）
    #:
    #: 🔒 `None` 表示「契约未上报」—— 鸭子类型取不到就如实说不知道，
    #:    不本地默认成 `COMPLETE`（那正是这个 Bug 的成因）。
    cognitive_status: Optional[str] = None

    #: provider 报的原始 `finish_reason`（诊断用，保留原值不改写）。
    finish_reason: Optional[str] = None

    #: 🔴 **一次认知里的「段」**（2026-10-10，用户决定「形状 2」）。
    #:
    #:     一段 = **一次 LLM 调用 + 它决定的动作**。
    #:
    #:     【为什么会有多段】
    #:         Agent 发起读取（`workspace_read` / `shared_context_read` / …）
    #:         时，读取到的内容**当场**作为下一条消息回喂给模型，
    #:         它会**在同一次认知里**接着决定做什么。
    #:         ⇒ 一次认知最多 **2 段**（硬上限，见 `run_cognition`）。
    #:
    #:     🔒 **旧调用方不受影响**：不开这个能力时永远只有 1 段，
    #:        而且 `decision` / `action_intents` / `action_results`
    #:        的语义**保持不变**（见下）。
    #:
    #:     🔒 `segments[0]` 的第一段永远存在（只要调用成功）。
    segments: List["CognitiveSegment"] = field(default_factory=list)

    #: 🔴 这次认知**真的做了** follow-up 吗？
    #:    它是**事实**，不是意图 —— 没触发（没有可回喂的载荷）时是 False。
    follow_up_used: bool = False

    #: 最后一段给出的 thinking 指令（phase → 设置；None = 没有表态）。
    #: 🔒 它只表达「Agent 决定了什么」；真正是否注入、注入给谁，
    #:    由 Brain 的 provider 适配层决定。
    thinking: Optional[Dict[str, Dict[str, Any]]] = None

    #: 最后一次**明确表态**的认知预算指令（phase → 偏好；None = 没表态）。
    cognitive_budget: Optional[Dict[str, int]] = None

    #: 本轮 `summary` 阶段产出的完整文字总结（没有 summary 阶段时为空）。
    summary: str = ""

    @property
    def action_intent(self) -> Optional[ActionIntent]:
        """向后兼容：**第一个**动作。"""
        return self.action_intents[0] if self.action_intents else None

    @property
    def action_result(self) -> Optional[ActionResult]:
        """向后兼容：**第一个**动作的结果。"""
        return self.action_results[0] if self.action_results else None

    @property
    def has_action(self) -> bool:
        return bool(self.action_intents)

    @property
    def is_truncated(self) -> bool:
        """
        这一轮认知输出被**截断**了吗？

        🔒 只有**明确**收到 `TRUNCATED` 才返回 True。
           `None`（契约未上报）返回 False —— 不许把「不知道」
           当成「截断了」，也不许当成「完整」。
        """
        return self.cognitive_status == "TRUNCATED"

    @property
    def is_cognitive_complete(self) -> bool:
        """这一轮认知**明确**被 provider 报为正常结束。"""
        return self.cognitive_status == "COMPLETE"

    @property
    def is_empty_output(self) -> bool:
        """
        这次 Cognition 有没有拿到可用的生产内容？

        🔒 只有**明确**收到 `EMPTY_CONTENT` 才返回 True。
           `None`（契约未上报）返回 False —— 本属性不许把「不知道」
           当成「空产出」，也不许把它当成「有产出」。
        """
        return self.output_status == "EMPTY_CONTENT"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.to_dict() if self.decision else None,
            "action_intents": [i.to_dict() for i in self.action_intents],
            "action_results": [(r.to_dict() if r else None)
                               for r in self.action_results],
            "action_intent": (self.action_intent.to_dict()
                              if self.action_intent else None),
            "action_result": (self.action_result.to_dict()
                              if self.action_result else None),
            "reflection_count": len(self.reflection_proposals),
            "reflections_committed": sum(
                1 for o in self.write_outcomes
                if getattr(o, "committed", False)),
            "output_status": self.output_status,
            "cognitive_status": self.cognitive_status,
            "finish_reason": self.finish_reason,
            "is_truncated": self.is_truncated,
            # 🔴 段：一次认知可能有 2 段（读到了 → 当场接着决定）。
            "segments": [s.to_dict() for s in self.segments],
            "segment_count": len(self.segments),
            "follow_up_used": self.follow_up_used,
            "thinking": dict(self.thinking) if self.thinking else None,
            "cognitive_budget": (dict(self.cognitive_budget)
                                 if self.cognitive_budget else None),
            "summary": self.summary,
        }
