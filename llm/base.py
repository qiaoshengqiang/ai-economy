"""
LLM Provider 基础接口
============================================================

【这个文件存在的唯一理由】

    让 Agent 永远不需要知道自己在用谁家的模型。

DeepSeek 返回的 JSON、豆包返回的 JSON、Grok 返回的 JSON
三者结构完全不同。如果没有这一层，那些差异就会一路冒泡到 Agent 里，
最后变成满地的 `if provider == "deepseek": ...`。

所以规则是：

    ┌─────────────────────────────────────────────┐
    │  厂商各自的 JSON                             │
    │    ↓  在各自 Provider 内部转换               │
    │  LLMResponse  ← 统一的、稳定的结构           │
    │    ↑                                        │
    │  上一层（Brain / Agent）只认识这个            │
    └─────────────────────────────────────────────┘

【这个文件里没有任何一家厂商的名字】
    它只定义「所有 Provider 都必须满足的契约」。
    具体的 DeepSeek / 豆包 / Grok 实现，各自去 llm/ 下新建文件。
    加一个新厂商，不需要动这个文件，也不需要动 Agent。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from core.identity import utc_now_iso


# ============================================================
# 异常体系
# ============================================================
class LLMError(Exception):
    """LLM 层的错误基类。"""


class ProviderNotFoundError(LLMError):
    """这个 provider 名字根本没注册过（多半是拼错了）。"""


class ProviderNotImplementedError(LLMError):
    """
    这个 provider 我们已经知道，但还没实现。

    为什么要把「没实现」和「不存在」分开？
        因为这两种情况的处理方式完全不同：
            ProviderNotFoundError       → 你拼错了，去改配置
            ProviderNotImplementedError → 功能还没做，等下一阶段

        如果混成一个错误，用户看到「找不到 deepseek」会以为是
        自己拼错了，然后花半小时找一个根本不存在的拼写问题。
    """


class ProviderDisabledError(LLMError):
    """这个 provider 在配置里被 enabled: false 关掉了。"""


class LLMRequestError(LLMError):
    """调用模型失败（网络问题、鉴权失败、余额不足等）。"""


# ------------------------------------------------------------
# 调用失败的细分类型
# ------------------------------------------------------------
# 【为什么要把失败分这么细】
#
#     因为不同的失败，正确的反应完全不同：
#
#         AuthenticationError   key 错了 / 过期了
#                               → 重试没有意义，必须改配置
#         RateLimitError        被限流了
#                               → 等一会儿重试是有效的（未来的 fallback 会用）
#         LLMTimeoutError       超时
#                               → 可以重试，或换更快的模型
#         LLMHTTPError          服务端 5xx
#                               → 可以重试
#         InvalidResponseError  返回体解析不了
#                               → 重试通常也没用，是接口变了或中间有代理
#
#     如果全塞进一个 LLMRequestError，上层就只能「一律重试」，
#     而对着 401 无限重试是最浪费钱的失败模式。
#
# 这些异常全部继承 LLMRequestError，所以已有的
# `except LLMRequestError` 代码不受影响，向后兼容。
# ------------------------------------------------------------
class AuthenticationError(LLMRequestError):
    """鉴权失败（HTTP 401 / 403）。重试无意义，要改 Key。"""


class RateLimitError(LLMRequestError):
    """被限流（HTTP 429）。等一会儿重试有效。"""


class LLMTimeoutError(LLMRequestError):
    """请求超时。可以重试。"""


class LLMHTTPError(LLMRequestError):
    """其它 HTTP 错误码（4xx / 5xx）。"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class InvalidResponseError(LLMRequestError):
    """
    返回体能拿到，但解析不了 —— JSON 坏了、字段缺失、结构变了。

    这一类通常意味着接口变了，或者中间有代理改写了响应。
    """


# ============================================================
# 消息与角色
# ============================================================
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

VALID_ROLES = {ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT}

# 一条消息： {"role": "user", "content": "你好"}
Message = Mapping[str, Any]


