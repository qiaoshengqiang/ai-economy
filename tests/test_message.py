"""
Test: Message（消息数据结构）
============================================================

消息是整条通信链路的物理载体。
它必须能变成字节、穿过网络、再变回来 —— 所以这个文件
重点测的就是「序列化往返无损」。

另外还测两件容易被忽略、但未来会救命的事：
    · 前向兼容（未知字段不丢）
    · 字段校验（不合法就早报错）
"""

from __future__ import annotations

import json

import pytest

from communication.errors import MessageValidationError
from communication.message import PROTOCOL_VERSION, Message, MessageType


# ============================================================
# 1. 创建与默认值
# ============================================================
class TestCreation:
    def test_minimal_message(self):
        msg = Message(from_agent="agent_a", to_agent="agent_b")

        assert msg.from_agent == "agent_a"
        assert msg.to_agent == "agent_b"
        assert msg.message_type == MessageType.TEXT
        assert msg.payload == {}

    def test_message_id_is_auto_generated(self):
        msg = Message(from_agent="agent_a", to_agent="agent_b")

        assert msg.message_id
        assert msg.message_id.startswith("msg_")

    def test_two_messages_get_different_ids(self):
        a = Message(from_agent="a", to_agent="b")
        b = Message(from_agent="a", to_agent="b")

        assert a.message_id != b.message_id

    def test_timestamp_is_auto_filled(self):
        msg = Message(from_agent="a", to_agent="b")
        assert msg.timestamp

    def test_explicit_id_is_kept(self):
        """测试和重放场景需要指定 ID。"""
        msg = Message(
            from_agent="a", to_agent="b", message_id="msg_fixed_001"
        )
        assert msg.message_id == "msg_fixed_001"

    def test_protocol_version_default(self):
        msg = Message(from_agent="a", to_agent="b")
        assert msg.protocol_version == PROTOCOL_VERSION

    # ------------------------------------------------------------
    def test_text_helper(self):
        msg = Message(from_agent="a", to_agent="b", payload={"text": "你好"})
        assert msg.text == "你好"

    def test_text_helper_on_empty_payload(self):
        msg = Message(from_agent="a", to_agent="b")
        assert msg.text == ""

    def test_message_type_is_uppercased(self):
        msg = Message(from_agent="a", to_agent="b", message_type="text")
        assert msg.message_type == "TEXT"


# ============================================================
# 2. 字段校验
# ============================================================
class TestValidation:
    def test_missing_sender_rejected(self):
        with pytest.raises(MessageValidationError, match="from_agent"):
            Message(from_agent="", to_agent="b")

    def test_missing_recipient_rejected(self):
        with pytest.raises(MessageValidationError, match="to_agent"):
            Message(from_agent="a", to_agent="")

    def test_missing_type_rejected(self):
        with pytest.raises(MessageValidationError, match="message_type"):
            Message(from_agent="a", to_agent="b", message_type="")

    def test_missing_id_rejected(self):
        with pytest.raises(MessageValidationError, match="message_id"):
            Message(from_agent="a", to_agent="b", message_id="")

    def test_self_message_rejected(self):
        """
        自己给自己发消息。

        这条规则现在看似多余，但它防的是未来一个很实际的坑：
        Agent 之间的自动回复一旦形成自环，就会无限循环，
        而每一轮循环都要花钱调模型。
        """
        with pytest.raises(MessageValidationError, match="给自己"):
            Message(from_agent="agent_a", to_agent="agent_a")

    def test_payload_must_be_mapping(self):
        with pytest.raises(MessageValidationError, match="payload"):
            Message(from_agent="a", to_agent="b", payload=["不是字典"])

    def test_none_payload_becomes_empty_dict(self):
        msg = Message(from_agent="a", to_agent="b", payload=None)
        assert msg.payload == {}

    def test_extensions_must_be_mapping(self):
        with pytest.raises(MessageValidationError, match="extensions"):
            Message(from_agent="a", to_agent="b", extensions="不是字典")

    def test_payload_is_copied(self):
        """外部改原始字典，不该影响已经创建的消息。"""
        original = {"text": "原始"}
        msg = Message(from_agent="a", to_agent="b", payload=original)

        original["text"] = "被改了"

        assert msg.text == "原始"

    def test_from_agent_is_stripped(self):
        msg = Message(from_agent="  agent_a  ", to_agent="agent_b")
        assert msg.from_agent == "agent_a"


