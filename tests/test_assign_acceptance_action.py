"""
`assign_acceptance` —— Agent 自己指派验收责任（轻量测试，不调 LLM）

【它补的是哪一个缺口】
`task_execution_service` 自己写着「谁负责验收由 **Agent 自己**通过现有的
**ASSIGN** 能力决定」—— 但 ASSIGN **没有消息通道**，于是真实实验里只能由
runner 代做。没有它：Agent 能发 SUBMIT，却**没有人能被授权 VERIFY**
（`acceptors_of()` 返回空集，fail-closed）。
"""
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

A, B, OUTSIDER = "novel_author", "novel_reader", "novel_editor"
TASK, TASK_NO_CTX = "t_assign", "t_no_context"


@pytest.fixture(scope="module")
def env():
    from core.task import Task
    from main import build_runtime, build_task_people_checker

    db = os.path.join(tempfile.mkdtemp(), "assign.db")
    rt = build_runtime(db_path=db, agent_ids=[A, B, OUTSIDER])
    rt.tasks.save(Task.create(creator=A, description="d", workspace_id="W",
                              task_id=TASK, participants=[A, B]))
    rt.tasks.save(Task.create(creator=A, description="d", workspace_id="W2",
                              task_id=TASK_NO_CTX, participants=[A, B]))
    rt.alignment.create_context(TASK, by=A, objective="写东西",
                                acceptance_criteria=["有内容"])
    people = build_task_people_checker(rt.tasks)
    try:
        yield rt, people
    finally:
        rt.close()


def _assign(env, *, by=A, to=B, task=TASK, **kw):
    from services.assignment_ops import assign_acceptance

    rt, people = env
    return assign_acceptance(rt.alignment, task, by=by, to=to,
                             is_participant=kw.pop("is_participant", people),
                             **kw)


def _acceptors(env, task=TASK):
    rt, _ = env
    return rt.task_execution.acceptors_of(rt.tasks.get(task))


# ---- 缺口本身 ----
def test_before_assigning_nobody_can_verify(env):
    """🔴 这就是那个缺口：`acceptors_of()` 空 ⇒ 谁都 VERIFY 不了。"""
    assert _acceptors(env, TASK_NO_CTX) == ()


def test_assigning_closes_the_gap(env):
    r = _assign(env, by=A, to=B)
    assert r["ok"] and r["to"] == B
    assert B in _acceptors(env)
    assert r["responsibility"] == "acceptance"


# ---- 边界 ----
def test_cannot_assign_acceptance_to_yourself(env):
    r = _assign(env, by=A, to=A)
    assert r["ok"] is False and r["code"] == "cannot_assign_to_self"


def test_cannot_assign_to_a_phantom_agent(env):
    """
    🔴 对抗测试抓到的真 bug：第一版**不检查接收方是否存在** ⇒
       `to="nobody"` 会成功，然后 `acceptors_of()` 返回一个**幽灵** ——
       后果不是报错，而是**静默停住**（谁都 VERIFY 不了）。
       与 `handoff` 已堵过的是同一类洞。
    """
    r = _assign(env, by=A, to="not_a_real_agent")
    assert r["ok"] is False and r["code"] == "unknown_agent"


def test_cannot_assign_to_someone_outside_the_task(env):
    """真实存在、但**不是这条 Task 的人** —— 同样不能当验收人。"""
    r = _assign(env, by=A, to=OUTSIDER)
    assert r["ok"] is False and r["code"] == "unknown_agent"


def test_assignor_must_be_on_the_task_too(env):
    r = _assign(env, by=OUTSIDER, to=B)
    assert r["ok"] is False and r["code"] == "not_participant"


def test_requires_a_shared_context(env):
    r = _assign(env, task=TASK_NO_CTX)
    assert r["ok"] is False and r["code"] == "no_shared_context"


def test_fails_closed_without_a_participant_checker(env):
    """
    🔒 **fail-closed**：没有检查器就**拒绝**，而不是放行。
       「无法验证身份时照做」正是幽灵验收人的成因。
    """
    r = _assign(env, is_participant=None)
    assert r["ok"] is False and r["code"] == "no_participant_checker"


def test_empty_to_is_refused(env):
    assert _assign(env, to="")["code"] == "bad_argument"


# ---- 动作层 ----
def test_action_dispatches(env):
    from actions import ActionDispatcher, AssignAcceptanceExecutor, build_intent
    from services.assignment_ops import assign_acceptance

    rt, people = env
    d = ActionDispatcher([AssignAcceptanceExecutor(
        lambda *, by, to, reason="": assign_acceptance(
            rt.alignment, TASK, by=by, to=to, reason=reason,
            is_participant=people))])
    r = d.dispatch(build_intent(A, {"type": "assign_acceptance",
                                    "parameters": {"to": B}}))
    assert r.success and r.output["to"] == B


def test_agent_id_comes_from_intent_not_parameters(env):
    """🔴 发起方只取自 `intent.agent_id` —— 否则 Agent 能冒充别人去指派。"""
    import ast

    src = (ROOT / "actions" / "assignment.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "assign_fn"]
    assert calls, "没找到 assign_fn 调用点"
    for c in calls:
        seg = ast.get_source_segment(src, c) or ""
        assert "by=intent.agent_id" in seg, seg
        assert 'params.get("agent_id")' not in seg, seg


def test_actions_layer_does_not_import_services():
    import ast

    src = (ROOT / "actions" / "assignment.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    for bad in ("services", "core", "agents"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), sorted(mods)


def test_composition_root_registers_it():
    import ast

    src = (ROOT / "experiments" / "heterogeneous_production"
           / "production.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_action_seam")
    assert "AssignAcceptanceExecutor" in {n.id for n in ast.walk(fn)
                                          if isinstance(n, ast.Name)}


def test_task_people_checker_is_fail_closed():
    """查不到 Task ⇒ False（与 Gateway 的 `agent_exists` 同一原则）。"""
    from main import build_task_people_checker

    class _NoStore:
        def get(self, _tid):
            raise RuntimeError("炸了")

    assert build_task_people_checker(_NoStore())("t", "a") is False
