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


def test_too_long_arg_is_a_clear_structured_failure(runner):
    result = runner.run_from_task(
        _Gateway({}), "WS-TEST", "agent_a",
        tool="python", args=["-c", "x" * 2001])
    assert result.ran is False
    assert result.code == "pre_run_failed"
    assert "2000" in result.message


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


# ============================================================
# Playwright：项目配置发现 + 冒烟门禁
# ============================================================
def test_playwright_project_config_is_used(runner):
    """子目录的 qa/playwright.config.js 不能被根 bootstrap 遮蔽。"""
    spec = (
        "const { test, expect } = require('@playwright/test');\n"
        "test('config used', async ({ page }) => {\n"
        "  await page.setContent('<h1>config-ok</h1>');\n"
        "  await expect(page.locator('h1')).toHaveText('config-ok');\n"
        "});\n"
    )
    config = (
        "const { defineConfig } = require('@playwright/test');\n"
        "module.exports = defineConfig({\n"
        "  testDir: '.',\n"
        "  testMatch: '*.spec.js',\n"
        "  use: { channel: 'chrome', headless: true },\n"
        "});\n"
    )
    gw = _Gateway({
        "qa/playwright.config.js": config,
        "qa/smoke.spec.js": spec,
    })
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="playwright", args=["test"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert result.config_used == "qa/playwright.config.js"


def test_playwright_smoke_gate_blocks_broken_entry(runner):
    """页面只有半个 <head> 时，冒烟门禁必须快速失败，不跑全量。"""
    broken = (
        '<!DOCTYPE html><html lang="zh-CN"><head>'
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover'
    )
    spec = (
        "const { test, expect } = require('@playwright/test');\n"
        "test('should not run', async ({ page }) => {\n"
        "  await page.setContent('<h1>should-not-run</h1>');\n"
        "  await expect(page.locator('h1')).toHaveText('should-not-run');\n"
        "});\n"
    )
    gw = _Gateway({
        "site/index.html": broken,
        "qa/smoke.spec.js": spec,
    })
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="playwright", args=["test"])
    assert result.ran and not result.ok
    assert result.code == "smoke_failed"
    assert result.smoke["enabled"] is True
    assert result.smoke["ok"] is False
    # 全量 spec 没有被执行
    assert "should-not-run" not in result.stdout


def test_playwright_smoke_gate_passes_good_entry(runner):
    good = (
        "<!DOCTYPE html><html><body>"
        '<div id="app"><canvas></canvas></div>'
        '<div id="ui-root"><button>ok</button></div>'
        "</body></html>"
    )
    spec = (
        "const { test, expect } = require('@playwright/test');\n"
        "test('runs after smoke', async ({ page }) => {\n"
        "  await page.setContent('<h1>after-smoke</h1>');\n"
        "  await expect(page.locator('h1')).toHaveText('after-smoke');\n"
        "});\n"
    )
    gw = _Gateway({
        "site/index.html": good,
        "qa/smoke.spec.js": spec,
    })
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="playwright", args=["test"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert result.smoke["ok"] is True
    assert "after-smoke" not in result.stdout  # spec 本身不打印内容


def test_playwright_smoke_can_be_disabled(runner):
    broken = "<!DOCTYPE html><html><head></head></html>"
    spec = (
        "const { test, expect } = require('@playwright/test');\n"
        "test('runs without smoke', async ({ page }) => {\n"
        "  await page.setContent('<h1>no-smoke-ok</h1>');\n"
        "  await expect(page.locator('h1')).toHaveText('no-smoke-ok');\n"
        "});\n"
    )
    gw = _Gateway({
        "site/index.html": broken,
        "qa/smoke.spec.js": spec,
    })
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="playwright",
        args=["test", "--no-smoke"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert result.smoke["enabled"] is False
    assert result.smoke["reason"] == "disabled_by_args"


def test_python_tool_surfaces_new_images(runner):
    """run_tool 新产生的图片要以元数据形式回给 Agent（不含 base64）。"""
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAusB9Wl2nXQAAAAASUVORK5CYII="
    )
    code = (
        "import base64\n"
        f"open('shot.png','wb').write(base64.b64decode('{png_b64}'))\n"
        "print('image-written')\n"
    )
    gw = _Gateway({"make_image.py": code})
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="python", args=["make_image.py"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert len(result.images) == 1
    assert result.images[0]["path"] == "shot.png"
    assert result.images[0]["mime"] == "image/png"
    assert "base64" not in result.to_agent_dict()["images"][0]


def test_smoke_gate_allows_slow_cdn_pages():
    """CDN 模块可能让 DOMContentLoaded 超过 8 秒；门禁必须留足余量。"""
    from tool_runtime.runner import SMOKE_CONFIG_SOURCE, SMOKE_SPEC_SOURCE

    assert "timeout: 20000" in SMOKE_SPEC_SOURCE
    assert "timeout: 35000" in SMOKE_CONFIG_SOURCE
    assert "timeout: 8000" not in SMOKE_SPEC_SOURCE


@pytest.mark.skipif(
    not _playwright_ready(),
    reason="Playwright 包或本机 Google Chrome 未安装",
)
def test_playwright_suite_gets_base_url_for_workspace_entry(runner):
    """
    smoke gate 的服务器在 smoke 后关闭；正式 suite 必须能拿到入口 URL。

    真实 run `demand_cc61c0b5`：E 的 spec 硬编码 8000/8080，全部
    ERR_CONNECTION_REFUSED，验收无法复跑。
    """
    html = (
        "<!DOCTYPE html><html><body>"
        '<h1 id="served">served-ok</h1>'
        "</body></html>"
    )
    spec = (
        "const { test, expect } = require('@playwright/test');\n"
        "test('base url is served', async ({ page }) => {\n"
        "  expect(process.env.BASE_URL).toBeTruthy();\n"
        "  await page.goto(process.env.BASE_URL, "
        "{ waitUntil: 'domcontentloaded' });\n"
        "  await expect(page.locator('#served')).toHaveText('served-ok');\n"
        "});\n"
    )
    gw = _Gateway({
        "index.html": html,
        "tests/base.spec.js": spec,
    })
    result = runner.run_from_task(
        gw, "WS-TEST", "agent_a", tool="playwright",
        args=["tests/base.spec.js"])
    assert result.ran and result.ok, result.to_agent_dict()
    assert result.smoke["ok"] is True
    assert "1 passed" in result.stdout
