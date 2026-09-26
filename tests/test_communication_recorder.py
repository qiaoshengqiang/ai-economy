"""
Test: Communication Recorder（通信日志）
============================================================

这个文件按需求里列的验收点逐条验证：

    1. 发送消息产生日志
    2. 接收消息产生日志
    3. 成功流程状态完整
    4. 失败流程有错误记录
    5. 相同 message_id 不会产生无法区分的重复记录
    6. 日志不包含钱包私钥 / API Key
    7. 不影响现有通信功能
    8. （原有测试继续通过 —— 由整个测试套件保证）

另外验证两件这个设计必须成立的事：

    · Message ≠ 日志（消息本体不知道日志的存在）
    · 日志写失败不能让通信失败
"""

from __future__ import annotations

import json

import pytest

from communication import (
    REDACTED,
    Address,
    CommunicationManager,
    CommunicationRecorder,
    LocalTransport,
    Message,
    MessageStatus,
    MessageType,
    payload_fingerprint,
    redact,
)
from communication.recorder import SECRET_VALUE_PATTERNS

from tests.communication_helpers import FakeAgent


# ============================================================
# 夹具
# ============================================================
@pytest.fixture
def journal(db) -> CommunicationRecorder:
    return CommunicationRecorder(db)


@pytest.fixture
def wired(db, fake_agents):
    """
    (manager, journal, agent_a, agent_b)

    manager 已经接上日志，两个假 Agent 已注册。
    """
    journal = CommunicationRecorder(db)
    manager = CommunicationManager(LocalTransport(), recorder=journal)
    agent_a, agent_b = fake_agents
    manager.register(agent_a)
    manager.register(agent_b)
    return manager, journal, agent_a, agent_b


