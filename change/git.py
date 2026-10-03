"""
GitManager —— Git CLI 的**薄封装**
============================================================

【🔒 本文件的核心原则：只调用成熟 Git 原语，不重新实现 Git】

    任务书 §2 明确要求：

        Git 本身负责 repository / worktree / commit / branch /
        checkout / filesystem materialization。

        AI Economy OS **只负责**在 Git 之上建立
        Task / Workspace / Agent 的管理关系。

    **绝不重新实现**：

        ❌ Git object database
        ❌ Git index
        ❌ Git merge algorithm
        ❌ Git diff algorithm
        ❌ branch implementation
        ❌ commit implementation

    所以本文件里**没有**任何文件格式解析、哈希计算、DAG 遍历 ——
    只有「把参数拼给 git，把输出解析成结构」。

【为什么用 CLI 而不是 GitPython / pygit2】

    1. **零新依赖** —— 本项目至今只用标准库
       （SQLite 走 `sqlite3`，HTTP 走 `urllib`）。
       引入 pygit2 要编译 libgit2；GitPython 要拉一堆依赖。
    2. **`git worktree` 本身就是一个 CLI 概念**。
       库封装反而要在「库的抽象」和「CLI 语义」之间做翻译。
    3. **行为与用户手动跑 git 完全一致** ——
       排查问题时 `git worktree list` 直接能看到同样的东西。

    ⚠️ 代价：依赖宿主机的 `git` 可执行文件。
       这是**有意的**选择 —— Git 是这个系统的**外部生产资源**，
       与 LLM 的定位一致（见 `llm/`）。

【🔒 两条安全约束】

    ① **不暴露 physical path 给 Agent**
       `GitWorktreeInfo` 里有 `path`，但那是**内部**结构。
       Agent-facing 层（`WorktreeManager` 的 public 方法）会把它剥掉。

    ② **不直接改工作区内容**
       本文件只做 worktree 级别的操作（add / list / remove / 查询）。
       改文件内容必须走 Workspace Gateway（任务书 §15）。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from core.logging_setup import get_logger

logger = get_logger(__name__)


class GitCommandError(RuntimeError):
    """`git` 命令失败。"""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git {' '.join(args)} 失败（exit {returncode}）：{stderr.strip()}"
        )


@dataclass(frozen=True)
class GitWorktreeInfo:
    """
    `git worktree list --porcelain` 解析出来的一条记录。

    ⚠️ **`path` 是物理路径 —— 只允许在这一层与内部日志里出现。**
       任何 Agent-facing 的返回都必须把它剥掉（见 `WorktreeManager`）。
    """

    path: str
    head: Optional[str]          # commit sha（detached / bare 时可能没有）
    branch: Optional[str]        # refs/heads/... 或 None
    detached: bool
    bare: bool
    locked: bool
    prunable: bool


class GitManager:
    """
    Git 命令的薄封装。

    【设计上的一个关键取舍】

        它**不缓存**任何状态，也**不知道** worktree 的语义。
        每次调用都是「跑 git、解析输出」。
        状态在 DB 里（`change/worktree_store.py`）。

        为什么？因为这正是 §11 的 reconciliation 能成立的前提 ——
        **Git 是 Git 的权威，DB 是 DB 的权威，两者比较得出真相。**
        如果 GitManager 自己也缓存一份，就会出现**第三份**状态。
    """

    def __init__(self, *, repo_path: Optional[str] = None,
                 timeout_s: float = 60.0) -> None:
        self.repo_path = str(repo_path) if repo_path else None
        self.timeout_s = timeout_s

    # ==========================================================
    # 底层
    # ==========================================================
    def run(
        self,
        *args: str,
        cwd: Optional[str] = None,
        check: bool = True,
    ) -> str:
        """
        跑一条 git 命令，返回 stdout。

        🔒 **不暴露 stderr 给 Agent** —— 它可能含物理路径。
           调用方拿到的是结构化异常，日志里才有完整信息。
        """
        workdir = cwd or self.repo_path
        cmd = ["git", *args]

        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(args, -1, f"超时（{self.timeout_s}s）") from exc
        except FileNotFoundError as exc:
            raise GitCommandError(
                args, -1, "找不到 git 可执行文件（宿主机没有安装 Git？）"
            ) from exc

        if check and proc.returncode != 0:
            logger.debug("git 失败：%s\n%s", cmd, proc.stderr)
            raise GitCommandError(args, proc.returncode, proc.stderr)

        return proc.stdout

    # ==========================================================
    # Repository
    # ==========================================================
    def is_repository(self) -> bool:
        """这个目录（或它的祖先）是不是一个 Git 仓库。"""
        if not self.repo_path:
            return False
        try:
            out = self.run("rev-parse", "--is-inside-work-tree")
            return out.strip() == "true"
        except GitCommandError:
            return False

    def rev_parse(self, ref: str) -> Optional[str]:
        """
        把任意 ref 解析成 commit sha。

        ⚠️ 我们用 `--verify` + `^{commit}` 让它在「不是 commit」时失败，
           而不是返回一个 tree/blob 的 sha。
        """
        try:
            return self.run(
                "rev-parse", "--verify", f"{ref}^{{commit}}"
            ).strip()
        except GitCommandError:
            return None

    def head_commit(self) -> Optional[str]:
        return self.rev_parse("HEAD")

    def current_branch(self) -> Optional[str]:
        """当前分支名（detached 时返回 None）。"""
        try:
            out = self.run("symbolic-ref", "--quiet", "--short", "HEAD")
            return out.strip() or None
        except GitCommandError:
            return None

    def repository_root(self) -> Optional[str]:
        try:
            return self.run("rev-parse", "--show-toplevel").strip()
        except GitCommandError:
            return None

    def common_dir(self) -> Optional[str]:
        """
        Git 的 **common dir**（多个 worktree 共享的那个 .git）。

        🔒 这是识别「某个 git worktree list 输出是不是本仓库的」的正规办法。
        """
        try:
            return self.run("rev-parse", "--path-format=absolute",
                            "--git-common-dir").strip()
        except GitCommandError:
            try:
                return self.run("rev-parse", "--git-common-dir").strip()
            except GitCommandError:
                return None

    # ==========================================================
    # Branch（成熟原语）
    # ==========================================================
    def branch_exists(self, name: str) -> bool:
        try:
            self.run("show-ref", "--verify", "--quiet", f"refs/heads/{name}")
            return True
        except GitCommandError:
            return False

    def create_branch(self, name: str, start_point: str) -> None:
        self.run("branch", name, start_point)

    def delete_branch(self, name: str, *, force: bool = False) -> None:
        """
        ⚠️ 只在**明确要求**时删除分支 —— 见 `WorktreeManager` 的说明。
        """
        self.run("branch", "-D" if force else "-d", name)

    # ==========================================================
    # Worktree（成熟原语）
    # ==========================================================
    def list_worktrees(self) -> list[GitWorktreeInfo]:
        """
        解析 `git worktree list --porcelain`。

        输出形如：

            worktree /path/to/wt
            HEAD abc123...
            branch refs/heads/task-1

            worktree /path/to/main
            HEAD def456...
            detached
        """
        try:
            out = self.run("worktree", "list", "--porcelain")
        except GitCommandError:
            return []

        entries: list[GitWorktreeInfo] = []
        cur: dict = {}

        def flush() -> None:
            if not cur.get("path"):
                return
            branch_ref = cur.get("branch")
            entries.append(
                GitWorktreeInfo(
                    path=cur["path"],
                    head=cur.get("HEAD"),
                    branch=branch_ref,
                    detached=bool(cur.get("detached")),
                    bare=bool(cur.get("bare")),
                    locked=bool(cur.get("locked")),
                    prunable=bool(cur.get("prunable")),
                )
            )
            cur.clear()

        for line in out.splitlines():
            if not line.strip():
                flush()
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                flush()
                cur["path"] = value
            elif key in ("HEAD", "branch"):
                cur[key] = value
            elif key in ("detached", "bare", "locked", "prunable"):
                cur[key] = True

        flush()
        return entries

    def find_worktree(self, path: str) -> Optional[GitWorktreeInfo]:
        """按物理路径找 worktree（reconciliation 用）。"""
        target = str(Path(path).resolve()) if path else ""
        for info in self.list_worktrees():
            try:
                if str(Path(info.path).resolve()) == target:
                    return info
            except OSError:
                continue
        return None

    def worktree_add(
        self,
        path: str,
        *,
        commit: Optional[str] = None,
        branch: Optional[str] = None,
        detach: bool = False,
        create_branch: bool = False,
    ) -> None:
        """
        `git worktree add` —— **成熟原语，直接调用**。

        🔒 我们**不**自己去建目录、不自己写 `.git` 文件、
           不自己算 HEAD。那些全是 Git 的职责。
        """
        args = ["worktree", "add"]
        if detach:
            args.append("--detach")
        if create_branch and branch:
            args += ["-b", branch]
        args.append(path)
        if branch and not create_branch:
            args.append(branch)
        elif commit:
            args.append(commit)

        self.run(*args)

    def worktree_remove(self, path: str, *, force: bool = False) -> None:
        """
        `git worktree remove`。

        ⚠️ **这是一个有破坏性的原语** ——
           `WorktreeManager` 的 V1 公开接口**不提供**无条件调用它
           （任务书 §24：不要因为有 `git worktree remove`
            就简单提供一个无条件 delete）。
        """
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(path)
        self.run(*args)

    def worktree_prune(self, *, dry_run: bool = False) -> str:
        """
        `git worktree prune` —— 清理 Git 侧**已经不存在**的 worktree 记录。

        🔒 注意：prune **只动 Git 的记账**，不动我们的 DB。
           它是 reconciliation 的**工具**，不是决策者。
        """
        args = ["worktree", "prune"]
        if dry_run:
            args.append("--dry-run")
        return self.run(*args)

    def worktree_is_usable(self, path: str) -> bool:
        """
        这个 worktree 目录**真的**能当工作区用吗？

        比 `Path.exists()` 强：它验证 Git 自己也认这个 worktree
        （目录可能在，但 `.git` 链接被删了 —— 那是**损坏**，不是可用）。
        """
        if not path or not Path(path).exists():
            return False
        try:
            out = self.run("rev-parse", "--is-inside-work-tree", cwd=path)
            return out.strip() == "true"
        except GitCommandError:
            return False

    # ==========================================================
    # 只读查询（供未来的 Patch 阶段用，本阶段只做基础）
    # ==========================================================
    def status_porcelain(self, *, cwd: Optional[str] = None) -> str:
        """
        `git status --porcelain` 的原始输出。

        ⚠️ 本阶段**不**把它翻译成 Workspace Revision ——
           那正是任务书 §14 明令禁止的
           「`scan_filesystem()` → 每个物理变化 → revision +1」。
           这里只提供**只读**的事实，供未来 Patch 阶段决定怎么用。
        """
        return self.run("status", "--porcelain", cwd=cwd)
