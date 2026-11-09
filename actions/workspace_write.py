"""
WorkspaceWriteExecutor —— 把「Workspace 写操作」接进 Action Seam
================================================================

    ActionIntent(workspace_write) → WorkspaceWriteExecutor
        → 注入的 write_fn（由组合根提供）
        → ActionResult

【🔒 它为什么存在（真实缺口，见 ACTION_FEEDBACK_AUDIT.md）】

    生产实验原先直接调用 `services/artifacts.write_artifact()`，
    返回值是一个**裸 dict**，只写进内存日志：

        production.py → write_artifact() → dict → turn["write"] → 进程结束即消失

    于是 Gateway 明明知道「这个文件已存在」，Agent 下一轮完全不知道，
    连续 5 轮重复 `CREATE` 同一个路径。

    本模块补的就是这一小段：**把写操作变成一个标准 ActionResult**，
    好让它能走既有的 `ExperienceStore.record_action_result()`
    → `CognitiveContextBuilder` → 下一轮 Cognition。

【🔴 三条不能违反的边界】

① **只执行，不决策**

    ❌ 不选 write_mode（那是 Agent 的声明，原样透传）
    ❌ 不自动重试
    ❌ 不因为失败就换一个 mode / 换一条路径
    ❌ 不 import `selfmodel` / 不读 Strategy（有守卫扫 `actions/*.py`）

② **不 import `services/`**

    `actions/` 是**叶子层**。它只拿到一个**注入的** `write_fn`
    （鸭子类型），由 `main.py` / 实验 runner（组合根）决定接哪个实现。
    所以这一层结构上不认识 WorkspaceGateway、更不认识数据库。

③ **成功与失败都必须返回结构化 ActionResult**

    ❌ 不允许「失败就抛异常」或「失败返回 None」
    ✅ `success=False` + `error=<人类可读>` + `metadata["code"]=<机器可读>`
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from actions.intent import ActionResult, ActionIntent

#: 这个执行器认领的 action_type（§十：类型是**显式**的，不猜）。
ACTION_WORKSPACE_WRITE = "workspace_write"

#: `write_fn` 的成功返回里，哪些字段值得带进 `ActionResult.output`。
#:
#: 🔒 这些**全部**是 `services/artifacts.write_artifact()` 真实返回的键，
#:    这里不新增、不重命名、不推断。
_OUTPUT_FIELDS = ("path", "mode", "revision", "previous_revision",
                  "previous_chars", "appended_chars", "chars")


class WorkspaceWriteExecutor:
    """
    执行 `action_type = "workspace_write"` 的 ActionIntent。

    🔒 依赖以**构造注入**给出（与 `stage/control.py:StageControlExecutor(store)`
       同一种手法）：

        gateway        —— 传给 write_fn 的第一个参数（本层不碰它的任何方法）
        workspace_id   —— 这次写操作发生在哪个 Workspace
        write_fn       —— 真正干活的函数，签名与
                          `services.artifacts.write_artifact` 一致

    Agent 只需要在 `parameters` 里给出 `path` / `content` / `write_mode`，
    **不需要**知道 gateway 是什么。
    """

    action_types = frozenset({ACTION_WORKSPACE_WRITE})

    def __init__(
        self,
        write_fn: Callable[..., Any],
        *,
        gateway: Any = None,
        workspace_id: str = "",
        max_content_chars: Optional[int] = None,
    ) -> None:
        if write_fn is None or not callable(write_fn):
            raise ValueError(
                "WorkspaceWriteExecutor 需要一个可调用的 write_fn —— "
                "它由组合根注入（例如 services.artifacts.write_artifact）")
        if max_content_chars is not None and int(max_content_chars) <= 0:
            raise ValueError(
                "max_content_chars 必须是正整数或 None —— "
                "0/负数会静默禁掉所有写入")
        self.write_fn = write_fn
        self.gateway = gateway
        self.workspace_id = workspace_id
        #: 🔒 单次 `content` 的**结构上限**（字符数），由组合根决定。
        #: None = 不限制（实验/旧调用方保持兼容）。
        self.max_content_chars = (int(max_content_chars)
                                  if max_content_chars is not None else None)

    # ------------------------------------------------------------
    def execute(self, intent: ActionIntent) -> ActionResult:
        """
        执行一次写入。**原样使用 intent 里的 write_mode，绝不改写。**

        🔒 失败也是 ActionResult（§十七），不是异常。
           唯一的 `except` 是防「注入的 write_fn 自己崩了」——
           那也必须变成一个**结构化**的失败结果。
        """
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "")
        write_mode = str(params.get("write_mode") or "")
        content = params.get("content")

        # 🔒 结构上限在调用 write_fn **之前**检查：超限不写入。
        #    这不是建议 —— Agent 必须把大文件拆成 create + append/edit。
        if self.max_content_chars is not None:
            body = content if isinstance(content, str) else str(content or "")
            if len(body) > self.max_content_chars:
                return self._fail(
                    intent,
                    (f"单次写入 content 有 {len(body)} 字符，超过上限 "
                     f"{self.max_content_chars}；本次**没有写入**。"
                     "请拆分：先用 create 写小骨架，再用 append / "
                     "workspace_edit 逐段补全；不要一次重写整个大文件。"),
                    code="content_too_large", path=path,
                    write_mode=write_mode,
                    chars=len(body), max_chars=self.max_content_chars,
                )

        try:
            raw = self.write_fn(
                self.gateway, self.workspace_id, intent.agent_id,
                path, content, write_mode=write_mode,
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error", path=path, write_mode=write_mode,
            )

        if not isinstance(raw, dict):
            return self._fail(
                intent, f"write_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_write_result", path=path,
                write_mode=write_mode,
            )

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

        return self._fail(
            intent,
            str(raw.get("message") or "写入失败"),
            code=str(raw.get("code") or "unknown"),
            path=path, write_mode=write_mode,
            gateway_code=raw.get("gateway_code"),
        )

    # ------------------------------------------------------------
    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, path: str, write_mode: str,
              gateway_code: Optional[str] = None,
              **extra: Any) -> ActionResult:
        """
        统一的失败结果。

        🔒 `output` 刻意留空 —— 失败时**没有**「写出来的东西」，
           编一个空 dict 会让人以为写入发生过。
           真正的事实放在 `metadata`（机器读）与 `error`（人读）。
        """
        metadata: dict = {"code": code, "path": path,
                          "write_mode": write_mode}
        if gateway_code:
            metadata["gateway_code"] = gateway_code
        metadata.update(extra)
        return ActionResult(
            success=False,
            output=None,
            error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata=metadata,
        )
