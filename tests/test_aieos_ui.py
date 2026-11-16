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
        self.submitted_repo = ""
        self.submitted_workdir = ""
        self.retried = []
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

    def submit(self, text, *, commit_repo="", workdir="",
               client_message_id=""):
        self.submitted.append(text)
        self.submitted_repo = commit_repo
        self.submitted_workdir = workdir
        self.submitted_client_message_id = client_message_id
        return "job_test"

    def workdir_info(self, path=""):
        target = path or "/tmp/aieos-project"
        return {
            "path": target,
            "exists": True,
            "is_dir": True,
            "error": "",
        }

    def commit_repo_info(self, path=""):
        target = path or "/tmp/aieos-output"
        return {
            "path": target,
            "exists": True,
            "is_dir": True,
            "is_git_repo": True,
            "git_root": target,
            "repo_mode": "worktree",
            "error": "",
        }

    def retry(self, job_id):
        self.retried.append(job_id)
        return "job_retry"

    def update_settings(self, *, front_agent=None, max_parallel_teams=None,
                        max_builtin_turns=None, max_solo_turns=None,
                        pi_work_agents=None,
                        disabled_agents=None, archived_agents=None,
                        prompt_log_enabled=None,
                        agent_thinking=None, pi_timeout_enabled=None,
                        pi_timeout_seconds=None, work_mode=None,
                        front_mode=None, **commit_settings):
        if front_agent:
            self.front = front_agent
            self.settings["front_agent"] = front_agent
        if max_parallel_teams is not None:
            self.settings["max_parallel_teams"] = max_parallel_teams
        if max_builtin_turns is not None or max_solo_turns is not None:
            self.settings["max_builtin_turns"] = (
                max_builtin_turns
                if max_builtin_turns is not None else max_solo_turns)
        if pi_work_agents is not None:
            self.settings["pi_work_agents"] = pi_work_agents
        if disabled_agents is not None:
            self.settings["disabled_agents"] = disabled_agents
        if archived_agents is not None:
            self.settings["archived_agents"] = archived_agents
        if prompt_log_enabled is not None:
            self.settings["prompt_log_enabled"] = prompt_log_enabled
        if agent_thinking is not None:
            self.settings["agent_thinking"] = agent_thinking
        if pi_timeout_enabled is not None:
            self.settings["pi_timeout_enabled"] = pi_timeout_enabled
        if pi_timeout_seconds is not None:
            self.settings["pi_timeout_seconds"] = pi_timeout_seconds
        return self.settings

    def settings_snapshot(self):
        return {
            "regular": self.settings,
            "system": {"providers": {}, "models": {},
                       "credentials": {}, "restart_required": False},
            "roles": {"roles": []},
            "agents": [],
        }

    def inbox_stats(self):
        return {"enabled": False, "agents": {
            "agent_a": {"agent_id": "agent_a", "pending": 0},
        }}

    def activities(self, agent_id, *, limit=50):
        if agent_id != "agent_a":
            raise ValueError(f"未知 Agent：{agent_id!r}")
        return {
            "agent_id": agent_id,
            "seen_at": "",
            "unread": 1,
            "activities": [{
                "run_id": "wake_1",
                "agent_id": agent_id,
                "outcome": "no_notify",
                "summary": "状态没有变化",
            }],
        }

    def mark_activities_seen(self, agent_id, *, run_id=""):
        return {"agent_id": agent_id, "seen_at": "now", "unread": 0}

    def activity_detail(self, run_id):
        if run_id != "wake_1":
            raise ValueError("活动记录不存在")
        return {"ok": True, "activity": {
            "run_id": "wake_1",
            "agent_id": "agent_a",
            "outcome": "no_notify",
            "summary": "状态没有变化",
            "detail": {"calls": [{
                "step": 1,
                "raw_output": '{"intent":"no_notify"}',
                "decisions": ["no_notify"],
                "actions": [],
                "receipt": "",
            }]},
        }}

    def agent_memory(self, agent_id):
        if agent_id != "agent_a":
            raise ValueError(f"未知 Agent：{agent_id!r}")
        return {
            "agent_id": agent_id,
            "short": {
                "revision": 1,
                "entries": [{
                    "entry_id": "stm_1",
                    "summary": "用户偏好简洁",
                    "importance": 4,
                }],
                "needs_compaction": False,
            },
            "long": [],
        }

    def delete_agent_memory(self, agent_id, *, kind, entry_id,
                            expected_revision=None):
        return {
            "agent_id": agent_id,
            "short": {"revision": 2, "entries": [],
                      "needs_compaction": False},
            "long": [],
        }

    def clear_agent_memory(self, agent_id):
        return {
            "agent_id": agent_id,
            "deleted": {"short_term": 1, "archived": 2, "long_term": 0},
            "memory": {
                "agent_id": agent_id,
                "short": {"revision": 0, "entries": [],
                          "needs_compaction": False},
                "long": [],
            },
        }

    def inbox_pending(self, agent_id, *, limit=50):
        if agent_id != "agent_a":
            raise ValueError(f"未知 Agent：{agent_id!r}")
        return {
            "agent_id": agent_id,
            "stats": {"agent_id": agent_id, "pending": 0},
            "messages": [],
        }

    def apply_system_settings(self, action, payload):
        return {"action": action, "payload": payload, "restart_required": True}

    def apply_worker_roles(self, action, payload):
        return {"action": action, "payload": payload, "roles": []}

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
    status, payload = _post(
        base + "/api/submit",
        {"text": "做个游戏", "client_message_id": "client-1"},
    )
    assert status == 200
    assert payload["ok"] is True
    assert fake.submitted == ["做个游戏"]
    assert fake.submitted_client_message_id == "client-1"

    status, payload = _post(base + "/api/settings",
                            {"front_agent": "agent_b"})
    assert status == 200
    assert payload["settings"]["front_agent"] == "agent_b"

    # 归档列表必须真的穿过 HTTP 层进入 controller；
    # 之前漏了这个字段，导致「归档」只改了前端草稿。
    status, payload = _post(base + "/api/settings",
                            {"archived_agents": ["agent_a"]})
    assert status == 200
    assert payload["settings"]["archived_agents"] == ["agent_a"]

    status, payload = _post(base + "/api/retry", {"job_id": "job_failed"})
    assert status == 200
    assert payload["job_id"] == "job_retry"
    assert fake.retried == ["job_failed"]


