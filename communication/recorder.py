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
from communication.message import Message
from storage.database import Database
from storage.models import TABLE_COMM_EVENTS, TABLE_COMM_MESSAGES

logger = get_logger(__name__)


# ============================================================
# 状态
# ============================================================
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
        return status in {cls.PROCESSED, cls.FAILED, cls.DUPLICATE}


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
    ):
        """
        参数：
            store_payload:     是否保存完整 payload（开发环境建议 True）
            max_payload_chars: payload 超长时截断，防止日志无限膨胀
            strict:            记日志失败时是否抛异常
        """
        self.db = db
        self.store_payload = bool(store_payload)
        self.max_payload_chars = int(max_payload_chars)
        self.strict = bool(strict)

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
