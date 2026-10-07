"""
Agent Stop Judgment —— **必须是 Agent Action**（§十四–§十八）
============================================================

【🔴 Decision Owner = Agent】

    Agent 产生：
        continue / stop / pause / submit_stage / request_feedback
    Runtime **只负责执行**。

    ❌ 禁止（§十六）：
        if progress > 80: stop()
        if chapter_count >= target: stop()

    Stage Goal 是**结构**，Stop Judgment 是**行为**（§十七）。

【🔴 submit_stage ≠ 自动完成（§十八）】

    Agent 说「我完成了」**不等于** Stage COMPLETED。
    必须经过 Deliverable / Validation 才能进 COMPLETED。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from stage.models import (
    REASON_AGENT_PAUSE, REASON_AGENT_STOP, REASON_SUBMIT,
    STATUS_ACTIVE, STATUS_COMPLETED, STATUS_WAITING,
    WAITING_FOR_AGENT_DECISION, WAITING_FOR_FEEDBACK,
    WAITING_FOR_HUMAN_INPUT,
)
from stage.store import StageStore

#: Agent 可以表达的 Stage 决策
DECISION_CONTINUE = "continue"
DECISION_STOP = "stop"
DECISION_PAUSE = "pause"
DECISION_SUBMIT_STAGE = "submit_stage"
DECISION_REQUEST_FEEDBACK = "request_feedback"
VALID_DECISIONS = {DECISION_CONTINUE, DECISION_STOP, DECISION_PAUSE,
                   DECISION_SUBMIT_STAGE, DECISION_REQUEST_FEEDBACK}


class StageControlExecutor:
    """
    执行 `action_type = "stage_control"` 的 ActionIntent。

    🔒 它**不决定** Agent 该不该停 —— 它只执行 Agent 已经做出的决定。
       这保证 `Decision Owner = Agent`（§十五）。
    """

    action_types = frozenset({"stage_control"})

    def __init__(self, store: StageStore, *,
                 validate_deliverables: Optional[Callable] = None) -> None:
        self.store = store
        #: 交付物校验器：(stage, artifacts) -> (ok, message)
        #: 🔒 默认校验器**只检查结构**（deliverables 非空），
        #:    不做业务判断 —— 业务判断属于 World / Acceptance。
        self.validate_deliverables = (
            validate_deliverables or _default_validate)

    def execute(self, intent: Any) -> Any:
        from actions.intent import ActionResult

        params = dict(getattr(intent, "parameters", {}) or {})
        decision = str(params.get("decision") or "").strip()
        stage_id = str(params.get("stage_id") or "").strip()
        reason = str(params.get("reason") or "")
        artifacts = list(params.get("artifacts") or [])

        if decision not in VALID_DECISIONS:
            return ActionResult(
                success=False,
                error=f"未知 stage decision {decision!r}；"
                      f"合法：{sorted(VALID_DECISIONS)}",
                action_id=intent.intent_id, action_type=intent.action_type)
        if not stage_id:
            return ActionResult(
                success=False, error="stage_control 需要 stage_id",
                action_id=intent.intent_id, action_type=intent.action_type)
        stage = self.store.get_stage(stage_id)
        if stage is None:
            return ActionResult(
                success=False, error=f"stage {stage_id!r} 不存在",
                action_id=intent.intent_id, action_type=intent.action_type)

        try:
            out = self._apply(decision, stage, reason, artifacts)
        except Exception as exc:                          # noqa: BLE001
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id, action_type=intent.action_type)

        ok, message = out
        return ActionResult(
            success=ok, output={"decision": decision, "stage_id": stage_id,
                                "message": message},
            error=None if ok else message,
            action_id=intent.intent_id, action_type=intent.action_type)

    def _apply(self, decision: str, stage: Any, reason: str,
               artifacts: List[Any]):
        if decision == DECISION_CONTINUE:
            if stage.status == STATUS_WAITING:
                self.store.resume(stage.stage_id)
            return True, "继续生产"

        if decision == DECISION_STOP:
            # 🔴 §十三：Agent 主动停止 → 它**自己的**决定，不是 Hard Limit。
            #    Stage 仍 ACTIVE（Session 停了，Stage 没停）。
            self.store.save_checkpoint(
                stage.stage_id, reason=REASON_AGENT_STOP,
                metadata={"agent_reason": reason})
            return True, "Agent 主动停止，已保存 Checkpoint"

        if decision == DECISION_PAUSE:
            self.store.save_checkpoint(
                stage.stage_id, reason=REASON_AGENT_PAUSE,
                metadata={"agent_reason": reason})
            self.store.wait(stage.stage_id, WAITING_FOR_AGENT_DECISION)
            return True, "Stage 已暂停（WAITING）"

        if decision == DECISION_REQUEST_FEEDBACK:
            self.store.save_checkpoint(
                stage.stage_id, reason=REASON_AGENT_PAUSE,
                metadata={"agent_reason": reason})
            self.store.wait(stage.stage_id, WAITING_FOR_FEEDBACK)
            return True, "Stage 等待外部反馈（WAITING）"

        if decision == DECISION_SUBMIT_STAGE:
            # 🔴 §十八：Agent 说 done ≠ Stage completed。必须过校验。
            ok, message = self.validate_deliverables(stage, artifacts)
            self.store.save_checkpoint(
                stage.stage_id, reason=REASON_SUBMIT,
                progress={"artifacts": artifacts},
                metadata={"agent_reason": reason,
                          "validation_ok": ok, "validation": message})
            if not ok:
                return False, f"submit_stage 校验未通过：{message}"
            self.store.complete(stage.stage_id)
            return True, "Stage 已完成（通过交付物校验）"

        raise AssertionError(decision)                     # pragma: no cover


def _default_validate(stage: Any, artifacts: List[Any]):
    """
    默认交付物校验：**只检查结构**，不做业务判断。

    🔒 理由：业务口径属于 World / Acceptance，不属于 Stage 系统。
       这里只回答「Agent 是否真的给了交付物」。
    """
    expected = list(getattr(stage, "deliverables", []) or [])
    if not artifacts:
        return False, "没有提供任何交付物（artifacts 为空）"
    if expected:
        need = {str(d.get("name")) for d in expected
                if isinstance(d, dict) and d.get("name")}
        got = {str(a.get("name")) for a in artifacts
               if isinstance(a, dict) and a.get("name")}
        missing = need - got
        if missing:
            return False, f"缺少交付物：{sorted(missing)}"
    return True, "交付物结构检查通过"
