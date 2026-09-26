"""
Task Workspace —— 任务专属的受控工作空间（**只有引用，没有权限**）
============================================================

【这个文件必须和 core/workspace.py 一起读】

AI Economy OS 从现在开始有两种 Workspace，它们是**完全不同**的东西：

┌──────────────────────┬──────────────────────────────────────────────┐
│ Agent Private        │ Task Workspace                               │
│ Workspace            │                                              │
├──────────────────────┼──────────────────────────────────────────────┤
│ core/workspace.py    │ core/task_workspace.py（本文件）             │
│                      │                                              │
│ 每个 Agent 一个       │ 每个 Task 一个                               │
│ Agent A → 自己的      │ TASK-001 → WS-001                            │
│ Agent B → 自己的      │ TASK-002 → WS-002                            │
│                      │                                              │
│ 默认**不对别人开放**  │ 任务级**共享**空间，可多 Agent 参与          │
│                      │                                              │
│ ✅ 有真实文件 I/O     │ ❌ **没有任何 I/O**                          │
│ ✅ 有路径边界检查     │ ❌ 没有路径概念                              │
│ ✅ 记忆存在这里       │ ❌ 不存记忆                                  │
└──────────────────────┴──────────────────────────────────────────────┘

| private 是「我的东西」，task 是「这件事的东西」。

【本文件实现什么】

    只有一个「逻辑引用」：TaskWorkspaceRef
        · 它只记住 workspace_id
        · 它【不知道】任何文件路径
        · 它【不能】读写任何文件
        · 它【不授予】任何权限

【本文件刻意不实现什么】（属于后续步骤）

    ❌ Workspace Manager（创建/分配 workspace 目录）
    ❌ Workspace Gateway（鉴权）
    ❌ Capability / Permission（read / write / run_tests / create_commit）
    ❌ allowed_paths 限制
    ❌ Git Worktree
    ❌ workspace_members / workspace_files 之类的表

【为什么现在就要有这个文件】

    因为「概念」如果不落到代码里，就会在文档里漂。
    有了这个类型的**存在**，就可以写测试锁住它：

        · TaskWorkspaceRef 没有 read_text / write_text
        · TaskWorkspaceRef 没有 grant / allow / permission
        · workspace_id 不是路径、不是 URL

    这比在文档里写一百遍「workspace_id 不是权限」有用得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping


# ============================================================
# Workspace 的种类
# ============================================================
class WorkspaceKind:
    """
    两种 Workspace 的种类标识。

    【为什么要显式区分种类】
        因为它们的所有权、生命周期、可见性完全不同：

            PRIVATE  属于一个 Agent，永久存在，别人看不到
            TASK     属于一个 Task，任务结束后就没了意义，多个 Agent 参与

        如果代码里只有一个裸的 workspace_id 字符串，
        就分不清「这是哪个 Agent 的私有空间」还是「这是哪个任务的共享空间」——
        而这决定了未来该用哪套权限规则。
    """

    #: Agent 私有工作空间（由 core/workspace.py 管理，有真实文件 I/O）
    PRIVATE = "PRIVATE"

    #: Task 专属工作空间（本文件，纯逻辑引用，无 I/O）
    TASK = "TASK"

    @classmethod
    def all(cls) -> list:
        return [cls.PRIVATE, cls.TASK]


# ============================================================
# 异常
# ============================================================
class TaskWorkspaceError(Exception):
    """Task Workspace 引用相关错误。"""


# ============================================================
# Task Workspace 引用
# ============================================================
@dataclass(frozen=True)
class TaskWorkspaceRef:
    """
    对一个 Task Workspace 的**逻辑引用**。

        ref = TaskWorkspaceRef("WS-001")
        ref.workspace_id        # "WS-001"
        str(ref)                # "task-workspace:WS-001"

    【它是什么】
        一个标识。用来回答「这个 Task 关联哪个工作空间」。

    【它不是什么】（每一条都有对应的测试）
        ❌ 不是文件路径   —— 没有 .path / .root / .dir
        ❌ 不是 URL       —— 不是 http://... 也不是 file://...
        ❌ 不是权限凭证   —— 没有 token / capability / permission
        ❌ 不是访问能力   —— 没有 read / write / open / list
        ❌ 不是 Python 对象引用 —— 它只是个值，不代表空间已存在

    【一个必须记住的例子】

        Agent B 收到一条消息，payload 里写着：
            "workspace_id": "WS-001"

        Agent B 现在【知道】WS-001 这个标识。
        Agent B 【仍然不能】读取它、写入它、列出它的文件。

        要真的访问，必须先经过：
            Task → Workspace Manager → Workspace Gateway → Capability 检查
        而这些全部**尚未实现**（属于后续步骤）。
    """

    workspace_id: str

    # ------------------------------------------------------------
    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise TaskWorkspaceError(
                f"workspace_id 必须是非空字符串，收到 {self.workspace_id!r}"
            )
        object.__setattr__(self, "workspace_id", self.workspace_id.strip())

    # ------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------
    @property
    def kind(self) -> str:
        """
        这种 Workspace 的种类。

        注意它是**计算出来的**，不是一个可变的字段 ——
        TaskWorkspaceRef 永远只能是 TASK 种类，
        调用方不能把它「改成」PRIVATE 来绕过未来的权限规则。
        """
        return WorkspaceKind.TASK

    @property
    def is_task_workspace(self) -> bool:
        return True

    @property
    def is_private_workspace(self) -> bool:
        return False

    # ------------------------------------------------------------
    # 转换
    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TaskWorkspaceRef":
        """
        从字典构造。

        兼容两种写法，因为历史 payload 里 workspace_id 曾经是裸字符串：
            "WS-001"                        ← 简写
            {"workspace_id": "WS-001", ...} ← 完整
        """
        if isinstance(data, str):
            return cls(data)
        if isinstance(data, Mapping):
            if "workspace_id" not in data:
                raise TaskWorkspaceError(f"缺少 workspace_id：{dict(data)}")
            return cls(str(data["workspace_id"]))
        raise TaskWorkspaceError(
            f"无法把 {type(data).__name__} 解释成 Task Workspace 引用：{data!r}"
        )

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """给人看的信息。**只描述，不代表能访问。**"""
        return {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "accessible": False,       # ← 现在永远是不可能访问的
            "note": "这只是逻辑引用；实际的访问权限由未来的 Workspace Gateway 授予",
        }

    def __str__(self) -> str:
        # 【注意】刻意不用 "://" 或任何像 URL / 路径的形式，
        # 避免有人误以为它可以被 open() 或当地址用。
        return f"task-workspace:{self.workspace_id}"

    def __repr__(self) -> str:
        return f"TaskWorkspaceRef(workspace_id={self.workspace_id!r})"
