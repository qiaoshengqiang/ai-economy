"""Workspace Recon 的产物 —— Worker 自己判断“新建 / 修改 / 不确定”。

🔒 这不是 Front 的开关，也不是 Task 的静态字段：
    它是 Worker 在**只读侦察**之后给出的计划，由 Runtime 校验后决定
    target / lock / snapshot 的作用域。

    mode:
        create   目标目录不存在或为空，准备新建
        modify   目标目录已存在，准备修改
        mixed    目标目录已存在，既有修改也有新增
        unclear  证据不足，不能安全开始写
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional

WORKSPACE_MODES = frozenset({"create", "modify", "mixed", "unclear"})
_MODE_ALIASES = {
    "new": "create",
    "fresh": "create",
    "existing": "modify",
    "edit": "modify",
    "update": "modify",
    "unknown": "unclear",
    "ambiguous": "unclear",
}


class WorkspacePlanError(ValueError):
    """Recon 结果不合法，或目标路径不能安全使用。"""


@dataclass(frozen=True)
class WorkspacePlan:
    mode: str
    target_path: str
    reason: str = ""
    confidence: float = 0.0
    raw_output: str = ""

    @property
    def is_clear(self) -> bool:
        return self.mode in ("create", "modify", "mixed")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "target_path": self.target_path,
            "reason": self.reason,
            "confidence": self.confidence,
        }


def _extract_object(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start:end + 1])
        except (TypeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _safe_target(workspace_root: Path, raw: Any) -> str:
    value = str(raw or ".").strip().replace("\\", "/")
    if not value:
        value = "."
    path = PurePosixPath(value)
    if path.is_absolute():
        raise WorkspacePlanError("target_path 不能是绝对路径")
    if any(part == ".." for part in path.parts):
        raise WorkspacePlanError("target_path 不能包含 ..")
    root = Path(workspace_root).expanduser().resolve()
    candidate = (root / str(path)).resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkspacePlanError("target_path 逃出了 workspace_root")
    if candidate.exists() and not candidate.is_dir():
        raise WorkspacePlanError("target_path 已存在但不是目录")
    relative = candidate.relative_to(root).as_posix()
    return relative or "."


def parse_workspace_plan(
    text: str,
    *,
    workspace_root: Path,
) -> WorkspacePlan:
    """解析 Worker 的 recon 输出并校验 target_path。

    🔒 这里只做**结构与路径**校验；是否覆盖已有内容由 Runtime 在
       上锁之后再次检查。
    """
    payload = _extract_object(text)
    if payload is None:
        raise WorkspacePlanError("workspace_plan 不是合法 JSON 对象")
    mode = str(payload.get("mode") or "").strip().lower()
    mode = _MODE_ALIASES.get(mode, mode)
    if mode not in WORKSPACE_MODES:
        raise WorkspacePlanError(
            f"未知 workspace mode：{mode!r}；允许：{sorted(WORKSPACE_MODES)}")
    target = _safe_target(workspace_root, payload.get("target_path"))
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if mode == "unclear":
        return WorkspacePlan(
            mode=mode, target_path=target,
            reason=str(payload.get("reason") or ""),
            confidence=confidence,
            raw_output=str(text or ""),
        )
    if target != ".":
        candidate = (Path(workspace_root) / target).resolve()
        if mode == "create" and candidate.exists():
            try:
                non_empty = any(candidate.iterdir())
            except OSError:
                non_empty = True
            if non_empty:
                raise WorkspacePlanError(
                    f"mode=create 但目标目录已存在且非空：{target}")
        if mode in ("modify", "mixed") and not candidate.exists():
            raise WorkspacePlanError(
                f"mode={mode} 但目标目录不存在：{target}")
    return WorkspacePlan(
        mode=mode,
        target_path=target,
        reason=str(payload.get("reason") or ""),
        confidence=confidence,
        raw_output=str(text or ""),
    )


__all__ = [
    "WORKSPACE_MODES", "WorkspacePlan", "WorkspacePlanError",
    "parse_workspace_plan",
]
