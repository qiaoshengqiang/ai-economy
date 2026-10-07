"""
Communication 真实接线 —— **复用已有协议，不重设计**
========================================================

【这个模块只解决一个「接不上」的问题】

    现有 `CommunicationManager` 是 **async** 的（`send` / `receive` 是协程），
    而这条小说生产实验的驱动器是**同步**的（Brain 与 Economy 都是同步的）。

    于是本模块提供一个**极小的**同步桥：
        `SyncBridge.run(coro)` → `loop.run_until_complete(coro)`

    🔒 它**不**改通信层的任何语义，也**不**新增 MessageType。
       协作消息一律用已有的 `MessageType.TEXT`。

【🔒 三条不能破坏的边界】

    1. **Wakeup ≠ Message**（`communication/wakeup.py`）
       `is_woken()` 只是「有个信号」，它**不携带内容**。
       消息只能从信箱里取 —— 所以下面 `drain()` 一定调 `receive()`。

    2. **Message ≠ 第二套 Workspace**
       完整正文不进 Message。Message 只承载
       「请求 / 意见 / 说明我改了什么」。

    3. **不替 Agent 决定回报什么**
       本模块只**投递**和**收取**，不生成任何消息内容。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

#: 取件超时（秒）。信箱里已有消息时它立即返回 —— 这个值只是「空信箱别卡住」。
RECEIVE_TIMEOUT = 0.05

#: 一次 drain 最多取多少条（防止无限循环把一轮卡死）
MAX_DRAIN = 50


class SyncBridge:
    """把 async 通信层接到同步驱动器上的最小桥。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()

    def run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:                                     # noqa: BLE001
            pass


# ================================================================
# 收取
# ================================================================
def drain(comm: Any, bridge: SyncBridge, agent_id: str,
          *, max_messages: int = MAX_DRAIN) -> Dict[str, Any]:
    """
    把一个 Agent 信箱里的消息**取空**，并如实记录四种事件（§十二）。

    返回：
        {
          "wakeup":   bool,     # 取之前 wakeup 是否为 SET（Event Wakeup 证据）
          "received": [...],    # 真的从信箱取出来的消息
          "consumed": [...],    # 取出后 was_processed()==True 的消息 ID
          "duplicates": int,
          "empty":    bool,
        }

    🔒 **先记 wakeup，再 clear，再 drain** —— 顺序不能反：
       如果先 clear，我们就再也证明不了「它确实被叫醒过」。
    """
    was_woken = False
    try:
        was_woken = bool(comm.is_woken(agent_id))
    except Exception:                                         # noqa: BLE001
        pass

    # Wakeup 与 Message 分开：clear 只影响信号，不影响信箱
    try:
        comm.clear_wakeup(agent_id)
    except Exception:                                         # noqa: BLE001
        pass

    received: List[Any] = []
    consumed: List[str] = []
    duplicates = 0

    while len(received) < max_messages:
        try:
            pending = comm.pending(agent_id)
        except Exception:                                     # noqa: BLE001
            break
        if pending <= 0:
            break
        try:
            inbound = bridge.run(comm.receive(agent_id, timeout=RECEIVE_TIMEOUT))
        except Exception:                                     # noqa: BLE001
            break
        if inbound is None or inbound.message is None:
            break
        if inbound.is_duplicate:
            duplicates += 1
            continue
        if not inbound.delivered:
            break
        received.append(inbound.message)
        try:
            if comm.was_processed(inbound.message.message_id):
                consumed.append(inbound.message.message_id)
        except Exception:                                     # noqa: BLE001
            pass

    return {
        "wakeup": was_woken,
        "received": received,
        "consumed": consumed,
        "duplicates": duplicates,
        "empty": not received,
    }


def render_inbox(messages: List[Any]) -> str:
    """
    把收到的消息渲染成 Cognitive Context 里的一段**信息**。

    🔒 只陈述「谁对你说了什么」——
       **不**写「你应该回复」「你应该照做」（那是替 Agent 决策）。
    """
    if not messages:
        return "（这一轮你收到 0 条消息。）"
    lines = [f"这一轮你收到 {len(messages)} 条来自其他参与者的消息：", ""]
    for m in messages:
        text = ""
        try:
            payload = m.payload or {}
            text = str(payload.get("text") or payload.get("summary") or "")
            if not text:
                text = str(payload)
        except Exception:                                     # noqa: BLE001
            text = ""
        lines.append(f"- 来自 {m.from_agent}  (message_id={m.message_id})")
        lines.append(f"  {text.strip()[:600]}")
    return "\n".join(lines)


# ================================================================
# 发送
# ================================================================
def send(comm: Any, bridge: SyncBridge, *, from_agent: str, to_agent: str,
         text: str, trace_id: Optional[str] = None,
         conversation_id: Optional[str] = None,
         reply_to: Optional[str] = None) -> Dict[str, Any]:
    """
    投递一条协作消息。**Runtime 执行，Agent 判断** ——
    这里拿到的 `text` 是 Agent 自己写的，本函数一个字都不改。

    返回投递记录（含 message_id / wakeup 是否被置位）。
    """
    from communication import MessageType

    try:
        msg = bridge.run(comm.send(
            from_agent=from_agent, to=to_agent, text=text,
            message_type=MessageType.TEXT,
            trace_id=trace_id, conversation_id=conversation_id,
            reply_to=reply_to,
        ))
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "from": from_agent, "to": to_agent,
                "error": f"{type(exc).__name__}: {exc}"}

    # wakeup 是**投递的副作用**（LocalTransport: 先入信箱，再 signal）
    try:
        woken = bool(comm.is_woken(to_agent))
    except Exception:                                         # noqa: BLE001
        woken = False

    return {
        "ok": True,
        "message_id": msg.message_id,
        "from": from_agent,
        "to": to_agent,
        "trace_id": msg.trace_id,
        "conversation_id": msg.conversation_id,
        "chars": len(text or ""),
        "wakeup_signalled": woken,
    }


# ================================================================
# 统计
# ================================================================
def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把整场 Session 的通信事件汇总成可报告的统计。"""
    sent = [e for e in events if e.get("kind") == "sent"]
    recv = [e for e in events if e.get("kind") == "received"]
    wake = [e for e in events if e.get("kind") == "wakeup"]
    cons = [e for e in events if e.get("kind") == "consumed"]

    pairs: Dict[str, int] = {}
    for e in sent:
        key = f"{e.get('from')}→{e.get('to')}"
        pairs[key] = pairs.get(key, 0) + 1

    senders = sorted({e.get("from") for e in sent if e.get("from")})
    receivers = sorted({e.get("to") for e in sent if e.get("to")})

    return {
        "messages_sent": len(sent),
        "messages_received": len(recv),
        "wakeups": len(wake),
        "messages_consumed": len(cons),
        "distinct_pairs": len(pairs),
        "pairs": pairs,
        "senders": senders,
        "receivers": receivers,
        # 🔒 N 个 Agent 各自独立产出 **不是**协作 —— 这里如实报告
        "any_agent_to_agent": len(sent) > 0,
    }
