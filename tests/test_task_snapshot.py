"""Task Snapshot：私有 Git 快照的单元测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from work.task_directory import TaskDirectory
from work.task_snapshot import SnapshotError, TaskSnapshotStore
from work.task_snapshot import SNAPSHOT_IGNORE

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="需要 git")


def _directory(tmp_path: Path, task_id: str = "task_snapshot") -> TaskDirectory:
    return TaskDirectory(tmp_path / "tasks", task_id).ensure()


def _store(tmp_path: Path, **kwargs) -> TaskSnapshotStore:
    return TaskSnapshotStore(_directory(tmp_path), **kwargs)


def test_capture_diff_and_restore_selected_files(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace

    baseline = store.capture("baseline")
    assert baseline is not None
    assert baseline.seq == 0
    assert baseline.files == 0

    (workspace / "hello.txt").write_text("one", encoding="utf-8")
    first = store.capture(
        "phase:prototype:end", phase="prototype", turn=2)
    assert first is not None
    assert first.files == 1
    assert first.phase == "prototype"
    assert first.turn == 2

    (workspace / "hello.txt").write_text("two", encoding="utf-8")
    (workspace / "extra.txt").write_text("extra", encoding="utf-8")
    final = store.capture("final")
    assert final is not None
    assert final.files == 2

    assert [item.label for item in store.list()] == [
        "baseline", "phase:prototype:end", "final"]

    diff = store.diff(first.seq, final.seq)
    assert "-one" in diff
    assert "+two" in diff
    assert "+extra" in diff

    changed = store.changed_files(baseline.seq, final.seq)
    assert {"status": "A", "path": "hello.txt"} in changed
    assert {"status": "A", "path": "extra.txt"} in changed

    assert store.file_at(first.seq, "hello.txt") == b"one"

    store.restore(first.seq, ["hello.txt"], capture=False)
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "one"
    # 只恢复指定文件，不删除其他文件。
    assert (workspace / "extra.txt").exists()

    # 快照里不存在的 path：恢复等于删除。
    (workspace / "gone.txt").write_text("gone", encoding="utf-8")
    store.capture("with-gone")
    store.restore(first.seq, ["gone.txt"], capture=False)
    assert not (workspace / "gone.txt").exists()


def test_restore_directory_and_single_string_path(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace
    folder = workspace / "src"
    folder.mkdir()
    (folder / "a.py").write_text("a = 1\n", encoding="utf-8")
    (folder / "b.py").write_text("b = 2\n", encoding="utf-8")
    first = store.capture("one")

    (folder / "a.py").write_text("a = 99\n", encoding="utf-8")
    (folder / "b.py").unlink()
    store.capture("two")

    store.restore(first.seq, "src", capture=False)

    assert (folder / "a.py").read_text(encoding="utf-8") == "a = 1\n"
    assert (folder / "b.py").read_text(encoding="utf-8") == "b = 2\n"


def test_restore_exact_matches_snapshot(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    first = store.capture("one")
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    store.capture("two")

    store.restore(first.seq, exact=True, capture=False)

    assert (workspace / "a.txt").exists()
    assert not (workspace / "b.txt").exists()


def test_capture_records_restore_and_gc(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    store.capture("one")

    store.restore(0, ["a.txt"], capture=True)

    labels = [item.label for item in store.list()]
    assert "before-restore" in labels
    assert "after-restore" in labels
    assert store.disk_usage() >= 0
    store.gc()


def test_ignored_paths_and_large_files_are_not_snapshotted(tmp_path):
    store = _store(tmp_path, max_file_bytes=1024)
    workspace = store.workspace

    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "pkg.js").write_text(
        "x", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    (workspace / "big.bin").write_bytes(b"x" * 2048)
    (workspace / "small.txt").write_text("small", encoding="utf-8")

    record = store.capture("one")

    assert record is not None
    assert record.files == 1
    assert record.skipped == 1
    assert record.bytes == 5
    assert store.file_at(record.seq, "small.txt") == b"small"
    for ignored in ("node_modules/pkg.js", ".env", "big.bin"):
        with pytest.raises(SnapshotError):
            store.file_at(record.seq, ignored)


def test_workspace_never_gets_a_git_directory(tmp_path):
    store = _store(tmp_path)
    store.capture("baseline")

    assert not (store.workspace / ".git").exists()
    assert (store.git_dir / "HEAD").is_file()


def test_snapshot_ignore_file_is_written(tmp_path):
    store = _store(tmp_path)
    store.capture("baseline")

    text = store.ignore_path.read_text(encoding="utf-8")
    assert text == SNAPSHOT_IGNORE
    assert ".env" in text
    assert "node_modules/" in text


def test_path_escape_is_rejected(tmp_path):
    store = _store(tmp_path)
    store.capture("baseline")

    with pytest.raises(ValueError):
        store.restore(0, ["../outside.txt"])
    with pytest.raises(ValueError):
        store.diff(0, 0, paths=["../outside.txt"])
    with pytest.raises(ValueError):
        store.file_at(0, "/etc/passwd")


def test_symlink_escape_is_rejected(tmp_path):
    store = _store(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (store.workspace / "link.txt").symlink_to(outside)
    record = store.capture("one")

    with pytest.raises(ValueError):
        store.file_at(record.seq, "link.txt")


def test_concurrent_capture_keeps_records_consistent(tmp_path):
    store = _store(tmp_path)
    workspace = store.workspace

    def capture(index: int):
        (workspace / f"file_{index}.txt").write_text(
            f"value {index}", encoding="utf-8")
        return store.capture(f"thread:{index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(capture, range(8)))

    assert all(record is not None for record in records)
    stored = store.list()
    assert len(stored) == 8
    assert sorted(record.seq for record in stored) == list(range(8))


def test_missing_git_is_fail_open(tmp_path):
    store = TaskSnapshotStore(
        _directory(tmp_path), git_binary="/nonexistent/git")
    assert store.enabled is False
    assert store.capture("baseline") is None
    assert store.list() == []


def test_snapshot_ignores_inherited_git_environment(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "README.md").write_text("project", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t",
         "commit", "-qm", "init"],
        cwd=project, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project,
        capture_output=True, text=True, check=True).stdout.strip()

    monkeypatch.setenv("GIT_DIR", str(project / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(project))
    store = _store(tmp_path)
    record = store.capture("baseline")

    assert record is not None
    assert record.files == 0
    with pytest.raises(SnapshotError):
        store.file_at(record.seq, "README.md")
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project,
        capture_output=True, text=True, check=True).stdout.strip() == head


def test_manifest_can_carry_snapshot_metadata(tmp_path):
    directory = _directory(tmp_path)
    (directory.workspace / "a.txt").write_text("a", encoding="utf-8")
    store = TaskSnapshotStore(directory)
    record = store.capture("final")

    entries = directory.write_manifest(snapshot=store.summary())
    payload = json.loads(
        (directory.aieos / "manifest.json").read_text(encoding="utf-8"))

    assert entries[0]["path"] == "a.txt"
    assert payload["snapshot"]["seq"] == record.seq
    assert payload["snapshot"]["commit"] == record.commit
    assert payload["snapshot"]["tree"] == record.tree
    assert payload["snapshot"]["count"] == 1


def test_nested_git_repo_does_not_break_project_snapshot(tmp_path):
    directory = _directory(tmp_path)
    workspace = directory.workspace
    (workspace / "hello.py").write_text("print('hello')\n", encoding="utf-8")
    nested = workspace / "nested-project"
    nested.mkdir()
    (nested / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)

    store = TaskSnapshotStore(directory)
    record = store.capture("baseline")

    assert record is not None
    assert [item["path"] for item in store.file_entries(record.seq)] == [
        "hello.py"]
