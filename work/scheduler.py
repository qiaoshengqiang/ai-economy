"""
TeamScheduler —— Team 调度 / 排队
===================================

Front 把 Brief 放进队列；Scheduler 决定“什么时候能启动 Team”。

    TeamRequest
      ├─ brief
      ├─ lead
      ├─ members
      ├─ priority
      └─ status

🔒 队列里放的是 **Brief**，不是 Task。
   只有真正启动 Team 时，Team Lead 才创建 Task。

🔒 这是 Work 层的运行调度，不是 Life 层的邀请协议。
   Participant 仍然由 Front / Team Lead 直接指定。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.identity import utc_now_iso
from work.availability import AgentAvailability
from work.team_registry import TeamRegistry


class RequestStatus:
    QUEUED = "QUEUED"
    WAITING_FOR_AGENTS = "WAITING_FOR_AGENTS"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @classmethod
    def active(cls) -> List[str]:
        return [cls.QUEUED, cls.WAITING_FOR_AGENTS]


@dataclass
class TeamRequest:
    request_id: str
    brief: Dict[str, Any]
    lead: str
    members: List[str] = field(default_factory=list)
    priority: int = 0
    status: str = RequestStatus.QUEUED
    task_id: str = ""
    error: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def agents(self) -> List[str]:
        return list(dict.fromkeys([self.lead, *self.members]))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "brief": dict(self.brief or {}),
            "lead": self.lead,
            "members": list(self.members),
            "priority": self.priority,
            "status": self.status,
            "task_id": self.task_id,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TeamScheduler:
    """进程内 Team 调度器（V1）。"""

    def __init__(
        self,
        *,
        registry: Optional[TeamRegistry] = None,
        availability: Optional[AgentAvailability] = None,
        max_parallel_teams: int = 3,
        max_agents_per_team: int = 5,
    ) -> None:
        self.availability = availability or AgentAvailability()
        self.registry = registry or TeamRegistry(
            self.availability,
            max_parallel_teams=max_parallel_teams,
        )
        self.max_parallel_teams = int(self.registry.max_parallel_teams)
        self.max_agents_per_team = max(1, int(max_agents_per_team))
        self._lock = threading.RLock()
        self._requests: Dict[str, TeamRequest] = {}

    # ------------------------------------------------------------
    def submit(
        self,
        brief: Dict[str, Any],
        *,
        lead: str,
        members: List[str],
        priority: int = 0,
    ) -> TeamRequest:
        lead = str(lead or "").strip()
        if not lead:
            raise ValueError("TeamRequest 需要 lead")
        normalized: List[str] = []
        for raw in members or []:
            aid = str(raw or "").strip()
            if aid and aid != lead and aid not in normalized:
                normalized.append(aid)
        if len(normalized) + 1 > self.max_agents_per_team:
            raise ValueError(
                f"Team 最多 {self.max_agents_per_team} 个 Agent；"
                f"收到 {len(normalized) + 1}")

        request = TeamRequest(
            request_id=f"teamreq_{uuid.uuid4().hex[:10]}",
            brief=dict(brief or {}),
            lead=lead,
            members=normalized,
            priority=int(priority),
        )
        with self._lock:
            self._requests[request.request_id] = request
        return request

    # ------------------------------------------------------------
    def start_next(
        self,
        start_fn: Callable[[TeamRequest], Dict[str, Any]],
    ) -> Optional[TeamRequest]:
        """
        尝试启动下一个可运行的 Team。

        `start_fn(request)` 必须返回：

            {"ok": True, "task_id": "..."}
            {"ok": False, "message": "..."}

        它负责创建 Task；Scheduler 负责预占 Agent 和容量。
        """
        if not callable(start_fn):
            raise ValueError("start_next 需要 start_fn")

        with self._lock:
            if self.registry.active_count() >= self.max_parallel_teams:
                return None

            candidates = sorted(
                (r for r in self._requests.values()
                 if r.status in RequestStatus.active()),
                key=lambda r: (-r.priority, r.created_at, r.request_id),
            )
            for request in candidates:
                ok, error, _slot = self.registry.register(
                    task_id=request.request_id,
                    lead=request.lead,
                    members=request.members,
                )
                if not ok:
                    if error.startswith("agents_busy"):
                        request.status = RequestStatus.WAITING_FOR_AGENTS
                        request.error = error
                        request.updated_at = utc_now_iso()
                    continue

                request.status = RequestStatus.STARTING
                request.updated_at = utc_now_iso()
                try:
                    result = start_fn(request) or {}
                except Exception as exc:                      # noqa: BLE001
                    result = {
                        "ok": False,
                        "message": f"{type(exc).__name__}: {exc}",
                    }

                if not result.get("ok"):
                    self.registry.release(request.request_id)
                    request.status = RequestStatus.FAILED
                    request.error = str(
                        result.get("message") or "team start failed")
                    request.updated_at = utc_now_iso()
                    return request

                task_id = str(result.get("task_id") or "")
                request.task_id = task_id
                request.status = RequestStatus.RUNNING
                request.error = ""
                request.updated_at = utc_now_iso()
                self.registry.bind_task(request.request_id, task_id)
                return request

        return None

    # ------------------------------------------------------------
    def finish(
        self,
        request_id: str,
        *,
        status: str = RequestStatus.COMPLETED,
        error: str = "",
    ) -> bool:
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None:
                return False
            request.status = status
            request.error = str(error or "")
            request.updated_at = utc_now_iso()
            return self.registry.release(request.request_id)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None:
                return False
            if request.status in (
                RequestStatus.RUNNING, RequestStatus.STARTING):
                return False
            request.status = RequestStatus.CANCELLED
            request.updated_at = utc_now_iso()
            return True

    # ------------------------------------------------------------
    def get(self, request_id: str) -> Optional[TeamRequest]:
        with self._lock:
            return self._requests.get(str(request_id))

    def pending(self) -> List[TeamRequest]:
        with self._lock:
            return sorted(
                (r for r in self._requests.values()
                 if r.status in RequestStatus.active()),
                key=lambda r: (-r.priority, r.created_at, r.request_id),
            )

    def active(self) -> List[TeamRequest]:
        with self._lock:
            return [
                r for r in self._requests.values()
                if r.status in (
                    RequestStatus.STARTING, RequestStatus.RUNNING)
            ]

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._requests.values()]
