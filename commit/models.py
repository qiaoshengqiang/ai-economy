"""
Commit Domain —— 输出仓库的提交请求
=====================================

    Task COMPLETED
        ↓
    commit_request（授权 / 状态 / 目标）
        ↓
    OutputCommitService（唯一执行 git commit 的组件）

🔒 `commit_request` 是**外部副作用**的记录，不是 Task 生命周期：
   · Task 仍然是 COMPLETED；
   · 没有 `COMMITTED` 这个 Task 状态；
   · Worker 不读取这里的授权 / 结果。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict


class CommitMode(str, Enum):
    #: 每次提交都需要人类确认。
    MANUAL = "manual"
    #: 自动提交到任务分支；不合并、不推送。
    LIMITED = "limited"
    #: 自动提交；可选自动合并到输出仓库基线分支。
    AUTO = "auto"


class CommitStatus(str, Enum):
    #: manual：等待人类确认。
    AWAITING_HUMAN = "AWAITING_HUMAN"
    #: 已授权，准备执行。
    APPROVED = "APPROVED"
    #: 正在执行 git 操作。
    COMMITTING = "COMMITTING"
    #: 已提交到输出仓库。
    COMMITTED = "COMMITTED"
    #: 门禁 / git 操作失败；可以重试。
    FAILED = "FAILED"
    #: 门禁不通过，等待人类处理。
    BLOCKED = "BLOCKED"
    #: Task 已 Reopen / iteration 变了 / 快照不再是 final。
    STALE = "STALE"
    #: 人类取消。
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class CommitRequest:
    request_id: str
    task_id: str
    iteration: int
    snapshot_seq: int
    snapshot_commit: str
    snapshot_tree: str
    mode: str
    status: str
    output_repo: str
    target_ref: str
    base_commit: str = ""
    commit_sha: str = ""
    merge_sha: str = ""
    authorized_by: str = ""
    authorized_at: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def code(self) -> str:
        """给人类确认用的短码。"""
        return str(self.snapshot_commit or "")[:8]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["code"] = self.code
        return data


__all__ = [
    "CommitMode",
    "CommitStatus",
    "CommitRequest",
]
