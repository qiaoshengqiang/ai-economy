"""
WorkPlan —— Leader 的分工计划
==============================

Leader 在 Work 层做三件事：

    1. 计划 / 架构
    2. 分配任务
    3. 汇总、接线、收尾

本模块只描述 **Leader 决定的计划**，不自动选人、不自动调度：

    WorkPlan
      ├─ items: WorkItem[]     每个 item 明确 assigned_to
      ├─ qa: Optional[str]     Leader 决定是否设置 QA
      ├─ directories: {path: owner}
      └─ notes

🔒 QA 是可选的；是否存在由 Leader 决定。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class WorkItem:
    item_id: str
    agent_id: str
    goal: str
    paths: List[str] = field(default_factory=list)
    acceptance: List[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        self.item_id = str(self.item_id or f"item_{uuid.uuid4().hex[:8]}")
        self.agent_id = str(self.agent_id or "").strip()
        self.goal = str(self.goal or "").strip()
        if not self.agent_id:
            raise ValueError(f"WorkItem {self.item_id} 缺少 agent_id")
        if not self.goal:
            raise ValueError(f"WorkItem {self.item_id} 缺少 goal")
        self.paths = _strings(self.paths)
        self.acceptance = _strings(self.acceptance)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "agent_id": self.agent_id,
            "goal": self.goal,
            "paths": list(self.paths),
            "acceptance": list(self.acceptance),
            "notes": self.notes,
        }


@dataclass
class WorkPlan:
    items: List[WorkItem]
    qa: Optional[str] = None
    directories: Dict[str, str] = field(default_factory=dict)
    notes: str = ""
    #: 计划版本。Leader 运行中重新计划时 +1；旧版本保留在 history 里。
    revision: int = 1

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("WorkPlan 至少需要一个 item")
        if not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("WorkPlan.revision 必须是从 1 开始的整数")
        self.qa = str(self.qa or "").strip() or None
        self.directories = {
            str(path).strip().lstrip("/"): str(owner).strip()
            for path, owner in (self.directories or {}).items()
            if str(path).strip() and str(owner).strip()
        }

    def items_for(self, agent_id: str) -> List[WorkItem]:
        return [item for item in self.items if item.agent_id == agent_id]

    def workers(self) -> List[str]:
        out: List[str] = []
        for item in self.items:
            if item.agent_id not in out:
                out.append(item.agent_id)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "items": [item.to_dict() for item in self.items],
            "qa": self.qa,
            "directories": dict(self.directories),
            "notes": self.notes,
        }


def _iter_json_objects(text: str):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


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


def parse_work_plan(
    text: str,
    *,
    participants: Iterable[str],
    default_goal: str = "",
) -> Tuple[Optional[WorkPlan], str]:
    """
    解析 Leader 输出的 JSON 计划。

    返回 `(plan, error)`；成功时 error 为空字符串。
    """
    people = {str(item) for item in participants}
    for payload in _iter_json_objects(str(text or "")):
        raw_items = payload.get("items") or payload.get("work_items")
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list) or not raw_items:
            continue

        items: List[WorkItem] = []
        seen_ids = set()
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                return None, f"item_{index}_not_object"
            agent_id = str(
                raw.get("assigned_to") or raw.get("agent_id")
                or raw.get("owner") or "").strip()
            if not agent_id:
                return None, f"item_{index}_missing_assignee"
            if agent_id not in people:
                return None, f"item_{index}_unknown_agent:{agent_id}"
            goal = str(raw.get("goal") or raw.get("task") or "").strip()
            if not goal:
                return None, f"item_{index}_missing_goal"
            item_id = str(raw.get("item_id") or f"item_{index}").strip()
            if item_id in seen_ids:
                return None, f"duplicate_item_id:{item_id}"
            seen_ids.add(item_id)
            try:
                items.append(WorkItem(
                    item_id=item_id,
                    agent_id=agent_id,
                    goal=goal,
                    paths=raw.get("paths") or raw.get("files") or [],
                    acceptance=raw.get("acceptance") or [],
                    notes=str(raw.get("notes") or ""),
                ))
            except ValueError as exc:
                return None, str(exc)

        qa = str(payload.get("qa") or payload.get("qa_agent") or "").strip()
        qa = qa or None
        if qa and qa not in people:
            return None, f"qa_unknown_agent:{qa}"

        directories = payload.get("directories") or {}
        if not isinstance(directories, dict):
            return None, "directories_not_object"
        for path, owner in directories.items():
            if str(owner).strip() not in people:
                return None, f"directory_unknown_owner:{owner}"

        try:
            return WorkPlan(
                items=items,
                qa=qa,
                directories=directories,
                notes=str(payload.get("notes") or ""),
            ), ""
        except ValueError as exc:
            return None, str(exc)

    return None, "work_plan_not_found"
