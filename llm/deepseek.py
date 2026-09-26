"""
DeepSeek Provider —— 第一个真实的大模型接入
============================================================

【这个文件的定位】

    它是 LLMProvider 契约的第二个实现（第一个是 MockProvider）。

    它存在的意义不只是「能调 DeepSeek」，更是为了验证一件事：

        上一阶段设计的那套抽象，在真实 HTTP API 面前到底成不成立。

    如果为了接 DeepSeek 必须去改 Brain、Router 或 Agent，
    那就说明抽象设计失败了。本文件的目标就是证明不需要。

【所有 DeepSeek 特有的东西都关在这个文件里】

    · endpoint 路径
    · Authorization 头怎么拼
    · 请求 JSON 长什么样
    · 响应 JSON 里 content 在哪、usage 在哪
    · 各种 HTTP 错误码怎么理解
    · 超时怎么算

    出不了这个文件。Brain / Router / Agent 全都看不到。

【为什么用标准库 urllib 而不是 requests】

    这个项目的原则是「能用标准库就不引第三方库」。
    DeepSeek 用的是 OpenAI 兼容的 /chat/completions 接口，
    一次 HTTPS POST 加 JSON 解析，urllib 完全够用。

    代价是代码稍微啰嗦一点，换来的是：
    整个项目运行时的第三方依赖依然是 0。

【可测试性】

    HTTP 请求被抽成了一个可替换的函数（http_post 参数）。
    测试时注入一个假的，就能把「构造请求」和「解析响应」
    这两件事单独测透，不需要网络、不需要 API Key。
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
    normalize_messages,
)


# ============================================================
# HTTP 层（故意和 Provider 逻辑分开）
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
# DeepSeek Provider
# ============================================================
class DeepSeekProvider(LLMProvider):
    """
    DeepSeek 的真实接入。

    用法：
        provider = DeepSeekProvider(config["llm"]["providers"]["deepseek"])
        response = provider.generate(messages="你好")

    API Key 只从环境变量读，永远不从代码或配置文件读。
    """

    name = "deepseek"
    requires_api_key = True

    # 这些是【兜底默认值】，配置里写了就用配置的。
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-chat"
    DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
    DEFAULT_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
    DEFAULT_MODEL_ENV = "DEEPSEEK_MODEL"
    DEFAULT_CHAT_PATH = "/chat/completions"
    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        provider_config: Optional[Mapping[str, Any]] = None,
        default_model: str = "",
        http_post: Optional[HTTPPost] = None,
    ):
        """
        参数：
            provider_config: config.yaml 里 llm.providers.deepseek 那一段
            default_model:   兜底模型名
            http_post:       注入自定义 HTTP 实现（测试用）
        """
        super().__init__(provider_config=provider_config, default_model=default_model)

        cfg = self.config

        # --- Key 的来源：只认环境变量 ---
        # 存的是「环境变量的名字」，不是 key 本身。
        self.api_key_env = str(
            cfg.get("api_key_env") or self.DEFAULT_API_KEY_ENV
        )
        self._api_key = os.environ.get(self.api_key_env, "").strip()

        # --- base_url：环境变量 > 配置 > 默认 ---
        self.base_url_env = str(
            cfg.get("base_url_env") or self.DEFAULT_BASE_URL_ENV
        )
        self.base_url = (
            os.environ.get(self.base_url_env, "").strip()
            or str(cfg.get("base_url") or self.DEFAULT_BASE_URL)
        ).rstrip("/")

        # --- model：环境变量 > 配置 > 构造参数 > 默认 ---
        self.model_env = str(cfg.get("model_env") or self.DEFAULT_MODEL_ENV)
        env_model = os.environ.get(self.model_env, "").strip()
        if env_model:
            self.default_model = env_model
        elif not self.default_model:
            self.default_model = self.DEFAULT_MODEL

        # --- 其它 ---
        self.chat_path = str(cfg.get("chat_path") or self.DEFAULT_CHAT_PATH)
        self.timeout = float(cfg.get("timeout_seconds") or self.DEFAULT_TIMEOUT)

        # 可替换的 HTTP 层
        self._http_post: HTTPPost = http_post or default_http_post

        # 统计（调试用）
        self.call_count = 0
        self.error_count = 0

    # ------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------
    def is_available(self) -> bool:
        return bool(self._api_key) and bool(self.endpoint) and bool(self.default_model)

    def unavailable_reason(self) -> Optional[str]:
        """
        说明为什么不能用。

        【安全提醒】这里【绝对不能】把 key 的值打出来。
        只说「哪个环境变量没设置」就足够了。
        """
        if not self._api_key:
            return (
                f"环境变量 {self.api_key_env} 未设置或为空。\n"
                f"请复制 .env.example 为 .env，然后填入你的 Key：\n"
                f"    cp .env.example .env\n"
                f"    # 编辑 .env，填入 {self.api_key_env}=<你的Key>\n"
                f"\n"
                f"（提示里故意不写 Key 的示例前缀 —— "
                f"否则密钥扫描工具会把提示文案误报成泄露。）"
            )
        if not self.default_model:
            return "没有配置模型名（llm.providers.deepseek.model）"
        if not self.endpoint:
            return "base_url 为空"
        return None

    @property
    def endpoint(self) -> str:
        """完整的请求地址。"""
        if not self.base_url:
            return ""
        return f"{self.base_url}{self.chat_path}"

    # ------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------
    def build_payload(
        self,
        messages: Sequence[Mapping[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        **extra: Any,
    ) -> Dict[str, Any]:
        """
        把统一格式的 messages 变成 DeepSeek 要的 JSON。

        这是「厂商适配」的第一处。DeepSeek 用的是 OpenAI 兼容格式，
        所以这里看起来简单 —— 但换一家接口不一样的（比如豆包用
        endpoint id），差异就会全部集中在这个方法里。

        单独抽成方法是为了可以被测试直接调用，
        不用真的发请求就能验证序列化对不对。
        """
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            # 明确不开流式：Step 2.5 只需要一次性拿完整响应。
            # 流式会让 usage 出现在最后一个 chunk 里，解析复杂得多。
            "stream": False,
        }
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

        唯一允许的「推导」：如果 API 给了输入和输出，但没给总计，
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

        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            # 精确相加，不是估算
            total_tokens = input_tokens + output_tokens
            notes["total_derived"] = "total_tokens 由 输入+输出 精确相加得出"

        if input_tokens is None and output_tokens is None and total_tokens is None:
            notes["usage_missing"] = True

        return input_tokens, output_tokens, total_tokens, notes

    def parse_response(
        self,
        result: HTTPResult,
        model: str,
        latency_ms: float,
    ) -> LLMResponse:
        """
        把 DeepSeek 的返回 JSON 转成统一的 LLMResponse。

        这是「厂商适配」的第二处，也是最重要的一处。
        上游（Brain / Agent）永远不会看到 DeepSeek 的 JSON 结构。

        单独抽成方法，测试可以直接喂各种 JSON 进来验证解析。
        """
        data = result.json()

        if not isinstance(data, Mapping):
            raise InvalidResponseError(
                f"返回体顶层必须是对象，实际是 {type(data).__name__}"
            )

        # --- 有些网关会把错误塞在 HTTP 200 里 ---
        if "error" in data and not data.get("choices"):
            raise LLMRequestError(
                self._describe_api_error(data.get("error"))
            )

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
            # 注意：content 为空字符串是合法的（比如模型什么都没说），
            #       但字段本身缺失说明结构不对。
            raise InvalidResponseError(
                "choices[0].message.content 缺失。\n"
                f"message 里实际有的字段：{sorted(message.keys())}"
            )

        # --- usage ---
        input_tokens, output_tokens, total_tokens, notes = self._read_usage(
            data.get("usage")
        )
        token_source = (
            TOKEN_SOURCE_API if total_tokens is not None else TOKEN_SOURCE_UNKNOWN
        )

        if any(notes.values()):
            if notes.get("usage_missing"):
                notes["warning"] = (
                    "API 没有返回 usage，因此 token 用量标记为 unknown。"
                    "这里【没有】用估算值顶替 —— 请以 None 为准。"
                )

        return LLMResponse(
            content=str(content),
            provider=self.name,
            model=str(data.get("model") or model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            token_source=token_source,
            latency_ms=latency_ms,
            # 保留原始 JSON。各厂商结构不同，所以这个字段故意不做类型约束。
            raw_response=dict(data),
            finish_reason=(
                str(first_choice.get("finish_reason"))
                if first_choice.get("finish_reason") is not None
                else None
            ),
            simulated=False,
            metadata={
                "endpoint": self.endpoint,
                "usage_notes": notes,
            },
        )

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

    def _scrub(self, text: str) -> str:
        """
        把文本里的 API Key 抹掉。

        【为什么必须有这个】

            错误信息会被写进日志、写进数据库、甚至显示给用户。
            如果某一天某个环节不小心把请求头或 key 拼进了消息里，
            没有这一层，key 就会永久留在日志和 llm_calls 表里。

            这是「防御性」的 —— 正常情况下 key 根本不会出现在
            错误文本里。但泄露一次就够麻烦了，所以宁可多这一道。
        """
        if not text:
            return text

        scrubbed = text

        if self._api_key:
            scrubbed = scrubbed.replace(self._api_key, "***REDACTED***")
            # Bearer 形式也一起抹掉
            scrubbed = scrubbed.replace(
                f"Bearer {self._api_key}", "Bearer ***REDACTED***"
            )

        return scrubbed

    def _raise_for_status(self, result: HTTPResult) -> None:
        """把 HTTP 错误码翻译成项目统一的异常体系。"""
        status = result.status
        snippet = self._scrub((result.body or "")[:300].replace("\n", " "))

        if status in (401, 403):
            raise AuthenticationError(
                f"DeepSeek 鉴权失败（HTTP {status}）。\n"
                f"通常是这几种原因：\n"
                f"  · {self.api_key_env} 的值不对或已失效\n"
                f"  · Key 前后有空格或换行\n"
                f"  · Key 被撤销了\n"
                f"重试没有意义，请检查环境变量。\n"
                f"返回体：{snippet}"
            )

        if status == 429:
            raise RateLimitError(
                f"DeepSeek 限流了（HTTP 429）。\n"
                f"稍等一会儿再试通常就好。\n"
                f"返回体：{snippet}"
            )

        if status == 402:
            raise LLMHTTPError(
                f"DeepSeek 账户余额不足（HTTP 402）。\n"
                f"请去 DeepSeek 平台充值。\n"
                f"返回体：{snippet}",
                status_code=status,
            )

        if status == 400:
            raise LLMHTTPError(
                f"请求被 DeepSeek 拒绝（HTTP 400）。\n"
                f"常见原因：上下文超长、参数非法、model 名字不对。\n"
                f"返回体：{snippet}",
                status_code=status,
            )

        if 500 <= status < 600:
            raise LLMHTTPError(
                f"DeepSeek 服务端错误（HTTP {status}）。\n"
                f"这是对方的问题，稍后重试通常有效。\n"
                f"返回体：{snippet}",
                status_code=status,
            )

        raise LLMHTTPError(
            f"DeepSeek 返回了意外的状态码（HTTP {status}）。\n"
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
        调一次 DeepSeek，返回统一的 LLMResponse。

        签名和 LLMProvider 基类完全一致 ——
        所以 Brain 和 Agent 完全不知道这次调用换成了真实 HTTP。
        """
        if not self._api_key:
            raise AuthenticationError(self.unavailable_reason() or "缺少 API Key")

        resolved_model = self.resolve_model(model)
        if not resolved_model:
            raise LLMRequestError(
                "没有指定模型名。请在 config/config.yaml 里设置 "
                "llm.providers.deepseek.model"
            )

        normalized = normalize_messages(messages, system=system)
        payload = self.build_payload(
            messages=normalized,
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        headers = self.build_headers()

        started = time.perf_counter()
        try:
            result = self._http_post(
                self.endpoint, payload, headers, self.timeout
            )
        except LLMRequestError:
            self.error_count += 1
            raise
        except Exception as exc:  # noqa: BLE001
            # 任何没预料到的异常也统一进异常体系，
            # 免得 Agent 层看到 urllib / socket 这种东西。
            self.error_count += 1
            raise LLMRequestError(
                f"调用 DeepSeek 时发生未预期的错误："
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
                f"解析 DeepSeek 返回时出错：{type(exc).__name__}: "
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
                "calls": self.call_count,
                "errors": self.error_count,
                "real_http": self._http_post is default_http_post,
            }
        )
        return info
