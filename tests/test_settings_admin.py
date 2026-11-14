"""系统设置写入口测试（providers / models / credentials）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from core.paths import GLOBAL_CONFIG_FILE, GLOBAL_LLM_CONFIG_FILE
from work.settings_admin import SettingsAdminError, SystemConfigAdmin


def _admin(tmp_path: Path) -> SystemConfigAdmin:
    config = tmp_path / "config.yaml"
    llm = tmp_path / "llm.yaml"
    env = tmp_path / ".env"
    shutil.copy(GLOBAL_CONFIG_FILE, config)
    shutil.copy(GLOBAL_LLM_CONFIG_FILE, llm)
    env.write_text("DEEPSEEK_KEY_AGENT_C=existing\n", encoding="utf-8")
    return SystemConfigAdmin(
        config_path=config,
        llm_path=llm,
        env_path=env,
        backup_dir=tmp_path / "backups",
    )


def test_snapshot_never_returns_secret_material(tmp_path):
    admin = _admin(tmp_path)
    snap = admin.snapshot()

    text = str(snap)
    assert "existing" not in text
    assert snap["credentials"]["deepseek_key_c"]["present"] is True
    assert snap["credentials"]["deepseek_key_c"]["env"] == "DEEPSEEK_KEY_AGENT_C"


def test_add_provider_model_and_credential(tmp_path):
    admin = _admin(tmp_path)
    admin.apply("provider.upsert", {
        "name": "local",
        "adapter": "openai_compatible",
        "base_url": "http://127.0.0.1:1234/v1",
        "timeout_seconds": 30,
    })
    admin.apply("model.upsert", {
        "profile": "local_model",
        "provider": "local",
        "model_id": "local-1",
        "context_window": 32000,
        "max_output_tokens": 4096,
        "supports_reasoning": False,
    })
    admin.apply("credential.upsert", {
        "profile": "local_key",
        "env": "LOCAL_API_KEY",
        "api_key": "sk-local-secret",
    })

    snap = admin.snapshot()
    assert snap["providers"]["local"]["adapter"] == "openai_compatible"
    assert snap["models"]["local_model"]["context_window"] == 32000
    assert snap["credentials"]["local_key"]["present"] is True
    assert snap["restart_required"] is True

    llm_text = (tmp_path / "llm.yaml").read_text(encoding="utf-8")
    assert "sk-local-secret" not in llm_text
    assert "LOCAL_API_KEY" in llm_text
    assert "LOCAL_API_KEY=sk-local-secret" in (
        tmp_path / ".env").read_text(encoding="utf-8")


def test_adapter_specs_expose_default_base_url():
    from llm.router import adapter_specs

    specs = adapter_specs()
    assert specs["deepseek"]["default_base_url"] == "https://api.deepseek.com"
    assert specs["bailian"]["default_base_url"].endswith(
        "/compatible-mode/v1")
    assert specs["openai_compatible"]["default_base_url"] == ""
    assert specs["openai_compatible"]["label"]


def test_provider_can_use_adapter_default_base_url(tmp_path):
    admin = _admin(tmp_path)
    admin.apply("provider.upsert", {
        "name": "deepseek_local",
        "adapter": "deepseek",
        "timeout_seconds": 30,
    })

    data = yaml.safe_load(
        (tmp_path / "llm.yaml").read_text(encoding="utf-8"))
    assert data["providers"]["deepseek_local"]["base_url"] == (
        "https://api.deepseek.com")


def test_opencode_provider_credential_and_models_round_trip(tmp_path):
    admin = _admin(tmp_path)
    admin.apply("provider.upsert", {
        "name": "opencode",
        "adapter": "openai_compatible",
        "base_url": "https://opencode.ai/inference/openai/v1",
        "timeout_seconds": 300,
    })
    admin.apply("credential.upsert", {
        "profile": "opencode_key",
        "env": "OPENCODE_API_KEY",
        "api_key": "oc_sk_test_secret",
    })
    admin.apply("model.upsert", {
        "profile": "opencode_deepseek",
        "provider": "opencode",
        "model_id": "deepseek-v4.1-flash",
        "context_window": 1000000,
        "max_output_tokens": 32768,
    })

    snap = admin.snapshot()
    assert snap["providers"]["opencode"]["adapter"] == "openai_compatible"
    assert snap["providers"]["opencode"]["base_url"] == (
        "https://opencode.ai/inference/openai/v1")
    assert snap["models"]["opencode_deepseek"]["provider"] == "opencode"
    assert snap["credentials"]["opencode_key"]["env"] == "OPENCODE_API_KEY"
    adapter = next(
        item for item in snap["adapters"]
        if item["name"] == "openai_compatible")
    assert adapter["default_base_url"] == ""


def test_update_agent_model_and_credential_preserves_config_text(tmp_path):
    admin = _admin(tmp_path)
    before_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    before = yaml.safe_load(before_text)
    other_model = before["agents"]["agent_a"]["brain"]["model"]

    snap = admin.apply("agent.update", {
        "agent_id": "agent_b",
        "model": "opencode_glm",
        "credential": "opencode_key",
    })
    assert snap["updated_agent"] == "agent_b"
    assert snap["restart_required"] is True

    after_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    # 文本补丁不能抹掉文件头注释与手工排版。
    assert "# AI Economy OS v0.2.0 - 全局配置" in after_text
    after = yaml.safe_load(after_text)
    assert after["agents"]["agent_b"]["brain"]["model"] == "opencode_glm"
    assert after["agents"]["agent_b"]["brain"]["credential"] == "opencode_key"
    assert after["agents"]["agent_a"]["brain"]["model"] == other_model

    from core.config_resolver import load_resolved_config
    from llm.router import adapter_specs

    resolved = load_resolved_config(
        tmp_path / "config.yaml", adapter_specs=adapter_specs)
    agent = resolved.agent("agent_b")
    assert agent.model_profile == "opencode_glm"
    assert agent.credential_profile == "opencode_key"

    with pytest.raises(SettingsAdminError):
        admin.apply("agent.update", {
            "agent_id": "agent_b",
            "model": "no_such_model",
            "credential": "opencode_key",
        })
    with pytest.raises(SettingsAdminError):
        admin.apply("agent.update", {
            "agent_id": "no_such_agent",
            "model": "opencode_glm",
            "credential": "opencode_key",
        })


def test_model_context_limits_are_validated(tmp_path):
    admin = _admin(tmp_path)
    with pytest.raises(SettingsAdminError):
        admin.apply("model.upsert", {
            "profile": "bad_model",
            "provider": "deepseek",
            "model_id": "bad",
            "context_window": 1000,
            "max_output_tokens": 2000,
        })


def test_delete_provider_is_blocked_while_models_reference_it(tmp_path):
    admin = _admin(tmp_path)
    with pytest.raises(SettingsAdminError) as excinfo:
        admin.apply("provider.delete", {"name": "deepseek"})
    assert "仍被模型使用" in str(excinfo.value)


def test_delete_credential_is_blocked_while_agents_reference_it(tmp_path):
    admin = _admin(tmp_path)
    with pytest.raises(SettingsAdminError) as excinfo:
        admin.apply("credential.delete", {"profile": "deepseek_key_a"})
    assert "仍被 Agent 使用" in str(excinfo.value)


def test_create_agent_appends_config_and_requires_restart(tmp_path):
    admin = _admin(tmp_path)
    assert admin.snapshot()["restart_required"] is False

    snap = admin.apply("agent.create", {
        "agent_id": "agent_test_new",
        "name": "小福测试",
        "model": "deepseek_chat",
        "credential": "deepseek_key_a",
        "initial_balance": 100,
    })
    assert snap["created_agent"] == "agent_test_new"
    assert snap["restart_required"] is True

    data = yaml.safe_load(
        (tmp_path / "config.yaml").read_text(encoding="utf-8"))
    agent = data["agents"]["agent_test_new"]
    assert agent["name"] == "小福测试"
    assert agent["runtime"] is True
    assert agent["brain"]["model"] == "deepseek_chat"
    assert agent["brain"]["credential"] == "deepseek_key_a"
    assert agent["economy"]["initial_balance"] == 100.0

    from core.config_resolver import load_resolved_config
    from llm.router import adapter_specs

    resolved = load_resolved_config(
        tmp_path / "config.yaml", adapter_specs=adapter_specs)
    assert "agent_test_new" in resolved.runtime_agents
    assert resolved.agent("agent_test_new").name == "小福测试"

    with pytest.raises(SettingsAdminError):
        admin.apply("agent.create", {
            "agent_id": "agent_test_new",
            "name": "重复",
            "model": "deepseek_chat",
            "credential": "deepseek_key_a",
            "initial_balance": 100,
        })
    with pytest.raises(SettingsAdminError):
        admin.apply("agent.create", {
            "agent_id": "Bad-ID",
            "name": "坏 ID",
            "model": "deepseek_chat",
            "credential": "deepseek_key_a",
            "initial_balance": 100,
        })
    with pytest.raises(SettingsAdminError):
        admin.apply("agent.create", {
            "agent_id": "agent_g",
            "name": "坏模型",
            "model": "no_such_model",
            "credential": "deepseek_key_a",
            "initial_balance": 100,
        })


def test_llm_registry_keeps_only_canonical_sections(tmp_path):
    admin = _admin(tmp_path)
    admin.apply("provider.upsert", {
        "name": "extra",
        "adapter": "mock",
        "timeout_seconds": 10,
    })
    data = yaml.safe_load((tmp_path / "llm.yaml").read_text("utf-8"))
    assert set(data) == {"version", "providers", "models", "credentials"}
    assert "extra" in data["providers"]
