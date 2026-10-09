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


# ================================================================
# 对抗性边界（2026-10-09 加）
#
# 【为什么加】用户确认「对公共资料区的动作只做了查询，没有安全风险」——
#    这句话**必须能被验证**，不能只是相信。下面把当时跑过的对抗用例
#    固化成回归：**符号链接逃逸**、控制字符、以及「一次都不许泄漏」。
# ================================================================
@pytest.fixture
def hostile(tmp_path):
    """一个**故意**布了逃逸的公共资料区 + 一份外面的机密。"""
    root = tmp_path / "public"
    root.mkdir()
    (root / "OK.md").write_text("正常资料", encoding="utf-8")
    secret = tmp_path / "SECRET.txt"
    secret.write_text("机密", encoding="utf-8")

    (root / "link_out.txt").symlink_to(secret)        # 直接指向外面
    (root / "link_dir").symlink_to(tmp_path)          # 指向外面的目录
    (root / "sub").mkdir()
    (root / "sub" / "deep_link").symlink_to(secret)   # 嵌一层
    (root / ".hidden").write_text("隐藏", encoding="utf-8")

    from services.public_reference import PublicReference

    return PublicReference(root), secret


@pytest.mark.parametrize("name", [
    "../SECRET.txt", "..", "/etc/passwd", "~/.ssh/id_rsa",
    "link_out.txt", "link_dir/SECRET.txt", "sub/deep_link",
    "link_dir/../SECRET.txt", "sub/../../SECRET.txt",
    "sub\\..\\..\\SECRET.txt", "", "   ", ".",
])
def test_nothing_escapes_the_public_area(hostile, name):
    """🔴 **一次都不许拿到外面的东西。**"""
    ref, secret = hostile
    got = ref.read(name)
    assert got.get("ok") is False, f"{name!r} 竟然成功了"
    assert got.get("content") != secret.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", [
    "nul\x00.md", "tab\tname", "esc\x1b[31m", "\x7f", "\n",
])
def test_control_characters_are_refused_not_raised(hostile, name):
    """
    🔴 回归：对抗测试抓到的真 bug —— 带 `\\x00` 的名字会让
       `Path.resolve()` 抛 `ValueError: lstat: embedded null character`，
       而 `read()` 的契约说「失败返回**结构化**结果」。
       **抛异常 = 破坏契约**（调用方得靠运气才不崩）。
    """
    ref, _ = hostile
    got = ref.read(name)                     # ← 不许抛
    assert got.get("ok") is False
    assert got.get("code") == "bad_name"


def test_symlinks_are_caught_by_resolve_not_by_string_matching(hostile):
    """逃逸检查必须发生在 `resolve()` **之后**（否则符号链接绕过）。"""
    ref, _ = hostile
    assert ref.read("link_out.txt").get("code") == "outside_public"


def test_listing_skips_hidden_files(hostile):
    ref, _ = hostile
    names = ref.list_names()
    assert ".hidden" not in names
    assert "OK.md" in names


def test_the_area_is_structurally_read_only():
    """
    🔒 「只读」是**结构性**的 —— 不是「有写接口但不给用」。

    对抗性验证的结论要能被守卫钉住，不能只靠当次目测。
    """
    import ast

    src = (ROOT / "services" / "public_reference.py").read_text(encoding="utf-8")
    names = {n.name for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef)}
    for forbidden in ("write", "write_text", "save", "create", "delete",
                      "unlink", "mkdir", "rename", "move"):
        assert forbidden not in names, f"公共资料区长出了写接口：{forbidden}"


def test_only_one_action_type_touches_the_public_area():
    """目前只有 `public_query` 一个动作类型 —— 用户确认过的范围。"""
    import ast

    src = (ROOT / "actions" / "public_query.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    types = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("ACTION_")
                and isinstance(node.value, ast.Constant)):
            types.add(node.value.value)
    assert types == {"public_query"}, sorted(types)