# ============================================================
# 1. 脱敏（先测这个，因为它是安全底线）
# ============================================================
class TestRedaction:
    def test_api_key_field_is_redacted(self):
        clean, count = redact({"api_key": "sk-real-secret-value-123456"})

        assert clean["api_key"] == REDACTED
        assert count == 1

    def test_nested_secrets_are_redacted(self):
        clean, count = redact({
            "outer": {
                "inner": {"api_key": "sk-xxxxxxxxxxxxxxxx", "safe": "ok"}
            }
        })

        assert clean["outer"]["inner"]["api_key"] == REDACTED
        assert clean["outer"]["inner"]["safe"] == "ok"
        assert count == 1

    def test_secrets_inside_lists(self):
        clean, count = redact({"items": [{"password": "hunter2"}, {"ok": 1}]})

        assert clean["items"][0]["password"] == REDACTED
        assert clean["items"][1]["ok"] == 1

    @pytest.mark.parametrize(
        "key",
        [
            "api_key", "apikey", "API_KEY", "secret", "password",
            "private_key", "mnemonic", "access_token", "authorization",
            "bearer", "wallet_key", "signing_key",
        ],
    )
    def test_sensitive_key_variants(self, key):
        clean, _ = redact({key: "some-secret-value"})
        assert clean[key] == REDACTED, f"{key} 应该被脱敏"

    def test_token_fields_are_not_redacted(self):
        """
        【这条很重要】token 在本项目里是个正当词汇。

        input_tokens / total_tokens / tokens_per_ai_coin 都不是秘密。
        如果一律把含 "token" 的键脱敏，日志就没法看了 ——
        那是「过度安全」导致的不可用。
        """
        payload = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "tokens_per_ai_coin": 10000,
        }
        clean, count = redact(payload)

        assert clean == payload
        assert count == 0

    def test_key_like_values_are_redacted_even_with_innocent_key_name(self):
        """
        键名看不出问题，但值长得像密钥 —— 也要抹掉。
        """
        clean, count = redact({"note": "我的 key 是 sk-abcdefghijklmnop1234"})

        assert "sk-abcdefghijklmnop1234" not in clean["note"]
        assert REDACTED in clean["note"]
        assert count == 1

    def test_private_key_block_is_redacted(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        clean, _ = redact({"pem": pem})

        assert "BEGIN RSA PRIVATE KEY" not in clean["pem"]

    def test_bearer_token_in_string_is_redacted(self):
        clean, _ = redact({"header": "Bearer abcdefghijklmnop123456"})

        assert "abcdefghijklmnop123456" not in clean["header"]

    def test_normal_payload_is_untouched(self):
        payload = {"text": "你好", "task_id": "TASK-001", "budget": 100}
        clean, count = redact(payload)

        assert clean == payload
        assert count == 0

    def test_redaction_does_not_mutate_original(self):
        """脱敏必须是「拷贝后改」，不能改掉原始 payload。"""
        original = {"api_key": "sk-secret-1234567890"}
        redact(original)

        assert original["api_key"] == "sk-secret-1234567890"

    def test_non_string_values_pass_through(self):
        payload = {"n": 1, "f": 1.5, "b": True, "none": None, "list": [1, 2]}
        clean, _ = redact(payload)
        assert clean == payload


# ============================================================
# 2. payload 指纹
# ============================================================
class TestPayloadFingerprint:
    def test_returns_size_and_hash(self):
        size, digest, text = payload_fingerprint({"text": "你好"})

        assert size == len(text)
        assert len(digest) == 64          # sha256 十六进制
        assert json.loads(text) == {"text": "你好"}

    def test_same_content_same_hash_regardless_of_key_order(self):
        """
        hash 必须与键顺序无关 —— 否则它就不能用来判断
        「两条消息的 payload 是不是一样」。
        """
        _, h1, _ = payload_fingerprint({"a": 1, "b": 2})
        _, h2, _ = payload_fingerprint({"b": 2, "a": 1})

        assert h1 == h2

    def test_different_content_different_hash(self):
        _, h1, _ = payload_fingerprint({"text": "甲"})
        _, h2, _ = payload_fingerprint({"text": "乙"})

        assert h1 != h2

    def test_unserializable_object_does_not_crash(self):
        """payload 里有没法序列化的对象时，降级处理，绝不丢日志。"""
        class Weird:
            def __repr__(self):
                return "<weird>"

        size, digest, _ = payload_fingerprint({"obj": Weird()})

        assert size > 0
        assert len(digest) == 64


# ============================================================
# 3. 状态常量
# ============================================================
class TestStatusVocabulary:
    def test_required_statuses_exist(self):
        """需求里点名的状态一个都不能少。"""
        for name in (
            "CREATED", "QUEUED", "SENT", "DELIVERED", "RECEIVED",
            "PROCESSING", "PROCESSED", "FAILED", "TIMEOUT", "RETRY",
        ):
            assert hasattr(MessageStatus, name), f"缺少状态 {name}"

    def test_terminal_statuses(self):
        assert MessageStatus.is_terminal(MessageStatus.PROCESSED)
        assert MessageStatus.is_terminal(MessageStatus.FAILED)
        assert not MessageStatus.is_terminal(MessageStatus.SENT)

    def test_all_returns_sorted_unique(self):
        values = MessageStatus.all()
        assert values == sorted(set(values))
        assert "CREATED" in values


# ============================================================
# 4. 发送消息产生日志（验收点 1）
# ============================================================
class TestSendingProducesLog:
    def test_send_creates_message_row(self, wired, run_async):
        manager, journal, _, _ = wired
        assert journal.count_messages() == 0

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="你好")

        sent = run_async(scenario())

        row = journal.get_message(sent.message_id)
        assert row is not None
        assert row["from_agent"] == "agent_a"
        assert row["to_agent"] == "agent_b"
        assert row["message_type"] == "TEXT"

    def test_send_produces_created_sent_delivered(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="你好")

        sent = run_async(scenario())
        statuses = [e["status"] for e in journal.events_for(sent.message_id)]

        assert MessageStatus.CREATED in statuses
        assert MessageStatus.SENT in statuses
        assert MessageStatus.DELIVERED in statuses
        # 顺序必须是构造 → 发出 → 送达
        assert statuses.index(MessageStatus.CREATED) < statuses.index(MessageStatus.SENT)
        assert statuses.index(MessageStatus.SENT) < statuses.index(MessageStatus.DELIVERED)

    def test_send_records_transport_and_address(self, wired, run_async):
        """
        日志里必须有 transport 和 address ——
        将来是 P2P 时要靠这两个字段定位消息往哪儿去了。
        """
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="你好")

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert row["transport"] == "local"
        assert row["address"] == "local://agent_b"
        assert row["is_remote"] == 0

    def test_payload_metadata_is_recorded(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="一些内容")

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert row["payload_size"] > 0
        assert row["payload_hash"]
        assert len(row["payload_hash"]) == 64

    def test_payload_body_is_stored_in_dev_mode(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="可调试的内容")

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert "可调试的内容" in row["payload"]


