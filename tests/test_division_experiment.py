# 🔴 2026-10-10：删掉「思考过程显示」的 3 条守卫 —— 功能本身已按用户要求**移除**。
#    它在真实终端上不稳定（擦除依赖 TTY 时序），用户实测看不到。
#    **不留无功能的守卫。**

"""
观察实验（agent_division）—— **结构守卫**，不调 LLM

【🔴 钉住的是「不干涉」这条性质】

    最高原则：**不要设计合作行为本身，要设计让合作值得发生的环境。**（§0.5）

    所以这个 runner **不许**：
        ❌ 预设命名空间（那是替它们分工）
        ❌ 建 DoD / 指派验收责任（那是替它们决定谁来验收）
        ❌ 发 START / SUBMIT / VERIFY / COMPLETE（那是替它们完成任务）
        ❌ 轮转排班（`for aid in AGENTS`）—— 那是系统在排班

    ✅ 它只做三件事：给环境 · 把 Demand 给第一个 Agent · **记录**
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC_PATH = (ROOT / "experiments" / "agent_division" / "run_division.py")


def _src() -> str:
    return SRC_PATH.read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """纯代码（去掉注释与文档字符串）。

    🔒 必须这样 —— 本文件的**注释里**就写着
       「❌ 不许发 START / SUBMIT / VERIFY / COMPLETE」，
       直接用原文会被自己的说明文字绊倒（本项目记录过的 bug 类 ⑦）。
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


@pytest.fixture(scope="module")
def code():
    c = _code_only(_src())
    assert len(c) > 2000, f"清理后只剩 {len(c)} 字符 —— 守卫可能在对着空串断言"
    return c


def test_it_does_not_preset_namespaces(code):
    """❌ 不许预设命名空间 —— 谁建目录、给谁是**他们的**决定。"""
    assert "provision_namespaces" not in code
    assert "ARTIFACT_NAMESPACES = {}" in code, (
        "必须显式**清空** ARTIFACT_NAMESPACES，否则会继承旧实验的预设分工")


def test_it_does_not_drive_the_completion_chain(code):
    """❌ 不许替 Agent 完成 —— 完成链应该由 Agent 自己走。"""
    for forbidden in ("_try_complete_task", "_ensure_completion_setup"):
        assert forbidden not in code, f"runner 调了 {forbidden} —— 那是替 Agent 完成"


def test_it_does_not_send_protocol_messages(code):
    """❌ 不许发 START / SUBMIT / VERIFY / COMPLETE。"""
    for mt in ("MessageType.START", "MessageType.SUBMIT",
               "MessageType.VERIFY", "MessageType.COMPLETE",
               "MessageType.TASK_OFFER"):
        assert mt not in code, f"runner 发了 {mt} —— 那是替 Agent 推进"


def test_it_does_not_round_robin(code):
    """
    ❌ 不许 `for aid in AGENTS` 轮转 —— 那是**系统在排班**。
       只跑被消息唤醒的人（§22.27：消息到达本身就是唤醒事件）。
    """
    assert "for aid in AGENTS" not in code
    assert "is_woken" in code, "没有基于唤醒信号筛选 —— 那它是靠什么决定跑谁？"


def test_first_agent_is_root_controller(code):
    """🔒 第一个 Agent = Creator = root Controller（这是"分不分"的结构前提）。"""
    assert "ensure_workspace(WS_ID, first)" in code
    assert "creator=first" in code


def test_human_cannot_send_through_the_communication_layer(code):
    """
    🔴 真实踩到：通信层**只接受已登记 Agent 作为发送方** ——

           AgentNotFoundError: 发送方 'human' 没有接入通信层

       所以 runner **不许**假扮 Human 发消息。设计说「Human 是正式协作
       Actor」（ADR-0022），但**实现里没有 Human 入口** ——
       那是一个真实缺口，属于设计决定，**不该在实验里悄悄绕过**。
    """
    assert 'from_agent=HUMAN' not in code
    assert 'from_agent="human"' not in code


