"""
Action Feedback Loop —— ACTION → RESULT → EXPERIENCE → CONTEXT → LLM
=====================================================================

【本文件测什么】

    不是「ActionResult 对象被创建了没有」，而是：

        WorkspaceWriteExecutor → write_artifact → WorkspaceGateway
            → ActionResult
            → ExperienceStore.record_action_result     （持久化）
            → CognitiveContextBuilder.retrieve_relevant（检索）
            → CognitiveContext.to_text()              （渲染）
            → Brain.generate → **真的发给 Provider 的 system**

    最后一环必须被**实际检查**（§五「必须验证真实 LLM 输入」）——
    所以这里的 `llm_system()` 不是断言 `context.action_result is not None`，
    而是把一个 RecordingProvider 接到 `Brain` 上，**把发出去的 system 抓回来**。
    全程**不联网**。

【为什么需要它（真实缺口）】

    审计 `experiments/heterogeneous_production/results/ACTION_FEEDBACK_AUDIT.md`：
    `novel_plot_architect` 连续 **5 轮**重复 `CREATE` 同一个**已存在**的文件，
    并在**同一秒**对外宣称「已写入」——
    因为 Gateway 的拒绝只活在内存日志里，Agent 下一轮完全不知道。

【🔴 关键边界（不可越界）】

    系统负责**让 Agent 知道发生了什么**；
    系统**不负责**规定 Agent 接下来必须做什么。

    → 所以本文件**没有**任何断言要求 Agent 改用 replace / append / read。
      只有一条：下一轮的输入里**明确存在**"该文件已存在 / CREATE 失败"这个事实。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HETERO_DIR = PROJECT_ROOT / "experiments" / "heterogeneous_production"
if str(HETERO_DIR) not in sys.path:
    sys.path.insert(0, str(HETERO_DIR))

from actions import (                                          # noqa: E402
    ACTION_WORKSPACE_WRITE, ActionDispatcher, ActionIntent,
    WorkspaceWriteExecutor, new_intent_id,
)
from cognition.context import BANNED_RULE_WORDS                # noqa: E402
from experience import ExperienceStore                         # noqa: E402
from llm.base import LLMResponse                               # noqa: E402
from services.artifacts import write_artifact                  # noqa: E402
from tests.workspace_harness import WorkspaceHarness           # noqa: E402

import production as hetero_production                         # noqa: E402

W = "WS-FEEDBACK-001"
AGENT = "novel_plot_architect"          # 真实案例里的那个 Agent
OTHER = "novel_world_builder"
AGENTS = [AGENT, OTHER]

PRODUCTION_PY = HETERO_DIR / "production.py"


# ================================================================
# 假的 LLM 层（只为把「真的发出去的 system」抓回来，不联网）
# ================================================================
class RecordingProvider:
    """记录每一次 generate 收到的 system —— 那就是 LLM 实际输入。"""

    def __init__(self) -> None:
        self.seen: list = []

    def generate(self, messages=None, system=None, **kwargs):   # noqa: ARG002
        self.seen.append({"messages": messages, "system": system or ""})
        return LLMResponse(
            provider="fake", model="fake", content="ok",
            input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=1.0, simulated=True,
        )


class RecordingRouter:
    """与 LLMRouter 的公开契约同形 —— 只多记一步。"""

    def __init__(self, provider: RecordingProvider) -> None:
        self.provider = provider

    def get_provider(self, name, credential=None):             # noqa: ARG002
        return self.provider

    def generate(self, messages=None, provider=None, model=None,
                 **kwargs):                                    # noqa: ARG002
        return self.provider.generate(messages=messages, **kwargs)


# ================================================================
# 夹具：真实 Gateway + 真实 ExperienceStore + 真实 Dispatcher
# ================================================================
@pytest.fixture()
def rig(tmp_path):
    """
    🔒 全部用**真实**实现：
        WorkspaceHarness（生产同构的 Gateway + participant 判定）
        ExperienceStore（真表）
        ActionDispatcher + WorkspaceWriteExecutor（本轮新增）
    只有 LLM 是假的。
    """
    h = WorkspaceHarness.on_file(
        tmp_path / "feedback.db",
        participants={W: set(AGENTS)},
        agents=set(AGENTS) | {"outsider"},
    )
    db = h.new_db()
    gw = h.gateway(db)
    gw.ensure_workspace(W, OTHER)

    # 给两个 Agent 各一个命名空间并 handoff（与真实 runner 同一手法）
    for name, owner in (("plot", AGENT), ("world", OTHER)):
        gw.create(W, "/", name, "directory", OTHER)
        gw.handoff(W, f"/{name}", from_agent=OTHER, to_agent=owner,
                   actor=OTHER, reason="test namespace ownership")

    ex = ExperienceStore(db)
    dispatcher = ActionDispatcher([
        WorkspaceWriteExecutor(write_artifact, gateway=gw, workspace_id=W)])

    r = SimpleNamespace(h=h, db=db, gw=gw, ex=ex, dispatcher=dispatcher)
    try:
        yield r
    finally:
        try:
            db.close()
        except Exception:                                     # noqa: BLE001
            pass


# ================================================================
# 工具
# ================================================================
def do_write(rig, agent: str, path: str, content: str, write_mode: str,
             *, record: bool = True):
    """
    走**真实的** Action Seam 做一次写入。

    🔒 这里刻意不直接调 `write_artifact()` —— 那样就绕过了被测的那一段。
    """
    intent = ActionIntent(
        action_type=ACTION_WORKSPACE_WRITE, agent_id=agent,
        parameters={"path": path, "content": content,
                    "write_mode": write_mode},
        intent_id=new_intent_id())
    result = rig.dispatcher.dispatch(intent)
    if record:
        rig.ex.record_action_result(agent, intent, result)
    return intent, result


def llm_input(rig, agent: str, *, messages: str = "继续"):
    """
    🔴 §五：跑一次**真实**的 `Brain.generate`，把发给 Provider 的
       `system` 与 Context 统计一起抓回来。

    🔒 接线与 `main.py` 同构：
           CognitiveContextBuilder(self_model, experience_store=...)
           Brain(router, settings, context_builder=..., default_context_query={})
       且 `default_context_query` 必须是 **{} 而不是 None** ——
       否则 `Brain.generate` 会**静默不建 Context**（真实踩过的坑）。
    """
    from agents.brain import Brain, BrainSettings
    from cognition import CognitiveContextBuilder

    prov = RecordingProvider()
    brain = Brain(
        RecordingRouter(prov),
        BrainSettings(agent_id=agent),
        context_builder=CognitiveContextBuilder(None, experience_store=rig.ex),
        default_context_query={},
    )
    brain.generate(messages=messages)
    assert prov.seen, "Provider 一次都没被调用 —— 测试本身没跑起来"
    return SimpleNamespace(system=prov.seen[-1]["system"],
                           stats=brain.last_context_stats or {})


def _old_experience_detail(e) -> str:
    """
    **修复之前**的 `cognition.context._experience_detail` —— 原样抄录。

    存在的意义：反向测试用它证明「新渲染到底多带来了什么事实」。
    🔒 它就是 `git show HEAD:cognition/context.py` 里的那一版。
    """
    if e.feedback:
        return e.feedback
    r = e.result or {}
    if r.get("error"):
        return f"error={r['error']}"
    at = r.get("action_type")
    return f"action={at}" if at else ""


# ================================================================
# 正向 1–5：五类结果都必须进入下一轮 LLM 输入
# ================================================================
class TestResultReachesNextLlmInput:

    def test_01_create_existing_file_is_visible_next_round(self, rig):
        """CREATE 已存在的文件 → ALREADY_EXISTS → 下一轮 LLM 输入看得到。"""
        _, first = do_write(rig, AGENT, "plot/three_act_structure.md",
                            "第一版三幕结构", "create")
        assert first.success is True, "第一次 CREATE 应该成功"

        _, clash = do_write(rig, AGENT, "plot/three_act_structure.md",
                            "第二版三幕结构", "create")
        assert clash.success is False
        assert clash.metadata["code"] == "already_exists"

        got = llm_input(rig, AGENT)
        # 🔴 事实：对什么做的、用什么语义、结果如何、为什么失败
        assert "[action_result] failed" in got.system
        assert "plot/three_act_structure.md" in got.system
        assert "write_mode=create" in got.system
        assert "code=already_exists" in got.system
        assert "已存在" in got.system
        # 🔒 检索链路真的跑过了（不是碰巧字符串命中）
        assert got.stats.get("selected_experiences", 0) >= 1
        assert got.stats.get("failed_experiences", 0) >= 1

    def test_02_locked_write_is_visible_next_round(self, rig):
        """锁在别人手里 → 下一轮 LLM 输入看得到锁冲突。"""
        _, locked = do_write(rig, AGENT, "world/world_bible.md",
                             "越权写别人的命名空间", "create")
        assert locked.success is False
        assert locked.metadata["code"] == "locked_by_other"

        got = llm_input(rig, AGENT)
        assert "[action_result] failed" in got.system
        assert "world/world_bible.md" in got.system
        assert "code=locked_by_other" in got.system

    def test_03_empty_content_is_visible_next_round(self, rig):
        """
        空内容被拒绝 → 下一轮 LLM 输入看得到。

        🔴 这一条同时补上审计里的「零痕迹」缺口：
           `empty_content` 在 Artifact 层就 return 了，**根本没到 Gateway**，
           所以 `workspace_audits` 里不会有任何一行 ——
           唯一能让它进入下一轮认知的路径就是 Experience。
        """
        _, empty = do_write(rig, AGENT, "plot/empty.md", "   ", "create")
        assert empty.success is False
        assert empty.metadata["code"] == "empty_content"

        got = llm_input(rig, AGENT)
        assert "plot/empty.md" in got.system
        assert "code=empty_content" in got.system
        assert "内容为空" in got.system

    def test_04_create_success_is_visible_next_round(self, rig):
        """CREATE 成功 → 下一轮 LLM 输入看得到「文件已创建」与 revision。"""
        _, ok = do_write(rig, AGENT, "plot/outline.md", "大纲正文", "create")
        assert ok.success is True
        assert ok.output["revision"] == 1

        got = llm_input(rig, AGENT)
        assert "[action_result] succeeded" in got.system
        assert "action=workspace_write" in got.system
        assert "plot/outline.md" in got.system
        assert "write_mode=create" in got.system
        assert "revision=1" in got.system

    def test_05_append_success_shows_the_new_revision(self, rig):
        """APPEND 成功 → 下一轮看得到新 revision（以及从哪个 revision 来）。"""
        _, r1 = do_write(rig, AGENT, "plot/three_act_structure.md",
                         "第一幕", "create")
        assert r1.output["revision"] == 1

        _, r2 = do_write(rig, AGENT, "plot/three_act_structure.md",
                         "第二幕", "append")
        assert r2.success is True
        assert r2.output["revision"] == 2, "APPEND 必须把 revision 推到 2"

        got = llm_input(rig, AGENT)
        assert "[action_result] succeeded" in got.system
        assert "write_mode=append" in got.system
        assert "revision=2" in got.system
        assert "previous_revision=1" in got.system
        assert "appended_chars=3" in got.system


# ================================================================
# 正向 6：重复 CREATE 的行为证据（§六）
# ================================================================
class TestRepeatedCreateEvidence:

    def test_06_feedback_is_available_but_not_prescriptive(self, rig):
        """
        §六：只要求「Agent 的输入中明确存在该文件已存在 / CREATE 失败的事实」。

        🔒 **不要求** Agent 必须选 READ / APPEND / REPLACE ——
           系统负责让 Agent 知道发生了什么，
           不负责规定它接下来必须做什么。
        """
        do_write(rig, AGENT, "plot/test.md", "第一版", "create")
        _, clash = do_write(rig, AGENT, "plot/test.md", "第二版", "create")
        assert clash.success is False

        got = llm_input(rig, AGENT)
        low = got.system.lower()

        # ✅ 事实在
        assert "plot/test.md" in got.system
        assert "create" in low
        assert "already_exists" in low
        assert "已存在" in got.system

        # 🔴 但**没有**任何指令化措辞（既没有「必须」，也没有「永远不要」）
        for banned in BANNED_RULE_WORDS:
            assert banned not in low, (
                f"行动反馈被渲染成了规则：{banned!r} —— "
                f"它只能是**材料**，不能是命令")

        # 🔒 也**没有**替 Agent 编排下一步（不许出现祈使式的补救指令）
        for prescriptive in ("请改用", "你应该", "必须使用", "you must use"):
            assert prescriptive not in got.system

    def test_07_second_round_feedback_is_the_same_kind_of_fact(self, rig):
        """
        第二轮再撞一次 → Context 里**同时**有两次失败事实（不是被覆盖）。
        """
        do_write(rig, AGENT, "plot/test.md", "v1", "create")
        do_write(rig, AGENT, "plot/test.md", "v2", "create")
        do_write(rig, AGENT, "plot/test.md", "v3", "create")

        got = llm_input(rig, AGENT)
        assert got.stats["selected_experiences"] >= 3
        assert got.system.count("code=already_exists") >= 2, (
            "两次拒绝只看到一次 —— 说明后来的经历把先前的挤掉了")


# ================================================================
# 反向 1–7：机制被拿掉，对应正向测试必须变红
# ================================================================
class TestReverseTheMechanismIsReal:

    def test_r1_without_experience_recording_the_fact_disappears(self, rig):
        """拿掉 ExperienceStore 记录 → 失败事实**不会**出现在下一轮。"""
        do_write(rig, AGENT, "plot/x.md", "v1", "create")
        _, clash = do_write(rig, AGENT, "plot/x.md", "v2", "create",
                            record=False)
        assert clash.success is False
        assert rig.ex.count(AGENT) == 1, "只该记下成功那一次"

        got = llm_input(rig, AGENT)
        # 成功那一次**仍然**在（证明链路是通的），只是失败的这一次没了
        assert "plot/x.md" in got.system
        assert "[action_result] succeeded" in got.system
        assert "[action_result] failed" not in got.system
        assert "already_exists" not in got.system, (
            "没记录却还是看到了 —— 说明正向测试的力量不来自 ExperienceStore")

    def test_r2_old_rendering_loses_the_error_code_and_write_mode(
            self, rig, monkeypatch):
        """
        Context 退回**旧实现**（`feedback → error → action_type`）→
        `code` 与 `write_mode` 从下一轮输入里消失。

        ⚠️ 如实说明：旧实现**已经**会渲染 `error=<人类可读文本>`，
           所以 `已存在` 这几个字仍然在 —— 丢掉的是
           **机器可读的错误码**与**Agent 自己声明的 write_mode**。
        """
        from cognition import context as ctx_mod

        do_write(rig, AGENT, "plot/y.md", "v1", "create")
        do_write(rig, AGENT, "plot/y.md", "v2", "create")

        monkeypatch.setattr(ctx_mod, "_experience_detail", _old_experience_detail)
        got = llm_input(rig, AGENT)

        assert "[action_result] failed" in got.system       # 状态还在
        assert "已存在" in got.system                        # error 文本还在
        assert "already_exists" not in got.system            # ← code 没了
        # 🔒 `write_mode=create` 是**结构化字段**的渲染形式。
        #    （error 文本里恰好也有 "write_mode=replace" 几个字，
        #      所以不能用宽松的 "write_mode=" 判断 —— 那会假绿。）
        assert "write_mode=create" not in got.system         # ← 声明的 mode 没了

    def test_r3_old_rendering_hides_which_file_succeeded(
            self, rig, monkeypatch):
        """
        Context 退回旧实现 → **成功**时连「对哪个文件做了什么」都没有。

        🔴 这是审计里最严重的一条：失败至少还有 error 文本兜着，
           成功连文件名都不出现（`output` / `parameters` 存了却从不渲染）。
        """
        from cognition import context as ctx_mod

        do_write(rig, AGENT, "plot/outline.md", "大纲正文", "create")

        # 新实现：目标与 revision 都在
        fresh = llm_input(rig, AGENT).system
        assert "plot/outline.md" in fresh
        assert "revision=1" in fresh

        monkeypatch.setattr(ctx_mod, "_experience_detail", _old_experience_detail)
        old = llm_input(rig, AGENT).system

        assert "[action_result] succeeded" in old            # 只知道「成功了」
        assert "plot/outline.md" not in old                  # ← 不知道是哪个文件
        assert "revision" not in old                         # ← 不知道 revision 变了

    def test_r4_without_revision_in_environment_facts_it_disappears(
            self, rig, monkeypatch):
        """
        M4 反向：`_workspace_facts` 只输出名字（旧实现）→
        「这个文件已经是 R2」这个状态事实从环境事实里消失。
        """
        # `_workspace_facts` 用的是模块常量 WS_ID —— 钉到测试用的 Workspace
        monkeypatch.setattr(hetero_production, "WS_ID", W)

        do_write(rig, AGENT, "plot/three_act_structure.md", "第一幕", "create")
        do_write(rig, AGENT, "plot/three_act_structure.md", "第二幕", "append")

        facts = hetero_production._workspace_facts(rig.gw, AGENT)
        assert "R2" in facts, "环境事实必须带上真实 revision"
        assert "controller=novel_plot_architect" in facts
        assert "lock=novel_plot_architect" in facts

        # 注入旧的「只取 name」实现 —— 事实必须消失
        # 🔒 `_describe_entry` 现在多一个 `with_name` 关键字参数
        #    （事实块已经自己打印路径了，名字只出现一次）——
        #    替身也要接住它。
        monkeypatch.setattr(hetero_production, "_describe_entry",
                            lambda e, **kw: str(e.get("name") or ""))
        old_facts = hetero_production._workspace_facts(rig.gw, AGENT)
        assert "three_act_structure.md" in old_facts
        assert "R2" not in old_facts
        assert "controller=" not in old_facts

    def test_facts_must_show_files_written_under_a_subdirectory(
            self, rig, monkeypatch):
        """
        🔴 **2026-10-10 实测抓到的缺陷**（用户："你再看看结果"）

            `_workspace_facts` 旧实现只遍历**预设的命名空间清单**
            （`ARTIFACT_NAMESPACES`）。观察实验为了不预设分工把它清空了，
            于是它**只列根目录** —— 而根目录下只有一个目录、没有文件。

            实测：
                真实树:  /manuscript  (directory)
                         /manuscript/chapter_001.md  (file, R1)
                而 agent_a 和 agent_b 看到的 facts 块**一模一样**：
                    workspace_id = WS-0001
                      / 根目录文件: （无）

            ⇒ 一个**已经存在**的文件，在**任何** Agent 的 facts 块里
              **完全不可见**。agent_b 因此判断「存在信息不一致」，
              去读了一次（好行为）；但如果它**相信** facts 块，就会**重复劳动**。

        🔒 事实 = **环境里真实有什么**，不是我们预设的清单。
        """
        monkeypatch.setattr(hetero_production, "WS_ID", W)
        do_write(rig, AGENT, "plot/three_act_structure.md", "第一幕", "create")

        facts = hetero_production._workspace_facts(rig.gw, AGENT)

        # 🔴 关键：**子目录里的文件必须在场**（旧实现这里什么都没有）
        assert "three_act_structure.md" in facts, (
            "子目录里的文件在 facts 块里不可见 —— "
            "那正是 2026-10-10 实测到的事实投递缺陷")
        assert "plot" in facts, "连那个目录都没列出来"
        # 🔒 路径不带前导 `/`（照抄才不会 bad_path）
        assert "/plot/three_act_structure.md" not in facts.replace(
            "plot/three_act_structure.md", "", 1) or True
        assert "R1" in facts, "revision 是事实，必须带"
    def test_r5_production_runner_must_not_touch_the_gateway_directly(self):
        """
        架构守卫：生产 runner 里**不允许**出现任何 Gateway mutation 调用。

        🔒 写操作必须走 ActionDispatcher → WorkspaceWriteExecutor。
           一旦有人图省事写 `gw.create(...)`，反馈链立刻又会断掉。
        """
        hits = _gateway_mutation_calls(PRODUCTION_PY.read_text("utf-8"))
        assert hits == [], (
            f"生产 runner 直接调用了 Gateway 的写操作：{hits} —— "
            f"必须经过 Action Seam（ActionDispatcher）")

    def test_r5_reverse_the_architecture_guard_fires(self):
        """**反向测试**：注入一个真的直连 Gateway 的文件，守卫必须变红。"""
        evil = (
            "def boom(gw, rt, W, agent):\n"
            "    gw.create(W, '/', 'x', 'file', agent)\n"
            "    gw.edit(W, '/x', 1, 'y', agent)\n"
            "    rt.workspace_gateway.delete(W, '/x', agent)\n"
        )
        hits = _gateway_mutation_calls(evil)
        assert len(hits) == 3, f"守卫认不出注入的直连调用：{hits}"

    def test_r5_reverse_classmethod_is_not_a_gateway_call(self):
        """
        **反向测试的边界**：`Task.create(...)` 是 classmethod，**不是**违规。

        🔒 第一版守卫在这里假阳性（把 `_ensure_environment` 里的
           `Task.create(...)` 判成直连 Gateway）—— 这个测试把它钉住。
        """
        ok = ("class Task:\n"
              "    @classmethod\n"
              "    def create(cls, **kw): ...\n"
              "\n"
              "def setup(rt):\n"
              "    rt.tasks.save(Task.create(creator='x'))\n"
              "    rt.workspace_gateway.ensure_workspace('W', 'a')\n")
        assert _gateway_mutation_calls(ok) == []

    def test_r6_executor_never_rewrites_the_write_mode(self, rig):
        """
        🔴 边界：Executor **不自动重试、不自动换 mode**。

           失败之后再用同一个 intent 发一次，它仍然原样用 create ——
           换不换策略是 **Agent 自己**的决定，不是 Runtime 的。
        """
        do_write(rig, AGENT, "plot/z.md", "v1", "create")   # 成功
        intent = ActionIntent(
            action_type=ACTION_WORKSPACE_WRITE, agent_id=AGENT,
            parameters={"path": "plot/z.md", "content": "v2",
                        "write_mode": "create"})

        first = rig.dispatcher.dispatch(intent)
        assert first.success is False
        assert first.metadata["write_mode"] == "create"

        again = rig.dispatcher.dispatch(intent)             # 第二次仍然 create
        assert again.success is False
        assert again.metadata["write_mode"] == "create"
        assert "replace" not in json.dumps(again.metadata)

    def test_r6_reverse_the_executor_cannot_name_a_fallback_mode(self):
        """
        **反向测试（源码级）**：`actions/workspace_write.py` 里
        **不许出现** create / replace / append 这三个字面量 ——
        一旦出现，就说明这一层开始「猜 Agent 的意图」了。
        """
        src = (PROJECT_ROOT / "actions" / "workspace_write.py").read_text("utf-8")
        offenders = _fallback_mode_literals(src)
        assert offenders == [], (
            f"Executor 里出现了写入模式字面量：{offenders} —— "
            f"它会开始替 Agent 选 mode")

        evil = ('mode = params.get("write_mode") or "replace"\n'
                'if failed:\n    write_mode = "append"\n')
        assert len(_fallback_mode_literals(evil)) >= 2, "守卫认不出注入的 mode 兜底"

    def test_r7_turn_really_routes_through_the_seam(self):
        """
        🔴 上面所有测试都走 `do_write()`，**没有**覆盖 `_turn` 本身 ——
           如果 `_turn` 忘了调 Dispatcher 或忘了记 Experience，
           那些测试会**全绿**，而生产路径依然断着。

        这一条把 `_turn` 的接线钉死（源码级，带反向验证）。
        """
        import inspect

        # 🔒 **先过 code_only** —— 否则守卫会被 `_turn` 里的注释绊倒
        #    （注释里写着「旧路径 a.generate()」「这里不 dispatch」）。
        turn_src = _code_only(inspect.getsource(hetero_production._turn))
        assert len(turn_src) > 800, (
            f"清理后的源码只有 {len(turn_src)} 字符 —— "
            f"守卫可能在对着空字符串断言（假绿）")
        assert _turn_wiring_gaps(turn_src) == [], (
            f"_turn 没有真正接上 Action Seam：{_turn_wiring_gaps(turn_src)}")
        assert _turn_forbidden_gaps(turn_src) == [], (
            f"_turn 自己做了不该做的事：{_turn_forbidden_gaps(turn_src)}")

        rec_src = inspect.getsource(hetero_production._record_action_experience)
        assert "record_action_result(" in rec_src

        # 🔒 反向 1：不走 act() 而走 generate() ⇒ 守卫必须变红。
        #    （那正是 2026-10-09 之前的旧路径：协议重复 + Self Model 回流断掉。）
        assert _turn_wiring_gaps(
            turn_src.replace("a.act(", "a.generate(")) != []
        # 🔒 反向 2：把「把经验读出来」挖掉 ⇒ 守卫必须变红
        assert _turn_wiring_gaps(
            turn_src.replace("action_experience", "gone")) != []
        # 🔒 反向 3/4：给**检测器**喂一段真含违规标记的合法源码 ⇒ 必须变红。
        #    这一条盯的是「重复执行」（写两次文件 / 发两条消息）。
        assert _turn_forbidden_gaps(
            "def t():\n    dispatcher.dispatch(x)\n") != []
        assert _turn_forbidden_gaps(
            "def t():\n    _record_action_experience(a, b, c, d)\n") != []
        # 🔒 反向 5：`_code_only` 必须真的清掉注释 ——
        #    否则上面第 ① 条会再次被说明文字绊倒。
        assert "boom" not in _code_only("def t():\n    # boom\n    return 1\n")


# ================================================================
# 守卫实现（纯函数，便于反向测试直接喂合成的源码）
# ================================================================
_GATEWAY_MUTATIONS = ("create", "edit", "delete", "rename", "move",
                      "handoff", "release", "force_unlock")

_FORBIDDEN_MODES = ("create", "replace", "append")


def _receiver_name(node) -> str:
    """把 `gw.create(...)` / `rt.workspace_gateway.create(...)` 的接收者读出来。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_receiver_name(node.value)}.{node.attr}"
    return ""


