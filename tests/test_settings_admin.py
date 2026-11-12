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
