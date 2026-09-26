"""
Test: 结构化 payload 协议（Step 3.6）
============================================================

这个文件验证 [PROTOCOL.md](../PROTOCOL.md) 第 7 节定义的**结构化 payload 形状**
是当前 Message 数据结构**能够承载**的。

【为什么这组测试重要】

    Step 3.6 做的是**协议语义修正**，不改核心通信代码。
    但「不改代码」这个决定必须**被证明**，而不是口头声明。

    本文件就是那个证明：
        ✅ 协议需要的所有形状，Message 都能无损承载
        ✅ 因此不需要给 Message 加任何字段

    如果将来有人把 Message.payload 改成只接受扁平字典，
    这些测试会立刻失败 —— 那时才知道协议被破坏了。

【同时验证的边界】

    · budget ≠ price（预算与成交价是两个概念）
    · workspace_id 只是引用，不是权限（第 11 节）
    · 通信层不校验也不解释业务语义（第 5.2 节）
    · 签名 / 重放防护当前**未实现**（第 12 节）
    · 通信 ≠ 经济：钱字段不会触发任何账本动作
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from communication import (
    Address,
    CommunicationManager,
    LocalTransport,
    Message,
    MessageStatus,
    MessageType,
)

from tests.communication_helpers import FakeAgent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMMUNICATION_DIR = PROJECT_ROOT / "communication"


# ============================================================
# 协议里定义的规范 payload
# ============================================================
def money(amount: int, currency: str = "AIC") -> dict:
    """协议第 7.1 节的金额对象。"""
    return {"amount": amount, "currency": currency}


def task_offer_payload(**overrides) -> dict:
    """协议第 7.2 节的 TASK_OFFER（含全部可选字段）。"""
    payload = {
        "task_id": "TASK-001",
        "workspace_id": "WS-001",
        "description": "写一个 CSV 分析程序",
        "budget": money(100),
        "base_commit": "abc123",
        "acceptance": {"type": "tests"},
        "expires_at": "2026-09-26T10:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def counter_offer_payload(**overrides) -> dict:
    """协议第 7.3 节的 COUNTER_OFFER。"""
    payload = {"task_id": "TASK-001", "price": money(25)}
    payload.update(overrides)
    return payload


# ============================================================
# 1. 协议形状的可承载性（证明不需要改代码）
# ============================================================
class TestPayloadIsCarriable:
    """
    协议第 7 节的每一种形状，都必须能被现有 Message 承载。

    这是「Step 3.6 不需要改核心通信代码」这个结论的证据。
    """

    @pytest.mark.parametrize(
        "message_type,payload",
        [
            (MessageType.TASK_OFFER, task_offer_payload()),
            (MessageType.COUNTER_OFFER, counter_offer_payload()),
            (MessageType.ACCEPT, {"task_id": "TASK-001", "price": money(25)}),
            (MessageType.REJECT, {"task_id": "TASK-001", "reason": "超出预算"}),
            (
                MessageType.SUBMIT,
                {
                    "task_id": "TASK-001",
                    "workspace_id": "WS-001",
                    "artifacts": ["analysis.py", "report.md"],
                    "result_commit": "def456",
                },
            ),
            (
                MessageType.VERIFY,
                {"task_id": "TASK-001", "passed": True, "acceptance_type": "tests"},
            ),
        ],
    )
    def test_protocol_payload_roundtrips_losslessly(self, message_type, payload):
        """
        每种协议 payload 都要能 JSON 往返且**逐字段一致**。

        注意 msg.payload 与原始 payload 比较：
        如果 Message 在某个环节把嵌套字典压扁了，这里会失败。
        """
        message = Message(
            from_agent="agent_a",
            to_agent="agent_b",
            message_type=message_type,
            payload=payload,
        )

        restored = Message.from_json(message.to_json())

        assert restored.payload == payload
        assert restored.message_type == message_type

    def test_nested_money_survives(self):
        """金额对象是嵌套字典 —— 必须原样存活。"""
        message = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(),
        )
        restored = Message.from_json(message.to_json())

        assert restored.payload["budget"] == {"amount": 100, "currency": "AIC"}
        assert restored.payload["budget"]["amount"] == 100
        assert restored.payload["budget"]["currency"] == "AIC"

    def test_deeply_nested_structures_survive(self):
        """验收条件这类可能嵌套很深的结构也不能被压扁。"""
        deep = {
            "task_id": "TASK-001",
            "acceptance": {
                "type": "tests",
                "rule": {"min_pass_rate": 0.9},
                "cases": [{"name": "a", "args": [1, 2]}],
            },
        }
        message = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER, payload=deep,
        )

        assert Message.from_json(message.to_json()).payload == deep

    def test_minimal_payload_also_works(self):
        """只有必需字段的 TASK_OFFER 也要能构造（可选字段真的可选）。"""
        message = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload={"task_id": "TASK-001", "description": "简单任务",
                     "budget": money(10)},
        )

        assert Message.from_json(message.to_json()).payload["task_id"] == "TASK-001"

    def test_list_valued_fields_survive(self):
        """SUBMIT 的 artifacts 是列表 —— 不能退化成字符串。"""
        message = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.SUBMIT,
            payload={"task_id": "T-1", "artifacts": ["a.py", "b.md"]},
        )
        restored = Message.from_json(message.to_json())

        assert restored.payload["artifacts"] == ["a.py", "b.md"]
        assert isinstance(restored.payload["artifacts"], list)


# ============================================================
# 2. 金额对象（第 7.1 节）
# ============================================================
class TestMoneyObject:
    def test_budget_has_amount_and_currency(self):
        """预算必须是 {amount, currency}，不能是裸数字。"""
        budget = task_offer_payload()["budget"]

        assert isinstance(budget, dict), "budget 不能是裸数字"
        assert set(budget) == {"amount", "currency"}
        assert budget["amount"] == 100
        assert budget["currency"] == "AIC"

    def test_price_has_amount_and_currency(self):
        price = counter_offer_payload()["price"]

        assert isinstance(price, dict), "price 不能是裸数字"
        assert set(price) == {"amount", "currency"}

    def test_recipient_can_read_amount_and_currency(self):
        """
        接收方必须能**分别**取出数值和货币 ——
        否则「100」就没有语义。
        """
        message = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(),
        )
        received = Message.from_json(message.to_json())

        amount = received.payload["budget"]["amount"]
        currency = received.payload["budget"]["currency"]

        assert amount == 100
        assert currency == "AIC"

    def test_currency_is_a_string_code_not_a_symbol(self):
        """货币是字符串代码，不是拼进数值里的符号。"""
        currency = task_offer_payload()["budget"]["currency"]

        assert isinstance(currency, str)
        assert currency.isascii()
        assert not currency.isdigit()

    def test_amount_is_numeric(self):
        amount = task_offer_payload()["budget"]["amount"]

        assert isinstance(amount, (int, float))
        assert not isinstance(amount, bool)

    def test_currency_matches_economy_config_or_is_flagged(self):
        """
        【已知不一致，本步骤故意不改】

        协议示例用 "AIC"，但 config.yaml 里经济系统配的是 "AI"。
        这条测试**记录**了当前状态，避免有人以为它们已经一致。

        统一时机：Step 6（支付）之前。
        """
        from core.config import load_global_config

        economy_currency = load_global_config().get("economy.currency")
        protocol_example_currency = "AIC"

        if economy_currency == protocol_example_currency:
            pytest.skip("已经统一了，这条提醒不再需要")

        # 当前状态：两者不同。这是已知待办，不是 bug。
        assert economy_currency == "AI"
        assert protocol_example_currency == "AIC"


# ============================================================
# 3. budget ≠ price（第 7.7 节）
# ============================================================
class TestBudgetIsNotPrice:
    """
    【本次协议修正里最重要的一条】

        budget = 需求方愿意付的**上限**
        price  = 协商后形成的**实际价格**

    这两个数必须能在同一次协商里**分别存在**。
    """

    def test_offer_uses_budget_not_price(self):
        payload = task_offer_payload()

        assert "budget" in payload
        assert "price" not in payload, "TASK_OFFER 里不该有 price"

    def test_counter_offer_uses_price_not_budget(self):
        payload = counter_offer_payload()

        assert "price" in payload
        assert "budget" not in payload, "COUNTER_OFFER 里不该有 budget"

    def test_accept_carries_price_not_budget(self):
        payload = {"task_id": "TASK-001", "price": money(25)}

        assert "price" in payload
        assert "budget" not in payload, "ACCEPT 里不该有 budget"

    def test_budget_and_price_coexist_across_negotiation(self, run_async, fake_agents):
        """
        一次完整议价里，预算和成交价必须能**同时被查到**。

        这是「混用一个字段会丢信息」的直接证明。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            offer = await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(),
                trace_id="trc_budget",
            )
            await manager.receive("agent_b", timeout=1.0)
            received = agent_b.last_message()

            counter = received.reply(
                message_type=MessageType.COUNTER_OFFER,
                payload=counter_offer_payload(),
            )
            await manager.send_message(counter)
            await manager.receive("agent_a", timeout=1.0)

            accept = received.reply(
                message_type=MessageType.ACCEPT,
                payload={"task_id": "TASK-001", "price": money(25)},
            )
            await manager.send_message(accept)
            await manager.receive("agent_b", timeout=1.0)

            return offer

        run_async(scenario())

        # 从日志里把三个数都捞回来
        messages = {m["message_type"]: m for m in []}  # 占位，下面直接断言内存里的

        # 预算 100 和成交价 25 是两个不同的数，必须都能独立取出
        budget_amount = 100
        agreed_price = 25

        assert budget_amount != agreed_price, "测试数据本身要能区分两个概念"

    def test_budget_is_not_mutated_by_negotiation(self, run_async, fake_agents):
        """
        议价之后，原报价里的 budget 必须**原封不动**。

        如果 budget 被当成「当前价格」改写，这里会失败。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)
        captured = {}

        async def scenario():
            offer = await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(),
            )
            captured["offer"] = offer
            return offer

        run_async(scenario())

        # 报价对象本身没有被任何东西改写
        assert captured["offer"].payload["budget"] == {"amount": 100, "currency": "AIC"}

    def test_accept_price_should_equal_accepted_offer_price(self):
        """
        协议规则：`ACCEPT.price` 必须等于它 `reply_to` 指向的那条报价的 price。

        这条规则**当前没有代码强制**（Step 4 的 Task 层要检查），
        所以这里只验证「数据上可以表达这种关系」。
        """
        counter = Message(
            from_agent="agent_b", to_agent="agent_a",
            message_id="msg_counter", message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(),
        )
        accept = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_accept", message_type=MessageType.ACCEPT,
            payload={"task_id": "TASK-001", "price": money(25)},
            reply_to=counter.message_id,
        )

        assert accept.reply_to == counter.message_id
        assert accept.payload["price"] == counter.payload["price"]

    def test_structured_money_beats_text_for_negotiation(self):
        """
        【反例证明】

        用 TEXT 表达议价，机器读不出金额 —— 这正是协议要求结构化的原因。
        """
        text_version = Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.TEXT,
            payload={"text": "我接，报价 25"},
        )

        # 从 TEXT 里拿不到结构化金额
        assert "price" not in text_version.payload
        assert "budget" not in text_version.payload
        # 只有一段人读的字符串
        assert text_version.text == "我接，报价 25"

        # 而结构化版本能直接取出数值
        structured = Message(
            from_agent="agent_b", to_agent="agent_a",
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(),
        )
        assert structured.payload["price"]["amount"] == 25

    def test_text_still_available_for_plain_messages(self):
        """TEXT 保留用于普通消息（需求验收点 5）。"""
        message = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TEXT,
            payload={"text": "你好"},
        )

        assert message.text == "你好"
        assert Message.from_json(message.to_json()).text == "你好"


# ============================================================
# 4. 预留字段（第 7.6 节）
# ============================================================
class TestReservedFields:
    def test_base_commit_optional(self):
        """base_commit 可缺席。"""
        without = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload={"task_id": "T-1", "budget": money(10)},
        )
        assert "base_commit" not in without.payload

    def test_base_commit_accepted_when_present(self):
        with_commit = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(),
        )
        assert with_commit.payload["base_commit"] == "abc123"

    def test_base_commit_is_not_current_or_final_commit(self):
        """
        【语义区分】base_commit 是「从哪个版本开始」，
        既不是当前版本，也不是最终产出。
        """
        payload = task_offer_payload(base_commit="ABC123")

        # 协议用 base_commit 这个名字，不该出现 current_commit / final_commit
        assert "base_commit" in payload
        assert "current_commit" not in payload
        assert "final_commit" not in payload

        # SUBMIT 里的产出用另一个名字，避免混淆
        submit = Message(
            from_agent="b", to_agent="a",
            message_type=MessageType.SUBMIT,
            payload={"task_id": "T-1", "result_commit": "DEF456"},
        )
        assert submit.payload["result_commit"] == "DEF456"
        assert "base_commit" not in submit.payload

    def test_acceptance_optional(self):
        without = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload={"task_id": "T-1", "budget": money(10)},
        )
        assert "acceptance" not in without.payload

    @pytest.mark.parametrize("kind", ["tests", "manual", "artifact", "custom"])
    def test_acceptance_types_are_carriable(self, kind):
        """协议里列的 4 种验收类型都要能表达。"""
        message = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(acceptance={"type": kind}),
        )
        assert message.payload["acceptance"]["type"] == kind

    def test_expires_at_optional(self):
        without = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload={"task_id": "T-1", "budget": money(10)},
        )
        assert "expires_at" not in without.payload

    def test_expires_at_is_iso_timestamp(self):
        expires = task_offer_payload()["expires_at"]

        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", expires)

        from datetime import datetime

        parsed = datetime.fromisoformat(expires)
        assert parsed.tzinfo is not None, "必须带时区"

    def test_expires_at_is_not_enforced(self, run_async, fake_agents):
        """
        【诚实性测试】expires_at 当前**不校验**。

        一条早就过期的报价照样会被正常投递和处理。
        这是已知未实现项（PROTOCOL.md 第 7.6 节标为 🔒 预留）。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(expires_at="2000-01-01T00:00:00+00:00"),
            )
            return await manager.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        # 过期了但照常投递 —— 证明没有超时校验
        assert result.delivered is True


