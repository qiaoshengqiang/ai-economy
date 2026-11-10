"""Task Workspace → 宿主机目录导出。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from work.artifact_export import (
    ArtifactExportError, export_task_workspace,
)


class _Gateway:
    def __init__(self, files):
        self.files = dict(files)

    def list(self, _ws, path, _agent):
        path = "/" + str(path or "").strip("/")
        if path == "/":
            prefix = "/"
        else:
            prefix = path.rstrip("/") + "/"
        names = {}
        for file_path in self.files:
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix):]
            if not rest:
                continue
            head = rest.split("/", 1)[0]
            names[head] = "directory" if "/" in rest else "file"
        entries = [
            {"name": name, "type": kind}
            for name, kind in sorted(names.items())
        ]
        return SimpleNamespace(success=True, result={"entries": entries})

    def read(self, _ws, path, _agent):
        path = "/" + str(path or "").strip("/")
        if path not in self.files:
            return SimpleNamespace(
                success=False, error="not found", result=None)
        content = self.files[path]
        return SimpleNamespace(
            success=True,
            result={"content": content, "revision": 1})


def test_export_task_workspace_writes_files_and_manifest(tmp_path):
    gateway = _Gateway({
        "/index.html": "<h1>hi</h1>",
        "/js/app.js": "console.log('hi')",
    })

    result = export_task_workspace(
        gateway,
        workspace_id="WS-1",
        agent_id="agent_a",
        task_id="task_1",
        dest_root=tmp_path / "exports",
        task_meta={"status": "COMPLETED"},
    )

    root = tmp_path / "exports" / "task_1"
    assert result["ok"] is True
    assert result["path"] == str(root.resolve())
    assert (root / "index.html").read_text() == "<h1>hi</h1>"
    assert (root / "js" / "app.js").read_text() == "console.log('hi')"
    manifest = json.loads((root / "_manifest.json").read_text())
    assert manifest["task_id"] == "task_1"
    assert manifest["status"] == "COMPLETED"
    assert {item["path"] for item in manifest["files"]} == {
        "index.html", "js/app.js"}


def test_export_task_workspace_rejects_empty_workspace(tmp_path):
    with pytest.raises(ArtifactExportError, match="没有任何产物"):
        export_task_workspace(
            _Gateway({}),
            workspace_id="WS-1",
            agent_id="agent_a",
            task_id="task_1",
            dest_root=tmp_path / "exports",
        )
