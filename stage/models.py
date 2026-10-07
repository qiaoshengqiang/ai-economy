"""
Production Stage V1 —— 数据结构
============================================================

【Stage 是什么（§四）】

    Stage 是 **Long-lived Task 的阶段性生产单元**。
    Stage **不是**新的 Task，**也不是**新的 Agent。

        Task   完成一部长篇小说
          └ Stage 1  完成第 1～10 章
          └ Stage 2  完成第 11～20 章

【🔴🔴 最高架构守卫：Stage 不得成为 Workflow Script（§七）】

    Stage **只**允许描述：
        Goal · Deliverables · Constraints / Hard Limits
        Current State · Checkpoint

    Stage **绝对不允许**包含：
        steps · sequence · workflow · ordered_steps · step_1 …

    为什么这是最高级别的守卫？
        一旦 Stage 里有步骤序列，Agent 就从「**决定怎么做**」
        退化成「**执行脚本**」—— 直接违背最高设计原则
        「规则限制结构，**策略决定行为**」。

    → 有 AST 守卫测试扫 `stage/` 下所有源码（见 tests/test_stage.py）。

【Stage 与 Task 的状态不要形成两套冲突的状态机（§八）】

    Stage 状态是**派生**的、更细的视角，不是并列的第二套中央状态机。
    且**原因是信息，不是状态** —— 「为什么在等」放进 `waiting_for`，
    而不是做成 WAITING_FOR_FEEDBACK / WAITING_FOR_HUMAN 等一堆状态（§九）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- Stage 状态（§九）----------------------------------------------------
STATUS_PENDING = "PENDING"
STATUS_ACTIVE = "ACTIVE"
STATUS_WAITING = "WAITING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
VALID_STATUSES = {STATUS_PENDING, STATUS_ACTIVE, STATUS_WAITING,
                  STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}

#: 🔒 「为什么在等」是**信息**，不是状态（§九）。
WAITING_FOR_HUMAN_INPUT = "human_input"
WAITING_FOR_FEEDBACK = "feedback"
WAITING_FOR_RESOURCE = "resource"
WAITING_FOR_RESUME = "resume"
WAITING_FOR_AGENT_DECISION = "agent_decision"
VALID_WAITING_FOR = {WAITING_FOR_HUMAN_INPUT, WAITING_FOR_FEEDBACK,
                     WAITING_FOR_RESOURCE, WAITING_FOR_RESUME,
                     WAITING_FOR_AGENT_DECISION}

#: 🔴 §七：这些**字段名**永远不许出现在 Stage 上。守卫会扫。
FORBIDDEN_STAGE_FIELDS = ("steps", "sequence", "workflow",
                          "ordered_steps", "step_1", "step_2",
                          "pipeline", "stages_script")

# --- Checkpoint 的 reason（§二十一）-------------------------------------
REASON_HARD_LIMIT = "hard_limit"
REASON_AGENT_STOP = "agent_stop"
REASON_AGENT_PAUSE = "agent_pause"
REASON_SUBMIT = "submit"
REASON_CRASH = "crash"

# --- Feedback 来源（§二十四）--------------------------------------------
FEEDBACK_HUMAN = "human"
FEEDBACK_EXTERNAL = "external"
FEEDBACK_EVALUATION = "evaluation"
VALID_FEEDBACK_SOURCES = {FEEDBACK_HUMAN, FEEDBACK_EXTERNAL,
                          FEEDBACK_EVALUATION}


def new_stage_id() -> str:
    return f"stage_{uuid.uuid4().hex[:12]}"


def new_checkpoint_id() -> str:
    return f"ckpt_{uuid.uuid4().hex[:12]}"


def new_feedback_id() -> str:
    return f"fb_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Stage:
    """
    一个阶段性生产单元。

    🔴 **只有目标与交付结构，没有步骤。** 见模块 docstring。
    """

    stage_id: str
    task_id: str
    goal: str
    deliverables: List[Dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_PENDING
    #: 🔒 只有 status == WAITING 时有意义。原因是**信息**不是状态。
    waiting_for: str = ""
    current_checkpoint: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in (STATUS_PENDING, STATUS_ACTIVE, STATUS_WAITING)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_id": self.stage_id, "task_id": self.task_id,
            "goal": self.goal,
            "deliverables": list(self.deliverables or []),
            "status": self.status, "waiting_for": self.waiting_for,
            "current_checkpoint": self.current_checkpoint,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class Checkpoint:
    """
    可恢复的状态快照（§十九 / §二十 / §二十一）。

    🔴 **不复制 Workspace。** 它只**引用** Workspace Revision ——
       内容状态的事实由 Workspace 持有，Checkpoint 只记录
       「当前 Stage 在哪个 revision 上形成了可恢复状态」。
    """

    checkpoint_id: str
    stage_id: str
    workspace_id: str = ""
    revision: str = ""
    reason: str = ""
    progress: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id, "stage_id": self.stage_id,
            "workspace_id": self.workspace_id, "revision": self.revision,
            "reason": self.reason, "progress": dict(self.progress or {}),
            "created_at": self.created_at,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class Feedback:
    """
    外部反馈入口（§二十四）。

    🔴 **Feedback 不直接修改 Self Model**（§二十五）：
        Feedback → Stimulus/Observation → Agent Cognition
                 → Experience / Reflection → （可能的）Self Model
    """

    feedback_id: str
    content: str
    task_id: str = ""
    stage_id: str = ""
    agent_id: str = ""
    source: str = FEEDBACK_HUMAN
    created_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feedback_id": self.feedback_id, "content": self.content,
            "task_id": self.task_id, "stage_id": self.stage_id,
            "agent_id": self.agent_id, "source": self.source,
            "created_at": self.created_at,
            "metadata": dict(self.metadata or {}),
        }
