"""
读共同认知（`shared_context_read`）—— 功能 + 边界 + 反向测试
==============================================================

【🔴 这个动作补的是哪一个缺口（用户 2026-10-10 指出）】

    「Task 的共同认知应该要有 Action 给 Agent 查的。」

    在此之前，Shared Context **只有写通道**（`assign_acceptance`）：

        Agent 能写下 objective / constraints / acceptance_criteria /
        decisions / assumptions / facts / assignments ——
        而它自己**一个字都读不到**。

    ⇒ 「共同认知」对唯一需要它的人不可见。

【本文件钉住的四件事】

    ① 读得到，而且读到的是**真的**（空就是空，不编内容）
    ② 不是这条 Task 的人**读不到**（fail-closed）
    ③ 它是**纯读** —— 不改任何状态、不产生新 revision
    ④ **参数一概不读** —— 读哪条 Task 由组合根绑定，
       以什么身份读由 `intent.agent_id` 决定，两者 Agent 都指定不了
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.intent import ActionIntent                          # noqa: E402
from actions.shared_context_read import (                        # noqa: E402
    ACTION_SHARED_CONTEXT_READ, SharedContextReadExecutor,
)
from core.task import Task                                       # noqa: E402
from services.context_ops import read_shared_context             # noqa: E402

AGENTS = ["agent_a", "agent_b", "agent_c", "agent_d", "agent_e"]
INSIDERS = ["agent_a", "agent_b"]
OUTSIDERS = ["agent_c", "agent_d", "agent_e"]


@pytest.fixture()
def rt(tmp_path):
    from main import build_runtime

    r = build_runtime(db_path=str(tmp_path / "scr.db"),
                      agent_ids=list(AGENTS))
    # 🔴 这条 Task **只有** a/b —— c/d/e 不是参与者
    r.create_task(Task.create(task_id="T-1", creator="agent_a",
                              description="d", participants=list(INSIDERS)))
    yield r
    r.close()


def _people(rt):
    """组合根提供的那种检查器（fail-closed）。"""
    def _is(tid, aid):
        t = rt.tasks.get(str(tid))
        return bool(t and t.is_participant(str(aid)))
    return _is


def _read(rt, by, **kw):
    return read_shared_context(rt.alignment, "T-1", by=by,
                               is_participant=_people(rt), **kw)


def _assign(rt, to="agent_b"):
    """让容器里真的有东西 —— 走**真实**的对齐协议，不直接写库。"""
    ch = rt.alignment.propose(
        "T-1", proposed_by="agent_a", change_type="ASSIGN",
        new_value={"agent_id": to, "responsibility": "acceptance"},
        expected_revision=1, reason="指派验收")
    rt.alignment.accept("T-1", ch.change_id, decided_by="agent_a",
                        reason="ASSIGN 属于声明类，规则允许自批")


# ============================================================
# 一、读得到，而且读到的是真的
# ============================================================
class TestItActuallyReads:
    def test_a_participant_can_read(self, rt):
        got = _read(rt, "agent_a")

        assert got["ok"] is True
        assert got["task_id"] == "T-1"
        assert got["revision"] == 1

    def test_it_returns_the_whole_snapshot_not_a_summary(self, rt):
        """共同认知的每一块都要在 —— 少一块就等于「它以为没有」。"""
        got = _read(rt, "agent_a")

        assert set(got["context"]) >= {
            "objective", "constraints", "acceptance_criteria",
            "decisions", "assumptions", "open_questions",
            "assignments", "facts", "terminology", "artifact_refs",
        }

    def test_domain_state_is_a_reference_to_task_core(self, rt):
        """
        §6.2：Shared Context **引用** Domain State，不复制。

        所以 `domain_state` 必须现场读、并标明来源。
        """
        got = _read(rt, "agent_a")

        assert got["domain_state_source"] == "task_core"
        assert got["domain_state"]["status"] == rt.tasks.get("T-1").status
        # 🔴 共同认知里**没有** status —— 它不是第二套 Task Core
        assert "status" not in got["context"]

    def test_the_domain_state_follows_task_core(self, rt):
        """Task Core 变了，下一次读立刻反映（证明不是存下来的副本）。"""
        before = _read(rt, "agent_a")["domain_state"]["status"]
        t = rt.tasks.get("T-1")
        t.offer()
        rt.tasks.save(t)

        after = _read(rt, "agent_a")["domain_state"]["status"]

        assert before == "CREATED" and after == "OFFERED", (
            f"domain_state 没有跟着 Task Core 走：{before} → {after}")

    def test_an_empty_container_reads_as_empty(self, rt):
        """
        🔴 **空就是空** —— 不许为了「看起来有用」编一个 objective 出来。

        （本项目记录过的 bug 类：接口成功掩盖语义空转。）
        """
        got = _read(rt, "agent_a")
        c = got["context"]

        assert c["objective"] == ""
        assert c["acceptance_criteria"] == []
        assert c["assignments"] == []
        assert got["recent_changes"] == []

    def test_what_was_assigned_shows_up(self, rt):
        _assign(rt, to="agent_b")

        got = _read(rt, "agent_a")
        assigns = got["context"]["assignments"]

        assert got["revision"] == 2
        assert [a["agent_id"] for a in assigns] == ["agent_b"]
        assert [a["responsibility"] for a in assigns] == ["acceptance"]
        # 最近的变更也带上了（谁提的、批了没、变成第几版）
        assert got["recent_changes"][-1]["change_type"] == "ASSIGN"
        assert got["recent_changes"][-1]["result_revision"] == 2


# ============================================================
# 二、不是这条 Task 的人读不到（fail-closed）
# ============================================================
class TestOnlyParticipantsCanRead:
    @pytest.mark.parametrize("outsider", OUTSIDERS)
    def test_a_non_participant_is_refused(self, rt, outsider):
        got = _read(rt, outsider)

        assert got["ok"] is False
        assert got["code"] == "not_participant"
        assert "context" not in got, "被拒时不许夹带内容"

    def test_the_creator_always_counts_as_a_participant(self, rt):
        """`is_participant` 含 creator —— 所以 creator 一定读得到。"""
        assert _read(rt, "agent_a")["ok"] is True

    def test_a_missing_checker_is_refused(self, rt):
        """
        🔴 没有检查器 ⇒ **拒绝**，不是放行。

        不检查就能读到别的 Task 的内部讨论
        （objective / decisions / assumptions）—— 那是 §16 的边界。
        """
        got = read_shared_context(rt.alignment, "T-1", by="agent_a",
                                  is_participant=None)

        assert got["ok"] is False
        assert got["code"] == "no_participant_checker"

    def test_an_empty_actor_is_refused(self, rt):
        got = _read(rt, "")

        assert got["ok"] is False
        assert got["code"] == "no_actor"

    def test_an_unknown_task_does_not_leak(self, rt):
        """查不到的 Task ⇒ fail-closed（不是抛异常，也不是放行）。"""
        got = read_shared_context(rt.alignment, "T-NOT-THERE", by="agent_a",
                                  is_participant=_people(rt))

        assert got["ok"] is False
        assert got["code"] == "not_participant"


# ============================================================
# 三、它是**纯读**
# ============================================================
class TestItIsAPureRead:
    def test_it_does_not_advance_the_revision(self, rt):
        _assign(rt)                       # revision 1 → 2
        before = rt.alignment.current("T-1").revision

        for _ in range(5):
            _read(rt, "agent_a")

        assert rt.alignment.current("T-1").revision == before, (
            "读操作改变了 revision —— 它不是纯读")

    def test_it_does_not_create_any_change(self, rt):
        before = len(rt.alignment.changes("T-1"))

        _read(rt, "agent_a")
        _read(rt, "agent_b")

        assert len(rt.alignment.changes("T-1")) == before, (
            "读操作往待裁决队列里塞了东西")

    def test_a_refused_read_leaves_no_trace(self, rt):
        before = (len(rt.alignment.changes("T-1")),
                  rt.alignment.current("T-1").revision)

        _read(rt, "agent_c")              # 非参与者，会被拒

        after = (len(rt.alignment.changes("T-1")),
                 rt.alignment.current("T-1").revision)
        assert after == before


# ============================================================
# 四、执行器：参数一概不读，失败也是 ActionResult
# ============================================================
class TestTheExecutorIgnoresParameters:
    @staticmethod
    def _executor(rt):
        def _bound(*, by):
            return read_shared_context(rt.alignment, "T-1", by=by,
                                       is_participant=_people(rt))
        return SharedContextReadExecutor(_bound)

    def _intent(self, agent_id, **params):
        return ActionIntent(intent_id="i1", agent_id=agent_id,
                            action_type=ACTION_SHARED_CONTEXT_READ,
                            parameters=params)

    def test_success_shape(self, rt):
        res = self._executor(rt).execute(self._intent("agent_a"))

        assert res.success is True
        assert res.error is None
        assert res.metadata["code"] is None      # 成功不编一个 "ok" 出来
        assert set(res.output) >= {"context", "domain_state", "revision"}

    def test_failure_shape(self, rt):
        """🔴 失败时 `output` 必须是 **None**，不能编一个空 dict ——
        那会让人以为「读过，只是空的」（接口成功掩盖语义空转）。"""
        res = self._executor(rt).execute(self._intent("agent_c"))

        assert res.success is False
        assert res.output is None
        assert res.metadata["code"] == "not_participant"
        assert res.error

    def test_parameters_cannot_redirect_to_another_task(self, rt):
        """
        🔴 **本文件最重要的一条。**

        读哪条 Task 由组合根绑定。Agent 就算在 parameters 里写
        `task_id`，也只能读到被绑定的那一条。
        """
        rt.create_task(Task.create(task_id="T-SECRET", creator="agent_c",
                                   description="别的 Task",
                                   participants=["agent_c"]))
        ch = rt.alignment.propose(
            "T-SECRET", proposed_by="agent_c", change_type="ASSIGN",
            new_value={"agent_id": "agent_c",
                       "responsibility": "acceptance"},
            expected_revision=1, reason="x")
        rt.alignment.accept("T-SECRET", ch.change_id, decided_by="agent_c",
                            reason="y")

        res = self._executor(rt).execute(
            self._intent("agent_a", task_id="T-SECRET"))

        assert res.success is True
        assert res.output["task_id"] == "T-1", (
            "Agent 用 parameters 换了 Task —— 组合根的绑定被绕过了")

    def test_parameters_cannot_claim_another_identity(self, rt):
        """
        以什么身份读只来自 `intent.agent_id`。

        `intent.agent_id = agent_c`（非参与者），而 parameters 里
        写着 `by=agent_a` / `actor=agent_a` —— 那**必须**被无视。
        """
        res = self._executor(rt).execute(
            self._intent("agent_c", by="agent_a", actor="agent_a"))

        # parameters 里的 by/actor 被无视；intent.agent_id = agent_c ⇒ 被拒
        assert res.success is False
        assert res.metadata["code"] == "not_participant"

    def test_a_non_dict_result_is_a_structured_failure(self, rt):
        ex = SharedContextReadExecutor(lambda *, by: "我不是 dict")

        res = ex.execute(self._intent("agent_a"))

        assert res.success is False
        assert res.metadata["code"] == "invalid_read_result"

    def test_a_crashing_read_fn_is_a_structured_failure(self, rt):
        def _boom(*, by):
            raise RuntimeError("注入的 read_fn 崩了")

        res = SharedContextReadExecutor(_boom).execute(self._intent("agent_a"))

        assert res.success is False
        assert res.metadata["code"] == "executor_error"
        assert "RuntimeError" in res.error

    def test_a_missing_read_fn_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            SharedContextReadExecutor(None)


# ============================================================
# 五、接线：它真的被注册了 + 逐层边界
# ============================================================
class TestWiring:
    def test_the_composition_root_registers_it(self, rt):
        """从**真实的组合根**取 dispatcher，而不是自己拼一个。"""
        from experiments.heterogeneous_production import production as P

        class _FakeBridge:
            def __getattr__(self, name):
                return lambda *a, **kw: None

        dispatcher, _ = P._action_seam(rt, _FakeBridge())

        assert dispatcher.supports("shared_context_read"), (
            f"组合根没注册它，实际注册的是 {dispatcher.action_types}")

    def test_actions_layer_stays_a_leaf(self):
        """
        `actions/` 是**叶子层**：不许 import `services/` / `core/`。

        🔒 用 AST，不用子串匹配（本项目记录过的误报 bug 类）。
        """
        path = ROOT / "actions" / "shared_context_read.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert not (imported & {"services", "core", "storage",
                                "communication", "experiments"}), (
            f"actions/ 叶子层被破坏了，import 了 {sorted(imported)}")

    def test_it_is_exported_from_the_package(self):
        import actions

        assert "ACTION_SHARED_CONTEXT_READ" in actions.__all__
        assert "SharedContextReadExecutor" in actions.__all__
        assert actions.ACTION_SHARED_CONTEXT_READ == "shared_context_read"

    def test_it_is_not_a_global_builtin(self):
        """
        🔒 它不是「任何调用方都自动具备」的能力 ——
           和 `workspace_write` / `assign_acceptance` 一样，
           由**组合根按需注册**。
        """
        from actions.intent import BUILTIN_ACTION_TYPES

        assert "shared_context_read" not in BUILTIN_ACTION_TYPES

    def test_the_experiment_prompt_documents_it(self):
        """Agent 得知道有这个动作 —— 否则等于没有。"""
        from experiments.agent_division.prompt import PROMPT

        assert "shared_context_read" in PROMPT
        assert "共同认知" in PROMPT

    def test_the_output_is_json_serializable(self, rt):
        """
        它要经 `ExperienceStore.record_action_result()` 落库 ——
        不可序列化会让整条经历落不下来（实测把整轮结果卡死过）。
        """
        _assign(rt)
        got = _read(rt, "agent_a")

        json.dumps(got, ensure_ascii=False)          # 不抛就是通过
