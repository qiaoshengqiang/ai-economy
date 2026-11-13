"""
PiWorkRuntime：真实目录 + Pi 事件 + 记忆桥 + Task Core 的冒烟测试。

不启动真实 Pi；用 fake PiRpcClient 检查：
    · 任务目录里落盘的文件进入 manifest；
    · Pi turn / tool / usage 映射成 WorkResult / round；
    · working.md / inbox 被 AIEOS 记忆桥摄取；
    · Task Core 最终 COMPLETED。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.task import Task, TaskMode, TaskStatus
from core.task_store import TaskStore
from work.pi_runtime import PiWorkRuntime
from work.pi_rpc import PiRunResult
from work.task_directory import TaskDirectoryManager


class _FakeBrain:
    provider = "deepseek"
    model = "deepseek-flash"
    settings = SimpleNamespace(
        context_window=600000,
        max_output_tokens=32768,
        supports_reasoning=True,
        thinking_levels=("off", "low", "high", "max"),
        max_tokens=2048,
    )

    def credential_for(self, _provider):
        return SimpleNamespace(material="fake-key")


class _FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root / "workspace"
        self.memory_dir = root / "memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)


class _FakeWorkingMemory:
    def __init__(self):
        self.updated = None

    def render(self, _agent_id, _task_id):
        return "revision = 1\n（空）"

    def read(self, _agent_id, _task_id):
        return SimpleNamespace(revision=1)

    def update(self, _agent_id, _task_id, *, expected_revision, nodes,
               remove_nodes=None):
        self.updated = (expected_revision, nodes)
        return SimpleNamespace(
            revision=2,
            to_dict=lambda: {
                "revision": 2,
                "nodes": nodes,
            },
        )


class _FakeShortMemory:
    def __init__(self):
        self.added = None

    def render(self, _agent_id):
        return "（空）"

    def add_from_final(self, agent_id, task_id, snapshot):
        self.added = (agent_id, task_id, snapshot)


class _FakeLongMemory:
    def render_top(self, _agent_id):
        return "（空）"


class _Recorder:
    def __init__(self):
        self.calls = []
        self.PURPOSE_COGNITION = "cognition"

    def record(self, **kwargs):
        self.calls.append(kwargs)
        return "call_1"


def test_pi_work_runtime_completes_and_ingests_memory(db, monkeypatch, tmp_path):
    task = Task.create(
        task_id="T-PI-WORK", creator="agent_c",
        description="写一个 hello.txt",
        workspace_id="WS-PI", participants=["agent_c"],
        acceptance={"work_profile": "creative_product"},
        mode=TaskMode.SOLO)
    task.claim_producer("agent_c")
    store = TaskStore(db)
    store.save(task)

    workspace = _FakeWorkspace(tmp_path / "agent_c")
    working = _FakeWorkingMemory()
    short = _FakeShortMemory()
    recorder = _Recorder()
    runtime = SimpleNamespace(
        tasks=store,
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=_FakeBrain(), workspace=workspace)),
        private_working_memory=working,
        private_short_term_memory=short,
        private_long_term_memory=_FakeLongMemory(),
        recorder=recorder,
        experiment_run_id="exp-pi",
    )

    directory = TaskDirectoryManager(tmp_path / "tasks").for_task(task.task_id)
    rounds = []

    class _FakePiClient:
        def __init__(self, **kwargs):
            self.cwd = Path(kwargs["cwd"])
            self.kwargs = kwargs

        def run(self, prompt, *, timeout_seconds, should_stop, event_sink):
            (self.cwd / "hello.txt").write_text("hello", encoding="utf-8")
            if "原型阶段" in prompt:
                (self.cwd / "PROTOTYPE.md").write_text(
                    "prototype design", encoding="utf-8")
            elif "工程阶段" in prompt:
                (self.cwd / "ENGINEERING.md").write_text(
                    "engineering verification", encoding="utf-8")
            else:
                (self.cwd / "RESULT.md").write_text(
                    "已写出 hello.txt", encoding="utf-8")
                inbox = workspace.memory_dir / "pi" / "inbox"
                inbox.mkdir(parents=True, exist_ok=True)
                (inbox / "note.md").write_text(
                    "next time use a cat", encoding="utf-8")
            events = [
                {"type": "turn_start", "_received_at": 1.0},
                {
                    "type": "message_end",
                    "_received_at": 2.0,
                    "message": {
                        "role": "assistant",
                        "provider": "deepseek",
                        "model": "deepseek-flash",
                        "stopReason": "toolUse",
                        "content": [
                            {"type": "text", "text": "准备写文件"},
                            {"type": "thinking", "thinking": "先检查目录"},
                        ],
                        "usage": {
                            "input": 10, "output": 5,
                            "cacheRead": 0, "cacheWrite": 0,
                            "totalTokens": 15,
                        },
                    },
                },
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call_1",
                    "toolName": "write",
                    "args": {"path": "hello.txt"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call_1",
                    "toolName": "write",
                    "result": "ok",
                    "isError": False,
                    "durationMs": 12,
                },
                {"type": "turn_end", "_received_at": 3.0},
                {"type": "agent_settled", "_received_at": 3.1},
            ]
            if event_sink is not None:
                for event in events:
                    event_sink(event)
            return PiRunResult(events=events, exit_code=0)

    monkeypatch.setattr(
        "work.pi_runtime.PiRpcClient", _FakePiClient, raising=True)

    runner = PiWorkRuntime(
        runtime, task,
        task_directory=directory,
        pi_cli_path="fake-pi",
        use_llm_proxy=False,
        role_prompt="ROLE-MARKER: game developer",
        round_sink=rounds.append,
    )
    result = runner.run()

    assert result.ok is True, result.error
    assert result.status == TaskStatus.COMPLETED
    assert store.get(task.task_id).status == TaskStatus.COMPLETED
    assert (directory.workspace / "hello.txt").read_text() == "hello"
    assert result.turns == 3
    assert result.tokens == 45
    assert result.actions == 3
    assert result.summary == "已写出 hello.txt"
    assert "ROLE-MARKER" in runner._phase_prompt("engineering", "")
    assert runner._phase_thinking("prototype") == "max"
    assert runner._phase_thinking("engineering") == "high"
    assert runner._phase_thinking("evolution") == "high"
    assert runner.extra_args == ["--no-skills", "--no-context-files"]
    assert (directory.workspace / "TASK_DOC.md").exists()
    plan, resumed, reason = runner._phase_plan_for_run({
        "iteration": 1, "phase": "engineering",
        "phase_status": "running"})
    assert plan == ["engineering", "evolution"]
    assert resumed is True
    assert reason == "task_resume"
    runner.work_profile = "direct"
    assert runner._phase_plan_for_run({})[0] == ["direct"]
    assert "hello.txt" in result.artifacts
    assert directory.manifest()
    assert working.updated is not None
    assert short.added is not None
    assert recorder.calls and recorder.calls[0]["task_id"] == "T-PI-WORK"
    assert rounds and rounds[0]["round"] == 1
    assert [item["phase"] for item in rounds] == [
        "prototype", "engineering", "evolution"]
    assert (directory.aieos / "work_state.json").exists()
    if runner.snapshots.enabled:
        assert [item.label for item in runner.snapshots.list()] == [
            "baseline",
            "phase:prototype:end",
            "phase:engineering:end",
            "phase:evolution:end",
            "final",
        ]
        manifest_payload = json.loads(
            (directory.aieos / "manifest.json").read_text("utf-8"))
        assert manifest_payload["snapshot"]["label"] == "final"
    state_path = directory.aieos / "work_state.json"
    state = json.loads(state_path.read_text("utf-8"))
    assert state["session_id"] == "T-PI-WORK"
    runner._write_work_state(
        "prototype", status="failed", thinking="max",
        error="Pi 超时（1800.0s）")
    state = json.loads(state_path.read_text("utf-8"))
    assert state["last_error"] == "Pi 超时（1800.0s）"
    runner._write_work_state(
        "prototype", status="completed", thinking="max")
    state = json.loads(state_path.read_text("utf-8"))
    assert state["last_error"] == ""
    assert rounds[0]["segments"][0]["actions"][0]["action_type"] == "write"
    assert rounds[0]["segments"][0]["results"][0]["success"] is True

    from work.worker_roles import ROLES
    role_runner = PiWorkRuntime(
        runtime, task,
        task_directory=directory,
        pi_cli_path="fake-pi",
        use_llm_proxy=False,
        role=ROLES["game_developer"],
    )
    assert "核心玩法必须可操作" in role_runner._phase_prompt(
        "prototype", "")
    assert "检查核心交互" in role_runner._phase_prompt(
        "engineering", "")

    class _BoomSnapshots:
        def capture(self, *args, **kwargs):
            raise RuntimeError("snapshot boom")

    runner.snapshots = _BoomSnapshots()
    assert runner._capture_snapshot("boom") is None
    assert "snapshot boom" in (
        directory.aieos / "snapshot-error.log").read_text("utf-8")

    pi_agent_dir = runner._write_pi_models("http://127.0.0.1:1", "m")
    model_cfg = (pi_agent_dir / "models.json").read_text("utf-8")
    assert '"maxTokens": 32768' in model_cfg
    assert '"contextWindow": 600000' in model_cfg
    assert '"max": "max"' in model_cfg
    assert '"medium": null' in model_cfg
    pi_settings = json.loads(
        (pi_agent_dir / "settings.json").read_text("utf-8"))
    assert pi_settings["compaction"] == {
        "enabled": True,
        "reserveTokens": 16384,
        "keepRecentTokens": 20000,
    }

    monkeypatch.setenv(
        "AIEOS_WEB_SEARCH_CMD", "/opt/aieos/aieos_web_search.py")
    prompt = runner._minimal_system_prompt()
    assert "aieos_web_search.py search" in prompt
    assert "aieos_web_search.py fetch" in prompt


def test_autonomous_mode_uses_single_minimal_run(db, monkeypatch, tmp_path):
    task = Task.create(
        task_id="T-AUTONOMOUS", creator="agent_c",
        description="做一个温暖的阅读页。",
        workspace_id="WS-AUTO", participants=["agent_c"],
        acceptance={
            "work_profile": "creative_product",
            "work_mode": "autonomous",
        },
        mode=TaskMode.SOLO)
    task.claim_producer("agent_c")
    store = TaskStore(db)
    store.save(task)

    workspace = _FakeWorkspace(tmp_path / "agent_c")
    runtime = SimpleNamespace(
        tasks=store,
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=_FakeBrain(), workspace=workspace)),
        private_working_memory=_FakeWorkingMemory(),
        private_short_term_memory=_FakeShortMemory(),
        private_long_term_memory=_FakeLongMemory(),
        recorder=_Recorder(),
        experiment_run_id="exp-auto",
    )
    directory = TaskDirectoryManager(
        tmp_path / "tasks").for_task(task.task_id)
    runner = PiWorkRuntime(
        runtime, task,
        task_directory=directory,
        pi_cli_path="fake-pi",
        use_llm_proxy=False,
        work_mode="autonomous",
    )

    assert runner._base_phase_plan() == ["autonomous"]
    plan, resumed, _ = runner._phase_plan_for_run({})
    assert plan == ["autonomous"]
    assert resumed is False
    plan, resumed, _ = runner._phase_plan_for_run({
        "phase": "autonomous", "phase_status": "running", "iteration": 1})
    assert plan == ["autonomous"]
    assert resumed is True

    assert runner._phase_prompt("autonomous", "") == task.description
    assert runner._phase_thinking("autonomous") == "high"
    system = runner._minimal_system_prompt()
    assert "原型阶段" not in system
    assert "工程阶段" not in system
    assert "QUESTION.json" not in system
    assert "TASK_DOC.md" not in system
    assert str(directory.workspace) in system
    assert "read/write/edit/bash" in system
    # 私有 workspace 的用途（正向表述，不含负向约束）
    assert "私有 workspace" in system
    assert "草稿" in system
    assert "中间产物" in system
    assert "tools/" in system
    assert "不要" not in system
    assert "禁止" not in system
    assert "不允许" not in system

    class _FakePiClient:
        def __init__(self, **kwargs):
            self.cwd = Path(kwargs["cwd"])

        def run(self, prompt, *, timeout_seconds, should_stop, event_sink):
            (self.cwd / "autonomous.txt").write_text("ok", encoding="utf-8")
            events = [
                {"type": "turn_start", "_received_at": 1.0},
                {
                    "type": "message_end",
                    "_received_at": 2.0,
                    "message": {
                        "role": "assistant",
                        "provider": "deepseek",
                        "model": "deepseek-flash",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": {
                            "input": 1, "output": 1,
                            "cacheRead": 0, "cacheWrite": 0,
                            "totalTokens": 2,
                        },
                    },
                },
                {"type": "turn_end", "_received_at": 3.0},
                {"type": "agent_settled", "_received_at": 3.1},
            ]
            if event_sink is not None:
                for event in events:
                    event_sink(event)
            return PiRunResult(events=events, exit_code=0)

    monkeypatch.setattr(
        "work.pi_runtime.PiRpcClient", _FakePiClient, raising=True)
    result = runner.run()
    assert result.ok is True, result.error
    assert (directory.workspace / "autonomous.txt").read_text() == "ok"
    state = json.loads(
        (directory.aieos / "work_state.json").read_text("utf-8"))
    assert state["phase"] == "complete"
    assert state["history"][-1]["phase"] == "autonomous"
