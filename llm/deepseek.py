"""
DeepSeek Provider
============================================================

【这个文件现在的样子，就是「加一家厂商」应有的成本】

    Step 2.5 刚写的时候，这个文件有 647 行 ——
    里面塞满了 HTTP 请求、JSON 解析、错误翻译。

    加豆包的时候发现：豆包、DeepSeek、Grok 三家的接口形状是一样的
    （都是 OpenAI 兼容格式）。如果把那 647 行复制三份，
    以后改一个 bug 就要改三处，迟早改漏。

    所以那些「形状」被抽到了 llm/openai_compatible.py。
    现在这个文件只回答 DeepSeek 自己的问题：

        · 我叫 deepseek
        · 我的 base_url 是 https://api.deepseek.com
        · 我默认用 deepseek-chat
        · 我读 DEEPSEEK_API_KEY
        · 我的 402 是「余额不足」，要提示去充值

    就这些。剩下的全是继承来的。

【这个文件里没有一行代码能影响到 Agent】
    Agent 看到的只有一个 provider 名字字符串，
    以及一个统一的 LLMResponse。
"""

from __future__ import annotations

# 这几个是从基类模块重新导出的。
# 为什么要在 deepseek.py 里再导出一遍？
#     因为历史原因，已经有代码和测试写成
#     `from llm.deepseek import HTTPResult, default_http_post`。
#     原地转发一下，它们的 import 就还能用，不用改。
#     这属于「保持向后兼容」的小技巧，成本极低。
from llm.openai_compatible import (  # noqa: F401
    HTTPPost,
    HTTPResult,
    OpenAICompatibleProvider,
    default_http_post,
)


class DeepSeekProvider(OpenAICompatibleProvider):
    """
    DeepSeek 的真实接入。

    用法：
        provider = DeepSeekProvider(config["llm"]["providers"]["deepseek"])
        response = provider.generate(messages="你好")

    API Key 只从环境变量读，永远不从代码或配置文件读。
    """

    # ---------------- 身份 ----------------
    name = "deepseek"
    PROVIDER_LABEL = "DeepSeek"
    requires_api_key = True

    # ---------------- 各家不同的默认值 ----------------
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-chat"
    DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
    DEFAULT_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
    DEFAULT_MODEL_ENV = "DEEPSEEK_MODEL"

    # DeepSeek 有合理的默认模型，用户不填也能用
    requires_explicit_model = False

    # DeepSeek V4 支持 thinking 开关 + reasoning_effort（low/high/max）。
    supports_thinking = True
    supports_reasoning_effort = True
    # deepseek-flash（V4.1-Flash）原生支持图片输入；
    # 图片只允许放在 user 消息里（见 llm/base.py 的 normalize）。
    supports_image_input = True

    # ---------------- DeepSeek 特有的错误含义 ----------------
    def _extra_status_hint(self, status: int) -> str:
        """
        补充 DeepSeek 特有的解释。

        为什么要单独处理 402？
            DeepSeek 用 402 表示「余额不足」，
            这个状态码在别家很少见，用户看到会一头雾水。
            所以要说清楚「去充值」，而不是让他以为是代码问题。
        """
        if status == 402:
            return "  → 请去 https://platform.deepseek.com 充值"
        return ""
