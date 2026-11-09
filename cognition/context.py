"""
Cognitive Context V1 —— 当前这一轮认知实际需要看到的信息集合
============================================================

【定义（§五）】

    Cognitive Context = **当前这一轮 Agent Cognition 实际需要看到的信息集合**。

    它不是长期存储，是**临时构建**出来的。
    它**不是**第二份长期数据库。

【正确 / 错误的数据流（§七）】

    ✅ Persistent State → Relevance Selection → Selected State
                        → Cognitive Context → LLM

    ❌ 所有 Persistent State → 全部拼接 → LLM
       （整个 Self Model / 整个 Strategy 库 / 全部 Memory / 全部历史消息）

【🔴 两条绝对不能违反的边界】

    ① **不能把 Self Model 伪装成 System Prompt Rules（§十三）**

        ❌ "You always prioritize character development."
        ❌ "You must use strategy X."
        ❌ "Never do Y."

        ✅ "Relevant beliefs and strategies I currently hold: …"
           → **由 Agent 自己判断是否采用**

       一旦写成 MUST / ALWAYS / NEVER，
       Strategy 就退化成 Rule，直接违背 §0.5。

       有守卫测试 `TestContextIsNotSystemRules` 扫这几个词。

    ② **LLM 不应该知道 Runtime 内部存储结构（§十一）**

        LLM 不该看到：数据库表名 · SQLite id · row id · 内部存储路径。

        ✅ Storage Representation → Cognitive Context Representation → LLM
        ❌ 不要把数据库 schema 直接当 Prompt Schema

        有守卫测试断言渲染出来的文本里**不含** claim_id / strategy_id。

【§十 为什么内部保留结构而不是一个大字符串】

    最终发给 LLM 时当然要变成文本，
    但**内部**从一开头就只有 `context = "......"` 的话，
    以后加 token budget / 压缩 / 摘要 就没有着力点了。

    所以本模块内部是 `list[ContextSource]`，
    文本渲染只是**最后一步**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from experience.models import (
    DEFAULT_MAX_CRITICAL_OBSERVATIONS,
    Experience,
)
from selfmodel.models import SelfClaim, SelfModelView, Strategy

#: 🔴 禁止出现在 Self Model 呈现文本里的**规则化措辞**（§十三）。
#:    出现任意一个，就意味着 Strategy 被伪装成了 Rule。
BANNED_RULE_WORDS = ("must", "always", "never", "you should",
                     "required to", "mandatory")

#: §十一：LLM 不该看到内部存储标识。渲染时检查这些**字段名**不出现。
BANNED_INTERNAL_TOKENS = ("claim_id", "strategy_id", "agent_id",
                          "row_id", "sqlite", "SELECT ", ".db")


@dataclass(frozen=True)
class ContextSource:
    """
    Context 里的一个**来源块**。

    §十六：保留 `source` / `content` / `priority` 的扩展空间，
    以后再加入 token budget / 压缩 / 摘要 时不用重构。
    """

    source: str                 # identity / drives / stimulus / self_claim / strategy
    content: str
    priority: float = 0.5       # 0~1，仅用于**排序**，不是门控
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CognitiveContext:
    """
    一次 Cognition 的上下文。

    🔒 它是一袋**材料**，不是一份**指令**。

        它**没有**：
            decide() / recommended_action() / best_strategy()
            / should_use() / to_instruction()

        为什么故意没有？
            一旦有，Context Builder 就在替 Agent 做 Decision ——
            §三 / §九 明确禁止（Retrieval 是选择，不是门控）。
    """

    identity: Optional[str] = None
    constitution: Optional[str] = None
    drives: Optional[str] = None
    current_stimulus: Optional[str] = None
    current_task: Optional[str] = None
    #: §二十七 / §二十八：当前 Stage 的**世界状态 / 任务状态**。
    #: 🔒 它是状态信息，**不是**「你现在必须做 X」的指令。
    #:    内容如：Stage Goal · Status · Deliverables · 当前 Checkpoint ·
    #:    Hard runtime limit · 本次 Session 已用时间。
    current_stage: Optional[str] = None
    #: 🔴 Economy Awareness V1：**当前可观察到的经济事实**。
    #:
    #: 【它与 Self Model 的关系 —— 这是本轮最重要的一条边界】
    #:
    #:     Ledger / Economy  →  经济**事实**        →  EconomicContext
    #:     Self Model        →  Agent 对自己的**信念** →  self_claims / strategies
    #:
    #:     ❌ Self Model **不许**成为余额、Ledger、Capability、
    #:        Authorization、Identity 的第二来源
    #:        （由 selfmodel/fact_domains.py + SelfModelWriteService 强制）
    #:
    #:     ✅ 但 Self Model **可以**有经济内容 —— 只要它是关于
    #:        「我自己过去怎么花钱 / 我倾向怎么做」的判断，
    #:        而不是「我的余额是 43」这种系统事实的副本。
    #:
    #: 🔒 它是**快照**：每次 build 重新从 Ledger 取值（§十），
    #:    绝不在 Agent 初始化时写死。
    economic_context: Optional[Any] = None
    self_claims: List[SelfClaim] = field(default_factory=list)
    strategies: List[Strategy] = field(default_factory=list)
    #: §七 / §八：相关历史经历。
    #: 🔒 与 Self Model **分开** —— 经历**不能**伪装成 Self Claim。
    #:    只有 Agent Cognition 产生 Reflection Proposal 之后，
    #:    才可能 Experience → Reflection → Self Model。
    #: 🔴 **关键观察通道** —— 与 `relevant_experiences` **独立**（§7）。
    #:
    #:     关键观察  → 固定进入 Context（visibility guarantee）
    #:     普通经历  → 继续走 relevance ranking（ranking preference）
    #:
    #: 🔒 两条通道**不共享池子**：关键观察有自己的槽位，
    #:    不会把普通 top-N 里的某一条挤掉，也不会被它挤掉。
    #:    绝不允许把 critical 塞进 relevance score（§8）。
    critical_observations: List[Experience] = field(default_factory=list)

    relevant_experiences: List[Experience] = field(default_factory=list)
    other: List[ContextSource] = field(default_factory=list)

    # ---- 结构保留（§十）------------------------------------------------
    def sources(self) -> List[ContextSource]:
        """把上下文摊平成**带来源标签**的块，供后续优化使用。"""
        out: List[ContextSource] = []

        def add(name: str, text: Optional[str], prio: float) -> None:
            if text:
                out.append(ContextSource(source=name, content=text,
                                         priority=prio))

        add("identity", self.identity, 1.0)
        add("constitution", self.constitution, 0.95)
        add("drives", self.drives, 0.9)
        add("stimulus", self.current_stimulus, 1.0)
        add("task", self.current_task, 0.95)
        add("stage", self.current_stage, 0.95)

        # §七 / §九：经济事实作为一个**独立来源块**。
        # 🔒 priority 高 —— 它是当前状态，不是历史回忆。
        if self.economic_context is not None:
            lines = self.economic_context.to_lines()
            if lines:
                out.append(ContextSource(
                    source="economic_context",
                    content="\n".join(lines),
                    priority=0.9,
                    metadata={"as_of": getattr(self.economic_context,
                                               "as_of", ""),
                              "source": "Ledger"},
                ))

        for c in self.self_claims:
            out.append(ContextSource(
                source="self_claim", content=c.content,
                priority=float(c.confidence or 0.5),
                metadata={"kind": c.kind, "scope": c.scope.to_dict()},
            ))
        for s in self.strategies:
            out.append(ContextSource(
                source="strategy", content=s.name,
                priority=float(s.confidence or 0.5),
                metadata={"domain": s.domain, "situation": s.situation,
                          "rationale": s.rationale},
            ))
        for e in self.critical_observations:
            out.append(ContextSource(
                source="critical_observation",
                content=_experience_block(e),
                # 比普通经历高，但**仍然只是排序** —— 真正的保证
                # 来自它有自己的通道，不来自这个分数。
                priority=0.95))
        for e in self.relevant_experiences:
            # §七：经历是**材料**，不是指令 —— priority 固定偏低，
            # 表示「可以参考」而不是「必须遵守」。
            out.append(ContextSource(
                source="experience",
                content=_experience_line(e),
                priority=0.4,
                metadata={"source": e.source,
                          "experience_id": e.experience_id,
                          "failed": e.failed},
            ))
        out.extend(self.other)
        return out

    @property
    def is_empty(self) -> bool:
        return not self.sources()

    def has_experiences(self) -> bool:
        """§七：本轮是否看到了历史经历（供 Agent 判断「值不值得反思」）。"""
        return bool(self.relevant_experiences or self.critical_observations)

    def stats(self) -> Dict[str, Any]:
        """§二十二：轻量可观测信息。**不含**上下文原文。"""
        return {
            "selected_claims": len(self.self_claims),
            "selected_strategies": len(self.strategies),
            "selected_experiences": len(self.relevant_experiences),
            "critical_observations": len(self.critical_observations),
            "failed_experiences": sum(
                1 for e in self.relevant_experiences if e.failed),
            "task_present": bool(self.current_task),
            "stimulus_present": bool(self.current_stimulus),
            "identity_present": bool(self.identity),
            "stage_present": bool(self.current_stage),
            # 🔴 Economy Awareness：本轮 Context 里**有没有**经济事实。
            #    这是「Agent 看到了经济事实」这一层的可观测证据 ——
            #    与「它理解了」/「它因此改变了行为」是两件不同的事。
            "economic_context_present": self.economic_context is not None,
            "economic_fields_unavailable": list(getattr(
                self.economic_context, "unavailable", ()) or ()),
            "total_sources": len(self.sources()),
        }

    # ---- 渲染（最后一步，§十一 / §十三）--------------------------------
    def to_text(self) -> str:
        """
        把上下文渲染成给 LLM 看的文本。

        🔴 两条纪律：
            ① 语气是「**我目前持有的相关认知**」，不是「你必须…」
            ② 不出现 claim_id / strategy_id 等内部标识
        """
        parts: List[str] = []

        head: List[str] = []
        if self.identity:
            head.append(f"I am {self.identity}.")
        if self.constitution:
            head.append(self.constitution)
        if self.drives:
            head.append(self.drives)
        if head:
            parts.append("\n".join(head))

        if self.current_stimulus:
            parts.append(f"Current input:\n{self.current_stimulus}")
        if self.current_task:
            parts.append(f"Current task:\n{self.current_task}")
        if self.current_stage:
            parts.append(f"Current stage:\n{self.current_stage}")

        # 🔴 Economy Awareness V1（§九）：
        #    经济事实作为**独立一段**渲染，语气与其它段一致 ——
        #    只陈述事实，不给指令。
        #    渲染内容由 `EconomicContext.to_lines()` 负责，
        #    本函数不参与措辞 —— 所以「不许写指令」这条纪律
        #    只需要在一个地方守住。
        if self.economic_context is not None:
            eco = self.economic_context.to_lines()
            if eco:
                parts.append("\n".join(eco))

        if self.self_claims:
            lines = ["Relevant beliefs I currently hold about myself:"]
            for c in self.self_claims:
                scope = _scope_label(c.scope.world, c.scope.domain,
                                     c.scope.situation)
                lines.append(f"- {c.content}"
                             f"\n  Confidence: {c.confidence:.2f}"
                             + (f"\n  Scope: {scope}" if scope else ""))
            parts.append("\n".join(lines))

        if self.strategies:
            lines = ["Relevant strategies I currently hold:"]
            for s in self.strategies:
                scope = _scope_label(None, s.domain, s.situation)
                # 🔴 §五：**正式展示 strategy_id**。
                #    依据是真实 API 诊断（eebf420）：模型会使用它在
                #    Context 里实际看到的 Strategy 标识 ——
                #    只给名字 → 它填名字；给 id → 它填 id。
                #
                #    🔒 它是**数据**，不是指令。
                #       绝不能渲染成 "You must use strategy_id…"。
                block = (f"- {s.name}\n"
                         f"  Strategy ID: {s.strategy_id}\n"
                         f"  Confidence: {s.confidence:.2f}")
                if scope:
                    block += f"\n  Scope: {scope}"
                if s.rationale:
                    block += f"\n  Rationale: {s.rationale}"
                lines.append(block)
            parts.append("\n".join(lines))

        if self.critical_observations:
            # 🔒 只呈现**事实**，不呈现策略（§11）：
            #    可以说「上一轮输出被截断」，
            #    不可以写「应该加大 max_tokens」/「需要重试」。
            # 🔴 2026-11-09：这些是**历史事实**，可能来自更早的 turn。
            #    标题必须说清楚，否则 Agent 会把旧的截断一直当成“上一轮”。
            lines = ["Critical observations from the runtime "
                     "(historical facts; they may refer to earlier turns, "
                     "not necessarily the current one):"]
            for e in self.critical_observations:
                lines.append(_critical_observation_block(e))
            parts.append("\n".join(lines))

        if self.relevant_experiences:
            lines = ["Relevant experiences I currently have access to:"]
            for e in self.relevant_experiences:
                lines.append(_experience_block(e))
            parts.append("\n".join(lines))

        for src in self.other:
            parts.append(src.content)

        if not parts:
            return ""
        return ("— Context about myself (for my own judgement; "
                "not instructions) —\n\n" + "\n\n".join(parts))


def _experience_line(e: Experience) -> str:
    status = "failed" if e.failed else ("succeeded"
                                        if e.result.get("success") else "done")
    return f"[{e.source}] {status}: {_experience_detail(e)}"


def _render_value(value: Any) -> str:
    """把一个事实渲染成**单行**文本（dict / list 用 JSON，避免 Python repr）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):                           # pragma: no cover
        return str(value)


