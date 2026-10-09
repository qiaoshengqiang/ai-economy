"""
把 Stage 渲染成 Cognitive Context 里的一段**状态信息**（§二十七 / §二十八）
============================================================

🔴 **语义纪律**

    ✅ 这是 Agent 的**当前世界状态 / 任务状态**：
         Stage Goal · Status · Deliverables · 当前 Checkpoint
         Session 墙钟上限 · 已用时间（**不是** Agent 的写作速度预算）

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
    rounds_left: Optional[int] = None,
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

    # 🔴 措辞修正（2026-10-10）：原先写 `Hard runtime limit: N minutes`。
    #
    #    实测：Agent 把它读成了**自己的写作时间预算** —— 它的思考原话是
    #        「The runtime limit is 4 minutes, elapsed 0」
    #    于是决定「**先赶紧把正文写出来**」。
    #
    #    🔒 但它**作用不了**那个数字：LLM 的产出在**一次调用内**完成
    #       （~10 秒），它没有任何办法"写快一点"。
    #
    #    🔒 这条限制的**真实含义**是「这段还能给你几个回合」——
    #       所以要说成那个意思，而不是"你还有几分钟"。
    #       给一个自己作用不了的约束，只会让它去**猜**该怎么应对。
    if limit_seconds or rounds_left is not None:
        parts = []
        if limit_seconds:
            parts.append(f"墙钟上限 {int(limit_seconds / 60)} 分钟")
            parts.append(f"已用 {int((elapsed_seconds or 0) / 60)} 分钟")
        if rounds_left is not None:
            parts.append(f"**最多再跑 {int(rounds_left)} 轮**")
        lines.append("Session: " + " · ".join(parts) + " · 到点后不再有新的回合")
        lines.append(
            "（它限制的是「还能给你几个回合」，不是「你写得多快」——"
            " 你的一次产出在一次调用内完成。"
            " 另外：你**只有在收到消息时**才会有下一个回合。）")
    return "\n".join(lines)
