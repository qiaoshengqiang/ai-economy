"""
Work run checkpoint：跨 run 的事实快照
======================================

验证：

    · 失败扫描跨轮，不只看最后一轮；
    · `run_tool` 的 ActionResult.success=True 但工具非零退出也算失败；
    · 总结取最近一条非空轮次总结；
    · render 把失败轮次和原因写给下一次 resume。
"""

from __future__ import annotations

from work.checkpoint import checkpoint_from_rounds


def _round(number, *, summary="", actions=(), results=()):
    return {
        "round": number,
        "run_id": "run_before",
        "summary": summary,
        "segments": [{
            "actions": list(actions),
            "results": list(results),
        }],
    }


def test_failures_are_collected_across_rounds():
    rounds = [
        _round(
            1,
            actions=[{
                "action_type": "run_tool",
                "parameters": {
                    "tool": "playwright",
                    "args": ["tests/app.spec.js"],
                },
            }],
            results=[{
                "success": True,
                "output": {
                    "ran": True,
                    "ok": False,
                    "exit_code": 1,
                    "stdout": (
                        "\nRunning 2 tests using 1 worker\n"
                        'Error: expect(received).toBe(expected)\n'
                        'Expected: "running"\n'
                        'Received: "no-context"'),
                },
            }],
        ),
        _round(
            2,
            summary="继续读文件，没有执行工具。",
            actions=[{
                "action_type": "workspace_read",
                "parameters": {"path": "src/app.js"},
            }],
            results=[{"success": True, "output": {"content": "x"}}],
        ),
    ]

    checkpoint = checkpoint_from_rounds("task_x", rounds)

    assert checkpoint.summary == "继续读文件，没有执行工具。"
    assert len(checkpoint.action_failures) == 1
    failure = checkpoint.action_failures[0]
    assert failure["round"] == 1
    assert failure["action"] == "run_tool"
    assert failure["target"] == "playwright tests/app.spec.js"
    assert 'Expected: "running"' in failure["error"]
    assert 'Received: "no-context"' in failure["error"]

    rendered = checkpoint.render()
    assert "round 1: run_tool playwright tests/app.spec.js" in rendered
    assert 'Expected: "running"' in rendered


def test_summary_skips_empty_last_round():
    rounds = [
        _round(1, summary="真正的总结"),
        _round(2, summary=""),
    ]

    checkpoint = checkpoint_from_rounds("task_x", rounds)

    assert checkpoint.summary == "真正的总结"


def test_direct_action_failure_is_kept():
    rounds = [_round(
        1,
        actions=[{
            "action_type": "workspace_write",
            "parameters": {"path": "README.md"},
        }],
        results=[{
            "success": False,
            "error": "路径不存在：README.md",
            "metadata": {"code": "not_found"},
        }],
    )]

    checkpoint = checkpoint_from_rounds("task_x", rounds)

    assert checkpoint.action_failures[0]["action"] == "workspace_write"
    assert checkpoint.action_failures[0]["target"] == "README.md"
    assert "路径不存在" in checkpoint.action_failures[0]["error"]
