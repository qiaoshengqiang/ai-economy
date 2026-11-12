"""Agent 中途提问 / 人类直接回答的控制器契约。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from work.controller import AIEOSController
from work.roles import WorkResult


class _Directory:
    def __init__(self, ids):
        self.entries = [SimpleNamespace(agent_id=aid) for aid in ids]

    def all(self):
        return list(self.entries)


class _FakeRunner:
    def __init__(self, directory):
        self.directory = directory

    def run(self):
        self.directory.workspace.mkdir(parents=True, exist_ok=True)
        (self.directory.workspace / "RESULT.md").write_text(
            "answered", encoding="utf-8")
        return WorkResult(
            task_id="task_q", status="COMPLETED",
            summary="answered", turns=1, tokens=1)


def test_answer_question_resumes_without_front(tmp_path):
    task = SimpleNamespace(
        task_id="task_q", creator="agent_c", contractor="agent_c",
        participants=["agent_c"], status="IN_PROGRESS",
        is_open=True, acceptance={"role": "generalist"},
        description="做一个页面", iteration=1,
    )
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_c"]),
        economy=SimpleNamespace(
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(get=lambda tid: task if tid == "task_q" else None),
    )
    controller = AIEOSController(
        runtime,
        front_agent="agent_a",
        pi_work_agents=["agent_c"],
        task_root=tmp_path / "tasks",
    )
    directory = controller.task_directories.for_task("task_q")
    directory.ensure()
    directory.write_json("question.json", {
        "task_id": "task_q",
        "question": "标题用 A 还是 B？",
        "options": ["A", "B"],
        "allow_free_text": True,
    })
    controller._pi_runner = lambda *_args, **_kwargs: _FakeRunner(directory)
    try:
        job_id = controller.answer_question(
            "task_q", "", option_id="B")
        deadline = time.time() + 5
        while time.time() < deadline:
            job = controller._jobs[job_id]
            if job.status in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.05)
        assert job.status == "COMPLETED", job.error
        assert not (directory.aieos / "question.json").exists()
        answer = json.loads(
            (directory.aieos / "ANSWER.json").read_text("utf-8"))
        assert answer["answer"] == "B"
        history = json.loads(
            (directory.aieos / "questions.json").read_text("utf-8"))
        assert history[0]["answer"] == "B"
    finally:
        controller.close()