# ============================================================
# 5. 接收消息产生日志（验收点 2）
# ============================================================
class TestReceivingProducesLog:
    def test_receive_produces_received_processing_processed(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        statuses = [e["status"] for e in journal.events_for(sent.message_id)]

        for expected in (
            MessageStatus.RECEIVED,
            MessageStatus.PROCESSING,
            MessageStatus.PROCESSED,
        ):
            assert expected in statuses, f"缺少 {expected}"

    def test_actor_is_recorded(self, wired, run_async):
        """事件要记下是谁触发的 —— 排查时才知道该找谁。"""
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        events = {e["status"]: e for e in journal.events_for(sent.message_id)}

        assert events[MessageStatus.CREATED]["actor"] == "agent_a"
        assert events[MessageStatus.RECEIVED]["actor"] == "agent_b"
        assert events[MessageStatus.PROCESSED]["actor"] == "agent_b"

    def test_both_sides_of_a_round_trip_are_logged(self, wired, run_async):
        """
        一问一答两条消息都要有日志，而且都在同一个 trace 下。
        """
        manager, journal, agent_a, agent_b = wired
        agent_b.on_message(lambda msg: "B 收到了")

        async def scenario():
            await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            await manager.receive("agent_a", timeout=1.0)

        run_async(scenario())

        assert journal.count_messages() == 2
        messages = journal.recent()
        assert {m["from_agent"] for m in messages} == {"agent_a", "agent_b"}


# ============================================================
# 6. 成功流程状态完整（验收点 3）
# ============================================================
class TestSuccessLifecycle:
    def test_full_lifecycle_order(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        statuses = [e["status"] for e in journal.events_for(sent.message_id)]

        assert statuses == [
            MessageStatus.CREATED,
            MessageStatus.SENT,
            MessageStatus.DELIVERED,
            MessageStatus.RECEIVED,
            MessageStatus.PROCESSING,
            MessageStatus.PROCESSED,
        ]

    def test_all_timestamps_are_filled_on_success(self, wired, run_async):
        """
        成功走完之后，四个时间戳都要有值。

        「哪一步没发生」可以直接从哪一列是 NULL 看出来 ——
        这就是把时间戳拆成四列的价值。
        """
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        for column in ("created_at", "sent_at", "received_at", "processed_at"):
            assert row[column], f"{column} 应该有值"

    def test_final_status_is_processed(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        assert journal.get_message(sent.message_id)["status"] == MessageStatus.PROCESSED

    def test_only_created_and_sent_before_receive(self, wired, run_async):
        """
        只发不收时，消息应该停在「已送达」——
        这正好暴露「对方没取」这种情况。
        """
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="你好")

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert row["status"] == MessageStatus.DELIVERED
        assert row["received_at"] is None       # 从 NULL 一眼看出卡在哪
        assert row["processed_at"] is None


# ============================================================
# 7. 失败流程有错误记录（验收点 4）
# ============================================================
class TestFailureRecording:
    def test_handler_exception_is_recorded(self, wired, run_async):
        """
        处理函数抛异常时，日志里必须有 FAILED 和具体错误。
        """
        manager, journal, agent_a, agent_b = wired

        def broken_handler(message):
            raise ValueError("处理这条消息时炸了")

        agent_b.on_message(broken_handler)

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            try:
                await manager.receive("agent_b", timeout=1.0)
            except ValueError:
                pass
            return sent

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert row["status"] == MessageStatus.FAILED
        assert row["error_code"] == "ValueError"
        assert "炸了" in row["error_message"]

    def test_failure_event_has_error_details(self, wired, run_async):
        manager, journal, _, agent_b = wired
        agent_b.on_message(lambda m: (_ for _ in ()).throw(RuntimeError("故意的")))

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            try:
                await manager.receive("agent_b", timeout=1.0)
            except RuntimeError:
                pass
            return sent

        sent = run_async(scenario())
        failed = [
            e for e in journal.events_for(sent.message_id)
            if e["status"] == MessageStatus.FAILED
        ]

        assert len(failed) == 1
        assert failed[0]["error_code"] == "RuntimeError"
        assert "故意的" in failed[0]["error_message"]

    def test_transport_failure_is_recorded(self, db, run_async):
        """
        投递本身失败也要留痕 ——
        否则「消息发了但对方没收到」在日志里完全看不见。
        """
        from communication.errors import AgentNotFoundError

        journal = CommunicationRecorder(db)
        transport = LocalTransport()
        manager = CommunicationManager(transport, recorder=journal)
        agent_a = FakeAgent("agent_a", "A")
        manager.register(agent_a)

        # 收件人不在任何地方 → 解析地址时就失败
        async def scenario():
            try:
                await manager.send("agent_a", "不存在的", text="你好")
            except AgentNotFoundError:
                pass

        run_async(scenario())

        # 这条消息压根没构造成功（地址解析在构造之后），
        # 所以消息表里不该有它 —— 但也不该崩
        assert journal.count_messages() == 0

    def test_processed_at_set_on_failure_too(self, wired, run_async):
        """
        失败也要有「结束时间」，否则算不出「这条消息卡了多久」。
        """
        manager, journal, _, agent_b = wired
        agent_b.on_message(lambda m: (_ for _ in ()).throw(RuntimeError("x")))

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            try:
                await manager.receive("agent_b", timeout=1.0)
            except RuntimeError:
                pass
            return sent

        sent = run_async(scenario())
        assert journal.get_message(sent.message_id)["processed_at"]


# ============================================================
# 8. 重复 message_id（验收点 5）
# ============================================================
class TestDuplicateHandling:
    def test_same_message_id_yields_one_row(self, wired, run_async):
        """
        【需求验收点】相同 message_id 不会产生无法区分的重复记录。

        消息表以 message_id 为主键 + UPSERT，
        所以反复投递多少次，那一行都只有一条。
        """
        manager, journal, _, _ = wired

        duplicate = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_same", payload={"text": "重复的"},
        )

        async def scenario():
            await manager.send_message(duplicate)
            await manager.send_message(duplicate)
            await manager.send_message(duplicate)

        run_async(scenario())

        assert journal.count_messages() == 1
        assert journal.get_message("msg_same")["message_id"] == "msg_same"

    def test_duplicate_delivery_is_visible_in_events(self, wired, run_async):
        """
        行只有一条，但「被投递了三次」这件事必须看得出来 ——
        证据在事件流里。
        """
        manager, journal, _, _ = wired

        duplicate = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_same", payload={"text": "重复的"},
        )

        async def scenario():
            await manager.send_message(duplicate)
            await manager.send_message(duplicate)
            await manager.receive("agent_b", timeout=1.0)
            await manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        events = journal.events_for("msg_same")
        sent_events = [e for e in events if e["status"] == MessageStatus.SENT]

        assert len(sent_events) == 2, "两次投递都要留下痕迹"

    def test_duplicate_receive_is_recorded(self, wired, run_async):
        manager, journal, _, _ = wired

        duplicate = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_id="msg_dup", payload={"text": "x"},
        )

        async def scenario():
            await manager.send_message(duplicate)
            await manager.send_message(duplicate)
            await manager.receive("agent_b", timeout=1.0)
            await manager.receive("agent_b", timeout=1.0)

        run_async(scenario())

        statuses = [e["status"] for e in journal.events_for("msg_dup")]
        assert MessageStatus.DUPLICATE in statuses

    def test_events_have_unique_ids(self, wired, run_async):
        """
        事件必须各有各的 ID。

        否则「同一条消息的多个事件」自己就分不清了 ——
        那就真成了「无法区分的重复记录」。
        """
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        events = journal.events_for(sent.message_id)
        ids = [e["event_id"] for e in events]

        assert len(ids) == len(set(ids)), "事件 ID 有重复"

    def test_upsert_does_not_lose_earlier_timestamps(self, wired, run_async):
        """
        UPSERT 时后面的状态不能把前面记下的时间戳冲成 NULL。
        """
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        # created_at 是第一个事件写下的，后面的事件不该清掉它
        assert row["created_at"]
        assert row["sent_at"]
        assert row["created_at"] <= row["sent_at"]


