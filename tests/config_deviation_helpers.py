"""
`config_deliberate_deviations.json` 的读取辅助
================================================

【为什么单独抽出来】

    同一个「有意偏离」会被两个迁移基线测试读取：

        tests/test_config_resolver.py
        tests/test_resolved_config_runtime.py

    如果两边各写一份解析，很容易一边支持 `model`、另一边只支持
    `max_tokens` —— 于是「测试通过」取决于你跑的是哪一个文件。

    🔒 这个 helper 只做读取与**理由校验**，不改变任何断言强度：
       没有登记在 deviations 里的 Agent 仍然必须与迁移前快照一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

_HERE = Path(__file__).resolve().parent


def load_deviations() -> Dict[str, Any]:
    path = _HERE / "config_deliberate_deviations.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def require_reason(devs: Dict[str, Any], section: str) -> None:
    """
    🔒 有偏离就必须有理由。没有理由的偏离 = 偷偷改变行为。
    """
    values = devs.get(section) or {}
    assert not values or (devs.get("_reasons") or {}).get(section), (
        f"有意偏离 {section!r} 必须带理由（_reasons）")


def model_deviation(devs: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    """
    某个 Agent 的模型/端点偏离；没有登记就返回空 dict。

    可用的键：
        requested_model · provider · base_url · credential_env
    """
    return dict(((devs.get("model") or {}).get(agent_id)) or {})
