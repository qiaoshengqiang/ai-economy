"""
Front 层数据契约
=================

Brief 承载 Human 原文 + Front 选择的 `task_text`（原文或忠实重述）
+ 可选的 goal / deliverables / constraints / acceptance + reply_style。
Front 负责保持原意，不把任务改写成另一种需求。
Result 是 Work 回到 Front 的**可读结果**，不是 Task 状态。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso


class FrontIntent:
    """Front 对 Human 输入的意图分类。"""

    #: 直接回答，不创建任何工作
    DIRECT_REPLY = "direct_reply"
    #: 新工作 → Brief → Team Lead 创建 Task
    HANDOFF = "handoff"
    #: 对已完成 Task 的返工 → 路由给 Creator → Reopen
    REWORK = "rework"
    #: 对进行中 Task 的追问 / 补充 → 路由给 Creator
    FOLLOW_UP = "follow_up"
    #: Human 明确要求停止进行中的 Task
    CANCEL = "cancel"
    #: Human 要求查看某个 Task 的产物（导出到宿主机）
    ARTIFACTS = "artifacts"
    #: 意图不明确，向 Human 澄清
    CLARIFY = "clarify"

    @classmethod
    def all(cls) -> List[str]:
        return [
            cls.DIRECT_REPLY, cls.HANDOFF, cls.REWORK,
            cls.FOLLOW_UP, cls.CANCEL, cls.ARTIFACTS, cls.CLARIFY,
        ]


@dataclass
class FrontBrief:
    """
    Front → Team Lead 的工作 Brief。

    🔒 `human_text` 永远是 Human 原文；`task_text` 是 Front 选择交给
       Work 的版本（可以是原文或忠实重述）。结构化字段是可选补充。
    """

    goal: str
    brief_id: str = ""
    human_text: str = ""
    #: 交给 Work Agent 的任务文本。可以是 Human 原文，也可以是 Front
    #: 判断后做的忠实重述；必须保持原意。
    task_text: str = ""
    deliverables: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    acceptance_expectations: List[str] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    non_goals: List[str] = field(default_factory=list)
    context_refs: List[str] = field(default_factory=list)
    #: Front 给 Work Agent 的最终回复格式要求（不是任务需求）。
    reply_style: str = ""
    created_at: str = ""
    fallback: bool = False

    def __post_init__(self) -> None:
        self.goal = str(self.goal or "").strip()
        if not self.goal:
            raise ValueError("FrontBrief 需要 goal")
        self.human_text = str(self.human_text or "").strip()
        self.task_text = str(
            self.task_text or self.goal or self.human_text).strip()
        self.brief_id = str(self.brief_id or f"brief_{uuid.uuid4().hex[:12]}")
        self.created_at = str(self.created_at or utc_now_iso())
        self.deliverables = _strings(self.deliverables)
        self.constraints = _strings(self.constraints)
        self.acceptance_expectations = _strings(self.acceptance_expectations)
        self.ambiguities = _strings(self.ambiguities)
        self.non_goals = _strings(self.non_goals)
        self.context_refs = _strings(self.context_refs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "goal": self.goal,
            "human_text": self.human_text,
            "task_text": self.task_text,
            "deliverables": list(self.deliverables),
            "constraints": list(self.constraints),
            "acceptance_expectations": list(self.acceptance_expectations),
            "ambiguities": list(self.ambiguities),
            "non_goals": list(self.non_goals),
            "context_refs": list(self.context_refs),
            "reply_style": self.reply_style,
            "created_at": self.created_at,
            "fallback": self.fallback,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FrontBrief":
        return cls(
            goal=str(data.get("goal") or ""),
            brief_id=str(data.get("brief_id") or ""),
            human_text=str(data.get("human_text") or ""),
            task_text=str(data.get("task_text") or ""),
            deliverables=data.get("deliverables") or [],
            constraints=data.get("constraints") or [],
            acceptance_expectations=(
                data.get("acceptance_expectations") or []),
            ambiguities=data.get("ambiguities") or [],
            non_goals=data.get("non_goals") or [],
            context_refs=data.get("context_refs") or [],
            reply_style=str(data.get("reply_style") or ""),
            created_at=str(data.get("created_at") or ""),
            fallback=bool(data.get("fallback")),
        )


@dataclass
class FrontDecision:
    """一次 Human 输入的 Front 决策。"""

    intent: str
    #: direct_reply
    reply: str = ""
    #: handoff
    brief: Optional[FrontBrief] = None
    #: rework / follow_up
    task_id: str = ""
    #: clarify
    question: str = ""
    #: handoff：给 Work Agent / 最终回复的格式要求（不是任务需求）
    reply_style: str = ""
    confidence: float = 1.0
    reason: str = ""

    def __post_init__(self) -> None:
        if self.intent not in FrontIntent.all():
            raise ValueError(
                f"未知 FrontIntent：{self.intent!r}；"
                f"合法值：{FrontIntent.all()}")
        if self.intent == FrontIntent.HANDOFF and self.brief is None:
            raise ValueError("handoff 必须带 FrontBrief")
        if self.intent in (
            FrontIntent.REWORK, FrontIntent.FOLLOW_UP,
            FrontIntent.CANCEL, FrontIntent.ARTIFACTS,
        ):
            if not str(self.task_id or "").strip():
                raise ValueError(f"{self.intent} 必须带 task_id")
        if self.intent == FrontIntent.CLARIFY:
            if not str(self.question or "").strip():
                self.question = "我需要再确认一下你的目标，能具体说明吗？"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "reply": self.reply,
            "brief": self.brief.to_dict() if self.brief else None,
            "task_id": self.task_id,
            "question": self.question,
            "reply_style": self.reply_style,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass
class FrontResult:
    """
    Work → Front 的结果。

    🔒 它不是 Task 状态；`status` 只是从 Task Core 读到的快照引用。
    """

    task_id: str
    status: str
    summary: str = ""
    artifacts: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    iteration: int = 1
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "next_actions": list(self.next_actions),
            "iteration": self.iteration,
            "error": self.error,
        }

    def to_human_text(self) -> str:
        """把结果整理成 Human 可读文本。"""
        lines = [f"任务 {self.task_id}：{self.status}（第 {self.iteration} 轮）"]
        if self.summary:
            lines.append(self.summary)
        if self.artifacts:
            lines.append("产物：")
            lines.extend(f"- {item}" for item in self.artifacts)
        if self.error:
            lines.append(f"问题：{self.error}")
        if self.next_actions:
            lines.append("下一步：")
            lines.extend(f"- {item}" for item in self.next_actions)
        return "\n".join(lines)


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out
