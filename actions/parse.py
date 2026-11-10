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
ENVELOPE_KEYS = {"decision", "action", "actions", "action_type",
                 "reflections", "reflection", "thinking",
                 "cognitive_budget"}

#: `thinking.effort` 允许的值。
#:
#: 🔒 这只是**语法白名单**，不是「推荐哪个值」——
#:    具体映射（medium→high 等）由 provider 决定。
THINKING_EFFORTS = frozenset({
    "low", "medium", "high", "max",
    "minimal", "xhigh", "ultra", "none",
})


THINKING_PHASES = ("intake", "cognition", "decision", "work", "summary")


def _normalize_single_thinking(raw: Any) -> Optional[Dict[str, Any]]:
    """`{"enabled": bool, "effort": str?}` 的单相校验。"""
    # 兼容常见简写：thinking: "high" / "off"
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in THINKING_EFFORTS:
            return {"enabled": True, "effort": token}
        if token in ("off", "disabled"):
            return {"enabled": False}
        if token in ("on", "enabled"):
            return {"enabled": True}
        return None
    if not isinstance(raw, dict):
        return None
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        # 兼容 provider 形状：{"type": "enabled"|"disabled"}
        type_raw = raw.get("type")
        if isinstance(type_raw, str):
            token = type_raw.strip().lower()
            if token in ("enabled", "on", "true"):
                enabled = True
            elif token in ("disabled", "off", "false"):
                enabled = False
        # 兼容 {"effort": "high"}：显式给了强度，默认开启。
        if not isinstance(enabled, bool):
            effort_only = raw.get("effort")
            if (isinstance(effort_only, str)
                    and effort_only.strip().lower() in THINKING_EFFORTS):
                enabled = True
            else:
                return None
    effort = raw.get("effort")
    if effort is None:
        return {"enabled": enabled}
    if not isinstance(effort, str):
        return None
    effort = effort.strip().lower()
    if effort not in THINKING_EFFORTS:
        return None
    return {"enabled": enabled, "effort": effort}


