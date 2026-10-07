"""
从 LLM 的**结构化信封**里解析 Reflection Proposal（§九 / §十 / §十四）
============================================================

【🔴 §十 / §十一：Reflection 是**可选**的】

    `reflections` 缺失、为空数组、或解析失败
        → **零个** Proposal
        → Self Model **不发生任何变化**

    ❌ Runtime 绝不能因为「有 Experience」或「Action 失败」
       就自动补一个 Proposal。

【🔴 §十四：Proposal 必须引用 Experience】

    `evidence` 必须是 experience_id 列表。
    ⚠️ 这里只做**结构**检查（是不是非空字符串列表）；
       「evidence 是否真的存在、是否属于本 Agent」
       由 `SelfModelWriteService` 校验（§十六：不重复实现一套）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.identity import utc_now_iso
from core.logging_setup import get_logger
from reflection.proposal import (
    VALID_KINDS, ReflectionProposal, new_proposal_id,
)

logger = get_logger(__name__)


def parse_reflections(
    agent_id: str,
    envelope: Optional[Dict[str, Any]],
) -> List[ReflectionProposal]:
    """
    信封 → Proposal 列表。**没有就是空列表，不补、不猜。**

    接受两种写法：
        "reflections": [ {...}, {...} ]
        "reflection":  { ... }          （单个，向后放宽）
    """
    if not isinstance(envelope, dict):
        return []

    raw = envelope.get("reflections")
    if raw is None:
        single = envelope.get("reflection")
        raw = [single] if isinstance(single, dict) else []
    if not isinstance(raw, list):
        return []

    out: List[ReflectionProposal] = []
    for item in raw:
        proposal = _one(agent_id, item)
        if proposal is not None:
            out.append(proposal)
    return out


def _one(agent_id: str, item: Any) -> Optional[ReflectionProposal]:
    if not isinstance(item, dict):
        return None

    kind = str(item.get("kind") or item.get("proposal_kind") or "").strip()
    if kind not in VALID_KINDS:
        # 🔒 未知 kind → 丢掉这一条，**不**猜测它想做什么
        logger.info("忽略未知的 Reflection kind=%r (agent_id=%s)",
                    kind, agent_id)
        return None

    change = item.get("proposed_change")
    if change is None:
        change = item.get("change")
    if not isinstance(change, dict):
        change = {}

    evidence = item.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    evidence = [str(e) for e in evidence if isinstance(e, (str, int))
                and str(e).strip()]

    scope = item.get("scope")
    if not isinstance(scope, dict):
        scope = {}

    return ReflectionProposal(
        proposal_id=str(item.get("proposal_id") or new_proposal_id()),
        agent_id=agent_id,
        kind=kind,
        target=str(item.get("target") or ""),
        proposed_change=dict(change),
        reason=str(item.get("reason") or ""),
        evidence=evidence,
        scope=dict(scope),
        created_at=utc_now_iso(),
    )
