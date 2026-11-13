"""
Task Snapshot —— Task 私有 Git 快照
====================================

    tasks/<task_id>/
      workspace/              ← 内容权威；Pi 直接写
      .aieos/
        snapshot.git/         ← 独立 GIT_DIR
        snapshot.index        ← 快照索引缓存
        snapshot-ignore       ← 默认忽略规则
        snapshots.json        ← 快照记录

🔒 这不是 Task Workspace 的第二个状态源。
   快照是 workspace 的派生物：只做历史 / diff / restore。
   不写 AIEOS 主仓库，不自动提交主分支。

🔒 这里的所有 git 调用都是受控子进程：
   固定 --git-dir / --work-tree、不经过 shell、超时、失败不影响 Task。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Sequence

from work.task_directory import TaskDirectory

try:  # pragma: no cover - Windows 没有 fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


SNAPSHOT_IGNORE = """\
# AIEOS Task Snapshot 默认忽略规则
.aieos/
.git/
node_modules/
.venv/
venv/
__pycache__/
.pytest_cache/
.DS_Store
*.db-wal
*.db-shm
.env
*.key
*.pem
id_rsa*
secrets/
"""


class SnapshotError(RuntimeError):
    """快照仓库操作失败。"""


@dataclass(frozen=True)
class SnapshotRecord:
    seq: int
    commit: str
    tree: str
    parent: str
    label: str
    phase: str
    turn: int
    files: int
    bytes: int
    skipped: int
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SnapshotRecord":
        return cls(
            seq=int(data.get("seq") or 0),
            commit=str(data.get("commit") or ""),
            tree=str(data.get("tree") or ""),
            parent=str(data.get("parent") or ""),
            label=str(data.get("label") or ""),
            phase=str(data.get("phase") or ""),
            turn=int(data.get("turn") or 0),
            files=int(data.get("files") or 0),
            bytes=int(data.get("bytes") or 0),
            skipped=int(data.get("skipped") or 0),
            created_at=str(data.get("created_at") or ""),
        )


class TaskSnapshotStore:
    """一个 Task 的私有 Git 快照仓库。

    只通过显式的 ``--git-dir`` / ``--work-tree`` 操作，
    不会在 ``workspace`` 里创建 ``.git``。
    """

    def __init__(
        self,
        directory: TaskDirectory,
        *,
        git_binary: str = "git",
        max_file_bytes: int = 2 * 1024 * 1024,
        enabled: bool = True,
    ) -> None:
        self.directory = directory
        self.workspace = Path(directory.workspace)
        self.git_dir = Path(directory.aieos) / "snapshot.git"
        self.index_path = Path(directory.aieos) / "snapshot.index"
        self.records_path = Path(directory.aieos) / "snapshots.json"
        self.ignore_path = Path(directory.aieos) / "snapshot-ignore"
        self.lock_path = Path(directory.aieos) / "snapshot.lock"
        self.max_file_bytes = int(max_file_bytes)
        self.git_binary = str(git_binary or "git")
        self._git = shutil.which(self.git_binary)
        self.enabled = bool(enabled) and self._git is not None
        self._thread_lock = threading.RLock()

    # ------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.enabled and (self.git_dir / "HEAD").exists()

    def ensure(self) -> bool:
        """惰性创建快照仓库；失败时返回 False。"""
        if not self.enabled:
            return False
        self.directory.ensure()
        self._write_ignore()
        if (self.git_dir / "HEAD").exists():
            return True
        self._run(["init", "-q"], check=True)
        self._run(
            ["symbolic-ref", "HEAD", "refs/aieos/main"], check=True)
        self._run(["config", "gc.auto", "0"], check=True)
        return True

    # ------------------------------------------------------------
    def capture(
        self,
        label: str,
        *,
        phase: str = "",
        turn: int = 0,
    ) -> Optional[SnapshotRecord]:
        """抓取当前 workspace 的快照。

        失败会抛 ``SnapshotError``；Task 层负责 fail-open。
        """
        if not self.enabled:
            return None
        with self._lock():
            return self._capture_locked(label, phase=phase, turn=turn)

    def _capture_locked(
        self,
        label: str,
        *,
        phase: str = "",
        turn: int = 0,
    ) -> Optional[SnapshotRecord]:
        if not self.ensure():
            return None
        records = self._load()
        parent = records[-1].commit if records else ""
        previous_paths = self._index_paths(self.index_path)
        temp_index = self._temp_path("index")
        try:
            self._seed_index(temp_index)
            self._run(["add", "-A", "--", "."], index=temp_index)
            skipped = self._drop_oversized(temp_index, previous_paths)
            tree = self._run(
                ["write-tree"], index=temp_index).stdout.strip()
            if not tree:
                raise SnapshotError("git write-tree 没有返回 tree")
            commit = self._commit(
                tree, parent, label, phase=phase, turn=turn)
            self._run(["update-ref", "refs/aieos/main", commit])
            files, size = self._tree_stats(tree)
            record = SnapshotRecord(
                seq=(records[-1].seq + 1) if records else 0,
                commit=commit,
                tree=tree,
                parent=parent,
                label=str(label or "snapshot"),
                phase=str(phase or ""),
                turn=int(turn or 0),
                files=files,
                bytes=size,
                skipped=skipped,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            records.append(record)
            self._save(records)
            os.replace(temp_index, self.index_path)
            return record
        finally:
            temp_index.unlink(missing_ok=True)

    # ------------------------------------------------------------
    def list(self) -> List[SnapshotRecord]:
        with self._lock():
            return self._load()

    def latest(self) -> Optional[SnapshotRecord]:
        records = self.list()
        return records[-1] if records else None

    def summary(self) -> Dict[str, Any]:
        records = self.list()
        if not records:
            return {}
        last = records[-1]
        return {
            **last.to_dict(),
            "count": len(records),
        }

    # ------------------------------------------------------------
    def diff(
        self,
        from_seq: int,
        to_seq: Optional[int] = None,
        *,
        paths: Optional[Sequence[str]] = None,
        context: int = 3,
    ) -> str:
        records = self.list()
        if not records:
            return ""
        source = self._find(records, from_seq)
        target = self._find(
            records, to_seq if to_seq is not None else records[-1].seq)
        # 先校验路径：即使两端相同，非法路径也不能被静默接受。
        normalized = [
            self._safe_path(path) for path in self._path_list(paths)]
        if source.commit == target.commit:
            return ""
        args = [
            "diff", "--no-color", "--no-ext-diff",
            f"--unified={max(0, int(context))}",
            source.commit, target.commit,
        ]
        if normalized:
            args += ["--", *normalized]
        return self._run(args).stdout

    def changed_files(
        self,
        from_seq: int,
        to_seq: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        records = self.list()
        if not records:
            return []
        source = self._find(records, from_seq)
        target = self._find(
            records, to_seq if to_seq is not None else records[-1].seq)
        if source.commit == target.commit:
            return []
        out = self._run([
            "diff", "--name-status", "--no-renames",
            source.commit, target.commit,
        ]).stdout
        items: List[Dict[str, str]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                items.append({
                    "status": parts[0],
                    "path": parts[-1],
                })
        return items

    def file_at(self, seq: int, path: str) -> bytes:
        record = self._find(self._load(), seq)
        rel = self._safe_path(path)
        proc = self._run(
            ["cat-file", "blob", f"{record.commit}:{rel}"],
            binary=True, check=False)
        if proc.returncode != 0:
            raise SnapshotError(f"快照中没有文件：{rel}")
        return proc.stdout

    # ------------------------------------------------------------
    def restore(
        self,
        seq: int,
        paths: Optional[Sequence[str]] = None,
        *,
        exact: bool = False,
        capture: bool = True,
    ) -> Optional[SnapshotRecord]:
        """从快照恢复文件。

        - ``paths``：只恢复这些文件；快照中不存在的 path 会被删除。
        - ``exact=True``：让 workspace 的已跟踪/未忽略文件与快照完全一致；
          额外文件会被删除（忽略规则内的文件不动）。
        - 默认先抓 ``before-restore``，恢复后再抓 ``after-restore``。
        """
        if not self.enabled:
            raise SnapshotError("快照未启用")
        with self._lock():
            records = self._load()
            record = self._find(records, seq)
            if capture:
                self._capture_locked("before-restore", phase="restore")
            if exact:
                wanted = self._tree_paths(record.tree)
                current = self._workspace_paths()
                for rel in sorted(current - wanted):
                    self._remove(rel)
                targets = sorted(wanted)
            else:
                targets = [
                    self._safe_path(path)
                    for path in self._path_list(paths)]
                if not targets:
                    raise ValueError(
                        "restore 需要至少一个 path，或 exact=True")
            for rel in targets:
                self._restore_one(record, rel)
            if capture:
                return self._capture_locked(
                    "after-restore", phase="restore")
            return None

    # ------------------------------------------------------------
    def disk_usage(self) -> int:
        """返回快照仓库的大致字节数；不可用时返回 0。"""
        if not self.enabled or not (self.git_dir / "HEAD").exists():
            return 0
        proc = self._run(["count-objects", "-v"], check=False)
        total = 0
        for line in proc.stdout.splitlines():
            key, _, value = line.partition(":")
            if key in ("size", "size-pack"):
                try:
                    total += int(value.strip()) * 1024
                except ValueError:
                    continue
        return total

    def gc(self) -> None:
        if not self.enabled or not (self.git_dir / "HEAD").exists():
            return
        with self._lock():
            self._run(["gc", "--auto"], check=False)

    # ------------------------------------------------------------
    def _commit(
        self,
        tree: str,
        parent: str,
        label: str,
        *,
        phase: str,
        turn: int,
    ) -> str:
        message = str(label or "snapshot").strip() or "snapshot"
        message = message[:200]
        detail = []
        if phase:
            detail.append(f"phase={phase}")
        if turn:
            detail.append(f"turn={int(turn)}")
        if detail:
            message += "\n\n" + " ".join(detail)
        args = ["commit-tree", tree]
        if parent:
            args += ["-p", parent]
        args += ["-m", message]
        return self._run(args).stdout.strip()

    def _tree_stats(self, tree: str) -> tuple[int, int]:
        out = self._run(["ls-tree", "-r", "-l", tree]).stdout
        files = 0
        total = 0
        for line in out.splitlines():
            meta, _, _path = line.partition("\t")
            parts = meta.split()
            if len(parts) < 4:
                continue
            files += 1
            try:
                total += int(parts[3])
            except ValueError:
                continue
        return files, total

    def _tree_paths(self, tree: str) -> set[str]:
        out = self._run(
            ["ls-tree", "-r", "-z", "--name-only", tree]).stdout
        return {path for path in out.split("\0") if path}

    def _tree_entry(self, tree: str, rel: str) -> Optional[str]:
        out = self._run(
            ["ls-tree", "-z", tree, "--", rel]).stdout
        entry = out.split("\0", 1)[0]
        if not entry:
            return None
        meta, _, path = entry.partition("\t")
        if path != rel:
            return None
        parts = meta.split()
        return parts[0] if parts else None

    def _workspace_paths(self) -> set[str]:
        tracked = self._index_paths(self.index_path)
        out = self._run(
            ["ls-files", "-z", "--others", "--exclude-standard"]).stdout
        others = {path for path in out.split("\0") if path}
        return tracked | others

    def _index_paths(self, index: Path) -> set[str]:
        if not Path(index).exists():
            return set()
        out = self._run(["ls-files", "-z"], index=index).stdout
        return {path for path in out.split("\0") if path}

    def _drop_oversized(
        self, index: Path, previous: set[str],
    ) -> int:
        """把新出现且超过上限的文件移出本次快照。

        已跟踪文件即使变大也保留，避免历史文件被悄悄丢掉。
        """
        skipped = 0
        for rel in sorted(self._index_paths(index) - previous):
            target = self.workspace / rel
            try:
                if target.is_symlink() or not target.is_file():
                    continue
                if target.stat().st_size <= self.max_file_bytes:
                    continue
                self._run(
                    ["update-index", "--force-remove", "--", rel],
                    index=index)
                skipped += 1
            except OSError:
                continue
        return skipped

    def _restore_one(self, record: SnapshotRecord, rel: str) -> None:
        target = self.directory.resolve(rel)
        mode = self._tree_entry(record.tree, rel)
        if mode is None:
            self._remove(rel)
            return
        if mode == "040000":
            prefix = rel.rstrip("/") + "/"
            for path in sorted(self._tree_paths(record.tree)):
                if path.startswith(prefix):
                    self._restore_one(record, path)
            return
        data = self._run(
            ["cat-file", "blob", f"{record.tree}:{rel}"],
            binary=True).stdout
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if mode == "120000":
            os.symlink(data.decode("utf-8"), target)
        elif mode == "160000":
            return
        else:
            target.write_bytes(data)
            os.chmod(target, 0o755 if mode == "100755" else 0o644)

    def _remove(self, rel: str) -> None:
        target = self.directory.resolve(rel)
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    # ------------------------------------------------------------
    def _safe_path(self, path: str) -> str:
        raw = str(path or "").replace("\\", "/").strip()
        if not raw:
            raise ValueError("快照路径不能为空")
        pure = PurePosixPath(raw)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"非法快照路径：{path!r}")
        if pure.parts and pure.parts[0] in (".aieos", ".git"):
            raise ValueError(f"不能恢复元数据目录：{path!r}")
        # 复用 TaskDirectory 的越界检查（包含 symlink 逃逸）。
        self.directory.resolve(raw)
        return pure.as_posix()

    @staticmethod
    def _path_list(
        paths: Optional[Sequence[str]],
    ) -> List[str]:
        if paths is None:
            return []
        if isinstance(paths, str):
            return [paths]
        return [str(path) for path in paths]

    @staticmethod
    def _find(
        records: List[SnapshotRecord], seq: int,
    ) -> SnapshotRecord:
        try:
            wanted = int(seq)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"非法快照序号：{seq!r}") from exc
        for record in records:
            if record.seq == wanted:
                return record
        raise ValueError(f"未知快照序号：{seq!r}")

    # ------------------------------------------------------------
    def _load(self) -> List[SnapshotRecord]:
        if not self.records_path.exists():
            return []
        try:
            data = json.loads(
                self.records_path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("snapshots") if isinstance(data, dict) else []
        records: List[SnapshotRecord] = []
        for item in items or []:
            try:
                records.append(SnapshotRecord.from_dict(item))
            except (TypeError, ValueError):
                continue
        records.sort(key=lambda item: item.seq)
        return records

    def _save(self, records: List[SnapshotRecord]) -> None:
        payload = {
            "version": 1,
            "task_id": self.directory.task_id,
            "snapshots": [record.to_dict() for record in records],
        }
        temp = self._temp_path("snapshots")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
            os.replace(temp, self.records_path)
        finally:
            temp.unlink(missing_ok=True)

    # ------------------------------------------------------------
    def _write_ignore(self) -> None:
        if self.ignore_path.exists():
            return
        self.ignore_path.write_text(SNAPSHOT_IGNORE, encoding="utf-8")

    def _seed_index(self, target: Path) -> None:
        if not self.index_path.exists():
            return
        try:
            os.link(self.index_path, target)
        except OSError:
            shutil.copy2(self.index_path, target)

    def _temp_path(self, prefix: str) -> Path:
        return Path(self.directory.aieos) / (
            f".{prefix}-{os.getpid()}-{uuid.uuid4().hex}.tmp")

    # ------------------------------------------------------------
    def _base_cmd(self) -> List[str]:
        return [
            self._git or self.git_binary,
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.workspace}",
            "-c", f"core.excludesFile={self.ignore_path}",
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
        ]

    @staticmethod
    def _env(index: Optional[Path] = None) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LC_ALL": "C",
            "GIT_AUTHOR_NAME": "AIEOS",
            "GIT_AUTHOR_EMAIL": "aieos@local",
            "GIT_COMMITTER_NAME": "AIEOS",
            "GIT_COMMITTER_EMAIL": "aieos@local",
        })
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        else:
            env.pop("GIT_INDEX_FILE", None)
        return env

    def _run(
        self,
        args: Sequence[str],
        *,
        index: Optional[Path] = None,
        check: bool = True,
        binary: bool = False,
        timeout: float = 120.0,
    ) -> subprocess.CompletedProcess:
        cmd = self._base_cmd() + [str(item) for item in args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.workspace),
                env=self._env(index),
                capture_output=True,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SnapshotError(
                f"git 执行失败：{type(exc).__name__}: {exc}") from exc
        if check and proc.returncode != 0:
            stderr = proc.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise SnapshotError(
                f"git {' '.join(str(item) for item in args)} "
                f"返回 {proc.returncode}：{str(stderr or '').strip()}")
        return proc

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self._thread_lock:
            Path(self.directory.aieos).mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, "a+")
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()


__all__ = [
    "SNAPSHOT_IGNORE",
    "SnapshotError",
    "SnapshotRecord",
    "TaskSnapshotStore",
]