def test_report_refuses_to_conclude_without_a_turn(code):
    """
    🔴 报告必须先看「这次有没有获得观察」。

       真实踩到：Demand 没送进去 ⇒ 0 个 turn ⇒ 报告却说
       「**一条消息都没有** —— 第一个 Agent 谁也没找」。
       **那是错的**：它根本没轮到过。
    """
    assert "没有获得观察" in _src(), "报告缺少「没获得观察」的分支"
    assert "AGENTS_FIRST" in code


# ================================================================
# 🔴 「模型没返回内容」必须被响亮地说出来（2026-10-10 真实踩到）
# ================================================================
def test_report_distinguishes_empty_content_from_a_choice():
    """
    实测（03:08）：`agent_a` 的 turn 是

        finish_reason=length · output_status=EMPTY_CONTENT ·
        cognitive_status=TRUNCATED · chars=0

    而报告却说「agent_a 跑过，但**没有把工作分给任何人**」——
    **它什么都没产出，凭什么说它"选择不分工"？**

    🔒 「Agent 跑过且选择不做」与「Agent 跑过但模型没返回」
       长得一模一样，**必须分开**。否则报告在替 Agent 编故事。
    """
    src = SRC_PATH.read_text(encoding="utf-8")
    assert "EMPTY_CONTENT" in src, "报告没有识别 EMPTY_CONTENT"
    assert "TRUNCATED" in src, "报告没有识别 TRUNCATED"
    assert "模型没返回内容" in src, "没有把两者区分开的文字"
    assert "不能**用来回答" in src or "不能" in src


def test_it_prints_model_and_budget_up_front():
    """
    🔴 早暴露：`agent_a` 的 `max_tokens` 是 **2048**，
       而 `deepseek-flash` 是**推理模型** —— 推理把 2048 吃满，
       `content` 一个字都不剩。这个数字早打印出来就不会查半天。
    """
    src = SRC_PATH.read_text(encoding="utf-8")
    assert "max_tokens" in src, "环境说明里没有打印 max_tokens"


