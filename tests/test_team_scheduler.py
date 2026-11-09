"""
TeamScheduler —— 调度 / 排队 / 并行上限
=========================================

验证：

    · 最多 3 个 Team 同时运行；
    · Agent 不是 IDLE 时，请求排队等待；
    · 完成后释放 Agent，推进队列；
    · 启动失败释放预占；
    · 优先级影响启动顺序。
"""

from __future__ import annotations

from work.availability import AgentAvailability
from work.scheduler import RequestStatus, TeamScheduler
from work.team_registry import TeamRegistry


def _start_fn(prefix="task"):
    counter = {"n": 0}

    def start(request):
        counter["n"] += 1
        return {"ok": True, "task_id": f"{prefix}_{counter['n']}"}

    return start


def test_max_parallel_teams_is_three():
    availability = AgentAvailability()
    scheduler = TeamScheduler(
        availability=availability, max_parallel_teams=3)

    requests = [
        scheduler.submit({}, lead=f"lead_{i}", members=[f"m_{i}"])
        for i in range(4)
    ]
    started = []
    for _ in range(3):
        request = scheduler.start_next(_start_fn())
        assert request is not None
        started.append(request)

    assert scheduler.registry.active_count() == 3
    assert scheduler.start_next(_start_fn()) is None
    assert requests[3].status == RequestStatus.QUEUED

    scheduler.finish(started[0].request_id)
    promoted = scheduler.start_next(_start_fn())
    assert promoted is not None
    assert promoted.request_id == requests[3].request_id


def test_busy_agent_waits_then_promotes():
    availability = AgentAvailability()
    registry = TeamRegistry(availability, max_parallel_teams=3)
    scheduler = TeamScheduler(
        registry=registry, availability=availability)

    first = scheduler.submit({}, lead="agent_a", members=["agent_b"])
    started = scheduler.start_next(_start_fn())
    assert started is first
    assert availability.state("agent_a") == AgentAvailability.WORKING

    second = scheduler.submit({}, lead="agent_a", members=["agent_c"])
    assert scheduler.start_next(_start_fn()) is None
    assert second.status == RequestStatus.WAITING_FOR_AGENTS

    scheduler.finish(first.request_id)
    promoted = scheduler.start_next(_start_fn())
    assert promoted is second
    assert availability.state("agent_a") == AgentAvailability.WORKING


def test_start_failure_releases_agents():
    availability = AgentAvailability()
    scheduler = TeamScheduler(availability=availability)

    request = scheduler.submit({}, lead="agent_a", members=["agent_b"])
    failed = scheduler.start_next(
        lambda req: {"ok": False, "message": "boom"})

    assert failed is request
    assert request.status == RequestStatus.FAILED
    assert availability.is_idle("agent_a")
    assert availability.is_idle("agent_b")


def test_priority_order():
    scheduler = TeamScheduler()
    low = scheduler.submit({}, lead="a1", members=[], priority=0)
    high = scheduler.submit({}, lead="a2", members=[], priority=10)

    started = scheduler.start_next(_start_fn())
    assert started is high
    assert scheduler.get(low.request_id).status == RequestStatus.QUEUED


def test_max_agents_per_team():
    scheduler = TeamScheduler(max_agents_per_team=2)
    try:
        scheduler.submit({}, lead="a", members=["b", "c"])
    except ValueError as exc:
        assert "最多 2" in str(exc)
    else:
        raise AssertionError("应该拒绝超过 max_agents_per_team 的 Team")
