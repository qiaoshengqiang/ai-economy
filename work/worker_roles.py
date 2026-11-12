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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass(frozen=True)
class WorkerRole:
    role_id: str
    name: str
    description: str
    system_prompt: str
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
        system_prompt=(
            "以务实的方式工作。先理解任务，检查 Workspace 现状，再选择"
            "最简单正确的做法，并用可用工具验证结果。"
        ),
    ),
    "frontend_engineer": WorkerRole(
        role_id="frontend_engineer",
        name="前端工程师",
        description="网页、交互界面、响应式布局、浏览器兼容与可访问性。",
        system_prompt=(
            "以前端工程师的方式工作。优先交付可运行页面，保证清晰的视觉"
            "层级、响应式布局、可访问控件和浏览器级验证。避免不必要的"
            "依赖。检查控制台错误和真实交互，而不只是静态 HTML。"
        ),
        summary_sections=[
            "页面打开 / 运行方式",
            "响应式与浏览器验证",
        ],
    ),
    "game_developer": WorkerRole(
        role_id="game_developer",
        name="游戏开发者",
        description="小游戏、Canvas/WebGL、输入控制、碰撞、计分和可玩性。",
        system_prompt=(
            "以游戏开发者的方式工作。构建可玩的循环，提供清晰的控制方式、"
            "确定性的状态更新、碰撞 / 结束 / 重开逻辑和视觉反馈。适当使用 "
            "requestAnimationFrame 与 delta time。要在真实浏览器里试玩，"
            "而不只是检查代码。"
        ),
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
        system_prompt=(
            "以后端工程师的方式工作。定义清晰接口，显式处理失败路径，"
            "避免静默异常，并用可执行测试或真实请求验证。保持数据和"
            "副作用可审计。"
        ),
        summary_sections=[
            "API / 入口",
            "测试或请求验证",
        ],
    ),
    "researcher": WorkerRole(
        role_id="researcher",
        name="研究员",
        description="资料检索、对比、证据整理和结构化结论。",
        system_prompt=(
            "以研究员的方式工作。区分事实与推断，引用来源或具体证据，"
            "比较备选方案，给出结构化结论并标明不确定性。"
        ),
        summary_sections=[
            "来源 / 证据",
            "不确定性或冲突证据",
        ],
    ),
    "writer": WorkerRole(
        role_id="writer",
        name="写作者",
        description="长文、说明、文案、结构化和面向读者的表达。",
        system_prompt=(
            "以写作者的方式工作。先确认读者和目的，清晰组织材料，保持"
            "论点具体，做可读性编辑。不要用空泛套话填充。"
        ),
        summary_sections=[
            "读者与结构",
            "写了或改了什么",
        ],
    ),
    "qa_verifier": WorkerRole(
        role_id="qa_verifier",
        name="质量验证者",
        description="复现、测试矩阵、缺陷定位和证据记录。",
        system_prompt=(
            "以质量验证者的方式工作。根据证据复现行为，为已说明的风险建立"
            "小型测试矩阵，报告准确命令和结果，区分真实缺陷与未验证假设。"
            "没有证据不要声称通过。"
        ),
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


def render_role_prompt(role: WorkerRole) -> str:
    lines = [
        "========== Worker 角色 ==========",
        f"role_id: {role.role_id}",
        f"role_name: {role.name}",
        role.system_prompt,
    ]
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


class WorkerRoleError(ValueError):
    """角色配置不合法。"""


def _role_from_mapping(role_id: str, raw: Any) -> WorkerRole:
    if not isinstance(raw, dict):
        raise WorkerRoleError(f"角色 {role_id!r} 必须是映射。")
    summary = raw.get("summary_sections") or []
    if not isinstance(summary, list):
        raise WorkerRoleError(f"角色 {role_id!r} 的 summary_sections 必须是列表。")
    return WorkerRole(
        role_id=role_id,
        name=str(raw.get("name") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        system_prompt=str(raw.get("system_prompt") or "").strip(),
        summary_sections=[str(item).strip() for item in summary
                          if str(item).strip()],
        builtin=bool(raw.get("builtin", role_id in ROLES)),
        archived=bool(raw.get("archived", False)),
    )


def _role_to_mapping(role: WorkerRole) -> Dict[str, Any]:
    return {
        "name": role.name,
        "description": role.description,
        "system_prompt": role.system_prompt,
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

    def render(self, role: WorkerRole) -> str:
        return render_role_prompt(role)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path) if self.path is not None else "",
            "roles": [
                {
                    "role_id": role.role_id,
                    "name": role.name,
                    "description": role.description,
                    "system_prompt": role.system_prompt,
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
        prompt = str(payload.get("system_prompt") or "").strip()
        if not name or not description or not prompt:
            raise WorkerRoleError("name / description / system_prompt 不能为空。")
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise WorkerRoleError(
                f"system_prompt 不能超过 {_MAX_PROMPT_CHARS} 字符。")
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
]