def test_settings_snapshot_and_system_roles_api(server):
    fake, base = server
    status, body = _get(base + "/api/settings")
    assert status == 200
    assert json.loads(body)["regular"]["front_agent"] == "agent_a"

    status, payload = _post(base + "/api/settings/system", {
        "action": "provider.upsert",
        "data": {"name": "x"},
    })
    assert status == 200
    assert payload["system"]["action"] == "provider.upsert"

    status, payload = _post(base + "/api/settings/roles", {
        "action": "upsert",
        "data": {"role_id": "x"},
    })
    assert status == 200
    assert payload["roles"]["action"] == "upsert"


def test_inbox_stats_and_pending_api(server):
    _fake, base = server
    status, body = _get(base + "/api/inbox/stats")
    assert status == 200
    payload = json.loads(body)
    assert payload["enabled"] is False
    assert "agent_a" in payload["agents"]

    status, body = _get(base + "/api/inbox/agent_a?limit=10")
    assert status == 200
    payload = json.loads(body)
    assert payload["agent_id"] == "agent_a"
    assert payload["messages"] == []

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(base + "/api/inbox/no_such_agent")
    assert excinfo.value.code == 404


def test_activities_api(server):
    _fake, base = server
    status, body = _get(base + "/api/activities?agent_id=agent_a&limit=10")
    assert status == 200
    payload = json.loads(body)
    assert payload["agent_id"] == "agent_a"
    assert payload["unread"] == 1
    assert payload["activities"][0]["outcome"] == "no_notify"

    status, payload = _post(base + "/api/activities/seen", {
        "agent_id": "agent_a",
    })
    assert status == 200
    assert payload["ok"] is True
    assert payload["unread"] == 0

    status, body = _get(base + "/api/activities/wake_1")
    assert status == 200
    detail = json.loads(body)
    assert detail["ok"] is True
    assert detail["activity"]["detail"]["calls"][0]["step"] == 1


def test_activity_detail_missing(server):
    _fake, base = server
    try:
        _get(base + "/api/activities/wake_missing")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("未知 run_id 应返回 404")


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


def test_controller_archives_agent_and_forces_disable():
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        controller.update_settings(archived_agents=["agent_b"])
        assert "agent_b" in controller.archived_agents
        assert "agent_b" in controller.disabled_agents
        facts = {
            item["agent_id"]: item
            for item in controller.settings_snapshot()["agents"]
        }
        assert facts["agent_b"]["archived"] is True
        assert facts["agent_b"]["enabled"] is False

        with pytest.raises(ValueError):
            controller.update_settings(archived_agents=["agent_a"])
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

    class _Builtin:
        def __init__(self, _rt, task, **_kwargs):
            seen["runner"] = "builtin"
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
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
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
    monkeypatch.setattr(controller_module, "BuiltinWorkRuntime", _Builtin)
    monkeypatch.setattr(controller_module, "TeamWorkRuntime", _Team)

    job = WorkJob(job_id="job_solo", human_text="写一个 hello.py")
    try:
        controller._run_job(job)
    finally:
        controller.close()

    assert seen["members"] == []
    assert seen["mode"] == "SOLO"
    assert seen["runner"] == "builtin"
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
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
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

    class _Builtin:
        def __init__(self, _rt, task_, **kwargs):
            seen["runner"] = "builtin"
            seen["task_id"] = task_.task_id
            seen["max_turns"] = kwargs.get("max_turns")
            seen["cognitive_budget"] = kwargs.get("cognitive_budget")

        def run(self):
            return SimpleNamespace(ok=True, tokens=2, turns=1)

    monkeypatch.setattr(controller_module, "BuiltinWorkRuntime", _Builtin)
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
        "runner": "builtin",
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
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
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
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
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


def test_rework_uses_front_instruction_and_preserves_provenance(monkeypatch):
    import work.controller as controller_module
    from front import FrontBrief, FrontDecision, FrontIntent, FrontResult
    from work.controller import WorkJob

    seen = {}

    class _Front:
        def decide(self, _text, active_tasks=(), completed_tasks=(),
                   conversation=(), **kwargs):
            return FrontDecision(
                intent=FrontIntent.REWORK,
                task_id="task_done",
                brief=FrontBrief(
                    goal="把格子改成正方形",
                    human_text="格子看起来有点变形",
                    task_text="把格子改成正方形",
                    front_notes="建议保持竖屏；这只是建议。",
                ),
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
        description="做一个静态网页", iteration=1, acceptance={},
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
        def __init__(self, _rt, task_, **kwargs):
            self.task = task_
            seen["brief"] = kwargs.get("brief")

        def reopen_and_run(self, *, reason=""):
            seen["reason"] = reason
            return SimpleNamespace(ok=True, tokens=5, turns=2)

    controller = AIEOSController(
        runtime, front_agent="agent_a", solo_only=False)
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
    monkeypatch.setattr(controller_module, "TeamWorkRuntime", _Team)

    job = WorkJob(
        job_id="job_rework", human_text="格子看起来有点变形")
    try:
        controller._run_job(job)
    finally:
        controller.close()

    assert seen["reason"] == "把格子改成正方形"
    assert "返工要求（第 2 轮）" in task.description
    assert "把格子改成正方形" in task.description
    assert task.acceptance["brief"]["human_text"] == "格子看起来有点变形"
    assert task.acceptance["brief"]["front_notes"] == "建议保持竖屏；这只是建议。"
    assert seen["brief"]["task_text"] == "把格子改成正方形"
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
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
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
            return '{"intent":"direct_reply","reply":"好"}'

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


def test_task_detail_js_shows_only_task_text():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "task.js").read_text(encoding="utf-8")
    assert "Front → Work · task_text" in js
    assert "task.description" in js
    assert "data.brief" not in js
    assert "acceptance_expectations" not in js


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
            has_brain=True,
            name={"agent_a": "小艾", "agent_b": "小博"}.get(aid, aid))),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        facts = {item["agent_id"]: item for item in controller._agent_facts()}
    finally:
        controller.close()

    assert facts["agent_a"]["role"] == "FRONT"
    assert facts["agent_a"]["name"] == "小艾"
    assert facts["agent_a"]["can_work"] is False
    assert facts["agent_b"]["role"] == "WORKER"
    assert facts["agent_b"]["name"] == "小博"
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


