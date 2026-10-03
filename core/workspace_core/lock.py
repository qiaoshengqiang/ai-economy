"""
Lock 与责任解析 —— **Effective Lock Resolution**（Step 4）
============================================================

【本文件是整个 Workspace Core 里最关键、也最容易写错的部分】

    它是**纯函数**。不访问数据库、不读物理文件、没有副作用。
    给定「一批 Resource + 一批 ExplicitLock」，它回答：

        这个资源**现在**谁负责？
        这个目录**长期**谁负责？

【四条规则（任务书 §10 · LOCK_MODEL.md §5.1）】

    Rule 1  资源自身有 Explicit Lock      → Effective = 它
    Rule 2  自身没有                      → 沿 parent 链向上找
    Rule 3  找到最近的显式 Directory Lock → Effective = 它
    Rule 4  更近的胜出                    → 由「从近到远」的遍历顺序保证

【🔒 两条不可违反的约束】

    ① **Explicit Lock 是唯一的持久化事实。**
       `effective_lock` / `effective_controller` 永远**派生** ——
       没有表、没有列、没有缓存（任务书 §48 / ADR-0027）。

    ② **File 不需要 Controller。**
       Controller 的最小粒度是 **Directory**（任务书 §6）。
       File 的 `effective_controller` 取自它所在目录链 ——
       这满足 `list()` 要返回 `effective_controller` 的需求，
       但**不代表 File 拥有自己的 Controller**。

【一个由「设计简化」带来的真实结论】

    ADR-0027 移除了 Authorization 之后：

        对一个 **Directory** 而言，
        **Controller 与 Directory Lock 的持有者是同一个人**
        —— 它们由**同一条** explicit lock 记录派生。

    在那个被移除的旧模型里它们本可以不同
    （controller=B，但 write lock=C）。移除权限系统后，
    「谁负责这个目录」与「谁承担这个目录的修改责任」
    已经没有第二个概念可以分开了。

    这**不是**我们新加的设计，而是 §13 的行为
    （Directory Handoff → Controller = to **且** Directory Lock = to）
    在简化后的自然结果。已在报告中记为设计说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Optional, Sequence

from core.workspace_core.resource import (
    Resource,
    ResourceType,
    normalize_path,
)


class LockKind(str, Enum):
    """
    显式锁的种类。

    🔒 **File Lock 与 Directory Lock 语义不同**（任务书 §8 / §9）：

        DIRECTORY  → Namespace / Structure 的修改责任
                     （CREATE / DELETE / RENAME / MOVE / 子目录）
        FILE       → 文件**内容**的独占修改责任

        Directory Lock **不等于**独占目录内所有文件。
    """

    DIRECTORY = "directory"
    FILE = "file"


@dataclass(frozen=True)
class ExplicitLock:
    """
    **唯一持久化的责任事实。**

    一个 resource 最多有一条（`resource_id` 为主键）。

    🔒 创建资源时**不写**这张记录（任务书 §11）——
       新资源隐式继承父目录的责任，重复记录同一责任是冗余。
    """

    resource_id: str
    holder: str
    kind: LockKind
    acquired_at: str
    acquired_by: str

    def __repr__(self) -> str:  # pragma: no cover
        return f"ExplicitLock({self.resource_id!r}, {self.kind.value}, {self.holder!r})"


# ============================================================
# 单资源解析（Rule 1–4 的直译）
# ============================================================
def resolve_effective_lock(
    resource: Resource,
    explicit_by_resource: Mapping[str, ExplicitLock],
) -> Optional[str]:
    """
    Rule 1 + Rule 2/3 —— 只解析**自己**这一层。

    ⚠️ 这个函数**不向上遍历**；遍历由 `ResourceTree` 负责。
       这样「一层」的逻辑可以被单独测试。

    返回：
        · 自身有 Explicit Lock      → 它的 holder
        · 自身没有                   → None（调用方继续向上）
    """
    own = explicit_by_resource.get(resource.resource_id)
    return own.holder if own is not None else None


def resolve_effective_controller(
    resource: Resource,
    explicit_by_resource: Mapping[str, ExplicitLock],
) -> Optional[str]:
    """
    Controller 的**单层**解析。

    🔒 **File 自己没有 Controller** —— 对 File 永远返回 None，
       调用方必须继续向父目录链找。

    对 Directory：Controller 与 Directory Lock 同源（见模块 docstring）。
    """
    if resource.resource_type is ResourceType.FILE:
        return None
    return resolve_effective_lock(resource, explicit_by_resource)


# ============================================================
# ResourceTree —— 在「已经读出来的数据」上做纯解析
# ============================================================
class ResourceTree:
    """
    一个**只读视图**：把一批 Resource + ExplicitLock 包起来，
    提供路径解析、祖先遍历、Effective Lock / Controller 解析。

    【为什么不直接在 store 里做】

        因为这些逻辑要能被密集单测。
        `ResourceTree` 不碰数据库 —— 测试可以凭空构造资源树，
        不需要建表、不需要事务。

    【为什么每个 mutation 都要重建它】

        它是一次**快照**。Gateway 在同一个写事务里
        读一次资源 → 建 tree → 解析 → 写回。
        这样解析结果与被修改的状态**同源**，
        不会出现「解析用的是旧数据、写的是新数据」。
    """

    def __init__(
        self,
        resources: Iterable[Resource],
        explicit_locks: Iterable[ExplicitLock] = (),
    ) -> None:
        self._by_id: dict[str, Resource] = {r.resource_id: r for r in resources}
        self._by_path: dict[tuple[str, str], Resource] = {
            (r.workspace_id, normalize_path(r.path)): r for r in self._by_id.values()
        }
        self._by_parent: dict[Optional[str], list[Resource]] = {}
        for resource in self._by_id.values():
            self._by_parent.setdefault(resource.parent_id, []).append(resource)

        self._explicit: dict[str, ExplicitLock] = {
            lock.resource_id: lock for lock in explicit_locks
        }

    # ----------------------------------------------------------
    # 基本查询
    # ----------------------------------------------------------
    def get(self, resource_id: str) -> Optional[Resource]:
        return self._by_id.get(resource_id)

    def require(self, resource_id: str) -> Resource:
        resource = self.get(resource_id)
        if resource is None:
            raise KeyError(resource_id)
        return resource

    def by_path(self, workspace_id: str, path: str) -> Optional[Resource]:
        return self._by_path.get((workspace_id, normalize_path(path)))

    def children_of(self, parent_id: Optional[str]) -> Sequence[Resource]:
        return tuple(self._by_parent.get(parent_id, ()))

    def root_of(self, workspace_id: str) -> Optional[Resource]:
        return self.by_path(workspace_id, "/")

    def explicit_lock_of(self, resource_id: str) -> Optional[ExplicitLock]:
        return self._explicit.get(resource_id)

    # ----------------------------------------------------------
    # 祖先遍历（Rule 2/3/4 的实现基础）
    # ----------------------------------------------------------
    def ancestors(self, resource: Resource) -> list[Resource]:
        """
        从**最近**的父到**最远**的祖先。

        🔒 顺序非常重要：Rule 4「更近的胜出」就是靠这个顺序
           + 「找到第一个就返回」实现的。
        """
        chain: list[Resource] = []
        seen: set[str] = {resource.resource_id}
        current = resource

        while current.parent_id is not None:
            parent = self.get(current.parent_id)
            if parent is None or parent.resource_id in seen:
                # 数据破损时不要死循环 —— 明确停住
                break
            seen.add(parent.resource_id)
            chain.append(parent)
            current = parent

        return chain

    def path_of(self, resource: Resource) -> str:
        """用 parent 链拼出逻辑路径（用于验证表里的 path 缓存是否漂移）。"""
        parts: list[str] = []
        seen: set[str] = set()
        current: Optional[Resource] = resource

        while current is not None and current.resource_id not in seen:
            seen.add(current.resource_id)
            if current.name:
                parts.append(current.name)
            current = self.get(current.parent_id) if current.parent_id else None

        parts.reverse()
        return "/" + "/".join(parts) if parts else "/"

    def descendants(self, resource: Resource) -> list[Resource]:
        """整棵子树（不含自己）。move / delete 需要它。"""
        out: list[Resource] = []
        stack = list(self.children_of(resource.resource_id))
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(self.children_of(node.resource_id))
        return out

    # ----------------------------------------------------------
    # 🔒 Effective Lock Resolution（本轮核心）
    # ----------------------------------------------------------
    def effective_lock(self, resource: Resource) -> Optional[str]:
        """
        **Rule 1–4 的完整实现。**

            ① 自身有 Explicit Lock            → 返回它（**Explicit > Inherited**）
            ② 否则沿 parent 链从近到远
            ③ 第一个**显式 Directory Lock**    → 返回它的 holder
            ④ 「更近的胜出」由遍历顺序保证

        ⚠️ 只认 **DIRECTORY** 类的显式锁作为继承来源。
           一个 File 的 Explicit **File** Lock 只对它自己有效，
           **不会**被它的子资源继承（File 本来也没有子资源，
           但这条规则让语义更明确：继承来源必须是 Directory）。
        """
        own = self._explicit.get(resource.resource_id)
        if own is not None:
            return own.holder

        for ancestor in self.ancestors(resource):
            lock = self._explicit.get(ancestor.resource_id)
            if lock is not None and lock.kind is LockKind.DIRECTORY:
                return lock.holder

        return None

    def effective_controller(self, resource: Resource) -> Optional[str]:
        """
        Controller 解析。

        🔒 **File 没有自己的 Controller**（任务书 §6）——
           对 File 直接在父目录链上找。

        对 Directory：与 `effective_lock` 同源。
        """
        if resource.resource_type is ResourceType.DIRECTORY:
            return self.effective_lock(resource)

        for ancestor in self.ancestors(resource):
            if ancestor.resource_type is not ResourceType.DIRECTORY:
                continue
            lock = self._explicit.get(ancestor.resource_id)
            if lock is not None and lock.kind is LockKind.DIRECTORY:
                return lock.holder

        return None

    # ----------------------------------------------------------
    # 责任边界（delete / move / rename 的安全阀）
    # ----------------------------------------------------------
    def boundary_holders(self, resource: Resource) -> dict[str, str]:
        """
        找出子树里**所有 Explicit Lock**（= 已独立建立的责任边界）。

        返回 `{resource_id: holder}`。

        🔒 用于两条硬规则：

            · `delete(directory)` 不能绕过更具体的 Resource Boundary
            · `move` / `rename` 不能动别的 Agent 已经独立负责的资源
        """
        holders: dict[str, str] = {}
        for node in [resource, *self.descendants(resource)]:
            lock = self._explicit.get(node.resource_id)
            if lock is not None:
                holders[node.resource_id] = lock.holder
        return holders

    def foreign_boundaries(
        self, resource: Resource, agent_id: str
    ) -> dict[str, str]:
        """
        子树里**不属于** `agent_id` 的显式责任边界。

        非空 → 该操作会越过别人的责任边界 → 必须拒绝。
        """
        return {
            rid: holder
            for rid, holder in self.boundary_holders(resource).items()
            if holder != agent_id
        }

    def is_within_foreign_boundary(
        self, resource: Resource, agent_id: str
    ) -> Optional[str]:
        """
        这个资源是否落在**别人的**责任边界里？

        返回那个边界的 resource_id，否则 None。

        ============================================================
        【🔒 必须在**找到的第一个**边界处停下 —— 这是一个真实修掉的 bug】
        ============================================================

            第一版写成了「继续向上遍历，找到任何 foreign 就返回」。于是：

                data/             Directory Lock = A
                data/processed/   Directory Lock = B
                data/processed/r.csv

            对 `B` 查询 `r.csv` 时：
                · R2（processed，B 自己的）→ 跳过（是自己的）
                · R1（data，A 的）        → **返回 R1**  ❌

            这是错的：

                **B 自己的边界应该「屏蔽」外层的边界。**

            否则就会出现荒谬的结果 —— B 明明独立负责
            `data/processed/`，却被告知「你在 A 的边界里」，
            于是 B **连自己的文件都动不了**。

        ------------------------------------------------------------
        【正确语义】

            沿 parent 链从近到远，**遇到第一条显式 Directory Lock 就停**：

                · 它是自己的  → 返回 None（被自己的边界保护）
                · 它是别人的  → 返回它（确实越界了）

            这正是「**Explicit 优先于 Inherited**」
            在「边界检查」这件事上的应用 ——
            **更近的边界决定归属，外层的边界被内层的遮住。**
        """
        for ancestor in self.ancestors(resource):
            lock = self._explicit.get(ancestor.resource_id)
            if lock is None or lock.kind is not LockKind.DIRECTORY:
                continue

            # 🔒 第一条显式 Directory Lock —— 无论属于谁，都在这里停
            return None if lock.holder == agent_id else ancestor.resource_id

        return None

    def __len__(self) -> int:
        return len(self._by_id)
