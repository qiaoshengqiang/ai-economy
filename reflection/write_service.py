"""
Controlled Self Model Write —— 受控写入路径（§十 / §十一 / §十二）
============================================================

    Agent Cognition
          ↓
    ReflectionProposal
          ↓
    **SelfModelWriteService**
          ↓
    Validation
          ↓
    SelfModelStore

【🔴 为什么不把校验塞进 Store（§十）】

    SelfModelStore 负责：**Persistence**
    本 Service 负责：Input validation · Provenance validation ·
                     Scope validation · Allowed operation validation

【§十一：Runtime 校验什么，**不**校验什么】

    ✅ 校验：结构完整 / evidence 存在 / target 属于本 Agent /
            scope 合法 / operation 允许
    ❌ **不**校验：「这个 Strategy 在现实中到底对不对」
       confidence=0.55 **不能**成为拒绝理由 ——
       Agent 完全可以形成一个自己只有 0.55 confidence 的判断。

【§十二：只能改自己的】

    Agent A 只能修改 Agent A 的 Self Model。
    target 指向的对象若属于别人 → 拒绝（`not_owner`）。

【§二十二：非法就拒绝，**不自动修正**】

    confidence = -5 → 拒绝。
    **不要** Runtime 自动改成 0 —— 那 Runtime 就开始替 Agent
    决定认知内容了。
"""

from __future__ import annotations

from typing import Any, Optional

from core.identity import utc_now_iso
from core.logging_setup import get_logger
from reflection.proposal import (
    KIND_ABANDON_STRATEGY, KIND_CREATE_CLAIM, KIND_CREATE_STRATEGY,
    KIND_RETIRE_CLAIM, KIND_REVISE_CLAIM, KIND_REVISE_STRATEGY,
    NEEDS_TARGET, VALID_KINDS, ReflectionProposal, ValidationResult,
    WriteOutcome,
)
from selfmodel.models import (
    Evidence, KIND_CLAIM, Scope, _clamp_confidence,
)

logger = get_logger(__name__)


