"""
读共同认知的执行器 —— `shared_context_read`
=============================================

    ActionIntent(shared_context_read)
        → SharedContextReadExecutor
        → 注入的 read_fn（由组合根绑定 alignment + task_id）
        → ActionResult

【🔴 它补的是哪一个缺口】

    Shared Context 有**写**通道（`assign_acceptance`），没有**读**通道：

        Agent 写了 objective / constraints / acceptance_criteria /
        decisions / assumptions / facts / assignments，
        而它自己**一个字都看不到**。

    ⇒ 「共同认知」对唯一需要它的人不可见。

    🔒 `TaskAlignmentService.snapshot()` 早就写着自己是
       「生成一个**可消费的读取视图**」—— 它缺的只是**一个 Agent
       够得到的入口**。本模块就是那个入口。

【🔒 三条边界（与其它执行器完全一致）】

① **只执行，不决策**

     ❌ 不替 Agent 挑要看哪一版（读的就是**当前**那一版）
     ❌ 不总结、不摘录、不判断"哪条重要"
     ❌ 不做任何推断 —— 原样搬运 snapshot 的字段

② **不 import `services/` `core/`**

     `actions/` 是**叶子层**。它只拿一个注入的 `read_fn`（鸭子类型），
     由组合根决定接哪个实现。
     所以这一层结构上不认识 TaskAlignmentService、更不认识数据库。

③ **失败也是 ActionResult**，不是异常

【🔒 没有参数】

     `parameters` 一概不读。
     读哪个 Task 由组合根绑定；以什么身份读由 `intent.agent_id` 决定
     —— **两者 Agent 都指定不了**。

     为什么不给 `revision` 参数：那会让「读共同认知」变成
     「翻历史」。历史是审计材料，不是本轮判断的输入；
     要给的话应该是一个**独立**的动作，而不是这个动作的一个开关。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from actions.intent import ActionResult, ActionIntent

#: 这个执行器认领的 action_type。
#:
#: 🔒 刻意**不**加进 `actions/intent.py` 的 `BUILTIN_ACTION_TYPES`：
#:    那是「框架内置、任何调用方都自动具备」的集合。
#:    读共同认知与 workspace_read 一样，是**由组合根按需注册**的能力。
ACTION_SHARED_CONTEXT_READ = "shared_context_read"

#: `read_fn` 的成功返回里，哪些字段值得带进 `ActionResult.output`。
#:
#: 🔒 这些**全部**是 `ContextSnapshot.to_dict()` 真实返回的键，
#:    这里不新增、不重命名、不推断。
#:
#: `context` 必须在列 —— 没有它，「读到共同认知」这件事就没有发生。
_OUTPUT_FIELDS = (
    "task_id", "context_id", "revision", "taken_at",
    "context",                 # 共同认知本身（objective/constraints/…）
    "domain_state",            # 现场读 Task Core 的**引用**
    "domain_state_source",     # = "task_core"，标明它不是副本
    "recent_changes",          # 最近的变更摘要
)


class SharedContextReadExecutor:
    """
    执行 `action_type = "shared_context_read"` 的 ActionIntent。

    🔒 `alignment` 与 `task_id` **由组合根绑定** ——
       Agent 既看不到 alignment 服务，也无法指定**别的** task。
       **Agent 不需要、也不允许提供任何 parameters。**
    """

    action_types = frozenset({ACTION_SHARED_CONTEXT_READ})

    def __init__(self, read_fn: Callable[..., Any]) -> None:
        if read_fn is None or not callable(read_fn):
            raise ValueError(
                "SharedContextReadExecutor 需要一个可调用的 read_fn —— "
                "它由组合根注入（例如 services.context_ops.read_shared_context）")
        self.read_fn = read_fn

    # ------------------------------------------------------------
    def execute(self, intent: ActionIntent) -> ActionResult:
        """
        执行一次读取。**没有参数，不重试，不解释。**

        🔒 失败也是 ActionResult（§十七），不是异常。
           唯一的 `except` 是防「注入的 read_fn 自己崩了」——
           那也必须变成一个**结构化**的失败结果。
        """
        try:
            # 🔴 读取方**只**来自 intent —— 绝不可从 parameters 读。
            #    否则 Agent 能冒充别人去读它读不到的 Task。
            raw = self.read_fn(by=intent.agent_id)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")

        if not isinstance(raw, dict):
            return self._fail(
                intent,
                f"read_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_read_result")

        if raw.get("ok"):
            output = {k: raw[k] for k in _OUTPUT_FIELDS if k in raw}
            return ActionResult(
                success=True,
                output=output,
                error=None,
                action_id=intent.intent_id,
                action_type=intent.action_type,
                # 🔒 成功没有错误码 —— 显式 None，不编一个 "ok" 出来。
                metadata={"code": None},
            )

        return self._fail(intent,
                          str(raw.get("message") or "读取共同认知失败"),
                          code=str(raw.get("code") or "unknown"))

    # ------------------------------------------------------------
    def _fail(self, intent: ActionIntent, message: str, *,
              code: str) -> ActionResult:
        """
        统一的失败结果。

        🔒 `output` 刻意留空 —— 失败时**没有**「读出来的共同认知」，
           编一个空 dict 会让人以为读过（那正是本项目的
           「接口成功掩盖语义空转」bug 类）。
           真正的事实放在 `metadata`（机器读）与 `error`（人读）。
        """
        return ActionResult(
            success=False,
            output=None,
            error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": code},
        )


__all__ = ["ACTION_SHARED_CONTEXT_READ", "SharedContextReadExecutor"]
