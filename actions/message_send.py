"""
MessageSendExecutor —— 把「发消息」接进 Action Seam
=====================================================

    ActionIntent(message_send) → MessageSendExecutor
        → 注入的 send_fn（由组合根提供）
        → ActionResult

【🔒 它为什么存在】

    `CommunicationManager.send()` **本来就接受** `message_type` + `payload`，
    协议层也**早就注册好**了 8 种业务 handler：

        TASK_OFFER · COUNTER_OFFER · ACCEPT · REJECT
        START · SUBMIT · VERIFY · COMPLETE

    但 Agent 发不出它们 —— 实验的 `channels.send()` 写死
    `MessageType.TEXT`。于是 `TASK_OFFER → handle_offer`
    这条**会创建 Task**（`creator = 发起方 Agent`）的链路
    在组合根里接好了，**从来没被触发过**。

    本模块补的就是这个出口。

【🔴 三条不能违反的边界】（与 WorkspaceWriteExecutor / WorkspaceReadExecutor 对称）

① **只执行，不决策**

     ❌ 不枚举「允许发哪些 message_type」—— 类型是**自由字符串**，
        合法性由下游 `TaskMessageMapper` / `handle_*` 判断
     ❌ 不校验 payload 语义（字段缺没缺、金额合不合理）
     ❌ 不替 Agent 挑收件人
     ❌ 失败**不重发**、不换一个收件人
     ❌ 不 import `selfmodel` / 不读 Strategy（有守卫扫 `actions/*.py`）

     🔒 在本层枚举类型 = **替 Agent 划定它能说什么**，那是 §1 禁止的；
        而且会形成第二套协议规则（本项目反复吃过这个亏）。

② **不 import `services/`**

     `actions/` 是**叶子层**。它只拿到一个**注入的** `send_fn`
     （鸭子类型），由组合根决定接哪个实现。
     所以这一层结构上不认识 CommunicationManager，
     更不认识 Task / Workspace。

③ **成功与失败都必须返回结构化 ActionResult**

     ❌ 不允许「失败就抛异常」或「失败返回 None」
     ✅ `success=False` + `error=<人类可读>` + `metadata["code"]=<机器可读>`

【🔒 from_agent 从哪来】

     只取自 `intent.agent_id`（**运行时**填的），
     **绝不**从 `parameters` 里读 —— 否则 Agent 可以冒充别人发消息。
     这与 `Identity ≠ Address`、`ActionIntent ≠ Authorization` 同源。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from actions.intent import ActionResult, ActionIntent

#: 这个执行器认领的 action_type（§十：类型是**显式**的，不猜）。
#:
#: 🔒 与 `workspace_write` / `workspace_read` 一样，**不**加进
#:    `BUILTIN_ACTION_TYPES` —— 发消息是**由组合根按需注册**的能力，
#:    不是「任何调用方都自动具备」的东西。
ACTION_MESSAGE_SEND = "message_send"

#: 省略 `message_type` 时用的类型。
#:
#: 🔒 这不是「业务默认值」，而是**普通文本**这个既有类型 ——
#:    与实验原先 `message` 字段的行为一致。
#:    ⚠️ 它**不是**在暗示 Agent 该发什么业务消息；
#:       要发 TASK_OFFER 必须自己显式给出。
DEFAULT_MESSAGE_TYPE = "TEXT"

#: `send_fn` 的成功返回里，哪些字段值得带进 `ActionResult.output`。
#:
#: 🔒 这些**全部**是 `services.messaging.send_message_as()` 真实返回的键。
_OUTPUT_FIELDS = ("message_id", "to", "message_type", "chars",
                  "wakeup_signalled")


class MessageSendExecutor:
    """
    执行 `action_type = "message_send"` 的 ActionIntent。

    🔒 依赖以**构造注入**给出（与另外两个 Workspace 执行器同一种手法）：

           comm       —— 传给 send_fn 的第一个参数（本层不碰它的任何方法）
           send_fn    —— 真正干活的函数，签名与
                         `services.messaging.send_message_as` 一致
                         （**同步**；异步通信层由组合根用桥适配）

    Agent 只需在 `parameters` 里给出 `to` / `text`，
    要发业务消息时再加 `message_type` / `payload`。
    """

    action_types = frozenset({ACTION_MESSAGE_SEND})

    def __init__(
        self,
        send_fn: Callable[..., Any],
        *,
        comm: Any = None,
    ) -> None:
        if send_fn is None or not callable(send_fn):
            raise ValueError(
                "MessageSendExecutor 需要一个可调用的 send_fn —— "
                "它由组合根注入（例如 services.messaging.send_message_as）")
        self.send_fn = send_fn
        self.comm = comm

    # ------------------------------------------------------------
    def execute(self, intent: ActionIntent) -> ActionResult:
        """
        执行一次发送。**原样使用 Agent 给出的 type / text / payload。**

        🔒 失败也是 ActionResult（§十七），不是异常。
           唯一的 `except` 是防「注入的 send_fn 自己崩了」——
           那也必须变成一个**结构化**的失败结果。
        """
        params = dict(getattr(intent, "parameters", None) or {})
        to_agent = str(params.get("to") or "").strip()
        message_type = str(params.get("message_type")
                           or DEFAULT_MESSAGE_TYPE).strip()
        text = params.get("text")
        payload = params.get("payload")
        if not isinstance(payload, dict):
            payload = None

        try:
            raw = self.send_fn(
                self.comm,
                from_agent=intent.agent_id,
                to_agent=to_agent,
                message_type=message_type,
                text=None if text is None else str(text),
                payload=payload,
            )
        except Exception as exc:                              # noqa: BLE001
            return self._fail(
                intent, f"{type(exc).__name__}: {exc}",
                code="executor_error", to_agent=to_agent,
                message_type=message_type,
            )

        if not isinstance(raw, dict):
            return self._fail(
                intent, f"send_fn 返回了 {type(raw).__name__}，期望 dict",
                code="invalid_send_result", to_agent=to_agent,
                message_type=message_type,
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
            str(raw.get("message") or "发送失败"),
            code=str(raw.get("code") or "unknown"),
            to_agent=str(raw.get("to") or to_agent),
            message_type=str(raw.get("message_type") or message_type),
        )

    # ------------------------------------------------------------
    def _fail(self, intent: ActionIntent, message: str, *,
              code: str, to_agent: str, message_type: str) -> ActionResult:
        """
        统一的失败结果。

        🔒 `output` 刻意留空 —— 失败时**没有**「发出去的消息」，
           编一个空 dict 会让人以为发送发生过。
        """
        return ActionResult(
            success=False,
            output=None,
            error=message,
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": code, "to": to_agent,
                      "message_type": message_type},
        )
