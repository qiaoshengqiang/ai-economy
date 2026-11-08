"""
WorkspaceSearchExecutor —— 在 Task Workspace 里做只读字面搜索
===============================================================

    ActionIntent(workspace_search)
        → WorkspaceSearchExecutor
        → 注入的 search_fn（services.artifacts.search_artifacts）
        → ActionResult

【🔒 边界】

① 只读；不建索引、不缓存、不写回；
② 走 WorkspaceGateway 的参与者检查与读取边界；
③ 不 import services/core —— 只拿注入的 search_fn；
④ 失败也是 ActionResult。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_WORKSPACE_SEARCH = "workspace_search"

_OUTPUT_FIELDS = (
    "path", "glob", "query", "matches", "total_matches",
    "files_scanned", "truncated",
)


class WorkspaceSearchExecutor:
    action_types = frozenset({ACTION_WORKSPACE_SEARCH})

    def __init__(self, search_fn: Callable[..., Any]) -> None:
        if search_fn is None or not callable(search_fn):
            raise ValueError(
                "WorkspaceSearchExecutor 需要一个可调用的 search_fn —— "
                "它由组合根注入（例如 services.artifacts.search_artifacts）")
        self.search_fn = search_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        query = str(params.get("query") or "")
        if not query:
            return self._fail(intent, "query 不能为空",
                              code="bad_query")
        try:
            raw = self.search_fn(
                intent.agent_id,
                query,
                path=str(params.get("path") or ""),
                glob=str(params.get("glob") or ""),
                max_results=params.get("max_results", 20),
                case_sensitive=bool(params.get("case_sensitive", True)),
                context_lines=params.get("context_lines", 0),
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error")

        if not isinstance(raw, dict):
            return self._fail(
                intent,
                f"search_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_search_result")
        if raw.get("ok"):
            output = {k: raw[k] for k in _OUTPUT_FIELDS if k in raw}
            return ActionResult(
                success=True, output=output, error=None,
                action_id=intent.intent_id,
                action_type=intent.action_type,
                metadata={"code": None})
        return self._fail(
            intent, str(raw.get("message") or "搜索失败"),
            code=str(raw.get("code") or "unknown"),
            path=str(raw.get("path") or ""))

    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, **extra: Any) -> ActionResult:
        metadata = {"code": code}
        metadata.update(extra)
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata=metadata)
