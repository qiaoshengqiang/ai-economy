"""config.yaml + llm.yaml 的 LLM 注册表拆分测试。"""

from __future__ import annotations

import pytest

from core.config_resolver import (
    ConfigLoader, ConfigurationError, load_resolved_config,
    merge_llm_sections,
)
from core.paths import GLOBAL_CONFIG_FILE
from llm.router import adapter_specs


def test_real_config_merges_llm_registry():
    canonical = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).canonical
    assert canonical["providers"]
    assert canonical["models"]
    assert canonical["credentials"]
    assert "deepseek" in canonical["providers"]


def test_model_context_and_output_limits_resolve():
    resolved = load_resolved_config(
        GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs)
    model = resolved.model("deepseek_chat")
    assert model.context_window == 1000000
    assert model.max_output_tokens == 16384
    assert model.supports_reasoning is True
    assert model.thinking_levels == ("off", "low", "high", "max")

    agent = resolved.agent("agent_c")
    assert agent.context_window == 1000000
    assert agent.max_output_tokens == 16384
    assert agent.thinking_levels == ("off", "low", "high", "max")


def test_duplicate_llm_section_is_rejected():
    with pytest.raises(ConfigurationError) as excinfo:
        merge_llm_sections(
            {"providers": {"a": {}}},
            {"providers": {"a": {}}},
        )
    assert "只能有一个权威来源" in str(excinfo.value)


def test_unknown_llm_section_is_rejected():
    with pytest.raises(ConfigurationError):
        merge_llm_sections({}, {"agents": {}})
