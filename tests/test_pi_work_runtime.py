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
from work.project_lock import ProjectLockManager
from work.task_directory import TaskDirectoryManager
from work.task_snapshot import TaskSnapshotStore


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


def test_pi_prompts_separate_name_and_agent_id(db, tmp_path):
    task = Task.create(
        task_id="T-PI-IDENTITY", creator="agent_c",
        description="身份提示词。",
        workspace_id="WS-PI-ID", participants=["agent_c"],
        acceptance={"brief": {
            "goal": "把格子改成正方形",
            "human_text": "格子看起来有点变形",
            "task_text": "把格子改成正方形",
            "front_notes": "建议保持竖屏；这只是建议。",
        }},
        mode=TaskMode.SOLO)
    task.claim_producer("agent_c")
    store = TaskStore(db)
    store.save(task)

    workspace = _FakeWorkspace(tmp_path / "agent_c")
    runtime = SimpleNamespace(
        resolved_config=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(name="小希")),
        tasks=store,
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=_FakeBrain(),
            workspace=workspace, name="小希")),
        private_working_memory=_FakeWorkingMemory(),
        private_short_term_memory=_FakeShortMemory(),
        private_long_term_memory=_FakeLongMemory(),
        recorder=_Recorder(),
        experiment_run_id="exp-identity",
    )
    directory = TaskDirectoryManager(tmp_path / "tasks").for_task(task.task_id)
    runner = PiWorkRuntime(
        runtime, task,
        task_directory=directory,
        use_llm_proxy=False,
    )

    for prompt in (
        runner._minimal_system_prompt(),
        runner._system_prompt(""),
    ):
        assert "你的名字是：小希" in prompt
        assert "你的 Agent ID 是：agent_c" in prompt
        assert "小希（agent_c）" not in prompt
        assert "Human 对 Front 的原话（不一定是需求）" in prompt
        assert "Front 给 Worker 的任务说明" in prompt
        assert "Front 的建议（非硬要求）" in prompt
        assert "格子看起来有点变形" in prompt
        assert "保持竖屏" in prompt


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
    # 结束方式：告诉它写到哪里、最后输出什么，不规定“何时算完成”
    assert "========== 结束方式 ==========" in system
    assert str(directory.workspace / "RESULT.md") in system
    assert "TASK_COMPLETE" in system
    assert "系统会优先读取这个文件作为任务总结" in system
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


