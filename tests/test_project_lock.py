"""ProjectLock：同一项目目录的互斥。"""

from __future__ import annotations

import pytest

from work.project_lock import ProjectBusyError, ProjectLockManager


def test_same_project_is_locked(tmp_path):
    manager = ProjectLockManager(tmp_path / "locks")
    project = tmp_path / "project"
    project.mkdir()

    first = manager.acquire(project)
    try:
        with pytest.raises(ProjectBusyError):
            manager.acquire(project)
    finally:
        first.release()

    second = manager.acquire(project)
    second.release()


def test_different_projects_can_run_in_parallel(tmp_path):
    manager = ProjectLockManager(tmp_path / "locks")
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    first = manager.acquire(one)
    second = manager.acquire(two)
    try:
        assert first.path != second.path
    finally:
        first.release()
        second.release()
