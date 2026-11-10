"""`tool_help` —— 让 Agent 自己查公共工具的用法。

🔒 本模块是叶子层：不 import `tool_runtime/`、不读文件。
   真正的文档读取由组合根注入的 `help_fn(agent_id, tool)` 完成。

为什么需要它：
    工具的用法、限制、真实踩坑不应该只活在 prompt 里。每次出问题
    就往 prompt 补一段，会让 prompt 变成不可维护的“事故备忘录”。
    正确做法是：prompt 只告诉 Agent「去哪里查」，文档保持单一来源。
"""

from __future__ import annotations

from typing import Any, Callable

from actions.intent import ActionResult, ActionIntent

ACTION_TOOL_HELP = "tool_help"


class ToolHelpExecutor:
    action_types = frozenset({ACTION_TOOL_HELP})

    def __init__(self, help_fn: Callable[[str, str], Any]) -> None:
        if help_fn is None or not callable(help_fn):
            raise ValueError("ToolHelpExecutor 需要一个可调用的 help_fn")
        self.help_fn = help_fn

    def execute(self, intent: ActionIntent) -> ActionResult:
        params = dict(getattr(intent, "parameters", None) or {})
        tool = str(params.get("tool") or "").strip()
        try:
            raw = self.help_fn(intent.agent_id, tool)
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error")
        if not isinstance(raw, dict):
            return self._fail(
                intent,
                f"help_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_help_result")
        if not raw.get("ok"):
            return self._fail(
                intent,
                str(raw.get("message") or "没有找到工具用法"),
                code=str(raw.get("code") or "unknown"),
            )
        return ActionResult(
            success=True,
            output={
                "tool": str(raw.get("tool") or ""),
                "content": str(raw.get("content") or ""),
                "tools": list(raw.get("tools") or []),
            },
            error=None,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": None},
        )

    @staticmethod
    def _fail(intent: ActionIntent, message: str, *,
              code: str) -> ActionResult:
        return ActionResult(
            success=False,
            output=None,
            error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": code},
        )
