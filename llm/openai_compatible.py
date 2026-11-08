"""
OpenAI 兼容 Provider 基类
============================================================

【为什么需要这个文件】

    在加豆包的时候发现一件事：

        DeepSeek        POST /chat/completions   Bearer
        豆包（火山方舟）  POST /chat/completions   Bearer
        Grok (xAI)      POST /chat/completions   Bearer

    三家的「形状」完全一样 —— 这是业界事实标准（OpenAI 兼容格式）。
    请求体都是 model/messages/temperature/max_tokens，
    响应体都是 choices[0].message.content + usage.{prompt,completion,total}_tokens。

    如果每家都复制一份，就会有 3 份几乎相同的 HTTP、解析、错误翻译代码。
    改一个 bug 要改三处，而且迟早会改漏其中一处。

    所以把「形状」抽到这里，「身份」留给子类。

【子类只需要回答这几个问题】

    · 我叫什么名字？                    name / PROVIDER_LABEL
    · 我的 base_url 默认是什么？         DEFAULT_BASE_URL
    · 我的默认模型是什么？               DEFAULT_MODEL
    · 我读哪个环境变量拿 Key？           DEFAULT_API_KEY_ENV
    · 我的错误码有没有特殊含义？          _extra_status_hint()

    就这些。DeepSeek 和豆包各自只需要 100 行左右。

【这里的名字里为什么带 openai】

    这是【协议名】，不是【厂商名】。
    就像「HTTP」不等于某一家公司 ——
    OpenAI 兼容格式已经是行业通用协议，用它的厂商远不止 OpenAI。

    所以这个类的存在不代表我们接了 OpenAI。
    Agent 层依然不认识任何厂商，包括 OpenAI。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Union

from llm.base import (
    TOKEN_SOURCE_API,
    TOKEN_SOURCE_UNKNOWN,
    AuthenticationError,
    InvalidResponseError,
    LLMHTTPError,
    LLMProvider,
    LLMRequestError,
    LLMResponse,
    LLMTimeoutError,
    Message,
    RateLimitError,
    messages_have_images,
    normalize_messages,
)

from core.logging_setup import get_logger  # noqa: E402

logger = get_logger(__name__)


# ============================================================
# 输出契约辅助（§四 / §五）
# ============================================================
#: 各家网关给「推理内容」用的字段名。
#:
#: DeepSeek 用 `reasoning_content`；另一些 OpenAI-compatible 网关
#: 用 `reasoning`。这里**只**做只读识别 —— 认识了也不代表能用它当 content。
REASONING_FIELDS = ("reasoning_content", "reasoning")


def _read_reasoning(message: Mapping[str, Any]) -> Optional[str]:
    """
    从返回体的 `message` 里读推理内容。

    🔒 **它只用于诊断，永远不会变成 content。**
       §五 明令禁止 `if not content: content = reasoning_content` ——
       推理与最终答复语义不同，前者是过程，后者是交付物。
       把过程写进 Workspace 会让下游 Agent 以为模型决定了什么。

    返回 None 表示「这次响应里没有推理内容」，与「推理内容为空串」区分开。
    """
    for field_name in REASONING_FIELDS:
        value = message.get(field_name)
        if isinstance(value, str):
            return value
    return None


# ============================================================
# HTTP 层
# ============================================================
@dataclass
class HTTPResult:
    """
    一次 HTTP 调用的结果。

    注意：4xx / 5xx 也走这个结构，不算「异常」。
    因为「服务器返回了 401」和「网络断了」是两件不同的事：
        前者我们拿到了明确的答案，应该按状态码分门别类地翻译；
        后者我们什么都没拿到。
    """

    status: int
    body: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """把 body 解析成 JSON。解析不了就抛 InvalidResponseError。"""
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, TypeError) as exc:
            preview = (self.body or "")[:200].replace("\n", " ")
            raise InvalidResponseError(
                f"返回体不是合法 JSON：{exc}\n"
                f"HTTP 状态：{self.status}\n"
                f"返回体开头：{preview!r}"
            ) from exc


# HTTP 调用的函数签名。
# 换成别的实现（比如以后换成 httpx）只要满足这个签名即可。
HTTPPost = Callable[
    [str, Mapping[str, Any], Mapping[str, str], float],
    HTTPResult,
]


def default_http_post(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
) -> HTTPResult:
    """
    用标准库发一个 JSON POST。

    约定（很重要，因为测试会照着这个约定来伪造）：
        · 拿到了 HTTP 响应（含 4xx/5xx）  → 返回 HTTPResult
        · 根本没拿到响应（超时/断网）      → 抛 LLMTimeoutError / LLMRequestError

    为什么用标准库而不是 requests / httpx？
        这个项目的原则是「能用标准库就不引第三方库」。
        一次 HTTPS POST 加 JSON 解析，urllib 完全够用。
        代价是代码稍微啰嗦，换来的是运行时第三方依赖为 0。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=dict(headers),
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return HTTPResult(
                status=int(response.status),
                body=body,
                headers={k: v for k, v in response.headers.items()},
            )

    except urllib.error.HTTPError as exc:
        # 服务端返回了错误状态码。把 body 读出来，方便翻译成有用的话。
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读不出来就算了，不掩盖原始错误
            body = ""
        return HTTPResult(
            status=int(exc.code),
            body=body,
            headers={k: v for k, v in (exc.headers or {}).items()},
        )

    except urllib.error.URLError as exc:
        # URLError 常常把真正的超时包在 reason 里
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise LLMTimeoutError(f"请求超时（{timeout} 秒）") from exc
        raise LLMRequestError(f"网络错误：{exc.reason}") from exc

    except (socket.timeout, TimeoutError) as exc:
        raise LLMTimeoutError(f"请求超时（{timeout} 秒）") from exc

    except OSError as exc:
        raise LLMRequestError(f"网络错误：{exc}") from exc


