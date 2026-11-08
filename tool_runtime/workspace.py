"""
Run Workspace —— 每次工具执行的独立物理目录
================================================

【为什么每次都要新目录】

    · pytest 会写 `__pycache__`、`.pytest_cache`、临时文件；
    · Node 测试会写 `test-results`、截图、trace；
    · 两个 Agent 同时跑同一目录会互相踩；
    · 工具二进制可以共享，**运行状态不能共享**。

【输入来源】

    `task`    —— 当前 Task Workspace 的逻辑文件（SQLite 资源树）
    `private` —— Agent 私有 Workspace（宿主机目录，仅文本）

    🔒 两者都只**复制到** run 目录；执行过程不回写 Workspace。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core.workspace_core import WorkspacePathError, normalize_path


class ToolRunWorkspaceError(Exception):
    """run 目录创建 / 物化失败。"""


#: 防止一次物化把整个大 Workspace 灌进 run 目录。
MAX_FILES = 200
MAX_CHARS = 2_000_000


@dataclass(frozen=True)
class RunWorkspace:
    """一次工具执行的物理运行目录。"""

    run_id: str
    root: Path
    work: Path
    home: Path
    tmp: Path

    @classmethod
    def create(cls, base: Path) -> "RunWorkspace":
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        root = (Path(base) / run_id).resolve()
        work = root / "work"
        home = root / "home"
        tmp = root / "tmp"
        for directory in (work, home, tmp):
            directory.mkdir(parents=True, exist_ok=True)
        return cls(run_id=run_id, root=root, work=work, home=home, tmp=tmp)


def safe_join(root: Path, relative: str) -> Path:
    """把逻辑相对路径安全地落到 root 下。"""
    raw = str(relative or "").replace("\\", "/").lstrip("/")
    if not raw or raw in (".", "/"):
        return root
    try:
        normalized = normalize_path(raw)
    except WorkspacePathError as exc:
        raise ToolRunWorkspaceError(f"非法逻辑路径 {relative!r}: {exc}") from exc
    rel = normalized.lstrip("/")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ToolRunWorkspaceError(f"路径跑出 run 目录：{relative!r}")
    return target


def _write_text(target: Path, content: str, stats: dict) -> None:
    if stats["files"] >= MAX_FILES:
        raise ToolRunWorkspaceError(f"物化文件数超过上限 {MAX_FILES}")
    total = stats["chars"] + len(content)
    if total > MAX_CHARS:
        raise ToolRunWorkspaceError(f"物化内容超过上限 {MAX_CHARS} 字符")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    stats["files"] += 1
    stats["chars"] = total


# ======================================================================
# Task Workspace
# ======================================================================
def materialize_task_workspace(
    gateway: Any,
    workspace_id: str,
    agent_id: str,
    source_path: str = "",
    *,
    target: Optional[Path] = None,
) -> dict:
    """
    把 Task Workspace 的逻辑文件复制到 run 目录。

    🔒 只读 Gateway；不修改 Workspace 的任何 revision / lock。
    """
    root = Path(target or ".")
    stats = {"files": 0, "chars": 0}
    start = "/" + str(source_path or "").strip().strip("/")
    if start == "/":
        start = "/"

    def walk(path: str) -> None:
        listed = gateway.list(workspace_id, path, agent_id)
        if not listed.success:
            # 可能是文件而不是目录
            got = gateway.read(workspace_id, path, agent_id)
            if not got.success:
                return
            content = str((got.result or {}).get("content") or "")
            rel = path.lstrip("/")
            _write_text(safe_join(root, rel), content, stats)
            return

        for entry in (listed.result or {}).get("entries", []) or []:
            name = str(entry.get("name") or "")
            if not name:
                continue
            child = f"{path.rstrip('/')}/{name}" if path != "/" else f"/{name}"
            if str(entry.get("type") or "") == "directory":
                walk(child)
            else:
                got = gateway.read(workspace_id, child, agent_id)
                if not got.success:
                    continue
                content = str((got.result or {}).get("content") or "")
                _write_text(safe_join(root, child.lstrip("/")), content, stats)

    walk(start)
    return stats


# ======================================================================
# Private Workspace
# ======================================================================
def materialize_private_workspace(
    workspace: Any,
    source_path: str = "",
    *,
    target: Optional[Path] = None,
) -> dict:
    """
    把 Agent 私有 Workspace 的文本文件复制到 run 目录。

    🔒 只复制；执行不会写回私有 Workspace。
    """
    root = Path(target or ".")
    stats = {"files": 0, "chars": 0}
    prefix = str(source_path or "").replace("\\", "/").lstrip("/").rstrip("/")
    for rel in workspace.list_files():
        rel = str(rel).replace("\\", "/")
        if prefix and rel != prefix and not rel.startswith(prefix + "/"):
            continue
        content = workspace.read_text(rel)
        _write_text(safe_join(root, rel), content, stats)
    return stats
