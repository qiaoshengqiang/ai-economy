"""
收尾窗口 · 回喂上限 · 契约测试脚手架
==========================================

三件在 5-Agent 空间站实验里暴露出来的问题：

① 回喂内容太大（6–11KB）→ 推理模型单轮 30–50k tokens；
② 墙钟最后没有验收窗口 → SUBMIT 之后没时间 VERIFY / COMPLETE；
③ 测试只断言「服务起得来」→ 契约字段漂移没有被发现。

本文件锁住：
    · `_in_final_window` 只按**真实墙钟**判定；
    · `run_cognition` 把字符上限传给回喂渲染；
    · 空间站脚手架把「断言契约字段」写进 DoD 与测试角色提示；
    · 收尾窗口只跳过实现者，不跳过生产者/验收人。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import ActionResult, run_cognition                # noqa: E402
from experiments.heterogeneous_production import production as P  # noqa: E402


# ------------------------------------------------------------
# 收尾窗口
# ------------------------------------------------------------
class TestFinalWindow:
    def test_only_real_wall_clock_counts(self):
        total = 100.0
        now = time.time()
        assert P._in_final_window(now - 70, total) is False
        assert P._in_final_window(now - 85, total) is True

    def test_zero_inputs_do_not_enter_the_window(self):
        assert P._in_final_window(0.0, 100.0) is False
        assert P._in_final_window(time.time() - 999, 0.0) is False

    def test_default_skip_is_empty_for_autonomy(self):
        assert P.FINAL_WINDOW_SKIP_AGENTS == ()


# ------------------------------------------------------------
# 回喂字符上限
# ------------------------------------------------------------
class _Resp:
    def __init__(self, content):
        self.content = content
        self.total_tokens = 10
        self.output_status = "VALID_CONTENT"
        self.cognitive_status = "COMPLETE"
        self.finish_reason = "stop"


class _Agent:
    def __init__(self, script):
        self.agent_id = "agent_x"
        self.script = list(script)
        self.calls = []
        self._i = 0

    def generate(self, messages=None, **kwargs):
        self.calls.append(messages)
        content = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        return _Resp(content)


class _ReadDispatcher:
    def dispatch(self, intent):
        return ActionResult(
            success=True,
            output={"path": "docs/big.md", "content": "x" * 5000},
            action_id=intent.intent_id,
            action_type=intent.action_type)


class TestFollowUpLimits:
    def _first_envelope(self):
        return json.dumps({
            "decision": "act", "reasoning": "读文档",
            "action": {"type": "workspace_read",
                       "parameters": {"path": "docs/big.md"}},
        })

    def test_limits_are_passed_to_the_renderer(self):
        a = _Agent([self._first_envelope(), "读完了。"])
        run_cognition(
            a, [{"role": "user", "content": "go"}],
            dispatcher=_ReadDispatcher(),
            follow_up_on_reads=True,
            follow_up_per_payload_max=100,
            follow_up_total_max=200)

        assert len(a.calls) == 2, "读取成功必须触发 follow-up"
        fed_back = a.calls[1][-1]["content"]
        # 截断必须**看得出来**，并给出完整长度
        assert "被**截断**" in fed_back
        assert "5000" in fed_back
        # 不该把 5000 字原样塞回去
        assert len(fed_back) < 500


# ------------------------------------------------------------
# 空间站脚手架：契约字段必须进 DoD / 测试角色
# ------------------------------------------------------------
class TestSpaceStationScaffold:
    def _src(self):
        return (ROOT / "experiments" / "tool_collab"
                / "run_space_station.py").read_text(encoding="utf-8")

    def test_dod_requires_contract_fields(self):
        src = self._src()
        assert "`/api/state` 包含契约字段" in src
        assert "测试断言契约字段形状" in src

    def test_test_role_asserts_shape_not_only_200(self):
        src = self._src()
        assert "断言 `/api/state` 的字段形状" in src
        assert "只断言 HTTP 200 不算通过" in src

    def test_guided_mode_skips_implementers_in_closing_window(self):
        src = self._src()
        assert 'P.FINAL_WINDOW_SKIP_AGENTS = (' in src
        assert '"agent_c", "agent_d"' in src