def normalize_messages(
    messages: Union[str, Sequence[Message], None] = None,
    system: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    把各种调用姿势统一成一种内部格式：[{"role": ..., "content": ...}, ...]

    支持这些写法（都不想让调用方纠结）：

        # 1. 最标准
        normalize_messages([{"role": "user", "content": "你好"}])

        # 2. 直接给一个字符串，当成用户消息
        normalize_messages("你好")

        # 3. system 单独传
        normalize_messages("你好", system="你是 Agent A")

        # 4. system 混在 messages 里
        normalize_messages([
            {"role": "system", "content": "你是 Agent A"},
            {"role": "user", "content": "你好"},
        ])

    为什么要把 system 单独支持？
        因为几乎所有厂商的 API 都把 system 单独拿出来，
        或者用特殊字段。让调用方能单独传，转换时就少一层判断。
    """
    result: List[Dict[str, str]] = []

    # --- system 先放 ---
    if system:
        result.append({"role": ROLE_SYSTEM, "content": str(system)})

    if messages is None:
        return result

    # --- 一个裸字符串 ---
    if isinstance(messages, str):
        result.append({"role": ROLE_USER, "content": messages})
        return result

    if not isinstance(messages, (list, tuple)):
        raise LLMRequestError(
            f"messages 必须是字符串或列表，收到：{type(messages).__name__}"
        )

    for index, raw in enumerate(messages):
        if isinstance(raw, str):
            result.append({"role": ROLE_USER, "content": raw})
            continue

        if not isinstance(raw, Mapping):
            raise LLMRequestError(
                f"第 {index} 条消息必须是 dict 或 str，"
                f"收到：{type(raw).__name__}"
            )

        role = str(raw.get("role", ROLE_USER)).lower()
        if role not in VALID_ROLES:
            raise LLMRequestError(
                f"第 {index} 条消息的 role 非法：{role!r}。"
                f"合法取值：{sorted(VALID_ROLES)}"
            )

        content = _normalize_content(
            raw.get("content", ""), role=role, index=index)
        result.append({"role": role, "content": content})

    return result


def _normalize_content(content: Any, *, role: str, index: int) -> Any:
    """
    把一条消息的 `content` 归一化成 **str 或 content parts**。

    🔒 多模态边界（DeepSeek 官方限制）：
       - 图片只能出现在 `user` 消息里；
       - `image_url` 只接受 http(s) 或 `data:image/...`；
       - `detail` 只接受 low / high / original / auto。
       这些在**进入 Provider 之前**就拒绝，避免把坏请求发出去。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        # 历史行为：标量内容转成字符串（不把 int / None 当图片）。
        return str(content)

    parts: List[Dict[str, Any]] = []
    for part_index, raw_part in enumerate(content):
        if not isinstance(raw_part, Mapping):
            raise LLMRequestError(
                f"第 {index} 条消息的第 {part_index} 个 content part "
                f"必须是 dict，收到：{type(raw_part).__name__}")
        part_type = str(raw_part.get("type") or "").strip().lower()
        if part_type == "text":
            text = raw_part.get("text")
            if not isinstance(text, str):
                raise LLMRequestError(
                    f"第 {index} 条消息的 text part 缺少字符串 text")
            parts.append({"type": "text", "text": text})
            continue

        if part_type == "image_url":
            if role != ROLE_USER:
                raise LLMRequestError(
                    "图片只能出现在 user 消息里"
                    f"（第 {index} 条是 {role!r}）")
            image = raw_part.get("image_url")
            if isinstance(image, str):
                image = {"url": image}
            if not isinstance(image, Mapping):
                raise LLMRequestError(
                    f"第 {index} 条消息的 image_url 必须是字符串或对象")
            url = image.get("url")
            if not isinstance(url, str) or not url:
                raise LLMRequestError(
                    f"第 {index} 条消息的 image_url 缺少 url")
            if not url.startswith(("http://", "https://", "data:image/")):
                raise LLMRequestError(
                    "image_url 只支持 http(s) 或 data:image/...")
            out: Dict[str, Any] = {"type": "image_url",
                                   "image_url": {"url": url}}
            detail = image.get("detail")
            if detail is not None:
                detail = str(detail).strip().lower()
                if detail not in ("low", "high", "original", "auto"):
                    raise LLMRequestError(
                        f"image_url.detail 非法：{detail!r}")
                out["image_url"]["detail"] = detail
            parts.append(out)
            continue

        if part_type == "file":
            if role != ROLE_USER:
                raise LLMRequestError(
                    "file part 只能出现在 user 消息里"
                    f"（第 {index} 条是 {role!r}）")
            file_id = raw_part.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise LLMRequestError(
                    f"第 {index} 条消息的 file part 缺少 file_id")
            parts.append({"type": "file", "file_id": file_id})
            continue

        raise LLMRequestError(
            f"第 {index} 条消息包含未知 content part：{part_type!r}")

    return parts


# ============================================================
# token 估算
# ============================================================
def is_cjk(char: str) -> bool:
    """判断一个字符是不是中日韩文字（中文、日文、韩文）。"""
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF      # 中日韩统一表意文字
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x3040 <= code <= 0x30FF   # 日文平假名 / 片假名
        or 0xAC00 <= code <= 0xD7AF   # 韩文
        or 0xF900 <= code <= 0xFAFF   # 兼容表意文字
    )


