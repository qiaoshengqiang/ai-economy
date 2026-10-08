"""
Completion / Acceptance 最小闭环 —— 测试
==========================================

对应实施规格 §10 的 23 项，外加一条**完整真实行为**测试（§11）。

    Positive            1–7
    Rework              8–13
    Completion bug 回归 14–17
    Negative / 守卫     18–23

🔴 纪律（规格 §13）：
    · `Task.status == COMPLETED` **必须来自真实执行链** ——
      走 `TaskService.handle_message()` → Handler → ExecutionService →
      Task Core 状态机。测试**不**直接调领域方法把状态设成 COMPLETED。
    · 不 skip / 不 xfail / 不弱化断言。

不调用真实 LLM API（这条链根本不经过 Brain）。
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from communication.message import Message, MessageType
from core.task import Task, TaskStatus
from core.task_execution import (
    COMPLETION_PROPOSAL_KEY, CriteriaStatus, CompletionProposal, TaskResultRef,
)
from core.task_shared_context import ACCEPTANCE_RESPONSIBILITY

from experiments.heterogeneous_production import production as _production

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 生产者 / 验收者 —— 与实验 runner 用的是同两个角色。
PRODUCER = "novel_author"
ACCEPTOR = "novel_reader"
OTHER = "novel_editor"

#: 🔒 用 runner **真实**的任务 ID ——
#:    `_try_complete_task()` / `_completion_facts()` 都按模块常量
#:    `TASK_ID` 去查任务，用别的 ID 它们会看不到。
#:    这也顺带验证了「runner 的操作对象就是它自己声明的那个 Task」。
TASK = _production.TASK_ID

DOD = ("手稿存在且非空", "有结尾标记")


# ============================================================
# 夹具：真实的组合根（不是模拟对象）
# ============================================================
@pytest.fixture
def rt(tmp_path):
    """
    用**真实**的 `build_runtime` 装配 —— TaskStore / TaskService /
    TaskExecutionService / Alignment / Shared Context 全部是真件。

    这样 `acceptors_of` / `criteria_of` 的注入也一起被验证了
    （它们在 `main.py` 里接线）。
    """
    from main import build_runtime

    runtime = build_runtime(db_path=str(tmp_path / "completion.db"),
                            agent_ids=[PRODUCER, ACCEPTOR, OTHER])
    try:
        yield runtime
    finally:
        runtime.close()


def _peer(actor: str) -> str:
    """给 `actor` 找一个**不是他自己**的收件人。

    🔒 协议不允许给自己发消息（MessageValidationError）——
       但「生产者试图自验」这类测试恰恰需要 from_agent=生产者。
       所以收件人单独选，与「谁在发」解耦。
    """
    for candidate in (PRODUCER, ACCEPTOR, OTHER):
        if candidate != actor:
            return candidate
    return "somebody_else"


def _msg(sender, recipient, message_type, payload, reply_to=None) -> Message:
    return Message(from_agent=sender, to_agent=recipient,
                   message_type=message_type, payload=payload,
                   reply_to=reply_to)


def _session_log(*, claimed: bool = True, index: int = 1) -> dict:
    """构造一个 **session 聚合之后**的 s_log（模拟 `_run_session` 的产物）。"""
    turns = ([{"agent_id": PRODUCER, "claims_done": True}] if claimed else
             [{"agent_id": PRODUCER, "claims_done": False}])
    return {"index": index, "turns": turns,
            "agents_claim_done": claimed,
            "claiming_agents": [PRODUCER] if claimed else [],
            "claiming_turns": 1 if claimed else 0}


def _create_task(rt, *, participants=None):
    task = Task.create(
        creator="human_demand",
        description="写一部小说",
        task_id=TASK,
        participants=list(participants or (PRODUCER, ACCEPTOR, OTHER)),
    )
    rt.tasks.save(task)
    return rt.tasks.get(TASK)


def _set_up_dod_and_acceptor(rt, *, dod=DOD, acceptor=ACCEPTOR,
                             assign_by=None):
    """① 建 DoD  ② 指派 Acceptance Responsibility（都走现有能力）。"""
    by = assign_by or PRODUCER
    rt.alignment.create_context(TASK, by=by,
                                objective="写一部小说",
                                acceptance_criteria=list(dod))
    change = rt.alignment.propose(
        TASK, proposed_by=by,
        change_type="ASSIGN",
        new_value={"agent_id": acceptor,
                   "responsibility": ACCEPTANCE_RESPONSIBILITY},
        expected_revision=rt.alignment.require_current(TASK).revision,
        reason="指派验收责任人")
    rt.alignment.accept(TASK, change.change_id, decided_by=by,
                        reason="生产者指派验收方")
    return change


def _start(rt, actor=PRODUCER):
    return rt.task_service.handle_message(
        _msg(actor, ACCEPTOR, MessageType.START, {"task_id": TASK}))


def _submit(rt, *, actor=PRODUCER, proposal=None, result_ref="manuscript/novel"):
    payload = {"task_id": TASK,
               "result": {"result_ref": result_ref,
                          "result_type": "document",
                          "summary": "手稿"}}
    if proposal is not None:
        payload[COMPLETION_PROPOSAL_KEY] = proposal.to_dict()
    return rt.task_service.handle_message(
        _msg(actor, ACCEPTOR, MessageType.SUBMIT, payload))


def _verify(rt, *, actor=ACCEPTOR, passed=True, feedback=""):
    if not passed and not feedback:
        feedback = "第三章还没写完"
    return rt.task_service.handle_message(
        _msg(actor, _peer(actor), MessageType.VERIFY,
             {"task_id": TASK, "passed": passed, "feedback": feedback}))


def _complete(rt, *, actor=ACCEPTOR):
    return rt.task_service.handle_message(
        _msg(actor, _peer(actor), MessageType.COMPLETE,
             {"task_id": TASK, "summary": "验收通过，结项"}))


def _proposal(*, status=CriteriaStatus.MET, gaps=(), evidence=None):
    return CompletionProposal(
        proposal_id="cp_test_1", task_id=TASK, proposed_by=PRODUCER,
        criteria_status=status,
        evidence=evidence or (TaskResultRef(
            result_ref="manuscript/novel", result_type="document"),),
        outstanding_gaps=tuple(gaps),
    )


def _status(rt):
    return rt.tasks.get(TASK).status


# ============================================================
# Positive（1–7）
# ============================================================
class TestPositive:
    def test_01_agent_creates_definition_of_done(self, rt):
        """① Agent 创建 DoD —— 走现有 Shared Context 能力。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)

        context = rt.alignment.require_current(TASK).context
        assert context.acceptance_criteria == DOD
        assert rt.task_execution.definition_of_done(rt.tasks.get(TASK)) == DOD

    def test_02_agent_creates_completion_proposal(self, rt):
        """② Agent 创建 Completion Proposal —— 它落在 SUBMIT 事件上。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        result = _submit(rt, proposal=_proposal())

        assert result.ok is True, result.errors
        # 从**已有的** SUBMIT 事件里读回来（不新增表）
        read_back = rt.task_execution.latest_completion_proposal(TASK)
        assert read_back is not None
        assert read_back.proposed_by == PRODUCER
        assert read_back.criteria_status == CriteriaStatus.MET
        assert len(read_back.evidence) == 1

    def test_03_acceptance_responsibility_is_established(self, rt):
        """③ Acceptance Responsibility 成功建立（复用 AssignmentRecord）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)

        context = rt.alignment.require_current(TASK).context
        assert context.assignees_for(ACCEPTANCE_RESPONSIBILITY) == (ACCEPTOR,)
        assert context.acceptors() == (ACCEPTOR,)
        # 注入到执行服务之后也读得到
        assert rt.task_execution.acceptors_of(rt.tasks.get(TASK)) == (ACCEPTOR,)

    def test_04_producer_cannot_self_verify(self, rt):
        """④ 生产者无法自验 —— 即使他给自己派了验收责任。"""
        _create_task(rt)
        # 故意把验收责任也派给生产者自己
        _set_up_dod_and_acceptor(rt, acceptor=PRODUCER)
        _start(rt)
        assert _submit(rt, proposal=_proposal()).ok is True

        result = _verify(rt, actor=PRODUCER)

        assert result.ok is False
        assert any("承接方" in e and "运动员" in e for e in result.errors)
        assert _status(rt) == TaskStatus.SUBMITTED        # 状态没动

    def test_05_acceptance_pass(self, rt):
        """⑤ Acceptance PASS —— 由非生产者发出。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())

        result = _verify(rt, actor=ACCEPTOR, passed=True)

        assert result.ok is True, result.errors
        assert _status(rt) == TaskStatus.VERIFIED

    def test_06_task_really_becomes_completed_in_db(self, rt):
        """⑥ 🔴 **DB 中真实 Task 状态 = COMPLETED**（走完整链）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=True)
        result = _complete(rt, actor=ACCEPTOR)

        assert result.ok is True, result.errors
        # 从**数据库**重新读，而不是信任内存里的对象
        assert _status(rt) == TaskStatus.COMPLETED

        # 直接从 SQLite 读一行 —— 这是「数据库里真的是这个状态」的硬证据，
        # 而不是相信内存里的对象。
        connection = sqlite3.connect(str(rt.economy.db.path))
        try:
            row = connection.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (TASK,)
            ).fetchone()
        finally:
            connection.close()
        assert row is not None, "任务必须存在于 tasks 表"
        assert row[0] == TaskStatus.COMPLETED, row
        # 用 TaskStore 重新 get 一次（也是从库里读）
        assert rt.tasks.get(TASK).status == TaskStatus.COMPLETED
        assert rt.tasks.get(TASK).is_terminal is True

    def test_07_runner_exits_completed_not_wall_limit(self, rt):
        """⑦ Production Runner 因 COMPLETED 正常退出（不靠 Wall Limit）。"""
        from experiments.heterogeneous_production import production as P

        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)

        s_log = _session_log()
        log = {}
        # 先只到「声称完成」这一步：SUBMIT 还没发
        outcome = P._try_complete_task(rt, rt.workspace_gateway, log, s_log)
        # 没有手稿 artifact → 被挡住（而不是假装完成）
        assert outcome != "COMPLETED"

        # 直接补上 SUBMIT + VERIFY + COMPLETE（走协议）
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=True)
        outcome = P._try_complete_task(rt, rt.workspace_gateway, log, s_log)

        assert outcome == "COMPLETED"
        assert _status(rt) == TaskStatus.COMPLETED
        # runner 记录的完成事件确实是三步协议
        events = [e["event"] for e in log.get("completion_events", [])]
        assert "COMPLETE" in events
        assert all(e["ok"] for e in log.get("completion_events", []))


