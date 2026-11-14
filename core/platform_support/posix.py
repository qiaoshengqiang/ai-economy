"""
PosixAdapter —— macOS / Linux 的现有行为
==========================================

🔒 这一版只做**行为搬家**：把原本散落在
   `tool_runtime/runner.py`、`work/project_lock.py`、
   `work/task_snapshot.py`、`work/settings_admin.py` 的
   POSIX 调用集中到这里，语义保持不变。

    macOS 与 Linux 共用这套实现；macOS 特有的差异目前不存在。
"""

from __future__ import annotations

import fcntl
import os
import signal
from typing import Any, Dict

from core.platform_support.base import PlatformAdapter


class PosixAdapter(PlatformAdapter):
    name = "posix"

    # ------------------------------------------------------------
    # 进程
    # ------------------------------------------------------------
    def spawn_kwargs(self) -> Dict[str, Any]:
        #: 独立会话/进程组：超时可以整组终止，避免子进程残留。
        return {"start_new_session": True}

    def kill_tree(self, proc: Any) -> None:
        """杀掉整个进程组（SIGKILL）。

        调用方负责兜底：进程组不可用时退回 `proc.kill()`。
        """
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    # ------------------------------------------------------------
    # 文件锁
    # ------------------------------------------------------------
    def file_lock(self, handle: Any, *, blocking: bool = True) -> None:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), flags)

    def file_unlock(self, handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------
    # 文件系统
    # ------------------------------------------------------------
    def create_symlink(self, target: Any, link: Any) -> None:
        os.symlink(target, link)

    def chmod(self, path: Any, mode: int) -> None:
        os.chmod(path, mode)


__all__ = ["PosixAdapter"]
