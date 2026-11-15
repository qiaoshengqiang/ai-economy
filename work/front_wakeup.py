"""Front 的 WakeupHandler。

🔒 这里只把 WakeupEvent 交给 controller 的既有 Front 执行路径；
   不自己调用 Brain，不创建 Task。
"""

from __future__ import annotations

from typing import Any, Callable

from core.alarm import WakeupEvent, WakeupResult


class FrontWakeupHandler:
    def __init__(self, wake: Callable[[WakeupEvent], WakeupResult]) -> None:
        self._wake = wake

    def can_wake(self, agent_id: str) -> bool:
        return True

    def wake(self, event: WakeupEvent) -> WakeupResult:
        return self._wake(event)


__all__ = ["FrontWakeupHandler"]
