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
    controller = AIEOSController(runtime, front_agent="agent_a")
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
