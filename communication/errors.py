"""
通信层的错误体系
============================================================

【设计原则】

    通信失败 ≠ Agent 崩溃。

    如果 Agent B 不存在，调用方应该收到一个明确的 AgentNotFoundError，
    而不是从字典里冒出一个 KeyError。
    这两者的区别在于：
        KeyError          → 「代码有 bug，快去修」
        AgentNotFoundError → 「对方不在，这是正常的运行时状况」

    在分布式环境里，「对方不在」是常态，不是 bug。
    所以它必须是一个可预期、可处理的异常。

【为什么异常这么少】

    这里刻意只留了必要的几个。
    异常体系一旦膨胀，调用方就只能写一堆 except，
    最后还是 `except Exception` 兜底 —— 等于没有分类。

    宁可少而准。
"""

from __future__ import annotations


class CommunicationError(Exception):
    """通信层的错误基类。所有通信相关异常都继承它。"""


# ============================================================
# 寻址相关
# ============================================================
class AgentNotFoundError(CommunicationError):
    """
    找不到这个 Agent。

    【通信场景里这是常态，不是 bug】
        未来的网络环境里，对方可能：
            · 没启动
            · 已经下线
            · 在另一个节点上但还没被发现
            · 地址过期了

        所以这是一个「可预期的运行时状况」，
        调用方应该能优雅处理，而不是崩溃。
    """


class InvalidAddressError(CommunicationError):
    """地址格式不对（比如 local:// 里混进了 IP，或者 scheme 不认识）。"""


# ============================================================
# 注册相关
# ============================================================
class DuplicateAgentError(CommunicationError):
    """同一个 agent_id 被注册了两次。"""


# ============================================================
# 消息相关
# ============================================================
class MessageValidationError(CommunicationError):
    """
    消息字段不合法。

    比如缺 from_agent、message_type 是空的、payload 不是字典。
    这类问题应该在【发送前】就拦住，而不是等对方收到才发现。
    """


class DuplicateMessageError(CommunicationError):
    """
    重复消息。

    【默认不会抛这个异常】
        CommunicationManager 默认对重复消息采取「检测并忽略」的策略 ——
        因为网络环境下重复投递是常态，为它抛异常会让调用方
        到处写 try/except。

        这个异常是给「严格模式」用的：
            manager.receive(agent_id, strict_dedup=True)
        测试和调试时想知道「是不是有重复」就用它。
    """


# ============================================================
# 传输相关
# ============================================================
class TransportError(CommunicationError):
    """底层传输出了问题的基类。"""


class DeliveryError(TransportError):
    """
    消息没能送达。

    和 AgentNotFoundError 的区别：
        AgentNotFoundError → 找不到收件人（根本不知道该往哪送）
        DeliveryError      → 知道该往哪送，但送失败了
                             （连接断了、超时了、队列满了）
    """


class TransportClosedError(TransportError):
    """传输层已经关闭，不能再收发消息。"""
