"""Agent Wakeup V1 —— 闹钟与唤醒事件的数据模型。

🔒 `Alarm` 是**调度状态**，不是 Message、Task 或 Heartbeat。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


def _task_ids(value: Any) -> List[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        key = str(item or "").strip()
        if key and key not in out:
            out.append(key)
    return out


@dataclass
class Alarm:
    alarm_id: str
    agent_id: str
    status: str
    reason: str
    instruction: str
    task_ids: List[str] = field(default_factory=list)
    schedule_kind: str = "once"
    interval_seconds: Optional[int] = None
    next_fire_at: str = ""
    last_fired_at: str = ""
    fire_count: int = 0
    max_runs: Optional[int] = None
    expires_at: str = ""
    dedupe_key: str = ""
    last_error: str = ""
    created_by: str = ""
    source_message_id: str = ""
    conversation_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "Alarm":
        data = dict(row)
        return cls(
            alarm_id=str(data.get("alarm_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            status=str(data.get("status") or ""),
            reason=str(data.get("reason") or ""),
            instruction=str(data.get("instruction") or ""),
            task_ids=_task_ids(data.get("task_ids_json")),
            schedule_kind=str(data.get("schedule_kind") or "once"),
            interval_seconds=(
                int(data["interval_seconds"])
                if data.get("interval_seconds") is not None else None),
            next_fire_at=str(data.get("next_fire_at") or ""),
            last_fired_at=str(data.get("last_fired_at") or ""),
            fire_count=int(data.get("fire_count") or 0),
            max_runs=(
                int(data["max_runs"])
                if data.get("max_runs") is not None else None),
            expires_at=str(data.get("expires_at") or ""),
            dedupe_key=str(data.get("dedupe_key") or ""),
            last_error=str(data.get("last_error") or ""),
            created_by=str(data.get("created_by") or ""),
            source_message_id=str(data.get("source_message_id") or ""),
            conversation_id=str(data.get("conversation_id") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alarm_id": self.alarm_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "reason": self.reason,
            "instruction": self.instruction,
            "task_ids": list(self.task_ids),
            "schedule_kind": self.schedule_kind,
            "interval_seconds": self.interval_seconds,
            "next_fire_at": self.next_fire_at,
            "last_fired_at": self.last_fired_at,
            "fire_count": self.fire_count,
            "max_runs": self.max_runs,
            "expires_at": self.expires_at,
            "last_error": self.last_error,
            "created_by": self.created_by,
            "source_message_id": self.source_message_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class WakeupEvent:
    wakeup_id: str
    agent_id: str
    kind: str = "alarm"
    reason: str = ""
    instruction: str = ""
    task_ids: List[str] = field(default_factory=list)
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    scheduled_for: str = ""
    fire_count: int = 0
    max_runs: Optional[int] = None
    expires_at: str = ""

    @classmethod
    def from_alarm(
        cls,
        alarm: Alarm,
        *,
        tasks: Optional[List[Mapping[str, Any]]] = None,
    ) -> "WakeupEvent":
        return cls(
            wakeup_id=alarm.alarm_id,
            agent_id=alarm.agent_id,
            reason=alarm.reason,
            instruction=alarm.instruction,
            task_ids=list(alarm.task_ids),
            tasks=[dict(item) for item in (tasks or ())],
            scheduled_for=alarm.next_fire_at,
            fire_count=alarm.fire_count + 1,
            max_runs=alarm.max_runs,
            expires_at=alarm.expires_at,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wakeup_id": self.wakeup_id,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "reason": self.reason,
            "instruction": self.instruction,
            "task_ids": list(self.task_ids),
            "tasks": [dict(item) for item in self.tasks],
            "scheduled_for": self.scheduled_for,
            "fire_count": self.fire_count,
            "max_runs": self.max_runs,
            "expires_at": self.expires_at,
        }


@dataclass
class WakeupResult:
    outcome: str = "failed"
    summary: str = ""
    job_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "summary": self.summary,
            "job_id": self.job_id,
        }


__all__ = ["Alarm", "WakeupEvent", "WakeupResult"]
