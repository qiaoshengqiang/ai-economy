"""Controller 接线：life 策略读取、委托闹钟权限、Worker handler 注册。"""

from __future__ import annotations

from types import SimpleNamespace

from core.private_long_term_memory_store import PrivateLongTermMemoryStore
from core.private_short_term_memory_store import PrivateShortTermMemoryStore
from core.task import Task, TaskMode
from core.task_store import TaskStore
from core.workspace import Workspace
from services.private_long_term_memory_service import (
    PrivateLongTermMemoryService,
)
from services.private_short_term_memory_service import (
    PrivateShortTermMemoryService,
)
from work.controller import AIEOSController
from work.task_directory import TaskDirectoryManager


def _runtime(life: dict | None):
    resolved_agent = SimpleNamespace(
        raw={"life": life} if life is not None else {},
        system_prompt="你是小伊。",
    )
    front_agent = SimpleNamespace(
        raw={}, system_prompt="你是小艾。")
    return SimpleNamespace(
        directory=SimpleNamespace(all=lambda: [
            SimpleNamespace(agent_id="agent_a",
                            capabilities=SimpleNamespace(names=())),
            SimpleNamespace(agent_id="agent_b",
                            capabilities=SimpleNamespace(names=())),
        ]),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=None)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
        resolved_config=SimpleNamespace(
            agent=lambda aid: (
                resolved_agent if aid == "agent_b" else front_agent)),
        communication=SimpleNamespace(recorder=None),
    )


def _controller(runtime):
    return AIEOSController(runtime, front_agent="agent_a")


def test_life_policy_is_read_from_agent_raw_config():
    controller = _controller(_runtime({
        "enabled": True,
        "wake_on_message": True,
        "accept_delegated_alarms": True,
    }))
    try:
        policy = controller._life_policy("agent_b")
        assert policy.enabled is True
        assert policy.wake_on_message is True
        assert policy.accept_delegated_alarms is True
        assert controller._life_policy("agent_a").enabled is False
    finally:
        controller.close()


def test_delegated_alarm_requires_opt_in():
    controller = _controller(_runtime({
        "enabled": True,
        "accept_delegated_alarms": True,
    }))
    try:
        ok, reason = controller._delegated_alarm_allowed("agent_b")
        assert ok is True
        assert reason == ""

        controller._life_policy_cache.clear()
        controller.rt.resolved_config = SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(
                raw={"life": {"enabled": True}},
                system_prompt=""))
        ok, reason = controller._delegated_alarm_allowed("agent_b")
        assert ok is False
        assert "没有接受" in reason
    finally:
        controller.close()


def test_life_wake_starts_and_registers_worker_alarm_handler():
    controller = _controller(_runtime({
        "enabled": True,
        "wake_on_message": True,
        "wake_on_alarm": True,
    }))
    try:
        assert controller._life_wake is not None
        controller._register_life_alarm_handlers()
        assert controller._alarm_dispatcher.handler_for("agent_b") is not None
        # Front 仍然使用自己的 FrontWakeupHandler，不在这里注册。
        assert controller._alarm_dispatcher.handler_for("agent_a") is None
    finally:
        controller.close()


def test_front_relays_life_reply_without_creating_job():
    controller = _controller(_runtime({"enabled": True}))
    try:
        controller._on_front_inbox_message(SimpleNamespace(
            from_agent="agent_b",
            payload={
                "kind": "agent_message",
                "life": True,
                "text": "你好小艾",
            },
        ))
        assert any(
            "回复" in item["text"] and "你好小艾" in item["text"]
            for item in controller._messages)
        assert controller._jobs == {}
    finally:
        controller.close()


def test_life_wake_is_not_started_without_opt_in():
    controller = _controller(_runtime({"enabled": False}))
    try:
        assert controller._life_wake is None
        controller._register_life_alarm_handlers()
        assert controller._alarm_dispatcher.handler_for("agent_b") is None
    finally:
        controller.close()


def test_life_tools_are_wired_and_fail_closed():
    controller = _controller(_runtime({"enabled": True}))
    try:
        tools = controller._life_tools()
        assert tools.recent_tasks is not None
        assert tools.task_inspect is not None
        assert tools.task_files is not None
        assert tools.task_read is not None
        assert tools.private_write is not None
        assert tools.short_term_add is not None
        assert tools.long_term_consolidate is not None

        assert tools.recent_tasks("agent_b", 5) == []
        assert tools.task_inspect("agent_b", "task_missing")["ok"] is False
        assert tools.private_list("agent_b")["ok"] is False
        assert tools.short_term_read("agent_b")["ok"] is False
        assert tools.long_term_search("agent_b", "x")["ok"] is False
    finally:
        controller.close()


