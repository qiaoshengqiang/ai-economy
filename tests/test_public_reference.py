"""公共资料区 + public_query 动作 —— 轻量测试（不调 LLM）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def ref(tmp_path):
    from services.public_reference import PublicReference

    (tmp_path / "A.md").write_text("内容A", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "B.md").write_text("内容B", encoding="utf-8")
    return PublicReference(tmp_path)


def _dispatch(fn, params):
    from actions import ActionDispatcher, PublicQueryExecutor, build_intent

    return ActionDispatcher([PublicQueryExecutor(fn)]).dispatch(
        build_intent("agent_x", {"type": "public_query", "parameters": params}))


# ---- 资料区本身 ----
def test_list_and_read(ref):
    assert ref.list_names() == ["A.md", "sub/B.md"]
    got = ref.read("A.md")
    assert got["ok"] and got["content"] == "内容A" and got["chars"] == 3


def test_escape_is_refused(ref):
    for bad in ("../secret", "sub/../../x", "..", ""):
        got = ref.read(bad)
        assert got["ok"] is False, bad


def test_absolute_path_cannot_escape(ref):
    """绝对路径被归一化后仍落在公共区内 —— 出不去。"""
    got = ref.read("/etc/passwd")
    assert got["ok"] is False
    assert got.get("code") in ("not_found", "bad_name")


def test_missing_lists_what_exists(ref):
    got = ref.read("nope.md")
    assert got["ok"] is False and got["code"] == "not_found"
    assert "A.md" in got["available"]


def test_it_is_read_only():
    """🔒 结构上没有写接口 —— 不是「有但不给用」。"""
    import ast

    src = (ROOT / "services" / "public_reference.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for forbidden in ("write", "write_text", "save", "create", "delete"):
        assert forbidden not in names, f"公共资料区长了写接口：{forbidden}"


def test_leaf_layer():
    """actions/ 不许 import services/（只看**代码**，不看说明文字）。"""
    import ast

    src = (ROOT / "actions" / "public_query.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    assert not any(m == "services" or m.startswith("services.")
                   for m in mods), sorted(mods)


# ---- 动作 ----
def test_action_lists_and_reads(ref):
    r = _dispatch(ref.query, {})
    assert r.success and r.output["names"] == ["A.md", "sub/B.md"]

    r2 = _dispatch(ref.query, {"name": "sub/B.md"})
    assert r2.success and r2.output["content"] == "内容B"


def test_action_failure_is_structured(ref):
    r = _dispatch(ref.query, {"name": "nope.md"})
    assert r.success is False and r.metadata["code"] == "not_found"
    assert r.output is None


def test_action_survives_a_crashing_fn():
    def boom(_name=None):
        raise RuntimeError("炸了")

    r = _dispatch(boom, {"name": "x"})
    assert r.success is False and r.metadata["code"] == "executor_error"


def test_composition_root_registers_it():
    import ast

    src = (ROOT / "experiments" / "heterogeneous_production"
           / "production.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_action_seam")
    assert "PublicQueryExecutor" in {n.id for n in ast.walk(fn)
                                     if isinstance(n, ast.Name)}
    assert "SYSTEM_MAP.md" in src, "prompt 里没告诉 Agent 去查系统地图"
