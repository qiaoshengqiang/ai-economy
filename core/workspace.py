"""
Workspace —— Agent 的文件访问边界
============================================================

【这是本项目最重要的架构原则之一，请认真读】

问题：
    如果 Agent 代码里写 `open("/Users/xxx/agents/agent_b/memory/secret.txt")`，
    那工作空间隔离就完全是一句空话。

解决办法：
    Agent 永远不接触真实文件系统路径。
    它只拿到一个 Workspace 对象，而这个对象只会解析到
    【属于它自己的目录】。

        Agent
         ↓
        Workspace  ← 这里做边界检查
         ↓
        agents/<自己的 id>/

    任何试图跑到自己目录外面的路径（比如 "../../agent_b/memory"）
    都会被直接拒绝并抛异常。

为什么现在（单机）就要做这件事？
    因为以后 Agent B 会搬到另一台机器上。
    如果现在不把边界建立起来，那时候要改的就是整个 Agent 的代码；
    现在建立起来，以后只需要换掉 Workspace 的实现。

未来扩展：
    RemoteWorkspace —— 通过网络 API 访问远端工作空间
    但 Agent 的代码一行都不用改，因为它调用的还是同样的方法。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union


class WorkspaceViolationError(Exception):
    """
    Agent 试图访问自己工作空间之外的文件。

    这个异常是【安全边界】，不是普通错误。
    看到它就说明架构被打破了，要立刻修。
    """


class Workspace:
    """
    一个 Agent 的私有文件空间。

    用法：
        ws = Workspace.for_agent("agent_a")
        ws.write_text("output/result.py", "print('hi')")
        content = ws.read_text("output/result.py")
        ws.list_files()

    注意所有路径都是【相对于工作空间根目录】的相对路径，
    绝对不能是绝对路径。
    """

    def __init__(self, root: Union[str, Path], agent_id: str):
        self.agent_id = agent_id
        self.root = Path(root).resolve()

        # 工作空间目录必须存在。不存在就建，
        # 这样新加一个 Agent C 的时候不需要手动 mkdir。
        self.root.mkdir(parents=True, exist_ok=True)

        # 记忆目录：Agent 的私有记忆单独放，和工作产物分开
        self.memory_dir = self.root.parent / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------
    @classmethod
    def for_agent(cls, agent_id: str) -> "Workspace":
        """
        按 agent_id 创建对应的工作空间。
        路径由 core/paths.py 统一计算，避免各处硬编码。
        """
        from core.paths import agent_workspace_dir

        return cls(root=agent_workspace_dir(agent_id), agent_id=agent_id)

    # ------------------------------------------------------------
    # 边界检查（核心）
    # ------------------------------------------------------------
    def _resolve(self, relative_path: Union[str, Path]) -> Path:
        """
        把相对路径解析成绝对路径，并确保它没有跑出工作空间。

        这是整个隔离机制的咽喉要道，所有文件操作都必须经过它。
        """
        raw = Path(relative_path)

        if raw.is_absolute():
            raise WorkspaceViolationError(
                f"Agent {self.agent_id} 不允许使用绝对路径：{relative_path}\n"
                f"请使用相对于自己工作空间的路径，例如 'output/result.txt'"
            )

        # resolve() 会展开 .. 和符号链接，得到真实路径
        candidate = (self.root / raw).resolve()

        # 检查候选路径是否真的在 root 里面
        # 用 relative_to 而不是字符串 startswith：
        #   startswith 会把 /a/agent_a_evil 误判为在 /a/agent_a 里面
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise WorkspaceViolationError(
                f"Agent {self.agent_id} 试图访问自己工作空间之外的文件：\n"
                f"  请求：{relative_path}\n"
                f"  解析后：{candidate}\n"
                f"  允许范围：{self.root}\n"
                f"这是被禁止的操作。如果你确实需要跨 Agent 交换文件，"
                f"请通过 Communication 层发消息。"
            )

        return candidate

    # ------------------------------------------------------------
    # 文件读写
    # ------------------------------------------------------------
    def write_text(
        self,
        relative_path: Union[str, Path],
        content: str,
        encoding: str = "utf-8",
    ) -> Path:
        """写文本文件。父目录不存在会自动创建。"""
        target = self._resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return target

    def read_text(
        self,
        relative_path: Union[str, Path],
        encoding: str = "utf-8",
    ) -> str:
        """读文本文件。不存在会抛 FileNotFoundError。"""
        target = self._resolve(relative_path)
        return target.read_text(encoding=encoding)

    def exists(self, relative_path: Union[str, Path]) -> bool:
        try:
            return self._resolve(relative_path).exists()
        except WorkspaceViolationError:
            return False

    def delete(self, relative_path: Union[str, Path]) -> bool:
        """删除文件。返回是否真的删掉了。"""
        target = self._resolve(relative_path)
        if target.is_file():
            target.unlink()
            return True
        return False

    def list_files(self, pattern: str = "**/*") -> List[str]:
        """
        列出工作空间里的文件，返回相对于工作空间的路径。

        pattern 默认递归列出所有文件。
        """
        results: List[str] = []
        for path in sorted(self.root.glob(pattern)):
            if path.is_file() and not path.name.startswith("."):
                results.append(str(path.relative_to(self.root)))
        return results

    def size_bytes(self) -> int:
        """工作空间占用大小。未来算「存储成本」时会用到。"""
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    # ------------------------------------------------------------
    # 记忆（私有，别的 Agent 读不到）
    # ------------------------------------------------------------
    def memory_path(self, name: str) -> Path:
        """
        记忆文件的路径。

        注意：memory 和工作空间是平级的两个目录，
        但都属于同一个 Agent，所以它同样受边界保护。
        """
        # 【为什么这里重写过一次 —— 审计发现的真实漏洞】
        #
        #   旧版只查两件事：is_absolute() 和 ".." in parts。
        #   它**看起来**够了，但和 `_resolve()` 不是同一套原则：
        #
        #       ❌ 不 resolve() → **符号链接可以逃逸**
        #
        #   实测（修之前）：
        #       os.symlink("/etc/passwd", memory/evil)
        #       workspace.memory_path("evil")     → 放行
        #       Path(...).read_text()             → **真的读到 /etc/passwd**
        #
        #   现在统一成和 `_resolve()` **完全一致**的三步：
        #       ① 拒绝绝对路径
        #       ② resolve()（展开 .. 与 symlink）
        #       ③ relative_to() 确认仍在 memory 根内
        #
        #   两道闸用同一套语义 —— 「路径安全」全项目只有一个定义。
        raw = Path(name)

        # ① 绝对路径直接拒
        if raw.is_absolute():
            raise WorkspaceViolationError(
                f"Agent {self.agent_id} 的记忆名不允许绝对路径：{name}\n"
                f"请用相对于记忆目录的名字，例如 'identity.txt'"
            )

        memory_root = self.memory_dir.resolve()

        # ② resolve() 展开 .. 和符号链接
        candidate = (memory_root / raw).resolve()

        # ③ 必须仍在 memory 根内。
        #    用 relative_to 而不是 startswith：
        #    startswith 会把 .../memory_evil 误判为在 .../memory 里
        try:
            candidate.relative_to(memory_root)
        except ValueError:
            raise WorkspaceViolationError(
                f"Agent {self.agent_id} 试图访问自己记忆目录之外的文件：\n"
                f"  请求：{name}\n"
                f"  解析后：{candidate}\n"
                f"  允许范围：{memory_root}\n"
                f"这是被禁止的操作。"
            )

        return candidate

    def write_memory(self, name: str, content: str) -> Path:
        target = self.memory_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_memory(self, name: str) -> str:
        return self.memory_path(name).read_text(encoding="utf-8")

    def list_memories(self) -> List[str]:
        if not self.memory_dir.exists():
            return []
        return sorted(
            path.name
            for path in self.memory_dir.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )

    # ------------------------------------------------------------
    def __repr__(self) -> str:
        return f"Workspace(agent_id={self.agent_id!r}, root={str(self.root)!r})"
