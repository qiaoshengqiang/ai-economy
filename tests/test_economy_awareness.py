"""
Economy Awareness V1 —— Self Model 事实隔离 + Economic Context
================================================================

【本文件回答两个问题】

    ① Self Model 能不能把「我的余额是 43」写成自己的事实？（**不许**）
    ② Agent 能不能看到真实的经济事实？（**能**，且只来自 Ledger）

【🔴 三件事必须分开，本文件也分开测】

    1  Agent **看到**了经济事实        → `TestEconomicContextEntersCognition`
    2  Agent **理解**了经济事实        → 需要真实实验观察（见报告 I–L）
    3  Agent 因此**改变**了行为        → 需要真实实验观察（见报告 I–L）

    本文件只能证明第 1 件。**不要**把「Context 里有余额」读成
    「Agent 有了经济意识」。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from cognition.economic import (                                     # noqa: E402
    CURRENCY_NOTE, EconomicContext, EconomicContextBuilder,
)
from reflection import ReflectionProposal                            # noqa: E402
from reflection.proposal import (                                    # noqa: E402
    KIND_CREATE_CLAIM, KIND_CREATE_STRATEGY, KIND_REVISE_CLAIM,
)
from reflection.write_service import SelfModelWriteService           # noqa: E402
from selfmodel import SelfModelStore                                 # noqa: E402
from selfmodel.fact_domains import (                                 # noqa: E402
    FACT_DOMAIN_ECONOMY, FACT_DOMAIN_SELF, SYSTEM_FACT_AUTHORITY,
    VALID_FACT_DOMAINS,
)
from storage.database import create_memory_database                  # noqa: E402

AGENT = "novel_author"


# ================================================================
# 夹具
# ================================================================
class _Experience:
    """最小 evidence 存根（write service 会校验它存在且属于本 Agent）。"""

    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.experience_id = "exp_1"


class _ExperienceStore:
    def __init__(self):
        self._one = _Experience(AGENT)

    def get(self, eid):
        return self._one if eid == "exp_1" else None


@pytest.fixture()
def sm():
    db = create_memory_database()
    db.initialize()
    store = SelfModelStore(db)
    return db, store, SelfModelWriteService(store,
                                            experience_store=_ExperienceStore())


def _claim(content, *, fact_domain="__missing__", confidence=0.8):
    change = {"content": content, "confidence": confidence}
    if fact_domain != "__missing__":
        change["fact_domain"] = fact_domain
    return ReflectionProposal(
        proposal_id="p1", agent_id=AGENT, kind=KIND_CREATE_CLAIM,
        proposed_change=change, reason="r", evidence=["exp_1"], scope={})


def _strategy(name, *, fact_domain="__missing__", rationale="r"):
    change = {"name": name, "rationale": rationale, "confidence": 0.7}
    if fact_domain != "__missing__":
        change["fact_domain"] = fact_domain
    return ReflectionProposal(
        proposal_id="p2", agent_id=AGENT, kind=KIND_CREATE_STRATEGY,
        proposed_change=change, reason="r", evidence=["exp_1"], scope={})


def _scan_domain_literals(path: Path) -> list:
    """
    扫一个文件里出现的**系统域字面量字符串**。

    🔒 按 AST 常量比对，不做子串匹配 ——
       本项目真实发生过 `agent_directory` 含 `agent_d`、
       `seen_tracker` 含 `ack` 这类误报。
    """
    tree = ast.parse(Path(path).read_text("utf-8"))
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return sorted(d for d in SYSTEM_FACT_AUTHORITY if d in literals)


# ================================================================
# §六 A–D：Self Model 事实隔离
# ================================================================
class TestSelfModelFactIsolation:
    """§二 / §三：Self Model 不许成为系统经济事实的第二来源。"""

    # ---- §六 A ----
    def test_a_balance_assertion_is_rejected(self, sm):
        """
        §六 A：`「我的 AIC 余额是 43」` 必须 `committed=False`
        并给出**明确** validation code。
        """
        _db, store, svc = sm

        outcome = svc.apply(_claim("我的 AIC 余额是 43",
                                   fact_domain=FACT_DOMAIN_ECONOMY))

        assert outcome.committed is False
        assert outcome.validation.code == "system_fact_not_self_owned"
        # 拒绝理由必须说清楚「这件事该问谁」——
        # 一句只回 rejected 的拒绝对 Agent 来说不可学习
        assert "Ledger" in outcome.validation.reason
        # 而且**什么都没写进去**
        assert store.retrieve(AGENT).claims == []

    def test_a2_undeclared_assertion_is_also_rejected(self, sm):
        """
        §六 A 的另一半：**不声明** `fact_domain` 也拒绝。

        🔒 这条是关键 —— 之前漏洞的形状正是「没有声明」。
           如果默认成 `self`，`「我的余额是 43」` 会直接通过。
        """
        _db, store, svc = sm

        outcome = svc.apply(_claim("我的 AIC 余额是 43"))

        assert outcome.committed is False
        assert outcome.validation.code == "missing_fact_domain"
        assert outcome.validation.fields == ["fact_domain"]
        assert store.retrieve(AGENT).claims == []

    def test_unknown_fact_domain_is_rejected(self, sm):
        _db, _store, svc = sm
        outcome = svc.apply(_claim("随便什么", fact_domain="astrology"))
        assert outcome.committed is False
        assert outcome.validation.code == "unknown_fact_domain"

    @pytest.mark.parametrize("domain", sorted(SYSTEM_FACT_AUTHORITY))
    def test_all_system_domains_are_rejected(self, sm, domain):
        """§四：economy / identity / capability / authorization 都不许。"""
        _db, _store, svc = sm
        outcome = svc.apply(_claim(f"系统事实 {domain}", fact_domain=domain))
        assert outcome.committed is False
        assert outcome.validation.code == "system_fact_not_self_owned"

    def test_system_domain_list_is_the_only_judgement(self):
        """
        🔒 结构守卫（§四）：判定**只有**一张表，没有散落的关键词判断。

        `write_service` 里不许出现任何系统域的**字面量字符串** ——
        以后新增一类系统事实只需改 `fact_domains.py` 一张表。
        """
        offenders = _scan_domain_literals(
            ROOT / "reflection" / "write_service.py")
        assert offenders == [], (
            f"write_service 里出现了系统域字面量 {offenders} —— "
            f"判定应当只在 selfmodel/fact_domains.py 的一张表里")

    def test_reverse_the_domain_literal_guard_fires(self, tmp_path):
        """
        **反向测试**：把字面量真的写进一个文件，确认同一段判定**会红**。
        """
        evil = tmp_path / "bad_service.py"
        evil.write_text(
            "def check(domain):\n"
            "    if domain == 'economy':\n"
            "        return False\n"
            "    return True\n",
            encoding="utf-8")
        assert _scan_domain_literals(evil), (
            "守卫认不出注入的系统域字面量 —— 它是假绿的")

    # ---- §六 B ----
    def test_b_self_model_cannot_change_the_ledger_balance(self, sm):
        """§六 B：Ledger 余额 99 不因任何 Self Model 写入而改变。"""
        from core.ledger import Ledger

        db, store, svc = sm
        ledger = Ledger(db=db)
        ledger.record("GENESIS_MINT", 99.0, to_agent=AGENT, asset="AIC")
        assert ledger.balance_of(AGENT, "AIC") == 99.0

        # 先写一条普通 Claim（合法）
        ok = svc.apply(_claim("我倾向于在早期生产阶段减少高成本探索。",
                              fact_domain=FACT_DOMAIN_SELF))
        assert ok.committed is True
        # 再试一条余额断言（非法）
        bad = svc.apply(_claim("我的 AIC 余额是 43",
                               fact_domain=FACT_DOMAIN_ECONOMY))
        assert bad.committed is False

        assert ledger.balance_of(AGENT, "AIC") == 99.0
        assert len(store.retrieve(AGENT).claims) == 1

    # ---- §六 C ----
    def test_c_controlled_write_does_not_touch_ledger(self, sm):
        """§六 C：Controlled Write 之后 Ledger 数据**完全不受影响**。"""
        from core.ledger import Ledger

        db, _store, svc = sm
        ledger = Ledger(db=db)
        ledger.record("GENESIS_MINT", 100.0, to_agent=AGENT, asset="AIC")
        ledger.record("LLM_COST", 1.5, from_agent=AGENT,
                      to_agent="SYS_TREASURY", asset="AIC")

        before = db.fetchall("SELECT tx_id FROM ledger_transactions ORDER BY tx_id")

        svc.apply(_claim("我发现自己过去经常过度消耗 AIC。",
                         fact_domain=FACT_DOMAIN_SELF))
        svc.apply(_strategy("早期少做高成本探索",
                            fact_domain=FACT_DOMAIN_SELF))
        svc.apply(_claim("我的 AIC 余额是 43", fact_domain=FACT_DOMAIN_ECONOMY))

        after = db.fetchall("SELECT tx_id FROM ledger_transactions ORDER BY tx_id")
        assert [r["tx_id"] for r in before] == [r["tx_id"] for r in after]
        assert ledger.balance_of(AGENT, "AIC") == 98.5

    # ---- §六 D ----
    def test_d_economic_belief_and_strategy_are_allowed(self, sm):
        """
        §五 / §六 D：**不要把所有经济内容都禁止**。

            ❌ 系统事实：「我的余额是 43。」
            ✅ Agent 认知：「我发现自己过去经常过度消耗 AIC。」
        """
        _db, store, svc = sm

        for content in (
            "我过去在长篇任务中经常过早消耗资源。",
            "我发现前期探索阶段使用昂贵模型的收益较低。",
            "在任务接近提交时，编辑反馈的成本通常值得。",
        ):
            out = svc.apply(_claim(content, fact_domain=FACT_DOMAIN_SELF))
            assert out.committed is True, (content, out.validation.to_dict())

        s = svc.apply(_strategy("我倾向于在早期生产阶段减少高成本探索。",
                                fact_domain=FACT_DOMAIN_SELF,
                                rationale="过去几次早期探索的产出回报很低"))
        assert s.committed is True
        assert len(store.retrieve(AGENT).strategies) == 1

    def test_revise_claim_also_needs_fact_domain(self, sm):
        """修订路径同样受约束 —— 不能靠 revise 绕过。"""
        _db, store, svc = sm
        c = svc.apply(_claim("我容易在开头花太多", fact_domain=FACT_DOMAIN_SELF))
        assert c.committed is True

        bad = svc.apply(ReflectionProposal(
            proposal_id="p9", agent_id=AGENT, kind=KIND_REVISE_CLAIM,
            target=c.result.claim_id,
            proposed_change={"content": "我的余额是 43",
                             "fact_domain": FACT_DOMAIN_ECONOMY},
            reason="r", evidence=["exp_1"], scope={}))

        assert bad.committed is False
        assert bad.validation.code == "system_fact_not_self_owned"
        # 原 Claim 没被改动
        assert store.get_claim(c.result.claim_id).content == "我容易在开头花太多"

    def test_fact_domain_is_a_declaration_not_a_stored_field(self, sm):
        """
        🔒 `fact_domain` 参与校验但**不落库** —— 所以它不能出现在
           `changed_fields` 里。

           否则会出现「changed_fields=['fact_domain'] 而 committed=True，
           但数据库里什么都没变」—— 那正是 silent semantic no-op。
        """
        _db, _store, svc = sm
        out = svc.apply(_claim("我容易在开头花太多",
                               fact_domain=FACT_DOMAIN_SELF))
        assert out.committed is True
        assert "fact_domain" not in out.changed_fields
        assert out.changed_fields == ["content", "confidence"]

    def test_declaration_only_change_is_not_a_revision(self, sm):
        """只改声明、不改内容 → 不构成一次有效修订（不产生假成功）。"""
        _db, _store, svc = sm
        c = svc.apply(_claim("我容易在开头花太多", fact_domain=FACT_DOMAIN_SELF))

        out = svc.apply(ReflectionProposal(
            proposal_id="p10", agent_id=AGENT, kind=KIND_REVISE_CLAIM,
            target=c.result.claim_id,
            proposed_change={"content": "我容易在开头花太多",
                             "fact_domain": FACT_DOMAIN_SELF},
            reason="r", evidence=["exp_1"], scope={}))

        assert out.committed is False
        assert out.validation.code == "no_supported_changes"


# ================================================================
# §十七 D–G：Economic Context
# ================================================================
class TestEconomicContext:
    def test_reads_real_ledger_balance(self):
        from core.ledger import Ledger

        db = create_memory_database()
        db.initialize()
        ledger = Ledger(db=db)
        ledger.record("GENESIS_MINT", 100.0, to_agent=AGENT, asset="AIC")
        ledger.record("LLM_COST", 0.58, from_agent=AGENT,
                      to_agent="SYS_TREASURY", asset="AIC")

        b = EconomicContextBuilder(
            balance_of=lambda aid: ledger.balance_of(aid, "AIC"), currency="AIC")
        ctx = b.build(AGENT)

        assert ctx.current_balance == 99.42
        assert ctx.currency == "AIC"
        assert ctx.sources["current_balance"] == "Ledger.balance_of"

    def test_balance_is_dynamic_not_frozen_at_init(self):
        """
        §十：**不要**在 Agent 初始化时把余额写死。

        Session 1 balance=99.2 → 发生 LLM cost → Session 2 balance=98.7
        Context 必须显示新的真实值。
        """
        from core.ledger import Ledger

        db = create_memory_database()
        db.initialize()
        ledger = Ledger(db=db)
        ledger.record("GENESIS_MINT", 100.0, to_agent=AGENT, asset="AIC")
        ledger.record("LLM_COST", 0.8, from_agent=AGENT,
                      to_agent="SYS_TREASURY", asset="AIC")

        b = EconomicContextBuilder(
            balance_of=lambda aid: ledger.balance_of(aid, "AIC"))
        first = b.build(AGENT)
        assert first.current_balance == 99.2

        # 真实成本发生
        ledger.record("LLM_COST", 0.5, from_agent=AGENT,
                      to_agent="SYS_TREASURY", asset="AIC")

        second = b.build(AGENT)
        assert second.current_balance == 98.7
        assert second.current_balance != first.current_balance

    def test_unavailable_fields_are_reported_not_faked(self):
        """§十一：取不到就写 not currently available，**不伪造 0**。"""
        b = EconomicContextBuilder(balance_of=lambda aid: None)
        ctx = b.build(AGENT)

        assert ctx.current_balance is None       # None ≠ 0.0
        assert "current_balance" in ctx.unavailable
        assert "not currently available" in "\n".join(ctx.to_lines())

    def test_task_and_session_cost_come_from_callers(self):
        b = EconomicContextBuilder(
            balance_of=lambda aid: 100.0,
            cost_of_task=lambda tid: 1.83,
            cost_since=lambda aid, since: 0.42,
        )
        ctx = b.build(AGENT, task_id="task_novel_001",
                      session_started_at="2026-10-07T00:00:00+00:00")

        assert (ctx.current_task_cost, ctx.current_session_cost) == (1.83, 0.42)
        assert ctx.unavailable == ()
        assert "Current task cost: 1.8300 AIC" in "\n".join(ctx.to_lines())

    def test_broken_callback_is_none_not_zero(self):
        """取值函数炸了 → None（不知道），**不是** 0（以为破产了）。"""
        def boom(_):
            raise RuntimeError("ledger down")

        ctx = EconomicContextBuilder(balance_of=boom).build(AGENT)
        assert ctx.current_balance is None
        assert "current_balance" in ctx.unavailable

    def test_no_other_ledger_is_created(self):
        """
        §十七 I：**不会出现多套经济账本**。

        EconomicContextBuilder 只持有回调，**不认识任何数据库**。
        """
        src = (ROOT / "cognition" / "economic.py").read_text("utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for m in modules:
                assert not m.startswith(("storage", "core.ledger",
                                         "core.economy", "core.wallet")), (
                    f"cognition/economic.py 导入了 {m} —— "
                    f"经济事实只能通过注入的回调取，不能自己开账本")


# ================================================================
# §十七 E–G：进入 CognitiveContext，且不含指令
# ================================================================
class TestEconomicContextEntersCognition:
    @staticmethod
    def _builder(balance=99.42):
        from cognition import CognitiveContextBuilder
        return CognitiveContextBuilder(
            economic_builder=EconomicContextBuilder(
                balance_of=lambda aid: balance))

    def test_lands_on_the_cognitive_context_field(self):
        """§十二：结构化字段，**不是**一坨不可解析的大字符串。"""
        ctx = self._builder().build(AGENT)

        assert isinstance(ctx.economic_context, EconomicContext)
        assert ctx.economic_context.current_balance == 99.42
        assert ctx.stats()["economic_context_present"] is True

    def test_appears_as_its_own_source_block(self):
        ctx = self._builder().build(AGENT)
        sources = {s.source: s for s in ctx.sources()}

        assert "economic_context" in sources
        assert sources["economic_context"].metadata["source"] == "Ledger"
        assert "Current balance: 99.4200 AIC" in sources[
            "economic_context"].content

    def test_rendered_into_the_text_the_llm_sees(self):
        text = self._builder().build(AGENT).to_text()
        assert "Economic context:" in text
        assert "Currency: AIC" in text
        assert "Current balance: 99.4200 AIC" in text

    def test_render_contains_no_behavioural_instruction(self):
        """
        §九 / §十七 G：**禁止**经济行为指令。

        检查的是渲染出来的**真实文本**，而且用的是本仓已有的
        `BANNED_RULE_WORDS` —— 不另造一份词表。
        """
        from cognition.context import BANNED_RULE_WORDS

        text = self._builder().build(AGENT).to_text().lower()
        for word in BANNED_RULE_WORDS:
            assert word not in text, f"经济段里出现了指令词 {word!r}"

        for forbidden in ("save aic", "minimize cost", "prefer cheap",
                          "avoid spending", "maximize profit",
                          "you should", "profit_target", "economic_score",
                          "recommended_model", "省钱", "节约", "必须"):
            assert forbidden not in text, f"出现了经济指令：{forbidden!r}"

    def test_reverse_the_instruction_guard_fires(self):
        """
        **反向测试**：把指令塞进一段渲染文本，确认上面那套判定**真的会红**。
        """
        from cognition.context import BANNED_RULE_WORDS

        evil = ("Economic context:\nCurrency: AIC\n"
                "Current balance: 99.42 AIC\nYou should save AIC.\n").lower()
        hits = [w for w in BANNED_RULE_WORDS if w in evil]
        assert hits, "指令守卫认不出注入的 'You should save AIC.' —— 它是假绿的"

    def test_absent_builder_means_absent_section(self):
        """向后兼容：不注入 economic_builder 时，行为与以前完全一致。"""
        from cognition import CognitiveContextBuilder

        ctx = CognitiveContextBuilder().build(AGENT)
        assert ctx.economic_context is None
        assert "Economic context:" not in ctx.to_text()
        assert ctx.stats()["economic_context_present"] is False

    def test_broken_economic_builder_does_not_break_cognition(self):
        """经济事实取不到 → 缺这一段，但 Cognition 照常返回。"""
        from cognition import CognitiveContextBuilder

        class _Boom:
            def build(self, *a, **kw):
                raise RuntimeError("boom")

        ctx = CognitiveContextBuilder(economic_builder=_Boom()).build(AGENT)
        assert ctx.economic_context is None


# ================================================================
# §十七 F / J：Provider 真的收到，且 Brain 不认识具体 Provider
# ================================================================
class TestProviderReceivesEconomicContext:
    def test_economic_facts_reach_the_provider_system_prompt(self):
        """
        §十七 F：Provider 收到的 system 里**必须**含经济事实。

        用的是 Capture provider —— 它只记录收到了什么。
        """
        from agents.brain import Brain, BrainSettings
        from cognition import CognitiveContextBuilder

        class _Capture:
            def __init__(self):
                self.systems = []

            def generate(self, messages=None, system=None, **kw):
                from llm.base import LLMResponse
                self.systems.append(system)
                return LLMResponse(provider="cap", model="cap", content="ok",
                                   input_tokens=1, output_tokens=1,
                                   total_tokens=2, latency_ms=1.0,
                                   simulated=True)

        class _Router:
            def __init__(self, p):
                self.p = p

            def get_provider(self, name, credential=None):
                return self.p

            def generate(self, messages=None, provider=None, model=None,
                         system=None, **kw):
                return self.p.generate(messages=messages, system=system, **kw)

        capture = _Capture()
        brain = Brain(
            router=_Router(capture),
            settings=BrainSettings(agent_id=AGENT, provider="mock",
                                   model="m"),
            context_builder=CognitiveContextBuilder(
                economic_builder=EconomicContextBuilder(
                    balance_of=lambda aid: 99.42)),
            default_context_query={},
        )

        brain.generate(messages="做点什么")

        assert capture.systems, "provider 没收到 system"
        assert "Current balance: 99.4200 AIC" in capture.systems[0]

    def test_brain_has_no_provider_name_branch(self):
        """§十七 J：Brain 不依赖具体 Provider（沿用既有守卫的判据）。"""
        known = {"mock", "deepseek", "doubao", "grok",
                 "bailian_qwen", "bailian_glm"}
        tree = ast.parse((ROOT / "agents" / "brain.py").read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for c in node.comparators:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    assert c.value.lower() not in known, (
                        f"brain.py 里出现了和 {c.value!r} 的比较")


# ================================================================
# 架构守卫：cognition 不认识账本
# ================================================================
class TestCognitionDoesNotKnowTheLedger:
    """§八：Ledger 是唯一经济事实源；cognition 只能拿到**值**。"""

    FORBIDDEN = ("core.ledger", "core.economy", "core.wallet",
                 "storage.database", "storage")

    def _scan(self, root: Path) -> list:
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    if any(m == f or m.startswith(f + ".") for f in self.FORBIDDEN):
                        offenders.append(f"{path.name}: {m}")
        return offenders

    def test_cognition_never_imports_ledger_or_storage(self):
        assert self._scan(ROOT / "cognition") == [], (
            "cognition/ 导入了账本或数据库 —— 它只该拿到注入的值")

    def test_reverse_the_import_guard_fires(self, tmp_path):
        """**反向测试**：注入一个真的 import 账本的文件，守卫必须变红。"""
        evil = tmp_path / "cognition"
        evil.mkdir()
        (evil / "bad.py").write_text(
            "from core.ledger import Ledger\n"
            "from storage.database import Database\n",
            encoding="utf-8")
        found = self._scan(evil)
        assert len(found) == 2, f"守卫认不出注入的 import：{found}"


# ================================================================
# §十七 A：Ledger 是唯一事实源
# ================================================================
class TestLedgerIsTheOnlySourceOfTruth:
    def test_wallets_table_still_has_no_balance_column(self):
        """余额是**算出来的**，不是存下来的 —— 所以不可能有第二个事实源。"""
        db = create_memory_database()
        db.initialize()
        cols = [r["name"] for r in db.fetchall("PRAGMA table_info(wallets)")]
        assert "balance" not in cols

    def test_context_balance_equals_ledger_derivation(self):
        from core.ledger import Ledger

        db = create_memory_database()
        db.initialize()
        ledger = Ledger(db=db)
        ledger.record("GENESIS_MINT", 100.0, to_agent=AGENT, asset="AIC")
        for amount in (0.1, 0.2, 0.3):
            ledger.record("LLM_COST", amount, from_agent=AGENT,
                          to_agent="SYS_TREASURY", asset="AIC")

        ctx = EconomicContextBuilder(
            balance_of=lambda aid: ledger.balance_of(aid, "AIC")).build(AGENT)

        assert ctx.current_balance == round(100.0 - 0.6, 10)
        assert ctx.current_balance == ledger.balance_of(AGENT, "AIC")