def test_work_completion_message_is_system_not_front():
    from front import FrontResult
    from work.controller import WorkJob

    class _Front:
        def result_from_work(self, _result):
            return FrontResult(
                task_id="task_x", status="COMPLETED", summary="done")

        def format_result(self, result):
            return result.to_human_text()

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_done", human_text="x")
    result = SimpleNamespace(
        ok=True, error="", tokens=3, turns=1, status="COMPLETED",
        summary="done")
    try:
        controller._finish_work(job, _Front(), "task_x", result)
        message = controller.state()["messages"][-1]
    finally:
        controller.close()

    assert message["role"] == "system"
    assert "任务 task_x：COMPLETED（第 1 轮）" in message["text"]


def test_task_cards_mark_open_task_interrupted():
    task = SimpleNamespace(
        task_id="task_open", status="IN_PROGRESS", mode="SOLO",
        iteration=1, description="d", creator="agent_b",
        participants=["agent_b"], updated_at="", is_open=True,
        acceptance={"front_mode": "relay", "work_mode": "autonomous"})
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
    assert card["front_mode"] == "relay"
    assert card["work_mode"] == "autonomous"


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
    assert "message.retryable" in js
    assert "/api/retry" in js
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


def test_task_detail_js_renders_cache_ratio():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "task.js").read_text(encoding="utf-8")
    assert "cache_hit_rate" in js
    assert "cache_read_tokens" in js
    assert "缓存命中" in js


def test_llm_summary_includes_cache_ratio():
    calls = {
        "calls": 2,
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_tokens": 900,
        "cache_write_tokens": 0,
        "latency_ms": 2000.0,
        "cost_ai": 0.5,
        "allowance_usd": 0.00002,
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

    assert summary["cache_read_tokens"] == 900
    assert summary["cache_hit_rate"] == 0.9
    assert summary["allowance_usd"] == 0.00002


def test_handoff_sends_front_reply(monkeypatch, tmp_path):
    from front import FrontBrief, FrontDecision, FrontIntent
    from work.controller import WorkJob
    import work.controller as controller_module

    class _FakeRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                ok=True, tokens=1, turns=1, status="COMPLETED",
                summary="done", error="")

    monkeypatch.setattr(controller_module, "BuiltinWorkRuntime", _FakeRunner)

    task = SimpleNamespace(task_id="task_x", status="IN_PROGRESS")
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(
            get=lambda tid: task,
            list_tasks=lambda limit=50: [],
        ),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", log_dir=tmp_path)
    controller._acquire_worker = lambda job: "agent_b"
    controller._create_solo_task = (
        lambda job, worker, decision: {"ok": True, "task_id": "task_x"})
    controller._bind_task_to_agents = lambda agents, tid: None

    class _Front:
        def result_from_work(self, _result):
            return SimpleNamespace(summary="done", status="COMPLETED")

        def format_result(self, _result):
            return "result"

    job = WorkJob(job_id="job_handoff", human_text="x")
    decision = FrontDecision(
        intent=FrontIntent.HANDOFF,
        reply="我来安排后台执行。",
        brief=FrontBrief(goal="g", task_text="t"),
    )
    try:
        controller._run_handoff_solo(job, decision, _Front())
        messages = controller.state()["messages"]
    finally:
        controller.close()

    assert any(
        item["role"] == "front"
        and item["text"] == "我来安排后台执行。"
        for item in messages)


def test_resolve_brief_files_reads_private_and_drafts():
    from front import FrontBrief

    class _Workspace:
        def read_text(self, path):
            assert path == "src/app.js"
            return "PRIVATE-CODE"

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None, workspace=_Workspace())),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    controller._front_drafts_store = SimpleNamespace(
        read=lambda aid, did: {"ok": True, "content": "DRAFT-CODE"})
    brief = FrontBrief(
        goal="g",
        files=[
            {"path": "a.js", "from_private": "src/app.js"},
            {"path": "b.js", "draft_id": "draft_1"},
            {"path": "c.js", "content": "INLINE"},
        ],
    )
    try:
        controller._resolve_brief_files(brief)
    finally:
        controller.close()

    assert brief.files == [
        {"path": "a.js", "content": "PRIVATE-CODE"},
        {"path": "b.js", "content": "DRAFT-CODE"},
        {"path": "c.js", "content": "INLINE"},
    ]


def test_front_failure_is_retryable_and_retry_reuses_text():
    from work.controller import WorkJob

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(
        job_id="job_front_fail", human_text="原始需求",
        front_ms=12, front_thinking={"enabled": True, "effort": "low"})
    controller._jobs[job.job_id] = job
    try:
        controller._interrupt_front(job, "network down")
        message = controller.state()["messages"][-1]
        controller.submit = lambda text: f"new:{text}"
        retried = controller.retry(job.job_id)
    finally:
        controller.close()

    assert job.status == "INTERRUPTED"
    assert job.retryable is True
    assert message["retryable"] is True
    assert "没有创建任务" in message["text"]
    assert retried == "new:原始需求"


