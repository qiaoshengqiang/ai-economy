"""
Message **发送**动作 —— 测试
================================

【它解决的真实问题】

    `CommunicationManager.send()` 本来就接受 `message_type` + `payload`，
    协议层也早注册好 8 种业务 handler，但实验的 `channels.send()`
    **写死 `MessageType.TEXT`** —— Agent 只能发普通文本。

    后果：`TASK_OFFER → handle_offer` 这条**会创建 Task**
    （`creator = 发起方 Agent`）的链路在组合根里接好了，
    **从来没被触发过**。

    ⇒ 「Agent 自己组织生产」缺的不是能力，是**出口**。

【本文件要钉住的四件事】

    ① **发得出去** —— 且 `TASK_OFFER` 真的会让系统建 Task，
       `creator` 是**发起方 Agent**（ADR-0024）
    ② **`from_agent` 不可伪造** —— 只来自运行时，不来自 parameters
    ③ **不枚举 message_type** —— 解析器/执行器都不替 Agent 划定能说什么
    ④ **坏 payload 给结构化反馈** —— Agent 能学到，而不是静默失败

🔒 全部测试**不调用任何 LLM**。
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                  # noqa: E402

load_dotenv(ROOT / ".env")

PROD = ROOT / "experiments" / "heterogeneous_production" / "production.py"
ACTIONS = ROOT / "actions"


# ======================================================================
# ① 执行器本身（注入假 send_fn，不碰通信层）
# ======================================================================
def _executor(fn, **kw):
    from actions.message_send import MessageSendExecutor

    return MessageSendExecutor(fn, **kw)


def _intent(params, agent="agent_a"):
    from actions.intent import ActionIntent

    return ActionIntent(action_type="message_send",
                        parameters=params, agent_id=agent)


def test_action_type_is_explicit_and_not_globally_builtin():
    from actions.intent import BUILTIN_ACTION_TYPES
    from actions.message_send import ACTION_MESSAGE_SEND, MessageSendExecutor

    assert ACTION_MESSAGE_SEND == "message_send"
    assert MessageSendExecutor.action_types == frozenset({"message_send"})
    assert ACTION_MESSAGE_SEND not in BUILTIN_ACTION_TYPES, (
        "发消息不该是「任何调用方都自动具备」的能力 —— 由组合根按需注册")


def test_requires_callable_send_fn():
    from actions.message_send import MessageSendExecutor

    with pytest.raises(ValueError):
        MessageSendExecutor(None)


def test_success_carries_message_id_and_type():
    seen = {}

    def fake_send(comm, *, from_agent, to_agent, message_type, text, payload):
        seen.update(comm=comm, from_agent=from_agent, to_agent=to_agent,
                    message_type=message_type, text=text, payload=payload)
        return {"ok": True, "message_id": "msg_1", "to": to_agent,
                "message_type": message_type, "chars": len(text or ""),
                "wakeup_signalled": True}

    ex = _executor(fake_send, comm="COMM")
    res = ex.execute(_intent({"to": "agent_b", "text": "接不接？",
                              "message_type": "TASK_OFFER",
                              "payload": {"task_id": "T-1"}}))

    assert res.success is True
    assert res.metadata.get("code") is None, "成功不该编一个错误码"
    assert res.output["message_id"] == "msg_1"
    assert res.output["message_type"] == "TASK_OFFER"
    assert res.output["wakeup_signalled"] is True
    assert seen["comm"] == "COMM"
    assert seen["payload"] == {"task_id": "T-1"}


def test_delivery_is_not_handler_acceptance():
    """
    投递成功 ≠ 生命周期动作被接受。

    🔒 发送层不知道处理器结果时，必须如实说 DEFERRED，
       不能把“已投递”伪装成“已接受”。
    """
    def fake_send(comm, *, from_agent, to_agent, message_type, text, payload):
        return {"ok": True, "message_id": "msg_2", "to": to_agent,
                "message_type": message_type, "chars": 1,
                "wakeup_signalled": True,
                "delivery": "DELIVERED",
                "handler_result": "DEFERRED",
                "handler_note": "处理在收件人取信时运行"}

    res = _executor(fake_send).execute(_intent({
        "to": "agent_b", "text": "x", "message_type": "START"}))
    assert res.success is True
    assert res.output["delivery"] == "DELIVERED"
    assert res.output["handler_result"] == "DEFERRED"
    assert "处理" in res.output["handler_note"]


def test_send_message_as_reports_deferred_handler_result():
    import asyncio
    from types import SimpleNamespace
    from services.messaging import send_message_as

    class _Comm:
        async def send(self, **kwargs):
            return SimpleNamespace(message_id="m1")

        def is_woken(self, agent_id):
            return False

    out = asyncio.new_event_loop().run_until_complete(
        send_message_as(_Comm(), from_agent="agent_a", to_agent="agent_b",
                        message_type="START", text="x"))
    assert out["ok"] is True
    assert out["delivery"] == "DELIVERED"
    assert out["handler_result"] == "DEFERRED"
    assert "Task 状态" in out["handler_note"]


def test_from_agent_comes_from_the_runtime_not_from_parameters():
    """
    🔴 **冒充守卫**：发送方只能取 `intent.agent_id`（运行时填的）。

    如果从 `parameters` 里读 `from_agent`，Agent 就能冒充别人发消息 ——
    这与 `Identity ≠ Address`、`ActionIntent ≠ Authorization` 同源。
    """
    captured = {}

    def fake_send(comm, *, from_agent, to_agent, message_type, text, payload):
        captured["from_agent"] = from_agent
        return {"ok": True}

    ex = _executor(fake_send)
    ex.execute(_intent({"to": "agent_b", "text": "x",
                        "from_agent": "agent_z"        # ← 企图冒充
                        }, agent="agent_a"))

    assert captured["from_agent"] == "agent_a", (
        "parameters 里的 from_agent 被采信了 —— Agent 可以冒充别人")


def test_default_message_type_is_plain_text():
    from actions.message_send import DEFAULT_MESSAGE_TYPE

    got = {}

    def fake_send(comm, *, from_agent, to_agent, message_type, text, payload):
        got["t"] = message_type
        return {"ok": True}

    _executor(fake_send).execute(_intent({"to": "agent_b", "text": "hi"}))
    assert got["t"] == DEFAULT_MESSAGE_TYPE == "TEXT", (
        "省略类型时应当是既有行为（普通文本），不是某个业务类型")


def test_any_message_type_passes_through():
    """
    🔒 **不过于耦合的关键**：执行器**不枚举**允许的类型。

    合法性由下游（mapper / handle_*）判断；在这里白名单化
    = 替 Agent 划定「它能说什么」。
    """
    got = {}

    def fake_send(comm, *, from_agent, to_agent, message_type, text, payload):
        got["t"] = message_type
        return {"ok": True}

    for t in ("SOME_FUTURE_TYPE_V9", "ACCEPT", "COMPLETE"):
        _executor(fake_send).execute(
            _intent({"to": "b", "message_type": t}))
        assert got["t"] == t


def test_failure_is_structured_not_an_exception():
    def fake_send(*a, **k):
        return {"ok": False, "to": "agent_b", "code": "send_failed",
                "message": "通信层炸了", "message_type": "TASK_OFFER"}

    res = _executor(fake_send).execute(_intent({"to": "agent_b"}))

    assert res.success is False
    assert "通信层炸了" in res.error
    assert res.metadata["code"] == "send_failed"
    assert res.output is None, "失败时 output 必须留空 —— 否则像发出去了"


def test_a_crashing_send_fn_still_returns_a_result():
    def boom(*a, **k):
        raise RuntimeError("comm 崩了")

    res = _executor(boom).execute(_intent({"to": "agent_b"}))
    assert res.success is False
    assert "RuntimeError" in res.error
    assert res.metadata["code"] == "executor_error"


def test_non_dict_return_is_rejected():
    res = _executor(lambda *a, **k: "裸字符串").execute(_intent({"to": "b"}))
    assert res.success is False
    assert res.metadata["code"] == "invalid_send_result"


def test_does_not_retry_or_swap_the_recipient():
    """只执行、不决策：失败也不换收件人重发。"""
    calls = []

    def fake_send(comm, *, from_agent, to_agent, message_type, text, payload):
        calls.append(to_agent)
        return {"ok": False, "code": "x", "message": "失败"}

    _executor(fake_send).execute(_intent({"to": "agent_b"}))
    assert calls == ["agent_b"], f"不该重试或换人，实际：{calls}"


# ======================================================================
# ② 叶子层：actions/ 不许 import services/（既有守卫覆盖到新文件）
# ======================================================================
def test_message_send_is_a_leaf():
    src = (ACTIONS / "message_send.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not (mod == "services" or mod.startswith("services.")), (
                f"message_send.py import 了 {mod} —— 它就不再是叶子层了")
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("services"), a.name


def test_service_side_has_no_action_or_comm_import():
    """反向：`services/messaging.py` 不认识 actions/ 与 communication 的具体实现。"""
    src = (ROOT / "services" / "messaging.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    for bad in ("actions", "agents", "storage"):
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), (
            f"services/messaging.py 不该 import {bad}：{sorted(mods)}")


# ======================================================================
# ③ 实验接线
# ======================================================================
def test_composition_root_registers_the_send_executor():
    tree = ast.parse(PROD.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_action_seam"),
              None)
    assert fn is not None
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "MessageSendExecutor" in names, (
        "_action_seam 没有注册 MessageSendExecutor —— Agent 又只能发 TEXT 了")


def test_prompt_states_message_send_as_a_fact():
    src = PROD.read_text(encoding="utf-8")
    assert "message_send" in src
    assert "TASK_OFFER" in src, "prompt 里要列出环境已注册的消息类型"
    for banned in ("你必须发", "一定要发", "应当先发给"):
        assert banned not in src, f"prompt 开始规定行为了：{banned!r}"


# ======================================================================
# ④ 真通信层：TASK_OFFER 真的让系统建 Task，creator 是发起方
# ======================================================================
@pytest.fixture
def comm_env(tmp_path):
    from main import build_runtime

    db = os.path.join(str(tmp_path), "msg.db")
    rt = build_runtime(db_path=db, agent_ids=["agent_a", "agent_b"])
    loop = asyncio.new_event_loop()
    try:
        yield rt, rt.communication, loop
    finally:
        loop.close()
        rt.close()


def _dispatcher(comm, loop):
    from actions import ActionDispatcher, MessageSendExecutor
    from services.messaging import send_message_as

    def _send(c, *, from_agent, to_agent, message_type, text, payload):
        return loop.run_until_complete(send_message_as(
            c, from_agent=from_agent, to_agent=to_agent,
            message_type=message_type, text=text, payload=payload))

    return ActionDispatcher([MessageSendExecutor(_send, comm=comm)])


def _offer(disp, tid, budget, desc="写一篇评测文档"):
    from actions import build_intent
    from services.messaging import build_offer_payload

    payload = build_offer_payload(task_id=tid, description=desc,
                                  budget=budget, workspace_id="WS-M")
    return disp.dispatch(build_intent("agent_a", {
        "type": "message_send",
        "parameters": {"to": "agent_b", "text": "接不接？",
                       "message_type": "TASK_OFFER", "payload": payload}}))


def test_task_offer_creates_a_task_with_the_sender_as_creator(comm_env):
    """🔴 核心：Agent 发一条 TASK_OFFER，系统就用**它**当 creator 建 Task。"""
    rt, comm, loop = comm_env
    res = _offer(_dispatcher(comm, loop), "T-OFFER-1",
                 {"amount": 30, "currency": "AIC"})
    assert res.success is True, res.error

    loop.run_until_complete(comm.receive("agent_b", timeout=0.2))

    t = rt.tasks.get("T-OFFER-1")
    assert t is not None, "TASK_OFFER 没有让系统建 Task"
    assert t.creator == "agent_a", (
        f"creator 应当是发起方 Agent（ADR-0024），实际 {t.creator!r}")
    assert t.status == "OFFERED", (
        f"应当是 OFFERED 而不是 ACCEPTED —— 接不接由收件方判断，实际 {t.status}")
    assert set(t.participants) == {"agent_a", "agent_b"}


def test_bad_payload_is_rejected_with_structured_feedback(comm_env):
    """
    🔒 坏 payload **不是**静默失败：

        Task 不建，而且发起方收到一条 `ERROR`，里面是逐条错误。

        ⇒ Agent 能**学到**（这比"发信成功"重要）。
    """
    rt, comm, loop = comm_env
    res = _offer(_dispatcher(comm, loop), "T-BAD", 30)   # 裸数字
    assert res.success is True, "发信本身是成功的 —— 校验在下游"

    loop.run_until_complete(comm.receive("agent_b", timeout=0.2))
    assert rt.tasks.get("T-BAD") is None, "坏 payload 不该建出 Task"

    got = loop.run_until_complete(comm.receive("agent_a", timeout=0.3))
    assert got is not None and got.message is not None, "发起方没有收到任何反馈"
    assert got.message.message_type == "ERROR"
    errors = (got.message.payload or {}).get("errors") or []
    assert any("currency" in e or "裸数字" in e for e in errors), (
        f"错误信息里应当指出金额格式问题，实际：{errors}")


def test_wakeup_is_signalled_but_carries_nothing(comm_env):
    """`Wakeup ≠ Message`：信号置位，但内容只能从信箱取。"""
    _rt, comm, loop = comm_env
    res = _offer(_dispatcher(comm, loop), "T-WAKE",
                 {"amount": 5, "currency": "AIC"})
    assert res.output["wakeup_signalled"] is True
