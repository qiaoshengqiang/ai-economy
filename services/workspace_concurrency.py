"""
资源级互斥 —— **不是全局串行**
============================================================

【🔒 为什么不能用一把全局锁（任务书 §37）】

    最省事的实现是：

        with GLOBAL_GATEWAY_LOCK:
            ...

    但它会让**所有** Workspace 的所有操作排队 ——
    与「Gateway 是并发安全的，但不是全局串行的」直接冲突。

【本文件提供的三件事】

    ① **资源级锁**：`(workspace_id, resource_id)` → 一把锁
       同一 File 串行；不同 File 并行；不同 Workspace 天然并行。

    ② **命名空间锁**：目录结构操作（create / rename / move / delete
       目录）共用**父目录**的锁 —— 保证 Namespace 一致，
       不出现「两个 create 交叉写坏目录」。

    ③ **多资源有序获取**：move 要同时锁 source parent 与 dest parent，
       必须**按固定顺序**获取，否则两个相反的 move 会死锁。

【与 SQLite 的关系】

    🔒 **这两层解决的是不同问题，都需要：**

        资源级锁   → 在进入事务**之前**就把同资源的竞争者排好队
        SQLite 事务 → 保证「检查 + 修改」对**跨资源**的约束也原子

    ⚠️ **底层瓶颈要如实说**：SQLite 是单写者，
       所以不同 Workspace 的**写**在底层仍会排队。
       这是**基础设施瓶颈**，不是 Workspace 语义 ——
       它**不会**让 W2 得到 `LOCKED_BY_OTHER_AGENT`。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class ResourceLockRegistry:
    """
    进程内的资源级锁表。

    【为什么不用 `threading.Lock` 直接做键值】

        `RLock` 允许同一线程重入 —— Gateway 的某些操作
        （例如 move 同时锁 source 与 dest）可能在同一线程里
        重复触碰同一把锁。用普通 `Lock` 会自死锁。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.RLock] = {}

    # ----------------------------------------------------------
    def _lock_for(self, key: tuple[str, str]) -> threading.RLock:
        # 双检：先无锁读，没有再上锁建 —— 减少 _guard 争用
        existing = self._locks.get(key)
        if existing is not None:
            return existing
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())

    # ----------------------------------------------------------
    @contextmanager
    def hold(self, workspace_id: str, *resource_keys: str) -> Iterator[None]:
        """
        获取一个或多个资源的锁。

        🔒 **多个 key 会先排序再获取** ——
           这是死锁防护：两个相反的 move（A→B 与 B→A）
           都按同样的顺序拿锁，就不会互相等待。
        """
        keys = sorted({k for k in resource_keys if k})
        locks = [self._lock_for((workspace_id, k)) for k in keys]

        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            # 逆序释放
            for lock in reversed(locks):
                lock.release()

    # ----------------------------------------------------------
    @contextmanager
    def hold_namespace(self, workspace_id: str, directory_id: str) -> Iterator[None]:
        """
        获取**目录命名空间**的锁。

        用于 create / rename / move / delete(目录) ——
        这些操作改的是「父目录里有什么」，不是某个文件的内容。
        """
        with self.hold(workspace_id, f"NS:{directory_id}"):
            yield

    # ----------------------------------------------------------
    def tracked_locks(self) -> int:
        """当前登记了多少把锁（诊断 / 测试用）。"""
        return len(self._locks)
