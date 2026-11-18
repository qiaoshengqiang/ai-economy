"""Front Markdown 渲染器的功能测试（Node，可用时执行）。

只验证安全子集：HTML 必须被 escape，链接只允许 http(s)/mailto，
表格/代码/列表/标题能生成固定标签。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MARKDOWN_JS = (
    Path(__file__).resolve().parent.parent
    / "ui" / "static" / "markdown.js")


def _render(text: str) -> str:
    script = (
        "global.window = {};"
        f"require({json.dumps(str(MARKDOWN_JS))});"
        "const html = window.AIEOSMarkdown.render("
        f"{json.dumps(text)});"
        "process.stdout.write(html);"
    )
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_markdown_renders_rich_blocks():
    html = _render(
        "## 结果\n\n"
        "- 第一项\n"
        "- 第二项\n\n"
        "| 项 | 值 |\n"
        "|---|---|\n"
        "| 状态 | IN_PROGRESS |\n\n"
        "```text\ncheckpoint.error = max_turns\n```\n\n"
        "[查看](https://example.com/a?b=1&c=2)"
    )

    assert "<h2>结果</h2>" in html
    assert "<ul" in html and "<li>第一项</li>" in html
    assert "<table" in html
    assert "<th" in html and "IN_PROGRESS" in html
    assert "<pre" in html and "max_turns" in html
    assert 'href="https://example.com/a?b=1&amp;c=2"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_markdown_escapes_raw_html_and_rejects_javascript_links():
    html = _render(
        "<script>alert(1)</script>\n\n"
        "[危险](javascript:alert(1))\n\n"
        "<img src=x onerror=alert(1)>"
    )

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "&lt;img" in html
    assert 'href="javascript' not in html
    assert "危险" in html