def test_pi_project_workdir_modifies_existing_project(db, monkeypatch, tmp_path):
    project = tmp_path / "existing-project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    (project / "README.md").write_text("old project\n", encoding="utf-8")

    task = Task.create(
        task_id="T-PI-PROJECT", creator="agent_c",
        description="把 app.py 的值改成 2",
        workspace_id="WS-PROJECT", participants=["agent_c"],
        acceptance={
            "workspace_mode": "project",
            "project_path": str(project),
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
        experiment_run_id="exp-project",
    )
    directory = TaskDirectoryManager(
        tmp_path / "tasks").for_task(task.task_id, workdir=project)
    seen = {}

    class _FakePiClient:
        def __init__(self, **kwargs):
            self.is_recon = (
                str(kwargs.get("tools") or "") == "read,grep,find,ls")
            seen.setdefault("cwds", []).append(Path(kwargs["cwd"]))

        def run(self, prompt, *, timeout_seconds, should_stop, event_sink):
            (self_cwd := seen["cwds"][-1])
            if self.is_recon:
                events = [
                    {"type": "turn_start", "_received_at": 1.0},
                    {
                        "type": "message_end", "_received_at": 2.0,
                        "message": {
                            "role": "assistant",
                            "content": [{
                                "type": "text",
                                "text": json.dumps({
                                    "mode": "modify",
                                    "target_path": ".",
                                    "reason": "已有 app.py",
                                }),
                            }],
                        },
                    },
                    {"type": "turn_end", "_received_at": 3.0},
                ]
                return PiRunResult(events=events, exit_code=0)
            (self_cwd / "app.py").write_text(
                "value = 2\n", encoding="utf-8")
            (self_cwd / "new_module.py").write_text(
                "added = True\n", encoding="utf-8")
            events = [
                {"type": "turn_start", "_received_at": 1.0},
                {
                    "type": "message_end", "_received_at": 2.0,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "项目已修改"}],
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
    runner = PiWorkRuntime(
        runtime, task,
        task_directory=directory,
        pi_cli_path="fake-pi",
        use_llm_proxy=False,
        work_mode="autonomous",
        phase_plan=["autonomous"],
        project_lock_manager=ProjectLockManager(tmp_path / "locks"),
    )
    result = runner.run()

    assert result.ok is True, result.error
    assert seen["cwds"][0] == project.resolve(), "recon 在 workspace_root"
    assert seen["cwds"][-1] == project.resolve(), "target='.' 仍在项目根"
    assert (project / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (project / "new_module.py").exists()
    assert not (project / "TASK_DOC.md").exists()
    assert (directory.aieos / "TASK_DOC.md").exists()
    assert directory.workspace.exists() is False

    manifest = json.loads(
        (directory.aieos / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["workspace_mode"] == "project"
    assert manifest["workdir"] == str(project.resolve())
    paths = {item["path"] for item in manifest["files"]}
    assert paths == {"app.py", "new_module.py"}

    snapshots = TaskSnapshotStore(directory).list()
    assert [item.label for item in snapshots] == [
        "baseline", "phase:autonomous:end", "final"]
    assert snapshots[-1].files == 3


def test_pi_project_recon_creates_subdir_and_snapshots_only_it(
        db, monkeypatch, tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    (parent / "unrelated").mkdir()
    (parent / "unrelated" / "big.txt").write_text(
        "x" * 100_000, encoding="utf-8")

    task = Task.create(
        task_id="T-PI-RECON-CREATE", creator="agent_c",
        description="在父目录下新建 pelican-cycling 小游戏",
        workspace_id="WS-RECON", participants=["agent_c"],
        acceptance={
            "workspace_mode": "project",
            "project_path": str(parent),
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
        experiment_run_id="exp-recon",
    )
    directory = TaskDirectoryManager(
        tmp_path / "tasks").for_task(task.task_id, workdir=parent)
    seen = {"cwds": []}

    class _FakePiClient:
        def __init__(self, **kwargs):
            self.is_recon = (
                str(kwargs.get("tools") or "") == "read,grep,find,ls")
            seen["cwds"].append(Path(kwargs["cwd"]))

        def run(self, prompt, *, timeout_seconds, should_stop, event_sink):
            cwd = seen["cwds"][-1]
            if self.is_recon:
                events = [{
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{
                            "type": "text",
                            "text": json.dumps({
                                "mode": "create",
                                "target_path": "pelican-cycling",
                                "reason": "目标目录不存在",
                            }),
                        }],
                    },
                }]
                return PiRunResult(events=events, exit_code=0)
            assert cwd == (parent / "pelican-cycling").resolve()
            (cwd / "index.html").write_text("<html>game</html>", "utf-8")
            events = [{
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            }]
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
        work_mode="autonomous",
        phase_plan=["autonomous"],
        project_lock_manager=ProjectLockManager(tmp_path / "locks"),
    )
    result = runner.run()

    assert result.ok is True, result.error
    assert seen["cwds"][0] == parent.resolve(), "recon 使用 workspace_root"
    assert seen["cwds"][-1] == (parent / "pelican-cycling").resolve()
    assert (parent / "pelican-cycling" / "index.html").exists()

    manifest = json.loads(
        (directory.aieos / "manifest.json").read_text("utf-8"))
    assert manifest["workdir"] == str(
        (parent / "pelican-cycling").resolve())
    assert manifest["project_root"] == str(parent.resolve())
    assert manifest["project_target"] == "pelican-cycling"
    assert {item["path"] for item in manifest["files"]} == {"index.html"}

    snapshots = TaskSnapshotStore(directory).list()
    assert [item.label for item in snapshots] == [
        "baseline", "phase:autonomous:end", "final"]
    # baseline 是空目录；final 只有目标子目录里的文件。
    assert snapshots[0].files == 0
    assert snapshots[-1].files == 1


def test_pi_provider_spec_uses_resolved_provider_endpoint(tmp_path):
    task = Task.create(
        task_id="T-PROVIDER-SPEC",
        creator="agent_o",
        description="provider spec",
        workspace_id="WS-PROVIDER",
        participants=["agent_o"],
        mode=TaskMode.SOLO,
    )
    directory = TaskDirectoryManager(tmp_path / "tasks").for_task(task.task_id)
    provider = SimpleNamespace(
        name="opencode",
        adapter="openai_compatible",
        base_url="https://opencode.ai/inference/go/openai/v1",
        chat_path="/chat/completions",
        request={"extra_headers": {"x-opencode-session": "ses_test"}},
    )
    resolved_agent = SimpleNamespace(provider=provider, model_id="glm-5.3")
    resolved = SimpleNamespace(
        agent=lambda _aid: resolved_agent,
        provider=lambda _name: provider,
    )
    runtime = SimpleNamespace(
        resolved_config=resolved,
        tasks=SimpleNamespace(),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=_FakeBrain(),
            workspace=_FakeWorkspace(tmp_path / "agent_o"))),
        communication=None,
    )

    runner = PiWorkRuntime(
        runtime, task,
        task_directory=directory,
        use_llm_proxy=False,
    )
    spec = runner._provider_spec()

    assert spec.provider == "opencode"
    assert spec.model == "glm-5.3"
    assert spec.adapter == "openai_compatible"
    assert spec.base_url == "https://opencode.ai/inference/go/openai/v1"
    assert spec.chat_url == (
        "https://opencode.ai/inference/go/openai/v1/chat/completions")
    assert spec.request["extra_headers"]["x-opencode-session"] == "ses_test"


def test_last_assistant_error_surfaces_upstream_message():
    events = [
        {"type": "message_end", "message": {
            "role": "assistant", "stopReason": "toolUse", "content": []}},
        {"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error",
            "errorMessage": "401: Authentication Fails"}},
    ]
    assert PiWorkRuntime._last_assistant_error(events) == (
        "401: Authentication Fails")


