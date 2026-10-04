"""
Credential —— Agent 与 Provider 之间的**凭据**抽象
============================================================

【为什么要单独有一层】

    以前：所有 Agent 共享 **一个** provider 实例 → **一个** API Key。
         在供应商眼里，所有 Agent 是**同一个主体**。

    现在：
        Agent A → Credential A → API Key A → DeepSeek
        Agent B → Credential B → API Key B → DeepSeek

    正式确立：
        Agent Identity  ≠  Credential  ≠  Provider Account

    Provider Account **不是**系统的一个概念 —— 它只是 API Key
    背后的外部资源归属。本层不建模账号。

【🔒 关于「本模块手里握着明文 Key」这件事】

    这是**刻意的**：Credential 是系统里**唯一**允许持有明文 key 的地方。
    控制手段不是「不持有」，而是**它的出口只有一个**（`material`），
    并且 `__repr__` / `__str__` / 异常信息里**绝不出现**它。

    这样「key 会不会泄露到日志」这个问题就变成了一个**可检查的**
    局部问题（只查这一个类 + 调用点），而不是「全项目到处都可能漏」。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

#: 🔒 每个进程一个随机盐。
#:
#: 为什么要加盐？
#:     `identity` 会被当作 router 的缓存键。如果直接 sha256(key)，
#:     那么这个值就是一个**可被对照验证的指纹** ——
#:     拿到它的人可以拿候选 key 去撞。加盐之后它只在**本进程内**
#:     有比较意义，对外不泄露任何关于 key 的信息。
_PROCESS_SALT = secrets.token_bytes(16)


def _fingerprint(material: str) -> str:
    """把 key 变成**不可逆、不可跨进程对照**的稳定短的内部 ID。"""
    if not material:
        return "none"
    digest = hashlib.sha256(_PROCESS_SALT + material.encode("utf-8"))
    return digest.hexdigest()[:16]


#: Credential 的来源 —— 用于日志与 provenance，**不含任何 secret**。
SOURCE_AGENT_ENV = "agent_env"        # Agent 自己的 api_key_env（推荐）
SOURCE_AGENT_INLINE = "agent_inline"  # Agent 配置里直接写的 api_key（测试便利）
SOURCE_GLOBAL_ENV = "global_env"      # 回退：全局 provider 的 api_key_env
SOURCE_NONE = "none"                  # 不可用


@dataclass(frozen=True)
class Credential:
    """
    一次 LLM 调用的**凭据**。

    🔒 `material`（明文 key）**只应该被 provider client 读取**。
       不要把它写进日志、消息、记忆、事件、CLI 输出。
    """

    ref: str = ""                                   # 逻辑标识，如 "agent_a_deepseek"
    source: str = SOURCE_NONE
    material: str = field(default="", repr=False)   # 🔒 repr=False：不进 repr
    provider: str = ""

    # ----------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.material)

    @property
    def identity(self) -> str:
        """
        🔒 **router 缓存键**用的稳定内部 ID。

        = `credential_ref` + 加盐哈希(material)

        【为什么是这两段拼起来】（§8 + §19）

            · `credential_ref`  —— 让「不同的逻辑凭据」得到**不同实例**，
                                   这样 provider 实例天然按 Agent 归属，
                                   usage 可以清楚记到每个 credential 上。
            · material 的哈希    —— 防止「同一个 ref 配了不同 key」
                                   时被错误地共享（配置写错也不会串）。

        ⚠️ 绝不能用**明文 key** 当缓存键 —— 那会让 key 出现在
           `_instances` 的 key 里（调试打印、异常、`describe()` 都可能漏）。
           哈希用的是**进程内随机盐**，因此对外不可逆、不可跨进程对照。
        """
        ref = self.ref or self.source
        return f"{ref}:{_fingerprint(self.material)}"

    # ----------------------------------------------------------
    def __repr__(self) -> str:
        """🔒 **绝不包含 material。**"""
        return (
            f"Credential(ref={self.ref!r}, provider={self.provider!r}, "
            f"source={self.source!r}, available={self.available})"
        )

    __str__ = __repr__

    def describe(self) -> dict:
        """给 CLI / usage 记录用的安全描述。🔒 没有 key。"""
        return {
            "credential_ref": self.ref,
            "credential_source": self.source,
            "credential_available": self.available,
            "provider": self.provider,
        }


class CredentialResolver:
    """
    **很薄**的一层：`credential 配置` → `Credential`。

    🔒 它的职责**只有**凭据解析。它不碰：
       LLM 调用 · Agent · Task · Economy · Billing · Conversation。

    【解析优先级】（任务书 §3 —— 采用用户建议的顺序）

        1. `api_key_env` **有值**（环境变量存在且非空）→ 用它
        2. 否则 `api_key`                             → 用它
        3. 否则 **回退到全局** provider 的 `api_key_env`
        4. 都没有                                     → 不可用

    为什么 `api_key_env` 优先于 `api_key`：
        开发者本地常常同时留着 `api_key_env: DEEPSEEK_KEY_A`
        和一个旧的 `api_key: "sk-old-test"`。
        正式运行时应该走环境变量，**不应该意外继续用旧测试 Key**。
    """

    def __init__(
        self,
        global_config: Optional[Any] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._config = global_config
        self._environ = environ if environ is not None else os.environ

    # ----------------------------------------------------------
    def resolve(
        self,
        *,
        provider: str,
        agent_id: str = "",
        credential: Optional[Mapping[str, Any]] = None,
        credential_ref: str = "",
    ) -> Credential:
        """
        解析出这个 Agent 在这家 provider 上应该用的凭据。

        `credential` 是 Agent 配置里的那一段，形如：
            {"api_key_env": "DEEPSEEK_KEY_A"}   或
            {"api_key": "sk-..."}               或
            None / {}（表示不给，走全局）
        """
        provider = (provider or "").strip().lower()
        cfg = dict(credential or {})
        ref = credential_ref or self._default_ref(agent_id, provider)

        # ---- ① Agent 自己的环境变量 ----
        env_name = str(cfg.get("api_key_env") or "").strip()
        if env_name:
            value = (self._environ.get(env_name) or "").strip()
            if value:
                return Credential(ref=ref, source=SOURCE_AGENT_ENV,
                                  material=value, provider=provider)

        # ---- ② Agent 配置里直接写的 key（测试阶段合法方式）----
        inline = str(cfg.get("api_key") or "").strip()
        if inline:
            return Credential(ref=ref, source=SOURCE_AGENT_INLINE,
                              material=inline, provider=provider)

        # ---- ③ 回退：全局 provider credential（旧配置继续工作）----
        global_env = self._global_api_key_env(provider)
        if global_env:
            value = (self._environ.get(global_env) or "").strip()
            if value:
                return Credential(ref=ref or f"{provider}_global",
                                  source=SOURCE_GLOBAL_ENV,
                                  material=value, provider=provider)

        # ---- ④ 不可用 ----
        return Credential(ref=ref, source=SOURCE_NONE,
                          material="", provider=provider)

    # ----------------------------------------------------------
    def _global_api_key_env(self, provider: str) -> str:
        """读全局 `llm.providers.<p>.api_key_env`（旧配置路径）。"""
        if self._config is None:
            return ""

        # ⚠️ **实测抓到的 bug**：原来直接 `self._config.get("a.b.c")` ——
        #    这只对 `core.config.Config` 那种「认点号路径」的对象成立。
        #    测试里传普通嵌套 dict 时它永远返回 None，
        #    于是「回退到全局凭据」静默失效（而且不报错）。
        #
        #    这里复用 router 的同一套查找语义，两种来源都支持。
        path = f"llm.providers.{provider}.api_key_env"

        if isinstance(self._config, Mapping):
            node: Any = self._config
            for part in path.split("."):
                if not isinstance(node, Mapping) or part not in node:
                    return ""
                node = node[part]
            return str(node or "").strip()

        getter = getattr(self._config, "get", None)
        if callable(getter):
            try:
                return str(getter(path, "") or "").strip()
            except Exception:                      # pragma: no cover - 防御
                return ""
        return ""

    @staticmethod
    def _default_ref(agent_id: str, provider: str) -> str:
        """没显式给 credential_ref 时，生成一个稳定的逻辑标识。"""
        if agent_id and provider:
            return f"{agent_id}_{provider}"
        return provider or "default"
