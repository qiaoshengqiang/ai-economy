"""
Production Cognition Telemetry Repair V2 —— 调用归属与 Turn 关联测试
======================================================================

对应规格 §10 的 Test A–I。

🔒 核心语义边界（§5）：

        LLM Call  ≠  Cognitive Turn

    但**每一条真实模型调用都必须能归类**：
        一次 Cognitive Turn → 一次 LLM Call          ✅
        一次 Cognitive Turn → 多次 LLM Call          ✅（多对一必须能表达）
        非 Cognition Utility Call（cognition_turn_id = null） ✅

🔒 全部使用 mock provider + 临时隔离数据库，**不调用真实 API**。
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from llm.base import LLMResponse
from llm.recorder import LLMCallRecorder

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
              / "production.py")

AGENT_ID = "novel_author"
RUN_ID = "run_pytest_001"


@pytest.fixture
def rt_and_db(tmp_path):
    """
    真实 Runtime + **真正的临时 DB 路径**。

    ⚠️ 不能用 `resolved_config.database_path` —— 那是 canonical 配置里的
       `data/economy.db`（**项目自己的库**）。我第一版就是这么写的，
       结果断言读的是项目库、写的是临时库，全部对不上。
    """
    from main import build_runtime

    path = tmp_path / "telemetry.db"
    rt = build_runtime(db_path=str(path), agent_ids=[AGENT_ID])
    yield rt, path
    rt.close()


@pytest.fixture
def runtime(rt_and_db):
    return rt_and_db[0]


@pytest.fixture
def db_path(rt_and_db):
    return str(rt_and_db[1])


@pytest.fixture
def agent(runtime):
    return next(a for a in runtime.economy.all_agents()
                if a.agent_id == AGENT_ID)


def _stub(agent, finish_reason="stop", content='{"thought": "x"}'):
    """把 brain.generate 换成受控响应，保留 Brain 的其余行为。

    ⚠️ 注意：这里替换的是 **Brain.generate**，所以 ``last_call_id`` /
       ``last_request_evidence`` **不会**被填 —— 需要真实记录的测试
       必须走真 Brain（见 ``_real_call``）。
    """
    agent.brain.generate = lambda messages=None, **kw: LLMResponse(
        content=content, provider="mock", model="mock_model",
        finish_reason=finish_reason, total_tokens=100, token_source="api")
    return agent


def _real_call(agent, *, purpose="cognition", turn_id="turn_x", run_id=RUN_ID,
               provider="mock", model="mock_model", messages="hi"):
    """走**真 Brain.generate**（会真正写 llm_calls 并产出边界证据）。"""
    return agent.brain.generate(
        messages=messages, provider=provider, model=model, record=True,
        call_purpose=purpose, cognition_turn_id=turn_id,
        experiment_run_id=run_id)


def _calls(db_path):
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM llm_calls ORDER BY created_at")]
    finally:
        con.close()


def _critical_count(agent):
    return len(agent.experience_store.retrieve_critical(agent.agent_id))


# ============================================================
# Test A：调用分类
# ============================================================
class TestA_CallClassification:
    def test_cognition_call_gets_cognition_purpose(self, runtime, agent, db_path):
        _real_call(agent, purpose=LLMCallRecorder.PURPOSE_COGNITION,
                   turn_id="turn_A1")
        row = _calls(db_path)[-1]
        assert row["call_purpose"] == "cognition"
        assert row["cognition_turn_id"] == "turn_A1"

    def test_utility_call_gets_utility_purpose(self, runtime, agent, db_path):
        _real_call(agent, purpose=LLMCallRecorder.PURPOSE_UTILITY,
                   turn_id=None)
        row = _calls(db_path)[-1]
        assert row["call_purpose"] == "utility"
        assert row["cognition_turn_id"] is None

    def test_undeclared_purpose_is_flagged_not_hidden(self, runtime, agent, db_path):
        """
        §5.3：不允许生产路径静默使用 unknown 充当正常值。

        🔒 未声明 → 落库为 `unspecified`，**可被审计查询暴露** ——
           而不是被悄悄当成 cognition。
        """
        agent.brain.generate(messages="hi", provider="mock",
                             model="mock_model", record=True)
        row = _calls(db_path)[-1]
        assert row["call_purpose"] == "unspecified"
        assert row["call_purpose"] != "cognition"

    def test_purpose_is_not_inferred_from_finish_reason(self, runtime, agent, db_path):
        """
        §5.3：不得从 `finish_reason=length` 推导出 `call_purpose=cognition`。
        """
        _real_call(agent, purpose=LLMCallRecorder.PURPOSE_UTILITY, turn_id=None)
        row = _calls(db_path)[-1]
        assert row["call_purpose"] == "utility"
        # 无论 finish_reason 是什么，purpose 都来自调用方
        assert row["call_purpose"] != "cognition"

    def test_production_turn_declares_purpose(self):
        """生产 `_turn` 必须**显式**声明 cognition（源码结构守卫）。"""
        tree = ast.parse(PRODUCTION.read_text(encoding="utf-8"))
        turn_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "_turn")
        code = ast.unparse(turn_fn)
        # ⚠️ ast.unparse 会把引号**规范化成单引号** ——
        #    这里必须用规范化后的形式，否则守卫会因为引号风格假红。
        assert "call_purpose='cognition'" in code, (
            "生产认知入口必须显式声明 purpose")
        assert "cognition_turn_id=cognition_turn_id" in code, (
            "turn id 必须真的传进调用链，不能只写进返回 JSON")
        assert "experiment_run_id=EXPERIMENT_RUN_ID" in code


# ============================================================
# Test B：LLM Call ID
# ============================================================
class TestB_CallId:
    def test_call_id_is_unique_and_returned(self, runtime, agent, db_path):
        ids = set()
        for i in range(5):
            r = _real_call(agent, turn_id=f"turn_{i}")
            ids.add(agent.brain.last_call_id)
            assert agent.brain.last_call_id, "record() 必须返回 call_id"
        assert len(ids) == 5, "call_id 必须唯一"
        rows = _calls(db_path)
        assert {r["call_id"] for r in rows} == ids, (
            "返回值与数据库记录必须用同一个 call_id")

    def test_failed_call_is_traceable(self, runtime, agent, db_path):
        """§5.1：Provider 异常路径也必须留下可追踪的 call_id。"""
        def _boom(messages=None, **kw):
            raise RuntimeError("模拟 provider 崩溃")

        agent.brain.router.generate = _boom
        with pytest.raises(RuntimeError):
            agent.brain.generate(messages="hi", provider="mock",
                                 model="mock_model", record=True,
                                 call_purpose="utility")
        row = _calls(db_path)[-1]
        assert row["success"] == 0
        assert "模拟 provider 崩溃" in (row["error"] or "")
        assert agent.brain.last_call_id == row["call_id"], (
            "失败路径也要把 call_id 交给调用方")

    def test_run_id_is_persisted(self, runtime, agent, db_path):
        _real_call(agent)
        row = _calls(db_path)[-1]
        assert row["experiment_run_id"] == RUN_ID


# ============================================================
# Test C：Turn 关联
# ============================================================
class TestC_TurnAssociation:
    def test_turn_id_associates_call_to_turn(self, runtime, agent, db_path):
        """
        模拟真实结构的 Cognition 链：

            BaseAgent.generate() → Brain.generate() → LLMCallRecorder

        断言 DB 里的 call 能通过 cognition_turn_id 关联到 turn。
        """
        turn_id = "turn_C_round3_novel_author"
        # 走 BaseAgent.generate —— 它才会把 last_call_id 交给调用方
        agent.generate(
            messages="hi", provider="mock", model="mock_model", record=True,
            call_purpose="cognition", cognition_turn_id=turn_id,
            experiment_run_id=RUN_ID)
        rows = _calls(db_path)
        match = [r for r in rows if r["cognition_turn_id"] == turn_id]
        assert len(match) == 1
        assert match[0]["call_purpose"] == "cognition"
        # 关联键必须就是 recorder 返回的那个 id（不是猜的）
        assert match[0]["call_id"] == agent.last_call_id

    def test_multi_call_turn_is_expressible(self, runtime, agent, db_path):
        """§5：一次 Turn → 多次 Call 是合法的，必须能表达（不是 orphan）。"""
        turn_id = "turn_C_multi"
        for _ in range(3):
            agent.brain.generate(messages="hi", provider="mock",
                                 model="mock_model", record=True,
                                 call_purpose="cognition",
                                 cognition_turn_id=turn_id,
                                 experiment_run_id=RUN_ID)
        rows = [r for r in _calls(db_path)
                if r["cognition_turn_id"] == turn_id]
        assert len(rows) == 3, "同一 turn 的多条调用必须都能按 turn id 查到"

    def test_audit_query_separates_cognition_from_utility(self, runtime, agent, db_path):
        _real_call(agent, purpose="cognition", turn_id="t1")
        _real_call(agent, purpose="utility", turn_id=None)
        rows = _calls(db_path)
        cog = [r for r in rows if r["call_purpose"] == "cognition"]
        util = [r for r in rows if r["call_purpose"] == "utility"]
        assert len(cog) == 1 and len(util) == 1
        assert all(r["cognition_turn_id"] for r in cog), (
            "每个 cognition 调用都必须有 turn id（§12.3）")
        assert all(r["cognition_turn_id"] is None for r in util), (
            "utility 调用**不应**被要求拥有 turn id（§12）")


# ============================================================
# Test D：非认知调用
# ============================================================
class TestD_UtilityCall:
    def test_utility_call_recorded_without_turn(self, runtime, agent, db_path):
        _real_call(agent, purpose="utility", turn_id=None)
        row = _calls(db_path)[-1]
        assert row["call_purpose"] == "utility"
        assert row["cognition_turn_id"] is None

    def test_utility_truncation_does_not_enter_cognitive_history(self, agent):
        """
        🔴 §8：utility 调用的截断**不得**被记成 Agent 的认知经历。

        🔒 但它仍然是一次真实的 Provider 截断 —— 技术事实不变。
        """
        _stub(agent, finish_reason="length")
        assert _critical_count(agent) == 0
        agent.generate(messages="hi", call_purpose="utility")
        assert _critical_count(agent) == 0, (
            "utility 调用的截断被误记进了 Cognitive History")

    def test_unspecified_truncation_does_not_enter_cognitive_history(self,
                                                                     agent):
        _stub(agent, finish_reason="length")
        agent.generate(messages="hi")            # 不声明 purpose
        assert _critical_count(agent) == 0


# ============================================================
# Test E：截断观察
# ============================================================
class TestE_TruncationObservation:
    def test_cognition_truncation_is_recorded_once(self, agent):
        _stub(agent, finish_reason="length")
        agent.generate(messages="hi", call_purpose="cognition",
                       cognition_turn_id="turn_E1")
        assert _critical_count(agent) == 1, "必须恰好创建一次"

    def test_truncation_keeps_technical_fact(self, runtime, agent, db_path):
        """`finish_reason` 必须原样保留（原始技术事实）。"""
        _real_call(agent, purpose="cognition", turn_id="turn_E2")
        row = _calls(db_path)[-1]
        assert row["finish_reason"] == "stop"      # mock 返回 stop
        assert row["output_status"] == "VALID_CONTENT"

    def test_observation_links_to_turn(self, agent):
        """截断观察必须能追到 turn / call 上下文（结构化事实里带得上）。"""
        _stub(agent, finish_reason="length")
        agent.generate(messages="hi", call_purpose="cognition")
        obs = agent.experience_store.retrieve_critical(agent.agent_id)[0]
        assert obs.result["observed_state"] == "cognitive_output_truncated"
        assert obs.result["finish_reason"] == "length"
        assert obs.result["cognitive_status"] == "TRUNCATED"

    def test_act_does_not_double_record(self, agent):
        _stub(agent, finish_reason="length")
        agent.act(messages="hi", call_purpose="cognition")
        assert _critical_count(agent) == 1, "截断被重复记录"

    def test_truncation_boundary_uses_purpose_not_finish_reason(self):
        """§8：判据必须是**声明的 purpose**，不是 finish_reason。"""
        src = (PROJECT_ROOT / "agents" / "base_agent.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_record_truncation_if_needed")
        code = ast.unparse(fn)
        assert "PURPOSE_COGNITION" in code, (
            "截断边界必须检查 call_purpose == cognition")
        assert "call_purpose" in code


# ============================================================
# Test F：最终 Provider 输入（§7 / §10.F）
# ============================================================
class TestF_FinalProviderInput:
    """
    🔴 不能只断言 `ctx.to_text()` 含该 ID。
       必须证明**最终送给 Provider 的那份请求**含它。
    """

    def _seed_critical(self, agent):
        """造一条真实的关键观察（走 ExperienceStore，不手工塞 ctx）。"""
        return agent.experience_store.record(
            agent.agent_id, "observation",
            result={"critical_observation": True,
                    "observed_state": "cognitive_output_truncated",
                    "cognitive_output": "TRUNCATED",
                    "finish_reason": "length"},
            feedback="上一轮认知输出未完整完成（finish_reason=length）。")

    def test_evidence_finds_critical_observation_in_final_request(self, agent):
        exp = self._seed_critical(agent)
        _real_call(agent, purpose="cognition", turn_id="turn_F1")

        ev = agent.brain.last_request_evidence
        assert ev is not None, "必须产出 Provider 边界证据"
        assert exp.experience_id in ev["critical_observation_ids_selected"], (
            "关键观察必须被 critical 通道选中")
        assert exp.experience_id in \
            ev["critical_observation_ids_found_in_final_request"], (
            "🔴 关键观察必须出现在**最终送给 Provider 的文本**里")
        assert ev["final_request_contains_expected_critical_observation"] is True

    def test_evidence_records_snapshot_id_and_hash(self, agent):
        self._seed_critical(agent)
        _real_call(agent, purpose="cognition", turn_id="turn_F2")
        ev = agent.brain.last_request_evidence
        assert ev["context_snapshot_id"], "必须有 context_snapshot_id"
        assert len(ev["request_sha256_16"]) == 16
        assert ev["request_chars"] > 0
        # 🔒 绝不记 secret
        blob = json.dumps(ev, ensure_ascii=False)
        for banned in ("Authorization", "api_key", "sk-", "Bearer"):
            assert banned not in blob, f"证据里出现了 {banned}"

    def test_evidence_is_honest_when_nothing_expected(self, agent):
        """没有关键观察时：期望为空 → 也算「没被丢掉」，但列表为空。"""
        _real_call(agent, purpose="cognition", turn_id="turn_F3")
        ev = agent.brain.last_request_evidence
        assert ev["critical_observation_ids_selected"] == []
        assert ev["critical_observation_ids_found_in_final_request"] == []
        assert ev["final_request_contains_expected_critical_observation"] is True

    def test_evidence_detects_a_missing_observation(self, agent, monkeypatch):
        """
        🔴 反向能力：如果 Context 里的关键观察**没能**进最终请求，
        证据必须报 False —— 否则这个字段就是摆设。
        """
        self._seed_critical(agent)
        import cognition.context as _c

        real = _c.CognitiveContext.to_text
        # 注入：把 **experience_id** 从渲染里剥掉 ——
        # 证据的判据就是「id 在不在最终请求里」，所以要剥 id，
        # 而不是剥那段散文标记（第一版剥错了，测试因此假绿）。
        def _strip(self):
            text = real(self)
            for e in (getattr(self, "critical_observations", ()) or ()):
                text = text.replace(e.experience_id, "")
            return text
        monkeypatch.setattr(_c.CognitiveContext, "to_text", _strip)

        _real_call(agent, purpose="cognition", turn_id="turn_F4")
        ev = agent.brain.last_request_evidence
        assert ev["critical_observation_ids_selected"], "前提：它被选中了"
        assert ev["critical_observation_ids_found_in_final_request"] == [], (
            "渲染被剥掉后，证据必须承认它**没有**进最终请求")
        assert ev["final_request_contains_expected_critical_observation"] is False


# ============================================================
# Test G：正常停止
# ============================================================
class TestG_NormalStop:
    def test_stop_creates_no_observation(self, agent):
        _stub(agent, finish_reason="stop")
        agent.generate(messages="hi", call_purpose="cognition")
        assert _critical_count(agent) == 0

    def test_output_status_is_untouched(self, agent):
        r = _stub(agent, finish_reason="stop")
        resp = agent.generate(messages="hi", call_purpose="cognition")
        assert resp.output_status == "VALID_CONTENT"


# ============================================================
# Test H：结果文件可追踪（§3 / §10.H）
# ============================================================
class TestH_ResultFileTraceability:
    def test_production_writes_state_and_run_id_atomically(self):
        """源码结构：结果必须带 run_id + state，且先写 tmp 再 replace。"""
        src = PRODUCTION.read_text(encoding="utf-8")
        assert 'log["run_id"] = EXPERIMENT_RUN_ID' in src
        assert 'log["state"] = "FINALIZED"' in src
        assert "os.replace(tmp, out)" in src, "必须原子替换，避免半写入文件"
        assert "with_suffix(\".json.tmp\")" in src

    def test_production_refuses_concurrent_runs(self):
        """§3：必须有排他运行锁 —— 上一轮的事故就是并发。"""
        import experiments.heterogeneous_production.production as P

        assert hasattr(P, "_acquire_run_lock")
        assert hasattr(P, "_pid_alive")

    def test_lock_blocks_a_live_pid(self, tmp_path, monkeypatch):
        import experiments.heterogeneous_production.production as P

        monkeypatch.setattr(P, "RESULTS", tmp_path)
        lock = tmp_path / ".hetero_run.lock"
        lock.write_text(f"run_id=run_x pid={os.getpid()}", encoding="utf-8")
        with pytest.raises(RuntimeError) as ei:
            P._acquire_run_lock("run_y")
        assert "拒绝并发启动" in str(ei.value)

    def test_stale_lock_is_taken_over(self, tmp_path, monkeypatch):
        import experiments.heterogeneous_production.production as P

        monkeypatch.setattr(P, "RESULTS", tmp_path)
        # 找一个几乎不可能存在的 pid
        lock = tmp_path / ".hetero_run.lock"
        lock.write_text("run_id=run_old pid=999999", encoding="utf-8")
        acquired = P._acquire_run_lock("run_new")
        assert acquired.exists()
        assert "run_new" in acquired.read_text(encoding="utf-8")

    def test_analysis_rejects_a_stale_result(self, tmp_path):
        """
        分析脚本必须验证 run_id / state / mtime，
        不能只判断「文件存在」—— 我上一轮就是栽在这上面。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "analyze_critical",
            PROJECT_ROOT / "experiments" / "heterogeneous_production"
            / "analyze_critical.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert hasattr(mod, "validate_result_file"), (
            "分析脚本必须暴露一个可测试的结果校验函数")

        stale = tmp_path / "stale.json"
        stale.write_text(json.dumps({
            "run_id": "run_old", "state": "FINALIZED",
            "total_wall_limit": 7200.0}), encoding="utf-8")
        ok, problems = mod.validate_result_file(stale, expected_wall_limit=3600.0)
        assert ok is False
        assert any("7200" in p for p in problems)

        running = tmp_path / "running.json"
        running.write_text(json.dumps({
            "run_id": "run_new", "state": "RUNNING",
            "total_wall_limit": 3600.0}), encoding="utf-8")
        ok, problems = mod.validate_result_file(running,
                                                expected_wall_limit=3600.0)
        assert ok is False
        assert any("RUNNING" in p or "未结束" in p for p in problems)

        good = tmp_path / "good.json"
        good.write_text(json.dumps({
            "run_id": "run_new", "state": "FINALIZED",
            "total_wall_limit": 3600.0}), encoding="utf-8")
        ok, problems = mod.validate_result_file(good, expected_wall_limit=3600.0)
        assert ok is True, problems