def test_pi_runtime_accepts_persistent_inbox_bridge(tmp_path):
    task = Task.create(
        task_id="T-PEERSISTENT-INBOX",
        creator="agent_c",
        description="persistent inbox",
        workspace_id="WS-INBOX",
        participants=["agent_c"],
        mode=TaskMode.SOLO,
    )
    directory = TaskDirectoryManager(tmp_path / "tasks").for_task(task.task_id)

    class _Inbox:
        inbox_md = tmp_path / "inbox" / "pi-inbox.md"

        def start(self):
            raise AssertionError("persistent inbox 不应由 PiWorkRuntime 启动")

        def stop(self):
            raise AssertionError("persistent inbox 不应由 PiWorkRuntime 停止")

        def drain_for_prompt(self):
            return ""

    inbox = _Inbox()
    runtime = SimpleNamespace(
        tasks=SimpleNamespace(),
        economy=SimpleNamespace(agent=lambda _aid: SimpleNamespace(
            has_brain=True, brain=_FakeBrain(),
            workspace=_FakeWorkspace(tmp_path / "agent_c"))),
        communication=None,
    )
    runner = PiWorkRuntime(
        runtime, task,
        task_directory=directory,
        use_llm_proxy=False,
        inbox_bridge=inbox,
    )
    assert runner._owns_inbox_bridge is False
    assert runner._inbox_bridge is inbox
