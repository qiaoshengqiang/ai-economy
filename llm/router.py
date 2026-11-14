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

    def requires_api_key(self, name: str) -> bool:
        """
        这个 provider **要不要**密钥？

        【为什么问注册表，而不是让调用方判断 provider 名字】

            `Brain.preflight_credential()` 需要在调用**之前**知道
            「这个 provider 没有 key 是不是真的有问题」。

            第一版它写的是 `if provider == "mock": 跳过` ——
            结果是 `agents/` 里出现了厂商名分支，被
            `test_no_provider_branching_anywhere_outside_providers`
            当场抓住（这是对的：那种写法每加一个 provider 就要改一次 Brain）。

            ✅ 正确的知识来源是 **Provider 自己**：
               每个 Provider 类用 `requires_api_key` 声明这件事
               （`llm/base.py`，`MockProvider` 声明 False）。
               注册表只是把「名字 → 类」这层映射暴露出来。

        🔒 **fail-closed**：查不到（未注册 / 工厂不是类 / 没有该属性）
           一律返回 True —— 即「假设它需要 key」。
           反过来会让一个真的缺 key 的 provider 静默通过预检。
        """
        key = str(name or "").strip().lower()
        factory = self._factories.get(key)
        if factory is None:
            return True
        return bool(getattr(factory, "requires_api_key", True))

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
# Adapter 注册表（Configuration System Refactor）
# ============================================================
def adapter_classes() -> Dict[str, Any]:
    """
    **唯一**把「配置里的 `adapter:` 字符串」映射到 Python 类的地方。

    【为什么需要这一层】

        以前 provider 名**既是**厂商身份、**又是**配置键、**又是**
        Python 注册键。加一个新厂商 = 改 Python（三处）。

        现在拆成两层：

            providers.<name>.adapter  →  一个**协议实现**（本函数）
            providers.<name>          →  一个**端点**（base_url / timeout）

        ⇒ 新增一个 provider，只要它的协议能用已有 adapter，
          **完全不改 Python**。见 `tests/test_config_resolver.py`
          的 `test_10_*`。

    【为什么 deepseek / doubao / bailian 各占一个 adapter】

        它们**确实**有厂商专属行为，不是纯 OpenAI 兼容：

            DeepSeekProvider  → 402「去充值」提示
            DoubaoProvider    → 404/429/401 提示 + model_kind
            BailianProvider   → 401/404/429 提示

        这些都体现在**错误信息**里，属于「保持当前真实 API 行为」
        （§18）的一部分 —— 所以保留为**厂商 adapter**，
        而不是硬塞进通用的 `openai_compatible`。

        `openai_compatible` 是**通用** adapter：任何标准 OpenAI 兼容
        端点（新厂商、自建服务、代理）用它即可，零 Python 改动。

    🔒 未实现的 adapter 名 → 调用方拿 `None`，然后**必须报错**，
       不许静默退回 mock。
    """
    from llm.bailian import BailianProvider
    from llm.deepseek import DeepSeekProvider
    from llm.doubao import DoubaoProvider
    from llm.mock import MockProvider
    from llm.openai_compatible import OpenAICompatibleProvider

    return {
        "openai_compatible": OpenAICompatibleProvider,
        "deepseek": DeepSeekProvider,
        "doubao": DoubaoProvider,
        "bailian": BailianProvider,
        "mock": MockProvider,
    }