def estimate_tokens(text: str) -> int:
    """
    粗略估算一段文本有多少 token。

    ⚠️ 这不是真正的 tokenizer。
        真正的计数要用各家厂商的分词器（tiktoken 之类），
        那会引入额外依赖，而且每家的分词方式都不一样。

        这里只求「量级正确」，用于 MockProvider 模拟 token 用量，
        以及给真实 Provider 在拿不到 usage 字段时兜底。

    估算规则：
        中日韩文字：1 个字 ≈ 1 token
            （真实情况大约 1~1.5 个字 1 个 token，这里偏保守）
        其它字符：4 个字符 ≈ 1 token
            （英文的常见经验值）

    为什么中日韩要单独算？
        如果一律按「4 字符 = 1 token」，一段 100 字的中文会被算成
        25 token，实际大概 70~100 token —— 差 3~4 倍。
        而 token 在这个项目里是要换钱的（tokens_per_ai_coin），
        差 3 倍就意味着成本估算差 3 倍，不能接受。
    """
    if not text:
        return 0

    cjk_count = 0
    other_count = 0

    for char in text:
        if is_cjk(char):
            cjk_count += 1
        else:
            other_count += 1

    # 向上取整：宁可多算一点，也不要因为取整把成本算成 0
    return cjk_count + math.ceil(other_count / 4)


#: 单张图片的保守 token 估算上限。
#: DeepSeek 官方：图片会按尺寸换算 token，单图最多约 1024。
#: 这里用于 MockProvider / 预算估算；真实成本仍以 API usage 为准。
IMAGE_TOKEN_ESTIMATE = 1024


def estimate_messages_tokens(messages: Iterable[Message]) -> int:
    """
    估算一组 messages 的总 token 数。

    除了正文，每条消息还加了固定的开销（角色标签、分隔符等）。
    主流模型每条消息大约有 3~4 token 的结构开销，这里取 4。

    🔒 多模态消息：文本 part 按文本估算；图片/文件 part 按
       `IMAGE_TOKEN_ESTIMATE` 计。**不把 base64 当文本数** ——
       那会把一张图的 token 估算成几十万。
    """
    total = 0
    for message in messages:
        total += _estimate_content_tokens(message.get("content", ""))
        total += 4   # 每条消息的结构开销
    return total


def _estimate_content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, (list, tuple)):
        total = 0
        for part in content:
            if not isinstance(part, Mapping):
                continue
            part_type = str(part.get("type") or "")
            if part_type == "text":
                total += estimate_tokens(str(part.get("text") or ""))
            elif part_type in ("image_url", "file"):
                total += IMAGE_TOKEN_ESTIMATE
        return total
    return estimate_tokens(str(content))


def messages_have_images(messages: Iterable[Message]) -> bool:
    """消息里是否有 image / file content part。"""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, (list, tuple)):
            continue
        for part in content:
            if (isinstance(part, Mapping)
                    and str(part.get("type") or "") in ("image_url", "file")):
                return True
    return False


