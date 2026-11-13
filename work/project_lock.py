"""
ProjectLock —— 同一项目目录同时只允许一个 Task 修改
=====================================================

    Project A ──► task_1（持有锁）
                task_2（拒绝：项目忙）

🔒 直接修改用户项目时，两个 Agent 同时写同一目录会产生不可预期的覆盖。
   这不是 Workspace 的逻辑锁，而是**进程/宿主机级**的目录互斥。

实现：`fcntl.flock(LOCK_EX | LOCK_NB)`，锁文件放在 AIEOS 运行目录，
不写用户项目。
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

try:  # pragma: no cover - Windows 没有 fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class ProjectBusyError(RuntimeError):
    """项目目录正在被另一个 Task 使用。"""


class ProjectLock:
    def __init__(self, path: Path, handle) -> None:
        self.path = path
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class ProjectLockManager:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(
            root or Path(__file__).resolve().parent.parent
            / "runs" / "project_locks"
        ).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _lock_path(self, project_path: Path) -> Path:
        digest = hashlib.sha256(
            str(Path(project_path).expanduser().resolve()).encode("utf-8")
        ).hexdigest()[:24]
        return self.root / f"{digest}.lock"

    def acquire(self, project_path: Path) -> ProjectLock:
        path = self._lock_path(project_path)
        handle = open(path, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise ProjectBusyError(
                    f"项目目录正在被另一个 Task 使用：{project_path}"
                ) from exc
        return ProjectLock(path, handle)

    @contextmanager
    def hold(self, project_path: Path) -> Iterator[ProjectLock]:
        lock = self.acquire(project_path)
        try:
            yield lock
        finally:
            lock.release()


__all__ = ["ProjectBusyError", "ProjectLock", "ProjectLockManager"]
