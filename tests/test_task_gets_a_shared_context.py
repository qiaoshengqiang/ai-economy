"""
🔴 **Task 建出来的时候，它的 Shared Context 也该诞生**

【用户指出的缺陷】

    「应该要建立的啊，这明显之前实现任务创建的时候把它漏了。
      Agent 创建任务的时候就该把这个给创建好了。」

【Shared Context 是什么】

    「这个 Task 的共同认知」的**容器** —— 属于 Task，不属于任何 Agent。
    里面装 objective / constraints / acceptance_criteria / decisions /
    assignments / facts / …，并且**每一版都不可变、有历史**
    （`ContextRevision`：revision · parent_revision · created_by · change_reason）。

    「谁负责什么」就在 `assignments` 里 —— **验收责任人也在那儿**。

【漏了它会发生什么（实测）】

    观察实验里 agent_a 想指派验收人（分工的核心动作）：
        assign_acceptance → `no_shared_context` 失败
    ⇒ **它想合作，环境只给了它「会失败」的告知，没给「能做到」的路径。**

【所以】

    🔒 **建的是空容器**：objective 空 · 无 acceptance_criteria · 无 ASSIGN。
       容器是**环境形状**（和 `ensure_workspace` 同类），
       **内容全归 Agent** —— 不预设任何分工（§0.5）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.task import Task                                    # noqa: E402
from core.task_store import TaskStore                         # noqa: E402
from services.task_negotiation_service import (               # noqa: E402
    TaskNegotiationService,
)
from storage.database import Database                         # noqa: E402

AGENTS = ("agent_a", "agent_b", "agent_c")


@pytest.fixture()
def rt(tmp_path):
    from main import build_runtime

    r = build_runtime(db_path=str(tmp_path / "tsc.db"), agent_ids=list(AGENTS))
    yield r
    r.close()


def _offer(task_id="T-OFFER-1", sender="agent_a", recipient="agent_b"):
    from communication.message import Message, MessageType

    return Message(
        from_agent=sender, to_agent=recipient,
        message_type=MessageType.TASK_OFFER, trace_id="trc_x",
        payload={"task_id": task_id, "description": "写个分析程序",
                 "budget": {"amount": 100, "currency": "AIC"}},
    )


# ---- ① Runtime.create_task ----
def test_runtime_create_task_also_creates_the_container(rt):
    rt.create_task(Task.create(task_id="T-1", creator="agent_a",
                               description="写个短篇",
                               participants=list(AGENTS)))
    ctx = rt.alignment.current("T-1")
    assert ctx is not None, "Task 建了，容器没建 —— 这正是被漏掉的那一步"
    assert ctx.revision == 1


def test_the_container_is_EMPTY(rt):
    """🔒 容器是**环境形状**，内容全归 Agent —— 不预设任何分工（§0.5）。"""
    rt.create_task(Task.create(task_id="T-1", creator="agent_a",
                               description="写个短篇",
                               participants=list(AGENTS)))
    c = rt.alignment.current("T-1").context
    assert c.objective == "", "不该替 Agent 写目标"
    assert c.acceptance_criteria == (), "不该替 Agent 定 DoD"
    assert c.assignments == (), "不该替 Agent 指派任何人"


def test_it_is_idempotent(rt):
    """🔒 已经有容器就**不动它** —— 绝不覆盖 Agent 写进去的东西。"""
    from services.assignment_ops import assign_acceptance

    t = Task.create(task_id="T-1", creator="agent_a", description="x",
                    participants=list(AGENTS))
    rt.create_task(t)
    assert rt.alignment.current("T-1").revision == 1

    # Agent 往容器里写了东西（指派验收责任 —— `ASSIGN` 是声明类，可自批）
    assert assign_acceptance(rt.alignment, "T-1", by="agent_a", to="agent_b",
                             is_participant=lambda tid, aid: True)["ok"]
    after_write = rt.alignment.current("T-1")
    assert after_write.revision == 2
    assignees = [x.agent_id for x in after_write.context.assignments]
    assert assignees == ["agent_b"]

    rt.create_task(t)                                  # 再建一次

    now = rt.alignment.current("T-1")
    assert now.revision == 2, "幂等被破坏 —— 又建了一版"
    assert [x.agent_id for x in now.context.assignments] == ["agent_b"], \
        "重跑把 Agent 指派的人弄丢了"


def test_assign_acceptance_works_once_the_container_exists(rt):
    """🎯 这是**因果链的闭合点**：容器在 ⇒ 指派验收人就能成。"""
    from services.assignment_ops import assign_acceptance

    rt.create_task(Task.create(task_id="T-1", creator="agent_a",
                               description="x", participants=list(AGENTS)))
    r = assign_acceptance(rt.alignment, "T-1", by="agent_a", to="agent_b",
                          is_participant=lambda tid, aid: True)
    assert r["ok"] is True, r
    assert rt.alignment.current("T-1").context.assignments, "验收责任没落进 assignments"


# ---- ② Agent 路径：收到 TASK_OFFER ----
def test_an_agent_receiving_an_offer_gets_a_container(rt):
    """
    🔴 **「Agent 创建任务」的那条真实路径。**

        TaskService.handle_offer
          → handlers["TASK_OFFER"]          OfferHandler
            → negotiation.open_offer(...)   TaskNegotiationService
              → _ensure_shared_context(...) ← 容器在这里诞生
    """
    assert rt.tasks.get("T-OFFER-1") is None
    rt.task_service.handle_offer(_offer())

    assert rt.tasks.get("T-OFFER-1") is not None, "任务没建"
    ctx = rt.alignment.current("T-OFFER-1")
    assert ctx is not None, (
        "Agent 建了任务却没建容器 —— 于是它连验收人都指派不了")
    assert ctx.context.assignments == (), "Offer 处理不该顺带指派任何人"


def test_the_wiring_is_real_not_just_a_parameter(rt):
    """
    🔒 守卫：组合根必须**真的**把对齐层传下去。
       少传它 ⇒ `alignment=None` ⇒ 悄悄什么都不建（而且不报错）。
    """
    assert rt.task_service.negotiation.alignment is rt.alignment, (
        "TaskService 没把对齐层传给议价服务 —— 容器又会被静默漏掉")


def test_old_construction_still_works(tmp_path):
    """🔒 `alignment` 是**可选**参数 —— 老调用方一行都不用改。"""
    s = TaskNegotiationService(TaskStore(Database(str(tmp_path / "x.db"))))
    assert s.alignment is None
    s._ensure_shared_context(Task.create(task_id="T-X", creator="agent_a"))