# ============================================================
# token 数据的来源
# ============================================================
# 【为什么必须区分这三种】
#
#     API      真实 usage，能直接拿去算钱、对账单
#     ESTIMATED 我们自己估的，只能用来做粗略预算
#     UNKNOWN   API 没给，我们也不知道 —— 不许猜
#
# 最危险的做法是「API 没给 usage 就自己估一个，然后当作真实值」。
# 那样两份数据混在一起以后，你永远无法回答
# 「上个月到底花了多少 token」。
TOKEN_SOURCE_API = "api"
TOKEN_SOURCE_ESTIMATED = "estimated"
TOKEN_SOURCE_UNKNOWN = "unknown"

VALID_TOKEN_SOURCES = {
    TOKEN_SOURCE_API,
    TOKEN_SOURCE_ESTIMATED,
    TOKEN_SOURCE_UNKNOWN,
}


# ============================================================
# Provider 输出契约（output contract）
# ============================================================
#
# 【为什么需要这两个状态】
#
#     真实实验（commit 6322092）里观察到 `deepseek-flash`（推理模型）返回：
#
#         {"content": "", "reasoning_content": "……很长的推理……"}
#
#     而 usage 里 completion_tokens 依然很大（推理 token 也是输出 token）。
#
#     当时的后果非常具体：
#
#         · API 调用「成功」            → success=True
#         · token 照常计费              → 真实成本进了 LLM call 记录
#         · 但 external content 是空的  → Agent 一个字都没拿到
#
#     于是实验报告里很容易把它读成
#     「这个 Agent 选择不动手 / 消极怠工」—— **那是错的**。
#     它首先是 **Provider 输出契约**问题。
#
# 🔒 三条不许破坏的语义
#
#     1. `EMPTY_CONTENT ≠ VALID_CONTENT`
#        空 content 不能因为「有 reasoning 字段」就升级成有效内容。
#
#     2. `reasoning_content` **不是** content
#        推理和最终答复语义不同。绝不允许
#            if not content: content = reasoning_content
#        这种修法（它会把模型的思考当成交付物写进 Workspace）。
#
#     3. `EMPTY_CONTENT` **不等于**「没有成本」
#        token 真的花了，账就要真的记（§八）。不许为了让实验数据好看而冲销。
OUTPUT_VALID_CONTENT = "VALID_CONTENT"
OUTPUT_EMPTY_CONTENT = "EMPTY_CONTENT"

# ============================================================
# 认知输出语义状态（Cognitive Output Status）
# ============================================================
#
# 🔴 **这是与 `output_status` 正交的第二个维度，不是它的替代品。**
#
#     output_status  : **语法契约** —— 解析器能不能拿到结构？
#                      （VALID_CONTENT / EMPTY_CONTENT）
#     cognitive_status: **语义完成度** —— 这一轮认知**跑完了**吗？
#                      （COMPLETE / TRUNCATED / INCOMPLETE / UNKNOWN）
#
#     `VALID_CONTENT + finish_reason=length`
#         = 语法上合法，**但认知没有跑完**。
#     两者绝不能互相推导 —— 有守卫禁止。
#
# 【为什么必须独立】
#     实测（Run 2）：11 turns 的 `finish_reason="length"` 被记成
#     `output_status=VALID_CONTENT`，于是看起来是「一次成功的认知」。
#     真实语义是：**模型输出撞到 token 上限被截断，本轮认知不完整。**
COGNITIVE_COMPLETE = "COMPLETE"
COGNITIVE_TRUNCATED = "TRUNCATED"
COGNITIVE_INCOMPLETE = "INCOMPLETE"
COGNITIVE_UNKNOWN = "UNKNOWN"

VALID_COGNITIVE_STATUSES = frozenset({
    COGNITIVE_COMPLETE, COGNITIVE_TRUNCATED,
    COGNITIVE_INCOMPLETE, COGNITIVE_UNKNOWN,
})

