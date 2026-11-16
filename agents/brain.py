"""
Brain —— Agent 的智力门面
============================================================

【Brain 在这个架构里到底是什么】

    它不是模型。它是一个「转接头」。

    Agent 对着 Brain 说：brain.generate(messages=...)
    Brain 负责：
        1. 补上这个 Agent 的默认参数（system_prompt / temperature / max_tokens）
        2. 转交给 Router
        3. 把这次调用的 token 用量记进账
        4. 把统一的 LLMResponse 原样交回 Agent

【为什么 Brain 要放在 agents/ 而不是 llm/】

    因为 Brain 是 Agent 的一部分 —— 就像 Identity、Memory、Wallet 一样。
    llm/ 那个包是【外部资源】（相当于电力公司），
    Brain 是 Agent 身上「买电、用电」的那个接口。

    分清这一点很重要：
        Agent 是经济主体（会破产、会赚钱、有声誉）
        Brain 是它的一个器官
        Provider 是它花钱买的东西

    所以 Brain 可以碰数据库（记自己的账），
    而 Provider 绝对不能碰数据库。

【为什么记账放在 Brain，而不是 Router 或 Provider】

    记账需要两个信息：agent_id 和 调用结果。

        Provider  只知道调用结果，不知道是谁调的 → 不行
        Router    不关心调用方是谁（那样它才能通用）→ 不行
        Brain     两个都知道                                → ✅

    这样切分之后，Router 和 Provider 都能被单独、干净地测试，
    不用去 mock 一个数据库。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Union

from llm import prompt_log
from core.logging_setup import get_logger
from llm.base import LLMResponse, Message
from llm.recorder import LLMCallRecorder
from llm.router import LLMRouter
from llm.session import build_session_id, task_scope

logger = get_logger(__name__)


@dataclass
class BrainSettings:
    """
    Brain 的默认参数。

    单独抽出来是为了让「从 Agent 配置构造」这件事有个明确的数据结构，
    而不是在 Brain.__init__ 里塞八个参数。
    """

    agent_id: str
    provider: str = "mock"
    model: str = ""
    system_prompt: str = ""
    temperature: float = 0.7
    max_tokens: int = 2048
    #: 来自 model profile 的上下文窗口 / 输出上限。
    #: 🔒 Agent 不再单独覆盖；同一 model profile 的所有 Agent 共享。
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    supports_reasoning: bool = False
    #: 模型支持的固定思考强度；UI 的选项来自这里。
    thinking_levels: tuple = ()

    #: 🔒 **这里刻意只有 `credential_ref`，没有 `api_key`。**
    #:
    #:    任务书 §9：Brain 应该知道「我要用哪个 Credential」，
    #:    但**不应该成为 Secret Store**。
    #:    真正的明文 key 由 `CredentialResolver` 解析、直接交给 router，
    #:    Brain 自己从不持有它。
    credential_ref: str = ""

    @classmethod
    def from_resolved_agent(cls, resolved: Any) -> "BrainSettings":
        """
        从 **ResolvedAgent** 读出这个 Agent 的大脑设置（生产路径）。

        🔒 这是 Runtime 的**唯一**配置入口。四件事全部来自已解析结果：

            provider  ← models.<profile>.provider      （由 model 推导）
            model     ← models.<profile>.model_id      （真实 model id）
            credential_ref ← credential profile 名
            （credential 的 env 变量名由调用方一并取出，见 main.py）

        🔒 不再走 `AgentConfig.provider` / `AgentConfig.model` /
           `provider.default_model` 那条旧 fallback 路径 ——
           那条路径会自己猜，而配置缺失应该**显式失败**。
        """
        return cls(
            agent_id=str(resolved.agent_id),
            provider=str(resolved.provider.name),
            model=str(resolved.model_id or ""),
            system_prompt=str(resolved.system_prompt or ""),
            temperature=float(resolved.temperature),
            max_tokens=int(resolved.max_tokens if resolved.max_tokens
                           is not None else 2048),
            context_window=resolved.context_window,
            max_output_tokens=resolved.max_output_tokens,
            supports_reasoning=bool(resolved.supports_reasoning),
            thinking_levels=tuple(resolved.thinking_levels or ()),
            credential_ref=str(resolved.credential_profile or ""),
        )

    @classmethod
    def from_agent_config(cls, agent_config: Any) -> "BrainSettings":
        """
        ⚠️ **DEPRECATED** —— 从 AgentConfig 里读大脑设置。

        保留只为测试与旧调用方。生产路径（`main.py`）必须用
        `from_resolved_agent()` —— 有守卫钉住这一点。

        【为什么它不能继续当生产路径】
            `AgentConfig.model` 是 core/config.py 从 canonical
            **投影**出来的镜像。让 Runtime 依赖那个镜像，等于同时存在
            两个「配置事实来源」—— 而这一轮的目标就是消除它。
        """
        return cls(
            agent_id=str(agent_config.agent_id),
            provider=str(agent_config.provider),
            model=str(agent_config.model or ""),
            system_prompt=str(agent_config.system_prompt or ""),
            temperature=float(agent_config.temperature),
            max_tokens=int(agent_config.max_tokens),
            credential_ref=str(getattr(agent_config, "credential_ref", "") or ""),
        )


class Brain:
    """
    Agent 用来调用智力的那个接口。

    用法：
        brain = Brain(router=router, settings=settings, recorder=recorder)

        response = brain.generate(messages="你好")
        print(response.content)

        # 只要文本时
        text = brain.think("你好")
    """

    #: 🔒 「最近一次 generate 用到的 Cognitive Context」，只读观测用。
    #:    记录它不违反任何边界 —— 它只是**已经发出去的东西**的回执。
    last_context_stats: Optional[Dict[str, Any]] = None

    def __init__(
        self,
        router: LLMRouter,
        settings: BrainSettings,
        recorder: Optional[LLMCallRecorder] = None,
        credential_resolver: Optional[Any] = None,
        credential_config: Optional[dict] = None,
        context_builder: Optional[Any] = None,
        default_context_query: Optional[dict] = None,
    ):
        self.router = router
        self.settings = settings

        #: 🔴 Agent 的**分相**思考策略（phase → 设置）。
        #:
        #:    "intake"   = 接收人类命令的首次理解（优先高强度思考）
        #:    "decision" = 决策阶段（后续决策 / 工作后的检查点）
        #:    "work"     = 工作阶段（多轮工具/读写/测试）
        #:
        #:    每个槽：
        #:        None = 不注入，保持 provider 自己的默认行为；
        #:        {"enabled": False} = 关闭；
        #:        {"enabled": True, "effort": "high"} = 开启并指定强度。
        #:
        #:    🔴 **默认偏好**不是硬限制：
        #:        · intake 优先高强度（理解人类命令）；
        #:        · decision 设为 off/low（优先低思考）；
        #:        · work 设为 high（优先高思考）；
        #:        · Agent 可以在信封里覆盖**任一相** —— Runtime 不 clamp。
        self.thinking_policy: Dict[str, Optional[Dict[str, Any]]] = {
            "intake": None,
            "cognition": None,
            "decision": None,
            "work": None,
            "summary": None,
        }

        #: 🔴 分相 temperature 策略（None = 用 config 里的 temperature）。
        #:
        #:    decision 优先更稳定（低温度）；
        #:    work 用任务/配置决定的温度（代码任务也偏向低，创意任务可高）。
        #:
        #:    🔒 与 thinking 一样：这是**默认偏好**，不是强制；
        #:       调用方显式传 temperature= 时优先。
        self.temperature_policy: Dict[str, Optional[float]] = {
            "intake": None,
            "cognition": None,
            "decision": None,
            "work": None,
            "summary": None,
        }
        #: 最近一次实际使用的 temperature（观测用）。
        self.last_temperature: Optional[float] = None

        #: 🔴 分相**认知预算偏好**（phase → max_tokens；None = 用 config 的
        #: `max_tokens`）。
        #:
        #:    “工作相需要更长输出”是 Agent 自己的策略；它可以和 thinking
        #:    在同一个信封里一起设置。
        #:
        #:    🔒 这不是硬限制：Human / 系统给的 `cognitive_budget_cap`
        #:       仍然优先。实际值 = min(偏好, cap)，Provider 还会做结构裁剪。
        self.cognitive_budget_policy: Dict[str, Optional[int]] = {
            "intake": None,
            "cognition": None,
            "decision": None,
            "work": None,
            "summary": None,
        }
        #: 最近一次预算解析结果（观测 / prompt 反馈用）。
        #: 形状：phase / requested / effective / cap / clipped。
        self.last_cognitive_budget: Optional[Dict[str, Any]] = None

        #: Cognitive Context Builder（可选）。
        #: 🔒 它是**可注入**的 —— Brain 不认识 SelfModelStore，
        #:    只认识「给我一个 query，我还你一段上下文文本」这个契约。
        #:    （依赖方向：main.py 组装，cognition/ 实现，agents/ 只使用）
        self.context_builder = context_builder

        #: 默认的 Context Query。
        #: 🔒 Runtime 自动用它 —— 调用方**不需要**手写 context_query（§五）。
        #: 显式传入的 context_query 优先于它（供测试和特殊场景用）。
        self.default_context_query = dict(default_context_query or {})

        #: 最近一次 Context 构建失败的信息（§十三：Fail-soft **≠** Fail-silent）。
        #: ⚠️ 只记 agent_id 与异常类型，**不记**上下文原文。
        self.last_context_error: Optional[dict] = None
        self.context_failures: int = 0
        self.recorder = recorder

        # ---- Credential（凭据）----
        # ⚠️ Brain **不持有 key** —— 它只持有「怎么解析 key」这件事，
        #    并把它交给 router。这样 Brain 不会变成 Secret Store。
        self._credential_resolver = credential_resolver
        self._credential_config = dict(credential_config or {})
        self._credential_cache: dict = {}

        # 本 Brain 被用过多少次（调试 / 测试用）
        self.call_count = 0
        self.failure_count = 0
        self.last_response: Optional[LLMResponse] = None

    # ------------------------------------------------------------
    # 基本属性（都是只读的，方便展示和调试）
    # ------------------------------------------------------------
    @property
    def agent_id(self) -> str:
        return self.settings.agent_id

    @property
    def provider(self) -> str:
        """这个 Agent 主要用哪家。注意这只是个名字，不是代码依赖。"""
        return self.settings.provider

    @property
    def credential_ref(self) -> str:
        """逻辑凭据标识（可安全显示）。🔒 里面没有 key。"""
        return self.settings.credential_ref

    def credential_for(self, provider: Optional[str] = None):
        """拿到（并缓存）某个 provider 上的 Credential。🔒 返回值不可直接打印。"""
        return self._credential_for(provider or self.settings.provider)

    def set_thinking(self, enabled: bool,
                     effort: Optional[str] = None, *,
                     phase: str = "work") -> None:
        """
        设置**某一相**的思考模式（默认 work）。

        🔒 只改本 Brain 的状态，不改全局配置，也不影响别的 Agent。
        🔒 不 clamp：Agent 可以把 decision 设成 high，也可以把 work 设成 off。
           “决策优先低思考”是实验的**默认值**，不是 Runtime 的强制规则。
        🔒 provider 不支持时不会报错 —— 是否注入由 `generate()` 决定。
        """
        key = str(phase or "").strip().lower()
        if key not in self.thinking_policy:
            raise ValueError(
                f"未知的思考阶段 {phase!r}；只支持 "
                f"{sorted(self.thinking_policy)}")
        self.thinking_policy[key] = {
            "enabled": bool(enabled),
            "effort": (str(effort).strip().lower() if effort else None),
        }

    def thinking_for(self, phase: str) -> Optional[Dict[str, Any]]:
        """读取某一相的当前策略（None = 不注入）。"""
        return self.thinking_policy.get(str(phase or "").strip().lower())

    def set_temperature(self, value: Optional[float], *,
                        phase: str = "work") -> None:
        """
        设置某一相的 temperature；`None` = 回到 config 的默认值。

        🔒 不 clamp 到某个“正确值”：只做 0..2 的结构校验
           （与 DeepSeek/OpenAI 兼容 API 的范围一致）。
        """
        key = str(phase or "").strip().lower()
        if key not in self.temperature_policy:
            raise ValueError(
                f"未知的 temperature 阶段 {phase!r}；只支持 "
                f"{sorted(self.temperature_policy)}")
        if value is None:
            self.temperature_policy[key] = None
            return
        number = float(value)
        if number < 0.0 or number > 2.0:
            raise ValueError(
                f"temperature 必须在 0..2，收到 {value!r}")
        self.temperature_policy[key] = number

    def temperature_for(self, phase: str) -> Optional[float]:
        """读取某一相的 temperature 覆盖（None = 用 config 默认值）。"""
        return self.temperature_policy.get(str(phase or "").strip().lower())

    def set_cognitive_budget(self, value: Optional[int], *,
                             phase: str = "work") -> None:
        """
        设置**某一相**的认知预算偏好（单位：max_tokens）。

        🔒 这是 Agent 自己的偏好，不是硬限制：
            · `None` = 回到 config 的 `max_tokens`；
            · 真正的上限由调用方的 `cognitive_budget_cap` 与 Provider
              的结构边界决定。
        🔒 不做“推荐值”校验，只做结构性校验（必须是正整数）。
        """
        key = str(phase or "").strip().lower()
        if key not in self.cognitive_budget_policy:
            raise ValueError(
                f"未知的认知预算阶段 {phase!r}；只支持 "
                f"{sorted(self.cognitive_budget_policy)}")
        if value is None:
            self.cognitive_budget_policy[key] = None
            return
        if isinstance(value, bool):
            raise ValueError(f"cognitive_budget 必须是整数，收到 {value!r}")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"cognitive_budget 必须是整数，收到 {value!r}") from None
        if number <= 0:
            raise ValueError(
                f"cognitive_budget 必须是正整数，收到 {value!r}")
        self.cognitive_budget_policy[key] = number

    def cognitive_budget_for(self, phase: str) -> Optional[int]:
        """读取某一相的认知预算偏好（None = 用 config 默认值）。"""
        return self.cognitive_budget_policy.get(
            str(phase or "").strip().lower())

    def cognitive_budget_preferences(self) -> Dict[str, Any]:
        """当前分相预算偏好 + config 默认值（观测用，不含 secret）。"""
        return {
            "config_default": int(self.settings.max_tokens),
            "per_phase": {
                phase: value
                for phase, value in self.cognitive_budget_policy.items()
                if value is not None
            },
        }

    @property
    def thinking_enabled(self) -> Optional[bool]:
        """向后兼容视图：work 相是否开启。"""
        policy = self.thinking_policy.get("work") or {}
        return policy.get("enabled")

    @property
    def thinking_effort(self) -> Optional[str]:
        """向后兼容视图：work 相强度。"""
        policy = self.thinking_policy.get("work") or {}
        return policy.get("effort")

    @staticmethod
    def _as_budget_int(value: Any) -> Optional[int]:
        """把预算值规范成正整数；认不出来返回 None（不猜）。"""
        if value is None or isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _credential_for(self, provider: Optional[str]):
        """
        按 **实际使用的 provider** 解析凭据。

        ⚠️ 为什么按 effective provider 而不是配置里的 provider：
            `--provider doubao` 这种临时切换必须换到 doubao 的凭据，
            否则会拿 deepseek 的 key 去请求豆包。
        """
        resolver = self._credential_resolver
        if resolver is None:
            return None

        name = (provider or self.settings.provider or "").strip().lower()
        if name in self._credential_cache:
            return self._credential_cache[name]

        cred = resolver.resolve(
            provider=name,
            agent_id=self.agent_id,
            credential=self._credential_config,
            credential_ref=self.settings.credential_ref,
        )
        self._credential_cache[name] = cred
        return cred

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def system_prompt(self) -> str:
        return self.settings.system_prompt

    # ------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------
    def _credential_meta(self, provider: str) -> dict:
        """
        给记录器用的**凭据元信息**（绝不含 secret）。

        🔒 `env` 是**环境变量名**，不是值。
           它回答「这次调用用的是哪个变量」——
           这正是历史审计里 `llm_calls` 回答不了的那个问题。
        """
        try:
            cred = self._credential_for(provider)
        except Exception:                                     # noqa: BLE001
            return {}
        cfg = dict(self._credential_config or {})
        return {
            "credential_ref": getattr(cred, "ref", "") or "",
            "credential_source": getattr(cred, "source", "") or "",
            "credential_env": str(cfg.get("api_key_env") or ""),
        }

    def preflight_credential(self) -> dict:
        """
        调用真实 API **之前**检查凭据是否就绪（§九）。

        🔒 目标：以后再增加 Agent 时，**不要等真实 API 返回 401
           才发现配置错误**。

        返回 `describe()` 的结果；若 provider 需要 key 但没有拿到，
        **抛异常**（尽早暴露），而不是让它发出去换一个 401。
        """
        provider = self.settings.provider
        # 🔒 「要不要 key」由 **Provider 自己** 声明
        #    （`LLMProvider.requires_api_key`），Brain 只负责问。
        #
        #    ⚠️ 第一版这里写的是 `if provider == "mock": 跳过` ——
        #       那让 `agents/` 出现了厂商名分支，
        #       被 `test_no_provider_branching_anywhere_outside_providers`
        #       当场抓住。守卫是对的：那种写法每加一个 provider
        #       就要改一次 Brain。
        if not self.router.requires_api_key(provider):
            return {"source": "none", "provider": provider,
                    "ref": "", "fingerprint": "", "present": True,
                    "credential_available": True,
                    "skipped": "这个 provider 声明它不需要 key"}
        cred = self._credential_for(provider)
        info = cred.describe() if hasattr(cred, "describe") else {}
        if not info.get("credential_available"):
            raise RuntimeError(
                f"Agent {self.settings.agent_id!r} 的 provider {provider!r} "
                f"没有可用的凭据（source={info.get('credential_source')!r}）。\n"
                f"  · 在 agents/{self.settings.agent_id}/config.yaml 的 "
                f"brain.credential.api_key_env 指定一个**有值**的环境变量；\n"
                f"  · 或修好该 provider 的全局 api_key_env。"
            )
        return info

    @staticmethod
    def _capture_request_evidence(*, messages: Any, system: Optional[str],
                                  ctx: Any, call_purpose: Optional[str],
                                  cognition_turn_id: Optional[str],
                                  experiment_run_id: Optional[str]) -> dict:
        """
        在 Provider 边界记录**最终请求**里到底有没有关键观察（§7）。

        返回一个 dict（**只含摘要与命中情况**，不含请求原文）：

            context_snapshot_id
            critical_observation_ids_selected     从 ctx 的关键通道取出
            critical_observation_ids_rendered     渲染文本里**确实出现**的
            critical_observation_ids_found_in_final_request
            final_request_contains_expected_critical_observation

        🔒 判据是「experience_id 是否出现在**最终送给 Provider 的文本**里」，
           而不是「ctx 里有没有」。两者不同：渲染可能为空，
           拼接可能失败，`effective_system` 可能没被用上。
        """
        import hashlib

        def _text_of(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                # 🔒 图片 base64 不能进请求证据：它会把日志/DB 撑爆，
                #    而且对「关键观察是否在请求里」没有证明价值。
                if value.startswith("data:image/"):
                    return "[image]"
                return value
            if isinstance(value, (list, tuple)):
                return "\n".join(_text_of(v) for v in value)
            if isinstance(value, Mapping):
                # 多模态 content part：只留类型占位，不展开 URL/base64。
                if str(value.get("type") or "") in ("image_url", "file"):
                    return "[image]"
                return "\n".join(f"{k}={_text_of(v)}"
                                  for k, v in sorted(value.items()))
            return str(value)

        request_text = _text_of(messages) + "\n" + _text_of(system)

        selected = []
        rendered = []
        if ctx is not None:
            selected = [e.experience_id
                        for e in (getattr(ctx, "critical_observations", ()) or ())]
            rendered_text = ""
            try:
                rendered_text = ctx.to_text()
            except Exception:                                 # noqa: BLE001
                rendered_text = ""
            rendered = [eid for eid in selected if eid in rendered_text]

        # 🔑 真正的判据：id 是否出现在**最终请求文本**里
        found = [eid for eid in selected if eid in request_text]

        digest = hashlib.sha256(
            request_text.encode("utf-8", "replace")).hexdigest()[:16]

        return {
            "context_snapshot_id": (
                f"ctx_{digest}" if ctx is not None else None),
            "critical_observation_ids_selected": selected,
            "critical_observation_ids_rendered": rendered,
            "critical_observation_ids_found_in_final_request": found,
            # 期望 == 实际命中。没有期望时也视为「没有被丢掉」。
            "final_request_contains_expected_critical_observation":
                bool(selected) == bool(found),
            # 🔒 规范化请求摘要：覆盖 messages + effective_system 的拼接文本。
            "request_sha256_16": digest,
            "request_chars": len(request_text),
            "call_purpose": call_purpose,
            "cognition_turn_id": cognition_turn_id,
            "experiment_run_id": experiment_run_id,
        }

    def generate(
        self,
        messages: Union[str, Sequence[Message], None] = None,
        system: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        record: bool = True,
        task_id: Optional[str] = None,
        session_started_at: Optional[str] = None,
        #: 逻辑会话 scope（front|... / task|... / alarm|...）。
        #: Provider 用它生成缓存/路由 header；不传时按 task/agent 派生。
        llm_session_scope: Optional[str] = None,
        #: 🔴 Cognitive Budget V1：Agent 请求的认知预算（max_tokens 上限）。
        #: Runtime 负责裁剪；**禁止**由 AIC 余额换算而来。
        requested_cognitive_budget: Optional[int] = None,
        #: 🔴 Human / 系统给这次调用的**硬上限**。
        #: 实际预算 = min(Agent 偏好, cap)；Provider 还会做结构裁剪。
        cognitive_budget_cap: Optional[int] = None,
        context_query: Optional[dict] = None,
        cognitive: bool = True,
        #: 🔴 Telemetry Repair V2：这次调用的**用途**，由调用方声明。
        #: 见 `LLMCallRecorder.PURPOSE_*`。缺省是 `unspecified` ——
        #: 一个会被审计查询暴露出来的**错误标记**，不是正常值。
        call_purpose: Optional[str] = None,
        #: 认知轮次 id（**仅**认知调用有值）。由生产 `_turn()` 生成并透传。
        cognition_turn_id: Optional[str] = None,
        #: 本次实验运行的唯一 id。
        experiment_run_id: Optional[str] = None,
        #: 🔴 认知阶段：decision / work。
        #: 只决定用哪一相的 thinking 策略；不改变消息或 provider。
        phase: str = "decision",
        **kwargs: Any,
    ) -> LLMResponse:
        """
        生成一次回复。

        参数全部可选 —— 不传就用这个 Agent 配置里的默认值。

        context_query（§十七 Cognitive Context 的接入点）：
            传一个 dict（world/domain/situation/stimulus/task/...），
            Brain 会用它构建本轮的 Cognitive Context，
            并把渲染结果**前置到 system** 里。

        cognitive（§十五 区分「Agent Cognition」与「内部 LLM utility」）：
            True（默认）= 这是一次 **Agent 认知**，应当带上 Cognitive Context。
            False        = 这是**内部 utility 调用**（探活、解析、格式化…），
                           不注入上下文。

            ⚠️ 默认是 True，因为默认路径应该**自动**接入（§五）。
               内部 utility 的调用点必须**显式**写 cognitive=False ——
               这样「哪些调用属于认知」是一个**显式决定**，不是漏网之鱼。

        不传 context_query 时：
            用 `default_context_query`（Runtime 组装时注入）。
            → 这就是「调用方不需要手写 context_query」的实现方式。

            🔒 三个刻意的设计选择：
              ① 它是**可选的** —— 不传就跟以前完全一样（零回归）
              ② Brain **不知道** Self Model 在哪 —— 只调用注入的 builder
              ③ 渲染出来的语气是「我目前持有的相关认知」，
                 **不是**「你必须…」—— 见 cognition/context.py

        record=False 表示这次调用不记账。
            什么情况下用？比如系统自己发一个「探活」请求，
            那不是 Agent 的经营行为，不该算在它的成本里。

        requested_cognitive_budget / cognitive_budget_cap（Cognitive Budget V1）：
            Agent 的**偏好**（可来自本相 policy 或显式参数）与
            Human / 系统给的**硬上限**。实际 `max_tokens` = min(偏好, 上限)，
            Provider 还会做 MIN / MAX 裁剪；裁剪原因在响应的
            `budget_clipped` 上，下一轮 prompt 会显示。

        失败时会：
            1. 记一条 success=0 的记录（失败也是重要的经济信息）
            2. 把异常原样抛出去（不能让 Agent 以为调用成功了）
        """
        # --- 用默认值补齐参数 ---
        # 注意 system 的处理：显式传入的优先，
        # 没传才用 Agent 自己的 system_prompt。
        effective_system = system if system is not None else self.system_prompt

        # ---- Cognitive Context V1（§十七）--------------------------------
        # 若注入了 builder 且调用方给了 context_query，就构建本轮上下文，
        # 并把渲染结果**前置**到 system。
        # 🔒 不改变 messages；不改变 provider；只是把「我目前持有的
        #    相关认知」放进 system 的最前面。
        self.last_context_stats = None
        #: 本次调用的 llm_call_id（recorder 生成并返回）。
        #: 🔒 失败路径也会赋值 —— 「这次失败了」同样要可追踪（§5.1）。
        self.last_call_id: Optional[str] = None
        #: Provider 请求边界的证据（§7）。
        self.last_request_evidence: Optional[dict] = None
        #: 本次调用的**进程内回执**：tokens + cost_ai + token_source。
        #: 失败 / 未记录时为 None。
        self.last_usage: Optional[dict] = None
        # 🔴 必须区分 None 与 {} —— 这是两种不同的意思：
        #      None = 「这次不要 Context」
        #      {}   = 「要 Context，但不限定检索情境」
        #    写成 `context_query or default or None` 会把 {} 当成 None，
        #    默认路径于是**静默不构建上下文**（实测踩过）。
        if not cognitive:
            query = None
        else:
            base_query = (context_query if context_query is not None
                          else self.default_context_query)
            query = dict(base_query or {})
            # 这两个值是当前调用的**事实**，不是 Agent 自己填的参数：
            # 它们让 EconomicContext 能聚合 task/session 用量。
            if task_id:
                query.setdefault("task_id", task_id)
            if session_started_at:
                query.setdefault("session_started_at", session_started_at)
        built_ctx: Any = None      # §7：真正构建出来的 Context（供边界取证）
        if self.context_builder is not None and query is not None:
            try:
                ctx = self.context_builder.build(
                    self.settings.agent_id, **query)
                built_ctx = ctx
                self.last_context_stats = ctx.stats()
                rendered = ctx.to_text()
                if rendered:
                    effective_system = (
                        f"{rendered}\n\n{effective_system}"
                        if effective_system else rendered
                    )
            except Exception as exc:                      # noqa: BLE001
                # 上下文构建失败**不能让 LLM 调用失败**（§十四：
                # Cognitive Context 是增量增强，不是绝对依赖）
                #
                # §十三：Fail-soft **≠** Fail-silent。必须可观测，
                # 但**不**记录上下文原文 / Memory / Prompt。
                self.last_context_error = {
                    "agent_id": self.settings.agent_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc)[:200],
                }
                self.context_failures += 1
                logger.warning(
                    "Cognitive Context 构建失败，本次降级为空 "
                    "(agent_id=%s, error=%s): %s",
                    self.settings.agent_id, type(exc).__name__, exc)
        effective_provider = provider or self.settings.provider

        # 判断这次是不是「临时换了另一家 provider」。
        #
        # 【为什么这件事很重要】这是个很容易踩的坑：
        #     Agent 的配置是 provider=mock, model=mock-model-v1。
        #     如果临时指定 provider=deepseek 却把
        #     model=mock-model-v1 一起带过去，
        #     DeepSeek 会收到一个它不认识的模型名然后报错。
        #
        #     原因在于 self.settings.model 是「针对原 provider
        #     解析出来的模型名」，换 provider 之后它就失效了。
        #
        # 所以规则是：
        #     显式给了 model        → 用它
        #     换了 provider          → 不传 model，让新 Provider 用自己的默认模型
        #     没换 provider          → 用 Agent 配置里的 model
        provider_switched = (
            effective_provider.strip().lower()
            != str(self.settings.provider).strip().lower()
        )

        if model is not None:
            effective_model = model
        elif provider_switched:
            effective_model = None
        else:
            effective_model = self.settings.model or None

        # ---- LLM 会话（缓存 / 路由）----------------------------------
        # 同一逻辑对话复用同一个值；不同 Task / 对话 / 闹钟必须不同。
        # provider/model 也进 hash：换模型就换会话，避免跨模型复用。
        session_scope = str(llm_session_scope or "").strip()
        if not session_scope and task_id:
            session_scope = task_scope(
                str(task_id), agent_id=self.settings.agent_id)
        if not session_scope:
            session_scope = (
                f"agent|{self.settings.agent_id}"
                f"|purpose={call_purpose or 'utility'}")
        llm_session_id = build_session_id(
            session_scope,
            provider=effective_provider,
            model=effective_model or "",
        )

        # 显式传入 > phase 策略 > config 默认值
        phase_key = str(phase or "").strip().lower()
        phase_temperature = self.temperature_policy.get(phase_key)
        if temperature is not None:
            effective_temperature = float(temperature)
        elif phase_temperature is not None:
            effective_temperature = float(phase_temperature)
        else:
            effective_temperature = self.settings.temperature
        self.last_temperature = effective_temperature

        # ---- Cognitive Budget（按 phase 选择 Agent 自己的偏好）---------
        #
        #   🔒 三种来源，优先级从高到低：
        #      ① 调用方显式 `requested_cognitive_budget`（生产 / 实验）
        #      ② Agent 在本相设置的偏好（信封 → Brain）
        #      ③ config 的 `brain.max_tokens`（默认偏好）
        #
        #   🔒 `max_tokens` 显式参数 / `cognitive_budget_cap` 是**硬上限**，
        #      不参与“偏好”选择。实际值 = min(偏好, 所有硬上限)。
        #   🔒 Provider 仍会做自己的结构裁剪（MIN / MAX）；
        #      被裁剪的原因会出现在响应的 `budget_clipped` 上。
        phase_key = str(phase or "").strip().lower()
        preference = self._as_budget_int(requested_cognitive_budget)
        if preference is None:
            preference = self.cognitive_budget_for(phase_key)
        if preference is None:
            preference = self._as_budget_int(self.settings.max_tokens)
        effective_max_tokens = preference
        clipped = None
        cap_reason = ""
        hard_caps = []
        if max_tokens is not None:
            hard_caps.append((self._as_budget_int(max_tokens),
                              "above_caller_limit"))
        if cognitive_budget_cap is not None:
            hard_caps.append((self._as_budget_int(cognitive_budget_cap),
                              "above_cap"))
        for cap_value, reason in hard_caps:
            if cap_value is None:
                continue
            if effective_max_tokens is not None and \
                    effective_max_tokens > cap_value:
                effective_max_tokens = cap_value
                clipped = reason
                cap_reason = reason
        self.last_cognitive_budget = {
            "phase": phase_key,
            "requested": preference,
            "effective": effective_max_tokens,
            "cap": (self._as_budget_int(cognitive_budget_cap)
                    if cognitive_budget_cap is not None else None),
            "clipped": clipped,
            "clipped_by": cap_reason or None,
        }

        # ---- 思考模式（按 phase 选择 Agent 自己的策略）-----------------
        #
        #   🔒 Brain **不认识厂商**：它只根据 Agent 的 phase 策略注入通用字段；
        #      provider 是否支持、是否丢弃，由 Provider 自己的能力声明
        #      （`supports_thinking` / `supports_reasoning_effort`）决定。
        #   🔒 调用方显式传了 `thinking` / `reasoning_effort` 时不覆盖。
        #   🔒 `enabled=False` 时不注入 `reasoning_effort` ——
        #      否则会把「关闭思考」和「强度」同时发出去。
        #   🔒 **不做 clamp**：decision 可以是 high，work 可以是 off；
        #      “决策优先低思考”只体现在实验设置的默认值里。
        policy = self.thinking_policy.get(str(phase or "").strip().lower())
        if policy and "thinking" not in kwargs:
            enabled = bool(policy.get("enabled"))
            kwargs["thinking"] = {
                "type": "enabled" if enabled else "disabled"}
            effort = policy.get("effort")
            if enabled and effort and "reasoning_effort" not in kwargs:
                kwargs["reasoning_effort"] = effort

        logger.debug(
            "%s 的大脑正在思考（provider=%s model=%s）",
            self.agent_id,
            effective_provider,
            effective_model or "(默认)",
        )

        # --- 调用 ---
        try:
                # ============================================================
            # §7 Provider 请求边界取证 —— **最重要的一环**
            # ============================================================
            #
            # 🔒 为什么必须在**这里**：
            #    `ctx.to_text()` 只证明「构造过一个含关键观察的 Context」，
            #    不证明「它真的进了这次请求」。中间还有拼接、截断、
            #    以及 `effective_system` 是否被真正使用。
            #
            #    所以证据必须取自**最终交给 router 的那两个值**：
            #        messages  +  effective_system
            #
            # 🔒 只记**摘要与命中情况**，不记原文（体积/隐私），
            #    并保存足以核对「关键观察确实在请求里」的最小证据。
            #    绝不记 Authorization / API key。
            self.last_request_evidence = self._capture_request_evidence(
                messages=messages, system=effective_system, ctx=built_ctx,
                call_purpose=call_purpose, cognition_turn_id=cognition_turn_id,
                experiment_run_id=experiment_run_id,
            )

            # 可选 Prompt 日志：记录最终交给 Router 的输入。
            # 🔒 默认关闭；失败不影响调用。
            prompt_log.record(
                self.agent_id,
                phase=str(phase or ""),
                system=str(effective_system or ""),
                messages=messages,
                task_id=str(task_id or ""),
                provider=str(effective_provider or ""),
                model=str(effective_model or ""),
                purpose=str(call_purpose or ""),
            )

            response = self.router.generate(
                messages=messages,
                provider=effective_provider,
                credential=self._credential_for(effective_provider),
                system=effective_system or None,
                model=effective_model,
                temperature=effective_temperature,
                max_tokens=effective_max_tokens,
                    requested_cognitive_budget=preference,
                    llm_session_id=llm_session_id,
                    **kwargs,
                )
        except Exception as exc:
            self.failure_count += 1

            if record and self.recorder is not None:
                # 失败也记一笔。注意这里用的是「请求的」provider/model，
                # 因为调用根本没成功，拿不到实际使用的模型名。
                self.last_call_id = self.recorder.record(
                    agent_id=self.agent_id,
                    response=None,
                    task_id=task_id,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                    provider=effective_provider,
                    model=effective_model or "",
                    call_purpose=call_purpose,
                    cognition_turn_id=cognition_turn_id,
                    experiment_run_id=experiment_run_id,
                    **self._credential_meta(effective_provider),
                )

            logger.debug(
                "%s 调用模型失败：%s: %s",
                self.agent_id,
                type(exc).__name__,
                exc,
            )
            raise

        # --- 记账 ---
        self.call_count += 1
        self.last_response = response

        if record and self.recorder is not None:
            self.last_call_id = self.recorder.record(
                agent_id=self.agent_id,
                response=response,
                task_id=task_id,
                success=True,
                call_purpose=call_purpose,
                cognition_turn_id=cognition_turn_id,
                experiment_run_id=experiment_run_id,
                # 🔒 只记**元信息**（ref / source / 变量名），**绝不记 key 本身**。
                **self._credential_meta(effective_provider),
            )
            # 🔴 进程内回执：用**同一个** CostCalculator 计算，
            #    与刚写入 llm_calls.cost_ai 的规则完全一致。
            cost_ai, usage_tokens, cost_known = \
                self.recorder.cost_of(response)
            self.last_usage = {
                "call_id": self.last_call_id,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "total_tokens": usage_tokens,
                "cost_ai": cost_ai,
                "cost_known": bool(cost_known),
                "token_source": response.token_source,
                "provider": response.provider,
                "model": response.model,
                "created_at": getattr(response, "created_at", ""),
            }

        return response

    # ------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------
    def think(
        self,
        prompt: str,
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """
        只想要文本回复时用这个。

        注意：如果只想拿文本，Agent 会丢掉 token 用量等经济信息。
        所以在真正要做经济决策的场合，请直接用 generate() 拿完整响应。
        """
        response = self.generate(messages=prompt, system=system, **kwargs)
        return response.content

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """自述。给启动横幅和调试输出用。"""
        return {
            "agent_id": self.agent_id,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "cognitive_budget": self.cognitive_budget_preferences(),
            "has_recorder": self.recorder is not None,
            "calls": self.call_count,
            "failures": self.failure_count,
        }

    def __str__(self) -> str:
        return f"Brain({self.agent_id} → {self.provider}/{self.model or '默认模型'})"

    def __repr__(self) -> str:
        return (
            f"Brain(agent_id={self.agent_id!r}, provider={self.provider!r}, "
            f"model={self.model!r}, calls={self.call_count})"
        )
