"""Life 工具层 —— 只读任务视图 + 私有空间/记忆动作的纯函数。

🔒 这里只定义 **Life 能做什么**，不定义权限策略：
    · 任务是否属于这个 Agent：由调用方（组合根）用 Task.is_participant 判断；
    · 私有空间是否越界：由 `Workspace._resolve()` 判断；
    · 记忆写入：由 Private*MemoryService 判断。

🔒 任务内容只读：
    · `task_content_*` 只读任务交付目录，不写；
    · 不读 `.aieos` 元数据（元数据由 `task_inspect` 单独返回）；
    · 拒绝绝对路径、`..`、点文件/点目录。
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

#: 一次 task_list 最多返回多少条。
LIFE_MAX_TASK_LIST = 10
#: 一次 task_content_list 最多返回多少个文件。
LIFE_MAX_TASK_FILES = 200
#: 单次读取最多返回多少行。
LIFE_MAX_READ_LINES = 500
#: 单次读取最多返回多少字符（防止把整个文件塞进 prompt）。
LIFE_MAX_READ_CHARS = 20_000
#: 任务内容单文件读取上限（字节）。
LIFE_MAX_TASK_FILE_BYTES = 1_000_000
#: 私有文件单文件读取上限（字节）。
LIFE_MAX_PRIVATE_FILE_BYTES = 1_000_000

#: 任务内容列表跳过这些目录。
_SKIP_DIRS = frozenset({
    ".git", ".aieos", "node_modules", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
#: 任务内容列表跳过这些文件。
_SKIP_FILES = frozenset({
    ".DS_Store", "Thumbs.db",
})


def clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…（截断）"


def _bad_path(path: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "path": path, "code": "bad_path", "message": message}


def _resolve_under(root: Path, raw: Any) -> Path:
    """把相对路径解析到 root 内；拒绝绝对路径、`..`、点文件/点目录。"""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("path 不能为空")
    path = Path(text)
    if path.is_absolute():
        raise ValueError("path 不能是绝对路径")
    if ".." in path.parts:
        raise ValueError("path 不能包含 ..")
    if any(part.startswith(".") for part in path.parts):
        raise ValueError("任务内容不允许读取点文件/点目录")
    root = Path(root).expanduser().resolve()
    candidate = (root / text).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path 逃出了任务目录")
    return candidate


def _relative(root: Path, candidate: Path) -> str:
    return candidate.relative_to(Path(root).resolve()).as_posix()


def list_task_files(
    root: Path,
    manifest_files: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    limit: int = LIFE_MAX_TASK_FILES,
) -> Dict[str, Any]:
    """列出任务交付目录里的文件。

    优先使用 manifest（任务实际交付的文件）；没有 manifest 时做有界扫描。
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return {
            "ok": False, "code": "no_workspace",
            "message": f"任务交付目录不存在：{root}",
        }
    max_files = max(1, min(int(limit or LIFE_MAX_TASK_FILES),
                           LIFE_MAX_TASK_FILES))
    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _add(candidate: Path) -> bool:
        if len(entries) >= max_files:
            return False
        try:
            rel = _relative(root, candidate)
        except ValueError:
            return True
        if rel in seen:
            return True
        seen.add(rel)
        try:
            size = candidate.stat().st_size
        except OSError:
            size = 0
        entries.append({"path": rel, "bytes": size})
        return True

    if manifest_files:
        for item in manifest_files:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("path") or "").strip()
            if not raw:
                continue
            try:
                candidate = _resolve_under(root, raw)
            except ValueError:
                continue
            if not candidate.is_file():
                continue
            if not _add(candidate):
                break
        truncated = len(entries) >= max_files
        return {
            "ok": True, "root": str(root), "paths": entries,
            "count": len(entries), "truncated": truncated,
            "source": "manifest",
        }

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name in _SKIP_FILES or name.startswith("."):
                continue
            if not _add(Path(dirpath) / name):
                break
        if len(entries) >= max_files:
            break
    truncated = len(entries) >= max_files
    return {
        "ok": True, "root": str(root), "paths": entries,
        "count": len(entries), "truncated": truncated,
        "source": "scan",
    }


