"""
`Public Manifest` + 公开履历 —— 结构守卫与轻量测试（不调 LLM）

【依据】
  §22.20 B  系统维护的公开事实：Agent ID · Current Provider · Current Model
  §0.5.8    **OS 提供：Agent 信息 · 价格 · 信誉 · 历史 · 资源**
  §0.5.7    🔴 不要预先规定「A 是程序员、B 是测试 Agent」——
            专业化应由 `能力 · 成本 · 信誉 · 历史` 长期反馈**涌现**
  §22.21    Public Manifest ≠ Capability ≠ Authorization ≠ Ability Evaluation
  §22.19    Public Manifest（公开事实）≠ Other-Agent Model（观察者自己的认知）
"""
from __future__ import annotations

import ast
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

PROD = ROOT / "experiments" / "heterogeneous_production" / "production.py"
DIV = ROOT / "experiments" / "agent_division" / "run_division.py"


def _code_only(src: str) -> str:
    """
    **纯代码**：去掉注释与文档字符串。

    🔴 【为什么每次都要这一步 —— 这个坑已经踩了三次】

        `_roster_facts` 的 **docstring 里**就写着
        「为什么不投 `skills` 了」—— 直接对 `ast.unparse` 的结果断言
        `"skills" not in code`，会被**它自己的说明文字**绊倒。

        这是本项目记录过的 bug 类 ⑦「守卫查原文而不是纯代码」。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _roster_src() -> str:
    src = PROD.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_roster_facts")
    code = _code_only(ast.unparse(fn))
    assert len(code) > 200, "清理后太短 —— 守卫可能在对着空串断言"
    return code


# ---- 源码级：投什么、不投什么 ----
def test_skills_are_no_longer_delivered():
    """
    🔴 §0.5.7：不要预先规定「A 是程序员、B 是测试 Agent」。
       `skills` 是我们手写的标签、没有任何东西支撑 ——
       它**就是"预设角色表"换了个名字**，不该作为区分度投出去。
    """
    code = _roster_src()
    assert "skills" not in code
    assert "capabilities" not in code, (
        "roster 又把 capabilities 投出去了 —— 那是 §0.5.7 禁止的角色表")


def test_it_delivers_provider_and_model():
    """§22.20 B：Provider / Model 是**系统维护**的公开事实。"""
    code = _roster_src()
    assert "provider" in code and "model" in code


def test_it_delivers_reputation_and_history():
    """§0.5.8：OS 提供 信誉 · 历史。"""
    code = _roster_src()
    assert "reputation" in code or "声誉" in code
    src = PROD.read_text(encoding="utf-8")
    assert "履历" in src


# ---- 运行时：真实渲染 ----
@pytest.fixture(scope="module")
def rt():
    from main import build_runtime

    agents = ["agent_a", "agent_b", "agent_c"]
    db = os.path.join(tempfile.mkdtemp(), "manifest.db")
    r = build_runtime(db_path=db, agent_ids=agents)
    try:
        yield r, agents
    finally:
        r.close()


def test_directory_carries_provider_and_model(rt):
    r, agents = rt
    for e in r.directory.all():
        meta = dict(e.metadata or {})
        assert meta.get("provider"), f"{e.agent_id} 没有 provider"
        assert meta.get("model"), f"{e.agent_id} 没有 model"


def test_it_does_not_leak_the_profile_alias(rt):
    """
    🔴 模型**档案别名**里有角色暗示（如 `doubao_novel`）——
       投它等于又泄露一次分工。投的必须是**真实 model_id**。
    """
    r, _ = rt
    for e in r.directory.all():
        meta = dict(e.metadata or {})
        assert "model_profile" not in meta, "投了档案别名（会泄露角色）"
        blob = repr(meta)
        for leaky in ("doubao_novel", "qwen_max", "qwen_fast", "deepseek_chat"):
            assert leaky not in blob, f"泄露了档案别名 {leaky}"


def test_rendered_roster(rt):
    import experiments.heterogeneous_production.production as P

    r, agents = rt
    txt = P._roster_facts(r, agents[0])
    assert "provider=" in txt and "model=" in txt
    assert "skills" not in txt and "capabilities" not in txt
    # §22.19：只陈述事实，不许替观察者下判断
    for banned in ("你应该", "建议找", "最好找", "更厉害", "推荐"):
        assert banned not in txt, f"roster 里出现了判断/建议：{banned}"


# ---- 🔴 假数据必须被记录 ----
def test_fake_history_is_recorded_and_labelled():
    """
    🔴 注入假履历**可以**（真实反馈回路还没实现，审计 A-11），
       但**必须被记录**，否则任何人回头看都会把它当成真实数据。
    """
    src = DIV.read_text(encoding="utf-8")
    assert "FAKE_HISTORY" in src
    assert "injected_fake_history" in src, "假履历没有进结果 JSON"
    assert "不是真实历史" in src, "没有明确标注它是假的"
    # 控制台也要说
    assert "测试注入的假履历" in src


def test_fake_histories_are_deliberately_different():
    """
    🔒 全同就变成瞎猜 —— 测不出"选择"。
       这里要的是：老手 / 新人 / 高成功率 / 有失败记录。
    """
    import experiments.agent_division.run_division as D

    vals = [(v["reputation_score"], v["completed_tasks"], v["failed_tasks"])
            for v in D.FAKE_HISTORY.values()]
    assert len(set(vals)) == len(vals), f"假履历有重复：{vals}"
    assert len(vals) >= 3


def test_observation_experiment_uses_neutral_names():
    """
    🔴 名字本身就在泄露分工：`novel_world_builder` 告诉所有人
       "我是世界构建者"，于是"该找谁"根本不需要涌现。
    """
    import experiments.agent_division.run_division as D

    assert D.NEUTRAL_AGENTS == ["agent_a", "agent_b", "agent_c",
                               "agent_d", "agent_e"]
    for a in D.NEUTRAL_AGENTS:
        for hint in ("novel", "world", "plot", "author", "editor", "reader"):
            assert hint not in a, f"名字里有分工暗示：{a}"