def test_life_tools_read_own_task_and_memory(db, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")

    task = Task.create(
        task_id="task_life_tools",
        creator="agent_c",
        description="写 hello world",
        workspace_id="WS-LIFE-TOOLS",
        participants=["agent_c"],
        acceptance={
            "workspace_mode": "project",
            "project_path": str(project),
            "work_mode": "autonomous",
        },
        mode=TaskMode.SOLO,
    )
    task.claim_producer("agent_c")
    store = TaskStore(db)
    store.save(task)

    directory = TaskDirectoryManager(
        tmp_path / "tasks").for_task(task.task_id, workdir=project)
    directory.ensure()
    directory.write_json("workspace_plan.json", {
        "mode": "create",
        "target_path": ".",
        "reason": "project root",
    })
    directory.write_json("work_state.json", {
        "phase": "autonomous",
        "status": "failed",
        "last_error": "Body Timeout",
        "history": [{"phase": "autonomous", "status": "failed"}],
    })
    directory.write_json("manifest.json", {
        "files": [{"path": "index.html", "bytes": 13}],
        "snapshot": {"label": "final", "files": 1, "bytes": 13},
    })

    workspace = Workspace(tmp_path / "agent_c", "agent_c")
    short_store = PrivateShortTermMemoryStore(db)
    short_term = PrivateShortTermMemoryService(short_store)
    long_term = PrivateLongTermMemoryService(
        PrivateLongTermMemoryStore(db), short_store)

    fake = SimpleNamespace(
        rt=SimpleNamespace(
            tasks=store,
            private_short_term_memory=short_term,
            private_long_term_memory=long_term,
        ),
        _agent=lambda _aid: SimpleNamespace(workspace=workspace),
        _task_directory=lambda _task: directory,
        _rounds=lambda _tid: [{
            "round": 1, "phase": "autonomous", "tokens": 10,
            "latency_ms": 100, "summary": "recon done",
            "segments": [{"error": "Body Timeout"}],
        }],
        _read_checkpoint=lambda _tid: None,
        _is_live=lambda _tid: False,
    )
    tools = AIEOSController._life_tools(fake)

    own = tools.recent_tasks("agent_c", 5)
    assert own[0]["task_id"] == task.task_id
    assert own[0]["last_error"] == "Body Timeout"
    assert tools.recent_tasks("agent_x", 5) == []

    inspected = tools.task_inspect("agent_c", task.task_id)
    assert inspected["ok"] is True
    assert inspected["task"]["contractor"] == "agent_c"
    assert inspected["workspace"]["work_state"]["last_error"] == (
        "Body Timeout")
    assert inspected["rounds"][0]["errors"] == ["Body Timeout"]
    assert tools.task_inspect("agent_x", task.task_id)["ok"] is False

    files = tools.task_files("agent_c", task.task_id)
    assert files["ok"] is True
    assert [item["path"] for item in files["paths"]] == ["index.html"]
    read = tools.task_read("agent_c", task.task_id, "index.html", 1, 10)
    assert read["ok"] is True
    assert "hello" in read["content"]
    escape = tools.task_read(
        "agent_c", task.task_id, "../escape.txt", 1, 10)
    assert escape["ok"] is False
    assert escape["code"] == "bad_path"

    assert tools.private_write(
        "agent_c", "notes/a.md", "remember")["ok"] is True
    private_read = tools.private_read(
        "agent_c", "notes/a.md", 1, 10)
    assert private_read["ok"] is True
    assert "remember" in private_read["content"]
    assert tools.private_delete("agent_c", "notes/a.md")["ok"] is True

    added = tools.short_term_add(
        "agent_c", "记住这个任务", "", "", 4)
    assert added["ok"] is True
    assert tools.short_term_read("agent_c")["memory"]["entries"]
    entry_id = added["entry"]["entry_id"]
    revision = added["memory"]["revision"]
    consolidated = tools.long_term_consolidate(
        "agent_c", revision, "抽象后的长期记忆", [], [], [entry_id])
    assert consolidated["ok"] is True
    found = tools.long_term_search("agent_c", "长期记忆")
    assert found["ok"] is True
    assert found["entries"]
