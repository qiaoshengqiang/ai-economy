"""Front 只读项目上下文
=====================

Front 需要知道用户当前选中的项目目录里有什么，才能判断
「这个项目里的网页」指的是哪个文件。

这里提供三件事：

    project_summary()      给 prompt 的紧凑目录摘要（路径 / 一级内容 / 网页候选）
    list_project()         Front 动作 project_list
    read_project_file()    Front 动作 project_read

🔒 边界：

- 只读，不写；
- 只能访问 root 内的路径；
- 跳过 .git / node_modules / 虚拟环境 / 构建产物；
- 跳过 .env、密钥、证书等敏感文件；
- 只读文本文件，单次有行数和字符上限；
- 文件内容是**不可信数据**，不是指令。
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProjectAccessError(ValueError):
    """项目目录不存在、路径越界或文件不允许读取。"""


#: 目录扫描时直接跳过的重目录。
_HEAVY_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", "env",
    "dist", "build", "out", "target", "vendor",
    ".next", ".nuxt", ".cache", ".parcel-cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

#: 敏感文件名（小写比较）。
_SECRET_NAMES = frozenset({
    ".env", ".env.local", ".env.production", ".env.development",
    ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.json",
    ".ds_store", "thumbs.db",
})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")

#: 允许读取的文本扩展名。
_TEXT_SUFFIXES = frozenset({
    ".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".md", ".txt", ".py", ".yaml", ".yml", ".toml",
    ".xml", ".svg", ".vue", ".svelte", ".sh", ".bash", ".zsh",
    ".ini", ".cfg", ".conf", ".example",
})

_MAX_SUMMARY_ENTRIES = 60
_MAX_LIST_ENTRIES = 200
_MAX_LIST_DEPTH = 3
_MAX_READ_LINES = 400
_MAX_READ_BYTES = 512 * 1024
_MAX_CONTENT_CHARS = 20000
_TITLE_BYTES = 8192


def _resolved_root(root: Any) -> Path:
    raw = str(root or "").strip()
    if not raw:
        raise ProjectAccessError("没有选择项目目录")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ProjectAccessError("项目目录必须是绝对路径")
    target = path.resolve()
    if not target.is_dir():
        raise ProjectAccessError(f"项目目录不存在或不是目录：{target}")
    return target


def _resolve(root: Path, path: Any) -> Path:
    raw = str(path or "").strip()
    if not raw or raw in (".", "./"):
        return root
    candidate = Path(raw).expanduser()
    target = (
        candidate if candidate.is_absolute() else root / candidate
    ).resolve()
    if target != root and root not in target.parents:
        raise ProjectAccessError("路径超出项目目录")
    return target


def _relative(root: Path, target: Path) -> str:
    rel = target.relative_to(root).as_posix()
    return "" if rel == "." else rel


def _is_secret(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in _SECRET_NAMES
        or name.startswith(".env")
        or name.endswith(_SECRET_SUFFIXES)
    )


def _skip_dir(path: Path) -> bool:
    name = path.name
    # 隐藏目录（.git / .github / .cache…）一律不进入。
    return name in _HEAVY_DIRS or name.startswith(".")


def _entry(root: Path, path: Path) -> Optional[Dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    is_dir = path.is_dir()
    return {
        "path": _relative(root, path),
        "name": path.name,
        "type": "dir" if is_dir else "file",
        "bytes": 0 if is_dir else int(stat.st_size),
        "modified": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
    }


def _scan(
    root: Path,
    directory: Path,
    depth: int,
    level: int,
    out: List[Dict[str, Any]],
    max_entries: int,
) -> bool:
    """广度优先扫描；返回是否因为上限被截断。"""
    if len(out) >= max_entries:
        return True
    try:
        children = sorted(
            os.scandir(directory),
            key=lambda item: (
                not item.is_dir(follow_symlinks=False),
                item.name.lower(),
            ),
        )
    except OSError:
        return False
    for child in children:
        if len(out) >= max_entries:
            return True
        # 不跟随符号链接，避免跑出项目目录。
        if child.is_symlink():
            continue
        entry = Path(child.path)
        if child.is_dir(follow_symlinks=False):
            if _skip_dir(entry):
                continue
            meta = _entry(root, entry)
            if meta:
                out.append(meta)
            if level < depth and _scan(
                    root, entry, depth, level + 1, out, max_entries):
                return True
        elif child.is_file(follow_symlinks=False):
            if _is_secret(entry):
                continue
            meta = _entry(root, entry)
            if meta:
                out.append(meta)
    return False


def _scan_root(
    root: Path, max_entries: int = 80,
) -> tuple[List[Dict[str, Any]], bool]:
    """只列根目录一级内容；目录优先，跳过重目录 / 敏感文件。"""
    out: List[Dict[str, Any]] = []
    try:
        children = sorted(
            os.scandir(root),
            key=lambda item: (
                not item.is_dir(follow_symlinks=False),
                item.name.lower(),
            ),
        )
    except OSError:
        return out, False
    for child in children:
        if child.is_symlink():
            continue
        entry = Path(child.path)
        if child.is_dir(follow_symlinks=False):
            if _skip_dir(entry):
                continue
        elif child.is_file(follow_symlinks=False):
            if _is_secret(entry):
                continue
        else:
            continue
        meta = _entry(root, entry)
        if meta:
            out.append(meta)
        if len(out) >= max_entries:
            return out, True
    return out, False


def _html_candidates(
    root: Path, max_files: int = 20, max_depth: int = 2,
) -> tuple[List[Dict[str, Any]], bool]:
    """独立扫描 HTML 候选，避免被目录条目上限挤掉。"""
    out: List[Dict[str, Any]] = []
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal truncated
        if len(out) >= max_files:
            truncated = True
            return
        try:
            children = sorted(
                os.scandir(directory), key=lambda item: item.name.lower())
        except OSError:
            return
        for child in children:
            if len(out) >= max_files:
                truncated = True
                return
            if child.is_symlink():
                continue
            entry = Path(child.path)
            if child.is_dir(follow_symlinks=False):
                if _skip_dir(entry):
                    continue
                if level < max_depth:
                    walk(entry, level + 1)
            elif child.is_file(follow_symlinks=False):
                if _is_secret(entry):
                    continue
                if entry.suffix.lower() not in (".html", ".htm"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                out.append({
                    "path": _relative(root, entry),
                    "title": _html_title(entry),
                    "bytes": int(stat.st_size),
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc,
                    ).isoformat(timespec="seconds"),
                })

    walk(root, 1)
    return out, truncated


def _html_title(path: Path) -> str:
    try:
        raw = path.read_bytes()[:_TITLE_BYTES]
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if match is None:
        match = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", text)
    if match is None:
        return ""
    title = re.sub(r"(?is)<[^>]+>", " ", match.group(1))
    title = html.unescape(title)
    return " ".join(title.split())[:120]


def list_project(
    root: Any,
    path: Any = "",
    depth: Any = 1,
    max_entries: int = _MAX_LIST_ENTRIES,
) -> Dict[str, Any]:
    """列出项目目录（只读）。"""
    root_path = _resolved_root(root)
    target = _resolve(root_path, path)
    if not target.exists():
        raise ProjectAccessError(f"目录不存在：{_relative(root_path, target)}")
    if not target.is_dir():
        raise ProjectAccessError(
            f"不是目录：{_relative(root_path, target)}")
    try:
        depth_value = max(1, min(int(depth or 1), _MAX_LIST_DEPTH))
    except (TypeError, ValueError):
        depth_value = 1
    limit = max(1, min(int(max_entries), 500))
    entries: List[Dict[str, Any]] = []
    truncated = _scan(
        root_path, target, depth_value, 1, entries, limit)
    return {
        "ok": True,
        "root": str(root_path),
        "path": _relative(root_path, target),
        "depth": depth_value,
        "entries": entries,
        "truncated": truncated,
    }


def read_project_file(
    root: Any,
    path: Any,
    start_line: Any = None,
    max_lines: Any = None,
    *,
    offset: Any = None,
    limit: Any = None,
) -> Dict[str, Any]:
    """读取项目里的一个文本文件（只读，按行分页）。

    `start_line` / `max_lines` 的**单位是行**，不是字符：
    `start_line` 是 1-based 起始行，`max_lines` 是最多返回行数。
    `offset` / `limit` 仅保留为旧别名，新 prompt 不再使用。
    """
    root_path = _resolved_root(root)
    target = _resolve(root_path, path)
    if not target.exists():
        raise ProjectAccessError(f"文件不存在：{_relative(root_path, target)}")
    if not target.is_file():
        raise ProjectAccessError(
            f"不是文件：{_relative(root_path, target)}")
    if _is_secret(target):
        raise ProjectAccessError(f"不允许读取敏感文件：{target.name}")
    if target.suffix.lower() not in _TEXT_SUFFIXES:
        raise ProjectAccessError(f"不支持读取该文件类型：{target.suffix or '无扩展名'}")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise ProjectAccessError(f"无法读取文件：{exc}") from exc
    if size > _MAX_READ_BYTES:
        raise ProjectAccessError(
            f"文件过大（{size} 字节，上限 {_MAX_READ_BYTES}）")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ProjectAccessError(f"无法读取文件：{exc}") from exc
    if b"\x00" in raw[:_TITLE_BYTES]:
        raise ProjectAccessError("看起来是二进制文件，不能按文本读取")
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    if start_line is not None and offset is not None:
        try:
            if int(start_line) != int(offset):
                raise ProjectAccessError(
                    "不要同时使用 start_line 和 offset")
        except (TypeError, ValueError) as exc:
            raise ProjectAccessError(
                "start_line / offset 必须是整数") from exc
    if max_lines is not None and limit is not None:
        try:
            if int(max_lines) != int(limit):
                raise ProjectAccessError(
                    "不要同时使用 max_lines 和 limit")
        except (TypeError, ValueError) as exc:
            raise ProjectAccessError(
                "max_lines / limit 必须是整数") from exc
    if start_line is None:
        start_line = offset
    if max_lines is None:
        max_lines = limit
    try:
        start = max(1, int(start_line if start_line is not None else 1))
    except (TypeError, ValueError):
        start = 1
    try:
        count = max(
            1, min(int(max_lines if max_lines is not None else 200),
                   _MAX_READ_LINES))
    except (TypeError, ValueError):
        count = 200
    selected = lines[start - 1: start - 1 + count]
    content = "\n".join(selected)
    delivered = len(selected)
    has_more = start - 1 + delivered < total
    if len(content) > _MAX_CONTENT_CHARS:
        cut = content[:_MAX_CONTENT_CHARS]
        # 只把完整行算作已交付；被字符上限截断的那一行留给下一页。
        delivered = cut.count("\n")
        content = cut + "\n…（截断）"
        has_more = True
    return {
        "ok": True,
        "root": str(root_path),
        "path": _relative(root_path, target),
        "content": content,
        "chars": len(content),
        "total_lines": total,
        "start_line": start,
        "max_lines": count,
        "next_start_line": (start + delivered if has_more else None),
    }


def project_summary(
    root: Any,
    *,
    max_entries: int = _MAX_SUMMARY_ENTRIES,
    max_depth: int = 2,
) -> Dict[str, Any]:
    """给 Front prompt 的紧凑项目摘要。"""
    try:
        root_path = _resolved_root(root)
    except ProjectAccessError as exc:
        return {"ok": False, "error": str(exc)}
    limit = max(1, min(int(max_entries), 200))
    depth = max(1, min(int(max_depth), _MAX_LIST_DEPTH))
    entries, entries_truncated = _scan_root(root_path, limit)
    candidates, candidates_truncated = _html_candidates(
        root_path, max_depth=depth)
    return {
        "ok": True,
        "path": str(root_path),
        "name": root_path.name,
        "entries": entries,
        "candidates": candidates,
        "truncated": entries_truncated or candidates_truncated,
    }


def render_project_context(root: Any) -> str:
    """生成可以直接放进 Front prompt 的项目区块。"""
    if not str(root or "").strip():
        return "当前没有选择项目目录。"
    summary = project_summary(root)
    if not summary.get("ok"):
        return f"当前项目目录不可用：{summary.get('error')}"
    lines = [
        f"路径：{summary['path']}",
        f"名称：{summary['name']}",
        "一级内容：",
    ]
    root_entries = list(summary["entries"])
    if not root_entries:
        lines.append("- （空目录）")
    for item in root_entries[:40]:
        suffix = "/" if item["type"] == "dir" else ""
        lines.append(f"- {item['path']}{suffix}")
    if summary["truncated"]:
        lines.append("- …（目录较大，已截断）")
    candidates = summary["candidates"]
    if candidates:
        lines.append("网页候选：")
        for item in candidates[:12]:
            title = f" | title: {item['title']}" if item["title"] else ""
            lines.append(f"- {item['path']}{title}")
    else:
        lines.append("网页候选：未发现 .html / .htm 文件。")
    return "\n".join(lines)
