"""
Pi Work 的独立验收
==================

生产者（Pi）的自测报告是**证据**，不是验收结论。
这里由 AIEOS 侧做一次独立检查：

    1. manifest 非空；
    2. 如果 Task.acceptance 带 command，执行它（独立命令）；
    3. 否则用 acceptance_agent 的 Brain 逐条对照 criteria + 文件内容；
    4. 解析失败 / 无 criteria / 无 verifier → fail-closed。

🔒 不使用 Task Shared Context；criteria 来自 Task Core 的 acceptance。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from work.task_directory import TaskDirectory


@dataclass
class AcceptanceResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    verifier: str = ""
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "gaps": list(self.gaps),
            "verifier": self.verifier,
            "raw": self.raw[:4000],
        }


class PiAcceptance:
    def __init__(
        self,
        runtime: Any,
        *,
        task: Any,
        task_directory: TaskDirectory,
        manifest: List[Dict[str, Any]],
        acceptance_agent: str = "",
        timeout_seconds: float = 120.0,
    ) -> None:
        self.rt = runtime
        self.task = task
        self.task_directory = task_directory
        self.manifest = list(manifest or [])
        self.acceptance_agent = str(acceptance_agent or "")
        self.timeout_seconds = float(timeout_seconds)

    # ------------------------------------------------------------
    def verify(self) -> AcceptanceResult:
        if not self.manifest:
            return AcceptanceResult(
                passed=False, gaps=["Task Workspace 没有交付文件"],
                verifier="manifest")
        criteria = self._criteria()
        if not criteria:
            return AcceptanceResult(
                passed=False, gaps=["Task 没有 acceptance criteria"],
                verifier="criteria")

        command = self._acceptance_command()
        if command:
            return self._run_command(command)

        if not self.acceptance_agent:
            return AcceptanceResult(
                passed=False,
                gaps=["没有配置 acceptance_agent，不能做独立验收"],
                verifier="none")
        return self._verify_with_agent(criteria)

    # ------------------------------------------------------------
    def _acceptance(self) -> Dict[str, Any]:
        raw = getattr(self.task, "acceptance", None)
        return dict(raw) if isinstance(raw, dict) else {}

    def _criteria(self) -> List[str]:
        raw = self._acceptance().get("criteria") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(item).strip() for item in raw if str(item).strip()]

    def _acceptance_command(self) -> List[str]:
        raw = self._acceptance().get("command")
        if isinstance(raw, str):
            return [part for part in raw.split() if part]
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        return []

    def _run_command(self, command: List[str]) -> AcceptanceResult:
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.task_directory.workspace),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )
        except Exception as exc:                              # noqa: BLE001
            return AcceptanceResult(
                passed=False,
                gaps=[f"验收命令失败：{type(exc).__name__}: {exc}"],
                verifier="command")
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        passed = proc.returncode == 0
        return AcceptanceResult(
            passed=passed,
            reasons=[f"验收命令 exit={proc.returncode}"],
            gaps=[] if passed else [output[:2000] or "验收命令非零退出"],
            verifier="command",
            raw=output,
        )

    # ------------------------------------------------------------
    def _verify_with_agent(self, criteria: List[str]) -> AcceptanceResult:
        agent = self._agent(self.acceptance_agent)
        if agent is None or not getattr(agent, "has_brain", False):
            return AcceptanceResult(
                passed=False,
                gaps=[f"验收 Agent {self.acceptance_agent} 不可用"],
                verifier=self.acceptance_agent)
        prompt = self._prompt(criteria)
        system = (
            "You are an independent acceptance verifier. You do not trust "
            "the producer's self-report. Check the stated criteria against "
            "the actual files. Return JSON only: "
            '{"passed": true|false, "reasons": ["..."], "gaps": ["..."]}.')
        try:
            response = agent.generate(
                messages=prompt,
                system=system,
                cognitive=False,
                task_id=self.task.task_id,
                call_purpose="utility",
                max_tokens=4096,
            )
            content = str(getattr(response, "content", "") or "")
        except Exception as exc:                              # noqa: BLE001
            return AcceptanceResult(
                passed=False,
                gaps=[f"验收 LLM 失败：{type(exc).__name__}: {exc}"],
                verifier=self.acceptance_agent)
        payload = _extract_json(content)
        if not isinstance(payload, dict) or "passed" not in payload:
            return AcceptanceResult(
                passed=False,
                gaps=["验收 LLM 没有返回合法 JSON"],
                verifier=self.acceptance_agent,
                raw=content)
        reasons = payload.get("reasons") or []
        gaps = payload.get("gaps") or []
        return AcceptanceResult(
            passed=bool(payload.get("passed")),
            reasons=[str(item) for item in reasons][:20],
            gaps=[str(item) for item in gaps][:20],
            verifier=self.acceptance_agent,
            raw=content,
        )

    def _agent(self, agent_id: str) -> Any:
        try:
            return self.rt.economy.agent(agent_id)
        except Exception:                                     # noqa: BLE001
            return None

    def _prompt(self, criteria: List[str]) -> str:
        parts = [
            f"Task: {self.task.task_id}",
            "Acceptance criteria:",
        ]
        parts.extend(f"- {item}" for item in criteria)
        parts.append("\nDeliverable files:")
        used = 0
        for item in self.manifest:
            rel = str(item.get("path") or "")
            target = self.task_directory.workspace / rel
            text = ""
            if target.is_file():
                try:
                    text = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    text = ""
            parts.append(f"\n--- {rel} ---")
            if text:
                clipped = text[:8000]
                parts.append(clipped)
                used += len(clipped)
            else:
                parts.append("(binary or unreadable)")
            if used > 50000:
                parts.append("\n… more files omitted …")
                break
        parts.append(
            "\nJudge only against the criteria. A file existing is not "
            "enough if its content does not satisfy the criterion. "
            "Return the JSON object.")
        return "\n".join(parts)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(value[start:end + 1])
            return data if isinstance(data, dict) else None
        except (TypeError, ValueError):
            return None
    return None


__all__ = ["PiAcceptance", "AcceptanceResult"]