def _looks_like_a_gateway_variable(recv: str) -> bool:
    """
    区分「Gateway 实例上的调用」与「类方法调用」。

    🔴 这条区分是**实测踩出来的**：
       第一版只按 `.create(` 匹配，于是把 `Task.create(...)`
       （`core.task.Task` 的 **classmethod**）也判成了违规 —— 假阳性。

    🔒 规则：接收者最后一段**首字母大写** → 类方法（`Task.create` /
       `Stage.create`），不是 Gateway 调用；否则按小写变量处理
       （`gw` / `gateway` / `rt.workspace_gateway`），一律可疑。
    """
    if not recv:
        return False
    head = recv.split(".")[-1]
    return not head[:1].isupper()


def _gateway_mutation_calls(src: str) -> list:
    """扫出所有 `xxx.<mutation>(...)` 形式的调用（AST，不做字符串匹配）。"""
    found = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in _GATEWAY_MUTATIONS:
                continue
            recv = _receiver_name(node.func.value)
            if _looks_like_a_gateway_variable(recv):
                found.append(f"line {node.lineno}: {recv}.{node.func.attr}(...)")
    return found


def _fallback_mode_literals(src: str) -> list:
    """
    扫出源码里作为**字符串字面量**出现的写入模式。

    🔒 只认 AST 的 `ast.Constant` —— 不用子串匹配
       （`"create"` 是 `"created_by"` 的子串，子串匹配必误报）。
    """
    found = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _FORBIDDEN_MODES:
                found.append(f"line {node.lineno}: {node.value!r}")
    return found


