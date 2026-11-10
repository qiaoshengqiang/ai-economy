"""把 Task Workspace 的逻辑文件导出到宿主机目录。

Workspace 本身是**逻辑资源树**，DB 才是权威。这里只做一次显式的
“导出快照”：读 Gateway → 写宿主机目录 → 返回绝对路径。

🔒 不修改 Workspace / Lock / Revision / Audit。
🔒 只写 `dest_root/<task_id>/`；路径必须留在目标目录内。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from core.identity import utc_now_iso


class ArtifactExportError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_segment(value: Any, fallback: str = "task") -> str:
    text = "".join(
        char for char in str(value or "")
        if char.isalnum() or char in ("_", "-", ".")
    )
    return text.strip(".") or fallback


def _safe_relative(path: Any) -> str:
    parts = []
    for part in str(path or "").replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ArtifactExportError(
                "bad_path", f"产物路径包含 '..': {path!r}")
        parts.append(part)
    return "/".join(parts)


def export_task_workspace(
    gateway: Any,
    *,
    workspace_id: str,
    agent_id: str,
    task_id: str,
    dest_root: Path,
    task_meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """导出全部文本文件，返回 `path / files / count / bytes`。"""
    if gateway is None:
        raise ArtifactExportError("no_gateway", "当前 Runtime 没有 Workspace Gateway")
    workspace_id = str(workspace_id or "").strip()
    agent_id = str(agent_id or "").strip()
    if not workspace_id or not agent_id:
        raise ArtifactExportError("bad_workspace", "Task 没有 Workspace 或执行 Agent")

    root = Path(dest_root).expanduser().resolve()
    target = (root / _safe_segment(task_id)).resolve()
    if root not in target.parents and target != root:
        raise ArtifactExportError("bad_path", "导出目录越界")

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    files: List[Dict[str, Any]] = []
    total_bytes = 0

    def write_file(path: str, content: Any, revision: Any = None) -> None:
        nonlocal total_bytes
        rel = _safe_relative(path)
        if not rel:
            return
        out = (target / rel).resolve()
        if target not in out.parents:
            raise ArtifactExportError(
                "bad_path", f"产物路径越界：{path!r}")
        text = "" if content is None else str(content)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        size = len(text.encode("utf-8"))
        total_bytes += size
        files.append({
            "path": rel,
            "revision": revision,
            "chars": len(text),
            "bytes": size,
        })

    def walk(path: str) -> None:
        try:
            listed = gateway.list(workspace_id, path, agent_id)
        except Exception as exc:                              # noqa: BLE001
            raise ArtifactExportError(
                "list_failed", f"读取目录 {path!r} 失败：{exc}") from exc
        if not getattr(listed, "success", False):
            raise ArtifactExportError(
                "list_failed",
                f"读取目录 {path!r} 失败：{getattr(listed, 'error', '')}")

        payload = getattr(listed, "result", None) or {}
        for entry in payload.get("entries", []) or []:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            child = (
                f"{path.rstrip('/')}/{name}" if path not in ("", "/")
                else f"/{name}"
            )
            if str(entry.get("type") or "") == "directory":
                walk(child)
                continue
            got = gateway.read(workspace_id, child, agent_id)
            if not getattr(got, "success", False):
                raise ArtifactExportError(
                    "read_failed",
                    f"读取文件 {child!r} 失败：{getattr(got, 'error', '')}")
            result = getattr(got, "result", None) or {}
            write_file(
                child,
                result.get("content"),
                revision=result.get("revision", entry.get("revision")),
            )

    walk("/")
    if not files:
        raise ArtifactExportError("no_artifacts", "这个 Task 还没有任何产物文件")

    meta = dict(task_meta or {})
    meta.update({
        "task_id": str(task_id),
        "workspace_id": workspace_id,
        "exported_at": utc_now_iso(),
        "path": str(target),
        "count": len(files),
        "bytes": total_bytes,
        "files": files,
    })
    (target / "_manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "path": str(target),
        "files": files,
        "count": len(files),
        "bytes": total_bytes,
    }
