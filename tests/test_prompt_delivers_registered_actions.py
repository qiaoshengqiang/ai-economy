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

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD = (ROOT / "experiments" / "heterogeneous_production" / "production.py")


def _registered_executor_classes() -> set:
    """从 `_action_seam` 里扫出它注册了哪些执行器类。"""
    src = PROD.read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_action_seam")
    return set(re.findall(r"(\w+Executor)\(", ast.get_source_segment(src, fn)))


def _action_types_of(class_names: set) -> set:
    import actions

    types = set()
    for name in sorted(class_names):
        cls = getattr(actions, name, None)
        assert cls is not None, f"{name} 不在 actions 的导出里"
        types |= set(getattr(cls, "action_types", ()) or ())
    return types


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
