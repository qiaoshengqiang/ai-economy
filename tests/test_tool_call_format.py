"""按 **model profile** 选择工具调用格式：`standard` / `dsml`。

背景：DSML 解析原本只挂在 `DeepSeekProvider` 上；同一个模型经
OpenCode 这类 OpenAI-compatible 代理调用时，DSML 会原样出现在
`content` 里，而通用 adapter 只读标准 `tool_calls`，于是「模型调用了
工具」在系统里变成「连续两轮没有动作」。

🔒 这个文件同时守三条边界：

1. 默认值：DeepSeek adapter 默认 `dsml`（保持历史行为），
   其他 adapter 默认 `standard`（不误伤 Qwen / Kimi / GLM）。
2. 显式声明：OpenAI-compatible 代理上的 DeepSeek 模型必须写
   `tool_call_format: dsml` 才会恢复。
3. UI / 系统设置保存时必须**合并**旧字段，不能把
   `supports_image_input` 之类静默删掉。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from core.config_resolver import (
    ConfigResolver,
    ConfigurationError,
)
from core.paths import GLOBAL_CONFIG_FILE, GLOBAL_LLM_CONFIG_FILE
from llm.base import LLMResponse
from llm.deepseek import DeepSeekProvider
from llm.openai_compatible import HTTPResult, OpenAICompatibleProvider
from work.settings_admin import SettingsAdminError, SystemConfigAdmin

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dsml"


# ============================================================
# 公共工具
# ============================================================
def _dsml_text() -> str:
    return (FIXTURES / "tool_help_double_bar.txt").read_text(encoding="utf-8")


def _canonical() -> dict:
    """最小 canonical 配置：一个 deepseek adapter，一个代理。"""
    return {
        "version": 1,
        "runtime": {
            "log_level": "INFO",
            "database_path": "data/x.db",
            "message_claim_lease_seconds": 300,
            "default_provider": "proxy",
        },
        "providers": {
            "ds": {
                "adapter": "deepseek",
                "base_url": "https://api.deepseek.com",
                "timeout_seconds": 30,
                "default_model": "ds_model",
            },
            "proxy": {
                "adapter": "openai_compatible",
                "base_url": "https://proxy.example.com/v1",
                "timeout_seconds": 30,
                "default_model": "qwen_model",
            },
        },
        "models": {
            "ds_model": {
                "provider": "ds",
                "model_id": "deepseek-flash",
            },
            "qwen_model": {
                "provider": "proxy",
                "model_id": "qwen3.8-flash",
            },
            "ds_via_proxy": {
                "provider": "proxy",
                "model_id": "deepseek-v4.1-flash",
                "tool_call_format": "dsml",
            },
        },
        "credentials": {"key": {"env": "TEST_KEY"}},
        "agents": {
            "agent_one": {
                "name": "One",
                "brain": {"model": "ds_via_proxy", "credential": "key"},
            },
        },
    }


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, provider="proxy", model="proxy-model")


def _http_ok(content: str, tool_calls=None):
    """返回一个可注入的 HTTP 实现；同时把 payload 记下来给测试断言。"""
    seen = {}

    def post(url, payload, headers, timeout):
        seen["payload"] = dict(payload)
        message = {"role": "assistant", "content": content}
        if tool_calls is not None:
            message["tool_calls"] = tool_calls
        body = {
            "model": "proxy-model",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": message,
            }],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
            },
        }
        return HTTPResult(status=200, body=json.dumps(body), headers={})

    post.seen = seen
    return post


def _proxy(http_post=None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        {
            "base_url": "https://proxy.example.com/v1",
            "api_key_env": "NOT_USED",
        },
        default_model="proxy-model",
        http_post=http_post,
        api_key_override="test-key",
    )


def _deepseek(http_post=None) -> DeepSeekProvider:
    return DeepSeekProvider(
        {"base_url": "https://api.deepseek.com"},
        default_model="deepseek-chat",
        http_post=http_post,
        api_key_override="test-key",
    )


# ============================================================
# 1. Resolver：默认值与校验
# ============================================================
class TestResolverDefaults:
    def test_undeclared_format_defers_to_provider_default(self):
        """`core/` 不认识厂商：未声明就留空，由 Provider 决定。"""
        resolver = ConfigResolver(_canonical())
        assert resolver.model("ds_model").tool_call_format == ""
        assert resolver.model("qwen_model").tool_call_format == ""

    def test_explicit_dsml_on_proxy_is_kept(self):
        model = ConfigResolver(_canonical()).model("ds_via_proxy")
        assert model.tool_call_format == "dsml"

    def test_agent_carries_the_model_format(self):
        agent = ConfigResolver(_canonical()).agent("agent_one")
        assert agent.tool_call_format == "dsml"
        assert agent.to_dict()["tool_call_format"] == "dsml"

    def test_format_is_normalized_but_typos_are_rejected(self):
        cfg = _canonical()
        cfg["models"]["qwen_model"]["tool_call_format"] = " DSML "
        assert (ConfigResolver(cfg).model("qwen_model").tool_call_format
                == "dsml")

        cfg["models"]["qwen_model"]["tool_call_format"] = "xml"
        with pytest.raises(ConfigurationError) as excinfo:
            ConfigResolver(cfg).model("qwen_model")
        assert "tool_call_format" in str(excinfo.value)
        assert "standard" in str(excinfo.value)


# ============================================================
# 2. Provider：只有声明 dsml 的模型才恢复
# ============================================================
class TestProviderRecovery:
    def test_openai_compatible_with_dsml_recovers_tool_calls(self):
        text = _dsml_text()
        out = _proxy()._augment_response(
            _response(text), {"content": text}, tool_call_format="dsml")

        assert out.tool_calls_source == "dsml"
        assert len(out.tool_calls) == 2
        assert out.tool_calls[0]["function"]["name"] == "tool_help"
        assert out.metadata["dsml"]["parsed"] is True

    def test_openai_compatible_with_standard_does_not_touch_dsml(self):
        text = _dsml_text()
        out = _proxy()._augment_response(
            _response(text), {"content": text},
            tool_call_format="standard")

        assert out.tool_calls == []
        assert out.tool_calls_source == "none"
        assert out.content == text
        assert "dsml" not in out.metadata

    def test_default_without_declaration_is_standard_on_base_provider(self):
        """直接调用基类（未声明格式）不能被当成 DSML。"""
        text = _dsml_text()
        out = _proxy()._augment_response(_response(text), {"content": text})

        assert out.tool_calls_source == "none"
        assert out.content == text

    def test_standard_tool_calls_win_over_dsml(self):
        text = _dsml_text()
        message = {
            "content": text,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "workspace_read",
                    "arguments": json.dumps({"path": "index.html"}),
                },
            }],
        }
        out = _proxy()._augment_response(
            _response(text), message, tool_call_format="dsml")

        assert out.tool_calls_source == "api"
        assert out.tool_calls[0]["function"]["name"] == "workspace_read"
        assert out.content == text, "有标准通道时不应改 content"

    def test_deepseek_without_declaration_keeps_dsml_default(self):
        text = _dsml_text()
        out = _deepseek()._augment_response(_response(text),
                                            {"content": text})

        assert out.tool_calls_source == "dsml"
        assert len(out.tool_calls) == 2

    def test_deepseek_explicit_standard_disables_recovery(self):
        text = _dsml_text()
        out = _deepseek()._augment_response(
            _response(text), {"content": text},
            tool_call_format="standard")

        assert out.tool_calls == []
        assert out.tool_calls_source == "none"
        assert out.content == text


# ============================================================
# 3. generate 链路：格式必须透传，且不能泄漏进 payload
# ============================================================
class TestGenerateWiring:
    def test_dsml_flag_is_used_by_generate_and_not_sent_to_api(self):
        post = _http_ok(_dsml_text())
        response = _proxy(post).generate(
            messages="hello", tool_call_format="dsml")

        assert response.tool_calls_source == "dsml"
        assert len(response.tool_calls) == 2
        assert "tool_call_format" not in post.seen["payload"]

    def test_standard_flag_leaves_dsml_as_text(self):
        text = _dsml_text()
        post = _http_ok(text)
        response = _proxy(post).generate(
            messages="hello", tool_call_format="standard")

        assert response.tool_calls_source == "none"
        assert response.content == text
        assert "tool_call_format" not in post.seen["payload"]

    def test_standard_api_calls_are_preferred(self):
        post = _http_ok(_dsml_text(), tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "workspace_read",
                "arguments": json.dumps({"path": "a.txt"}),
            },
        }])
        response = _proxy(post).generate(
            messages="hello", tool_call_format="dsml")

        assert response.tool_calls_source == "api"
        assert response.tool_calls[0]["function"]["name"] == "workspace_read"


# ============================================================
# 4. 系统设置：保存格式 + 保留未在表单里的字段
# ============================================================
def _admin(tmp_path: Path) -> SystemConfigAdmin:
    config = tmp_path / "config.yaml"
    llm = tmp_path / "llm.yaml"
    env = tmp_path / ".env"
    shutil.copy(GLOBAL_CONFIG_FILE, config)
    shutil.copy(GLOBAL_LLM_CONFIG_FILE, llm)
    env.write_text("", encoding="utf-8")
    return SystemConfigAdmin(
        config_path=config,
        llm_path=llm,
        env_path=env,
        backup_dir=tmp_path / "backups",
    )


def _llm_data(tmp_path: Path) -> dict:
    return yaml.safe_load(
        (tmp_path / "llm.yaml").read_text(encoding="utf-8"))


class TestSettingsAdmin:
    def test_model_upsert_round_trips_tool_call_format(self, tmp_path):
        admin = _admin(tmp_path)
        snap = admin.apply("model.upsert", {
            "profile": "mock_deepseek",
            "provider": "mock",
            "model_id": "mock-model-v1",
            "context_window": 128000,
            "max_output_tokens": 16384,
            "tool_call_format": "dsml",
        })

        assert snap["models"]["mock_deepseek"]["tool_call_format"] == "dsml"
        assert (_llm_data(tmp_path)["models"]["mock_deepseek"]
                ["tool_call_format"] == "dsml")

    def test_model_upsert_rejects_unknown_format(self, tmp_path):
        admin = _admin(tmp_path)
        with pytest.raises(SettingsAdminError) as excinfo:
            admin.apply("model.upsert", {
                "profile": "mock_bad_format",
                "provider": "mock",
                "model_id": "mock-model-v1",
                "context_window": 128000,
                "max_output_tokens": 16384,
                "tool_call_format": "qwen_xml",
            })
        assert "tool_call_format" in str(excinfo.value)

    def test_model_upsert_merges_and_preserves_extra_fields(self, tmp_path):
        """编辑模型不能把 UI 表单之外的字段静默删掉。"""
        admin = _admin(tmp_path)
        data = _llm_data(tmp_path)
        data["models"]["mock_model"]["supports_image_input"] = True
        data["models"]["mock_model"]["tool_call_format"] = "dsml"
        data["models"]["mock_model"]["future_field"] = "keep-me"
        (tmp_path / "llm.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8")

        snap = admin.apply("model.upsert", {
            "profile": "mock_model",
            "provider": "mock",
            "model_id": "mock-model-v1",
            "context_window": 128000,
            "max_output_tokens": 16384,
            "supports_reasoning": False,
            "thinking_levels": ["off"],
            # 故意不传 tool_call_format：旧客户端 / 旧表单也不能把 dsml 降级。
        })

        saved = snap["models"]["mock_model"]
        assert saved["supports_image_input"] is True
        assert saved["future_field"] == "keep-me"
        assert saved["tool_call_format"] == "dsml"

    def test_model_upsert_can_switch_back_to_standard(self, tmp_path):
        admin = _admin(tmp_path)
        data = _llm_data(tmp_path)
        data["models"]["mock_model"]["tool_call_format"] = "dsml"
        (tmp_path / "llm.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8")

        snap = admin.apply("model.upsert", {
            "profile": "mock_model",
            "provider": "mock",
            "model_id": "mock-model-v1",
            "context_window": 128000,
            "max_output_tokens": 16384,
            "tool_call_format": "standard",
        })

        assert snap["models"]["mock_model"]["tool_call_format"] == "standard"

    def test_old_client_does_not_write_a_guessed_format(self, tmp_path):
        """旧客户端不传字段时留空，让 Provider 默认值继续生效。"""
        admin = _admin(tmp_path)
        snap = admin.apply("model.upsert", {
            "profile": "deepseek_chat",
            "provider": "deepseek",
            "model_id": "deepseek-chat",
            "context_window": 600000,
            "max_output_tokens": 32768,
            "supports_reasoning": True,
            "thinking_levels": ["off", "low", "high", "max"],
        })

        assert "tool_call_format" not in snap["models"]["deepseek_chat"]
