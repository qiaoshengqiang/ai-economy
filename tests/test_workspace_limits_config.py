"""
资源层结构上限 —— 配置驱动 —— 测试
=========================================

🔴 修的分层失误：

    「路径最多 8 层」原来**写死在 `services/artifacts.py`** 里：

        if len(segments) > 8:
            return "path 层级过深（>8）"

    这是**分层失误** —— 「路径有多深」「一个目录能挂几个孩子」是关于
    **资源树结构**的事实，而资源树的权威是 `WorkspaceGateway`
    （它拥有 WorkspaceStore 与锁）。`services/artifacts.py` 只是便捷包装。

    后果：换个调用方就绕过上限；而且那个 `8` **没有任何测试覆盖**。

🔴 同时补上**根本不存在**的上限：

    以前没有任何「一个目录最多几个子项」的限制 ——
    一个持有目录的 Agent 可以建无限多子目录。

现在的结构：

    config.yaml  workspace: {max_path_depth, max_children_per_directory}
          ↓  唯一来源
    core/config_resolver.py   →  resolved.workspace
          ↓
    main.py（组合根）  WorkspaceLimits.from_mapping(resolved.workspace)
          ↓
    WorkspaceGateway.limits   →  在 create 时强制

🔒 全部测试**不调用任何 LLM**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                  # noqa: E402

load_dotenv(ROOT / ".env")

CONFIG = ROOT / "config" / "config.yaml"
ARTIFACTS = ROOT / "services" / "artifacts.py"
GATEWAY = ROOT / "services" / "workspace_gateway.py"
MAIN = ROOT / "main.py"


# ======================================================================
# ① 值对象：从配置构造，非法值安全回落
# ======================================================================
def test_from_mapping_reads_the_configured_values():
    from services.workspace_gateway import WorkspaceLimits

    got = WorkspaceLimits.from_mapping(
        {"max_path_depth": 3, "max_children_per_directory": 7})
    assert got.max_path_depth == 3
    assert got.max_children_per_directory == 7


@pytest.mark.parametrize("raw", [
    None, {}, {"max_path_depth": 0}, {"max_path_depth": -1},
    {"max_path_depth": "x"}, {"max_path_depth": ""},
    {"max_path_depth": None}, {"max_children_per_directory": 0},
])
def test_bad_values_fall_back_to_the_declared_default(raw):
    """
    🔒 非法值回落到**值对象里声明的默认**，不是回落到另一处写死的数字
       （那会变成两个来源）。

    ⚠️ 尤其 `0` / 负数必须被忽略 —— 上限为 0 会**静默禁掉一切创建**，
       比不设上限更危险。
    """
    from services.workspace_gateway import WorkspaceLimits

    got = WorkspaceLimits.from_mapping(raw)
    assert got.max_path_depth == 8
    assert got.max_children_per_directory == 50


def test_gateway_defaults_when_no_limits_injected():
    """不注入就用默认 —— 既有测试都是这么造 Gateway 的。"""
    from services.workspace_gateway import WorkspaceGateway

    gw = WorkspaceGateway(None, agent_exists=lambda _a: False)
    assert gw.limits.max_path_depth == 8
    assert gw.limits.max_children_per_directory == 50


# ======================================================================
# ② 单一来源：内容在 config，代码里不再有那个 8
# ======================================================================
def test_config_has_the_workspace_section():
    text = CONFIG.read_text(encoding="utf-8")
    assert "\nworkspace:" in text, "config.yaml 里没有 workspace 段"
    assert "max_path_depth:" in text
    assert "max_children_per_directory:" in text


def test_artifacts_layer_no_longer_hardcodes_the_depth():
    """
    🔴 **反向守卫**：`services/artifacts.py` 不许再出现写死的深度上限。

    查 **AST** 后的常量比较表达式，不查原文 —— 否则注释里那句
    「曾经写死 `if len(segments) > 8`」的说明会把自己绊倒
    （本项目踩过这个坑）。
    """
    import ast

    # 🔒 只查**严格大于**（`len(...) > N`）。
    #
    #    ⚠️ 不能把 `>=` 也算进来：`validate_artifact_path` 里有一处
    #       合法检查是 `len(raw) >= 2 and raw[1] == ":"`（Windows 盘符形状）。
    #       第一版守卫把它误报了 —— 守卫太宽和守卫太窄一样是 bug。
    tree = ast.parse(ARTIFACTS.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Call)
                and isinstance(node.left.func, ast.Name)
                and node.left.func.id == "len"):
            continue
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(op, ast.Gt) and isinstance(comp, ast.Constant) and \
                    isinstance(comp.value, int):
                offenders.append(f"line {node.lineno}: "
                                 f"len(...) > {comp.value}")
    assert not offenders, (
        f"artifacts 层又出现写死的长度上限了：{offenders}\n"
        f"结构规则属于 WorkspaceGateway（读配置），不属于这里")


def test_resolver_carries_the_section():
    """配置要真的流动起来 —— 不是声明了没人读（`task.default_timeout_seconds`
    就是个反例：它在配置里，但全项目没有任何代码读它）。"""
    from core.config_resolver import ResolvedRuntimeConfig

    assert "workspace" in ResolvedRuntimeConfig.__dataclass_fields__, (
        "ResolvedRuntimeConfig 没有 workspace 字段 —— 配置读不进来")
    assert "workspace" in MAIN.read_text(encoding="utf-8"), (
        "组合根没有读 resolved.workspace")


def test_composition_root_injects_the_configured_limits():
    import ast

    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    src = MAIN.read_text(encoding="utf-8")
    assert "WorkspaceLimits.from_mapping(resolved.workspace)" in src, (
        "组合根没有把配置注入 Gateway")


# ======================================================================
# ③ 真的被强制（真 Gateway，内存库）
# ======================================================================
@pytest.fixture
def gw():
    from services.workspace_gateway import WorkspaceLimits
    from tests.workspace_harness import WorkspaceHarness

    W, A = "W", "agent_a"
    h = WorkspaceHarness.in_memory(participants={W: {A}}, agents={A})
    g = h.gateway()
    g.limits = WorkspaceLimits(max_path_depth=3,
                               max_children_per_directory=2)
    g.ensure_workspace(W, A)
    try:
        yield g, W, A
    finally:
        h.close()


def _create(gw, W, A, path):
    from services.artifacts import write_artifact

    return write_artifact(gw, W, A, path, "x", write_mode="create")


def test_depth_within_limit_is_allowed(gw):
    g, W, A = gw
    for p in ("a.md", "d1/a.md", "d1/d2/a.md"):
        assert _create(g, W, A, p).get("ok"), p


def test_depth_beyond_limit_is_rejected(gw):
    """
    🔒 错误码仍是 `bad_path` —— 与旧实现一致（旧检查也报 bad_path）。
       行为对调用方**没有变化**，只是判定点搬到了正确的层。
    """
    g, W, A = gw
    got = _create(g, W, A, "d1/d2/d3/a.md")
    assert got.get("ok") is False
    assert got.get("code") == "bad_path"
    assert "层级过深" in str(got.get("message"))


def test_child_count_limit_is_enforced(gw):
    """
    🔴 这个上限**以前根本不存在** —— 一个持有目录的 Agent
       可以建无限多子目录。
    """
    g, W, A = gw
    assert _create(g, W, A, "d1/f1.md").get("ok"), "第 1 个应当成功"
    assert _create(g, W, A, "d1/f2.md").get("ok"), "第 2 个应当成功（上限 2）"

    got = _create(g, W, A, "d1/f3.md")
    assert got.get("ok") is False
    assert got.get("code") == "limit_exceeded", got
    assert "上限" in str(got.get("message"))


def test_limits_are_per_directory_not_global(gw):
    """一个目录满了，不妨碍**另一个**目录。"""
    g, W, A = gw
    _create(g, W, A, "d1/f1.md")
    _create(g, W, A, "d1/f2.md")
    assert _create(g, W, A, "d1/f3.md").get("ok") is False

    assert _create(g, W, A, "d2/f1.md").get("ok"), (
        "另一个目录不该被牵连 —— 上限是**每目录**的")


def test_a_generous_limit_does_not_interfere(gw):
    """上限宽到不影响正常分工：50 个子项应当都能建。"""
    from services.workspace_gateway import WorkspaceLimits

    g, W, A = gw
    g.limits = WorkspaceLimits(max_path_depth=8,
                               max_children_per_directory=50)
    ok = sum(1 for i in range(50)
             if _create(g, W, A, f"d/f{i:02d}.md").get("ok"))
    assert ok == 50, (
        f"上限 50 表示「**最多** 50 个直接子项」—— 前 50 个应当都成功，"
        f"第 51 个才被拒。实得 {ok}")
    # 第 51 个必须被拒 —— 这才证明上限真的在生效
    assert _create(g, W, A, "d/f50.md").get("ok") is False
    assert _create(g, W, A, "d/f50.md").get("code") == "limit_exceeded"