def test_job_log_contains_front_decision_and_raw_output():
    from work.controller import WorkJob

    job = WorkJob(
        job_id="job_log",
        human_text="x",
        front_decision={
            "intent": "handoff",
            "task_text": "TEXT",
            "brief": {"goal": "g"},
            "raw_output": "RAW-FRONT-OUTPUT",
        },
        front_actions=[{"step": 1, "actions": [], "receipt": "ok"}],
    )

    state_data = job.to_dict()
    log_data = job.to_log_dict()

    assert "front_decision" not in state_data
    assert log_data["front_decision"]["task_text"] == "TEXT"
    assert log_data["front_decision"]["raw_output"] == "RAW-FRONT-OUTPUT"
    assert log_data["front_actions"][0]["receipt"] == "ok"


def test_job_log_file_contains_front_raw_output(tmp_path):
    from work.controller import WorkJob

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", log_dir=tmp_path)
    job = WorkJob(
        job_id="job_raw_log", human_text="x",
        front_decision={
            "task_text": "TEXT",
            "raw_output": "RAW-FRONT",
            "brief": {"goal": "g"},
        })
    controller._jobs[job.job_id] = job
    try:
        controller._write_job_log(job)
    finally:
        controller.close()

    data = json.loads(
        (tmp_path / "job_raw_log.json").read_text(encoding="utf-8"))
    assert data["job"]["front_decision"]["raw_output"] == "RAW-FRONT"
    assert data["job"]["front_decision"]["task_text"] == "TEXT"


def test_main_layout_keeps_messages_scrollable_and_composer_visible():
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent
           / "ui" / "static" / "style.css").read_text(encoding="utf-8")
    assert "height: 100dvh" in css
    assert "overflow: hidden" in css
    assert "flex: 1 1 auto" in css
    assert "min-height: 0" in css
    assert "overflow-y: auto" in css
    assert "resize: none" in css
    # 移动端必须让聊天面板占满首屏，而不是和侧栏平分高度。
    assert "flex: 0 0 100%" in css


def test_multiple_cancel_actions_in_one_turn(monkeypatch):
    from front import FrontDecision, FrontIntent, FrontTurn
    from work.controller import WorkJob

    tasks = {
        "task_a": SimpleNamespace(
            task_id="task_a", status="IN_PROGRESS", is_open=True),
        "task_b": SimpleNamespace(
            task_id="task_b", status="IN_PROGRESS", is_open=True),
    }
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(
            get=lambda tid: tasks.get(tid),
            list_tasks=lambda limit=50: [],
        ),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    controller._mark_task_cancelled = lambda task, reason: True

    class _Front:
        def decide_turn(self, _text, **_kwargs):
            return FrontTurn(decisions=[
                FrontDecision(
                    intent=FrontIntent.CANCEL, task_id="task_a",
                    reply="A 已停"),
                FrontDecision(
                    intent=FrontIntent.CANCEL, task_id="task_b",
                    reply="B 已停"),
            ])

    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: _Front())
    job = WorkJob(job_id="job_multi_cancel", human_text="两个都停掉")
    controller._jobs[job.job_id] = job
    try:
        controller._run_job(job)
        messages = controller.state()["messages"]
    finally:
        controller.close()

    front_texts = [
        item["text"] for item in messages if item["role"] == "front"]
    assert front_texts == ["A 已停", "B 已停"]
    assert job.front_decision["decisions"][0]["intent"] == "cancel"
    assert len(job.front_decision["decisions"]) == 2


def test_front_parse_failure_falls_back_to_clarify_and_records_attempts(
    monkeypatch,
):
    from front import FrontAgentRuntime
    from work.controller import WorkJob

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    front = FrontAgentRuntime(
        "agent_a", lambda _prompt: "not json", think_retries=1)
    monkeypatch.setattr(
        controller, "_front_runtime", lambda *_args, **_kwargs: front)
    job = WorkJob(
        job_id="job_parse_fail", human_text="语音输入有点乱")
    controller._jobs[job.job_id] = job
    try:
        controller._run_job(job)
        messages = controller.state()["messages"]
    finally:
        controller.close()

    assert job.status == "CLARIFY"
    assert job.front_decision["decisions"][0]["intent"] == "clarify"
    assert len(job.front_decision["attempts"]) == 2
    assert "没有把输入解析成明确任务" in (
        job.front_decision["decisions"][0]["question"])
    assert any(
        "没有把输入解析成明确任务" in (item.get("text") or "")
        for item in messages)


def test_follow_up_sends_front_reply(monkeypatch):
    from front import FrontBrief, FrontDecision, FrontIntent
    from work.controller import WorkJob
    import work.controller as controller_module

    class _FakeRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                ok=True, tokens=1, turns=1, status="COMPLETED",
                summary="done", error="")

    monkeypatch.setattr(controller_module, "BuiltinWorkRuntime", _FakeRunner)
    task = SimpleNamespace(
        task_id="task_follow", status="IN_PROGRESS", mode="SOLO",
        is_open=True, creator="agent_b", participants=["agent_b"],
        workspace_id="WS", iteration=1, description="d")
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(
            get=lambda tid: task,
            list_tasks=lambda limit=50: [],
        ),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    job = WorkJob(job_id="job_follow", human_text="继续")
    controller._jobs[job.job_id] = job

    class _Front:
        def result_from_work(self, _result):
            return SimpleNamespace(summary="done", status="COMPLETED")

        def format_result(self, _result):
            return "result"

    decision = FrontDecision(
        intent=FrontIntent.FOLLOW_UP,
        task_id="task_follow",
        brief=FrontBrief(
            goal="继续修竖屏",
            human_text="继续",
            task_text="继续修竖屏",
            front_notes="先看 ui.js 的格子比例。",
        ),
        reply="我继续把它做完。",
    )
    try:
        controller._resume_or_rework(job, decision, _Front())
        messages = controller.state()["messages"]
    finally:
        controller.close()

    assert any(
        item["role"] == "front"
        and item["text"] == "我继续把它做完。"
        for item in messages)
    assert "续跑要求：" in task.description
    assert "继续修竖屏" in task.description
    assert task.acceptance["brief"]["human_text"] == "继续"
    assert task.acceptance["brief"]["front_notes"] == "先看 ui.js 的格子比例。"