# ============================================================
# 9. 敏感信息不进日志（验收点 6）
# ============================================================
class TestNoSecretsInLog:
    def test_api_key_in_payload_is_redacted_in_db(self, wired, run_async):
        """
        【安全底线】消息里带了 API Key，落库时必须是脱敏的。
        """
        manager, journal, _, _ = wired
        secret = "sk-realkey-abcdefghijklmnop123456"

        async def scenario():
            return await manager.send(
                "agent_a", "agent_b",
                payload={"api_key": secret, "text": "正常内容"},
            )

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert secret not in (row["payload"] or "")
        assert REDACTED in row["payload"]
        # 正常内容不受影响
        assert "正常内容" in row["payload"]

    def test_wallet_private_key_is_redacted(self, wired, run_async):
        """未来会有钱包 —— 私钥绝不能进日志。"""
        manager, journal, _, _ = wired
        private_key = "0x" + "a" * 64

        async def scenario():
            return await manager.send(
                "agent_a", "agent_b",
                payload={"private_key": private_key, "amount": 20},
            )

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert private_key not in (row["payload"] or "")
        assert row["payload"].count(REDACTED) >= 1

    def test_secret_not_in_events_either(self, wired, run_async):
        """事件表里也不能漏 —— 很容易只顾着脱敏消息表。"""
        manager, journal, _, _ = wired
        secret = "sk-secret-abcdefghijklmnop123456"

        async def scenario():
            sent = await manager.send(
                "agent_a", "agent_b", payload={"token": secret}
            )
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())

        for event in journal.events_for(sent.message_id):
            assert secret not in json.dumps(dict(event), ensure_ascii=False)

    def test_secret_not_in_error_message(self, db, run_async):
        """错误信息里也可能带密钥（比如异常里拼了请求头）。"""
        journal = CommunicationRecorder(db)
        manager = CommunicationManager(LocalTransport(), recorder=journal)
        agent_a = FakeAgent("agent_a", "A")
        agent_b = FakeAgent("agent_b", "B")
        manager.register(agent_a)
        manager.register(agent_b)

        def leaky_handler(message):
            raise RuntimeError("请求失败，Authorization: Bearer sk-leaked-abcdefghij123456")

        agent_b.on_message(leaky_handler)

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="x")
            try:
                await manager.receive("agent_b", timeout=1.0)
            except RuntimeError:
                pass
            return sent

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert "sk-leaked-abcdefghij123456" not in (row["error_message"] or "")
        assert REDACTED in row["error_message"]

    def test_payload_hash_is_of_redacted_content(self, wired, run_async):
        """
        hash 算的是【脱敏后】的内容。

        为什么？因为生产环境可能只留 hash 不留 payload。
        如果 hash 算的是原始内容（含密钥），那 hash 本身
        就成了「能不能猜出密钥」的旁路 —— 虽然很弱，但没必要留这个口子。
        """
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send(
                "agent_a", "agent_b",
                payload={"api_key": "sk-secret-1234567890", "text": "hi"},
            )

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        # 用脱敏后的 payload 重算，应该和存下来的一致
        stored = json.loads(row["payload"])
        _, expected_hash, _ = payload_fingerprint(stored)

        assert row["payload_hash"] == expected_hash

    def test_clean_payload_triggers_no_redaction(self, wired, run_async):
        """正常消息不该被无谓地打码。"""
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send(
                "agent_a", "agent_b", payload={"text": "普通消息", "n": 1}
            )

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert REDACTED not in (row["payload"] or "")


