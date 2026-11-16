"""
Draft Actions —— 私有草稿与批量落盘
=====================================

    draft_write / draft_append / draft_read / draft_list / draft_delete
    workspace_promote_drafts

【边界】

① `draft_*` 只操作**当前 Agent 自己的**草稿；不是项目文件；
② `workspace_promote_drafts` 是**显式**落盘动作：
   一次可提交多个 `{draft_id, path, write_mode}`，由组合根通过
   Workspace Gateway 顺序写入；不是自动 promotion；
③ actions/ 仍是叶子层：只拿注入的 fn，不认识 Gateway / 数据库；
④ 失败/部分失败都返回结构化 ActionResult。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_DRAFT_WRITE = "draft_write"
ACTION_DRAFT_APPEND = "draft_append"
ACTION_DRAFT_READ = "draft_read"
ACTION_DRAFT_LIST = "draft_list"
ACTION_DRAFT_DELETE = "draft_delete"
ACTION_WORKSPACE_PROMOTE_DRAFTS = "workspace_promote_drafts"

_DRAFT_META_FIELDS = (
    "draft_id", "label", "chars", "sha256", "preview",
    "created_at", "updated_at",
)
_READ_FIELDS = (
    "draft_id", "label", "start_char", "max_chars", "next_start_char",
    "chars", "total_chars", "content", "truncated", "sha256",
)


class _DraftBase:
    action_types: frozenset = frozenset()
    _allowed: frozenset = frozenset()

    def __init__(self, fn: Callable[..., Any]) -> None:
        if fn is None or not callable(fn):
            raise ValueError(
                f"{type(self).__name__} 需要一个可调用的 fn（由组合根注入）")
        self.fn = fn

    def _params(self, intent: ActionIntent) -> tuple[dict, ActionResult | None]:
        params = dict(getattr(intent, "parameters", None) or {})
        unknown = sorted(set(params) - set(self._allowed))
        if unknown:
            return params, self._fail(
                intent,
                f"{intent.action_type} 不认识的参数：{unknown}；"
                f"只支持 {sorted(self._allowed)}",
                code="unknown_parameters", unknown=unknown)
        return params, None

    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, **extra: Any) -> ActionResult:
        metadata = {"code": code}
        metadata.update(extra)
        return ActionResult(
            success=False, output=None, error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata=metadata)

    def _ok(self, intent: ActionIntent, output: dict) -> ActionResult:
        return ActionResult(
            success=True, output=output, error=None,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None})


class DraftWriteExecutor(_DraftBase):
    action_types = frozenset({ACTION_DRAFT_WRITE})
    _allowed = frozenset({"content", "label"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        content = params.get("content")
        if not isinstance(content, str):
            return self._fail(intent, "content 必须是字符串",
                              code="bad_content")
        try:
            raw = self.fn(intent.agent_id, content,
                          label=str(params.get("label") or ""))
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict):
            return self._fail(intent, "draft_write 返回的不是对象",
                              code="invalid_result")
        if not raw.get("ok"):
            return self._fail(intent,
                              str(raw.get("message") or "draft_write 失败"),
                              code=str(raw.get("code") or "unknown"))
        return self._ok(intent, {
            k: raw[k] for k in _DRAFT_META_FIELDS if k in raw})


class DraftAppendExecutor(_DraftBase):
    action_types = frozenset({ACTION_DRAFT_APPEND})
    _allowed = frozenset({"draft_id", "content"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        draft_id = str(params.get("draft_id") or "").strip()
        if not draft_id:
            return self._fail(intent, "draft_append 需要 draft_id",
                              code="missing_draft_id")
        content = params.get("content")
        if not isinstance(content, str):
            return self._fail(intent, "content 必须是字符串",
                              code="bad_content")
        try:
            raw = self.fn(intent.agent_id, draft_id, content)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict):
            return self._fail(intent, "draft_append 返回的不是对象",
                              code="invalid_result")
        if not raw.get("ok"):
            return self._fail(intent,
                              str(raw.get("message") or "draft_append 失败"),
                              code=str(raw.get("code") or "unknown"))
        return self._ok(intent, {
            k: raw[k] for k in _DRAFT_META_FIELDS if k in raw})


class DraftReadExecutor(_DraftBase):
    action_types = frozenset({ACTION_DRAFT_READ})
    _allowed = frozenset(
        {"draft_id", "start_char", "max_chars", "offset", "limit"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        draft_id = str(params.get("draft_id") or "").strip()
        if not draft_id:
            return self._fail(intent, "draft_read 需要 draft_id",
                              code="missing_draft_id")
        # 旧字段名 -> 字符字段；新 prompt 只发 start_char / max_chars。
        if "start_char" in params and "offset" in params:
            try:
                same = int(params["start_char"]) == int(params["offset"])
            except (TypeError, ValueError):
                same = False
            if not same:
                return self._fail(
                    intent, "不要同时使用 start_char 和 offset",
                    code="bad_pagination")
        if "max_chars" in params and "limit" in params:
            try:
                same = int(params["max_chars"]) == int(params["limit"])
            except (TypeError, ValueError):
                same = False
            if not same:
                return self._fail(
                    intent, "不要同时使用 max_chars 和 limit",
                    code="bad_pagination")
        if "start_char" not in params and "offset" in params:
            params["start_char"] = params.pop("offset")
        if "max_chars" not in params and "limit" in params:
            params["max_chars"] = params.pop("limit")
        try:
            raw = self.fn(
                intent.agent_id, draft_id,
                start_char=params.get("start_char", 0),
                max_chars=params.get("max_chars", 12_000))
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict):
            return self._fail(intent, "draft_read 返回的不是对象",
                              code="invalid_result")
        if not raw.get("ok"):
            return self._fail(intent,
                              str(raw.get("message") or "draft_read 失败"),
                              code=str(raw.get("code") or "unknown"))
        return self._ok(intent, {
            k: raw[k] for k in _READ_FIELDS if k in raw})


class DraftListExecutor(_DraftBase):
    action_types = frozenset({ACTION_DRAFT_LIST})
    _allowed = frozenset()

    def execute(self, intent: ActionIntent) -> ActionResult:
        _, failed = self._params(intent)
        if failed is not None:
            return failed
        try:
            raw = self.fn(intent.agent_id)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict) or not raw.get("ok"):
            return self._fail(intent, "draft_list 失败",
                              code="list_failed")
        return self._ok(intent, {
            "drafts": raw.get("drafts") or [],
            "count": raw.get("count") or 0})


class DraftDeleteExecutor(_DraftBase):
    action_types = frozenset({ACTION_DRAFT_DELETE})
    _allowed = frozenset({"draft_id"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        draft_id = str(params.get("draft_id") or "").strip()
        if not draft_id:
            return self._fail(intent, "draft_delete 需要 draft_id",
                              code="missing_draft_id")
        try:
            raw = self.fn(intent.agent_id, draft_id)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict) or not raw.get("ok"):
            return self._fail(intent,
                              str((raw or {}).get("message") or "draft_delete 失败"),
                              code=str((raw or {}).get("code") or "unknown"))
        return self._ok(intent, {"draft_id": draft_id})


class WorkspacePromoteDraftsExecutor(_DraftBase):
    """
    一次 Action 落盘多个文件。

    items:
        [{"draft_id": "...", "path": "src/a.js", "write_mode": "create"}, ...]

    执行由组合根负责：顺序走 Workspace Gateway；返回 per-item results。
    """
    action_types = frozenset({ACTION_WORKSPACE_PROMOTE_DRAFTS})
    _allowed = frozenset({"items"})
    _ITEM_KEYS = frozenset({"draft_id", "path", "write_mode"})
    _MODES = frozenset({"create", "replace", "append"})

    def execute(self, intent: ActionIntent) -> ActionResult:
        params, failed = self._params(intent)
        if failed is not None:
            return failed
        items = params.get("items")
        if not isinstance(items, list) or not items:
            return self._fail(
                intent, "workspace_promote_drafts 需要非空 items 列表",
                code="empty_items")
        clean = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return self._fail(
                    intent, f"items[{index}] 必须是对象",
                    code="invalid_item", index=index)
            unknown = sorted(set(item) - self._ITEM_KEYS)
            if unknown:
                return self._fail(
                    intent,
                    f"items[{index}] 不认识的字段：{unknown}",
                    code="unknown_item_fields", index=index)
            draft_id = str(item.get("draft_id") or "").strip()
            path = str(item.get("path") or "").strip()
            mode = str(item.get("write_mode") or "").strip().lower()
            if not draft_id or not path:
                return self._fail(
                    intent,
                    f"items[{index}] 需要 draft_id 和 path",
                    code="invalid_item", index=index)
            if mode not in self._MODES:
                return self._fail(
                    intent,
                    f"items[{index}] 的 write_mode 必须是 "
                    f"{sorted(self._MODES)}",
                    code="bad_write_mode", index=index)
            clean.append({"draft_id": draft_id, "path": path,
                          "write_mode": mode})
        try:
            raw = self.fn(intent.agent_id, clean)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(intent, f"{type(exc).__name__}: {exc}",
                              code="executor_error")
        if not isinstance(raw, dict):
            return self._fail(intent, "promote 返回的不是对象",
                              code="invalid_result")
        results = raw.get("results") or []
        summary = str(raw.get("summary") or "")
        if raw.get("ok"):
            return self._ok(intent, {"results": results,
                                     "summary": summary})
        return ActionResult(
            success=False,
            output={"results": results, "summary": summary},
            error=summary or "部分或全部文件 promotion 失败",
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": "promote_partial_or_failed"},
        )


__all__ = [
    "ACTION_DRAFT_WRITE", "ACTION_DRAFT_APPEND", "ACTION_DRAFT_READ",
    "ACTION_DRAFT_LIST", "ACTION_DRAFT_DELETE",
    "ACTION_WORKSPACE_PROMOTE_DRAFTS",
    "DraftWriteExecutor", "DraftAppendExecutor", "DraftReadExecutor",
    "DraftListExecutor", "DraftDeleteExecutor",
    "WorkspacePromoteDraftsExecutor",
]
