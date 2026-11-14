"""平台适配层的契约测试。

每个适配器都必须满足同一套语义；Windows 骨架还会断言
「未实现的能力显式失败」，避免静默降级。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.platform_support import (
    PlatformCapabilityError,
    PlatformNotSupportedError,
    default_platform,
    select_platform,
)
from core.platform_support.windows import WindowsAdapter

#: POSIX 行为只能在 POSIX 主机上验证；Windows 贡献者仍可跑
#: 平台选择 / Windows 骨架 / 守卫测试。
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="POSIX 行为测试不能在 Windows 主机上运行")


# ------------------------------------------------------------
# 选择
# ------------------------------------------------------------
@POSIX_ONLY
@pytest.mark.parametrize("system", ["Darwin", "darwin", "Linux", "linux"])
def test_select_platform_returns_posix_adapter(system):
    from core.platform_support.posix import PosixAdapter

    adapter = select_platform(system)
    assert isinstance(adapter, PosixAdapter)
    assert adapter.name == "posix"
    assert adapter.missing_capabilities() == []


@pytest.mark.parametrize("system", ["Windows", "windows", "win32", "nt"])
def test_select_platform_returns_windows_adapter(system):
    adapter = select_platform(system)
    assert isinstance(adapter, WindowsAdapter)
    assert adapter.name == "windows"


def test_select_platform_rejects_unknown_system():
    with pytest.raises(PlatformNotSupportedError) as excinfo:
        select_platform("Plan9")
    assert "Plan9" in str(excinfo.value)
    assert "PLATFORM_COMPATIBILITY.md" in str(excinfo.value)


def test_default_platform_matches_host():
    adapter = default_platform()
    expected = "windows" if os.name == "nt" else "posix"
    assert adapter.name == expected


# ------------------------------------------------------------
# POSIX 契约
# ------------------------------------------------------------
@POSIX_ONLY
def test_posix_spawn_kwargs_use_new_session():
    from core.platform_support.posix import PosixAdapter

    assert PosixAdapter().spawn_kwargs() == {"start_new_session": True}


@POSIX_ONLY
def test_posix_file_lock_is_exclusive_and_releases(tmp_path):
    from core.platform_support.posix import PosixAdapter

    adapter = PosixAdapter()
    lock_path = tmp_path / "test.lock"
    first = open(lock_path, "a+")
    second = open(lock_path, "a+")
    try:
        adapter.file_lock(first, blocking=False)
        with pytest.raises(BlockingIOError):
            adapter.file_lock(second, blocking=False)

        adapter.file_unlock(first)
        adapter.file_lock(second, blocking=False)
        adapter.file_unlock(second)
    finally:
        first.close()
        second.close()


@POSIX_ONLY
def test_posix_kill_tree_terminates_process_group():
    from core.platform_support.posix import PosixAdapter

    adapter = PosixAdapter()
    # 父进程再启动一个子进程，验证整组都被终止。
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        **adapter.spawn_kwargs(),
    )
    try:
        time.sleep(0.2)
        adapter.kill_tree(proc)
        proc.wait(timeout=10)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@POSIX_ONLY
def test_posix_symlink_and_chmod(tmp_path):
    from core.platform_support.posix import PosixAdapter

    adapter = PosixAdapter()
    target = tmp_path / "target.txt"
    target.write_text("hello", encoding="utf-8")
    link = tmp_path / "link.txt"

    adapter.create_symlink(str(target), link)
    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "hello"

    adapter.chmod(target, 0o755)
    assert target.stat().st_mode & 0o777 == 0o755


# ------------------------------------------------------------
# Windows 骨架
# ------------------------------------------------------------
def test_windows_spawn_kwargs_use_process_group():
    kwargs = WindowsAdapter().spawn_kwargs()
    assert "creationflags" in kwargs
    assert kwargs["creationflags"] == 0x00000200


def test_windows_reports_missing_capabilities():
    missing = WindowsAdapter().missing_capabilities()
    assert any("kill_tree" in item for item in missing)
    assert any("file_lock" in item for item in missing)
    assert any("create_symlink" in item for item in missing)
    assert all("WIN-" in item for item in missing)


@pytest.mark.parametrize(
    "capability",
    ["kill_tree", "file_lock", "create_symlink"],
)
def test_windows_unimplemented_capabilities_fail_loudly(capability):
    adapter = WindowsAdapter()
    args = {
        "kill_tree": (None,),
        "file_lock": (None,),
        "create_symlink": ("target", "link"),
    }[capability]
    with pytest.raises(PlatformCapabilityError) as excinfo:
        getattr(adapter, capability)(*args)
    message = str(excinfo.value)
    assert capability in message
    assert "PLATFORM_COMPATIBILITY.md" in message
    assert "尚未实现" in message


def test_windows_chmod_is_documented_noop(tmp_path):
    # Windows 没有 POSIX mode；这是有意的 no-op，不是缺失实现。
    WindowsAdapter().chmod(tmp_path / "whatever", 0o755)
    assert WindowsAdapter().file_unlock(None) is None


def test_windows_adapter_does_not_silently_unlock_project_lock(tmp_path):
    """最严重的回归风险：Windows 上项目锁变成 no-op。

    适配器缺失能力时，调用点必须失败，而不是继续以“无锁”状态执行。
    """
    from work.project_lock import ProjectLockManager

    manager = ProjectLockManager(
        tmp_path / "locks", platform=WindowsAdapter())
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(PlatformCapabilityError):
        manager.acquire(project)
