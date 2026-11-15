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


def test_context_query_has_separate_name_and_agent_id(rt):
    for aid in AGENTS:
        q = _brain(rt, aid).default_context_query
        identity = q.get("identity", "")
        assert "我的名字是：" in identity, aid
        assert f"我的 Agent ID 是：{aid}" in identity, aid


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


# ============================================================
# Agent 对**整个系统**的认知（2026-10-10，用户要求）
# ============================================================
#
#     「对于系统的认知这是什么系统 / 我为什么在这里 / 长期会怎样，
#       可以先放到系统提示词里给 Agent。」
#
# 【🔴 实测发现】
#
#     `agent_a..e` **完全没有 `cognition` 段**（只有 novel_* 有）——
#     而 `to_text()` 的头段只渲染 `identity` / `constitution`。
#     ⇒ 观察实验里的 5 个 Agent 对所处的系统几乎一无所知：
#       它们的 cognition context 只有 **444 字符**，全部是经济快照。
#
# 【🔒 为什么放 `cognition.constitution`，而不是 `brain.system_prompt`】
#
#     `system_prompt` 被 `config_migration_baseline.json` **冻结** ——
#     `test_config_resolver.py` / `test_resolved_config_runtime.py`
#     严格断言它的长度不变，且**没有**偏离机制（只有 `max_tokens` 有）。
#     那本身就是「它不是用来装新东西的」的信号。
#
#     而 `cognition.*` 是明确设计好的扩展点：`_agent_context_query()`
#     直接读 `ResolvedAgent.raw`，新配置项**天然通过**。

OS_BRIEF_AGENTS = ["agent_a", "agent_b", "agent_c", "agent_d", "agent_e"]

#: 三段标题 —— 对应「这是什么系统 / 我为什么在这里 / 长期会怎样」。
OS_BRIEF_SECTIONS = ("【这个系统是什么】", "【你为什么在这里】", "【长期来看】")


def _cfg() -> dict:
    import yaml

    return yaml.safe_load(
        (ROOT / "config" / "config.yaml").read_text("utf-8"))


def test_agent_a_to_e_have_a_system_brief():
    """每个要跑的 Agent 都得知道自己在什么系统里。"""
    cfg = _cfg()
    for aid in OS_BRIEF_AGENTS:
        got = (cfg["agents"][aid].get("cognition") or {}).get("constitution")
        assert got and got.strip(), f"{aid} 没有 cognition.constitution"
        for sec in OS_BRIEF_SECTIONS:
            assert sec in got, f"{aid} 的系统认知里少了「{sec}」"


def test_agent_a_to_e_share_the_same_system_brief():
    """
    这 5 段必须**完全一致**。

    🔴 为什么：它回答的是「这个系统是什么」—— 对所有 Agent 应该是
       **同一个事实**。若 a 认为「系统提供调度器」而 b 认为「不提供」，
       那不是 5 个 Agent 认知不同，而是**系统在自相矛盾**。

    ⚠️ 配置里这 5 段是**复制**出来的（与 novel_* 同一手法）——
       这个测试就是防复制的唯一保险：改一段忘了改其余，
       它立刻变红。

    🔒 名字被 `config/config.yaml` 里 agent_b 上方的注释**引用** ——
       改名要同时改那里。
    """
    cfg = _cfg()
    briefs = {aid: cfg["agents"][aid]["cognition"]["constitution"]
              for aid in OS_BRIEF_AGENTS}

    uniq = set(briefs.values())
    assert len(uniq) == 1, (
        "agent_a..e 的系统认知不一致（改一段忘了改其余）："
        + ", ".join(f"{a}={len(b)}字符" for a, b in briefs.items()))


def test_the_system_brief_really_reaches_the_prompt(tmp_path):
    """端到端：它真的渲染进了 **system prompt 的开头**。"""
    import os
    import tempfile

    from main import build_runtime

    r = build_runtime(db_path=os.path.join(tempfile.mkdtemp(), "os.db"),
                      agent_ids=list(OS_BRIEF_AGENTS))
    try:
        for aid in OS_BRIEF_AGENTS:
            b = _brain(r, aid)
            assert "constitution" in b.default_context_query, aid
            txt = b.context_builder.build(
                aid, **b.default_context_query).to_text()
            assert "【这个系统是什么】" in txt, f"{aid} 的提示词里没有系统认知"
            assert txt.index("【这个系统是什么】") < 200, (
                f"{aid}: 没渲染进 head 段")
    finally:
        r.close()


def test_that_consistency_guard_would_really_catch_a_drift():
    """
    反向：把 5 段里的一段改掉，确认「不一致」**真的会被判定为不一致**。

    不做这一步的话，上面那条断言可能只是因为取错了键而永远为真。
    """
    cfg = _cfg()
    briefs = {aid: cfg["agents"][aid]["cognition"]["constitution"]
              for aid in OS_BRIEF_AGENTS}
    assert len(set(briefs.values())) == 1, "前提不成立：基线就不一致"

    briefs["agent_c"] = briefs["agent_c"] + "（被悄悄改过）"

    assert len(set(briefs.values())) == 2, (
        "漂移检测失效 —— 改了内容却判不出不一致")


def test_the_section_guard_is_not_vacuous():
    """
    反向：那三个小标题**不是**随处可见的通用词。

    否则「每个 Agent 都有这三段」可能只是因为到处都有这几个词。
    """
    cfg = _cfg()
    novel = cfg["agents"]["novel_author"]["cognition"]["constitution"]

    assert "【这个系统是什么】" not in novel, (
        "novel_author 也有这个标题 —— 那这条检查就区分不出什么了")

