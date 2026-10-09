"""
Production Stage V1 —— 测试
============================================================

覆盖 §三十一 的 13 个测试域。
"""

from __future__ import annotations

import ast
import pathlib
import time

import pytest

from actions import ActionIntent
from stage import (
    DECISION_PAUSE, DECISION_STOP, DECISION_SUBMIT_STAGE,
    FORBIDDEN_STAGE_FIELDS, REASON_HARD_LIMIT,
    SESSION_STOPPED_HARD_LIMIT, STATUS_ACTIVE, STATUS_COMPLETED,
    STATUS_FAILED, STATUS_PENDING, STATUS_WAITING, STATUS_CANCELLED,
    StageControlExecutor, StageError, StageStore, RuntimeSession,
)
from storage.database import Database

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def store(tmp_path):
    db = Database(str(tmp_path / "stage.db"))
    db.initialize()
    yield StageStore(db)
    try:
        db.close()
    except Exception:                                   # noqa: BLE001
        pass


def _active(store, **kw):
    kw.setdefault("deliverables", [{"name": "chapter_01.md"}])
    return store.create_stage("task_1", "完成第 1～10 章",
                              status=STATUS_ACTIVE, **kw)


# ================================================================
# 1 / 13. Persistence + Restart Recovery
# ================================================================
class TestPersistence:
    def test_stage_survives_restart(self, tmp_path):
        path = str(tmp_path / "p.db")
        d1 = Database(path); d1.initialize()
        s = StageStore(d1).create_stage("task_1", "完成第 1～10 章",
                                        status=STATUS_ACTIVE)
        StageStore(d1).save_checkpoint(s.stage_id, reason="agent_stop",
                                       workspace_id="WS-1", revision="rev-7")
        d1.close()

        d2 = Database(path); d2.initialize()
        st2 = StageStore(d2)
        got = st2.get_stage(s.stage_id)
        assert got is not None and got.goal == "完成第 1～10 章"
        ck = st2.latest_checkpoint(s.stage_id)
        assert ck is not None and ck.revision == "rev-7"
        d2.close()

    def test_long_gap_then_recover(self, tmp_path):
        """§二十三：Runtime 退出后跨时间恢复同一 Stage。"""
        path = str(tmp_path / "gap.db")
        d1 = Database(path); d1.initialize()
        st1 = StageStore(d1)
        s = _active(st1)
        st1.wait(s.stage_id, "feedback")
        d1.close()

        d2 = Database(path); d2.initialize()
        st2 = StageStore(d2)
        got = st2.get_stage(s.stage_id)
        assert got.status == STATUS_WAITING and got.waiting_for == "feedback"
        # 新 Runtime 恢复
        assert st2.resume(s.stage_id).status == STATUS_ACTIVE
        d2.close()


# ================================================================
# 2. Lifecycle
# ================================================================
class TestLifecycle:
    def test_main_transitions(self, store):
        s = store.create_stage("t", "goal")
        assert s.status == STATUS_PENDING
        assert store.activate(s.stage_id).status == STATUS_ACTIVE
        w = store.wait(s.stage_id, "human_input")
        assert w.status == STATUS_WAITING and w.waiting_for == "human_input"
        assert store.resume(s.stage_id).status == STATUS_ACTIVE
        assert store.complete(s.stage_id).status == STATUS_COMPLETED

    def test_waiting_for_only_in_waiting(self, store):
        """🔒 只有 WAITING 才带等待原因（原因是信息，不是状态）。"""
        s = _active(store)
        assert store.wait(s.stage_id, "feedback").waiting_for == "feedback"
        assert store.activate(s.stage_id).waiting_for == ""

    def test_unknown_status_rejected(self, store):
        s = _active(store)
        with pytest.raises(StageError):
            store.set_status(s.stage_id, "MADE_UP")

    def test_unknown_waiting_for_rejected(self, store):
        s = _active(store)
        with pytest.raises(StageError):
            store.wait(s.stage_id, "made_up_reason")

    def test_current_stage_returns_open_one(self, store):
        s = _active(store)
        assert store.current_stage("task_1").stage_id == s.stage_id
        store.complete(s.stage_id)
        assert store.current_stage("task_1") is None


