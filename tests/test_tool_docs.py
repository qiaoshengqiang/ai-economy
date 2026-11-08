"""
公共工具文档守卫
==================

真实运行暴露的问题：

    Agent 不知道 `run_tool` 的写**不会回写** Workspace；
    不知道 Playwright 必须先写 spec；
    验收人无法复跑时反复读文件，不发 VERIFY(passed=false)。

所以每个工具都必须有独立说明，并且关键事实不能只藏在代码里。

本文件钉住：

    ① `manifests/*.json` 里的每个工具都有 `tools/<name>.md`；
    ② 每份文档都包含 `run_tool` 与“写不回来”的事实；
    ③ `tools/README.md` 链接到每一份文档；
    ④ Playwright 文档包含 spec / smoke gate；
    ⑤ 反向测试：删掉一份文档，守卫必须变红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "resources" / "public" / "tools"
MANIFESTS = TOOLS / "manifests"
README = TOOLS / "README.md"


def _manifest_names(root: Path = MANIFESTS) -> list:
    names = []
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        name = str(data.get("name") or path.stem)
        names.append(name)
    return names


def _missing_docs(tools_dir: Path, names: list) -> list:
    return [n for n in names if not (tools_dir / f"{n}.md").is_file()]


def test_every_manifest_has_a_doc():
    names = _manifest_names()
    assert names, "没有扫到任何 manifest —— 守卫可能没在工作"
    missing = _missing_docs(TOOLS, names)
    assert not missing, (
        f"这些工具没有使用说明：{missing}\n"
        f"Agent 只能靠猜 —— 这正是 Playwright 用法错误的来源。")


def test_each_doc_mentions_run_tool_and_no_write_back():
    for name in _manifest_names():
        path = TOOLS / f"{name}.md"
        text = path.read_text(encoding="utf-8")
        assert "run_tool" in text, f"{path.name} 没写 run_tool 的调用形状"
        assert ("不会回写" in text or "不会自动回写" in text), (
            f"{path.name} 没有说明「run_tool 的写不会回写 Workspace」—— "
            f"这是真实火车任务失败的直接原因。")


def test_readme_links_every_doc():
    text = README.read_text(encoding="utf-8")
    missing = [n for n in _manifest_names() if f"{n}.md" not in text]
    assert not missing, f"tools/README.md 没链接这些说明：{missing}"


def test_playwright_doc_has_spec_and_smoke_facts():
    text = (TOOLS / "playwright.md").read_text(encoding="utf-8")
    assert "*.spec.js" in text or ".spec.js" in text
    assert "smoke" in text.lower()
    assert "tests/foo.spec.js" in text
    assert "ModuleNotFoundError" in text or "Cannot find module" in text


def test_the_guard_is_not_vacuous(tmp_path):
    """反向：少一份文档 ⇒ `_missing_docs` 必须报出来。"""
    names = _manifest_names()
    victim = names[0]
    assert _missing_docs(tmp_path, names) == names
    (tmp_path / f"{victim}.md").write_text("stub", encoding="utf-8")
    assert _missing_docs(tmp_path, names) == names[1:]


def test_docs_are_reachable_via_public_query():
    """文档在公共资料区，`public_query` 真的能读到。"""
    from services.public_reference import PublicReference

    pub = PublicReference()
    names = set(pub.list_names())
    for name in _manifest_names():
        target = f"tools/{name}.md"
        assert target in names, f"公共资料区里没有 {target}"
        got = pub.query(target)
        assert got["ok"] is True
        assert got["chars"] > 200, f"{target} 太短，可能是空文件"
