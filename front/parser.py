"""
Front 决策解析
===============

从模型输出里解析：

    {"intent":"direct_reply","reply":"..."}
    {"intent":"handoff","task_text":"..."}
    {"intent":"handoff","brief":{"files":[...]}}
    {"intent":"rework","task_id":"..."}
    {"intent":"follow_up","task_id":"..."}
    {"intent":"cancel","task_id":"..."}
    {"intent":"artifacts","task_id":"..."}
    {"intent":"clarify","question":"..."}

🔒 解析只做结构校验和任务 ID 归属校验，不创建 Task、不改 Task 状态。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from front.contracts import FrontBrief, FrontDecision, FrontIntent


#: Front 的动作名（与 work.front_actions.FRONT_ACTION_TYPES 对齐）。
#: 模型有时把动作写成 {"intent":"artifact_read", ...}；
#: 这里把它识别成动作步骤，而不是最终 intent。
FRONT_ACTION_NAMES = frozenset({
    "artifact_read",
    "project_list", "project_read",
    "web_search", "web_fetch",
    "private_read", "private_write", "private_list", "private_delete",
    "draft_read", "draft_write", "draft_append", "draft_list",
    "draft_delete",
    "alarm_create", "alarm_list", "alarm_cancel",
    "short_term_memory_add", "short_term_memory_update",
    "short_term_memory_reinforce",
    "long_term_memory_search", "long_term_memory_read",
    "long_term_memory_consolidate",
})

#: 自然形状 decision 允许出现的键。用来避免把文件内容里的
#: `{"question": ...}` / `{"reply": ...}` 误认成 Front 决定。
_NATURAL_DECISION_KEYS = frozenset({
    "goal", "brief", "task_text", "front_notes", "question", "reply",
    "reason", "confidence", "deliverables", "constraints",
    "acceptance_expectations", "ambiguities", "non_goals",
    "context_refs", "files", "role", "work_profile", "preferred_agent",
    "reply_style", "front_mode", "work_mode", "task_id",
    "commit_code", "rework_scope",
})


def _fenced_ranges(text: str) -> List[Tuple[int, int]]:
    """Markdown fenced code block 的字符区间。"""
    ranges: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for match in re.finditer(r"(?m)^\s*```", text):
        if start is None:
            start = match.start()
        else:
            ranges.append((start, match.end()))
            start = None
    if start is not None:
        ranges.append((start, len(text)))
    return ranges


def _iter_json_objects(text: str):
    """逐个产出 JSON 对象，并标记它是否位于 Markdown 代码块内。

    🔒 代码块里的普通 JSON 可能是**文件内容 / 示例**，不是 Front 的
       最终 decision。自然形状推断只对代码块外的对象生效。
    """
    text = str(text or "")
    ranges = _fenced_ranges(text)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            fenced = any(
                start <= index < end for start, end in ranges)
            yield value, fenced


def _brief_from(payload: Dict[str, Any], human_text: str) -> Optional[FrontBrief]:
    raw = payload.get("brief")
    if not isinstance(raw, dict):
        raw = payload if ("goal" in payload or "task_text" in payload) else None
    if not isinstance(raw, dict):
        return None
    task_text = str(payload.get("task_text") or "").strip()
    # 🔒 `task_text` 是 Worker 执行的任务说明。`goal` 只是日志里的短标签。
    goal = task_text or str(raw.get("goal") or "").strip()
    if not goal:
        return None
    front_notes = str(
        payload.get("front_notes")
        or raw.get("front_notes") or "").strip()
    try:
        return FrontBrief(
            goal=goal,
            human_text=human_text,
            task_text=task_text or goal,
            front_notes=front_notes,
            # 结构化 brief 字段不再从 Front 输出解析：它们不会传给 Worker，
            # 继续接收只会让 Front 误以为自己已经交付了这些要求。
            files=raw.get("files") or [],
            fallback=bool(raw.get("fallback")),
        )
    except ValueError:
        return None


def parse_front_actions(
    text: str,
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """解析 Front 的动作步骤。

    返回 `(actions, error)`：

        actions = None  本轮不是动作步骤（交给 parse_front_decision）
        actions = [...] 动作步骤；按声明顺序执行
        error != ""     看起来是动作信封，但结构不合法
    """
    for payload, _fenced in _iter_json_objects(str(text or "")):
        direct = str(
            payload.get("intent") or payload.get("action_type") or "").strip()
        if direct in FRONT_ACTION_NAMES:
            params = payload.get("parameters")
            if not isinstance(params, dict):
                params = {
                    key: value for key, value in payload.items()
                    if key not in (
                        "intent", "action_type", "reason", "decision",
                        "action", "actions",
                    )
                }
            raw = [{"type": direct, "parameters": params}]
        else:
            raw = payload.get("actions")
            if raw is None and isinstance(payload.get("action"), dict):
                raw = [payload["action"]]
        if raw is None:
            continue
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return None, "front_actions_not_a_list"
        actions: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                return None, "front_action_not_an_object"
            action_type = str(
                item.get("type") or item.get("action_type") or "").strip()
            parameters = item.get("parameters")
            if not action_type:
                return None, "front_action_missing_type"
            if not isinstance(parameters, dict):
                return None, "front_action_missing_parameters"
            actions.append({
                "type": action_type,
                "parameters": dict(parameters),
            })
        if actions:
            return actions, ""
    return None, ""


def parse_front_decision(
    text: str,
    *,
    human_text: str = "",
    active_task_ids: Iterable[str] = (),
    completed_task_ids: Iterable[str] = (),
) -> Tuple[Optional[FrontDecision], str]:
    """
    解析前端模型输出。

    返回 `(decision, error)`；成功时 error 为空字符串。
    """
    active = {str(item) for item in active_task_ids}
    completed = {str(item) for item in completed_task_ids}

    for payload, fenced in _iter_json_objects(str(text or "")):
        raw_intent = str(
            payload.get("intent") or payload.get("decision") or "").strip()
        # 动作步骤不是最终决定：即使模型顺手写了 reply，也不能把它
        # 当成 direct_reply 而跳过动作。交给 parse_front_actions。
        if not raw_intent and (
                "actions" in payload or "action" in payload):
            continue
        # 兼容自然形状：有 goal 就是 handoff，有 reply 就是 direct_reply。
        # 🔒 代码块里的 JSON 可能是文件内容（例如 QUESTION.json），
        #    只有显式 intent 才能作为最终决定。
        if not raw_intent:
            if fenced:
                continue
            if any(
                    key not in _NATURAL_DECISION_KEYS
                    for key in payload):
                continue
            if "goal" in payload or "brief" in payload:
                raw_intent = FrontIntent.HANDOFF
            elif "question" in payload:
                raw_intent = FrontIntent.CLARIFY
            elif "reply" in payload:
                raw_intent = FrontIntent.DIRECT_REPLY

        if raw_intent not in FrontIntent.all():
            continue

        confidence = payload.get("confidence", 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 1.0

        if raw_intent == FrontIntent.DIRECT_REPLY:
            reply = str(payload.get("reply") or "").strip()
            if not reply:
                return None, "direct_reply_missing_reply"
            return FrontDecision(
                intent=raw_intent, reply=reply,
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.NO_NOTIFY:
            return FrontDecision(
                intent=raw_intent,
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.HANDOFF:
            # Front 自己决定：逐字转交原文，或给一个忠实的重述。
            # 两种都允许；`task_text` 就是 Worker 执行的任务说明。
            reply_style = str(payload.get("reply_style") or "").strip()
            front_notes = str(payload.get("front_notes") or "").strip()
            brief = _brief_from(payload, human_text)
            task_text = str(payload.get("task_text") or "").strip()
            if not task_text and brief is not None:
                task_text = brief.goal
            if not task_text:
                task_text = str(human_text or "").strip()
            if not task_text:
                return None, "handoff_missing_task_text"
            if brief is None:
                brief = FrontBrief(
                    goal=task_text,
                    human_text=human_text,
                    task_text=task_text,
                    front_notes=front_notes,
                    reply_style=reply_style,
                )
            else:
                brief.human_text = str(human_text or brief.human_text)
                brief.task_text = task_text
                brief.front_notes = front_notes or brief.front_notes
                brief.reply_style = reply_style or brief.reply_style
            return FrontDecision(
                intent=raw_intent, brief=brief,
                reply=str(payload.get("reply") or "").strip(),
                reply_style=reply_style,
                preferred_agent=str(
                    payload.get("preferred_agent") or "").strip(),
                role=str(payload.get("role") or "").strip(),
                work_profile=str(
                    payload.get("work_profile") or "standard").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.REWORK:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id not in completed:
                return None, "rework_task_not_completed"
            brief = _brief_from(payload, human_text)
            if brief is not None:
                brief.reply_style = str(
                    payload.get("reply_style") or "").strip()
                brief.front_notes = str(
                    payload.get("front_notes") or "").strip()
            return FrontDecision(
                intent=raw_intent, task_id=task_id, brief=brief,
                reply=str(payload.get("reply") or "").strip(),
                rework_scope=str(
                    payload.get("rework_scope") or "enhance").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.FOLLOW_UP:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id not in active:
                return None, "follow_up_task_not_active"
            brief = _brief_from(payload, human_text)
            if brief is not None:
                brief.reply_style = str(
                    payload.get("reply_style") or "").strip()
                brief.front_notes = str(
                    payload.get("front_notes") or "").strip()
            return FrontDecision(
                intent=raw_intent, task_id=task_id, brief=brief,
                reply=str(payload.get("reply") or "").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.CANCEL:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id not in active:
                return None, "cancel_task_not_active"
            return FrontDecision(
                intent=raw_intent, task_id=task_id,
                reply=str(payload.get("reply") or "").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.ARTIFACTS:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id not in (active | completed):
                return None, "artifacts_task_not_found"
            return FrontDecision(
                intent=raw_intent, task_id=task_id,
                reply=str(payload.get("reply") or "").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.INSPECT:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id not in (active | completed):
                return None, "inspect_task_not_found"
            return FrontDecision(
                intent=raw_intent, task_id=task_id,
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.SET_MODE:
            front_mode = str(
                payload.get("front_mode") or "").strip().lower()
            work_mode = str(
                payload.get("work_mode") or "").strip().lower()
            if not front_mode and not work_mode:
                return None, "set_mode_missing_target"
            return FrontDecision(
                intent=raw_intent,
                front_mode=front_mode,
                work_mode=work_mode,
                reply=str(payload.get("reply") or "").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.COMMIT_CONFIRM:
            task_id = str(payload.get("task_id") or "").strip()
            code = str(
                payload.get("commit_code")
                or payload.get("code") or "").strip()
            if task_id not in (active | completed):
                return None, "commit_confirm_task_not_found"
            return FrontDecision(
                intent=raw_intent,
                task_id=task_id,
                commit_code=code,
                reply=str(payload.get("reply") or "").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.CLARIFY:
            question = str(payload.get("question") or "").strip()
            return FrontDecision(
                intent=raw_intent, question=question,
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

    return None, "front_decision_not_found"


def parse_front_decisions(
    text: str,
    *,
    human_text: str = "",
    active_task_ids: Iterable[str] = (),
    completed_task_ids: Iterable[str] = (),
) -> Tuple[List[FrontDecision], str]:
    """解析**多个最终动作**。

    支持的形状：

        {"decisions":[{...}, {...}]}
        {"intents":[{...}, {...}]}
        {"actions":[{...}, {...}]}   // 仅当元素带 intent/decision 时

    单动作仍走 parse_front_decision；这里返回它的单元素列表。
    """
    active = {str(item) for item in active_task_ids}
    completed = {str(item) for item in completed_task_ids}

    for payload, _fenced in _iter_json_objects(str(text or "")):
        raw = payload.get("decisions")
        if raw is None:
            raw = payload.get("intents")
        if raw is None:
            acts = payload.get("actions")
            if isinstance(acts, list) and any(
                    isinstance(item, dict)
                    and ("intent" in item or "decision" in item)
                    for item in acts):
                raw = acts
        if raw is None:
            continue
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            return [], "front_decisions_empty"
        decisions: List[FrontDecision] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                return [], f"front_decision_{index}_not_object"
            decision, error = parse_front_decision(
                json.dumps(item, ensure_ascii=False),
                human_text=human_text,
                active_task_ids=active,
                completed_task_ids=completed,
            )
            if decision is None:
                return [], f"front_decision_{index}:{error}"
            decisions.append(decision)
        return decisions, ""

    decision, error = parse_front_decision(
        text,
        human_text=human_text,
        active_task_ids=active,
        completed_task_ids=completed,
    )
    if decision is None:
        return [], error
    return [decision], ""