# ============================================================
# 基类
# ============================================================
class OpenAICompatibleProvider(LLMProvider):
    """
    OpenAI 兼容格式的 Provider 基类。

    子类必须覆盖：
        name                 配置里用的名字，比如 "deepseek"
        DEFAULT_BASE_URL     比如 "https://api.deepseek.com"
        DEFAULT_API_KEY_ENV  比如 "DEEPSEEK_API_KEY"

    子类通常还要覆盖：
        DEFAULT_MODEL
        PROVIDER_LABEL       错误信息里给人看的名字
    """

    name = "openai_compatible"
    requires_api_key = True

    # 给人看的名字，只用在错误提示里
    PROVIDER_LABEL = "这个模型服务"

    # 各家不同的默认值（子类覆盖）
    DEFAULT_BASE_URL = ""
    DEFAULT_MODEL = ""
    DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
    DEFAULT_BASE_URL_ENV = "OPENAI_BASE_URL"
    DEFAULT_MODEL_ENV = "OPENAI_MODEL"
    DEFAULT_CHAT_PATH = "/chat/completions"
    DEFAULT_TIMEOUT = 60.0

    # 模型名是不是必须由用户显式指定（没有合理的默认值）
    requires_explicit_model = False

    #: 🔴 Configuration System Refactor：这个 adapter **需不需要 base_url**。
    #:
    #: 与 `requires_api_key` 同一种手法 —— 由 **Provider 自己声明**，
    #: 让 `ConfigValidator` 不必在 `core/` 里硬编码 adapter 名字。
    #: OpenAI 兼容协议**必须**有端点，所以是 True；Mock 声明 False。
    requires_base_url = True

    def __init__(
        self,
        provider_config: Optional[Mapping[str, Any]] = None,
        default_model: str = "",
        http_post: Optional[HTTPPost] = None,
        api_key_override: Optional[str] = None,
    ):
        """
        参数：
            provider_config: config.yaml 里该 provider 那一段
            default_model:   兜底模型名
            http_post:       注入自定义 HTTP 实现（测试用）
            api_key_override: **已经解析好的** key

        【为什么是「已解析好的」而不是「credential 配置」】

            Provider **不应该**去理解 credential 配置的优先级规则 ——
            那是 `CredentialResolver` 的职责。
            Provider 只接受最终结果，于是「key 从哪来」这件事
            全项目只有**一个**地方定义（resolver）。

        🔒 注意它**仍然不读配置文件里的 key** ——
           传进来的是 resolver 的结果，不是让它自己去翻 config。
        """
        super().__init__(provider_config=provider_config, default_model=default_model)

        cfg = self.config

        # --- Key：只从环境变量读，绝不从配置文件读 ---
        # 存的是「环境变量的名字」，不是 key 本身。
        self.api_key_env = str(cfg.get("api_key_env") or self.DEFAULT_API_KEY_ENV)
        # 🔒 优先级：注入的（已由 CredentialResolver 解析）> 环境变量
        self._api_key = (
            api_key_override or os.environ.get(self.api_key_env, "")
        ).strip()

        # --- base_url：环境变量 > 配置 > 类默认值 ---
        self.base_url_env = str(cfg.get("base_url_env") or self.DEFAULT_BASE_URL_ENV)
        self.base_url = (
            os.environ.get(self.base_url_env, "").strip()
            or str(cfg.get("base_url") or self.DEFAULT_BASE_URL)
        ).rstrip("/")

        # --- model：环境变量 > 配置 > 构造参数 > 类默认值 ---
        self.model_env = str(cfg.get("model_env") or self.DEFAULT_MODEL_ENV)
        env_model = os.environ.get(self.model_env, "").strip()
        if env_model:
            self.default_model = env_model
        elif not self.default_model:
            self.default_model = self.DEFAULT_MODEL

        # --- 其它 ---
        # chat_path 做成可配置：万一某家的路径不一样，
        # 改配置就行，不用改代码。
        self.chat_path = str(cfg.get("chat_path") or self.DEFAULT_CHAT_PATH)
        self.timeout = float(cfg.get("timeout_seconds") or self.DEFAULT_TIMEOUT)
        #: 最近一次调用的认知预算状态（供 parse_response 回填到 LLMResponse）
        self._last_requested_budget: Optional[int] = None
        self._last_max_tokens: Optional[int] = None
        self._last_budget_clipped: Optional[str] = None

        # 请求体里 max_tokens 这个字段的名字。
        # 为什么要做成可配置？
        #     因为 OpenAI 新接口推出了 max_completion_tokens，
        #     有些模型只认新名字。这属于「会变的东西」，不该硬编码。
        self.max_tokens_field = str(
            cfg.get("max_tokens_field") or "max_tokens"
        )

        # 额外塞进请求体的字段（各家的小差异兜底用）
        extra_body = cfg.get("extra_body")
        self.extra_body: Dict[str, Any] = (
            dict(extra_body) if isinstance(extra_body, Mapping) else {}
        )

        # 可替换的 HTTP 层
        self._http_post: HTTPPost = http_post or default_http_post

        self.call_count = 0
        self.error_count = 0

    # ------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------
    @property
    def endpoint(self) -> str:
        """完整的请求地址。"""
        if not self.base_url:
            return ""
        return f"{self.base_url}{self.chat_path}"

    def is_available(self) -> bool:
        """
        这个端点**能不能发起调用**？

        🔒 判据 = 凭据 + 端点 + （若该 provider 声明了）端点级模型兜底。

        【`requires_explicit_model` 是什么】
            有些厂商（豆包 / 百炼）**没有普适的默认模型名** ——
            填接入点 ID 还是模型名取决于用户自己的账号。
            猜一个只会让人困惑，所以这些 provider 声明
            `requires_explicit_model = True`：**必须**在端点层
            给出一个兜底模型，否则宁可早报错。

            ⚠️ 这条策略是 provider 的**声明**，不是写死的全局规则 ——
               `deepseek` 声明 False（它有公认的默认模型）。

        🔒 真正「这次调用没有模型名」的情况在**调用时**另有一道检查
           （`generate()` 里的 `resolve_model()`），
           因为只有真的要用的时候才知道缺不缺。
        """
        if not (self._api_key and self.endpoint):
            return False
        if self.requires_explicit_model and not self.default_model:
            return False
        return True

    def unavailable_reason(self) -> Optional[str]:
        """
        说明为什么不能用。

        【安全提醒】绝对不要把 key 的值打出来。
        只说「哪个环境变量没设置」就足够了。
        """
        if not self._api_key:
            return self._missing_key_message()

        if self.requires_explicit_model and not self.default_model:
            return self._missing_model_message()

        if not self.endpoint:
            return "base_url 为空"

        return None

    def _missing_key_message(self) -> str:
        return (
            f"环境变量 {self.api_key_env} 未设置或为空。\n"
            f"请复制 .env.example 为 .env，然后填入你的 Key：\n"
            f"    cp .env.example .env\n"
            f"    # 编辑 .env，填入 {self.api_key_env}=<你的Key>\n"
            f"\n"
            f"（提示里故意不写 Key 的示例前缀 —— "
            f"否则密钥扫描工具会把提示文案误报成泄露。）"
        )

    def _missing_model_message(self) -> str:
        """
        ⚠️ 这条文案以前指的是 `llm.providers.<name>.model` 与
           `BAILIAN_MODEL` 之类的环境变量 —— **Configuration V1 之后
           这些路径都不存在了**，照着它去改配置只会白费功夫。
           现在指向真正生效的两个位置。
        """
        return (
            f"{self.PROVIDER_LABEL} 这次调用没有拿到模型名。\n"
            f"  正常路径：Agent 的 `agents.<id>.brain.model` 指向一个 "
            f"model profile，Runtime 会把它解析成真实 model id 传进来。\n"
            f"  端点级兜底（可选）：在 config/config.yaml 的 "
            f"`providers.{self.name}.default_model` 里填一个 profile 名。\n"
            f"  查看当前解析结果：./dev.sh config"
        )

    # ------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------
    def build_payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        **extra: Any,
    ) -> Dict[str, Any]:
        """
        把统一格式的 messages 变成厂商要的 JSON。

        单独抽成方法，是为了能被测试直接调用 ——
        不用真的发请求就能验证序列化对不对。

        🔒 图片能力由 Provider 自己声明；不支持图片的 Provider
           在这里**明确拒绝**，不把 image part 静默丢掉。
        """
        if (messages_have_images(messages)
                and not getattr(self, "supports_image_input", False)):
            raise LLMRequestError(
                f"provider {self.name!r} 不支持图片输入")
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            self.max_tokens_field: max_tokens,
            # 明确不开流式：流式会让 usage 出现在最后一个 chunk 里，
            # 解析复杂得多，而本项目目前不需要。
            "stream": False,
        }

        # 配置里的额外字段（各家小差异的兜底）
        payload.update(self.extra_body)
        # 调用时传进来的额外字段优先级最高
        payload.update(extra)

        return payload

    def build_headers(self) -> Dict[str, str]:
        """构造请求头。Key 只在这里出现，不会进日志。"""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    # ------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------
    @staticmethod
    def _read_usage(
        raw_usage: Any,
    ) -> tuple[Optional[int], Optional[int], Optional[int], Dict[str, Any]]:
        """
        从 usage 字段里读 token 数。

        返回值：(输入, 输出, 总计, 备注)

        【核心规则】API 没给的字段就返回 None，绝不瞎猜。
            因为 token 在这个项目里要换钱，
            把猜测当事实会让账永远对不上。

        唯一允许的「推导」：如果 API 给了输入和输出但没给总计，
        那总计 = 输入 + 输出。这是精确算术，不是估算。
        """
        notes: Dict[str, Any] = {}

        if not isinstance(raw_usage, Mapping):
            notes["usage_missing"] = True
            return None, None, None, notes

        def _as_int(value: Any) -> Optional[int]:
            if value is None or isinstance(value, bool):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        input_tokens = _as_int(raw_usage.get("prompt_tokens"))
        output_tokens = _as_int(raw_usage.get("completion_tokens"))
        total_tokens = _as_int(raw_usage.get("total_tokens"))

        if (
            total_tokens is None
            and input_tokens is not None
            and output_tokens is not None
        ):
            total_tokens = input_tokens + output_tokens
            notes["total_derived"] = "total_tokens 由 输入+输出 精确相加得出"

        if input_tokens is None and output_tokens is None and total_tokens is None:
            notes["usage_missing"] = True

        return input_tokens, output_tokens, total_tokens, notes

    #: 🔒 Cognitive Budget 的结构边界。
    #:    Agent **不能**要求低于 MIN（那几乎必然截断）或高于 MAX
    #:    （那会绕过部署侧的成本与超时控制）。Runtime 裁剪，不协商。
    MIN_COGNITIVE_BUDGET = 256
    MAX_COGNITIVE_BUDGET = 32768

    @staticmethod
    def _as_budget_int(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def clip_cognitive_budget(
        cls, requested: Any, fallback: Optional[int],
    ) -> tuple:
        """
        把 Agent 请求的认知预算裁剪成一个合法的 `max_tokens`。

        返回 `(effective_max_tokens, clipped_reason_or_None)`。

        🔒 裁剪理由是**结构**的，不是「你该省着点用」：
            · 低于 MIN → 会几乎必然截断 → 抬到 MIN
            · 高于 MAX / hard limit → 绕过部署侧控制 → 压到 MAX
        """
        want = cls._as_budget_int(requested)
        base = cls._as_budget_int(fallback)

        if want is None:
            # 🔒 没有 Agent 请求 → 原样使用调用方的 `max_tokens`。
            #    这里**不**套 MIN：直接调用 Provider 的小请求（例如
            #    `max_tokens=16` 的连通性测试）必须保持 16。
            return base, None
        if want < cls.MIN_COGNITIVE_BUDGET:
            return cls.MIN_COGNITIVE_BUDGET, "below_min"

        # 🔴 顺序很重要：先把请求压到 Provider 的结构上限，
        #    再和调用方的硬上限比较。否则 requested=99999 + cap=16384
        #    会返回 32768 —— **越过调用方上限**。
        clipped = None
        if want > cls.MAX_COGNITIVE_BUDGET:
            want = cls.MAX_COGNITIVE_BUDGET
            clipped = "above_max"
        if base is not None:
            # 调用方上限本身也不能越过 Provider 的 MAX。
            base = min(base, cls.MAX_COGNITIVE_BUDGET)
            if want > base:
                return base, "above_caller_limit"
        return want, clipped

    def parse_response(
        self,
        result: HTTPResult,
        model: str,
        latency_ms: float,
    ) -> LLMResponse:
        """
        把厂商的返回 JSON 转成统一的 LLMResponse。

        这是「厂商适配」第二处，也是最重要的一处。
        上游（Brain / Agent）永远不会看到厂商的 JSON 结构。
        """
        data = result.json()

        if not isinstance(data, Mapping):
            raise InvalidResponseError(
                f"返回体顶层必须是对象，实际是 {type(data).__name__}"
            )

        # --- 有些网关会把错误塞在 HTTP 200 里 ---
        if "error" in data and not data.get("choices"):
            raise LLMRequestError(self._describe_api_error(data.get("error")))

        # --- choices ---
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise InvalidResponseError(
                "返回体里没有 choices，或 choices 是空的。\n"
                f"实际返回的顶层字段：{sorted(data.keys())}"
            )

        first_choice = choices[0]
        if not isinstance(first_choice, Mapping):
            raise InvalidResponseError("choices[0] 不是对象")

        message = first_choice.get("message")
        if not isinstance(message, Mapping):
            raise InvalidResponseError("choices[0].message 缺失或不是对象")

        content = message.get("content")
        if content is None:
            # 🔒 字段**缺失 / 为 null** 与「空字符串」是两件不同的事：
            #
            #      content=""    → 结构合法，但**没有产出** → EMPTY_CONTENT
            #      content 缺失  → 结构不对 → 明确失败（不猜、不补）
            #
            #    §六 Case C / Case D 允许这里是一个「等价的明确失败状态」。
            #    这样处理是刻意的：如果把它降级成一个普通的空产出，
            #    一个坏掉的网关就会伪装成「模型今天没说话」。
            #
            #    ⚠️ 特别注意：这里**不看** `reasoning_content`。
            #       有推理字段**不能**让一个缺失的 content 变成合法。
            raise InvalidResponseError(
                "choices[0].message.content 缺失（或为 null）。\n"
                f"message 里实际有的字段：{sorted(message.keys())}\n"
                "注意：`reasoning_content` **不能**代替 content —— "
                "推理与最终答复语义不同。"
            )

        # --- 推理内容（诊断用，**永远不是** content）---
        #
        # 🔒 §五：允许用于 Provider 诊断 / telemetry / 实验记录，
        #    禁止 `if not content: content = reasoning_content` 这类修法。
        #    所以它走**独立字段**，绝不与 content 拼接或回填。
        reasoning = _read_reasoning(message)

        # --- usage ---
        input_tokens, output_tokens, total_tokens, notes = self._read_usage(
            data.get("usage")
        )
        token_source = (
            TOKEN_SOURCE_API if total_tokens is not None else TOKEN_SOURCE_UNKNOWN
        )

        if notes.get("usage_missing"):
            notes["warning"] = (
                "API 没有返回 usage，因此 token 用量标记为 unknown。"
                "这里【没有】用估算值顶替 —— 请以 None 为准。"
            )

        response = LLMResponse(
            content=str(content),
            provider=self.name,
            # 响应模型 = API 回显的那个（事实）
            model=str(data.get("model") or model),
            # 🔴 请求模型 = 我们实际发出去的那个。二者分开记，绝不互相覆盖。
            requested_model=str(model),
            requested_cognitive_budget=getattr(
                self, "_last_requested_budget", None),
            actual_max_tokens=self._last_max_tokens,
            budget_clipped=self._last_budget_clipped,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            token_source=token_source,
            latency_ms=latency_ms,
            raw_response=dict(data),
            finish_reason=(
                str(first_choice.get("finish_reason"))
                if first_choice.get("finish_reason") is not None
                else None
            ),
            reasoning=reasoning,
            simulated=False,
            metadata={
                "endpoint": self.endpoint,
                "usage_notes": notes,
            },
        )

        # ============================================================
        # 🔒 §七：「空 content」不许悄悄成功
        # ============================================================
        #
        #   实测（commit 6322092）：`novel_world_builder` 连续 4 轮
        #   content 为空、每轮仍烧掉约 4300 output token，
        #   而日志里它看起来只是「这个 Agent 没动手」。
        #
        #   EMPTY_CONTENT **不是**异常 —— 调用确实成功了、钱确实花了。
        #   所以这里不抛错，但必须发出**可见的告警**，
        #   否则同一个 Bug 会第三次悄悄溜过去。
        #
        #   ⚠️ 注意：**不动成本记账**。token 已经在上面的 usage 里，
        #      记账是 recorder / economy 的事，Provider 无权冲销。
        if not response.has_valid_content:
            logger.warning(
                "LLM 空产出（EMPTY_CONTENT）：provider=%s model=%s "
                "finish_reason=%s output_tokens=%s reasoning_chars=%d。"
                "这次调用**消耗了真实 token，但没有可用的生产内容** ——"
                "不要把它记成「Agent 没有生产」或「Agent 消极怠工」。",
                self.name, response.model, response.finish_reason,
                response.output_tokens, len(reasoning or ""),
            )
            response.metadata["empty_content"] = {
                "reasoning_chars": len(reasoning or ""),
                "finish_reason": response.finish_reason,
                "output_tokens": response.output_tokens,
                "hint": (
                    "推理模型把输出预算用在 reasoning_content 上时，"
                    "content 会是空字符串。"
                ),
            }

        return response

    # ------------------------------------------------------------
    # 错误翻译
    # ------------------------------------------------------------
    @staticmethod
    def _describe_api_error(error: Any) -> str:
        """把返回体里的 error 字段拼成一句人话。"""
        if isinstance(error, Mapping):
            message = error.get("message") or error.get("msg") or str(dict(error))
            code = error.get("code") or error.get("type")
            return f"API 返回错误：{message}" + (f"（code={code}）" if code else "")
        return f"API 返回错误：{error}"

    def _extra_status_hint(self, status: int) -> str:
        """
        子类可以覆盖这个方法，给某个状态码补充「人话解释」。

        为什么需要这个钩子？
            同一个状态码在不同厂商那里含义不完全一样，
            而且对用户来说「下一步该做什么」也不一样。

            比如 404：
                DeepSeek  → 请求路径不对（多半是我们自己的问题）
                豆包      → 推理接入点不存在（多半是用户 model 填错了）

            后者要给出「去哪儿找正确的接入点 ID」，前者不用。
        """
        return ""

    def _scrub(self, text: str) -> str:
        """
        把文本里的 API Key 抹掉。

        【为什么必须有这个】

            错误信息会被写进日志、写进数据库、甚至显示给用户。
            如果某一天某个环节不小心把请求头或 key 拼进了消息里，
            没有这一层，key 就会永久留在日志和 llm_calls 表里。

            这是防御性的 —— 正常情况下 key 根本不会出现在错误文本里。
            但泄露一次就够麻烦了，宁可多这一道。
        """
        if not text:
            return text

        scrubbed = text

        if self._api_key:
            scrubbed = scrubbed.replace(self._api_key, "***REDACTED***")
            scrubbed = scrubbed.replace(
                f"Bearer {self._api_key}", "Bearer ***REDACTED***"
            )

        return scrubbed

    def _raise_for_status(self, result: HTTPResult) -> None:
        """把 HTTP 错误码翻译成项目统一的异常体系。"""
        status = result.status
        snippet = self._scrub((result.body or "")[:300].replace("\n", " "))
        extra = self._extra_status_hint(status)
        label = self.PROVIDER_LABEL
        tail = f"\n{extra}" if extra else ""

        if status in (401, 403):
            raise AuthenticationError(
                f"{label} 鉴权失败（HTTP {status}）。\n"
                f"通常是这几种原因：\n"
                f"  · {self.api_key_env} 的值不对或已失效\n"
                f"  · Key 前后有空格或换行\n"
                f"  · Key 被撤销了\n"
                f"重试没有意义，请检查环境变量。{tail}\n"
                f"返回体：{snippet}"
            )

        if status == 429:
            raise RateLimitError(
                f"{label} 限流或配额用尽（HTTP 429）。\n"
                f"稍等一会儿再试通常有效。{tail}\n"
                f"返回体：{snippet}"
            )

        if status == 402:
            raise LLMHTTPError(
                f"{label} 账户余额不足（HTTP 402）。\n"
                f"请去对应平台充值。{tail}\n"
                f"返回体：{snippet}",
                status_code=status,
            )

        if status == 400:
            raise LLMHTTPError(
                f"请求被 {label} 拒绝（HTTP 400）。\n"
                f"常见原因：上下文超长、参数非法、模型名不对。{tail}\n"
                f"返回体：{snippet}",
                status_code=status,
            )

        if status == 404:
            raise LLMHTTPError(
                f"{label} 说找不到资源（HTTP 404）。\n"
                f"常见原因：模型名/接入点 ID 写错、base_url 不对。{tail}\n"
                f"返回体：{snippet}",
                status_code=status,
            )

        if 500 <= status < 600:
            raise LLMHTTPError(
                f"{label} 服务端错误（HTTP {status}）。\n"
                f"这是对方的问题，稍后重试通常有效。{tail}\n"
                f"返回体：{snippet}",
                status_code=status,
            )

        raise LLMHTTPError(
            f"{label} 返回了意外的状态码（HTTP {status}）。{tail}\n"
            f"返回体：{snippet}",
            status_code=status,
        )

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
        调一次 API，返回统一的 LLMResponse。

        签名和 LLMProvider 基类完全一致 ——
        所以 Brain 和 Agent 完全不知道这次调用是哪一家。
        """
        if not self._api_key:
            raise AuthenticationError(self.unavailable_reason() or "缺少 API Key")

        resolved_model = self.resolve_model(model)
        if not resolved_model:
            raise LLMRequestError(self._missing_model_message())

        normalized = normalize_messages(messages, system=system)

        # ============================================================
        # 🔴 Cognitive Budget V1：Agent 请求的认知预算 → max_tokens
        # ============================================================
        #
        #   Agent 可以在一次认知运行里提出 `requested_cognitive_budget`。
        #   **Runtime 负责裁剪**（最小值 / 最大值 / Provider hard limit）——
        #   这是**结构规则**，不是 Agent 策略。
        #
        #   🔒 明令禁止：AIC balance → max_tokens 的自动换算。
        #      `max_tokens` 是**上限**，不是 Agent 必须消耗的 token 数。
        requested_budget = kwargs.pop("requested_cognitive_budget", None)
        effective_tokens, clipped = self.clip_cognitive_budget(
            requested_budget, max_tokens)
        self._last_requested_budget = self._as_budget_int(requested_budget)
        self._last_max_tokens = effective_tokens
        self._last_budget_clipped = clipped
        if clipped:
            # 🔒 这是**结构事实**，不是错误：Agent 下一轮会看到
            #    requested / actual / clipped_reason，自行决定是否调整。
            logger.info(
                "认知预算被裁剪：provider=%s requested=%s actual=%s "
                "reason=%s",
                self.name, self._last_requested_budget,
                self._last_max_tokens, clipped)

        # 🔒 思考模式是 **Provider 能力**，不是 Agent 层的厂商判断。
        #    不支持的 provider 在这里安静地丢掉这些字段，而不是
        #    把未知字段发给厂商换一个 400。
        if not getattr(self, "supports_thinking", False):
            kwargs.pop("thinking", None)
        if not getattr(self, "supports_reasoning_effort", False):
            kwargs.pop("reasoning_effort", None)

        payload = self.build_payload(
            messages=normalized,
            model=resolved_model,
            temperature=temperature,
            max_tokens=effective_tokens,
            **kwargs,
        )
        headers = self.build_headers()

        started = time.perf_counter()
        try:
            result = self._http_post(self.endpoint, payload, headers, self.timeout)
        except LLMRequestError as exc:
            # ⚠️ **实测抓到的泄露点**：
            #    以前这里直接 `raise` —— 如果 HTTP 层把 key 拼进了消息
            #    （连接失败的 URL / 头部回显等），这个 key 会**原样**
            #    进日志、进 llm_calls 表、进 CLI。
            #    任务书 §12.4 明确要求「错误信息里不能出现 key」。
            self.error_count += 1
            scrubbed = self._scrub(str(exc))
            if scrubbed != str(exc):
                raise LLMRequestError(scrubbed) from exc
            raise
        except Exception as exc:  # noqa: BLE001
            # 任何没预料到的异常也统一进异常体系，
            # 免得 Agent 层看到 urllib / socket 这种东西。
            self.error_count += 1
            raise LLMRequestError(
                f"调用 {self.PROVIDER_LABEL} 时发生未预期的错误："
                f"{type(exc).__name__}: {self._scrub(str(exc))}"
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0

        if not result.ok:
            self.error_count += 1
            self._raise_for_status(result)

        try:
            response = self.parse_response(result, resolved_model, latency_ms)
        except LLMRequestError:
            self.error_count += 1
            raise
        except Exception as exc:  # noqa: BLE001
            self.error_count += 1
            raise InvalidResponseError(
                f"解析 {self.PROVIDER_LABEL} 返回时出错：{type(exc).__name__}: "
                f"{self._scrub(str(exc))}"
            ) from exc

        self.call_count += 1
        return response

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """
        自述。

        【安全】这里【只报告 key 是否存在】，绝不报告 key 的内容。
        """
        info = super().describe()
        info.update(
            {
                "base_url": self.base_url,
                "endpoint": self.endpoint,
                "api_key_env": self.api_key_env,
                "api_key_configured": bool(self._api_key),
                "timeout_seconds": self.timeout,
                "max_tokens_field": self.max_tokens_field,
                "calls": self.call_count,
                "errors": self.error_count,
                "real_http": self._http_post is default_http_post,
            }
        )
        return info
