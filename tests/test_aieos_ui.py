"""
AIEOS UI / Controller 的轻量回归
=================================

不调用真实 LLM：

    · UI 静态页和 JSON API 可访问；
    · Controller 的 workspace plan 父目录展开正确；
    · 设置接口可保存 Front Agent。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from work.controller import AIEOSController


class _FakeController:
    def __init__(self):
        self.front = "agent_a"
        self.submitted = []
        self.settings = {
            "front_agent": "agent_a",
            "max_parallel_teams": 3,
            "max_agents_per_team": 5,
        }

    def state(self):
        return {
            "front_agent": self.front,
            "settings": self.settings,
            "agents": [
                {"agent_id": "agent_a", "status": "IDLE",
                 "role": "FRONT", "task_id": "", "has_brain": True},
            ],
            "tasks": [],
            "jobs": [],
            "messages": [],
            "running_jobs": 0,
        }

    def submit(self, text):
        self.submitted.append(text)
        return "job_test"

    def update_settings(self, *, front_agent=None, max_parallel_teams=None):
        if front_agent:
            self.front = front_agent
            self.settings["front_agent"] = front_agent
        return self.settings

    def task_detail(self, task_id):
        if task_id != "task_ok":
            raise ValueError("找不到 Task")
        return {
            "task": {"task_id": task_id, "status": "COMPLETED",
                     "mode": "TEAM", "iteration": 1,
                     "creator": "agent_b", "contractor": "agent_b",
                     "participants": ["agent_b", "agent_c"],
                     "description": "test task", "created_at": "t",
                     "updated_at": "t", "workspace_id": "WS"},
            "job": None,
            "executions": [],
            "workspace_files": [],
            "public_memory": "",
            "llm": {"calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "cost_ai": 0},
            "messages": [],
        }


@pytest.fixture
def server():
    from ui.server import Handler

    fake = _FakeController()
    Handler.controller = fake
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def _post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_index_and_state_api(server):
    fake, base = server
    status, body = _get(base + "/")
    assert status == 200
    assert b"AIEOS" in body
    assert b"Front" in body

    status, body = _get(base + "/api/state")
    payload = json.loads(body)
    assert status == 200
    assert payload["front_agent"] == "agent_a"
    assert payload["agents"][0]["role"] == "FRONT"


def test_submit_and_settings_api(server):
    fake, base = server
    status, payload = _post(base + "/api/submit", {"text": "做个游戏"})
    assert status == 200
    assert payload["ok"] is True
    assert fake.submitted == ["做个游戏"]

    status, payload = _post(base + "/api/settings",
                            {"front_agent": "agent_b"})
    assert status == 200
    assert payload["settings"]["front_agent"] == "agent_b"


def test_task_detail_api(server):
    _fake, base = server
    status, body = _get(base + "/api/task/task_ok")
    payload = json.loads(body)
    assert status == 200
    assert payload["task"]["task_id"] == "task_ok"
    assert payload["task"]["status"] == "COMPLETED"

    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/api/task/task_missing")


def test_unknown_api_is_404(server):
    _fake, base = server
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/api/nope")


class _Directory:
    def __init__(self, ids):
        self.entries = [
            SimpleNamespace(agent_id=aid, available=True) for aid in ids
        ]

    def all(self):
        return list(self.entries)


def test_controller_state_marks_front_and_idle_agents():
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        state = controller.state()
        assert state["front_agent"] == "agent_a"
        assert state["agents"][0]["role"] == "FRONT"
        assert state["agents"][1]["status"] == "IDLE"
    finally:
        controller.close()


def test_leader_plan_can_assign_all_work_to_members():
    """单文件任务：Leader 只检查/汇总，不强制自己拥有 item。"""
    from front import FrontBrief
    from work.controller import WorkJob

    plan_json = json.dumps({
        "items": [{
            "item_id": "item_1",
            "assigned_to": "agent_b",
            "goal": "写一个 hello.html",
            "paths": ["hello.html"],
        }],
        "qa": None,
        "directories": {".": "agent_b"},
    })

    class _Agent:
        has_brain = True

        def think(self, _prompt):
            return plan_json

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: _Agent()),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_c")
    try:
        job = WorkJob(job_id="job_test", human_text="写一个 hello.html")
        plan = controller._leader_plan(
            job, "agent_a",
            FrontBrief(goal="写一个 hello.html"))
        assert plan is not None
        assert [item.agent_id for item in plan.items] == ["agent_b"]
    finally:
        controller.close()


def test_job_without_members_degrades_to_solo(monkeypatch):
    """Leader 只分配给自己时，不失败；按 SOLO 执行。"""
    import work.controller as controller_module
    from front import FrontBrief, FrontDecision, FrontIntent, FrontResult
    from work.controller import WorkJob
    from work.plan import parse_work_plan

    class _Front:
        def decide(self, _text, active_tasks=(), completed_tasks=()):
            return FrontDecision(
                intent=FrontIntent.HANDOFF,
                brief=FrontBrief(goal="写一个 hello.py"))

        def result_from_work(self, _result):
            return FrontResult(
                task_id="task_solo", status="COMPLETED", summary="done")

        def format_result(self, result):
            return result.to_human_text()

    plan, error = parse_work_plan(
        json.dumps({
            "items": [{
                "item_id": "item_1",
                "assigned_to": "agent_b",
                "goal": "写一个 hello.py",
            }],
            "qa": None,
        }),
        participants=["agent_b", "agent_c"],
    )
    assert plan is not None, error

    seen = {}

    class _Solo:
        def __init__(self, _rt, task, **_kwargs):
            seen["runner"] = "solo"
            seen["task_id"] = task.task_id

        def run(self):
            return SimpleNamespace(ok=True, tokens=7, turns=2)

    class _Team:
        def __init__(self, *_args, **_kwargs):
            seen["runner"] = "team"

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(
            list_tasks=lambda limit=50: [],
            get=lambda tid: SimpleNamespace(task_id=tid),
        ),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", solo_only=False)
    monkeypatch.setattr(controller, "_front_runtime", lambda: _Front())
    monkeypatch.setattr(
        controller, "_acquire_leader", lambda _job: "agent_b")
    monkeypatch.setattr(
        controller, "_leader_plan",
        lambda _job, _leader, _brief: plan)

    def _create(job, leader, members, plan_, decision, *, mode):
        seen["members"] = list(members)
        seen["mode"] = mode
        return {"ok": True, "task_id": "task_solo"}

    monkeypatch.setattr(controller, "_create_task", _create)
    monkeypatch.setattr(controller_module, "SoloWorkRuntime", _Solo)
    monkeypatch.setattr(controller_module, "TeamWorkRuntime", _Team)

    job = WorkJob(job_id="job_solo", human_text="写一个 hello.py")
    try:
        controller._run_job(job)
    finally:
        controller.close()

    assert seen["members"] == []
    assert seen["mode"] == "SOLO"
    assert seen["runner"] == "solo"
    assert job.status == "COMPLETED"
    assert job.task_id == "task_solo"


def test_workspace_plan_accepts_root_marker_directory():
    """Leader 用 "." 表示根目录时，不能变成空目录名。"""
    from demand.task_creation import TaskCreationService

    plan = AIEOSController._workspace_plan(
        "agent_a", ["agent_b"], {".": "agent_b", "game/src": "agent_b"})
    service = TaskCreationService(SimpleNamespace(), SimpleNamespace())
    validated = service.validate_workspace_plan(
        plan, people=["agent_a", "agent_b"], producer="agent_a")
    assert "." not in validated["directories"]
    assert validated["directories"]["game/src"] == "agent_b"
    assert validated["directories"]["game"] == "agent_a"


def test_task_refs_hides_legacy_team_tasks_in_solo_mode():
    tasks = [
        SimpleNamespace(
            task_id="task_team", status="IN_PROGRESS", mode="TEAM",
            description="旧 Team 任务", is_open=True),
        SimpleNamespace(
            task_id="task_solo", status="IN_PROGRESS", mode="SOLO",
            description="Solo 任务", is_open=True),
    ]
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: tasks),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        active, completed = controller._task_refs()
    finally:
        controller.close()

    assert [item["task_id"] for item in active] == ["task_solo"]
    assert completed == []


def test_handoff_runs_one_agent_solo_without_leader_plan(monkeypatch):
    """Solo 模式：Front handoff 后直接选一个 Agent，不进入 Leader 规划。"""
    import work.controller as controller_module
    from front import FrontBrief, FrontDecision, FrontIntent, FrontResult
    from work.controller import WorkJob

    class _Front:
        def decide(self, _text, active_tasks=(), completed_tasks=()):
            return FrontDecision(
                intent=FrontIntent.HANDOFF,
                brief=FrontBrief(
                    goal="写一个 hello.py",
                    deliverables=["hello.py"],
                ))

        def result_from_work(self, _result):
            return FrontResult(
                task_id="task_solo", status="COMPLETED", summary="done")

        def format_result(self, result):
            return result.to_human_text()

    seen = {}
    task = SimpleNamespace(task_id="task_solo")
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(
            list_tasks=lambda limit=50: [],
            get=lambda tid: task,
            save=lambda _task: None,
        ),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    monkeypatch.setattr(controller, "_front_runtime", lambda: _Front())
    monkeypatch.setattr(
        controller, "_acquire_worker", lambda _job: "agent_b")

    def _create(job, worker, decision):
        seen["worker"] = worker
        seen["goal"] = decision.brief.goal
        return {"ok": True, "task_id": "task_solo"}

    monkeypatch.setattr(controller, "_create_solo_task", _create)

    def _no_plan(*_args, **_kwargs):
        raise AssertionError("solo_only 不应调用 Leader 规划")

    monkeypatch.setattr(controller, "_leader_plan", _no_plan)

    class _Solo:
        def __init__(self, _rt, task_, **kwargs):
            seen["runner"] = "solo"
            seen["task_id"] = task_.task_id
            seen["max_turns"] = kwargs.get("max_turns")
            seen["cognitive_budget"] = kwargs.get("cognitive_budget")

        def run(self):
            return SimpleNamespace(ok=True, tokens=2, turns=1)

    monkeypatch.setattr(controller_module, "SoloWorkRuntime", _Solo)
    monkeypatch.setattr(
        controller_module, "TeamWorkRuntime",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("solo_only 不应进入 Team")))
    monkeypatch.setattr(
        controller, "_acquire_members",
        lambda _members: (_ for _ in ()).throw(
            AssertionError("solo_only 不应占用多个 Agent")))

    job = WorkJob(job_id="job_solo_handoff", human_text="写一个 hello.py")
    try:
        controller._run_job(job)
    finally:
        controller.close()

    assert seen == {
        "worker": "agent_b",
        "goal": "写一个 hello.py",
        "runner": "solo",
        "task_id": "task_solo",
        "max_turns": 24,
        "cognitive_budget": 65536,
    }
    assert job.leader == "agent_b"
    assert job.members == []
    assert job.status == "COMPLETED"
    assert job.task_id == "task_solo"


def test_task_refs_marks_open_task_without_job_as_interrupted():
    task = SimpleNamespace(
        task_id="task_old", status="IN_PROGRESS", mode="SOLO",
        description="写 hello world", is_open=True)
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: [task]),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        active, completed = controller._task_refs()
    finally:
        controller.close()
    assert completed == []
    assert active == [{
        "task_id": "task_old",
        "status": "INTERRUPTED",
        "summary": "写 hello world",
    }]


def test_followup_resumes_interrupted_task(monkeypatch):
    import work.controller as controller_module
    from front import FrontDecision, FrontIntent, FrontResult
    from work.controller import WorkJob

    class _Front:
        def decide(self, _text, active_tasks=(), completed_tasks=()):
            return FrontDecision(
                intent=FrontIntent.FOLLOW_UP,
                task_id="task_old",
                reply="这个任务之前中断了，我现在继续。",
            )

        def result_from_work(self, _result):
            return FrontResult(
                task_id="task_old", status="COMPLETED", summary="done")

        def format_result(self, result):
            return result.to_human_text()

    task = SimpleNamespace(
        task_id="task_old", status="IN_PROGRESS", mode="TEAM",
        creator="agent_b", contractor="agent_b",
        participants=["agent_b", "agent_c"], is_open=True,
        description="写 hello world", iteration=1,
    )
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(
            list_tasks=lambda limit=50: [],
            get=lambda tid: task,
            save=lambda _task: None,
        ),
    )

    class _Team:
        def __init__(self, _rt, task_, **_kwargs):
            self.task = task_

        def run(self):
            return SimpleNamespace(ok=True, tokens=3, turns=1)

    controller = AIEOSController(
        runtime, front_agent="agent_a", solo_only=False)
    monkeypatch.setattr(controller, "_front_runtime", lambda: _Front())
    monkeypatch.setattr(controller_module, "TeamWorkRuntime", _Team)

    job = WorkJob(job_id="job_resume", human_text="继续做")
    try:
        controller._run_job(job)
    finally:
        controller.close()

    assert job.task_id == "task_old"
    assert job.status == "COMPLETED"
    assert any(
        "之前中断了" in message["text"]
        for message in controller.state()["messages"]
    )


def test_rework_reopens_completed_task_with_new_request(monkeypatch):
    import work.controller as controller_module
    from front import FrontDecision, FrontIntent, FrontResult
    from work.controller import WorkJob

    seen = {}

    class _Front:
        def decide(self, _text, active_tasks=(), completed_tasks=()):
            return FrontDecision(
                intent=FrontIntent.REWORK,
                task_id="task_done",
                reply="收到，我让原团队改。",
            )

        def result_from_work(self, _result):
            return FrontResult(
                task_id="task_done", status="COMPLETED", summary="已改")

        def format_result(self, result):
            return result.to_human_text()

    task = SimpleNamespace(
        task_id="task_done", status="COMPLETED", mode="TEAM",
        creator="agent_b", contractor="agent_b",
        participants=["agent_b", "agent_c"], is_open=False,
        description="做一个静态网页", iteration=1,
    )
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(
            list_tasks=lambda limit=50: [],
            get=lambda tid: task,
            save=lambda _task: None,
        ),
    )

    class _Team:
        def __init__(self, _rt, task_, **_kwargs):
            self.task = task_

        def reopen_and_run(self, *, reason=""):
            seen["reason"] = reason
            return SimpleNamespace(ok=True, tokens=5, turns=2)

    controller = AIEOSController(
        runtime, front_agent="agent_a", solo_only=False)
    monkeypatch.setattr(controller, "_front_runtime", lambda: _Front())
    monkeypatch.setattr(controller_module, "TeamWorkRuntime", _Team)

    job = WorkJob(
        job_id="job_rework",
        human_text="把网页文字改成 BLG加油！")
    try:
        controller._run_job(job)
    finally:
        controller.close()

    assert seen["reason"] == "把网页文字改成 BLG加油！"
    assert "返工要求（第 2 轮）" in task.description
    assert "BLG加油！" in task.description
    assert job.status == "COMPLETED"


def test_cancel_running_task_marks_job_and_task():
    from front import FrontDecision, FrontIntent
    from work.controller import WorkJob

    class _Task:
        task_id = "task_live"
        status = "IN_PROGRESS"
        is_open = True

        def __init__(self):
            self.reason = ""

        def cancel(self, reason=None):
            self.status = "CANCELLED"
            self.reason = reason or ""

    task = _Task()
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(
            get=lambda tid: task,
            save=lambda _task: task,
            list_tasks=lambda limit=50: [],
        ),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    target = WorkJob(
        job_id="job_live", human_text="做一个支出记录器",
        status="RUNNING", task_id="task_live")
    controller._jobs[target.job_id] = target
    controller.availability.try_acquire("agent_b")
    controller._bind_agent("agent_b", target, "LEADER")

    cancel_job = WorkJob(job_id="job_cancel", human_text="停掉这个任务")
    decision = FrontDecision(
        intent=FrontIntent.CANCEL, task_id="task_live",
        reply="好的，正在停止")
    try:
        controller._cancel_work(cancel_job, decision)
        state = controller.state()
    finally:
        controller.close()

    assert target.cancelled is True
    assert target.status == "CANCELLING"
    assert task.status == "CANCELLED"
    assert "停掉这个任务" in task.reason
    assert cancel_job.status == "DIRECT_REPLY"
    agent = next(
        item for item in state["agents"] if item["agent_id"] == "agent_b")
    assert agent["status"] == "STOPPING"


def test_workspace_plan_declares_parent_directories():
    plan = AIEOSController._workspace_plan(
        "agent_a",
        ["agent_b"],
        {"game/src/entities": "agent_b"},
    )
    dirs = plan["directories"]
    assert dirs["game/src/entities"] == "agent_b"
    assert dirs["game"] == "agent_a"
    assert dirs["game/src"] == "agent_a"
    assert plan["root_owner"] == "agent_a"
