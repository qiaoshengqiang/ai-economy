"""
Workspace Core —— 领域模型（Step 3 / Step 4）
============================================================

【这个包是什么】

    Task Workspace 的**纯领域层**。它不访问数据库，不知道物理路径，
    也不做任何 I/O —— 只表达「资源、责任、版本」的语义。

【为什么单独一个包】

    Workspace Core 的**可测试部分**必须能脱离 storage 单独测。
    尤其 `effective_lock` / `effective_controller` 是**纯函数**，
    它们的正确性不应该依赖「先建好数据库」。

【与既有模块的关系】

    core/task_workspace.py   TaskWorkspaceRef —— 一个 workspace_id 字符串（无 I/O）
    core/workspace.py        Agent **Private** Workspace（有真实 I/O）
    core/workspace_core/     本包 —— Task Workspace 的领域模型
    core/workspace_store.py  本包的持久化（唯一边界）
    services/workspace_gateway.py   唯一受控入口

【本包的硬约束】

    🔒 只有 `ExplicitLock` 是持久化事实。
       `effective_lock` / `effective_controller` **永远派生**，
       **没有表、没有列、没有缓存**（ADR-0027 / 任务书 §48）。
"""

from core.workspace_core.resource import (
    Resource,
    ResourceState,
    ResourceType,
    WorkspacePathError,
    normalize_path,
)
from core.workspace_core.lock import (
    ExplicitLock,
    LockKind,
    ResourceTree,
    resolve_effective_controller,
    resolve_effective_lock,
)
from core.workspace_core.operation import (
    ErrorCode,
    OperationResult,
    new_operation_id,
)
from core.workspace_core.events import (
    AuditAction,
    AuditResult,
    WorkspaceEventType,
)

__all__ = [
    "Resource",
    "ResourceState",
    "ResourceType",
    "WorkspacePathError",
    "normalize_path",
    "ExplicitLock",
    "LockKind",
    "ResourceTree",
    "resolve_effective_lock",
    "resolve_effective_controller",
    "ErrorCode",
    "OperationResult",
    "new_operation_id",
    "AuditAction",
    "AuditResult",
    "WorkspaceEventType",
]