def test_front_artifact_read_uses_task_workspace(monkeypatch):
    import work.controller as controller_module

    task = SimpleNamespace(
        task_id="task_x", workspace_id="WS-X", creator="agent_b",
        participants=["agent_b"], status="COMPLETED")
    gateway = object()
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(
            get=lambda tid: task if tid == "task_x" else None,
            list_tasks=lambda limit=50: [],
        ),
        workspace_gateway=gateway,
    )
    calls = {}

    def fake_read(_gateway, workspace_id, agent_id, path, *,
                  start_line=None, max_lines=None):
        calls.update({
            "gateway": _gateway, "workspace_id": workspace_id,
            "agent_id": agent_id, "path": path,
            "start_line": start_line, "max_lines": max_lines,
        })
        return {
            "ok": True, "path": path, "content": "line1\nline2",
            "revision": 1, "chars": 11, "total_lines": 2,
        }

    monkeypatch.setattr(controller_module, "read_artifact", fake_read)
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        result = controller._front_artifact_read(
            "task_x", "index.html", start_line=1, max_lines=2)
        missing = controller._front_artifact_read(
            "task_missing", "index.html")
    finally:
        controller.close()

    assert result["ok"] is True
    assert result["task_id"] == "task_x"
    assert result["read_as"] == "agent_b"
    assert calls == {
        "gateway": gateway, "workspace_id": "WS-X",
        "agent_id": "agent_b", "path": "index.html",
        "start_line": 1, "max_lines": 2,
    }
    assert missing["ok"] is False
    assert missing["code"] == "not_found"


def test_front_runtime_exposes_artifact_read():
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None, workspace=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        front = controller._front_runtime()
    finally:
        controller.close()

    assert "artifact_read" in front.action_types


def test_front_runtime_exposes_project_actions(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text(
        "<html><head><title>Demo</title></head></html>", encoding="utf-8")
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None, workspace=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        front = controller._front_runtime(str(project))
    finally:
        controller.close()

    assert "project_list" in front.action_types
    assert "project_read" in front.action_types


def test_front_turn_passes_selected_project(tmp_path):
    from front import FrontDecision, FrontIntent, FrontTurn
    from work.controller import WorkJob

    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text(
        "<html><head><title>Demo</title></head></html>", encoding="utf-8")
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None, workspace=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", front_max_inspect=6)

    class _Front:
        def __init__(self):
            self.kwargs = {}

        def decide_turn(self, _text, **kwargs):
            self.kwargs = kwargs
            return FrontTurn(decisions=[
                FrontDecision(
                    intent=FrontIntent.DIRECT_REPLY, reply="好")])

    front = _Front()
    job = WorkJob(
        job_id="job_project", human_text="把这个项目里的网页换个风格",
        workdir=str(project))
    try:
        controller._front_turn(front, job, [], [])
    finally:
        controller.close()

    context = front.kwargs["project_context"]
    assert str(project.resolve()) in context
    assert "index.html" in context
    assert "Demo" in context
    assert front.kwargs["max_inspect"] == 6


def test_pi_task_creation_skips_shared_context(db):
    from core.task_store import TaskStore
    from front import FrontBrief, FrontDecision, FrontIntent

    store = TaskStore(db)
    alignment_calls = []

    class _Alignment:
        def current(self, task_id):
            alignment_calls.append(task_id)
            return SimpleNamespace(context=SimpleNamespace(
                acceptance_criteria=()))

    class _Budget:
        def set_budget(self, task_id, amount):
            return None

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=store,
        alignment=_Alignment(),
        budget_service=_Budget(),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", pi_work_agents=["agent_c"])
    decision = FrontDecision(
        intent=FrontIntent.HANDOFF,
        role="game_developer",
        work_profile="creative_product",
        brief=FrontBrief(
            goal="写 hello.txt",
            task_text="写 hello.txt",
            deliverables=["hello.txt"],
        ),
    )
    try:
        created = controller._create_solo_task(
            SimpleNamespace(job_id="job_pi"), "agent_c", decision)
    finally:
        controller.close()

    assert created["ok"] is True, created
    task = store.get(created["task_id"])
    assert task.creator == "agent_c"
    assert task.workspace_id is None
    assert task.acceptance["role"] == "game_developer"
    assert task.acceptance["work_profile"] == "creative_product"
    assert alignment_calls == []


def test_task_detail_keeps_brief_out_of_public_facts(db):
    from core.task_store import TaskStore
    from front import FrontBrief, FrontDecision, FrontIntent

    store = TaskStore(db)
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=store,
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", pi_work_agents=["agent_c"])
    # 即使旧调用方仍塞入结构化字段，Controller 也只使用 task_text。
    decision = FrontDecision(
        intent=FrontIntent.HANDOFF,
        role="generalist",
        work_profile="standard",
        brief=FrontBrief(
            goal="写 hello.txt",
            task_text="写 hello.txt",
            deliverables=["hello.txt"],
            constraints=["无第三方依赖"],
            acceptance_expectations=["文件存在"],
        ),
    )
    try:
        created = controller._create_solo_task(
            SimpleNamespace(job_id="job_pi"), "agent_c", decision)
        assert created["ok"] is True, created
        detail = controller.task_detail(created["task_id"])
        task = store.get(created["task_id"])
    finally:
        controller.close()

    # Brief 作为**来源分层**持久化在 Task.acceptance 中；
    # 它不属于 task_detail 对外的公共事实。
    assert task.acceptance["brief"]["task_text"] == "写 hello.txt"
    assert task.acceptance["criteria"] == ["写 hello.txt"]
    assert "brief" not in detail
    assert "criteria" not in detail["task"]
    assert detail["task"]["role"] == "generalist"
    assert detail["task"]["work_profile"] == "standard"


def test_pi_task_artifact_read_reads_disk(tmp_path):
    from work.task_directory import TaskDirectoryManager

    directory = TaskDirectoryManager(tmp_path).for_task("task_pi_disk")
    directory.ensure()
    directory.write_text("index.html", "<html>pi</html>")
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(
            get=lambda tid: SimpleNamespace(
                task_id=tid, creator="agent_c",
                participants=["agent_c"], workspace_id=None),
            list_tasks=lambda limit=50: [],
        ),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", task_root=tmp_path)
    try:
        result = controller._front_artifact_read(
            "task_pi_disk", "index.html")
        paged = controller._front_artifact_read(
            "task_pi_disk", "index.html", start_line=1, max_lines=1)
    finally:
        controller.close()

    assert result["ok"] is True
    assert result["content"] == "<html>pi</html>"
    assert result["read_as"] == "pi_task_directory"
    assert paged["ok"] is True
    assert paged["total_lines"] == 1
    assert paged["content"] == "<html>pi</html>"
    assert paged["start_line"] == 1
    assert paged["max_lines"] == 1
    assert paged["next_start_line"] is None


def test_chat_polling_does_not_clear_text_selection():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "hasActiveTextSelection" in js
    assert "selectstart" in js
    assert "pointerSelecting" in js
    assert "lastMessagesKey" in js
    assert "if (hasActiveTextSelection()) return;" in js
    # 选择 Task 卡片文字时不能触发整页跳转。
    assert "if (hasActiveTextSelection()) return;" in js
    assert "window.location.assign" in js


def test_task_detail_polling_does_not_clear_text_selection():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    js = (static / "task.js").read_text(encoding="utf-8")
    assert "hasActiveTextSelection" in js
    assert "selectstart" in js
    assert "pointerSelecting" in js
    assert "if (signature !== lastSignature && !selecting)" in js
    assert "if (!selecting && (jobEnded" in js


def test_styles_keep_copy_actions_from_blocking_selection():
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent
           / "ui" / "static" / "style.css").read_text(encoding="utf-8")
    assert "::selection" in css
    assert "pointer-events: none" in css
    assert ".msg-actions .copy-btn { pointer-events: auto; }" in css


