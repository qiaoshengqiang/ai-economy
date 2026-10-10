"""
Agent 可发起的对齐操作 —— 读共同认知（Task Shared Context）
==============================================================

【🔴 它补的是哪一个缺口（用户 2026-10-10 指出）】

    Shared Context 有**变更通道**（`assign_acceptance` → `ASSIGN`），
    但**没有读通道**：

        Agent 能写下 objective / constraints / acceptance_criteria /
        decisions / assumptions / facts / assignments ——
        而它自己**一个字都读不到**。

    ⇒ 「Task 的共同认知」对唯一需要它的人不可见。

    🔒 这不是"新增能力"。`TaskAlignmentService.snapshot()` 早就写着
       自己是「生成一个**可消费的读取视图**」—— 只是**从来没有
       Agent 能够到它**。本模块把那条通道接上，与 `private_*` /
       `assign_acceptance` 同一类（能力在、通道没接）。

【🔒 读的就是那个设计好的视图，不另造一份投影】

    返回 `ContextSnapshot.to_dict()`，它含三块：

        context         共同认知本身（不可变的一版）
        domain_state    **现场从 Task Core 读**的状态引用
        recent_changes  最近的变更摘要

    §6.2 允许 Shared Context **引用** Domain State，**不允许复制**。
    这里完全遵守：`domain_state_source = "task_core"` 就是那个标记，
    Context 自己**不存** status / agreed_price / contractor。

【🔒 纯读 —— 不改任何状态】

    不 propose、不 accept、不碰 revision、不写任何表。
    失败也不会留下痕迹。

【🔴 `is_participant` 是必需的（fail-closed）】

    与 `assign_acceptance` 同一条纪律：**不是这条 Task 的人读不到
    它的共同认知**。不检查就会让任何一个 Agent 读到别的 Task 的
    内部讨论（objective / decisions / assumptions）——
    那是 §16 的边界。
"""

from __future__ import annotations

from typing import Any, Dict

#: 默认带几段最近变更。多了会让 ActionResult 变胖（见下方 ⚠️）。
DEFAULT_RECENT_CHANGE_LIMIT = 5


def _fail(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "code": code, "message": message}
    out.update(extra)
    return out


def read_shared_context(
    alignment: Any,
    task_id: str,
    *,
    by: Any,
    is_participant: Any = None,
    recent_change_limit: int = DEFAULT_RECENT_CHANGE_LIMIT,
) -> Dict[str, Any]:
    """
    读一个 Task 的**当前**共同认知。

    参数由组合根注入的形状决定：
        `alignment`   —— TaskAlignmentService（**不是** Shared Context 本身）
        `task_id`     —— 绑在组合根上（Agent 指定不了别的 Task）
        `by`          —— 读取方，**只取自 `intent.agent_id`**（不可伪造）
        `is_participant(task_id, agent_id) -> bool` —— 必需，fail-closed

    返回（与其它服务函数同形状）：
        成功 → {"ok": True, "task_id":…, "revision":…, "context":{…},
                "domain_state":{…}, "recent_changes":[…]}
        失败 → {"ok": False, "code":…, "message":…}

    ⚠️ **体积提醒（真实数据）**：一个几乎空的 Context 序列化后
       约 1.1 KB；写入 objective / constraints / decisions 之后会更大。
       而 `ExperienceStore.record_action_result()` 会把 `output`
       **原样全量**存进经历表（它只截 `reason`）。
       所以这个动作每被调用一次，经历库就多一份这个体积的副本。
       —— 这是既有机制的性质，本模块不在这里改它；
          要改应该改在 `record_action_result` 的截断策略上（全局决定）。
    """
    actor = str(by or "").strip()
    if not actor:
        return _fail("no_actor", "无法确定发起方（intent.agent_id 为空）")

    # ---- 🔴 必须是这条 Task 的人 ----
    if is_participant is None or not callable(is_participant):
        return _fail(
            "no_participant_checker",
            "缺少 is_participant 检查器 —— 拒绝在无法验证身份时读取"
            "（fail-closed：不检查就能读到别的 Task 的内部讨论）")
    if not is_participant(task_id, actor):
        return _fail(
            "not_participant",
            f"{actor} 不是这条 Task 的人 —— 读不到它的共同认知")

    # ---- 先确认容器在，给一个**清楚**的失败码 ----
    #     （不让调用方去看 ContextNotFoundError 的栈）
    try:
        current = alignment.current(task_id)
    except Exception as exc:                                  # noqa: BLE001
        return _fail("alignment_error", f"{type(exc).__name__}: {exc}")
    if current is None:
        return _fail(
            "no_shared_context",
            "这个 Task 还没有 Shared Context —— "
            "还没有任何人对它的目标 / 约束 / 验收标准留下共同认知")

    # ---- 读**刚刚确认过的**那一版 ----
    #     🔒 显式传 `revision`：这样即使在我们两次调用之间有人推进了
    #        revision，我们读到的仍然是刚才检查过的那一版，
    #        不会出现「检查的是 3、读回来的是 4」这种不透明行为。
    try:
        snap = alignment.snapshot(
            task_id, revision=current.revision,
            recent_change_limit=int(recent_change_limit),
        )
    except Exception as exc:                                  # noqa: BLE001
        return _fail("snapshot_failed", f"{type(exc).__name__}: {exc}")

    out = snap.to_dict()
    out["ok"] = True
    return out


__all__ = ["read_shared_context", "DEFAULT_RECENT_CHANGE_LIMIT"]
