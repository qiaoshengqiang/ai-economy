"""
真实 Task 目录 —— Pi Work 的文件系统边界
=========================================

    tasks/<task_id>/
      workspace/        ← Pi 的 cwd；生产内容直接落盘
      .aieos/           ← AIEOS 元数据；不进入交付物
        TASK.md
        manifest.json
        pi-session/

🔒 这与 Workspace Core 的逻辑资源树是两套东西：
    · Pi Work 以真实目录为内容权威；
    · AIEOS 只保存路径、manifest 和 hash；
    · Solo Pi 任务不需要逻辑锁 / revision。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


_SAFE_TASK = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _safe_task_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not value:
        raise ValueError("task_id 不能为空")
    if not _SAFE_TASK.fullmatch(value):
        raise ValueError(f"非法 task_id：{task_id!r}")
    return value


class TaskDirectory:
    def __init__(self, root: Path, task_id: str) -> None:
        self.task_id = _safe_task_id(task_id)
        self.root = Path(root).expanduser().resolve() / self.task_id
        self.workspace = self.root / "workspace"
        self.aieos = self.root / ".aieos"
        self.session_dir = self.aieos / "pi-session"

    # ------------------------------------------------------------
    def ensure(self) -> "TaskDirectory":
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.aieos.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        return self.workspace.is_dir()

    # ------------------------------------------------------------
    def resolve(self, relative_path: str) -> Path:
        """把 workspace 内的相对路径解析成真实路径，拒绝逃逸。"""
        raw = str(relative_path or "").strip().lstrip("/")
        candidate = (self.workspace / raw).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise ValueError(f"路径超出 Task Workspace：{relative_path!r}")
        return candidate

    def write_text(self, relative_path: str, content: str) -> Path:
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return target

    def read_text(self, relative_path: str) -> str:
        return self.resolve(relative_path).read_text(encoding="utf-8")

    def write_meta(self, name: str, content: str) -> Path:
        target = self.aieos / Path(name).name
        target.write_text(str(content), encoding="utf-8")
        return target

    def write_json(self, name: str, payload: Any) -> Path:
        return self.write_meta(
            name, json.dumps(payload, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------
    def list_files(self, *, include_aieos: bool = False) -> List[Dict[str, Any]]:
        if not self.workspace.is_dir():
            return []
        files: List[Dict[str, Any]] = []
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.workspace).as_posix()
            try:
                content = path.read_text(encoding="utf-8")
                chars: Optional[int] = len(content)
            except (OSError, UnicodeDecodeError):
                chars = None
            files.append({
                "path": rel,
                "chars": chars,
                "bytes": path.stat().st_size,
            })
        return files

    def manifest(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if not self.workspace.is_dir():
            return entries
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.workspace).as_posix()
            try:
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                size = len(data)
            except OSError:
                digest = ""
                size = 0
            entries.append({
                "path": rel,
                "bytes": size,
                "sha256": digest,
            })
        return entries

    def write_manifest(
        self,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        entries = self.manifest()
        payload: Dict[str, Any] = {
            "task_id": self.task_id,
            "files": entries,
        }
        if snapshot:
            payload["snapshot"] = dict(snapshot)
        self.write_json("manifest.json", payload)
        return entries


class TaskDirectoryManager:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or Path(__file__).resolve().parent.parent / "tasks")
        self.root = self.root.expanduser().resolve()

    def for_task(self, task_id: str) -> TaskDirectory:
        return TaskDirectory(self.root, task_id)

    def exists(self, task_id: str) -> bool:
        try:
            return self.for_task(task_id).exists()
        except ValueError:
            return False


__all__ = ["TaskDirectory", "TaskDirectoryManager"]
