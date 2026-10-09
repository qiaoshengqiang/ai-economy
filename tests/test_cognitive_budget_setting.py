"""
认知预算（COGNITIVE_BUDGET）设置 —— 测试
==========================================

🔴 背景（2026-10-09）：

    `COGNITIVE_BUDGET` 从 **8192 抬到 16384**。三个依据：

      ① 行业默认就是 16384 —— Pydantic AI 把 Anthropic 的默认
         `max_tokens` 从 4096 抬到了 16384（Claude Sonnet 4.5+）。
      ② Agent 配置本来就是 16384（`config/config.yaml` 的
         `brain.max_tokens`）—— 8192 反而把它压低了一半，
         因为 `requested_cognitive_budget` 优先于配置。
      ③ 一次真实截断就是被 8192 撞出来的：作者要把整章写进 JSON 的
         单个 `content` 字段，超了就截断 → 解析全失败 → 整轮不写入。

🔒 这些测试**不调用任何 LLM**：直接调用 Provider 的纯函数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                  # noqa: E402

load_dotenv(ROOT / ".env")

EXPECTED_DEFAULT = 16384


# ============================================================
# ① 单一来源
# ============================================================
def test_production_default_is_16384():
    from experiments.heterogeneous_production import production as P

    assert P.COGNITIVE_BUDGET == EXPECTED_DEFAULT, (
        "实验默认认知预算应当是 16384（行业默认 / Agent 配置同值）")


def test_sibling_scripts_share_the_same_value():
    """
    `preflight.py` / `artifact_smoke.py` 必须**导入**同一个常量，
    不许各自再写一份数字 —— 以前三处各写 8192，改一处漏一处。
    """
    from experiments.heterogeneous_production import artifact_smoke as AS
    from experiments.heterogeneous_production import preflight as PF
    from experiments.heterogeneous_production import production as P

    assert PF.COGNITIVE_BUDGET == P.COGNITIVE_BUDGET
    assert AS.COGNITIVE_BUDGET == P.COGNITIVE_BUDGET


def test_no_leftover_8192_literals():
    """
    🔒 结构守卫：实验里不许再出现写死的 `requested_cognitive_budget=8192`。

    ⚠️ 只看**可执行代码**（AST），不看注释 —— 否则说明文字里
       提到旧的 8192 会把自己绊倒。
    """
    import ast

    d = ROOT / "experiments" / "heterogeneous_production"
    offenders = []
    for path in sorted(d.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "requested_cognitive_budget" and \
                        isinstance(kw.value, ast.Constant):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"还有写死的认知预算字面量：{offenders} —— 应统一从 "
        f"production.COGNITIVE_BUDGET 取")


# ============================================================
# ② 它真的会变成 Provider 的 max_tokens（而不是被裁掉）
# ============================================================
_PROVIDERS: dict = {}


def _provider(name: str):
    """
    拿一个**真实** Provider 实例（不联网 —— 只用它的纯函数）。

    🔒 走 `LLMRouter.get_provider()`，也就是生产路径本身，
       不自己拼构造参数（那样测的就不是真东西了）。
    """
    if name not in _PROVIDERS:
        from core.config import load_global_config
        from llm.router import LLMRouter

        _PROVIDERS[name] = LLMRouter(load_global_config()).get_provider(name)
    return _PROVIDERS[name]


def test_16384_is_not_clipped():
    """
    🔒 16384 必须**原样通过**裁剪（`clipped is None`）。

    它同时等于 Agent 配置的 `max_tokens`，所以既不低于 MIN(256)、
    不高于 MAX(32768)，也不高于调用方给的上限。
    """
    p = _provider("bailian")
    eff, why = p.clip_cognitive_budget(EXPECTED_DEFAULT, EXPECTED_DEFAULT)
    assert (eff, why) == (EXPECTED_DEFAULT, None), (
        f"16384 被裁成了 {eff}（原因 {why!r}）—— 那它就传不到 Provider")


def test_clip_boundaries_unchanged():
    """裁剪边界行为没被这次改动影响。"""
    p = _provider("bailian")
    # 低于 MIN → 抬到 MIN
    assert p.clip_cognitive_budget(10, 16384)[0] == p.MIN_COGNITIVE_BUDGET
    # 高于 MAX → 压到 MAX
    assert p.clip_cognitive_budget(99999, 99999)[0] == p.MAX_COGNITIVE_BUDGET
    # 没请求 → 用调用方默认
    assert p.clip_cognitive_budget(None, 4096) == (4096, None)


def test_cannot_exceed_the_agent_configured_max_tokens():
    """
    ⚠️ **一个必须知道的约束**：`requested_cognitive_budget` 超不过
    Agent 配置的 `max_tokens`。

        clip_cognitive_budget(32768, fallback=16384)
            → (16384, "above_caller_limit")

    所以想再往上抬（例如 32768），**光加参数没用** —— 还得同时改
    `config/config.yaml` 里那个 Agent 的 `brain.max_tokens`。
    """
    p = _provider("bailian")
    eff, why = p.clip_cognitive_budget(32768, EXPECTED_DEFAULT)
    assert eff == EXPECTED_DEFAULT
    assert why == "above_caller_limit"


def test_payload_carries_the_value():
    """
    最终 HTTP 请求体里的那个字段必须等于 16384。

    🔒 注意两家用的字段名不同：bailian 走 `max_tokens`，
       doubao 走 `max_completion_tokens`（配置里声明的）。
    """
    from experiments.heterogeneous_production import production as P

    for name, field in (("bailian", "max_tokens"),
                        ("doubao", "max_completion_tokens")):
        p = _provider(name)
        eff, _ = p.clip_cognitive_budget(P.COGNITIVE_BUDGET, EXPECTED_DEFAULT)
        payload = p.build_payload(
            messages=[{"role": "user", "content": "hi"}],
            model="some-model", temperature=0.7, max_tokens=eff)
        assert payload.get(field) == EXPECTED_DEFAULT, (
            f"{name} 的 payload.{field} = {payload.get(field)}，"
            f"应当是 {EXPECTED_DEFAULT}")
        assert payload.get("stream") is False, (
            "我们仍然发非流式 —— 抬到 16384 是安全的，"
            "但再往上就要先有流式能力")


def test_still_within_non_streaming_practical_limit():
    """
    🔒 16384 仍在「非流式请求」的实际安全区内。

    Anthropic 的非流式 SDK 上限约 21,000 tokens（Pydantic AI 的记录）。
    我们永远发 `stream: False`，所以这个上限对我们是有意义的约束 ——
    这也是**没有**一次抬到 32768 的原因。
    """
    assert EXPECTED_DEFAULT < 21000, (
        "超过约 21000 就需要先支持流式，否则某些端点会直接 400")
