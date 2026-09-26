"""
Test: Task Workspace 引用（Step 4A）
============================================================

这个文件的核心任务只有一件：

    **证明 workspace_id 只是一个标识，不产生任何能力。**

Step 4A 明确不实现 Workspace 权限。所以这里测的不是「权限对不对」，
而是「权限确实不存在」—— 也就是把「不实现」这件事变成可验证的事实。

同时也要证明「两种 Workspace 是分开的」：
    Agent Private Workspace   core/workspace.py     有真实文件 I/O
    Task Workspace            core/task_workspace.py 只有标识
"""

from __future__ import annotations

import pytest

from core.task_workspace import (
    TaskWorkspaceError,
    TaskWorkspaceRef,
    WorkspaceKind,
)


# ============================================================
# 1. 基本构造
# ============================================================
class TestConstruction:
    def test_basic(self):
        ref = TaskWorkspaceRef("WS-001")

        assert ref.workspace_id == "WS-001"
        assert ref.kind == WorkspaceKind.TASK

    def test_whitespace_stripped(self):
        assert TaskWorkspaceRef("  WS-001  ").workspace_id == "WS-001"

    @pytest.mark.parametrize("bad", ["", "   ", None, 123])
    def test_invalid_id_rejected(self, bad):
        with pytest.raises(TaskWorkspaceError):
            TaskWorkspaceRef(bad)

    def test_immutable(self):
        ref = TaskWorkspaceRef("WS-001")

        with pytest.raises(Exception):
            ref.workspace_id = "WS-002"

    def test_hashable(self):
        assert TaskWorkspaceRef("WS-001") in {TaskWorkspaceRef("WS-001")}
        assert len({TaskWorkspaceRef("WS-001"), TaskWorkspaceRef("WS-002")}) == 2

    def test_two_workspaces_are_different(self):
        """不同 Task 的 Workspace 默认互相隔离。"""
        assert TaskWorkspaceRef("WS-001") != TaskWorkspaceRef("WS-002")


# ============================================================
# 2. 种类
# ============================================================
class TestKind:
    def test_task_kind_is_derived_not_settable(self):
        """
        kind 是**计算出来的**，不是字段。

        这样调用方不能把它改成 PRIVATE 来绕过未来的权限规则。
        """
        ref = TaskWorkspaceRef("WS-001")

        assert ref.kind == WorkspaceKind.TASK
        with pytest.raises(Exception):
            ref.kind = WorkspaceKind.PRIVATE

    def test_is_task_not_private(self):
        ref = TaskWorkspaceRef("WS-001")

        assert ref.is_task_workspace is True
        assert ref.is_private_workspace is False

    def test_two_kinds_exist(self):
        assert WorkspaceKind.all() == ["PRIVATE", "TASK"]


# ============================================================
# 3. 没有权限（最重要的一组）
# ============================================================
class TestHasNoPermissions:
    """
    【Step 4A 的核心承诺：workspace_id 不产生任何能力】

    这一组测试把「不实现权限」变成了可验证的事实。
    将来谁不小心加了权限方法，这里会红 ——
    从而被迫先想清楚「这属于哪一步」。
    """

    @pytest.mark.parametrize(
        "forbidden",
        [
            # 文件访问
            "read_text", "write_text", "read", "write", "open",
            "read_file", "write_file", "list_files", "delete",
            "exists", "root", "path", "dir", "directory",
            # 权限
            "grant", "permission", "capability", "authorize", "allow",
            "access", "token", "credential",
            # 网络
            "url", "endpoint", "host",
            # 执行
            "run", "execute", "shell", "subprocess",
        ],
    )
    def test_forbidden_attribute_absent(self, forbidden):
        ref = TaskWorkspaceRef("WS-001")

        assert not hasattr(ref, forbidden), (
            f"TaskWorkspaceRef 不该有 {forbidden!r} —— "
            f"workspace_id 只是逻辑引用，不是路径/权限/执行能力"
        )

    def test_no_methods_at_all(self):
        """
        这个类型只应该有「读属性」和「转换」，
        不该有任何能产生副作用的方法。
        """
        allowed = {
            "workspace_id", "kind", "is_task_workspace", "is_private_workspace",
            "to_dict", "from_dict", "describe",
        }
        actual = {name for name in dir(TaskWorkspaceRef)
                  if not name.startswith("_")}

        extra = actual - allowed
        assert not extra, f"TaskWorkspaceRef 出现了预期之外的方法：{extra}"

    def test_describe_says_not_accessible(self):
        """describe() 必须明确说明「不可访问」。"""
        info = TaskWorkspaceRef("WS-001").describe()

        assert info["accessible"] is False
        assert "Gateway" in info["note"]

    def test_string_form_is_not_a_path_or_url(self):
        """
        str() 不能长得像路径或 URL ——
        否则有人会拿它去 open() 或者当地址用。
        """
        text = str(TaskWorkspaceRef("WS-001"))

        assert "://" not in text            # 不是 URL
        assert not text.startswith("/")     # 不是绝对路径
        assert ".." not in text
        assert text == "task-workspace:WS-001"

    def test_path_like_id_is_not_resolved(self):
        """
        即使 ID 长得像路径，也不会被解析成路径。

        它就是一个字符串，原样保存。
        """
        evil = "../../etc/passwd"
        ref = TaskWorkspaceRef(evil)

        assert ref.workspace_id == evil
        assert isinstance(ref.workspace_id, str)


