"""Agent Inbox 的通用 Runtime Consumer。

Phase 3：Front 需要真正消费自己的信箱，而不是 controller 直接拿 UI 文本。

    CommunicationManager.receive(agent_id)
      → handler(message)
      → handler 决定是否提交 Job / 回复 / 忽略

🔒 每个 Agent 只能有一个 Consumer；第二个 Consumer 会替换 handler，
   造成消息分流。启动前由调用方保证互斥。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class InboxConsumer:
    """一个 Agent 的常驻收件箱消费线程。"""

    def __init__(
        self,
        communication: Any,
        *,
        agent_id: str,
        handler: Callable[[Any], Any],
        name: str = "",
    ) -> None:
        self.communication = communication
        self.agent_id = str(agent_id or "").strip()
        self.handler = handler
        self.name = str(name or f"inbox-{self.agent_id}")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._previous_handler: Any = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self.communication is None or not self.agent_id:
            return False
        registry = getattr(self.communication, "registry", None)
        if registry is not None and not registry.exists(self.agent_id):
            logger.warning("Inbox：%s 未接入通信层", self.agent_id)
            return False
        if self._running:
            return True
        self._previous_handler = self.communication.replace_handler(
            self.agent_id, self.handler)
        self._running = True
        self._thread = threading.Thread(
            target=self._thread_main, name=self.name, daemon=True)
        self._thread.start()
        return True

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._poll())
        finally:
            loop.close()

    async def _poll(self) -> None:
        while self._running:
            try:
                await self.communication.receive(
                    self.agent_id, timeout=0.2)
            except asyncio.CancelledError:
                return
            except Exception:                                 # noqa: BLE001
                logger.warning(
                    "Inbox 消费失败：%s", self.agent_id, exc_info=True)
                await asyncio.sleep(0.2)

    def stop(self) -> None:
        self._running = False
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(lambda: None)
            except Exception:                                 # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.communication is not None:
            try:
                self.communication.restore_handler(
                    self.agent_id, self._previous_handler)
            except Exception:                                 # noqa: BLE001
                pass
            self._previous_handler = None
        self._loop = None


__all__ = ["InboxConsumer"]
