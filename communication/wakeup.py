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

    【为什么连「预留常量」都没有】

        第一版我在这里留了 HEARTBEAT / TASK_EVENT / WORKSPACE_EVENT /
        SYSTEM_EVENT 四个占位常量，想着「先留名字，以后好扩展」。

        结果 `tests/test_multiprocess_guards.py::TestGuard8NoHeartbeat`
        立刻红了 —— 它禁止 `communication/` 里出现 "heartbeat" 这个词。

        我一开始觉得是守卫太粗，但**重读之后发现守卫是对的**：
        项目有一条 `FORBIDDEN NOW`：不得提前实现 Heartbeat。
        而 §7 原话也是「**不要**把未来所有类型都实现」——
        **预留常量本身就是一种提前实现**（它让未来看起来已经开始）。

        所以正确的做法不是我改守卫，而是**撤掉这些常量**。
        将来真要加 Heartbeat，那时再把它加进来 —— 那时它才是一个真实需要。
    """

    MESSAGE = "message"

    @classmethod
    def implemented(cls) -> tuple:
        return (cls.MESSAGE,)
