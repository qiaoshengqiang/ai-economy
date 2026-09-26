"""
services —— 业务服务层（消息与领域之间的适配）
============================================================

【为什么单独一层】

    core/            领域对象（Task / Money / Capability / Directory）
    communication/   把消息送到
    services/        把消息翻译成领域操作   ← 本层

    依赖方向是单向的：

        main.py（组合根）
           ↓
        services/  →  core/  +  communication/  +  storage/

    communication/ 不依赖 services/，core/ 谁都不依赖。

【为什么适配层不能放进 communication/】

    因为 communication/ 必须能在完全不知道 Task 存在的情况下工作 ——
    这样才能单独使用、单独测试、单独换成网络实现。
    有架构守卫盯着这条线。

【目前有什么】

    task_service.py   TaskService
        · candidates_for(task)       → 找候选（不选人）
        · handle_offer(message)      → 处理 TASK_OFFER
        · handle_counter_offer(msg)  → 处理 COUNTER_OFFER（含预算校验）
"""

from services.task_service import HandlerResult, TaskService

__all__ = [
    "TaskService",
    "HandlerResult",
]
