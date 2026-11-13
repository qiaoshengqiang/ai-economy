"""Controller 的提交钩子 / Front 确认路由测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from commit.models import CommitRequest
from front import FrontDecision, FrontIntent
from work.controller import AIEOSController, JobStatus, WorkJob
from work.task_directory import TaskDirectoryManager
from work.task_snapshot import TaskSnapshotStore


class _Directory:
    def all(self):
        return [SimpleNamespace(agent_id=aid)
                for aid in ("agent_a", "agent_b")]


def _request(status: str, *, sha: str = "") -> CommitRequest:
    return CommitRequest(
        request_id="commit_x",
        task_id="task_x",
        iteration=1,
        snapshot_seq=0,
        snapshot_commit="a" * 40,
        snapshot_tree="b" * 40,
        mode="manual",
        status=status,
        output_repo="/tmp/output",
        target_ref="refs/heads/main",
        commit_sha=sha,
    )


class _CommitService:
    def __init__(self, request: CommitRequest) -> None:
        self.request = request
        self.confirmed = None
        self.last_mode = ""
        self.last_output_repo = ""
        self.last_repo_mode = ""

    def on_task_completed(
        self, task, *, mode, output_repo="", repo_mode="output",
    ):
        self.last_mode = mode
        self.last_output_repo = output_repo
        self.last_repo_mode = repo_mode
        return self.request

    def front_context(self, *, mode):
        return f"当前默认提交模式：{mode}\n待确认提交：task_x"

    def confirm(self, task, code, *, actor="human"):
        self.confirmed = (task.task_id, code, actor)
        return _request("COMMITTED", sha="c" * 40)


def _controller(request: CommitRequest):
    task = SimpleNamespace(
        task_id="task_x",
        iteration=1,
        status="COMPLETED",
        contractor="agent_b",
        creator="agent_b",
    )
    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(
            list_tasks=lambda limit=50: [],
            get=lambda task_id: task,
        ),
    )
    return AIEOSController(
        runtime,
        front_agent="agent_a",
        commit_service=_CommitService(request),
    )


def test_completed_task_creates_manual_commit_prompt():
    controller = _controller(_request("AWAITING_HUMAN"))
    job = WorkJob(job_id="job_x", human_text="测试")
    try:
        controller._after_task_completed(job, "task_x")
        texts = [item["text"] for item in controller._messages]
        assert any("提交" in text for text in texts)
        assert any("仓库：" in text and "目录：" in text for text in texts)
        assert any("aaaaaaaa" in text for text in texts)
    finally:
        controller.close()


def test_task_commit_mode_snapshot_wins_over_current_setting():
    controller = _controller(_request("AWAITING_HUMAN"))
    controller.commit_mode = "manual"
    task = controller.rt.tasks.get("task_x")
    task.acceptance = {"commit_mode": "limited"}
    job = WorkJob(job_id="job_x", human_text="测试")
    try:
        controller._after_task_completed(job, "task_x")
        assert controller.commit_service.last_mode == "limited"
    finally:
        controller.close()


def test_task_commit_target_snapshot_wins_over_current_setting():
    controller = _controller(_request("AWAITING_HUMAN"))
    task = controller.rt.tasks.get("task_x")
    task.acceptance = {
        "commit_mode": "manual",
        "commit_output_repo": "/tmp/selected-project",
        "commit_output_repo_mode": "project",
    }
    job = WorkJob(job_id="job_x", human_text="测试")
    try:
        controller._after_task_completed(job, "task_x")
        assert controller.commit_service.last_output_repo == (
            "/tmp/selected-project")
        assert controller.commit_service.last_repo_mode == "project"
    finally:
        controller.close()


def test_controller_builds_real_commit_service_from_db(db, tmp_path):
    manager = TaskDirectoryManager(tmp_path / "tasks")
    directory = manager.for_task("task_real").ensure()
    (directory.workspace / "hello.txt").write_text("hi\n", encoding="utf-8")
    TaskSnapshotStore(directory).capture("final")
    task = SimpleNamespace(
        task_id="task_real",
        iteration=1,
        status="COMPLETED",
        contractor="agent_b",
        creator="agent_b",
        description="real service",
    )
    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(
            db=db,
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(
            list_tasks=lambda limit=50: [],
            get=lambda task_id: task,
        ),
    )
    controller = AIEOSController(
        runtime,
        front_agent="agent_a",
        commit_mode="manual",
        commit_output_repo=str(tmp_path / "output"),
        task_root=tmp_path / "tasks",
    )
    job = WorkJob(job_id="job_real", human_text="测试")
    try:
        assert controller.commit_service is not None
        controller._after_task_completed(job, "task_real")
        texts = [item["text"] for item in controller._messages]
        assert any("提交" in text for text in texts)
    finally:
        controller.close()


def test_front_commit_confirm_routes_to_service():
    controller = _controller(_request("AWAITING_HUMAN"))
    job = WorkJob(job_id="job_x", human_text="提交吧")
    try:
        controller._apply_control_decision(
            job,
            FrontDecision(
                intent=FrontIntent.COMMIT_CONFIRM,
                task_id="task_x",
                commit_code="aaaaaaaa",
                reply="提交中",
            ),
        )
        assert controller.commit_service.confirmed == (
            "task_x", "aaaaaaaa", "human")
        assert job.status == JobStatus.DIRECT_REPLY
        assert "已提交" in job.summary
    finally:
        controller.close()


def test_validate_commit_repo_requires_absolute_git_repo(db, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    runtime = SimpleNamespace(
        directory=_Directory(),
        economy=SimpleNamespace(
            db=db,
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(
            list_tasks=lambda limit=50: [],
            get=lambda task_id: None,
        ),
    )
    controller = AIEOSController(runtime, front_agent="agent_a")
    try:
        assert controller._validate_commit_repo(str(project)) == str(
            project.resolve())
        with pytest.raises(ValueError, match="绝对路径"):
            controller._validate_commit_repo("relative/path")
        with pytest.raises(ValueError, match="不是 Git 仓库"):
            controller._validate_commit_repo(str(plain))
    finally:
        controller.close()
