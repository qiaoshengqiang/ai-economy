"""
WorkspaceReadExecutor —— 把「Workspace 读操作」接进 Action Seam
================================================================

    ActionIntent(workspace_read) → WorkspaceReadExecutor
        → 注入的 read_fn（由组合根提供）
        → ActionResult

【🔒 它为什么存在】

    Workspace Gateway **早就有** `read()`（只检查「是不是 Task 参与者」，
    不查 Write Lock —— 锁挡的是修改与删除，不是阅读）。

    但「Agent 能发起的动作」里一直没有读：
        actions/intent.py  BUILTIN_ACTION_TYPES = {reply, noop, observe}
        actions/executor.py  ACTION_OBSERVE 由 NoopExecutor 认领
                             ——它的 docstring 自己写着「什么都不做」

    于是验收责任人**结构上无法读到被验收的内容**，只能看到
    「文件存在、谁锁着」。真实后果：`novel_reader` 在
    `/reader/chapter1_reader_acceptance.md` 里写道

        「我目前只能看到 /manuscript/chapter_001.md 的路径和锁定状态，
          无法读取正文，所以不能负责任地给出通过。」

    它拒绝给自己读不到的东西盖章 —— 那是**对的行为**（`Observation ≠
    Conclusion`）。本模块补的是它缺的那条通道。

    ⚠️ 并且注意：它当时推测「是文件被锁住了」——**那个推断是错的**。
       锁从来不影响读。它之所以那么猜，是因为环境事实里投递了
       `effective_lock_holder` 而没有给它任何读的手段。
       **事实投递的问题不只是「给不给」，还有「给了会不会误导」。**

【🔴 三条不能违反的边界】（与 WorkspaceWriteExecutor 完全对称）

① **只执行，不决策**

     ❌ 不替 Agent 挑要读哪个文件（path 由 Agent 显式给出）
     ❌ 不自动附加「相关文件」
     ❌ 读不到就换路径重试
     ❌ 不 import `selfmodel` / 不读 Strategy（有守卫扫 `actions/*.py`）

② **不 import `services/`**

     `actions/` 是**叶子层**。它只拿到一个**注入的** `read_fn`
     （鸭子类型），由组合根决定接哪个实现。
     所以这一层结构上不认识 WorkspaceGateway、更不认识数据库。

③ **成功与失败都必须返回结构化 ActionResult**

     ❌ 不允许「失败就抛异常」或「失败返回 None」
     ✅ `success=False` + `error=<人类可读>` + `metadata["code"]=<机器可读>`

【🔴 一个必须说清的语义（不是缺陷，是既有机制的性质）】

     读到的内容进入 `ActionResult.output`，而它经由
     `ExperienceStore.record_action_result()` → `CognitiveContextBuilder`
     **在下一轮 Cognition 才被 Agent 看到**。

     也就是说：**Agent 这一轮发起读，下一轮才用到内容。**
     这与写操作的反馈回路是同一条（写的结果也是下一轮才知道）。

     🔒 本模块**不**发明一条「同步返回给本轮」的新路径 ——
        那会绕过既有的 Experience 机制，是架构改动。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from actions.intent import ActionResult, ActionIntent

#: 这个执行器认领的 action_type（§十：类型是**显式**的，不猜）。
#:
#: 🔒 刻意**不**加进 `actions/intent.py` 的 `BUILTIN_ACTION_TYPES`：
#:    那是「框架内置、任何调用方都自动具备」的集合。
#:    `workspace_write` 也不在里面 —— 写与读都是**由组合根按需注册**的能力。
#:    把类型登记成全局内置，等于替**所有**调用方决定它们能做什么。
ACTION_WORKSPACE_READ = "workspace_read"

#: `read_fn` 的成功返回里，哪些字段值得带进 `ActionResult.output`。
#:
#: 🔒 这些**全部**是 `services.artifacts.read_artifact()` 真实返回的键，
#:    这里不新增、不重命名、不推断。
#:
#: `content` 必须在列 —— 没有它，「读」这件事就没有发生。
_OUTPUT_FIELDS = ("path", "revision", "chars", "content")


class WorkspaceReadExecutor:
    """
    执行 `action_type = "workspace_read"` 的 ActionIntent。

    🔒 依赖以**构造注入**给出（与 `WorkspaceWriteExecutor` /
       `stage/control.py:StageControlExecutor(store)` 同一种手法）：

           gateway        —— 传给 read_fn 的第一个参数（本层不碰它的任何方法）
           workspace_id   —— 这次读操作发生在哪个 Workspace
           read_fn        —— 真正干活的函数，签名与
                             `services.artifacts.read_artifact` 一致

     Agent 只需要在 `parameters` 里给出 `path`，
     **不需要**知道 gateway 是什么、也不需要知道锁的状态。
    """

    action_types = frozenset({ACTION_WORKSPACE_READ})

    def __init__(
        self,
        read_fn: Callable[..., Any],
        *,
        gateway: Any = None,
        workspace_id: str = "",
    ) -> None:
        if read_fn is None or not callable(read_fn):
            raise ValueError(
                "WorkspaceReadExecutor 需要一个可调用的 read_fn —— "
                "它由组合根注入（例如 services.artifacts.read_artifact）")
        self.read_fn = read_fn
        self.gateway = gateway
        self.workspace_id = workspace_id

    # ------------------------------------------------------------
    def execute(self, intent: ActionIntent) -> ActionResult:
        """
        执行一次读取。**路径原样使用，绝不改写、不重试。**

        🔒 失败也是 ActionResult（§十七），不是异常。
           唯一的 `except` 是防「注入的 read_fn 自己崩了」——
           那也必须变成一个**结构化**的失败结果。
        """
        params = dict(getattr(intent, "parameters", None) or {})
        path = str(params.get("path") or "")

        try:
            raw = self.read_fn(
                self.gateway, self.workspace_id, intent.agent_id, path,
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error", path=path,
            )

        if not isinstance(raw, dict):
            return self._fail(
                intent, f"read_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_read_result", path=path,
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
            str(raw.get("message") or "读取失败"),
            code=str(raw.get("code") or "unknown"),
            path=path,
            gateway_code=raw.get("gateway_code"),
        )

    # ------------------------------------------------------------
    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, path: str,
              gateway_code: Optional[str] = None) -> ActionResult:
        """
        统一的失败结果。

        🔒 `output` 刻意留空 —— 失败时**没有**「读出来的东西」，
           编一个空 dict 会让人以为读过。
           真正的事实放在 `metadata`（机器读）与 `error`（人读）。
        """
        metadata: dict = {"code": code, "path": path}
        if gateway_code:
            metadata["gateway_code"] = gateway_code
        return ActionResult(
            success=False,
            output=None,
            error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata=metadata,
        )
