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

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass(frozen=True)
class WorkerRole:
    role_id: str
    name: str
    description: str
    #: 通用专业要求（旧字段，继续兼容用户自定义角色）。
    system_prompt: str = ""
    #: phase → 该角色在对应阶段要关注的具体方面。
    phase_requirements: Dict[str, List[str]] = field(default_factory=dict)
    summary_sections: List[str] = field(default_factory=list)
    builtin: bool = False
    archived: bool = False

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
        phase_requirements={
            "prototype": [
                "把需求变成一个具体、可体验的初始成果",
                "自主决定成果的形式、结构和实现方式，不要先写计划",
            ],
            "engineering": [
                "检查现有成果和实际效果，修复阻断性问题",
                "保留原设计意图，必要时才调整结构",
            ],
            "evolution": [
                "基于当前成果、最近结果和未解决问题持续改进",
                "优先处理对目标影响最大的问题",
            ],
        },
    ),
    "frontend_engineer": WorkerRole(
        role_id="frontend_engineer",
        name="前端工程师",
        description="网页、交互界面、响应式布局、浏览器兼容与可访问性。",
        phase_requirements={
            "prototype": [
                "产出完整、可打开的页面或前端原型",
                "自主决定视觉层级、布局、交互和实现方式",
                "优先保证核心页面真的可用，而不是先做工程规范",
            ],
            "engineering": [
                "检查真实交互、浏览器控制台和响应式布局",
                "修复阻断使用的问题；不要只做静态 HTML 检查",
                "保留原型的视觉与交互意图",
            ],
            "evolution": [
                "优先改善可用性、可访问性、响应式和视觉一致性",
                "处理高影响体验问题，不追求无边界的细节回归",
            ],
        },
        summary_sections=[
            "页面打开 / 运行方式",
            "响应式与浏览器验证",
        ],
    ),
    "game_developer": WorkerRole(
        role_id="game_developer",
        name="游戏开发者",
        description="小游戏、Canvas/WebGL、输入控制、碰撞、计分和可玩性。",
        phase_requirements={
            "prototype": [
                "核心玩法必须可操作、可体验，不能只是静态画面",
                "自主决定控制方式、镜头、物理、UI、视觉风格和实现方式",
                "允许原型粗糙，但必须形成完整的核心循环",
            ],
            "engineering": [
                "检查核心交互：控制、碰撞、镜头、HUD、暂停和重开",
                "实际运行或试玩核心路径，确认不是只“看起来能跑”",
                "优先修复影响可玩性的阻断问题",
                "不要把边缘视觉打磨当成工程阶段的阻断问题",
            ],
            "evolution": [
                "优先提升手感、反馈、AI、赛道变化和视觉/音效",
                "优先处理对体验影响最大的问题",
                "不要为了细节牺牲核心循环",
            ],
        },
        summary_sections=[
            "操作方式与玩法",
            "玩法验证（移动、碰撞、重开）",
            "已知玩法限制",
        ],
    ),
    "backend_engineer": WorkerRole(
        role_id="backend_engineer",
        name="后端工程师",
        description="服务、API、数据处理、错误处理和自动化测试。",
        phase_requirements={
            "prototype": [
                "产出可运行的接口、脚本或数据处理原型",
                "自主决定接口形状、数据流和实现方式",
                "优先让核心路径端到端可用",
            ],
            "engineering": [
                "检查接口边界、失败路径和数据一致性",
                "修复阻断性问题，避免静默异常",
                "只在任务要求或确有风险时补测试",
            ],
            "evolution": [
                "优先改善可靠性、性能、可观测性和错误处理",
                "处理高影响的运行问题，不无限扩大测试矩阵",
            ],
        },
        summary_sections=[
            "API / 入口",
            "测试或请求验证",
        ],
    ),
    "researcher": WorkerRole(
        role_id="researcher",
        name="研究员",
        description="资料检索、对比、证据整理和结构化结论。",
        phase_requirements={
            "prototype": [
                "形成一份有实质内容的初步调研成果",
                "自主决定检索范围、对比维度和组织方式",
                "先建立能支撑结论的证据基础",
            ],
            "engineering": [
                "检查关键事实、引用和推理是否一致",
                "修复证据缺口、错误引用和逻辑断裂",
                "区分事实、推断和不确定性",
            ],
            "evolution": [
                "补充对比、证据和结构化结论",
                "优先解决影响结论可信度的问题",
            ],
        },
        summary_sections=[
            "来源 / 证据",
            "不确定性或冲突证据",
        ],
    ),
    "writer": WorkerRole(
        role_id="writer",
        name="写作者",
        description="长文、说明、文案、结构化和面向读者的表达。",
        phase_requirements={
            "prototype": [
                "产出一份完整可读的初稿，而不是只列提纲",
                "自主决定文体、结构、语气和组织方式",
                "先让内容真正成立",
            ],
            "engineering": [
                "检查结构、逻辑、事实和前后一致性",
                "修复明显断裂、重复和表达问题",
                "不把润色当成重写",
            ],
            "evolution": [
                "优先改善节奏、语气、可读性和重点表达",
                "处理对读者理解影响最大的部分",
            ],
        },
        summary_sections=[
            "读者与结构",
            "写了或改了什么",
        ],
    ),
    "qa_verifier": WorkerRole(
        role_id="qa_verifier",
        name="质量验证者",
        description="复现、测试矩阵、缺陷定位和证据记录。",
        phase_requirements={
            "prototype": [
                "先建立可复现的验证路径和基础证据",
                "明确要验证的核心风险",
            ],
            "engineering": [
                "根据证据复现问题，记录准确步骤和结果",
                "区分真实缺陷和未验证假设",
                "优先覆盖阻断性问题",
            ],
            "evolution": [
                "补充高价值验证和回归覆盖",
                "报告剩余风险和未验证项",
            ],
        },
        summary_sections=[
            "测试了什么、怎么测",
            "失败、复现步骤与证据",
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


def requirements_for(role: WorkerRole, phase: str = "") -> List[str]:
    """角色在某个阶段的具体要求；旧 system_prompt 作为通用要求。"""
    requirements: List[str] = []
    if role.system_prompt:
        requirements.append(role.system_prompt)
    if phase:
        requirements.extend(role.phase_requirements.get(phase) or [])
    return requirements


def render_role_prompt(role: WorkerRole, phase: str = "") -> str:
    """渲染角色；有 phase 时只渲染该阶段要求，避免变成第二套流程。"""
    lines = [
        "========== Worker 角色 ==========",
        f"role_id: {role.role_id}",
        f"role_name: {role.name}",
    ]
    if phase:
        lines.append(f"phase: {phase}")
        requirements = requirements_for(role, phase)
        if requirements:
            lines.append("本阶段的专业要求：")
            lines.extend(f"- {item}" for item in requirements)
    else:
        if role.system_prompt:
            lines.append("通用专业要求：")
            lines.append(role.system_prompt)
        for name, requirements in role.phase_requirements.items():
            if not requirements:
                continue
            lines.append(f"{name} 阶段：")
            lines.extend(f"- {item}" for item in requirements)
    if role.summary_sections:
        lines.append("角色额外总结项：")
        lines.extend(f"- {item}" for item in role.summary_sections)
    return "\n".join(lines)




# ============================================================
# Persistent role store
# ============================================================

_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_MAX_PROMPT_CHARS = 8000
_MAX_SUMMARY_ITEMS = 20
_MAX_SUMMARY_ITEM_CHARS = 200
_PHASES = ("prototype", "engineering", "evolution")


class WorkerRoleError(ValueError):
    """角色配置不合法。"""


def _phase_requirements_from_raw(raw: Any) -> Dict[str, List[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WorkerRoleError("phase_requirements 必须是映射。")
    out: Dict[str, List[str]] = {}
    for phase, items in raw.items():
        name = str(phase)
        if name not in _PHASES:
            raise WorkerRoleError(f"未知阶段：{name!r}")
        if isinstance(items, str):
            items = items.splitlines()
        if not isinstance(items, list):
            raise WorkerRoleError(f"{name}.phase_requirements 必须是列表。")
        values = [str(item).strip() for item in items if str(item).strip()]
        if values:
            out[name] = values
    return out


def _role_from_mapping(role_id: str, raw: Any) -> WorkerRole:
    if not isinstance(raw, dict):
        raise WorkerRoleError(f"角色 {role_id!r} 必须是映射。")
    summary = raw.get("summary_sections") or []
    if not isinstance(summary, list):
        raise WorkerRoleError(f"角色 {role_id!r} 的 summary_sections 必须是列表。")
    role = WorkerRole(
        role_id=role_id,
        name=str(raw.get("name") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        system_prompt=str(raw.get("system_prompt") or "").strip(),
        phase_requirements=_phase_requirements_from_raw(
            raw.get("phase_requirements")),
        summary_sections=[str(item).strip() for item in summary
                          if str(item).strip()],
        builtin=bool(raw.get("builtin", role_id in ROLES)),
        archived=bool(raw.get("archived", False)),
    )
    # 兼容旧 worker_roles.yaml：内置角色没有 phase_requirements 时，
    # 使用代码里的阶段要求；文件里的 system_prompt 作为通用要求保留。
    if role_id in ROLES and not role.phase_requirements:
        role = replace(
            role,
            phase_requirements=dict(ROLES[role_id].phase_requirements),
        )
    return role


def _role_to_mapping(role: WorkerRole) -> Dict[str, Any]:
    return {
        "name": role.name,
        "description": role.description,
        "system_prompt": role.system_prompt,
        "phase_requirements": {
            phase: list(items)
            for phase, items in role.phase_requirements.items()
        },
        "summary_sections": list(role.summary_sections),
        "builtin": bool(role.builtin or role.role_id in ROLES),
        "archived": bool(role.archived),
    }


class WorkerRoleStore:
    """Worker Roles 的持久化存储。

    内置角色在代码里提供默认值；文件存在时以文件为权威（可以覆盖内置
    角色的 prompt，也可以新增自定义角色）。删除使用归档，避免历史 Task
    的 `acceptance["role"]` 突然指向不存在的定义。
    """

    def __init__(
        self, path: Optional[Path] = None, *,
        backup_dir: Optional[Path] = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.backup_dir = (
            Path(backup_dir) if backup_dir is not None
            else (self.path.parent / "backups" if self.path is not None else None)
        )
        self._roles: Dict[str, WorkerRole] = self._load()

    # ---- load / save ----
    def _load(self) -> Dict[str, WorkerRole]:
        roles: Dict[str, WorkerRole] = {}
        if self.path is not None and self.path.exists():
            try:
                data = yaml.safe_load(self.path.read_text("utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise WorkerRoleError(
                    f"读取 Worker Roles 失败：{self.path}：{exc}") from exc
            raw_roles = data.get("roles") if isinstance(data, dict) else {}
            if not isinstance(raw_roles, dict):
                raise WorkerRoleError(
                    f"{self.path} 的 roles 必须是映射。")
            for role_id, raw in raw_roles.items():
                roles[str(role_id)] = _role_from_mapping(str(role_id), raw)
        # 内置角色是保底：文件里缺了某个内置角色时补回来，
        # 但显式 archived=true 的内置角色会保留归档状态。
        for role_id, role in ROLES.items():
            if role_id not in roles:
                roles[role_id] = WorkerRole(**{
                    **role.__dict__, "builtin": True,
                })
        return roles

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.backup_dir is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            try:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                backup = self.backup_dir / f"worker_roles.{stamp}.yaml"
                backup.write_text(self.path.read_text("utf-8"), "utf-8")
            except OSError:
                pass
        payload = {
            "version": 1,
            "roles": {
                role_id: _role_to_mapping(role)
                for role_id, role in self._roles.items()
            },
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(
            yaml.safe_dump(payload, allow_unicode=True,
                           sort_keys=False, width=1000),
            encoding="utf-8")
        tmp.replace(self.path)

    # ---- read ----
    def all(self) -> Dict[str, WorkerRole]:
        return dict(self._roles)

    def catalog(self) -> List[Dict[str, str]]:
        return [
            role.to_catalog()
            for role in self._roles.values()
            if not role.archived
        ]

    def resolve(
        self, role_id: Optional[str], *, include_archived: bool = False,
    ) -> Optional[WorkerRole]:
        value = str(role_id or "").strip()
        if not value:
            value = DEFAULT_ROLE
        role = self._roles.get(value)
        if role is None:
            return None
        if role.archived and not include_archived:
            return None
        return role

    def render(self, role: WorkerRole, phase: str = "") -> str:
        return render_role_prompt(role, phase=phase)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path) if self.path is not None else "",
            "roles": [
                {
                    "role_id": role.role_id,
                    "name": role.name,
                    "description": role.description,
                    "system_prompt": role.system_prompt,
                    "phase_requirements": {
                        phase: list(items)
                        for phase, items in role.phase_requirements.items()
                    },
                    "summary_sections": list(role.summary_sections),
                    "builtin": bool(role.builtin or role.role_id in ROLES),
                    "archived": bool(role.archived),
                }
                for role in self._roles.values()
            ],
        }

    # ---- write ----
    def upsert(self, payload: Dict[str, Any]) -> WorkerRole:
        role_id = str(payload.get("role_id") or "").strip()
        if not _ROLE_ID_RE.fullmatch(role_id):
            raise WorkerRoleError(
                "role_id 必须是 2..41 位小写字母/数字/下划线，"
                "且以字母开头。")
        existing = self._roles.get(role_id)
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        prompt = str(
            payload.get("system_prompt", existing.system_prompt if existing else "")
            or "").strip()
        raw_phase = payload.get("phase_requirements")
        if raw_phase is None:
            phase_requirements = (
                dict(existing.phase_requirements) if existing else {})
        else:
            phase_requirements = _phase_requirements_from_raw(raw_phase)
        if not name or not description:
            raise WorkerRoleError("name / description 不能为空。")
        if not prompt and not phase_requirements:
            raise WorkerRoleError(
                "system_prompt 与 phase_requirements 至少有一个不能为空。")
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise WorkerRoleError(
                f"system_prompt 不能超过 {_MAX_PROMPT_CHARS} 字符。")
        for phase, items in phase_requirements.items():
            if len(items) > _MAX_SUMMARY_ITEMS:
                raise WorkerRoleError(
                    f"{phase} 要求最多 {_MAX_SUMMARY_ITEMS} 项。")
            if any(len(item) > 500 for item in items):
                raise WorkerRoleError(
                    f"{phase} 每项要求不能超过 500 字符。")
        raw_summary = payload.get("summary_sections") or []
        if isinstance(raw_summary, str):
            raw_summary = raw_summary.splitlines()
        if not isinstance(raw_summary, list):
            raise WorkerRoleError("summary_sections 必须是列表或每行一项的文本。")
        summary = [str(item).strip() for item in raw_summary
                   if str(item).strip()]
        if len(summary) > _MAX_SUMMARY_ITEMS:
            raise WorkerRoleError(
                f"summary_sections 最多 {_MAX_SUMMARY_ITEMS} 项。")
        if any(len(item) > _MAX_SUMMARY_ITEM_CHARS for item in summary):
            raise WorkerRoleError(
                f"每个 summary section 不能超过 {_MAX_SUMMARY_ITEM_CHARS} 字符。")
        role = WorkerRole(
            role_id=role_id,
            name=name,
            description=description,
            system_prompt=prompt,
            phase_requirements=phase_requirements,
            summary_sections=summary,
            builtin=bool(existing.builtin) if existing else role_id in ROLES,
            archived=False,
        )
        self._roles[role_id] = role
        self._save()
        return role

    def archive(self, role_id: str) -> WorkerRole:
        role = self._roles.get(str(role_id or "").strip())
        if role is None:
            raise WorkerRoleError(f"未知角色：{role_id!r}")
        if role.role_id == DEFAULT_ROLE:
            raise WorkerRoleError(f"{DEFAULT_ROLE} 不能归档。")
        active = [r for r in self._roles.values()
                  if not r.archived and r.role_id != role.role_id]
        if not active:
            raise WorkerRoleError("至少要保留一个可用角色。")
        self._roles[role.role_id] = WorkerRole(**{
            **role.__dict__, "archived": True,
        })
        self._save()
        return self._roles[role.role_id]

    def reset(self, role_id: str) -> WorkerRole:
        value = str(role_id or "").strip()
        if value not in ROLES:
            raise WorkerRoleError(f"{value!r} 不是内置角色，无法恢复默认。")
        role = ROLES[value]
        self._roles[value] = WorkerRole(**{
            **role.__dict__, "builtin": True, "archived": False,
        })
        self._save()
        return self._roles[value]


__all__ = [
    "WorkerRole", "WorkerRoleError", "WorkerRoleStore", "ROLES", "DEFAULT_ROLE",
    "role_catalog", "resolve_role", "render_role_prompt",
    "requirements_for",
]
