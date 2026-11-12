"""
Pi Work 独立验收：命令 / verifier Agent / fail-closed。
"""

from __future__ import annotations

from types import SimpleNamespace

from work.pi_acceptance import PiAcceptance
from work.task_directory import TaskDirectoryManager


def _directory(tmp_path):
    directory = TaskDirectoryManager(tmp_path).for_task("task_acc")
    directory.ensure()
    directory.write_text("hello.txt", "hi")
    directory.write_text("RESULT.md", "done")
    return directory


def test_acceptance_uses_independent_verifier_agent(tmp_path):
    directory = _directory(tmp_path)
    calls = []

    class _Brain:
        provider = "deepseek"
        model = "deepseek-flash"

    class _Agent:
        has_brain = True
        brain = _Brain()

        def generate(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content='{"passed": true, "reasons": ["ok"], "gaps": []}')

    runtime = SimpleNamespace(
        economy=SimpleNamespace(agent=lambda aid: _Agent()))
    task = SimpleNamespace(
        task_id="task_acc",
        acceptance={"criteria": ["hello.txt contains hi"]},
    )
    result = PiAcceptance(
        runtime, task=task, task_directory=directory,
        manifest=directory.manifest(), acceptance_agent="agent_a",
    ).verify()

    assert result.passed is True
    assert result.verifier == "agent_a"
    assert calls and calls[0]["task_id"] == "task_acc"
    assert calls[0]["cognitive"] is False


def test_acceptance_command_is_independent(tmp_path):
    directory = _directory(tmp_path)
    task = SimpleNamespace(
        task_id="task_acc",
        acceptance={
            "criteria": ["hello.txt"],
            "command": ["python3", "-c", "import sys; sys.exit(0)"],
        },
    )
    result = PiAcceptance(
        SimpleNamespace(economy=None), task=task,
        task_directory=directory,
        manifest=directory.manifest(),
    ).verify()

    assert result.passed is True
    assert result.verifier == "command"


def test_acceptance_fails_closed_without_verifier(tmp_path):
    directory = _directory(tmp_path)
    task = SimpleNamespace(
        task_id="task_acc",
        acceptance={"criteria": ["hello.txt"]},
    )
    result = PiAcceptance(
        SimpleNamespace(economy=None), task=task,
        task_directory=directory,
        manifest=directory.manifest(),
    ).verify()

    assert result.passed is False
    assert "acceptance_agent" in result.gaps[0]
