"""
Task Execution Service —— 执行 / 提交 / 验收 / 完成的业务规则与状态落地
============================================================

【它负责什么】

    · START     承接方说「我开工了」        ACCEPTED    → IN_PROGRESS
    · SUBMIT    承接方说「我交付了这份成果」 IN_PROGRESS → SUBMITTED
    · VERIFY    发起方说「验收通过 / 不通过」SUBMITTED   → VERIFIED
                                                        或 IN_PROGRESS（返工）
    · COMPLETE  发起方说「任务正式结束」    VERIFIED    → COMPLETED

【它不负责什么 —— 这些边界是本步骤最要紧的部分】

    ❌ 不执行代码
       没有 subprocess / shell / Docker / sandbox / 自动测试。
       "执行" 在这里指的是**任务生命周期进入执行状态**，
       不是「系统替 Agent 跑代码」。

    ❌ 不调 LLM
       验收结论由发起方**显式**给出（passed=true/false）。
       不会收到 SUBMIT 就自动问模型「这份成果行不行」。

    ❌ 不碰钱
       COMPLETE 只表示生命周期结束，**不做结算**。
       没有 Wallet / Ledger / Escrow / Payment。
       结算是一个独立的后续阶段（它可以引用 TaskAgreementRecord）。

    ❌ 不碰 Workspace
       workspace_id 仍然只是逻辑引用。
       不创建、不授权、不读写文件。

    ❌ 不碰 Git
    ❌ 不做 retry / ACK / 队列 —— 只做 message_id 级业务幂等

【它和 TaskNegotiationService 的关系：完全并列，互不知道】

    Message
       ↓
    Handler（按类型）
       ├──→ TaskNegotiationService   OFFER / COUNTER_OFFER / ACCEPT / REJECT
       └──→ TaskExecutionService     START / SUBMIT / VERIFY / COMPLETE
                 ↓
              Task Core（复用 4A 已有的状态机）
                 ↓
              TaskStore

    【为什么必须分开】
        Agent 未来会自己制定、修改、学习**议价策略**，
        而执行流程是**系统硬约束** —— 两者变化的原因完全不同：

            议价怎么谈 → 会随着 Agent 的智能程度不断演化
            执行怎么走 → 由协议和状态机固定

        混在一起的结果是：想改议价策略时得动执行代码，
        而执行代码里有系统硬约束 —— 一改就可能破坏协议。

    两个服务都继承 TaskApplicationService（共享幂等 / trace / 协议违规回复），
    但**互相不引用**。有架构守卫盯着这条线。

【关于「第二套状态机」】

    本文件**没有**定义任何状态转移。
    所有状态变化都交给 core/task.py 里 4A 就实现好的
    start() / submit() / verify() / complete()。

    这里只负责回答：「现在这个状态，允许这个动作吗？」
    至于「允许之后状态变成什么」，是 Task Core 的事。
    这样状态机只有一份，不会漂移。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.logging_setup import get_logger
from core.task import Task, TaskStatus, TaskValidationError
from core.task_execution import (
    ExecutionEvent,
    TaskExecutionRecord,
    TaskResultRef,
    TaskVerification,
)
from core.task_store import TaskStore

from communication.message import Message, MessageType

from services.task_application_service import TaskApplicationService
from services.task_message_mapper import (
    CompleteIntent,
    StartIntent,
    SubmitIntent,
    TaskMessageMapper,
    VerifyIntent,
)
from services.task_result import HandlerAction, HandlerResult

logger = get_logger(__name__)


# ============================================================
# 角色（谁能做哪个动作）
# ============================================================
class ExecutionRole:
    """
    执行阶段的两种角色。

    【为什么用常量而不是直接写 task.contractor / task.creator】
        因为「谁该发哪条消息」是**协议的一部分**，
        值得有一个地方集中声明：
            START / SUBMIT  → 承接方（干活的人）
            VERIFY / COMPLETE → 发起方（付钱、验收的人）

        这样看一眼就知道整条链的角色分配，
        而不是散在四个 handler 里各判断一次。
    """

    CONTRACTOR = "contractor"
    INITIATOR = "initiator"


#: 每个事件要求谁发送
REQUIRED_ROLE: Dict[str, str] = {
    ExecutionEvent.START: ExecutionRole.CONTRACTOR,
    ExecutionEvent.SUBMIT: ExecutionRole.CONTRACTOR,
    # 【为什么验收必须是发起方】
    #     承接方不能自己宣布「我验收自己通过」——
    #     那会直接破坏角色边界：干活的既当运动员又当裁判。
    ExecutionEvent.VERIFY: ExecutionRole.INITIATOR,
    ExecutionEvent.COMPLETE: ExecutionRole.INITIATOR,
}

#: 每个事件允许在哪些状态下发生（协议硬约束）
ALLOWED_STATES: Dict[str, Tuple[str, ...]] = {
    ExecutionEvent.START: (TaskStatus.ACCEPTED,),
    ExecutionEvent.SUBMIT: (TaskStatus.IN_PROGRESS,),
    ExecutionEvent.VERIFY: (TaskStatus.SUBMITTED,),
    ExecutionEvent.COMPLETE: (TaskStatus.VERIFIED,),
}

#: 每个事件之后任务应该处于的状态（用于校验 Task Core 真的照做了）
EXPECTED_STATE: Dict[str, str] = {
    ExecutionEvent.START: TaskStatus.IN_PROGRESS,
    ExecutionEvent.SUBMIT: TaskStatus.SUBMITTED,
    ExecutionEvent.COMPLETE: TaskStatus.COMPLETED,
    # VERIFY 有两个出口，不在这里写死 —— 见 apply_verify
}


class TaskExecutionService(TaskApplicationService):
    """
    执行阶段的业务服务。

    用法：

        execution = TaskExecutionService(store)
        task, error = execution.load_task(message, task_id)
        errors = execution.validate(task, actor, ExecutionEvent.START, message)
        result = execution.apply_start(task, incoming=message)
    """

    def __init__(
        self,
        store: TaskStore,
        mapper: Optional[TaskMessageMapper] = None,
    ):
        super().__init__(store=store, mapper=mapper)

    # ============================================================
    # 一、业务规则（校验）
    # ============================================================
    def actor_role(self, task: Task, actor: str) -> Optional[str]:
        """
        这个 Agent 在这个任务里是什么角色？

        【判断依据全部来自任务字段，没有任何具体 Agent 的名字】
            contractor  → 承接方（干活）
            creator     → 发起方（付钱、验收）
            都不是      → None（无关的人）

        换成任何两个 Agent 都成立。
        有守卫禁止在 services/ 里出现硬编码的 Agent 名。
        """
        if task.contractor and actor == task.contractor:
            return ExecutionRole.CONTRACTOR
        if actor == task.creator:
            return ExecutionRole.INITIATOR
        return None

    def validate_role(
        self, task: Task, actor: str, event_type: str
    ) -> List[str]:
        """
        发送方必须是这个动作要求的角色。

        【这是 4D 最关键的角色边界】
            START / SUBMIT 只有承接方能发 ——
                否则发起方可以自己「开工」自己的任务。
            VERIFY / COMPLETE 只有发起方能发 ——
                否则承接方可以自己宣布验收通过。

        最后一条尤其重要：它保证了「验收」这个动作
        永远由**另一方**做出，而不是自己说自己合格。
        """
        required = REQUIRED_ROLE.get(event_type)
        if required is None:
            return []

        actual = self.actor_role(task, actor)

        if actual is None:
            known = {task.creator}
            if task.contractor:
                known.add(task.contractor)
            return [
                f"{actor} 不是任务 {task.task_id} 的参与方。\n"
                f"  这个任务的发起方是 {task.creator}，"
                f"承接方是 {task.contractor or '（还没有）'}。\n"
                f"  没有参与这个任务的人，不能推进它的执行。"
            ]

        if actual != required:
            who = (
                "承接方" if required == ExecutionRole.CONTRACTOR else "发起方"
            )
            return [
                f"{event_type} 必须由{who}发送，但 {actor} 是"
                f"{'承接方' if actual == ExecutionRole.CONTRACTOR else '发起方'}。\n"
                + (
                    f"  （承接方不能自己宣布验收结果 —— "
                    f"干活的不能既当运动员又当裁判。）"
                    if required == ExecutionRole.INITIATOR
                    else f"  （发起方不能替承接方宣布开工或提交成果。）"
                )
            ]

        return []

    def validate_state(
        self, task: Task, event_type: str
    ) -> List[str]:
        """
        任务当前状态允许这个动作吗？

        【这是**协议硬约束**，不是 Agent 能决定的】
            状态机允许什么就允许什么，不允许就报错。
            比如 ACCEPTED → SUBMITTED 是跳步，必须被拒绝。

        【注意这里不重新实现状态机】
            允许的状态列表来自本模块顶部的 ALLOWED_STATES，
            它必须和 core/task.py 的 TRANSITIONS 一致 ——
            有测试断言两者不漂移。
        """
        allowed = ALLOWED_STATES.get(event_type)
        if allowed is None:
            return []

        if task.status in allowed:
            return []

        if task.is_terminal:
            return [
                f"任务 {task.task_id} 已经是终态 {task.status}，"
                f"不能再接受 {event_type}。"
            ]

        return [
            f"任务 {task.task_id} 当前状态是 {task.status}，"
            f"不允许 {event_type}。\n"
            f"  {event_type} 只能在这些状态下发生：{list(allowed)}\n"
            f"  （状态只能按状态机走，不能跳步。）"
        ]

    def validate_result(
        self, intent: SubmitIntent
    ) -> List[str]:
        """
        SUBMIT 必须带一个合法的成果引用。

        【为什么必填】
            不能只发「我做好了」—— 那样发起方不知道验收什么，
            也不知道该针对哪一份成果给出结论。
            所以协议要求 SUBMIT 必须说清楚「交付的是什么」。
        """
        if intent.result is None:
            return [
                "SUBMIT 必须携带成果引用（result_ref）。\n"
                "  只发「我做好了」是不够的 —— "
                "发起方需要知道验收的是哪一份成果。"
            ]
        return []

    def validate_verification(
        self, intent: VerifyIntent
    ) -> List[str]:
        """
        VERIFY 必须带一个明确的验收结论。

        【passed 必须是布尔值，不能是 1 / 0 / 缺失】
            因为「没传」和「传了 false」完全是两回事：
            前者是协议错误，后者是明确的「不通过」。
            混在一起会让返工流程出现说不清的状态。
        """
        if intent.passed is None:
            return [
                "VERIFY 必须携带 passed（布尔值）。\n"
                "  缺省或非布尔值会让「没传」和「传了不通过」分不清。"
            ]

        errors: List[str] = []

        if not intent.passed and not (intent.feedback or "").strip():
            errors.append(
                "验收不通过时必须给出 feedback。\n"
                "  承接方需要知道「哪里不行」才能返工 —— "
                "只说『不通过』等于让它盲改。"
            )

        return errors

    def validate(
        self,
        task: Task,
        actor: str,
        event_type: str,
        message: Message,
    ) -> List[str]:
        """
        执行阶段所有通用规则的汇总。

        顺序：状态 → 角色。
        （先看「现在能不能做」，再看「你配不配做」——
          这样报错信息更贴近对方的真实问题。）
        """
        errors = self.validate_state(task, event_type)
        errors.extend(self.validate_role(task, actor, event_type))
        return errors

    # ============================================================
    # 二、幂等
    # ============================================================
    def already_recorded(self, task: Task, message: Message) -> bool:
        """
        这条执行消息之前处理过了吗？

        【判断依据】
            task_executions 表里有没有同样 message_id 的记录。
            每个执行事件都对应且只对应一条消息，所以 message_id 就是它的身份。
        """
        if not message.message_id:
            return False
        return self.store.find_execution_by_message(message.message_id) is not None

    # ============================================================
    # 三、状态落地
    # ============================================================
    def apply_start(
        self, task: Task, incoming: Message
    ) -> HandlerResult:
        """START：ACCEPTED → IN_PROGRESS。"""
        return self._apply_transition(
            task=task,
            incoming=incoming,
            event_type=ExecutionEvent.START,
            transition=task.start,
            action=HandlerAction.STARTED,
        )

    def apply_submit(
        self,
        task: Task,
        result: TaskResultRef,
        incoming: Message,
    ) -> HandlerResult:
        """
        SUBMIT：IN_PROGRESS → SUBMITTED。

        【成果引用只是一条记录】
            本方法**不会**去访问 result.result_ref 指向的任何东西。
            不打开文件、不克隆仓库、不下载。
            它只把「承接方说他交付了什么」记下来。
        """
        return self._apply_transition(
            task=task,
            incoming=incoming,
            event_type=ExecutionEvent.SUBMIT,
            transition=task.submit,
            action=HandlerAction.SUBMITTED,
            result=result,
            extra_details={
                "result": result.to_dict(),
                "result_ref": result.result_ref,
                "attempt": None,  # 落地后再算（见下）
            },
        )

    def apply_verify(
        self,
        task: Task,
        verification: TaskVerification,
        incoming: Message,
    ) -> HandlerResult:
        """
        VERIFY：SUBMITTED → VERIFIED（通过）或 SUBMITTED → IN_PROGRESS（返工）。

        【两个出口】
            passed=True   → VERIFIED，等 COMPLETE 收尾
            passed=False  → IN_PROGRESS，承接方可以再 SUBMIT 一次

        【为什么失败不是判死】
            交付不合格通常是可以修的。
            直接把任务判死（FAILED）会让一次小错误毁掉整个合同 ——
            而合同已经签了（有 TaskAgreementRecord）。

        【为什么 feedback 一定要落库】
            承接方可能不在线、可能过很久才处理。
            feedback 只放在消息里的话，一旦消息日志被清理就没了。
            落进 task_executions 之后，
            「上一轮为什么不通过」是**可查询**的。
        """
        return self._apply_transition(
            task=task,
            incoming=incoming,
            event_type=ExecutionEvent.VERIFY,
            transition=lambda: task.verify(passed=verification.passed),
            action=HandlerAction.VERIFIED if verification.passed
            else HandlerAction.REWORK,
            verification=verification,
            extra_details={
                "passed": verification.passed,
                "feedback": verification.feedback,
                "outcome": "verified" if verification.passed else "rework",
                "rework": not verification.passed,
            },
        )

    def apply_complete(
        self, task: Task, incoming: Message
    ) -> HandlerResult:
        """
        COMPLETE：VERIFIED → COMPLETED。

        【它**只**表示任务生命周期结束】
            ❌ 不付款、不转账、不释放托管、不结算
            ❌ 不动 Wallet / Ledger

            「活干完了」和「钱给了」是两件事，
            中间还差一个独立的结算阶段 ——
            那个阶段可以引用 TaskAgreementRecord 里约定的 agreed_price。
        """
        return self._apply_transition(
            task=task,
            incoming=incoming,
            event_type=ExecutionEvent.COMPLETE,
            transition=task.complete,
            action=HandlerAction.COMPLETED,
        )

    # ------------------------------------------------------------
    def _apply_transition(
        self,
        task: Task,
        incoming: Message,
        event_type: str,
        transition,
        action: str,
        result: Optional[TaskResultRef] = None,
        verification: Optional[TaskVerification] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> HandlerResult:
        """
        四个 apply_* 的公共骨架。

        【顺序很重要】
            1. sync_trace       先把链路缝合
            2. 状态转移         交给 Task Core（不合法会抛异常，不会留下半改数据）
            3. 记录执行事件      带着转移后的状态
            4. 落库             TaskStore.save

        【为什么先转移再记录】
            记录里有 status_after 字段。如果先记录再转移，
            要么得预测状态（可能和 Task Core 不一致），
            要么得回填（多一次写）。先转移就直接拿到了事实。

        【为什么 transition 是一个 callable】
            VERIFY 有两个出口（取决于 passed），
            用 lambda 传进来比在骨架里 if event_type == VERIFY 干净。
        """
        trace_changed = self.sync_trace(task, incoming)

        # ============================================================
        # 【Step 4D.1：状态变化 + 事件记录必须在同一个事务里】
        # ============================================================
        #
        # 4D 时这里是两个独立的事务：
        #
        #     self.store.record_execution(record)   # 事务 1
        #     self.store.save(task)                 # 事务 2
        #
        # 两个事务之间崩溃 → 半成功状态：
        #     ✅ 事件写了，❌ 状态没改
        # 或者反过来。而这种不一致**不会报错**，只会让数据慢慢变质。
        #
        # 为什么 4D 没做？因为那时候做不到 ——
        # Database.transaction() 不可重入，嵌套 BEGIN 会直接抛
        # "cannot start a transaction within a transaction"。
        # 4D.1 先把它变成可重入的，这里才可能包起来。
        #
        # 【现在的保证】
        #     要么 (状态改了 + 事件写了)
        #     要么 (状态没改 + 事件没写)
        # 没有中间状态。
        #
        # 【状态转移放在事务里还是外】
        #     transition() 改的是**内存里**的 Task，
        #     它不碰数据库（Task Core 不依赖存储）。
        #     所以放在事务外也不会造成半成功 ——
        #     真正落库的只有下面两件事，那才是需要原子的部分。
        #     但为了读起来清楚，整段一起包住。
        with self.store.db.transaction():
            # 状态转移 —— Task Core 会校验合法性，不合法直接抛
            task = transition()

            record = TaskExecutionRecord(
                message_id=incoming.message_id,
                task_id=task.task_id,
                event_type=event_type,
                result=result,
                verification=verification,
                reply_to=incoming.reply_to,
                from_agent=incoming.from_agent,
                trace_id=incoming.trace_id,
                conversation_id=incoming.conversation_id,
                status_after=task.status,
            )
            self.store.record_execution(record)
            self.store.save(task)

        logger.debug(
            "任务 %s：%s → %s（来自 %s）",
            task.task_id, event_type, task.status, incoming.from_agent,
        )

        # 记录完之后才拿得到「第几轮」
        attempt = self.store.attempt_count(task.task_id)

        details: Dict[str, Any] = {
            "status": task.status,
            "event_type": event_type,
            "message_id": incoming.message_id,
            "reply_to": incoming.reply_to,
            "from_agent": incoming.from_agent,
            "trace_id": incoming.trace_id,
            "conversation_id": incoming.conversation_id,
            "trace_synced": trace_changed,
            "attempt": attempt,
            # 【明确记录这些事情都没有发生】——
            # 让读到结果的人不必去翻代码确认
            "payment": None,
            "ledger": None,
            "workspace": None,
            "git": None,
            "llm_calls": 0,
            "code_executed": False,
        }
        if extra_details:
            details.update(extra_details)
        if event_type == ExecutionEvent.SUBMIT:
            details["attempt"] = attempt

        return HandlerResult(
            ok=True,
            action=action,
            task_id=task.task_id,
            # 【不回复】
            #     START / SUBMIT / VERIFY / COMPLETE 都是**单向通知**：
            #     对方不需要为它再发一条消息。
            #     （出错时会回 ERROR，那是另一回事。）
            reply_type=None,
            reply_payload=None,
            details=details,
        )

    # ============================================================
    # 四、查询（只读）
    # ============================================================
    def executions_for(self, task_id: str) -> List[TaskExecutionRecord]:
        """执行历史（append-only，按时间顺序）。"""
        return self.store.executions_for(task_id)

    def history_summary(self, task_id: str) -> Dict[str, Any]:
        """
        执行历史的一个可读摘要。

        【为什么需要它】
            返工流程下历史会变长，演示和日志里需要一眼看清
            「提交了几次、每次为什么被打回」。
        """
        records = self.store.executions_for(task_id)
        submissions = self.store.submissions_for(task_id)
        verifications = self.store.verifications_for(task_id)

        return {
            "task_id": task_id,
            "events": [
                {
                    "event_type": record.event_type,
                    "from_agent": record.from_agent,
                    "message_id": record.message_id,
                    "reply_to": record.reply_to,
                    "status_after": record.status_after,
                }
                for record in records
            ],
            "submissions": len(submissions),
            "verifications": len(verifications),
            "attempts": len(submissions),
            "rework_count": sum(
                1 for record in verifications
                if record.verification and not record.verification.passed
            ),
        }

    # ============================================================
    def describe(self) -> Dict[str, Any]:
        return {
            "service": type(self).__name__,
            "handles": list(ExecutionEvent.all()),
            "tasks": self.store.count(),
            "execution_records": self.store.count_executions(),
            # 反复声明这条边界，因为它最容易被后续步骤越过
            "executes_code": False,
            "calls_llm": False,
            "touches_wallet": False,
            "uses_workspace": False,
        }

    def __repr__(self) -> str:
        return f"TaskExecutionService(handles={list(ExecutionEvent.all())})"
