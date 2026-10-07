"""
把 LLM 的**结构化信封**变成 Decision / ActionIntent
============================================================

【🔴 §十四 的纪律：Action 是结构化结果，不是猜出来的字符串】

    明确**不要**：
        LLM text → 字符串解析 → 猜测它想执行什么

    只接受**显式信封**。没有信封 → 就是普通回复。

    所以本模块**没有**正则猜测、没有关键词匹配、
    没有「它提到了 write 所以大概想写文件」。

【信封格式（§十三 方案 3）】

    ```json
    {
      "decision": "act",
      "reasoning": "先确认核心冲突",
      "action": {"type": "reply", "parameters": {"content": "..."}}
    }
    ```

    · `decision` 省略时按 `"act"`（有 action 块就是 act）
    · 没有 `action` 块 → `decision="reply"`，**不产生 Intent**

【信封怎么从 content 里找出来】

    LLM 常常把 JSON 包在 ```json ... ``` 里，或者前后带说明文字。
    所以：**取第一个平衡的花括号块**，尝试 json.loads。
    解析不了 → 当作普通回复（**不报错**，§十八 向后兼容）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from actions.intent import (
    DECISION_ACT, DECISION_REPLY, ActionIntent, Decision,
    new_decision_id, new_intent_id,
)
from core.identity import utc_now_iso


def _first_balanced_object(text: str) -> Optional[str]:
    """
    取出第一个**括号平衡**的 `{...}` 块。

    ⚠️ 用括号计数而不是正则 —— 因为 JSON 可以嵌套，
       正则匹配不了嵌套结构（而且字符串里的 `}` 会骗过朴素正则）。
       这里额外跳过字符串字面量里的括号。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


#: 一个 JSON 对象要算「信封」，至少得有其中一个键。
#: 🔴 `reflections` / `reflection` 必须在里面 —— 实测发现：
#:    真实模型常常**只输出 reflections，不输出 action**
#:    （例如决定「不动手，但要改策略」）。
#:    第一版白名单漏了它们，导致合法的 Reflection 被静默丢弃，
#:    进而把「parser bug」误读成「模型不遵守协议」。
ENVELOPE_KEYS = {"decision", "action", "action_type",
                 "reflections", "reflection"}


def extract_envelope(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    从 LLM 输出里找出结构化信封。找不到返回 None。

    🔒 **只认显式 JSON**。找不到就是普通回复 —— 不猜。

    接受两种根：
        { ... }   → 对象信封
        [ ... ]   → **数组根**，规范化为 {"reflections": [...]}
                    （实测：真实模型有时直接输出 reflection 数组）
    """
    if not text or not isinstance(text, str):
        return None

    data = _first_balanced_json(text)
    if data is None:
        return None

    # 数组根 → 只可能是 reflection 列表（Action 一定是对象）
    if isinstance(data, list):
        if all(isinstance(x, dict) for x in data) and data:
            return {"reflections": data}
        return None

    if not isinstance(data, dict):
        return None

    # 必须至少有一个可识别的键，否则这只是一段碰巧是 JSON 的文本
    if not (ENVELOPE_KEYS & set(data.keys())):
        return None
    return data


def _first_balanced_json(text: str) -> Any:
    """
    找出第一个**括号平衡**的 `{...}` 或 `[...]`，解析成 Python 对象。

    ⚠️ 用括号计数而不是正则 —— JSON 可以嵌套，
       而且字符串字面量里的括号会骗过朴素正则。
    """
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)

    pairs = {"{": "}", "[": "]"}
    open_ch = text[start]
    close_ch = pairs[open_ch]
    depth = 0
    in_str = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
    return None


def build_intent(agent_id: str, action: Dict[str, Any],
                 *, reason: str = "", decision_id: str = "") -> ActionIntent:
    """从信封的 `action` 块构造 ActionIntent。"""
    if not isinstance(action, dict):
        raise ValueError("action 必须是对象")
    action_type = action.get("type") or action.get("action_type") or ""
    params = action.get("parameters")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("action.parameters 必须是对象")
    return ActionIntent(
        action_type=str(action_type),
        parameters=dict(params),
        intent_id=new_intent_id(),
        agent_id=agent_id,
        reason=reason,
        decision_id=decision_id,
        created_at=utc_now_iso(),
    )


def decision_from_text(agent_id: str, text: Optional[str]) -> Decision:
    """
    LLM 输出 → Decision（§十三 / §十四）。

    §十八 向后兼容：
        **没有信封** → `kind="reply"`，`intent=None`，
        `reasoning` 就是原文。普通 chat 完全不受影响。
    """
    decision_id = new_decision_id()
    envelope = extract_envelope(text)

    if envelope is None:
        return Decision(
            decision_id=decision_id, agent_id=agent_id,
            kind=DECISION_REPLY, reasoning=text or "",
            intent=None, created_at=utc_now_iso(),
        )

    reasoning = str(envelope.get("reasoning") or "")
    action = envelope.get("action")

    if not isinstance(action, dict) or not (
            action.get("type") or action.get("action_type")):
        return Decision(
            decision_id=decision_id, agent_id=agent_id,
            kind=DECISION_REPLY, reasoning=reasoning or (text or ""),
            intent=None, created_at=utc_now_iso(),
        )

    try:
        intent = build_intent(agent_id, action, reason=reasoning,
                              decision_id=decision_id)
    except ValueError:
        # 信封畸形的 action 块 → 降级为普通回复，**不崩**
        return Decision(
            decision_id=decision_id, agent_id=agent_id,
            kind=DECISION_REPLY, reasoning=reasoning or (text or ""),
            intent=None, created_at=utc_now_iso(),
        )

    return Decision(
        decision_id=decision_id, agent_id=agent_id,
        kind=DECISION_ACT, reasoning=reasoning,
        intent=intent, created_at=utc_now_iso(),
    )


#: 给 Prompt 用的信封说明。🔒 这是**格式说明**，不是行为指令 ——
#: 它不告诉 Agent「该不该行动」，只说「如果要行动，写成什么形状」。
ENVELOPE_INSTRUCTION = (
    'If you decide to take an action, answer with a JSON object:\n'
    '{"decision":"act","reasoning":"...",'
    '"action":{"type":"<type>","parameters":{...}}}\n'
    'Otherwise just reply normally.'
)
