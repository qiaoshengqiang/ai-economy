"""
LLM 可观测性 + Cognitive Budget V1
========================================================

【为什么需要这个文件】

    审计发现 `llm_calls` **回答不了**两个基本问题：
        · 这一条调用到底用了哪个 credential？
        · 请求的模型和响应的模型是不是同一个？

    以及一个诊断问题：
        · `output_tokens == 4096` 到底是截断还是巧合？（缺 finish_reason）

    本文件把这三件事钉住。
"""

from __future__ import annotations

import pytest

from llm.base import LLMResponse
from llm.openai_compatible import OpenAICompatibleProvider


def _provider():
    return OpenAICompatibleProvider(
        provider_config={"base_url": "https://example.invalid/v1",
                         "api_key_env": "NOT_USED"},
        default_model="test-model")


# ================================================================
# §六：requested_model ≠ response_model，二者绝不互相覆盖
# ================================================================
class TestModelIsSplitIntoTwoFacts:
    def test_requested_and_response_are_separate_fields(self):
        r = LLMResponse(content="x", provider="deepseek",
                        model="deepseek-flash", requested_model="deepseek-chat")
        assert r.response_model == "deepseek-flash"
        assert r.requested_model == "deepseek-chat"
        assert r.model_mismatch is True

    def test_agreeing_models_are_not_a_mismatch(self):
        r = LLMResponse(content="x", provider="p", model="m",
                        requested_model="m")
        assert r.model_mismatch is False

    def test_missing_requested_model_is_unknown_not_a_mismatch(self):
        """🔒 没记录 ≠ 不一致。不知道就是不知道。"""
        r = LLMResponse(content="x", provider="p", model="m")
        assert r.requested_model is None
        assert r.model_mismatch is False

    def test_provider_records_both(self):
        """真 Provider 解析时，请求模型与响应模型各记各的。"""
        import json
        from llm.openai_compatible import HTTPResult

        body = {"model": "echoed-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                          "total_tokens": 3}}
        resp = _provider().parse_response(
            HTTPResult(status=200, body=json.dumps(body), headers={}),
            model="requested-model", latency_ms=1.0)

        assert resp.requested_model == "requested-model"
        assert resp.response_model == "echoed-model"
        assert resp.model_mismatch is True


# ================================================================
# §八：finish_reason 必须被保存
# ================================================================
class TestFinishReasonIsRecorded:
    @pytest.mark.parametrize("fr", ["stop", "length"])
    def test_finish_reason_lands_on_the_response(self, fr):
        import json
        from llm.openai_compatible import HTTPResult

        body = {"model": "m",
                "choices": [{"index": 0, "finish_reason": fr,
                             "message": {"role": "assistant", "content": "x"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}
        resp = _provider().parse_response(
            HTTPResult(status=200, body=json.dumps(body), headers={}),
            model="m", latency_ms=1.0)
        assert resp.finish_reason == fr

    def test_finish_reason_reaches_the_db(self):
        from llm.base import LLMResponse as R
        from llm.cost import CostCalculator
        from llm.recorder import LLMCallRecorder
        from storage.database import create_memory_database

        db = create_memory_database(); db.initialize()
        rec = LLMCallRecorder(db=db, tokens_per_ai_coin=10000,
                              cost_calculator=CostCalculator.from_config({}))
        rec.record("agent_x", R(content="", provider="deepseek",
                                model="deepseek-flash",
                                requested_model="deepseek-chat",
                                output_tokens=4096, total_tokens=4349,
                                token_source="api", finish_reason="length"))

        row = db.fetchall("SELECT * FROM llm_calls")[0]
        assert row["finish_reason"] == "length"
        assert row["requested_model"] == "deepseek-chat"
        assert row["response_model"] == "deepseek-flash"
        assert row["output_status"] == "EMPTY_CONTENT"


# ================================================================
# §五：credential 可观测性（只记元信息，绝不记 secret）
# ================================================================
class TestCredentialIsObservable:
    def test_recorder_stores_credential_metadata(self):
        from llm.base import LLMResponse as R
        from llm.cost import CostCalculator
        from llm.recorder import LLMCallRecorder
        from storage.database import create_memory_database

        db = create_memory_database(); db.initialize()
        rec = LLMCallRecorder(db=db, tokens_per_ai_coin=10000,
                              cost_calculator=CostCalculator.from_config({}))
        rec.record("novel_author", R(content="x", provider="doubao", model="m"),
                   credential_ref="novel_author_doubao",
                   credential_source="agent_env",
                   credential_env="DOUBAO_API_KEY")

        row = db.fetchall("SELECT * FROM llm_calls")[0]
        assert row["requested_credential_ref"] == "novel_author_doubao"
        assert row["requested_credential_source"] == "agent_env"
        assert row["requested_credential_env"] == "DOUBAO_API_KEY"

    def test_brain_passes_env_name_not_the_secret(self):
        """🔒 `credential_env` 必须是**变量名**，绝不能是 key 本身。"""
        from agents.brain import Brain, BrainSettings

        b = Brain(router=None, settings=BrainSettings(
            agent_id="x", provider="deepseek", model="m"),
            credential_config={"api_key_env": "DEEPSEEK_KEY_AGENT_A"})
        meta = b._credential_meta("deepseek")

        assert meta["credential_env"] == "DEEPSEEK_KEY_AGENT_A"
        # 元信息里没有任何长随机串（key 的形状）
        assert all(len(str(v)) < 64 for v in meta.values())


# ================================================================
# §九：Cognitive Budget V1 —— Runtime 裁剪，不是 Agent 策略
# ================================================================
class TestCognitiveBudget:
    def test_agent_request_within_bounds_is_used(self):
        eff, why = OpenAICompatibleProvider.clip_cognitive_budget(8192, 16384)
        assert (eff, why) == (8192, None)

    def test_below_min_is_raised(self):
        eff, why = OpenAICompatibleProvider.clip_cognitive_budget(10, 16384)
        assert eff == OpenAICompatibleProvider.MIN_COGNITIVE_BUDGET
        assert why == "below_min"

    def test_above_max_is_capped(self):
        eff, why = OpenAICompatibleProvider.clip_cognitive_budget(
            10_000_000, 999_999)
        assert eff == OpenAICompatibleProvider.MAX_COGNITIVE_BUDGET
        assert why == "above_max"

    def test_above_caller_limit_is_capped_to_the_caller(self):
        eff, why = OpenAICompatibleProvider.clip_cognitive_budget(8192, 4096)
        assert (eff, why) == (4096, "above_caller_limit")

    def test_no_request_falls_back_to_caller_default(self):
        assert OpenAICompatibleProvider.clip_cognitive_budget(None, 4096) == \
            (4096, None)

    def test_no_aic_to_token_conversion_exists(self):
        """
        🔒 §九 明令禁止：**AIC balance → max_tokens 自动换算**。

        结构守卫：裁剪函数只看「请求值 / 调用方上限 / MIN / MAX」，
        签名里**没有** balance、wallet、cost 之类的东西。
        """
        import inspect

        sig = inspect.signature(
            OpenAICompatibleProvider.clip_cognitive_budget)
        names = set(sig.parameters)
        for forbidden in ("balance", "wallet", "cost", "aic", "ledger",
                          "spend", "budget_aic"):
            assert forbidden not in names, (
                f"clip_cognitive_budget 接受了 {forbidden!r} —— "
                f"那就是 AIC → Token Quota 的换算入口")

    def test_budget_reaches_the_response_and_the_db(self):
        import json
        from llm.openai_compatible import HTTPResult
        from llm.cost import CostCalculator
        from llm.recorder import LLMCallRecorder
        from storage.database import create_memory_database

        prov = _provider()
        body = {"model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "x"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}
        # 手动模拟 generate 里的裁剪（不发起网络）
        eff, _ = prov.clip_cognitive_budget(8192, 4096)
        prov._last_requested_budget = 8192
        prov._last_max_tokens = eff
        resp = prov.parse_response(
            HTTPResult(status=200, body=json.dumps(body), headers={}),
            model="m", latency_ms=1.0)

        assert resp.requested_cognitive_budget == 8192
        assert resp.actual_max_tokens == 4096      # 被调用方上限裁剪

        db = create_memory_database(); db.initialize()
        rec = LLMCallRecorder(db=db, tokens_per_ai_coin=10000,
                              cost_calculator=CostCalculator.from_config({}))
        rec.record("a", resp)
        row = db.fetchall("SELECT * FROM llm_calls")[0]
        assert row["requested_cognitive_budget"] == 8192
        assert row["actual_max_tokens"] == 4096
