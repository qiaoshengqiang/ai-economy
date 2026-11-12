"""常规设置 / Worker Roles 的 Controller 契约测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from llm import prompt_log
from work.controller import AIEOSController


class _Directory:
    def __init__(self, ids):
        self.entries = [SimpleNamespace(agent_id=aid) for aid in ids]

    def all(self):
        return list(self.entries)


def _controller(tmp_path, **kwargs):
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b", "agent_c"]),
        economy=SimpleNamespace(
            agent=lambda aid: SimpleNamespace(has_brain=True)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    return AIEOSController(
        runtime,
        front_agent="agent_a",
        settings_path=tmp_path / "ui_settings.json",
        **kwargs,
    )


def test_regular_settings_round_trip(tmp_path):
    path = tmp_path / "ui_settings.json"
    controller = _controller(tmp_path)
    try:
        controller.update_settings(
            max_parallel_teams=4,
            max_solo_turns=12,
            pi_work_agents=["agent_b"],
            disabled_agents=["agent_c"],
        )
    finally:
        controller.close()

    data = json.loads(path.read_text("utf-8"))
    assert data["max_parallel_teams"] == 4
    assert data["max_solo_turns"] == 12
    assert data["pi_work_agents"] == ["agent_b"]
    assert data["disabled_agents"] == ["agent_c"]

    reloaded = _controller(tmp_path)
    try:
        settings = reloaded.settings_dict()
        assert settings["max_solo_turns"] == 12
        assert settings["pi_work_agents"] == ["agent_b"]
        assert settings["disabled_agents"] == ["agent_c"]
        facts = {item["agent_id"]: item for item in reloaded._agent_facts()}
        assert facts["agent_c"]["can_work"] is False
    finally:
        reloaded.close()


def test_front_agent_cannot_be_disabled(tmp_path):
    controller = _controller(tmp_path)
    try:
        try:
            controller.update_settings(disabled_agents=["agent_a"])
        except ValueError as exc:
            assert "Front Agent" in str(exc)
        else:
            raise AssertionError("Front Agent 不应该能被停用")
    finally:
        controller.close()


def test_prompt_log_toggle_round_trip(tmp_path):
    path = tmp_path / "ui_settings.json"
    controller = _controller(
        tmp_path, prompt_log_path=tmp_path / "prompts")
    try:
        controller.update_settings(prompt_log_enabled=True)
        assert prompt_log.enabled() is True
        assert json.loads(path.read_text("utf-8"))[
            "prompt_log_enabled"] is True
        controller.update_settings(prompt_log_enabled=False)
        assert prompt_log.enabled() is False
    finally:
        controller.close()
    assert prompt_log.enabled() is False


def test_worker_roles_crud_through_controller(tmp_path):
    roles_path = tmp_path / "worker_roles.yaml"
    controller = _controller(tmp_path, worker_roles_path=roles_path)
    try:
        controller.apply_worker_roles("upsert", {
            "role_id": "data_engineer",
            "name": "数据工程师",
            "description": "数据处理",
            "system_prompt": "Work as a data engineer.",
            "summary_sections": ["Data validation"],
        })
        snapshot = controller.settings_snapshot()
        ids = {item["role_id"] for item in snapshot["roles"]["roles"]}
        assert "data_engineer" in ids
        assert roles_path.exists()

        controller.apply_worker_roles("archive", {"role_id": "data_engineer"})
        snapshot = controller.settings_snapshot()
        role = next(
            item for item in snapshot["roles"]["roles"]
            if item["role_id"] == "data_engineer")
        assert role["archived"] is True
    finally:
        controller.close()