#: provider 的 `finish_reason` → 认知输出状态。
#:
#: 🔒 **只映射有确定语义的值**，不机械地把所有非-stop 都当成截断：
#:     · `stop`           — 模型自然结束          → COMPLETE
#:     · `length`         — 撞到 token 上限被截断 → TRUNCATED
#:     · `content_filter` — 内容被过滤，输出不完整 → TRUNCATED
#:     · `tool_calls` /
#:       `function_call`  — 模型转去调工具，正文没写完 → INCOMPLETE
#:     · 其它非空值        — 认不出来，如实说 INCOMPLETE
#:     · 缺失 / 空         — provider **没有上报** → UNKNOWN
#:                          （UNKNOWN **不**阻断 Action；TRUNCATED 才阻断。)
COGNITIVE_FINISH_REASON_MAP = {
    "stop": COGNITIVE_COMPLETE,
    "length": COGNITIVE_TRUNCATED,
    "content_filter": COGNITIVE_TRUNCATED,
    "tool_calls": COGNITIVE_INCOMPLETE,
    "function_call": COGNITIVE_INCOMPLETE,
}

VALID_OUTPUT_STATUSES = {
    OUTPUT_VALID_CONTENT,
    OUTPUT_EMPTY_CONTENT,
}

#: 第三个状态（PROVIDER_ERROR）**故意不在这里**。
#:
#: HTTP / 传输 / 鉴权 / 结构错误在 LLM 层是**异常**
#: （`LLMRequestError` / `InvalidResponseError` / …），
#: 根本走不到 LLMResponse。把它们塞成一个「状态」会让
#: 「失败了」和「成功了但没内容」看起来一样 —— 那正是本 Bug 的成因。


