"""
TeamRegistry —— Task 就是团队容器
==================================

不新增 Team 实体。这里只是一个**运行中 Team 的注册表**：

    task_id → 正在运行的 Work Team

职责：

    · 并行上限（默认 3）
    · 加入时原子占用所有 participants 的 IDLE
    · 释放时统一回到 IDLE

🔒 团队信息仍以 Task 的 creator / contractor / participants 为准，
   这里不复制第二个任务状态。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from work.availability import AgentAvailability


@dataclass
class TeamSlot:
    task_id: str
    lead: str
    members: List[str] = field(default_factory=list)
    workspace_id: str = ""

    @property
    def agents(self) -> List[str]:
        return list(dict.fromkeys([self.lead, *self.members]))


class TeamRegistry:
    """运行中 Team 的容量与可用性管理。"""

    def __init__(
        self,
        availability: Optional[AgentAvailability] = None,
        *,
        max_parallel_teams: int = 3,
    ) -> None:
        self.availability = availability or AgentAvailability()
        self.max_parallel_teams = max(1, int(max_parallel_teams))
        self._lock = threading.RLock()
        self._teams: Dict[str, TeamSlot] = {}
        self._by_task: Dict[str, str] = {}

    # ------------------------------------------------------------
    def can_start(self) -> bool:
        with self._lock:
            return len(self._teams) < self.max_parallel_teams

    def register(
        self,
        *,
        task_id: str,
        lead: str,
        members: List[str],
        workspace_id: str = "",
    ) -> Tuple[bool, str, Optional[TeamSlot]]:
        """
        尝试加入一个运行中 Team。

        返回 `(ok, error_code, slot)`。
        """
        task_id = str(task_id)
        lead = str(lead or "").strip()
        if not lead:
            return False, "missing_lead", None

        normalized: List[str] = []
        for raw in members or []:
            aid = str(raw or "").strip()
            if aid and aid not in normalized:
                normalized.append(aid)
        if lead in normalized:
            normalized.remove(lead)

        with self._lock:
            if task_id in self._teams:
                return False, "already_registered", self._teams[task_id]
            if len(self._teams) >= self.max_parallel_teams:
                return False, "max_parallel_teams", None

            agents = [lead, *normalized]
            busy = [a for a in agents if not self.availability.is_idle(a)]
            if busy:
                return False, f"agents_busy:{','.join(sorted(busy))}", None

            acquired: List[str] = []
            for aid in agents:
                if not self.availability.try_acquire(aid):
                    for done in acquired:
                        self.availability.release(done)
                    return False, f"agents_busy:{aid}", None
                acquired.append(aid)

            slot = TeamSlot(
                task_id=task_id,
                lead=lead,
                members=normalized,
                workspace_id=str(workspace_id or ""),
            )
            self._teams[task_id] = slot
            self._by_task[task_id] = task_id
            return True, "", slot

    def bind_task(self, slot_id: str, task_id: str) -> bool:
        """
        把预占槽位（例如 request_id）绑定到真实 Task。

        🔒 这不允许换人；只更新引用。
        """
        with self._lock:
            slot = self._teams.get(str(slot_id))
            if slot is None:
                return False
            self._by_task.pop(slot.task_id, None)
            slot.task_id = str(task_id)
            self._by_task[slot.task_id] = str(slot_id)
            return True

    def release(self, slot_or_task_id: str) -> bool:
        with self._lock:
            key = str(slot_or_task_id)
            slot = self._teams.pop(key, None)
            if slot is None:
                key = self._by_task.get(key, "")
                slot = self._teams.pop(key, None)
            if slot is None:
                return False
            self._by_task.pop(slot.task_id, None)
            for aid in slot.agents:
                self.availability.release(aid)
            return True

    def get(self, task_id: str) -> Optional[TeamSlot]:
        with self._lock:
            slot = self._teams.get(str(task_id))
            if slot is not None:
                return slot
            slot_id = self._by_task.get(str(task_id))
            return self._teams.get(slot_id) if slot_id else None

    def active(self) -> List[TeamSlot]:
        with self._lock:
            return list(self._teams.values())

    def active_count(self) -> int:
        with self._lock:
            return len(self._teams)
