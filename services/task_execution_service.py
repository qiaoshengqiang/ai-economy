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

from typing import (
    Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

from core.logging_setup import get_logger
from core.task import Task, TaskMode, TaskStatus, TaskValidationError
from core.task_execution import (
    COMPLETION_PROPOSAL_KEY,
    CompletionProposal,
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
    执行阶段的三种角色。

    【为什么用常量而不是直接写 task.contractor / task.creator】
        因为「谁该发哪条消息」是**协议的一部分**，
        值得有一个地方集中声明：
            START / SUBMIT    → 承接方（干活的人）
            VERIFY / COMPLETE → 发起方（付钱、验收的人）
                                **或** 验收责任人（acceptance
                                responsibility，见 Shared Context）

        这样看一眼就知道整条链的角色分配，
        而不是散在四个 handler 里各判断一次。

    【ACCEPTOR 不是「新角色系统」】
        它就是「Shared Context 里被指派了
        `responsibility = ACCEPTANCE_RESPONSIBILITY` 的那个 Agent」。
        没有独立的表、没有独立的注册、没有固定的角色名 ——
        谁负责验收由 **Agent 自己**通过现有的 ASSIGN 能力决定。
    """

    CONTRACTOR = "contractor"
    INITIATOR = "initiator"
    ACCEPTOR = "acceptor"


#: 每个事件要求谁发送（**可接受的集合**，不是单一角色）。
#:
#: 🔒 用集合而不是单一值，是因为「谁来验收」不该被固定成
#:    「一定是发起方」—— 发起方可能是 human_demand（不是 Agent），
#:    也可能这个任务根本没有商业发起方（Root Task）。
#:    Acceptance Responsibility 提供了另一条合法路径。
REQUIRED_ROLE: Dict[str, Tuple[str, ...]] = {
    # START 还有一个额外情况：从 CREATED 开工时
    # contractor 还没确定，发送者会**成为** contractor。
    # 那一段由 validate_role 单独处理（见那里的 _validate_start_role）。
    ExecutionEvent.START: (ExecutionRole.CONTRACTOR,),
    ExecutionEvent.SUBMIT: (ExecutionRole.CONTRACTOR,),
    # 【为什么验收不能是承接方】
    #     承接方不能自己宣布「我验收自己通过」——
    #     那会直接破坏角色边界：干活的既当运动员又当裁判。
    #
    #     注意：这个集合里**不含 CONTRACTOR**，但仅靠集合还不够 ——
    #     一个 Agent 可能**同时**是 contractor 和 acceptor
    #     （自己给自己派了验收责任）。那条路在 validate_role 里
    #     被显式挡住（contractor 身份优先）。
    ExecutionEvent.VERIFY: (ExecutionRole.INITIATOR, ExecutionRole.ACCEPTOR),
    ExecutionEvent.COMPLETE: (ExecutionRole.INITIATOR, ExecutionRole.ACCEPTOR),
}

#: 每个事件允许在哪些状态下发生（协议硬约束）
ALLOWED_STATES: Dict[str, Tuple[str, ...]] = {
    # 🔒 CREATED 也允许 START —— Root Task 不是 Commerce，
    #    不需要先走 OFFERED / ACCEPTED。
    #    这不是绕过校验：角色校验照样要过（见 validate_role）。
    ExecutionEvent.START: (TaskStatus.ACCEPTED, TaskStatus.CREATED),
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
        acceptors_of: Optional[Callable[[str], Iterable[str]]] = None,
        criteria_of: Optional[Callable[[str], Iterable[str]]] = None,
    ):
        """
        参数：
            acceptors_of: 只读回调 `task_id → 承担验收责任的 Agent`。
            criteria_of:  只读回调 `task_id → Definition of Done 条目`。

                🔒 两者都是**注入**，不在这里自己读 Shared Context 的存储 ——
                   `services/` 不直接依赖 `storage/`（AGENTS.md §3），
                   而 Shared Context 的读取入口在
                   `TaskAlignmentService` / `TaskContextStore`。
                   接线由组合根负责（`main.py`）。

                🔒 为 `None` 时行为与以前**完全一致**（没有 acceptor、
                   没有 DoD），所以既有的执行测试不受影响。

                   这和 WorkspaceGateway 收注入 `is_participant`
                   是同一种手法：**让服务依赖一个能力，而不是一个实现。**
        """
        super().__init__(store=store, mapper=mapper)
        self._acceptors_of = acceptors_of
        self._criteria_of = criteria_of

    def acceptors_of(self, task: Task) -> Tuple[str, ...]:
        """
        这个任务当前承担「验收责任」的 Agent（只读查询）。

        🔒 读失败时返回**空集**（fail-closed）：
           空的 acceptor 集合会让 VERIFY / COMPLETE 因为角色不匹配被拒，
           而不是让它们「看起来通过了」。
           验收链最不该出现的行为就是「读不到就放行」。
        """
        if self._acceptors_of is None:
            return ()
        try:
            return tuple(
                sorted({str(a) for a in self._acceptors_of(task.task_id) if a})
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "读取任务 %s 的验收责任失败 —— 按「没有验收人」处理"
                "（fail-closed，不会因此放行验收）",
                task.task_id,
            )
            return ()

    # ============================================================
    # 一、业务规则（校验）
    # ============================================================
    def actor_role(self, task: Task, actor: str) -> Optional[str]:
        """
        这个 Agent 在这个任务里是什么角色？

        【判断依据全部来自任务字段 + Shared Context，没有任何具体 Agent 的名字】
            contractor          → 承接方（干活）
            acceptance 责任人   → 验收方（acceptance responsibility）
            creator             → 发起方（付钱、验收）
            都不是              → None（无关的人）

        换成任何两个 Agent 都成立。
        有守卫禁止在 services/ 里出现硬编码的 Agent 名。

        🔴 **顺序至关重要：contractor 必须排在 acceptor 前面。**

            否则一个「既是生产者、又给自己派了验收责任」的 Agent
            会以 ACCEPTOR 身份通过 VERIFY —— 那就是自验。

            让 contractor 先匹配，就等于让
            「生产者不能自验」**优先于**任何分配记录。
            规格 §4 明确要求：生产者不能利用自己的 acceptance
            assignment 绕过这条规则。
        """
        if task.contractor and actor == task.contractor:
            return ExecutionRole.CONTRACTOR
        if actor in self.acceptors_of(task):
            return ExecutionRole.ACCEPTOR
        if actor == task.creator:
            return ExecutionRole.INITIATOR
        return None

    #: 角色 → 给人看的中文名（只用于错误信息）
    _ROLE_LABEL = {
        ExecutionRole.CONTRACTOR: "承接方",
        ExecutionRole.INITIATOR: "发起方",
        ExecutionRole.ACCEPTOR: "验收责任人",
    }

    @staticmethod
    def looks_like_commerce(task: Task) -> Optional[str]:
        """
        这个任务看起来是不是**商业任务**？是则返回原因，否则 `None`。

        【为什么需要这一条】

            `CREATED → IN_PROGRESS` 是为 **Root Task** 开的：
            Human Demand → Agent 建 Root Task → 直接进入生产，
            中间没有报价与成交。

            但**不能**因此让「任何 CREATED 任务 + 是参与方」都能跳过
            整条商业流程 —— 那会把 Commerce 边界悄悄放宽。

        🔒 三个概念不能混在一起：

               Participant  ≠  Contractor  ≠  Commerce Acceptor
               能参与任务      在干活          通过报价/成交接下任务

            「是参与方」只说明这个人**和任务有关**，
            不说明这个任务**不需要谈价**。

        🔒 判据只用**既有事实**，不新增字段（本轮禁止新增 schema）：

            agreed_price  —— 已经谈成过价
            offers        —— 已经进入过报价流程
            budget        —— 发起方声明了「愿意付的上限」= 商业意图

            Root Task 三者都没有（Human Demand 不报价、不给预算）。

        ⚠️ **已知限制**：这三个事实判定的是「**已经**表现出商业意图」。
           V1 没有 `parent_task_id` / `origin` / `is_root` 之类的结构化字段
           （Task Lineage 是 `FORBIDDEN NOW`），所以**一个刚创建、
           既没报价也没预算的商业任务，在结构上无法与 Root Task 区分**。
           见实现报告的「Root/Commerce differentiation: UNAVAILABLE」。
        """
        if task.agreed_price is not None:
            return "它已经谈成了价格（agreed_price 已设置）"
        if task.offers:
            return "它已经进入过报价流程（有 offers 记录）"
        if task.budget is not None:
            return "它声明了预算（budget 已设置，说明是商业委托）"
        return None

    def _validate_start_from_created(
        self, task: Task, actor: str
    ) -> List[str]:
        """
        `CREATED → IN_PROGRESS` 的**特殊角色规则**。

        【为什么不能沿用 REQUIRED_ROLE[START] = CONTRACTOR】

            从 CREATED 出发时 `contractor` **还不存在** ——
            而「谁接下这份工作」正是这一刻要确定的事。
            Root Task 不走商业前置（没有报价与成交），
            所以没有 `accept()` 来产生 contractor。

        【三道检查，缺一不可】

            ① **不能是商业任务** —— 有商业意图的任务必须走
               `CREATED → OFFERED → ACCEPTED → IN_PROGRESS`。
               🔒 这条是本轮加的：以前只要「是参与方」就能直接开工，
                  等于把 Commerce 边界放宽了。
            ② 必须是 **Task Participant**（creator / contractor /
               participants 之一）—— 不能让路过的人认领别人的任务。
            ③ 若已有 contractor，只有他能开工。

        【之后发生什么】
            通过校验的人会在 `apply_start()` 里
            **成为承接方**（`Task.claim_producer()`）——
            那是后续 SUBMIT 的依据，也是「不能自验」的依据。
        """
        if task.contractor:
            if actor == task.contractor:
                return []                     # 已经是承接方，正常开工
            return [
                f"任务 {task.task_id} 已经由 {task.contractor} 承接，"
                f"{actor} 不能替它开工。"
            ]

        # ---- 🔴 ① 商业任务不得跳过商业流程 ----
        commerce = self.looks_like_commerce(task)
        if commerce:
            return [
                f"任务 {task.task_id} 不能直接从 CREATED 开工：{commerce}。\n"
                f"  这是**商业任务**，必须走 "
                f"CREATED → OFFERED → ACCEPTED → IN_PROGRESS。\n"
                f"  （CREATED 直接开工只适用于**没有商业流程**的任务，"
                f"    例如来自 Human Demand 的 Root Task。）\n"
                f"  🔒 参与方身份 ≠ 商业承接方 —— "
                f"「能参与」不表示「这个任务不需要谈价」。"
            ]

        # ---- ② 必须是参与方 ----
        if not task.is_participant(actor):
            return [
                f"{actor} 不是任务 {task.task_id} 的参与方，不能承接它。\n"
                f"  （任务还没开工，任何人都不能替别人认领。）"
            ]
        return []

    def validate_role(
        self, task: Task, actor: str, event_type: str
    ) -> List[str]:
        """
        发送方必须是这个动作要求的角色。

        【这是 4D 最关键的角色边界】
            START / SUBMIT   只有承接方能发 ——
                否则发起方可以自己「开工」自己的任务。
            VERIFY / COMPLETE 只有发起方**或验收责任人**能发 ——
                否则承接方可以自己宣布验收通过。

        最后一条尤其重要：它保证了「验收」这个动作
        永远由**另一方**做出，而不是自己说自己合格。

        🔒 验收允许两种角色，但**都不包括承接方**：
           `REQUIRED_ROLE` 里没有 CONTRACTOR，而且这里还有一道
           独立的显式拦截（顺序优先于任何 assignment）——
           防止「生产者给自己派了验收责任」这种绕过。
        """
        required = REQUIRED_ROLE.get(event_type)
        if required is None:
            return []

        # ---- CREATED 开工：contractor 还没确定，走单独的规则 ----
        if event_type == ExecutionEvent.START and task.status == TaskStatus.CREATED:
            return self._validate_start_from_created(task, actor)

        actual = self.actor_role(task, actor)

        if actual is None:
            known = {task.creator}
            if task.contractor:
                known.add(task.contractor)
            known.update(self.acceptors_of(task))
            return [
                f"{actor} 不是任务 {task.task_id} 的参与方。\n"
                f"  这个任务的发起方是 {task.creator}，"
                f"承接方是 {task.contractor or '（还没有）'}，"
                f"验收责任人是 {'、'.join(sorted(self.acceptors_of(task))) or '（还没有）'}。\n"
                f"  没有参与这个任务的人，不能推进它的执行。"
            ]

        # ---- 🔴 生产者不得自验（优先于集合判断）----
        #
        # 单独写出来而不是只靠 REQUIRED_ROLE 不含 CONTRACTOR，
        # 是因为「这个 Agent 恰好是 contractor」必须给出一条
        # **说得清**的错误，否则调用方会以为是自己少了某个 assignment。
        #
        # 【SOLO 的显式例外】
        #     Solo Task 只有一个生产者；它没有团队内部 acceptor。
        #     Human 也没有“接受”动作。因此 Solo 允许承接方自己
        #     VERIFY / COMPLETE —— 见 ADR-0056。
        #     这是**唯一**允许生产者自验的路径，Team Task 不受影响。
        if (
            actual == ExecutionRole.CONTRACTOR
            and event_type in (
                ExecutionEvent.VERIFY, ExecutionEvent.COMPLETE,
            )
            and task.mode == TaskMode.SOLO
        ):
            return []

        if actual == ExecutionRole.CONTRACTOR and event_type in (
            ExecutionEvent.VERIFY, ExecutionEvent.COMPLETE,
        ):
            return [
                f"{event_type} 不能由承接方 {actor} 发送。\n"
                f"  干活的不能既当运动员又当裁判 —— "
                f"生产者不能验收自己的成果。\n"
                f"  {event_type} 必须由**发起方**或**验收责任人**发送。\n"
                f"  ⚠️ 就算 {actor} 在 Shared Context 里被指派了验收责任，"
                f"这条规则依然成立（生产者身份优先）。\n"
                f"  换个参与方来验收，或者把验收责任指派给别人。"
            ]

        if actual not in required:
            required_label = "或".join(
                self._ROLE_LABEL.get(r, r) for r in required
            )
            return [
                f"{event_type} 必须由{required_label}发送，但 {actor} 是"
                f"{self._ROLE_LABEL.get(actual, actual)}。\n"
                + (
                    f"  （承接方不能自己宣布验收结果 —— "
                    f"干活的不能既当运动员又当裁判。）"
                    if event_type in (ExecutionEvent.VERIFY, ExecutionEvent.COMPLETE)
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

    # ============================================================
    # 二·五、Completion / Acceptance（最小闭环，只读查询）
    # ============================================================
    def definition_of_done(self, task: Task) -> Tuple[str, ...]:
        """
        Definition of Done（只读）。

        🔒 权威来源是 **TaskSharedContext.acceptance_criteria** ——
           刻意没有第二套 DoD 数据结构。
        🔒 读失败返回空元组（fail-closed）：没有 DoD 就不允许结项。
        """
        if self._criteria_of is None:
            return ()
        try:
            return tuple(
                str(c).strip() for c in self._criteria_of(task.task_id)
                if str(c).strip()
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "读取任务 %s 的 Definition of Done 失败 —— 按「没有 DoD」处理"
                "（fail-closed，不会因此放行结项）",
                task.task_id,
            )
            return ()

    def latest_completion_proposal(
        self, task_id: str
    ) -> Optional[CompletionProposal]:
        """
        从**已有的** SUBMIT 事件里读回最近一次 Completion Proposal。

        🔒 不新增表：它存在 `task_executions.result_metadata` 里
           （见 `COMPLETION_PROPOSAL_KEY`）。这里按时间倒序找第一条
           能解析出来的，解析不了的直接跳过（旧数据 / 手工造的记录）。
        """
        for record in reversed(self.store.submissions_for(task_id)):
            if record.result is None:
                continue
            raw = (record.result.metadata or {}).get(COMPLETION_PROPOSAL_KEY)
            if not isinstance(raw, Mapping):
                continue
            try:
                return CompletionProposal.from_dict(raw)
            except ValueError:
                continue
        return None

    def validate_completion_proposal(
        self,
        task: Task,
        proposal: CompletionProposal,
    ) -> List[str]:
        """
        Completion Proposal 的五条校验（规格 §3）。

            ① proposed_by 必须是 Task Participant
            ② task_id 必须存在且匹配
            ③ criteria_status 必须是结构化值（由 dataclass 保证）
            ④ 声称全部达标时 outstanding_gaps 必须为空（由 dataclass 保证）
            ⑤ evidence 不能为空（由 dataclass 保证）

        🔒 ③④⑤ 在 `CompletionProposal.__post_init__` 里 ——
           一个**非法对象根本构造不出来**，所以这里不需要重复检查。
           这是「让非法状态无法被表达」，比「构造后再校验」更可靠。
        """
        errors: List[str] = []

        if not task:
            return ["Completion Proposal 必须针对一个存在的 Task。"]

        # ② task_id 匹配
        if proposal.task_id != task.task_id:
            errors.append(
                f"Completion Proposal 的 task_id 是 {proposal.task_id!r}，"
                f"但当前任务是 {task.task_id!r}。\n"
                f"  （提议必须针对它自己所声称的那个任务。）"
            )

        # ① proposed_by 必须是 Task Participant
        if not task.is_participant(proposal.proposed_by):
            errors.append(
                f"{proposal.proposed_by} 不是任务 {task.task_id} 的参与方，"
                f"不能为它提交 Completion Proposal。\n"
                f"  只有参与方才能声称「这个任务完成了」。"
            )
        # ---- 前置：必须有可验证的 Definition of Done ----
        if not self.definition_of_done(task):
            errors.append(
                f"任务 {task.task_id} 没有 Definition of Done，"
                f"不能提交 Completion Proposal。\n"
                f"  「完成了」必须先定义「完成是什么」——\n"
                f"  否则验收方没有可核对的依据，"
                f"『完成』就只是一句自述。\n"
                f"  （DoD 写在 Task Shared Context 的 acceptance_criteria 里，"
                f"任何参与方都可以添加。）"
            )
        return errors

    def completion_blockers(
        self,
        task: Task,
        *,
        proposal: Optional[CompletionProposal] = None,
    ) -> List[str]:
        """
        「这个任务现在**可以**结项吗？」—— 只读检查，返回**未满足**的项。

        规格 §6 的六条：

            ① Task 存在合法 Definition of Done
            ② Agent 提交了 Completion Proposal
            ③ Acceptance Responsibility 存在
            ④ Acceptance 通过（状态已到 VERIFIED）
            ⑤ Deliverable / evidence 满足现有验证逻辑
            ⑥ Task 状态最终进入 COMPLETED

        本方法检查 ①②③④；⑤ 由 `validate_result()` 在 SUBMIT 时保证；
        ⑥ 是 COMPLETE 事件的结果 —— 它不是「前置条件」，
        所以不在这里，也不能在这里被伪造。

        🔒 **只读**：不改状态、不写库。
           真正的状态变化仍然只能由 VERIFY / COMPLETE 事件产生 ——
           这条方法的返回值**不是**完成，它只是「还差什么」。
        """
        blockers: List[str] = []

        if not self.definition_of_done(task):
            blockers.append("没有 Definition of Done（acceptance_criteria 为空）")

        found = proposal if proposal is not None else self.latest_completion_proposal(
            task.task_id)
        if found is None:
            blockers.append("没有提交过 Completion Proposal")

        if not self.acceptors_of(task):
            blockers.append("没有 Acceptance Responsibility（没人负责验收）")

        if task.status != TaskStatus.VERIFIED:
            blockers.append(
                f"验收还没通过（当前状态 {task.status}，需要先 VERIFY(passed=True)）"
            )
        return blockers

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
        """
        START：`CREATED` 或 `ACCEPTED` → `IN_PROGRESS`。

        【从 CREATED 开工时先「承接」】

            从 CREATED 出发时 `contractor` 还没确定
            （Root Task 不走商业前置，没有 `accept()` 来产生它）。

            发送者已经通过了 `validate_role` 的参与方校验，
            在这里**成为承接方** —— 这是后续 SUBMIT 的依据，
            也是「生产者不能自验」的依据。

        🔒 走领域方法 `Task.claim_producer()`，**不在应用层直接写字段**：
           领域层负责「这个事实合不合法」（状态对不对、会不会换人），
           应用层负责「这个人有没有权做」。
        """
        if task.status == TaskStatus.CREATED:
            task.claim_producer(incoming.from_agent)
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
        proposal: Optional[CompletionProposal] = None,
    ) -> HandlerResult:
        """
        SUBMIT：IN_PROGRESS → SUBMITTED。

        【成果引用只是一条记录】
            本方法**不会**去访问 result.result_ref 指向的任何东西。
            不打开文件、不克隆仓库、不下载。
            它只把「承接方说他交付了什么」记下来。

        【Completion Proposal 的落地方式】
            它被写进 `result.metadata[COMPLETION_PROPOSAL_KEY]`，
            随**同一条** SUBMIT 记录落进 `task_executions` ——
            **不新增表、不新开事件**。

            `TaskResultRef` 是不可变的，所以这里是**造一个新的**
            （原有 metadata 全部保留，只多一个键），
            而不是就地改 —— 与「提交之后不能被改写」的语义一致。
        """
        if proposal is not None:
            result = TaskResultRef(
                result_ref=result.result_ref,
                result_type=result.result_type,
                summary=result.summary,
                metadata={
                    **dict(result.metadata),
                    COMPLETION_PROPOSAL_KEY: proposal.to_dict(),
                },
            )
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

    def validate_reopen(self, task: Task, actor: str) -> List[str]:
        """
        Reopen 的角色 / 状态规则（ADR-0056）。

        V1 约定：

            · 只有 COMPLETED 的任务可以 Reopen；
            · 只有 Task Creator（Team Lead）可以 Reopen；
            · Solo 时 creator = contractor，所以 Solo Agent 自己可以 Reopen；
            · Front 不直接调用它 —— Front 只把 Human 的返工意图
              路由给 Task Creator。

        🔒 Reopen 不是“新建 Task”，也不释放 Workspace 锁。
        """
        errors: List[str] = []

        if task.status != TaskStatus.COMPLETED:
            errors.append(
                f"任务 {task.task_id} 当前是 {task.status}，"
                f"只有 COMPLETED 的任务可以 Reopen。"
            )

        if actor != task.creator:
            errors.append(
                f"{actor} 不能 Reopen 任务 {task.task_id}。\n"
                f"  只有 Task Creator（{task.creator}）可以发起返工 / 新 Iteration。\n"
                f"  Front 负责把 Human 的返工意图路由给 Creator，"
                f"不直接修改 Task。"
            )
        return errors

    def apply_reopen(
        self,
        task: Task,
        *,
        actor: str,
        reason: str = "",
    ) -> HandlerResult:
        """
        Reopen：COMPLETED → IN_PROGRESS，`iteration += 1`。

        【为什么没有 incoming Message】
            Work 层已经不再使用 Agent 间消息机制。
            Reopen 由 Front 路由到 Task Creator 后，由应用层直接调用。

        【保留什么】
            · 旧 task_executions / submissions / verifications 全部保留；
            · Workspace 不变；
            · Lock 不变（不自动释放）。

        【不做什么】
            · 不新建 Task；
            · 不覆盖旧验收；
            · 不动 Wallet / Ledger。
        """
        errors = self.validate_reopen(task, actor)
        if errors:
            return HandlerResult(
                ok=False,
                action=HandlerAction.REOPENED,
                task_id=task.task_id,
                errors=tuple(errors),
            )

        previous_iteration = task.iteration
        task.reopen(reason=reason or None)
        saved = self.store.save(task)

        logger.info(
            "任务 %s 已 Reopen：iteration %s → %s，actor=%s",
            saved.task_id, previous_iteration, saved.iteration, actor,
        )
        return HandlerResult(
            ok=True,
            action=HandlerAction.REOPENED,
            task_id=saved.task_id,
            details={
                "iteration": saved.iteration,
                "previous_iteration": previous_iteration,
                "reason": reason,
                "actor": actor,
                "status": saved.status,
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