# ================================================================
# 3. 🔴🔴 最高守卫：Stage 不得成为 Workflow Script（§七）
# ================================================================
class TestNoWorkflowScript:
    def test_stage_has_no_forbidden_fields(self):
        from stage import Stage
        import dataclasses
        names = {f.name for f in dataclasses.fields(Stage)}
        bad = names & set(FORBIDDEN_STAGE_FIELDS)
        assert not bad, f"Stage 出现 workflow 字段：{sorted(bad)}"

    def test_stage_to_dict_has_no_forbidden_keys(self, store):
        d = _active(store).to_dict()
        bad = set(d) & set(FORBIDDEN_STAGE_FIELDS)
        assert not bad

    def test_ast_guard_no_workflow_fields_in_source(self):
        """
        🔴 用 AST（不是字符串搜索 —— 子串误报在本项目反复出现过）
        扫 `stage/` 下所有源码，禁止出现固定执行顺序的字段名。
        """
        offenders = []
        for py in (ROOT / "stage").glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.AnnAssign) and \
                        isinstance(node.target, ast.Name):
                    if node.target.id in FORBIDDEN_STAGE_FIELDS:
                        offenders.append(f"{py.name}:{node.target.id}")
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and \
                                t.id in FORBIDDEN_STAGE_FIELDS:
                            offenders.append(f"{py.name}:{t.id}")
        assert not offenders, (
            f"stage/ 出现了 Workflow Script 字段 {offenders} —— "
            f"Agent 会从「决定怎么做」退化成「执行脚本」"
        )

    def test_no_stage_engine_classes(self):
        """§三十二：禁止 StageEngine / WorkflowEngine / StagePlanner。"""
        banned = ("StageEngine", "WorkflowEngine", "ProjectManager",
                  "ProductionManager", "StagePlanner", "StageRunner")
        hits = []
        for py in (ROOT / "stage").glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for b in banned:
                if f"class {b}" in text:
                    hits.append(f"{py.name}:{b}")
        assert not hits, f"出现 Stage 专用引擎：{hits}"

    def test_stage_has_no_step_execution_api(self, store):
        """Store 不应有「执行下一步」这类 API。"""
        banned = {"run_step", "next_step", "execute_steps", "run_workflow",
                  "execute_pipeline", "advance_step"}
        found = {n for n in dir(store) if n in banned}
        assert not found, f"StageStore 出现步骤执行 API：{sorted(found)}"


# ================================================================
# 4. Stage → Cognitive Context（§二十七 / §二十八）
# ================================================================
class TestStageInContext:
    def test_render_contains_state_not_instruction(self, store):
        from stage.context import render_stage
        s = _active(store)
        ck = store.save_checkpoint(s.stage_id, reason="agent_stop",
                                   workspace_id="WS-1", revision="rev-7")
        text = render_stage(s, checkpoint=ck, limit_seconds=5400,
                            elapsed_seconds=3780)
        assert "完成第 1～10 章" in text          # Goal
        assert "Status: ACTIVE" in text
        assert "revision=rev-7" in text
        # 🔴 2026-10-10 改措辞：原先写 `Hard runtime limit: N minutes`，
        #    而 Agent 把它读成了**自己的写作时间预算**（实测它的思考原话：
        #    「The runtime limit is 4 minutes, elapsed 0」⇒「赶紧写完」）。
        #    🔒 它**作用不了**那个数字 —— 产出在一次调用内完成。
        assert "Session: 墙钟上限 90 分钟" in text
        assert "已用 63 分钟" in text
        assert "不再有新的回合" in text
        # 澄清必须**在场** —— 否则又会被读成"你还有几分钟"
        assert "不是「你写得多快」" in text
        # 而且必须告诉它「只有在收到消息时才会有下一个回合」——
        # 否则它会以为"留到后续回合"总会成立（实测连续 5 次都这么说）
        assert "只有在收到消息时" in text

    def test_rounds_left_is_rendered_when_given(self, store):
        """
        🔴 「还能给你几个回合」是 Agent **能算**的事实
           （"留到后续回合"到底可不可行）。
           "还有几分钟"它算不了 —— LLM 的产出在一次调用内完成。
        """
        from stage.context import render_stage

        s = _active(store)
        text = render_stage(s, limit_seconds=480, elapsed_seconds=0,
                            rounds_left=6)
        assert "最多再跑 6 轮" in text
        # 不给就**不渲染**（向后兼容：老的调用点一行都不用改）
        assert "最多再跑" not in render_stage(s, limit_seconds=480)
        low = text.lower()
        for w in ("must", "should", "required to"):
            assert w not in low, f"Stage 被渲染成了指令：{w}"

    def test_stage_reaches_provider(self, store, tmp_path):
        """§三十一.4：Provider / fake provider 能看到 current stage。"""
        from agents.brain import Brain, BrainSettings
        from cognition import CognitiveContextBuilder
        from stage.context import render_stage

        class Cap:
            def __init__(self): self.seen = []
            def generate(self, messages=None, system=None, **kw):
                from llm.base import LLMResponse
                self.seen.append(system)
                return LLMResponse(provider="c", model="c", content="ok",
                                   input_tokens=1, output_tokens=1,
                                   total_tokens=2, latency_ms=1.0,
                                   simulated=True)
        class R:
            def __init__(self, p): self.p = p
            def get_provider(self, n, credential=None): return self.p
            def generate(self, messages=None, provider=None, model=None,
                         system=None, **kw):
                return self.p.generate(messages=messages, system=system, **kw)

        s = _active(store)
        prov = Cap()
        b = Brain(R(prov), BrainSettings(agent_id="agent_a"),
                  context_builder=CognitiveContextBuilder(None))
        ctx = b.context_builder.build("agent_a", stimulus="继续",
                                      task="长篇小说")
        ctx.current_stage = render_stage(s, limit_seconds=5400)
        b.context_builder.build = lambda *a, **k: ctx
        b.generate(messages="hi", context_query={"domain": "fiction"})
        sys_text = prov.seen[-1] or ""
        assert "Current stage" in sys_text
        assert "完成第 1～10 章" in sys_text

    def test_stats_report_stage_presence(self, store):
        from cognition import CognitiveContextBuilder
        from stage.context import render_stage
        s = _active(store)
        ctx = CognitiveContextBuilder(None).build("agent_a")
        ctx.current_stage = render_stage(s)
        assert ctx.stats()["stage_present"] is True


