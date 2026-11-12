"""WorkerRoleStore 的持久化 / 归档 / 校验测试。"""

from __future__ import annotations

import pytest

from work.worker_roles import WorkerRoleError, WorkerRoleStore


def test_custom_role_round_trips_to_disk(tmp_path):
    path = tmp_path / "worker_roles.yaml"
    store = WorkerRoleStore(path)
    store.upsert({
        "role_id": "data_engineer",
        "name": "数据工程师",
        "description": "数据处理与验证",
        "system_prompt": "Work as a data engineer.",
        "summary_sections": ["Data sources", "Validation"],
    })

    reloaded = WorkerRoleStore(path)
    role = reloaded.resolve("data_engineer")
    assert role is not None
    assert role.name == "数据工程师"
    assert role.summary_sections == ["Data sources", "Validation"]
    assert reloaded.catalog()


def test_archive_hides_role_from_catalog_but_keeps_resume_lookup(tmp_path):
    store = WorkerRoleStore(tmp_path / "worker_roles.yaml")
    store.upsert({
        "role_id": "temporary_role",
        "name": "临时角色",
        "description": "temporary",
        "system_prompt": "temporary prompt",
    })
    store.archive("temporary_role")

    assert store.resolve("temporary_role") is None
    assert store.resolve("temporary_role", include_archived=True) is not None
    assert "temporary_role" not in {item["id"] for item in store.catalog()}


def test_generalist_cannot_be_archived(tmp_path):
    store = WorkerRoleStore(tmp_path / "worker_roles.yaml")
    with pytest.raises(WorkerRoleError):
        store.archive("generalist")


def test_unknown_role_is_not_silently_downgraded(tmp_path):
    store = WorkerRoleStore(tmp_path / "worker_roles.yaml")
    assert store.resolve("not_a_role") is None


def test_reset_restores_builtin_role(tmp_path):
    store = WorkerRoleStore(tmp_path / "worker_roles.yaml")
    store.upsert({
        "role_id": "writer",
        "name": "改过的写作者",
        "description": "changed",
        "system_prompt": "changed prompt",
    })
    store.reset("writer")
    role = store.resolve("writer")
    assert role is not None
    assert role.name == "写作者"
    assert role.system_prompt != "changed prompt"
