"""
Worker Roles —— 任务级专业角色
==============================

Role 是**任务属性**，不是 Agent 身份、权限或预算：

    Role      = 这次任务用什么专业视角工作
    Agent     = 谁来做
    Permission= 能访问什么
    Budget    = 花多少

Front 只看到 id + 一句话描述；Worker 只拿到被选中的完整 role prompt。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class WorkerRole:
    role_id: str
    name: str
    description: str
    system_prompt: str
    summary_sections: List[str] = field(default_factory=list)

    def to_catalog(self) -> Dict[str, str]:
        return {
            "id": self.role_id,
            "name": self.name,
            "description": self.description,
        }


ROLES: Dict[str, WorkerRole] = {
    "generalist": WorkerRole(
        role_id="generalist",
        name="通用执行者",
        description="没有明确专业归类时使用；先理解任务，再选择合适工具完成。",
        system_prompt=(
            "Work as a pragmatic generalist. Understand the task, inspect the "
            "workspace before changing it, choose the simplest correct "
            "approach, and verify the result with the available tools."
        ),
    ),
    "frontend_engineer": WorkerRole(
        role_id="frontend_engineer",
        name="前端工程师",
        description="网页、交互界面、响应式布局、浏览器兼容与可访问性。",
        system_prompt=(
            "Work as a frontend engineer. Prioritize a working page, clear "
            "visual hierarchy, responsive layout, accessible controls, and "
            "browser-level verification. Avoid unnecessary dependencies. "
            "Check console errors and real interaction, not just static HTML."
        ),
        summary_sections=[
            "How to open/run the page",
            "Responsive and browser verification",
        ],
    ),
    "game_developer": WorkerRole(
        role_id="game_developer",
        name="游戏开发者",
        description="小游戏、Canvas/WebGL、输入控制、碰撞、计分和可玩性。",
        system_prompt=(
            "Work as a game developer. Build a playable loop with clear "
            "controls, deterministic state updates, collision/game-over/"
            "restart behavior, and visual feedback. Use requestAnimationFrame "
            "with delta time where appropriate. Playtest the actual game in a "
            "browser, not only unit-level code."
        ),
        summary_sections=[
            "Controls and how to play",
            "Gameplay verification (movement, collision, restart)",
            "Known gameplay limitations",
        ],
    ),
    "backend_engineer": WorkerRole(
        role_id="backend_engineer",
        name="后端工程师",
        description="服务、API、数据处理、错误处理和自动化测试。",
        system_prompt=(
            "Work as a backend engineer. Define clear interfaces, handle "
            "failure paths explicitly, avoid silent exceptions, and verify "
            "with executable tests or real requests. Keep data and side "
            "effects auditable."
        ),
        summary_sections=[
            "API/entry points",
            "Test or request verification",
        ],
    ),
    "researcher": WorkerRole(
        role_id="researcher",
        name="研究员",
        description="资料检索、对比、证据整理和结构化结论。",
        system_prompt=(
            "Work as a researcher. Separate facts from inference, cite "
            "sources or concrete evidence, compare alternatives, and produce "
            "a structured conclusion with uncertainty called out."
        ),
        summary_sections=[
            "Sources/evidence",
            "Uncertainty or conflicting evidence",
        ],
    ),
    "writer": WorkerRole(
        role_id="writer",
        name="写作者",
        description="长文、说明、文案、结构化和面向读者的表达。",
        system_prompt=(
            "Work as a writer. Identify the audience and purpose, organize "
            "the material clearly, keep claims concrete, and edit for "
            "readability. Do not pad with generic filler."
        ),
        summary_sections=[
            "Audience and structure",
            "What was written or changed",
        ],
    ),
    "qa_verifier": WorkerRole(
        role_id="qa_verifier",
        name="质量验证者",
        description="复现、测试矩阵、缺陷定位和证据记录。",
        system_prompt=(
            "Work as a QA verifier. Reproduce behavior from evidence, build a "
            "small test matrix for the stated risks, report exact commands and "
            "results, and distinguish a real defect from an untested "
            "assumption. Do not claim a pass without evidence."
        ),
        summary_sections=[
            "What was tested and how",
            "Failures, reproduction steps, and evidence",
        ],
    ),
}

DEFAULT_ROLE = "generalist"


def role_catalog() -> List[Dict[str, str]]:
    return [ROLES[key].to_catalog() for key in ROLES]


def resolve_role(role_id: Optional[str]) -> Optional[WorkerRole]:
    value = str(role_id or "").strip()
    if not value:
        return ROLES[DEFAULT_ROLE]
    return ROLES.get(value)


def render_role_prompt(role: WorkerRole) -> str:
    lines = [
        "========== WORKER ROLE ==========",
        f"role_id: {role.role_id}",
        f"role_name: {role.name}",
        role.system_prompt,
    ]
    if role.summary_sections:
        lines.append("Role-specific summary additions:")
        lines.extend(f"- {item}" for item in role.summary_sections)
    return "\n".join(lines)


__all__ = [
    "WorkerRole", "ROLES", "DEFAULT_ROLE",
    "role_catalog", "resolve_role", "render_role_prompt",
]