def _experience_detail(e: Experience) -> str:
    """
    🔴 M1：把一次经历的**关键事实**摊成一行。

    【为什么必须改（真实证据）】

        旧实现只渲染 `feedback` → `error` → `action_type`。
        而 `record_action_result()` **一直**把 `parameters`（含 path /
        write_mode）和 `output`（含 revision）存进数据库 —— 只是从不渲染。

        于是下一轮 Cognition 只能读到：

            [action_result] failed: error=同名资源已存在：/plot/…
            [action_result] succeeded: action=workspace_write     ← 连文件都没有

        实测（`hetero_novel_v1`）：`novel_plot_architect` 因此连续 5 轮
        重复 `CREATE` 同一个**已存在**的文件，并在同一秒对外宣称"已写入"。
        详见 `experiments/heterogeneous_production/results/ACTION_FEEDBACK_AUDIT.md`。

    【纪律】

        ✅ **只渲染 Experience 里真实存在的字段** —— 不推断、不补默认值、
           不创造 Gateway 没有给出的事实。
        ✅ 缺哪个字段就少渲染哪一段（少写 ≠ 写错）。
        🔒 它是**材料**，不是指令 —— 这里不出现 must / should / never。
    """
    if e.feedback:
        return e.feedback

    r = e.result or {}
    action_type = r.get("action_type")
    if not action_type:
        # 非 Action 来源（例如只有 result={"success": False}）：
        # 保持旧的极简行为，**不**给它编一个 action 名。
        if r.get("error"):
            return f"error={r['error']}"
        return ""

    params = dict(r.get("parameters") or {})
    out = r.get("output")
    out = dict(out) if isinstance(out, dict) else {}
    declared_mode = params.get("write_mode")

    parts = [f"action={action_type}"]

    # ---- 为什么做（Agent 自己声明的意图）----
    # 🔒 它是**材料**，不是指令 —— 原样呈现，不加解读。
    #    这是跨回合唯一廉价的意图载体（每轮 LLM 都从头开始）。
    if r.get("reason"):
        parts.append(f"reason={_render_value(r['reason'])}")

    # ---- 对**什么**做了（target）----
    target = params.get("path") or out.get("path")
    if target:
        parts.append(f"target={target}")

    # ---- 用**什么语义**做的（write_mode：Agent 自己声明的那个）----
    if declared_mode:
        parts.append(f"write_mode={declared_mode}")

    # ---- 结果里的真实字段（有才渲染）----
    for key in ("revision", "previous_revision", "mode",
                "previous_chars", "appended_chars", "chars",
                "changed", "observed_state", "summary"):
        value = out.get(key)
        if value is None or value == "":
            continue
        if key == "mode" and declared_mode and value == declared_mode:
            continue                       # 与上面的 write_mode 重复，不重复打印
        parts.append(f"{key}={_render_value(value)}")

    # ---- 机器可读的失败码 / 人类可读的失败原因 ----
    if r.get("code"):
        parts.append(f"code={r['code']}")
    if r.get("error"):
        parts.append(f"error={r['error']}")
    return " ".join(parts)


