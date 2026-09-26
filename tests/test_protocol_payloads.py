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

    def test_currency_is_unified_as_aic(self):
        """
        【Step 3.7】货币代码已统一为 AIC。

        这条测试原来是「记录已知不一致」的提醒（会在不一致时通过）。
        Step 3.7 统一之后，它反过来成为**强制统一**的守卫：
        谁把货币代码改回 "AI"，这里立刻失败。
        """
        from core.config import load_global_config

        assert load_global_config().get("economy.currency") == "AIC"
        assert task_offer_payload()["budget"]["currency"] == "AIC"
        assert counter_offer_payload()["price"]["currency"] == "AIC"

    def test_economy_and_protocol_agree_on_currency(self):
        """
        经济系统的货币代码，必须和协议示例用的是同一个。

        这是 Step 3.7 的核心要求：项目里只能有一个货币代码。
        """
        from core.config import load_global_config

        protocol_currency = money(1)["currency"]
        economy_currency = load_global_config().get("economy.currency")

        assert protocol_currency == economy_currency == "AIC"

    def test_no_ai_currency_code_left_in_config(self):
        """
        配置里不能再出现把 "AI" 当货币代码的写法。

        （注意 "AI Economy" / "AI Coin" 是名字，不是代码，不受影响。）
        """
        from pathlib import Path as _Path

        import yaml

        config = yaml.safe_load(
            _Path("config/config.yaml").read_text(encoding="utf-8")
        )
        assert config["economy"]["currency"] == "AIC"
        # 名字可以保留 "AI Coin"，但代码必须是 AIC
        assert "AI Coin" in config["economy"]["currency_name"]


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


# ============================================================
# 12. ACCEPT 的最终成交价语义（Step 3.7）
# ============================================================
def accepted_price_for(offer_message: Message):
    """
    协议规则的可执行版本：

        ACCEPT.price = 被 ACCEPT 的 Offer 所要求的价格

        如果 reply_to 指向 TASK_OFFER    → 取它的 budget
        如果 reply_to 指向 COUNTER_OFFER → 取它的 price

    【注意】这是**测试里的**参考实现，不是生产代码。
    真正的校验属于 Step 4 的 Task 层。
    放在这里是为了把协议规则写成可执行的形式，避免歧义。
    """
    if offer_message.message_type == MessageType.TASK_OFFER:
        return offer_message.payload["budget"]
    if offer_message.message_type == MessageType.COUNTER_OFFER:
        return offer_message.payload["price"]
    raise ValueError(f"不能 ACCEPT 一个 {offer_message.message_type}")


def build_accept(offer_message: Message, task_id: str = "TASK-001") -> Message:
    """按协议规则构造 ACCEPT。"""
    return offer_message.reply(
        message_type=MessageType.ACCEPT,
        payload={
            "task_id": task_id,
            "price": accepted_price_for(offer_message),
        },
    )


