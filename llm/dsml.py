"""DeepSeek DSML → 中性 tool_calls 的适配层。

🔒 只做三件事：

    1. 把真实 wire format 归一化到官方 parser 能接受的形状；
    2. 调用 vendored 的 DeepSeek 官方 parser；
    3. 校验并返回中性 `{name, arguments}`。

它**不**认识 AIEOS 的 `workspace_write` 等动作语义 —— 那是
`actions/` 的事。这样 `llm/` 仍然是厂商适配层，不是业务层。

格式事实（来自真实日志）：

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="workspace_write">
    <｜｜DSML｜｜ parameter name="path" string="true">index.html</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ calls>

即 **双竖线**（每个 ｜ 是 U+FF5C）。官方 encoding 的常量是单竖线，
所以必须先归一化。参考 CherryHQ/cherry-studio PR #14747 的实测结论。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from core.dsml import DSML_MODE_ENV, DEFAULT_DSML_MODE, dsml_mode
from llm.vendor.deepseek import encoding as dsml_encoding

_TOKEN = "｜DSML｜"
_MARKER_RE = re.compile(r"<[｜|]{1,2}DSML[｜|]{1,2}", re.IGNORECASE)

_CALLS_START_RE = re.compile(
    r"<" + re.escape(_TOKEN) + r"\s*(?:tool_calls|function_calls|calls)\s*>",
    re.IGNORECASE,
)
_CALLS_END_RE = re.compile(
    r"</" + re.escape(_TOKEN) + r"\s*(?:tool_calls|function_calls|calls)\s*>",
    re.IGNORECASE,
)
_INVOKE_START_RE = re.compile(
    r"<" + re.escape(_TOKEN) + r"\s*invoke\b", re.IGNORECASE)
_INVOKE_END_RE = re.compile(
    r"</" + re.escape(_TOKEN) + r"\s*invoke\s*>", re.IGNORECASE)
_PARAM_START_RE = re.compile(
    r"<" + re.escape(_TOKEN) + r"\s*parameter\b", re.IGNORECASE)
_PARAM_END_RE = re.compile(
    r"</" + re.escape(_TOKEN) + r"\s*parameter\s*>", re.IGNORECASE)

#: 官方 parser 的包装开始标记（注意它包含一个前导空格）。
_CANONICAL_CALLS_START = f"<{_TOKEN} calls>"
_CANONICAL_CALLS_END = f"</{_TOKEN} calls>"
_CANONICAL_INVOKE_PREFIX = f"<{_TOKEN} invoke"
_CANONICAL_INVOKE_END = f"</{_TOKEN} invoke>"
_CANONICAL_PARAM_PREFIX = f"<{_TOKEN} parameter"
_CANONICAL_PARAM_END = f"</{_TOKEN} parameter>"


@dataclass
class DSMLParseResult:
    """一次 DSML 解析的结果。"""

    found: bool = False
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    content: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.found and not self.error


def looks_like_dsml(text: Optional[str]) -> bool:
    return bool(text) and _MARKER_RE.search(str(text)) is not None


def _normalize_markers(text: str) -> str:
    # 双竖线 → 单竖线（开闭标签一起处理）
    text = text.replace("｜｜DSML｜｜", _TOKEN)
    # ASCII 变体
    text = text.replace("||DSML||", _TOKEN)
    text = text.replace("|DSML|", _TOKEN)
    return text


def _normalize_tags(text: str) -> str:
    token = re.escape(_TOKEN)
    text = re.sub(
        rf"<{token}\s*(?:tool_calls|function_calls|calls)\s*>",
        _CANONICAL_CALLS_START, text, flags=re.IGNORECASE)
    text = re.sub(
        rf"</{token}\s*(?:tool_calls|function_calls|calls)\s*>",
        _CANONICAL_CALLS_END, text, flags=re.IGNORECASE)
    text = re.sub(
        rf"<{token}\s*invoke\b", _CANONICAL_INVOKE_PREFIX, text,
        flags=re.IGNORECASE)
    text = re.sub(
        rf"</{token}\s*invoke\s*>", _CANONICAL_INVOKE_END, text,
        flags=re.IGNORECASE)
    text = re.sub(
        rf"<{token}\s*parameter\b", _CANONICAL_PARAM_PREFIX, text,
        flags=re.IGNORECASE)
    text = re.sub(
        rf"</{token}\s*parameter\s*>", _CANONICAL_PARAM_END, text,
        flags=re.IGNORECASE)
    return text


def _ensure_wrapper(text: str) -> str:
    """只有 invoke 没有 calls 包装时，补一层。"""
    if _CANONICAL_CALLS_START in text:
        return text
    if _CANONICAL_INVOKE_PREFIX not in text:
        return text

    start = text.find(_CANONICAL_INVOKE_PREFIX)
    end = text.rfind(_CANONICAL_INVOKE_END)
    if end == -1:
        body = text[start:]
        tail = ""
    else:
        body = text[start:end + len(_CANONICAL_INVOKE_END)]
        tail = text[end + len(_CANONICAL_INVOKE_END):]
    return (
        text[:start] + _CANONICAL_CALLS_START + "\n"
        + body + "\n" + _CANONICAL_CALLS_END + tail
    )


def _ensure_closing_wrapper(text: str) -> str:
    if _CANONICAL_CALLS_START not in text:
        return text
    if _CANONICAL_CALLS_END in text:
        return text
    return text.rstrip() + "\n" + _CANONICAL_CALLS_END


def _add_default_string_flag(text: str) -> str:
    """官方 parser 要求参数上有 string="true|false"。"""
    token = re.escape(_TOKEN)
    return re.sub(
        rf'(<{token}\s*parameter\s+name="[^"]+")\s*>',
        r'\1 string="true">', text, flags=re.IGNORECASE)


def _ensure_line_structure(text: str) -> str:
    """官方 parser 要求包装/关闭标签后是 `>\\n`。"""
    token = re.escape(_TOKEN)
    text = re.sub(
        rf"({re.escape(_CANONICAL_CALLS_START)})[ \t]*(?!\n)",
        r"\1\n", text)
    text = re.sub(
        rf'(<{token}\s*invoke\s+name="[^"]+">)[ \t]*(?!\n)',
        r"\1\n", text, flags=re.IGNORECASE)
    text = re.sub(
        rf"({re.escape(_CANONICAL_PARAM_END)})[ \t]*(?!\n)",
        r"\1\n", text)
    text = re.sub(
        rf"({re.escape(_CANONICAL_INVOKE_END)})[ \t]*(?!\n)",
        r"\1\n", text)
    return text


def normalize_dsml(text: str) -> str:
    """把真实 DSML 变体归一化成官方 parser 能吃的形状。"""
    value = str(text or "")
    if not looks_like_dsml(value):
        return value
    value = _normalize_markers(value)
    value = _normalize_tags(value)
    value = _ensure_wrapper(value)
    value = _ensure_closing_wrapper(value)
    value = _add_default_string_flag(value)
    value = _ensure_line_structure(value)
    return value


def _unwrap_arguments(params: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 `{"arguments": {...}}` 的嵌套包装。"""
    if set(params) != {"arguments"}:
        return params
    inner = params.get("arguments")
    if isinstance(inner, dict):
        return dict(inner)
    if isinstance(inner, str):
        try:
            parsed = json.loads(inner)
        except (ValueError, TypeError):
            return params
        if isinstance(parsed, dict):
            return parsed
    return params