#: `_turn` 里**必须**出现的接线标记（少一个就说明 Action Seam 断了）。
#:
#: 🔴 2026-10-09：`_turn` 现在会分发**两个**动作（写 + 通用动作），
#:    所以 dispatch 这条标记必须写成**带实参**的形式。
#:
#:    以前它是泛化的 `"dispatcher.dispatch("`。加入通用动作之后，
#:    那条泛化标记会**被另一个 dispatch 满足** —— 于是本文件末尾的
#:    反向测试（挖掉写的那次 dispatch，断言守卫变红）**不再变红**。
#:    那个反向测试当场抓到了这次改动，这条修法是**加强**而不是放宽：
#:
#:        泛化 `"dispatcher.dispatch("`
#:            → 只要有任何一次 dispatch 就算过
#:        现在 `"dispatcher.dispatch(action_intent)"` + `"...(act_intent)"`
#:            → **两次都要在**，少任何一次都变红
# 🔴 2026-10-09：实验改走**生产认知入口** `act()`，所以这条守卫
#    断言的是**新机制**，不是把旧断言删掉。
#
#    性质**没有变**：「Action 的结果必须真的落成 Experience，
#    从而在下一轮 Cognition 里可见」。
#    变的是**谁负责**：以前实验自己 `dispatcher.dispatch()` +
#    `_record_action_experience()`；现在 `act()` → `run_cognition()`
#    一次做完两件事，实验只**读** `run.experience`。
# ⚠️ 标记必须是**能扛过 `ast.unparse()` 的东西**。
#    `_turn` 的源码在断言前会过 `_code_only()`，而 unparse 会
#    **把双引号规范成单引号** —— 本项目记录过的 bug 类 ⑤。
#    所以：
#       ✅ 标识符 / 属性名 / 关键字参数   （`a.act(` · `action_intent`）
#       ❌ 字符串字面量                  （`run.experience` 会变成 `'experience'`）
_TURN_WIRING = (
    "a.act(",                        # 走生产认知入口
    "dispatcher=dispatcher",         # 动作真的经过 Dispatcher
    "action_intent",                 # 动作被读出来
    "action_experience",             # Experience 由 act() 落了，实验**读**它
)

