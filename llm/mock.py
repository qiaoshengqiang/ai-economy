"""
MockProvider —— 假的模型，真的链路
============================================================

【为什么这个文件是整个 Step 2 最重要的东西】

它一点都不智能。它不会推理，不会写代码，不会思考。
但它能让【整条 LLM 调用链】在没有一个 API Key 的情况下完整跑起来：

    Agent → Brain → Router → MockProvider → LLMResponse

这件事的价值在于：
    经济系统的逻辑（记账、成本、结算）可以先被完整地开发和测试，
    不用等模型接进来，也不用为每次调试花真金白银。

    如果 Mock 没跑通就去接真实 API，一旦出问题你分不清是
    「经济逻辑有 bug」还是「API 调用有 bug」——
    两个变量同时变，是最难排查的情况。

【它必须返回真实的 token 用量】
    即使没有真实成本，也要返回 input_tokens / output_tokens，
    这样「token → AI 币成本」这条链路以后能直接接上。

    但它会把自己标记成 simulated=True，
    让你在账本里能一眼区分「模拟数据」和「真花了钱」。

【这个文件里也没有任何厂商的名字】
    它不知道 DeepSeek 存在。它只是 LLMProvider 的一个实现。
"""

from __future__ import annotations

import re
import time
from typing import Any, Mapping, Optional, Sequence, Union

from llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    estimate_messages_tokens,
    estimate_tokens,
    normalize_messages,
)

# ============================================================
# 从 system prompt 里认出「这个 Agent 叫什么」
# ============================================================
# 为什么要做这件事？
#     因为这样 Mock 的回复能带上 Agent 的名字，
#     一眼就能看出「这次调用真的把 system_prompt 传下去了」。
#     如果回复永远是同一句话，你就无法判断参数有没有被正确传递。
#
# 支持中英文两种写法，遇到标点就停：
#     "你是 Agent A，一个独立的经济主体。"  → "Agent A"
#     "You are Agent A"                     → "Agent A"
_AGENT_NAME_PATTERNS = (
    re.compile(r"你是\s*([^，。,.;；\n]+)"),
    re.compile(r"you\s+are\s+([^,.;\n]+)", re.IGNORECASE),
)

# 回复里回显用户输入时，最多显示多少个字
_ECHO_PREVIEW_CHARS = 60


class MockProvider(LLMProvider):
    """
    一个不需要网络、不需要 Key、结果稳定的假模型。

    用法：
        provider = MockProvider()
        response = provider.generate(
            messages="Hello",
            system="你是 Agent A",
        )
        print(response.content)      # Mock response generated for Agent A.
        print(response.total_tokens) # 估算值
    """

    name = "mock"

    # Mock 不需要 API Key —— 这是它存在的全部意义
    requires_api_key = False

    def __init__(
        self,
        provider_config: Optional[Mapping[str, Any]] = None,
        default_model: str = "mock-model-v1",
        fixed_content: Optional[str] = None,
        fixed_input_tokens: Optional[int] = None,
        fixed_output_tokens: Optional[int] = None,
    ):
        """
        参数：
            provider_config:    配置里 llm.providers.mock 那一段
            default_model:      模型名（假的，但要有个名字好显示）
            fixed_content:      恒定回复。测试里想让输出完全可预测时用。
            fixed_input_tokens: 固定输入 token 数。不传就按文本估算。
            fixed_output_tokens:固定输出 token 数。不传就按文本估算。

        为什么 fixed_* 是构造参数而不是配置项？
            因为它们是【测试用的注入点】，不是运行时的业务配置。
            config.yaml 里塞这种东西，会让人误以为生产环境也该调它。
        """
        super().__init__(provider_config=provider_config, default_model=default_model)

        self.fixed_content = fixed_content
        self.fixed_input_tokens = fixed_input_tokens
        self.fixed_output_tokens = fixed_output_tokens

        # 统计一下被调用过多少次（调试有用，也能让测试验证调用确实发生了）
        self.call_count = 0

    # ------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------
    def generate(
        self,
        messages: Union[str, Sequence[Message], None] = None,
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        返回一个稳定的假回复。

        注意签名和 LLMProvider 完全一致 ——
        这样以后把 provider 换成 DeepSeek，调用方代码一个字都不用改。
        """
        started = time.perf_counter()

        normalized = normalize_messages(messages, system=system)

        # --- 组装回复内容 ---
        agent_name = self._detect_agent_name(normalized)
        content = self._compose_content(normalized, agent_name)

        # --- token 用量 ---
        input_tokens = (
            self.fixed_input_tokens
            if self.fixed_input_tokens is not None
            else estimate_messages_tokens(normalized)
        )
        output_tokens = (
            self.fixed_output_tokens
            if self.fixed_output_tokens is not None
            else estimate_tokens(content)
        )

        latency_ms = (time.perf_counter() - started) * 1000.0

        self.call_count += 1

        return LLMResponse(
            content=content,
            provider=self.name,
            model=self.resolve_model(model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            latency_ms=latency_ms,
            # 保留一个「原始返回」的样子。
            # 真实的 Provider 这里放的是厂商的原始 JSON；
            # Mock 就放一个结构类似的 dict，好让 raw_response
            # 这条链路也能被真正走一遍、被测试覆盖到。
            raw_response={
                "mock": True,
                "message_count": len(normalized),
                "detected_agent": agent_name,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            finish_reason="stop",
            simulated=True,
            metadata={
                "note": "这是模拟数据，不是真实模型返回",
                "agent_name": agent_name,
            },
        )

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------
    @staticmethod
    def _detect_agent_name(messages: Sequence[Mapping[str, str]]) -> Optional[str]:
        """从 system 消息里认出 Agent 的名字。认不出来返回 None。"""
        for message in messages:
            if message.get("role") != "system":
                continue

            content = str(message.get("content", ""))
            for pattern in _AGENT_NAME_PATTERNS:
                match = pattern.search(content)
                if match:
                    name = match.group(1).strip()
                    # 去掉可能拖着的尾巴标点
                    name = name.strip("：: 　")
                    if name:
                        return name
        return None

    def _compose_content(
        self,
        messages: Sequence[Mapping[str, str]],
        agent_name: Optional[str],
    ) -> str:
        """
        拼出回复内容。

        两种形态，对应两种使用场景：

            有 system prompt（正常调用 Agent）
                → "Mock response generated for Agent A."
                   证明 system_prompt 被正确传下来了

            没有 system prompt（裸调用）
                → "Mock response: Hello"
                   把用户说的话回显出来，证明 messages 被正确传下来了
        """
        if self.fixed_content is not None:
            return self.fixed_content

        if agent_name:
            return f"Mock response generated for {agent_name}."

        last_user = self._last_user_content(messages)
        if last_user:
            preview = last_user.strip().replace("\n", " ")
            if len(preview) > _ECHO_PREVIEW_CHARS:
                preview = preview[:_ECHO_PREVIEW_CHARS] + "…"
            return f"Mock response: {preview}"

        return "Mock response generated."

    @staticmethod
    def _last_user_content(messages: Sequence[Mapping[str, str]]) -> str:
        """取最后一条 user 消息的内容。"""
        for message in reversed(list(messages)):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    # ------------------------------------------------------------
    def is_available(self) -> bool:
        """Mock 永远可用 —— 这就是它存在的意义。"""
        return True

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "simulated": True,
                "fixed_content": self.fixed_content,
                "call_count": self.call_count,
            }
        )
        return info
