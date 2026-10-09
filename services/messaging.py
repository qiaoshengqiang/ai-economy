"""
Messaging —— 按**显式** `message_type` 发一条消息
======================================================

【它解决的真实问题】

    `CommunicationManager.send()` **本来就接受** `message_type` + `payload`，
    协议层也**早就注册好**了 8 种业务消息 handler：

        TASK_OFFER · COUNTER_OFFER · ACCEPT · REJECT
        START · SUBMIT · VERIFY · COMPLETE

    但**没有任何一条路让 Agent 发出它们** ——
    小说实验的 `channels.send()` 写死：

        message_type=MessageType.TEXT

    于是 Agent 只能发普通文本。后果（实测）：
    `TASK_OFFER → handle_offer` 这条**会创建 Task** 的链路
    （`creator = 发起方 Agent`，见 `task_negotiation_service`）
    **在组合根里接好了，却从来没被触发过。**

    ⇒ 「Agent 自己组织生产」缺的不是能力，是**出口**。

【🔴 本模块刻意**不**校验 payload 语义】

    ❌ 不检查 `TASK_OFFER` 该带哪些字段
    ❌ 不检查 `budget` 是不是 Money
    ❌ 不检查 `task_id` 格式
    ❌ 不枚举「允许发哪些 message_type」

    这些都**已经在下游**：`TaskMessageMapper.parse_offer()` 解析、
    `handle_offer()` 做**角色校验 + 状态机校验**，失败会返回结构化错误。

    🔒 在这里再校验一遍 = **两套规则**（本项目反复吃过这个亏）。
       而且会变成「替 Agent 划定它能说什么」—— 那是 §1 禁止的。

    🔒 唯一**必须**在这里拦住的是「连协议都拼不出来」的情况 ——
       例如 message_type 为空。那不是业务判断，是**调用方参数错误**。

【🔒 返回结构与其他服务函数统一】

    成功 → {"ok": True, "message_id": ..., "to": ..., "message_type": ...,
            "chars": ..., "wakeup_signalled": ...}
    失败 → {"ok": False, "to": ..., "code": ..., "message": ...}

    （与 `services/artifacts.write_artifact` / `read_artifact` 同一形状，
      好让对应的执行器能共用同一套翻译逻辑。）
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


def _fail(to_agent: str, code: str, message: str,
          **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "to": to_agent,
                           "code": code, "message": message}
    out.update(extra)
    return out


async def send_message_as(
    comm: Any,
    *,
    from_agent: str,
    to_agent: str,
    message_type: str,
    text: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    trace_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    按**显式** `message_type` 发一条消息。

    `message_type` **必须由 Agent 给出** —— 本函数不猜、不给默认业务类型。
    （省略它由调用方决定，见执行器；这里要求非空。）

    🔒 `from_agent` 由**运行时**注入，不来自 Agent 的 parameters ——
       否则 Agent 可以冒充别人发消息（`Identity ≠ Address` 的同源要求）。
       `comm.send` 自己还会再查一次「发送方是否已登记」。

    🔒 失败一律返回结构化 dict，**不抛异常**（§十七）。
       唯一的 `except` 是防通信层自己崩了。
    """
    sender = str(from_agent or "").strip()
    recipient = str(to_agent or "").strip()
    mtype = str(message_type or "").strip()

    if not recipient:
        return _fail(recipient, "missing_recipient",
                     "必须给出收件人 agent_id")
    if not mtype:
        # 这不是业务校验 —— 是没有类型就无法构造消息。
        return _fail(recipient, "missing_message_type",
                     "必须给出 message_type（本函数不猜业务类型）")

    try:
        msg = await comm.send(
            from_agent=sender,
            to=recipient,
            text=text,
            payload=dict(payload or {}),
            message_type=mtype,
            reply_to=reply_to,
            trace_id=trace_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:                                  # noqa: BLE001
        return _fail(recipient, "send_failed",
                     f"{type(exc).__name__}: {exc}",
                     message_type=mtype)

    message_id = str(getattr(msg, "message_id", "") or "")

    # 🔒 wakeup 是**投递的副作用**（LocalTransport：先入信箱，再 signal）。
    #    读不到就报 None —— **不编一个 False 出来**（那会被误读成
    #    「没唤醒」，而事实是「我们没查」）。见 `Wakeup ≠ Message`。
    try:
        signalled: Optional[bool] = bool(comm.is_woken(recipient))
    except Exception:                                         # noqa: BLE001
        signalled = None

    return {
        "ok": True,
        "message_id": message_id,
        "to": recipient,
        "message_type": mtype,
        "chars": len(text or ""),
        "wakeup_signalled": signalled,
    }


def build_offer_payload(
    *,
    task_id: str,
    description: Optional[str] = None,
    budget: Any = None,
    workspace_id: Optional[str] = None,
    required_capabilities: Any = None,
    acceptance: Any = None,
    expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    构造一条 `TASK_OFFER` 的 **payload**。

    【为什么需要它】

        mapper 里已经有**应答方**的构造器
        （`accept_reply` / `reject_reply` / `counter_offer_reply`），
        但**没有发起方**的 —— 因为以前 Task 都是 runner 直接建的，
        Agent 从来没机会发起邀约。

    🔴 **本函数不做校验，也不做类型强制**（除了 task_id 转字符串）。

        调用方给什么就放什么 —— 包括**明显错误**的值（例如
        `budget=30` 这种裸数字）。

        为什么？因为**校验点只能有一个**，它在下游：
        `TaskMessageMapper.parse_money()` 会拒绝裸数字，并回一条
        **写得很清楚**的错误消息：

            「budget 必须是 {amount, currency} 形式的对象，
              不能是裸数字 30。裸数字没有货币语义 —— 见 PROTOCOL.md 7.1」

        🔒 如果这里也校验/强制一次，就变成**两套规则** ——
           本项目反复吃过这个亏（接口成功掩盖语义空转）。

        ⚠️ 本函数第一版对 `budget` 做了 `dict(budget)` ——
           传裸数字时直接 `TypeError` 崩掉，**把协议层那句好错误
           消息挡掉了**。那正是「两套规则」的坏处，已改掉。

    🔒 它**不发送**。发送是 `send_message_as` 的事 ——
      「构造」与「投递」分开，便于分别守卫与测试。
    """
    out: Dict[str, Any] = {"task_id": str(task_id or "").strip()}
    if description is not None:
        out["description"] = description
    if budget is not None:
        out["budget"] = budget
    if workspace_id is not None:
        out["workspace_id"] = workspace_id
    if required_capabilities is not None:
        out["required_capabilities"] = required_capabilities
    if acceptance is not None:
        out["acceptance"] = acceptance
    if expires_at is not None:
        out["expires_at"] = expires_at
    return out