# ============================================================
# Rework（8–13）
# ============================================================
class TestRework:
    def test_08_acceptance_fail(self, rt):
        """⑧ Acceptance FAIL。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())

        result = _verify(rt, actor=ACCEPTOR, passed=False)

        assert result.ok is True, result.errors

    def test_09_task_returns_to_in_progress(self, rt):
        """⑨ Task 回到 IN_PROGRESS（打回重做，不是判死）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=False)

        assert _status(rt) == TaskStatus.IN_PROGRESS
        assert rt.tasks.get(TASK).is_terminal is False

    def test_10_feedback_is_recorded(self, rt):
        """⑩ Feedback 被记录（可查询，不只在消息里）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=False, feedback="结尾太仓促")

        latest = rt.tasks.latest_verification(TASK)
        assert latest is not None
        assert latest.verification.passed is False
        assert "结尾太仓促" in latest.verification.feedback

    def test_11_agent_can_submit_again(self, rt):
        """⑪ Agent 可以再次 SUBMIT。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=False)

        again = _submit(rt, proposal=_proposal(status=CriteriaStatus.MET))

        assert again.ok is True, again.errors
        assert _status(rt) == TaskStatus.SUBMITTED
        assert rt.tasks.attempt_count(TASK) == 2      # 轮次是数出来的

    def test_12_second_verify_passes(self, rt):
        """⑫ 第二次 VERIFY PASS。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=False)
        _submit(rt, proposal=_proposal())

        result = _verify(rt, actor=ACCEPTOR, passed=True)

        assert result.ok is True, result.errors
        assert _status(rt) == TaskStatus.VERIFIED

    def test_13_finally_completed(self, rt):
        """⑬ 最终 COMPLETED。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=False)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=True)
        assert _complete(rt, actor=ACCEPTOR).ok is True

        assert _status(rt) == TaskStatus.COMPLETED


