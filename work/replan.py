"""
WorkPlan 运行中更新
====================

Leader 可以在两轮执行之间调整计划：

    add        增加 item
    reassign   改派 item
    cancel     取消 item
    update     修改 item 目标 / 验收 / 路径

🔒 约束：

    · only Leader；
    · 必须带 expected_revision；
    · DONE 的 item 不能改派 / 取消；
    · 新 assignee 必须仍是 participants；
    · 旧 revision 不删除（由 runtime 保留 history）。

本模块只做**结构与状态校验**，不执行工作、不改 Task 状态。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Iterable, List, Optional, Tuple

from work.plan import WorkItem, WorkPlan


class ReplanError(Exception):
    """计划更新被拒绝。"""


def apply_plan_operations(
    plan: WorkPlan,
    operations: Any,
    *,
    participants: Iterable[str],
    leader: str,
    item_status: Optional[Dict[str, str]] = None,
    qa: Any = None,
    qa_provided: bool = False,
    reason: str = "",
) -> Tuple[WorkPlan, List[Dict[str, Any]]]:
    """
    返回 `(new_plan, applied_operations)`。

    失败抛 `ReplanError`；调用方负责把它变成结构化 ActionResult。
    """
    people = {str(item) for item in participants}
    status = dict(item_status or {})
    if not isinstance(operations, list) or not operations:
        raise ReplanError("operations 必须是非空列表")

    items = list(plan.items)
    by_id = {item.item_id: item for item in items}
    all_ids = set(by_id) | set(status)
    applied: List[Dict[str, Any]] = []

    for index, raw in enumerate(operations, start=1):
        if not isinstance(raw, dict):
            raise ReplanError(f"operation_{index}_not_object")
        op = str(raw.get("op") or raw.get("type") or "").strip().lower()
        if not op:
            raise ReplanError(f"operation_{index}_missing_op")

        if op == "add":
            item = _item_from(raw.get("item") or raw)
            if item.item_id in all_ids:
                raise ReplanError(f"duplicate_item_id:{item.item_id}")
            _require_agent(item.agent_id, people, item.item_id)
            by_id[item.item_id] = item
            items.append(item)
            all_ids.add(item.item_id)
            applied.append({"op": "add", "item_id": item.item_id})
            continue

        item_id = str(raw.get("item_id") or "").strip()
        if not item_id:
            raise ReplanError(f"operation_{index}_missing_item_id")
        current = by_id.get(item_id)
        if current is None:
            raise ReplanError(f"unknown_item:{item_id}")
        current_status = str(status.get(item_id) or "")

        if op == "reassign":
            if current_status == "DONE":
                raise ReplanError(f"item_already_done:{item_id}")
            assigned_to = str(
                raw.get("assigned_to") or raw.get("agent_id") or "").strip()
            _require_agent(assigned_to, people, item_id)
            updated = replace(current, agent_id=assigned_to)
            by_id[item_id] = updated
            items = [updated if item.item_id == item_id else item
                     for item in items]
            applied.append({
                "op": "reassign", "item_id": item_id,
                "assigned_to": assigned_to,
            })
            continue

        if op == "cancel":
            if current_status == "DONE":
                raise ReplanError(f"item_already_done:{item_id}")
            items = [item for item in items if item.item_id != item_id]
            by_id.pop(item_id, None)
            applied.append({"op": "cancel", "item_id": item_id})
            continue

        if op == "update":
            fields = {}
            if "goal" in raw:
                fields["goal"] = str(raw.get("goal") or "").strip()
            if "paths" in raw:
                fields["paths"] = raw.get("paths") or []
            if "acceptance" in raw:
                fields["acceptance"] = raw.get("acceptance") or []
            if "notes" in raw:
                fields["notes"] = str(raw.get("notes") or "")
            if not fields:
                raise ReplanError(f"update_no_fields:{item_id}")
            try:
                updated = replace(current, **fields)
            except ValueError as exc:
                raise ReplanError(str(exc)) from exc
            by_id[item_id] = updated
            items = [updated if item.item_id == item_id else item
                     for item in items]
            applied.append({"op": "update", "item_id": item_id})
            continue

        raise ReplanError(f"unknown_operation:{op}")

    if not items:
        raise ReplanError("plan_would_be_empty")

    new_qa = plan.qa
    if qa_provided:
        new_qa = str(qa or "").strip() or None
        if new_qa:
            _require_agent(new_qa, people, "qa")
            if new_qa == leader:
                raise ReplanError("qa_is_leader")

    try:
        new_plan = WorkPlan(
            items=items,
            qa=new_qa,
            directories=dict(plan.directories),
            notes=plan.notes,
            revision=plan.revision + 1,
        )
    except ValueError as exc:
        raise ReplanError(str(exc)) from exc

    if reason:
        new_plan.notes = (new_plan.notes + "\n" if new_plan.notes else "") + str(reason)
    return new_plan, applied


def _item_from(raw: Any) -> WorkItem:
    if not isinstance(raw, dict):
        raise ReplanError("add_item_not_object")
    agent_id = str(
        raw.get("assigned_to") or raw.get("agent_id")
        or raw.get("owner") or "").strip()
    goal = str(raw.get("goal") or raw.get("task") or "").strip()
    if not goal:
        raise ReplanError("add_item_missing_goal")
    try:
        return WorkItem(
            item_id=str(raw.get("item_id") or "").strip(),
            agent_id=agent_id,
            goal=goal,
            paths=raw.get("paths") or raw.get("files") or [],
            acceptance=raw.get("acceptance") or [],
            notes=str(raw.get("notes") or ""),
        )
    except ValueError as exc:
        raise ReplanError(str(exc)) from exc


def _require_agent(agent_id: str, people: Iterable[str], item_id: str) -> None:
    if not agent_id:
        raise ReplanError(f"missing_assignee:{item_id}")
    if agent_id not in people:
        raise ReplanError(f"unknown_agent:{agent_id}")
