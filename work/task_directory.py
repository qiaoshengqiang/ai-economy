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
    · Pi 执行的 SOLO Task 不需要逻辑锁 / revision。
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
    def __init__(
        self,
        root: Path,
        task_id: str,
        *,
        workdir: Optional[Path] = None,
    ) -> None:
        self.task_id = _safe_task_id(task_id)
        self.root = Path(root).expanduser().resolve() / self.task_id
        self.workspace = self.root / "workspace"
        #: project 模式：Human 选择的 workspace_root；
        #: 空 = 继续使用 Task Workspace。
        self.project_root = (
            Path(workdir).expanduser().resolve() if workdir else None)
        #: Worker recon 结束后确定的相对目标路径；空 = 尚未决定。
        self.project_target = ""
        #: 实际 Agent 工作目录。recon 前暂时等于 project_root。
        self.project_workdir = self.project_root
        self.workdir = self.project_root or self.workspace
        self.aieos = self.root / ".aieos"
        self.session_dir = self.aieos / "pi-session"
        # 恢复上次 recon 已经确定的目标；否则 controller 在任务结束后
        # 重新构造 TaskDirectory 时会退回 workspace_root。
        if self.project_root is not None:
            self._load_project_target()

    @property
    def project_mode(self) -> bool:
        return self.project_root is not None

    def _load_project_target(self) -> None:
        path = self.aieos / "workspace_plan.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        target = str(data.get("target_path") or "").strip()
        if not target:
            return
        try:
            self.set_project_target(target)
        except ValueError:
            # 计划与当前目录不一致时不让构造失败；调用方会重新 recon。
            return

    # ------------------------------------------------------------
    def ensure(self) -> "TaskDirectory":
        self.aieos.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if self.project_mode:
            # recon 之前只要求 Human 选的 workspace_root 存在；
            # 目标子目录由 Worker recon 决定，之后再创建。
            if self.project_root is None or not self.project_root.is_dir():
                raise ValueError(f"项目目录不存在：{self.project_root}")
        else:
            self.workspace.mkdir(parents=True, exist_ok=True)
        return self

    def set_project_target(self, target_path: str) -> Path:
        """设置 Worker 决定的相对目标目录；必须仍在 workspace_root 内。"""
        if not self.project_mode or self.project_root is None:
            raise ValueError("当前不是 project 模式")
        raw = str(target_path or ".").strip().replace("\\", "/") or "."
        path = Path(raw)
        if path.is_absolute():
            raise ValueError("项目目标不能是绝对路径")
        if ".." in path.parts:
            raise ValueError("项目目标不能包含 ..")
        candidate = (self.project_root / raw).resolve()
        root = self.project_root
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"项目目标超出 workspace_root：{target_path!r}")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f"项目目标不是目录：{candidate}")
        self.project_target = candidate.relative_to(root).as_posix() or "."
        self.project_workdir = candidate
        self.workdir = candidate
        return candidate

    def ensure_project_target(self, *, create: bool = True) -> Path:
        """确保 Worker 选择的目标目录存在；create=False 时只允许已有目录。"""
        if not self.project_mode or self.project_root is None:
            raise ValueError("当前不是 project 模式")
        target = self.workdir
        if target.exists():
            if not target.is_dir():
                raise ValueError(f"项目目标不是目录：{target}")
            return target
        if not create:
            raise ValueError(f"项目目标不存在：{target}")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def exists(self) -> bool:
        return self.workdir.is_dir()

    # ------------------------------------------------------------
    def resolve(self, relative_path: str) -> Path:
        """把工作目录内的相对路径解析成真实路径，拒绝逃逸。"""
        raw = str(relative_path or "").strip().lstrip("/")
        candidate = (self.workdir / raw).resolve()
        if candidate != self.workdir and self.workdir not in candidate.parents:
            raise ValueError(f"路径超出工作目录：{relative_path!r}")
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
        if not self.workdir.is_dir():
            return []
        files: List[Dict[str, Any]] = []
        for path in sorted(self.workdir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.workdir).as_posix()
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
        if not self.workdir.is_dir():
            return entries
        for path in sorted(self.workdir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.workdir).as_posix()
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
        files: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        entries = list(files) if files is not None else self.manifest()
        payload: Dict[str, Any] = {
            "task_id": self.task_id,
            "workspace_mode": "project" if self.project_mode else "task",
            "workdir": str(self.workdir),
            "files": entries,
        }
        if self.project_mode:
            payload["project_root"] = str(self.project_root)
            payload["project_target"] = self.project_target or "."
        if snapshot:
            payload["snapshot"] = dict(snapshot)
        self.write_json("manifest.json", payload)
        return entries


class TaskDirectoryManager:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or Path(__file__).resolve().parent.parent / "tasks")
        self.root = self.root.expanduser().resolve()

    def for_task(
        self, task_id: str, *, workdir: Optional[Path] = None,
    ) -> TaskDirectory:
        return TaskDirectory(self.root, task_id, workdir=workdir)

    def exists(self, task_id: str) -> bool:
        try:
            return self.for_task(task_id).exists()
        except ValueError:
            return False


__all__ = ["TaskDirectory", "TaskDirectoryManager"]