# ============================================================
# 4. 序列化
# ============================================================
class TestSerialization:
    def test_to_dict(self):
        assert TaskWorkspaceRef("WS-001").to_dict() == {
            "workspace_id": "WS-001",
            "kind": "TASK",
        }

    def test_from_dict_full_form(self):
        ref = TaskWorkspaceRef.from_dict(
            {"workspace_id": "WS-001", "kind": "TASK"}
        )
        assert ref.workspace_id == "WS-001"

    def test_from_dict_shorthand_string(self):
        """
        兼容裸字符串 —— 因为协议 payload 里 workspace_id 就是个字符串。
        """
        assert TaskWorkspaceRef.from_dict("WS-001").workspace_id == "WS-001"

    def test_from_dict_missing_id_rejected(self):
        with pytest.raises(TaskWorkspaceError):
            TaskWorkspaceRef.from_dict({"kind": "TASK"})

    def test_from_dict_wrong_type_rejected(self):
        with pytest.raises(TaskWorkspaceError):
            TaskWorkspaceRef.from_dict(123)

    def test_roundtrip(self):
        original = TaskWorkspaceRef("WS-001")
        assert TaskWorkspaceRef.from_dict(original.to_dict()) == original


# ============================================================
# 5. 两种 Workspace 是分开的
# ============================================================
class TestTwoWorkspaceKindsAreSeparate:
    """
    Agent Private Workspace 和 Task Workspace 必须是两个东西。

    下面用真实类型对比来证明这一点。
    """

    def test_private_workspace_has_io_and_task_workspace_does_not(self):
        from core.workspace import Workspace

        # 私有工作空间：有真实文件 I/O
        assert hasattr(Workspace, "read_text")
        assert hasattr(Workspace, "write_text")

        # Task Workspace 引用：什么都没有
        assert not hasattr(TaskWorkspaceRef, "read_text")
        assert not hasattr(TaskWorkspaceRef, "write_text")

    def test_private_workspace_is_per_agent(self):
        """私有工作空间是「每个 Agent 一个」。"""
        import inspect

        from core.workspace import Workspace

        sig = inspect.signature(Workspace.for_agent)
        assert "agent_id" in sig.parameters

    def test_task_workspace_is_not_per_agent(self):
        """
        Task Workspace 是「每个任务一个」，不是「每个 Agent 一个」。

        所以它的构造只要 workspace_id，不要 agent_id。
        """
        import inspect

        sig = inspect.signature(TaskWorkspaceRef.__init__)
        assert "workspace_id" in sig.parameters
        assert "agent_id" not in sig.parameters

    def test_private_workspace_module_does_not_know_about_tasks(self):
        """
        core/workspace.py（私有）不该知道 Task 的存在 ——
        它比 Task 更早存在，也不该依赖 Task。
        """
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "core" / "workspace.py"
        text = source.read_text(encoding="utf-8")

        assert "task_workspace" not in text
        assert "TaskWorkspaceRef" not in text
