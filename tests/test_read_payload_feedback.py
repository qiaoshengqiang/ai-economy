"""
「读取到的内容**当场**回喂给模型」—— 形状 2（用户 2026-10-10 决定）
========================================================================

【这个文件钉住的缺口】

    实测（运行 1010-1009，用**真实渲染函数**跑出来的证据）：

        `workspace_read` 的 `ActionResult.output.content` 里完整存着
        2521 字符的正文，而 Agent **下一轮实际看到**的是

            action=workspace_read target=manuscript/chapter_001.md
            revision=1 chars=2521

        🔴 **正文在里面吗 = False**

        ⇒ 「读」这件事结构上成功了，语义上是**空转**。

【修法】`run_cognition(follow_up_on_reads=True)` 时：

    ① 第 1 次调用 → Decision（里面可能有读取动作）
    ② 执行动作 → 拿到真实返回
    ③ **如果有可回喂的载荷**：把返回渲染成下一条 user 消息，
       连同模型自己的原文一起**再调一次** → 它当场接着决定
    ④ 第 2 段的动作**也执行**

【🔴 本文件钉住的硬约束】

    · **最多 2 次调用**（不做循环 —— 每次调用都要花钱）
    · **默认关闭**（多一次调用，成本由调用方明说要不要付）
    · **不是 tool calling**（撞 `actions/__init__.py` 的 Tool Registry 禁令）
    · 只有**成功且真的有载荷**的观察类动作才触发
    · 长度**有上限**，且截断必须**看得出来**
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import run as run_mod                                  # noqa: E402
from actions.executor import ActionDispatcher                        # noqa: E402
from actions.intent import ActionResult, ActionIntent                # noqa: E402
from actions.read_payload import (                                   # noqa: E402
    CONFIRMATION_ACTIONS, DEMAND_ONLY_ACTIONS, OBSERVABLE_ACTIONS,
    render_read_results,
)
from tests.action_registry_helpers import registered_action_types     # noqa: E402

MANUSCRIPT = "# 《借命》\n\n## 第一章 二牛的手\n\n青州市入夏第一场雨，下得毫无道理。\n"


# ============================================================
# 工具：可编排的假 Agent + 只回真结果的执行器
# ============================================================
class _Resp:
    def __init__(self, content, *, output_status="VALID_CONTENT",
                 cognitive_status="COMPLETE", finish_reason="stop",
                 tokens=100):
        self.content = content
        self.output_status = output_status
        self.cognitive_status = cognitive_status
        self.finish_reason = finish_reason
        self.total_tokens = tokens


class _ScriptedAgent:
    """按顺序返回 `responses`；把每次收到的 messages 记下来。"""

    agent_id = "agent_a"

    def __init__(self, responses):
        self._responses = list(responses)
        self.seen_messages = []

    def generate(self, messages=None, **kw):
        self.seen_messages.append(messages)
        if not self._responses:
            # 多调了一次 = 守卫失败。给一个明确的爆炸，
            # 而不是悄悄返回一个空回复让断言看起来通过。
            raise AssertionError(
                f"模型被调用了 {len(self.seen_messages)} 次 —— 超过脚本")
        return self._responses.pop(0)


class _ReadExecutor:
    """认领一个动作类型，返回**真的** ActionResult（含载荷）。"""

    def __init__(self, action_type, payloads, log):
        self.action_types = frozenset({action_type})
        self._payloads = list(payloads)
        self.log = log

    def execute(self, intent):
        self.log.append(intent.action_type)
        spec = self._payloads.pop(0) if self._payloads else {}
        return ActionResult(
            success=spec.get("success", True),
            output=spec.get("output"),
            error=spec.get("error"),
            action_id=intent.intent_id,
            action_type=intent.action_type,
            metadata={"code": spec.get("code")})


def _act(*actions):
    return json.dumps({"decision": "act", "reasoning": "r",
                       "actions": list(actions)})


_READ_ONE = _act({"type": "workspace_read",
                  "parameters": {"path": "manuscript/chapter_001.md"}})


# ============================================================
# 一、渲染器
# ============================================================
class TestTheRenderer:
    def test_tool_help_docs_are_fed_back_in_the_same_cognition(self):
        intent = ActionIntent(
            action_type="tool_help",
            parameters={"tool": "playwright"},
            agent_id="agent_a")
        result = ActionResult(
            success=True,
            output={
                "tool": "playwright",
                "content": "必须使用 *.spec.js 和 @playwright/test",
            },
            action_id=intent.intent_id,
            action_type=intent.action_type)

        got = render_read_results([(intent, result)])

        assert "*.spec.js" in got
        assert "@playwright/test" in got
    def _pairs(self, action_type, output, *, success=True, params=None):
        intent = type("I", (), {"action_type": action_type,
                                "parameters": params or {}})()
        result = ActionResult(success=success, output=output, error=None,
                              action_id="a", action_type=action_type,
                              metadata={"code": None})
        return [(intent, result)]

    def test_it_returns_the_real_content(self):
        pairs = self._pairs("workspace_read",
                            {"content": MANUSCRIPT, "chars": len(MANUSCRIPT)},
                            params={"path": "manuscript/chapter_001.md"})
        got = render_read_results(pairs)

        assert MANUSCRIPT in got
        assert "manuscript/chapter_001.md" in got, "没说读的是哪个文件"

    def test_no_observable_payload_means_empty_string(self):
        """🔒 返回 `""` 是「不要 follow-up」的判据 —— 不能返回一个空壳。"""
        assert render_read_results([]) == ""

        # 读**失败** → 没有载荷
        pairs = self._pairs("workspace_read", None, success=False)
        assert render_read_results(pairs) == ""

    def test_a_failed_read_is_not_rendered(self):
        pairs = self._pairs("workspace_read", {"content": "不该出现"},
                            success=False)
        assert "不该出现" not in render_read_results(pairs)

    def test_write_results_are_not_echoed_back(self):
        """
        🔴 写动作的 `parameters.content` 是 Agent**自己刚给**的东西 ——
           回喂它没有意义，还会占满上下文。
        """
        intent = type("I", (), {
            "action_type": "workspace_write",
            "parameters": {"path": "a.md", "content": "我自己刚写的正文"}})()
        result = ActionResult(success=True, output={"revision": 1},
                              error=None, action_id="a",
                              action_type="workspace_write",
                              metadata={"code": None})

        assert "我自己刚写的正文" not in render_read_results([(intent, result)])

    def test_shared_context_read_is_rendered_structurally(self):
        pairs = self._pairs("shared_context_read", {
            "revision": 2,
            "context": {"objective": "写第一章", "assignments": []},
            "domain_state": {"status": "CREATED"},
            "recent_changes": [{"change_type": "ASSIGN"}],
        })
        got = render_read_results(pairs)

        assert "写第一章" in got
        assert "CREATED" in got, "域状态没渲染 —— 那是现场读的引用"
        assert "ASSIGN" in got

    def test_same_revision_and_range_is_not_fed_twice(self):
        """
        同一 path + revision + 范围重复读取时，不把同一段全文再塞一遍。
        revision 变了则重新回喂。
        """
        seen = set()
        output = {
            "path": "index.html",
            "content": "FULL_FILE_" * 100,
            "chars": 1000,
            "revision": 3,
            "start_line": 1,
            "end_line": 50,
            "total_lines": 50,
        }
        pairs = self._pairs(
            "workspace_read", output,
            params={"path": "index.html"})

        first = render_read_results(pairs, seen_reads=seen)
        assert "FULL_FILE_" in first

        second = render_read_results(pairs, seen_reads=seen)
        assert "FULL_FILE_" not in second
        assert "内容未重复发送" in second

        changed = dict(output, revision=4)
        third = render_read_results(
            self._pairs("workspace_read", changed,
                        params={"path": "index.html"}),
            seen_reads=seen)
        assert "FULL_FILE_" in third

    def test_private_list_is_rendered(self):
        pairs = self._pairs("private_list", {"paths": ["notes.md", "draft.md"]})
        got = render_read_results(pairs)

        assert "notes.md" in got and "draft.md" in got

    def test_an_empty_list_says_so_instead_of_nothing(self):
        """空目录也是**真实事实** —— 要说出来，不能静默什么都不给。"""
        pairs = self._pairs("private_list", {"paths": []})
        assert "没有文件" in render_read_results(pairs)

    def test_it_caps_a_single_payload_and_says_so(self):
        """
        🔴 读取可以拿到任意大的正文 —— 没有上限的话，
           一次读取就能把上下文撑爆。
        """
        huge = "甲" * 5000
        pairs = self._pairs("workspace_read", {"content": huge})
        got = render_read_results(pairs, per_payload_max=100)

        assert "甲" * 100 in got
        assert "甲" * 101 not in got
        assert "截断" in got, "截断了却不说 —— 模型会以为这就是全部"
        assert "5000" in got, "没说完整长度"

    def test_it_caps_the_total_and_says_so(self):
        pairs = []
        for _ in range(5):
            pairs += self._pairs("workspace_read", {"content": "乙" * 200})
        got = render_read_results(pairs, per_payload_max=200,
                                  total_max=450)

        assert "总长度上限" in got, "丢了东西却不说"
        assert got.count("[") < 5, "总上限没有生效"

    def test_the_wording_carries_no_instructions(self):
        """
        §七：观察只能渲染成**材料**，不能渲染成指令。

        🔒 扫的是**渲染出来的文本**，不是源码 —— 源码里的说明文字
           里有「你应该」这样的例子（本项目记录过的假阳性 bug 类）。
        """
        pairs = self._pairs("workspace_read", {"content": MANUSCRIPT})
        got = render_read_results(pairs)

        for word in ("你应该", "你必须", "请继续", "建议你", "should",
                     "must", "please"):
            assert word not in got, f"回喂文本里出现了指令性措辞：{word}"


# ============================================================
# 二、动作分类必须**完整**（否则新动作会静默不回喂）
# ============================================================
class TestEveryActionIsClassified:
    def test_the_two_sets_cover_every_registered_action(self):
        registered = registered_action_types() | DEMAND_ONLY_ACTIONS
        classified = OBSERVABLE_ACTIONS | CONFIRMATION_ACTIONS

        missing = sorted(registered - classified)
        assert not missing, (
            f"这些动作**没有分类**：{missing}\n"
            f"⇒ 它们的读取结果会**静默**不回喂给模型 —— "
            f"那正是这个 bug 的成因。请显式放进 OBSERVABLE_ACTIONS "
            f"或 CONFIRMATION_ACTIONS。")

    def test_no_action_is_classified_that_is_not_registered(self):
        phantom = sorted(
            (OBSERVABLE_ACTIONS | CONFIRMATION_ACTIONS)
            - (registered_action_types() | DEMAND_ONLY_ACTIONS))
        assert not phantom, f"分类里有不存在的动作：{phantom}"

    def test_the_two_sets_do_not_overlap(self):
        assert not (OBSERVABLE_ACTIONS & CONFIRMATION_ACTIONS)


# ============================================================
# 三、run_cognition：最多一次 follow-up
# ============================================================
class TestTheFollowUp:
    def _run(self, responses, payloads, *, follow_up, dispatcher_types=None):
        log = []
        executors = []
        for t in (dispatcher_types or ["workspace_read"]):
            executors.append(_ReadExecutor(t, payloads, log))
        agent = _ScriptedAgent(responses)
        r = run_mod.run_cognition(
            agent, messages="原始 prompt",
            dispatcher=ActionDispatcher(executors),
            follow_up_on_reads=follow_up)
        return r, agent, log

    def test_it_is_OFF_by_default(self):
        """
        🔴 默认必须是关的 —— 它让「读」的那一轮**多一次完整 LLM 调用**，
           这种成本要由调用方明说。
        """
        resp = [_Resp(_READ_ONE)]
        r, agent, log = self._run(resp, [{"output": {"content": MANUSCRIPT}}],
                                  follow_up=False)

        assert len(agent.seen_messages) == 1
        assert r.follow_up_used is False
        assert len(r.segments) == 1

    def test_a_read_triggers_exactly_one_follow_up(self):
        resp = [_Resp(_READ_ONE), _Resp('{"decision":"reply","reasoning":"看完了"}')]
        r, agent, log = self._run(resp, [{"output": {"content": MANUSCRIPT}}],
                                  follow_up=True)

        assert len(agent.seen_messages) == 2, "读取没有触发回喂"
        assert r.follow_up_used is True
        assert len(r.segments) == 2

    def test_the_second_call_really_carries_the_content(self):
        """🔴 本文件的核心断言 —— 正文真的进了第二次调用的输入。"""
        resp = [_Resp(_READ_ONE), _Resp('{"decision":"reply","reasoning":"看完了"}')]
        _, agent, _ = self._run(resp, [{"output": {"content": MANUSCRIPT}}],
                                follow_up=True)

        # 🔒 比对**真实的 content**，不要 `json.dumps` ——
        #    那会把换行转义成 `\n` 两个字面字符，正文就匹配不上了
        #    （第一版就是这么假红的）。
        msgs = agent.seen_messages[1]
        joined = "\n".join(
            str(m.get("content", "")) if isinstance(m, dict) else str(m)
            for m in msgs)

        assert MANUSCRIPT in joined, "第二次调用里没有正文"
        assert "workspace_read" in joined
        # 原始 prompt 与它自己的原文都要在 —— 否则它不知道自己上一句说了什么
        assert "原始 prompt" in joined
        assert "reasoning" in joined
        # 🔴 顺序必须是 user → assistant → user（消息角色不能乱）
        assert [m.get("role") for m in msgs] == ["user", "assistant", "user"]

    def test_it_never_makes_a_third_call(self):
        """🔴 硬上限：**最多 2 次**。第二次也读了也不许再调。"""
        resp = [_Resp(_READ_ONE), _Resp(_READ_ONE)]
        r, agent, _ = self._run(
            resp,
            [{"output": {"content": MANUSCRIPT}},
             {"output": {"content": MANUSCRIPT}}],
            follow_up=True)

        assert len(agent.seen_messages) == 2, "做了第三次调用 —— 没有硬上限"
        assert len(r.segments) == 2

    def test_a_write_only_turn_does_not_follow_up(self):
        resp = [_Resp(_act({"type": "workspace_write",
                            "parameters": {"path": "a.md", "content": "x"}}))]
        r, agent, log = self._run(
            resp, [{"output": {"revision": 1}}], follow_up=True,
            dispatcher_types=["workspace_write"])

        assert len(agent.seen_messages) == 1
        assert r.follow_up_used is False

    def test_a_failed_read_does_not_follow_up(self):
        resp = [_Resp(_READ_ONE)]
        r, agent, _ = self._run(
            resp, [{"success": False, "code": "not_found"}], follow_up=True)

        assert len(agent.seen_messages) == 1, "读失败还多花了一次调用"
        assert r.follow_up_used is False

    def test_an_empty_file_does_not_follow_up(self):
        """读到**空文件**：没有载荷可回喂，不该白花一次调用。"""
        resp = [_Resp(_READ_ONE)]
        r, agent, _ = self._run(resp, [{"output": {"content": ""}}],
                                follow_up=True)

        assert len(agent.seen_messages) == 1
        assert r.follow_up_used is False

    def test_the_second_segment_actions_really_execute(self):
        """🔴 形状 2 的意义所在：**读完能当场动手**。"""
        second = _act({"type": "message_send",
                       "parameters": {"to": "agent_b", "text": "交给你"}})
        resp = [_Resp(_READ_ONE), _Resp(second)]
        r, agent, log = self._run(
            resp,
            [{"output": {"content": MANUSCRIPT}},
             {"output": {}}],
            follow_up=True,
            dispatcher_types=["workspace_read", "message_send"])

        assert log == ["workspace_read", "message_send"], (
            f"第二段的动作没有执行（或顺序不对）：{log}")
        assert len(r.action_intents) == 2, "跨段动作没有被拼起来"
        assert [i.action_type for i in r.action_intents] == [
            "workspace_read", "message_send"]

    def test_a_truncated_second_segment_executes_nothing(self):
        """第二段被截断 ⇒ 它的动作**不执行**（与第一段同一条纪律）。"""
        second = _act({"type": "message_send",
                       "parameters": {"to": "agent_b", "text": "x"}})
        resp = [_Resp(_READ_ONE),
                _Resp(second, cognitive_status="TRUNCATED",
                      finish_reason="length")]
        r, agent, log = self._run(
            resp, [{"output": {"content": MANUSCRIPT}}], follow_up=True,
            dispatcher_types=["workspace_read", "message_send"])

        assert log == ["workspace_read"], "被截断的第二段还是执行了动作"
        assert r.action_results[-1] is None
        assert len(r.action_intents) == 2, "意图要保留（那是真实事实）"

    def test_unrecognizable_messages_shape_skips_the_follow_up(self):
        """
        `_as_message_list` 认不出来的形状 ⇒ **跳过** follow-up。

        🔒 宁可少一次回喂，也不要冒着把原始 prompt 弄坏的风险去猜。
        """
        log = []
        agent = _ScriptedAgent([_Resp(_READ_ONE)])
        r = run_mod.run_cognition(
            agent, messages=12345,
            dispatcher=ActionDispatcher(
                [_ReadExecutor("workspace_read",
                               [{"output": {"content": MANUSCRIPT}}], log)]),
            follow_up_on_reads=True)

        assert len(agent.seen_messages) == 1
        assert r.follow_up_used is False
        assert r.action_results[0].success is True, "第一段照常执行"

    def test_per_segment_tokens_are_recorded(self):
        """
        🔴 `last_response` 只保留**最后一次**调用 ——
           不逐段记的话，「这一轮花了多少」会被算成只有第二段。
        """
        resp = [_Resp(_READ_ONE, tokens=111),
                _Resp('{"decision":"reply"}', tokens=222)]
        r, _, _ = self._run(resp, [{"output": {"content": MANUSCRIPT}}],
                            follow_up=True)

        assert [s.tokens for s in r.segments] == [111, 222]
        assert sum(s.tokens for s in r.segments) == 333

    def test_the_decision_field_keeps_its_old_meaning(self):
        """向后兼容：`decision` 仍是**第一段**（它看到 prompt 时的判断）。"""
        resp = [_Resp(_READ_ONE),
                _Resp('{"decision":"reply","reasoning":"第二段说的"}')]
        r, _, _ = self._run(resp, [{"output": {"content": MANUSCRIPT}}],
                            follow_up=True)

        assert r.decision.reasoning == "r", "decision 变成第二段了"
        assert r.segments[-1].reasoning == "第二段说的"

    def test_to_dict_carries_the_segments(self):
        resp = [_Resp(_READ_ONE), _Resp('{"decision":"reply"}')]
        r, _, _ = self._run(resp, [{"output": {"content": MANUSCRIPT}}],
                            follow_up=True)

        d = r.to_dict()
        assert d["segment_count"] == 2
        assert d["follow_up_used"] is True
        json.dumps(d)          # 必须可序列化


# ============================================================
# 四、实验侧真的把它打开了
# ============================================================
class TestTheExperimentTurnsItOn:
    def test_production_turn_passes_the_flag(self):
        """
        🔒 默认关闭 ⇒ 实验**必须显式打开**，否则这个能力等于没接。
           现在允许实验级 `FOLLOW_UP_ON_READS` 关闭它（慢 provider 场景），
           但**默认值必须仍是 True**，且 `_turn` 必须把它传给 `act()`。
        """
        from experiments.heterogeneous_production import production as P

        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        assert "follow_up_on_reads=FOLLOW_UP_ON_READS" in src
        assert P.FOLLOW_UP_ON_READS is True, (
            "默认必须打开读取回喂；实验可以显式改成 False，"
            "但不能把默认值改掉")

    def test_the_turn_records_both_segments(self):
        src = (ROOT / "experiments" / "heterogeneous_production"
               / "production.py").read_text(encoding="utf-8")
        for key in ('turn["segments"]', 'turn["reasonings"]',
                    'turn["follow_up_used"]', '"tokens_by_segment"'):
            assert key in src, f"turn 记录里缺 {key}"

    def test_the_report_shows_the_follow_up(self):
        src = (ROOT / "experiments" / "agent_division"
               / "run_division.py").read_text(encoding="utf-8")
        assert "follow_up_used" in src
        assert "第" in src and "段判断" in src, "报告没有列出两段判断"

    def test_the_prompts_no_longer_claim_content_is_delayed(self):
        """
        🔴 两个 prompt 里原先写着「读到的内容**不会**立刻回到你手上」——
           现在那是**假话**，必须已经改掉。
        """
        for rel in ("experiments/agent_division/prompt.py",
                    "experiments/heterogeneous_production/production.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            assert "不会**立刻回到你手上" not in text, (
                f"{rel} 里还留着「不会立刻回到你手上」—— 现在是假话")
            assert "当场" in text, f"{rel} 没有说明新的行为"


# ============================================================
# 五、真端到端：真 Gateway + 真 Brain + 真 ExperienceStore
# ============================================================
#
#   🔒 上面那些用假执行器验的是**编排**；这一节验的是
#      **真实文件真的被读到、并且真的进了第二次调用**。
#      只有 provider 是脚本化的。

W = "WS-E2E"
A = "novel_author"
PATH = "manuscript/chapter_001.md"
E2E_MANUSCRIPT = "# 《借命》\n\n## 第一章 二牛的手\n\n青州市入夏第一场雨。\n"


class _ScriptedProvider:
    """按顺序返回 content —— 每次调用都记下收到的 messages / system。"""

    def __init__(self, contents):
        self._contents = list(contents)
        self.seen = []

    def generate(self, messages=None, system=None, **kw):       # noqa: ARG002
        from llm.base import LLMResponse

        self.seen.append({"messages": messages, "system": system or ""})
        content = self._contents.pop(0) if self._contents else "ok"
        return LLMResponse(provider="fake", model="fake", content=content,
                           input_tokens=1, output_tokens=1, total_tokens=7,
                           latency_ms=1.0, simulated=True,
                           finish_reason="stop")


class _Router:
    def __init__(self, prov):
        self.provider = prov

    def get_provider(self, name, credential=None):              # noqa: ARG002
        return self.provider

    def generate(self, messages=None, provider=None, model=None,
                 **kw):                                             # noqa: ARG002
        return self.provider.generate(messages=messages, **kw)


class _BrainAgent:
    """有 `.generate` 的最小 Agent —— 转发给**真实**的 Brain。"""

    def __init__(self, agent_id, brain):
        self.agent_id = agent_id
        self.brain = brain

    def generate(self, messages=None, **kw):
        return self.brain.generate(messages=messages, **kw)


def _flat(messages) -> str:
    if isinstance(messages, str):
        return messages
    if isinstance(messages, (list, tuple)):
        return "\n".join(
            str(m.get("content", "")) if isinstance(m, dict) else str(m)
            for m in messages)
    return str(messages)


def test_end_to_end_a_real_read_reaches_the_second_call():
    """
    🔴 **本文件最重要的一条。**

    走完整链路：

        run_cognition
          → 真实 Brain.generate（含真实 Cognitive Context）
          → 脚本化 provider 返回读取信封
          → 真实 ActionDispatcher → 真实 WorkspaceGateway 读**真文件**
          → 渲染 → 作为下一条 user 消息再调一次
          → 第二次 provider 调用里**必须能看到那段正文**

    这条通过，就证明「Agent 请求读文件 → 收到文件内容」**真的通了**。
    """
    from agents.brain import Brain, BrainSettings
    from cognition import CognitiveContextBuilder
    from experience.store import ExperienceStore
    from actions.workspace_read import WorkspaceReadExecutor
    from services.artifacts import MODE_CREATE, read_artifact, write_artifact
    from tests.workspace_harness import WorkspaceHarness

    h = WorkspaceHarness.in_memory(participants={W: {A}}, agents={A})
    try:
        db = h.new_db()
        gw = h.gateway(db)
        gw.ensure_workspace(W, A)
        wrote = write_artifact(gw, W, A, PATH, E2E_MANUSCRIPT,
                               write_mode=MODE_CREATE)
        assert wrote.get("ok"), f"前提不成立，文件没写进去：{wrote}"

        ex = ExperienceStore(db)
        prov = _ScriptedProvider([
            _READ_ONE,                                   # 第 1 段：要读
            '{"decision":"reply","reasoning":"读完"}',   # 第 2 段：说完了
        ])
        brain = Brain(
            _Router(prov),
            BrainSettings(agent_id=A),
            context_builder=CognitiveContextBuilder(None, experience_store=ex),
            default_context_query={},
        )
        dispatcher = ActionDispatcher([
            WorkspaceReadExecutor(read_artifact, gateway=gw, workspace_id=W)])

        run = run_mod.run_cognition(_BrainAgent(A, brain),
                                    messages="把第一章写出来",
                                    dispatcher=dispatcher,
                                    follow_up_on_reads=True)

        # ---- ① 真的读了，而且读到的是**真文件** ----
        assert run.action_results[0].success is True, run.action_results[0]
        assert "二牛的手" in (run.action_results[0].output or {}).get(
            "content", ""), "Gateway 没读到正文"

        # ---- ② 真的又调了一次 ----
        assert len(prov.seen) == 2, f"调用次数 = {len(prov.seen)}"
        assert run.follow_up_used is True

        # ---- ③ 🔴 第二次调用里**必须**有那段正文 ----
        second = _flat(prov.seen[1]["messages"])
        assert "二牛的手" in second, (
            "真 Gateway 读到的正文**没有**进第二次调用 —— 这条链还是断的")

        # ---- ④ 第二次调用照样走完整的 Context 装配（没被降级）----
        #
        #   🔒 这个测试里 `BrainSettings` 没配 system_prompt、
        #      也没有 economic_builder，所以 system 本来就是空的 ——
        #      所以断言的是**两次一致**，而不是「非空」。
        #      它要证明的是：follow-up **没有**绕过 Context 装配那条路。
        assert prov.seen[1]["system"] == prov.seen[0]["system"], (
            "第二次调用的 system 与第一次不一致 —— 它走了另一条装配路径")
    finally:
        h.close()


def test_end_to_end_without_the_flag_the_content_never_arrives():
    """
    🔴 反向：**不开** `follow_up_on_reads` 时，内容**收不到**。

    这条同时钉住两件事：
        ① 默认关闭 = 默认还是老行为（成本由调用方决定）；
        ② 上面那条测试**不是**假绿的 —— 差别就是这一个开关。
    """
    from agents.brain import Brain, BrainSettings
    from cognition import CognitiveContextBuilder
    from experience.store import ExperienceStore
    from actions.workspace_read import WorkspaceReadExecutor
    from services.artifacts import MODE_CREATE, read_artifact, write_artifact
    from tests.workspace_harness import WorkspaceHarness

    h = WorkspaceHarness.in_memory(participants={W: {A}}, agents={A})
    try:
        db = h.new_db()
        gw = h.gateway(db)
        gw.ensure_workspace(W, A)
        write_artifact(gw, W, A, PATH, E2E_MANUSCRIPT,
                       write_mode=MODE_CREATE)

        ex = ExperienceStore(db)
        prov = _ScriptedProvider([_READ_ONE])
        brain = Brain(
            _Router(prov),
            BrainSettings(agent_id=A),
            context_builder=CognitiveContextBuilder(None, experience_store=ex),
            default_context_query={},
        )
        dispatcher = ActionDispatcher([
            WorkspaceReadExecutor(read_artifact, gateway=gw, workspace_id=W)])

        run = run_mod.run_cognition(_BrainAgent(A, brain),
                                    messages="把第一章写出来",
                                    dispatcher=dispatcher,
                                    follow_up_on_reads=False)

        assert len(prov.seen) == 1, "默认关闭时还是多调了一次"
        assert run.follow_up_used is False
        # 读本身还是成功了 —— 内容在 ActionResult 里，只是**没回喂**
        assert run.action_results[0].success is True
    finally:
        h.close()
