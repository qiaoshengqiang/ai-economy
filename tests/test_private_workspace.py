"""
Agent Private Workspace V1 —— 定向测试
============================================================

【这个文件要证明的核心事实】

    Agent
      ↓
    自己的 Private Workspace（不是别人 的）
      ↓
    读写 + 长期记忆
      ↓
    **进程重启/对象重建之后仍然存在**
      ↓
    不同 Agent 之间**严格隔离**

【🔒 不用真实 LLM · 不碰数据库 · 不碰 Task Workspace】

    这里测的是 Agent-facing 接线层。每个 Agent 用一个**临时目录**
    当 workspace，所以测试之间互不干扰，也不会往仓库里写东西。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.base_agent import BaseAgent
from core.workspace import Workspace, WorkspaceViolationError


# ============================================================
# 脚手架：一个真实的 Agent，但 workspace 指向 tmp_path
# ============================================================
def make_agent(tmp_path: Path, agent_id: str) -> BaseAgent:
    """
    造一个**真的** `BaseAgent`，只把 workspace 根换成临时目录。

    `BaseAgent` 只把 identity/config/wallet 存起来 ——
    本次新增的方法只用到 `self.workspace` 与 `self.agent_id`，
    所以用 SimpleNamespace 代替那三个依赖是**诚实的**，
    不是「测了个假的」。
    """
    root = tmp_path / agent_id / "workspace"
    return BaseAgent(
        identity=SimpleNamespace(agent_id=agent_id, name=agent_id),
        config=SimpleNamespace(agent_id=agent_id),
        wallet=SimpleNamespace(agent_id=agent_id),
        workspace=Workspace(root=root, agent_id=agent_id),
    )


@pytest.fixture
def agent_a(tmp_path):
    return make_agent(tmp_path, "agent_a")


@pytest.fixture
def agent_b(tmp_path):
    return make_agent(tmp_path, "agent_b")


# ============================================================
# §5 持久性
# ============================================================
class TestPersistence:
    def test_write_then_read(self, agent_a):
        """测试 1：写进去，读出来。"""
        agent_a.write_private("notes/test.txt", "hello")
        assert agent_a.read_private("notes/test.txt") == "hello"

    def test_survives_agent_reconstruction(self, tmp_path):
        """
        测试 2：**销毁 Agent 对象，重建一个相同 agent_id 的** ——
               内容仍然在。

        🔒 这是「脱离进程内存、成为持久资产」的判据。
        """
        first = make_agent(tmp_path, "agent_a")
        first.write_private("notes/test.txt", "hello")
        del first                                    # 对象没了

        second = make_agent(tmp_path, "agent_a")     # 全新对象，同 agent_id
        assert second.read_private("notes/test.txt") == "hello"

    def test_long_memory_survives_reconstruction(self, tmp_path):
        """测试 3：长期记忆跨重建仍在。"""
        first = make_agent(tmp_path, "agent_a")
        first.remember_long("identity.txt", "I am agent_a")
        del first

        second = make_agent(tmp_path, "agent_a")
        assert second.read_long("identity.txt") == "I am agent_a"

    def test_long_memory_is_not_in_process_memory(self, tmp_path):
        """
        🔒 长期记忆和 `remember()` 的短期记忆是**两回事**：

            remember()       进程内 list（重建即失）
            remember_long()  落盘文件（重建仍在）
        """
        first = make_agent(tmp_path, "agent_a")
        first.remember("something_happened")
        first.remember_long("durable.txt", "still here")

        assert len(first.memory) == 1                # 短期在内存里
        del first

        second = make_agent(tmp_path, "agent_a")
        # 短期记忆没了（这是它的定义，不是 bug）
        assert second.memory == []
        # 长期记忆还在
        assert second.read_long("durable.txt") == "still here"

    def test_list_and_delete(self, agent_a):
        agent_a.write_private("a.txt", "1")
        agent_a.write_private("sub/b.txt", "2")

        files = agent_a.list_private()
        assert "a.txt" in files and "sub/b.txt" in files
        assert agent_a.exists_private("a.txt") is True

        assert agent_a.delete_private("a.txt") is True
        assert agent_a.exists_private("a.txt") is False
        assert agent_a.delete_private("nope.txt") is False


# ============================================================
# §6 Agent 隔离
# ============================================================
class TestIsolation:
    def test_b_cannot_read_a_files(self, agent_a, agent_b):
        agent_a.write_private("notes/test.txt", "secret")
        with pytest.raises(FileNotFoundError):
            agent_b.read_private("notes/test.txt")

    def test_b_cannot_read_a_memory(self, agent_a, agent_b):
        agent_a.remember_long("identity.txt", "I am agent_a")
        with pytest.raises(FileNotFoundError):
            agent_b.read_long("identity.txt")

    def test_b_cannot_escape_with_dotdot(self, agent_b):
        with pytest.raises(WorkspaceViolationError):
            agent_b.read_private("../agent_a/workspace/notes/test.txt")

    def test_b_cannot_use_absolute_path(self, agent_a, agent_b):
        target = str(agent_a.workspace.root / "notes" / "test.txt")
        with pytest.raises(WorkspaceViolationError):
            agent_b.read_private(target)

    def test_b_cannot_escape_via_symlink(self, tmp_path, agent_a, agent_b):
        """
        🔒 符号链接逃逸 —— 这是 `_resolve()` 第二道闸专门防的。
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("top secret", encoding="utf-8")

        link = agent_b.workspace.root / "link.txt"
        os.symlink(victim, link)

        with pytest.raises(WorkspaceViolationError):
            agent_b.read_private("link.txt")

    def test_writes_do_not_cross(self, agent_a, agent_b):
        """写也不会串。"""
        agent_a.write_private("same.txt", "from A")
        agent_b.write_private("same.txt", "from B")

        assert agent_a.read_private("same.txt") == "from A"
        assert agent_b.read_private("same.txt") == "from B"

    def test_exists_private_returns_false_not_raise(self, agent_b):
        """越界查询返回 False（不是异常）—— 保持 exists 的语义。"""
        assert agent_b.exists_private("../agent_a/workspace/x.txt") is False


