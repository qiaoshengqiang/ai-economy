"""
阿里云百炼（DashScope）—— OpenAI 兼容接入
============================================================

【为什么一个文件里有两个类】

    Agent D（千问）和 Agent E（GLM）**共用同一个百炼端点、
    同一个 base_url**，只是模型不同。

    但 `LLMProvider.name` 是**类属性**，而且它出现在日志、
    usage 记录、错误信息里。如果两个名字共用同一个类，
    显示出来的厂商名就会是错的。

    所以做两个**极薄**的子类，各自只改身份字段 ——
    协议实现完全继承自 `OpenAICompatibleProvider`。
    这与 deepseek / doubao 的做法一致。

【base_url 必须带 /compatible-mode/v1】

    那是百炼的 OpenAI 兼容入口；不带后缀的是它自己的原生协议。
    https://www.alibabacloud.com/help/zh/model-studio/compatibility-of-openai-with-dashscope

【为什么不给默认模型】

    百炼的模型 id 会更新（qwen / glm 系列都在变）。
    写死一个默认值，几个月后会变成「模型不存在」——
    doubao 就踩过这个坑：config 里写死的 id 盖住了 .env 的正确值。

    所以这里 `DEFAULT_MODEL = ""` 且 `requires_explicit_model = True`：
    没配模型时，会在**调用前**给出一条明确的「请先配模型」，
    而不是发出去换回一个 400。
"""

from __future__ import annotations

from llm.openai_compatible import OpenAICompatibleProvider


class BailianProvider(OpenAICompatibleProvider):
    """
    百炼的公共部分。**不要直接注册这个类** —— 用下面两个子类。

    它们的区别只有身份字段；协议、HTTP、错误翻译全部继承。
    """

    PROVIDER_LABEL = "阿里云百炼"
    requires_api_key = True

    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_API_KEY_ENV = "BAILIAN_API_KEY"
    DEFAULT_BASE_URL_ENV = "BAILIAN_BASE_URL"
    DEFAULT_MODEL_ENV = "BAILIAN_MODEL"

    #: 🔴 百炼的模型 id 会更新，不给默认值。
    #:    没配 → 调用前就报「请先配模型」，而不是发出去换 400。
    DEFAULT_MODEL = ""
    requires_explicit_model = True

    def _extra_status_hint(self, status: int) -> str:
        """百炼特有的状态码解释。"""
        if status == 401:
            return ("  → API Key 无效或未填。检查 .env 里的 "
                    "BAILIAN_KEY_AGENT_D / BAILIAN_KEY_AGENT_E")
        if status == 404:
            return ("  → 模型 id 可能不对。去百炼控制台确认**准确的**模型 id，"
                    "填进 .env（BAILIAN_QWEN_MODEL / BAILIAN_GLM_MODEL）")
        if status == 429:
            return "  → 触发限流，稍后重试或降低并发"
        return ""


class BailianQwenProvider(BailianProvider):
    """
    Agent D 用 —— 千问系列。

        provider = BailianQwenProvider(config["llm"]["providers"]["bailian_qwen"])
    """

    name = "bailian_qwen"
    PROVIDER_LABEL = "阿里云百炼 · 千问"
    # 与 config.yaml 的 provider 级 api_key_env 保持一致。
    # 🔒 **每个 Agent 自己的 key** 来自它 config.yaml 里的
    #    `brain.credential.api_key_env`，优先级高于这里。
    DEFAULT_API_KEY_ENV = "BAILIAN_API_KEY"
    DEFAULT_MODEL_ENV = "BAILIAN_QWEN_MODEL"


class BailianGLMProvider(BailianProvider):
    """
    Agent E 用 —— GLM 系列（智谱，托管在百炼上）。

        provider = BailianGLMProvider(config["llm"]["providers"]["bailian_glm"])
    """

    name = "bailian_glm"
    PROVIDER_LABEL = "阿里云百炼 · GLM"
    # 同上：Agent E 自己的 key 在它自己的 config.yaml 里配。
    DEFAULT_API_KEY_ENV = "BAILIAN_API_KEY"
    DEFAULT_MODEL_ENV = "BAILIAN_GLM_MODEL"
