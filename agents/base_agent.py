"""
BaseAgent —— 经济主体的骨架
============================================================

【最重要的一句话】Agent 不等于 LLM。

看看这个类里有什么：
    Identity       —— 我是谁
    Wallet         —— 我有多少钱
    Workspace      —— 我能碰哪些文件
    Memory         —— 我记得什么
    Brain          —— 我用什么智力（注意：是「资源」，不是「我」）
    Reputation     —— 别人怎么看我

这些才是 Agent 的本体。
LLM 只是 Brain 底下可以更换的一个零件。

所以本文件里【不会出现任何一个模型厂商的名字】。
没有 DeepSeek、没有 Doubao、没有 Grok。
Agent 只会说 brain.generate(...)，
至于这句话背后是哪个公司的模型，Agent 不关心、也不该关心。

这样带来的好处：
    明天 DeepSeek 涨价了，或者某个厂商挂了，
    只需要改 config，Agent 代码一行都不用动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.config import AgentConfig
from core.identity import AgentIdentity
from core.logging_setup import get_logger
from core.wallet import Wallet, WalletRepository
from core.workspace import Workspace

logger = get_logger(__name__)


# ============================================================
# 声誉
# ============================================================
@dataclass
class Reputation:
    """
    声誉。v0.1 故意做得很简单。

    只有三个最朴素的计数。
    复杂的评分公式（成功率、返工率、响应时间、客户满意度）
    等到经济系统跑通、有真实数据以后再设计 ——
    否则就是拍脑袋定系数，没有意义。
    """

    score: float = 0.0
    completed_tasks: int = 0
    failed_tasks: int = 0

    @property
    def total_tasks(self) -> int:
        return self.completed_tasks + self.failed_tasks

    @property
    def success_rate(self) -> Optional[float]:
        """成功率。还没做过任务时返回 None（而不是 0，避免误导）。"""
        if self.total_tasks == 0:
            return None
        return self.completed_tasks / self.total_tasks

    def record_success(self, score_delta: float = 1.0) -> None:
        self.completed_tasks += 1
        self.score += score_delta

    def record_failure(self, score_delta: float = 1.0) -> None:
        self.failed_tasks += 1
        self.score -= score_delta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "success_rate": self.success_rate,
        }

    def __str__(self) -> str:
        rate = self.success_rate
        rate_str = "n/a" if rate is None else f"{rate:.0%}"
        return (
            f"score={self.score:g}, "
            f"done={self.completed_tasks}, failed={self.failed_tasks}, "
            f"success={rate_str}"
        )


# ============================================================
# Agent 状态
# ============================================================
class AgentStatus:
    """Agent 的存活状态。"""

    ACTIVE = "ACTIVE"          # 正常工作
    INSOLVENT = "INSOLVENT"    # 破产：没钱了，不能承担付不起的责任
    SUSPENDED = "SUSPENDED"    # 被暂停（未来仲裁用）

    ALL = {ACTIVE, INSOLVENT, SUSPENDED}


# ============================================================
# BaseAgent
# ============================================================
class BaseAgent:
    """
    所有 Agent 的基类。

    v0.1 阶段它主要是一个「组装好的骨架」。
    后面几个阶段会往里面长东西：
        Step 2: brain（LLM Router）
        Step 3: communication（消息收发）
        Step 4: task / contract / escrow
        Step 5: 完整的经济决策循环
    """

    def __init__(
        self,
        identity: AgentIdentity,
        config: AgentConfig,
        wallet: Wallet,
        workspace: Workspace,
        reputation: Optional[Reputation] = None,
        status: str = AgentStatus.ACTIVE,
    ):
        self.identity = identity
        self.config = config
        self.wallet = wallet
        self.workspace = workspace
        self.reputation = reputation or Reputation()
        self.status = status

        # 大脑先占位。Step 2 会把它换成真正的 LLM Router。
        # 现在放 None，这样「Agent 有没有大脑」就是可检测的状态，
        # 而不是塞一个假的进去掩盖问题。
        self.brain: Optional[Any] = None

        # 记忆（Step 3 之后会变成更结构化的东西）
        self._short_term_memory: List[Dict[str, Any]] = []

    # ------------------------------------------------------------
    # 基本属性
    # ------------------------------------------------------------
    @property
    def agent_id(self) -> str:
        return self.identity.agent_id

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def balance(self) -> float:
        """余额。注意这是向账本实时查询的结果，不是缓存字段。"""
        return self.wallet.balance

    @property
    def is_insolvent(self) -> bool:
        """
        是否破产。

        【设计说明】破产是经济状态，不是程序错误。
        Agent 破产后依然存在、依然有记忆，
        只是不能再承担它付不起的经济责任了。
        未来这里会扩展出：贷款、抵押、信用、破产重组。
        """
        return self.wallet.is_insolvent

    @property
    def can_take_financial_risk(self) -> bool:
        """
        能不能承担经济责任？

        所有「要花钱」或者「承诺交付」的动作，都应该先问这个。
        这是防止 Agent 承诺自己做不到的事情的第一道闸门。
        """
        return self.status == AgentStatus.ACTIVE and not self.is_insolvent

    # ------------------------------------------------------------
    # 记忆
    # ------------------------------------------------------------
    def remember(self, event: str, **details: Any) -> None:
        """
        记住一件事（短期记忆，进程内）。

        长期记忆（写文件、写数据库）留给后面的阶段。
        现在先让 Agent 有一个「我刚才干了啥」的缓冲区，
        这对调试和理解 Agent 的行为非常有帮助。
        """
        from core.identity import utc_now_iso

        entry = {"event": event, "at": utc_now_iso(), **details}
        self._short_term_memory.append(entry)

        # 只保留最近 100 条，防止内存无限增长
        if len(self._short_term_memory) > 100:
            self._short_term_memory = self._short_term_memory[-100:]

    @property
    def memory(self) -> List[Dict[str, Any]]:
        """只读的短期记忆。"""
        return list(self._short_term_memory)

    def recall(self, event: str) -> List[Dict[str, Any]]:
        """按事件名检索记忆。"""
        return [entry for entry in self._short_term_memory if entry["event"] == event]

    # ------------------------------------------------------------
    # 大脑（Step 2 实现）
    # ------------------------------------------------------------
    def attach_brain(self, brain: Any) -> None:
        """
        给 Agent 装上大脑。

        注意这个方法接收的是「任何符合 LLM 接口的对象」，
        所以它可以是一个 mock、一个 DeepSeek、或者以后一个本地小模型。
        Agent 完全不关心具体是哪个。
        """
        self.brain = brain
        logger.debug("%s 的大脑已挂载：%s", self.name, type(brain).__name__)

    @property
    def has_brain(self) -> bool:
        return self.brain is not None

    def generate(self, messages: Any = None, **kwargs: Any) -> Any:
        """
        Agent 思考一次。

        这是 Agent 对外表达「我要用一下智力」的唯一入口。
        注意它只跟 self.brain 说话，不知道背后是哪家模型：

            agent.generate(messages="帮我分析这个任务")
                ↓
            Brain → Router → Provider

        调用成功会顺手记进短期记忆，方便回头 debug
        「我刚才为什么这么决定」。

        返回值是统一的 LLMResponse（含 token 用量）。
        为什么不让这里直接返回字符串？
            因为 token 用量是经济信息，Agent 迟早要拿它算成本。
            如果这里就把响应拍扁成字符串，那些信息就丢了。
            只想拿文本的场合，用下面的 think()。
        """
        if self.brain is None:
            raise RuntimeError(
                f"{self.name} 还没有大脑，无法思考。\n"
                f"请先用 attach_brain(brain) 给它装一个。"
            )

        response = self.brain.generate(messages=messages, **kwargs)

        # 记一笔：这次思考的元信息（不含内容，避免记忆膨胀）
        self.remember(
            "brain_generate",
            provider=getattr(response, "provider", None),
            model=getattr(response, "model", None),
            total_tokens=getattr(response, "total_tokens", None),
        )

        return response

    def think(self, prompt: str, **kwargs: Any) -> str:
        """
        只想拿到文本时用这个。

        代价是丢掉 token 用量 —— 所以真正要做经济决策时，
        请直接用 generate()。
        """
        return self.generate(messages=prompt, **kwargs).content

    # ------------------------------------------------------------
    # 状态变化
    # ------------------------------------------------------------
    def update_status(self) -> str:
        """
        根据经济状况刷新自己的状态。

        v0.1 规则极简：没钱了就是 INSOLVENT。
        必须保留这个概念 —— 因为它是整个「Agent 要靠经济生存」
        这个设想的落脚点。
        """
        if self.status != AgentStatus.SUSPENDED:
            if self.is_insolvent:
                if self.status != AgentStatus.INSOLVENT:
                    logger.warning(
                        "%s 已破产（余额 %.4f %s），状态切换为 INSOLVENT",
                        self.name,
                        self.balance,
                        self.wallet.currency,
                    )
                self.status = AgentStatus.INSOLVENT
            else:
                self.status = AgentStatus.ACTIVE
        return self.status

    # ------------------------------------------------------------
    # 自述
    # ------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """
        输出自己的完整状态。启动横幅和各种调试输出都用它。
        """
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "status": self.status,
            "balance": self.balance,
            "currency": self.wallet.currency,
            "insolvent": self.is_insolvent,
            "provider": self.config.provider,
            "model": self.config.model,
            "skills": self.config.skills,
            "reputation": self.reputation.to_dict(),
            "has_brain": self.has_brain,
            "workspace": str(self.workspace.root),
        }

    def __str__(self) -> str:
        return f"{self.name} [{self.status}] {self.balance:g} {self.wallet.currency}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(agent_id={self.agent_id!r}, "
            f"status={self.status!r}, balance={self.balance})"
        )