# ============================================================
# 统一返回结构
# ============================================================
@dataclass
class LLMResponse:
    """
    所有 Provider 都必须返回这个结构。

    这是整个 LLM 层对外唯一的数据格式。
    上层（Brain / Agent / Economy）只认识它，不认识任何厂商的原始 JSON。

    关于 token 字段（Step 2.5 修正）：

        真实 Provider 从 API 的 usage 字段取 → 填具体数字，token_source="api"
        MockProvider 自己估算              → 填估算值，  token_source="estimated"
        API 没返回 usage                    → 三个字段留 None，token_source="unknown"

        【关键】第三种情况绝对不许「补一个估计值凑上」。
        不知道就是不知道 —— 因为 token 在这个项目里要换钱，
        把猜测当事实会让账永远对不上。

        None 和 0 的区别很重要：
            0     = 确实用了 0 个 token（比如空输入）
            None  = 不知道用了多少
    """

    content: str
    provider: str

    #: 🔴 **响应模型** —— API 返回体里的 `model`（事实）。
    #:
    #: ⚠️ 历史上 `model` 一个字段同时承担「请求的模型」和「响应的模型」，
    #:    于是「请求 deepseek-chat、响应回显 deepseek-flash」这种情况
    #:    **无法被记录，也无法被追问**。现在拆开：
    #:
    #:        requested_model   Runtime 实际发出去的
    #:        response_model    API 回显的（= 本字段）
    #:
    #:    🔒 二者**绝不能互相覆盖** —— 不一致本身就是重要事实，
    #:       不能自动断定是哪边「错」。
    model: str

    #: Runtime 实际请求的模型名。None = 当时没记录（不是猜一个）。
    requested_model: Optional[str] = None

    #: Agent 请求的认知预算（映射到 max_tokens 的**上限**）。
    requested_cognitive_budget: Optional[int] = None
    #: Runtime 裁剪后**真正**使用的 max_tokens。
    actual_max_tokens: Optional[int] = None
    #: 如果请求值被裁剪，这里是**结构化原因**（None = 没有裁剪）：
    #:
    #:     below_min          低于 Provider 的结构下限
    #:     above_max          高于 Provider 的结构上限
    #:     above_caller_limit 高于调用方（Human / 系统）给出的硬上限
    #:
    #: 🔒 这是**事实**，不是错误：Agent 据此可以自行决定下一轮是否调整预算。
    budget_clipped: Optional[str] = None

    # 允许 None：None 表示「不知道」，0 表示「确实是 0」
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    #: 命中缓存的输入 token（provider 返回才填；None = 不知道）。
    #: OpenCode 在 `usage.opencode.tokens.cache_read`；
    #: OpenAI 兼容在 `usage.prompt_tokens_details.cached_tokens`。
    cache_read_tokens: Optional[int] = None
    #: 写入缓存的输入 token（Anthropic `cache_creation_input_tokens`；
    #: OpenCode `cache_write_5m + cache_write_1h`）。
    cache_write_tokens: Optional[int] = None
    #: Provider 侧真实美元成本（OpenCode `usage.cost`）。
    provider_cost_usd: Optional[float] = None
    #: Go 套餐用量扣减（OpenCode `usage.opencode.allowance`）。
    allowance_usd: Optional[float] = None
    #: 计费来源：free / balance / go / go-plus / byok（OpenCode）。
    billing_source: str = ""
    #: 本次调用使用的 LLM 会话 ID（用于缓存 / 路由关联）。
    llm_session_id: str = ""

    # 这些 token 数是从哪来的（api / estimated / unknown）
    token_source: str = TOKEN_SOURCE_UNKNOWN

    # 显式带单位：latency_ms。不叫 latency 是为了防止
    # 有人误以为是秒，然后把 1.5 秒当成 1.5 毫秒处理。
    latency_ms: float = 0.0

    # 各厂商的原始返回。故意不做类型约束 ——
    # 因为不同厂商的结构本来就不一样，强行统一等于把差异藏起来。
    raw_response: Optional[Any] = None

    finish_reason: Optional[str] = None

    #: 推理模型的「思考内容」（DeepSeek 的 `reasoning_content`、
    #: 其它网关的 `reasoning`）。**它永远不是 content。**
    #:
    #: 🔒 保留它只用于 Provider 层诊断 / telemetry / 实验记录。
    #:    绝不允许回填进 content —— 见上面「输出契约」的语义 2。
    reasoning: Optional[str] = None

    #: 结构化的工具调用（OpenAI 形状：`{type, function:{name, arguments}}`）。
    #:
    #: 🔒 这是**中性结构**，不含 AIEOS 动作语义；映射成 ActionIntent
    #:    由 `actions/` 负责。`tool_calls_source` 记录它从哪来：
    #:    `api`（API 原生返回）/ `dsml`（从 content 里的 DSML 恢复）/ `none`。
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls_source: str = "none"

    # 这个 token 数是不是模拟/估算出来的。
    # 保留这个字段是为了向后兼容（Step 2 的代码和测试都在用），
    # 但它能表达的信息比 token_source 少 —— 新代码请优先看 token_source。
    simulated: bool = False

    created_at: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """
        做一点一致性归一，避免出现自相矛盾的响应对象。

        规则：
            1. token_source 非法 → 报错（早失败好过悄悄用错值）
            2. simulated=True 但没指定来源 → 推断为 estimated
            3. token_source="api" 但数字是 None → 说明调用方用错了
        """
        if self.token_source not in VALID_TOKEN_SOURCES:
            raise ValueError(
                f"token_source 非法：{self.token_source!r}。"
                f"合法取值：{sorted(VALID_TOKEN_SOURCES)}"
            )

        # 只给了 simulated 这个老字段时，推断来源
        if self.simulated and self.token_source == TOKEN_SOURCE_UNKNOWN:
            self.token_source = TOKEN_SOURCE_ESTIMATED

        # estimated 必须同时把 simulated 标上，否则老代码会看漏
        if self.token_source == TOKEN_SOURCE_ESTIMATED:
            self.simulated = True

    # ------------------------------------------------------------
    # token 相关
    # ------------------------------------------------------------
    @property
    def response_model(self) -> str:
        """API 返回体里的模型（`model` 字段的语义别名）。"""
        return self.model

    @property
    def model_mismatch(self) -> bool:
        """请求的模型与响应的模型是否**不一致**（事实，不是错误判定）。"""
        return bool(self.requested_model) and self.requested_model != self.model

    @property
    def usage_known(self) -> bool:
        """token 用量是不是已知的。"""
        return self.total_tokens is not None

    @property
    def usage_from_api(self) -> bool:
        """这些 token 数是不是 API 给的准确值。可以拿去对账单。"""
        return self.token_source == TOKEN_SOURCE_API

    @property
    def usage_is_estimated(self) -> bool:
        return self.token_source == TOKEN_SOURCE_ESTIMATED

    # ------------------------------------------------------------
    # 兼容你需求里的叫法
    # ------------------------------------------------------------
    @property
    def latency(self) -> float:
        """latency_ms 的别名（单位仍是毫秒）。"""
        return self.latency_ms

    # ------------------------------------------------------------
    @property
    def latency_seconds(self) -> float:
        return self.latency_ms / 1000.0

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()

    # ------------------------------------------------------------
    # 输出契约（§四）
    # ------------------------------------------------------------
    @property
    def output_status(self) -> str:
        """
        这次调用的**输出契约状态**：`VALID_CONTENT` 还是 `EMPTY_CONTENT`。

        🔒 刻意做成**派生属性**而不是可写字段 ——
           如果它是一个可以单独赋值的字段，就可能出现
           `content=""` 而 `output_status="VALID_CONTENT"` 的自相矛盾对象。
           那正是本 Bug 的同一类错误（状态与事实不一致）在更高一层的复现。

        判据只有一条：**external content 去掉空白后是不是空的。**
            · 与 token 数无关（花了很多 token 也可能是 EMPTY_CONTENT）
            · 与有没有 reasoning 无关（有推理也不能升级成有效内容）
        """
        return (OUTPUT_EMPTY_CONTENT if not self.content.strip()
                else OUTPUT_VALID_CONTENT)

    @property
    def cognitive_status(self) -> str:
        """
        这一轮**认知输出**的语义完成度。

        🔒 与 `output_status` **正交**：
             `output_status` 回答「能不能解析出结构」，
             本属性回答「模型是不是把这一轮想完了」。

        🔒 它**不是** Provider Error：
             provider 正常完成了 API 请求，只是模型输出撞到了上限。
             所以写成「Provider call = SUCCESS，Cognitive output = TRUNCATED」，
             绝不允许包装成 `ProviderError`。

        🔒 取不到 `finish_reason`（未上报）→ `UNKNOWN`，
           **不假装 COMPLETE，也不假装 TRUNCATED**。
        """
        reason = str(self.finish_reason or "").strip().lower()
        if not reason:
            return COGNITIVE_UNKNOWN
        return COGNITIVE_FINISH_REASON_MAP.get(reason, COGNITIVE_INCOMPLETE)

    @property
    def is_truncated(self) -> bool:
        """这一轮认知输出被截断了吗？"""
        return self.cognitive_status == COGNITIVE_TRUNCATED

    @property
    def has_valid_content(self) -> bool:
        """这次调用有没有拿到**可用**的生产内容。"""
        return self.output_status == OUTPUT_VALID_CONTENT

    @property
    def has_reasoning(self) -> bool:
        """模型有没有给出推理内容（诊断用，**不代表有产出**）。"""
        return bool((self.reasoning or "").strip())

    @property
    def reasoning_only(self) -> bool:
        """
        「只有推理、没有内容」——这正是本次实验真实遇到的那一种。

        🔒 它是诊断信号，**不是**失败信号：
           API 确实成功了，成本确实发生了。
           调用方据此**不能**把这次调用当成一次有效生产。
        """
        return self.output_status == OUTPUT_EMPTY_CONTENT and self.has_reasoning

    # ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "token_source": self.token_source,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "requested_cognitive_budget": self.requested_cognitive_budget,
            "actual_max_tokens": self.actual_max_tokens,
            "budget_clipped": self.budget_clipped,
            "simulated": self.simulated,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            # §四 输出契约
            "output_status": self.output_status,
            "cognitive_status": self.cognitive_status,
            "finish_reason": self.finish_reason,
            # 🔒 reasoning 单独一格，**不混进 content**。
            #    放进来是为了让实验记录能解释「为什么 content 是空的」。
            "reasoning_chars": len(self.reasoning or ""),
            "tool_calls": list(self.tool_calls or []),
            "tool_calls_source": self.tool_calls_source,
        }

    def __str__(self) -> str:
        if self.total_tokens is None:
            usage = "token 用量未知"
        else:
            usage = f"{self.total_tokens} tokens"
            if self.simulated:
                usage += " (模拟)"

        return f"<LLMResponse {self.provider}/{self.model} {usage}>"

    def __repr__(self) -> str:
        preview = self.content[:40].replace("\n", " ")
        if len(self.content) > 40:
            preview += "…"
        return (
            f"LLMResponse(provider={self.provider!r}, model={self.model!r}, "
            f"tokens={self.total_tokens}, "
            f"token_source={self.token_source!r}, content={preview!r})"
        )


