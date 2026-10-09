"""
Agent 可发起的对齐操作 —— 指派验收责任（ASSIGN）
==================================================

【🔴 它补的是哪一个缺口】

`services/task_execution_service.py` 自己写着：

    谁负责验收由 **Agent 自己**通过现有的 **ASSIGN** 能力决定。

而**实测**：`ASSIGN` 是 TaskSharedContext 的变更类型，只有
`TaskAlignmentService` 的 API 可达 —— **没有消息通道**。
于是真实实验里，指派验收责任**只能由 runner 代做**，
「Agent 自主完成任务」这条链因此断在这里：

    Agent 能发 SUBMIT
        ↓
    但 Shared Context 里没有 ACCEPTANCE_RESPONSIBILITY ⇒
    `acceptors_of()` 返回空集 ⇒ **谁都无法 VERIFY**（fail-closed）

🔒 **所以这不是"新增能力"，是把设计已经声明的能力接上** ——
   与 `private_*` 同一类（能力在、通道没接）。

【🔒 为什么 ASSIGN 可以自己批自己】

它属于**声明类**（FACT / ASSUME / QUESTION / ASSIGN）——
Constitution §8.2「Said ≠ Confirmed」约束的是 **DECIDE 类**
（一个 Agent 自己说了不算共同决定）。
「我指定你负责验收」是个人声明，不需要别人附议。
**依据**：`services/task_alignment_service.py` 模块开头的原文。

【⚠️ 如实记下的两条语义】

① **被指派的人不能拒绝。**
   `AssignmentStatus` 只有 `ACTIVE / DONE / RELEASED / BLOCKED` ——
   **没有 PENDING** ⇒ 指派**立即生效**，当前没有"拒收"通道。

② **不许把验收责任派给自己。**
   执行服务在 VERIFY 时会拒「生产者自验」（`task_execution_service`
   「🔴 生产者不得自验（优先于集合判断）」），但那只在 VERIFY 那一刻
   生效。这里**提前**拒，理由更清楚，也避免出现
   「某人对自己的任务持有验收责任」这种糊涂状态。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.task_shared_context import ACCEPTANCE_RESPONSIBILITY


def _fail(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "code": code, "message": message}
    out.update(extra)
    return out


def assign_acceptance(alignment: Any, task_id: str, *,
                      by: Any, to: Any, reason: str = "",
                      is_participant: Any = None) -> Dict[str, Any]:
    """
    把一个 Task 的**验收责任**指派给某个 Agent。

    参数由组合根注入的形状决定：
        `alignment`  —— TaskAlignmentService（**不是** Shared Context 本身）
        `task_id`    —— 绑在组合根上
        `by`         —— 发起方，**只取自 `intent.agent_id`**（不可伪造）
        `to`         —— 接收方

    返回（与其它服务函数同形状）：
        成功 → {"ok": True,  "task_id":…, "to":…, "change_id":…}
        失败 → {"ok": False, "code":…, "message":…}

    🔴 `is_participant(task_id, agent_id) -> bool` 是**必需**的
       （fail-closed）。第一版没有它，于是

           assign_acceptance(to="not_a_real_agent")

       **成功了** —— 后果不是报错，而是**静默停住**：
       `acceptors_of()` 返回一个幽灵，谁都 VERIFY 不了。
       与 `handoff` 已堵过的是同一类洞。
    """
    target = str(to or "").strip()
    actor = str(by or "").strip()
    if not actor:
        return _fail("no_actor", "无法确定发起方（intent.agent_id 为空）")
    if not target:
        return _fail("bad_argument", "to 不能为空（要指派给谁）")
    if target == actor:
        return _fail(
            "cannot_assign_to_self",
            "不能把**验收责任**派给自己 —— "
            "那等于自验；执行服务在 VERIFY 时也会拒（生产者不得自验）")

    # ---- 🔴 双方都必须是**真实存在、且属于这条 Task** 的人 ----
    #    不检查的后果不是报错，而是**静默停住**（幽灵验收人）。
    if is_participant is None or not callable(is_participant):
        return _fail(
            "no_participant_checker",
            "缺少 is_participant 检查器 —— 拒绝在无法验证身份时指派"
            "（fail-closed：不检查就会造出幽灵验收人）")
    if not is_participant(task_id, actor):
        return _fail("not_participant", f"{actor} 不是这条 Task 的人")
    if not is_participant(task_id, target):
        return _fail(
            "unknown_agent",
            f"{target} 不是这条 Task 的人 —— 指派给他会造出一个"
            f"**幽灵验收人**：acceptors_of() 会返回他，但他永远不会来验收")

    # ---- 必须有 Shared Context：验收责任人写在它里面 ----
    try:
        context = alignment.current(task_id)
    except Exception as exc:                                  # noqa: BLE001
        return _fail("alignment_error", f"{type(exc).__name__}: {exc}")
    if context is None:
        return _fail(
            "no_shared_context",
            "这个 Task 还没有 Shared Context —— "
            "验收责任是 Shared Context 的一份 AssignmentRecord，"
            "所以得先有人建立上下文（objective / acceptance_criteria）")

    try:
        change = alignment.propose(
            task_id,
            proposed_by=actor,
            change_type="ASSIGN",
            new_value={"agent_id": target,
                       "responsibility": ACCEPTANCE_RESPONSIBILITY},
            # 🔒 乐观锁：用**刚刚读到的** revision。
            #    期间被别人推进了 ⇒ propose 会冲突 ⇒ 如实失败，不重试。
            expected_revision=context.revision,
            reason=str(reason or f"{actor} 把验收责任指派给 {target}"),
        )
    except Exception as exc:                                  # noqa: BLE001
        return _fail("propose_failed", f"{type(exc).__name__}: {exc}")

    change_id = getattr(change, "change_id", None)
    try:
        # 🔒 声明类允许自批 —— 这是**协议允许**的，不是绕过。
        alignment.accept(task_id, change_id, decided_by=actor,
                         reason="ASSIGN 属于声明类，规则允许自批")
    except Exception as exc:                                  # noqa: BLE001
        return _fail("accept_failed", f"{type(exc).__name__}: {exc}",
                     change_id=change_id)

    return {"ok": True, "task_id": task_id, "to": target,
            "responsibility": ACCEPTANCE_RESPONSIBILITY,
            "change_id": change_id}


def revoke_acceptance(alignment: Any, task_id: str, *,
                      by: Any, target: Any, reason: str = ""
                      ) -> Dict[str, Any]:
    """
    撤回某个 Agent 的验收责任。

    🔒 用 `ASSIGN` + `status = RELEASED` 表达（**不新造变更类型**）——
       为什么复用：`AssignmentRecord` 已经有 `status`，
       而 §14「Release ≠ Handoff」的精神是"放掉独立责任"。

    ⚠️ 当前**没有**在实验里接这个动作；先作为能力放在这里，
       等真实需要时再暴露成 Action（避免提前给 Agent 一堆用不上的动作）。
    """
    target_id = str(target or "").strip()
    actor = str(by or "").strip()
    if not actor or not target_id:
        return _fail("bad_argument", "by 与 target 都不能为空")
    context = alignment.current(task_id)
    if context is None:
        return _fail("no_shared_context", "这个 Task 还没有 Shared Context")
    try:
        change = alignment.propose(
            task_id, proposed_by=actor, change_type="ASSIGN",
            new_value={"agent_id": target_id,
                       "responsibility": ACCEPTANCE_RESPONSIBILITY,
                       "status": "RELEASED"},
            expected_revision=context.revision,
            reason=str(reason or f"{actor} 撤回 {target_id} 的验收责任"),
        )
        alignment.accept(task_id, getattr(change, "change_id", None),
                         decided_by=actor, reason="声明类，允许自批")
    except Exception as exc:                                  # noqa: BLE001
        return _fail("revoke_failed", f"{type(exc).__name__}: {exc}")
    return {"ok": True, "task_id": task_id, "released": target_id}