def _validate_calls(
    calls: Iterable[Dict[str, Any]],
    *,
    allowed_actions: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    allowed = set(allowed_actions) if allowed_actions is not None else None
    out: List[Dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError("tool call 不是对象")
        fn = call.get("function")
        if not isinstance(fn, dict):
            raise ValueError("tool call 缺少 function")
        name = str(fn.get("name") or "").strip()
        if not name:
            raise ValueError("tool call 缺少 name")
        if allowed is not None and name not in allowed:
            raise ValueError(f"未知动作：{name}")
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                params = json.loads(raw_args)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"arguments 不是合法 JSON：{exc}") from exc
        elif isinstance(raw_args, dict):
            params = dict(raw_args)
        else:
            raise ValueError("arguments 必须是 JSON 字符串或对象")
        if not isinstance(params, dict):
            raise ValueError("arguments 必须是 JSON 对象")
        params = _unwrap_arguments(params)
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(params, ensure_ascii=False),
            },
        })
    return out


def parse_dsml_tool_calls(
    text: Optional[str],
    *,
    allowed_actions: Optional[Iterable[str]] = None,
) -> DSMLParseResult:
    """解析文本里的 DSML 块。

    `found=True` 表示文本里**有** DSML 标记；`error` 非空表示解析失败。
    调用方必须根据 `error` 决定 fail closed / 重试，不能拿半截动作执行。
    """
    original = str(text or "")
    if not looks_like_dsml(original):
        return DSMLParseResult(found=False, content=original)

    normalized = normalize_dsml(original)
    start = normalized.find(_CANONICAL_CALLS_START)
    if start == -1:
        return DSMLParseResult(
            found=True, content=original,
            error="missing_calls_block")

    # 官方 parser 从 ` calls` 之后开始读，期待接下来是 `>\n<invoke...>`
    index = start + len(f"<{_TOKEN} calls")
    try:
        new_index, stop_token, calls = dsml_encoding.parse_tool_calls(
            index, normalized)
    except Exception as exc:                                  # noqa: BLE001
        return DSMLParseResult(
            found=True, content=original,
            error=f"{type(exc).__name__}: {exc}")

    if stop_token != _CANONICAL_CALLS_END:
        return DSMLParseResult(
            found=True, content=original,
            error="incomplete_calls_block")

    try:
        openai_calls = dsml_encoding.tool_calls_to_openai_format(calls)
        validated = _validate_calls(
            openai_calls, allowed_actions=allowed_actions)
    except Exception as exc:                                  # noqa: BLE001
        return DSMLParseResult(
            found=True, content=original,
            error=f"{type(exc).__name__}: {exc}")

    content = (normalized[:start] + normalized[new_index:]).strip()
    return DSMLParseResult(
        found=True, tool_calls=validated, content=content)
