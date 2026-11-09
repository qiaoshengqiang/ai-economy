"""
SUBMIT 前的 DOM/import 静态 Smoke 检查
=======================================

真实 run `demand_6b2fd87b`：

    index.html R2 删掉了 id="app"，main.js 仍然 `$('app')`；
    SUBMIT 前没有结构检查，页面首帧 TypeError 也没被拦住。

本文件验证：

    ① 缺 DOM id / 缺本地 import 会阻塞 SUBMIT；
    ② 可空检查（if (loading)）只给 warning；
    ③ 未被引用 JS / .js 目录给 warning；
    ④ 门禁失败时 SUBMIT 消息**没有发出**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demand.submit_check import (                              # noqa: E402
    check_submit_deliverable,
    format_submit_check_failure,
)


class _Gateway:
    def __init__(self, files: dict[str, str],
                 dirs: set[str] | None = None):
        self.files = {
            "/" + path.lstrip("/"): content
            for path, content in files.items()}
        self.dirs = {"/" + path.lstrip("/")
                     for path in (dirs or set())}

    def read(self, workspace_id, path, agent_id):
        rel = "/" + str(path or "").lstrip("/")
        if rel in self.files:
            return SimpleNamespace(
                success=True,
                result={"path": rel, "revision": 1,
                        "content": self.files[rel]})
        return SimpleNamespace(
            success=False, result=None,
            error={"code": "RESOURCE_NOT_FOUND",
                   "message": f"missing {rel}"})

    def list(self, workspace_id, path, agent_id):
        rel = "/" + str(path or "").strip("/")
        if rel != "/" and rel not in self.dirs:
            return SimpleNamespace(success=False, result=None)
        prefix = "" if rel == "/" else rel.strip("/") + "/"
        names: dict[str, str] = {}
        for file_path in self.files:
            if not file_path.startswith("/" + prefix):
                continue
            rest = file_path[1 + len(prefix):]
            if not rest:
                continue
            name = rest.split("/", 1)[0]
            child = "/" + prefix + name
            names[name] = ("directory"
                           if child in self.dirs or "/" in rest
                           else "file")
        for dir_path in self.dirs:
            if not dir_path.startswith("/" + prefix):
                continue
            rest = dir_path[1 + len(prefix):]
            if not rest:
                continue
            name = rest.split("/", 1)[0]
            names[name] = "directory"
        return SimpleNamespace(
            success=True,
            result={"entries": [
                {"name": name, "type": kind}
                for name, kind in sorted(names.items())
            ]})


def _index(ids: list[str], scripts: list[str]) -> str:
    body = "".join(f'<div id="{x}"></div>' for x in ids)
    tags = "".join(f'<script type="module" src="{s}"></script>'
                   for s in scripts)
    return f"<!doctype html><html><body>{body}{tags}</body></html>"


def test_pass_when_dom_ids_and_imports_resolve():
    gateway = _Gateway({
        "index.html": _index(["app"], ["./src/main.js"]),
        "src/main.js": (
            "import * as THREE from 'three';\n"
            "const app = $('app');\n"
            "app.appendChild(document.createElement('div'));\n"
        ),
    }, dirs={"src"})
    report = check_submit_deliverable(gateway, "WS", "agent_c")
    assert report["ok"] is True
    assert report["errors"] == []


def test_missing_dom_id_blocks_and_optional_id_only_warns():
    gateway = _Gateway({
        "index.html": _index(["scene"], ["./src/main.js"]),
        "src/main.js": (
            "const app = $('app');\n"
            "app.appendChild(document.createElement('div'));\n"
            "const loading = $('loading');\n"
            "if (loading) loading.classList.add('hide');\n"
        ),
    }, dirs={"src"})
    report = check_submit_deliverable(gateway, "WS", "agent_c")
    assert report["ok"] is False
    assert any("'app'" in e for e in report["errors"])
    assert any("'loading'" in w for w in report["warnings"])
    text = format_submit_check_failure(report)
    assert "SUBMIT 被拒绝" in text
    assert "Task 仍在 IN_PROGRESS" in text


def test_missing_local_import_blocks():
    gateway = _Gateway({
        "index.html": _index(["app"], ["./src/main.js"]),
        "src/main.js": (
            "import { buildWorld } from './world.js';\n"
            "const app = $('app');\n"
        ),
    }, dirs={"src"})
    report = check_submit_deliverable(gateway, "WS", "agent_c")
    assert report["ok"] is False
    assert any("import 不存在" in e for e in report["errors"])


def test_external_imports_are_skipped():
    gateway = _Gateway({
        "index.html": _index(["app"], ["./src/main.js"]),
        "src/main.js": (
            "import * as THREE from 'three';\n"
            "import { OrbitControls } from 'three/addons/controls/OrbitControls.js';\n"
            "const app = $('app');\n"
        ),
    }, dirs={"src"})
    assert check_submit_deliverable(
        gateway, "WS", "agent_c")["ok"] is True


def test_unreferenced_js_and_js_directory_are_warnings_not_errors():
    gateway = _Gateway({
        "index.html": _index(["app"], ["./src/main.js"]),
        "src/main.js": "const app = $('app');\n",
        "src/world.js": "export const world = 1;\n",
    }, dirs={"src", "src/terrain.js"})
    report = check_submit_deliverable(gateway, "WS", "agent_c")
    assert report["ok"] is True
    assert any("world.js" in w for w in report["warnings"])
    assert any("terrain.js" in w and "目录" in w
               for w in report["warnings"])


def test_no_index_html_skips_web_check():
    report = check_submit_deliverable(
        _Gateway({"README.md": "# x"}), "WS", "agent_c")
    assert report["ok"] is True
    assert report["skipped"] is True


# ======================================================================
# SUBMIT 门禁接线：失败时消息不发出
# ======================================================================
class _Comm:
    async def send(self, **kwargs):
        return SimpleNamespace(message_id="m1")

    def is_woken(self, agent_id):
        return False


class _Bridge:
    def __init__(self):
        self.calls = 0

    def run(self, coro):
        import asyncio
        self.calls += 1
        return asyncio.run(coro)


def _submit_dispatcher(files, dirs):
    from demand.actions import build_task_dispatcher
    from core.task import Task

    task = Task.create(
        creator="agent_a", description="d", workspace_id="WS-SUBMIT",
        task_id="t_submit", participants=["agent_a", "agent_c"])
    task.claim_producer("agent_c")
    runtime = SimpleNamespace(
        workspace_gateway=_Gateway(files, dirs),
        communication=_Comm(),
    )
    bridge = _Bridge()
    return build_task_dispatcher(
        runtime, task=task, bridge=bridge), bridge


def _submit_intent():
    from actions.intent import ActionIntent

    return ActionIntent(
        action_type="message_send", agent_id="agent_c",
        parameters={
            "to": "agent_a", "message_type": "SUBMIT",
            "payload": {"task_id": "t_submit"},
        })


def test_submit_gate_blocks_broken_deliverable_without_sending():
    dispatcher, bridge = _submit_dispatcher({
        "index.html": _index(["scene"], ["./src/main.js"]),
        "src/main.js": "const app = $('app');\napp.appendChild(x);\n",
    }, {"src"})
    result = dispatcher.dispatch(_submit_intent())
    assert result.success is False
    assert result.metadata["code"] == "submit_check_failed"
    assert "app" in (result.error or "")
    assert bridge.calls == 0


def test_submit_gate_allows_fixed_deliverable():
    dispatcher, bridge = _submit_dispatcher({
        "index.html": _index(["app"], ["./src/main.js"]),
        "src/main.js": "const app = $('app');\napp.appendChild(x);\n",
    }, {"src"})
    result = dispatcher.dispatch(_submit_intent())
    assert result.success is True
    assert bridge.calls == 1
