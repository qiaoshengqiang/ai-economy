"""
Experience —— 一次**已经发生的经历**的结构化记录
============================================================

【🔴 三条边界（§三 / §五）】

① **Experience ≠ Memory ≠ Self Model**

       Experience 是**一次经历/结果**的记录。
       它**不是** Self Model，也**不直接**写进 Self Model。

② **Experience 是证据，不是结论（§五）**

       ✅ ActionResult: write failed  →  Experience
       ❌ ActionResult: write failed  →  Belief「我不擅长写文件」
       ❌ ActionResult: write failed  →  Strategy「以后不要写文件」

       从证据到结论**必须经过 Agent Reflection**。

③ **Experience 的来源不止 ActionResult（§六）**

       action_result · feedback · observation
       （其余来源保留扩展空间，但**不**一次性造复杂系统）
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SOURCE_ACTION_RESULT = "action_result"
SOURCE_FEEDBACK = "feedback"
SOURCE_OBSERVATION = "observation"
VALID_SOURCES = {SOURCE_ACTION_RESULT, SOURCE_FEEDBACK, SOURCE_OBSERVATION}

#: `Experience.result` 里的关键观察标记。
#:
#: 🔴 **Critical Observation ≠ Important Experience。**
#:
#:     普通 Experience  → 参与 relevance ranking（可能被 top-N 淘汰）
#:     Critical Observation → Runtime 已确认的关键事实，**保证进入 Context**
#:
#: 🔒 **不是新增数据库列** —— 它写在既有的 `result` JSON 里。
#:    只有一个布尔语义（§5：不引入 CRITICAL/HIGH/MEDIUM/LOW 分级，
#:    也不引入 importance_score / salience_score / retrieval_weight）。
CRITICAL_OBSERVATION_KEY = "critical_observation"

#: 关键观察的**保留条数上限**（§10）。
#:
#: 🔒 取 1 是有意的（"latest critical observation"）：
#:     本轮要解决的最小核心问题是「**上一轮刚刚发生的关键事实不能消失**」。
#:     这个值直接决定 Context 的额外 token 开销 ——
#:     每多留一条，就多一段必然进入 Context 的文本。
#:     不设成大数，是因为关键观察必须**稀缺**（§22：不是垃圾桶）。
DEFAULT_MAX_CRITICAL_OBSERVATIONS = 1


def new_experience_id() -> str:
    return f"exp_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Experience:
    """
    一次经历。

    🔒 它是**证据**。不要在这里放任何「所以以后应该…」的内容 ——
       那是 Reflection 的产物，属于 Self Model。
    """

    experience_id: str
    agent_id: str
    source: str
    #: 指向触发它的东西（intent_id / action_id / feedback_id …）
    event_ref: str = ""
    #: 结构化结果。**原样记录**，不做解读。
    result: Dict[str, Any] = field(default_factory=dict)
    #: 人类/其他 Agent 的反馈文本（来源为 source=feedback 时）
    feedback: str = ""
    #: 发生时的情境（可空）—— 供未来 Reflection 检索用
    scope: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """这次经历是不是一次失败。**仅供上层判断，不触发任何自动行为。**"""
        return self.result.get("success") is False

    @property
    def is_critical_observation(self) -> bool:
        """
        这条经历是不是**关键观察**？

        🔒 判据只有一个：`result["critical_observation"]` 是真值。
           **不**看 source、**不**看 feedback、**不**看失败与否 ——
           "重要" 不等于 "关键"，不许拿别的信号来猜。
        """
        return bool((self.result or {}).get(CRITICAL_OBSERVATION_KEY))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experience_id": self.experience_id, "agent_id": self.agent_id,
            "source": self.source, "event_ref": self.event_ref,
            "result": dict(self.result or {}), "feedback": self.feedback,
            "scope": dict(self.scope or {}), "created_at": self.created_at,
            "metadata": dict(self.metadata or {}),
        }