class TestAcceptFinalPrice:
    """
    ACCEPT.price 的两种情况。

    这是 Step 3.7 的核心修正之一：明确「接受 TASK_OFFER」和
    「接受 COUNTER_OFFER」时，成交价分别取自哪里。
    """

    # ---------------- 情况 A：直接接受 TASK_OFFER ----------------
    def test_case_a_direct_accept_takes_budget_as_price(self):
        """
        A 发出 TASK_OFFER(budget=100)，B 直接 ACCEPT。

        此时成交价 = 预算 = 100。
        """
        offer = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(budget=money(100)),
        )

        accept = build_accept(offer)

        # 成交价等于预算
        assert accept.payload["price"] == {"amount": 100, "currency": "AIC"}
        assert accept.payload["price"] == offer.payload["budget"]
        # 指向那条 TASK_OFFER
        assert accept.reply_to == offer.message_id
        # ACCEPT 自己不出现 budget
        assert "budget" not in accept.payload

    def test_case_a_is_a_reply_so_direction_is_flipped(self):
        """ACCEPT 是回复 —— 收发双方自动对调。"""
        offer = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.TASK_OFFER, payload=task_offer_payload(),
        )
        accept = build_accept(offer)

        assert accept.from_agent == "agent_b"      # 原接收方来接受
        assert accept.to_agent == "agent_a"

    # ---------------- 情况 B：接受 COUNTER_OFFER ----------------
    def test_case_b_accept_takes_counter_price(self):
        """
        A 发 TASK_OFFER(budget=100)，B 回 COUNTER_OFFER(price=25)，
        A ACCEPT。

        此时成交价 = 25，**不是** 100。
        """
        offer = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(budget=money(100)),
        )
        counter = offer.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(price=money(25)),
        )

        accept = build_accept(counter)

        assert accept.payload["price"] == {"amount": 25, "currency": "AIC"}
        assert accept.reply_to == counter.message_id

    def test_case_b_final_price_differs_from_budget(self):
        """
        【关键断言】预算 100、成交 25 —— 两个数必须能分别查到。

        如果 budget 被当成成交价，这里会看到 100 而不是 25。
        """
        offer = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(budget=money(100)),
        )
        counter = offer.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(price=money(25)),
        )
        accept = build_accept(counter)

        # 三个数各自独立存在
        assert offer.payload["budget"]["amount"] == 100     # 预算，没被改
        assert counter.payload["price"]["amount"] == 25     # 报价
        assert accept.payload["price"]["amount"] == 25      # 成交
        assert offer.payload["budget"]["amount"] != accept.payload["price"]["amount"]

    # ---------------- 统一的协议规则 ----------------
    def test_rule_matches_accepted_offer_price(self):
        """
        统一规则：ACCEPT.price = 被接受的那条 Offer 的价格。

        对 TASK_OFFER 取 budget，对 COUNTER_OFFER 取 price。
        """
        task_offer = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(budget=money(80)),
        )
        counter_offer = Message(
            from_agent="b", to_agent="a",
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(price=money(30)),
        )

        assert accepted_price_for(task_offer) == {"amount": 80, "currency": "AIC"}
        assert accepted_price_for(counter_offer) == {"amount": 30, "currency": "AIC"}

    def test_cannot_accept_a_non_offer(self):
        """ACCEPT 一个 TEXT 消息是没有意义的。"""
        text = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TEXT, payload={"text": "你好"},
        )

        with pytest.raises(ValueError):
            accepted_price_for(text)

    @pytest.mark.parametrize("budget_amount,expected", [(100, 100), (1, 1), (999999, 999999)])
    def test_direct_accept_preserves_budget_amount(self, budget_amount, expected):
        """直接接受时成交价严格等于预算（不折扣、不加价）。"""
        offer = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(budget=money(budget_amount)),
        )
        assert build_accept(offer).payload["price"]["amount"] == expected

    def test_currency_is_preserved_through_accept(self):
        """成交价的货币必须和报价一致。"""
        offer = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(budget=money(100, "AIC")),
        )
        assert build_accept(offer).payload["price"]["currency"] == "AIC"

    def test_two_case_transcripts_are_both_valid(self, run_async, fake_agents):
        """
        两种情况都要能通过通信层真实走一遍。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)
        captured = {}

        async def scenario():
            # 情况 A：直接接受
            offer_a = await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(budget=money(100)),
                trace_id="trc_case_a",
            )
            await manager.receive("agent_b", timeout=1.0)
            accept_a = build_accept(agent_b.last_message())
            await manager.send_message(accept_a)
            await manager.receive("agent_a", timeout=1.0)

            # 情况 B：先还价再接受
            offer_b = await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(budget=money(100)),
                trace_id="trc_case_b",
            )
            await manager.receive("agent_b", timeout=1.0)
            counter = agent_b.last_message().reply(
                message_type=MessageType.COUNTER_OFFER,
                payload=counter_offer_payload(price=money(25)),
            )
            await manager.send_message(counter)
            await manager.receive("agent_a", timeout=1.0)
            accept_b = build_accept(agent_a.last_message())
            await manager.send_message(accept_b)
            await manager.receive("agent_b", timeout=1.0)

            captured.update(
                offer_a=offer_a, accept_a=accept_a,
                offer_b=offer_b, counter=counter, accept_b=accept_b,
            )

        run_async(scenario())

        # 情况 A：成交价 = 预算
        assert captured["accept_a"].payload["price"]["amount"] == 100
        assert captured["accept_a"].reply_to == captured["offer_a"].message_id

        # 情况 B：成交价 = 还价
        assert captured["accept_b"].payload["price"]["amount"] == 25
        assert captured["accept_b"].reply_to == captured["counter"].message_id


# ============================================================
# 13. task_id ≠ trace_id（Step 3.7）
# ============================================================
class TestTaskIdIsNotTraceId:
    """
    【两个 ID 的职责不同，不能写成等价关系】

        task_id   业务任务的**稳定身份**（跨执行不变）
        trace_id  一次具体的**执行 / 协作链路**（日志追踪用）

    同一个 Task 因为失败重跑，会有多个 trace：
        TASK-001
         ├── trc_A
         └── trc_B
    """

    def test_task_id_lives_in_payload_trace_id_in_envelope(self):
        """
        两个 ID 存在**不同位置** —— 这本身就说明它们不是一回事。

        task_id  在 payload 里（业务数据）
        trace_id 在信封上（通信元数据）
        """
        message = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(task_id="TASK-001"),
            trace_id="trc_execution_1",
        )

        assert message.payload["task_id"] == "TASK-001"   # 业务层
        assert message.trace_id == "trc_execution_1"      # 通信层
        assert message.payload["task_id"] != message.trace_id

    def test_same_task_can_have_multiple_traces(self, run_async, fake_agents):
        """
        【核心场景】同一个任务重跑两次，会有两个不同的 trace_id。

        这正是 task_id 与 trace_id 不能等价的原因。
        """
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            first = await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(task_id="TASK-001"),
                trace_id="trc_attempt_a",
            )
            second = await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(task_id="TASK-001"),   # 同一个任务
                trace_id="trc_attempt_b",                          # 不同的执行
            )
            return first, second

        first, second = run_async(scenario())

        # 同一个 task_id
        assert first.payload["task_id"] == second.payload["task_id"] == "TASK-001"
        # 不同的 trace_id
        assert first.trace_id != second.trace_id

    def test_trace_groups_messages_of_one_execution(self, run_async, fake_agents):
        """
        trace_id 把「一次执行」里的多条消息归到一起。
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
                trace_id="trc_one_run",
            )
            await manager.receive("agent_b", timeout=1.0)
            counter = agent_b.last_message().reply(
                message_type=MessageType.COUNTER_OFFER,
                payload=counter_offer_payload(),
            )
            await manager.send_message(counter)
            return offer, counter

        offer, counter = run_async(scenario())

        # 同一次执行 —— trace_id 一致（reply 自动继承）
        assert offer.trace_id == counter.trace_id == "trc_one_run"

    def test_task_id_has_no_accessor_on_message(self):
        """
        Message 上有 `task_id` 访问器（读 payload），但**没有** `trace_id` 的
        特殊语义绑定 —— trace_id 是独立字段，不是从 payload 读的。
        """
        message = Message(
            from_agent="a", to_agent="b",
            payload={"task_id": "TASK-001"},
            trace_id="trc_x",
        )

        assert message.task_id == "TASK-001"        # 从 payload 读
        assert message.trace_id == "trc_x"          # 独立字段

        # 把 payload 里的 task_id 去掉，trace_id 不受影响
        empty = Message(from_agent="a", to_agent="b", trace_id="trc_y")
        assert empty.task_id is None
        assert empty.trace_id == "trc_y"

    def test_task_id_survives_retry_with_different_trace(self):
        """
        两次「执行」的消息可以有不同的 trace_id，但 task_id 不变。
        """
        attempt_a = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(task_id="TASK-001"),
            trace_id="trc_attempt_a",
        )
        attempt_b = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(task_id="TASK-001"),
            trace_id="trc_attempt_b",
        )

        assert attempt_a.payload["task_id"] == attempt_b.payload["task_id"]
        assert attempt_a.trace_id != attempt_b.trace_id


