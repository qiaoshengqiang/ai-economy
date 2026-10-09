"""
🔴 **Agent 的 system_prompt 里不许有「禁令」和「目标」**

【用户的要求】
    「系统提示词里你现在是对 Agent 做了限制吗，这个不应该要有。」

【为什么不该有 —— 两条设计原则】

    ① §0.5.5「**硬边界，软行为**」

        ✅ 正确                        ❌ 而不是
        没有 Workspace 权限 → 无法写入   Prompt 告诉 Agent「不要这样做」
        文件有 Lock → 无法并发写
        Ledger 余额不足 → 无法支付

       🔒 边界靠**结构**（Ledger 只追加 ⇒ 它**做不到**），不靠 prompt 说。
          在 prompt 里写「你不能修改别人的余额」，
          等于**用一句话代替一个结构** —— 而那句话既不可执行，
          又暗示"如果没有这句话你就能"。

    ② §13 Core Drives【`OPEN` —— 只记录方向，**禁止实现**】

        > 🔴 尤其不要现在就把下面这些硬编码成 Agent 的驱动：
        > 「提高能力」·「节约算力」·**「赚钱」**·**「避免死亡」**

       🔴 「你的目标是**长期存活**」= 避免死亡；「**赚取** AI 币」= 赚钱。
       §0.5.9 也写了：Core Drives **不应该只是普通 System Prompt**。

    ⇒ 所以 system_prompt 只陈述**身份**和**它真有的东西**（事实），
      不写规则，也不写目标。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config_resolver import load_resolved_config      # noqa: E402
from core.paths import GLOBAL_CONFIG_FILE                  # noqa: E402
from llm.router import adapter_specs                       # noqa: E402

AGENTS = ("agent_a", "agent_b", "agent_c", "agent_d", "agent_e")

#: 禁令式措辞 —— §0.5.5 说边界该在结构里，不在 prompt 里。
FORBIDDEN_RULES = ("必须", "不能", "不得", "禁止", "不要", "不允许", "严禁")

#: 目标指定 —— §13 明令禁止硬编码成驱动。
FORBIDDEN_GOALS = ("你的目标是", "长期存活", "避免死亡", "赚钱", "赚取",
                   "收益最大", "利润最大", "生存下去")


def _prompts():
    r = load_resolved_config(GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs)
    return {a: r.agent(a).system_prompt for a in AGENTS}


def test_no_restrictive_wording():
    """🔴 不许在 prompt 里**规定边界** —— 那是结构的事（§0.5.5）。"""
    for aid, sp in _prompts().items():
        for w in FORBIDDEN_RULES:
            assert w not in sp, (
                f"{aid} 的 system_prompt 里有禁令式措辞「{w}」—— "
                f"边界靠结构，不靠 prompt（§0.5.5）")


def test_no_goals_are_prescribed():
    """🔴 不许**指定目标** —— §13 明确禁止硬编码「赚钱」「避免死亡」。"""
    for aid, sp in _prompts().items():
        for w in FORBIDDEN_GOALS:
            assert w not in sp, (
                f"{aid} 的 system_prompt 里指定了目标「{w}」—— "
                f"§13 禁止把它硬编码成 Agent 的驱动")


def test_the_five_agents_are_not_given_different_roles():
    """
    🔒 §0.5.7：「不要预先规定「A 是程序员、B 是测试 Agent」」
       ⇒ 5 个 Agent 的 prompt **除了名字**必须完全一样。
         专业化应由**经济机制与长期反馈**涌现，不是系统预设。
    """
    prompts = _prompts()
    stripped = {a: sp.replace(a.replace("agent_", "Agent ").upper(), "")
                for a, sp in prompts.items()}
    # 去掉名字后再比（名字形如 "Agent A"）
    import re
    norm = {a: re.sub(r"Agent [A-E]", "Agent X", sp)
            for a, sp in prompts.items()}
    assert len(set(norm.values())) == 1, (
        f"5 个 Agent 的 prompt 不一样：{norm}")


def test_it_still_says_what_it_is_and_what_it_has():
    """
    🔒 又不是**什么都不说** —— 身份和它真有的东西是**事实**，该说：
       · 它是一个独立的经济主体
       · 它有自己的钱包 / 记忆 / 声誉
       · 它的智力来自外部 LLM（§2：Agent ≠ LLM）
    """
    for aid, sp in _prompts().items():
        assert "独立的经济主体" in sp, aid
        assert "钱包" in sp and "声誉" in sp, aid
        assert "外部 LLM" in sp, aid