def test_task_cards_have_status_colors_filters_and_actions():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="task-filters"' in html
    assert 'data-task-filter="archived"' in html
    assert "task-card-menu" in js
    assert "/api/task/archive" in js
    assert "/api/task/delete" in js
    assert ".task-card.COMPLETED" in css
    assert ".task-card.IN_PROGRESS" in css
    assert ".task-status.COMPLETED" in css


def test_danger_actions_use_reusable_confirm_dialog():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")

    # 统一组件，而不是浏览器原生 confirm()
    assert 'id="confirm-modal"' in html
    assert 'role="alertdialog"' in html
    assert 'aria-modal="true"' in html
    assert 'id="confirm-cancel"' in html
    assert 'id="confirm-ok"' in html
    assert "function confirmDialog(" in js
    assert "window.confirm" not in js
    # 危险操作用 danger tone 并带目标 Task + 状态
    assert 'tone: "danger"' in js
    assert "target: taskId" in js
    assert "detailsTitle" in js
    # 默认焦点在取消；Esc 取消；遮罩取消
    assert "els.confirmCancel.focus()" in js
    assert 'event.key === "Escape"' in js
    assert "if (event.target === els.confirmModal) finishConfirm(false);" in js
    # 三种 tone 的样式
    assert ".confirm-modal.danger" in css
    assert ".confirm-modal.warning" in css
    assert ".cd-btn.danger" in css


def test_chat_header_and_messages_are_compact():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")

    # 顶部拓扑：Human ↔ Front ↔ Worker
    assert "Human ↔ Front ↔ Worker" in html
    assert "Solo Agent" not in html

    # 消息已有颜色 / 位置区分，不再重复「你」「Front Agent」标签
    assert 'message.role === "human" ? "你"' not in js
    assert 'message.role === "front" ? "Front Agent"' not in js
    assert 'message.role === "task" ? "Task log"' in js

    # 人类提交后强制滚动到底部
    assert "forceMessagesScroll = true" in js
    assert "scrollMessagesToBottom()" in js


def test_submit_uses_client_message_id_for_idempotency():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "client_message_id" in js
    assert "aieos.pendingSubmit" in js
    assert "rememberPendingSubmission" in js
    assert "clearPendingSubmission" in js


def test_composer_has_voice_input_button_before_workspace_chip():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent
            / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="mic-btn"' in html
    assert 'aria-label="语音输入"' in html
    assert html.index('id="mic-btn"') < html.index('id="workspace-chip"')


def test_voice_input_uses_browser_speech_recognition():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "webkitSpeechRecognition" in js
    assert "recognition.lang = \"zh-CN\"" in js
    assert "not-allowed" in js
    assert "MIC_PLATFORM_SUPPORTED" in js
    # 提交后浏览器可能补发最后一次 result，不能写回已清空的输入框。
    assert "micDiscardResults" in js
    assert "if (micDiscardResults) return;" in js
    assert "stopMic({ discard: true })" in js
    assert "recognition.abort()" in js


