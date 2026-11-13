"""OutputCommitService：输出仓库提交的契约测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from commit.service import CommitBlocked, CommitError, OutputCommitService
from commit.store import CommitStore
from core.task import TaskStatus
from work.task_directory import TaskDirectoryManager
from work.task_snapshot import TaskSnapshotStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="需要 git")


def _task(task_id: str = "task_commit", iteration: int = 1):
    return SimpleNamespace(
        task_id=task_id,
        iteration=iteration,
        status=TaskStatus.COMPLETED,
        contractor="agent_b",
        creator="agent_b",
        description="commit service test",
    )


def _setup(tmp_path: Path, *, task_id: str = "task_commit"):
    manager = TaskDirectoryManager(tmp_path / "tasks")
    directory = manager.for_task(task_id).ensure()
    (directory.workspace / "hello.txt").write_text(
        "hello\n", encoding="utf-8")
    (directory.workspace / "TASK_DOC.md").write_text(
        "AIEOS metadata\n", encoding="utf-8")
    store = TaskSnapshotStore(directory)
    record = store.capture("final")
    assert record is not None
    return manager, directory, record


def _service(db, manager, output_repo: Path, settings=None, **overrides):
    values = {
        "output_repo": str(output_repo),
        "base_ref": "main",
        "auto_merge": False,
        "max_file_bytes": 20 * 1024 * 1024,
        "max_total_bytes": 50 * 1024 * 1024,
    }
    values.update(settings or {})
    return OutputCommitService(
        task_directories=manager,
        store=CommitStore(db),
        settings=lambda: dict(values),
        project_root=output_repo.parent / "aieos-project",
        **overrides,
    )


def _git(repo: Path, *args):
    return subprocess.run(
        ["git", f"--git-dir={repo / '.git'}", *args],
        capture_output=True, text=True,
    )


def test_manual_requires_confirmation_then_commits_to_task_branch(
    db, tmp_path,
):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")

    request = service.on_task_completed(task, mode="manual")
    assert request is not None
    assert request.status == "AWAITING_HUMAN"
    assert _git(tmp_path / "output", "show-ref", "--verify",
                f"refs/heads/aieos/task-{task.task_id}").returncode != 0

    done = service.confirm(task, request.code)
    assert done.status == "COMMITTED"
    assert done.commit_sha

    shown = _git(
        tmp_path / "output",
        "show", f"refs/heads/aieos/task-{task.task_id}:"
                f"tasks/{task.task_id}/hello.txt",
    )
    assert shown.returncode == 0
    assert shown.stdout == "hello\n"
    # AIEOS 元数据不进入输出仓库。
    assert _git(
        tmp_path / "output",
        "cat-file", "-e", f"refs/heads/aieos/task-{task.task_id}:"
                          f"tasks/{task.task_id}/TASK_DOC.md",
    ).returncode != 0
    # manual 不自动合并到 main。
    assert _git(
        tmp_path / "output",
        "cat-file", "-e", f"refs/heads/main:tasks/{task.task_id}/hello.txt",
    ).returncode != 0


def test_limited_commits_automatically_without_merge(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")

    request = service.on_task_completed(task, mode="limited")

    assert request is not None
    assert request.status == "COMMITTED"
    assert _git(
        tmp_path / "output",
        "show", f"refs/heads/aieos/task-{task.task_id}:"
                f"tasks/{task.task_id}/hello.txt",
    ).stdout == "hello\n"
    assert _git(
        tmp_path / "output",
        "cat-file", "-e", "refs/heads/main:tasks/"
        f"{task.task_id}/hello.txt",
    ).returncode != 0


def test_auto_merge_fast_forwards_base_branch(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(
        db, manager, tmp_path / "output", settings={"auto_merge": True})

    request = service.on_task_completed(task, mode="auto")

    assert request is not None
    assert request.status == "COMMITTED"
    assert request.merge_sha == request.commit_sha
    assert _git(
        tmp_path / "output",
        "show", f"refs/heads/main:tasks/{task.task_id}/hello.txt",
    ).stdout == "hello\n"


def test_completion_is_idempotent(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")

    first = service.on_task_completed(task, mode="manual")
    second = service.on_task_completed(task, mode="manual")

    assert first is not None and second is not None
    assert first.request_id == second.request_id
    assert len(service.store.list_for_task(task.task_id)) == 1


def test_rework_updates_the_same_task_branch(db, tmp_path):
    manager, directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")
    first = service.on_task_completed(task, mode="limited")
    assert first is not None and first.status == "COMMITTED"

    (directory.workspace / "hello.txt").write_text(
        "hello v2\n", encoding="utf-8")
    TaskSnapshotStore(directory).capture("final")
    task.iteration = 2
    second = service.on_task_completed(task, mode="limited")
    assert second is not None and second.status == "COMMITTED"
    assert second.commit_sha != first.commit_sha

    shown = _git(
        tmp_path / "output",
        "show", f"refs/heads/aieos/task-{task.task_id}:"
                f"tasks/{task.task_id}/hello.txt",
    )
    assert shown.stdout == "hello v2\n"
    parent = _git(
        tmp_path / "output",
        "rev-parse", f"{second.commit_sha}^",
    ).stdout.strip()
    assert parent == first.commit_sha


def test_empty_snapshot_is_blocked(db, tmp_path):
    manager = TaskDirectoryManager(tmp_path / "tasks")
    directory = manager.for_task("task_empty").ensure()
    TaskSnapshotStore(directory).capture("final")
    task = _task("task_empty")
    service = _service(db, manager, tmp_path / "output")

    request = service.on_task_completed(task, mode="limited")

    assert request is not None
    assert request.status == "BLOCKED"
    assert "空" in request.error


def test_oversized_snapshot_is_blocked(db, tmp_path):
    manager = TaskDirectoryManager(tmp_path / "tasks")
    directory = manager.for_task("task_big").ensure()
    (directory.workspace / "big.txt").write_text(
        "x" * 100, encoding="utf-8")
    TaskSnapshotStore(directory).capture("final")
    task = _task("task_big")
    service = _service(
        db, manager, tmp_path / "output",
        settings={"max_file_bytes": 10})

    request = service.on_task_completed(task, mode="limited")

    assert request is not None
    assert request.status == "BLOCKED"
    assert "上限" in request.error


def test_commit_only_reads_task_workspace(db, tmp_path):
    manager, directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")
    before = {
        path.relative_to(directory.workspace).as_posix(): path.read_bytes()
        for path in directory.workspace.rglob("*")
        if path.is_file()
    }
    snapshots_before = (
        directory.aieos / "snapshots.json").read_text(encoding="utf-8")

    request = service.on_task_completed(task, mode="limited")

    assert request is not None and request.status == "COMMITTED"
    after = {
        path.relative_to(directory.workspace).as_posix(): path.read_bytes()
        for path in directory.workspace.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (directory.workspace / ".git").exists()
    assert (
        directory.aieos / "snapshots.json"
    ).read_text(encoding="utf-8") == snapshots_before


def test_reopen_makes_pending_request_stale(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")
    request = service.on_task_completed(task, mode="manual")
    assert request is not None

    task.iteration = 2
    with pytest.raises(CommitError, match="失效"):
        service.confirm(task, request.code)

    stored = service.store.find(request.request_id)
    assert stored is not None
    assert stored.status == "STALE"


def test_reopen_invalidates_pending_request(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")
    request = service.on_task_completed(task, mode="manual")
    assert request is not None

    service.invalidate_task(task.task_id)

    stored = service.store.find(request.request_id)
    assert stored is not None
    assert stored.status == "STALE"


def test_output_repo_cannot_be_aieos_project_root(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    project_root = tmp_path / "aieos-project"
    project_root.mkdir()
    service = OutputCommitService(
        task_directories=manager,
        store=CommitStore(db),
        settings=lambda: {
            "output_repo": str(project_root),
            "base_ref": "main",
            "auto_merge": False,
            "max_file_bytes": 1024,
            "max_total_bytes": 2048,
        },
        project_root=project_root,
    )

    request = service.on_task_completed(task, mode="limited")

    assert request is not None
    assert request.status == "BLOCKED"
    assert "主仓库" in request.error


def test_confirm_without_code_uses_the_only_pending_request(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    task = _task()
    service = _service(db, manager, tmp_path / "output")
    request = service.on_task_completed(task, mode="manual")
    assert request is not None

    done = service.confirm(task, "")

    assert done.status == "COMMITTED"
    assert done.commit_sha


def test_explicit_project_repo_is_used_and_recorded(db, tmp_path):
    manager, _directory, _record = _setup(tmp_path)
    project = tmp_path / "other-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    task = _task()
    service = _service(db, manager, tmp_path / "output")

    info = service.inspect_repo(str(project))
    assert info["is_git_repo"] is True

    request = service.on_task_completed(
        task, mode="manual", output_repo=str(project),
        repo_mode="project")
    assert request is not None
    assert request.status == "AWAITING_HUMAN"
    assert request.repo_mode == "project"
    assert request.output_repo == str(project.resolve())

    done = service.confirm(task, request.code)
    assert done.status == "COMMITTED"
    assert _git(
        project, "show", f"refs/heads/aieos/task-{task.task_id}:"
        f"tasks/{task.task_id}/hello.txt",
    ).stdout == "hello\n"
