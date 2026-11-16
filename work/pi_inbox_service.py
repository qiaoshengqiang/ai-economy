"""Pi Worker 的持久 Inbox Service。

Phase 2：消息不再只在 Pi 任务运行期间被消费。
每个启用的 Pi 引擎 Agent 都有一个常驻 `PiInboxBridge`：

    CommunicationManager
      → PiInboxBridge（常驻线程）
      → agents/<agent_id>/inbox/pi-inbox.md
      → Pi 下一次 phase / turn 读取

🔒 这里不启动 Front / 内置引擎的收件箱消费者。
   Front Inbox-first 属于 Phase 3。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.paths import AGENTS_DIR
from work.pi_comm import PiInboxBridge

logger = logging.getLogger(__name__)


class PiInboxService:
    """管理所有 Pi 引擎 Agent 的常驻 Inbox Bridge。"""

    def __init__(
        self,
        communication: Any,
        *,
        agents_dir: Optional[Path] = None,
    ) -> None:
        self.communication = communication
        self.agents_dir = Path(agents_dir or AGENTS_DIR)
        self._bridges: Dict[str, PiInboxBridge] = {}

    # ------------------------------------------------------------
    def _inbox_dir(self, agent_id: str) -> Path:
        return self.agents_dir / str(agent_id) / "inbox"

    def _set_enabled(self, agent_id: str, enabled: bool) -> None:
        recorder = getattr(self.communication, "recorder", None)
        if recorder is None:
            return
        try:
            recorder.set_inbox_enabled(agent_id, enabled)
        except Exception:                                     # noqa: BLE001
            logger.warning(
                "Pi inbox state 更新失败：%s", agent_id, exc_info=True)

    # ------------------------------------------------------------
    def ensure(self, agent_id: str) -> bool:
        """确保某个 Pi 引擎 Agent 的常驻 bridge 正在运行。"""
        key = str(agent_id or "").strip()
        if not key or self.communication is None:
            return False

        current = self._bridges.get(key)
        if current is not None and getattr(current, "_running", False):
            return True
        if current is not None:
            self.stop(key, update_state=False)

        bridge = PiInboxBridge(
            self.communication,
            agent_id=key,
            inbox_dir=self._inbox_dir(key),
        )
        # 先建立 cutover，再启动消费；避免启动瞬间的消息被误判为 legacy。
        self._set_enabled(key, True)
        try:
            started = bridge.start()
        except Exception:                                     # noqa: BLE001
            logger.warning("Pi inbox 启动失败：%s", key, exc_info=True)
            self._set_enabled(key, False)
            return False
        if not started:
            self._set_enabled(key, False)
            return False
        self._bridges[key] = bridge
        return True

    def stop(self, agent_id: str, *, update_state: bool = True) -> None:
        key = str(agent_id or "").strip()
        bridge = self._bridges.pop(key, None)
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:                                 # noqa: BLE001
                logger.warning(
                    "Pi inbox 停止失败：%s", key, exc_info=True)
        if update_state:
            self._set_enabled(key, False)

    def sync(self, agent_ids: Iterable[str]) -> None:
        """按当前 Pi 引擎 Agent 列表增删常驻 bridge。"""
        wanted = {
            str(item).strip() for item in (agent_ids or ())
            if str(item).strip()
        }
        for key in sorted(set(self._bridges) - wanted):
            self.stop(key)
        for key in sorted(wanted):
            self.ensure(key)

    def stop_all(self) -> None:
        for key in sorted(self._bridges):
            self.stop(key)

    def bridge_for(self, agent_id: str) -> Optional[PiInboxBridge]:
        key = str(agent_id or "").strip()
        bridge = self._bridges.get(key)
        if bridge is None or not getattr(bridge, "_running", False):
            return None
        return bridge

    @property
    def active_agents(self) -> list[str]:
        return sorted(self._bridges)


__all__ = ["PiInboxService"]
