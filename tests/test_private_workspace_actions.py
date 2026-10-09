"""私有 Workspace 的三个动作 —— 轻量测试（不调 LLM）。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                  # noqa: E402

load_dotenv(ROOT / ".env")

A, B = "novel_author", "novel_reader"


@pytest.fixture(scope="module")
def disp():
    from actions import (ActionDispatcher, PrivateListExecutor,
                         PrivateReadExecutor, PrivateWriteExecutor)
    from main import build_runtime
    from services.private_workspace import (list_private, read_private,
                                            write_private)

    db = os.path.join(tempfile.mkdtemp(), "priv.db")
    rt = build_runtime(db_path=db, agent_ids=[A, B])

    def ws_of(aid):
        try:
            return rt.economy.agent(aid).workspace
        except Exception:
            return None

    d = ActionDispatcher([
        PrivateWriteExecutor(write_private, workspace_of=ws_of),
        PrivateReadExecutor(read_private, workspace_of=ws_of),
        PrivateListExecutor(list_private, workspace_of=ws_of)])
    try:
        yield d
    finally:
        rt.close()


def go(disp, aid, typ, params=None):
    from actions import build_intent

    return disp.dispatch(build_intent(
        aid, {"type": typ, "parameters": params or {}}))


def test_write_read_list_roundtrip(disp):
    r = go(disp, A, "private_write",
           {"path": "notes/draft.md", "content": "我的节奏笔记"})
    assert r.success and r.output["chars"] == 6

    r2 = go(disp, A, "private_read", {"path": "notes/draft.md"})
    assert r2.success and r2.output["content"] == "我的节奏笔记"

    r3 = go(disp, A, "private_list")
    assert r3.success and "notes/draft.md" in r3.output["paths"]


def test_each_agent_has_its_own_space(disp):
    """🔴 隔离：B 读不到 A 的文件。"""
    go(disp, A, "private_write", {"path": "only_a.md", "content": "A 的"})
    r = go(disp, B, "private_read", {"path": "only_a.md"})
    assert r.success is False
    assert r.metadata["code"] == "not_found"


def test_agent_cannot_spoof_another_agent(disp):
    """🔴 `agent_id` 只取自 intent，不从 parameters 读。"""
    go(disp, B, "private_write",
       {"path": "hack.md", "content": "x", "agent_id": A})
    listing = go(disp, A, "private_list")
    assert "hack.md" not in listing.output["paths"], (
        "parameters 里的 agent_id 被采信了 —— 能写别人的私有空间")


def test_boundaries(disp):
    assert go(disp, A, "private_write",
              {"path": "../x.md", "content": "a"}).metadata["code"] == "bad_path"
    assert go(disp, A, "private_write",
              {"path": "a.md", "content": "  "}).metadata["code"] == "empty_content"
    assert go(disp, A, "private_read",
              {"path": "../secret"}).metadata["code"] == "bad_path"


def test_failure_is_structured(disp):
    r = go(disp, A, "private_read", {"path": "nope.md"})
    assert r.success is False and r.output is None
    assert r.metadata["code"] == "not_found"


def test_leaf_layer():
    """actions/ 不许 import services/ 或 core/。"""
    import ast

    src = (ROOT / "actions" / "private_workspace.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    for bad in ("services", "core", "agents"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), \
            f"private_workspace.py import 了 {bad}: {sorted(mods)}"


def test_composition_root_registers_all_three():
    import ast

    src = (ROOT / "experiments" / "heterogeneous_production"
           / "production.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_action_seam")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    for cls in ("PrivateWriteExecutor", "PrivateReadExecutor",
                "PrivateListExecutor"):
        assert cls in names, f"没注册 {cls}"