# ============================================================
# 3. 消息类型可扩展
# ============================================================
class TestMessageTypes:
    """
    类型必须能扩展 —— 否则以后每加一种消息都要改这个文件。
    """

    def test_known_types_exist(self):
        assert MessageType.TEXT
        assert MessageType.PING
        assert MessageType.PONG

    def test_future_types_are_declared(self):
        """接下来几步要用的类型已经留好名字了。"""
        for name in (
            "TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT",
            "SUBMIT", "VERIFY", "PAYMENT_REQUEST", "PAYMENT_RESULT",
        ):
            assert hasattr(MessageType, name)

    def test_custom_type_is_allowed(self):
        """未知类型不应该被拒绝 —— 否则就是变相的白名单。"""
        msg = Message(
            from_agent="a", to_agent="b", message_type="MY_FUTURE_TYPE"
        )
        assert msg.message_type == "MY_FUTURE_TYPE"

    def test_is_of_type(self):
        msg = Message(from_agent="a", to_agent="b", message_type="PING")

        assert msg.is_of_type("PING") is True
        assert msg.is_of_type("ping") is True     # 大小写不敏感
        assert msg.is_of_type("PONG") is False


# ============================================================
# 4. 序列化（本文件最重要的一组）
# ============================================================
class TestSerialization:
    def test_to_dict_has_all_core_fields(self):
        msg = Message(
            from_agent="agent_a",
            to_agent="agent_b",
            message_type=MessageType.TEXT,
            payload={"text": "你好"},
        )
        data = msg.to_dict()

        for field in (
            "message_id", "from_agent", "to_agent",
            "message_type", "timestamp", "payload", "protocol_version",
        ):
            assert field in data, f"to_dict 缺少 {field}"

    def test_dict_roundtrip_is_lossless(self):
        msg = Message(
            from_agent="agent_a",
            to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload={"task_id": "TASK-001", "workspace_id": "WS-001"},
        )

        restored = Message.from_dict(msg.to_dict())

        assert restored.to_dict() == msg.to_dict()
        assert restored.message_id == msg.message_id
        assert restored.payload == msg.payload

    def test_json_roundtrip_is_lossless(self):
        msg = Message(
            from_agent="agent_a",
            to_agent="agent_b",
            payload={"text": "中文和 emoji 🎉"},
        )

        text = msg.to_json()
        restored = Message.from_json(text)

        assert restored.to_dict() == msg.to_dict()

    def test_json_is_valid_json(self):
        msg = Message(from_agent="a", to_agent="b", payload={"text": "x"})
        parsed = json.loads(msg.to_json())

        assert parsed["from_agent"] == "a"

    def test_json_keeps_chinese_readable(self):
        """
        ensure_ascii=False：中文在 JSON 里直接是中文，不是 \\u4f60\\u597d。

        为什么在意这个？
            日志里能直接读懂，排查通信问题时不用先去解码。
        """
        msg = Message(from_agent="a", to_agent="b", payload={"text": "你好"})
        assert "你好" in msg.to_json()

    def test_reply_to_survives_roundtrip(self):
        msg = Message(from_agent="a", to_agent="b", reply_to="msg_original")

        restored = Message.from_json(msg.to_json())
        assert restored.reply_to == "msg_original"

    def test_reply_to_absent_when_none(self):
        """没有 reply_to 时不该在字典里留一个 None 键。"""
        msg = Message(from_agent="a", to_agent="b")
        assert "reply_to" not in msg.to_dict()

    # ------------------------------------------------------------
    def test_unknown_fields_are_preserved(self):
        """
        【前向兼容】未来版本加的字段不能在这版被丢掉。

        否则旧节点转发新格式消息时，签名之类的信息就没了。
        """
        raw = {
            "message_id": "msg_1",
            "from_agent": "a",
            "to_agent": "b",
            "message_type": "TEXT",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "payload": {"text": "hi"},
            "protocol_version": "2",
            "signature": "future-signature-value",
            "nonce": "abc123",
            "ttl": 30,
        }

        msg = Message.from_dict(raw)

        assert msg.extensions["signature"] == "future-signature-value"
        assert msg.extensions["nonce"] == "abc123"
        assert msg.extensions["ttl"] == 30

        # 再次序列化时，它们应该还在
        forwarded = msg.to_dict()
        assert forwarded["signature"] == "future-signature-value"
        assert forwarded["ttl"] == 30

    def test_protocol_version_from_newer_peer_is_kept(self):
        raw = {
            "from_agent": "a", "to_agent": "b", "payload": {},
            "protocol_version": "99",
        }
        assert Message.from_dict(raw).protocol_version == "99"

    # ------------------------------------------------------------
    def test_from_dict_missing_id_generates_one(self):
        """对方没给 ID 时兜一个，不能让消息变成无法去重的。"""
        raw = {"from_agent": "a", "to_agent": "b", "payload": {}}
        msg = Message.from_dict(raw)
        assert msg.message_id.startswith("msg_")

    def test_from_dict_missing_timestamp_generates_one(self):
        raw = {"from_agent": "a", "to_agent": "b", "payload": {}}
        assert Message.from_dict(raw).timestamp

    def test_from_dict_rejects_non_mapping(self):
        with pytest.raises(MessageValidationError, match="字典"):
            Message.from_dict(["不是字典"])

    def test_from_json_rejects_bad_json(self):
        with pytest.raises(MessageValidationError, match="JSON"):
            Message.from_json("这不是 JSON {{{")

    def test_from_json_rejects_non_object(self):
        with pytest.raises(MessageValidationError, match="对象"):
            Message.from_json("[1, 2, 3]")

    def test_from_dict_still_validates(self):
        """反序列化也要校验，不能绕过规则。"""
        with pytest.raises(MessageValidationError):
            Message.from_dict({"from_agent": "", "to_agent": "b"})


