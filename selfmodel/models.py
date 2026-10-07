"""
Self Model V1 —— 数据结构
============================================================

【Self Model 是什么（§四）】

    Self Model 是 Agent 对「自己」的**动态工作模型**。
    它**不是** Agent 的全部持久数据。

    Self Model ≠ Memory        （Memory 是持久存储，Self Model 是高层抽象）
    Self Model ≠ World Model
    Self Model ≠ Other-Agent Model
    Self Model ≠ Public Manifest
    Self Model ≠ Identity / Ledger / Capability / Authorization

【🔴 Self Model 不能成为第二权威源（§十八）】

    以下状态的权威来源**仍然是它们自己**：
        AIC 余额 · Ledger · 资产 · Capability · Authorization
        Workspace 权限 · Lock · Git 状态 · Task 状态

    Self Model 里只允许有**Agent 的判断**，例如：
        ✅「我认为自己的 AIC 不够支持这个任务」   （判断，可能错）
        ❌「我的 AIC = 43」                      （这是 Ledger 的事实）

【为什么 Scope 是一等公民（§八）】

    "我不擅长长篇结构设计" **不能**被理解成
    "Agent 在任何事情上都不擅长结构设计"。

    它只在 World=Digital Production / Domain=Fiction /
    Situation=Long-form Planning 范围内成立。

    所以 scope 是**三个独立可空字段**，而不是一段自由文本 ——
    这样结构化过滤（§十二）才有可能。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- 种类 ---------------------------------------------------------------
KIND_FACT = "fact"        # 系统/权威事实的**引用**（不是副本）
KIND_CLAIM = "claim"      # Agent 的判断（可能错，可被修正）
VALID_KINDS = {KIND_FACT, KIND_CLAIM}

# --- 状态 ---------------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"      # 已废弃/不再适用（**不删除**，保留 provenance）
VALID_STATUSES = {STATUS_ACTIVE, STATUS_RETIRED}

# --- 证据来源（§八：必须保留来源概念）------------------------------------
EVIDENCE_EXPERIENCE = "experience"
EVIDENCE_FEEDBACK = "feedback"
EVIDENCE_OBSERVATION = "observation"
EVIDENCE_REFLECTION = "reflection"
EVIDENCE_HUMAN_INPUT = "human_input"
EVIDENCE_SYSTEM_FACT = "system_fact"
VALID_EVIDENCE_SOURCES = {
    EVIDENCE_EXPERIENCE, EVIDENCE_FEEDBACK, EVIDENCE_OBSERVATION,
    EVIDENCE_REFLECTION, EVIDENCE_HUMAN_INPUT, EVIDENCE_SYSTEM_FACT,
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clamp_confidence(value: Any) -> float:
    """confidence 必须落在 [0, 1]。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


@dataclass(frozen=True)
class Evidence:
    """
    一条证据。**必须有来源** —— 没有来源的判断无法被评价和修正。
    """

    source: str
    detail: str = ""
    ref: Optional[str] = None      # 例如 task_id / message_id

    def __post_init__(self) -> None:
        if self.source not in VALID_EVIDENCE_SOURCES:
            raise ValueError(
                f"未知证据来源 {self.source!r}；"
                f"合法取值：{sorted(VALID_EVIDENCE_SOURCES)}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "detail": self.detail, "ref": self.ref}


@dataclass(frozen=True)
class Scope:
    """
    适用范围。三个维度都可为空 —— 空 = 不限定该维度。

    🔒 注意：空 scope **不等于**「全局人格事实」。
       它只表示「这条认知没有被限定」。
       §八 要求「不要把局部判断自动变成全局人格事实」——
       所以调用方**应当**尽量给出 scope。
    """

    world: Optional[str] = None
    domain: Optional[str] = None
    situation: Optional[str] = None

    def specificity(self) -> int:
        """有几个维度被限定了。用于检索排序 —— **仅用于排序**。"""
        return sum(1 for v in (self.world, self.domain, self.situation) if v)

    def compatible(self, other: "Scope") -> bool:
        """
        **硬兼容**判断 —— 只把 world / domain 的不一致当作排除理由。

        🔴 为什么 situation 不参与硬排除？
           §九 说 fiction/story_planning 应该「**优先看到**」
           fiction、digital_production/fiction —— 是**优先**，不是**只**。

           如果把 situation 不一致当作排除，
           一条 scope=character_design 的 claim 在 story_planning 场景下
           会被**静默漏掉** —— 实测踩过这个坑（selected_claims = 0）。
        """
        for mine, theirs in ((self.world, other.world),
                             (self.domain, other.domain)):
            if mine is None or theirs is None:
                continue
            if str(mine).strip().lower() != str(theirs).strip().lower():
                return False
        return True

    def relevance(self, other: "Scope") -> int:
        """
        **软相关度**：匹配上了几个维度。**仅用于排序，不用于排除。**

        用来实现 §九 的「优先看到」：
            situation 也匹配的 > 只有 domain 匹配的 > 无 scope 的
        """
        score = 0
        for mine, theirs in ((self.world, other.world),
                             (self.domain, other.domain),
                             (self.situation, other.situation)):
            if mine is None:
                continue
            if theirs is not None and \
                    str(mine).strip().lower() == str(theirs).strip().lower():
                score += 1
        return score

    def matches(self, other: "Scope") -> bool:
        """
        本 scope 是否适用于 `other` 情境。

        规则：本 scope 限定的每个维度都必须与 other 一致；
              本 scope 未限定的维度**不构成限制**。
        """
        for mine, theirs in ((self.world, other.world),
                             (self.domain, other.domain),
                             (self.situation, other.situation)):
            # 🔒 scope 值是自由文本（人 / LLM 都会写），
            #    所以比较**大小写不敏感** —— 否则 "Fiction" 与 "fiction"
            #    会被当成两个不同的 World，检索静默漏掉。
            if mine is None or theirs is None:
                continue
            if str(mine).strip().lower() != str(theirs).strip().lower():
                return False
            if mine is not None and theirs is None:
                # 我限定了具体范围，对方情境未指明该维度 → 仍算适用
                continue
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"world": self.world, "domain": self.domain,
                "situation": self.situation}