# ============================================================
# Provider 契约
# ============================================================
class LLMProvider(ABC):
    """
    所有模型提供商的基类。

    【怎么加一个新厂商】
        1. 在 llm/ 下新建一个文件，比如 llm/deepseek.py
        2. 写一个类继承 LLMProvider
        3. 实现 generate()
        4. 在 Router 的注册表里注册

        就这样。Agent 代码、Economy 代码、Brain 代码一行都不用改。

    【什么不该做】
        Provider 里不要碰数据库，不要碰钱包，不要算钱。
        Provider 只负责一件事：把 messages 变成 LLMResponse。
        记账是 Brain 的事，算钱是 Economy 的事。
    """

    # 子类必须覆盖。这个名字就是配置里 llm.providers.<name> 的那个 <name>。
    name: str = "base"

    # 是否需要 API Key。MockProvider 会把它设成 False。
    requires_api_key: bool = True

    #: 是否支持 `thinking` / `reasoning_effort` 请求字段。
    #:
    #: 🔒 这是 **Provider 自己的能力声明**，不是 Agent 层的厂商判断。
    #:    默认 False —— 不认识的 provider 不会被塞入未知字段。
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    #: 是否接受 `content` 里的 image_url / file parts。
    #: 默认 False —— 不认识的 provider 不会把图片悄悄降级成文本。
    supports_image_input: bool = False

    def __init__(
        self,
        provider_config: Optional[Mapping[str, Any]] = None,
        default_model: str = "",
    ):
        """
        参数：
            provider_config: 该 provider 在 config.yaml 里的那一段配置
            default_model:   兜底模型名

        优先级：配置里的 model > 构造参数 default_model
            为什么配置优先？
                因为配置是用户随时可以改的运行参数，
                构造参数只是代码里的兜底默认。
                如果反过来的话，用户改了 config.yaml 却不生效，
                会非常难查 —— 他会以为配置读错了，其实是代码把它盖掉了。
        """
        self.config: Dict[str, Any] = dict(provider_config or {})
        self.default_model = str(
            self.config.get("model") or default_model or ""
        )

    # ------------------------------------------------------------
    # 必须实现
    # ------------------------------------------------------------
    @abstractmethod
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
        生成一次回复。

        参数故意和主流厂商的 API 保持一致命名，
        这样接真实 API 时不用再翻译一层。

        返回：LLMResponse（统一结构，不能是厂商原始 JSON）
        """
        raise NotImplementedError

    # ------------------------------------------------------------
    # 可以覆盖，也可以直接用
    # ------------------------------------------------------------
    def is_available(self) -> bool:
        """
        这个 Provider 现在能不能用。

        MockProvider 永远返回 True。
        真实 Provider 应该检查 API Key 有没有配置。
        """
        return True

    def unavailable_reason(self) -> Optional[str]:
        """不能用的话，说明原因（给用户看的提示）。"""
        return None

    def resolve_model(self, model: Optional[str] = None) -> str:
        """决定这次调用用哪个模型名。"""
        return str(model or self.default_model or "")

    def describe(self) -> Dict[str, Any]:
        """自述。给启动横幅、调试输出和 describe 命令用。"""
        return {
            "provider": self.name,
            "model": self.default_model,
            "available": self.is_available(),
            "requires_api_key": self.requires_api_key,
        }

    def __str__(self) -> str:
        return f"{type(self).__name__}({self.name})"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.name!r}, "
            f"model={self.default_model!r})"
        )
