"""Front 批量 handoff：一次对话派发多个任务给不同 Worker。"""

from __future__ import annotations

from types import SimpleNamespace

from front import FrontBrief, FrontDecision, FrontIntent
from work.controller import (
    AIEOSController,
    JobStatus,
    WorkJob,
)


class _Directory:
    def __init__(self, ids):
        self.entries = [
            SimpleNamespace(agent_id=aid, available=True) for aid in ids
        ]

    def all(self):
        return list(self.entries)


class _Executor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, job):
        self.jobs.append((fn, job))

    def shutdown(self, *args, **kwargs):
        return None


def _controller(tmp_path, ids=("agent_a", "agent_b", "agent_c")):
    runtime = SimpleNamespace(
        directory=_Directory(ids),
        economy=SimpleNamespace(
            agent=lambda aid: SimpleNamespace(has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(
        runtime,
        front_agent="agent_a",
        task_root=tmp_path / "tasks",
        settings_path=tmp_path / "ui_settings.json",
    )
    executor = _Executor()
    controller._executor = executor
    return controller, executor


def _handoff(goal, *, preferred="", reply=""):
    return FrontDecision(
        intent=FrontIntent.HANDOFF,
        brief=FrontBrief(goal=goal, task_text=goal),
        preferred_agent=preferred,
        reply=reply,
    )


def test_batch_dispatches_one_child_job_per_handoff(tmp_path):
    controller, executor = _controller(tmp_path)
    decisions = [
        _handoff("写前端页面", preferred="agent_b"),
        _handoff("写后端接口", preferred="agent_c"),
    ]
    parent = WorkJob(job_id="job_batch", human_text="做两个页面")
    try:
        controller._dispatch_batch(parent, decisions)

        assert parent.status == JobStatus.BATCH_DISPATCHED
        assert parent.batch_size == 2
        assert len(executor.jobs) == 2
        children = [job for _fn, job in executor.jobs]
        assert [job.batch_index for job in children] == [1, 2]
        assert [job.assigned_worker for job in children] == [
            "agent_b", "agent_c"]
        assert all(job.parent_job_id == "job_batch" for job in children)
        assert controller.availability.state("agent_b") == "WORKING"
        assert controller.availability.state("agent_c") == "WORKING"
    finally:
        controller.availability.release("agent_b")
        controller.availability.release("agent_c")
        controller.close()


def test_batch_assigns_distinct_idle_workers_when_not_preferred(tmp_path):
    controller, executor = _controller(tmp_path)
    decisions = [_handoff("任务一"), _handoff("任务二")]
    parent = WorkJob(job_id="job_auto", human_text="两个任务")
    try:
        controller._dispatch_batch(parent, decisions)

        assert parent.status == JobStatus.BATCH_DISPATCHED
        workers = [job.assigned_worker for _fn, job in executor.jobs]
        assert len(set(workers)) == 2
        assert "agent_a" not in workers
    finally:
        for worker in ("agent_b", "agent_c"):
            controller.availability.release(worker)
        controller.close()


def test_batch_rejects_duplicate_worker(tmp_path):
    controller, executor = _controller(tmp_path)
    decisions = [
        _handoff("任务一", preferred="agent_b"),
        _handoff("任务二", preferred="agent_b"),
    ]
    parent = WorkJob(job_id="job_dup", human_text="重复 worker")
    try:
        controller._dispatch_batch(parent, decisions)

        assert parent.status == JobStatus.FAILED
        assert "duplicate" in parent.error
        assert executor.jobs == []
        assert controller.availability.state("agent_b") == "IDLE"
    finally:
        controller.close()


def test_batch_rejects_when_not_enough_idle_workers(tmp_path):
    controller, executor = _controller(tmp_path, ids=("agent_a", "agent_b"))
    decisions = [_handoff("任务一"), _handoff("任务二")]
    parent = WorkJob(job_id="job_short", human_text="worker 不够")
    try:
        controller._dispatch_batch(parent, decisions)

        assert parent.status == JobStatus.FAILED
        assert "没有足够的空闲 Worker" in parent.error
        assert executor.jobs == []
        assert controller.availability.state("agent_b") == "IDLE"
    finally:
        controller.close()


def test_batch_rejects_project_workdir(tmp_path):
    controller, executor = _controller(tmp_path)
    decisions = [_handoff("任务一"), _handoff("任务二")]
    parent = WorkJob(
        job_id="job_project",
        human_text="同一项目",
        workdir=str(tmp_path / "project"),
    )
    try:
        controller._dispatch_batch(parent, decisions)

        assert parent.status == JobStatus.FAILED
        assert "同一项目目录" in parent.error
        assert executor.jobs == []
    finally:
        controller.close()


def test_child_job_skips_front_and_uses_assigned_worker(tmp_path):
    controller, _executor = _controller(tmp_path)
    assert controller.availability.try_acquire("agent_b")
    child = WorkJob(
        job_id="job_batch_b1",
        human_text="子任务",
        batch_id="job_batch",
        parent_job_id="job_batch",
        batch_index=1,
        batch_size=1,
        assigned_worker="agent_b",
        precomputed_decision=_handoff("子任务"),
    )
    called = []

    def fake_handoff(job, decision, front):
        called.append((decision.brief.goal, front, job.assigned_worker))
        job.status = JobStatus.COMPLETED
        return "agent_b", []

    def fail_front(*args, **kwargs):
        raise AssertionError("子 Job 不应再次调用 Front")

    controller._run_handoff_solo = fake_handoff
    controller._front_runtime = fail_front
    try:
        controller._run_job(child)

        assert called == [("子任务", None, "agent_b")]
        assert child.status == JobStatus.COMPLETED
        assert controller.availability.state("agent_b") == "IDLE"
    finally:
        controller.close()


def test_batch_rejects_mixed_execution_actions(tmp_path):
    controller, executor = _controller(tmp_path)
    decisions = [
        _handoff("任务一"),
        FrontDecision(intent=FrontIntent.FOLLOW_UP, task_id="task_x"),
    ]
    parent = WorkJob(job_id="job_mixed", human_text="混合动作")
    try:
        controller._dispatch_batch(parent, decisions)

        assert parent.status == JobStatus.FAILED
        assert "只支持 handoff" in parent.error
        assert executor.jobs == []
    finally:
        controller.close()
