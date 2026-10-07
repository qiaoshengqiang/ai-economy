"""
Action Executor —— **机械执行**，不做任何决策
============================================================

【🔴 §九：Executor 的职责只有一件事】

    「收到一个**已经形成**的 ActionIntent，然后执行它。」

    它**不能**：
        ❌ 选择 Strategy
        ❌ 决定要不要行动
        ❌ 修改 Self Model
        ❌ 判断经济价值
        ❌ 判断任务优先级

    也就是说：
        ✅ Agent → Decision → ActionIntent → Executor
        ❌ Runtime → Executor → Executor 决定 Agent 应该做什么

【§二十 的守卫】

    Dispatcher 会记下「它到底执行了哪个 intent」
    （`last_dispatched`），这样测试可以断言：

        Dispatcher 执行的 intent **就是**传进来的那一个，
        没有因为 Strategy / confidence / cost 变化而被换掉。

【§二十一：可替换】

    Executor 是**注入**的。Agent 不依赖 subprocess / filesystem。
    未来可以接 World Gateway；现在用 ReplyExecutor / FakeExecutor。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from actions.intent import (
    ACTION_NOOP, ACTION_OBSERVE, ACTION_REPLY,
    ActionResult, ActionIntent,
)
from core.logging_setup import get_logger

logger = get_logger(__name__)


@runtime_checkable
class ActionExecutor(Protocol):
    """最小的执行器契约。**只执行，不决策。**"""

    #: 这个执行器能处理哪些 action_type
    action_types: Any

    def execute(self, intent: ActionIntent) -> ActionResult:
        ...


def _timed(fn):
    def wrapper(self, intent: ActionIntent) -> ActionResult:
        t0 = time.perf_counter()
        try:
            result = fn(self, intent)
        except Exception as exc:                          # noqa: BLE001
            # §十七：执行失败要变成 ActionResult，
            # **不是**让 Runtime 崩。
            # ⚠️ 但这只包住**执行期**的失败；
            #    程序性 bug 仍然会在日志里带 traceback 出现。
            logger.error("Action 执行失败 %s(%s): %s",
                         type(self).__name__, intent.action_type, exc)
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        if result.duration_ms == 0.0:
            result = ActionResult(
                success=result.success, output=result.output,
                error=result.error, action_id=result.action_id,
                action_type=result.action_type,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
                metadata=result.metadata,
            )
        return result
    return wrapper


class ReplyExecutor:
    """
    第一版最低风险的 Action（§十 / §十一 方案 A）。

    🔒 它**不写** host filesystem。
       §十一：Digital Production World 的文件写入必须走
       Action → Capability → Gateway → Workspace → Filesystem，
       那些层现在还不存在。
    """

    action_types = frozenset({ACTION_REPLY})

    @_timed
    def execute(self, intent: ActionIntent) -> ActionResult:
        content = (intent.parameters or {}).get("content", "")
        return ActionResult(
            success=True, output=str(content),
            action_id=intent.intent_id, action_type=intent.action_type,
        )


class NoopExecutor:
    """测试 / 占位用的 Action（§十）。什么都不做，但**真的走了一遍**。"""

    action_types = frozenset({ACTION_NOOP, ACTION_OBSERVE})

    @_timed
    def execute(self, intent: ActionIntent) -> ActionResult:
        return ActionResult(
            success=True, output=None,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata={"noop": True},
        )


class ActionDispatcher:
    """
    按 `action_type` 找到执行器并执行。

    🔒 **它不改变 intent。** 它只是「路由 + 执行」。
       未知 action_type → `ActionResult(success=False)`，
       **不是**异常，也**不是**回退到别的 Action。
    """

    def __init__(self, executors: Optional[list] = None) -> None:
        self._executors: Dict[str, Any] = {}
        #: §二十 的守卫：记下**实际被执行的那个 intent**（原样，不改写）
        self.last_dispatched: Optional[ActionIntent] = None
        for ex in (executors if executors is not None
                   else [ReplyExecutor(), NoopExecutor()]):
            self.register(ex)

    def register(self, executor: Any) -> None:
        types = getattr(executor, "action_types", None)
        if types is None:
            raise ValueError("executor 必须有 action_types")
        for t in (types if not isinstance(types, str) else [types]):
            self._executors[str(t)] = executor

    def supports(self, action_type: str) -> bool:
        return str(action_type) in self._executors

    @property
    def action_types(self) -> list:
        return sorted(self._executors)

    def dispatch(self, intent: ActionIntent) -> ActionResult:
        """执行一个 intent。**原样执行，不改写。**"""
        self.last_dispatched = intent
        executor = self._executors.get(str(intent.action_type))
        if executor is None:
            return ActionResult(
                success=False,
                error=(f"no executor for action_type "
                       f"{intent.action_type!r}; "
                       f"registered={self.action_types}"),
                action_id=intent.intent_id,
                action_type=intent.action_type,
            )
        # §十七：执行失败要变成 ActionResult，**不是**让 Runtime 崩。
        # 🔒 这里兜住的是**注入的** executor（内置那几个由 _timed 兜）。
        #    兜住不等于隐藏 —— traceback 仍然进日志。
        t0 = time.perf_counter()
        try:
            return executor.execute(intent)
        except Exception as exc:                          # noqa: BLE001
            logger.error("Action 执行异常 %s: %s", intent.action_type, exc,
                         exc_info=True)
            return ActionResult(
                success=False, error=f"{type(exc).__name__}: {exc}",
                action_id=intent.intent_id,
                action_type=intent.action_type,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
