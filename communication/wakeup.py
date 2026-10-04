"""
Wakeup —— 「有事值得看一眼」的信号
============================================================

【🔒 最重要的一条边界：Wakeup ≠ Message】

    Message 是**事实**（谁对谁说了什么，已经落库）。
    Wakeup 只是**提示**（「B 有新的工作值得检查」）。

    所以：
        ❌ 不把 wakeup 做成一种新的 MessageType
        ❌ 不把 wakeup 当成「消息已经处理了」的证据
        ✅ 消息仍然只能从信箱里取 —— wakeup 不携带内容

    【为什么这个区分是必要的】

        未来还会有 HEARTBEAT / TASK_EVENT / WORKSPACE_EVENT 都能「叫醒」
        一个 Agent。如果 wakeup 是消息，那 heartbeat 也得伪造一条消息 ——
        那就把「通信协议」和「调度信号」搅在一起了。
"""

from __future__ import annotations


class WakeupReason:
    """
    为什么被叫醒。

    本阶段**只实现 MESSAGE**（任务书 §7 明确要求不要提前实现其它类型）。
    其余常量先留名字，是为了让调用方从一开始就按
    「reason 是参数」而不是「只有一种情况」来写代码。
    """

    MESSAGE = "message"

    # ---- 以下**本阶段未实现**（只占位，不要用）----
    HEARTBEAT = "heartbeat"            # FUTURE
    TASK_EVENT = "task_event"          # FUTURE
    WORKSPACE_EVENT = "workspace_event"  # FUTURE
    SYSTEM_EVENT = "system_event"      # FUTURE

    @classmethod
    def implemented(cls) -> tuple:
        return (cls.MESSAGE,)
