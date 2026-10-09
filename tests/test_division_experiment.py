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
