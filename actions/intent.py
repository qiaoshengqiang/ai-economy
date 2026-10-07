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
    intent: Optional[ActionIntent] = None
    created_at: str = ""

    @property
    def is_action(self) -> bool:
        return self.kind == DECISION_ACT and self.intent is not None

    @property
    def is_reply_only(self) -> bool:
        return self.kind == DECISION_REPLY

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id, "agent_id": self.agent_id,
            "kind": self.kind, "reasoning": self.reasoning,
            "intent": self.intent.to_dict() if self.intent else None,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CognitiveRunResult:
    """
    一次 Agent Cognitive Run 的完整结果（§十六）。

    🔒 **Action Result 必须能被上层 Cognition 看见** ——
       不能让 Executor 执行完之后 result 被 Runtime 吃掉。
    """

    decision: Optional[Decision] = None
    action_intent: Optional[ActionIntent] = None
    action_result: Optional[ActionResult] = None
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

    @property
    def has_action(self) -> bool:
        return self.action_intent is not None

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
            "action_intent": (self.action_intent.to_dict()
                              if self.action_intent else None),
            "action_result": (self.action_result.to_dict()
                              if self.action_result else None),
            "reflection_count": len(self.reflection_proposals),
            "reflections_committed": sum(
                1 for o in self.write_outcomes
                if getattr(o, "committed", False)),
            "output_status": self.output_status,
        }
