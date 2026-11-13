"""
OutputCommitService —— 把已完成的 Task 快照提交到独立输出仓库
==============================================================

    Task COMPLETED
        ↓
    CommitRequest（manual 等待人类；limited/auto 自动）
        ↓
    OutputCommitService
        ↓
    aieos-output/
      tasks/<task_id>/...
      refs/heads/aieos/task-<task_id>
      refs/heads/main

🔒 三条边界：

    ① 只提交**已完成 Task 的 final snapshot**，不提交 live workspace；
    ② 不写 AIEOS 主仓库；输出仓库必须独立；
    ③ 不碰 Worker 私有记忆 / Task summary / Shared Context。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from commit.models import CommitMode, CommitRequest, CommitStatus
from commit.store import CommitStore
from core.paths import PROJECT_ROOT
from core.task import TaskStatus
from work.task_directory import TaskDirectoryManager
from work.task_snapshot import SnapshotError, TaskSnapshotStore

#: Git 的空 tree（SHA-1）。
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")


class CommitError(RuntimeError):
    """提交失败（可重试或需要人类处理）。"""


class CommitBlocked(CommitError):
    """门禁不通过：大小 / 空产物 / 目标仓库不合法。"""


@dataclass(frozen=True)
class _Repo:
    path: Path
    git_dir: Path
    worktree: Optional[Path]
    target_ref: str


class OutputCommitService:
    def __init__(
        self,
        *,
        task_directories: TaskDirectoryManager,
        store: CommitStore,
        settings: Callable[[], Dict[str, Any]],
        project_root: Path = PROJECT_ROOT,
        git_binary: str = "git",
        timeout_seconds: float = 120.0,
    ) -> None:
        self.task_directories = task_directories
        self.store = store
        self.settings = settings
        self.project_root = Path(project_root).resolve()
        self.git_binary = str(git_binary or "git")
        self._git_binary = shutil.which(self.git_binary)
        self.timeout_seconds = float(timeout_seconds)

    # ------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._git_binary) and bool(self._config()["output_repo"])

    def _config(self) -> Dict[str, Any]:
        raw = self.settings() or {}
        base_ref = str(raw.get("base_ref") or "main").strip()
        if not _SAFE_REF.fullmatch(base_ref) or ".." in base_ref:
            base_ref = "main"
        return {
            "output_repo": str(raw.get("output_repo") or "").strip(),
            "base_ref": base_ref,
            "auto_merge": bool(raw.get("auto_merge", False)),
            "max_file_bytes": int(raw.get("max_file_bytes") or 20 * 1024 * 1024),
            "max_total_bytes": int(
                raw.get("max_total_bytes") or 50 * 1024 * 1024),
        }

    # ------------------------------------------------------------
    def on_task_completed(
        self,
        task: Any,
        *,
        mode: str = CommitMode.MANUAL.value,
        output_repo: str = "",
        repo_mode: str = "output",
    ) -> Optional[CommitRequest]:
        """Task 完成后调用；manual 只创建待确认请求，limited/auto 执行。

        ``output_repo`` 是 Task 创建时选定的目标仓库；空 = 使用全局默认。
        """
        if str(getattr(task, "status", "")) != TaskStatus.COMPLETED:
            return None
        cfg = self._config()
        target = str(output_repo or cfg["output_repo"] or "").strip()
        if not target or not self._git_binary:
            return None
        target_path = str(Path(target).expanduser().resolve())
        normalized_repo_mode = (
            "project" if str(repo_mode or "").strip() == "project"
            else "output"
        )
        try:
            normalized_mode = CommitMode(str(mode)).value
        except ValueError:
            normalized_mode = CommitMode.MANUAL.value

        snapshot = self._latest_final_snapshot(task)
        if snapshot is None:
            return None
        status = (
            CommitStatus.AWAITING_HUMAN.value
            if normalized_mode == CommitMode.MANUAL.value
            else CommitStatus.APPROVED.value
        )
        request = self.store.create_or_get(
            task_id=str(task.task_id),
            iteration=int(getattr(task, "iteration", 1) or 1),
            snapshot_seq=int(snapshot.seq),
            snapshot_commit=str(snapshot.commit),
            snapshot_tree=str(snapshot.tree),
            mode=normalized_mode,
            repo_mode=normalized_repo_mode,
            status=status,
            output_repo=target_path,
            target_ref=f"refs/heads/{cfg['base_ref']}",
            base_commit="",
        )
        if (
            request.status == CommitStatus.APPROVED.value
            and not request.authorized_by
        ):
            request = self.store.update(
                request.request_id,
                authorized_by=f"policy:{normalized_mode}",
                authorized_at=_now(),
            )
        if request.status in (
            CommitStatus.AWAITING_HUMAN.value,
            CommitStatus.COMMITTED.value,
            CommitStatus.STALE.value,
            CommitStatus.CANCELLED.value,
            CommitStatus.FAILED.value,
            CommitStatus.BLOCKED.value,
        ):
            return request
        if request.status == CommitStatus.APPROVED.value:
            return self.execute(request.request_id)
        return request

    # ------------------------------------------------------------
    def confirm(
        self, task: Any, code: str, *, actor: str = "human",
    ) -> CommitRequest:
        """人类确认后执行 manual 请求。"""
        code = str(code or "").strip().lower()
        requests = self.store.list_for_task(str(task.task_id))
        pending = [
            item for item in requests
            if item.status == CommitStatus.AWAITING_HUMAN.value
            and (
                not code
                or item.snapshot_commit.lower().startswith(code)
                or item.snapshot_tree.lower().startswith(code)
            )
        ]
        if not pending:
            if code:
                done = [
                    item for item in requests
                    if item.status == CommitStatus.COMMITTED.value
                    and (
                        item.snapshot_commit.lower().startswith(code)
                        or item.snapshot_tree.lower().startswith(code)
                    )
                ]
                if done:
                    return done[0]
            raise CommitError("没有匹配的待确认提交请求")
        if len(pending) > 1:
            raise CommitError(
                "这个任务有多个待确认版本；请告诉我提交哪一个，"
                "或给出快照短码")
        request = pending[0]
        if (
            int(getattr(task, "iteration", 1) or 1) != request.iteration
            or str(getattr(task, "status", "")) != TaskStatus.COMPLETED
        ):
            self.store.update(
                request.request_id,
                status=CommitStatus.STALE.value,
                error="Task 已 Reopen 或不再是 COMPLETED",
            )
            raise CommitError("这个提交请求已失效：Task 已经进入新一轮")
        snapshot = self._latest_final_snapshot(task)
        if (
            snapshot is None
            or snapshot.commit != request.snapshot_commit
            or snapshot.tree != request.snapshot_tree
        ):
            self.store.update(
                request.request_id,
                status=CommitStatus.STALE.value,
                error="快照不再是该 Task 的最新 final",
            )
            raise CommitError("这个提交请求已失效：快照不再是最新 final")
        request = self.store.update(
            request.request_id,
            status=CommitStatus.APPROVED.value,
            authorized_by=str(actor or "human"),
            authorized_at=_now(),
            error="",
        )
        return self.execute(request.request_id)

    # ------------------------------------------------------------
    def execute(self, request_id: str) -> CommitRequest:
        request = self.store.find(request_id)
        if request is None:
            raise CommitError(f"提交请求不存在：{request_id}")
        if request.status == CommitStatus.COMMITTED.value:
            return request
        request = self.store.update(
            request.request_id,
            status=CommitStatus.COMMITTING.value,
            error="",
        )
        try:
            result = self._commit(request)
        except CommitBlocked as exc:
            return self.store.update(
                request.request_id,
                status=CommitStatus.BLOCKED.value,
                error=str(exc),
            )
        except Exception as exc:                              # noqa: BLE001
            return self.store.update(
                request.request_id,
                status=CommitStatus.FAILED.value,
                error=f"{type(exc).__name__}: {exc}",
            )
        return self.store.update(
            request.request_id,
            status=CommitStatus.COMMITTED.value,
            base_commit=result["base_commit"],
            commit_sha=result["commit_sha"],
            merge_sha=result.get("merge_sha", ""),
            error="",
        )

    def invalidate_task(
        self, task_id: str, *, reason: str = "Task 已 Reopen",
    ) -> None:
        """Reopen 时让旧的待确认请求失效，避免 Front 继续提示旧快照。"""
        for item in self.store.list_for_task(task_id):
            if item.status == CommitStatus.AWAITING_HUMAN.value:
                self.store.update(
                    item.request_id,
                    status=CommitStatus.STALE.value,
                    error=reason,
                )

    # ------------------------------------------------------------
    def front_context(self, *, mode: str = CommitMode.MANUAL.value) -> str:
        pending = self.store.list_pending_all(limit=20)
        lines = [f"当前默认提交模式：{mode}"]
        if pending:
            lines.append("待确认提交：")
            for item in pending:
                lines.append(
                    f"- task_id={item.task_id} code={item.code}"
                    f" snapshot_seq={item.snapshot_seq}"
                    f" mode={item.mode} repo={item.output_repo}")
        else:
            lines.append("待确认提交：无")
        return "\n".join(lines)

    # ------------------------------------------------------------
    def _latest_final_snapshot(self, task: Any):
        try:
            directory = self.task_directories.for_task(str(task.task_id))
            snapshots = TaskSnapshotStore(directory)
            latest = snapshots.latest()
        except (ValueError, OSError, SnapshotError):
            return None
        if latest is None or latest.label != "final":
            return None
        return latest

    def _commit(self, request: CommitRequest) -> Dict[str, str]:
        cfg = self._config()
        if not request.output_repo:
            raise CommitBlocked("没有配置输出仓库")
        if request.repo_mode == "project":
            info = self.inspect_repo(request.output_repo)
            if not info.get("is_git_repo"):
                raise CommitBlocked(
                    f"项目目录不是 Git 仓库：{request.output_repo}")
        repo = self._ensure_repo(request.output_repo, cfg)
        directory = self.task_directories.for_task(request.task_id)
        snapshots = TaskSnapshotStore(directory)
        try:
            snapshot = snapshots.get(request.snapshot_seq)
        except (ValueError, SnapshotError) as exc:
            raise CommitBlocked(f"找不到快照：{exc}") from exc
        if (
            snapshot.commit != request.snapshot_commit
            or snapshot.tree != request.snapshot_tree
            or snapshot.label != "final"
        ):
            raise CommitBlocked("快照与提交请求不一致，或不再是 final")

        entries = snapshots.file_entries(request.snapshot_seq)
        if not entries:
            raise CommitBlocked("快照是空的，不能提交")
        total = sum(int(item.get("bytes") or 0) for item in entries)
        if total > cfg["max_total_bytes"]:
            raise CommitBlocked(
                f"快照总大小 {total} 超过上限 {cfg['max_total_bytes']}")
        oversized = [
            item for item in entries
            if int(item.get("bytes") or 0) > cfg["max_file_bytes"]
        ]
        if oversized:
            raise CommitBlocked(
                f"{oversized[0]['path']} 超过单文件上限 "
                f"{cfg['max_file_bytes']}")
        patch = snapshots.tree_patch(request.snapshot_seq)
        if not patch.strip():
            raise CommitBlocked("快照 patch 为空")

        branch_ref = f"refs/heads/aieos/task-{request.task_id}"
        branch_sha = self._rev_parse(repo, branch_ref)
        if branch_sha:
            parent = branch_sha
        else:
            parent = self._rev_parse(repo, repo.target_ref)
        if not parent:
            raise CommitError("输出仓库没有可用的基线 commit")
        base_tree = self._rev_parse(repo, f"{parent}^{{tree}}")
        if not base_tree:
            raise CommitError("无法解析输出仓库的基线 tree")

        with tempfile.TemporaryDirectory(prefix="aieos-commit-") as temp:
            index = Path(temp) / "index"
            self._run(repo, "read-tree", base_tree, index=index, check=True)
            self._run(
                repo, "rm", "-r", "--cached", "--ignore-unmatch",
                "--", f"tasks/{request.task_id}", index=index, check=True)
            self._run(
                repo, "apply", "--cached", "--binary",
                "--exclude=*/TASK_DOC.md",
                "--directory", f"tasks/{request.task_id}", "-",
                index=index, check=True, input_text=patch)
            tree = self._run(
                repo, "write-tree", index=index, check=True).stdout.strip()
            if not tree:
                raise CommitError("git write-tree 没有返回 tree")
            message = self._commit_message(request)
            commit = self._run(
                repo, "commit-tree", tree, "-p", parent, "-m", message,
                check=True).stdout.strip()
            if not commit:
                raise CommitError("git commit-tree 没有返回 commit")
            self._run(
                repo, "update-ref", branch_ref, commit, check=True)

        merge_sha = ""
        if (
            request.mode == CommitMode.AUTO.value
            and cfg["auto_merge"]
            and self._can_fast_forward(repo, commit)
        ):
            self._run(
                repo, "update-ref", repo.target_ref, commit, check=True)
            merge_sha = commit
        return {
            "base_commit": parent,
            "commit_sha": commit,
            "merge_sha": merge_sha,
        }

    def _can_fast_forward(self, repo: _Repo, commit: str) -> bool:
        proc = self._run(
            repo, "merge-base", "--is-ancestor", repo.target_ref, commit,
            check=False)
        return proc.returncode == 0

    # ------------------------------------------------------------
    def inspect_repo(self, path: str) -> Dict[str, Any]:
        """只读检查一个目录是否是 Git 仓库，供 UI 选择仓库使用。"""
        raw = str(path or "").strip()
        result: Dict[str, Any] = {
            "path": "",
            "exists": False,
            "is_dir": False,
            "is_git_repo": False,
            "git_root": "",
            "repo_mode": "",
            "error": "",
        }
        if not raw:
            result["error"] = "目录为空"
            return result
        target = Path(raw).expanduser().resolve()
        result["path"] = str(target)
        result["exists"] = target.exists()
        result["is_dir"] = target.is_dir()
        if not result["exists"]:
            result["error"] = "目录不存在"
            return result
        if not result["is_dir"]:
            result["error"] = "不是目录"
            return result
        dot_git = target / ".git"
        bare = (target / "HEAD").is_file() and (target / "objects").is_dir()
        if not dot_git.exists() and not bare:
            result["error"] = "不是 Git 仓库"
            return result
        result["is_git_repo"] = True
        result["repo_mode"] = "bare" if bare else "worktree"
        result["git_root"] = str(target)
        if not bare:
            try:
                proc = subprocess.run(
                    [self._git_binary or "git", "-C", str(target),
                     "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=self.timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired):
                proc = None
            if (
                proc is not None
                and proc.returncode == 0
                and (proc.stdout or "").strip()
            ):
                result["git_root"] = str(
                    Path((proc.stdout or "").strip()).resolve())
        return result

    # ------------------------------------------------------------
    def _ensure_repo(
        self, output_repo: str, cfg: Dict[str, Any],
    ) -> _Repo:
        if not self._git_binary:
            raise CommitBlocked("宿主机没有 git")
        path = Path(output_repo).expanduser().resolve()
        if path == self.project_root or path == self.project_root / ".git":
            raise CommitBlocked("输出仓库不能是 AIEOS 主仓库")
        target_ref = f"refs/heads/{cfg['base_ref']}"
        path.mkdir(parents=True, exist_ok=True)
        dot_git = path / ".git"
        if dot_git.is_dir():
            git_dir = dot_git
            worktree = path
        elif (path / "HEAD").is_file() and (path / "objects").is_dir():
            git_dir = path
            worktree = None
        else:
            self._run(
                _Repo(path=path, git_dir=dot_git, worktree=path,
                      target_ref=target_ref),
                "init", "-q", check=True)
            git_dir = dot_git
            worktree = path
        repo = _Repo(
            path=path, git_dir=git_dir, worktree=worktree,
            target_ref=target_ref)
        self._run(repo, "config", "gc.auto", "0", check=True)
        if not self._rev_parse(repo, target_ref):
            empty = self._run(
                repo, "hash-object", "-t", "tree", os.devnull,
                check=True).stdout.strip()
            commit = self._run(
                repo, "commit-tree", empty, "-m",
                "chore: initialize AIEOS output repository",
                check=True).stdout.strip()
            self._run(repo, "update-ref", target_ref, commit, check=True)
            self._run(repo, "symbolic-ref", "HEAD", target_ref, check=True)
        return repo

    # ------------------------------------------------------------
    def _rev_parse(self, repo: _Repo, ref: str) -> str:
        proc = self._run(
            repo, "rev-parse", "--verify", ref, check=False)
        if proc.returncode != 0:
            return ""
        return str(proc.stdout or "").strip()

    def _commit_message(self, request: CommitRequest) -> str:
        lines = [
            f"task {request.task_id} (iteration {request.iteration})",
            "",
            f"snapshot: {request.snapshot_seq} {request.snapshot_commit}",
            f"mode: {request.mode}",
            f"repo: {request.output_repo}",
            f"branch: refs/heads/aieos/task-{request.task_id}",
        ]
        return "\n".join(lines)[:4000]

    def _run(
        self,
        repo: _Repo,
        *args: str,
        index: Optional[Path] = None,
        check: bool = True,
        input_text: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        if not self._git_binary:
            raise CommitError("宿主机没有 git")
        cmd = [self._git_binary, f"--git-dir={repo.git_dir}"]
        if repo.worktree is not None:
            cmd.append(f"--work-tree={repo.worktree}")
        cmd.extend(str(item) for item in args)
        env = dict(os.environ)
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "AIEOS",
            "GIT_AUTHOR_EMAIL": "aieos@local",
            "GIT_COMMITTER_NAME": "AIEOS",
            "GIT_COMMITTER_EMAIL": "aieos@local",
            "LC_ALL": "C",
        })
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        else:
            env.pop("GIT_INDEX_FILE", None)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo.worktree or repo.path),
                env=env,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommitError(
                f"git 执行失败：{type(exc).__name__}: {exc}") from exc
        if check and proc.returncode != 0:
            raise CommitError(
                f"git {' '.join(str(item) for item in args)} "
                f"返回 {proc.returncode}：{str(proc.stderr or '').strip()}")
        return proc


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "CommitBlocked",
    "CommitError",
    "OutputCommitService",
    "EMPTY_TREE",
]
