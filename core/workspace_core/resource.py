"""
Resource —— Task Workspace 里的一个目录或文件
============================================================

【最重要的规则】

    🔒 **`resource_id` 是身份；`path` 只是派生出来的位置。**

        rename() / move() 之后：
            resource_id  不变
            path         改变

        所以 Revision / Audit / Lock / Handoff / Agent Message
        都可以引用稳定的 resource_id。

    ❌ **不要把 path 当成 Resource Identity。**

【为什么 path 仍然存在】

    为了让 Agent 能用一个人类可读的路径访问资源
    （`read(workspace_id, "src/main.py")`）。

    它是 `parent_id` + `name` 推导出来的**缓存值**，
    存在表里只是为了 list / 解析方便，不是权威。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ResourceType(str, Enum):
    """第一版只支持两种类型（任务书 §2）。"""

    DIRECTORY = "directory"
    FILE = "file"


class ResourceState(str, Enum):
    """
    🔒 **删除不让 Resource 瞬间消失**（任务书 §21）。

    `DELETED` 之后：
        · resource_id 仍可查询
        · 历史 Revision / Audit / Handoff 仍在

    物理文件何时真正删除，留待 Git / Storage 阶段。
    """

    ACTIVE = "active"
    DELETED = "deleted"


class WorkspacePathError(ValueError):
    """逻辑路径不合法。"""


def normalize_path(path: str) -> str:
    """
    规范化**逻辑路径**。

    规则（刻意保持极简，第一版不需要更复杂的东西）：

        ""      / "."   / "/"        → "/"        （workspace 根）
        "src"                        → "/src"
        "src/"                       → "/src"
        "/src/main.py"               → "/src/main.py"
        "src//main.py"               → "/src/main.py"

    🔒 **拒绝** `..` —— Workspace 是一个封闭的命名空间，
       不允许通过路径逃出去（与 core/workspace.py 的
       `WorkspaceViolationError` 是同一条边界的精神）。

    ⚠️ 这里处理的是**逻辑路径**，不是宿主机物理路径。
       Agent 永远看不到物理路径（任务书 §15）。
    """
    if path is None:
        raise WorkspacePathError("path 不能是 None")

    if not isinstance(path, str):
        raise WorkspacePathError(f"path 必须是字符串，收到 {type(path).__name__}")

    raw = path.strip().replace("\\", "/")

    segments = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise WorkspacePathError(
                "逻辑路径不允许 '..' —— Workspace 是封闭命名空间"
            )
        segments.append(segment)

    return "/" + "/".join(segments)


def join_path(parent_path: str, name: str) -> str:
    """拼一个子路径（用于 create）。"""
    parent = normalize_path(parent_path)
    child = name.strip().strip("/")
    if not child or "/" in child:
        raise WorkspacePathError(f"name 必须是单层名字，收到 {name!r}")
    return normalize_path(f"{parent}/{child}")


def parent_path_of(path: str) -> str:
    """取逻辑父路径（根的父仍是根）。"""
    normalized = normalize_path(path)
    if normalized == "/":
        return "/"
    head, _, _ = normalized.rpartition("/")
    return head or "/"


def basename_of(path: str) -> str:
    """取最后一层名字（根的名字是空串）。"""
    normalized = normalize_path(path)
    if normalized == "/":
        return ""
    return normalized.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Resource:
    """
    一个 Task Workspace 资源（不可变快照）。

    【为什么 frozen】

        领域对象在**同一事务内**读出来就不应该被悄悄改掉。
        修改必须走「构造新对象 → 写回」的路径，
        这样 Revision 递增这件事不会被绕过。
    """

    resource_id: str
    workspace_id: str
    parent_id: Optional[str]
    name: str
    path: str
    resource_type: ResourceType
    state: ResourceState
    revision: int
    content: Optional[str]
    created_by: str
    created_at: str
    updated_at: str

    # ----------------------------------------------------------
    # 便捷判断
    # ----------------------------------------------------------
    @property
    def is_directory(self) -> bool:
        return self.resource_type is ResourceType.DIRECTORY

    @property
    def is_file(self) -> bool:
        return self.resource_type is ResourceType.FILE

    @property
    def is_active(self) -> bool:
        return self.state is ResourceState.ACTIVE

    @property
    def is_deleted(self) -> bool:
        return self.state is ResourceState.DELETED

    @property
    def is_root(self) -> bool:
        """workspace 根目录：没有 parent。"""
        return self.parent_id is None

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"Resource({self.resource_id!r}, {self.path!r}, "
            f"{self.resource_type.value}, R{self.revision}, {self.state.value})"
        )
