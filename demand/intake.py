"""
两阶段 Demand Intake 的 Front brief 解析
==========================================

V1 的入口 Agent 同时承担 Front 与 Creator，但**不应在同一次调用里
既解读 Human Demand，又做架构/分工**。

阶段 1（Front）只产出一个 brief：
    goal / deliverables / constraints / acceptance_expectations /
    ambiguities / non_goals

阶段 2（Creator）拿 brief + Agent Directory + Workspace 锁规则，
产出 workspace_plan，再调用 task_create。

🔒 这里只解析**数据**，不创建 Task、不读 Workspace、不决定分工。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

#: brief 里的列表字段。
BRIEF_LIST_FIELDS = (
    "deliverables",
    "constraints",
    "acceptance_expectations",
    "ambiguities",
    "non_goals",
)


def _iter_json_objects(text: str):
    """按出现顺序产出文本里能解析的 JSON 对象。"""
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


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def parse_brief(text: Optional[str]) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    从模型输出里解析 Front brief。

    返回 `(brief, error)`；成功时 error 为空字符串。
    接受 `{"brief": {...}}` 和直接的 `{"goal": ...}`。
    """
    for payload in _iter_json_objects(str(text or "")):
        raw = payload.get("brief")
        if not isinstance(raw, dict):
            raw = payload if "goal" in payload else None
        if not isinstance(raw, dict):
            continue
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            return None, "brief_missing_goal"
        brief: Dict[str, Any] = {"goal": goal}
        for key in BRIEF_LIST_FIELDS:
            brief[key] = _string_list(raw.get(key))
        return brief, ""
    return None, "brief_not_found"


def fallback_brief(demand: str) -> Dict[str, Any]:
    """Front 连续失败时的最小 fallback：不丢 Human Demand。"""
    return {
        "goal": str(demand or "").strip(),
        "deliverables": [],
        "constraints": [],
        "acceptance_expectations": [],
        "ambiguities": [],
        "non_goals": [],
        "fallback": True,
    }
