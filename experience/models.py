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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experience_id": self.experience_id, "agent_id": self.agent_id,
            "source": self.source, "event_ref": self.event_ref,
            "result": dict(self.result or {}), "feedback": self.feedback,
            "scope": dict(self.scope or {}), "created_at": self.created_at,
            "metadata": dict(self.metadata or {}),
        }