@dataclass(frozen=True)
class SelfClaim:
    """
    Self Facts / Claims（§八）。

    ⚠️ 注意字段名是 `content` + `confidence` + `evidence`，
       **不是** `key -> value`。
       §八 明确要求避免「简单粗暴的 key->value」——
       因为 Self Model 里很多东西**不是绝对事实**。
    """

    claim_id: str
    agent_id: str
    kind: str
    content: str
    scope: Scope
    confidence: float
    evidence: List[Evidence] = field(default_factory=list)
    status: str = STATUS_ACTIVE
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


@dataclass(frozen=True)
class Strategy:
    """
    Strategy V1（§九）。

    🔴 **Strategy ≠ Rule。**

        Strategy 是「Agent 从过去经验、反馈和思考中形成的、
        可供未来决策使用的**可复用思考/行为模式**」。

        ⚠️ 本类的属性（confidence / rationale / ...）是
           **提供给 Agent 作为判断依据的数据**，
           **不是** 由 Runtime 求值的门控条件。

        禁止（§十）：
            if applicability > 0.7: use_strategy()
            if expected_benefit > cost * 1.5: apply()

        正确方向：
            Strategy attributes → 提供给 Agent → Agent 自己比较 → Agent 自己决定
    """

    strategy_id: str
    agent_id: str
    name: str
    domain: Optional[str] = None
    situation: Optional[str] = None
    rationale: str = ""
    confidence: float = 0.5
    evidence: List[Evidence] = field(default_factory=list)
    status: str = STATUS_ACTIVE
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


@dataclass(frozen=True)
class SelfModelView:
    """
    **一次检索的结果**（§十二）。

    🔒 它是一袋**数据**，不是一份**指令**。

        它**没有**：
            should_use() / is_applicable() / best_strategy()
            / recommended_action() / decide()

        为什么故意没有？
            一旦有了 `best_strategy()`，Runtime 就在替 Agent 做选择了 ——
            那正是 §十 禁止的「Strategy 变回 Rule」。

        调用方拿到这个 view 后，应当把它**交给 Agent**
        （例如放进 Cognitive Context），由 Agent 自己判断。
    """

    claims: List[SelfClaim] = field(default_factory=list)
    strategies: List[Strategy] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.claims and not self.strategies

    def __len__(self) -> int:
        return len(self.claims) + len(self.strategies)


def dump_evidence(items: List[Evidence]) -> str:
    return json.dumps([e.to_dict() for e in items or []], ensure_ascii=False)


def load_evidence(raw: Any) -> List[Evidence]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    out = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Evidence(source=item.get("source", ""),
                                detail=item.get("detail", "") or "",
                                ref=item.get("ref")))
        except ValueError:
            continue                                    # 忽略坏证据，不炸
    return out
