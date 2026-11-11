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
from front import FRONT_SYSTEM_PROMPT


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


def test_task_detail_page_is_served(server):
    _fake, base = server
    status, body = _get(base + "/task.html?task_id=task_1")
    assert status == 200
    assert b"AIEOS" in body
    assert b"/static/task.js" in body


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

        def think(self, _prompt, **_kwargs):
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
        def decide(self, _text, active_tasks=(), completed_tasks=(), conversation=(), **kwargs):
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
        def decide(self, _text, active_tasks=(), completed_tasks=(), conversation=(), **kwargs):
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
        def decide(self, _text, active_tasks=(), completed_tasks=(), conversation=(), **kwargs):
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
        def decide(self, _text, active_tasks=(), completed_tasks=(), conversation=(), **kwargs):
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


def test_controller_passes_recent_conversation_to_front(monkeypatch):
    from front import FrontDecision, FrontIntent
    from work.controller import WorkJob

    captured = {}

    class _Front:
        def decide(self, _text, active_tasks=(), completed_tasks=(),
                   conversation=(), **kwargs):
            captured["conversation"] = list(conversation)
            return FrontDecision(
                intent=FrontIntent.DIRECT_REPLY, reply="好")

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    controller._messages = [
        {"role": "human", "text": "之前那个台球网页做好了吗",
         "job_id": "job_old"},
        {"role": "front", "text": "已经做好了", "job_id": "job_old"},
        {"role": "human", "text": "再做一个", "job_id": "job_new"},
    ]
    monkeypatch.setattr(controller, "_front_runtime", lambda: _Front())
    job = WorkJob(job_id="job_new", human_text="再做一个")
    try:
        controller._run_job(job)
    finally:
        controller.close()

    assert captured["conversation"] == [
        {"role": "human", "text": "之前那个台球网页做好了吗"},
        {"role": "front", "text": "已经做好了"},
    ]


def test_task_description_uses_front_chosen_task_text():
    from front import FrontBrief
    from work.controller import _description_from_brief

    raw = "我今天好无聊，请做一个贪吃蛇小游戏网页给我玩玩"
    brief = FrontBrief(
        goal="做一个贪吃蛇网页",
        human_text=raw,
        task_text=raw,
        deliverables=["snake.html"],
        constraints=["分模块"],
    )
    assert _description_from_brief(brief) == raw

    summarized = FrontBrief(
        goal="做一个贪吃蛇网页",
        human_text=raw,
        task_text="做一个贪吃蛇网页，分模块",
    )
    assert _description_from_brief(summarized) == (
        "做一个贪吃蛇网页，分模块")


def test_front_think_uses_minimal_system_context():
    class _Agent:
        has_brain = True

        def __init__(self):
            self.seen = []

        def think(self, prompt, **kwargs):
            self.seen.append(dict(kwargs))
            return "{}"

    agent = _Agent()
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: agent),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        front = controller._front_runtime()
        front.decide("hello")
    finally:
        controller.close()

    assert agent.seen
    assert all(item.get("cognitive") is False for item in agent.seen)
    assert agent.seen[0].get("system") == FRONT_SYSTEM_PROMPT


def test_task_detail_js_does_not_reuse_reply_as_thinking():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "task.js").read_text(encoding="utf-8")
    assert "seg.model_reasoning" in js
    assert "decisionReasoning.trim() !== content.trim()" in js


def test_controller_sets_front_thinking_to_low():
    class _Brain:
        def __init__(self):
            self.calls = []

        def set_thinking(self, enabled, effort=None, *, phase="work"):
            self.calls.append((enabled, effort, phase))

    agent = SimpleNamespace(has_brain=True, brain=_Brain())
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: agent),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        calls = list(agent.brain.calls)
    finally:
        controller.close()

    assert calls == [(True, "low", "decision")]


def test_task_round_log_appends_and_reads(tmp_path):
    from ui.rounds import TaskRoundLog

    log = TaskRoundLog(tmp_path / "rounds")
    log.append("task_1", {"round": 1})
    log.append("task_1", {"round": 2})

    assert log.read("task_1") == [
        {"round": 1}, {"round": 2}]
    assert log.read("task_missing") == []


def test_controller_round_log_roundtrip():
    class _Log:
        def __init__(self):
            self.items = []

        def append(self, _task_id, payload):
            self.items.append(payload)

        def read(self, _task_id):
            return list(self.items)

    log = _Log()
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", round_log=log)
    try:
        controller._record_round("task_1", {"round": 1, "summary": "ok"})
        rounds = controller._rounds("task_1")
    finally:
        controller.close()

    assert rounds == [{"round": 1, "summary": "ok"}]


