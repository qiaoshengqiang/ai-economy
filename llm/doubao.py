"""
豆包 Provider（火山方舟 / Volcengine Ark）
============================================================

【它和 DeepSeek 的关系】

    接口形状完全一样（都是 OpenAI 兼容格式），
    所以这个文件只写「豆包自己的事」，剩下的继承基类。

    对比一下：
        llm/openai_compatible.py   704 行  ← HTTP / 解析 / 错误翻译（共用）
        llm/deepseek.py             84 行  ← DeepSeek 的身份
        llm/doubao.py               本文件  ← 豆包的身份

    加第三家、第四家也都会是这么薄 —— 这就是抽基类的回报。

【豆包最特别的地方：模型名怎么写】

    豆包不像 DeepSeek 那样有「一个固定的模型名」。
    它有两种写法，你必须指定其中一种：

    写法一：推理接入点 ID（较老的方式）
        ep-2024xxxxxx-xxxxx
        需要先去火山方舟控制台「创建推理接入点」，然后拿到这个 ID。
        好处是可以绑定自定义的配置（限流、模型版本等）。

    写法二：模型名称（较新的方式，直接用）
        doubao-seed-1-6-250615 这类
        不需要创建接入点，直接写模型名就能调。
        日期后缀是模型版本，新版本会换新的日期。

    因为「用哪一个」完全取决于你的账号和习惯，
    所以这里【不给默认模型】—— 没配就直接告诉你两种写法。

【关于错误码的一点提醒】

    豆包的 404 特别容易踩：模型名/接入点写错、没部署、
    或者 base_url 指向了错误的区域（cn-beijing vs cn-shanghai），
    都会表现为 404。所以 404 的提示要具体一点。

【我不确定的地方（请你实测时留意）】

    · max_tokens 这个字段名对某些较新的豆包模型可能已经改名
      （业界在往 max_completion_tokens 迁移）
      → 所以这个字段名做成了可配置：llm.providers.doubao.max_tokens_field
    · 某些推理型模型（带 thinking 的）可能需要额外参数
      → 用 llm.providers.doubao.extra_body 可以直接塞进去，不用改代码

    这两处如果真遇到问题，改配置即可，不需要动 Python。
"""

from __future__ import annotations

from typing import Optional

from llm.openai_compatible import OpenAICompatibleProvider


class DoubaoProvider(OpenAICompatibleProvider):
    """
    豆包（火山方舟）的真实接入。

    用法：
        provider = DoubaoProvider(config["llm"]["providers"]["doubao"])
        response = provider.generate(messages="你好")

    API Key 只从环境变量读，永远不从代码或配置文件读。
    """

    # ---------------- 身份 ----------------
    name = "doubao"
    PROVIDER_LABEL = "豆包（火山方舟）"
    requires_api_key = True

    # ---------------- 各家不同的默认值 ----------------
    DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
    DEFAULT_API_KEY_ENV = "DOUBAO_API_KEY"
    DEFAULT_BASE_URL_ENV = "DOUBAO_BASE_URL"
    DEFAULT_MODEL_ENV = "DOUBAO_MODEL"

    # 豆包【故意】不给默认模型：填接入点 ID 还是模型名，
    # 取决于用户自己的账号设置，猜一个只会让人困惑。
    DEFAULT_MODEL = ""
    requires_explicit_model = True

    # 豆包支持 `thinking` 开关；`reasoning_effort` 不是它的文档字段，
    # 所以这里只声明 thinking —— 不把未知字段发过去。
    supports_thinking = True
    supports_reasoning_effort = False

    # ---------------- 豆包特有的提示 ----------------
    def _missing_model_message(self) -> str:
        """
        没配模型时的提示。

        这个提示要写得具体，因为豆包的模型名规则
        确实比 DeepSeek 那种「一个固定名字」要绕。
        """
        return (
            f"{self.PROVIDER_LABEL} 没有配置模型。\n"
            f"\n"
            f"豆包需要你指定以下两种之一：\n"
            f"\n"
            f"  ① 模型名称（推荐，直接用）\n"
            f"     形如 doubao-seed-1-6-250615、doubao-1-5-pro-32k-250115\n"
            f"     在火山方舟控制台的「模型广场」可以看到当前可用的模型名。\n"
            f"     日期后缀是版本号，新版本会换新日期。\n"
            f"\n"
            f"  ② 推理接入点 ID（较老的方式）\n"
            f"     形如 ep-2024xxxxxxxxx-xxxxx\n"
            f"     需要先在控制台「在线推理」里创建接入点，然后复制它的 ID。\n"
            f"     好处是可以绑定自定义限流和模型版本。\n"
            f"\n"
            f"填在哪里（二选一即可）：\n"
            f"  · 环境变量 {self.model_env}\n"
            f"  · 或 config/config.yaml 的 llm.providers.{self.name}.model\n"
        )

    def _extra_status_hint(self, status: int) -> str:
        """补充豆包特有的错误解释。"""
        if status == 404:
            return (
                "  → 404 在豆包这里几乎总是「模型名/接入点写错了」，常见原因：\n"
                "       · 模型名拼错，或那个版本已经下线\n"
                "       · 用的是接入点 ID 但它还没部署好，或者已被删除\n"
                "       · base_url 的区域不对（cn-beijing / cn-shanghai 不通用）\n"
                "     建议先去火山方舟控制台确认模型名，再对比 .env 里的配置。"
            )

        if status == 429:
            return (
                "  → 豆包的 429 可能是「请求太频繁」，也可能是「账户配额用完了」。\n"
                "     前者等一会儿就好，后者要去控制台看配额和账单。"
            )

        if status == 401:
            return (
                "  → 注意豆包用的是「方舟 API Key」，\n"
                "     不要和「火山引擎账号的 AK/SK」搞混，两者不通用。"
            )

        return ""

    def describe(self) -> dict:
        info = super().describe()
        # 额外报告一下模型名的「形状」，方便排查 404。
        # 注意只报告形状，不报告完整值。
        model = self.default_model
        if not model:
            shape = "（未配置）"
        elif model.startswith("ep-"):
            shape = "推理接入点 ID"
        elif model.startswith("doubao"):
            shape = "模型名称"
        else:
            shape = "未知格式（既不像 ep- 也不像 doubao 开头，请核对）"
        info["model_kind"] = shape
        return info
