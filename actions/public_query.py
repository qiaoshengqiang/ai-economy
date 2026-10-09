"""
PublicQueryExecutor —— 把「查公共资料」接进 Action Seam
=========================================================

    ActionIntent(public_query) → PublicQueryExecutor
        → 注入的 query_fn（由组合根提供）
        → ActionResult

【它是什么】

    Agent 查**公共资料区**：读一份资料，或者列出有哪些。

    `parameters`：
        {"name": "SYSTEM_MAP.md"}    读那一份
        {"list": true}  或  {}        列出有哪些

【🔒 边界（与另外三个执行器一致）】

① **只执行，不决策**
     ❌ 不替 Agent 挑该读哪一份
     ❌ 失败不重试、不换名字

② **不 import `services/`**
     `actions/` 是叶子层。只拿一个**注入的** `query_fn`。

③ **失败也是 ActionResult**（§十七），不是异常。

【🔒 只读】

    本执行器只能**读**。公共资料区没有写接口 ——
    不是「有但不给用」，是结构上不存在。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

#: 这个执行器认领的 action_type。
ACTION_PUBLIC_QUERY = "public_query"

#: `query_fn` 的成功返回里，哪些字段值得带进 `ActionResult.output`。
_OUTPUT_FIELDS = ("name", "chars", "content", "names", "count")


class PublicQueryExecutor:
    """
    执行 `action_type = "public_query"` 的 ActionIntent。

    🔒 依赖以**构造注入**给出：`query_fn` 的签名与
       `services.public_reference.PublicReference.query` 一致。
    """

    action_types = frozenset({ACTION_PUBLIC_QUERY})

    def __init__(self, query_fn: Callable[..., Any]) -> None:
        if query_fn is None or not callable(query_fn):
            raise ValueError(
                "PublicQueryExecutor 需要一个可调用的 query_fn —— "
                "它由组合根注入（例如 PublicReference().query）")
        self.query_fn = query_fn

    # ------------------------------------------------------------
    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        # `list: true` 与「不给 name」等价 —— 都是「列出有哪些」。
        listing = bool(params.get("list")) or not str(
            params.get("name") or "").strip()
        name = None if listing else params.get("name")

        try:
            raw = self.query_fn(name)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error", name=str(name or ""))

        if not isinstance(raw, dict):
            return self._fail(intent,
                              f"query_fn 返回了 {type(raw).__name__}，期望 dict",
                              code="invalid_query_result", name=str(name or ""))

        if raw.get("ok"):
            output = {k: raw[k] for k in _OUTPUT_FIELDS if k in raw}
            return ActionResult(
                success=True, output=output, error=None,
                action_id=intent.intent_id, action_type=intent.action_type,
                metadata={"code": None},
            )

        return self._fail(intent,
                          str(raw.get("message") or "查询失败"),
                          code=str(raw.get("code") or "unknown"),
                          name=str(raw.get("name") or name or ""),
                          extra={"available": raw.get("available")})

    # ------------------------------------------------------------
    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, name: str, extra: Any = None) -> ActionResult:
        metadata: dict = {"code": code, "name": name}
        if extra:
            metadata["available"] = extra
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id, action_type=intent.action_type,
            metadata=metadata,
        )
