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

        # 通信（Step 3）。
        # 注意这里存的是 CommunicationManager，不是 Transport。
        # Agent 永远看不到 LocalTransport —— 它只知道「我能给人发消息」，
        # 不知道消息是怎么过去的。这和 brain 的处理方式完全一致：
        # 都是「拿到一个抽象门面，不关心底层实现」。
        self.communication: Optional[Any] = None

        # 收到过的消息（短期备忘）。
        # 真正的消息去重和持久化在通信层，这里只是 Agent 自己的记录。
        self._received_messages: List[Any] = []
        self._message_handler: Optional[Any] = None

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
    # Private Workspace（V1 最小接线）
    # ------------------------------------------------------------
    #
    # 【这一节在做什么，以及**不**在做什么】
    #
    #   做：把**已经存在**的 `self.workspace` 能力接到 Agent 身上。
    #   不做：Tool Calling · Capability Framework · 新的 Memory 架构。
    #
    # 【🔒 为什么这些方法是纯转发】
    #
    #   路径安全（绝对路径 / .. / symlink / containment）已经在
    #   `core/workspace.py` 的 `_resolve()` / `memory_path()` 里
    #   定义好了，而且**只有那一个定义**。
    #
    #   如果 Agent 这里再判断一次路径，就会出现**两套安全语义** ——
    #   而两套语义迟早会跑偏（审计里 memory_path 就是这么跑偏的）。
    #
    #   所以这里的每个方法都只有一行：转给 self.workspace。
    #
    # 【🔒 Agent 只看到逻辑路径】
    #
    #   Agent 传的是 "notes/test.txt"，不是
    #   "/Users/.../agents/agent_a/workspace/notes/test.txt"。
    #   物理路径**不出现在 Agent 的任何接口上**。

    def write_private(self, path: str, content: str):
        """
        写自己的私有文件（自动建父目录）。

        🔒 只能写自己 —— 越界由 Workspace 拒绝，不由这里判断。
        """
        return self.workspace.write_text(path, content)

    def read_private(self, path: str) -> str:
        """读自己的私有文件。"""
        return self.workspace.read_text(path)

    def exists_private(self, path: str) -> bool:
        """自己的私有文件是否存在（越界一律返回 False）。"""
        return self.workspace.exists(path)

    def delete_private(self, path: str) -> bool:
        """删自己的私有文件。返回是否真的删掉了。"""
        return self.workspace.delete(path)

    def list_private(self, pattern: str = "**/*") -> List[str]:
        """列出自己的私有文件（返回**逻辑**相对路径）。"""
        return self.workspace.list_files(pattern)

    # ---- 长期记忆：脱离 Python 进程内存，落成自己的持久资产 ----

    def remember_long(self, name: str, content: str):
        """
        🔒 **长期记忆**（与 `remember()` 的短期记忆相对）。

            remember()      → 进程内 list，重启即失
            remember_long() → 写进 Private Workspace 的 memory 目录，重启仍在

        刻意保持极薄：没有 embedding / 检索 / 排序 / 摘要 / 生命周期。
        那些是 **Memory Architecture**，本次明确不做。
        """
        return self.workspace.write_memory(name, content)

    def read_long(self, name: str) -> str:
        """读回一条长期记忆。"""
        return self.workspace.read_memory(name)

    def list_long(self) -> List[str]:
        """列出长期记忆的名字。"""
        return self.workspace.list_memories()

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

        🔴 **认知输出被截断时，这里记一条关键观察。**
           理由见 `_record_truncation_if_needed()`。
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

        # 🔴 **认知输出被截断 → 记成关键观察**（Critical Observation）。
        #
        #    ⚠️ 这段以前只在 `act()` 里。但**生产实验走的是 `generate()`**
        #       （见 experiments/heterogeneous_production/production.py），
        #       于是截断事实在真实生产里**从来没有被记录过** ——
        #       `cognitive_status=TRUNCATED` 只是躺在返回值里没人看。
        #       实测证明：generate() 走 11 轮 length，Experience 表 0 条。
        #
        #    🔒 放在 `generate()` 是**唯一不重复**的位置：
        #       `act()` 内部经 `run_cognition()` → `self.generate()`，
        #       所以它自动被覆盖，不需要在 `act()` 里再记一次。
        self.last_call_id = getattr(self.brain, "last_call_id", None)
        self.last_request_evidence = getattr(
            self.brain, "last_request_evidence", None)
        # 🔒 与上面两个对称：调用方（实验 / CLI）需要这次调用的
        #    token 用量与 model 名，而那只有 response 上有。
        #    `Brain.last_response` 早就有，这里只是**把它暴露到 Agent 层** ——
        #    否则调用方要穿透到 `agent.brain` 去取。
        self.last_response = response

        self._record_truncation_if_needed(response, **kwargs)

        return response

    def _record_truncation_if_needed(self, response: Any, **kwargs: Any) -> None:
        """
        认知输出被截断时，记一条 **Critical Observation Experience**。

        🔒 为什么走 Experience：这是已有的「事实 → 下一轮可见」通道。
           不为一个状态新建 CognitiveTurnStore，也不新建表。
           下一轮 `CognitiveContext.critical_observations` 会带上它。

        🔒 **只记事实，不写策略** —— 不写「你必须重试」。
           那属于 Agent 自己的决定（规则限制结构，策略决定行为）。

        🔒 记录失败**不能**破坏主要 Cognition。
        """
        if getattr(response, "cognitive_status", None) != "TRUNCATED":
            return

        # ============================================================
        # 🔴 §8 截断观察的**创建边界**
        # ============================================================
        #
        # 只有**认知调用**的截断才算「Agent 这一轮没想完」，
        # 才允许写进它的 Cognitive History。
        #
        # 非认知调用（utility / diagnostic）的截断**仍然**会作为一次
        # Provider 截断留在 `llm_calls` 遥测里（`finish_reason=length`
        # 是原始技术事实，不动它）—— 但它**不得**被误记成
        # Agent 的一段认知经历。
        #
        # 🔒 判据是**调用方声明的 purpose**，不是 `finish_reason`
        #    （§5.3：不得从 length 反推 cognition）。
        from llm.recorder import LLMCallRecorder as _R

        purpose = kwargs.get("call_purpose")
        if purpose != _R.PURPOSE_COGNITION:
            logger.debug(
                "截断发生在非认知调用上（purpose=%s）—— 记入 llm_calls "
                "遥测，但**不**写进 Agent Cognitive History。",
                purpose or _R.PURPOSE_UNSPECIFIED,
            )
            return
        store = getattr(self, "experience_store", None)
        if store is None:
            return
        finish_reason = getattr(response, "finish_reason", None)
        try:
            store.record(
                self.agent_id if hasattr(self, "agent_id") else self.name,
                "observation",
                result={
                    # 🔴 标记成关键观察 —— 保证进入下一轮 Context。
                    #    截断这件事如果丢了，Agent 会以为上一轮
                    #    「正常结束了」，于是基于不完整的认知继续走。
                    "critical_observation": True,
                    "observed_state": "cognitive_output_truncated",
                    "cognitive_output": "TRUNCATED",
                    "finish_reason": finish_reason,
                    "output_status": getattr(response, "output_status", None),
                    "cognitive_status": "TRUNCATED",
                },
                feedback=(
                    "上一轮认知输出未完整完成：模型输出达到 token 上限"
                    f"（finish_reason={finish_reason}）。"
                    "这一轮认知被标记为 TRUNCATED。"),
                metadata={"finish_reason": finish_reason},
            )
        except Exception as exc:                          # noqa: BLE001
            logger.warning("截断观察记录失败：%s", exc)

    def act(self, messages=None, *, dispatcher=None, execute: bool = True,
            write_service=None, **kwargs):
        """
        跑一次 Cognition，**并在 Agent 决定时执行它的 Action**（Action Seam）。

        🔒 与 generate() 的关系：
            generate()  只拿到 LLM 的文本（既有行为，**完全不变**）
            act()       在此之上解析结构化 Decision，必要时执行 Action

        §十八 向后兼容：Agent 只回文本时，act() 不调用 Executor，
                        decision.kind == "reply"。
        """
        from actions.run import run_cognition

        if dispatcher is None:
            dispatcher = getattr(self, "action_dispatcher", None)
        if write_service is None:
            write_service = getattr(self, "self_model_write_service", None)

        run = run_cognition(self, messages, dispatcher=dispatcher,
                            execute=execute, write_service=write_service,
                            **kwargs)

        # §五 / §二十三：ActionResult → Experience（**只记录，不解读**）
        store = getattr(self, "experience_store", None)
        if (store is not None and run.action_intent is not None
                and run.action_result is not None):
            try:
                run_experience = store.record_action_result(
                    self.agent_id if hasattr(self, "agent_id") else self.name,
                    run.action_intent, run.action_result)
                object.__setattr__(run, "experience", run_experience) \
                    if hasattr(run, "__dataclass_fields__") else None
            except Exception as exc:                      # noqa: BLE001
                logger.warning("Experience 记录失败：%s", exc)

        return run

    def attach_self_model_services(self, *, experience_store=None,
                                   write_service=None):
        """
        注入 Self Model 相关的生产服务（§二十）。

        🔒 Agent 只拿到**两个已经组装好的服务**，
           它不知道 SelfModelStore 在哪，也不直接碰数据库。
        """
        self.experience_store = experience_store
        self.self_model_write_service = write_service
        return self

    def think(self, prompt: str, **kwargs: Any) -> str:
        """
        只想拿到文本时用这个。

        代价是丢掉 token 用量 —— 所以真正要做经济决策时，
        请直接用 generate()。
        """
        return self.generate(messages=prompt, **kwargs).content

    # ------------------------------------------------------------
    # 通信（Step 3）
    # ------------------------------------------------------------
    def attach_communication(self, manager: Any) -> None:
        """
        给 Agent 接上通信能力。

        【为什么注入的是 manager，而不是 transport】

            如果 Agent 拿到的是 LocalTransport，那么：
                · 它就能直接操作信箱，绕过所有校验和去重
                · 换 P2PTransport 时它必须改
                · 「谁在本地、谁在远端」这种信息会漏到 Agent 里

            拿到 CommunicationManager 就没有这些问题：
            Agent 只会说「发给 agent_b」，剩下的不归它管。

        组装这件事由 Runtime（组合根）负责，Agent 自己不创建任何东西。
        """
        self.communication = manager
        logger.debug("%s 已接入通信层", self.name)

    @property
    def has_communication(self) -> bool:
        return self.communication is not None

    async def send(
        self,
        to: str,
        text: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        message_type: str = "TEXT",
        reply_to: Optional[str] = None,
    ) -> Any:
        """
        给另一个 Agent 发消息。

            await agent_a.send("agent_b", text="你好")

        【为什么这个方法是 async，而 generate() 是同步的】
            generate() 最终是一次阻塞的 HTTP 调用，同步最自然。
            而消息投递的未来形态是网络 I/O —— 天然异步。
            现在就把签名定成 async，将来换成 P2PTransport 时
            调用方一个字都不用改。

        【注意这里没有「本地/远端」的判断】
            Agent 不需要知道 agent_b 在哪台机器上。
            那是通信层的工作，而且未来会由 Transport 决定。
        """
        if self.communication is None:
            raise RuntimeError(
                f"{self.name} 还没有接入通信层，无法发消息。\n"
                f"请先由 Runtime 调用 attach_communication(manager)。"
            )

        return await self.communication.send(
            from_agent=self.agent_id,
            to=to,
            text=text,
            payload=payload,
            message_type=message_type,
            reply_to=reply_to,
        )

    def on_message(self, handler: Any) -> None:
        """
        注册一个消息处理函数。

        处理函数可以返回：
            None        不回复
            str         自动包成文本回复
            dict        自动包成 payload 回复
            Message     直接回复这条消息

        可以同步，也可以 async —— 通信层两种都支持。
        """
        if handler is not None and not callable(handler):
            raise ValueError(
                f"消息处理函数必须可调用，收到 {type(handler).__name__}"
            )
        self._message_handler = handler

    def handle_message(self, message: Any) -> Any:
        """
        收到一条消息时的默认行为。

        默认【不回复】，只记录下来。
        为什么默认不回复？
            因为「收到消息就自动回一句」是很危险的行为 ——
            将来两条 Agent 互相自动回复会形成无限循环，
            每次循环都要花钱调模型。

        需要回复的场景，请显式设置处理函数：

            agent.on_message(lambda msg: f"收到：{msg.text}")
        """
        if message is not None:
            self._received_messages.append(message)

            self.remember(
                "message_received",
                message_id=getattr(message, "message_id", None),
                from_agent=getattr(message, "from_agent", None),
                message_type=getattr(message, "message_type", None),
            )

        if self._message_handler is not None:
            return self._message_handler(message)

        return None

    @property
    def received_messages(self) -> List[Any]:
        """收到过的消息（只读副本）。"""
        return list(self._received_messages)

    def clear_received_messages(self) -> None:
        """清空收件记录（测试用）。"""
        self._received_messages.clear()

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