# ============================================================
# §4 memory_path 安全一致性
# ============================================================
class TestMemoryPathSecurity:
    def test_normal_name_ok(self, agent_a):
        assert agent_a.workspace.memory_path("identity.txt").name == \
            "identity.txt"

    @pytest.mark.parametrize("bad", ["../evil.txt", "/etc/passwd",
                                     "a/../../evil.txt"])
    def test_traversal_rejected(self, agent_a, bad):
        with pytest.raises(WorkspaceViolationError):
            agent_a.workspace.memory_path(bad)

    def test_symlink_escape_rejected(self, tmp_path, agent_a):
        """
        🔒 **修之前这里是漏的**（审计抓到的真实漏洞）。

           旧版只查 `is_absolute()` 和 `".." in parts` ——
           符号链接两种情况都不满足，于是：

               os.symlink("/etc/passwd", memory/evil)
               workspace.memory_path("evil")   → 放行
               读出来                          → **真的是 /etc/passwd**

           现在 memory_path 和 _resolve 用**完全一致**的三步
           （拒绝对路径 → resolve() → relative_to()）。
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("top secret", encoding="utf-8")

        link = agent_a.workspace.memory_dir / "evil"
        os.symlink(victim, link)

        with pytest.raises(WorkspaceViolationError):
            agent_a.workspace.memory_path("evil")

    def test_read_long_rejects_symlink(self, tmp_path, agent_a):
        """从 Agent API 这一层也拒绝（不只是底层方法）。"""
        victim = tmp_path / "victim.txt"
        victim.write_text("top secret", encoding="utf-8")
        os.symlink(victim, agent_a.workspace.memory_dir / "evil")

        with pytest.raises(WorkspaceViolationError):
            agent_a.read_long("evil")

    def test_memory_and_workspace_are_separate_dirs(self, agent_a):
        """
        🔒 memory 与 workspace 是**平级**的两个目录，
           但都属于同一个 Agent，都受它自己的边界保护。
        """
        assert agent_a.workspace.memory_dir != agent_a.workspace.root
        assert agent_a.workspace.memory_dir.parent == \
            agent_a.workspace.root.parent

        # 写进 memory 的东西不会出现在 list_private 里，反之亦然
        agent_a.remember_long("m.txt", "memory")
        agent_a.write_private("w.txt", "workspace")

        assert "w.txt" in agent_a.list_private()
        assert all("m.txt" not in f for f in agent_a.list_private())
        assert "m.txt" in agent_a.list_long()


# ============================================================
# §9 Agent 不该看到物理路径
# ============================================================
class TestNoPhysicalPathExposure:
    def test_agent_api_takes_logical_paths_only(self):
        """
        🔒 新增的 API 只接受**逻辑路径** —— 没有 `cwd` / `absolute` /
           `physical` 这类参数。物理路径不出现在签名里。
        """
        import inspect

        for name in ("write_private", "read_private", "exists_private",
                     "delete_private", "list_private", "remember_long",
                     "read_long"):
            params = set(inspect.signature(
                getattr(BaseAgent, name)).parameters)
            for banned in ("cwd", "physical_path", "absolute", "root"):
                assert banned not in params, f"{name} 暴露了 {banned}"

    def test_methods_are_thin_forwarders(self):
        """
        🔒 这些方法**只是转发** —— 不重新实现路径安全。

        证明方式：源码里不该出现路径判断
        （`is_absolute` / `relative_to` / `resolve` / `..`）。
        """
        import ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(BaseAgent))
        for name in ("write_private", "read_private", "exists_private",
                     "delete_private", "list_private", "remember_long",
                     "read_long"):
            tree = ast.parse(src)
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            body = ast.unparse(fn)
            for forbidden in ("is_absolute", "relative_to", "realpath"):
                assert forbidden not in body, (
                    f"{name} 里出现了 {forbidden} —— 路径安全只该有"
                    f" core/workspace.py 一个定义"
                )
            assert "self.workspace" in body, f"{name} 应当是纯转发"


# ============================================================
# Scope 守卫：没有引入 Tool Calling / Memory 架构
# ============================================================
class TestScopeNotExpanded:
    def test_no_tool_calling_was_introduced(self):
        """§七：本次**没有**引入 tool calling / capability framework。"""
        import inspect
        from agents import base_agent

        src = inspect.getsource(base_agent)
        for forbidden in ("tool_schema", "function_call", "tool_call",
                          "capability_registry", "ToolRegistry"):
            assert forbidden not in src, f"不该出现 {forbidden}"

    def test_no_memory_framework_was_introduced(self):
        """
        §三：没有 embedding / vector db / 检索 / 排序。

        ⚠️ 这条守卫**第一版是假红**的：它按文本扫整个源文件，
           结果被 `remember_long` 的**文档字符串**绊倒 ——
           那里面正好写着「刻意保持极薄：没有 embedding / 检索 / 排序」。

           这已经是本项目反复出现的一类假红（AGENTS.md §12.3）。
           正解：只看**代码**（import 与调用），不看说明文字。
        """
        import ast
        import inspect
        import textwrap
        from agents import base_agent

        tree = ast.parse(textwrap.dedent(inspect.getsource(base_agent)))

        # ① 不许 import 任何记忆/向量库
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        for forbidden in ("numpy", "faiss", "chromadb", "pinecone",
                          "sentence_transformers"):
            assert forbidden not in imported, f"引入了 {forbidden}"

        # ② 不许出现检索/排序/摘要的**调用**
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called.add(fn.attr if isinstance(fn, ast.Attribute)
                           else getattr(fn, "id", ""))

        for forbidden in ("rank_memories", "summarize_memory",
                          "search_memory", "embed", "similarity"):
            assert forbidden not in called, f"调用了 {forbidden}"