def test_artifacts_export_writes_host_directory(tmp_path):
    from front import FrontDecision, FrontIntent
    from work.controller import WorkJob

    class _Gateway:
        def list(self, _ws, path, _agent):
            return SimpleNamespace(success=True, result={"entries": [
                {"name": "hello.py", "type": "file"},
            ] if path in ("", "/") else []})

        def read(self, _ws, path, _agent):
            return SimpleNamespace(
                success=True,
                result={"content": "print('hello')\n", "revision": 1})

    class _Task:
        task_id = "task_art"
        status = "COMPLETED"
        mode = "SOLO"
        iteration = 1
        creator = "agent_b"
        participants = ["agent_b"]
        workspace_id = "WS-ART"

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(
            get=lambda tid: _Task(),
            list_tasks=lambda limit=50: [],
        ),
        workspace_gateway=_Gateway(),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a",
        export_root=tmp_path / "exports")
    job = WorkJob(job_id="job_art", human_text="看看产物")
    decision = FrontDecision(
        intent=FrontIntent.ARTIFACTS, task_id="task_art")
    try:
        controller._export_artifacts(job, decision)
        messages = controller.state()["messages"]
    finally:
        controller.close()

    target = tmp_path / "exports" / "task_art"
    assert (target / "hello.py").read_text() == "print('hello')\n"
    assert job.status == "DIRECT_REPLY"
    assert job.task_id == "task_art"
    assert any(str(target.resolve()) in item["text"] for item in messages)


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


def test_preferred_agent_idle_is_acquired():
    from work.controller import WorkJob

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_e"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_pref", human_text="让 agent_e 做")
    try:
        worker = controller._acquire_preferred_worker(job, "agent_e")
    finally:
        controller.close()

    assert worker == "agent_e"


def test_preferred_agent_busy_rejects_handoff():
    from work.controller import WorkJob

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_e"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_busy", human_text="让 agent_e 做")
    try:
        controller.availability.try_acquire("agent_e")
        worker = controller._acquire_preferred_worker(job, "agent_e")
    finally:
        controller.close()

    assert worker is None
    assert job.status == "FAILED"
    assert "preferred_agent_busy" in job.error


def test_preferred_agent_unknown_rejects_handoff():
    from work.controller import WorkJob

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_unknown", human_text="让 agent_x 做")
    try:
        worker = controller._acquire_preferred_worker(job, "agent_x")
    finally:
        controller.close()

    assert worker is None
    assert job.status == "FAILED"
    assert "preferred_agent_unavailable" in job.error


def test_preferred_agent_cannot_be_front_agent():
    from work.controller import WorkJob

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_front", human_text="让 agent_a 做")
    try:
        worker = controller._acquire_preferred_worker(job, "agent_a")
    finally:
        controller.close()

    assert worker is None
    assert job.status == "FAILED"
    assert "preferred_agent_is_front" in job.error


def test_agent_facts_include_status_and_can_work():
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        facts = {item["agent_id"]: item for item in controller._agent_facts()}
    finally:
        controller.close()

    assert facts["agent_a"]["role"] == "FRONT"
    assert facts["agent_a"]["can_work"] is False
    assert facts["agent_b"]["role"] == "WORKER"
    assert facts["agent_b"]["status"] == "IDLE"


def test_timeout_marks_job_interrupted():
    from work.controller import WorkJob

    class _Front:
        def result_from_work(self, _result):
            return SimpleNamespace(summary="", status="IN_PROGRESS")

        def format_result(self, _result):
            return "interrupted"

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_timeout", human_text="x")
    result = SimpleNamespace(
        ok=False, error="LLMTimeoutError: 请求超时（300.0 秒）",
        tokens=0, turns=0, status="IN_PROGRESS", summary="")
    try:
        controller._finish_work(job, _Front(), "task_x", result)
    finally:
        controller.close()

    assert job.status == "INTERRUPTED"
    assert "请求超时" in job.error


def test_task_cards_mark_open_task_interrupted():
    task = SimpleNamespace(
        task_id="task_open", status="IN_PROGRESS", mode="SOLO",
        iteration=1, description="d", creator="agent_b",
        participants=["agent_b"], updated_at="", is_open=True)
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: [task]),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        card = controller._task_cards()[0]
    finally:
        controller.close()

    assert card["status"] == "INTERRUPTED"