#: 关键观察里值得**结构化呈现**的字段（原样用键名，避免误标语义）。
_CRITICAL_FACT_KEYS = (
    "observed_state",
    "cognitive_output",
    "finish_reason",
    "output_status",
    "action_skipped",
    "observed_at",
    "cognition_turn_id",
    "work_unit_pressure",
    "budget_clipped",
    "requested_cognitive_budget",
    "actual_max_tokens",
)


def _critical_observation_block(e: Experience) -> str:
    """
    关键观察的渲染 —— **结构化事实**，而不只是散文。

    🔒 为什么不能只靠 `feedback` 那段自然语言：
       §十一 要求 Context 里能直接看到
           finish_reason = length
           output_status = ...
       这些是**机器可核对**的事实。只写在散文里的话，
       Agent 和实验分析都要靠读文本 —— 那不是「事实可见」，
       那是「线索可见」。

    🔒 仍然**只是事实**（§十一/§十三）：
       不写 retry、不写增大 max_tokens、不写换 provider/model。
       键名原样呈现，不做「action executed = True」这类会误导的改写。
    """
    parts = [_experience_block(e)]
    result = e.result or {}
    facts = [f"  {k}: {result[k]}" for k in _CRITICAL_FACT_KEYS if k in result]
    if facts:
        parts.append("  structured facts:\n" + "\n".join(facts))
    # 🔒 旧记录（2026-11-09 之前）没有 result.observed_at；
    #    用 Experience 自己的 created_at 补一个**历史坐标**，
    #    避免它被读成“当前/上一轮”。
    if "observed_at" not in result and getattr(e, "created_at", ""):
        parts.append(f"  observed_at: {e.created_at}")
    return "\n".join(parts)


