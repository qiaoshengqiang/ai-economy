"""
WindowsAdapter —— Windows 适配骨架
====================================

【当前状态：`DESIGNED` / 未实现】

    这不是「Windows 已经能用」，而是把需要补的能力集中到**一个文件**。
    未实现的方法会抛 `PlatformCapabilityError`，启动时会打印缺失项。

【怎么补】

    1. 实现下面标记为 TODO 的方法；
    2. 跑 `tests/test_platform_support.py` 的契约测试；
    3. 跑 `./dev.sh test`，macOS 基线必须仍然全绿；
    4. 更新 `docs/architecture/PLATFORM_COMPATIBILITY.md` 的状态表。

【不要做什么】

    ❌ 不要为了「先跑起来」把 file_lock 改成 no-op
       —— 那会让两个 Task 同时修改同一个项目目录。
    ❌ 不要改 Task / Ledger / 协议 / 数据库 schema。
    ❌ 不要把 Windows 特判写回业务代码。
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List

from core.platform_support.base import (
    PlatformAdapter,
    PlatformCapabilityError,
)

_GUIDE = "docs/architecture/PLATFORM_COMPATIBILITY.md §0"

#: 能力 → 实现提示。启动日志与异常信息都会引用。
_TODO: Dict[str, str] = {
    "kill_tree": "WIN-PROC-1：用 taskkill /F /T /PID 或 Job Object 终止整棵进程树",
    "file_lock": "WIN-LOCK-1：用 msvcrt.locking（或等价实现）保持互斥语义",
    "create_symlink": "WIN-FS-1：os.symlink 在开发者模式下可用；失败时要给出可操作的提示",
}


class WindowsAdapter(PlatformAdapter):
    name = "windows"

    def missing_capabilities(self) -> List[str]:
        return [f"{name}（{todo}）" for name, todo in _TODO.items()]

    # ------------------------------------------------------------
    # 进程
    # ------------------------------------------------------------
    def spawn_kwargs(self) -> Dict[str, Any]:
        #: 让子进程拥有独立进程组，便于后续整组终止。
        #: 该常量只在 Windows 上存在；getattr 的默认值让本模块
        #: 在 macOS/Linux 的测试里也能安全导入。
        return {
            "creationflags": getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
        }

    def kill_tree(self, proc: Any) -> None:
        raise PlatformCapabilityError(
            "Windows 适配尚未实现：kill_tree。" + _TODO["kill_tree"]
            + f"。实现位置：core/platform_support/windows.py；说明：{_GUIDE}。"
        )

    # ------------------------------------------------------------
    # 文件锁
    # ------------------------------------------------------------
    def file_lock(self, handle: Any, *, blocking: bool = True) -> None:
        raise PlatformCapabilityError(
            "Windows 适配尚未实现：file_lock。" + _TODO["file_lock"]
            + "。在此之前不要用 no-op 降级，否则项目锁会失效；"
            + f"说明：{_GUIDE}。"
        )

    def file_unlock(self, handle: Any) -> None:
        #: 与 file_lock 配对。file_lock 未实现时不会持有锁，
        #: 因此这里只能是 no-op；实现 file_lock 时必须同步实现它。
        return None

    # ------------------------------------------------------------
    # 文件系统
    # ------------------------------------------------------------
    def create_symlink(self, target: Any, link: Any) -> None:
        raise PlatformCapabilityError(
            "Windows 适配尚未实现：create_symlink。" + _TODO["create_symlink"]
            + f"。说明：{_GUIDE}。"
        )

    def chmod(self, path: Any, mode: int) -> None:
        #: Windows 没有 POSIX mode；权限由 ACL 管理。
        #: 这是有意的 no-op，不是「待实现」。
        return None


__all__ = ["WindowsAdapter"]
