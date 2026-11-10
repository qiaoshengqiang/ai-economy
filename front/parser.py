"""
Front 决策解析
===============

从模型输出里解析：

    {"intent":"direct_reply","reply":"..."}
    {"intent":"handoff","brief":{"goal":"..."}}
    {"intent":"rework","task_id":"..."}
    {"intent":"follow_up","task_id":"..."}
    {"intent":"cancel","task_id":"..."}
    {"intent":"artifacts","task_id":"..."}
    {"intent":"clarify","question":"..."}

🔒 解析只做结构校验和任务 ID 归属校验，不创建 Task、不改 Task 状态。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional, Tuple

from front.contracts import FrontBrief, FrontDecision, FrontIntent


def _iter_json_objects(text: str):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def _string_list(value: Any):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _brief_from(payload: Dict[str, Any], human_text: str) -> Optional[FrontBrief]:
    raw = payload.get("brief")
    if not isinstance(raw, dict):
        raw = payload if "goal" in payload else None
    if not isinstance(raw, dict):
        return None
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        return None
    try:
        return FrontBrief(
            goal=goal,
            human_text=human_text,
            deliverables=_string_list(raw.get("deliverables")),
            constraints=_string_list(raw.get("constraints")),
            acceptance_expectations=_string_list(
                raw.get("acceptance_expectations")),
            ambiguities=_string_list(raw.get("ambiguities")),
            non_goals=_string_list(raw.get("non_goals")),
            context_refs=_string_list(raw.get("context_refs")),
            fallback=bool(raw.get("fallback")),
        )
    except ValueError:
        return None


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

    for payload in _iter_json_objects(str(text or "")):
        raw_intent = str(
            payload.get("intent") or payload.get("decision") or "").strip()
        # 兼容自然形状：有 goal 就是 handoff，有 reply 就是 direct_reply。
        if not raw_intent:
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

        if raw_intent == FrontIntent.HANDOFF:
            # Front 自己决定：逐字转交原文，或给一个忠实的重述。
            # 两种都允许；`task_text` 就是 Work Agent 看到的任务文本。
            reply_style = str(payload.get("reply_style") or "").strip()
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
                    reply_style=reply_style,
                )
            else:
                brief.human_text = str(human_text or brief.human_text)
                brief.task_text = task_text
                brief.reply_style = reply_style or brief.reply_style
            return FrontDecision(
                intent=raw_intent, brief=brief,
                reply=str(payload.get("reply") or "").strip(),
                reply_style=reply_style,
                preferred_agent=str(
                    payload.get("preferred_agent") or "").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.REWORK:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id not in completed:
                return None, "rework_task_not_completed"
            return FrontDecision(
                intent=raw_intent, task_id=task_id,
                reply=str(payload.get("reply") or "").strip(),
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

        if raw_intent == FrontIntent.FOLLOW_UP:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id not in active:
                return None, "follow_up_task_not_active"
            return FrontDecision(
                intent=raw_intent, task_id=task_id,
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

        if raw_intent == FrontIntent.CLARIFY:
            question = str(payload.get("question") or "").strip()
            return FrontDecision(
                intent=raw_intent, question=question,
                confidence=confidence,
                reason=str(payload.get("reason") or "")), ""

    return None, "front_decision_not_found"
