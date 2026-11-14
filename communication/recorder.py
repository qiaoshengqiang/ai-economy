"""
Communication Recorder —— 通信日志
============================================================

【这个文件解决什么问题】

    Step 3 让两个 Agent 能说话了。但一旦它们开始真干活，
    出问题时就只剩一句「B 没收到」。

    你需要能回答这些问题：

        · 这条消息到底发出去没有？
        · 发了但对方没取？还是取了但处理失败了？
        · 这次任务一共来回了几条消息？
        · 哪一步最慢？
        · 有没有重复投递？
        · 失败的原因是什么？

    没有日志，这些全都只能靠猜。有了日志，就是一条查询的事。

【最重要的一条：Message ≠ 日志】

    ❌ 不能让 Message 自带日志功能
         那样消息体里会塞满「什么时候被谁记了一笔」这种观测数据，
         而消息是要发给别人的 —— 等于把本地日志通过网络发给对方。

    ❌ 不能在通信路径上顺手 print
         那样跨机器时什么都拿不到，而且没法查询。

    ✅ 而是：

         Message → Transport → Communication Recorder → SQLite

         消息本体（communication/message.py 的 Message）
         和观测记录（这个文件 + 两张表）完全分开。

         删掉整张日志表，通信照常工作。
         删掉 Message，日志也没有意义。
         两者互不依赖，这就是解耦。

【为什么不做成消息队列】

    这里【不】实现：
        · 投递保证 / 重试队列 / ACK 机制
        · 死信队列 / 消费位点
        · 分布式追踪后端

    只做到一件事：把通信过程中发生的事，可靠地记下来。
    记下来之后怎么用，是查询的事，不是记录的事。

【P2P 扩展性】

    日志里存的是 {transport, address}，而不是「本地」这种假设。

        本地:  transport="local", address="local://agent_b"
        P2P:   transport="p2p",   address="p2p://12D3KooW..."

    而且 is_remote 字段现在就存在（本地消息是 0）。
    将来换成 P2PTransport 时，日志表结构一行都不用改。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core.identity import new_message_id, utc_now_iso
from core.logging_setup import get_logger
from communication.address import Address
from datetime import timedelta

from communication.dedup import ClaimStatus, MessageClaim
from communication.message import Message
from storage.database import Database
from storage.models import (
    TABLE_COMM_EVENTS,
    TABLE_COMM_INBOX_STATE,
    TABLE_COMM_MESSAGES,
)

logger = get_logger(__name__)


# ============================================================
# 状态
# ============================================================
#: 认领租约的默认时长（秒）。
#:
#: 【它是什么】
#:     一个 worker 认领消息之后，这个认领**有效多久**。
#:     超过之后别的 worker 可以接手 —— 这解决「worker 崩了、
#:     认领永远卡住」的问题。
#:
#: 【它不是什么】
#:     ❌ 不是「处理超时就会失败」
#:     ❌ 不是「超时就把消息删掉」
#:     ❌ 不是「超时就算处理失败」
#:     ❌ 不是 retry 调度
#:     它只决定「这个临时占用还算不算数」。
#:
#: 【为什么默认 5 分钟】
#:     太短 → 正常处理中的消息被别的 worker 抢走 → 重复处理
#:     太长 → worker 崩溃后要等很久才恢复
#:     5 分钟对本地消息处理是很宽松的上限。
#:     真实部署应该按最慢的 handler 来调。
DEFAULT_CLAIM_LEASE_SECONDS = 300


class MessageStatus:
    """
    消息生命周期状态。

    【完整流程】

        CREATED ──→ SENT ──→ DELIVERED ──→ RECEIVED ──→ PROCESSING ──→ PROCESSED
                       │           │            │             │
                       └───────────┴────────────┴─────────────┴──→ FAILED

        分支：
            DUPLICATE  重复投递（已处理过，跳过）
            TIMEOUT    超时未送达 / 未确认
            RETRY      正在重试

    【关于 SENT 和 DELIVERED 在本地是同一瞬间】

        本地传输里，「交给传输层」和「进入对方信箱」是同一步操作。
        但这里仍然记两条事件 —— 因为：

            网络传输里它们是两件事（发出 ≠ 对方收到 ACK）
            如果本地少记一条，那么「查询已送达消息数」这类
            语句在本地和网络上的行为就不一样了。

        状态机的形状必须和传输方式无关，否则日志就没法通用。
        两条事件的时间戳会几乎相同，这是正常的，也是如实记录。
    """

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"

    # 以下三个不在需求的最小清单里，但缺了它们日志会失真：
    #   不记 DUPLICATE 就看不出「同一条消息被投递了两次」
    #   不记 FAILED   就没有错误记录（需求明确要求有）
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"
    TIMEOUT = "TIMEOUT"
    RETRY = "RETRY"
    #: Runtime 明确不处理（例如接入 Inbox 之前的 legacy 消息）。
    #: 保留审计；processed_once 仍然为 0，绝不伪造「已处理」。
    IGNORED = "IGNORED"

    @classmethod
    def all(cls) -> List[str]:
        return sorted(
            value
            for name, value in vars(cls).items()
            if not name.startswith("_") and isinstance(value, str)
        )

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        """终态：走到这里就不会再变了。"""
        return status in {cls.PROCESSED, cls.FAILED, cls.DUPLICATE, cls.IGNORED}


# 状态 → 消息表里的时间戳列。
# 没有对应列的状态（比如 DELIVERED）时间只记在事件流里。
_STATUS_TIMESTAMP_COLUMN = {
    MessageStatus.CREATED: "created_at",
    MessageStatus.SENT: "sent_at",
    MessageStatus.RECEIVED: "received_at",
    MessageStatus.PROCESSED: "processed_at",
    MessageStatus.FAILED: "processed_at",
}


# ============================================================
# 脱敏
# ============================================================
# 【为什么必须有这一层】
#
#     日志会被写进数据库、被查询、被打印、被贴到聊天里求助。
#     一旦 API Key 或私钥进去了，就等于泄露了 ——
#     而且会永久留在数据库里，删都删不干净（还有 WAL、备份）。
#
#     所以不是「小心一点别写进去」，而是【在入口处自动过滤】。
#     靠人自觉是防不住的。
SENSITIVE_KEY_HINTS = (
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "password",
    "passwd",
    "pwd",
    "private_key",
    "privatekey",
    "privkey",
    "mnemonic",
    "seed_phrase",
    "seedphrase",
    "authorization",
    "credential",
    "access_token",
    "refresh_token",
    "auth_token",
    "bearer",
    "wallet_key",
    "signing_key",
)

# 值本身的特征。有些 payload 是 {"key": "sk-xxxx"} 这种
# 键名看不出来、但值是明显的密钥。
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"ark-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

REDACTED = "***REDACTED***"


def _is_sensitive_key(key: str) -> bool:
    """
    键名看起来是不是敏感。

    【注意这里没有把 "token" 本身列为敏感】
        因为本项目里 token 是个正当词汇：
            input_tokens / total_tokens / tokens_per_ai_coin
        如果一律脱敏，日志就没法看了。
        所以只匹配更明确的组合（access_token / auth_token 等）。
    """
    lowered = str(key).lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def _redact_string(value: str) -> str:
    """把字符串里长得像密钥的片段抹掉。"""
    result = value
    for pattern in SECRET_VALUE_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def redact(obj: Any) -> Tuple[Any, int]:
    """
    递归脱敏。

    返回：(脱敏后的对象, 抹掉的字段数)

    为什么返回计数？
        因为「这次 payload 里有敏感字段」本身是值得知道的信息 ——
        如果某个 Agent 老是往消息里塞 API Key，你希望日志里能看出来，
        而不是悄无声息地抹掉。
    """
    counter = [0]

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            out = {}
            for key, value in node.items():
                if _is_sensitive_key(str(key)):
                    out[key] = REDACTED
                    counter[0] += 1
                else:
                    out[key] = walk(value)
            return out

        if isinstance(node, (list, tuple)):
            return [walk(item) for item in node]

        if isinstance(node, str):
            redacted = _redact_string(node)
            if redacted != node:
                counter[0] += 1
            return redacted

        # 数字、布尔、None 原样返回
        return node

    return walk(obj), counter[0]


# ============================================================
# payload 指纹
# ============================================================
def payload_fingerprint(payload: Any) -> Tuple[int, str, str]:
    """
    算出 payload 的大小、hash 和规范化后的 JSON。

    返回：(字符数, sha256 十六进制, 规范化 JSON 文本)

    【为什么要 sort_keys】
        同一个 payload 用不同的键顺序序列化会得到不同的字符串，
        于是 hash 也不同 —— 那样 hash 就不能用来判断
        「两条消息的 payload 是不是一样」了。
        排序之后，只要内容一样，hash 就一样。
    """
    try:
        text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str
        )
    except (TypeError, ValueError):
        # 有无法序列化的对象时降级处理，绝不因此丢日志
        text = json.dumps(str(payload), ensure_ascii=False)

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return len(text), digest, text


# ============================================================
# 记录器
# ============================================================
@dataclass
class RecordedEvent:
    """一条刚写下的事件（给调用方和测试用）。"""

    event_id: str
    message_id: str
    status: str
    trace_id: Optional[str] = None
    conversation_id: Optional[str] = None
    actor: Optional[str] = None
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "message_id": self.message_id,
            "status": self.status,
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "actor": self.actor,
            "created_at": self.created_at,
        }


def _iso_seconds_before(timestamp: str, seconds: int) -> str:
    """
    把一个 ISO 时间戳往前推 N 秒，返回同样格式的字符串。

    【为什么可以直接用字符串比较时间】
        core.identity.utc_now_iso() 产出的格式是**恒定**的：

            YYYY-MM-DDTHH:MM:SS.ffffff+00:00

        永远是 32 个字符、永远是 UTC、永远是 6 位微秒。
        这种格式下字典序 == 时间序，所以 SQL 里可以直接
        `claimed_at < ?` 比较。

        （有测试专门验证这个格式前提；如果哪天格式变了，
          那条测试会先红，而不是让租约判断悄悄失灵。）

    【为什么不用 julianday()】
        SQLite 能解析这个格式，但那样每次比较都要做一次函数调用，
        而且用不上 claimed_at 上的索引。
        字符串比较既直接又能走索引。
    """
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError(f"时间戳格式不认识：{timestamp!r}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return (parsed - timedelta(seconds=seconds)).isoformat()


class CommunicationRecorder:
    """
    把通信过程中发生的事记进 SQLite。

    用法：
        recorder = CommunicationRecorder(db)
        recorder.record(message, MessageStatus.CREATED)
        recorder.record(message, MessageStatus.SENT, transport="local")

    查询：
        recorder.get_message("msg_xxx")
        recorder.events_for("msg_xxx")
        recorder.trace("trc_xxx")           # 整条业务链路
        recorder.conversation("conv_xxx")   # 一轮对话

    【失败时怎么办】
        strict=False（默认）：记日志失败只写一条 warning，不影响通信。
            理由：日志是观测手段，不该成为通信的单点故障。
                  因为日志写不进去就让消息发不出去，是本末倒置。
        strict=True：异常直接抛出。
            测试用 —— 免得日志悄悄没写进去，测试还全绿。
    """

    def __init__(
        self,
        db: Database,
        store_payload: bool = True,
        max_payload_chars: int = 65536,
        strict: bool = False,
        claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ):
        """
        参数：
            store_payload:     是否保存完整 payload（开发环境建议 True）
            max_payload_chars: payload 超长时截断，防止日志无限膨胀
            strict:            记日志失败时是否抛异常
            claim_lease_seconds: 认领租约时长（秒），见模块顶部的说明

        【为什么租约配置放在这里，而不是 Handler / 服务里】
            它是**持久化层**的策略：决定「数据库里那个 claim 还算不算数」。
            放在 Handler 里就变成每个调用方各写一套，
            而且换个部署就要改业务代码。

            放在这里之后：
                组合根按部署需要传一个值 → 全链路一致
                测试可以传一个很小的值 → 不用真的等 5 分钟
        """
        if claim_lease_seconds <= 0:
            raise ValueError(
                f"claim_lease_seconds 必须大于 0，收到 {claim_lease_seconds}"
            )
        self.db = db
        self.store_payload = bool(store_payload)
        self.max_payload_chars = int(max_payload_chars)
        self.strict = bool(strict)
        self.claim_lease_seconds = int(claim_lease_seconds)

        self.event_count = 0
        self.message_count = 0
        self.error_count = 0
        self.redaction_count = 0

    # ------------------------------------------------------------
    # 写：事件
    # ------------------------------------------------------------
    def record(
        self,
        message: Message,
        status: str,
        actor: Optional[str] = None,
        transport: Optional[str] = None,
        address: Optional[Address] = None,
        detail: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        is_remote: Optional[bool] = None,
    ) -> Optional[RecordedEvent]:
        """
        记录一个状态事件。

        这是唯一的写入口 —— 所有状态都走这里。
        为什么不做成一堆专用方法？
            因为状态会不断增加（Step 4/5 还会加），
            一个通用入口 + 状态常量，比二十个方法好维护。

        返回 RecordedEvent；失败且 strict=False 时返回 None。
        """
        if not isinstance(message, Message):
            return self._handle_error(
                ValueError(f"record 需要 Message，收到 {type(message).__name__}")
            )

        status = str(status or "").strip().upper()
        if not status:
            return self._handle_error(ValueError("status 不能为空"))

        try:
            return self._write(message, status, actor, transport, address,
                               detail, error_code, error_message, is_remote)
        except Exception as exc:  # noqa: BLE001
            # 【故意捕获所有异常】
            # 日志写失败绝不能让消息发送失败。
            return self._handle_error(exc)

    def _write(
        self,
        message: Message,
        status: str,
        actor: Optional[str],
        transport: Optional[str],
        address: Optional[Address],
        detail: Optional[str],
        error_code: Optional[str],
        error_message: Optional[str],
        is_remote: Optional[bool],
    ) -> RecordedEvent:
        now = utc_now_iso()
        event_id = new_message_id("evt")

        # --- payload 脱敏 + 指纹 ---
        safe_payload, redacted_fields = redact(message.payload)
        if redacted_fields:
            self.redaction_count += redacted_fields
            logger.warning(
                "消息 %s 的 payload 里有 %d 个敏感字段，已在日志中脱敏",
                message.message_id, redacted_fields,
            )

        payload_size, payload_hash, payload_text = payload_fingerprint(safe_payload)

        stored_payload: Optional[str] = None
        if self.store_payload:
            stored_payload = payload_text
            if len(stored_payload) > self.max_payload_chars:
                stored_payload = (
                    stored_payload[: self.max_payload_chars]
                    + f"…[截断，原始 {len(payload_text)} 字符]"
                )

        address_text = str(address) if address is not None else None
        if is_remote is None:
            is_remote = bool(address is not None and not address.is_local)

        # --- 错误信息也要脱敏 ---
        safe_error = _redact_string(str(error_message)) if error_message else None
        if safe_error and safe_error != error_message:
            self.redaction_count += 1

        timestamp_column = _STATUS_TIMESTAMP_COLUMN.get(status)

        with self.db.transaction() as conn:
            # --- 1) UPSERT 消息当前状态 ---
            #
            # 为什么用 UPSERT 而不是 INSERT？
            #   因为同一条消息会经历多个状态，甚至被投递多次。
            #   用 message_id 做主键 + UPSERT，
            #   保证「一个 message_id 只有一行」，不会出现
            #   无法区分的多条记录。
            #
            # 为什么时间戳用 COALESCE？
            #   后面的状态不该把前面已经记下的时间戳冲成 NULL。
            #   比如 RECEIVED 事件不该抹掉 SENT 的时间。
            conn.execute(
                f"""
                INSERT INTO {TABLE_COMM_MESSAGES} (
                    message_id, trace_id, conversation_id,
                    from_agent, to_agent, message_type,
                    created_at, sent_at, received_at, processed_at,
                    transport, address, is_remote,
                    status, retry_count,
                    error_code, error_message,
                    payload_size, payload_hash, payload,
                    protocol_version, reply_to, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                          ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    trace_id      = COALESCE(excluded.trace_id, {TABLE_COMM_MESSAGES}.trace_id),
                    conversation_id = COALESCE(excluded.conversation_id, {TABLE_COMM_MESSAGES}.conversation_id),
                    created_at    = COALESCE({TABLE_COMM_MESSAGES}.created_at, excluded.created_at),
                    sent_at       = COALESCE(excluded.sent_at, {TABLE_COMM_MESSAGES}.sent_at),
                    received_at   = COALESCE(excluded.received_at, {TABLE_COMM_MESSAGES}.received_at),
                    processed_at  = COALESCE(excluded.processed_at, {TABLE_COMM_MESSAGES}.processed_at),
                    transport     = COALESCE(excluded.transport, {TABLE_COMM_MESSAGES}.transport),
                    address       = COALESCE(excluded.address, {TABLE_COMM_MESSAGES}.address),
                    is_remote     = COALESCE(excluded.is_remote, {TABLE_COMM_MESSAGES}.is_remote),
                    status        = excluded.status,
                    error_code    = COALESCE(excluded.error_code, {TABLE_COMM_MESSAGES}.error_code),
                    error_message = COALESCE(excluded.error_message, {TABLE_COMM_MESSAGES}.error_message),
                    payload_size  = CASE WHEN excluded.payload_size > 0
                                         THEN excluded.payload_size
                                         ELSE {TABLE_COMM_MESSAGES}.payload_size END,
                    payload_hash  = COALESCE(excluded.payload_hash, {TABLE_COMM_MESSAGES}.payload_hash),
                    payload       = COALESCE(excluded.payload, {TABLE_COMM_MESSAGES}.payload),
                    retry_count   = CASE WHEN excluded.status = 'RETRY'
                                         THEN {TABLE_COMM_MESSAGES}.retry_count + 1
                                         ELSE {TABLE_COMM_MESSAGES}.retry_count END,
                    updated_at    = excluded.updated_at
                """,
                (
                    message.message_id,
                    message.trace_id,
                    message.conversation_id,
                    message.from_agent,
                    message.to_agent,
                    message.message_type,
                    now if timestamp_column == "created_at" else None,
                    now if timestamp_column == "sent_at" else None,
                    now if timestamp_column == "received_at" else None,
                    now if timestamp_column == "processed_at" else None,
                    transport,
                    address_text,
                    1 if is_remote else 0,
                    status,
                    error_code,
                    safe_error,
                    payload_size,
                    payload_hash,
                    stored_payload,
                    message.protocol_version,
                    message.reply_to,
                    now,
                ),
            )

            # --- 2) 追加事件 ---
            #
            # 注意这里【只 INSERT】。
            # 事件流是历史，历史不允许被改写 ——
            # 这和账本 append-only 是同一个道理。
            conn.execute(
                f"""
                INSERT INTO {TABLE_COMM_EVENTS} (
                    event_id, message_id, trace_id, conversation_id,
                    status, actor, transport, address, detail,
                    error_code, error_message,
                    payload_size, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    message.message_id,
                    message.trace_id,
                    message.conversation_id,
                    status,
                    actor,
                    transport,
                    address_text,
                    detail,
                    error_code,
                    safe_error,
                    payload_size,
                    payload_hash,
                    now,
                ),
            )

        self.event_count += 1
        return RecordedEvent(
            event_id=event_id,
            message_id=message.message_id,
            status=status,
            trace_id=message.trace_id,
            conversation_id=message.conversation_id,
            actor=actor,
            created_at=now,
        )

    def _handle_error(self, exc: Exception) -> None:
        self.error_count += 1
        if self.strict:
            raise exc
        logger.warning("通信日志写入失败（不影响通信）：%s: %s", type(exc).__name__, exc)
        return None

    # ------------------------------------------------------------
    # 读：按 message_id
    # ------------------------------------------------------------
    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """取一条消息的当前状态。"""
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_COMM_MESSAGES} WHERE message_id = ?",
            (str(message_id),),
        )
        return dict(row) if row is not None else None

    def events_for(self, message_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """取一条消息的完整事件流（按时间正序）。"""
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_COMM_EVENTS}
             WHERE message_id = ?
             ORDER BY created_at ASC, rowid ASC
             LIMIT ?
            """,
            (str(message_id), int(limit)),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------
    # 读：按 trace_id / conversation_id
    # ------------------------------------------------------------
    def trace(self, trace_id: str) -> List[Dict[str, Any]]:
        """
        取一整条业务链路里的所有消息。

        这是「一次任务跨了哪些消息」的入口 ——
        Step 4 里 Task 会把自己的 trace_id 打在所有消息上，
        于是这条查询就能还原整个任务的通信过程。
        """
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_COMM_MESSAGES}
             WHERE trace_id = ?
             ORDER BY created_at ASC, rowid ASC
            """,
            (str(trace_id),),
        )
        return [dict(row) for row in rows]

    def trace_events(self, trace_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """一条链路的完整事件流（跨消息，按时间排序）。"""
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_COMM_EVENTS}
             WHERE trace_id = ?
             ORDER BY created_at ASC, rowid ASC
             LIMIT ?
            """,
            (str(trace_id), int(limit)),
        )
        return [dict(row) for row in rows]

    def conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        """取一轮对话 / 协商里的所有消息。"""
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_COMM_MESSAGES}
             WHERE conversation_id = ?
             ORDER BY created_at ASC, rowid ASC
            """,
            (str(conversation_id),),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------
    # 读：列表与统计
    # ------------------------------------------------------------
    def recent(
        self,
        limit: int = 20,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """最近的消息。"""
        conditions = []
        params: list = []

        if agent_id:
            conditions.append("(from_agent = ? OR to_agent = ?)")
            params.extend([agent_id, agent_id])
        if status:
            conditions.append("status = ?")
            params.append(str(status).upper())

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(int(limit))

        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_COMM_MESSAGES}
            {where}
            ORDER BY rowid DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    def traces(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        列出最近的 trace 及其消息数。

        为什么需要这个？
            你通常不知道 trace_id 是什么 ——
            得先能「看看最近有哪些任务」，才能挑一个去查。
        """
        rows = self.db.fetchall(
            f"""
            SELECT
                trace_id,
                COUNT(*)                       AS messages,
                MIN(created_at)                AS started_at,
                MAX(updated_at)                AS last_at,
                SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'PROCESSED' THEN 1 ELSE 0 END) AS processed
              FROM {TABLE_COMM_MESSAGES}
             WHERE trace_id IS NOT NULL
             GROUP BY trace_id
             ORDER BY last_at DESC
             LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(row) for row in rows]

    # ============================================================
    # 持久化的处理事实（Step 4D.1）
    #
    # 【为什么这组方法在这里，而不是在 Manager 里】
    #     因为 CommunicationRecorder 是通信层**唯一的数据库边界**。
    #     Manager 负责「消息怎么走」，不该知道有 SQLite。
    #     这和 Task 那边「TaskStore 是唯一边界」是同一个原则。
    # ============================================================
    def claim_message(
        self,
        message: Message,
        owner: str,
        lease_seconds: Optional[int] = None,
        now: Optional[str] = None,
    ) -> MessageClaim:
        """
        **原子地**认领一条消息（带租约）。这是持久化去重的核心。

        ============================================================
        【为什么必须是原子的】
        ============================================================

            错的写法（经典的 TOCTOU race）：

                if not was_processed(msg_id):     # A、B 都看到 False
                    mark_processing(msg_id)       # A、B 都去处理
                    handle(message)               # → 两次副作用

            正确写法是让**数据库**来裁决 —— 一次带条件的 UPDATE：

                UPDATE communication_messages
                   SET claim_id = ?, claimed_at = ?
                 WHERE message_id = ?
                   AND processed_once = 0
                   AND (claim_id IS NULL OR claimed_at < <过期线>)

            影响行数 1 → 我抢到了（新认领或接手过期的）
            影响行数 0 → 没抢到，再查一次看是被处理过了还是别人正在处理

        ============================================================
        【Step 4D.1 → Workspace Phase 0：加上了租约】
        ============================================================

            4D.1 的条件是 `claim_id IS NULL` —— 那意味着认领**永久**有效。
            如果 worker 认领之后被 kill -9，这个 claim 就永远卡在那里，
            消息永远显示 BUSY，再也没人能处理它。

            现在条件变成：

                claim_id IS NULL  OR  claimed_at < now - lease

            也就是「没人认领 **或** 上一个认领已经过期」。

        ============================================================
        【租约 ≠ 处理结果】
        ============================================================

            ✅ 租约过期  → 别人可以接手（临时占用失效）
            ❌ 租约过期  → processed_once 不变（成功事实与租约无关）
            ❌ 租约过期  → failure_count 不加（worker 崩了 ≠ 业务失败）
            ❌ 租约过期  → 消息不删除

            这是本阶段最容易搞错的地方：租约只管「谁现在有权处理」，
            不管「处理成功没有」。

        ============================================================
        【为什么先补一行再认领】
        ============================================================

            claim 的前提是 communication_messages 里已经有这一行。
            正常情况下 Manager 在这之前会先记一条 RECEIVED 日志。
            但这条方法不该依赖「调用方记得先记日志」——
            所以它自己先 INSERT OR IGNORE 兜底。

        参数：
            lease_seconds: 覆盖默认租约（测试会用很小的值）
            now:           覆盖当前时间（测试用来模拟「时间流逝」）
        """
        owner = str(owner)
        lease = self.claim_lease_seconds if lease_seconds is None else int(lease_seconds)
        if lease <= 0:
            raise ValueError(f"lease_seconds 必须大于 0，收到 {lease}")

        timestamp = now or utc_now_iso()
        # 过期线：claimed_at 早于这个时刻，就算过期
        cutoff = _iso_seconds_before(timestamp, lease)

        with self.db.transaction() as conn:
            # --- 1) 兜底：确保这一行存在 ---
            conn.execute(
                f"""
                INSERT INTO {TABLE_COMM_MESSAGES} (
                    message_id, trace_id, conversation_id,
                    from_agent, to_agent, message_type,
                    created_at, status, payload_size, retry_count,
                    processed_once, failure_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (
                    message.message_id,
                    message.trace_id,
                    message.conversation_id,
                    message.from_agent,
                    message.to_agent,
                    message.message_type,
                    timestamp,
                    "CREATED",
                    timestamp,
                ),
            )

            # --- 2) 先看一眼当前状态（用于区分「已处理」和「别人在处理」）---
            before = conn.execute(
                f"""
                SELECT processed_once, claim_id, claimed_at,
                       failure_count, status
                  FROM {TABLE_COMM_MESSAGES}
                 WHERE message_id = ?
                """,
                (message.message_id,),
            ).fetchone()

            if before is not None and int(before["processed_once"] or 0) == 1:
                return MessageClaim(
                    status=ClaimStatus.DUPLICATE,
                    message_id=message.message_id,
                    owner=before["claim_id"],
                    claimed_at=before["claimed_at"],
                    failure_count=int(before["failure_count"] or 0),
                    detail="这条消息以前已经成功处理过（持久化事实）",
                )

            if (before is not None
                    and str(before["status"] or "") == MessageStatus.IGNORED):
                return MessageClaim(
                    status=ClaimStatus.DUPLICATE,
                    message_id=message.message_id,
                    owner=before["claim_id"],
                    claimed_at=before["claimed_at"],
                    failure_count=int(before["failure_count"] or 0),
                    detail="消息已被 Inbox 标记为 IGNORED，不再处理",
                )

            previous_owner = before["claim_id"] if before is not None else None
            previous_claimed_at = (
                before["claimed_at"] if before is not None else None
            )

            # --- 3) 原子认领（带租约）---
            #
            # 【一行就是全部逻辑】
            #     processed_once = 0        排除已经成功处理过的
            #     claim_id IS NULL          没人认领 → 直接拿
            #     claimed_at < cutoff       上一个认领过期 → 接手
            #
            #     没有 SELECT-then-UPDATE 的窗口：
            #     两个进程同时发这条语句，数据库只会让一个 rowcount=1。
            cursor = conn.execute(
                f"""
                UPDATE {TABLE_COMM_MESSAGES}
                   SET claim_id = ?, claimed_at = ?, updated_at = ?
                 WHERE message_id = ?
                   AND processed_once = 0
                   AND status != ?
                   AND (
                        claim_id IS NULL
                        OR claimed_at IS NULL
                        OR claimed_at < ?
                   )
                """,
                (
                    owner, timestamp, timestamp, message.message_id,
                    MessageStatus.IGNORED, cutoff,
                ),
            )

            if cursor.rowcount == 1:
                reclaimed = previous_owner is not None
                return MessageClaim(
                    status=ClaimStatus.CLAIMED,
                    message_id=message.message_id,
                    owner=owner,
                    claimed_at=timestamp,
                    failure_count=int(before["failure_count"] or 0)
                    if before is not None else 0,
                    reclaimed=reclaimed,
                    previous_owner=previous_owner if reclaimed else None,
                    detail=(
                        f"接手了一个过期认领（原持有者 {previous_owner}，"
                        f"于 {previous_claimed_at} 认领，租约 {lease} 秒）"
                        if reclaimed else "认领成功，可以处理"
                    ),
                )

            # --- 4) 没抢到：分清「处理过了」还是「别人正在处理」---
            after = conn.execute(
                f"""
                SELECT processed_once, claim_id, claimed_at, failure_count
                  FROM {TABLE_COMM_MESSAGES}
                 WHERE message_id = ?
                """,
                (message.message_id,),
            ).fetchone()

            if after is not None and int(after["processed_once"] or 0) == 1:
                return MessageClaim(
                    status=ClaimStatus.DUPLICATE,
                    message_id=message.message_id,
                    owner=after["claim_id"],
                    claimed_at=after["claimed_at"],
                    failure_count=int(after["failure_count"] or 0),
                    detail="这条消息以前已经成功处理过（持久化事实）",
                )

            return MessageClaim(
                status=ClaimStatus.BUSY,
                message_id=message.message_id,
                owner=after["claim_id"] if after is not None else None,
                claimed_at=after["claimed_at"] if after is not None else None,
                failure_count=(
                    int(after["failure_count"] or 0) if after is not None else 0
                ),
                detail=(
                    f"有别人正在处理这条消息（认领未过期，租约 "
                    f"{lease} 秒）"
                ),
            )

    def mark_processed_id(self, message_id: str, owner: str) -> bool:
        """按 message_id 标记处理成功；给没有 Message 对象的 checkpoint 用。"""
        key = str(message_id or "").strip()
        if not key:
            return False
        owner = str(owner)
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {TABLE_COMM_MESSAGES}
                   SET processed_once = 1,
                       processed_at  = COALESCE(processed_at, ?),
                       updated_at    = ?
                 WHERE message_id = ?
                   AND processed_once = 0
                """,
                (now, now, key),
            )
            first_time = cursor.rowcount == 1
            conn.execute(
                f"""
                UPDATE {TABLE_COMM_MESSAGES}
                   SET claim_id = NULL, claimed_at = NULL
                 WHERE message_id = ? AND claim_id = ?
                """,
                (key, owner),
            )
        return first_time

    def mark_processed(self, message: Message, owner: str) -> bool:
        """
        把「成功处理过」变成**持久化事实**。

        【为什么用 MAX 语义（只增不减）】

            processed_once 一旦变成 1，就**永远不该回到 0**。

            为什么？因为后来还会有别的日志事件打到这一行上：

                处理成功       → status = PROCESSED, processed_once = 1
                又来一条重发   → status = DUPLICATE   ← status 被覆盖了

            如果 processed_once 也跟着被重写，那「处理过」这个事实
            就会被下一条重发抹掉 —— 去重就失效了。

            所以这里的 UPDATE 里它总是 1，而**别的地方**
            （recorder 的 UPSERT）用 MAX 保持它单调。

        返回是否真的从 0 翻到了 1（第一次成功）。
        """
        return self.mark_processed_id(message.message_id, owner)

    def mark_failed(
        self,
        message: Message,
        owner: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """
        记录「处理失败了」——**但绝不把它标成已完成**。

        ============================================================
        【为什么失败不能写 processed_once = 1】
        ============================================================

            失败可能是**临时**的（数据库忙、系统异常、网络抖动）。
            一旦把失败也标成「已处理」，这条消息就永远失去了
            被正确处理的机会 —— 而且没人会发现。

            所以这里只做两件事：

                1. failure_count + 1      （失败是事实，要留痕）
                2. error_code / error_message（失败原因要留痕）

            而 processed_once **保持 0**。

        ============================================================
        【为什么要释放认领】
        ============================================================

            处理失败了，就不该继续占着认领不放。
            否则这条消息会永远显示「有别人正在处理」，
            既处理不了，也看不出问题在哪。

            释放之后：如果将来有了重试机制，它可以重新认领；
            现在没有重试机制，它就停在「失败过一次」这个状态 ——
            这是一个**如实**的状态，不是一个骗人的状态。

            （注意：本阶段**没有**实现重试。释放认领只是
              「不撒谎」，不是「自动重来」。）
        """
        owner = str(owner)
        now = utc_now_iso()
        safe_error = _redact_string(str(error_message)) if error_message else None

        with self.db.transaction() as conn:
            conn.execute(
                f"""
                UPDATE {TABLE_COMM_MESSAGES}
                   SET failure_count = failure_count + 1,
                       error_code      = COALESCE(?, error_code),
                       error_message   = COALESCE(?, error_message),
                       claim_id        = NULL,
                       claimed_at      = NULL,
                       updated_at      = ?
                 WHERE message_id = ?
                """,
                (error_code, safe_error, now, message.message_id),
            )

    def was_processed(self, message_id: str) -> bool:
        """
        这条消息成功处理过吗？—— **持久化事实**。

        这是 4D.1 要补的那个能力：
        进程重启之后，这个问题仍然答得出来。
        """
        if not message_id:
            return False

        row = self.db.fetchone(
            f"""
            SELECT processed_once FROM {TABLE_COMM_MESSAGES}
             WHERE message_id = ?
            """,
            (message_id,),
        )
        return bool(row and int(row["processed_once"] or 0) == 1)

    def processing_state(self, message_id: str) -> Optional[Dict[str, Any]]:
        """
        这条消息的处理状态（只读，供调试和演示用）。

        返回 None 表示「从来没见过这条消息」。
        """
        row = self.db.fetchone(
            f"""
            SELECT status, processed_once, processed_at,
                   claim_id, claimed_at, failure_count,
                   error_code, error_message
              FROM {TABLE_COMM_MESSAGES}
             WHERE message_id = ?
            """,
            (message_id,),
        )
        if row is None:
            return None

        return {
            "status": row["status"],
            "processed": bool(int(row["processed_once"] or 0)),
            "processed_at": row["processed_at"],
            "claimed_by": row["claim_id"],
            "claimed_at": row["claimed_at"],
            "failure_count": int(row["failure_count"] or 0),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }

    # ============================================================
    # Agent Inbox V1：Runtime 消费边界
    # ============================================================
    def get_inbox_state(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """读取一个 Agent 的 Inbox 运行时状态。"""
        key = str(agent_id or "").strip()
        if not key:
            return None
        row = self.db.fetchone(
            f"SELECT * FROM {TABLE_COMM_INBOX_STATE} WHERE agent_id = ?",
            (key,),
        )
        if row is None:
            return None
        out = dict(row)
        out["enabled"] = bool(int(out.get("enabled") or 0))
        return out

    def list_inbox_states(self) -> List[Dict[str, Any]]:
        rows = self.db.fetchall(
            f"SELECT * FROM {TABLE_COMM_INBOX_STATE} ORDER BY agent_id")
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(int(item.get("enabled") or 0))
            out.append(item)
        return out

    def initialize_inbox(
        self,
        agent_id: str,
        *,
        enabled: bool = False,
        cutover_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """为 Agent 建立 Inbox 状态；只在第一次建立时标记 legacy。

        🔒 这是**一次性 cutover**：
           - 已有 state 的 Agent 不会重复标记历史消息；
           - cutover 之前未处理的消息 -> IGNORED；
           - processed_once 保持 0，不伪造「已处理」。
        """
        key = str(agent_id or "").strip()
        if not key:
            raise ValueError("agent_id 不能为空")
        existing = self.get_inbox_state(key)
        if existing is not None:
            return existing

        now = utc_now_iso()
        cutoff = str(cutover_at or now)
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {TABLE_COMM_INBOX_STATE} (
                    agent_id, enabled, cutover_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(agent_id) DO NOTHING
                """,
                (key, 1 if enabled else 0, cutoff, now),
            )

        # 首次建立状态时清理 legacy。失败不应该阻止启动：
        # 消息仍在，pending 查询也会按 cutover 过滤。
        try:
            self.ignore_pending_before(
                key, cutoff, reason="legacy_inbox_cutover")
        except Exception:  # noqa: BLE001
            logger.warning("Inbox legacy 标记失败：%s", key, exc_info=True)

        state = self.get_inbox_state(key)
        return state or {
            "agent_id": key,
            "enabled": bool(enabled),
            "cutover_at": cutoff,
            "last_seen_at": None,
            "updated_at": now,
        }

    def set_inbox_enabled(
        self,
        agent_id: str,
        enabled: bool,
        *,
        cutover_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """打开/关闭某个 Agent 的 Inbox 消费；不删除任何消息。"""
        key = str(agent_id or "").strip()
        if not key:
            raise ValueError("agent_id 不能为空")
        state = self.get_inbox_state(key)
        if state is None:
            return self.initialize_inbox(
                key, enabled=enabled, cutover_at=cutover_at)
        now = utc_now_iso()
        with self.db.transaction() as conn:
            if cutover_at:
                conn.execute(
                    f"""
                    UPDATE {TABLE_COMM_INBOX_STATE}
                       SET enabled = ?, cutover_at = ?, updated_at = ?
                     WHERE agent_id = ?
                    """,
                    (1 if enabled else 0, str(cutover_at), now, key),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE {TABLE_COMM_INBOX_STATE}
                       SET enabled = ?, updated_at = ?
                     WHERE agent_id = ?
                    """,
                    (1 if enabled else 0, now, key),
                )
        return self.get_inbox_state(key) or state

    def touch_inbox(self, agent_id: str) -> None:
        key = str(agent_id or "").strip()
        if not key:
            return
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                UPDATE {TABLE_COMM_INBOX_STATE}
                   SET last_seen_at = ?, updated_at = ?
                 WHERE agent_id = ?
                """,
                (utc_now_iso(), utc_now_iso(), key),
            )

    def list_pending(
        self,
        agent_id: str,
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列出 cutover 之后、尚未被 Runtime 消费的消息。"""
        key = str(agent_id or "").strip()
        state = self.get_inbox_state(key)
        if state is None:
            return []
        cutoff = str(state.get("cutover_at") or "")
        rows = self.db.fetchall(
            f"""
            SELECT * FROM {TABLE_COMM_MESSAGES}
             WHERE to_agent = ?
               AND processed_once = 0
               AND status IN (?, ?)
               AND created_at >= ?
             ORDER BY created_at ASC, rowid ASC
             LIMIT ?
            """,
            (
                key,
                MessageStatus.DELIVERED,
                MessageStatus.FAILED,
                cutoff,
                max(1, int(limit)),
            ),
        )
        return [dict(row) for row in rows]

    def mark_ignored(
        self, message_id: str, *, reason: str = "inbox_ignored",
    ) -> bool:
        """把一条消息标记为 IGNORED；保留 payload 和事件历史。"""
        key = str(message_id or "").strip()
        row = self.get_message(key)
        if row is None:
            return False
        if str(row.get("status") or "") == MessageStatus.IGNORED:
            return False
        if int(row.get("processed_once") or 0) == 1:
            return False

        now = utc_now_iso()
        safe_reason = _redact_string(str(reason or "inbox_ignored"))
        event_id = new_message_id("evt")
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                UPDATE {TABLE_COMM_MESSAGES}
                   SET status = ?,
                       claim_id = NULL,
                       claimed_at = NULL,
                       error_code = 'INBOX_IGNORED',
                       error_message = ?,
                       updated_at = ?
                 WHERE message_id = ?
                """,
                (MessageStatus.IGNORED, safe_reason, now, key),
            )
            conn.execute(
                f"""
                INSERT INTO {TABLE_COMM_EVENTS} (
                    event_id, message_id, trace_id, conversation_id,
                    status, actor, transport, address, detail,
                    error_code, error_message,
                    payload_size, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    key,
                    row.get("trace_id"),
                    row.get("conversation_id"),
                    MessageStatus.IGNORED,
                    "inbox",
                    row.get("transport"),
                    row.get("address"),
                    safe_reason,
                    "INBOX_IGNORED",
                    safe_reason,
                    row.get("payload_size"),
                    row.get("payload_hash"),
                    now,
                ),
            )
        self.event_count += 1
        return True

    def ignore_pending_before(
        self,
        agent_id: str,
        cutover_at: str,
        *,
        reason: str = "legacy_inbox_cutover",
    ) -> int:
        """把 cutover 之前、尚未处理的消息标记为 legacy IGNORED。"""
        key = str(agent_id or "").strip()
        cutoff = str(cutover_at or "").strip()
        if not key or not cutoff:
            return 0
        rows = self.db.fetchall(
            f"""
            SELECT message_id FROM {TABLE_COMM_MESSAGES}
             WHERE to_agent = ?
               AND processed_once = 0
               AND status NOT IN (?, ?, ?)
               AND created_at < ?
            """,
            (
                key,
                MessageStatus.PROCESSED,
                MessageStatus.DUPLICATE,
                MessageStatus.IGNORED,
                cutoff,
            ),
        )
        ignored = 0
        for row in rows:
            if self.mark_ignored(
                str(row["message_id"]), reason=reason):
                ignored += 1
        return ignored

    def inbox_stats(self, agent_id: str) -> Dict[str, Any]:
        """给 UI / Runtime 用的 Inbox 统计。"""
        key = str(agent_id or "").strip()
        state = self.get_inbox_state(key)
        total_row = self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM {TABLE_COMM_MESSAGES} WHERE to_agent = ?",
            (key,),
        )
        total = int(total_row["n"]) if total_row else 0
        if state is None:
            return {
                "agent_id": key,
                "initialized": False,
                "enabled": False,
                "cutover_at": None,
                "pending": 0,
                "failed": 0,
                "processing": 0,
                "processed": 0,
                "ignored": 0,
                "legacy": total,
                "total": total,
            }

        cutoff = str(state.get("cutover_at") or "")
        row = self.db.fetchone(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN processed_once = 1 THEN 1 ELSE 0 END) AS processed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS ignored,
                SUM(CASE WHEN processed_once = 0
                          AND status IN (?, ?)
                          AND created_at >= ? THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN processed_once = 0
                          AND status = ?
                          AND created_at >= ? THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS processing,
                SUM(CASE WHEN processed_once = 0
                          AND created_at < ?
                          AND status != ? THEN 1 ELSE 0 END) AS legacy
              FROM {TABLE_COMM_MESSAGES}
             WHERE to_agent = ?
            """,
            (
                MessageStatus.IGNORED,
                MessageStatus.DELIVERED,
                MessageStatus.FAILED,
                cutoff,
                MessageStatus.FAILED,
                cutoff,
                MessageStatus.PROCESSING,
                cutoff,
                MessageStatus.IGNORED,
                key,
            ),
        )
        data = dict(row) if row is not None else {}
        return {
            "agent_id": key,
            "initialized": True,
            "enabled": bool(state.get("enabled")),
            "cutover_at": cutoff,
            "pending": int(data.get("pending") or 0),
            "failed": int(data.get("failed") or 0),
            "processing": int(data.get("processing") or 0),
            "processed": int(data.get("processed") or 0),
            "ignored": int(data.get("ignored") or 0),
            "legacy": int(data.get("legacy") or 0),
            "total": int(data.get("total") or total),
        }

    def count_processed(self) -> int:
        """成功处理过的消息条数（持久化事实）。"""
        row = self.db.fetchone(
            f"""
            SELECT COUNT(*) AS n FROM {TABLE_COMM_MESSAGES}
             WHERE processed_once = 1
            """
        )
        return int(row["n"]) if row else 0

    def count_messages(self) -> int:
        row = self.db.fetchone(f"SELECT COUNT(*) AS n FROM {TABLE_COMM_MESSAGES}")
        return int(row["n"]) if row else 0

    def count_events(self) -> int:
        row = self.db.fetchone(f"SELECT COUNT(*) AS n FROM {TABLE_COMM_EVENTS}")
        return int(row["n"]) if row else 0

    def stats(self) -> Dict[str, Any]:
        """按状态汇总 —— 一眼看出有没有失败和重复。"""
        rows = self.db.fetchall(
            f"""
            SELECT status, COUNT(*) AS n
              FROM {TABLE_COMM_MESSAGES}
             GROUP BY status
            """
        )
        by_status = {row["status"]: int(row["n"]) for row in rows}

        event_rows = self.db.fetchall(
            f"""
            SELECT status, COUNT(*) AS n
              FROM {TABLE_COMM_EVENTS}
             GROUP BY status
            """
        )
        events_by_status = {row["status"]: int(row["n"]) for row in event_rows}

        return {
            "messages": self.count_messages(),
            "events": self.count_events(),
            "messages_by_status": by_status,
            "events_by_status": events_by_status,
            "write_errors": self.error_count,
            "redacted_fields": self.redaction_count,
        }

    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "recorder": type(self).__name__,
            "store_payload": self.store_payload,
            "max_payload_chars": self.max_payload_chars,
            "strict": self.strict,
            "messages": self.count_messages(),
            "events": self.count_events(),
            "write_errors": self.error_count,
        }

    def __repr__(self) -> str:
        return (
            f"CommunicationRecorder(messages={self.count_messages()}, "
            f"events={self.count_events()})"
        )
