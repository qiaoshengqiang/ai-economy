"""
Worktree 领域模型
============================================================

【本文件与 `core/workspace_core/` 的关系】

    🔒 **没有关系 —— 这是刻意的。**

        core/workspace_core/  = 逻辑协作空间（Resource / Revision / Lock）
        change/worktree.py    = Git materialization（worktree 生命周期）

    两边**不互相 import**。
    「Workspace 存在吗」是通过**注入的回调**问的（见 §4 的边界原则），
    不是通过 import。

【🔒 两个 provenance 必须分开存】

        workspace_revision  ← Workspace 的**逻辑**状态版本
        base_commit         ← Git 的 commit sha

    它们**几乎总是**不一致，而且**必须允许**不一致。
    合并成一个字段会让 Patch 阶段的 applicability 无法正确判定
    （ADR-0033 §三）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


def new_worktree_id() -> str:
    """稳定的 worktree 身份。"""
    return f"WT-{uuid.uuid4().hex[:12]}"


def new_intent_id() -> str:
    """稳定的 intent 身份（§9：intent 必须拥有稳定 identity）。"""
    return f"INT-{uuid.uuid4().hex[:12]}"


class WorktreeError(RuntimeError):
    """Worktree 层的错误基类。"""


class WorktreeState(str, Enum):
    """
    生命周期（设计文档 §4.1）。

        UNPROVISIONED → PROVISIONING → READY → ACTIVE → FROZEN → ARCHIVED

    🔒 **与 Workspace lifecycle 是两套独立状态机。**

        Workspace = COMPLETED  ≠  Worktree = deleted
        （Task 完成了，worktree 可能还需要保留供审查）

    另外有三个**不一致状态** —— 它们不是"流程中的一步"，
    而是 **reconciliation 的结论**：

        PROVISION_FAILED  Git 侧失败了（明确失败，不是不一致）
        INCONSISTENT      DB 说活着，Git 说没有（**不能假装 READY 仍正确**）
        ORPHANED          Git 里有，DB 里没有（**不能静默当合法**）
    """

    UNPROVISIONED = "unprovisioned"
    PROVISIONING = "provisioning"
    READY = "ready"
    ACTIVE = "active"
    FROZEN = "frozen"
    ARCHIVED = "archived"

    # ---- 不一致 / 失败（reconciliation 的结论）----
    PROVISION_FAILED = "provision_failed"
    INCONSISTENT = "inconsistent"
    ORPHANED = "orphaned"


#: 🔒 **「活着」的状态集合 —— 也是「同一 Workspace 只能有一个」的那个集合。**
#:
#:     `storage/models.py` 里有一个**部分唯一索引**正是建在这三个值上。
#:     并发 create 的正确性**由数据库保证**，
#:     不依赖「先查再插」那种非原子逻辑（任务书 §17）。
LIVE_STATES: frozenset = frozenset({
    WorktreeState.PROVISIONING,
    WorktreeState.READY,
    WorktreeState.ACTIVE,
})

#: 终态：不再变化（除非显式 reconciliation 修正）。
TERMINAL_STATES: frozenset = frozenset({
    WorktreeState.ARCHIVED,
    WorktreeState.PROVISION_FAILED,
})

#: 不一致状态 —— 需要人/治理介入，**不自动修复**。
INCONSISTENT_STATES: frozenset = frozenset({
    WorktreeState.INCONSISTENT,
    WorktreeState.ORPHANED,
})


class IntentKind(str, Enum):
    """
    Intent 的种类（§9 Intent Log）。

    `RECONCILE` 也是一种 intent —— 因为 reconciliation 本身
    可能被 crash 打断，也需要可恢复性。
    """

    PROVISION = "provision"
    FREEZE = "freeze"
    ARCHIVE = "archive"
    RECONCILE = "reconcile"


class IntentState(str, Enum):
    """Intent 自己的生命周期。"""

    PENDING = "pending"          # 已记录「准备做什么」，副作用还没做完
    COMPLETED = "completed"      # 副作用完成 + DB 已确认
    FAILED = "failed"            # 副作用明确失败（可重试）
    ABANDONED = "abandoned"      # 放弃（例如 workspace 不存在了）


class InformationalState(str, Enum):
    """
    `reconcile()` 的**结论** —— 不做任何修改，只报告。

    🔒 这是「只恢复 Change Pipeline 状态，不改 Workspace Responsibility」
       的接口体现：reconcile 返回结论，**由调用方决定怎么办**。
    """

    CONSISTENT = "consistent"                  # DB 与 Git 一致
    DB_PROVISIONING_GIT_EXISTS = "db_provisioning_git_exists"
    DB_READY_GIT_MISSING = "db_ready_git_missing"
    DB_NONE_GIT_EXISTS = "db_none_git_exists"
    NO_WORKTREE = "no_worktree"
    UNRESOLVED_INTENT = "unresolved_intent"


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """
    「Workspace 现在长什么样」的**只读快照** —— 由**注入的回调**提供。

    🔒 这个类存在的意义是**依赖倒置**：
       `change/` 不知道 `core.workspace_store` 的存在，
       它只知道「有人能告诉我 workspace 的 revision」。
       与 `WorkspaceGateway` 用注入的 `is_participant` 是同一种手法。
    """

    workspace_id: str
    revision: int
    exists: bool = True


@dataclass(frozen=True)
class WorktreeRecord:
    """
    一条 worktree 记录（对应 `git_worktrees` 表的一行）。

    🔒 **`physical_path` 是内部字段。**
       任何 Agent-facing 的返回都会把它剥掉
       （见 `WorktreeManager` 的 `_public()`）。
    """

    worktree_id: str
    workspace_id: str
    task_id: Optional[str]
    repository_ref: str          # 仓库的逻辑标识（**不是路径**）
    base_commit: str             # 🔒 Git 侧基线
    workspace_revision: int      # 🔒 Workspace 侧基线（与上面**不是一回事**）
    git_ref: Optional[str]       # refs/heads/... （detached 时为 None）
    state: WorktreeState

    # ---- 内部字段：绝不进 Agent-facing 接口 ----
    physical_path: Optional[str]

    created_at: str
    updated_at: str
    last_verified_at: Optional[str] = None
    detail: Optional[dict[str, Any]] = None

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def is_inconsistent(self) -> bool:
        return self.state in INCONSISTENT_STATES


@dataclass(frozen=True)
class WorktreeIntent:
    """
    一条 intent（§9）。

    🔒 **`intent_id` 是稳定身份，`idempotency_key` 是幂等的锚点。**

        同一个逻辑操作重试 → 同一个 `idempotency_key`
        → DB 唯一约束挡住第二次
        → **不会**创建第二个 Git worktree。
    """

    intent_id: str
    workspace_id: str
    kind: IntentKind
    state: IntentState
    idempotency_key: str
    operation_id: str
    worktree_id: Optional[str]
    payload: Optional[dict[str, Any]]
    created_at: str
    updated_at: str
    last_error: Optional[str] = None


class WorktreeEventType(str, Enum):
    """
    Worktree 自己的事件类型（任务书 §25）。

    🔒 **不要把这些硬塞进 Workspace Resource Event。**

        Workspace Event 回答「逻辑资源状态变了什么」
        Worktree Event 回答「Git 侧的物化状态变了什么」

        订阅方不同：
            Workspace Event → Task 参与者 / Agent
            Worktree Event  → Change Pipeline / CI / 未来的 Patch 阶段
    """

    WORKTREE_CREATED = "WORKTREE_CREATED"
    WORKTREE_FROZEN = "WORKTREE_FROZEN"
    WORKTREE_ARCHIVED = "WORKTREE_ARCHIVED"
    WORKTREE_RECONCILED = "WORKTREE_RECONCILED"
    WORKTREE_INCONSISTENT = "WORKTREE_INCONSISTENT"
    WORKTREE_ORPHANED = "WORKTREE_ORPHANED"
    WORKTREE_PROVISION_FAILED = "WORKTREE_PROVISION_FAILED"


class WorktreeErrorCode(str, Enum):
    """
    结构化错误码 —— **不要让调用方解析自然语言**（与 WorkspaceCore 同一条原则）。

    ⚠️ 故意**不放进** `core/workspace_core.operation.ErrorCode`：
       那是 Workspace Core 的封闭集合，加进去就修改了冻结层的语义。
       但**复用** `OperationResult` 这个统一结果类型 ——
       它接受任何有 `.value` 的枚举。
    """

    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKTREE_NOT_FOUND = "WORKTREE_NOT_FOUND"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"                 # 已有 live worktree
    NOT_A_REPOSITORY = "NOT_A_REPOSITORY"             # Git 侧前置条件不满足
    BASE_REF_NOT_FOUND = "BASE_REF_NOT_FOUND"
    PROVISION_FAILED = "PROVISION_FAILED"             # git worktree add 失败
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"   # §12 触发方式 2
    INVALID_STATE = "INVALID_STATE"                   # 生命周期不允许
    INCONSISTENT = "INCONSISTENT"
    DUPLICATE_OPERATION = "DUPLICATE_OPERATION"