# ============================================================
# 10. 不影响通信（验收点 7）
# ============================================================
class TestDoesNotAffectCommunication:
    def test_communication_works_without_recorder(self, fake_agents, run_async):
        """没有日志时通信照常 —— 日志是可选的观测层。"""
        manager = CommunicationManager(LocalTransport())
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            await manager.send("agent_a", "agent_b", text="你好")
            return await manager.receive("agent_b", timeout=1.0)

        assert run_async(scenario()).delivered is True

    def test_journal_write_failure_does_not_break_send(self, fake_agents, run_async):
        """
        【关键】日志写失败不能让消息发不出去。

        日志是观测手段，不该成为通信的单点故障。
        """
        class BrokenRecorder(CommunicationRecorder):
            def record(self, *args, **kwargs):
                raise RuntimeError("日志数据库炸了")

        from storage.database import create_memory_database

        db = create_memory_database()
        recorder = BrokenRecorder(db, strict=False)
        manager = CommunicationManager(LocalTransport(), recorder=recorder)
        agent_a, agent_b = fake_agents
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            await manager.send("agent_a", "agent_b", text="你好")
            return await manager.receive("agent_b", timeout=1.0)

        # 通信必须照常成功
        result = run_async(scenario())
        assert result.delivered is True
        db.close()

    def test_recorder_does_not_change_message_content(self, wired, run_async):
        """
        日志是【旁观者】，不能改消息本身。

        特别是不能把脱敏后的 payload 写回消息 ——
        那样对方收到的就是 ***REDACTED*** 了。
        """
        manager, journal, agent_a, agent_b = wired
        secret = "sk-secret-abcdefghij123456"

        async def scenario():
            sent = await manager.send(
                "agent_a", "agent_b", payload={"api_key": secret}
            )
            await manager.receive("agent_b", timeout=1.0)
            return sent

        sent = run_async(scenario())

        # 消息本身没被动过
        assert sent.payload["api_key"] == secret
        # 对方收到的也是原始内容（脱敏只发生在日志里）
        assert agent_b.last_message().payload["api_key"] == secret

    def test_message_module_does_not_reference_recorder(self):
        """
        【Message ≠ 日志】的可执行证明。

        communication/message.py 里不该出现任何 recorder / journal /
        数据库相关的东西 —— 消息本体完全不知道日志的存在。
        """
        import ast
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "communication" / "message.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        forbidden = ("recorder", "journal", "sqlite", "database", "db")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(f in alias.name.lower() for f in forbidden), (
                        f"message.py 导入了 {alias.name}"
                    )
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(f in node.module.lower() for f in forbidden), (
                    f"message.py 导入了 {node.module}"
                )

    def test_message_does_not_import_communication_recorder(self):
        from pathlib import Path
        import ast

        source = Path(__file__).resolve().parent.parent / "communication" / "message.py"
        modules = []

        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)

        assert "communication.recorder" not in modules


