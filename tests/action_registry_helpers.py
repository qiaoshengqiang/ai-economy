"""
「环境里真正注册了哪些动作」—— 供多个守卫共用
=================================================

🔒 **唯一的真相是 `production._action_seam()` 里那份执行器列表。**

    不是 prompt、不是 `SYSTEM_MAP.md`、也不是本文件。
    本文件只是把那份列表**读出来**，给守卫用。

【为什么要抽成共用模块】

    已经有两个守卫需要它：

        `test_prompt_delivers_registered_actions.py`
            —— 注册了的动作必须**告诉** Agent（prompt 里要有）
        `test_system_map_is_current.py`
            —— 注册了的动作必须写进**公共资料**（SYSTEM_MAP 里要有）

    各写一份「怎么算注册了」= 两份真相。抽出来只有一份。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION = (PROJECT_ROOT / "experiments"
              / "heterogeneous_production" / "production.py")
ACTIONS_DIR = PROJECT_ROOT / "actions"


def registered_executor_classes() -> set:
    """
    从 `_action_seam()` 里扫出它注册了哪些执行器类。

    🔒 用 AST 取那**一个函数**的源码段，再正则找 `XxxExecutor(` ——
       不是全文搜（全文搜会把 docstring 里的例子也算进来）。
    """
    src = PRODUCTION.read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_action_seam")
    return set(re.findall(r"(\w+Executor)\(", ast.get_source_segment(src, fn)))


def action_types_of(class_names) -> set:
    """这些执行器类各自认领哪些 `action_type`。"""
    import actions

    types = set()
    for name in sorted(class_names):
        cls = getattr(actions, name, None)
        assert cls is not None, f"{name} 不在 actions 的导出里"
        types |= set(getattr(cls, "action_types", ()) or ())
    return types


def registered_action_types() -> set:
    """环境里**真正注册**的全部 `action_type`。"""
    return action_types_of(registered_executor_classes())


def executor_param_keys(class_name: str) -> set:
    """
    这个执行器从 `intent.parameters` 里读了哪些键。

    🔒 只看**这个类自己的**方法体里的 `params.get("…")` ——
       不看别的类、不看注释。
    """
    for path in sorted(ACTIONS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef)
                    and node.name == class_name):
                continue
            keys = set()
            for call in ast.walk(node):
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "get"
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "params"
                        and call.args
                        and isinstance(call.args[0], ast.Constant)
                        and isinstance(call.args[0].value, str)):
                    continue
                keys.add(call.args[0].value)
            return keys
    raise AssertionError(
        f"在 actions/*.py 里找不到执行器类 {class_name} —— "
        f"它被改名或搬走了，调用方的守卫已经失效")


def action_type_to_executor() -> dict:
    """`action_type → 执行器类名`（用于把参数检查绑到具体类上）。"""
    import actions

    out = {}
    for name in sorted(registered_executor_classes()):
        cls = getattr(actions, name, None)
        assert cls is not None, f"{name} 不在 actions 的导出里"
        for t in (getattr(cls, "action_types", ()) or ()):
            out[str(t)] = name
    return out
