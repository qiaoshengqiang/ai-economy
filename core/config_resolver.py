"""
Canonical Configuration —— Loader · Validator · Resolver · ResolvedConfig
============================================================================

    config/config.yaml  (V2 canonical)
            ↓  ConfigLoader
        原始 dict
            ↓  ConfigValidator     ← 4 条永久守卫
        校验过的 dict
            ↓  ConfigResolver
        ResolvedConfig
            ↓
        Runtime / Provider / Brain

【🔴 它解决的真实问题（见 CONFIGURATION_SYSTEM_AUDIT.md）】

    重构前，同一个事实最多在 4 个地方各写一遍：

        Python 类默认值 · config/config.yaml · agents/<id>/config.yaml · .env

    而且**优先级在不同 provider 上不一样**：

        `AgentConfig.model` 会回退到 `llm.providers.<p>.model`，
        于是当那一项**非空**时，它变成显式 model 参数，
        **把 .env 的 `<P>_MODEL` 整个屏蔽掉**。
        deepseek / grok 中招，doubao / bailian 不中招。

    本模块把这件事收敛成**一条链**：

        Agent → Model Profile → model_id
                                 ↓
                              Provider → Adapter

        Agent → Credential Profile → env 变量名 → Secret（只在 .env）

【🔒 三条边界】

① **一个事实只有一个权威来源**

    迁移期新旧配置并存时，**值不同就必须报错**（`ConfigurationError`），
    **绝不静默选一个**。见 `ConfigValidator.check_no_retired_duplicate_keys`。

② **失败必须 loud**

    unknown provider / model / credential / adapter / 缺必填字段
    → 一律 `ConfigurationError`。
    **绝不 fallback 到 mock。** 见 `ConfigValidator.check_reference_integrity`。

③ **`core/` 不认识 `llm/`**

    adapter 名字集合由**组合根注入**（与 WorkspaceGateway 收注入的
    `is_participant` 是同一种手法）。所以本模块**不 import `llm/`**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

# ============================================================
# 异常
# ============================================================
class ConfigurationError(Exception):
    """
    配置错误。**必须让启动失败，不许降级。**

    🔒 报错信息里必须说清楚：哪个字段、哪个值、合法取值是什么、
       以及「去哪里改」。只说「配置错误」等于没说。
    """


# ============================================================
# 反向引用 / 来源标注
# ============================================================
def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            f"{where} 必须是一个映射（dict），收到 {type(value).__name__}。")
    return value


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{where} 必须是非空字符串，收到 {value!r}。")
    return value.strip()


def _require_positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{where} 必须是数字，收到 {value!r}。")
    if value <= 0:
        raise ConfigurationError(f"{where} 必须 > 0，收到 {value!r}。")
    return float(value)


def _optional_positive_int(value: Any, where: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{where} 必须是整数，收到 {value!r}。")
    if value <= 0:
        raise ConfigurationError(f"{where} 必须 > 0，收到 {value!r}。")
    return int(value)


#: 疑似 secret 的形状（Guard 3 用）。
#:
#: 🔒 刻意只做**形状**判断，不做熵计算：
#:    配置里出现 `env: SOME_API_KEY` 是**合法**的（那是变量名），
#:    出现一段长随机串才是可疑的。
_SECRET_KEY_NAMES = ("api_key", "apikey", "secret", "token", "password",
                     "credential_material")
_SECRET_VALUE_PREFIXES = ("sk-", "Bearer ", "AIza", "xoxb-")
_SECRET_MIN_RANDOM_LEN = 24


def _looks_like_a_literal_secret(key: str, value: Any) -> Optional[str]:
    """
    返回「为什么可疑」；不疑则 None。

    🔒 刻意只做**形状**判断，不做熵计算、不比对真实 key ——
       「判断一段字符串是不是真 key」本身不可靠，
       而**误报会让正常的 URL / 变量名被当成 secret**（比漏报更糟：
       它会让正确配置无法启动）。

    判定顺序（先排除误报，再找可疑）：
        ① 字段名本身就是放 secret 的地方       → 可疑
        ② 值以真实密钥前缀开头                 → 可疑
        ③ 字段名语义是「变量名引用」            → 合法（`*_env` / `*_var`）
        ④ 值是 URL / 路径                      → 合法
        ⑤ 长、无空格、纯 ASCII、字母+数字混排  → 可疑（像随机串）
    """
    lowered = str(key).strip().lower()

    # ① 字段名就是放 secret 的地方
    if lowered in _SECRET_KEY_NAMES:
        return f"字段名 {key!r} 看起来是直接放 secret 的地方"

    if not isinstance(value, str):
        return None

    # ② 真实密钥的已知前缀
    for prefix in _SECRET_VALUE_PREFIXES:
        if value.startswith(prefix):
            return f"值以 {prefix!r} 开头，像是一段真实密钥"

    # ③ 变量名引用是**合法**的（api_key_env / token_env / secret_var…）
    if lowered.endswith(("_env", "_var", "_name")) or lowered == "env":
        return None

    stripped = value.strip()

    # ④ URL / 路径不是 secret
    if "://" in stripped or stripped.startswith(("/", "~", ".", "data/")):
        return None

    # ⑤ 长随机串的形状
    if (len(stripped) >= _SECRET_MIN_RANDOM_LEN
            and " " not in stripped
            and stripped.isascii()
            and any(c.isalpha() for c in stripped)
            and any(c.isdigit() for c in stripped)):
        return "值是一段长随机串，像是真实密钥而不是变量名"
    return None


# ============================================================
# Resolved* —— Runtime 只该看到这些
# ============================================================
@dataclass(frozen=True)
class ResolvedProvider:
    """
    一个**端点**。

    🔒 它不持有任何 Agent，也不持有 secret（只有变量名在别处）。
       它的 `default_model_*` 只是「没显式选模型时用哪个」，
       永远让位给 Agent 显式选的 model profile。
    """

    name: str
    adapter: str
    base_url: str
    #: OpenAI 兼容 chat completions 的路径。默认 `/chat/completions`。
    #: Provider 的 base_url 已经包含 API 版本时，这里不要再带 `/v1`。
    chat_path: str = "/chat/completions"
    timeout_seconds: float = 300.0
    request: Mapping[str, Any] = field(default_factory=dict)
    #: 没显式选模型时用的 model profile（可空）。
    default_model_profile: str = ""
    #: 上面那个 profile 解析出的真实 model id（可空）。
    default_model_id: str = ""
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "adapter": self.adapter,
                "base_url": self.base_url,
                "chat_path": self.chat_path,
                "timeout_seconds": self.timeout_seconds,
                "request": dict(self.request),
                "default_model_profile": self.default_model_profile,
                "default_model_id": self.default_model_id,
                "source": self.source}


@dataclass(frozen=True)
class ResolvedModel:
    """一个 **Model Profile**：稳定的内部名 + 真实的 model id + 它跑在哪个 provider 上。"""

    profile: str
    model_id: str
    provider: ResolvedProvider
    #: 模型总上下文窗口（输入 + 输出）。None = 未声明，由调用方决定。
    context_window: Optional[int] = None
    #: 模型默认最大输出 tokens。None = 未声明，由调用方决定。
    max_output_tokens: Optional[int] = None
    #: 是否支持隐藏推理 / reasoning。
    supports_reasoning: bool = False
    #: 是否接受图片输入。由 model profile 声明，不能按 provider 一刀切。
    supports_image_input: bool = False
    #: 模型支持的固定思考强度；UI 的选项只从这里来。
    thinking_levels: Tuple[str, ...] = ()
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"profile": self.profile, "model_id": self.model_id,
                "provider": self.provider.name,
                "adapter": self.provider.adapter,
                "base_url": self.provider.base_url,
                "timeout_seconds": self.provider.timeout_seconds,
                "context_window": self.context_window,
                "max_output_tokens": self.max_output_tokens,
                "supports_reasoning": self.supports_reasoning,
                "supports_image_input": self.supports_image_input,
                "thinking_levels": list(self.thinking_levels),
                "source": self.source}


@dataclass(frozen=True)
class ResolvedCredential:
    """
    一个 **Credential Profile**。

    🔒 它**不知道 Agent**，也**不知道 Model**。
       它只回答「用哪个环境变量取 secret」。

    🔒 `material` 刻意**不放在这里** —— 本模块只解析「引用」，
       真正的明文由 `llm/credential.py` 在调用前一刻解析，
       并且永远不进日志 / 不进 `repr`。
    """

    profile: str
    env: str
    present: bool = False
    source: str = ""
    source_file: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """🔒 只输出变量名与「有没有」，**绝不输出值**。"""
        return {"profile": self.profile, "env": self.env,
                "present": self.present, "source": self.source,
                "source_file": self.source_file}


@dataclass(frozen=True)
class ResolvedAgent:
    """一个 Agent 的**最终解析结果**。Agent identity 不在本模块里改。"""

    agent_id: str
    name: str
    model_profile: str
    model_id: str
    provider: ResolvedProvider
    credential_profile: str
    credential: ResolvedCredential
    temperature: float
    #: 与 max_output_tokens 同值；保留字段兼容旧调用方。
    max_tokens: Optional[int]
    #: 来自 model profile 的上下文窗口 / 输出上限。
    #: 🔒 同一 model profile 的所有 Agent 共享这两个值。
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    supports_reasoning: bool = False
    supports_image_input: bool = False
    thinking_levels: Tuple[str, ...] = ()
    system_prompt: str = ""
    #: 其余**原样保留**的 Agent 配置段（economy / capabilities / negotiation /
    #: behavior / identity），迁移时不允许丢字段。
    raw: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id, "name": self.name,
            "model_profile": self.model_profile, "model_id": self.model_id,
            "provider": self.provider.name, "adapter": self.provider.adapter,
            "base_url": self.provider.base_url,
            "timeout_seconds": self.provider.timeout_seconds,
            "credential_profile": self.credential_profile,
            "credential_env": self.credential.env,
            "credential_present": self.credential.present,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_reasoning": self.supports_reasoning,
            "supports_image_input": self.supports_image_input,
            "thinking_levels": list(self.thinking_levels),
            "system_prompt_chars": len(self.system_prompt or ""),
            "source": self.source,
        }


@dataclass(frozen=True)
class ResolvedRuntimeConfig:
    """一次性解析出来的**全部**配置。Runtime 尽量只用它。"""

    version: int
    log_level: str
    database_path: str
    economy: Mapping[str, Any]
    task: Mapping[str, Any]
    system: Mapping[str, Any]
    #: 资源层结构上限（`workspace.*`）。🔒 Runtime 的**唯一**来源。
    workspace: Mapping[str, Any]
    cost: Mapping[str, Any]
    providers: Mapping[str, ResolvedProvider]
    models: Mapping[str, ResolvedModel]
    credentials: Mapping[str, ResolvedCredential]
    agents: Mapping[str, ResolvedAgent]
    #: 消息认领租约（秒）。🔒 Runtime 的**唯一**来源。
    message_claim_lease_seconds: Optional[int] = None
    #: 「没有显式指定 provider」时用哪个。🔒 Runtime 的**唯一**来源。
    default_provider: str = ""
    #: Agent Inbox V1：是否启用 Runtime 收件箱消费者。
    #: 默认 false —— Phase 1 只做持久化与观测，不启动消费者。
    inbox_enabled: bool = False
    #: dotted key → 这个值最终来自哪里（GUI / 排障用）
    sources: Mapping[str, str] = field(default_factory=dict)
    #: 启动时要装配的 Agent（`agents.<id>.runtime: true`）。
    #: 空 = 旧配置 / 测试未声明，由组合根回退到 DEFAULT_AGENTS。
    runtime_agents: Tuple[str, ...] = ()

    # ---- 查询（找不到就抛，**不返回 None**）----
    def provider(self, name: str) -> ResolvedProvider:
        got = self.providers.get(str(name))
        if got is None:
            raise ConfigurationError(
                f"unknown provider {name!r}。已知：" f"{sorted(self.providers)}")
        return got

    def model(self, profile: str) -> ResolvedModel:
        got = self.models.get(str(profile))
        if got is None:
            raise ConfigurationError(
                f"unknown model profile {profile!r}。已知：" f"{sorted(self.models)}")
        return got

    def credential(self, profile: str) -> ResolvedCredential:
        got = self.credentials.get(str(profile))
        if got is None:
            raise ConfigurationError(
                f"unknown credential profile {profile!r}。已知："
                f"{sorted(self.credentials)}")
        return got

    def agent(self, agent_id: str) -> ResolvedAgent:
        got = self.agents.get(str(agent_id))
        if got is None:
            raise ConfigurationError(
                f"unknown agent {agent_id!r}。已知：{sorted(self.agents)}")
        return got

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "runtime": {
                "log_level": self.log_level,
                "database_path": self.database_path,
                "message_claim_lease_seconds":
                    self.message_claim_lease_seconds,
                "default_provider": self.default_provider,
                "inbox_enabled": bool(self.inbox_enabled),
                "agents": list(self.runtime_agents),
            },
            "economy": dict(self.economy), "task": dict(self.task),
            "system": dict(self.system),
            "workspace": dict(self.workspace), "cost": dict(self.cost),
            "providers": {k: v.to_dict() for k, v in self.providers.items()},
            "models": {k: v.to_dict() for k, v in self.models.items()},
            "credentials": {k: v.to_dict()
                            for k, v in self.credentials.items()},
            "agents": {k: v.to_dict() for k, v in self.agents.items()},
            "sources": dict(self.sources),
        }


# ============================================================
# Loader
# ============================================================
#: canonical 的**映射段**（也是 GUI 的展示顺序）。
CANONICAL_SECTIONS: Tuple[str, ...] = (
    "runtime", "economy", "task", "system", "workspace",
    "providers", "models", "credentials", "cost", "agents",
)

#: canonical 的**标量**键（不是映射，不能当段处理）。
CANONICAL_SCALARS: Tuple[str, ...] = ("version",)

#: GUI 系统设置管理的 LLM 注册表段。它们从 `config/llm.yaml` 合并进来；
#: 同一个段不允许同时出现在 config.yaml 与 llm.yaml。
LLM_CONFIG_SECTIONS: Tuple[str, ...] = (
    "providers", "models", "credentials",
)

#: Pi / 模型支持的固定思考强度集合。
THINKING_LEVELS: Tuple[str, ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
)


def merge_llm_sections(
    base: Mapping[str, Any],
    llm: Mapping[str, Any],
    *,
    config_source: str = "config/config.yaml",
    llm_source: str = "config/llm.yaml",
) -> Dict[str, Any]:
    """把 llm.yaml 的三段合并进 config.yaml，并拒绝重复定义。"""
    merged = dict(base)
    for key in llm:
        if key not in ("version",) + LLM_CONFIG_SECTIONS:
            raise ConfigurationError(
                f"{llm_source} 包含未知段 {key!r}。"
                f"只允许：{', '.join(LLM_CONFIG_SECTIONS)}。")
    for section in LLM_CONFIG_SECTIONS:
        if section in merged and section in llm:
            raise ConfigurationError(
                f"{section!r} 同时出现在 {config_source} 与 {llm_source}。\n"
                f"同一事实只能有一个权威来源：请只保留 {llm_source} 里的定义。")
        if section in llm:
            merged[section] = llm[section]
    if "version" in llm and "version" not in merged:
        merged["version"] = llm["version"]
    return merged


def load_merged_raw_config(
    path: Any, *, loader: Optional[Callable] = None,
) -> Dict[str, Any]:
    """读取配置文件；若同目录存在 `llm.yaml`，合并 LLM 注册表。

    `llm.yaml` 是可选的：测试与一次性脚本仍然可以只传一份完整 config。
    """
    from pathlib import Path as _Path

    p = _Path(path)
    if loader is None:
        from core.config import load_yaml
        loader = load_yaml
    data = dict(_require_mapping(loader(p), str(p)))
    companion = p.with_name("llm.yaml")
    if companion.exists():
        llm = dict(_require_mapping(loader(companion), str(companion)))
        data = merge_llm_sections(
            data, llm, config_source=str(p), llm_source=str(companion))
    return data

#: 已被**合并进别处**的旧键。它们**不许再出现** ——
#: 否则同一事实又会多出一份定义。
#:
#:    旧键                            现在的唯一权威位置
#:    -----------------------------   ----------------------------------
#:    system.log_level                runtime.log_level
#:    system.reliability              runtime.message_claim_lease_seconds
#:    storage.database_path           runtime.database_path
#:
#: 🔒 这张表是 **Guard 1 的替代品**。
#:    迁移期 Guard 1 比的是「canonical 与旧 `llm` / `storage` / `system`
#:    段是否冲突」；旧段全部删除之后那个比较已经没有对象了。
#:    但它的**保护作用仍然需要** —— 防止有人日后又把旧键加回来。
#:    所以这里把它改成「禁止已废弃键回归」。
RETIRED_KEYS: Tuple[Tuple[str, str], ...] = (
    ("system.log_level", "runtime.log_level"),
    ("system.reliability", "runtime.message_claim_lease_seconds"),
    ("storage", "runtime.database_path"),
)


class ConfigLoader:
    """
    把 `config/config.yaml` 读成 dict，并分离「canonical 段」与「旧段」。

    🔒 它**不做校验、不做解析** —— 那两件事分别属于
       `ConfigValidator` 与 `ConfigResolver`。单一职责。
    """

    def __init__(self, data: Mapping[str, Any], *,
                 source_file: str = "config/config.yaml") -> None:
        self.raw: Dict[str, Any] = dict(_require_mapping(data, source_file))
        self.source_file = source_file

    @classmethod
    def from_file(cls, path: Any, *, loader: Optional[Callable] = None) -> "ConfigLoader":
        """从 YAML 文件加载。**文件不存在 → 报错**（Guard 4，不降级）。"""
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            raise ConfigurationError(
                f"配置文件不存在：{p}\n"
                f"（本系统不会在没有配置文件时降级到 mock —— 那会让"
                f"「配置错了」看起来像「跑起来了」。）")
        if loader is None:
            from core.config import load_yaml           # 复用既有 loader
            loader = load_yaml
        data = load_merged_raw_config(p, loader=loader)
        return cls(data, source_file=str(p))

    # ---- 取段 ----
    def section(self, name: str) -> Dict[str, Any]:
        value = self.raw.get(name)
        if value is None:
            return {}
        return dict(_require_mapping(value, f"{self.source_file} :: {name}"))

    @property
    def canonical(self) -> Dict[str, Any]:
        """canonical 的映射段 + 标量键（`version`）。"""
        out: Dict[str, Any] = {k: self.section(k) for k in CANONICAL_SECTIONS}
        for key in CANONICAL_SCALARS:
            out[key] = self.raw.get(key)
        return out

    def source_of(self, dotted: str) -> str:
        """这个值来自哪里（给 GUI / 排障）。"""
        return f"{self.source_file} :: {dotted}"


# ============================================================
# Validator —— 4 条永久守卫
# ============================================================
class ConfigValidator:
    """
    四条**永久**守卫。任何一条不过 → `ConfigurationError`。

    🔒 它们不是「迁移期临时检查」—— 迁移结束后继续生效，
       防止下一次有人再引入第二个来源。
    """

    def __init__(self, *, adapter_specs: Callable[[], Mapping[str, Mapping[str, Any]]]) -> None:
        #: 🔒 **注入**，不 import `llm/`（`core/` 不认识 `llm/`）。
        #:
        #: 形状：`{adapter_name: {"requires_base_url": bool, ...}}`
        #: 「某个 adapter 需不需要 base_url」是 **provider 的知识**，
        #: 所以由 `llm/` 提供 —— `core/` 里**没有**任何 adapter 名字的硬编码。
        self._adapter_specs = adapter_specs

    # ---- Guard 1：已废弃的重复键不许回归 ----
    @staticmethod
    def check_no_retired_duplicate_keys(raw: Mapping[str, Any]) -> List[str]:
        """
        **同一个事实只能有一个权威来源。**

        这条守卫守的是「**已经被合并掉的旧键不许回来**」：

            旧键                          现在的唯一权威位置
            ---------------------------   ----------------------------------
            system.log_level              runtime.log_level
            system.reliability            runtime.message_claim_lease_seconds
            storage.database_path         runtime.database_path

        【为什么是这一条，而不是别的】

            迁移期这里比的是「canonical 段与旧 `llm` / `storage` / `system`
            段的值是否一致」—— 那在旧段还存在时才有意义。
            旧段全部删除之后，那个比较**没有对象**了。

            但保护作用不能跟着消失：如果哪天有人把 `system.log_level`
            或 `storage:` 加回来，同一事实就又有了两份定义，
            而这次**不会**有任何东西报错 —— 因为读取路径已经全改到
            `runtime.*`，旧键会被静默忽略。那正是最危险的状态：
            「看起来改了，其实没生效」。

            所以这条守卫改成**禁止回归**：旧键一旦出现就报错，
            并明确告诉你它现在应该写在哪儿。

        🔒 它**只看原始配置**（`raw`），不看 canonical —— 因为
           `storage` 这种段根本不在 canonical 段列表里，
           从 canonical 视角是看不见的。
        """
        problems: List[str] = []
        for retired, canonical_home in RETIRED_KEYS:
            node: Any = raw
            found = True
            for part in retired.split("."):
                if isinstance(node, Mapping) and part in node:
                    node = node[part]
                else:
                    found = False
                    break
            if found:
                problems.append(
                    f"{retired} 仍然存在，但它已经合并进 "
                    f"{canonical_home}。\n"
                    f"  同一事实只能有一个权威来源 —— "
                    f"请删掉 {retired}，只保留 {canonical_home}。\n"
                    f"  （否则它会被静默忽略：读取路径已经全读 "
                    f"{canonical_home}。）")
        return problems

    # ---- Guard 2：引用完整性 + 失败必须 loud ----
    @staticmethod
    def check_agent_references(canonical: Mapping[str, Any]) -> List[str]:
        """
        Agent → model / credential 的引用检查。

        🔒 **不需要 adapter 知识**，所以是 `@staticmethod` ——
           这一点很关键：`core/config.py` 在投影 Agent 配置时要用它，
           而 `core/` **不允许** import `llm/`（否则 `core/ ↔ llm/`
           会形成循环，破坏 AGENTS.md §3 的依赖图）。

           把「Agent 引用检查」与「provider/adapter 检查」拆开之后，
           `core/` 只需要前者，`llm/` 的 adapter 知识只留在
           组合根（`main.py`）与 `./dev.sh config` 里用。
        """
        problems: List[str] = []
        models = dict(canonical.get("models") or {})
        credentials = dict(canonical.get("credentials") or {})
        agents = dict(canonical.get("agents") or {})

        for agent_id, agent in agents.items():
            agent = agent or {}
            try:
                brain = _require_mapping(agent.get("brain") or {},
                                         f"agents.{agent_id}.brain")
            except ConfigurationError as exc:
                problems.append(str(exc))
                continue
            model_profile = str(brain.get("model") or "")
            cred_profile = str(brain.get("credential") or "")

            if not model_profile:
                problems.append(
                    f"agents.{agent_id}.brain 缺少 model（必须显式引用一个 "
                    f"model profile）。")
            elif model_profile not in models:
                problems.append(
                    f"agents.{agent_id}.brain.model = {model_profile!r} 不存在。"
                    f"已知 model profile：{sorted(models)}。")

            if not cred_profile:
                problems.append(
                    f"agents.{agent_id}.brain 缺少 credential。\n"
                    f"  🔒 非 mock Agent **必须**显式引用一个 credential —— "
                    f"不会再自动去找 provider 的全局 key。\n"
                    f"  想共享就写同一个 credential 名。")
            elif cred_profile not in credentials:
                problems.append(
                    f"agents.{agent_id}.brain.credential = {cred_profile!r} "
                    f"不存在。已知 credential：{sorted(credentials)}。")

            if "max_tokens" in brain:
                problems.append(
                    f"agents.{agent_id}.brain.max_tokens 不再支持 —— "
                    f"输出上限由 models.{model_profile}.max_output_tokens 决定。")

            # 禁止 Agent 保存 provider 的重复信息
            for banned in ("provider", "api_key_env", "base_url"):
                if banned in brain:
                    problems.append(
                        f"agents.{agent_id}.brain.{banned} 不被允许 —— "
                        f"Agent 只引用 model / credential，"
                        f"provider 由 model 解析出来。")
        return problems

    def check_reference_integrity(
            self, canonical: Mapping[str, Any]) -> List[str]:
        problems: List[str] = []
        specs = dict(self._adapter_specs())
        adapters = set(specs)

        providers = dict(canonical.get("providers") or {})
        models = dict(canonical.get("models") or {})

        # provider → adapter / base_url
        for name, prov in providers.items():
            prov = prov or {}
            adapter = str(prov.get("adapter") or "")
            if not adapter:
                problems.append(f"providers.{name} 缺少 adapter。")
            elif adapter not in adapters:
                problems.append(
                    f"providers.{name}.adapter = {adapter!r} 不存在。"
                    f"已知 adapter：{sorted(adapters)}。"
                    f"（**不会**退回 mock。）")
            else:
                needs_url = bool(specs.get(adapter, {}).get("requires_base_url", True))
                if needs_url and not str(prov.get("base_url") or ""):
                    problems.append(
                        f"providers.{name} 缺少 base_url"
                        f"（adapter {adapter!r} 需要它）。")
            t = prov.get("timeout_seconds")
            if t is not None:
                try:
                    _require_positive_number(t, f"providers.{name}.timeout_seconds")
                except ConfigurationError as exc:
                    problems.append(str(exc))

            # provider 的默认模型必须指向一个**属于自己**的 model profile，
            # 否则「不显式选模型」时会静默用到别的端点的模型。
            default_profile = str(prov.get("default_model") or "").strip()
            if default_profile:
                target = models.get(default_profile)
                if not isinstance(target, Mapping):
                    problems.append(
                        f"providers.{name}.default_model = {default_profile!r} "
                        f"不是有效的 model profile。已知：{sorted(models)}。")
                elif str(target.get("provider") or "") != name:
                    problems.append(
                        f"providers.{name}.default_model = {default_profile!r}，"
                        f"但 models.{default_profile}.provider = "
                        f"{target.get('provider')!r} —— 默认模型必须属于"
                        f"这个端点自己。")

        # model → provider
        for profile, model in models.items():
            model = model or {}
            provider = str(model.get("provider") or "")
            if not provider:
                problems.append(f"models.{profile} 缺少 provider。")
            elif provider not in providers:
                problems.append(
                    f"models.{profile}.provider = {provider!r} 不存在。"
                    f"已知 provider：{sorted(providers)}。")
            if not str(model.get("model_id") or ""):
                problems.append(f"models.{profile} 缺少 model_id。")

        # credential → env
        for profile, cred in (canonical.get("credentials") or {}).items():
            cred = cred or {}
            if not str(cred.get("env") or ""):
                problems.append(f"credentials.{profile} 缺少 env（环境变量名）。")

        # agent → model / credential（与 core/ 复用的是同一段逻辑）
        problems += self.check_agent_references(canonical)
        return problems

    # ---- Guard 3：配置文件里不许出现 secret ----
    @staticmethod
    def check_no_secret_in_config(canonical: Mapping[str, Any]) -> List[str]:
        problems: List[str] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, Mapping):
                for k, v in node.items():
                    why = _looks_like_a_literal_secret(str(k), v)
                    if why:
                        problems.append(f"{path}.{k}：{why}。")
                    walk(v, f"{path}.{k}")
            elif isinstance(node, (list, tuple)):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")

        walk(canonical, "")
        return problems

    # ---- Guard 4：绝不静默退回 mock ----
    @staticmethod
    def check_no_silent_mock_fallback(
            canonical: Mapping[str, Any]) -> List[str]:
        """
        「默认 provider」如果是 mock，必须**是显式写出来的**，而不是兜底。

        🔒 这里只检查一件事：**canonical 段必须存在且非空**。
           空的 providers/models/credentials/agents 会被上游
           `check_reference_integrity` 抓出来（引用不到）。
           本检查负责的是「整段都没了」这种更早的失败。
        """
        problems: List[str] = []
        # runtime 段必须给出运行期事实 —— 缺了就是「Runtime 只能靠猜」，
        # 那正是本轮要消灭的情况。
        runtime = canonical.get("runtime") or {}
        for key, hint in (("log_level", "日志级别"),
                          ("database_path", "数据库路径"),
                          ("message_claim_lease_seconds", "消息认领租约"),
                          ("default_provider", "默认 provider")):
            if runtime.get(key) in (None, ""):
                problems.append(
                    f"runtime.{key} 缺失（{hint}）。\n"
                    f"  Runtime 必须能从 ResolvedConfig 拿到它 —— "
                    f"不允许缺配置时自己猜一个默认值。")
        for section in ("providers", "models", "credentials"):
            if not (canonical.get(section) or {}):
                problems.append(
                    f"canonical 段 {section!r} 为空或缺失。"
                    f"（本系统不会退回到 mock —— 请补全配置。）")
        return problems

    # ---- 汇总 ----
    def validate(self, loader: ConfigLoader) -> Dict[str, Any]:
        canonical = loader.canonical
        problems: List[str] = []
        problems += self.check_no_silent_mock_fallback(canonical)
        problems += self.check_no_retired_duplicate_keys(loader.raw)
        problems += self.check_reference_integrity(canonical)
        problems += self.check_no_secret_in_config(canonical)
        if problems:
            raise ConfigurationError(
                "配置校验失败（共 %d 项）：\n  - %s"
                % (len(problems), "\n  - ".join(problems)))
        return canonical


# ============================================================
# Resolver
# ============================================================
class ConfigResolver:
    """
    把**校验过的** canonical dict 解析成 `ResolvedConfig`。

    🔒 每个 Resolver 只回答一个问题（Provider / Model / Credential / Agent），
       互不越界 —— 这样 GUI 与测试都能单独调用其中一层。
    """

    def __init__(self, canonical: Mapping[str, Any], *,
                 source_file: str = "config/config.yaml",
                 environ: Optional[Mapping[str, str]] = None) -> None:
        self.canonical = dict(canonical)
        self.source_file = source_file
        #: 🔒 只用来回答「这个 secret 有没有配」——**不取它的值**。
        import os
        self._environ: Mapping[str, str] = environ if environ is not None else os.environ

    def _src(self, dotted: str) -> str:
        return f"{self.source_file} :: {dotted}"

    # ---- Provider ----
    def provider(self, name: str) -> ResolvedProvider:
        raw = dict((self.canonical.get("providers") or {}).get(name) or {})
        if not raw:
            raise ConfigurationError(
                f"unknown provider {name!r}。已知："
                f"{sorted((self.canonical.get('providers') or {}))}")

        # provider 的默认模型：一个 model profile 引用（可空）
        profile = str(raw.get("default_model") or "").strip()
        model_id = ""
        if profile:
            model_raw = dict((self.canonical.get("models") or {}).get(profile) or {})
            if not model_raw:
                raise ConfigurationError(
                    f"providers.{name}.default_model = {profile!r} 不是有效的 "
                    f"model profile。已知："
                    f"{sorted((self.canonical.get('models') or {}))}")
            model_id = str(model_raw.get("model_id") or "")

        return ResolvedProvider(
            name=name,
            adapter=_require_text(raw.get("adapter"),
                                  f"providers.{name}.adapter"),
            base_url=str(raw.get("base_url") or "").rstrip("/"),
            chat_path=str(raw.get("chat_path") or "/chat/completions"),
            timeout_seconds=_require_positive_number(
                raw.get("timeout_seconds", 300),
                f"providers.{name}.timeout_seconds"),
            request=dict(raw.get("request") or {}),
            default_model_profile=profile,
            default_model_id=model_id,
            source=self._src(f"providers.{name}"),
        )

    # ---- Model ----
    def model(self, profile: str) -> ResolvedModel:
        raw = dict((self.canonical.get("models") or {}).get(profile) or {})
        if not raw:
            raise ConfigurationError(
                f"unknown model profile {profile!r}。已知："
                f"{sorted((self.canonical.get('models') or {}))}")
        provider_name = _require_text(raw.get("provider"),
                                      f"models.{profile}.provider")
        context_window = _optional_positive_int(
            raw.get("context_window"), f"models.{profile}.context_window")
        max_output_tokens = _optional_positive_int(
            raw.get("max_output_tokens"), f"models.{profile}.max_output_tokens")
        if (context_window is not None and max_output_tokens is not None
                and max_output_tokens > context_window):
            raise ConfigurationError(
                f"models.{profile}.max_output_tokens ({max_output_tokens}) "
                f"不能大于 context_window ({context_window})。")
        raw_levels = raw.get("thinking_levels")
        if raw_levels is None:
            thinking_levels: Tuple[str, ...] = ()
        else:
            if not isinstance(raw_levels, (list, tuple)):
                raise ConfigurationError(
                    f"models.{profile}.thinking_levels 必须是列表。")
            thinking_levels = tuple(str(x) for x in raw_levels)
            unknown = [x for x in thinking_levels if x not in THINKING_LEVELS]
            if unknown:
                raise ConfigurationError(
                    f"models.{profile}.thinking_levels 包含未知档位 "
                    f"{unknown}；允许：{list(THINKING_LEVELS)}。")
            if not thinking_levels:
                raise ConfigurationError(
                    f"models.{profile}.thinking_levels 不能为空。")
        return ResolvedModel(
            profile=profile,
            model_id=_require_text(raw.get("model_id"),
                                   f"models.{profile}.model_id"),
            provider=self.provider(provider_name),
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_reasoning=bool(raw.get("supports_reasoning", False)),
            supports_image_input=bool(
                raw.get("supports_image_input", False)),
            thinking_levels=thinking_levels,
            source=self._src(f"models.{profile}"),
        )

    # ---- Credential ----
    def credential(self, profile: str) -> ResolvedCredential:
        raw = dict((self.canonical.get("credentials") or {}).get(profile) or {})
        if not raw:
            raise ConfigurationError(
                f"unknown credential profile {profile!r}。已知："
                f"{sorted((self.canonical.get('credentials') or {}))}")
        env = _require_text(raw.get("env"), f"credentials.{profile}.env")
        present = bool((self._environ.get(env) or "").strip())
        return ResolvedCredential(
            profile=profile, env=env, present=present,
            source=f"env:{env}", source_file=self._src(f"credentials.{profile}"),
        )

    # ---- Agent ----
    def agent(self, agent_id: str) -> ResolvedAgent:
        raw_all = self.canonical.get("agents") or {}
        if agent_id not in raw_all:
            raise ConfigurationError(
                f"unknown agent {agent_id!r}。已知：{sorted(raw_all)}")
        raw = dict(raw_all[agent_id] or {})
        brain = dict(raw.get("brain") or {})

        model_profile = _require_text(
            brain.get("model"), f"agents.{agent_id}.brain.model")
        cred_profile = _require_text(
            brain.get("credential"), f"agents.{agent_id}.brain.credential")

        if "max_tokens" in brain:
            raise ConfigurationError(
                f"agents.{agent_id}.brain.max_tokens 不再支持 —— "
                f"输出上限由 models.{model_profile}.max_output_tokens 决定。")

        for banned in ("provider", "api_key_env", "base_url"):
            if banned in brain:
                raise ConfigurationError(
                    f"agents.{agent_id}.brain.{banned} 不被允许 —— "
                    f"Agent 只引用 model / credential。")

        model = self.model(model_profile)
        return ResolvedAgent(
            agent_id=agent_id,
            name=str(raw.get("name") or agent_id),
            model_profile=model_profile,
            model_id=model.model_id,
            provider=model.provider,
            credential_profile=cred_profile,
            credential=self.credential(cred_profile),
            temperature=float(brain.get("temperature", 0.7)),
            max_tokens=model.max_output_tokens,
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            supports_reasoning=model.supports_reasoning,
            supports_image_input=model.supports_image_input,
            thinking_levels=model.thinking_levels,
            # 🔒 `.strip()` 不是装饰 —— 它保持与旧的
            #    `AgentConfig.system_prompt`（`core/config.py` 里也 strip）
            #    **逐字节一致**。
            #    YAML 的块标量（`|`）必然带一个尾换行；
            #    不 strip 就会让送进 LLM 的 system prompt 多一个字符。
            #    这是 `test_12_migration_preserves_behavior` 抓出来的。
            system_prompt=str(brain.get("system_prompt") or "").strip(),
            raw=raw,
            source=self._src(f"agents.{agent_id}"),
        )

    # ---- 全部 ----
    def resolve(self) -> ResolvedRuntimeConfig:
        runtime = dict(self.canonical.get("runtime") or {})

        providers = {n: self.provider(n)
                     for n in (self.canonical.get("providers") or {})}
        models = {p: self.model(p)
                  for p in (self.canonical.get("models") or {})}
        credentials = {c: self.credential(c)
                       for c in (self.canonical.get("credentials") or {})}
        agents = {a: self.agent(a)
                  for a in (self.canonical.get("agents") or {})}
        runtime_agents = tuple(
            agent_id
            for agent_id, agent in agents.items()
            if bool((agent.raw or {}).get("runtime") is True)
        )

        sources: Dict[str, str] = {}
        for name in providers:
            sources[f"providers.{name}"] = self._src(f"providers.{name}")
        for p in models:
            sources[f"models.{p}"] = self._src(f"models.{p}")
        for c in credentials:
            sources[f"credentials.{c}"] = self._src(f"credentials.{c}")
        for a in agents:
            sources[f"agents.{a}"] = self._src(f"agents.{a}")

        return ResolvedRuntimeConfig(
            version=int(self.canonical.get("version") or 1),
            log_level=str(runtime.get("log_level") or "INFO"),
            database_path=str(runtime.get("database_path") or "data/economy.db"),
            message_claim_lease_seconds=(
                int(runtime["message_claim_lease_seconds"])
                if runtime.get("message_claim_lease_seconds") is not None
                else None
            ),
            default_provider=str(runtime.get("default_provider") or ""),
            inbox_enabled=bool(runtime.get("inbox_enabled", False)),
            economy=dict(self.canonical.get("economy") or {}),
            task=dict(self.canonical.get("task") or {}),
            system=dict(self.canonical.get("system") or {}),
            workspace=dict(self.canonical.get("workspace") or {}),
            cost=dict(self.canonical.get("cost") or {}),
            providers=providers, models=models,
            credentials=credentials, agents=agents,
            runtime_agents=runtime_agents, sources=sources,
        )


# ============================================================
# 门面：一次调用拿到 ResolvedConfig
# ============================================================
def load_resolved_config(
    path: Any,
    *,
    adapter_specs: Callable[[], Mapping[str, Mapping[str, Any]]],
    environ: Optional[Mapping[str, str]] = None,
    loader: Optional[Callable] = None,
) -> ResolvedRuntimeConfig:
    """
    `config.yaml → Loader → Validator → Resolver → ResolvedConfig`

    🔒 这是**唯一**的入口。任何一步失败都抛 `ConfigurationError`，
       **绝不返回一个降级的默认配置**。
    """
    cfg_loader = ConfigLoader.from_file(path, loader=loader)
    validator = ConfigValidator(adapter_specs=adapter_specs)
    canonical = validator.validate(cfg_loader)
    return ConfigResolver(canonical, source_file=cfg_loader.source_file,
                          environ=environ).resolve()
