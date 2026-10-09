"""Constitution 进 system 提示词（V1 务实取舍）—— 轻量测试，不调 LLM。"""
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

AGENTS = ["novel_author", "novel_reader"]


@pytest.fixture(scope="module")
def rt():
    from main import build_runtime

    db = os.path.join(tempfile.mkdtemp(), "const.db")
    r = build_runtime(db_path=db, agent_ids=list(AGENTS))
    try:
        yield r
    finally:
        r.close()


def _brain(rt, aid):
    return next(x for x in rt.economy.all_agents() if x.agent_id == aid).brain


def test_config_has_constitution_for_every_novel_agent():
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text("utf-8"))
    for aid in ("novel_author", "novel_editor", "novel_plot_architect",
                "novel_reader", "novel_world_builder"):
        got = (cfg["agents"][aid].get("cognition") or {}).get("constitution")
        assert got and got.strip(), f"{aid} 没有 constitution"


def test_it_reaches_the_context_query(rt):
    for aid in AGENTS:
        q = _brain(rt, aid).default_context_query
        assert "constitution" in q, f"{aid} 的 context_query 里没有 constitution"


def test_it_renders_into_the_system_prompt(rt):
    for aid in AGENTS:
        b = _brain(rt, aid)
        txt = b.context_builder.build(aid, **b.default_context_query).to_text()
        assert "我的长期原则" in txt, aid
        # 🔒 渲染进的是 system 的**开头**（head 段）
        assert txt.index("我的长期原则") < 200, "没渲染进 head 段"


def test_core_drives_is_NOT_read(rt):
    """
    🔴 Core Drives 属于**经济/激励层**，**不进提示词**。

    代码里只读 `identity` / `constitution` —— 即使配置里写了 `drives`
    也不该被读进来。
    """
    import ast

    # 🔒 只看**那个键元组**，不看 docstring ——
    #    否则说明文字里那句「不读 `drives`」会把自己绊倒
    #    （本项目记录过的「子串匹配误报」bug 类）。
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_agent_context_query")
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Tuple) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str)
                for e in node.elts):
            keys |= {e.value for e in node.elts}
    assert "constitution" in keys, f"没找到键元组，只看到 {sorted(keys)}"
    assert "drives" not in keys, (
        "_agent_context_query 的键里出现了 drives —— "
        "Core Drives 属于激励层，不该进提示词")


def test_per_agent_not_shared(rt):
    """每个 Agent 一份 query —— 不能共用循环外那个 dict。"""
    a = _brain(rt, "novel_author").default_context_query
    b = _brain(rt, "novel_reader").default_context_query
    assert a is not b, "两个 Agent 共用了同一个 query dict"
