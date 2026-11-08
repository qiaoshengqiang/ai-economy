"""
同步桥 + 信箱 drain
====================

Runtime 的 CommunicationManager 是 async 的；DemandRunner 是同步的。
这里只做两件事：

    SyncBridge.run(coro)   —— 把 async 调用接到同步驱动器
    drain(comm, bridge, agent_id) —— 取空一个 Agent 的信箱

🔒 不新增消息语义；与 experiments/novel_production/channels.py 同一种形状。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

RECEIVE_TIMEOUT = 0.05
MAX_DRAIN = 50


class SyncBridge:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()

    def run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:                                     # noqa: BLE001
            pass


def drain(comm: Any, bridge: SyncBridge, agent_id: str) -> Dict[str, Any]:
    """取空一个 Agent 的信箱；返回 wakeup / received / duplicates。"""
    was_woken = False
    try:
        was_woken = bool(comm.is_woken(agent_id))
    except Exception:                                         # noqa: BLE001
        pass
    try:
        comm.clear_wakeup(agent_id)
    except Exception:                                         # noqa: BLE001
        pass

    received: List[Any] = []
    duplicates = 0
    while len(received) < MAX_DRAIN:
        try:
            if comm.pending(agent_id) <= 0:
                break
            inbound = bridge.run(comm.receive(agent_id, timeout=RECEIVE_TIMEOUT))
        except Exception:                                     # noqa: BLE001
            break
        if inbound is None or inbound.message is None:
            break
        if inbound.is_duplicate:
            duplicates += 1
            continue
        if not inbound.delivered:
            break
        received.append(inbound.message)
    return {
        "wakeup": was_woken,
        "received": received,
        "duplicates": duplicates,
        "empty": not received,
    }