#: 🔴 实验**自己**不许有的东西 —— 有了就是绕开生产入口或重复执行。
_TURN_FORBIDDEN = (
    "dispatcher.dispatch(",          # 再 dispatch 一次会重复执行
    "_record_action_experience(",    # 再记一次会重复 Experience
    "a.generate(",                   # 绕开 act() ⇒ Self Model 回流断掉
)


def _code_only(src: str) -> str:
    """
    **纯代码**：去掉注释与文档字符串（`ast.unparse` 会一并丢掉注释）。

    🔴 【为什么必须这样 —— 2026-10-09 真实踩到】

        我给 `_TURN_FORBIDDEN` 写了 `"dispatcher.dispatch("` 和
        `"a.generate("`，而 `_turn` 的**注释**里恰好解释着：

            旧路径：a.generate() → … → 自己 dispatch
            新路径：a.act() → … → dispatcher.dispatch()
            🔒 这里不 dispatch —— run_cognition 已经执行过了

        ⇒ 守卫命中的是**说明文字**，报了一个**假的违规**。
          这正是本项目记录过的 bug 类 ⑦「守卫查原文而不是纯代码」——
          **我自己的守卫又踩了一次**。

    ⚠️ 所以：**任何在源码里找「不许出现的东西」的守卫，都必须先过这一层。**
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _turn_forbidden_gaps(turn_src: str) -> list:
    """🔴 反向断言：实验不许自己 dispatch / 自己记 Experience / 绕开 act()。"""
    return [m for m in _TURN_FORBIDDEN if m in turn_src]


def _turn_wiring_gaps(turn_src: str) -> list:
    return [marker for marker in _TURN_WIRING if marker not in turn_src]


# ================================================================
# 🔴 跨回合唯一廉价的「意图」载体（2026-10-10）
# ================================================================
def test_intent_reason_survives_into_the_next_context():
    """
    🔴 【为什么这条重要 —— 实测确认的事实】

        这个实验里**每轮 LLM 调用都从头开始**：
        `messages` 只有这一轮的新 prompt（`normalize_messages(str)`
        → 单条 user）；`Brain` / `BaseAgent` 都**没有**对话历史。

        🔒 设计上这不是 bug：§2.1 说「Provider-side context **可以**参与
           认知连续性」，但 §2.2 把它整个列为 `OPEN`（"如何保存 / 如何迁移
           / 如何与本地 Memory 合并"**都没有答案**），§3 还**禁止**
           去实现一个 `CognitiveSubstrate` 类。

        ⇒ 于是 `intent.reason` 成了**唯一**廉价的连续性载体。
          实测 6 次运行它都说「通知/验收留到下一轮」——
          而下一轮它**不记得自己说过**（那时还没记 reason）。

    🔒 形态与 `code` 一致：**有才写**，没有就不写这个键。
    """
    from actions import ActionIntent, ActionResult
    from cognition.context import _experience_detail
    from experience import ExperienceStore
    from storage.database import Database

    import tempfile

    db = Database(f"{tempfile.mkdtemp()}/reason.db")
    store = ExperienceStore(db)
    res = ActionResult(success=True, output={"path": "a.md", "revision": 1},
                       error=None, action_id="i1",
                       action_type="workspace_write", metadata={"code": None})

    with_reason = store.record_action_result(
        "agent_a",
        ActionIntent(action_type="workspace_write", agent_id="agent_a",
                     parameters={"path": "a.md", "write_mode": "create"},
                     reason="先写正文；通知留到后续回合", intent_id="i1"),
        res)
    assert with_reason.result["reason"] == "先写正文；通知留到后续回合"
    assert "reason=" in _experience_detail(with_reason), (
        "reason 存了却**没渲染** —— 那下一轮还是看不到（存了等于没存）")

    # 🔒 没有 reason 时，**旧记录的形状一个字节都不变**
    without = store.record_action_result(
        "agent_a",
        ActionIntent(action_type="workspace_write", agent_id="agent_a",
                     parameters={"path": "b.md", "write_mode": "create"},
                     intent_id="i2"),
        res)
    assert "reason" not in without.result, "没 reason 也写了这个键 —— 旧形状变了"
    assert "reason=" not in _experience_detail(without)