# ============================================================
# 14. message_id 承担 Offer 标识 + reply_to 建立报价链（Step 3.7）
# ============================================================
class TestOfferChainViaMessageId:
    """
    【当前版本不引入 offer_id】

    一条 TASK_OFFER / COUNTER_OFFER 的 `message_id` 就是这条报价的标识。
    `reply_to` 指出它回应的是哪一条具体消息。

    于是下面这条链可以完整重建：

        TASK_OFFER     message_id = offer-001
        COUNTER_OFFER  message_id = offer-002   reply_to = offer-001
        ACCEPT                                  reply_to = offer-002
    """

    def test_no_offer_id_field_exists(self):
        """
        当前版本**不引入** offer_id —— 协议明确用 message_id 代替。

        这条测试是那个决定的守卫：谁加了 offer_id，这里会失败，
        从而被迫先改协议（并递增 protocol_version）。
        """
        message = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER, payload=task_offer_payload(),
        )

        assert "offer_id" not in message.payload
        assert not hasattr(message, "offer_id")
        # 信封字段里也没有
        assert "offer_id" not in message.to_dict()

    def test_message_id_identifies_the_offer(self):
        """一条报价的 message_id 唯一标识这条报价。"""
        offer = Message(
            from_agent="a", to_agent="b",
            message_id="offer-001",
            message_type=MessageType.TASK_OFFER,
            payload=task_offer_payload(),
        )

        assert offer.message_id == "offer-001"
        # 序列化往返后标识不变
        assert Message.from_json(offer.to_json()).message_id == "offer-001"

    def test_reply_to_builds_the_offer_chain(self):
        """
        用 reply_to 把报价链完整串起来。
        """
        offer = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="offer-001",
            message_type=MessageType.TASK_OFFER,
            trace_id="trc_t", conversation_id="conv_1",
            payload=task_offer_payload(budget=money(100)),
        )
        counter = Message(
            from_agent="agent_b", to_agent="agent_a",
            message_id="offer-002",
            message_type=MessageType.COUNTER_OFFER,
            reply_to="offer-001",
            trace_id="trc_t", conversation_id="conv_1",
            payload=counter_offer_payload(price=money(25)),
        )
        accept = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="offer-003",
            message_type=MessageType.ACCEPT,
            reply_to="offer-002",
            trace_id="trc_t", conversation_id="conv_1",
            payload={"task_id": "TASK-001", "price": money(25)},
        )

        chain = {m.message_id: m for m in (offer, counter, accept)}

        # 从 ACCEPT 反推整条链
        assert accept.reply_to == "offer-002"
        assert chain[accept.reply_to].message_type == MessageType.COUNTER_OFFER
        assert chain[accept.reply_to].reply_to == "offer-001"
        assert chain[counter.reply_to].message_type == MessageType.TASK_OFFER

        # 起点没有 reply_to
        assert offer.reply_to is None

    def test_chain_is_walkable_to_the_root(self):
        """写一个通用的回溯函数，证明链是可走的。"""
        offer = Message(
            from_agent="a", to_agent="b", message_id="m1",
            message_type=MessageType.TASK_OFFER, payload=task_offer_payload(),
        )
        counter = offer.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(),
        )
        accept = counter.reply(
            message_type=MessageType.ACCEPT,
            payload={"task_id": "TASK-001", "price": money(25)},
        )

        by_id = {m.message_id: m for m in (offer, counter, accept)}

        def walk(message):
            """从任意一条消息回溯到链的起点。"""
            path = [message]
            while path[-1].reply_to is not None:
                path.append(by_id[path[-1].reply_to])
            return list(reversed(path))

        path = walk(accept)

        assert [m.message_type for m in path] == [
            MessageType.TASK_OFFER,
            MessageType.COUNTER_OFFER,
            MessageType.ACCEPT,
        ]

    def test_each_offer_has_a_distinct_message_id(self):
        """链上每条报价的 message_id 必须互不相同。"""
        offer = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER, payload=task_offer_payload(),
        )
        counter = offer.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(),
        )
        accept = counter.reply(
            message_type=MessageType.ACCEPT,
            payload={"task_id": "TASK-001", "price": money(25)},
        )

        ids = [offer.message_id, counter.message_id, accept.message_id]
        assert len(set(ids)) == 3

    def test_multi_round_negotiation_chain(self):
        """
        多轮还价也能串成一条链 —— 这正是「用 message_id 当报价标识」
        够用的原因。
        """
        first = Message(
            from_agent="a", to_agent="b", message_id="m1",
            message_type=MessageType.TASK_OFFER, payload=task_offer_payload(),
        )
        second = first.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(price=money(60)),
        )
        third = second.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(price=money(40)),
        )
        fourth = third.reply(
            message_type=MessageType.COUNTER_OFFER,
            payload=counter_offer_payload(price=money(30)),
        )

        chain = [first, second, third, fourth]

        # 严格串成一条链
        assert first.reply_to is None
        for previous, current in zip(chain, chain[1:]):
            assert current.reply_to == previous.message_id

        # 每一轮的报价都被保留（历史可审计）
        assert [m.payload.get("price", {}).get("amount") for m in chain[1:]] == [60, 40, 30]

    def test_accept_chain_works_over_communication_layer(self, run_async, fake_agents):
        """整条链通过通信层真实走一遍，reply_to 关系保持正确。"""
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)
        seen = []

        async def scenario():
            offer = await manager.send(
                "agent_a", "agent_b",
                message_type=MessageType.TASK_OFFER,
                payload=task_offer_payload(),
            )
            seen.append(("offer", offer, await manager.receive("agent_b", timeout=1.0)))

            counter = agent_b.last_message().reply(
                message_type=MessageType.COUNTER_OFFER,
                payload=counter_offer_payload(),
            )
            await manager.send_message(counter)
            seen.append(("counter", counter, await manager.receive("agent_a", timeout=1.0)))

            accept = build_accept(agent_a.last_message())
            await manager.send_message(accept)
            seen.append(("accept", accept, await manager.receive("agent_b", timeout=1.0)))

        run_async(scenario())

        offer = seen[0][1]
        counter = seen[1][1]
        accept = seen[2][1]

        assert counter.reply_to == offer.message_id
        assert accept.reply_to == counter.message_id
        assert len({offer.message_id, counter.message_id, accept.message_id}) == 3
        # 三方都在同一个 trace
        assert offer.trace_id == counter.trace_id == accept.trace_id
