"""
Task Service —— Task 应用层的门面（Facade / Orchestrator）
============================================================

【这个文件在架构里的位置】

                    Communication
                          │
                          ▼
                    TaskService          ← 本文件（薄门面）
                   /     |      \
                  /      |       \
                 ▼       ▼        ▼
             Handler  Negotiation  Mapper
             (按类型)   (业务规则)  (协议↔数据)
                          │
                          ▼
                    DecisionPolicy       ← 只做决定
                          │
                          ▼
                       Decision
                          │
                          ▼
                      Task Core          ← 只有它改状态
                          │
                          ▼
                      TaskStore          ← 只有它碰数据库

【为什么保留一个门面，而不是让调用方直接用 Handler】

    §A 明确要求：不要为了「架构漂亮」把 TaskService 完全消失。

    因为调用方（组合根、未来的 Agent Runtime、测试）需要的是一个
    稳定的入口：

        task_service.handle_message(message)
        task_service.candidates_for(task)

    它们不该知道内部用了几个 handler、几个 service。
    这样内部怎么拆，外面都不知道 —— 这才是门面的价值。

【重构前 vs 重构后】

    重构前：这一个类 1453 行，承担了 19 项职责，
            从协议解析一路做到数据库写入和成交凭证。

    重构后：它只做三件事：
            1. 持有并暴露应用层组件（组合）
            2. 把消息路由到对应的 Handler
            3. 提供稳定的公开 API（含向后兼容的转发方法）

【它刻意不做的事】

    ❌ 不解析 payload         → TaskMessageMapper
    ❌ 不实现议价规则         → TaskNegotiationService
    ❌ 不直接改数据库         → TaskStore
    ❌ 不做经济决定           → DecisionPolicy
    ❌ 不发消息               → 调用方
    ❌ 不选人                 → candidates_for 只返回候选列表
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from core.agent_directory import AgentDirectory, DirectoryEntry
from core.decision_policy import Decision, DecisionPolicy
from core.logging_setup import get_logger
from core.task import InvalidResponderError, Task, TaskValidationError
from core.task_store import TaskStore

from communication.message import Message

from services.task_execution_service import TaskExecutionService
from services.task_handlers import (
    TaskHandlerRegistry,
    default_handler_registry,
)
from services.task_message_mapper import TaskMessageMapper
from services.task_negotiation_service import TaskNegotiationService
from services.task_result import HandlerAction, HandlerResult

logger = get_logger(__name__)


class TaskService:
    """
    Task 应用层门面。

    用法：
        service = TaskService(store, directory, policies)

        # 委派侧（Step 4B）
        candidates = service.candidates_for(task)      # A 自己从中挑

        # 接收侧（由组合根接到通信层的类型处理器上）
        result = service.handle_message(message)       # 通用入口
        result = service.handle_offer(message)         # 兼容的专用入口

        # 发送侧（Step 4C）
        decision = service.decide(message)
        result = service.respond(message)
    """

    def __init__(
        self,
        store: TaskStore,
        directory: Optional[AgentDirectory] = None,
        policies: Optional[Mapping[str, DecisionPolicy]] = None,
        mapper: Optional[TaskMessageMapper] = None,
        handlers: Optional[TaskHandlerRegistry] = None,
        execution: Optional[TaskExecutionService] = None,
    ):
        self.store = store
        self.directory = directory
        self.mapper = mapper or TaskMessageMapper()

        # 议价业务组件。它持有 policies —— 门面通过属性代理过去，
        # 这样 `task_service.policies[...] = ...` 这种写法仍然有效
        # （组合根的演示会临时替换某个 Agent 的策略）。
        self.negotiation = TaskNegotiationService(
            store=store, policies=policies, mapper=self.mapper
        )

        # 执行阶段的业务组件（Step 4D）。
        #
        # 【它和议价服务完全并列，互相不知道】
        #     议价：OFFER / COUNTER_OFFER / ACCEPT / REJECT
        #     执行：START / SUBMIT / VERIFY / COMPLETE
        #     两者都继承 TaskApplicationService，
        #     共享幂等 / trace 缝合 / 协议违规回复，但不互相引用。
        self.execution = execution or TaskExecutionService(
            store=store, mapper=self.mapper
        )

        # 消息处理器。
        #
        # 【Step 4D 加了四种消息，这里改了什么】
        #     只是把 execution 多传了一个参数 ——
        #     门面的路由代码**一行都没改**。
        #     这正是上一轮重构要保住的扩展点。
        #
        # 【为什么允许注入】
        #     组合根可以传一个已经注册好的 registry 进来，
        #     门面不需要任何改动（测试就是这么造扩展点探针的）。
        self.handlers = handlers or default_handler_registry(
            negotiation=self.negotiation,
            execution=self.execution,
            mapper=self.mapper,
        )

    # ============================================================
    # 兼容属性（保持原 API 不炸）
    # ============================================================
    @property
    def policies(self) -> Dict[str, DecisionPolicy]:
        """
        每个 Agent 自己的决策策略。

        【为什么做成属性而不是普通字段】
            组合根（negotiation-demo）会临时替换某个 Agent 的策略：
                task_service.policies["agent_b"] = ...
            所以这必须是一个**可变的、和协商服务共享的**字典。
            做成代理属性，门面和协商服务看到的就是同一份数据。
        """
        return self.negotiation.policies

    def policy_for(self, agent_id: str) -> Optional[DecisionPolicy]:
        """取出某个 Agent 自己的决策策略（转发）。"""
        return self.negotiation.policy_for(agent_id)

    # ============================================================
    # 委派侧：找候选（**不选人**）
    # ============================================================
    def candidates_for(self, task: Task) -> List[DirectoryEntry]:
        """
        找出有能力承接这个任务的候选 Agent。

        【它只做一件事】
            把 task.required_capabilities 交给目录，拿回列表。

        【它刻意不做的事】
            · 不排序（除了一致的 agent_id 顺序）
            · 不评分
            · 不返回「最佳人选」
            · 不自动发 TASK_OFFER

        选谁是发起方 Agent 的决定 —— 这是 AI Economy 的核心行为。
        """
        if self.directory is None:
            raise RuntimeError(
                "TaskService 没有配置 AgentDirectory，无法查找候选 Agent。\n"
                "（构造时传入 directory=...）"
            )

        return self.directory.find_capable(task.required_capabilities)

    def can_be_done_by(self, task: Task, agent_id: str) -> bool:
        """
        某个 Agent 声明的能力是否覆盖这个任务的要求。

        注意：这**不是**「它会不会接」——
        只是「它声称自己会做」。
        """
        if self.directory is None:
            raise RuntimeError("TaskService 没有配置 AgentDirectory")

        entry = self.directory.get(agent_id)
        if entry is None:
            return False
        return entry.covers(task.required_capabilities)

    def explain_candidates(self, task: Task) -> Dict[str, Any]:
        """
        解释「为什么这些人是候选、别人为什么不是」。

        这不是决策辅助 —— 它不排序、不推荐。
        它只是把一个**布尔判断**展开成可读的信息，
        方便日志和演示理解匹配过程。
        """
        if self.directory is None:
            raise RuntimeError("TaskService 没有配置 AgentDirectory")

        required = task.required_capabilities
        candidates = []
        excluded = []

        for entry in self.directory.all():
            if entry.covers(required):
                candidates.append({
                    "agent_id": entry.agent_id,
                    "capabilities": entry.capabilities.names,
                })
            else:
                excluded.append({
                    "agent_id": entry.agent_id,
                    "capabilities": entry.capabilities.names,
                    "missing": sorted(entry.missing(required)),
                })

        return {
            "required": required.names,
            "candidates": candidates,
            "excluded": excluded,
        }

    # ============================================================
    # 接收侧：统一入口 + 兼容的专用入口
    # ============================================================
    def handle_message(self, message: Message) -> HandlerResult:
        """
        **通用消息入口** —— 按消息类型路由到对应的 Handler。

        这是组合根应该注册到通信层的方法：

            communication.register_type_handler(
                MessageType.TASK_OFFER, task_service.handle_message
            )

        【注意】返回值是 HandlerResult，不是 Message。
               组合根需要一个很薄的适配（见 main.py 的 _wire_task_handlers）。
               这样通信层不需要知道 HandlerResult 是什么。

        【未注册的类型】
            明确抛 KeyError，而不是静默忽略 ——
            静默忽略会让「消息发出去了但没人处理」变成一个查不出的 bug。
        """
        if not isinstance(message, Message):
            raise TypeError(
                f"handle_message 需要 Message，收到 {type(message).__name__}"
            )

        handler = self.handlers.require(message.message_type)
        logger.debug(
            "路由 %s → %s", message.message_type, type(handler).__name__
        )
        return handler.handle(message)

    # --- 以下是保持向后兼容的专用入口（转调 handler）---
    def handle_offer(self, message: Message) -> HandlerResult:
        """处理 TASK_OFFER（兼容入口，内部转调 OfferHandler）。"""
        return self.handlers.require("TASK_OFFER").handle(message)

    def handle_counter_offer(self, message: Message) -> HandlerResult:
        """处理 COUNTER_OFFER（兼容入口）。"""
        return self.handlers.require("COUNTER_OFFER").handle(message)

    def handle_accept(self, message: Message) -> HandlerResult:
        """处理 ACCEPT（兼容入口）。"""
        return self.handlers.require("ACCEPT").handle(message)

    def handle_reject(self, message: Message) -> HandlerResult:
        """处理 REJECT（兼容入口）。"""
        return self.handlers.require("REJECT").handle(message)

    # --- Step 4D：执行阶段的四个兼容入口 ---
    def handle_start(self, message: Message) -> HandlerResult:
        """处理 START（兼容入口，内部转调 StartHandler）。"""
        return self.handlers.require("START").handle(message)

    def handle_submit(self, message: Message) -> HandlerResult:
        """处理 SUBMIT（兼容入口）。"""
        return self.handlers.require("SUBMIT").handle(message)

    def handle_verify(self, message: Message) -> HandlerResult:
        """处理 VERIFY（兼容入口）。"""
        return self.handlers.require("VERIFY").handle(message)

    def handle_complete(self, message: Message) -> HandlerResult:
        """处理 COMPLETE（兼容入口）。"""
        return self.handlers.require("COMPLETE").handle(message)

    # ============================================================
    # 决策（转发给协商服务）
    # ============================================================
    def build_context(self, task: Task, responder: str, message: Message):
        """把「任务 + 消息」翻译成策略能看的上下文（转发）。"""
        return self.negotiation.build_context(task, responder, message)

    def decide(self, message: Message) -> Decision:
        """
        问策略「我该怎么办」。

        【这个方法**不修改任何状态**】
            不改 Task、不改报价历史、不发消息。
            这样决策可以被单独测试、记录、重放。
        """
        return self.negotiation.decide(message)

    def respond(self, message: Message) -> HandlerResult:
        """
        收到一条需要回应的报价 → 问策略 → 执行决策。

        【这是编排，不是决策本身】
            真正的决定由 Agent 自己的 DecisionPolicy 做出；
            这里只负责「问」和「把决定变成合法的业务操作」。
        """
        decision = self.negotiation.decide(message)
        logger.debug(
            "任务 %s 的决策：%s（策略 %s）",
            message.payload.get("task_id"), decision, decision.policy,
        )
        return self.execute(message, decision)

    def execute(self, message: Message, decision: Decision) -> HandlerResult:
        """
        把一个已经确定的决策落到**本地**任务上，并返回要给对方的应答。

        【这里有一个容易搞混的地方，必须说清楚】

            有两类入口，方向相反：

            ① 发送侧：我**收到**对方的报价，决定回应什么
                   respond(incoming) → execute() → 改本地状态 + 返回要发的内容

            ② 接收侧：我**收到**对方的 ACCEPT / REJECT / COUNTER_OFFER
                   handle_accept(incoming) / handle_reject(...) / ...

            execute() 属于 ①。所以它**不能**把自己要发出去的那条消息
            再喂给 handle_* —— 那些方法模拟的是"收到"，
            而发出去的消息 to_agent 是**对方**，会被误判成"对方在做决定"。

            （这个坑真的踩过：B 的还价被当成 A 发的，触发了轮次校验。）

        【返回什么】
            返回的 HandlerResult 带着 reply_type / reply_payload / reply_message，
            调用方用 result.to_reply(incoming) 就能得到要发出去的消息。

        【注意顺序：先决策，再校验，最后才改状态】
            策略说的不算数 —— 它可能给出一个违反协议的决定
            （比如还价超过预算）。校验在协商服务，执行在 Task Core。
        """
        if not isinstance(decision, Decision):
            raise TypeError(
                f"execute 需要 Decision，收到 {type(decision).__name__}"
            )

        task_id = message.payload.get("task_id")
        task = self.store.get(task_id) if task_id else None
        if task is None:
            raise TaskValidationError(f"本地没有任务 {task_id!r}，无法执行决策")

        # 【发送侧的行动方是「我们」】—— 我们收到了对方的报价，正在决定怎么回。
        actor = message.to_agent
        if not task.is_participant(actor):
            raise InvalidResponderError(
                f"{actor} 不是任务 {task.task_id} 的参与方"
            )

        if decision.accepted:
            # 我接受的是**对方刚发来的这条**报价
            outgoing = self.mapper.accept_reply(
                message, task.task_id, decision.price
            )
            return self.negotiation.apply_accept(
                task, decision.price, incoming=message,
                accepted_offer_message_id=message.message_id,
                accepted_offer_type=message.message_type,
                outgoing=outgoing,
                # 【发送侧】是**我**决定接受对方的报价，
                # 所以接受方是这条消息的收件人（我）。
                accept_author=message.to_agent,
            )

        if decision.countered:
            outgoing = self.mapper.counter_offer_reply(
                message, task.task_id, decision.price
            )
            return self.negotiation.counter_locally(
                task, decision.price, incoming=message, outgoing=outgoing
            )

        outgoing = self.mapper.reject_reply(
            message, task.task_id, decision.reason
        )
        return self.negotiation.reject_locally(
            task, decision.reason, incoming=message, outgoing=outgoing
        )

    # ============================================================
    def describe(self) -> Dict[str, Any]:
        return {
            "service": type(self).__name__,
            "directory": (
                type(self.directory).__name__ if self.directory else None
            ),
            "tasks": self.store.count(),
            "handlers": self.handlers.types(),
            "policies": sorted(self.policies),
            "execution": type(self.execution).__name__,
        }

    def __repr__(self) -> str:
        return (
            f"TaskService(directory="
            f"{type(self.directory).__name__ if self.directory else None}, "
            f"handlers={self.handlers.types()})"
        )