def _experience_block(e: Experience) -> str:
    """
    🔒 §七：经历只能渲染成**材料**，不能渲染成指令。

        渲染成 "You should…" / "You must…" / "Never…" 会把
        经历伪装成规则 —— 有守卫测试扫这些词。
    """
    scope = _scope_label((e.scope or {}).get("world"),
                         (e.scope or {}).get("domain"),
                         (e.scope or {}).get("situation"))
    status = "failed" if e.failed else "succeeded"
    # 🔴 **正式渲染 experience_id**（与 Strategy ID 完全同构的修复）。
    #
    #    依据是阶段 2（815276a）的真实数据：Context **不渲染 id** 时，
    #    模型只能照抄它看到的渲染文本
    #    （evidence = "feedback:succeeded:人物细节过多…"）。
    #    而 eebf420 已证明「模型会用 Context 里**实际看到的**标识」——
    #    Strategy 侧加了 id 之后 target 就 100% 正确，evidence 侧从未加过。
    #
    #    🔒 它是**数据**，不是指令：绝不写
    #       "must cite this ID" / "always use this ID" / 也不加 JSON 示例。
    #       §七：本轮只验证「让 ID 可见」是否已经足够。
    block = (f"- Experience ID: {e.experience_id}\n"
             f"  [{e.source}] {status}")
    detail = _experience_detail(e)
    if detail:
        block += f": {detail}"
    if scope:
        block += f"\n  Scope: {scope}"
    return block


