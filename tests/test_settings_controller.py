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


def test_agent_thinking_round_trip(tmp_path):
    path = tmp_path / "ui_settings.json"
    controller = _controller(tmp_path)
    try:
        controller.update_settings(
            agent_thinking={"agent_c": "low"})
        assert controller.settings_dict()["agent_thinking"] == {
            "agent_c": "low"}
        state = controller.state()
        agent_c = next(
            item for item in state["agents"]
            if item["agent_id"] == "agent_c")
        assert agent_c["thinking"] == "low"
    finally:
        controller.close()

    data = json.loads(path.read_text("utf-8"))
    assert data["agent_thinking"] == {"agent_c": "low"}


def test_pi_timeout_switch_round_trip(tmp_path):
    path = tmp_path / "ui_settings.json"
    controller = _controller(tmp_path)
    try:
        controller.update_settings(
            pi_timeout_enabled=False, pi_timeout_seconds=900)
        assert controller.settings_dict()["pi_timeout_enabled"] is False
        assert controller._pi_timeout() == 0.0
    finally:
        controller.close()

    data = json.loads(path.read_text("utf-8"))
    assert data["pi_timeout_enabled"] is False
    assert data["pi_timeout_seconds"] == 900
    reloaded = _controller(tmp_path)
    try:
        assert reloaded.settings_dict()["pi_timeout_enabled"] is False
        reloaded.update_settings(pi_timeout_enabled=True)
        assert reloaded._pi_timeout() == 900
    finally:
        reloaded.close()


def test_work_mode_round_trip(tmp_path):
    path = tmp_path / "ui_settings.json"
    controller = _controller(tmp_path)
    try:
        assert controller.settings_dict()["work_mode"] == "workflow"
        controller.update_settings(work_mode="autonomous")
        assert controller.settings_dict()["work_mode"] == "autonomous"
    finally:
        controller.close()

    data = json.loads(path.read_text("utf-8"))
    assert data["work_mode"] == "autonomous"
    reloaded = _controller(tmp_path)
    try:
        assert reloaded.settings_dict()["work_mode"] == "autonomous"
        try:
            reloaded.update_settings(work_mode="unknown")
        except ValueError as exc:
            assert "work_mode" in str(exc)
        else:
            raise AssertionError("unknown work_mode should fail")
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


def test_front_mode_round_trip(tmp_path):
    path = tmp_path / "ui_settings.json"
    controller = _controller(tmp_path)
    try:
        assert controller.settings_dict()["front_mode"] == "autonomous"
        controller.update_settings(front_mode="relay")
        assert controller.settings_dict()["front_mode"] == "relay"
    finally:
        controller.close()

    data = json.loads(path.read_text("utf-8"))
    assert data["front_mode"] == "relay"
    reloaded = _controller(tmp_path)
    try:
        assert reloaded.settings_dict()["front_mode"] == "relay"
        try:
            reloaded.update_settings(front_mode="unknown")
        except ValueError as exc:
            assert "front_mode" in str(exc)
        else:
            raise AssertionError("unknown front_mode should fail")
    finally:
        reloaded.close()


def test_relay_front_mode_strips_brief_extras(tmp_path):
    from front import FrontBrief, FrontDecision, FrontIntent

    controller = _controller(tmp_path)
    decision = FrontDecision(
        intent=FrontIntent.HANDOFF,
        brief=FrontBrief(
            goal="做一个更完整的赛车游戏",
            human_text="我想玩游戏，再给 agent_b 一个任务，做一个赛车游戏。",
            task_text="做一个赛车游戏",
            deliverables=["index.html"],
            constraints=["不要使用外部依赖"],
            acceptance_expectations=["能玩"],
            ambiguities=["画风"],
            non_goals=["联机"],
            context_refs=["ref"],
            files=[{"path": "a.txt", "content": "x"}],
        ),
        role="game_developer",
        work_profile="creative_product",
    )
    controller.front_mode = "relay"
    try:
        controller._apply_front_mode(decision)
    finally:
        controller.close()

    assert decision.brief.task_text == "做一个赛车游戏"
    assert decision.brief.goal == "做一个赛车游戏"
    assert decision.brief.deliverables == []
    assert decision.brief.constraints == []
    assert decision.brief.acceptance_expectations == []
    assert decision.brief.ambiguities == []
    assert decision.brief.non_goals == []
    assert decision.brief.context_refs == []
    assert decision.brief.files == []


def test_front_set_mode_control_updates_both_modes(tmp_path):
    from front import FrontDecision, FrontIntent
    from work.controller import WorkJob

    path = tmp_path / "ui_settings.json"
    controller = _controller(tmp_path)
    job = WorkJob(
        job_id="job_modes",
        human_text="以后 Front 只传达，Worker 自主工作",
    )
    decision = FrontDecision(
        intent=FrontIntent.SET_MODE,
        front_mode="relay",
        work_mode="autonomous",
        reply="已切换：Front 只传达，Work 自主。",
    )
    try:
        controller._apply_control_decision(job, decision)
    finally:
        controller.close()

    data = json.loads(path.read_text("utf-8"))
    assert data["front_mode"] == "relay"
    assert data["work_mode"] == "autonomous"
    assert job.status == "DIRECT_REPLY"
    assert job.summary == "已切换：Front 只传达，Work 自主。"


def test_front_thinking_setting_updates_decision_policy(tmp_path):
    from types import SimpleNamespace

    class _Brain:
        def __init__(self):
            self.calls = []
            self.settings = SimpleNamespace(
                thinking_levels=("off", "low", "high", "max"))

        def set_thinking(self, enabled, effort, *, phase):
            self.calls.append((enabled, effort, phase))

        def thinking_for(self, phase):
            return None

    brain = _Brain()
    runtime = SimpleNamespace(
        directory=_Directory(["agent_a", "agent_b"]),
        economy=SimpleNamespace(agent=lambda aid: SimpleNamespace(
            has_brain=True, brain=brain)),
        tasks=SimpleNamespace(list_tasks=lambda limit=50: []),
    )
    controller = AIEOSController(
        runtime, front_agent="agent_a",
        settings_path=tmp_path / "ui_settings.json")
    try:
        assert brain.calls[-1] == (True, "low", "decision")
        controller.update_settings(agent_thinking={"agent_a": "high"})
        assert brain.calls[-1] == (True, "high", "decision")
        controller.update_settings(agent_thinking={"agent_a": "off"})
        assert brain.calls[-1] == (False, None, "decision")
    finally:
        controller.close()
