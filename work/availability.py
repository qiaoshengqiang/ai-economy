"""
AgentAvailability —— IDLE / WORKING
====================================

Life 层负责“谁可以被邀请”；Work 层只消费这个事实。

    IDLE     可以被邀请 / 可以开始新 Task
    WORKING  已经在某个 Task 中，不能接受新邀请

🔒 一个 Agent 同时只能属于一个 Work Task。
   接受邀请必须是**原子**的：第一个成功，其余失败。
"""

from __future__ import annotations

import threading
from typing import Dict, List


class AgentAvailability:
    """进程内可用状态注册表（并发安全）。"""

    IDLE = "IDLE"
    WORKING = "WORKING"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: Dict[str, str] = {}

    def register(self, agent_id: str) -> None:
        with self._lock:
            self._state.setdefault(str(agent_id), self.IDLE)

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            self._state.pop(str(agent_id), None)

    def state(self, agent_id: str) -> str:
        with self._lock:
            return self._state.get(str(agent_id), self.IDLE)

    def is_idle(self, agent_id: str) -> bool:
        return self.state(agent_id) == self.IDLE

    def try_acquire(self, agent_id: str) -> bool:
        """
        尝试把 Agent 从 IDLE 变为 WORKING。

        返回 True 表示成功；False 表示已经在 WORKING（不能接受邀请）。
        """
        with self._lock:
            key = str(agent_id)
            if self._state.get(key, self.IDLE) != self.IDLE:
                return False
            self._state[key] = self.WORKING
            return True

    def release(self, agent_id: str) -> bool:
        """把 Agent 放回 IDLE。返回是否真的从 WORKING 释放。"""
        with self._lock:
            key = str(agent_id)
            if self._state.get(key, self.IDLE) != self.WORKING:
                return False
            self._state[key] = self.IDLE
            return True

    def reset(self) -> None:
        with self._lock:
            self._state.clear()

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._state)

    def idle_agents(self) -> List[str]:
        with self._lock:
            return sorted(
                aid for aid, state in self._state.items()
                if state == self.IDLE)
