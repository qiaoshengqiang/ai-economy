"""工作恢复 / 返工的分类与 Pi session 规则。"""

from __future__ import annotations

from types import SimpleNamespace

from front import FrontBrief, FrontDecision, FrontIntent
from work.controller import (
    AIEOSController, JobStatus, WorkJob, _is_interruption,
)
from work.pi_runtime import PiWorkRuntime


def test_pi_timeout_is_treated_as_interruption():
    assert _is_interruption("Pi 超时（1800.0s）")
    assert _is_interruption("LLM timed out")
    assert not _is_interruption("ValueError: bad config")


def test_pi_session_id_reuses_only_same_iteration():
    assert PiWorkRuntime._pi_session_id("task_1", 1, {}) == "task_1"
    assert PiWorkRuntime._pi_session_id(
        "task_1", 1,
        {"iteration": 1, "session_id": "task_1"}) == "task_1"
    # Reopen：新 iteration 开新 session，避免旧 transcript 反复膨胀。
    assert PiWorkRuntime._pi_session_id(
        "task_1", 2,
        {"iteration": 1, "session_id": "task_1"}) == "task_1.i2"
    # 新 iteration 中断后恢复：复用该 iteration 的 session。
    assert PiWorkRuntime._pi_session_id(
        "task_1", 2,
        {"iteration": 2, "session_id": "task_1.i2"}) == "task_1.i2"


def test_live_follow_up_is_delivered_to_inbox(monkeypatch):
    class _Directory:
        def all(self):
            return [
                SimpleNamespace(agent_id="agent_a"),
                SimpleNamespace(agent_id="agent_b"),
            ]

    task = SimpleNamespace(
        task_id="task_live", status="IN_PROGRESS", mode="SOLO",
        is_open=True, contractor="agent_b", creator="agent_b",
        participants=["agent_b"], workspace_id="WS", iteration=1,
        description="原始任务", acceptance={})
    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(
                has_brain=True, brain=None)),
        tasks=SimpleNamespace(
            get=lambda _tid: task,
            list_tasks=lambda limit=50: [],
        ),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_front", human_text="补充一下")
    target = WorkJob(
        job_id="job_running", human_text="原任务",
        task_id=task.task_id, status=JobStatus.RUNNING,
        leader="agent_b", members=["agent_b"])
    controller._jobs[job.job_id] = job
    controller._jobs[target.job_id] = target
    assert controller._agent_busy("agent_b") is True

    sent = {}

    def fake_send(**kwargs):
        sent.update(kwargs)
        return {"ok": True, "message_id": "msg_follow"}

    monkeypatch.setattr(controller, "_send_front_message", fake_send)
    decision = FrontDecision(
        intent=FrontIntent.FOLLOW_UP,
        task_id=task.task_id,
        brief=FrontBrief(
            goal="补充",
            human_text="补充一下",
            task_text="请把失败用例修掉",
        ),
        reply="我转告他。",
    )
    try:
        controller._resume_or_rework(job, decision, None)
    finally:
        controller.close()

    assert sent["to_agent"] == "agent_b"
    assert sent["text"] == "请把失败用例修掉"
    assert sent["payload"]["kind"] == "task_follow_up"
    assert sent["payload"]["task_id"] == task.task_id
    assert job.task_id == task.task_id
    assert job.status == JobStatus.DIRECT_REPLY
    assert "已把补充要求" in job.summary
    # live follow_up 不重启、不改写 Task description。
    assert "续跑要求" not in task.description
    assert target.status == JobStatus.RUNNING


def test_rework_persists_selected_role(monkeypatch):
    from types import SimpleNamespace

    import work.controller as controller_module
    from front import FrontDecision, FrontIntent
    from work.controller import AIEOSController, WorkJob

    class _Runner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                ok=True, tokens=1, turns=1, status="COMPLETED",
                summary="done", error="")

    class _Directory:
        def all(self):
            return [
                SimpleNamespace(agent_id="agent_a", available=True),
                SimpleNamespace(agent_id="agent_b", available=True),
            ]

    monkeypatch.setattr(controller_module, "BuiltinWorkRuntime", _Runner)
    saved = []
    task = SimpleNamespace(
        task_id="task_rework", status="IN_PROGRESS", mode="SOLO",
        is_open=True, creator="agent_b", participants=["agent_b"],
        workspace_id="WS", iteration=1, description="d",
        acceptance={"role": "generalist", "work_profile": "standard"})
    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(has_brain=True, brain=None)),
        tasks=SimpleNamespace(
            get=lambda _tid: task,
            list_tasks=lambda limit=50: [],
            save=lambda item: saved.append(dict(item.acceptance or {})),
        ),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_rework", human_text="换成游戏开发视角看看")
    controller._jobs[job.job_id] = job

    class _Front:
        def result_from_work(self, _result):
            return SimpleNamespace(summary="done", status="COMPLETED")

        def format_result(self, _result):
            return "result"

    decision = FrontDecision(
        intent=FrontIntent.FOLLOW_UP,
        task_id="task_rework",
        role="game_developer",
        reply="我继续。",
    )
    try:
        controller._resume_or_rework(job, decision, _Front())
    finally:
        controller.close()

    assert task.acceptance["role"] == "game_developer"
    assert saved and saved[-1]["role"] == "game_developer"
