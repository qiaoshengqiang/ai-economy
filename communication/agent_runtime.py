"""
AgentRuntime —— 让 Agent 能「睡着等消息」而不是被外面轮询
============================================================

【为什么这个文件在 communication/ 而不是 agents/】

    它**驱动的是通信层**：等 wakeup 信号、从信箱取件。
    而 `agents/` 有一条明确的分层规矩（有守卫盯着）：
    Agent **不 import communication** —— 它只持有
    `attach_communication()` 注入进来的对象，靠鸭子类型调用。

    第一版放在 `agents/agent_runtime.py`，守卫立刻红了。
    那不是守卫太严，而是**放错了层**：这东西本来就属于通信侧。

【它解决的真实问题】

    在此之前，Agent B 收到消息的唯一方式是**外面有人显式调用**：

        await manager.receive("agent_b", timeout=1.0)

    也就是说 B 自己**不会醒**。CLI 命令或测试代码必须主动去问它。

    现在：

        消息投递 → wakeup 信号 → B 的 Runtime 醒来 → 自己 drain 信箱

【🔒 AgentRuntime ≠ Communication】

    它只做：
        睡 → 醒 → 看一眼信箱 → 把消息交给 Agent → 继续睡

    它**不**决定「这条消息该怎么处理」 —— 那是 Agent 的
    `handle_message()`（可由调用方替换）。
    §6 明确要求**不要**把它写死成 `on_message() → brain.generate()`，
    否则以后 Heartbeat / TaskEvent 就没法统一走同一条路。

【状态机】

    SLEEPING ──wakeup──> RUNNING ──drain+handle──> SLEEPING
                            │
                        （处理中）

    另有 WAITING（正在等下一次唤醒）与 THINKING（留给未来的自主决策）。
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from communication.wakeup import WakeupReason
from core.logging_setup import get_logger

logger = get_logger(__name__)


class RuntimeState:
    """Runtime 的状态。字符串常量，方便打印和测试。"""

    SLEEPING = "sleeping"     # 睡着，等 wakeup
    WAITING = "waiting"       # 正在等（等的那一刻）
    RUNNING = "running"       # 被叫醒，正在 drain + 处理
    THINKING = "thinking"     # 留给未来的自主决策（本阶段不用）
    STOPPED = "stopped"       # 已停止


class RuntimeAlreadyActive(RuntimeError):
    """同一个 Agent 已经有 runtime 在处理了（§10）。"""


class AgentRuntime:
    """
    一个 Agent 的**唯一**处理循环。

    用法：

        rt = AgentRuntime(agent=agent_b, communication=comm)
        task = asyncio.create_task(rt.run_forever())

        # 它在没人调用 receive() 的情况下，自己等到消息并处理
        ...
        rt.stop()
        await task
    """

    def __init__(
        self,
        *,
        agent: Any,
        communication: Any,
        poll_timeout: Optional[float] = None,
    ) -> None:
        if agent is None or communication is None:
            raise ValueError("agent 与 communication 都是必需依赖")

        self.agent = agent
        self.communication = communication
        self.agent_id = str(agent.agent_id)

        #: 等下一次唤醒的超时。None = 一直等（推荐）。
        self.poll_timeout = poll_timeout

        self._state = RuntimeState.SLEEPING
        self._stopped = False

        # 🔒 停止信号**必须独立于 wakeup**。
        #
        #    【实测抓到的挂死】：第一版 stop() 只调 signal_wakeup()。
        #    如果那一刻循环正好在 `_drain()` 里，drain 开头的 clear
        #    会把这个信号吃掉 —— 循环回到 next_wakeup() 就**永远等下去**
        #    （timeout=None），`_stopped` 再也检查不到。测试直接挂住。
        #
        #    独立的 stop Event 让「我要停」这件事**持久有效**，
        #    不会被任何一次 clear 抹掉。
        self._stop_event = asyncio.Event()

        # 🔒 §10：**同一个 Agent 同一时刻只能有一个**处理循环。
        #
        #    ⚠️ 第一版把锁放在**实例**上 —— 实测这是个真实缺陷：
        #       两个 AgentRuntime 实例服务同一个 Agent 时，
        #       各自看到「我的锁没被占」，于是**都**开始处理 →
        #       重复回复 / 重复花钱，而且第二个实例会一直等下去（挂死）。
        #
        #    现在把登记放到 **CommunicationManager** 上（按 agent_id 分键）：
        #        · 同一个 Agent 只能有一个 → 第二个直接 RuntimeAlreadyActive
        #        · 不同 Agent 互不影响         → 不是全局锁（§18.4）
        self._processing = asyncio.Lock()

        # ---- 可观测（测试用；不参与判断逻辑）----
        self.wakeup_count = 0
        self.processed_count = 0
        self.last_reason: Optional[str] = None
        self.processed_messages: List[Any] = []

    # ------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def is_sleeping(self) -> bool:
        return self._state in (RuntimeState.SLEEPING, RuntimeState.WAITING)

    @property
    def is_busy(self) -> bool:
        """
        当前是否有处理循环在跑（§10 的判据）。

        ⚠️ 判据是 **manager 上按 Agent 的登记**，不是实例自己的锁 ——
           这样「另一个实例正在跑」也能被看见。
        """
        try:
            if self.communication.has_active_runtime(self.agent_id):
                return True
        except AttributeError:                    # pragma: no cover - 兼容
            pass
        return self._processing.locked()

    # ------------------------------------------------------------
    # 等唤醒
    # ------------------------------------------------------------
    async def next_wakeup(self, timeout: Optional[float] = None) -> bool:
        """
        等这个 Agent 被唤醒。

        返回 True = 被唤醒；False = 超时。

        🔒 它**只等信号，不取消息** —— 取消息是 `drain()` 的事。
           这样「信号」和「事实」始终分开（§3）。
        """
        self._state = RuntimeState.WAITING

        wake = self.communication.transport.wakeup_event(self.agent_id)
        effective = self.poll_timeout if timeout is None else timeout

        # 🔒 同时等「被唤醒」和「被要求停止」——
        #    两者都可能先发生，谁先来就按谁来（不用定时器猜）。
        waiters = {asyncio.ensure_future(wake.wait()),
                   asyncio.ensure_future(self._stop_event.wait())}
        try:
            if effective is not None:
                done, pending = await asyncio.wait(
                    waiters, timeout=effective,
                    return_when=asyncio.FIRST_COMPLETED)
            else:
                done, pending = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in waiters:
                fut.cancel()

        if self._stopped:
            self._state = RuntimeState.SLEEPING
            return False

        if not done:
            self._state = RuntimeState.SLEEPING
            return False

        self.wakeup_count += 1
        self.last_reason = WakeupReason.MESSAGE
        return True

    # ------------------------------------------------------------
    # 处理一轮
    # ------------------------------------------------------------
    async def run_once(self, timeout: Optional[float] = None) -> List[Any]:
        """
        一轮完整处理：等唤醒 → drain → 处理 → 回去睡。

        返回这一轮处理掉的消息（可能有多条 —— §9 允许一次唤醒处理多条）。

        ⚠️ 如果当前已经有循环在跑，**直接抛错**而不是排队 ——
           排队会制造「两个循环同时在处理同一个 Agent」的假象。
        """
        if not self.communication.try_acquire_runtime(self.agent_id):
            raise RuntimeAlreadyActive(
                f"{self.agent_id} 已经有一个 runtime 在处理了"
            )

        async with self._processing:
            try:
                if not await self.next_wakeup(timeout):
                    return []
                if self._stopped:
                    return []

                self._state = RuntimeState.RUNNING
                return await self._drain()
            finally:
                self._state = RuntimeState.SLEEPING
                self.communication.release_runtime(self.agent_id)

    async def _drain(self) -> List[Any]:
        """
        把信箱取空，并交给 Agent 处理。

        🔒 **顺序是这套机制里最容易写错的地方**：

            1. 先 **clear** wakeup
            2. 再 drain 信箱

        为什么不能反过来（drain → clear）：
            在「drain 完」到「clear」之间到达的消息会把 wakeup 置位，
            然后被我们 clear 掉 —— **那条消息再也没人叫醒它了**。

        为什么 clear-first 是安全的：
            清掉之后新到的消息会**重新置位** → 下一轮一定能看到。
        """
        messages: List[Any] = []

        self.communication.clear_wakeup(self.agent_id)

        while self.communication.pending(self.agent_id) > 0:
            inbound = await self.communication.receive(self.agent_id,
                                                       timeout=0.01)
            if not inbound.delivered:
                break
            messages.append(inbound.message)

        # ============================================================
        # 🔒 **这里不要调 `agent.handle_message()`。**
        # ============================================================
        #
        #    `CommunicationManager.receive()` **已经**在取件之后
        #    自动分发到处理函数了：
        #
        #        handler = self._handlers.get(key)   # ← register() 时塞进去的
        #                                               就是 agent.handle_message
        #        outcome = handler(message)
        #
        #    「实测抓到的 bug」：第一版在这里又调了一次 handle_message，
        #    于是同一条消息被处理了**两遍** ——
        #    Demo 里 B 的 handler 被调用了 2 次。
        #
        #    在真实系统里这会变成：重复回复 / 重复副作用 / 重复花钱。
        #
        #    ✅ 所以 Runtime 的职责边界是：
        #        叫醒 + 把信箱取空
        #       **不**包含「怎么处理」—— 那是 Agent 的处理函数（§6）。
        #       这样 Heartbeat / TaskEvent 将来也只能通过同一个处理函数进来。
        self.processed_count += len(messages)
        self.processed_messages.extend(messages)

        return messages

    # ------------------------------------------------------------
    # 长期循环
    # ------------------------------------------------------------
    async def run_forever(self) -> None:
        """
        一直「睡 → 醒 → 处理」，直到 `stop()`。

        🔒 一个 Agent 只应该有**一个**这样的循环（§10）。
        """
        if not self.communication.try_acquire_runtime(self.agent_id):
            raise RuntimeAlreadyActive(
                f"{self.agent_id} 已经有一个 runtime 在处理了"
            )

        async with self._processing:
            try:
                await self._run_loop()
            finally:
                self.communication.release_runtime(self.agent_id)

    async def _run_loop(self) -> None:
        while not self._stopped:
            self._state = RuntimeState.SLEEPING
            if not await self.next_wakeup():
                if self._stopped:
                    break
                continue

            self._state = RuntimeState.RUNNING
            await self._drain()

        self._state = RuntimeState.STOPPED

    def stop(self) -> None:
        """让循环退出（当前这一轮结束后，或立刻从等待中醒）。"""
        self._stopped = True
        # 🔒 **先置 stop Event**，再顺手 signal —— 顺序不能反：
        #    stop Event 是持久的，signal 可能被 drain 的 clear 吃掉。
        self._stop_event.set()
        try:
            self.communication.transport.signal_wakeup(self.agent_id)
        except Exception:                              # pragma: no cover
            pass

    def __repr__(self) -> str:
        return (f"AgentRuntime(agent_id={self.agent_id!r}, "
                f"state={self._state!r}, wakeups={self.wakeup_count}, "
                f"processed={self.processed_count})")