def test_controller_checkpoint_roundtrip(tmp_path):
    from types import SimpleNamespace

    from ui.rounds import TaskRoundLog

    log = TaskRoundLog(tmp_path / "rounds")
    log.append("task_cp", {
        "round": 1,
        "run_id": "run_before",
        "summary": "已写完 index.html；Playwright 仍失败。",
        "segments": [{
            "actions": [{
                "action_type": "run_tool",
                "parameters": {"path": "tests/app.spec.js"},
            }],
            "results": [{
                "success": False,
                "error": 'Expected: "running" Received: "no-context"',
            }],
        }],
    })
    task = SimpleNamespace(
        task_id="task_cp", workspace_id="", creator="agent_b",
        participants=["agent_b"], iteration=2, status="IN_PROGRESS")
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(
            get=lambda tid: task,
            list_tasks=lambda limit=50: [],
        ),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", round_log=log)
    try:
        controller._write_checkpoint(
            "task_cp",
            SimpleNamespace(error="达到 max_turns=24，任务尚未完成",
                            summary=""))
        checkpoint = controller._read_checkpoint("task_cp")
    finally:
        controller.close()

    assert checkpoint is not None
    assert checkpoint.run_id == "run_before"
    assert checkpoint.iteration == 2
    assert checkpoint.status == "IN_PROGRESS"
    assert "max_turns=24" in checkpoint.error
    assert "已写完 index.html" in checkpoint.summary
    assert checkpoint.action_failures[0]["action"] == "run_tool"
    assert "no-context" in checkpoint.action_failures[0]["error"]
    assert (tmp_path / "checkpoints" / "task_cp.json").is_file()


def test_max_turns_marks_job_interrupted():
    from work.controller import WorkJob

    class _Front:
        def result_from_work(self, _result):
            return SimpleNamespace(summary="", status="IN_PROGRESS")

        def format_result(self, _result):
            return "interrupted"

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_max_turns", human_text="x")
    result = SimpleNamespace(
        ok=False, error="达到 max_turns=24，任务尚未完成",
        tokens=0, turns=24, status="IN_PROGRESS", summary="")
    try:
        controller._finish_work(job, _Front(), "task_x", result)
    finally:
        controller.close()

    assert job.status == "INTERRUPTED"
    assert "max_turns=24" in job.error


def test_task_detail_js_separates_resumed_runs():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "task.js").read_text(encoding="utf-8")
    assert "round.run_id" in js
    assert "run-divider" in js


def test_message_duration_is_serialized():
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        controller._message(
            "front", "耗时回复", job_id="job_1", duration_ms=1234,
            thinking={"enabled": True, "effort": "high"})
        controller._message("human", "没有耗时", job_id="job_1")
        messages = controller.state()["messages"]
    finally:
        controller.close()

    assert messages[0]["duration_ms"] == 1234
    assert messages[0]["thinking"] == {
        "enabled": True, "effort": "high"}
    assert messages[1]["duration_ms"] is None
    assert messages[1]["thinking"] is None


def test_job_duration_and_front_ms_are_exposed():
    from work.controller import WorkJob

    job = WorkJob(
        job_id="job_duration",
        human_text="x",
        status="COMPLETED",
        front_ms=850,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:02.500000+00:00",
    )

    data = job.to_dict()

    assert data["front_ms"] == 850
    assert data["duration_ms"] == 2500


def test_llm_summary_includes_latency():
    calls = {
        "calls": 3,
        "input_tokens": 100,
        "output_tokens": 50,
        "latency_ms": 30000.0,
        "cost_ai": 0.5,
    }
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
        recorder=SimpleNamespace(
            db=SimpleNamespace(fetchone=lambda *a, **k: calls)),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        summary = controller._llm_summary("task_x")
    finally:
        controller.close()

    assert summary["latency_ms"] == 30000.0
    assert summary["avg_latency_ms"] == 10000.0


def test_chat_js_renders_message_duration():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "message.duration_ms" in js
    assert "message.thinking" in js
    assert "msg-duration" in js
    assert "window.location.assign" in js
    assert "window.open(" not in js


def test_task_detail_js_renders_call_latency():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "task.js").read_text(encoding="utf-8")
    assert "seg.latency_ms" in js
    assert "seg.thinking" in js
    assert "round.latency_ms" in js
    assert "result.output.duration_ms" in js
    assert "avg_latency_ms" in js
