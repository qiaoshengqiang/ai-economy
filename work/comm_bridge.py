"""同步控制面调用异步 CommunicationManager 的桥。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def run_coro(coro: Any) -> Any:
    """在同步线程里执行 CommunicationManager 的协程。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # 调用方本身在事件循环里：换一个线程 + 新 loop 执行，避免嵌套。
    result = []
    error = []

    def _target() -> None:
        try:
            result.append(asyncio.run(coro))
        except Exception as exc:                              # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None
