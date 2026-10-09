"""
资源结构 / 责任三件套 —— 轻量测试（不调 LLM）。

🔴 为什么这三件是**必需**的（不是锦上添花）：
   根目录锁在 root Controller 手里，新建的顶层目录**继承**它 ——
   所以**没有 `handoff`，除 root Controller 外谁都没法写**。
   这就是「去掉 `ARTIFACT_NAMESPACES` 预设分工」的前置条件。
"""
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

ROOT_AGENT = "novel_author"          # 第一个 Agent = root Controller
OTHER, THIRD = "novel_reader", "novel_editor"
AGENTS = [ROOT_AGENT, OTHER, THIRD]
WS = "WS-RC-TEST"


@pytest.fixture(scope="module")
def disp():
    from actions import (ActionDispatcher, ResourceCreateExecutor,
                         ResourceHandoffExecutor, ResourceReleaseExecutor,
                         WorkspaceWriteExecutor)
    from core.task import Task
    from main import build_runtime
    from services.artifacts import write_artifact
    from services.resource_control import (create_directory, handoff_resource,
                                           release_resource)

    db = os.path.join(tempfile.mkdtemp(), "rc.db")
    rt = build_runtime(db_path=db, agent_ids=list(AGENTS))
    gw = rt.workspace_gateway
    rt.tasks.save(Task.create(creator=ROOT_AGENT, description="d",
                              workspace_id=WS, task_id="t_rc",
                              participants=list(AGENTS)))
    gw.ensure_workspace(WS, ROOT_AGENT)      # 第一个 Agent = root Controller

    def bind(fn):
        return lambda aid, p, *a, **k: fn(gw, WS, aid, p, *a, **k)

    d = ActionDispatcher([
        ResourceCreateExecutor(bind(create_directory)),
        ResourceHandoffExecutor(bind(handoff_resource)),
        ResourceReleaseExecutor(bind(release_resource)),
        WorkspaceWriteExecutor(write_artifact, gateway=gw, workspace_id=WS)])
    try:
        yield d, gw
    finally:
        rt.close()


def go(disp, aid, typ, params=None):
    from actions import build_intent

    return disp.dispatch(build_intent(
        aid, {"type": typ, "parameters": params or {}}))


# ---- 建目录 ----
def test_only_root_controller_can_create_at_top_level(disp):
    d, _ = disp
    assert go(d, ROOT_AGENT, "resource_create", {"path": "area_a"}).success
    r = go(d, OTHER, "resource_create", {"path": "area_b"})
    assert r.success is False and r.metadata["code"] == "locked_by_other"


def test_create_makes_missing_intermediate_dirs(disp):
    d, gw = disp
    r = go(d, ROOT_AGENT, "resource_create", {"path": "deep/a/b/c"})
    assert r.success and r.output["path"] == "deep/a/b/c"
    assert gw.read(WS, "/deep/a/b", ROOT_AGENT).success


# ---- 交接 ----
def test_handoff_is_what_lets_others_write(disp):
    """
    🔴 这条是这三件套存在的**理由**：交接之前别人写不进去，
       交接之后就能了 —— 而**没有**放宽任何权限。
    """
    d, _ = disp
    assert go(d, ROOT_AGENT, "resource_create", {"path": "proj"}).success

    # 交接前：别人写不进去
    before = go(d, OTHER, "workspace_write",
                {"path": "proj/x.md", "content": "1", "write_mode": "create"})
    assert before.success is False
    assert before.metadata["code"] == "locked_by_other"

    assert go(d, ROOT_AGENT, "resource_handoff",
              {"path": "proj", "to": OTHER, "reason": "交给你"}).success

    # 交接后：能写了
    after = go(d, OTHER, "workspace_write",
               {"path": "proj/x.md", "content": "1", "write_mode": "create"})
    assert after.success is True


def test_handoff_does_not_open_it_to_everyone(disp):
    """交接给一个人 ≠ 所有人都能写。"""
    d, _ = disp
    assert go(d, ROOT_AGENT, "resource_create", {"path": "solo"}).success
    assert go(d, ROOT_AGENT, "resource_handoff",
              {"path": "solo", "to": OTHER}).success
    r = go(d, THIRD, "workspace_write",
           {"path": "solo/y.md", "content": "1", "write_mode": "create"})
    assert r.success is False and r.metadata["code"] == "locked_by_other"


# ---- release ----
def test_release_needs_an_existing_resource(disp):
    r = go(disp[0], OTHER, "resource_release", {"path": "never_made"})
    assert r.success is False and r.metadata["code"] == "not_found"


def test_root_release_is_refused(disp):
    """根不能 release —— 那会让整棵树失去责任源头。"""
    r = go(disp[0], ROOT_AGENT, "resource_release", {"path": "/"})
    assert r.success is False
    assert r.metadata["code"] in ("bad_path", "limit_exceeded")


# ---- 边界 ----
@pytest.mark.parametrize("typ", ["resource_create", "resource_release"])
@pytest.mark.parametrize("path", ["/etc/passwd", "../up", "C:/x", "a\\b", ""])
def test_bad_paths_are_refused(disp, typ, path):
    r = go(disp[0], ROOT_AGENT, typ, {"path": path})
    assert r.success is False
    assert r.metadata["code"] == "bad_path"


def test_cannot_hand_off_to_yourself(disp):
    r = go(disp[0], ROOT_AGENT, "resource_handoff",
           {"path": "area_a", "to": ROOT_AGENT})
    assert r.success is False and r.metadata["code"] == "bad_argument"


def test_cannot_hand_off_to_a_nonexistent_agent(disp):
    """Gateway 的 hardening：接收方必须是真实的、属于这个 Workspace 的 Agent。"""
    r = go(disp[0], ROOT_AGENT, "resource_handoff",
           {"path": "area_a", "to": "not_a_real_agent"})
    assert r.success is False
    assert r.metadata["code"] == "unknown_agent"


# ---- 🔴 结构守卫 ----
def test_agent_id_comes_from_intent_not_parameters():
    """
    🔴 发起方只取自 `intent.agent_id` —— 否则 Agent 能**替别人**交接责任。
    """
    import ast

    src = (ROOT / "actions" / "resource_control.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "_call":
                seg = ast.get_source_segment(src, node) or ""
                assert "intent.agent_id" in seg, seg
                assert 'params.get("agent_id")' not in seg, seg


def test_actions_layer_does_not_import_services():
    import ast

    src = (ROOT / "actions" / "resource_control.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    for bad in ("services", "core", "agents"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), sorted(mods)


def test_the_shared_path_rules_are_not_copied():
    """
    🔒 路径规则**只有一份** —— `resource_control` 从 `artifacts` 导入。
       复制一份会得到两套路径语义（它们说的是同一种东西）。
    """
    src = (ROOT / "services" / "resource_control.py").read_text(encoding="utf-8")
    assert "from services.artifacts import" in src
    assert "validate_artifact_path" in src and "map_gateway_code" in src
    # 不许自己再定义一份
    assert "def validate_artifact_path" not in src
    assert "def map_gateway_code" not in src


def test_composition_root_registers_all_three():
    import ast

    src = (ROOT / "experiments" / "heterogeneous_production"
           / "production.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_action_seam")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    for cls in ("ResourceCreateExecutor", "ResourceHandoffExecutor",
                "ResourceReleaseExecutor"):
        assert cls in names, f"没注册 {cls}"