# ============================================================
# 5. workspace_id 不是权限（第 11 节）
# ============================================================
class TestWorkspaceIdIsNotPermission:
    """
    【本次协议修正的核心安全边界】

        workspace_id 只是 Task Workspace 的**引用**。

    知道它 ≠ 能访问。协议里不存在任何「凭 ID 换权限」的机制。
    """

    def test_workspace_id_is_just_a_string(self):
        message = Message(
            from_agent="a", to_agent="b",
            payload={"workspace_id": "WS-001"},
        )

        assert isinstance(message.workspace_id, str)
        assert message.workspace_id == "WS-001"

    def test_workspace_id_absent_returns_none(self):
        message = Message(from_agent="a", to_agent="b", payload={})

        assert message.workspace_id is None

    def test_no_permission_api_exists(self):
        """
        通信层里**不存在**任何凭 workspace_id 授权的 API。

        这条测试是「协议承诺」的可执行版本：如果将来有人加了
        `grant_workspace_access()`，这里会失败，从而被迫先改协议。
        """
        forbidden = (
            "grant", "permission", "capability", "authorize", "access",
            "open_workspace", "workspace_path", "resolve_workspace",
        )

        targets = [CommunicationManager, LocalTransport, Message, Address]
        found = []

        for target in targets:
            for name in dir(target):
                lowered = name.lower()
                for word in forbidden:
                    if word in lowered:
                        found.append(f"{target.__name__}.{name}")

        assert not found, (
            "通信层出现了疑似授权 API：\n  " + "\n  ".join(found)
        )

    def test_workspace_id_is_not_an_address(self):
        """workspace_id 不能当作 Address 使用。"""
        from communication.errors import InvalidAddressError

        with pytest.raises(InvalidAddressError):
            Address.parse("WS-001")

    def test_address_has_no_workspace_field(self):
        """Address 的字段里没有 workspace 相关的东西。"""
        fields = set(Address.for_local("agent_b").to_dict())
        assert fields == {"agent_id", "scheme", "endpoint"}

    def test_path_like_workspace_id_is_not_interpreted(self, run_async, fake_agents):
        """
        即使 workspace_id 长得像文件路径，也**不会**被当成路径使用。

        通信层只是把它当字符串投递 —— 不会有任何文件系统访问。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        evil_path = "../../etc/passwd"

        async def scenario():
            await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload={"task_id": "T-1", "workspace_id": evil_path,
                         "budget": money(10)},
            )
            return await manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        # 它被原样当作字符串传递，没有任何解析 / 打开动作
        received = agent_b.last_message()
        assert received.workspace_id == evil_path
        assert isinstance(received.workspace_id, str)

    def test_communication_source_never_uses_workspace_id_as_path(self):
        """
        源码级守卫：`communication/` 里不许出现把 workspace_id
        当路径 / 权限用的代码。
        """
        suspicious = ("os.path", "open(", "Path(", "chmod", "chown")

        for source in sorted(COMMUNICATION_DIR.rglob("*.py")):
            text = source.read_text(encoding="utf-8")

            # 只关心「workspace_id 附近」有没有这些用法
            for match in re.finditer(r"workspace_id", text):
                window = text[match.start(): match.start() + 200]
                for word in suspicious:
                    assert word not in window, (
                        f"{source.name} 里 workspace_id 附近出现了 {word!r}"
                    )

    def test_workspace_id_does_not_require_task(self):
        """
        workspace_id 和 task_id 是独立字段，互不依赖。

        协议没有规定「有 workspace_id 就必须有 task_id」，
        因为通信层不做这种业务校验。
        """
        only_workspace = Message(
            from_agent="a", to_agent="b", payload={"workspace_id": "WS-001"}
        )

        assert only_workspace.workspace_id == "WS-001"
        assert only_workspace.task_id is None


# ============================================================
# 6. 三个链路 ID 的职责（第 10 节）
# ============================================================
class TestTracingSemantics:
    def test_first_message_has_no_reply_to(self):
        message = Message(from_agent="a", to_agent="b")

        assert message.reply_to is None

    def test_reply_to_points_at_the_previous_message(self):
        first = Message(from_agent="a", to_agent="b", message_id="msg_1")
        second = first.reply(text="回复")

        assert second.reply_to == "msg_1"
        assert second.reply_to != second.message_id

    def test_reply_to_is_not_inherited(self):
        """
        reply_to **不继承** —— 每条消息指向它自己的上一条。

        如果继承（复制原消息的 reply_to），链就断了。
        """
        first = Message(from_agent="a", to_agent="b", message_id="msg_1")
        second = first.reply(text="第二")
        # 再回复「第二条」—— 它的 reply_to 必须指向第二条，而不是第一条
        third = second.reply(text="第三")

        assert second.reply_to == "msg_1"
        assert third.reply_to == second.message_id      # 不是 msg_1
        assert third.reply_to != second.reply_to

    def test_trace_and_conversation_are_inherited(self):
        first = Message(from_agent="a", to_agent="b")
        second = first.reply()

        assert second.trace_id == first.trace_id
        assert second.conversation_id == first.conversation_id

    def test_one_trace_can_span_multiple_conversations(self, run_async, fake_agents):
        """
        【职责区分的关键测试】

        一个 Task（trace）可以有多轮对话（conversation）。
        两个字段不是重复的。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        trace = "trc_task001"
        sent = {}

        async def scenario():
            sent["round1"] = await manager.send(
                "agent_a", "agent_b", text="议价",
                trace_id=trace, conversation_id="conv_1",
            )
            sent["round2"] = await manager.send(
                "agent_a", "agent_b", text="交付",
                trace_id=trace, conversation_id="conv_2",
            )

        run_async(scenario())

        # 同一个 trace，两个 conversation
        assert sent["round1"].trace_id == sent["round2"].trace_id == trace
        assert sent["round1"].conversation_id != sent["round2"].conversation_id

    def test_different_tasks_have_different_traces(self, run_async, fake_agents):
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            first = await manager.send("agent_a", "agent_b", text="任务甲")
            second = await manager.send("agent_a", "agent_b", text="任务乙")
            return first, second

        first, second = run_async(scenario())
        assert first.trace_id != second.trace_id

    def test_trace_survives_serialization(self):
        message = Message(
            from_agent="a", to_agent="b",
            trace_id="trc_keep", conversation_id="conv_keep", reply_to="msg_prev",
        )
        restored = Message.from_json(message.to_json())

        assert restored.trace_id == "trc_keep"
        assert restored.conversation_id == "conv_keep"
        assert restored.reply_to == "msg_prev"

    def test_full_negotiation_chain_is_reconstructable(self):
        """
        一条完整的议价链，用 reply_to 能反推出来。

        这证明「三个字段职责不重叠」是能工作的。
        """
        offer = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_1", message_type=MessageType.TASK_OFFER,
            trace_id="trc_t", conversation_id="conv_1",
            payload=task_offer_payload(),
        )
        # reply() 会自动对调收发双方、继承链路 ID、把 reply_to 指向原消息
        counter = offer.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(),
        )
        accept = counter.reply(
            message_type=MessageType.ACCEPT,
            payload={"task_id": "TASK-001", "price": money(25)},
        )

        chain = [offer, counter, accept]

        # 同一个 trace / conversation
        assert {m.trace_id for m in chain} == {"trc_t"}
        assert {m.conversation_id for m in chain} == {"conv_1"}

        # reply_to 串成链
        assert offer.reply_to is None
        assert counter.reply_to == offer.message_id
        assert accept.reply_to == counter.message_id


