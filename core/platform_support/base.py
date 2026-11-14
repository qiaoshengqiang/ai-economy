"""
PlatformAdapter —— 平台能力的抽象接口
=======================================

【设计原则】

    接口按**能力**命名，不按操作系统命名：

        ✅ platform.kill_tree(proc)
        ❌ if platform.is_windows: ...

    这样业务代码不需要知道当前是什么系统，只需要知道
    「我要杀掉这棵进程树」。

【失败语义】

    🔒 **缺少能力时显式失败，不静默降级。**

    Windows 骨架里尚未实现的方法会抛 `PlatformCapabilityError`。
    这比「悄悄跳过加锁」「只杀父进程」安全：
    前者会让两个 Task 同时改同一个项目，后者会留下孤儿进程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class PlatformCapabilityError(RuntimeError):
    """当前平台缺少某项能力，且没有安全的降级路径。"""


class PlatformNotSupportedError(RuntimeError):
    """当前操作系统没有对应的适配器。"""


class PlatformAdapter(ABC):
    """宿主平台能力接口。

    实现者：`PosixAdapter`（macOS / Linux）、`WindowsAdapter`（骨架）。
    """

    #: 适配器名称，用于启动日志（posix / windows）。
    name: str = "unknown"

    def missing_capabilities(self) -> List[str]:
        """返回尚未实现的能力；启动时打印，便于发现半成品适配。"""
        return []

    # ------------------------------------------------------------
    # 进程
    # ------------------------------------------------------------
    @abstractmethod
    def spawn_kwargs(self) -> Dict[str, Any]:
        """传给 `subprocess.Popen(**kwargs)` 的平台参数。

        POSIX: `start_new_session=True`（独立进程组，便于整组终止）
        Windows: `creationflags=CREATE_NEW_PROCESS_GROUP`
        """

    @abstractmethod
    def kill_tree(self, proc: Any) -> None:
        """终止 `Popen` 及其所有子进程。

        Raises:
            PlatformCapabilityError: 当前平台尚未实现。
        """

    # ------------------------------------------------------------
    # 文件锁
    # ------------------------------------------------------------
    @abstractmethod
    def file_lock(self, handle: Any, *, blocking: bool = True) -> None:
        """对已打开的文件句柄加排他锁。

        Args:
            handle: `open(..., "a+")` 返回的句柄。
            blocking: True 阻塞等待；False 拿不到锁时抛 `BlockingIOError`。

        Raises:
            BlockingIOError: `blocking=False` 且锁已被占用。
            PlatformCapabilityError: 当前平台尚未实现。
        """

    @abstractmethod
    def file_unlock(self, handle: Any) -> None:
        """释放 `file_lock` 持有的锁。必须与 `file_lock` 配对。"""

    # ------------------------------------------------------------
    # 文件系统
    # ------------------------------------------------------------
    @abstractmethod
    def create_symlink(self, target: Any, link: Any) -> None:
        """创建符号链接 `link -> target`。

        Raises:
            PlatformCapabilityError: 当前平台没有可用实现（例如
                Windows 未开启开发者模式）。
        """

    @abstractmethod
    def chmod(self, path: Any, mode: int) -> None:
        """设置 POSIX 权限位。

        这是**尽力而为**的操作：Windows 没有等价的 mode 语义，
        权限由 ACL 管理；调用方不得把它当作安全边界。
        """
