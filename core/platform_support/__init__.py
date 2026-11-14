"""
Platform Support —— 平台能力的唯一边界
=========================================

【为什么需要这一层】

    项目目前默认跑在 macOS 上。Windows 适配不应该散落成
    `if os.name == "nt"`；那会让业务代码同时承担两套行为，
    也让「哪里需要适配」变得不可见。

    这一层只回答一个问题：

        当前平台**怎么完成**这几件宿主机相关的事？

        · 启动子进程 / 杀掉整棵进程树
        · 对文件加互斥锁
        · 创建符号链接
        · 设置文件权限

【边界】

    ✅ 业务代码只调用 `PlatformAdapter` 的方法
    ✅ 平台判断只发生在这里（`select_platform`）
    ❌ 不在业务代码里 `import fcntl` / `os.killpg` / `sys.platform`
    ❌ 不改变 Task / Ledger / 协议 / 数据库 schema

    `tests/test_platform_guards.py` 会扫描生产代码，
    确保平台相关 API 只出现在本包内。
"""

from __future__ import annotations

import platform as _platform
from functools import lru_cache
from typing import Optional

from core.platform_support.base import (
    PlatformAdapter,
    PlatformCapabilityError,
    PlatformNotSupportedError,
)

#: 已确认的 POSIX 平台。Linux 仍在验证中，但共用同一套实现。
_POSIX_SYSTEMS = {"darwin", "linux"}
#: Windows 的几种常见写法（`platform.system()` 返回 "Windows"）。
_WINDOWS_SYSTEMS = {"windows", "win32", "nt"}


def select_platform(system: Optional[str] = None) -> PlatformAdapter:
    """按操作系统选择适配器。**只在启动/测试时调用一次。**

    Args:
        system: 覆盖检测结果，主要用于测试。默认读 `platform.system()`。

    Raises:
        PlatformNotSupportedError: 当前系统没有适配器。
    """
    detected = str(system or _platform.system() or "").strip()
    normalized = detected.lower()
    if normalized in _POSIX_SYSTEMS:
        # 延迟导入：Windows 上没有 fcntl，导入 posix 模块会失败。
        from core.platform_support.posix import PosixAdapter

        return PosixAdapter()
    if normalized in _WINDOWS_SYSTEMS:
        from core.platform_support.windows import WindowsAdapter

        return WindowsAdapter()
    raise PlatformNotSupportedError(
        f"暂不支持当前平台：{detected or '未知'}。"
        "已支持 macOS / Linux（POSIX），Windows 适配骨架见 "
        "docs/architecture/PLATFORM_COMPATIBILITY.md §0。"
    )


@lru_cache(maxsize=1)
def default_platform() -> PlatformAdapter:
    """给未显式注入适配器的调用方使用。

    🔒 进程内只选择一次：适配器无状态，且平台不会在运行中改变。
    """
    return select_platform()


__all__ = [
    "PlatformAdapter",
    "PlatformCapabilityError",
    "PlatformNotSupportedError",
    "default_platform",
    "select_platform",
]
