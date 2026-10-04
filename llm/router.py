"""
LLM Router —— 按名字找到 Provider
============================================================

【它解决什么问题】

    brain.generate(provider="mock", ...)

    Router 拿到 "mock"，去注册表里查出 MockProvider，把活派给它。

【为什么不用 if / elif】

    ❌ 这样写的话，每加一个厂商就要改 Router：

        if provider == "mock":      return MockProvider(...)
        elif provider == "deepseek": return DeepSeekProvider(...)
        elif provider == "doubao":   return DoubaoProvider(...)
        elif provider == "grok":     return GrokProvider(...)
        # 加第 5 个厂商 → 再来一个 elif → 改 Router → 有改坏的风险

    ✅ 用注册表，加厂商只需要「注册」一行：

        registry.register("deepseek", DeepSeekProvider)

    Router 本身一行都不用改。这就是开闭原则的实际好处：
        对扩展开放（加厂商），对修改关闭（不动 Router）。

【本阶段不做什么】
    · 不做自动模型选择（不根据任务难度挑模型）
    · 不做成本优化路由
    · 不做自动 fallback
    这些留给后续阶段。现在只做「指定名字 → 拿到 Provider」。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from core.logging_setup import get_logger
from llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderDisabledError,
    ProviderNotFoundError,
    ProviderNotImplementedError,
)

logger = get_logger(__name__)

# 一个「能造出 Provider」的东西：可以是类，也可以是函数
ProviderFactory = Callable[..., LLMProvider]


# ============================================================
# 配置读取（同时支持 Config 对象和普通字典）
# ============================================================
def _config_get(config: Any, dotted_key: str, default: Any = None) -> Any:
    """
    按 "llm.providers.mock.model" 这种路径取值。

    为什么要兼容两种来源？
        · 生产代码传进来的是 core.config.Config（有 .get 方法）
        · 测试里可能直接传一个普通 dict，写起来更省事

        两种都支持，llm/ 这个包就不必依赖 core.config，
        可以单独拿出来测试。
    """
    if config is None:
        return default

    # 普通嵌套字典：逐层往下找
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted_key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    # Config 风格的对象
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(dotted_key, default)

    return default


# ============================================================
# 注册表
# ============================================================
class ProviderRegistry:
    """
    provider 名字 → 能造出它的工厂。

    它同时记录「已实现」和「已知但未实现」两类名字。

    为什么要记「已知但未实现」？
        因为要区分这两种情况，它们的处理方式完全不同：

            用户写错名字    → "找不到 zzz"        → 去改配置
            功能还没做      → "deepseek 还没实现" → 等下一阶段

        如果混成同一个错误，用户看到「找不到 deepseek」会以为自己
        拼错了，然后花很久找一个根本不存在的拼写问题。
    """

    def __init__(self) -> None:
        self._factories: Dict[str, ProviderFactory] = {}
        self._unimplemented: Dict[str, str] = {}   # name -> 提示信息

    # ------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------
    def register(self, name: str, factory: ProviderFactory) -> None:
        """注册一个【已实现】的 provider。"""
        key = name.strip().lower()
        if not key:
            raise ValueError("provider 名字不能为空")

        self._factories[key] = factory
        # 如果之前标记过「未实现」，现在实现了就取消标记
        self._unimplemented.pop(key, None)

        logger.debug("已注册 LLM provider：%s", key)

    def register_unimplemented(self, name: str, hint: str) -> None:
        """
        登记一个【已知但还没实现】的 provider。

        hint 应该是「怎么把它做出来」的提示，
        这样报错信息可以直接告诉用户下一步做什么。
        """
        key = name.strip().lower()
        if key in self._factories:
            # 已经实现了，就不要降级标记成未实现
            return
        self._unimplemented[key] = hint

    def unregister(self, name: str) -> None:
        self._factories.pop(name.strip().lower(), None)

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def is_registered(self, name: str) -> bool:
        return name.strip().lower() in self._factories

    def is_known(self, name: str) -> bool:
        key = name.strip().lower()
        return key in self._factories or key in self._unimplemented

    @property
    def implemented(self) -> List[str]:
        """已经能用的 provider 名字。"""
        return sorted(self._factories)

    @property
    def unimplemented(self) -> List[str]:
        """知道名字但还没实现的 provider。"""
        return sorted(self._unimplemented)

    @property
    def known(self) -> List[str]:
        return sorted(set(self._factories) | set(self._unimplemented))

    def hint_for(self, name: str) -> Optional[str]:
        return self._unimplemented.get(name.strip().lower())

    # ------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------
    def create(
        self,
        name: str,
        provider_config: Optional[Mapping[str, Any]] = None,
        default_model: str = "",
        **extra: Any,
    ) -> LLMProvider:
        """
        造一个 Provider 实例。

        三种失败情况分别抛出不同的异常，方便上层区分处理。
        """
        key = name.strip().lower()

        if not self.is_known(key):
            raise ProviderNotFoundError(
                f"未知的 LLM provider：{name!r}\n"
                f"已实现的：{self.implemented or '（无）'}\n"
                f"已登记的：{self.known or '（无）'}\n"
                f"请检查配置里的 brain.provider 是否拼错。"
            )

        if key not in self._factories:
            hint = self._unimplemented.get(key, "")
            raise ProviderNotImplementedError(
                f"LLM provider {key!r} 还没有实现。\n"
                + (f"{hint}\n" if hint else "")
                + f"目前可以直接使用的：{self.implemented or '（无）'}"
            )

        factory = self._factories[key]

        try:
            provider = factory(provider_config or {}, default_model, **extra)
        except TypeError:
            try:
                provider = factory(provider_config or {}, default_model)
            except TypeError:
                # 兼容「工厂函数只接受 provider_config」这种写法
                provider = factory(provider_config or {})

        if not isinstance(provider, LLMProvider):
            raise ProviderNotFoundError(
                f"provider {key!r} 的工厂返回了 {type(provider).__name__}，"
                f"不是 LLMProvider 的子类。"
            )

        return provider

    def __repr__(self) -> str:
        return (
            f"ProviderRegistry(implemented={self.implemented}, "
            f"unimplemented={self.unimplemented})"
        )


# ============================================================
# 默认注册表
# ============================================================
def build_default_registry() -> ProviderRegistry:
    """
    建好系统自带的注册表。

    目前 mock / deepseek / doubao 是真正实现的，
    grok 先登记为「已知但未实现」，
    这样用户一旦在配置里写它，会得到一条
    明确告诉他「去建哪个文件」的提示，而不是一句「找不到」。

    未实现提示的写法，就是「怎么加一个厂商」的说明书。
    """
    registry = ProviderRegistry()

    # ---- 已实现 ----
    # MockProvider 的 __init__ 签名正好是 (provider_config, default_model)，
    # 所以类本身就可以当工厂用，不需要包一层 lambda。
    from llm.mock import MockProvider

    registry.register("mock", MockProvider)

    # DeepSeekProvider 的 __init__ 多了一个可选参数 http_post
    # （注入自定义 HTTP 实现，测试用）。因为有默认值，
    # 它同样可以直接当工厂用。
    #
    # ★ 这一行，就是「接入一家真实厂商」在 Router 侧的全部代价。★
    #   注册表相比 if/elif 链的好处，在这里体现得最直接。
    from llm.deepseek import DeepSeekProvider

    registry.register("deepseek", DeepSeekProvider)

    # 豆包走同一套 OpenAI 兼容基类，所以注册方式一模一样。
    # 注意它【没有默认模型】—— 必须先配好模型名才能用。
    from llm.doubao import DoubaoProvider

    registry.register("doubao", DoubaoProvider)

    # ---- 已登记，未实现 ----
    #
    # 每个提示都精确到「建哪个文件、写什么类、去哪里注册」，
    # 因为这就是用户接下来真正要做的事。
    _HOWTO = (
        "怎么做：\n"
        "  1. 新建 llm/{name}.py\n"
        "  2. 写一个继承 LLMProvider 的 {cls}，实现 generate()\n"
        "  3. 在 llm/router.py 的 build_default_registry() 里注册它\n"
        "  提示：照抄 llm/deepseek.py 就行。它已经把这四件事\n"
        "        示范清楚了：可替换的 HTTP 层、请求构造、\n"
        "        响应解析、错误翻译。\n"
        "  注意：Provider 只负责把 messages 转成 LLMResponse，\n"
        "        不要在里面记账、算钱、碰数据库。"
    )

    for name, cls_name in (
        ("grok", "GrokProvider"),
    ):
        registry.register_unimplemented(
            name,
            _HOWTO.format(name=name, cls=cls_name),
        )

    return registry


# ============================================================
# Router
# ============================================================
class LLMRouter:
    """
    按名字把请求派发给对应的 Provider。

    用法：
        router = LLMRouter(config)                 # config 是全局配置
        response = router.generate(provider="mock", messages="你好")

        # 也可以不指定 provider，用配置里的默认值
        response = router.generate(messages="你好")

    注意 Router 【不知道】调用方是谁。
        它不知道 agent_id，也不知道钱的事。
        记账是 Brain 的职责 —— 因为只有 Brain 同时知道
        「是哪个 Agent」和「调用结果」。
        这样 Router 和 Provider 都能被单独、干净地测试。
    """

    def __init__(
        self,
        config: Any = None,
        registry: Optional[ProviderRegistry] = None,
    ):
        self.config = config
        self.registry = registry or build_default_registry()

        # Provider 实例缓存。
        # 为什么缓存？因为每次调用都新建一个实例的话，
        # 真实 Provider 就没法复用 HTTP 连接，而且 MockProvider
        # 的 call_count 之类的内部状态会丢失。
        # 🔒 【本次改造的核心】缓存键从 `provider` 变成
        #    `(provider, credential_identity)`。
        #
        #    以前：Agent A / Agent B 都拿 `_instances["deepseek"]` ——
        #          同一个实例、同一个 API Key。
        #    现在：不同 Credential 必然拿到**不同实例**，
        #          因此不可能串 Key。
        #
        #    ⚠️ `credential_identity` 是**加盐哈希**，不是明文 key
        #       （见 llm/credential.py）。
        self._instances: Dict[Any, LLMProvider] = {}

    # ------------------------------------------------------------
    # 默认值
    # ------------------------------------------------------------
    @property
    def default_provider(self) -> str:
        """配置里的全局默认 provider。"""
        value = _config_get(self.config, "llm.default_provider", "mock")
        return str(value or "mock").strip().lower()

    def provider_config(self, name: str) -> Dict[str, Any]:
        """取某个 provider 在配置里的那一段。"""
        value = _config_get(self.config, f"llm.providers.{name}", None)
        return dict(value) if isinstance(value, Mapping) else {}

    def provider_model(self, name: str) -> str:
        """取某个 provider 的默认模型名。"""
        configured = self.provider_config(name)
        return str(configured.get("model", "") or "")

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def available_providers(self) -> List[str]:
        """能真正使用的 provider（已实现 + 配置里没被关掉）。"""
        result = []
        for name in self.registry.implemented:
            if self.is_enabled(name):
                result.append(name)
        return result

    def is_enabled(self, name: str) -> bool:
        """
        配置里有没有把它关掉。

        注意默认值是 True：配置里没写 enabled 就当作开启。
        这样新加一个 provider 忘记写 enabled 时不会莫名其妙用不了。
        """
        configured = self.provider_config(name)
        if "enabled" not in configured:
            return True
        return bool(configured.get("enabled"))

    def get_provider(
        self,
        name: Optional[str] = None,
        credential: Optional[Any] = None,
    ) -> LLMProvider:
        """
        拿到一个 Provider 实例（带缓存，**按 Credential 隔离**）。

        这一层负责把三种「用不了」的情况翻译成清晰的异常：
            enabled: false  → ProviderDisabledError
            未实现          → ProviderNotImplementedError
            名字写错        → ProviderNotFoundError

        `credential`：`llm.credential.Credential` 或 None。
            None 表示「用这个 provider 自己的全局 credential」（旧行为）。
        """
        key = (name or self.default_provider).strip().lower()

        # 🔒 缓存键 = (provider, credential 身份)
        #    没有 credential 时用 "~default" 保持旧语义。
        credential_id = credential.identity if credential is not None else "~default"
        cache_key = (key, credential_id)

        # 已缓存且可用 → 直接返回
        cached = self._instances.get(cache_key)
        if cached is not None and self.is_enabled(key):
            return cached

        if not self.is_enabled(key):
            raise ProviderDisabledError(
                f"LLM provider {key!r} 在配置里被关闭了"
                f"（llm.providers.{key}.enabled = false）。\n"
                f"想用的话把它改成 true。"
            )

        configured = self.provider_config(key)
        default_model = str(configured.get("model", "") or "")

        # 🔒 只有「解析出来确实有值」时才注入 ——
        #    否则让 provider 走它自己的全局路径（旧配置兼容）。
        extra: Dict[str, Any] = {}
        if credential is not None and getattr(credential, "available", False):
            extra["api_key_override"] = credential.material

        provider = self.registry.create(key, configured, default_model, **extra)
        self._instances[cache_key] = provider
        return provider

    # ------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------
    def generate(
        self,
        messages: Union[str, Sequence[Message], None] = None,
        provider: Optional[str] = None,
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        credential: Optional[Any] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        派发一次生成请求。

        参数：
            provider:   不传就用配置里的默认 provider
            credential: 用哪个凭据（None = provider 的全局凭据）
            其余参数原样透传给 Provider

        返回：LLMResponse（统一结构）

        未来扩展点（本阶段不做）：
            这里以后会加自动 fallback —— 主 provider 失败时
            按配置换备用 provider 重试。
        """
        target = self.get_provider(provider, credential=credential)

        if not target.is_available():
            reason = target.unavailable_reason() or "原因未知"
            raise ProviderDisabledError(
                f"LLM provider {target.name!r} 当前不可用：{reason}"
            )

        logger.debug(
            "路由到 provider=%s model=%s",
            target.name,
            target.resolve_model(model),
        )

        return target.generate(
            messages=messages,
            system=system,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """自述，给调试和状态展示用。"""
        return {
            "default_provider": self.default_provider,
            "available": self.available_providers(),
            "implemented": self.registry.implemented,
            "unimplemented": self.registry.unimplemented,
            "loaded_instances": sorted(self._instances),
        }

    def __repr__(self) -> str:
        return (
            f"LLMRouter(default={self.default_provider!r}, "
            f"available={self.available_providers()})"
        )
