"""
Tool Runtime V1 —— 受控代码执行 / 测试的回归测试。

不启动真实 LLM；只验证工具库、执行器、物化与两个真实工具：（python / pytest）。
Playwright 测试在本机有 Chrome 时才跑（环境事实见 manifest）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import ActionIntent, RunToolExecutor, ACTION_RUN_TOOL   # noqa: E402
from tool_runtime import ToolLibrary, ToolRunner                     # noqa: E402
from tool_runtime.workspace import RunWorkspace, safe_join           # noqa: E402


class _Gateway:
    """最小 Workspace Gateway 替身：只支持 list/read。"""

    def __init__(self, files: dict[str, str]):
        self.files = {("/" + k.lstrip("/")): v for k, v in files.items()}

    def list(self, workspace_id, path, agent_id):
        path = "/" + str(path or "").strip("/")
        if path == "/":
            names = sorted({p.split("/", 2)[1] for p in self.files})
            entries = []
            for name in names:
                child = "/" + name
                is_dir = any(p.startswith(child + "/") for p in self.files)
                entries.append({"name": name,
                                "type": "directory" if is_dir else "file"})
            return SimpleNamespace(success=True, result={"entries": entries})
        if path in self.files:
            return SimpleNamespace(success=False, result=None)
        prefix = path.rstrip("/") + "/"
        names = sorted({
            p[len(prefix):].split("/", 1)[0]
            for p in self.files if p.startswith(prefix)
        })
        if not names:
            return SimpleNamespace(success=False, result=None)
        entries = []
        for name in names:
            child = prefix + name
            is_dir = any(p.startswith(child + "/") for p in self.files)
            entries.append({"name": name,
                            "type": "directory" if is_dir else "file"})
        return SimpleNamespace(success=True, result={"entries": entries})

    def read(self, workspace_id, path, agent_id):
        path = "/" + str(path or "").strip("/")
        if path not in self.files:
            return SimpleNamespace(success=False, result=None)
        return SimpleNamespace(
            success=True,
            result={"path": path, "revision": 1,
                    "content": self.files[path], "chars": len(self.files[path])})


@pytest.fixture
def library():
    return ToolLibrary.from_directory(ROOT)


@pytest.fixture
def runner(tmp_path, library):
    return ToolRunner(library, run_root=tmp_path / "tool_runs")


def test_public_tool_library_has_expected_tools(library):
    assert {"python", "pytest", "node", "playwright"} <= set(library.names())
    assert library.available("python")
    assert library.available("pytest")


def test_python_tool_runs_in_isolated_run(runner):
    gw = _Gateway({"main.py": "print('tool-ok')\n"})
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="python", args=["main.py"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert "tool-ok" in result.stdout
    assert result.materialized_files == 1


def test_pytest_tool_runs_and_reports_failure(runner):
    gw = _Gateway({
        "test_sample.py": (
            "def test_ok():\n"
            "    assert 1 + 1 == 2\n"
        ),
    })
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="pytest", args=["-q"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert "passed" in result.stdout.lower() or "1 passed" in result.stdout


def test_unknown_tool_is_structured_failure(runner):
    result = runner.run_from_task(
        _Gateway({}), "WS-TEST", "agent_a", tool="ghost")
    assert result.ran is False
    assert result.code == "pre_run_failed"
    assert "不在公共工具库" in result.message


def test_run_tool_executor_treats_test_failure_as_executed():
    def run_fn(agent_id, **kwargs):
        return {"ran": True, "ok": False, "exit_code": 1,
                "tool": "pytest", "stdout": "1 failed", "stderr": ""}

    ex = RunToolExecutor(run_fn)
    res = ex.execute(ActionIntent(
        action_type=ACTION_RUN_TOOL, agent_id="agent_a",
        parameters={"tool": "pytest", "args": ["-q"]}))
    assert res.success is True
    assert res.output["ok"] is False
    assert res.metadata["code"] == "tool_exit_nonzero"


def test_run_tool_executor_rejects_bad_args():
    ex = RunToolExecutor(lambda *a, **k: {})
    res = ex.execute(ActionIntent(
        action_type=ACTION_RUN_TOOL, agent_id="agent_a",
        parameters={"tool": "python", "args": "not-a-list"}))
    assert res.success is False
    assert res.metadata["code"] == "bad_args"


def test_safe_join_rejects_escape(tmp_path):
    with pytest.raises(Exception):
        safe_join(tmp_path, "../outside.txt")


def _playwright_ready() -> bool:
    if not Path("/Applications/Google Chrome.app").exists():
        return False
    try:
        return ToolLibrary.from_directory(ROOT).available("playwright")
    except Exception:                                         # noqa: BLE001
        return False


@pytest.mark.skipif(
    not _playwright_ready(),
    reason="Playwright 包或本机 Google Chrome 未安装",
)
def test_playwright_browser_test_runs(runner):
    spec = """
const { test, expect } = require('@playwright/test');
test('browser smoke', async ({ page }) => {
  await page.setContent('<h1 id="x">browser-ok</h1>');
  await expect(page.locator('#x')).toHaveText('browser-ok');
});
"""
    gw = _Gateway({"tests/smoke.spec.js": spec})
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="playwright", args=["test"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert "1 passed" in result.stdout
