"""平台边界守卫。

规则：宿主机相关 API 只允许出现在 `core/platform_support/`。
业务代码通过 `PlatformAdapter` 调用能力，不直接判断操作系统。

为什么用 AST 而不是 grep：
    `hostname` 包含 `os.name` 子串；正则很容易误报 / 漏报。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = (
    "agents",
    "communication",
    "change",
    "commit",
    "core",
    "llm",
    "services",
    "storage",
    "tool_runtime",
    "ui",
    "work",
)
SCAN_FILES = ("main.py",)
ALLOWED_DIRS = (Path("core") / "platform_support",)

FORBIDDEN_IMPORTS = {
    "fcntl", "msvcrt", "winreg", "pwd", "grp", "resource",
}
FORBIDDEN_ATTRS = {
    ("os", "killpg"),
    ("os", "getpgid"),
    ("os", "symlink"),
    ("os", "chmod"),
    ("os", "setpgrp"),
    ("os", "getuid"),
    ("os", "geteuid"),
    ("os", "umask"),
    ("os", "name"),
    ("sys", "platform"),
    ("platform", "system"),
    ("subprocess", "CREATE_NEW_PROCESS_GROUP"),
}
FORBIDDEN_CALL_KEYWORDS = {"start_new_session"}


def _iter_python_files(root: Path) -> Iterable[Path]:
    for name in SCAN_DIRS:
        base = root / name
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))
    for name in SCAN_FILES:
        path = root / name
        if path.is_file():
            yield path


def _is_allowed(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(
        relative == allowed or allowed in relative.parents
        for allowed in ALLOWED_DIRS
    )


def scan_platform_violations(root: Path) -> List[str]:
    """返回 `path:line: detail` 形式的违规列表。空列表 = 通过。"""
    root = Path(root).resolve()
    files = list(_iter_python_files(root))
    if not files:
        # 反向测试会传一个临时目录；此时直接扫目录下的 .py。
        files = sorted(root.rglob("*.py"))
    violations: List[str] = []
    for path in files:
        if "__pycache__" in path.parts or _is_allowed(path, root):
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                        violations.append(
                            f"{relative}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".")[0]
                if module in FORBIDDEN_IMPORTS:
                    violations.append(
                        f"{relative}:{node.lineno}: from {node.module} import")
            elif isinstance(node, ast.Attribute):
                if (
                    isinstance(node.value, ast.Name)
                    and (node.value.id, node.attr) in FORBIDDEN_ATTRS
                ):
                    violations.append(
                        f"{relative}:{node.lineno}: "
                        f"{node.value.id}.{node.attr}")
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in FORBIDDEN_CALL_KEYWORDS:
                        violations.append(
                            f"{relative}:{node.lineno}: "
                            f"keyword {keyword.arg}=")
    return violations


def test_no_platform_specific_calls_outside_adapter():
    violations = scan_platform_violations(ROOT)
    assert not violations, (
        "平台相关 API 必须收敛到 core/platform_support/：\n"
        + "\n".join(violations)
    )


def test_guard_detects_injected_violations(tmp_path):
    """反向测试：把违规注入临时模块，确认守卫真的会变红。"""
    (tmp_path / "bad_module.py").write_text(
        "import fcntl\n"
        "import os\n"
        "import platform\n"
        "import sys\n"
        "import subprocess\n"
        "\n"
        "def bad(proc):\n"
        "    os.killpg(os.getpgid(proc.pid), 9)\n"
        "    os.symlink('a', 'b')\n"
        "    os.chmod('a', 0o755)\n"
        "    print(sys.platform)\n"
        "    print(os.name)\n"
        "    print(platform.system())\n"
        "    subprocess.Popen(['x'], start_new_session=True)\n",
        encoding="utf-8",
    )

    violations = scan_platform_violations(tmp_path)
    assert any("import fcntl" in item for item in violations)
    assert any("os.killpg" in item for item in violations)
    assert any("os.getpgid" in item for item in violations)
    assert any("os.symlink" in item for item in violations)
    assert any("os.chmod" in item for item in violations)
    assert any("sys.platform" in item for item in violations)
    assert any("os.name" in item for item in violations)
    assert any("platform.system" in item for item in violations)
    assert any("start_new_session" in item for item in violations)