# ================================================================
# 5 / 6. Agent Stop Judgment + submit_stage（§十四–§十八）
# ================================================================
class TestAgentStopJudgment:
    def test_agent_stop_saves_checkpoint_and_keeps_stage_active(self, store):
        """🔴 §十三：Agent 主动停止 → Session 停，**Stage 仍 ACTIVE**。"""
        s = _active(store)
        ex = StageControlExecutor(store)
        r = ex.execute(ActionIntent(action_type="stage_control",
                                    parameters={"decision": DECISION_STOP,
                                                "stage_id": s.stage_id,
                                                "reason": "质量已足够"}))
        assert r.success
        assert store.get_stage(s.stage_id).status == STATUS_ACTIVE
        ck = store.latest_checkpoint(s.stage_id)
        assert ck is not None and ck.reason == "agent_stop"

    def test_agent_pause_moves_to_waiting(self, store):
        s = _active(store)
        ex = StageControlExecutor(store)
        ex.execute(ActionIntent(action_type="stage_control",
                                parameters={"decision": DECISION_PAUSE,
                                            "stage_id": s.stage_id}))
        got = store.get_stage(s.stage_id)
        assert got.status == STATUS_WAITING and got.waiting_for == \
            "agent_decision"

    def test_submit_without_deliverables_not_completed(self, store):
        """🔴 §十八：Agent 说 done ≠ Stage completed。"""
        s = _active(store)
        ex = StageControlExecutor(store)
        r = ex.execute(ActionIntent(action_type="stage_control",
                                    parameters={
                                        "decision": DECISION_SUBMIT_STAGE,
                                        "stage_id": s.stage_id,
                                        "artifacts": []}))
        assert r.success is False
        assert store.get_stage(s.stage_id).status != STATUS_COMPLETED

    def test_submit_with_missing_deliverable_rejected(self, store):
        s = _active(store, deliverables=[{"name": "chapter_01.md"},
                                         {"name": "chapter_02.md"}])
        ex = StageControlExecutor(store)
        r = ex.execute(ActionIntent(action_type="stage_control",
                                    parameters={
                                        "decision": DECISION_SUBMIT_STAGE,
                                        "stage_id": s.stage_id,
                                        "artifacts": [{"name": "chapter_01.md"}]}))
        assert r.success is False and "chapter_02.md" in r.error

    def test_submit_with_deliverables_completes(self, store):
        s = _active(store)
        ex = StageControlExecutor(store)
        r = ex.execute(ActionIntent(action_type="stage_control",
                                    parameters={
                                        "decision": DECISION_SUBMIT_STAGE,
                                        "stage_id": s.stage_id,
                                        "artifacts": [{"name": "chapter_01.md"}]}))
        assert r.success and store.get_stage(s.stage_id).status == \
            STATUS_COMPLETED

    def test_unknown_decision_rejected(self, store):
        s = _active(store)
        r = StageControlExecutor(store).execute(ActionIntent(
            action_type="stage_control",
            parameters={"decision": "made_up", "stage_id": s.stage_id}))
        assert r.success is False and "未知" in r.error