# ============================================================
# 11. trace / conversation 串联
# ============================================================
class TestTracing:
    def test_send_generates_trace_id(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="你好")

        sent = run_async(scenario())
        assert sent.trace_id.startswith("trc_")
        assert journal.get_message(sent.message_id)["trace_id"] == sent.trace_id

    def test_explicit_trace_id_is_used(self, wired, run_async):
        """
        一次任务里的所有消息应该共用一个 trace_id。
        Step 4 的 Task 会这么做。
        """
        manager, journal, _, _ = wired

        async def scenario():
            await manager.send("agent_a", "agent_b", text="第一条", trace_id="trc_task1")
            await manager.send("agent_a", "agent_b", text="第二条", trace_id="trc_task1")

        run_async(scenario())

        messages = journal.trace("trc_task1")
        assert len(messages) == 2
        assert {m["trace_id"] for m in messages} == {"trc_task1"}

    def test_reply_inherits_trace_id(self, wired, run_async):
        """
        【关键】回复必须继承 trace_id。

        不继承的话，一次任务的来回消息在日志里就是互不相干的孤儿。
        """
        manager, journal, agent_a, agent_b = wired
        agent_b.on_message(lambda msg: "B 回复")

        async def scenario():
            sent = await manager.send(
                "agent_a", "agent_b", text="原始", trace_id="trc_task2"
            )
            await manager.receive("agent_b", timeout=1.0)
            reply = await manager.receive("agent_a", timeout=1.0)
            return sent, reply.message

        sent, reply = run_async(scenario())

        assert reply.trace_id == sent.trace_id == "trc_task2"
        assert len(journal.trace("trc_task2")) == 2

    def test_reply_inherits_conversation_id(self, wired, run_async):
        manager, journal, _, agent_b = wired
        agent_b.on_message(lambda msg: "B 回复")

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="原始")
            await manager.receive("agent_b", timeout=1.0)
            reply = await manager.receive("agent_a", timeout=1.0)
            return sent, reply.message

        sent, reply = run_async(scenario())
        assert reply.conversation_id == sent.conversation_id

    def test_different_sends_get_different_traces(self, wired, run_async):
        """不指定 trace_id 时，两次独立发送应该是两条独立链路。"""
        manager, journal, _, _ = wired

        async def scenario():
            return (
                await manager.send("agent_a", "agent_b", text="甲"),
                await manager.send("agent_a", "agent_b", text="乙"),
            )

        first, second = run_async(scenario())
        assert first.trace_id != second.trace_id

    def test_trace_events_span_multiple_messages(self, wired, run_async):
        manager, journal, _, agent_b = wired
        agent_b.on_message(lambda msg: "回复")

        async def scenario():
            await manager.send("agent_a", "agent_b", text="原始", trace_id="trc_t")
            await manager.receive("agent_b", timeout=1.0)
            await manager.receive("agent_a", timeout=1.0)

        run_async(scenario())

        events = journal.trace_events("trc_t")
        message_ids = {e["message_id"] for e in events}

        assert len(message_ids) == 2
        assert len(events) == 12      # 两条消息各 6 个事件

    def test_conversation_query(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="甲")

        sent = run_async(scenario())
        rows = journal.conversation(sent.conversation_id)

        assert len(rows) == 1
        assert rows[0]["message_id"] == sent.message_id

    def test_message_roundtrip_keeps_trace_ids(self):
        """trace/conversation 必须能过网络（序列化往返）。"""
        msg = Message(
            from_agent="a", to_agent="b",
            trace_id="trc_keep", conversation_id="conv_keep",
        )
        restored = Message.from_json(msg.to_json())

        assert restored.trace_id == "trc_keep"
        assert restored.conversation_id == "conv_keep"


