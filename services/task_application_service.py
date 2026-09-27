"""
Task Application Service —— 消息驱动应用服务的共同部分
============================================================

【为什么需要这个基类】

    Step 4D 引入了第二个消息驱动的应用服务：

        TaskNegotiationService  议价（OFFER / COUNTER / ACCEPT / REJECT）
        TaskExecutionService    执行（START / SUBMIT / VERIFY / COMPLETE）

    它们共享三件事：

        · trace 缝合        把任务的 current_trace_id 对齐到消息的 trace
        · 业务幂等          同一条 message_id 不重复处理
        · 协议违规回复      消息本身不合法时回 ERROR（而不是改任务状态）

    【为什么不能各写一份】

        这三件事**一旦有两份实现就会漂移**。
        比如某天幂等的判断依据从 message_id 改成别的，
        只改一边就会出现「议价去重了、执行没去重」这种极难查的 bug。

        重构时的原则里明确写了要避免复制：
            price validation / role validation / task state validation
            / offer persistence 不能出现多个互相漂移的实现。

        所以共同部分上提到这里，两个服务都继承它。

【为什么不是「过度抽象」】

    判据是：**有没有第二个真实的变化方向**。

        ❌ 为一个函数造 IFactory / IResolver / IProvider
           —— 没有第二份实现，纯粹是噪音

        ✅ 两个真实的服务共享三个真实的方法
           —— 复制它们一定会漂移

    这里属于后者。

【它刻意不知道什么】

    ❌ 不知道议价规则（价格、轮次、预算）—— 那是 TaskNegotiationService
    ❌ 不知道执行规则（谁能开工、什么状态能提交）—— 那是 TaskExecutionService
    ❌ 不知道 Handlers —— 方向是 handlers → service，反过来会成环
    ❌ 不碰 sqlite —— 那是 TaskStore

    它只知道：一条消息、一个任务、以及「怎么把消息和任务对起来」。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.logging_setup import get_logger
from core.task import Task
from core.task_store import TaskStore

from communication.message import Message, MessageType

from services.task_message_mapper import TaskMessageMapper
from services.task_result import HandlerAction, HandlerResult

logger = get_logger(__name__)


class TaskApplicationService:
    """
    消息驱动的应用服务的基类。

    子类负责：
        · 自己的业务规则（校验）
        · 自己的状态落地
        · 自己的幂等查找（already_recorded）

    基类负责：
        · 持有 store / mapper
        · trace 缝合
        · 统一的幂等响应
        · 统一的协议违规响应
    """

    def __init__(
        self,
        store: TaskStore,
        mapper: Optional[TaskMessageMapper] = None,
    ):
        self.store = store
        self.mapper = mapper or TaskMessageMapper()

    # ============================================================
    # 一、trace 缝合
    # ============================================================
    def sync_trace(self, task: Task, message: Message) -> bool:
        """
        把任务的 current_trace_id 对齐到消息的 trace_id。

        【为什么需要这个】
            Step 4A 里 task_id 和 trace_id 是**分开**的：
                task_id          任务的稳定身份
                current_trace_id 这一次执行的链路

            收到消息时，消息带的是「这一次执行」的 trace。
            把它同步过来，两个系统就缝合了：
                任务 → 报价 / 执行事件 → message_id → 通信日志
            这条链上的 trace_id 是同一个。

        【换 trace 是正常的】
            重试意味着新的一次执行、新的 trace，但 task_id 不变 ——
            这正是两个字段分开的目的。
            VERIFY(false) 之后的返工如果换了新的执行链，
            这里也会自然地把新 trace 接上，不需要发明 revision_id。

        返回是否真的发生了同步。
        """
        if task.current_trace_id == message.trace_id:
            return False

        previous = task.current_trace_id
        task.current_trace_id = message.trace_id
        logger.debug(
            "任务 %s 的 trace 从 %s 切换到 %s（收到新执行的 %s）",
            task.task_id, previous, message.trace_id, message.message_type,
        )
        return True

    # ============================================================
    # 二、业务幂等
    # ============================================================
    def already_recorded(self, task: Task, message: Message) -> bool:
        """
        这条消息之前处理过了吗？

        【子类必须实现】
            因为「记录在哪里」取决于业务：
                议价 → 任务的报价历史里
                执行 → task_executions 表里

        【为什么幂等这道门必须排在角色 / 状态校验之前】

            同一台机器上（单进程演示）两边共用一个 store：
                发送侧：我决定还价 / 提交 → 先记进历史 → 再发消息
                接收侧：收到那条消息 → 又想记一次

            等接收侧处理时，任务的**最后一条记录已经是发送者自己发的**了。
            如果先跑轮次 / 状态校验，就会误判成
            「他自己回自己」或「状态不对」—— 而那其实是同一条消息。

            在真实的多机部署里这条检查同样必要：
            消息可能因为重发而到达两次。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 必须实现 already_recorded()"
        )

    @staticmethod
    def already_processed(
        task: Task, message: Message, note: str = ""
    ) -> HandlerResult:
        """
        这条消息之前已经处理过了 —— 幂等返回，不改任何状态。

        【它不是错误】
            重复投递在分布式系统里是常态（重发、重试、网络抖动）。
            正确的反应是「认出来，什么也不做」，而不是报错。

        【本阶段不实现可靠通信】
            这只是**业务处理幂等** ——
            没有 retry、没有 ACK、没有队列。
        """
        logger.debug(
            "任务 %s 已经处理过消息 %s，跳过",
            task.task_id, message.message_id,
        )
        return HandlerResult(
            ok=True,
            action=HandlerAction.ALREADY_PROCESSED,
            task_id=task.task_id,
            reply_type=None,
            reply_payload=None,
            details={
                "status": task.status,
                "message_id": message.message_id,
                "message_type": message.message_type,
                "note": note or "这条消息之前已经记录过（幂等，未重复处理）",
            },
        )

    # ============================================================
    # 三、协议违规
    # ============================================================
    def reject_message(
        self,
        message: Message,
        errors,
        warnings=(),
        task_id: Optional[str] = None,
    ) -> HandlerResult:
        """
        **这条消息本身不合法**（协议违规 / 业务硬约束不允许）。

        【它和「业务拒绝」的区别 —— 这个区别很重要】

            业务拒绝（一条 REJECT 消息）：任务 → REJECTED ✅
                含义：「我理解这个请求，但我不愿意承接。」

            协议违规（本方法）：任务状态**不变**，回 ERROR
                含义：「你这条消息本身不成立。」

            把两者混成一回事，就再也分不清
            「对方不想干」和「对方发错了」。

        对 4D 的 START / SUBMIT / VERIFY / COMPLETE 来说，
        **所有**校验失败都走这里 —— 它们没有对应的「业务拒绝」语义。
        （比如「不是承接方却发 START」不是一种拒绝，而是一条无效消息。）
        """
        logger.warning(
            "拒绝一条 %s（来自 %s）：%s",
            message.message_type, message.from_agent, "; ".join(errors),
        )

        return HandlerResult(
            ok=False,
            action=HandlerAction.REJECTED,
            task_id=task_id,
            errors=tuple(errors),
            warnings=tuple(warnings),
            reply_type=MessageType.ERROR,
            reply_payload=self.mapper.error_reply_payload(
                message, task_id, errors
            ),
            details={
                "message_id": message.message_id,
                "message_type": message.message_type,
                "from_agent": message.from_agent,
            },
        )

    # ============================================================
    # 四、小工具
    # ============================================================
    def find_task(self, task_id: Optional[str]) -> Optional[Task]:
        """按 task_id 取任务（没有就返回 None）。"""
        return self.store.get(task_id) if task_id else None

    def load_task(
        self, message: Message, task_id: Optional[str]
    ) -> Tuple[Optional[Task], Optional[HandlerResult]]:
        """
        「取任务，取不到就准备好错误回复」的常用组合。

        返回 (task, error_result)：
            task 存在  → (task, None)
            task 不存在 → (None, 可以直接 return 的 HandlerResult)

        【为什么返回元组而不是抛异常】
            任务不存在是**期望中的输入**（对方发错了 task_id），
            不是程序错误。用返回值表达比用异常更贴切，
            而且调用方不容易忘记处理。
        """
        if not task_id:
            return None, self.reject_message(
                message, ["缺少 task_id"], [], task_id
            )

        task = self.store.get(task_id)
        if task is None:
            return None, self.reject_message(
                message,
                [f"本地没有任务 {task_id} —— 这条消息指向一个我不知道的任务"],
                [],
                task_id,
            )

        return task, None

    # ============================================================
    def describe(self) -> Dict[str, Any]:
        return {
            "service": type(self).__name__,
            "tasks": self.store.count(),
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(store={type(self.store).__name__})"