def adapter_specs() -> Dict[str, Dict[str, Any]]:
    """
    给 `core.config_resolver.ConfigValidator` 用的 adapter 元数据。

    🔒 **为什么要注入而不是让 `core/` 自己判断**：
       `core/` 不认识 `llm/`（依赖方向见 AGENTS.md §3）。
       所以「某个 adapter 需不需要 base_url / api_key」这件
       **provider 的知识**必须由 `llm/` 提供。

    🔒 这里**没有任何 adapter 名字的硬编码判断** ——
       全部来自 Provider 类的声明式属性
       （`requires_base_url` / `requires_api_key`），
       与 `requires_api_key` 是同一种手法。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for name, cls in adapter_classes().items():
        out[name] = {
            "class": cls,
            "label": str(getattr(cls, "PROVIDER_LABEL", name) or name),
            "requires_base_url": bool(getattr(cls, "requires_base_url", True)),
            "requires_api_key": bool(getattr(cls, "requires_api_key", True)),
            #: 这个 adapter 自带的默认端点。UI 用它做「留空即默认」提示，
            #: 设置层也用它把空 base_url 补成显式值。
            "default_base_url": str(
                getattr(cls, "DEFAULT_BASE_URL", "") or "").strip(),
        }
    return out


def adapter_for(name: str) -> Optional[Any]:
    """按 adapter 名取类；未实现返回 None（调用方**必须**报错）。"""
    return adapter_classes().get(str(name or "").strip().lower())


# ============================================================
# 默认注册表
# ============================================================
def build_default_registry(config: Any = None) -> ProviderRegistry:
    """
    建好注册表。

    【Configuration System Refactor 之后的职责】

        ① **从 canonical 配置动态注册**（`providers.<name>.adapter`）
           ⇒ 配置里加一个 provider 就够，不用改 Python。
        ② 保留**旧 provider 名**（`bailian_qwen` / `bailian_glm`）
           作为过渡别名 —— 旧 `llm.providers` 段还在时必须能用。
        ③ `grok` 继续登记为「已知但未实现」，提示精确到建哪个文件。

    参数：
        config: 全局配置（`Config` 或 raw dict）。为 None 时只注册
                内置的 adapter 名字集合（测试与兼容用）。
    """
    registry = ProviderRegistry()

    # ---- ① canonical：providers.<name>.adapter ----
    providers_cfg = _config_get(config, "providers", None)
    if isinstance(providers_cfg, Mapping):
        for name, spec in providers_cfg.items():
            spec = spec if isinstance(spec, Mapping) else {}
            adapter = str(spec.get("adapter") or "").strip().lower()
            cls = adapter_for(adapter)
            if cls is not None:
                registry.register(str(name), cls)
            # adapter 未知 → 这里**不注册**；ConfigValidator 已经
            # 在启动时把这种配置判为 ConfigurationError（fail-loud）。
            #
            # 🔒 这里**刻意不**为 `bailian` 顺手注册 `bailian_qwen` /
            #    `bailian_glm` 别名 —— 那两个名字存在的唯一理由是
            #    「老结构里一个 provider 只能有一个模型」，正是本次要
            #    消掉的东西。而且它们各自的 `model_env`
            #    （BAILIAN_QWEN_MODEL / BAILIAN_GLM_MODEL）与新结构里
            #    `models.<profile>.model_id` 表达的是同一件事，
            #    并存只会再造一个来源。

    # ---- ② 旧 llm.providers 段（过渡期）----
    legacy = _config_get(config, "llm.providers", None)
    if isinstance(legacy, Mapping) or config is None:
        _register_legacy_names(registry)

    # ---- ③ 「已知但代码里没有 adapter」----
    #
    # 🔒 **与配置无关，无条件注册。**
    #
    #    这一条回答的是「**代码**支持到什么程度」，不是「配置里写了什么」：
    #    用户写出 `grok` 这个名字时，应该拿到一条「怎么把它接进来」的提示，
    #    而不是一句「找不到」。所以它不能依赖配置文件里有没有 grok 段 ——
    #    那正是重构前「配置里留着 grok 块但根本没实现」的误导来源。
    _register_unimplemented(registry)

    return registry


def _register_legacy_names(registry: ProviderRegistry) -> None:
    """
    注册重构前的那批 provider **名字**（不含 unimplemented）。

    🔒 这是**过渡期**代码：等旧 `llm.providers` 段与
       `agents/*/config.yaml` 都删除后，本函数可一并删除。
       它存在只是为了「新配置上线时旧名字还能用」。
    """
    from llm.bailian import BailianGLMProvider, BailianQwenProvider
    from llm.deepseek import DeepSeekProvider
    from llm.doubao import DoubaoProvider
    from llm.mock import MockProvider

    for name, cls in (("mock", MockProvider),
                      ("deepseek", DeepSeekProvider),
                      ("doubao", DoubaoProvider),
                      ("bailian_qwen", BailianQwenProvider),
                      ("bailian_glm", BailianGLMProvider)):
        if not registry.is_registered(name):
            registry.register(name, cls)


def _register_unimplemented(registry: ProviderRegistry) -> None:
    """
    把「代码里还没有 adapter 的厂商」登记成**已知但未实现**。

    每个提示都精确到「建哪个文件、写什么类、去哪里登记」，
    因为这就是用户接下来真正要做的事。

    🔒 提示里**先说**「标准 OpenAI 兼容协议根本不用改 Python」——
       这是 Configuration System Refactor 带来的最重要变化。
    """
    _HOWTO = (
        "怎么做：\n"
        "  1. 在 config/config.yaml 的 providers: 段加一条，"
        "adapter 用 \"openai_compatible\"\n"
        "     （如果你的服务是标准 OpenAI 兼容协议，"
        "**到此为止 —— 不需要改 Python**）\n"
        "  2. 只有当它有厂商专属行为（特殊错误码 / 提示）时，"
        "才新建 llm/{name}.py\n"
        "  3. 写一个继承 LLMProvider（或 OpenAICompatibleProvider）"
        "的 {cls}\n"
        "  4. 在 llm/router.py 的 adapter_classes() 里登记它\n"
        "  提示：照抄 llm/deepseek.py 就行。\n"
        "  注意：Provider 只负责把 messages 转成 LLMResponse，\n"
        "        不要在里面记账、算钱、碰数据库。"
    )

    for name, cls_name in (
        ("grok", "GrokProvider"),
    ):
        if not registry.is_registered(name):
            registry.register_unimplemented(
                name,
                _HOWTO.format(name=name, cls=cls_name),
            )


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
        #: 🔒 注册表**从配置建** —— `providers.<name>.adapter` 决定用哪个类。
        #: 配置里加一个 provider 就够，不需要改这里（见 `adapter_classes`）。
        self.registry = registry or build_default_registry(config)

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
        """
        全局默认 provider。

        🔒 canonical `runtime.default_provider` 优先，旧
           `llm.default_provider` 作为过渡回退。**不隐式兜底** ——
           两处都没有时用 "mock"，但那是**声明式的最后一级**，
           不是「读不到就换 mock」的静默降级。
        """
        value = _config_get(self.config, "runtime.default_provider", None)
        if not value:
            value = _config_get(self.config, "llm.default_provider", "mock")
        return str(value or "mock").strip().lower()

    def provider_config(self, name: str) -> Dict[str, Any]:
        """
        取某个 provider 的**端点配置**（给 Provider 构造函数用）。

        🔒 **一个事实只有一个权威来源** 的落点就在这里：

            ① canonical `providers.<name>`   ← 权威（迁移后）
            ② 旧 `llm.providers.<name>`      ← 过渡期回退

        canonical 的 `request:` 子块会被**摊平**成 Provider 构造函数
        认识的键（`max_tokens_field` / `extra_body`）。

        🔴 **`model` 来自 `default_model`（一个 model profile 引用），
           而不是 provider 段自己写一个模型名。**

           这就是修掉 `CONFIGURATION_SYSTEM_AUDIT.md` §B1
           「Model 优先级倒置」的地方：以前 `llm.providers.<p>.model`
           会被 `AgentConfig.model` 当成**显式**参数，于是**静默屏蔽**
           `.env` 的值，而且只在「那一项恰好非空」的 provider 上发生
           （deepseek / grok 中招，doubao / bailian 不中招）。

           现在 provider 的默认模型是**显式声明**的
           （`providers.<name>.default_model: <model profile>`），
           来源唯一，且**永远让位**给 Agent 显式选的 model profile。

           没有 `default_model` 时 `model` 为 `""`，Provider 会走自己的
           `DEFAULT_MODEL`；两条都空就在**调用前**抛 `LLMRequestError`
           （fail-loud），而不是静默用一个错的模型。
        """
        canonical = _config_get(self.config, f"providers.{name}", None)
        if isinstance(canonical, Mapping) and canonical:
            out: Dict[str, Any] = {}
            if canonical.get("base_url"):
                out["base_url"] = canonical["base_url"]
            if canonical.get("timeout_seconds") is not None:
                out["timeout_seconds"] = canonical["timeout_seconds"]
            if canonical.get("chat_path"):
                out["chat_path"] = canonical["chat_path"]
            request = canonical.get("request")
            if isinstance(request, Mapping):
                out.update(dict(request))

            # provider 的默认模型 = `default_model` 指向的 model profile 的 model_id
            profile = str(canonical.get("default_model") or "").strip()
            out["model"] = str(
                _config_get(self.config, f"models.{profile}.model_id", "") or ""
            ) if profile else ""
            return out

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

    def requires_api_key(self, name: Optional[str] = None) -> bool:
        """
        这个 provider 要不要密钥？

        【为什么要问注册表，而不是在调用方判断 provider 名字】

            `Brain.preflight_credential()` 需要在**调用之前**知道
            「这个 provider 没有 key 到底是不是问题」。

            它原来写的是 `if provider == "mock": 跳过` ——
            于是 `agents/` 里出现了厂商名分支，被
            `test_no_provider_branching_anywhere_outside_providers`
            当场抓住（守卫是对的：那种写法每加一个 provider 都要改一次 Brain）。

            ✅ 正确的知识来源是 **Provider 自己**：
               每个 Provider 类用 `requires_api_key` 声明这件事
               （见 `llm/base.py`；`MockProvider` 声明为 False）。
               这里只是把「名字 → 类」那层映射暴露给上层。
        """
        return self.registry.requires_api_key(
            name or self.default_provider)

    def is_enabled(self, name: str) -> bool:
        """
        配置里有没有把它关掉。

        🔒 canonical `providers.<name>.enabled` 优先，旧
           `llm.providers.<name>.enabled` 作为过渡回退。

        注意默认值是 True：配置里没写 enabled 就当作开启。
        这样新加一个 provider 忘记写 enabled 时不会莫名其妙用不了。
        """
        canonical = _config_get(self.config, f"providers.{name}", None)
        if isinstance(canonical, Mapping) and canonical:
            if "enabled" not in canonical:
                return True
            return bool(canonical.get("enabled"))

        configured = _config_get(self.config, f"llm.providers.{name}", None)
        if not isinstance(configured, Mapping) or "enabled" not in configured:
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
