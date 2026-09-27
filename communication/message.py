"""
Message —— 通信消息
============================================================

【为什么 Message 必须是一个独立的数据结构】

    不能是「调用对方的方法」：
        agent_b.receive(text)          ❌ 本地函数调用，跨不了机器

    不能是「往对方的队列里塞对象」：
        agent_b.mailbox.put(obj)       ❌ 对方在另一台机器上时不存在

    必须是「一个可以被序列化成字节、也能从字节恢复的数据」：
        Message → JSON → 网络 → JSON → Message   ✅

    这是整条通信链路的物理基础：
        只有能变成字节的东西，才能穿过网络。

【为未来留的扩展位】

    Step 3 只需要 TEXT / PING / PONG。
    但接下来几步马上会用到 TASK_OFFER / BID / ACCEPT / SUBMIT / PAYMENT_RESULT……

    所以 MessageType 刻意【不是 Enum 白名单】：
        · 已知类型有常量，方便引用和拼写检查
        · 未知类型也允许通过 —— 否则每次加消息类型都要改这个文件，
          而「加消息类型」在未来是高频操作

    Message 还预留了：
        protocol_version  协议版本（以后双方版本不一致时用来协商）
        reply_to          回复哪条消息（形成会话链）
        extensions        未知字段的容器（前向兼容的关键，见下）

【前向兼容：为什么 from_dict 不丢弃未知字段】

    假设某天消息结构升级了，加了一个 signature 字段。
    旧版本的节点收到带 signature 的消息时：
        如果直接丢弃 → 转发给第三方时签名就没了，链路断裂
        存进 extensions → 原样保留，转发时不丢

    只需要多几行代码，就能避免未来一个很麻烦的兼容性问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from core.identity import new_message_id, utc_now_iso
from communication.errors import MessageValidationError


# ============================================================
# 消息类型
# ============================================================
class MessageType:
    """
    已知的消息类型常量。

    【注意这不是白名单】
        Message 允许使用这里没列出的类型。
        它存在的意义只是「别把字符串拼错」和「有个地方查」。

    Step 3 真正会用到的只有前三个。
    后面那些是接下来几步要用的，先写在这里当路标 ——
    它们现在【没有任何业务逻辑】，只是让数据结构提前准备好。
    """

    # ---- Step 3 实际使用 ----
    TEXT = "TEXT"          # 普通文本消息
    PING = "PING"          # 探活
    PONG = "PONG"          # 探活回复
    ERROR = "ERROR"        # 出错了

    # ---- 为后续步骤预留（现在只有名字，没有实现）----
    TASK_OFFER = "TASK_OFFER"            # 任务邀约（Step 4）
    COUNTER_OFFER = "COUNTER_OFFER"      # 还价（Step 4）
    ACCEPT = "ACCEPT"                    # 接受（Step 4）
    REJECT = "REJECT"                    # 拒绝（Step 4）
    # ---- 执行生命周期（Step 4D 接入）----
    #
    # 【为什么 START / COMPLETE 是这次才加的】
    #     3.6 冻结协议时只预留了 SUBMIT / VERIFY，
    #     因为那时以为「开工」和「结项」可以靠状态推断。
    #     4D 实际接消息时发现不行：状态变化必须由**一条明确的消息**触发，
    #     否则接收方无从知道「对方开工了」。
    #
    #     这是 **additive** 变更 —— 只增加新类型，不改任何已有类型
    #     的语义或形状，所以 protocol_version 保持 1（见 PROTOCOL.md §7.5）。
    START = "START"                      # 开工（承接方 → 发起方）
    SUBMIT = "SUBMIT"                    # 提交成果（承接方 → 发起方）
    VERIFY = "VERIFY"                    # 验收结论（发起方 → 承接方）
    COMPLETE = "COMPLETE"                # 结项（发起方 → 承接方）

    # ---- 后续步骤预留（现在只有名字，没有实现）----
    PAYMENT_REQUEST = "PAYMENT_REQUEST"  # 请求付款（结算阶段）
    PAYMENT_RESULT = "PAYMENT_RESULT"    # 付款结果（结算阶段）

    @classmethod
    def known(cls) -> List[str]:
        """列出所有已知类型（用于文档和调试输出）。"""
        return sorted(
            value
            for name, value in vars(cls).items()
            if not name.startswith("_") and isinstance(value, str)
        )


# 协议版本。以后协议有不兼容变更时递增。
PROTOCOL_VERSION = "1"


# ============================================================
# 消息
# ============================================================
@dataclass
class Message:
    """
    一条通信消息。

    最小必需字段：
        from_agent     谁发的
        to_agent       发给谁
        message_type   什么类型的消息
        payload        内容

    自动补上的字段：
        message_id     唯一 ID（用于去重和追踪）
        timestamp      发出时间

    可选字段：
        reply_to           在回复哪条消息
        protocol_version   协议版本
        extensions         未知字段（前向兼容）
    """

    from_agent: str
    to_agent: str
    message_type: str = MessageType.TEXT
    payload: Dict[str, Any] = field(default_factory=dict)

    # 自动生成，但允许显式指定（测试和重放场景需要）
    message_id: str = field(default_factory=lambda: new_message_id("msg"))
    timestamp: str = field(default_factory=utc_now_iso)

    # ---- 用于把多条消息串成一条链路（Step 3.5 新增）----
    #
    # 这两个 ID 是「消息本身的数据」，不是日志 ——
    # 因为它们要被发给对方。对方回复时会把同一个 trace_id 带回来，
    # 于是整条链路就串起来了。
    #
    #   trace_id         一次完整任务 / 业务流程（跨很多条消息）
    #   conversation_id  一轮对话 / 协商（通常一个来回）
    #
    # 举例：A 发起外包任务
    #     trace_id = trc_xxx         ← 这个任务从头到尾都用它
    #       TASK_REQUEST   conversation_id = conv_1    同一轮议价
    #       BID            conversation_id = conv_1
    #       ACCEPT         conversation_id = conv_1
    #       SUBMIT         conversation_id = conv_2   换了一轮：交付
    #
    # 一次任务只有一轮对话时两者内容会很像，但仍是两个独立维度：
    # 一个管「哪个任务」，一个管「哪轮对话」。
    trace_id: str = field(default_factory=lambda: new_message_id("trc"))
    conversation_id: str = field(default_factory=lambda: new_message_id("conv"))

    reply_to: Optional[str] = None
    protocol_version: str = PROTOCOL_VERSION
    extensions: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------
    def __post_init__(self) -> None:
        self.from_agent = str(self.from_agent or "").strip()
        self.to_agent = str(self.to_agent or "").strip()
        self.message_type = str(self.message_type or "").strip().upper()
        self.message_id = str(self.message_id or "").strip()

        if not self.from_agent:
            raise MessageValidationError("消息缺少 from_agent（发送方）")
        if not self.to_agent:
            raise MessageValidationError("消息缺少 to_agent（接收方）")
        if not self.message_type:
            raise MessageValidationError("消息缺少 message_type")
        if not self.message_id:
            raise MessageValidationError("消息缺少 message_id")

        # trace_id / conversation_id 现在就要校验：
        # 它们是把整条链路串起来的钥匙，缺了日志就串不起来。
        self.trace_id = str(self.trace_id or "").strip()
        self.conversation_id = str(self.conversation_id or "").strip()
        if not self.trace_id:
            raise MessageValidationError("消息缺少 trace_id")
        if not self.conversation_id:
            raise MessageValidationError("消息缺少 conversation_id")

        if self.from_agent == self.to_agent:
            raise MessageValidationError(
                f"不能给自己发消息：{self.from_agent}"
            )

        if self.payload is None:
            self.payload = {}
        if not isinstance(self.payload, Mapping):
            raise MessageValidationError(
                f"payload 必须是字典，收到 {type(self.payload).__name__}"
            )
        # 拷一份，避免外部持有引用后偷偷改掉消息内容
        self.payload = dict(self.payload)

        if self.extensions is None:
            self.extensions = {}
        if not isinstance(self.extensions, Mapping):
            raise MessageValidationError(
                f"extensions 必须是字典，收到 {type(self.extensions).__name__}"
            )
        self.extensions = dict(self.extensions)

    # ------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------
    @property
    def text(self) -> str:
        """
        取文本内容。

        这是最常用的字段，所以给个快捷方式：
            message.text  而不是  message.payload.get("text", "")
        """
        value = self.payload.get("text", "")
        return str(value) if value is not None else ""

    @property
    def task_id(self) -> Optional[str]:
        """
        关联的任务 ID。

        【为什么现在就留这个字段】
            接下来几步的消息（TASK_OFFER / SUBMIT / PAYMENT_RESULT）
            都要挂到某个任务上。
            把它做成一个正式的属性，而不是 payload 里的随手一个键，
            能提醒所有开发者：消息和任务是要关联起来的。
        """
        value = self.payload.get("task_id")
        return str(value) if value else None

    @property
    def workspace_id(self) -> Optional[str]:
        """
        关联的 Task Workspace ID。

        【本步骤不实现权限控制，只留数据接口】
            未来流程是：
                Task → Workspace Manager → 受控 Workspace → 有限权限
            Agent 拿到 task_id/workspace_id 之后，
            要通过 Workspace Gateway 去访问，而不是直接摸文件系统。
            现在把字段留好，将来不用改消息结构。
        """
        value = self.payload.get("workspace_id")
        return str(value) if value else None

    def is_of_type(self, message_type: str) -> bool:
        """类型判断。写成方法是为了将来能加「类型族」之类的语义。"""
        return self.message_type == str(message_type).upper()

    # ------------------------------------------------------------
    # 回复
    # ------------------------------------------------------------
    def reply(
        self,
        payload: Optional[Mapping[str, Any]] = None,
        message_type: str = MessageType.TEXT,
        text: Optional[str] = None,
    ) -> "Message":
        """
        造一条回复消息，自动对调收发双方、记下 reply_to。

        为什么要自动化这件事？
            手工写回复很容易把 from/to 写反 ——
            而这种错误在本地测试时不明显，到了网络上就是消息石沉大海。
        """
        body: Dict[str, Any] = dict(payload or {})
        if text is not None:
            body["text"] = text

        return Message(
            from_agent=self.to_agent,
            to_agent=self.from_agent,
            message_type=message_type,
            payload=body,
            reply_to=self.message_id,
            # 【关键】回复继承原消息的链路 ID。
            # 不继承的话，一次任务的来回消息在日志里就是互不相干的孤儿，
            # 你永远拼不出完整链路。
            trace_id=self.trace_id,
            conversation_id=self.conversation_id,
        )

    # ------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------
    # 本版本认识的字段。其余一律进 extensions（前向兼容）
    _KNOWN_FIELDS = (
        "message_id",
        "from_agent",
        "to_agent",
        "message_type",
        "timestamp",
        "payload",
        "trace_id",
        "conversation_id",
        "reply_to",
        "protocol_version",
    )

    def to_dict(self) -> Dict[str, Any]:
        """
        转成纯字典（可以直接 json.dumps）。

        注意 extensions 会被【展开】到顶层，
        而不是嵌在 "extensions" 键下面。
        这样升级后的节点发来的新字段，在旧节点眼里
        就是「多出来的顶层键」，转发时原样带出去即可。
        """
        data: Dict[str, Any] = {
            "message_id": self.message_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "message_type": self.message_type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "protocol_version": self.protocol_version,
        }
        if self.reply_to:
            data["reply_to"] = self.reply_to

        for key, value in self.extensions.items():
            if key not in data:
                data[key] = value

        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        """
        从字典恢复。

        【关键】不认识的字段不会丢，会进 extensions。
        这样旧节点转发新格式消息时不会损坏内容。
        """
        if not isinstance(data, Mapping):
            raise MessageValidationError(
                f"消息必须是字典，收到 {type(data).__name__}"
            )

        unknown = {
            key: value
            for key, value in data.items()
            if key not in cls._KNOWN_FIELDS
        }

        payload = data.get("payload", {})
        if payload is None:
            payload = {}

        return cls(
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            message_type=data.get("message_type", MessageType.TEXT),
            payload=dict(payload) if isinstance(payload, Mapping) else payload,
            message_id=data.get("message_id") or new_message_id("msg"),
            timestamp=data.get("timestamp") or utc_now_iso(),
            trace_id=data.get("trace_id") or new_message_id("trc"),
            conversation_id=data.get("conversation_id") or new_message_id("conv"),
            reply_to=data.get("reply_to"),
            protocol_version=str(data.get("protocol_version") or PROTOCOL_VERSION),
            extensions=unknown,
        )

    def to_json(self, **kwargs: Any) -> str:
        """
        转成 JSON 字符串。

        默认 ensure_ascii=False：中文直接以中文出现，不要变成 \\uXXXX。
        原因有两个：
            · 日志里能直接读懂
            · 同样内容字节数更少（虽然 Step 3 不在乎，但习惯要好）
        """
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_json(cls, text: str) -> "Message":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MessageValidationError(f"消息不是合法 JSON：{exc}") from exc

        if not isinstance(data, Mapping):
            raise MessageValidationError(
                f"JSON 顶层必须是对象，实际是 {type(data).__name__}"
            )
        return cls.from_dict(data)

    # ------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """给日志和 Demo 用的一行摘要。"""
        return {
            "message_id": self.message_id,
            "from": self.from_agent,
            "to": self.to_agent,
            "type": self.message_type,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "conversation_id": self.conversation_id,
            "reply_to": self.reply_to,
        }

    def __str__(self) -> str:
        preview = self.text[:30].replace("\n", " ")
        if len(self.text) > 30:
            preview += "…"
        return (
            f"[{self.message_type}] {self.from_agent} → {self.to_agent}"
            + (f": {preview}" if preview else "")
        )

    def __repr__(self) -> str:
        return (
            f"Message(id={self.message_id!r}, "
            f"{self.from_agent}→{self.to_agent}, {self.message_type})"
        )
