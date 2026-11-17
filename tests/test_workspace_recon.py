"""Workspace Recon：Worker 先判断“新建 / 修改”，再决定目标与快照范围。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work.task_directory import TaskDirectoryManager
from work.workspace_plan import (
    WorkspacePlanError,
    parse_workspace_plan,
)


def test_plan_create_for_missing_subdir(tmp_path):
    plan = parse_workspace_plan(
        json.dumps({
            "mode": "create",
            "target_path": "pelican-cycling",
            "reason": "目录不存在",
        }),
        workspace_root=tmp_path,
    )
    assert plan.mode == "create"
    assert plan.target_path == "pelican-cycling"


def test_plan_modify_for_existing_project(tmp_path):
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "index.html").write_text("x", encoding="utf-8")
    plan = parse_workspace_plan(
        '{"mode":"modify","target_path":"game"}',
        workspace_root=tmp_path,
    )
    assert plan.mode == "modify"
    assert plan.target_path == "game"


def test_plan_create_rejects_nonempty_existing_target(tmp_path):
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "index.html").write_text("x", encoding="utf-8")
    with pytest.raises(WorkspacePlanError, match="已存在且非空"):
        parse_workspace_plan(
            '{"mode":"create","target_path":"game"}',
            workspace_root=tmp_path,
        )


def test_plan_modify_rejects_missing_target(tmp_path):
    with pytest.raises(WorkspacePlanError, match="不存在"):
        parse_workspace_plan(
            '{"mode":"modify","target_path":"missing"}',
            workspace_root=tmp_path,
        )


@pytest.mark.parametrize("target", ["../escape", "/tmp/escape"])
def test_plan_rejects_paths_outside_root(tmp_path, target):
    with pytest.raises(WorkspacePlanError):
        parse_workspace_plan(
            json.dumps({"mode": "create", "target_path": target}),
            workspace_root=tmp_path,
        )


def test_plan_unclear_is_kept_but_not_actionable(tmp_path):
    plan = parse_workspace_plan(
        '{"mode":"unclear","target_path":".","reason":"ambiguous"}',
        workspace_root=tmp_path,
    )
    assert plan.mode == "unclear"
    assert plan.is_clear is False


def test_plan_accepts_fenced_json(tmp_path):
    plan = parse_workspace_plan(
        '```json\n{"mode":"create","target_path":"new-game"}\n```',
        workspace_root=tmp_path,
    )
    assert plan.mode == "create"
    assert plan.target_path == "new-game"


def test_task_directory_target_is_inside_root_and_created(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    directory = TaskDirectoryManager(tmp_path / "tasks").for_task(
        "T-RECON", workdir=root)
    directory.ensure()

    target = directory.set_project_target("pelican-cycling")
    assert target == (root / "pelican-cycling").resolve()
    assert directory.workdir == target
    directory.ensure_project_target(create=True)
    assert target.is_dir()

    with pytest.raises(ValueError, match="\\.\\."):
        directory.set_project_target("../escape")


def test_task_directory_reloads_target_from_workspace_plan(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    manager = TaskDirectoryManager(tmp_path / "tasks")
    directory = manager.for_task("T-RECON-RELOAD", workdir=root)
    directory.ensure()
    directory.set_project_target("pelican-cycling")
    directory.ensure_project_target(create=True)
    directory.write_json("workspace_plan.json", {
        "mode": "create",
        "target_path": "pelican-cycling",
        "reason": "new subdir",
    })

    reloaded = manager.for_task("T-RECON-RELOAD", workdir=root)
    assert reloaded.project_target == "pelican-cycling"
    assert reloaded.workdir == (root / "pelican-cycling").resolve()
