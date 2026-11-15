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

from core.identity import (
    agent_display_name, format_agent_identity, format_agent_label,
)


def resolve_agent_name(runtime: Any, agent_id: str) -> str:
    """解析显示名：canonical config 优先，其次 Economy 身份，最后为空。

    🔒 `agents.<id>.name` 是唯一权威来源。`ResolvedAgent.name` 直接来自它；
       `Economy` 的身份名在启动时也会同步到同一个配置值。
       这里保留 fallback，只是为了让测试 / 未声明 Agent 仍能工作。
    """
    key = str(agent_id or "").strip()
    if not key:
        return ""

    resolved = getattr(runtime, "resolved_config", None)
    if resolved is not None:
        try:
            name = str(getattr(resolved.agent(key), "name", "") or "").strip()
            if name:
                return name
        except Exception:                                     # noqa: BLE001
            pass

    economy = getattr(runtime, "economy", None)
    if economy is not None:
        try:
            name = str(getattr(economy.agent(key), "name", "") or "").strip()
            if name:
                return name
        except Exception:                                     # noqa: BLE001
            pass
    return ""


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

    `agent_name` 只是**称呼**，不是经济身份；`agent_id` 仍是唯一技术标识。
    """

    task_id: str
    agent_id: str
    role: str
    #: 显示名（来自 `agents.<id>.name`）；空时回退到 `agent_id`。
    agent_name: str = ""
    #: 其他参与者的显示名：`agent_id -> name`。只用于 prompt 称呼。
    agent_names: Dict[str, str] = field(default_factory=dict)
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
    #: 任务认知阶段的完整报告；后续所有决策都注入它。
    cognition_report: str = ""
    #: 当前 Agent 在这个 Task 下的草稿元数据。
    draft_facts: str = ""

    @property
    def display_name(self) -> str:
        return agent_display_name(self.agent_name, self.agent_id)

    @property
    def agent_label(self) -> str:
        """紧凑展示：`小艾（Agent ID: agent_a）`。只用于列表 / 消息头。"""
        return format_agent_label(self.agent_name, self.agent_id)

    @property
    def agent_identity(self) -> str:
        """自我介绍：名字和 Agent ID 分成两条事实。"""
        return format_agent_identity(self.agent_name, self.agent_id)

    def label_for(self, agent_id: str) -> str:
        """其他 Agent 的紧凑展示：`小博（Agent ID: agent_b）`。"""
        return format_agent_label(
            self.agent_names.get(str(agent_id), ""), agent_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "agent_names": dict(self.agent_names),
            "role": self.role,
            "iteration": self.iteration,
            "brief": dict(self.brief) if self.brief else None,
            "workspace_id": self.workspace_id,
            "acceptance_criteria": list(self.acceptance_criteria),
            "cognition_report": self.cognition_report,
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
    #: Work 层等待人类确认，不改变 Task 生命周期状态。
    waiting_human: bool = False
    question: Optional[Dict[str, Any]] = None

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
            "waiting_human": bool(self.waiting_human),
            "question": dict(self.question) if self.question else None,
        }