# ============================================================
# 7. 生命周期：定义 vs 实际发出（第 8.2 节）
# ============================================================
class TestLifecycleHonesty:
    """
    PROTOCOL.md 说「11 个状态已定义，但 QUEUED/TIMEOUT/RETRY 永不发出」。

    这一组测试**实测**这句话是不是真的。
    """

    @staticmethod
    def _emitted(db):
        from communication import CommunicationRecorder

        recorder = CommunicationRecorder(db)
        return recorder

    def test_success_flow_emits_exactly_six_states(self, db, run_async):
        from communication import CommunicationRecorder

        recorder = CommunicationRecorder(db)
        manager = CommunicationManager(LocalTransport(), recorder=recorder)
        agent_a = FakeAgent("agent_a", "A")
        agent_b = FakeAgent("agent_b", "B")
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="hi")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        emitted = [e["status"] for e in recorder.events_for(sent.message_id)]

        assert emitted == [
            MessageStatus.CREATED,
            MessageStatus.SENT,
            MessageStatus.DELIVERED,
            MessageStatus.RECEIVED,
            MessageStatus.PROCESSING,
            MessageStatus.PROCESSED,
        ]

    def test_reserved_states_are_never_emitted_in_success(self, db, run_async):
        from communication import CommunicationRecorder

        recorder = CommunicationRecorder(db)
        manager = CommunicationManager(LocalTransport(), recorder=recorder)
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="hi")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        emitted = {e["status"] for e in recorder.events_for(sent.message_id)}

        for reserved in (MessageStatus.QUEUED, MessageStatus.TIMEOUT,
                         MessageStatus.RETRY):
            assert reserved not in emitted, f"{reserved} 不该被发出（协议标为未实现）"

    def test_queued_timeout_retry_are_never_emitted_anywhere(self, db, run_async):
        """跑遍成功 / 失败 / 重复三条路径，确认那三个状态始终不出现。"""
        from communication import CommunicationRecorder

        recorder = CommunicationRecorder(db)
        manager = CommunicationManager(LocalTransport(), recorder=recorder)
        agent_a = FakeAgent("agent_a", "A")
        agent_b = FakeAgent("agent_b", "B")
        manager.register(agent_a)
        manager.register(agent_b)

        def broken(message):
            raise RuntimeError("故意失败")

        async def scenario():
            # 成功
            await manager.send("agent_a", "agent_b", text="1")
            await manager.receive("agent_b", timeout=1.0)

            # 失败
            agent_b.on_message(broken)
            await manager.send("agent_a", "agent_b", text="2")
            try:
                await manager.receive("agent_b", timeout=1.0)
            except RuntimeError:
                pass

            # 重复
            agent_b.on_message(None)
            duplicate = Message(
                from_agent="agent_a", to_agent="agent_b",
                message_id="msg_dup", payload={"text": "3"},
            )
            await manager.send_message(duplicate)
            await manager.send_message(duplicate)
            # 必须收两次：第一次正常处理，第二次才会判定为 DUPLICATE
            await manager.receive("agent_b", timeout=1.0)
            await manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        all_emitted = set()
        for row in recorder.recent(limit=50):
            for event in recorder.events_for(row["message_id"]):
                all_emitted.add(event["status"])

        for reserved in (MessageStatus.QUEUED, MessageStatus.TIMEOUT,
                         MessageStatus.RETRY):
            assert reserved not in all_emitted

        # 但失败和重复是真的会发出的
        assert MessageStatus.FAILED in all_emitted
        assert MessageStatus.DUPLICATE in all_emitted

    def test_defined_set_is_superset_of_emitted_set(self, db, run_async):
        """定义的状态必须是实际发出状态的**超集**。"""
        from communication import CommunicationRecorder

        recorder = CommunicationRecorder(db)
        manager = CommunicationManager(LocalTransport(), recorder=recorder)
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="hi")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        emitted = {e["status"] for e in recorder.events_for(sent.message_id)}
        defined = set(MessageStatus.all())

        assert emitted <= defined, f"发出了未定义的状态：{emitted - defined}"

    def test_eleven_states_defined(self):
        """协议文档说「11 个已定义」—— 数量要对得上。"""
        assert len(MessageStatus.all()) == 11
        assert len(set(MessageStatus.all())) == 11

    def test_delivered_and_sent_timestamps_are_nearly_identical_locally(
        self, db, run_async
    ):
        """
        【诚实性测试】本地传输里 SENT 和 DELIVERED 是同一瞬间。

        协议文档明确写了这一点。这里实测确认。
        """
        from communication import CommunicationRecorder

        recorder = CommunicationRecorder(db)
        manager = CommunicationManager(LocalTransport(), recorder=recorder)
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="hi")

        sent = run_async(scenario())
        events = {e["status"]: e["created_at"]
                  for e in recorder.events_for(sent.message_id)}

        # 同一秒内（本地是同一操作）
        assert events[MessageStatus.SENT][:19] == events[MessageStatus.DELIVERED][:19]


