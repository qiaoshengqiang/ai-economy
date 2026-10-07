"""
把 Stage 渲染成 Cognitive Context 里的一段**状态信息**（§二十七 / §二十八）
============================================================

🔴 **语义纪律**

    ✅ 这是 Agent 的**当前世界状态 / 任务状态**：
         Stage Goal · Status · Deliverables · 当前 Checkpoint
         Hard runtime limit · 本次 Session 已用时间

    ❌ **不是**「你现在必须做 X」
    ❌ 不写 "you must" / "you should"（有守卫测试扫）

    Agent 据此可以**自己**判断：继续 / 停止 / 提交 checkpoint。
    但 Runtime 仍然自己执行 Hard Limit ——
    **Agent 说「我还需要 20 分钟」不会延长 90 分钟**（§二十八）。
"""

from __future__ import annotations

from typing import Any, List, Optional

from stage.models import STATUS_WAITING


def render_stage(
    stage: Any,
    *,
    checkpoint: Optional[Any] = None,
    limit_seconds: Optional[float] = None,
    elapsed_seconds: Optional[float] = None,
) -> str:
    """
    渲染 Stage 状态。**只陈述状态，不给指令。**

    🔒 完全自包含：不查库、不依赖 Runtime。
    """
    if stage is None:
        return ""

    lines: List[str] = []
    lines.append(f"Stage: {stage.stage_id}")
    lines.append(f"Goal: {stage.goal}")
    lines.append(f"Status: {stage.status}")
    if stage.status == STATUS_WAITING and stage.waiting_for:
        # 🔒 「为什么在等」是信息（§九），不是状态名
        lines.append(f"Waiting for: {stage.waiting_for}")

    deliverables = list(getattr(stage, "deliverables", []) or [])
    if deliverables:
        names = [str(d.get("name")) for d in deliverables
                 if isinstance(d, dict) and d.get("name")]
        if names:
            lines.append(f"Deliverables: {', '.join(names)}")

    if checkpoint is not None:
        rev = getattr(checkpoint, "revision", "") or ""
        ws = getattr(checkpoint, "workspace_id", "") or ""
        line = f"Current checkpoint: {checkpoint.checkpoint_id}"
        if ws:
            line += f" (workspace={ws}"
            line += f", revision={rev})" if rev else ")"
        lines.append(line)

    if limit_seconds:
        lines.append(f"Hard runtime limit: {int(limit_seconds / 60)} minutes")
        if elapsed_seconds is not None:
            lines.append(
                f"Current session elapsed: {int(elapsed_seconds / 60)} minutes"
            )
    return "\n".join(lines)
