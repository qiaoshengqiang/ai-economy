"""运行 checkpoint —— 跨 run 保留「上次做到哪」。

🔒 它不是 Task Core 状态，也不是 Agent Memory：
    它是**一次 Work run 的事实快照**，只用于 resume 时给 Agent 上下文。
    Task 状态仍然只由 Task Core 决定。

一个 checkpoint 回答：

    · 上一次 run 的 id / iteration / 状态 / 错误
    · 上一次的完整轮次总结
    · 最近失败的动作（动作 / 目标 / 错误）
    · Workspace 里有哪些文件

存储由调用方决定（UI 用 `runs/ui/checkpoints/`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RunCheckpoint:
    task_id: str
    run_id: str = ""
    iteration: int = 1
    status: str = ""
    error: str = ""
    summary: str = ""
    action_failures: List[Dict[str, Any]] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "iteration": self.iteration,
            "status": self.status,
            "error": self.error,
            "summary": self.summary,
            "action_failures": list(self.action_failures),
            "files": list(self.files),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunCheckpoint":
        return cls(
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            iteration=int(data.get("iteration") or 1),
            status=str(data.get("status") or ""),
            error=str(data.get("error") or ""),
            summary=str(data.get("summary") or ""),
            action_failures=list(data.get("action_failures") or []),
            files=list(data.get("files") or []),
            created_at=str(data.get("created_at") or ""),
        )

    def render(self, *, summary_limit: int = 6000) -> str:
        """渲染成只读事实，注入 cognition / 第一轮 Work prompt。"""
        summary = self.summary.strip()
        if len(summary) > summary_limit:
            summary = summary[:summary_limit] + "\n…（上次总结已截断）"
        lines = [
            f"run_id: {self.run_id or '(unknown)'}",
            f"iteration: {self.iteration}",
            f"status: {self.status or '(unknown)'}",
        ]
        if self.error:
            lines.append(f"last error: {self.error}")
        if self.action_failures:
            lines.append("recent action failures (most recent first):")
            for item in self.action_failures[:10]:
                action = item.get("action") or "?"
                target = item.get("target") or ""
                error = item.get("error") or ""
                line = "- "
                if item.get("round") is not None:
                    line += f"round {item['round']}: "
                line += action
                if target:
                    line += f" {target}"
                if error:
                    line += f": {error}"
                lines.append(line)
        if self.files:
            lines.append("workspace files:")
            for item in self.files[:30]:
                path = item.get("path") or "?"
                chars = item.get("chars")
                lines.append(
                    f"- {path}" + (f" ({chars} chars)" if chars is not None else ""))
        if summary:
            lines.extend(["last run summary:", summary])
        return "\n".join(lines)


def _failure_detail(
    intent: Any, result: Any,
) -> Optional[Dict[str, Any]]:
    """把一次动作结果变成 checkpoint 里的失败事实。

    🔒 `run_tool` 的 `ActionResult.success` 可能为 True，
       但工具本身 `output.ok=False` / 非零退出。两者都算失败，
       否则 checkpoint 会漏掉最关键的验证失败。
    """
    if not isinstance(result, dict):
        return None
    output = result.get("output")
    failed = result.get("success") is False
    if not failed and isinstance(output, dict):
        if output.get("ok") is False:
            failed = True
        elif output.get("ran") and output.get("exit_code") not in (None, 0):
            failed = True
    if not failed:
        return None

    params = ((intent or {}).get("parameters") or {})
    action = str(
        (intent or {}).get("action_type")
        or result.get("action_type") or "")
    target = str(params.get("path") or params.get("query") or "")
    if not target and params.get("tool"):
        target = str(params.get("tool"))
        args = params.get("args")
        if isinstance(args, list):
            target += " " + " ".join(str(item) for item in args)
        elif args:
            target += " " + str(args)

    detail = ""
    if isinstance(output, dict):
        detail = str(
            output.get("message")
            or output.get("stderr")
            or output.get("stdout")
            or "")
    detail = detail or str(result.get("error") or "")
    if not detail:
        metadata = result.get("metadata") or {}
        detail = str(metadata.get("code") or "")
    # 工具 stdout 往往以进度行开头；从第一个 Error / failed 处截取，
    # 让 checkpoint 直接给出可执行的失败原因。
    for marker in ("Error:", "error:", "failed", "Failed"):
        index = detail.find(marker)
        if index > 0:
            detail = detail[index:]
            break
    return {
        "action": action,
        "target": target,
        "error": detail.strip()[:500],
    }


def checkpoint_from_rounds(
    task_id: str,
    rounds: List[Dict[str, Any]],
    *,
    run_id: str = "",
    iteration: int = 1,
    status: str = "",
    error: str = "",
    files: List[Dict[str, Any]] = (),
) -> RunCheckpoint:
    """从轮次日志构造 checkpoint；只读，不推断不存在的轮次。

    🔒 失败扫描**跨轮**，不只看最后一轮：
       实际日志里最后一轮常常只是「继续读文件」，真正的失败
       （Playwright 断言、工具非零退出）发生在前一两轮。
       只看最后一轮会把 resume 最需要的那条事实丢掉。
    """
    last = rounds[-1] if rounds else {}
    summary = ""
    for item in reversed(rounds):
        summary = str(item.get("summary") or "").strip()
        if summary:
            break
    failures: List[Dict[str, Any]] = []
    seen: set = set()
    for round_data in reversed(rounds):
        round_no = round_data.get("round")
        for segment in (round_data.get("segments") or []):
            for intent, result in zip(
                    segment.get("actions") or [],
                    segment.get("results") or []):
                detail = _failure_detail(intent, result)
                if detail is None:
                    continue
                key = (
                    detail["action"], detail["target"], detail["error"])
                if key in seen:
                    continue
                seen.add(key)
                detail["round"] = round_no
                failures.append(detail)
                if len(failures) >= 10:
                    break
            if len(failures) >= 10:
                break
        if len(failures) >= 10:
            break
    return RunCheckpoint(
        task_id=task_id,
        run_id=run_id or str(last.get("run_id") or ""),
        iteration=iteration,
        status=status,
        error=error,
        summary=summary,
        action_failures=failures[:10],
        files=list(files or ()),
        created_at=str(last.get("created_at") or ""),
    )


def dumps(checkpoint: RunCheckpoint) -> str:
    return json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2)
