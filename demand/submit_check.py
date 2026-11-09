"""
SUBMIT 前的 DOM/import 静态 Smoke 检查
=======================================

真实 run `demand_6b2fd87b`：

    index.html R1 有 id="app"；
    R2 改成 id="scene"；
    src/main.js 仍然 `$('app')` → `app.appendChild(...)`；
    页面首帧 TypeError，没人发现。

本模块只做**静态、可复现**的检查：

    ① index.html 引用的本地脚本是否存在；
    ② JS 的本地 import 是否解析得到真实文件；
    ③ JS 通过 getElementById / $() / querySelector('#id') 引用的
       DOM id 是否在 index.html 里存在；
    ④ 给出“未被引用 JS / .js 目录”等警告（不阻塞）。

🔒 它不是验收结论，也不是 Task 状态；只是 SUBMIT 前的结构门禁。
   Task Core 仍是唯一生命周期权威；检查失败时 SUBMIT 消息不会发出。
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

#: 这些目录不参与“未被引用 JS”的警告（测试/审查/笔记不是交付入口）。
_SKIP_DIRS = {
    "tests", "test", "review", "acceptance", "notes",
    "node_modules", ".git", "__pycache__",
}

_HTML_ID_RE = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']', re.I)
_HTML_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)

_REF_PATTERNS = (
    # const app = document.getElementById('app')
    re.compile(
        r'(?:(?:const|let|var)\s+(\w+)\s*=\s*)?[^;\n]*?'
        r'getElementById\(\s*["\']([^"\']+)["\']'),
    # const app = $('app')
    re.compile(
        r'(?:(?:const|let|var)\s+(\w+)\s*=\s*)?[^;\n]*?'
        r'\$\(\s*["\']([^"\']+)["\']\s*\)'),
    # document.querySelector('#app ...')
    re.compile(
        r'(?:(?:const|let|var)\s+(\w+)\s*=\s*)?[^;\n]*?'
        r'querySelector(?:All)?\(\s*["\']#([A-Za-z0-9_-]+)'),
)

_IMPORT_RE = re.compile(
    r'(?:from\s*|import\s*\()\s*["\']([^"\']+)["\']')
_SIDE_EFFECT_IMPORT_RE = re.compile(
    r'^\s*import\s+["\']([^"\']+)["\']')


def _norm_rel(path: Any) -> str:
    text = str(path or "").strip().replace("\\", "/").lstrip("/")
    parts: List[str] = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return ""
            parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


def _read(gateway: Any, workspace_id: str, agent_id: str,
          rel: str) -> Optional[str]:
    try:
        got = gateway.read(
            workspace_id, "/" + rel.lstrip("/"), agent_id)
    except Exception:                                         # noqa: BLE001
        return None
    if not got.success:
        return None
    content = (got.result or {}).get("content")
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


def _walk(gateway: Any, workspace_id: str, agent_id: str,
          rel: str = "", depth: int = 0,
          out: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
    files = out if out is not None else []
    if depth > 4 or len(files) >= 200:
        return files
    path = "/" + rel.lstrip("/") if rel else "/"
    try:
        got = gateway.list(workspace_id, path, agent_id)
    except Exception:                                         # noqa: BLE001
        return files
    if not got.success:
        return files
    for entry in (got.result or {}).get("entries", []) or []:
        name = str(entry.get("name") or "")
        if not name:
            continue
        child = f"{rel}/{name}" if rel else name
        kind = str(entry.get("type") or "")
        files.append({"path": child, "type": kind})
        if kind == "directory" and name not in _SKIP_DIRS:
            _walk(gateway, workspace_id, agent_id, child, depth + 1, files)
        if len(files) >= 200:
            break
    return files


def _is_external_spec(spec: str) -> bool:
    spec = str(spec or "").strip()
    if not spec:
        return True
    if spec.startswith(("http://", "https://", "//", "data:", "blob:")):
        return True
    return not spec.startswith((".", "/"))


def _resolve_import(current_rel: str, spec: str,
                    known: Set[str]) -> Optional[str]:
    spec = str(spec or "").strip()
    if spec.startswith("/"):
        base = _norm_rel(spec)
    else:
        base = _norm_rel(
            posixpath.dirname(current_rel) + "/" + spec)
    if not base:
        return None
    candidates = [base]
    if not posixpath.splitext(base)[1]:
        candidates.extend([base + ".js", base + ".mjs",
                           base + "/index.js"])
    for candidate in candidates:
        if candidate in known:
            return candidate
    return None


def _line_no(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _dom_refs(js: str) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen: Set[Tuple[int, str, str]] = set()
    for line_no, line in enumerate(js.splitlines(), 1):
        for pattern in _REF_PATTERNS:
            for match in pattern.finditer(line):
                var_name = str(match.group(1) or "")
                dom_id = str(match.group(2) or "")
                key = (line_no, var_name, dom_id)
                if not dom_id or key in seen:
                    continue
                seen.add(key)
                refs.append({
                    "id": dom_id,
                    "var": var_name,
                    "line": line_no,
                })
    return refs


def _imports(js: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    seen: Set[Tuple[int, str]] = set()
    for line_no, line in enumerate(js.splitlines(), 1):
        for match in _IMPORT_RE.finditer(line):
            spec = str(match.group(1) or "")
            key = (line_no, spec)
            if spec and key not in seen:
                seen.add(key)
                found.append({"spec": spec, "line": line_no})
        match = _SIDE_EFFECT_IMPORT_RE.match(line)
        if match:
            spec = str(match.group(1) or "")
            key = (line_no, spec)
            if spec and key not in seen:
                seen.add(key)
                found.append({"spec": spec, "line": line_no})
    return found


def _usage_is_guarded(js: str, var_name: str) -> bool:
    """
    `const loading = $('loading'); if (loading) ...` → 缺失只是 warning。
    `const app = $('app'); app.appendChild(...)` → 缺失是 error。
    """
    if not var_name:
        return False
    pattern = re.compile(rf"\b{re.escape(var_name)}\s*\.")
    guard_pattern = re.compile(
        rf"if\s*\(\s*{re.escape(var_name)}\b")
    and_pattern = re.compile(rf"\b{re.escape(var_name)}\s*&&")
    usages = [line for line in js.splitlines() if pattern.search(line)]
    if not usages:
        return True
    return all(guard_pattern.search(line) or and_pattern.search(line)
               for line in usages)


def check_submit_deliverable(
    gateway: Any,
    workspace_id: str,
    agent_id: str,
) -> Dict[str, Any]:
    """
    返回：
        {"ok": bool, "errors": [...], "warnings": [...],
         "skipped": bool, "checked": {...}}
    """
    index = _read(gateway, workspace_id, agent_id, "index.html")
    if index is None:
        return {
            "ok": True,
            "skipped": True,
            "reason": "没有 index.html；跳过 Web DOM/import 检查",
            "errors": [], "warnings": [],
        }

    errors: List[str] = []
    warnings: List[str] = []
    ids = set(_HTML_ID_RE.findall(index))
    scripts = _HTML_SCRIPT_RE.findall(index)

    files = _walk(gateway, workspace_id, agent_id)
    known_files: Set[str] = {
        item["path"] for item in files if item["type"] == "file"}

    js_texts: Dict[str, str] = {}
    queue: List[str] = []

    for src in scripts:
        src = str(src or "").strip()
        if not src or src.startswith(("http://", "https://", "//",
                                      "data:", "blob:")):
            continue
        rel = _norm_rel(src)
        if rel not in known_files:
            errors.append(
                f"index.html 引用的本地脚本不存在：{src!r}")
            continue
        text = _read(gateway, workspace_id, agent_id, rel)
        if text is None:
            errors.append(f"无法读取本地脚本：{src!r}")
            continue
        if rel not in js_texts:
            js_texts[rel] = text
            queue.append(rel)

    if not scripts:
        warnings.append("index.html 没有本地 <script src> 入口")
    if not queue and scripts:
        warnings.append("index.html 的脚本没有可检查的本地入口")

    while queue:
        rel = queue.pop(0)
        text = js_texts[rel]
        for item in _imports(text):
            spec = item["spec"]
            if _is_external_spec(spec):
                continue
            target = _resolve_import(rel, spec, known_files)
            if target is None:
                errors.append(
                    f"{rel}:{item['line']} 的本地 import 不存在："
                    f"{spec!r}")
                continue
            if target not in js_texts:
                target_text = _read(
                    gateway, workspace_id, agent_id, target)
                if target_text is None:
                    errors.append(
                        f"{rel}:{item['line']} 的 import 无法读取："
                        f"{spec!r}")
                    continue
                js_texts[target] = target_text
                queue.append(target)

    for rel, text in sorted(js_texts.items()):
        for ref in _dom_refs(text):
            if ref["id"] in ids:
                continue
            guarded = _usage_is_guarded(text, ref["var"])
            message = (
                f"{rel}:{ref['line']} 引用了 index.html 中不存在的 "
                f"DOM id {ref['id']!r}")
            if guarded:
                warnings.append(
                    message + "（仅用于可空检查，不阻塞）")
            else:
                errors.append(message)

    for item in files:
        path = item["path"]
        if path in js_texts:
            continue
        if item["type"] == "directory" and path.endswith(".js"):
            warnings.append(
                f"{path} 是目录，不是文件（.js 后缀通常表示文件）")
        elif (item["type"] == "file"
              and path.endswith((".js", ".mjs"))
              and not path.startswith(("tests/", "review/",
                                       "vendor/"))):
            warnings.append(
                f"{path} 没有被 index.html / import 图引用"
                "（可能是死代码）")

    return {
        "ok": not errors,
        "skipped": False,
        "errors": errors,
        "warnings": warnings,
        "checked": {
            "html_ids": len(ids),
            "local_scripts": len(js_texts),
            "errors": len(errors),
            "warnings": len(warnings),
        },
    }


def format_submit_check_failure(report: Dict[str, Any]) -> str:
    """把检查结果变成 Producer 能直接行动的 SUBMIT 拒绝理由。"""
    lines = ["SUBMIT 被拒绝：DOM/import 静态 smoke 检查失败。"]
    for error in list(report.get("errors") or [])[:8]:
        lines.append(f"- {error}")
    warnings = list(report.get("warnings") or [])
    if warnings:
        lines.append("警告（不阻塞）：")
        for warning in warnings[:5]:
            lines.append(f"- {warning}")
    lines.append(
        "请修复后重新 workspace_write / workspace_edit，再 SUBMIT；"
        "SUBMIT 消息没有发出，Task 仍在 IN_PROGRESS。")
    return "\n".join(lines)