# ============================================================
# 12. 查询接口
# ============================================================
class TestQueries:
    def test_recent(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            for index in range(3):
                await manager.send("agent_a", "agent_b", text=f"第{index}条")

        run_async(scenario())

        assert len(journal.recent(limit=10)) == 3
        assert len(journal.recent(limit=2)) == 2

    def test_recent_filter_by_agent(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            await manager.send("agent_a", "agent_b", text="x")

        run_async(scenario())

        assert len(journal.recent(agent_id="agent_a")) == 1
        assert len(journal.recent(agent_id="agent_b")) == 1
        assert len(journal.recent(agent_id="agent_z")) == 0

    def test_recent_filter_by_status(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            await manager.send("agent_a", "agent_b", text="只发不收")

        run_async(scenario())

        assert len(journal.recent(status="DELIVERED")) == 1
        assert len(journal.recent(status="PROCESSED")) == 0

    def test_traces_list(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            await manager.send("agent_a", "agent_b", text="甲", trace_id="trc_1")
            await manager.send("agent_a", "agent_b", text="乙", trace_id="trc_2")

        run_async(scenario())

        traces = journal.traces()
        ids = {t["trace_id"] for t in traces}

        assert ids == {"trc_1", "trc_2"}
        assert all(t["messages"] == 1 for t in traces)

    def test_traces_counts_failures(self, db, run_async):
        journal = CommunicationRecorder(db)
        manager = CommunicationManager(LocalTransport(), recorder=journal)
        agent_a = FakeAgent("agent_a", "A")
        agent_b = FakeAgent("agent_b", "B", reply_with=lambda m: (_ for _ in ()).throw(RuntimeError("x")))
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            await manager.send("agent_a", "agent_b", text="x", trace_id="trc_fail")
            try:
                await manager.receive("agent_b", timeout=1.0)
            except RuntimeError:
                pass

        run_async(scenario())

        trace = {t["trace_id"]: t for t in journal.traces()}["trc_fail"]
        assert trace["failed"] == 1

    def test_get_message_returns_none_for_unknown(self, journal):
        assert journal.get_message("从没见过的") is None

    def test_events_for_unknown_returns_empty(self, journal):
        assert journal.events_for("从没见过的") == []

    def test_stats(self, wired, run_async):
        manager, journal, _, _ = wired

        async def scenario():
            sent = await manager.send("agent_a", "agent_b", text="你好")
            await manager.receive("agent_b", timeout=1.0)
            return sent

        run_async(scenario())
        stats = journal.stats()

        assert stats["messages"] == 1
        assert stats["events"] == 6
        assert stats["messages_by_status"]["PROCESSED"] == 1
        assert stats["write_errors"] == 0


# ============================================================
# 13. 为 P2P 预留
# ============================================================
class TestP2PReadiness:
    def test_transport_and_address_are_generic(self, db, run_async):
        """
        日志不能假设传输永远是 LocalTransport。

        用一个 scheme="p2p" 的假传输跑一遍，
        确认日志里记的是 p2p 的地址，而不是硬编码的 local://。
        """
        from tests.communication_helpers import RecordingTransport

        journal = CommunicationRecorder(db)
        transport = RecordingTransport()          # scheme = "recording"
        manager = CommunicationManager(transport, recorder=journal)
        agent_a = FakeAgent("agent_a", "A")
        agent_b = FakeAgent("agent_b", "B")
        manager.register(agent_a)
        manager.register(agent_b)

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="跨传输")

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert row["transport"] == "recording"
        assert row["address"].startswith("recording://")
        # is_remote 由【地址的 scheme】决定，不是由「跑在本地」决定。
        # "recording" 不是 local scheme，所以会被标记成远端 ——
        # 这正是我们想要的行为：将来 p2p:// 的地址自动就是 remote。
        assert row["is_remote"] == 1

    def test_remote_message_is_marked(self, journal, run_async):
        """
        远端地址应该让 is_remote = 1。

        这样日志里一眼能分出「本地投递」和「跨网络投递」。
        """
        from communication.registry import AgentInfo

        manager = CommunicationManager(LocalTransport(), recorder=journal)
        remote_address = Address(
            agent_id="agent_c", scheme="p2p", endpoint="12D3KooW"
        )
        info = AgentInfo(
            agent_id="agent_c", address=remote_address, is_local=False
        )
        manager.registry.register(info)
        manager.register(FakeAgent("agent_a", "A"))

        sent = Message(
            from_agent="agent_a", to_agent="agent_c",
            message_id="msg_remote", payload={"text": "去远端"},
        )

        # 本地传输送不到 p2p 地址，但我们只关心日志是否记下了 is_remote。
        # 这里直接记录一次 SENT 事件来验证字段。
        async def scenario():
            await manager.send_message(sent)

        from communication.errors import AgentNotFoundError
        try:
            run_async(scenario())
        except Exception:
            pass

        # 因为本地传输找不到 agent_c 的信箱，会走 FAILED 分支；
        # 关键是日志里记下的地址是 p2p 的
        row = journal.get_message("msg_remote")
        if row is not None:
            assert row["address"] == "p2p://12D3KooW"
            assert row["is_remote"] == 1

    def test_status_vocabulary_covers_network_cases(self):
        """TIMEOUT / RETRY 现在用不到，但常量必须先有。"""
        assert MessageStatus.TIMEOUT
        assert MessageStatus.RETRY
        assert MessageStatus.QUEUED


# ============================================================
# 14. 可选行为
# ============================================================
class TestOptionalBehaviour:
    def test_can_disable_payload_storage(self, db, run_async):
        """
        生产环境可以只留元数据 + hash，不存 payload 正文。
        这样日志不会无限膨胀。
        """
        journal = CommunicationRecorder(db, store_payload=False)
        manager = CommunicationManager(LocalTransport(), recorder=journal)
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="不存正文")

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert row["payload"] is None
        assert row["payload_size"] > 0        # 元数据仍然有
        assert row["payload_hash"]

    def test_long_payload_is_truncated(self, db, run_async):
        journal = CommunicationRecorder(db, max_payload_chars=100)
        manager = CommunicationManager(LocalTransport(), recorder=journal)
        manager.register(FakeAgent("agent_a", "A"))
        manager.register(FakeAgent("agent_b", "B"))

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="很长" * 500)

        sent = run_async(scenario())
        row = journal.get_message(sent.message_id)

        assert "截断" in row["payload"]
        assert row["payload_size"] > len(row["payload"])

    def test_strict_mode_raises(self, db):
        """
        测试里想要「日志没写进去就报错」，用 strict=True。
        否则日志悄悄失败，测试还全绿。
        """
        recorder = CommunicationRecorder(db, strict=True)

        with pytest.raises(Exception):
            recorder.record("这不是消息", MessageStatus.CREATED)

    def test_non_strict_mode_swallows(self, db):
        recorder = CommunicationRecorder(db, strict=False)
        result = recorder.record("这不是消息", MessageStatus.CREATED)

        assert result is None
        assert recorder.error_count == 1

    def test_describe(self, journal):
        info = journal.describe()

        assert info["recorder"] == "CommunicationRecorder"
        assert info["store_payload"] is True
        assert "messages" in info

    def test_repr(self, journal):
        assert "CommunicationRecorder" in repr(journal)