def test_voice_input_is_hidden_for_now():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const VOICE_INPUT_ENABLED = false;" in js
    assert "els.mic.hidden = true;" in js


def test_platform_compatibility_documents_voice_input():
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent
           / "docs" / "architecture" / "PLATFORM_COMPATIBILITY.md"
           ).read_text(encoding="utf-8")
    assert "语音输入" in doc
    assert "Windows" in doc


def test_settings_page_has_prompt_log_toggle():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "settings.js").read_text(encoding="utf-8")
    assert "prompt-log-enabled" in html
    assert "prompt_log_enabled" in js
    assert "runs/ui/prompts/" in html


def test_provider_form_has_safe_defaults_and_opencode_preset():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "settings.js").read_text(encoding="utf-8")

    assert 'id="provider-preset"' in html
    assert "OpenCode Zen" in html
    assert "OpenCode Go" in html
    assert "https://opencode.ai/inference/openai/v1" in js

    start = js.index("function newProvider()")
    end = js.index("async function saveProvider()", start)
    block = js[start:end]
    # 新建服务商默认用通用 OpenAI 兼容 adapter，而不是排序第一的 bailian。
    assert '"openai_compatible"' in block
    # 新建时不能把所有模型都列进「默认模型」。
    assert "Object.keys((data.system.models" not in block
    assert 'option($("provider-default-model"), "", "(无)")' in block


def test_settings_provider_preview_is_available():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "settings_provider_preview.html").read_text(
        encoding="utf-8")

    assert "服务商与模型" in html
    assert 'id="provider-list"' in html
    assert 'id="provider-detail"' in html
    assert "https://opencode.ai/inference/go/openai/v1" in html
    # 预览只读，不应写入真实配置。
    assert "不会写入" in html


def test_settings_provider_and_model_are_one_page():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "settings.js").read_text(encoding="utf-8")

    assert 'data-pane="system-models"' not in html
    assert 'data-pane="system-providers"' in html
    assert "服务商与模型" in html
    assert 'id="provider-list"' in html
    assert 'id="model-list"' in html
    assert 'id="model-new"' in html
    assert 'id="model-save"' in html
    assert 'id="provider-model-hint"' in html
    assert "model-provider-name" in html

    assert "function renderModels()" in js
    assert "function hideModelEditor()" in js
    assert 'action: "model.upsert"' in js
    assert "await load(editing.provider)" in js


def test_model_editor_exposes_tool_call_format():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "settings.js").read_text(encoding="utf-8")

    assert 'id="model-tool-call-format"' in html
    assert "标准 OpenAI tool_calls" in html
    assert "DeepSeek DSML" in html

    start = js.index("function modelSnapshot()")
    end = js.index("function credentialSnapshot()", start)
    snapshot = js[start:end]
    assert "tool_call_format" in snapshot

    start = js.index("function editModel(profile)")
    end = js.index("function newModel()", start)
    edit = js[start:end]
    assert "model-tool-call-format" in edit
    assert "raw.tool_call_format || defaultToolCallFormat(provider)" in edit
    assert 'raw.adapter === "deepseek" ? "dsml" : "standard"' in js

    start = js.index("async function saveModel()")
    end = js.index("async function deleteModel(", start)
    save = js[start:end]
    assert "tool_call_format" in save


def test_agent_editor_exposes_life_wake_settings():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "settings.js").read_text(encoding="utf-8")

    for field in (
        "agent-life-enabled",
        "agent-life-wake-on-message",
        "agent-life-wake-on-alarm",
        "agent-life-accept-delegated",
        "agent-life-max-turns",
        "agent-life-min-interval",
        "agent-life-max-tokens",
        "agent-life-max-cost",
        "agent-life-max-conversation",
    ):
        assert f'id="{field}"' in html
    assert "Life 唤醒" in html
    assert "接受 Front 的委托闹钟" in html

    assert "function agentLifeSettings" in js
    assert "agent-life-accept-delegated" in js
    assert "lifeChanged" in js
    assert "update.life = life" in js


def test_agent_question_bubble_and_answer_ui():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert "question-modal" in html
    assert "agent-question-bubble" in js
    assert "/api/answer" in js
    assert "playQuestionSound" in js


def test_settings_page_has_pi_timeout_switch():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "settings.js").read_text(encoding="utf-8")
    assert "pi-timeout-enabled" in html
    assert "pi-timeout-seconds" in html
    assert "pi_timeout_enabled" in js


