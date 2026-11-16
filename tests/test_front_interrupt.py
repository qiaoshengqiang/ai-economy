"""Front 的 interrupt：暂停执行但保留 Task。"""

from __future__ import annotations

from types import SimpleNamespace

from front import FrontIntent, parse_front_decision
from work.controller import AIEOSController, JobStatus, WorkJob


def _controller():
    runtime = SimpleNamespace(
        directory=SimpleNamespace(all=lambda: [
            SimpleNamespace(agent_id="agent_a")]),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    return AIEOSController(runtime, front_agent="agent_a")


def test_parser_accepts_interrupt():
    decision, error = parse_front_decision(
        '{"intent":"interrupt","task_id":"task_x","reply":"先停一下"}',
        active_task_ids={"task_x"},
    )
    assert error == ""
    assert decision is not None
    assert decision.intent == FrontIntent.INTERRUPT
    assert decision.task_id == "task_x"


def test_interrupt_keeps_task_in_progress(monkeypatch):
    task = SimpleNamespace(
        task_id="task_x", status="IN_PROGRESS", is_open=True)
    controller = _controller()
    controller.rt.tasks.get = lambda task_id: task
    target = WorkJob(
        job_id="job_run", human_text="", status=JobStatus.RUNNING,
        task_id="task_x")
    with controller._lock:
        controller._jobs[target.job_id] = target
    request = WorkJob(job_id="job_front", human_text="先停一下")
    decision = SimpleNamespace(
        task_id="task_x", reply="", reason="用户要暂停")
    try:
        controller._interrupt_work(request, decision)
        assert target.cancelled is True
        assert target.stop_mode == "interrupt"
        assert target.status == JobStatus.CANCELLING
        assert task.status == "IN_PROGRESS"

        controller._finish_cancelled(target, "task_x")
        assert target.status == JobStatus.INTERRUPTED
        assert target.summary == "任务已中断，可以继续"
        assert task.status == "IN_PROGRESS"
    finally:
        controller.close()


def test_cancel_still_finishes_cancelled():
    controller = _controller()
    target = WorkJob(
        job_id="job_run", human_text="", status=JobStatus.RUNNING)
    target.stop_mode = "cancel"
    try:
        controller._finish_cancelled(target, "task_x")
        assert target.status == JobStatus.CANCELLED
    finally:
        controller.close()