def test_neutral_agents_have_a_workable_output_budget():
    """
    🔒 `agent_a..e` 曾经是 `max_tokens: 2048`（老配置遗留）。
       对**推理模型**（deepseek-flash）和写长文都远远不够 ⇒
       `finish_reason=length` + content 为空。
    """
    import yaml

    cfg = yaml.safe_load(
        (ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    for a in ("agent_a", "agent_b", "agent_c", "agent_d", "agent_e"):
        mt = ((cfg["agents"][a].get("brain") or {}).get("max_tokens"))
        assert mt is not None and int(mt) >= 8192, (
            f"{a} 的 max_tokens={mt} 太小 —— 推理模型会把 content 挤空")


def test_task_and_workspace_ids_do_not_leak_the_experiment_intent():
    """
    🔴 实测抓到的泄露：原先 `WS_ID = "WS-DIVISION-001"`，
       而 agent_a 的**思考**里出现了

           「the task says **"division"** — maybe agents are supposed to
             divide work? **But nothing says that.**」

       ⇒ 是**我们自己的 id 在提示它"这个实验要分工"** ——
         和 `novel_world_builder` 那种"名字即分工"是同一类问题。
    """
    import experiments.agent_division.run_division as D

    for value in (D.TASK_ID, D.WS_ID):
        low = value.lower()
        for hint in ("division", "divide", "novel", "author", "role",
                     "split", "share"):
            assert hint not in low, f"{value!r} 里有实验意图暗示：{hint}"


# ================================================================
# 🔴 同一个 DB 里换 Task 再跑 —— 不许撞 stage_id（2026-10-10 真实踩到）
# ================================================================
def test_two_tasks_in_one_db_do_not_collide_on_stage_id(tmp_path,
                                                        monkeypatch):
    """
    实测：
```
        sqlite3.IntegrityError: UNIQUE constraint failed:
            production_stages.stage_id
```
    根因：`ROOT_STAGE_ID` 借了 `production.ROOT_STAGE_ID`
    （固定常量 `"stage_hetero_root"`），而 `stage_id` 是 **UNIQUE** 的。
    `list_stages(TASK_ID)` 按 **task_id** 查 ⇒ **查不出** stage_id 的冲突。

    🔒 所以 stage_id 必须**跟着 task 走**。
    """
    from main import build_runtime

    import experiments.agent_division.run_division as D
    from experiments.heterogeneous_production import production as P

    db = str(tmp_path / "two.db")
    agents = ["agent_a", "agent_b"]

    def _one(task_id: str, ws_id: str):
        monkeypatch.setattr(D, "TASK_ID", task_id)
        monkeypatch.setattr(D, "WS_ID", ws_id)
        monkeypatch.setattr(D, "ROOT_STAGE_ID", f"stage_{task_id}_root")
        monkeypatch.setattr(D, "AGENTS", list(agents))
        monkeypatch.setattr(D, "DEMAND", "d")
        monkeypatch.setattr(P, "TASK_ID", task_id)
        monkeypatch.setattr(P, "WS_ID", ws_id)
        monkeypatch.setattr(P, "AGENTS", list(agents))
        monkeypatch.setattr(P, "ARTIFACT_NAMESPACES", {})
        rt = build_runtime(db_path=db, agent_ids=list(agents))
        try:
            D._setup(rt)
        finally:
            rt.close()

    _one("task_A", "WS-A")          # 第一个 Task
    _one("task_B", "WS-B")          # 同一个 DB 里再来一个 —— 以前这里会炸


def test_stage_id_follows_the_task_id():
    """stage_id 必须是**按 task 派生**的，不能是固定常量。"""
    import experiments.agent_division.run_division as D

    assert D.ROOT_STAGE_ID == f"stage_{D.TASK_ID}_root"
    assert D.ROOT_STAGE_ID != P_ROOT_STAGE_ID(), (
        "又借了 production 的固定 stage_id —— 换 Task 就会撞 UNIQUE")


def P_ROOT_STAGE_ID():
    from experiments.heterogeneous_production import production as P

    return P.ROOT_STAGE_ID


def test_default_run_directory_is_per_run():
    """
    🔒 这是**观察实验**，不是累计系统 ——
       默认共用一个 DB 会让第二次跑**继承上一次的 Task / 工作区**，
       观察结果被污染（实测：第二次跑直接 UNIQUE 冲突）。
    """
    src = SRC_PATH.read_text(encoding="utf-8")
    assert "RUNS" in src and "runs" in src
    assert "每次运行独立" in src or "每次一个全新目录" in src


# ================================================================
# 🔴 prompt 里**不许出现**「轮 / 回合 / 下一步 / 后续」（2026-10-10）
# ================================================================
def test_the_prompt_does_not_contain_any_next_step_language():
    """
    【用户的原话】

        「轮次是旧的那个实验里的概念，和新的实验没关系。
          我们想观察到的场景是第一个 agent **什么时候能想到把活进行分工**，
          而不是**想着下一轮给谁**。」

    🔴 而实测：旧模板（`production.PROMPT`）里塞了这些「你还有下一步」的暗示——

        「你**这一轮**做什么」
        「**一轮**一个动作……想都做就分**两轮**」
        「只有在收到消息时，才会有你的**下一个回合**」
        「结果会进入你的经历，在**下一轮**认知里才可见」
        `Session: … **最多再跑 6 轮** · 到点后不再有新的回合`
        `距离结项还差：没有 DoD、没有 Completion Proposal、没有验收责任人…`

    ⇒ 环境**反复告诉它"还有下一步"** ⇒ 它把「分工」想成「下一轮给谁」。
      **那不是分工，是拖延。**

    🔒 所以这个实验用自己的模板，并且**这些词一个都不许有**。
    """
    import re

    from experiments.agent_division.prompt import PROMPT as T

    for word in ("轮", "回合", "下一步", "后续"):
        assert not re.search(word, T), (
            f"观察实验的 prompt 里出现了「{word}」—— 那会把分工变成「下一轮给谁」")


def test_the_prompt_has_no_leftovers_from_the_old_experiment():
    """旧实验的框架**不许**跟过来（Stage · Session · 结项待办）。"""
    from experiments.agent_division.prompt import PROMPT as T

    for leftover in ("当前 Stage", "Session:", "距离结项还差",
                     "Definition of Done", "Completion Proposal",
                     "Acceptance Responsibility"):
        assert leftover not in T, f"旧实验的残留跟过来了：{leftover}"


def test_it_uses_its_own_template():
    """`_turn` 必须收到本实验的模板，而不是 `production.PROMPT`。"""
    src = SRC_PATH.read_text(encoding="utf-8")
    assert "prompt_template=DIVISION_PROMPT" in src
    # 而且 `_turn` 要支持这个参数（默认行为不变）
    prod = (ROOT / "experiments" / "heterogeneous_production"
            / "production.py").read_text(encoding="utf-8")
    assert "prompt_template: Any = None" in prod
    assert "prompt_template if prompt_template is not None else PROMPT" in prod


def test_other_agents_are_told_they_got_no_direct_order():
    """
    🔒 「人类命令」只给第一个 Agent。其他人看到的是**事实**
       （"人类没有直接给你命令"），不是"你该去问别人"。
    """
    from experiments.agent_division.prompt import NO_DIRECT_ORDER

    assert "没有" in NO_DIRECT_ORDER
    for nudge in ("你该", "你应该", "去找", "建议"):
        assert nudge not in NO_DIRECT_ORDER


# ================================================================
# 🔴 分发给 Agent 的 prompt 里**不许有规范性句子**（2026-10-10）
# ================================================================
def test_the_prompt_only_states_facts_not_instructions():
    """
    【用户的指正】

        「是你在测试的时候**分发不该有的提示词**。」

    🔴 实测分发出去的 prompt 里有 3 句是**指导行为**，不是环境事实：
        · 「写不完的长文**用 append，不要用 replace**」   ← 建议
        · 「**不要**整篇复制进 message —— 正文属于 Workspace」← 规则 + 策略
        · 「**先去查它**」                                  ← 引导

    为什么不该有：
        §0.5 —— **不要设计行为本身**。环境只陈述「它有什么、机制怎么走」；
        「用不用、怎么用」是 Agent 的判断。
        而且这三句**都可以被事实替代**：
        · 三个 write_mode 的语义上面已经作为**事实**列过了
        · 「公共资料区里有一份 SYSTEM_MAP.md」是事实；去不去查不必我说

    🔒 保留的「必须是」是**被强制执行的约束**（执行器会拒），那是**事实**，
       不是建议 —— 所以下面的词表**不含**「必须」。
    """
    import re

    from experiments.agent_division.prompt import PROMPT as D
    from experiments.heterogeneous_production import production as P

    forbidden = ("不要", "⚠️", "建议", "先去", "别 ")
    for name, text in (("观察实验", D), ("production", P.PROMPT)):
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue
            for w in forbidden:
                assert w not in s, (
                    f"{name} 的 prompt 里分发了规范性句子（{w}）：{s}")


def test_the_prompt_does_not_prescribe_a_write_mode():
    """🔒 三个 mode 的**语义**是事实，该说；**推荐哪个**是行为设计，不该说。"""
    from experiments.agent_division.prompt import PROMPT as D
    from experiments.heterogeneous_production import production as P

    for text in (D, P.PROMPT):
        assert '"append"  = 末尾追加' in text, "mode 的语义（事实）不能删"
        assert "不要用 \"replace\"" not in text