# ================================================================
# 7 / 8. Hard Limit（§十–§十三）
# ================================================================
class TestHardLimit:
    def test_limit_reached_stops_session(self):
        sess = RuntimeSession(limit_seconds=0.01).start()
        time.sleep(0.02)
        assert sess.enforce() is True
        assert sess.state == SESSION_STOPPED_HARD_LIMIT

    def test_hard_limit_is_not_stage_failure_nor_completion(self, store):
        """🔴 §十二：Hard Limit ≠ Stage FAILED ≠ Stage COMPLETED。"""
        s = _active(store)
        sess = RuntimeSession(limit_seconds=0.01).start()
        time.sleep(0.02)
        assert sess.enforce()
        ck = store.save_checkpoint(s.stage_id, reason=REASON_HARD_LIMIT,
                                   revision="rev-7",
                                   progress={"done": 7, "target": 10})
        got = store.get_stage(s.stage_id)
        assert got.status == STATUS_ACTIVE       # ✅ 仍可继续
        assert got.status not in (STATUS_FAILED, STATUS_COMPLETED)
        assert ck.reason == REASON_HARD_LIMIT
        assert ck.progress == {"done": 7, "target": 10}

    def test_agent_cannot_bypass_hard_limit(self, store):
        """§十一：Agent 请求 continue / 更多时间，**不能**突破 Hard Limit。"""
        s = _active(store)
        sess = RuntimeSession(limit_seconds=0.01).start()
        time.sleep(0.02)
        ex = StageControlExecutor(store)
        r = ex.execute(ActionIntent(action_type="stage_control",
                                    parameters={"decision": "continue",
                                                "stage_id": s.stage_id,
                                                "reason": "我还需要 20 分钟"}))
        assert r.success            # Agent 的决策被接受
        # 🔴 但 Hard Limit **仍然**有效 —— Runtime 自己执行它
        assert sess.is_exceeded() is True
        assert sess.enforce() is True

    def test_zero_or_none_limit_never_exceeds(self):
        assert RuntimeSession(limit_seconds=None).start().is_exceeded() is False
        assert RuntimeSession(limit_seconds=0).start().is_exceeded() is False


# ================================================================
# 9. Checkpoint 引用 Workspace Revision（§十九–§二十一）
# ================================================================
class TestCheckpoint:
    def test_checkpoint_references_revision_not_copies(self, store):
        s = _active(store)
        ck = store.save_checkpoint(s.stage_id, reason="agent_stop",
                                   workspace_id="WS-1", revision="rev-7",
                                   progress={"done": 8})
        assert ck.workspace_id == "WS-1"
        assert ck.revision == "rev-7"
        # 🔴 不复制整个 Workspace —— 没有文件内容字段
        for bad in ("files", "content", "file_hash", "diff", "tree"):
            assert not hasattr(ck, bad), f"Checkpoint 复制了 Workspace：{bad}"

    def test_stage_points_to_latest_checkpoint(self, store):
        s = _active(store)
        store.save_checkpoint(s.stage_id, reason="a", revision="rev-1")
        ck2 = store.save_checkpoint(s.stage_id, reason="b", revision="rev-2")
        assert store.get_stage(s.stage_id).current_checkpoint == ck2.checkpoint_id

    def test_checkpoint_requires_existing_stage(self, store):
        with pytest.raises(StageError):
            store.save_checkpoint("stage_nope", reason="x")


# ================================================================
# 10 / 11. Pause / WAITING / Resume（§二十二）
# ================================================================
class TestPauseResume:
    def test_waiting_allows_runtime_to_stop(self, store):
        s = _active(store)
        store.wait(s.stage_id, "feedback")
        # Runtime 已停止 —— 持久状态仍在，不依赖进程
        assert store.get_stage(s.stage_id).status == STATUS_WAITING

    def test_wakeup_resumes_stage(self, store):
        s = _active(store)
        store.wait(s.stage_id, "feedback")
        store.add_feedback("节奏偏慢", task_id="task_1", stage_id=s.stage_id)
        # Wakeup 之后 Agent 决定继续
        assert store.resume(s.stage_id).status == STATUS_ACTIVE


# ================================================================
# 12. External Feedback（§二十四 / §二十五）
# ================================================================
class TestExternalFeedback:
    def test_feedback_links_task_and_stage(self, store):
        s = _active(store)
        fb = store.add_feedback("读者觉得节奏慢", task_id="task_1",
                                stage_id=s.stage_id, agent_id="agent_a",
                                source="human")
        assert fb.task_id == "task_1" and fb.stage_id == s.stage_id
        assert [f.feedback_id for f in store.list_feedback(
            stage_id=s.stage_id)] == [fb.feedback_id]

    def test_feedback_does_not_touch_self_model(self, tmp_path):
        """🔴 §二十五：Feedback → Runtime → Strategy update 是**禁止**的。"""
        import ast as _ast
        offenders = []
        for py in (ROOT / "stage").glob("*.py"):
            tree = _ast.parse(py.read_text(encoding="utf-8"))
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                    if "selfmodel" in (getattr(node, "module", "") or ""):
                        offenders.append(py.name)
        assert not offenders, (
            f"stage/ 依赖了 selfmodel —— Feedback 可能直接改认知：{offenders}")

    def test_invalid_feedback_source_rejected(self, store):
        with pytest.raises(StageError):
            store.add_feedback("x", source="made_up")

    def test_empty_feedback_rejected(self, store):
        with pytest.raises(StageError):
            store.add_feedback("   ")
