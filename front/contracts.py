"""
Front 层数据契约
=================

Brief 承载来源分层的 Work 指令：

    human_text   Human 对 Front 的原话（不一定是需求）
    task_text    Front 给 Worker 的任务说明（Worker 执行的内容）
    front_notes  Front 自己的判断 / 建议（非硬要求）
    files        Front 准备好的文件
    reply_style  Front 要求的回复格式

🔒 `reply` 只给人类看，不是 Worker 指令。
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
    #: 只读查看 Task 事实（不是最终决定；Front 可以在决定前调用）
    INSPECT = "inspect"
    #: 修改 Front / Work 的全局模式
    SET_MODE = "set_mode"
    #: 人类确认提交某个已完成 Task 的 final snapshot
    COMMIT_CONFIRM = "commit_confirm"
    #: 闹钟唤醒后决定不打扰人类；仍然写 Activity Record。
    NO_NOTIFY = "no_notify"

    @classmethod
    def all(cls) -> List[str]:
        return [
            cls.DIRECT_REPLY, cls.HANDOFF, cls.REWORK,
            cls.FOLLOW_UP, cls.CANCEL, cls.ARTIFACTS, cls.CLARIFY,
            cls.INSPECT, cls.SET_MODE, cls.COMMIT_CONFIRM,
            cls.NO_NOTIFY,
        ]


@dataclass
class FrontBrief:
    """
    Front → Worker 的工作 Brief。

    🔒 `human_text` 永远是 Human 对 Front 的原话，**不一定本身是需求**；
       `task_text` 是 Front 给 Worker 的任务说明（Worker 执行的内容）；
       `front_notes` 是 Front 自己的判断 / 建议，**不是硬要求**。
    """

    goal: str
    brief_id: str = ""
    human_text: str = ""
    #: 交给 Work Agent 的任务文本。可以是 Human 原文，也可以是 Front
    #: 判断后做的忠实重述；必须保持原意。所有需求 / 约束 / 验收都应
    #: 直接写在这里。
    task_text: str = ""
    #: Front 自己的判断 / 建议 / 取舍说明；Worker 可以看到，但它不是
    #: Human 要求，也不是必须执行的 DoD。
    front_notes: str = ""
    #: 旧版结构化字段：只保留给 CLI / 旧 job log 兼容。
    #: Front handoff 不再解析它们，Controller 也不传给 Worker。
    deliverables: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    acceptance_expectations: List[str] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    non_goals: List[str] = field(default_factory=list)
    context_refs: List[str] = field(default_factory=list)
    #: Front 自己准备好的文件，随 Brief 交给 Work Agent。
    #:
    #: 每项：
    #:   {"path": "index.html", "content": "<完整内容>"}
    #:   {"path": "index.html", "from_private": "snake.html"}
    #:   {"path": "index.html", "draft_id": "draft_..."}
    #: 后两种由组合根从 Front 的私有空间 / 草稿解析成 content。
    files: List[Dict[str, str]] = field(default_factory=list)
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
        self.front_notes = str(self.front_notes or "").strip()
        self.brief_id = str(self.brief_id or f"brief_{uuid.uuid4().hex[:12]}")
        self.created_at = str(self.created_at or utc_now_iso())
        self.deliverables = _strings(self.deliverables)
        self.constraints = _strings(self.constraints)
        self.acceptance_expectations = _strings(self.acceptance_expectations)
        self.ambiguities = _strings(self.ambiguities)
        self.non_goals = _strings(self.non_goals)
        self.context_refs = _strings(self.context_refs)
        self.files = _files(self.files)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "goal": self.goal,
            "human_text": self.human_text,
            "task_text": self.task_text,
            "front_notes": self.front_notes,
            "deliverables": list(self.deliverables),
            "constraints": list(self.constraints),
            "acceptance_expectations": list(self.acceptance_expectations),
            "ambiguities": list(self.ambiguities),
            "non_goals": list(self.non_goals),
            "context_refs": list(self.context_refs),
            "files": [dict(item) for item in self.files],
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
            front_notes=str(data.get("front_notes") or ""),
            deliverables=data.get("deliverables") or [],
            constraints=data.get("constraints") or [],
            acceptance_expectations=(
                data.get("acceptance_expectations") or []),
            ambiguities=data.get("ambiguities") or [],
            non_goals=data.get("non_goals") or [],
            context_refs=data.get("context_refs") or [],
            files=data.get("files") or [],
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
    #: handoff / rework / follow_up：Worker 执行说明与来源分层。
    brief: Optional[FrontBrief] = None
    #: rework / follow_up
    task_id: str = ""
    #: clarify
    question: str = ""
    #: handoff：给 Work Agent / 最终回复的格式要求（不是任务需求）
    reply_style: str = ""
    #: handoff：Human 显式指定的执行者（路由约束，不是任务内容）。
    #: 空字符串 = 没有指定，由系统选空闲 Agent。
    preferred_agent: str = ""
    #: handoff：任务级专业角色（见 work.worker_roles）。
    role: str = ""
    #: handoff：交付模式 direct / standard / creative_product。
    work_profile: str = "standard"
    #: set_mode：Front 模式 autonomous / relay；空 = 不修改。
    front_mode: str = ""
    #: set_mode：Work 模式 workflow / autonomous；空 = 不修改。
    work_mode: str = ""
    #: commit_confirm：人类确认的快照短码。
    commit_code: str = ""
    #: rework：返工范围 fix / enhance / redesign。
    rework_scope: str = ""
    confidence: float = 1.0
    reason: str = ""
    #: 产生这个决定的模型原始输出（可审计，不参与逻辑）。
    raw_output: str = ""

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
            FrontIntent.INSPECT, FrontIntent.COMMIT_CONFIRM,
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
            # 顶层也放一份，方便 job log / UI 直接读，不必解 brief。
            "task_text": self.brief.task_text if self.brief else "",
            "front_notes": self.brief.front_notes if self.brief else "",
            "task_id": self.task_id,
            "question": self.question,
            "reply_style": self.reply_style,
            "preferred_agent": self.preferred_agent,
            "role": self.role,
            "work_profile": self.work_profile,
            "front_mode": self.front_mode,
            "work_mode": self.work_mode,
            "commit_code": self.commit_code,
            "rework_scope": self.rework_scope,
            "confidence": self.confidence,
            "reason": self.reason,
            "raw_output": self.raw_output,
        }


@dataclass
class FrontTurn:
    """一次 Front 决策回合：可以包含多个最终动作。"""

    decisions: List[FrontDecision] = field(default_factory=list)
    raw_output: str = ""
    action_history: List[Dict[str, Any]] = field(default_factory=list)
    #: 本回合每次 LLM 尝试的原始输出 / 解析错误 / 动作类型。
    #: 只用于诊断，不参与决策。
    attempts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def primary(self) -> FrontDecision:
        if not self.decisions:
            raise ValueError("FrontTurn 没有任何 decision")
        return self.decisions[0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decisions": [item.to_dict() for item in self.decisions],
            "raw_output": self.raw_output,
            "actions": list(self.action_history),
            "attempts": list(self.attempts),
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


def _files(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        entry: Dict[str, str] = {"path": path}
        content = item.get("content")
        if isinstance(content, str):
            entry["content"] = content
        source = item.get("from_private")
        if source:
            entry["from_private"] = str(source).strip()
        draft_id = item.get("draft_id")
        if draft_id:
            entry["draft_id"] = str(draft_id).strip()
        # 没有内容来源的条目没有意义，不保留。
        if len(entry) == 1:
            continue
        out.append(entry)
    return out


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
