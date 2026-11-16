"""Model profile 的图片能力声明。"""

from __future__ import annotations

from types import SimpleNamespace

from agents.brain import Brain, BrainSettings
from llm.base import LLMResponse


def test_brain_settings_reads_image_capability():
    resolved = SimpleNamespace(
        agent_id="agent_o",
        provider=SimpleNamespace(name="opencode"),
        model_id="deepseek-v4.1-flash",
        system_prompt="",
        temperature=0.7,
        max_tokens=1024,
        context_window=600000,
        max_output_tokens=1024,
        supports_reasoning=True,
        supports_image_input=True,
        thinking_levels=("off", "high"),
        credential_profile="opencode_key",
    )
    settings = BrainSettings.from_resolved_agent(resolved)
    assert settings.supports_image_input is True


def test_brain_passes_image_capability_to_router():
    class _Router:
        def __init__(self):
            self.kwargs = {}

        def generate(self, **kwargs):
            self.kwargs = kwargs
            return LLMResponse(
                content="ok", provider="opencode",
                model="deepseek-v4.1-flash",
                input_tokens=1, output_tokens=1, total_tokens=2)

    router = _Router()
    brain = Brain(
        router=router,
        settings=BrainSettings(
            agent_id="agent_o",
            provider="opencode",
            model="deepseek-v4.1-flash",
            supports_image_input=True,
        ),
    )
    brain.generate(messages="hi", record=False)
    assert router.kwargs["supports_image_input"] is True
