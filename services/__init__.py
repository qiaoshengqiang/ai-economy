"""
services —— 业务服务层（消息与领域之间的适配）
============================================================

【为什么单独一层】

    core/            领域对象（Task / Money / Capability / Directory / Policy）
    communication/   把消息送到
    services/        把消息翻译成领域操作   ← 本层

    依赖方向是单向的：

        main.py（组合根）
           ↓
        services/  →  core/  +  communication/  +  storage/

    communication/ 不依赖 services/，core/ 谁都不依赖。
    有架构守卫盯着这两条线。

【为什么适配层不能放进 communication/】

    因为 communication/ 必须能在完全不知道 Task 存在的情况下工作 ——
    这样才能单独使用、单独测试、单独换成网络实现。

【本层的模块划分（一次「Task Application Layer Refactor」的结果）】

    task_service.py              门面 / 编排（对外唯一入口）
    task_handlers.py             按消息类型隔离的处理器 + 注册表
    task_negotiation_service.py  议价业务规则 + 状态落地
    task_message_mapper.py       协议 payload ↔ 应用数据
    task_result.py               所有处理器的返回类型

    数据流：

        Message
          ↓
        TaskService.handle_message()      路由
          ↓
        Handler（按类型）                  编排
          ↓  ┌──────────────┬─────────────┐
             ↓              ↓             ↓
          Mapper      Negotiation    （DecisionPolicy）
        （解析）      （规则+落地）      （只做决定）

    每个模块只有一个主要变化原因：
        协议变了     → mapper
        议价规则变了 → negotiation_service
        加消息类型   → handlers（+ 组合根注册一行）
        对外 API 变了 → task_service
        返回结构变了 → task_result
"""

from services.task_application_service import TaskApplicationService
from services.task_execution_service import TaskExecutionService
from services.task_handlers import (
    AcceptHandler,
    CompleteHandler,
    CounterOfferHandler,
    ExecutionMessageHandler,
    NegotiationMessageHandler,
    OfferHandler,
    RejectHandler,
    StartHandler,
    SubmitHandler,
    TaskHandlerRegistry,
    TaskMessageHandler,
    VerifyHandler,
    default_handler_registry,
)
from services.task_message_mapper import (
    AcceptIntent,
    CompleteIntent,
    CounterOfferIntent,
    MessageIntent,
    OfferIntent,
    RejectIntent,
    StartIntent,
    SubmitIntent,
    TaskMessageMapper,
    VerifyIntent,
    parse_money,
)
from services.task_negotiation_service import TaskNegotiationService
from services.task_result import HandlerAction, HandlerResult
from services.task_service import TaskService

__all__ = [
    # 门面
    "TaskService",
    # 应用服务基类
    "TaskApplicationService",
    # 执行（Step 4D）
    "TaskExecutionService",
    "StartHandler",
    "SubmitHandler",
    "VerifyHandler",
    "CompleteHandler",
    "ExecutionMessageHandler",
    "NegotiationMessageHandler",
    "StartIntent",
    "SubmitIntent",
    "VerifyIntent",
    "CompleteIntent",
    # 结果
    "HandlerResult",
    "HandlerAction",
    # 处理器
    "TaskMessageHandler",
    "TaskHandlerRegistry",
    "OfferHandler",
    "CounterOfferHandler",
    "AcceptHandler",
    "RejectHandler",
    "default_handler_registry",
    # 议价
    "TaskNegotiationService",
    # 映射
    "TaskMessageMapper",
    "MessageIntent",
    "OfferIntent",
    "CounterOfferIntent",
    "AcceptIntent",
    "RejectIntent",
    "parse_money",
]
