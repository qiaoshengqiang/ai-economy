"""
🔴 守卫：**注册了的动作，必须告诉 Agent。**

【为什么需要它（2026-10-09 真实抓到）】

    实验里注册了 **12** 个执行器，而 prompt 只列了 **5** 个 ——
    `resource_create` / `resource_handoff` / `resource_release` /
    `assign_acceptance` **完全没告诉 Agent**。

    后果和 `_workspace_facts` 那条教训是**同一个**：
        「不列出来 Agent 就不知道它存在」
    —— 注册了但没投递 = 那个能力**等于不存在**。

    ⚠️ 而且它不会让任何测试变红：能力确实注册了、也确实能跑，
       只是**没有任何 Agent 会去用它**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 🔒 「怎么算注册了」只有**一份**真相 —— 见 `tests/action_registry_helpers.py`
#    （`test_system_map_is_current.py` 也用它）。
from tests.action_registry_helpers import (                     # noqa: E402
    action_types_of as _action_types_of,
    registered_executor_classes as _registered_executor_classes,
)

PROD = (ROOT / "experiments" / "heterogeneous_production" / "production.py")


def test_every_registered_action_type_is_documented_in_the_prompt():
    import experiments.heterogeneous_production.production as P

    classes = _registered_executor_classes()
    types = _action_types_of(classes)
    assert len(types) >= 10, f"只扫到 {sorted(types)} —— 守卫可能没在工作"

    prompt = P.PROMPT
    missing = sorted(t for t in types if t not in prompt)
    assert not missing, (
        f"这些动作**注册了但没告诉 Agent**：{missing}\n"
        f"⇒ 注册了但没投递 = 那个能力等于不存在（Agent 不会去用它）")


def test_the_guard_is_not_vacuous():
    """反向：往 prompt 里删掉一个已知动作 ⇒ 守卫必须能发现。"""
    import experiments.heterogeneous_production.production as P

    classes = _registered_executor_classes()
    types = _action_types_of(classes)
    victim = sorted(types)[0]
    # 模拟一个"漏了"的 prompt
    broken = P.PROMPT.replace(victim, "X" * len(victim))
    assert victim not in broken
    assert [t for t in types if t not in broken] == [victim]


def test_write_mode_is_shown_before_content_in_a_filled_shape():
    """
    🔴 2026-10-10 真实踩到：`write_mode` 被漏了。

        旧契约（**模型会给**）     新契约（**漏了**）
        · 填好的具体模板          · 一行 schema
        · write_mode 排在**第二** · write_mode 排在**最后**

    ⇒ 实测：`agent_a` 准备了 **1968 字符**的正文、路径也对，
      **只差 `write_mode`** ⇒ `bad_mode` ⇒ 那一次机会用掉了。

    🔒 这是「投递方式」问题，不是设计问题 ——
       所以修的是**形状**：给填好的样子，并把 `write_mode` 提到 `content` 之前。
    """
    import experiments.heterogeneous_production.production as P

    prompt = P.PROMPT
    i = prompt.index('· "workspace_write"')
    j = prompt.index('· "workspace_read"', i)
    block = prompt[i:j]

    assert '"write_mode"' in block and '"content"' in block
    assert block.index('"write_mode"') < block.index('"content"'), (
        "write_mode 又排到 content 后面了 —— 实测模型会漏掉它")
    # 必须是**填好的形状**（有具体取值），不是一行 schema
    assert '"append"' in block or '"create"' in block, (
        "只给了 schema 没给填好的取值 —— 那正是漏参数的原因")


def test_the_guard_is_not_vacuous_for_write_mode():
    """反向：把 write_mode 挪到 content 后面 ⇒ 守卫必须变红。"""
    import experiments.heterogeneous_production.production as P

    i = P.PROMPT.index('· "workspace_write"')
    j = P.PROMPT.index('· "workspace_read"', i)
    block = P.PROMPT[i:j]
    swapped = block.replace('"write_mode"', "TMP").replace(
        '"content"', '"write_mode"').replace("TMP", '"content"')
    assert swapped.index('"write_mode"') > swapped.index('"content"')