# ============================================================
# Completion bug 回归（14–17）
# ============================================================
class TestCompletionBugRegression:
    """
    🔴 修的是这条链：

        turn["claims_done"]=True → session aggregation →
        Completion detection

    以前 `s_log["agents_claim_done"]` 初始化后**再没人写过**，
    所以 COMPLETED 分支不可达。
    """

    def _prod(self):
        from experiments.heterogeneous_production import production as P
        return P

    def test_14_turn_claim_exists(self):
        """⑭ `turn["claims_done"]` 仍然被赋值。"""
        source = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
                  / "production.py").read_text(encoding="utf-8")
        assert 'turn["claims_done"] = True' in source

    def test_15_aggregation_writes_session_flag(self):
        """⑮ 正确进入 session aggregation（**有写入点**）。"""
        source = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
                  / "production.py").read_text(encoding="utf-8")
        assert 's_log["agents_claim_done"] = bool(claims)' in source, (
            "session 级聚合必须真的写 s_log —— 这正是原来的 bug")
        assert 'claims = [t for t in s_log["turns"] if t.get("claims_done")]' in source

    def test_16_completion_branch_is_reachable(self, rt):
        """⑯ Completion branch 可达（真的走到 COMPLETED）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())
        _verify(rt, actor=ACCEPTOR, passed=True)

        P = self._prod()
        outcome = P._try_complete_task(
            rt, rt.workspace_gateway, {},
            _session_log())

        assert outcome == "COMPLETED"

    def test_17_no_longer_relies_on_wall_limit(self):
        """⑰ 不再错误运行到 Wall Limit —— COMPLETED 分支与退出绑定。"""
        source = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
                  / "production.py").read_text(encoding="utf-8")
        assert 'exit_reason = "COMPLETED"' in source
        # 旧的「布尔 + 字数」判据必须已经换成真实协议
        # ⚠️ 只查**可执行代码**，不查注释 ——
        #    我的说明里正好引用了那行旧代码，纯文本匹配会把自己绊倒
        #    （AGENTS.md §12 的「子串匹配误报」）。
        code_only = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#"))
        assert 'if s_log.get("agents_claim_done") and chars_now >= 9000:' not in code_only
        assert "_try_complete_task(rt, gw, log, s_log)" in source


# ============================================================
# Negative / 架构守卫（18–23）
# ============================================================
class TestNegativeAndGuards:
    def test_18_no_dod_means_no_completion(self, rt):
        """⑱ Agent 没有 DoD 不得直接 Completed（提交被拒）。"""
        _create_task(rt)
        # 只指派验收责任，**不建 DoD**
        change = rt.alignment.propose(
            TASK, proposed_by=PRODUCER, change_type="ASSIGN",
            new_value={"agent_id": ACCEPTOR,
                       "responsibility": ACCEPTANCE_RESPONSIBILITY},
            expected_revision=rt.alignment.require_current(TASK).revision
            if rt.alignment.current(TASK) else 1,
            reason="指派验收责任人") if rt.alignment.current(TASK) else None
        if change is not None:
            rt.alignment.accept(TASK, change.change_id, decided_by=PRODUCER,
                                reason="x")
        _start(rt)

        result = _submit(rt, proposal=_proposal())

        assert result.ok is False
        assert any("Definition of Done" in e for e in result.errors)
        assert _status(rt) == TaskStatus.IN_PROGRESS

    def test_19_producer_self_verify_must_fail(self, rt):
        """⑲ 生产者自验必须失败（无 assignment 时也一样）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt, proposal=_proposal())

        result = _verify(rt, actor=PRODUCER)

        assert result.ok is False
        assert _status(rt) == TaskStatus.SUBMITTED

    def test_20_missing_acceptance_responsibility_blocks_completion(self, rt):
        """⑳ Acceptance Responsibility 缺失不得 Complete。"""
        _create_task(rt)
        rt.alignment.create_context(TASK, by=PRODUCER, objective="x",
                                    acceptance_criteria=list(DOD))   # 只有 DoD
        _start(rt)
        _submit(rt, proposal=_proposal())
        # 就算有人硬发 VERIFY，也会因为不是参与方/没有角色被拒
        verify = _verify(rt, actor=OTHER)
        assert verify.ok is False, "OTHER 不是验收责任人，不该能验收"
        assert _status(rt) == TaskStatus.SUBMITTED

        blockers = rt.task_execution.completion_blockers(rt.tasks.get(TASK))
        assert any("Acceptance Responsibility" in b for b in blockers)

    def test_20b_missing_proposal_blocks_completion(self, rt):
        """⑳b 没有 Completion Proposal 也要被挡（它是六条之一）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)
        _submit(rt)                      # 不带 proposal
        blockers = rt.task_execution.completion_blockers(rt.tasks.get(TASK))
        assert any("Completion Proposal" in b for b in blockers)

    def test_21_complete_cannot_bypass_validate(self, rt):
        """㉑ COMPLETE 不得绕过 validate()（状态不对就拒）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)                       # IN_PROGRESS，还不是 VERIFIED

        result = _complete(rt, actor=ACCEPTOR)

        assert result.ok is False
        assert any("VERIFIED" in e or "状态" in e for e in result.errors)
        assert _status(rt) == TaskStatus.IN_PROGRESS

    def test_21b_verified_requires_submitted(self, rt):
        """㉑b VERIFY 不得跳过 SUBMIT。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)

        result = _verify(rt, actor=ACCEPTOR, passed=True)

        assert result.ok is False
        assert _status(rt) == TaskStatus.IN_PROGRESS

    def test_22_runner_does_not_mutate_task_status_directly(self):
        """
        ㉒ Runner 不得直接修改 Task 状态。

        🔒 用 **AST** 扫，不用字符串匹配 ——
           否则注释里那句「不要直接改状态」会把自己绊倒。
        """
        tree = ast.parse((PROJECT_ROOT / "experiments"
                          / "heterogeneous_production"
                          / "production.py").read_text(encoding="utf-8"))

        # ① 不许给 status / _status 赋值
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                name = ""
                if isinstance(target, ast.Attribute):
                    name = target.attr
                elif isinstance(target, ast.Name):
                    name = target.id
                assert name not in ("status", "_status"), (
                    f"production.py 直接改了 Task 状态（第 {node.lineno} 行）—— "
                    f"必须走消息协议")

        # ② 不许直接调领域状态机方法
        banned = {"transition_to", "claim_producer", "submit", "verify",
                  "complete"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                receiver = ast.unparse(node.func.value)
                if attr in banned and receiver == "task":
                    raise AssertionError(
                        f"production.py 第 {node.lineno} 行直接调了 "
                        f"task.{attr}() —— 必须发消息走执行服务")

    def test_23_no_workflow_engine_or_coordinator(self):
        """㉓ 不允许新增 WorkflowEngine / Coordinator。"""
        banned = ("WorkflowEngine", "Coordinator", "TaskManager",
                  "Scheduler", "Heartbeat")
        offenders = []
        for folder in ("core", "services", "communication", "agents"):
            for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
                if "__pycache__" in str(path):
                    continue
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name in banned:
                        offenders.append(f"{path.name}::{node.name}")
        assert not offenders, f"出现了被禁止的组件：{offenders}"

    def test_24_no_new_task_status(self):
        """㉔（附加）没有新增 Task 状态 —— 状态机保持 12 个。"""
        from core.task import TaskStatus

        all_states = [v for k, v in vars(TaskStatus).items()
                      if not k.startswith("_") and isinstance(v, str)]
        assert len(all_states) == 12, all_states

    def test_25_no_new_database_table(self):
        """㉔b（附加）没有为 Completion 新增表。"""
        from core.task_execution import CompletionProposal  # noqa: F401

        models = (PROJECT_ROOT / "storage" / "models.py").read_text(
            encoding="utf-8")
        for bad in ("completion_proposals", "acceptances", "task_acceptances",
                    "definitions_of_done"):
            assert f'"{bad}"' not in models, f"不应该有 {bad} 表"


# ============================================================
# §11 最重要的真实行为测试：完整链
# ============================================================
class TestFullRealBehaviourChain:
    """
    🔴 规格 §11：至少一条完整测试，**真正走现有生产协议**。

        Agent 认知（DoD 可见）
              ↓
        Completion Proposal
              ↓  SUBMIT
        Acceptance Responsibility
              ↓  VERIFY
              ↓  COMPLETE
        Task.status == COMPLETED
              ↓
        Production Runner 正常退出

    不调用真实 LLM API。
    """

    def test_full_chain_proposal_submit_verify_complete(self, rt):
        from experiments.heterogeneous_production import production as P

        # ---- ① Agent 自主结构化 DoD（认知输入的一部分）----
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)

        # DoD 对 Agent **可见**（渲染进它的 context / prompt）
        facts = P._completion_facts(rt)
        assert "Definition of Done" in facts
        for item in DOD:
            assert item in facts
        assert ACCEPTOR in facts, "验收责任人必须出现在 Agent 能看到的事实里"
        assert "Task 当前状态" in facts

        # ---- ② 进入生产（走 START 协议，不直接 task.start()）----
        assert _start(rt).ok is True
        task = rt.tasks.get(TASK)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.contractor == PRODUCER, "START 时发送者应成为承接方"

        # ---- ③ Completion Proposal → SUBMIT ----
        proposal = _proposal()
        assert _submit(rt, proposal=proposal).ok is True
        assert _status(rt) == TaskStatus.SUBMITTED
        assert rt.task_execution.latest_completion_proposal(TASK) is not None

        # ---- ④ Acceptance → VERIFY(passed=True) ----
        assert _verify(rt, actor=ACCEPTOR, passed=True).ok is True
        assert _status(rt) == TaskStatus.VERIFIED

        # ---- ⑤ COMPLETE ----
        assert _complete(rt, actor=ACCEPTOR).ok is True
        assert _status(rt) == TaskStatus.COMPLETED

        # ---- ⑥ Runner 因此正常退出（不是 Wall Limit）----
        outcome = P._try_complete_task(
            rt, rt.workspace_gateway, {},
            _session_log())
        assert outcome == "COMPLETED"

        # ---- ⑦ 全链每一步都在 task_executions 里留了痕 ----
        events = [r.event_type for r in rt.tasks.executions_for(TASK)]
        assert events == ["START", "SUBMIT", "VERIFY", "COMPLETE"], events
        assert rt.tasks.get(TASK).version >= 4, "每次状态变化都该推进 version"


# ============================================================
# 边界审计（spec: 边界审计和收口）
# ============================================================
class TestCreatedToInProgressBoundary:
    """
    `CREATED → IN_PROGRESS` 的**适用范围**边界。

    这条边是为 **Root Task** 开的（Human Demand → 建任务 → 直接生产）。
    它**不能**变成「任何 CREATED 任务 + 是参与方」都能跳过商业流程。

    🔒 三个概念不能混：
           Participant  ≠  Contractor  ≠  Commerce Acceptor
    """

    def _root_task(self, rt, *, task_id=None, **kw):
        """一个**没有商业流程**的任务（= Root Task 的形状）。"""
        tid = task_id or TASK
        task = Task.create(creator="human_demand", description="写小说",
                           task_id=tid, participants=list(kw.pop(
                               "participants", (PRODUCER, ACCEPTOR, OTHER))),
                           **kw)
        rt.tasks.save(task)
        return rt.tasks.get(tid)

    def test_root_task_can_start_directly(self, rt):
        """① Root Task（无 budget / 无 offer）可以直接开工。"""
        self._root_task(rt)
        result = _start(rt)
        assert result.ok is True, result.errors
        task = rt.tasks.get(TASK)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.contractor == PRODUCER

    def test_commerce_task_with_budget_cannot_start_directly(self, rt):
        """② 🔴 声明了预算的任务**不得**直接从 CREATED 开工。"""
        from core.money import Money

        self._root_task(rt, budget=Money(100))
        result = _start(rt)

        assert result.ok is False
        assert any("OFFERED" in e and "ACCEPTED" in e for e in result.errors)
        assert any("参与方身份" in e for e in result.errors), (
            "错误信息必须点明 Participant ≠ Commerce Acceptor")
        assert _status(rt) == TaskStatus.CREATED, "状态必须没动"
        assert rt.tasks.get(TASK).contractor is None

    def test_looks_like_commerce_detects_each_commerce_fact(self, rt):
        """
        ③ 三个商业事实**每一个**都能被判出来。

        ⚠️ 不能靠 `task.offer()` 来构造 —— 它只改状态，
           **不**往 `task.offers` 里加东西（offers 由议价流程写）。
           所以这里直接构造那三种事实。

        另外：一旦状态到了 OFFERED，`ALLOWED_STATES[START]` 本来
        就不含它 —— 状态机已经挡住了。这条测的是**判定函数**，
        它是 `CREATED` 路径上额外的那道闸。
        """
        from core.money import Money
        from core.task import TRANSITIONS, Offer, OfferType

        assert TaskStatus.IN_PROGRESS in TRANSITIONS[TaskStatus.CREATED]

        # ① budget
        with_budget = Task.create(creator="c", task_id="t-budget",
                                  budget=Money(100))
        assert rt.task_execution.looks_like_commerce(with_budget) is not None

        # ② agreed_price
        priced = Task.create(creator="c", task_id="t-price")
        priced.agreed_price = Money(25)
        assert rt.task_execution.looks_like_commerce(priced) is not None

        # ③ offers
        offered = Task.create(creator="c", task_id="t-offers")
        offered.offers.append(Offer(  # type: ignore[attr-defined]
            offer_type=OfferType.TASK_OFFER, amount=Money(10),
            from_agent="a"))
        assert rt.task_execution.looks_like_commerce(offered) is not None

        # 反面：三者都没有 → 不是商业任务
        bare = Task.create(creator="c", task_id="t-bare")
        assert rt.task_execution.looks_like_commerce(bare) is None

    def test_looks_like_commerce_returns_none_for_root_task(self, rt):
        """④ 反面的反面：Root Task 的形状**不**被判成商业任务。"""
        self._root_task(rt)
        assert rt.task_execution.looks_like_commerce(rt.tasks.get(TASK)) is None

    def test_existing_contractor_cannot_be_replaced(self, rt):
        """⑤ 已有 contractor 的 CREATED 任务：只有他自己能开工。"""
        task = self._root_task(rt)
        task.claim_producer(OTHER)        # 已经有人承接
        rt.tasks.save(task)

        other = _start(rt, actor=PRODUCER)      # 别人想开工
        assert other.ok is False
        assert any("已经由" in e for e in other.errors)

        mine = _start(rt, actor=OTHER)          # 承接方自己可以
        assert mine.ok is True, mine.errors
        assert _status(rt) == TaskStatus.IN_PROGRESS

    def test_non_participant_cannot_start(self, rt):
        """⑥ 非参与方不得认领别人的任务。"""
        self._root_task(rt, participants=(PRODUCER,))
        result = _start(rt, actor=OTHER)

        assert result.ok is False
        assert any("不是任务" in e for e in result.errors)
        assert _status(rt) == TaskStatus.CREATED

    def test_root_task_creation_has_no_lineage_field(self):
        """
        ⑦ 🔴 **记录 V1 的结构性限制**：没有 Root / Commerce 的结构化区分。

        这条测试**不是**在要求新增字段，而是在**钉住当前事实**：

            · `Task.create()` 没有 `parent_task_id` / `origin` / `is_root`
            · `Task` 上没有 lineage 字段
            · Task Lineage 是 FORBIDDEN NOW（AGENTS.md §7）

        ⇒ 「Root/Commerce differentiation」在 V1 = **UNAVAILABLE**。

        所以收窄只能靠**行为事实**（budget / offers / agreed_price），
        而不是靠「这个任务是不是 Root Task」。
        如果哪天加了 lineage 字段，这条测试会红 —— 那时应该
        把 `looks_like_commerce` 换成读那个字段。
        """
        import inspect

        signature = inspect.signature(Task.create)
        for banned in ("parent_task_id", "root_task_id", "origin", "is_root",
                       "created_by_human", "skip_commerce"):
            assert banned not in signature.parameters, (
                f"Task.create 出现了 {banned} —— "
                f"如果是有意新增 lineage，请同步更新 looks_like_commerce "
                f"与 docs/architecture/TASK_ORIGIN_LINEAGE.md")

        fields = set(Task.create(creator="c", task_id="t-probe").to_dict())
        for banned in ("parent_task_id", "root_task_id", "origin", "is_root"):
            assert banned not in fields, f"Task 出现了 {banned} 字段"


class TestStageCompletionBoundary:
    """
    `Task COMPLETED → Stage COMPLETED` 的**触发范围**。

    【Task ↔ Stage 的真实关系：1 Task : N Stages】

        `Stage.task_id` 说明 Stage 属于哪个 Task；
        `StageStore.list_stages(task_id)` 返回**全部**。
        ⇒ 任务完成**不**蕴含它的所有 Stage 都该完成。
    """

    def _stage_store(self, rt):
        from stage import StageStore
        return StageStore(rt.economy.db)

    def test_one_task_can_own_multiple_stages(self, rt):
        """① 结构性事实：一个 Task 可以有多个 Stage（不是 1:1）。"""
        from stage import STATUS_ACTIVE

        self._root_task(rt) if hasattr(self, "_root_task") else _create_task(rt)
        st = self._stage_store(rt)
        st.create_stage(TASK, "阶段一", deliverables=[{"name": "a"}],
                        status=STATUS_ACTIVE, stage_id="stage_probe_1")
        st.create_stage(TASK, "阶段二", deliverables=[{"name": "b"}],
                        status=STATUS_ACTIVE, stage_id="stage_probe_2")

        stages = st.list_stages(TASK)
        assert len(stages) == 2
        assert {s.task_id for s in stages} == {TASK}

    def test_task_completed_closes_only_the_runners_own_stage(self, rt):
        """
        ② 🔴 Task COMPLETED **只**关 Runner 自己驱动的那一个 Stage。

        另一个属于同一个 Task 的 Stage 必须**保持 ACTIVE** ——
        否则「任务完成」就会误伤尚未完成的阶段。
        """
        from experiments.heterogeneous_production import production as P
        from stage import STATUS_ACTIVE

        _create_task(rt)
        st = self._stage_store(rt)
        st.create_stage(TASK, "root", deliverables=[{"name": "m"}],
                        status=STATUS_ACTIVE, stage_id=P.ROOT_STAGE_ID)
        st.create_stage(TASK, "另一个阶段", deliverables=[{"name": "x"}],
                        status=STATUS_ACTIVE, stage_id="stage_other")

        log = {}
        P._close_stage(rt, log)

        assert log["stage_completed"] == P.ROOT_STAGE_ID
        by_id = {s.stage_id: s for s in st.list_stages(TASK)}
        assert by_id[P.ROOT_STAGE_ID].status == "COMPLETED"
        assert by_id["stage_other"].status == STATUS_ACTIVE, (
            "别的 Stage 不该因为任务完成就被关掉")

    def test_close_stage_refuses_a_foreign_stage(self, rt):
        """③ `_close_stage` 绝不关**别的任务**的 Stage。"""
        from experiments.heterogeneous_production import production as P
        from stage import STATUS_ACTIVE

        _create_task(rt)
        other_task = Task.create(creator="someone", description="别的任务",
                                 task_id="task_other_x", participants=["someone"])
        rt.tasks.save(other_task)

        st = self._stage_store(rt)
        # 别的任务的 stage **故意用了** ROOT_STAGE_ID 这个名字
        st.create_stage("task_other_x", "别人的阶段",
                        deliverables=[{"name": "z"}],
                        status=STATUS_ACTIVE, stage_id=P.ROOT_STAGE_ID)

        log = {}
        P._close_stage(rt, log)

        # 🔒 真实的保证是：**别的任务的 Stage 一动不动**。
        #    `_close_stage` 先按 task_id 查（`list_stages(TASK_ID)`），
        #    所以它根本看不到别的任务那一个 —— 于是如实说「没找到」，
        #    而不是拿同名的 Stage 去关。
        assert log["stage_completed"] is None, "不能把别的任务的 Stage 记成已完成"
        assert "没有找到" in log["stage_note"]
        still = {s.stage_id: s for s in st.list_stages("task_other_x")}
        assert still[P.ROOT_STAGE_ID].status == STATUS_ACTIVE, (
            "别的任务的 Stage 绝不能被关掉")

    def test_ownership_guard_is_defensive(self, rt):
        """
        ③b 归属校验是**防御性**的。

        `_close_stage` 用 `list_stages(TASK_ID)` 查，天然只会拿到
        本任务的 Stage，所以 `stage.task_id != TASK_ID` 在当前实现下
        **不可达**。它留着是为了防止以后有人把查询改成「按 stage_id
        全局查」时忘记校验归属。

        🔒 这里不断言不可达的分支，只断言「查询是按 task 作用域做的」——
           那才是真正的安全机制。
        """
        import inspect
        from experiments.heterogeneous_production import production as P

        source = inspect.getsource(P._close_stage)
        assert "st.list_stages(TASK_ID)" in source, (
            "必须按 task 作用域查询，否则会误关别的任务的 Stage")

    def test_close_stage_is_idempotent(self, rt):
        """④ 重复调用不会出错（第二次不再改状态）。"""
        from experiments.heterogeneous_production import production as P
        from stage import STATUS_ACTIVE

        _create_task(rt)
        st = self._stage_store(rt)
        st.create_stage(TASK, "root", deliverables=[{"name": "m"}],
                        status=STATUS_ACTIVE, stage_id=P.ROOT_STAGE_ID)

        first, second = {}, {}
        P._close_stage(rt, first)
        P._close_stage(rt, second)

        assert first["stage_completed"] == second["stage_completed"] == P.ROOT_STAGE_ID
        assert {s.stage_id: s for s in st.list_stages(TASK)}[
            P.ROOT_STAGE_ID].status == "COMPLETED"

    def test_missing_stage_is_reported_not_faked(self, rt):
        """⑤ 找不到 Stage 时如实记录，不假装完成。"""
        from experiments.heterogeneous_production import production as P

        _create_task(rt)
        log = {}
        P._close_stage(rt, log)

        assert log["stage_completed"] is None
        assert "没有找到" in log["stage_note"]


class TestProposalIdentityAuthority:
    """
    🔴 `CompletionProposal` 的身份必须来自 **message envelope**，
    不能被 `payload` 自述覆盖。

    规格 §6.1 的场景：
        真实消息 task_id = REAL_TASK / from_agent = REAL_AGENT
        但 payload 恶意写成 FAKE_TASK / FAKE_AGENT
    最终必须用 **REAL**。
    """

    FAKE_TASK = "FAKE_TASK_9999"
    FAKE_AGENT = "FAKE_AGENT_9999"

    def _forged_payload(self):
        return {
            "task_id": TASK,
            "result": {"result_ref": "manuscript/novel",
                       "result_type": "document"},
            COMPLETION_PROPOSAL_KEY: {
                # 恶意/错误的身份
                "task_id": self.FAKE_TASK,
                "proposed_by": self.FAKE_AGENT,
                "proposal_id": "cp_forged",
                "criteria_status": CriteriaStatus.MET,
                "evidence": [{"result_ref": "manuscript/novel",
                              "result_type": "document"}],
            },
        }

    def test_payload_identity_is_overridden_by_message(self, rt):
        """① 解析出来的 Proposal 用**消息**的身份，不是 payload 的。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)

        message = _msg(PRODUCER, ACCEPTOR, MessageType.SUBMIT,
                       self._forged_payload())
        intent = rt.task_service.mapper.parse_submit(message)

        assert intent.proposal is not None, intent.errors
        assert intent.proposal.task_id == TASK, "task_id 必须以消息为准"
        assert intent.proposal.proposed_by == PRODUCER, "proposed_by 必须以消息为准"
        assert intent.proposal.task_id != self.FAKE_TASK
        assert intent.proposal.proposed_by != self.FAKE_AGENT

    def test_forged_proposal_is_persisted_with_real_identity(self, rt):
        """② 落库的也是**真实**身份（FAKE 不会进数据库）。"""
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)

        result = rt.task_service.handle_message(
            _msg(PRODUCER, ACCEPTOR, MessageType.SUBMIT,
                 self._forged_payload()))

        assert result.ok is True, result.errors
        read_back = rt.task_execution.latest_completion_proposal(TASK)
        assert read_back.task_id == TASK
        assert read_back.proposed_by == PRODUCER

        # 硬证据：整条 SUBMIT 记录的 JSON 里不许出现 FAKE 身份
        record = rt.tasks.latest_submission(TASK)
        blob = str(dict(record.result.metadata))
        assert self.FAKE_TASK not in blob
        assert self.FAKE_AGENT not in blob

    def test_forged_identity_cannot_authorize_verification(self, rt):
        """
        ③ 伪造身份**不能**换来验收权限。

        payload 写 FAKE_AGENT，而这条消息真的来自 OTHER（不是承接方）——
        角色校验用的是**消息的** from_agent，所以必须被拒。
        """
        _create_task(rt)
        _set_up_dod_and_acceptor(rt)
        _start(rt)                                    # PRODUCER 成为承接方

        forged = self._forged_payload()
        forged[COMPLETION_PROPOSAL_KEY]["proposed_by"] = PRODUCER  # 想冒充承接方
        result = rt.task_service.handle_message(
            _msg(OTHER, ACCEPTOR, MessageType.SUBMIT, forged))     # 但真的来自 OTHER

        assert result.ok is False
        assert _status(rt) == TaskStatus.IN_PROGRESS
        assert rt.tasks.latest_submission(TASK) is None, "被拒的 SUBMIT 不该落库"


