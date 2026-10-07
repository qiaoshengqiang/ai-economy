"""
Communication ≠ Commerce / Task Delegation = Commerce
========================================================

【本文件锁定的核心经济原则】

    **Communication is not commerce. Task delegation is commerce.**
    沟通不收费，生产性委托才进入经济。

【⚠️ 最容易搞混的一点（§十五）】

    「沟通不收费」**不是**「这次 LLM 调用不产生成本」。

        LLM API Cost            = 使用认知资源的成本（照记，LLM_COST）
        Agent-to-Agent Commerce = Agent 之间生产能力交换的经济交易

    本文件测的是**后者**。前者有它自己的测试。
    所以下面所有「AIC 没变」的断言，说的都是
    **Agent 之间的转账**没有发生 —— 不是「这次调用不花钱」。

【🔒 本文件不测什么】

    ❌ 不测「价格合不合理」—— 那是 Agent Strategy，Runtime 不许判断
    ❌ 不测「谁该接受」    —— 同上
    本文件只测：**结构对不对、账对不对、边界有没有被越过。**
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

#: 让测试能 import 实验目录里的 `channels`（Communication 的同步桥）
_EXP_DIR = Path(__file__).resolve().parent.parent / "experiments" / "novel_production"
if str(_EXP_DIR) not in sys.path:
    sys.path.insert(0, str(_EXP_DIR))

from core.economy import Economy
from delegation import (
    STATUS_ACCEPTED, STATUS_COMPLETED, STATUS_OFFERED, STATUS_REJECTED,
    STATUS_SETTLED, DelegationError, DelegationService, DelegationStore,
    InvalidTransitionError,
)
from storage.database import create_memory_database

ROOT = Path(__file__).resolve().parent.parent

AUTHOR = "novel_author"
EDITOR = "novel_editor"
READER = "novel_reader"
TASK = "task_novel_001"

GENESIS = 100.0


# ================================================================
# 夹具：**真实** Economy + 真实 Ledger（不用假账本）
# ================================================================
@pytest.fixture()
def rig(config):
    """
    三个 Agent，各 100 AIC 创世铸币，一个真的 Ledger。

    🔒 刻意用真 Economy —— 「余额检查」「Ledger 守恒」这两件事
       如果用假账本测，就等于没测。
    """
    db = create_memory_database()
    eco = Economy(db=db, config=config)
    for aid in (AUTHOR, EDITOR, READER):
        eco.create_agent(aid)
    store = DelegationStore(db)
    svc = DelegationService(store, eco)
    try:
        yield eco, store, svc
    finally:
        eco.close()


def balance(eco, agent_id) -> float:
    return eco.ledger.balance_of(agent_id, eco.currency)


def total_aic(eco) -> float:
    """整个账本上所有 Agent 的 AIC 之和（守恒用）。"""
    rows = eco.db.fetchall(
        "SELECT COALESCE(SUM(CASE WHEN to_agent   IS NOT NULL AND to_agent   "
        "NOT IN ('SYS_TREASURY','') THEN amount ELSE 0 END), 0)"
        "     - COALESCE(SUM(CASE WHEN from_agent IS NOT NULL AND from_agent "
        "NOT IN ('SYS_TREASURY','') THEN amount ELSE 0 END), 0) AS t "
        "FROM ledger_transactions")
    return round(float(rows[0]["t"]), 6)


# ================================================================
# §十九 1 / §十 E / §十四 Case 1：Communication 不产生 transfer
# ================================================================
class TestCommunicationIsNotCommerce:
    @staticmethod
    def _discuss(comm, bridge, sender, receiver, text):
        """一次**普通沟通**：发一条 TEXT 消息，对方读走。"""
        import channels
        channels.send(comm, bridge, from_agent=sender, to_agent=receiver,
                      text=text)
        return channels.drain(comm, bridge, receiver)

    @pytest.fixture()
    def channels(self):
        import channels as ch
        b = ch.SyncBridge()
        try:
            yield ch, b
        finally:
            b.close()

    @pytest.fixture()
    def comm(self, rig):
        from communication import CommunicationManager, LocalTransport
        from tests.communication_helpers import FakeAgent

        mgr = CommunicationManager(transport=LocalTransport())
        for aid in (AUTHOR, EDITOR, READER):
            mgr.register(FakeAgent(aid), capabilities=())
        return mgr

    def test_case1_communication_produces_no_aic_transfer(
            self, rig, comm, channels):
        """
        §十四 Case 1 / §十 E：**这是本轮最重要的两个 Case 之一。**

            Author → Editor：「你觉得这段人物塑造怎么样？」
            Editor → Author：「人物动机略显薄弱。」

        结果必须是：Message ✅ · Wakeup ✅ · **AIC Transfer = 0**
        """
        eco, store, _svc = rig
        ch, bridge = channels

        before = {a: balance(eco, a) for a in (AUTHOR, EDITOR, READER)}
        tx_before = eco.db.fetchall(
            "SELECT tx_id FROM ledger_transactions ORDER BY tx_id")

        # --- 一个来回 ---
        ch.send(comm, bridge, from_agent=AUTHOR, to_agent=EDITOR,
                text="你觉得这段人物塑造怎么样？")
        assert comm.is_woken(EDITOR) is True          # Wakeup 发生了
        got = ch.drain(comm, bridge, EDITOR)
        assert len(got["received"]) == 1              # Message 收到了

        ch.send(comm, bridge, from_agent=EDITOR, to_agent=AUTHOR,
                text="人物动机略显薄弱。")
        assert len(ch.drain(comm, bridge, AUTHOR)["received"]) == 1

        # --- 断言：经济上什么都没发生 ---
        after = {a: balance(eco, a) for a in (AUTHOR, EDITOR, READER)}
        assert after == before, "普通沟通改变了余额 —— 沟通变成了交易"

        tx_after = eco.db.fetchall(
            "SELECT tx_id FROM ledger_transactions ORDER BY tx_id")
        assert [r["tx_id"] for r in tx_after] == \
               [r["tx_id"] for r in tx_before], "沟通产生了 Ledger entry"

        # --- 也没有产生任何委托 ---
        assert store.count() == 0

    def test_communication_layer_does_not_know_delegation(self):
        """
        🔒 结构守卫：`communication/` **不许** import `delegation/`。

        通信层一旦认识委托，下一步就会开始判断「这条消息值多少钱」——
        那正是 `Communication ≠ Business Logic` 这条边界被破坏的形状。
        """
        assert _scan_imports(ROOT / "communication",
                             forbidden=("delegation",)) == []

    def test_delegation_layer_does_not_know_communication(self):
        """
        🔒 反向：`delegation/` 也不 import `communication/`。

        委托是**经济对象**，不是消息。让它依赖消息类型，
        以后换协议就要改经济逻辑。
        """
        assert _scan_imports(ROOT / "delegation",
                             forbidden=("communication",)) == []

    def test_reverse_the_cross_layer_guard_fires(self, tmp_path):
        """**反向测试**：注入真的跨层 import，同一段判定必须变红。"""
        evil = tmp_path / "communication"
        evil.mkdir()
        (evil / "bad.py").write_text(
            "from delegation import DelegationService\n",
            encoding="utf-8")
        assert _scan_imports(evil, forbidden=("delegation",)), (
            "跨层守卫认不出注入的 import —— 它是假绿的")


# ================================================================
# §十九 2 / 3 / §十四 Case 2：Task Delegation 产生 Offer
# ================================================================
class TestDelegationProducesOffer:
    def test_case2_task_delegation_creates_an_offer(self, rig):
        """
        §十四 Case 2：

            Author → Editor：「请你正式评审这一章，并把结构化编辑报告
                              写入 Workspace。」

        结果：Task Delegation · 产生 Offer（**不是** Message）。
        """
        _eco, store, svc = rig

        offer = svc.request(
            task_id=TASK, requester_agent_id=AUTHOR,
            provider_agent_id=EDITOR,
            description="正式评审第三章，并把结构化编辑报告写入 "
                        "editor_notes.md")

        assert offer.status == STATUS_OFFERED
        assert offer.requester_agent_id == AUTHOR
        assert offer.provider_agent_id == EDITOR
        assert offer.task_id == TASK
        assert offer.price_aic is None, "价格必须由 Provider 提出（§六）"
        assert store.count() == 1

    def test_offer_has_requester_and_provider(self, rig):
        _eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        assert o.role_of(AUTHOR) == "requester"
        assert o.role_of(EDITOR) == "provider"
        assert o.role_of(READER) == ""
        assert o.counterparty_of(AUTHOR) == EDITOR
        assert o.counterparty_of(EDITOR) == AUTHOR
        assert o.counterparty_of(READER) is None

    def test_cannot_delegate_to_yourself(self, rig):
        _eco, _store, svc = rig
        with pytest.raises(DelegationError):
            svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=AUTHOR, description="自己干")

    def test_request_alone_moves_no_money(self, rig):
        """提出委托**不是**经济行为 —— 没有报价、没有转账。"""
        eco, _store, svc = rig
        before = {a: balance(eco, a) for a in (AUTHOR, EDITOR)}
        svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                    provider_agent_id=EDITOR, description="写分析")
        assert {a: balance(eco, a) for a in (AUTHOR, EDITOR)} == before


# ================================================================
# §十九 4 / 5 / §十 A、B：拒绝
# ================================================================
class TestRejection:
    def test_provider_can_reject(self, rig):
        """§十 A：Provider 拒绝 → **没有经济交易**。"""
        eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        before = {a: balance(eco, a) for a in (AUTHOR, EDITOR)}

        out = svc.provider_respond(o.offer_id, accept=False,
                                   reason="这轮没空")

        assert out.status == STATUS_REJECTED
        assert out.rejected_by == EDITOR
        assert {a: balance(eco, a) for a in (AUTHOR, EDITOR)} == before

    def test_requester_can_reject_the_quote(self, rig):
        """§十 B：Requester 拒绝报价 → **没有经济交易**。"""
        eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        svc.provider_respond(o.offer_id, accept=True, price_aic=40.0)
        before = {a: balance(eco, a) for a in (AUTHOR, EDITOR)}

        out = svc.requester_respond(o.offer_id, accept=False,
                                    reason="太贵了")

        assert out.status == STATUS_REJECTED
        assert out.rejected_by == AUTHOR
        assert {a: balance(eco, a) for a in (AUTHOR, EDITOR)} == before

    def test_runtime_never_auto_accepts_on_price(self, rig):
        """
        §七：**不要** `if price < threshold: accept()`。

        证明方式：报一个**极低**的价（0.01 AIC），
        然后断言它**仍然停在 OFFERED**，没有被任何人自动接受。
        """
        _eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")

        out = svc.provider_respond(o.offer_id, accept=True, price_aic=0.01)

        assert out.status == STATUS_OFFERED, (
            "Runtime 因为价格低就自动接受了 —— 那是 Agent Strategy")
        assert out.has_price is True

    def test_rejected_is_terminal(self, rig):
        _eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        svc.provider_respond(o.offer_id, accept=False, reason="不接")
        with pytest.raises(InvalidTransitionError):
            svc.requester_respond(o.offer_id, accept=True)


# ================================================================
# §十九 6 / §九：Accept ≠ Settlement
# ================================================================
class TestAcceptIsNotSettlement:
    def test_accept_moves_no_money(self, rig):
        """
        🔴 §六 / §九：`ACCEPTED` **不立即把钱交给 Provider**。

        接受只是「双方同意这笔交易」；钱要到 settle 才动。
        """
        eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        svc.provider_respond(o.offer_id, accept=True, price_aic=7.5)
        before = {a: balance(eco, a) for a in (AUTHOR, EDITOR)}

        out = svc.requester_respond(o.offer_id, accept=True)

        assert out.status == STATUS_ACCEPTED
        assert out.accepted_at
        assert {a: balance(eco, a) for a in (AUTHOR, EDITOR)} == before, (
            "ACCEPTED 就把钱转走了 —— 违反 §九")
        assert out.settled_tx_id == "", "还没结算就不该有交易号"

    def test_complete_moves_no_money(self, rig):
        eco, _store, svc = rig
        o = _agreed(svc, price=7.5)
        before = {a: balance(eco, a) for a in (AUTHOR, EDITOR)}

        out = svc.complete(o.offer_id, deliverable_ref="/editor_notes.md")

        assert out.status == STATUS_COMPLETED
        assert {a: balance(eco, a) for a in (AUTHOR, EDITOR)} == before

    def test_cannot_settle_an_unaccepted_offer(self, rig):
        _eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        with pytest.raises(InvalidTransitionError):
            svc.settle(o.offer_id)


# ================================================================
# §十九 7 / §十 C：余额不足
# ================================================================
class TestInsufficientBalance:
    def test_cannot_accept_when_balance_is_too_low(self, rig):
        """
        §八 / §十 C：Requester 余额 3 AIC，Offer 8 AIC → **不能成交**。

        🔒 验证者是 Economy / Ledger，不是 Agent 自己算的。
        """
        eco, _store, svc = rig
        # 把 AUTHOR 的钱花到只剩 3
        eco.transfer(AUTHOR, READER, GENESIS - 3.0, reason="drain")
        assert balance(eco, AUTHOR) == pytest.approx(3.0)

        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        svc.provider_respond(o.offer_id, accept=True, price_aic=8.0)

        with pytest.raises(DelegationError) as exc:
            svc.requester_respond(o.offer_id, accept=True)

        assert "余额不足" in str(exc.value)
        # 状态没变，钱没动
        assert svc.store.get(o.offer_id).status == STATUS_OFFERED
        assert balance(eco, AUTHOR) == pytest.approx(3.0)
        assert balance(eco, EDITOR) == pytest.approx(GENESIS)

    def test_settlement_fails_if_balance_dropped_after_accept(self, rig):
        """
        §十 C 的第二半：**接受时够，结算时不够** → 结算必须失败。

        🔒 这正是「ACCEPTED 不预扣款」的代价 —— 如实暴露，
           不用 escrow（§五 明确 FORBIDDEN）掩盖它。
        """
        eco, _store, svc = rig
        o = _agreed(svc, price=50.0)
        svc.complete(o.offer_id, deliverable_ref="/editor_notes.md")

        # 接受之后、结算之前，AUTHOR 把钱花掉
        eco.transfer(AUTHOR, READER, 80.0, reason="spend_before_settle")
        assert balance(eco, AUTHOR) == pytest.approx(20.0)

        with pytest.raises(Exception):
            svc.settle(o.offer_id)

        # 关键断言：**没有发生部分转账，也没有出现负数余额**
        assert balance(eco, AUTHOR) == pytest.approx(20.0)
        assert balance(eco, EDITOR) == pytest.approx(GENESIS)
        assert svc.store.get(o.offer_id).status == STATUS_COMPLETED

    def test_balance_is_never_forced_negative(self, rig):
        """
        §八：**不许**把余额改成负数、**不许**假装支付成功。

        做法：接受时够、完成之后把钱用光，结算必须失败，
        而且两边的余额都仍然是非负的真实值。
        """
        eco, _store, svc = rig
        o = _agreed(svc, price=90.0)            # 接受时余额足够
        svc.complete(o.offer_id)

        eco.transfer(AUTHOR, READER, 95.0, reason="drain_before_settle")
        assert balance(eco, AUTHOR) == pytest.approx(5.0)

        with pytest.raises(Exception):
            svc.settle(o.offer_id)

        assert balance(eco, AUTHOR) == pytest.approx(5.0)
        assert balance(eco, AUTHOR) >= 0
        assert balance(eco, EDITOR) == pytest.approx(GENESIS)
        assert svc.store.get(o.offer_id).status == STATUS_COMPLETED


# ================================================================
# §十九 8–12 / §十 D：完成 → 结算
# ================================================================
class TestSettlement:
    def test_completed_can_settle_and_creates_ledger_entry(self, rig):
        eco, _store, svc = rig
        o = _agreed(svc, price=7.5)
        svc.complete(o.offer_id, deliverable_ref="/editor_notes.md")

        tx_before = len(eco.db.fetchall("SELECT tx_id FROM ledger_transactions"))
        out = svc.settle(o.offer_id)
        tx_after = eco.db.fetchall("SELECT tx_id FROM ledger_transactions")

        assert out.status == STATUS_SETTLED
        assert out.settled_at and out.settled_tx_id
        assert len(tx_after) == tx_before + 1, "结算必须产生**恰好一条** Ledger entry"

        row = eco.db.fetchone(
            "SELECT * FROM ledger_transactions WHERE tx_id=?",
            (out.settled_tx_id,))
        assert row["tx_type"] == "TRANSFER"
        assert row["from_agent"] == AUTHOR
        assert row["to_agent"] == EDITOR
        assert float(row["amount"]) == pytest.approx(7.5)
        assert row["task_id"] == TASK

    def test_requester_balance_decreases(self, rig):
        eco, _store, svc = rig
        o = _agreed(svc, price=7.5)
        svc.complete(o.offer_id)
        before = balance(eco, AUTHOR)
        svc.settle(o.offer_id)
        assert balance(eco, AUTHOR) == pytest.approx(before - 7.5)

    def test_provider_balance_increases_by_the_same_amount(self, rig):
        eco, _store, svc = rig
        o = _agreed(svc, price=7.5)
        svc.complete(o.offer_id)
        a0, e0 = balance(eco, AUTHOR), balance(eco, EDITOR)

        svc.settle(o.offer_id)

        assert balance(eco, EDITOR) == pytest.approx(e0 + 7.5)
        assert (a0 - balance(eco, AUTHOR)) == \
               (balance(eco, EDITOR) - e0)

    def test_ledger_total_is_conserved(self, rig):
        """§十九 12：委托结算**不创造也不销毁** AIC。"""
        eco, _store, svc = rig
        before = total_aic(eco)

        for price in (1.0, 2.5, 0.25):
            o = _agreed(svc, price=price)
            svc.complete(o.offer_id)
            svc.settle(o.offer_id)

        assert total_aic(eco) == pytest.approx(before), "AIC 总量不守恒"

    def test_cannot_settle_twice(self, rig):
        """结算是一次性的 —— 重复调用必须明确失败，而不是再转一次钱。"""
        eco, _store, svc = rig
        o = _agreed(svc, price=7.5)
        svc.complete(o.offer_id)
        svc.settle(o.offer_id)
        e_after = balance(eco, EDITOR)

        with pytest.raises(InvalidTransitionError):
            svc.settle(o.offer_id)

        assert balance(eco, EDITOR) == pytest.approx(e_after), "重复结算转了两次钱"

    def test_ledger_is_append_only(self, rig):
        """Ledger append-only：结算只新增行，不改旧行。"""
        eco, _store, svc = rig
        # 🔒 按 **rowid** 排序 = 真实插入顺序。
        #    `ORDER BY tx_id` 是错的：tx_id 是随机 hex，
        #    新行可能排到中间，于是「append-only」会被误判成失败。
        before = [dict(r) for r in eco.db.fetchall(
            "SELECT * FROM ledger_transactions ORDER BY rowid")]

        o = _agreed(svc, price=3.0)
        svc.complete(o.offer_id)
        svc.settle(o.offer_id)

        after = [dict(r) for r in eco.db.fetchall(
            "SELECT * FROM ledger_transactions ORDER BY rowid")]
        assert after[:len(before)] == before


# ================================================================
# §十九 13 / §十一：Ledger 是唯一事实源
# ================================================================
class TestLedgerIsTheOnlySourceOfTruth:
    def test_offer_table_has_no_balance_column(self, rig):
        """§十一：Offer **不许**保存「requester 当前余额 = 20」。"""
        _eco, store, _svc = rig
        cols = [r["name"] for r in store.db.fetchall(
            "PRAGMA table_info(delegation_offers)")]
        for forbidden in ("balance", "requester_balance", "provider_balance",
                          "current_balance"):
            assert forbidden not in cols, (
                f"delegation_offers 里出现了余额列 {forbidden!r} —— "
                f"余额只能来自 Ledger")

    def test_delegation_module_never_computes_a_balance(self):
        """
        🔒 结构守卫：`delegation/` 里**不许**出现余额计算。

        扫 AST：不许 import Ledger，不许调 `balance_of`。
        """
        offenders = []
        for path in sorted((ROOT / "delegation").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in (
                        "balance_of", "balance"):
                    offenders.append(f"{path.name}:{node.lineno} {node.attr}")
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("core.ledger"):
                        offenders.append(f"{path.name}: import core.ledger")
        assert offenders == [], (
            f"delegation/ 里出现了余额计算：{offenders}")

    def test_reverse_the_balance_guard_fires(self, tmp_path):
        """**反向测试**：注入一个真的算余额的文件，同一段判定必须变红。"""
        evil = tmp_path / "delegation"
        evil.mkdir()
        (evil / "bad.py").write_text(
            "from core.ledger import Ledger\n"
            "def f(ledger, a):\n"
            "    return ledger.balance_of(a)\n",
            encoding="utf-8")
        found = _scan_balance_in(evil)
        assert len(found) == 2, f"守卫认不出注入的余额计算：{found}"

    def test_self_model_cannot_hold_a_real_balance(self):
        """
        §十二：Self Model 可以有「我过去为类似任务报价 6 AIC，成功率较高」
        （Experience / Belief / Strategy），
        **不能**有「我的真实余额现在是 12 AIC」。

        🔒 这条由上一轮的 `fact_domains` 强制 —— 这里只做回归确认。
        """
        from selfmodel.fact_domains import (
            FACT_DOMAIN_ECONOMY, FACT_DOMAIN_SELF, is_system_domain)

        assert is_system_domain(FACT_DOMAIN_ECONOMY) is True
        assert is_system_domain(FACT_DOMAIN_SELF) is False

    def test_experiment_runner_must_not_touch_the_ledger_directly(self):
        """
        §十九 14：Experiment Runner **不直接修改 Ledger**。

        扫实验脚本：不许出现 `ledger.record(` / `wallet.transfer_to(`。
        转账只有一条路 —— `DelegationService.settle()` → `Economy.transfer()`。
        """
        offenders = []
        exp = ROOT / "experiments"
        for path in sorted(exp.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr in ("record", "transfer_to") and \
                        "ledger" in ast.dump(node.func).lower():
                    offenders.append(f"{path.name}:{node.lineno}")
                if node.func.attr == "transfer" and \
                        "economy" in ast.dump(node.func).lower():
                    offenders.append(f"{path.name}:{node.lineno} economy.transfer")
        assert offenders == [], (
            f"实验脚本直接动账本了：{offenders} —— 必须走 DelegationService")


# ================================================================
# §十三：委托进入 Context，但**不带建议**
# ================================================================
class TestPendingOffersInContext:
    def _builder(self, svc, eco):
        from cognition import CognitiveContextBuilder, EconomicContextBuilder

        def pending(agent_id):
            return [{
                "offer_id": o.offer_id,
                "task_id": o.task_id,
                "role": o.role_of(agent_id),
                "counterparty": o.counterparty_of(agent_id),
                "price_aic": o.price_aic,
                "status": o.status,
            } for o in svc.pending_for(agent_id)]

        return EconomicContextBuilder(
            balance_of=lambda aid: eco.ledger.balance_of(aid, eco.currency),
            pending_offers_of=pending)

    @staticmethod
    def _cognitive(econ_builder):
        """
        🔒 渲染必须经过 **CognitiveContext** ——
           因为「不许写指令」这条纪律住在 CognitiveContext 的渲染路径上。
           直接看 EconomicContext 会漏掉那一层。
        """
        from cognition import CognitiveContextBuilder
        return CognitiveContextBuilder(economic_builder=econ_builder)

    def test_pending_offer_shows_up_for_both_sides(self, rig):
        eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        svc.provider_respond(o.offer_id, accept=True, price_aic=6.0)

        b = self._builder(svc, eco)
        a_ctx = b.build(AUTHOR)
        e_ctx = b.build(EDITOR)
        r_ctx = b.build(READER)

        assert a_ctx.pending_offers[0]["role"] == "requester"
        assert a_ctx.pending_offers[0]["price_aic"] == 6.0
        assert e_ctx.pending_offers[0]["role"] == "provider"
        assert r_ctx.pending_offers == ()          # 不相关的人看不到

    def test_settled_offer_disappears_from_pending(self, rig):
        eco, _store, svc = rig
        o = _agreed(svc, price=2.0)
        svc.complete(o.offer_id)
        svc.settle(o.offer_id)

        assert self._builder(svc, eco).build(AUTHOR).pending_offers == ()

    def test_context_offers_no_advice(self, rig):
        """§十三 / §九：**只提供事实**，不写「你应该接受 / 拒绝」。"""
        from cognition.context import BANNED_RULE_WORDS

        eco, _store, svc = rig
        o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                        provider_agent_id=EDITOR, description="写分析")
        svc.provider_respond(o.offer_id, accept=True, price_aic=6.0)

        text = self._cognitive(self._builder(svc, eco)).build(AUTHOR) \
            .to_text().lower()
        for word in BANNED_RULE_WORDS:
            assert word not in text, f"委托段里出现了指令词 {word!r}"
        for forbidden in ("你应该", "接受它", "拒绝它", "太贵", "划算",
                          "you should accept", "reject it"):
            assert forbidden.lower() not in text

    def test_none_when_there_are_none(self, rig):
        eco, _store, svc = rig
        text = self._cognitive(self._builder(svc, eco)).build(AUTHOR).to_text()
        assert "Pending task delegations involving you: none" in text


# ================================================================
# §十九 15 / 16：不回归
# ================================================================
class TestNoRegression:
    def test_brain_does_not_depend_on_a_specific_provider(self):
        known = {"mock", "deepseek", "doubao", "grok",
                 "bailian_qwen", "bailian_glm"}
        tree = ast.parse((ROOT / "agents" / "brain.py").read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for c in node.comparators:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    assert c.value.lower() not in known

    def test_delegation_does_not_import_economy_module(self):
        """
        🔒 `delegation/` 不 import `core/economy.py` ——
           economy 是**注入**的（鸭子类型），不是 import 来的。

           这样它既能脱离 Economy 单测，也不会长出第二套账本。
        """
        assert _scan_imports(ROOT / "delegation",
                             forbidden=("core.economy", "core.wallet")) == []


# ================================================================
# 辅助
# ================================================================
def _agreed(svc, *, price: float):
    """走完 request → provider 报价 → requester 接受，停在 ACCEPTED。"""
    o = svc.request(task_id=TASK, requester_agent_id=AUTHOR,
                    provider_agent_id=EDITOR, description="正式评审一章")
    svc.provider_respond(o.offer_id, accept=True, price_aic=price)
    return svc.requester_respond(o.offer_id, accept=True)


def _scan_imports(root: Path, *, forbidden) -> list:
    """扫一棵源码树里的 import 模块名（AST，不做子串匹配）。"""
    out = []
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
                if any(m == f or m.startswith(f + ".") for f in forbidden):
                    out.append(f"{path.name}: {m}")
    return out


def _scan_balance_in(root: Path) -> list:
    """扫一棵源码树里对余额的引用（与正向守卫同一段判定）。"""
    out = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                    "balance_of", "balance"):
                out.append(f"{path.name}:{node.lineno} {node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("core.ledger"):
                    out.append(f"{path.name}: import core.ledger")
    return out
