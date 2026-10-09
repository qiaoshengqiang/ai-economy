"""
Critical Observation Visibility —— 测试
==========================================

规格：`Observation闭环与验证实验`

核心命题（§27）：

    检索负责**相关性**，Critical Channel 负责**可见性**。
    关键事实不能因为普通 relevance ranking 而从下一轮认知中消失。

    Critical   → visibility guarantee（自己的槽位）
    Relevant   → ranking preference（共享的池子）

🔒 两条通道**独立**：不共享池子，不把 critical 塞进 relevance score。
🔒 不调真实 API、不启动实验。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cognition.context import CognitiveContext, CognitiveContextBuilder
from experience.models import (
    CRITICAL_OBSERVATION_KEY, DEFAULT_MAX_CRITICAL_OBSERVATIONS,
    Experience,
)
from experience.store import ExperienceStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AGENT = "agent_crit"

#: TestE 要用真实 Runtime —— 那需要 config.yaml 里存在的 Agent id。
AGENT_ID = "novel_author"


@pytest.fixture
def store(tmp_path):
    from storage.database import Database

    db = Database(str(tmp_path / "crit.db"))
    db.initialize()
    yield ExperienceStore(db)
    db.close()


def _ordinary(store, n, *, prefix="ord", scope=None, agent_id=None):
    """造 n 条**普通**经历（走真实持久化路径）。"""
    who = agent_id or AGENT
    out = []
    for i in range(n):
        out.append(store.record(
            who, "observation",
            result={"note": f"{prefix}-{i}"},
            feedback=f"普通经历 {prefix}-{i}",
            scope=dict(scope or {}),
        ))
    return out


def _critical(store, *, finish_reason="length", marker="cp", agent_id=None):
    """造一条**关键观察**（真实持久化路径）。"""
    return store.record(
        agent_id or AGENT, "observation",
        result={
            CRITICAL_OBSERVATION_KEY: True,
            "observed_state": "cognitive_output_truncated",
            "cognitive_output": "TRUNCATED",
            "finish_reason": finish_reason,
            "output_status": "VALID_CONTENT",
            "note": marker,
        },
        feedback=(f"上一轮认知输出未完整完成：模型输出达到 token 上限"
                  f"（finish_reason={finish_reason}）。"),
    )


def _build(store, **kw):
    return CognitiveContextBuilder(experience_store=store, **kw).build(AGENT)


# ============================================================
# Test A：普通 Experience 仍然正常
# ============================================================
class TestA_OrdinaryUnchanged:
    def test_twenty_ordinary_still_ranked_by_relevance(self, store):
        """
        20 条普通经历 → 仍然按**原来的** relevance ranking 取 top-N。

        🔒 引入 Critical Channel **不能**改变普通检索行为。
        """
        # 5 条命中 scope，15 条不命中
        for i in range(5):
            store.record(AGENT, "observation",
                         result={"note": f"hit-{i}"},
                         scope={"world": "W", "domain": "D"})
        for i in range(15):
            store.record(AGENT, "observation",
                         result={"note": f"miss-{i}"}, scope={})

        picked = store.retrieve_relevant(
            AGENT, world="W", domain="D", limit=5)
        assert len(picked) == 5
        # 命中的那 5 条必须全被选中（relevance 优先）
        notes = {e.result.get("note") for e in picked}
        assert notes == {f"hit-{i}" for i in range(5)}, notes

    def test_limit_is_respected(self, store):
        _ordinary(store, 20)
        assert len(store.retrieve_relevant(AGENT, limit=5)) == 5
        assert len(store.retrieve_relevant(AGENT, limit=3)) == 3

    def test_critical_does_not_shrink_ordinary_slots(self, store):
        """
        §9 情况 B：关键观察**有自己槽位**，不挤占普通名额。

        Context 应该是 1 critical + 5 ordinary，而不是 1 + 4。
        """
        _ordinary(store, 20)
        _critical(store)
        ctx = _build(store)
        assert len(ctx.relevant_experiences) == 5, (
            "普通经历的名额不能被关键观察挤掉")
        assert len(ctx.critical_observations) == 1


# ============================================================
# Test B：关键观察即使 relevance 很低也必须进入
# ============================================================
class TestB_CriticalAlwaysVisible:
    def test_low_relevance_critical_still_enters(self, store):
        """
        §9 情况 A：19 条普通 + 1 条关键，关键那条排在普通候选之后。

        🔒 它 relevance = 0（scope 不匹配），而且**最早**创建 ——
           在纯 relevance ranking 下它必然落选。
           但 Context 里**必须**有它。
        """
        crit = _critical(store)                      # 最先创建
        for i in range(19):                          # 之后 19 条都命中 scope
            store.record(AGENT, "observation",
                         result={"note": f"hit-{i}"},
                         scope={"world": "W", "domain": "D"})

        # 先证明：纯 relevance ranking **确实会淘汰它**
        picked = store.retrieve_relevant(
            AGENT, world="W", domain="D", limit=5)
        assert crit.experience_id not in {e.experience_id for e in picked}, (
            "前提不成立：这条关键观察在普通 ranking 里本该落选，"
            "如果它被选中，本测试就证明不了「保证可见」")

        ctx = CognitiveContextBuilder(experience_store=store).build(
            AGENT, world="W", domain="D")

        assert [e.experience_id for e in ctx.critical_observations] == [
            crit.experience_id], "关键观察必须进入 Context，无论 relevance"
        assert len(ctx.relevant_experiences) == 5

    def test_critical_is_visible_in_rendered_text(self, store):
        _ordinary(store, 19)
        _critical(store)
        text = _build(store).to_text()
        assert "TRUNCATED" in text
        assert "length" in text

    def test_oldest_critical_survives_ordinary_flood(self, store):
        """洪泛 30 条普通经历之后，关键观察仍然在。"""
        crit = _critical(store)
        _ordinary(store, 30)
        ctx = _build(store)
        assert crit.experience_id in {e.experience_id
                                      for e in ctx.critical_observations}


# ============================================================
# Test C：两条通道独立存在
# ============================================================
class TestC_TwoIndependentChannels:
    def test_both_channels_populated(self, store):
        _ordinary(store, 10)
        _critical(store)
        ctx = _build(store)

        assert ctx.critical_observations, "critical 通道必须存在"
        assert ctx.relevant_experiences, "ordinary 通道必须存在"

    def test_channels_do_not_overlap(self, store):
        """关键观察不该**同时**出现在普通通道里（那会重复渲染）。"""
        _ordinary(store, 10)
        _critical(store)
        ctx = _build(store)
        crit_ids = {e.experience_id for e in ctx.critical_observations}
        ord_ids = {e.experience_id for e in ctx.relevant_experiences}
        assert not (crit_ids & ord_ids), (
            "关键观察被渲染了两次 —— 两条通道必须分离")

    def test_both_channels_rendered_separately(self, store):
        _ordinary(store, 10)
        _critical(store)
        text = _build(store).to_text()
        assert "Critical observations" in text
        assert "Relevant experiences" in text
        # 关键观察必须渲染在普通经历**之前**
        assert text.index("Critical observations") < text.index(
            "Relevant experiences")

    def test_diagnostics_reports_both(self, store):
        _ordinary(store, 10)
        _critical(store)
        diag = _build(store).stats()
        assert diag["critical_observations"] == 1
        assert diag["selected_experiences"] == 5


# ============================================================
# Test D：关键观察数量受控（§10）
# ============================================================
class TestD_RetentionBounded:
    def test_many_critical_are_bounded(self, store):
        """10 条关键观察 → Context 里只保留 retention rule 允许的条数。"""
        for i in range(10):
            _critical(store, marker=f"cp-{i}")
        ctx = _build(store)
        assert len(ctx.critical_observations) <= DEFAULT_MAX_CRITICAL_OBSERVATIONS
        assert len(ctx.critical_observations) == 1

    def test_latest_critical_wins(self, store):
        """保留的是**最近**那条 —— 上一轮刚发生的事实优先。"""
        _critical(store, marker="old")
        newest = _critical(store, marker="new")
        ctx = _build(store)
        assert len(ctx.critical_observations) == 1
        assert ctx.critical_observations[0].experience_id == \
            newest.experience_id

    def test_retention_is_configurable_not_unbounded(self, store):
        for i in range(10):
            _critical(store, marker=f"cp-{i}")
        builder = CognitiveContextBuilder(experience_store=store,
                                         max_critical_observations=3)
        ctx = builder.build(AGENT)
        assert len(ctx.critical_observations) == 3

    def test_zero_disables_the_channel(self, store):
        _ordinary(store, 5)
        _critical(store)
        ctx = CognitiveContextBuilder(
            experience_store=store, max_critical_observations=0).build(AGENT)
        assert ctx.critical_observations == []

    def test_default_is_small(self):
        """§10：不要设一个大数 —— 那是「垃圾桶」的开始。"""
        assert DEFAULT_MAX_CRITICAL_OBSERVATIONS <= 3, (
            "关键观察必须稀缺；默认保留条数应当很小")


# ============================================================
# Test E：TRUNCATED 必达（**真实 pipeline**，§15）
# ============================================================
class TestE_TruncatedReachesNextContext:
    """
    🔴 本轮最重要的测试。

    不是手工把 Experience 塞进 Context，而是真的走：

        Agent.generate()  （finish_reason=length，**生产路径就是这个入口**）
            ↓
        ExperienceStore.record()
            ↓
        CognitiveContextBuilder.build()
            ↓
        CognitiveContext.to_text()
            ↓
        Turn N+1 的 Agent 看得到

    ⚠️ **这条测试曾经错过一次**：截断记录原本只在 `act()` 里，
       而生产实验走的是 `generate()` —— 于是真实生产里
       `cognitive_status=TRUNCATED` 只是躺在返回值里，
       Experience 表**一条都没有**。所以这里必须走 `generate()`。
    """

    @pytest.fixture
    def agent(self, tmp_path, monkeypatch):
        """真实 Runtime 里的一个 Agent（brain.generate 被 stub 掉）。"""
        from llm.base import LLMResponse
        from main import build_runtime

        rt = build_runtime(db_path=str(tmp_path / "e.db"),
                           agent_ids=[AGENT_ID])
        target = next(a for a in rt.economy.all_agents()
                      if a.agent_id == AGENT_ID)

        def _stub(finish_reason):
            target.brain.generate = lambda messages=None, **kw: LLMResponse(
                content='{"thought": "写第一章"}',
                provider="mock", model="mock_model",
                finish_reason=finish_reason,
                total_tokens=100, token_source="api")
            return target

        yield _stub, target
        rt.close()

    def test_truncated_observation_reaches_next_turn_context(self, agent):
        stub, target = agent
        stub("length")
        response = target.generate(messages="hi", call_purpose="cognition")   # ← 生产路径的入口

        assert response.cognitive_status == "TRUNCATED"

        # Turn N+1：走**真正的** builder
        ctx = CognitiveContextBuilder(
            experience_store=target.experience_store).build(AGENT_ID)

        assert ctx.critical_observations, (
            "Turn N 的截断必须出现在 Turn N+1 的 critical 通道里")
        obs = ctx.critical_observations[0]
        assert obs.result.get("observed_state") == "cognitive_output_truncated"
        assert obs.result.get("finish_reason") == "length"

        text = ctx.to_text()
        assert "TRUNCATED" in text, "渲染文本里必须看得见"
        assert "length" in text

    def test_survives_a_flood_of_ordinary_experiences(self, agent):
        """截断之后又积累了 30 条普通经历 —— Turn N+1 仍必须看得到。"""
        stub, target = agent
        stub("length")
        target.generate(messages="hi", call_purpose="cognition")
        _ordinary(target.experience_store, 30, agent_id=AGENT_ID)

        ctx = CognitiveContextBuilder(
            experience_store=target.experience_store).build(AGENT_ID)
        text = ctx.to_text()
        assert "TRUNCATED" in text, "关键事实不能因为普通经历变多而消失"
        assert len(ctx.relevant_experiences) == 5

    def test_normal_stop_creates_no_critical_observation(self, agent):
        """🔒 阴性对照：正常 stop **不**产生关键观察。"""
        stub, target = agent
        stub("stop")
        target.generate(messages="hi", call_purpose="cognition")

        ctx = CognitiveContextBuilder(
            experience_store=target.experience_store).build(AGENT_ID)
        assert ctx.critical_observations == []

    def test_truncated_observation_is_marked_critical(self, agent):
        stub, target = agent
        stub("length")
        target.generate(messages="hi", call_purpose="cognition")

        rows = target.experience_store.list(AGENT_ID)
        crit = [e for e in rows if e.is_critical_observation]
        assert len(crit) == 1
        assert crit[0].result.get(CRITICAL_OBSERVATION_KEY) is True

    def test_act_does_not_double_record(self, agent):
        """
        🔒 回归：`act()` 内部经 `generate()`，所以**只能记一条**。

        截断记录放在 `generate()` 是「唯一不重复」的位置 ——
        如果哪天有人又往 `act()` 里加一份，这条会红。
        """
        stub, target = agent
        stub("length")
        target.act(messages="hi", call_purpose="cognition")

        rows = target.experience_store.list(AGENT_ID)
        crit = [e for e in rows if e.is_critical_observation]
        assert len(crit) == 1, f"截断被重复记录了 {len(crit)} 次"

    def test_generate_is_the_production_entry_point(self):
        """
        🔒 结构守卫：生产实验用的入口必须是**被覆盖**的那一个。

        这条防止未来有人把截断记录挪回 `act()`，
        而生产路径继续走 `generate()` —— 那正是本 Bug 的成因。
        """
        source = (PROJECT_ROOT / "agents" / "base_agent.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        gen = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "generate")
        assert "_record_truncation_if_needed" in ast.unparse(gen), (
            "generate() 必须自己记录截断 —— 生产走的是这个入口")

        runner = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
                  / "production.py").read_text(encoding="utf-8")
        assert "a.generate(" in runner, (
            "生产实验必须仍然走 generate()；若改成 act()，"
            "请同时更新这条守卫与上面那条测试")


# ============================================================
# Test F：普通 relevance 不漂移（§16）
# ============================================================
class TestF_OrdinaryRetrievalNoDrift:
    def test_relevant_selection_identical_with_and_without_critical(self, store):
        """
        🔒 regression：普通检索的选中集合
           **不因为关键观察的存在而改变**。
        """
        for i in range(5):
            store.record(AGENT, "observation", result={"note": f"hit-{i}"},
                         scope={"world": "W"})
        for i in range(15):
            store.record(AGENT, "observation", result={"note": f"miss-{i}"})

        before = [e.experience_id for e in store.retrieve_relevant(
            AGENT, world="W", limit=5)]

        _critical(store)                      # 插入一条关键观察

        after = [e.experience_id for e in store.retrieve_relevant(
            AGENT, world="W", limit=5)]

        assert before == after, (
            "普通 relevance 检索行为漂移了 —— retrieve_relevant 必须不受影响")

    def test_retrieve_relevant_has_no_critical_logic(self):
        """
        AST 守卫：`retrieve_relevant()` 里**不许**出现 critical 逻辑。

        🔒 那正是本轮要避免的建模错误：
           把「必须可见」当成「更加相关」。
        """
        source = (PROJECT_ROOT / "experience" / "store.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "retrieve_relevant"):
                func = node
        assert func is not None
        # 去掉 docstring 再检查（避免自引用假红）
        body = [n for n in func.body
                if not (isinstance(n, ast.Expr)
                        and isinstance(n.value, ast.Constant))]
        code = "\n".join(ast.unparse(n) for n in body)
        assert "critical" not in code.lower(), (
            "retrieve_relevant 里出现了 critical 逻辑 —— "
            "可见性不能用 relevance 打分来表达")

    def test_ordinary_build_unchanged_without_critical(self, store):
        """没有关键观察时，普通通道行为与以前完全一致。"""
        _ordinary(store, 20)
        ctx = _build(store)
        assert ctx.critical_observations == []
        assert len(ctx.relevant_experiences) == 5


# ============================================================
# Test G：不把策略写进 Context（§11）
# ============================================================
class TestG_FactsNotStrategy:
    STRATEGY_WORDS = ("retry", "retries", "max_tokens", "increase",
                      "加大", "必须重试", "降低 reasoning")

    def test_critical_rendering_contains_no_strategy(self, store):
        _critical(store)
        text = _build(store).to_text()
        low = text.lower()
        for word in self.STRATEGY_WORDS:
            assert word.lower() not in low, (
                f"关键观察的渲染里出现了策略词 {word!r} —— "
                f"Context 只呈现事实")

    def test_critical_rendering_contains_the_facts(self, store):
        _critical(store)
        text = _build(store).to_text()
        assert "TRUNCATED" in text
        assert "length" in text

    def test_no_banned_rule_words_in_critical_header(self):
        """Context 的文件级禁令仍然成立（must / always / never …）。"""
        from cognition.context import BANNED_RULE_WORDS

        source = (PROJECT_ROOT / "cognition" / "context.py").read_text(
            encoding="utf-8")
        marker = "Critical observations from the runtime"
        assert marker in source
        header = marker + " (facts about what just happened; not instructions):"
        for word in BANNED_RULE_WORDS:
            assert word not in header.lower(), (
                f"关键观察的标题里出现了被禁的规则词 {word!r}")


# ============================================================
# §14 反向守卫
# ============================================================
class TestGuardsAgainstRegression:
    def test_guard1_critical_not_in_ordinary_ranking(self, store):
        """
        Guard 1：把 critical **塞回**普通 top-N（而不是保证可见）→ 必须红。

        这里用「行为级」验证：构造一个 critical 一定落选的场景，
        如果哪天有人把 critical 混进 retrieve_relevant 的池子，
        这条断言就会失败。
        """
        crit = _critical(store)
        for i in range(19):
            store.record(AGENT, "observation", result={"note": f"h{i}"},
                         scope={"world": "W", "domain": "D", "situation": "S"})

        ctx = CognitiveContextBuilder(experience_store=store).build(
            AGENT, world="W", domain="D", situation="S")

        # ① 它不在普通通道里（说明没有混进 ranking）
        assert crit.experience_id not in {
            e.experience_id for e in ctx.relevant_experiences}, (
            "critical 混进了普通 relevance 池子 —— "
            "那会把「必须可见」错建模成「更加相关」")
        # ② 但它在 critical 通道里（说明保证可见仍然成立）
        assert crit.experience_id in {
            e.experience_id for e in ctx.critical_observations}, (
            "critical 没有进入 Context —— 可见性保证被破坏了")

    def test_guard2_build_really_fetches_critical(self):
        """
        Guard 2：`build()` 里不许出现「取了 critical 却不放进 Context」。

        🔒 AST 检查：`build()` 必须既调用 `retrieve_critical`，
           又把结果赋给 `critical_observations=`。
        """
        source = (PROJECT_ROOT / "cognition" / "context.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)

        # ① builder 的 build 必须调用 retrieve_critical
        build = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build":
                build = node
        assert build is not None
        code = ast.unparse(build)
        assert "retrieve_critical" in code, (
            "build() 没有取关键观察 —— 可见性保证无从成立")

        # ② 构造 CognitiveContext 时必须传 critical_observations=
        assert "critical_observations=critical" in code, (
            "build() 取了关键观察却没放进 CognitiveContext")

    def test_guard3_visibility_does_not_depend_on_relevance(self, store):
        """
        Guard 3：关键观察的可见性**不许**依赖任何 relevance 条件。

        构造「scope 完全不匹配、且最新」的 critical ——
        如果实现里偷偷加了 relevance 门控，它就会消失。
        """
        crit = store.record(
            AGENT, "observation",
            result={CRITICAL_OBSERVATION_KEY: True,
                    "observed_state": "cognitive_output_truncated",
                    "finish_reason": "length"},
            scope={"world": "完全不匹配的世界"})
        ctx = CognitiveContextBuilder(experience_store=store).build(
            AGENT, world="W", domain="D", situation="S")
        assert crit.experience_id in {e.experience_id
                                      for e in ctx.critical_observations}

    def test_guard4_no_new_database_column(self):
        """§5：不许为 critical 新增数据库列。"""
        models = (PROJECT_ROOT / "storage" / "models.py").read_text(
            encoding="utf-8")
        for banned in ("critical_observation", "is_critical", "priority_score",
                       "importance_score", "salience"):
            assert banned not in models, (
                f"storage/models.py 里出现了 {banned!r} —— "
                f"关键标记应当写在既有的 result JSON 里")

    def test_guard5_no_priority_framework(self):
        """§5：不许引入 CRITICAL/HIGH/MEDIUM/LOW 分级框架。"""
        for name in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            pass  # 分级名本身太泛，靠下面的具体类名守卫
        banned = ("PriorityFramework", "ImportanceScorer", "SalienceScorer",
                  "RetrievalWeight", "PriorityScore")
        offenders = []
        for folder in ("experience", "cognition"):
            for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
                if "__pycache__" in str(path):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name in banned:
                        offenders.append(f"{path.name}::{node.name}")
        assert not offenders, f"出现了被禁止的优先级框架：{offenders}"

    def test_guard6_retention_is_bounded(self):
        """§10：retention 必须有上限，且默认值小。"""
        assert isinstance(DEFAULT_MAX_CRITICAL_OBSERVATIONS, int)
        assert 1 <= DEFAULT_MAX_CRITICAL_OBSERVATIONS <= 3
