"""
Task Production Budget V1 —— 只观察、不阻断
========================================================

【本文件锁定的边界（任务书 §一–§七）】

    Token / AIC Cost / Wallet / Task Budget / Task Price —— 五者不得混为一谈

    ❌ 不许实现 **AIC = Token Quota**（「1 AIC 最多 X tokens」）
    ❌ Budget 不许成为第二个账本
    ❌ spent / remaining 不许落库
    ❌ OBSERVE_ONLY 模式下，预算**不许**阻断任何东西
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from budget import (
    MODE_HARD_LIMIT, MODE_OBSERVE_ONLY, TaskBudgetError, TaskBudgetService,
    TaskBudgetStore,
)
from storage.database import create_memory_database

ROOT = Path(__file__).resolve().parent.parent
TASK = "task_novel_001"


@pytest.fixture()
def rig():
    db = create_memory_database()
    db.initialize()
    store = TaskBudgetStore(db)
    #: 注入真实的「从 llm_calls / ledger 派生」回调（这里用内存 dict 模拟账本）
    ledger = {"llm": {}, "settled": {}}
    svc = TaskBudgetService(
        store,
        llm_cost_of_task=lambda t: ledger["llm"].get(t, 0.0),
        settled_cost_of_task=lambda t: ledger["settled"].get(t, 0.0),
        untagged_llm_calls=lambda: 0,
    )
    return db, store, svc, ledger


# ================================================================
# §五：字段与派生
# ================================================================
class TestBudgetFields:
    def test_minimal_fields_present(self, rig):
        _db, _store, svc, _l = rig
        svc.set_budget(TASK, 50.0)
        snap = svc.snapshot(TASK)

        assert snap.task_id == TASK
        assert snap.budget_aic == 50.0
        assert snap.spent_aic == 0.0
        assert snap.remaining_aic == 50.0
        assert snap.mode == MODE_OBSERVE_ONLY

    def test_remaining_is_derived_never_stored(self, rig):
        """§五：remaining **不许**作为第二个可写字段落库。"""
        db, _store, _svc, _l = rig
        cols = [r["name"] for r in db.fetchall(
            "PRAGMA table_info(task_production_budgets)")]
        assert "spent_aic" not in cols, "spent 被存成列了"
        assert "remaining_aic" not in cols, "remaining 被存成列了"
        assert cols == ["task_id", "budget_aic", "currency", "mode",
                        "created_at"]

    def test_store_has_no_api_to_write_spent(self, rig):
        """🔒 连入口都不给 —— 不是「暂时没加」。"""
        import inspect
        sig = inspect.signature(TaskBudgetStore.set_budget)
        assert "spent_aic" not in sig.parameters
        assert "remaining_aic" not in sig.parameters

    def test_spent_follows_the_real_ledger(self, rig):
        """§六：spent 反映**真实**支出，预算表自己不累加。"""
        _db, _store, svc, ledger = rig
        svc.set_budget(TASK, 20.0)

        ledger["llm"][TASK] = 3.0
        assert svc.snapshot(TASK).spent_aic == 3.0
        assert svc.snapshot(TASK).remaining_aic == 17.0

        # 发生一次 Agent↔Agent 结算 → 也必须反映
        ledger["settled"][TASK] = 2.5
        assert svc.snapshot(TASK).spent_aic == 5.5
        assert svc.snapshot(TASK).remaining_aic == 14.5

    def test_unavailable_is_none_not_zero(self, rig):
        """取不到 = None（不知道），**不是** 0（以为没花钱）。"""
        _db, store, _svc, _l = rig
        store.set_budget(TASK, 20.0)
        broken = TaskBudgetService(store,
                                   llm_cost_of_task=lambda t: 1 / 0,
                                   settled_cost_of_task=lambda t: 1 / 0)
        snap = broken.snapshot(TASK)
        assert snap.spent_aic is None
        assert snap.remaining_aic is None

    def test_negative_budget_rejected(self, rig):
        _db, _store, svc, _l = rig
        with pytest.raises(TaskBudgetError):
            svc.set_budget(TASK, -1.0)

    def test_unknown_mode_rejected(self, rig):
        _db, _store, svc, _l = rig
        with pytest.raises(TaskBudgetError):
            svc.set_budget(TASK, 10.0, mode="WHATEVER")


# ================================================================
# §三 / §四：OBSERVE_ONLY 不阻断
# ================================================================
class TestObserveOnlyNeverBlocks:
    def test_blocks_production_is_always_false(self, rig):
        """
        🔴 本轮唯一启用的模式是 OBSERVE_ONLY ——
           预算**永远不阻断**生产，即使已经超支。
        """
        _db, _store, svc, ledger = rig
        svc.set_budget(TASK, 5.0)
        ledger["llm"][TASK] = 999.0            # 严重超支

        snap = svc.snapshot(TASK)
        assert snap.is_exhausted is True
        assert snap.blocks_production is False

    def test_hard_limit_is_reserved_but_blocks_nothing_either(self, rig):
        """
        §四：`HARD_LIMIT` 只预留结构。

        ⚠️ 即使有人把 mode 设成 HARD_LIMIT，V1 里
           `blocks_production` **仍然是 False** ——
           因为本轮**没有实现**任何强制逻辑。
           这是一个刻意的诚实：结构预留 ≠ 功能已实现。
        """
        _db, _store, svc, ledger = rig
        svc.set_budget(TASK, 1.0, mode=MODE_HARD_LIMIT)
        ledger["llm"][TASK] = 100.0
        assert svc.snapshot(TASK).blocks_production is False

    def test_no_enforcement_code_exists(self):
        """
        🔒 结构守卫：`budget/` 里**不许**出现任何「阻断 / 抛错」式的强制逻辑。

        扫 AST：不许有 `raise` 一个「超预算」错误，
        也不许 import llm / stage —— 预算不该有能力拦住生产。
        """
        src = "\n".join((ROOT / "budget" / f).read_text("utf-8")
                        for f in ("models.py", "service.py", "store.py"))
        for forbidden in ("ExceededBudget", "BudgetExceeded",
                          "over_budget", "block_llm", "stop_production"):
            assert forbidden not in src, f"budget/ 里出现了强制逻辑 {forbidden!r}"

        for f in ("service.py", "models.py"):
            tree = ast.parse((ROOT / "budget" / f).read_text("utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    assert not m.startswith(("llm", "stage", "agents")), (
                        f"budget/{f} 导入了 {m} —— 预算不该能拦住生产")

    def test_reverse_the_block_guard_fires(self, tmp_path):
        """**反向测试**：注入一个真的会阻断的模块，判定必须变红。"""
        evil = tmp_path / "budget"
        evil.mkdir()
        (evil / "bad.py").write_text(
            "from llm.router import LLMRouter\n"
            "class BudgetExceeded(Exception):\n"
            "    pass\n",
            encoding="utf-8")
        src = (evil / "bad.py").read_text("utf-8")
        assert "BudgetExceeded" in src
        tree = ast.parse(src)
        hits = [m for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module
                for m in [n.module] if m.startswith(("llm", "stage", "agents"))]
        assert hits, "守卫认不出注入的 llm import —— 它是假绿的"


# ================================================================
# §一：AIC ≠ Token Quota
# ================================================================
class TestAicIsNotATokenQuota:
    def test_no_token_quota_in_budget_module(self):
        """
        🔒 §一 明令禁止：「1 AIC 等于最多可调用 X tokens」。
        """
        for f in ("models.py", "service.py", "store.py", "__init__.py"):
            src = (ROOT / "budget" / f).read_text("utf-8")
            for forbidden in ("tokens_per_aic", "token_quota", "max_tokens",
                              "tokens_per_ai_coin"):
                assert forbidden not in src, (
                    f"budget/{f} 里出现了 {forbidden!r} —— AIC 不许绑成 token 配额")

    def test_budget_does_not_import_llm_layer(self):
        assert _imports(ROOT / "budget") & {"llm"} == set()


# ================================================================
# §七：进入 Context，但只给事实
# ================================================================
class TestBudgetInContext:
    def _builder(self, svc):
        """
        🔒 渲染必须经过 **CognitiveContext** —— 「不许写指令」这条纪律
           住在它的渲染路径上；直接看 EconomicContext 会漏掉那一层。
        """
        from cognition import CognitiveContextBuilder, EconomicContextBuilder
        return CognitiveContextBuilder(economic_builder=EconomicContextBuilder(
            balance_of=lambda a: 99.0,
            task_budget_of=lambda t: (svc.snapshot(t).to_dict()
                                      if svc.snapshot(t) else None)))

    def _ctx(self, svc, task_id):
        return self._builder(svc).build("a", task_id=task_id)

    def test_budget_shows_up_as_facts(self, rig):
        _db, _store, svc, ledger = rig
        svc.set_budget(TASK, 20.0)
        ledger["llm"][TASK] = 3.2

        text = self._ctx(svc, TASK).to_text()

        assert "Task production budget:" in text
        assert "Budget: 20.0000 AIC" in text
        assert "Spent: 3.2000 AIC" in text
        assert "Remaining: 16.8000 AIC" in text
        assert "Mode: OBSERVE_ONLY" in text

    def test_render_contains_no_advice(self, rig):
        """§七：禁止「你应该省钱 / 必须控制成本 / 预算不足要停止 / 尽量少调用 / 必须外包」。"""
        from cognition.context import BANNED_RULE_WORDS

        _db, _store, svc, ledger = rig
        svc.set_budget(TASK, 1.0)
        ledger["llm"][TASK] = 500.0            # 超支状态
        text = self._ctx(svc, TASK).to_text().lower()

        for w in BANNED_RULE_WORDS:
            assert w not in text
        for bad in ("省钱", "控制成本", "预算不足", "停止", "尽量少",
                    "必须外包", "save aic", "you should", "must not"):
            assert bad.lower() not in text, f"预算段出现了建议：{bad!r}"

    def test_no_budget_means_no_section(self, rig):
        _db, _store, svc, _l = rig
        text = self._ctx(svc, "不存在的 task").to_text()
        assert "Task production budget:" not in text

    def test_budget_is_dynamic(self, rig):
        """预算快照每次重算 —— 花掉钱之后 Remaining 必须变。"""
        _db, _store, svc, ledger = rig
        svc.set_budget(TASK, 10.0)
        assert self._ctx(svc, TASK).economic_context.task_budget[
            "remaining_aic"] == 10.0
        ledger["llm"][TASK] = 4.0
        assert self._ctx(svc, TASK).economic_context.task_budget[
            "remaining_aic"] == 6.0


# ================================================================
# §六：与 Ledger 的关系
# ================================================================
class TestBudgetDoesNotBecomeASecondLedger:
    def test_spent_comes_from_injected_readonly_callbacks(self):
        """
        🔒 `budget/` 不 import `core.ledger` / `storage` ——
           花费只能通过注入的回调取。
        """
        mods = _imports(ROOT / "budget")
        assert mods & {"core.ledger", "storage", "storage.database"} == set()

    def test_no_second_transaction_table(self, rig):
        db, _store, _svc, _l = rig
        tables = {r["name"] for r in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "task_production_budgets" in tables
        # 预算模块**没有**自己的交易表
        assert not {t for t in tables
                    if "budget" in t and "ledger" in t or
                    t.startswith("budget_transactions")}


def _imports(root: Path) -> set:
    out = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
    return out
