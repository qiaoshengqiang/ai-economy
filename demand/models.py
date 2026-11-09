"""
Demand Entry V1 的数据模型
============================

`DemandRun` 是**人类请求的运行记录**，不是 Task。

🔒 它**不复制** Task 的生命周期状态：
   - `root_task_id` 只是引用；
   - 展示 Task 状态时读 Task Core；
   - `status` 是 **Run 的运行状态**（INTAKE / RUNNING / COMPLETED / ...），
     不是第二个 Task 状态机。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from core.identity import utc_now_iso


@dataclass
class DemandRun:
    run_id: str
    demand: str
    budget_aic: float
    wall_limit_seconds: float
    max_turns: int
    max_agents: int
    front_agent: str
    run_dir: str
    workspace_id: str = ""
    root_task_id: Optional[str] = None
    #: Run 级状态，**不是** Task.status。
    status: str = "INTAKE"
    exit_reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    tokens: int = 0
    cost_ai: float = 0.0
    turns: int = 0
    #: Front 阶段对 Human Demand 的解读（不是 Task 状态）。
    brief: Optional[Dict[str, Any]] = None
    #: Creator 阶段产出的 Workspace 责任计划（root/directory/file owner）。
    workspace_plan: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "demand": self.demand,
            "budget_aic": self.budget_aic,
            "wall_limit_seconds": self.wall_limit_seconds,
            "max_turns": self.max_turns,
            "max_agents": self.max_agents,
            "front_agent": self.front_agent,
            "run_dir": self.run_dir,
            "workspace_id": self.workspace_id,
            "root_task_id": self.root_task_id,
            "status": self.status,
            "exit_reason": self.exit_reason,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "tokens": self.tokens,
            "cost_ai": self.cost_ai,
            "turns": self.turns,
            "brief": dict(self.brief) if self.brief else None,
            "workspace_plan": (
                dict(self.workspace_plan)
                if self.workspace_plan else None),
        }


@dataclass
class TurnOutcome:
    """一次 Agent turn 的可观察结果（Scheduler 只需要这些事实）。"""

    agent_id: str
    ok: bool = False
    tokens: int = 0
    actions: int = 0
    status_before: str = ""
    status_after: str = ""
    error: str = ""
    #: 🔴 这一 turn 是否产生了**真实进展**（不是“有动作”）。
    #:
    #:    由 DemandRunner 判定：状态变化 / 成功写入或编辑 / 新产物 /
    #:    首次出现的有意义消息。单纯的 read / run_tool **不算**。
    progress: bool = False
    progress_reason: str = ""
    #: 🔴 强进展：Task 状态变化 / 成功写入或编辑 / 新目录。
    #:
    #:    普通协调消息只能算“弱进展”；`NO_PROGRESS` 只看强进展，
    #:    否则一封接一封的说明就能让 run 永远不触发 stall。
    strong_progress: bool = False
    #: 🔴 这一 turn 的认知是否被输出上限截断
    #:    （`finish_reason=length`，含 EMPTY_CONTENT）。
    #:
    #:    Scheduler 用它给一次恢复机会：截断不是“Agent 选择不做”，
    #:    它需要下一轮看到压力后拆小/提高预算。
    truncated: bool = False
