"""
Work 层的角色与上下文
======================

🔒 Work 层只有一个组织结构：

    Task（= 团队容器）
      ├─ Lead      Task Creator / Team Lead
      └─ Member    由 Task participants 表达

没有独立 Team 实体，也没有 Life 层的身份/经济字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class WorkRole:
    """Work 层角色。"""

    LEAD = "LEAD"
    MEMBER = "MEMBER"
    QA = "QA"
    SOLO = "SOLO"

    @classmethod
    def all(cls) -> List[str]:
        return [cls.LEAD, cls.MEMBER, cls.QA, cls.SOLO]


@dataclass
class WorkContext:
    """
    一个 Agent 在 Work 层工作时需要的最小上下文。

    🔒 刻意**不包含**：
       wallet / balance / price / agreed_price / identity economy /
       message inbox / acceptance assignment protocol。

    这些属于 Life 层，不进入具体工作的 prompt。
    """

    task_id: str
    agent_id: str
    role: str
    iteration: int = 1
    brief: Optional[Dict[str, Any]] = None
    workspace_id: str = ""
    acceptance_criteria: List[str] = field(default_factory=list)
    #: 由调用方渲染好的只读事实（例如 workspace 文件树）。
    workspace_facts: str = ""
    #: 私有工作记忆 / 短期记忆 / 长期记忆的只读渲染。
    memory_facts: str = ""
    #: 任务级公共工作记忆（所有参与者共享的契约 / 计划 / 进度）。
    #: 它是 Work prompt 的一等输入，不再依赖反复 shared_context_read。
    public_memory_facts: str = ""
    #: 当前 Agent 在这个 Task 下的草稿元数据。
    draft_facts: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "iteration": self.iteration,
            "brief": dict(self.brief) if self.brief else None,
            "workspace_id": self.workspace_id,
            "acceptance_criteria": list(self.acceptance_criteria),
        }


@dataclass
class WorkResult:
    """一次 Work run 的结果（不是 Task 状态）。"""

    task_id: str
    status: str
    iteration: int = 1
    turns: int = 0
    tokens: int = 0
    actions: int = 0
    summary: str = ""
    error: str = ""
    artifacts: List[str] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    plan_history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and self.status == "COMPLETED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "iteration": self.iteration,
            "turns": self.turns,
            "tokens": self.tokens,
            "actions": self.actions,
            "summary": self.summary,
            "error": self.error,
            "artifacts": list(self.artifacts),
            "events": list(self.events),
            "plan_history": list(self.plan_history),
        }