class SelfModelWriteService:
    """
    把 ReflectionProposal 校验并落进 SelfModelStore。

    🔒 它**不产生** Proposal —— Proposal 只来自 Agent Cognition。
       它只回答「这个建议**能不能合法地写**」，不回答「这个建议**对不对**」。
    """

    def __init__(self, store: Any, *, experience_store: Any = None) -> None:
        self.store = store
        self.experience_store = experience_store

    # ================================================================
    # 校验
    # ================================================================
    def validate(self, proposal: ReflectionProposal) -> ValidationResult:
        if not isinstance(proposal, ReflectionProposal):
            return ValidationResult(False, "bad_type", "不是 ReflectionProposal")

        if proposal.kind not in VALID_KINDS:
            return ValidationResult(
                False, "unknown_kind",
                f"未知 kind {proposal.kind!r}；合法取值：{sorted(VALID_KINDS)}")

        if not proposal.agent_id:
            return ValidationResult(False, "no_agent", "agent_id 为空")

        if proposal.kind in NEEDS_TARGET and not proposal.target:
            return ValidationResult(
                False, "missing_target", f"{proposal.kind} 需要 target")

        # --- provenance：evidence 必须存在（§十一）-------------------
        if not proposal.evidence:
            return ValidationResult(
                False, "no_evidence",
                "没有 evidence —— 没有来源的认知修改不可追溯")
        if self.experience_store is not None:
            for eid in proposal.evidence:
                exp = self.experience_store.get(eid)
                if exp is None:
                    return ValidationResult(
                        False, "unknown_evidence",
                        f"evidence {eid!r} 不存在")
                # 🔴 §十二：evidence 必须属于**同一个 Agent**
                if exp.agent_id != proposal.agent_id:
                    return ValidationResult(
                        False, "evidence_not_owner",
                        f"evidence {eid!r} 属于 {exp.agent_id!r}，"
                        f"不是 {proposal.agent_id!r}")

        # --- ownership：只能改自己的（§十二）--------------------------
        owner = self._target_owner(proposal)
        if owner is not None and owner != proposal.agent_id:
            return ValidationResult(
                False, "not_owner",
                f"target {proposal.target!r} 属于 {owner!r}，"
                f"不能由 {proposal.agent_id!r} 修改")

        # --- operation-specific --------------------------------------
        return self._validate_change(proposal)

    def _target_owner(self, proposal: ReflectionProposal) -> Optional[str]:
        if not proposal.target:
            return None
        if proposal.kind in (KIND_REVISE_STRATEGY, KIND_ABANDON_STRATEGY):
            obj = self.store.get_strategy(proposal.target)
        elif proposal.kind in (KIND_REVISE_CLAIM, KIND_RETIRE_CLAIM):
            obj = self.store.get_claim(proposal.target)
        else:
            return None
        return getattr(obj, "agent_id", None) if obj is not None else None

    def _validate_change(self, proposal: ReflectionProposal) -> ValidationResult:
        change = proposal.proposed_change or {}

        if proposal.kind in (KIND_CREATE_STRATEGY, KIND_REVISE_STRATEGY):
            if proposal.kind == KIND_CREATE_STRATEGY:
                if not str(change.get("name") or "").strip():
                    return ValidationResult(
                        False, "missing_name", "create_strategy 需要 name")
            if "confidence" in change:
                ok, why = self._check_confidence(change["confidence"])
                if not ok:
                    return ValidationResult(False, "bad_confidence", why)

        if proposal.kind == KIND_CREATE_CLAIM:
            if not str(change.get("content") or "").strip():
                return ValidationResult(
                    False, "missing_content", "create_claim 需要 content")
            if "confidence" in change:
                ok, why = self._check_confidence(change["confidence"])
                if not ok:
                    return ValidationResult(False, "bad_confidence", why)

        if proposal.kind == KIND_REVISE_CLAIM and "confidence" in change:
            ok, why = self._check_confidence(change["confidence"])
            if not ok:
                return ValidationResult(False, "bad_confidence", why)

        # target 必须真的存在（revise / abandon / retire）
        if proposal.kind in NEEDS_TARGET:
            if proposal.kind in (KIND_REVISE_STRATEGY,
                                 KIND_ABANDON_STRATEGY):
                if self.store.get_strategy(proposal.target) is None:
                    return ValidationResult(
                        False, "target_not_found",
                        f"strategy {proposal.target!r} 不存在")
            elif self.store.get_claim(proposal.target) is None:
                return ValidationResult(
                    False, "target_not_found",
                    f"claim {proposal.target!r} 不存在")

        return ValidationResult(True, "ok", "")

    @staticmethod
    def _check_confidence(value: Any):
        """🔒 只校验**取值范围**，不校验「够不够高」。"""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return False, f"confidence 不是数字：{value!r}"
        if not (0.0 <= f <= 1.0):
            return False, (f"confidence 越界：{f}（合法范围 [0,1]）。"
                           f"⚠️ Runtime **不**自动修正它 —— 请 Agent 重新提议")
        return True, ""

    # ================================================================
    # 写入
    # ================================================================
    def apply(self, proposal: ReflectionProposal) -> WriteOutcome:
        """
        校验 + 写入。**校验失败就不写，且不改写 Proposal。**

        §二十二：拒绝就是拒绝，不自动修正。
        """
        validation = self.validate(proposal)
        if not validation.ok:
            # §二十一：可观测，但**不**记录 Proposal 原文
            logger.info(
                "Reflection Proposal 被拒绝 (agent_id=%s, kind=%s, "
                "target=%s, code=%s)",
                proposal.agent_id, proposal.kind, proposal.target,
                validation.code)
            return WriteOutcome(committed=False, validation=validation)

        result = self._commit(proposal)
        logger.info(
            "Reflection Proposal 已写入 (agent_id=%s, kind=%s, target=%s)",
            proposal.agent_id, proposal.kind, proposal.target)
        return WriteOutcome(committed=True, validation=validation,
                            result=result)

    def _commit(self, proposal: ReflectionProposal) -> Any:
        change = proposal.proposed_change or {}
        scope = _scope_from(proposal.scope)
        evidence = _evidence_from(proposal)
        now = utc_now_iso()

        if proposal.kind == KIND_CREATE_STRATEGY:
            return self.store.add_strategy(
                proposal.agent_id, str(change.get("name")).strip(),
                domain=scope.domain, situation=scope.situation,
                rationale=str(change.get("rationale") or proposal.reason or ""),
                confidence=_clamp_confidence(change.get("confidence", 0.5)),
                evidence=evidence,
            )

        if proposal.kind == KIND_REVISE_STRATEGY:
            kwargs: dict = {"evidence": evidence}
            if "confidence" in change:
                kwargs["confidence"] = change["confidence"]
            if "rationale" in change:
                kwargs["rationale"] = change["rationale"]
            if "name" in change:
                kwargs["name"] = change["name"]
            if scope.domain is not None:
                kwargs["domain"] = scope.domain
            if scope.situation is not None:
                kwargs["situation"] = scope.situation
            return self.store.revise_strategy(proposal.target, **kwargs)

        if proposal.kind == KIND_ABANDON_STRATEGY:
            return self.store.abandon_strategy(proposal.target)

        if proposal.kind == KIND_CREATE_CLAIM:
            return self.store.add_claim(
                proposal.agent_id, str(change.get("content")).strip(),
                kind=KIND_CLAIM, scope=scope,
                confidence=_clamp_confidence(change.get("confidence", 0.5)),
                evidence=evidence,
            )

        if proposal.kind == KIND_REVISE_CLAIM:
            kwargs = {"evidence": evidence}
            if "confidence" in change:
                kwargs["confidence"] = change["confidence"]
            if "content" in change:
                kwargs["content"] = change["content"]
            return self.store.revise_claim(proposal.target, **kwargs)

        if proposal.kind == KIND_RETIRE_CLAIM:
            return self.store.retire_claim(proposal.target)

        raise AssertionError(f"未处理的 kind：{proposal.kind}")   # pragma: no cover


def _scope_from(raw: Any) -> Scope:
    raw = raw or {}
    if not isinstance(raw, dict):
        return Scope()
    return Scope(world=raw.get("world"), domain=raw.get("domain"),
                 situation=raw.get("situation"))


def _evidence_from(proposal: ReflectionProposal) -> list:
    """
    把 proposal 的 evidence（experience_id 列表）变成 Self Model 的 Evidence。

    🔒 来源固定为 `reflection` —— 因为这条认知确实来自 Agent 的反思，
       而不是系统事实。
    """
    out = []
    for eid in proposal.evidence or []:
        out.append(Evidence(source="reflection",
                            detail=proposal.reason or "", ref=str(eid)))
    return out