def read_text_slice(
    root: Path,
    raw_path: Any,
    *,
    start_line: int = 1,
    max_lines: int = 200,
) -> Dict[str, Any]:
    """读取任务/私有目录里的文本片段，带路径与大小限制。"""
    text = str(raw_path or "").strip()
    try:
        candidate = _resolve_under(root, text)
    except ValueError as exc:
        return _bad_path(text, str(exc))
    if not candidate.is_file():
        out = _bad_path(text, "没有这个文件")
        out["code"] = "not_found"
        return out
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return {
            "ok": False, "path": text, "code": "stat_failed",
            "message": f"{type(exc).__name__}: {exc}",
        }
    if size > LIFE_MAX_TASK_FILE_BYTES:
        return {
            "ok": False, "path": text, "code": "file_too_large",
            "message": f"文件 {size} bytes，超过上限 "
                       f"{LIFE_MAX_TASK_FILE_BYTES} bytes",
        }
    start = max(1, int(start_line or 1))
    count = max(1, min(int(max_lines or 200), LIFE_MAX_READ_LINES))
    try:
        with candidate.open("r", encoding="utf-8") as handle:
            lines = list(itertools.islice(
                handle, start - 1, start - 1 + count + 1))
    except UnicodeDecodeError:
        return {
            "ok": False, "path": text, "code": "binary_file",
            "message": "不是 UTF-8 文本文件",
        }
    except OSError as exc:
        return {
            "ok": False, "path": text, "code": "read_failed",
            "message": f"{type(exc).__name__}: {exc}",
        }
    truncated = len(lines) > count
    content = "".join(lines[:count])
    if len(content) > LIFE_MAX_READ_CHARS:
        content = content[:LIFE_MAX_READ_CHARS] + "\n…（字符数已截断）"
        truncated = True
    end_line = start + len(content.splitlines()) - 1 if content else start - 1
    return {
        "ok": True, "path": text, "start_line": start,
        "end_line": max(start, end_line), "content": content,
        "truncated": truncated,
    }


@dataclass
class LifeTools:
    """组合根注入给 Life Turn 的受控能力。

    所有回调都必须是**只读或受控写**；没有回调 = 这个动作不可用。
    """

    # ---- 记忆注入 ----
    short_term_render: Optional[Callable[[str], str]] = None
    long_term_render: Optional[Callable[[str], str]] = None

    # ---- 任务只读 ----
    recent_tasks: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None
    task_inspect: Optional[
        Callable[[str, str], Dict[str, Any]]] = None
    task_files: Optional[Callable[[str, str], Dict[str, Any]]] = None
    task_read: Optional[
        Callable[[str, str, str, int, int], Dict[str, Any]]] = None

    # ---- 外部信息（只读）----
    web_search: Optional[
        Callable[[str, str, int, bool, str], Dict[str, Any]]] = None
    web_fetch: Optional[
        Callable[[str, str], Dict[str, Any]]] = None

    # ---- 私有空间 ----
    private_list: Optional[Callable[[str], Dict[str, Any]]] = None
    private_read: Optional[
        Callable[[str, str, int, int], Dict[str, Any]]] = None
    private_write: Optional[
        Callable[[str, str, str], Dict[str, Any]]] = None
    private_delete: Optional[
        Callable[[str, str], Dict[str, Any]]] = None

    # ---- 私有记忆 ----
    short_term_read: Optional[Callable[[str], Dict[str, Any]]] = None
    short_term_add: Optional[
        Callable[[str, str, str, str, int], Dict[str, Any]]] = None
    short_term_update: Optional[
        Callable[..., Dict[str, Any]]] = None
    short_term_reinforce: Optional[
        Callable[[str, int, str, str], Dict[str, Any]]] = None
    long_term_search: Optional[
        Callable[[str, str], Dict[str, Any]]] = None
    long_term_read: Optional[
        Callable[[str, str], Dict[str, Any]]] = None
    long_term_consolidate: Optional[
        Callable[..., Dict[str, Any]]] = None


__all__ = [
    "LIFE_MAX_TASK_LIST", "LIFE_MAX_TASK_FILES", "LIFE_MAX_READ_LINES",
    "LIFE_MAX_READ_CHARS", "LifeTools", "clip_text", "list_task_files",
    "read_text_slice",
]
