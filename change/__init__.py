"""
Change Pipeline —— AI Economy OS 第二阶段
============================================================

【这一层是什么】

    `Git Worktree → Patch → Test → Acceptance → Merge` 的**受控实现**。

【为什么要**单独一层**，而不是塞进 services/ 或 core/】

    这是「**模块边界**」的直接体现（任务书 §4 / §29）：

        Workspace Core  = 逻辑协作空间
                          resource · revision · responsibility · lock
                          participant · audit · event

        Change Pipeline = Git materialization
                          repository · worktree · patch · test
                          acceptance · merge

    它们**必须能各自演化**：
        · 换 Git 实现 → 只动这一层
        · 改 Workspace 语义 → 完全不动这一层

    这与 `communication/` 和 `llm/` 是同一种组织方式 ——
    一个**独立的关注点**就是一个独立的一层。

【本阶段（Phase 1）实现什么】

    ✅ WorktreeManager —— Git worktree 的生命周期、intent、reconciliation

    ❌ Patch · Test Runner · Acceptance · Merge · Execution Lease
       （接口留边界，实现留到后续阶段）

【🔒 一条贯穿本层的约束】

    **Agent 永远拿不到 host physical path。**
    physical path 只能存在于内部结果与日志中。
"""

from change.git import GitCommandError, GitManager
from change.worktree import (
    IntentKind,
    IntentState,
    WorktreeErrorCode,
    WorktreeEventType,
    WorkspaceSnapshot,
    InformationalState,
    WorktreeError,
    WorktreeIntent,
    WorktreeRecord,
    WorktreeState,
    new_intent_id,
    new_worktree_id,
)
from change.worktree_store import WorktreeStore
from change.worktree_manager import ReconcileReport, WorktreeManager

__all__ = [
    "GitManager",
    "GitCommandError",
    "WorktreeManager",
    "ReconcileReport",
    "WorktreeStore",
    "WorktreeRecord",
    "WorktreeState",
    "WorktreeIntent",
    "IntentKind",
    "IntentState",
    "WorktreeErrorCode",
    "WorktreeEventType",
    "WorkspaceSnapshot",
    "InformationalState",
    "WorktreeError",
    "new_worktree_id",
    "new_intent_id",
]
