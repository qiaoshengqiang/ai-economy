"""
Action Feedback → **Behavior** —— 离线行为回归测试
=====================================================

【它证明什么、不证明什么】

    ✅ 证明：**反馈在行为上是充分的**。
       同一个决策程序，唯一自变量是「上一次 CREATE 的失败有没有到达
       发给 provider 的 system」：
           反馈到达 → 第二轮产生**不同的** ActionIntent
           反馈不到 → 第二轮**重复**同一个已经失败的 CREATE

    ❌ **不**证明：真实 LLM 会改变行为。
       那只能由真实实验回答（本文件全程离线、不联网、不调 API）。

【为什么这样设计才是诚实的】

    「第二轮 ActionIntent 与第一轮不同」这件事，如果由测试**写死**
    （脚本 provider 直接返回 append），那它只证明了测试作者会打字。

    所以这里的 provider 是一个**确定性策略**，但它**读自己收到的 system**：

        轮次 1  → system 里没有失败记录  → 发 CREATE
        轮次 2  → system 里有失败记录    → 发一个**新的**动作

    「读 system」这一步就是 Agent cognition。于是：
        · 主测试   = 有反馈   → 行为不同
        · 反向测试 = 同一策略、同一代码，只把反馈拿掉 → 行为不变
    差异**只能**来自反馈。

【🔴 严格边界（任务书明确要求）】

    ❌ 不规定第二轮必须 READ（这个动作在当前架构里根本不存在）
    ❌ 不规定必须 APPEND —— 参数化了 4 种**不同的**合理新动作
    ❌ 不在 Runtime / Gateway / Executor 里自动改策略
       （有 `test_action_feedback_loop.py::r6` 钉住这一点）
    ✅ 只要求：第二轮**不重复同一个已经失败的 CREATE**
    ✅ Agent 决定**不动手**（reply）也算合法的新行为
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from actions import ActionDispatcher, WorkspaceWriteExecutor   # noqa: E402
from cognition import CognitiveContextBuilder                  # noqa: E402
from experience import ExperienceStore                         # noqa: E402
from llm.base import LLMResponse                               # noqa: E402
from services.artifacts import write_artifact                  # noqa: E402
from tests.workspace_harness import WorkspaceHarness           # noqa: E402

W = "WS-BEHAVIOR-001"
AGENT = "novel_plot_architect"
OTHER = "novel_world_builder"
AGENTS = [AGENT, OTHER]

#: 第一轮要 CREATE 的目标 —— 它**预先就存在**（模拟真实事件）
PATH = "plot/test.md"


# ================================================================
# 确定性策略 provider —— 它读 system，然后决定
# ================================================================
_FIRST_ROUND_CREATE = {
    "decision": "act",
    "reasoning": "第一轮：我要建立 plot/test.md",
    "action": {"type": "workspace_write",
               "parameters": {"path": PATH, "content": "第一版内容",
                              "write_mode": "create"}},
}

#: 第二轮可以选的新动作 —— **刻意做成 4 种完全不同的形状**，
#: 就是为了让测试断言不依赖任何一种具体策略。
ALTERNATIVES = {
    "append_same_path": {
        "decision": "act", "reasoning": "上次 create 被拒，改成追加",
        "action": {"type": "workspace_write",
                   "parameters": {"path": PATH, "content": "第二版内容",
                                  "write_mode": "append"}}},
    "replace_same_path": {
        "decision": "act", "reasoning": "上次 create 被拒，改成整体替换",
        "action": {"type": "workspace_write",
                   "parameters": {"path": PATH, "content": "第二版内容（全量）",
                                  "write_mode": "replace"}}},
    "create_other_path": {
        "decision": "act", "reasoning": "换个路径，不碰那个已存在的",
        "action": {"type": "workspace_write",
                   "parameters": {"path": "plot/test_v2.md",
                                  "content": "第二版内容",
                                  "write_mode": "create"}}},
    "no_action_reply": {
        "decision": "reply", "reasoning": "先不动手，等确认清楚再说"},
}


def _context_says_my_create_failed(system: str, path: str) -> bool:
    """
    **Agent 的决策程序读 Context 的那一段。**

    🔒 只认**结构化事实**，不做关键词联想：
       同一行里同时出现 target=<path> / write_mode=create，且标记为 failed。
    """
    for line in (system or "").splitlines():
        if f"target={path}" not in line:
            continue
        if "write_mode=create" not in line:
            continue
        if "[action_result] failed" in line:
            return True
    return False


class PolicyProvider:
    """
    确定性、离线的「LLM」—— 但它**读自己收到的 system**。

    🔒 它是**固定**的：整个测试里唯一的自变量是「反馈有没有到达 system」。
    """

    def __init__(self, alternative: dict) -> None:
        self.alternative = dict(alternative)
        self.seen: list = []

    def generate(self, messages=None, system=None, **kwargs):     # noqa: ARG002
        system = system or ""
        self.seen.append({"messages": messages, "system": system})
        envelope = (self.alternative
                    if _context_says_my_create_failed(system, PATH)
                    else _FIRST_ROUND_CREATE)
        return LLMResponse(
            provider="fake", model="fake",
            content=json.dumps(envelope, ensure_ascii=False),
            input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=1.0, simulated=True,
        )


class FakeRouter:
    """与 LLMRouter 的公开契约同形（与 tests/test_action_seam.py 一致）。"""

    def __init__(self, provider) -> None:
        self.provider = provider

    def get_provider(self, name, credential=None):                # noqa: ARG002
        return self.provider

    def generate(self, messages=None, provider=None, model=None,
                 system=None, **kwargs):                          # noqa: ARG002
        return self.provider.generate(messages=messages, system=system,
                                      **kwargs)


class StubAgent:
    """
    轻量 Agent —— 但**复用真实的 `BaseAgent.act`**。

    🔒 用真实实现而不是在测试里重写一遍逻辑，否则测的就不是产品代码。
       （与 `tests/test_action_seam.py::StubAgent` 同一手法。）
    """

    def __init__(self, agent_id: str, brain) -> None:
        self.agent_id = agent_id
        self.brain = brain

    def generate(self, messages=None, **kwargs):
        return self.brain.generate(messages=messages, **kwargs)

    act = __import__("agents.base_agent", fromlist=["BaseAgent"]).BaseAgent.act


# ================================================================
# 夹具：真实 Gateway + 真实 ExperienceStore + 真实 Dispatcher
# ================================================================
@pytest.fixture()
def rig(tmp_path):
    """
    环境完全真实，只有 LLM 是确定性的：

        WorkspaceGateway（生产同构）· ExperienceStore（真表）
        ActionDispatcher + WorkspaceWriteExecutor（真实现）
        Brain + CognitiveContextBuilder（真实现，接线与 main.py 同构）
    """
    h = WorkspaceHarness.on_file(
        tmp_path / "behavior.db",
        participants={W: set(AGENTS)},
        agents=set(AGENTS) | {"outsider"},
    )
    db = h.new_db()
    gw = h.gateway(db)
    gw.ensure_workspace(W, OTHER)
    gw.create(W, "/", "plot", "directory", OTHER)
    gw.handoff(W, "/plot", from_agent=OTHER, to_agent=AGENT, actor=OTHER,
               reason="test namespace ownership")

    # ---- 前置：`plot/test.md` **已经存在**（模拟真实事件的起点）----
    #
    # 🔒 这是**测试环境的初始状态**，不是 Agent 的一次行动 ——
    #    所以直接写，**不**记进 Experience（第二轮之前它的经历必须是空的）。
    setup = write_artifact(gw, W, AGENT, PATH, "预先存在的初稿",
                           write_mode="create")
    assert setup["ok"] is True, f"夹具初始化失败：{setup}"

    ex = ExperienceStore(db)
    dispatcher = ActionDispatcher([
        WorkspaceWriteExecutor(write_artifact, gateway=gw, workspace_id=W)])

    r = SimpleNamespace(h=h, db=db, gw=gw, ex=ex, dispatcher=dispatcher)
    try:
        yield r
    finally:
        try:
            db.close()
        except Exception:                                         # noqa: BLE001
            pass


# ================================================================
# 跑两轮真实 cognition
# ================================================================
def _two_rounds(rig, alternative: dict, *, record: bool = True,
                break_render: bool = False, monkeypatch=None):
    """
    同一个 Agent，思考两轮。返回 (run1, run2, [system1, system2])。

    自变量只有三个，全部在**反馈通道**上：
        record=False        → ActionResult 不进 Experience
        break_render=True   → 进了 Experience，但 Context 不渲染它
    """
    from agents.brain import Brain, BrainSettings

    if break_render:
        from cognition import context as ctx_mod
        assert monkeypatch is not None
        monkeypatch.setattr(ctx_mod, "_experience_detail", lambda e: "")

    prov = PolicyProvider(alternative)
    brain = Brain(
        FakeRouter(prov),
        BrainSettings(agent_id=AGENT),
        context_builder=CognitiveContextBuilder(None, experience_store=rig.ex),
        default_context_query={},          # {} 而不是 None —— 否则静默不建 Context
    )
    agent = StubAgent(AGENT, brain)
    if record:
        # 与 BaseAgent.attach_self_model_services() 等价的那一行
        agent.experience_store = rig.ex

    run1 = agent.act(messages="开始这一轮生产", dispatcher=rig.dispatcher)
    run2 = agent.act(messages="继续", dispatcher=rig.dispatcher)

    assert len(prov.seen) == 2, "两轮 cognition 应该产生两次 provider 调用"
    return run1, run2, [s["system"] for s in prov.seen]


def _repeats_the_failed_create(run1, run2) -> bool:
    """
    第二轮是不是**又发了同一个已经失败的 CREATE**？

    🔒 判定刻意很窄 —— 只回答这一个问题：
       同一 action_type + 同一 path + 又一次 write_mode=create。

    ❌ 不判断「它有没有选对」：READ / APPEND / REPLACE / 换路径 / 不动手
       全都算「没有重复」。
    """
    i1, i2 = run1.action_intent, run2.action_intent
    if i2 is None:
        return False                      # 第二轮决定不动手 —— 不是重复
    if i1 is None:                        # pragma: no cover
        return False
    return (i2.action_type == i1.action_type
            and str(i2.parameters.get("path")) == str(i1.parameters.get("path"))
            and str(i2.parameters.get("write_mode")).strip().lower() == "create")


# ================================================================
# 1. 第一轮：真实的 CREATE + 真实的 Gateway 拒绝
# ================================================================
class TestRoundOneIsARealFailure:

    def test_01_round_one_create_is_really_rejected_by_the_gateway(self, rig):
        run1, _, systems = _two_rounds(
            rig, ALTERNATIVES["append_same_path"])

        # 第一轮的 decision 来自**真实**的 actions/parse.py 信封解析
        assert run1.action_intent is not None
        assert run1.action_intent.action_type == "workspace_write"
        assert run1.action_intent.parameters["path"] == PATH
        assert run1.action_intent.parameters["write_mode"] == "create"

        # 第一轮的结果来自**真实**的 WorkspaceGateway
        assert run1.action_result is not None
        assert run1.action_result.success is False
        assert run1.action_result.metadata["code"] == "already_exists"

        # 第一轮发送的 system 里**还没有**这个失败（它刚刚才发生）
        assert "already_exists" not in systems[0]
        assert f"target={PATH}" not in systems[0]


# ================================================================
# 2. 主测试：第二轮看到反馈，**不再重复**那个失败的 CREATE
# ================================================================
class TestFeedbackChangesTheNextAction:

    @pytest.mark.parametrize("alt_name", sorted(ALTERNATIVES))
    def test_02_round_two_does_not_repeat_the_failed_create(
            self, rig, alt_name):
        run1, run2, systems = _two_rounds(rig, ALTERNATIVES[alt_name])
        second = systems[1]

        # ---- ① 第二轮**真的看到了**第一轮的行动结果（四项事实齐全）----
        assert f"target={PATH}" in second,        "没看到 target"
        assert "action=workspace_write" in second, "没看到 action"
        assert "[action_result] failed" in second, "没看到 failure"
        assert "already_exists" in second,         "没看到 ALREADY_EXISTS"

        # ---- ② 第二轮**没有重复**同一个已经失败的 CREATE ----
        assert _repeats_the_failed_create(run1, run2) is False, (
            f"第二轮又发了一次无效的 CREATE：{run2.action_intent}")

        # ---- ③ 如果它动手了，新的行动必须是**真的执行过**的 ----
        if run2.action_intent is None:
            assert run2.action_result is None      # 决定不动手 —— 合法
        else:
            assert run2.action_result is not None
            assert run2.action_result.success is True, (
                f"第二轮换了动作但仍然失败：{run2.action_result.error}")

    def test_03_round_two_facts_are_structured_not_guessed(self, rig):
        """
        第二轮看到的是**结构化**事实，不是靠自然语言猜出来的。

        🔒 同时如实记录一个边界：Gateway 的原始错误码 `NAME_CONFLICT`
           **没有**被持久化（`ActionResult.metadata["gateway_code"]` 未入库），
           所以 Context 里只有 Artifact 层的 `already_exists`。
        """
        _, _, systems = _two_rounds(rig, ALTERNATIVES["append_same_path"])
        second = systems[1]

        assert "code=already_exists" in second
        assert "write_mode=create" in second
        # ⚠️ 这是**事实**，不是断言失败：Gateway 原始码没有落到 Experience
        assert "NAME_CONFLICT" not in second, (
            "如果 NAME_CONFLICT 出现了，说明 record_action_result 的行为变了 —— "
            "请同步更新本测试与 ACTION_FEEDBACK_IMPLEMENTATION.md §8①")


# ================================================================
# 3. 反向测试：同一策略、同一代码，只把反馈拿掉 → 行为**不变**
# ================================================================
class TestReverseTheBehaviorChangeComesFromTheFeedback:

    def test_r1_without_experience_record_round_two_repeats(self, rig):
        """
        同一策略程序、同一代码路径，只是 **ActionResult 不落 Experience**
        → 第二轮 system 里没有失败 → 策略再次发 CREATE → **真的重复了**。

        🔒 这就是主测试的「牙」：行为差异只可能来自反馈。
        """
        run1, run2, systems = _two_rounds(
            rig, ALTERNATIVES["append_same_path"], record=False)

        assert run1.action_result.success is False          # 第一轮照样失败
        assert rig.ex.count(AGENT) == 0                     # 只是没记
        assert "already_exists" not in systems[1]           # 第二轮看不到

        assert _repeats_the_failed_create(run1, run2) is True, (
            "拿掉反馈之后居然没重复 —— 说明主测试的差异不来自反馈")
        assert run2.action_result.success is False
        assert run2.action_result.metadata["code"] == "already_exists"

    def test_r2_without_context_rendering_round_two_repeats(
            self, rig, monkeypatch):
        """
        反馈**进了** Experience（表里有行），但 Context **不渲染**它
        → 第二轮照样看不到 → 照样重复。

        🔒 证明第 4 步（`to_text()`）是必需的，不是装饰。
        """
        run1, run2, systems = _two_rounds(
            rig, ALTERNATIVES["append_same_path"],
            break_render=True, monkeypatch=monkeypatch)

        # 🔴 两轮**都**失败了，所以**两条**经历都在表里 ——
        #    这正是「反馈没被渲染」的后果：Agent 撞了第二次墙，
        #    而第二次撞墙又被如实记了下来。
        assert rig.ex.count(AGENT) == 2, "两轮失败都该被记下来"
        assert "already_exists" not in systems[1], "渲染被关掉了却还看得见"

        assert _repeats_the_failed_create(run1, run2) is True
        assert run2.action_result.metadata["code"] == "already_exists"

    def test_r3_the_policy_really_reads_the_system(self, rig):
        """
        反向测试的**前提校验**：这个策略程序确实在读 system。

        🔒 否则主测试可能是「策略本来就总会换动作」——
           那样它就什么都没证明。
        """
        _, _, systems = _two_rounds(rig, ALTERNATIVES["append_same_path"])

        # 第一轮 system 里没有失败事实 → 策略读出来是 False
        assert _context_says_my_create_failed(systems[0], PATH) is False
        # 第二轮 system 里有 → 策略读出来是 True
        assert _context_says_my_create_failed(systems[1], PATH) is True

        # 而且两轮的 system **确实不同**（不是同一个上下文被复用）
        assert systems[0] != systems[1]