# ============================================================
# 5. 回复
# ============================================================
class TestReply:
    def test_reply_swaps_sender_and_recipient(self):
        original = Message(from_agent="agent_a", to_agent="agent_b")
        reply = original.reply(text="收到")

        assert reply.from_agent == "agent_b"
        assert reply.to_agent == "agent_a"

    def test_reply_records_reply_to(self):
        original = Message(from_agent="a", to_agent="b")
        reply = original.reply(text="收到")

        assert reply.reply_to == original.message_id

    def test_reply_gets_its_own_id(self):
        original = Message(from_agent="a", to_agent="b")
        reply = original.reply(text="收到")

        assert reply.message_id != original.message_id

    def test_reply_with_payload(self):
        original = Message(from_agent="a", to_agent="b")
        reply = original.reply(payload={"status": "ok", "count": 3})

        assert reply.payload["status"] == "ok"

    def test_reply_with_custom_type(self):
        original = Message(from_agent="a", to_agent="b", message_type="PING")
        reply = original.reply(message_type="PONG")

        assert reply.message_type == "PONG"

    def test_reply_is_a_real_message(self):
        """回复也必须能被序列化 —— 它可能要过网络。"""
        original = Message(from_agent="a", to_agent="b")
        reply = original.reply(text="收到")

        assert Message.from_json(reply.to_json()).payload["text"] == "收到"


# ============================================================
# 6. 未来的 task / workspace 接口
# ============================================================
class TestTaskWorkspaceHooks:
    """
    本步骤不实现任务和权限，但消息结构必须现在就容得下它们。

    这样 Step 4/5 加功能时是「往 payload 里塞东西」，
    而不是「改 Message 的字段」—— 后者会破坏兼容性。
    """

    def test_task_id_accessor(self):
        msg = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload={"task_id": "TASK-001"},
        )
        assert msg.task_id == "TASK-001"

    def test_workspace_id_accessor(self):
        msg = Message(
            from_agent="a", to_agent="b",
            message_type=MessageType.TASK_OFFER,
            payload={"workspace_id": "WS-001"},
        )
        assert msg.workspace_id == "WS-001"

    def test_both_absent_returns_none(self):
        msg = Message(from_agent="a", to_agent="b")
        assert msg.task_id is None
        assert msg.workspace_id is None

    def test_full_task_offer_roundtrips(self):
        """一条完整的任务邀约，序列化后信息不丢。"""
        offer = Message(
            from_agent="agent_a",
            to_agent="agent_b",
            message_type=MessageType.TASK_OFFER,
            payload={
                "task_id": "TASK-001",
                "workspace_id": "WS-001",
                "description": "写一个 CSV 分析程序",
                "budget": 100,
            },
        )

        restored = Message.from_json(offer.to_json())

        assert restored.task_id == "TASK-001"
        assert restored.workspace_id == "WS-001"
        assert restored.payload["budget"] == 100


# ============================================================
# 7. 展示
# ============================================================
class TestDisplay:
    def test_summary_has_key_fields(self):
        msg = Message(from_agent="a", to_agent="b", payload={"text": "hi"})
        summary = msg.summary()

        assert summary["from"] == "a"
        assert summary["to"] == "b"
        assert summary["type"] == "TEXT"
        assert summary["message_id"] == msg.message_id

    def test_str_is_readable(self):
        msg = Message(from_agent="a", to_agent="b", payload={"text": "hi"})
        text = str(msg)

        assert "TEXT" in text
        assert "a → b" in text

    def test_repr_contains_id(self):
        msg = Message(from_agent="a", to_agent="b")
        assert msg.message_id in repr(msg)