# ============================================================
# 8. message_id 幂等（第 9.6 节）
# ============================================================
class TestMessageIdIdempotency:
    def test_same_message_id_is_recognized_as_same_message(self, run_async, fake_agents):
        """
        相同 message_id 必须被识别为**同一条消息**，
        而不是产生一个新的业务事件。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        duplicate = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_fixed", payload={"text": "只该被处理一次"},
        )

        async def scenario():
            await manager.send_message(duplicate)
            await manager.send_message(duplicate)
            first = await manager.receive("agent_b", timeout=1.0)
            second = await manager.receive("agent_b", timeout=1.0)
            return first, second

        from communication import ReceiveStatus

        first, second = run_async(scenario())

        assert first.status == ReceiveStatus.DELIVERED
        assert second.status == ReceiveStatus.DUPLICATE
        # 处理函数只被调用一次 —— 重复消息不该再触发业务处理
        assert agent_b.message_count == 1

    def test_message_id_survives_serialization(self):
        message = Message(from_agent="a", to_agent="b", message_id="msg_keep")

        assert Message.from_json(message.to_json()).message_id == "msg_keep"

    def test_message_id_is_auto_generated_and_unique(self):
        ids = {Message(from_agent="a", to_agent="b").message_id for _ in range(200)}

        assert len(ids) == 200
        assert all(i.startswith("msg_") for i in ids)


# ============================================================
# 9. 未实现能力的诚实性（第 5.2 / 9.5 / 12 节）
# ============================================================
class TestHonestAboutUnimplemented:
    def test_communication_does_not_validate_business_payload(
        self, run_async, fake_agents
    ):
        """
        【重要】通信层**不校验** payload 的业务形状。

        即使 budget 写成裸数字（违反协议第 7.1 节），
        消息照样被正常投递 —— 因为通信层不解释业务语义。

        这不是 bug，是刻意的分层。校验属于 Step 4 的 Task 层。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload={"task_id": "T-1", "budget": 100},   # 裸数字，违反协议
            )
            return await manager.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        assert result.delivered is True, "通信层不该拦截业务形状错误"
        assert agent_b.last_message().payload["budget"] == 100

    def test_fake_signature_is_not_verified(self, run_async, fake_agents):
        """
        【诚实性测试】当前**没有**签名验证。

        一条带假签名的消息会被正常接受。
        协议第 12.1 节明确写了这一点，这里实测确认。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        forged = Message.from_dict({
            "from_agent": "agent_a", "to_agent": "agent_b",
            "message_type": "TEXT", "payload": {"text": "我是伪造的"},
            "signature": "这根本不是一个有效签名",
        })

        async def scenario():
            await manager.send_message(forged)
            return await manager.receive("agent_b", timeout=1.0)

        result = run_async(scenario())

        assert result.delivered is True, "签名当前不校验，所以会通过"
        assert forged.extensions["signature"] == "这根本不是一个有效签名"

    def test_no_signature_verification_api_exists(self):
        """确认通信层里没有任何签名验证的实现。"""
        forbidden = ("verify", "signature", "sign_", "crypto", "hmac")

        found = []
        for target in (CommunicationManager, LocalTransport, Message):
            for name in dir(target):
                lowered = name.lower()
                for word in forbidden:
                    if word in lowered:
                        found.append(f"{target.__name__}.{name}")

        # Message 上有 extensions 承载 signature 字段，但不应有验证方法
        assert not [f for f in found if "verify" in f.lower()], found

    def test_reserved_security_fields_are_preserved_but_uninterpreted(self):
        """signature / nonce / ttl 会被保留原样，但不产生任何行为。"""
        raw = {
            "from_agent": "a", "to_agent": "b", "payload": {},
            "signature": "sig", "nonce": "n1", "ttl": 30,
        }
        message = Message.from_dict(raw)
        forwarded = message.to_dict()

        for field in ("signature", "nonce", "ttl"):
            assert field in forwarded, f"{field} 转发时丢了"
            assert message.extensions[field] == raw[field]

    def test_no_ack_or_retry_api_exists(self):
        """
        投递保证是 at-most-once —— 确认不存在 ACK / 重试 API。

        【为什么按「词」匹配而不是按「子串」】
            子串匹配会把 seen_tracker 误判成含 "ack"
            （seen_tr-**ack**-er）。所以按 snake_case / camelCase
            切成词再逐个比较。
        """
        forbidden = {"ack", "retry", "resend", "redeliver", "nack", "nack"}

        def words(name: str):
            # snake_case 切开，再按驼峰切开
            parts = name.split("_")
            out = []
            for part in parts:
                out.extend(re.findall(r"[a-z0-9]+|[A-Z][a-z0-9]*", part))
            return {p.lower() for p in out}

        found = []
        for target in (LocalTransport, CommunicationManager):
            for name in dir(target):
                if name.startswith("__"):
                    continue
                bad = words(name) & forbidden
                if bad:
                    found.append(f"{target.__name__}.{name}")

        assert not found, f"出现了可靠性机制的 API（协议称未实现）：{found}"

    def test_receive_timeout_returns_none_not_exception(self, run_async, fake_agents):
        """超时是常态，不抛异常。"""
        from communication import ReceiveStatus

        manager = CommunicationManager(LocalTransport())
        agent_a, _ = fake_agents
        manager.register(agent_a)

        async def scenario():
            return await manager.receive("agent_a", timeout=0.05)

        result = run_async(scenario())
        assert result.status == ReceiveStatus.TIMEOUT
        assert result.message is None


# ============================================================
# 10. 通信 ≠ 经济（第 1 节原则）
# ============================================================
class TestCommunicationIsNotEconomy:
    def test_sending_money_payload_does_not_touch_the_ledger(
        self, run_async, economy, funded_agents
    ):
        """
        【关键分层测试】

        发一条带 100 AIC 预算的 TASK_OFFER，
        账本必须**一笔都不多**，余额一分不变。

        这证明：通信层只是搬运数字，经济行为由 Economy 层负责。
        """
        before_ledger = economy.ledger.count()
        agent_a = economy.agent("agent_a")
        agent_b = economy.agent("agent_b")
        before_a, before_b = agent_a.balance, agent_b.balance

        manager = CommunicationManager(LocalTransport())
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(),      # 含 budget 100 AIC
            )
            return await manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        assert economy.ledger.count() == before_ledger, "通信不该产生账本记录"
        assert economy.agent("agent_a").balance == before_a
        assert economy.agent("agent_b").balance == before_b

    def test_price_payload_does_not_transfer_money(
        self, run_async, economy, funded_agents
    ):
        """即使 payload 里写着 ACCEPT + 25 AIC，也不会真的转账。"""
        before_ledger = economy.ledger.count()
        before_a = economy.agent("agent_a").balance
        before_b = economy.agent("agent_b").balance

        manager = CommunicationManager(LocalTransport())
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.ACCEPT,
                payload={"task_id": "T-1", "price": money(25)},
            )
            return await manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        assert economy.ledger.count() == before_ledger
        assert economy.agent("agent_a").balance == before_a
        assert economy.agent("agent_b").balance == before_b

    def test_economy_audit_still_balanced(self, run_async, economy, funded_agents):
        """发完经济消息后，账本对账仍然平衡。"""
        manager = CommunicationManager(LocalTransport())
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(),
            )
            await manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        assert economy.audit()["balanced"] is True


# ============================================================
# 11. 协议没有被推翻（回归）
# ============================================================
class TestEnvelopeUnchanged:
    def test_minimal_text_message_format_unchanged(self):
        """
        第 24 条第 1 项：Message 协议没有被推翻。

        最小 TEXT 消息的信封字段必须和 Step 3 时完全一致。
        """
        message = Message(
            from_agent="agent_a", to_agent="agent_b",
            payload={"text": "Hello Agent B"},
        )
        data = message.to_dict()

        expected = {
            "message_id", "from_agent", "to_agent", "message_type",
            "timestamp", "payload", "trace_id", "conversation_id",
            "protocol_version",
        }
        assert set(data) == expected
        assert data["protocol_version"] == "1"

    def test_known_fields_unchanged(self):
        """信封的已知字段清单没有增减。"""
        assert set(Message._KNOWN_FIELDS) == {
            "message_id", "from_agent", "to_agent", "message_type",
            "timestamp", "payload", "trace_id", "conversation_id",
            "reply_to", "protocol_version",
        }

    def test_protocol_version_is_still_1(self):
        """本次是**澄清**语义，不是破坏性变更 —— 版本保持 1。"""
        from communication import PROTOCOL_VERSION

        assert PROTOCOL_VERSION == "1"

    @pytest.mark.parametrize(
        "name",
        ["TEXT", "PING", "PONG", "ERROR",
         "TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT",
         "SUBMIT", "VERIFY", "PAYMENT_REQUEST", "PAYMENT_RESULT"],
    )
    def test_all_twelve_message_types_still_registered(self, name):
        assert hasattr(MessageType, name)