def test_settings_pages_use_cards_and_agent_master_detail():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "settings.js").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    app_js = (static / "app.js").read_text(encoding="utf-8")

    runtime_start = html.index(
        '<section class="settings-pane" data-pane="regular-runtime"')
    agent_start = html.index(
        '<section class="settings-pane" data-pane="regular-agent"')
    roles_start = html.index(
        '<section class="settings-pane" data-pane="regular-roles"')
    runtime_section = html[runtime_start:agent_start]
    agent_section = html[agent_start:roles_start]

    # 协作方式只在 Agent 页；运行页不再出现 Front / Pi 工作模式。
    assert 'id="front-select"' not in runtime_section
    assert 'id="front-mode"' not in runtime_section
    assert 'id="work-mode"' not in runtime_section
    assert 'id="front-select"' in agent_section
    assert 'id="front-mode"' in agent_section
    assert 'id="work-mode"' in agent_section

    # Agent 页是主从编辑：左侧列表 + 右侧选中 Agent 的详情。
    assert 'id="agent-settings-list"' in agent_section
    assert 'id="agent-detail-id"' in agent_section
    assert 'id="agent-detail-model"' in agent_section
    assert 'id="agent-detail-provider"' in agent_section
    assert 'id="agent-detail-credential"' in agent_section
    assert 'id="new-agent-provider"' in agent_section
    assert 'id="agent-create-new"' in agent_section
    assert 'id="agent-create-card"' in agent_section
    assert 'id="agent-archived-toggle"' in agent_section
    assert 'id="agent-archive-btn"' in agent_section

    # 统一卡片 / 页脚保存。
    assert 'id="settings-footer-state"' in html
    assert 'id="settings-save"' in html
    assert ".setting-card" in css
    assert ".agent-list-item" in css
    assert ".settings-footer" in css

    # Agent 新增 / 归档走独立动作；保存时归档也会提交。
    assert "agent.create" in js
    assert "agent.update" in js
    assert "archived_agents" in js
    assert "fillModelOptions" in js
    assert "agent-detail-provider" in js
    assert "function createAgent(" in js
    assert "function archiveSelectedAgent(" in js
    assert 'await saveAgents("Agent 已归档并保存")' in js
    assert "agent.name" in js

    # 保存职责拆分：协作字段跟 Agent 走，不再混进运行设置。
    runtime_save = js[
        js.index("async function saveRuntime"):
        js.index("// Agent（主从编辑")
    ]
    assert "front_agent" not in runtime_save
    assert "front_mode" not in runtime_save
    assert "work_mode" not in runtime_save
    assert "front_agent: agentDraft.frontAgent" in js

    # 危险操作复用统一确认弹窗。
    assert "window.aieosConfirm = confirmDialog" in app_js
    assert "window.aieosConfirm" in js


def test_task_timeline_preserves_manual_open_state():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "ui" / "static" / "task.js").read_text(encoding="utf-8")
    assert "previousOpen" in js
    assert "data-round-key" in js
    assert "block.open = previousOpen.has(roundKey)" in js
    assert ": false;" in js


def test_front_inspect_includes_task_workspace_path(monkeypatch, tmp_path):
    from types import SimpleNamespace

    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    fake_dir = SimpleNamespace(
        exists=lambda: True,
        workspace=tmp_path / "tasks" / "task_x" / "workspace",
    )
    monkeypatch.setattr(
        controller, "task_detail",
        lambda task_id: {
            "task": {"task_id": task_id, "status": "COMPLETED"},
            "job": {}, "rounds": [], "workspace_files": [], "llm": {},
        })
    monkeypatch.setattr(
        controller.task_directories, "for_task",
        lambda task_id: fake_dir)
    try:
        facts = controller._front_inspect_facts("task_x")
    finally:
        controller.close()

    assert facts["task"]["workspace_path"] == str(fake_dir.workspace)


def test_commit_repo_api_and_selector_html(server):
    fake, base = server
    status, body = _get(base + "/api/workdir?path=/tmp/project")
    workdir = json.loads(body)
    assert status == 200
    assert workdir["is_dir"] is True
    assert workdir["path"] == "/tmp/project"

    status, body = _get(base + "/api/commit/repo")
    payload = json.loads(body)
    assert status == 200
    assert payload["is_git_repo"] is True
    assert payload["path"] == "/tmp/aieos-output"

    status, body = _get(base + "/api/fs/dirs?path=/")
    listing = json.loads(body)
    assert status == 200
    assert listing["ok"] is True
    assert listing["path"] == "/"

    status, body = _get(base + "/")
    html = body.decode("utf-8")
    assert status == 200
    assert 'id="workspace-chip"' in html
    assert 'id="workspace-menu"' in html
    assert 'id="commit-repo-input"' not in html
    assert 'id="repo-picker-modal"' in html


def test_submit_passes_commit_repo(server):
    fake, base = server
    status, payload = _post(
        base + "/api/submit",
        {"text": "做个游戏", "commit_repo": "/tmp/other",
         "workdir": "/tmp/project"},
    )
    assert status == 200
    assert payload["ok"] is True
    assert fake.submitted == ["做个游戏"]
    assert fake.submitted_repo == "/tmp/other"
    assert fake.submitted_workdir == "/tmp/project"


def test_pi_task_creation_persists_project_workdir(db, tmp_path):
    from core.task_store import TaskStore
    from front import FrontBrief, FrontDecision, FrontIntent

    project = tmp_path / "project"
    project.mkdir()
    store = TaskStore(db)
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_c"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=store,
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a", pi_work_agents=["agent_c"])
    decision = FrontDecision(
        intent=FrontIntent.HANDOFF,
        role="game_developer",
        work_profile="standard",
        brief=FrontBrief(
            goal="修改项目",
            task_text="修改项目",
            deliverables=["app.py"],
        ),
    )
    try:
        created = controller._create_solo_task(
            SimpleNamespace(job_id="job_project", workdir=str(project)),
            "agent_c", decision)
    finally:
        controller.close()

    assert created["ok"] is True, created
    task = store.get(created["task_id"])
    assert task.acceptance["workspace_mode"] == "project"
    assert task.acceptance["project_path"] == str(project.resolve())


def test_memory_api(server):
    _fake, base = server
    status, body = _get(base + "/api/memory?agent_id=agent_a")
    assert status == 200
    payload = json.loads(body)
    assert payload["agent_id"] == "agent_a"
    assert payload["short"]["revision"] == 1
    assert payload["short"]["entries"][0]["summary"] == "用户偏好简洁"

    status, payload = _post(base + "/api/memory/delete", {
        "agent_id": "agent_a",
        "kind": "short",
        "entry_id": "stm_1",
        "expected_revision": 1,
    })
    assert status == 200
    assert payload["ok"] is True
    assert payload["short"]["entries"] == []

    status, payload = _post(base + "/api/memory/clear", {
        "agent_id": "agent_a",
    })
    assert status == 200
    assert payload["ok"] is True
    assert payload["deleted"]["short_term"] == 1
    assert payload["memory"]["short"]["revision"] == 0