class TestNoSecondCompletionCriterion:
    """
    🔒 全仓守卫：**只有一套**完成判据。

        Agent claim  ≠  Completion fact

    唯一允许把 Task 判为完成的地方，是**读回持久化的 `task.status`**
    （`production.py::_try_complete_task` 的最后一段）。
    不许任何地方再从「Agent 自称完成」或「字数够了」推出完成。
    """

    def _sources(self):
        for folder in ("core", "services", "communication", "agents", "llm",
                       "storage"):
            for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
                if "__pycache__" not in str(path):
                    yield path, path.read_text(encoding="utf-8")
        runner = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
                  / "production.py")
        yield runner, runner.read_text(encoding="utf-8")

    @staticmethod
    def _code_only(source: str) -> str:
        """去掉注释 —— 否则说明文字里的旧代码会造成自引用假红。"""
        return "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))

    def test_no_module_infers_completion_from_a_claim(self):
        """① 没有任何模块把「自称完成」当成完成。"""
        offenders = []
        for path, source in self._sources():
            code = self._code_only(source)
            for pattern in ('claims_done:', 'agents_claim_done:',
                            'if claims_done', 'if agents_claim_done'):
                if pattern in code:
                    offenders.append(f"{path.name} :: {pattern}")
        assert not offenders, f"出现了「自称完成 → 完成」的推断：{offenders}"

    def test_no_module_infers_completion_from_size(self):
        """② 没有任何模块用「字数够了」当完成判据。"""
        offenders = []
        for path, source in self._sources():
            code = self._code_only(source)
            if "chars_now >= 9000" in code or "len(content) > 10000" in code:
                offenders.append(path.name)
        assert not offenders, f"出现了按字数判完成的逻辑：{offenders}"

    def test_completion_is_authoritative_only_in_one_place(self):
        """
        ③ 「Task 真的完成了」只在**一个**地方被断定。

        `_try_complete_task` 里那一次 `task.status == TaskStatus.COMPLETED`。
        `_completion_facts` 里的 `!=` 只是**展示**（决定要不要显示还差什么），
        不断定完成 —— 所以只允许出现一次 `==` 形式的判定。
        """
        runner = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
                  / "production.py").read_text(encoding="utf-8")
        code = self._code_only(runner)
        occurrences = code.count("task.status == TaskStatus.COMPLETED")
        assert occurrences == 1, (
            f"应该有且只有一处「读完状态后断定完成」，实际 {occurrences} 处")

    def test_runner_reads_status_back_from_store(self):
        """④ 断定完成之前必须**从库里重读**，不能只信内存对象。"""
        runner = (PROJECT_ROOT / "experiments" / "heterogeneous_production"
                  / "production.py").read_text(encoding="utf-8")
        code = self._code_only(runner)
        assert "task = rt.tasks.get(TASK_ID)" in code, (
            "必须从 TaskStore 重读状态作为证据")