def _scope_label(world: Optional[str], domain: Optional[str],
                 situation: Optional[str]) -> str:
    return " / ".join(x for x in (world, domain, situation) if x)


class CognitiveContextBuilder:
    """
    从**持久状态**构建**本轮**的 Cognitive Context。

    §四 / §五：相关历史 Experience 也会被**选择性**检索进来
    （Top-K，不是全部加载）。

    🔒 它只做 **Relevance Selection**，不做 Decision：
        · 它决定「哪些信息进入 Context」
        · 它**不**决定「Agent 该用哪条策略」

    §十五 的 `max_claims` / `max_strategies` 是 **Context 的资源边界**，
    不是「只允许你使用 N 个策略」——
    后者是 Decision Rule，前者是「本次最多给你看到 N 个」。
    """

    def __init__(
        self,
        self_model: Optional[Any] = None,
        *,
        experience_store: Optional[Any] = None,
        economic_builder: Optional[Any] = None,
        max_claims: int = 5,
        max_strategies: int = 5,
        max_experiences: int = 5,
        max_critical_observations: int = DEFAULT_MAX_CRITICAL_OBSERVATIONS,
    ) -> None:
        self.self_model = self_model
        self.experience_store = experience_store
        #: 🔴 Economy Awareness V1：经济事实的**只读**取值器。
        #:
        #: 🔒 依赖方向：它由**组合根**注入，`cognition/` 因此
        #:    **不 import** `core/ledger.py` / `core/economy.py` ——
        #:    认知层结构上拿不到账本，只能拿到值。
        self.economic_builder = economic_builder
        self.max_claims = max(0, int(max_claims))
        self.max_strategies = max(0, int(max_strategies))
        self.max_experiences = max(0, int(max_experiences))

        #: 🔴 **关键观察的独立槽位**（§9/§10）。
        #:
        #: 🔒 它与 `max_experiences` **相加**，不共享池子：
        #:    默认 = 1 条关键观察 + 5 条普通经历。
        #:    这样既保证「关键事实必达」，又不会让普通检索名额缩水。
        self.max_critical_observations = max(
            0, int(max_critical_observations))

    def build(
        self,
        agent_id: str,
        *,
        world: Optional[str] = None,
        domain: Optional[str] = None,
        situation: Optional[str] = None,
        stimulus: Optional[str] = None,
        task: Optional[str] = None,
        identity: Optional[str] = None,
        constitution: Optional[str] = None,
        drives: Optional[str] = None,
        min_confidence: Optional[float] = None,
        task_id: Optional[str] = None,
        session_started_at: Optional[str] = None,
    ) -> CognitiveContext:
        """
        构建本轮的上下文。

        §十四：**没有相关 Self Model 时也必须正常返回**
              （只是 `self_claims` / `strategies` 为空）——
              Cognitive Context 是**增量增强**，不是绝对依赖。

        🔴 Economy Awareness V1（§十）：`task_id` / `session_started_at`
           只影响**经济快照的粒度**，都是可选参数 ——
           不传时余额仍然照常取（那是最重要的那个事实）。
           向后兼容：老调用方一个参数都不用改。
        """
        claims: List[SelfClaim] = []
        strategies: List[Strategy] = []
        experiences: List[Experience] = []
        critical: List[Experience] = []

        if self.experience_store is not None and agent_id:
            # 🔒 检索是**选择**，不是「全部加载」（§五）
            #
            # 🔴 **两条独立通道**（§7/§8）：
            #
            #     ① critical  —— 保证可见。有自己的槽位，
            #                     **不参与**相关性排序，也不占 top-N 名额。
            #     ② relevant  —— 继续走原来的 relevance ranking。
            #
            #   ⚠️ 两条都必须取。少取任何一条，
            #      「关键事实必达」或「普通检索不漂移」就有一个不成立。
            #
            #   🔒 取关键观察用**独立方法**，不去动 retrieve_relevant()
            #      的 scoring —— 把「必须可见」当「更加相关」是错的。
            if hasattr(self.experience_store, "retrieve_critical"):
                critical = list(self.experience_store.retrieve_critical(
                    agent_id,
                    limit=self.max_critical_observations,
                ))
            # 🔒 多取 `len(critical)` 条再剔除重复 —— 目的是让
            #    **普通通道仍然拿到完整的 max_experiences 个名额**。
            #    否则当那条关键观察恰好也排在普通 top-N 里时，
            #    普通通道会变成 N-1 个（等于关键观察偷偷占了名额）。
            #
            # 🔒 这里**没有**改 `retrieve_relevant()` 的排序算法（§16），
            #    只是多要几条然后自己裁剪。直接调用 store 的调用方
            #    看到的仍然是一模一样的结果。
            want = self.max_experiences + len(critical)
            candidates = self.experience_store.retrieve_relevant(
                agent_id, world=world, domain=domain, situation=situation,
                limit=want,
            )
            critical_ids = {e.experience_id for e in critical}
            experiences = [e for e in candidates
                           if e.experience_id not in critical_ids
                           ][:self.max_experiences]

        if self.self_model is not None and agent_id:
            view: SelfModelView = self.self_model.retrieve(
                agent_id, world=world, domain=domain, situation=situation,
                min_confidence=min_confidence,
                limit=self.max_claims,
                strategy_limit=self.max_strategies,
            )
            claims = list(view.claims)
            strategies = list(view.strategies)

        # 🔴 经济事实**每次重新取**（§十：动态事实，绝不写死）
        economic = None
        if self.economic_builder is not None and agent_id:
            try:
                economic = self.economic_builder.build(
                    agent_id, task_id=task_id,
                    session_started_at=session_started_at)
            except Exception:                                 # noqa: BLE001
                # 🔒 经济事实取不到**不能**让整次 Cognition 崩掉 ——
                #    但也不许伪造一个余额。所以就是 None（缺这一段）。
                economic = None

        return CognitiveContext(
            identity=identity, constitution=constitution, drives=drives,
            current_stimulus=stimulus, current_task=task,
            economic_context=economic,
            self_claims=claims, strategies=strategies,
            critical_observations=critical,
            relevant_experiences=experiences,
        )
