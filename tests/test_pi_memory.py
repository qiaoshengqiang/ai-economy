"""
Pi 记忆文件桥：working/inbox 摄取 + 短期阈值触发长期合并。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from work.pi_memory import PiMemoryBridge
from work.task_directory import TaskDirectoryManager


class _Workspace:
    def __init__(self, root: Path):
        self.root = root / "workspace"
        self.memory_dir = root / "memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)


class _Working:
    def render(self, *_args):
        return "revision = 0\n（空）"

    def read(self, *_args):
        return SimpleNamespace(revision=0)

    def update(self, *args, **kwargs):
        return SimpleNamespace(
            revision=1,
            to_dict=lambda: {"revision": 1, "nodes": kwargs.get("nodes", {})},
        )


class _Short:
    def __init__(self):
        self.added = []

    def render(self, *_args):
        return "（空）"

    def add_from_final(self, *args):
        self.added.append(args)

    def read(self, *_args):
        return SimpleNamespace(revision=2, needs_compaction=True)


class _Long:
    def __init__(self):
        self.calls = []

    def render_top(self, *_args):
        return "（空）"

    def consolidate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None, None


def test_pi_memory_ingests_and_consolidates(tmp_path):
    short = _Short()
    long = _Long()
    runtime = SimpleNamespace(
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            workspace=_Workspace(tmp_path / "agent"))),
        private_working_memory=_Working(),
        private_short_term_memory=short,
        private_long_term_memory=long,
    )
    task = SimpleNamespace(task_id="task_mem")
    directory = TaskDirectoryManager(tmp_path / "tasks").for_task("task_mem")
    directory.ensure()
    bridge = PiMemoryBridge(
        runtime, agent_id="agent_c", task=task,
        task_workspace=directory.workspace)
    bridge.prepare()
    bridge.memory_dir.joinpath("working.md").write_text(
        "# Working\n\nlearned something", encoding="utf-8")
    inbox = bridge.memory_dir / "inbox"
    inbox.mkdir(exist_ok=True)
    inbox.joinpath("note.md").write_text("candidate", encoding="utf-8")

    result = bridge.ingest()

    assert result["ok"] is True
    assert result["short_term"] is True
    assert result["long_term"] is True
    assert short.added
    assert long.calls
    assert not list(inbox.glob("*.md"))


def test_pi_memory_context_documents_private_workspace(tmp_path):
    runtime = SimpleNamespace(
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            name="小希", workspace=_Workspace(tmp_path / "agent"))),
        private_working_memory=_Working(),
        private_short_term_memory=_Short(),
        private_long_term_memory=_Long(),
    )
    task = SimpleNamespace(task_id="task_ctx")
    directory = TaskDirectoryManager(tmp_path / "tasks").for_task("task_ctx")
    directory.ensure()
    bridge = PiMemoryBridge(
        runtime, agent_id="agent_c", task=task,
        task_workspace=directory.workspace)
    bridge.prepare()

    context = (bridge.memory_dir / "context.md").read_text(encoding="utf-8")
    assert "你的名字是：小希" in context
    assert "你的 Agent ID 是：agent_c" in context
    assert "私有 workspace" in context
    assert "草稿" in context
    assert "中间产物" in context
    assert "tools/" in context
    assert "不要" not in context