def normalize_thinking(raw: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    把信封里的 `thinking` 归一成 **phase → 设置** 的映射。

    接受两种形状：

        · 单相（覆盖**当前相**）：
            {"enabled": true, "effort": "high"}
            → {"*": {"enabled": true, "effort": "high"}}

        · 分相（分别覆盖 intake / decision / work）：
            {"intake":   {"enabled": true,  "effort": "max"},
             "decision": {"enabled": false, "effort": "low"},
             "work":     {"enabled": true,  "effort": "high"}}
            → 原样（只保留合法相）

    形状不合法返回 None —— 调用方据此记 `thinking_error`，
    但**不**因此丢掉同一信封里的动作。
    """
    if isinstance(raw, str):
        norm = _normalize_single_thinking(raw)
        return {"*": norm} if norm is not None else None
    if not isinstance(raw, dict):
        return None

    # 分相形状：只要出现 decision / work 键，就按分相解析。
    if any(k in raw for k in THINKING_PHASES):
        out: Dict[str, Dict[str, Any]] = {}
        for phase in THINKING_PHASES:
            if phase not in raw or raw.get(phase) is None:
                continue
            norm = _normalize_single_thinking(raw[phase])
            if norm is None:
                return None
            out[phase] = norm
        return out or None

    # 单相形状 → 当前相
    norm = _normalize_single_thinking(raw)
    if norm is None:
        return None
    return {"*": norm}


def _normalize_single_cognitive_budget(raw: Any) -> Optional[int]:
    """单个预算值：必须是正整数（bool 不算整数）。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    return None


def normalize_cognitive_budget(raw: Any) -> Optional[Dict[str, int]]:
    """
    把信封里的 `cognitive_budget` 归一成 **phase → 预算** 的映射。

    接受两种形状：

        · 单相（覆盖**当前相**）：
            32768
            → {"*": 32768}

        · 分相（分别覆盖 intake / decision / work）：
            {"intake": 8192, "decision": 8192, "work": 32768}
            → 原样（只保留合法相）

    形状不合法返回 None —— 调用方据此记 `cognitive_budget_error`，
    但**不**因此丢掉同一信封里的动作。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = _normalize_single_cognitive_budget(raw)
        return {"*": value} if value is not None else None
    if not isinstance(raw, dict):
        return None

    # 分相形状：只要出现 intake / decision / work 键，就按分相解析。
    if any(k in raw for k in THINKING_PHASES):
        out: Dict[str, int] = {}
        for phase in THINKING_PHASES:
            if phase not in raw or raw.get(phase) is None:
                continue
            value = _normalize_single_cognitive_budget(raw[phase])
            if value is None:
                return None
            out[phase] = value
        return out or None

    # 单相形状 → 当前相
    value = _normalize_single_cognitive_budget(raw)
    if value is None:
        return None
    return {"*": value}


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

    # 🔴 不能只看**第一个**花括号块。
    #    实测：模型会把示例 JSON 放在信封前面，或者外层信封被截断
    #    而内部某个小对象仍然合法。只看第一个 ⇒ 合法信封被漏掉，
    #    还会被误报成 `unparsable_envelope`。
    #    所以：**按出现顺序**逐个尝试，返回第一个可识别的信封。
    for data in _iter_balanced_json(text):
        # 数组根 → 只可能是 reflection 列表（Action 一定是对象）
        if isinstance(data, list):
            if all(isinstance(x, dict) for x in data) and data:
                return {"reflections": data}
            continue

        if not isinstance(data, dict):
            continue

        # 必须至少有一个可识别的键，否则这只是一段碰巧是 JSON 的文本
        if ENVELOPE_KEYS & set(data.keys()):
            return data

    return None


def _iter_balanced_json(text: str):
    """
    按出现顺序，产出**每一个**能解析的平衡 `{...}` / `[...]`。

    ⚠️ 用括号计数而不是正则 —— JSON 可以嵌套，
       而且字符串字面量里的括号会骗过朴素正则。
    """
    pairs = {"{": "}", "[": "]"}
    for start, first in enumerate(text):
        if first not in pairs:
            continue
        close_ch = pairs[first]
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
            if ch == first:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(text[start:i + 1])
                    except (ValueError, TypeError):
                        pass
                    break


def _first_balanced_json(text: str) -> Any:
    """第一个能解析的平衡 JSON 块（向后兼容的私有入口）。"""
    for data in _iter_balanced_json(text):
        return data
    return None


def looks_like_envelope(text: Optional[str]) -> bool:
    """
    这段输出**像不像**一个结构化信封（即使它坏了）？

    🔒 只用于**诊断**，不用于猜测动作。
       它回答的是「该不该报 malformed」，而不是「Agent 想做什么」。
    """
    if not text or not isinstance(text, str):
        return False
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("```json"):
        return True
    return any(k in text for k in ('"decision"', '"actions"',
                                    '"action_type"'))


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
        # 🔴 「没有信封」与「信封坏了」必须分开：
        #    前者是 Agent 主动只回复（正常），
        #    后者是**解析失败**（模型想行动，但输出不可用）。
        #    混在一起会把 parser 问题误读成 Agent 不行动。
        error = "unparsable_envelope" if looks_like_envelope(text) else ""
        return Decision(
            decision_id=decision_id, agent_id=agent_id,
            kind=DECISION_REPLY, reasoning=text or "",
            envelope_error=error,
            created_at=utc_now_iso(),
        )

    reasoning = str(envelope.get("reasoning") or "")

    # 🔴 Agent 可以决定**下一步**的思考模式。只解析，不在这里注入 ——
    #    真正是否支持、如何映射到 provider 参数，由 Brain 适配层决定。
    raw_thinking = envelope.get("thinking")
    thinking = normalize_thinking(raw_thinking)
    thinking_error = ""
    if raw_thinking is not None and thinking is None:
        thinking_error = "invalid_thinking"

    # 🔴 Agent 也可以决定下一步的**认知预算**（与 thinking 同一层）。
    #    这里只解析；是否应用、如何与 Human cap 取 min，由 Brain 决定。
    raw_budget = envelope.get("cognitive_budget")
    cognitive_budget = normalize_cognitive_budget(raw_budget)
    cognitive_budget_error = ""
    if raw_budget is not None and cognitive_budget is None:
        cognitive_budget_error = "invalid_cognitive_budget"

    # 🔴 2026-10-10：**一次决策可以有 0..N 个动作**。
    #
    #    【为什么要多个】
    #        API 完全支持，而我们**有这个需求** ——
    #        例如「写完正文的**同时**通知下一个人」。
    #        旧形状只有单数，Agent 只能拆成两次决策，
    #        而「下一次」需要有人唤醒它 ⇒ 拆完就断在那里。
    #
    #    🔒 `actions`（列表）与 `action`（单数）**都接受** ——
    #       向后兼容：老实验 / `real_api_self_evolution` 一行都不用改。
    raw = envelope.get("actions")
    if raw is None:
        single = envelope.get("action")
        raw = [] if single is None else [single]
    elif isinstance(raw, dict):
        raw = [raw]              # 容忍 `actions` 里塞了个 dict

    intents: List[ActionIntent] = []
    dropped = 0
    for entry in (raw if isinstance(raw, (list, tuple)) else []):
        if not isinstance(entry, dict) or not (
                entry.get("type") or entry.get("action_type")):
            dropped += 1
            continue
        try:
            intents.append(build_intent(
                agent_id, entry, reason=reasoning,
                decision_id=decision_id))
        except ValueError:
            # 单个写坏 → **跳过它**，但不静默（`dropped` 如实记数）
            dropped += 1

    if not intents:
        # 一个都没成 → 和今天一样**降级为普通回复，不崩**；
        # 但如果它**确实给了候选动作**，必须把失败原因带出来。
        error = ""
        if isinstance(raw, (list, tuple)) and len(raw) > 0:
            error = "no_valid_actions"
        return Decision(
            decision_id=decision_id, agent_id=agent_id,
            kind=DECISION_REPLY, reasoning=reasoning or (text or ""),
            dropped_actions=dropped, envelope_error=error,
            thinking=thinking, thinking_error=thinking_error,
            thinking_raw=(raw_thinking if thinking_error else None),
            cognitive_budget=cognitive_budget,
            cognitive_budget_error=cognitive_budget_error,
            created_at=utc_now_iso(),
        )

    return Decision(
        decision_id=decision_id, agent_id=agent_id,
        kind=DECISION_ACT, reasoning=reasoning,
        intents=intents, dropped_actions=dropped,
        thinking=thinking, thinking_error=thinking_error,
        thinking_raw=(raw_thinking if thinking_error else None),
        cognitive_budget=cognitive_budget,
        cognitive_budget_error=cognitive_budget_error,
        created_at=utc_now_iso(),
    )


#: 给 Prompt 用的信封说明。🔒 这是**格式说明**，不是行为指令 ——
#: 它不告诉 Agent「该不该行动」，只说「如果要行动，写成什么形状」。
ENVELOPE_INSTRUCTION = (
    'If you decide to take an action, answer with a JSON object:\n'
    '{"decision":"act","reasoning":"...",'
    '"actions":[{"type":"<type>","parameters":{...}}]}\n'
    'You may put **more than one** action in the list when they belong'
    ' together (e.g. write a file and notify somebody about it).\n'
    'The single form {"action":{...}} is also accepted.\n'
    'To choose thinking mode, add at the top level '
    '{"thinking":{"intake":{"enabled":true,"effort":"max"},'
    '"decision":{"enabled":false,"effort":"low"},'
    '"work":{"enabled":true,"effort":"high"}}}; '
    'a single {"enabled":...,"effort":...} overrides the current phase. '
    'Omit it to keep the current mode.\n'
    'You may also choose the cognitive budget (max output tokens) for the '
    'next call: add {"cognitive_budget":32768} for the current phase, or '
    '{"cognitive_budget":{"work":32768,"decision":8192}} per phase. '
    'The runtime may cap it; the actual value is shown next turn.\n'
    'Otherwise just reply normally.'
)
