"""
Tool Runner —— 受控的代码执行与测试
=========================================

【它做什么】

    在**独立的 run 目录**里执行公共工具库中声明过的工具：

        python / pytest / node / playwright

【它不是什么】

    ❌ 不是通用 shell
    ❌ 不是 Agent 任意下载/安装器
    ❌ 不是 OS 级安全沙箱（V1 的信任模型见设计文档）

【硬约束】

    ① 只执行 manifest 里的 command；Agent 只能追加参数，不能换可执行文件
    ② `shell=False`；命令是列表
    ③ cwd 固定在本次 run 的 work 目录
    ④ 每次 run 独立 HOME / TMPDIR；清空 LLM/API secret
    ⑤ 超时 + 输出上限
    ⑥ 工具库只读；安装/更新不由 Agent 触发
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from tool_runtime.library import ToolLibrary, ToolLibraryError, ToolSpec
from tool_runtime.workspace import (
    RunWorkspace, materialize_private_workspace, materialize_task_workspace,
)

#: 单次输出最多回给 Agent 多少字符（前面 8k + 后面 4k）。
MAX_OUTPUT_CHARS = 12_000
_OUT_HEAD = 8_000
_OUT_TAIL = 4_000


class ToolRunError(Exception):
    """执行前置条件失败（不是进程本身失败）。"""


@dataclass(frozen=True)
class ToolRunResult:
    """一次工具执行的完整结果。物理路径不进入 Agent 输出。"""

    ran: bool
    ok: bool
    tool: str
    run_id: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: float = 0.0
    code: str = ""
    message: str = ""
    materialized_files: int = 0
    output_truncated: bool = False

    def to_agent_dict(self) -> Dict[str, Any]:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "tool": self.tool,
            "run_id": self.run_id,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "code": self.code,
            "message": self.message,
            "materialized_files": self.materialized_files,
            "output_truncated": self.output_truncated,
        }


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    head = text[:_OUT_HEAD]
    tail = text[-_OUT_TAIL:]
    return (
        f"{head}\n\n...（输出被截断：共 {len(text)} 字符）...\n\n{tail}",
        True,
    )


class ToolRunner:
    """按公共工具清单执行命令。"""

    def __init__(self, library: ToolLibrary, *, run_root: Path,
                 max_timeout_seconds: int = 300):
        self.library = library
        self.run_root = Path(run_root).resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.max_timeout_seconds = int(max_timeout_seconds)

    # ================================================================
    # public
    # ================================================================
    def catalog(self) -> List[Dict[str, Any]]:
        return self.library.public_catalog()

    def run_from_task(self, gateway: Any, workspace_id: str, agent_id: str,
                      *, tool: str, args: Optional[List[str]] = None,
                      source_path: str = "",
                      timeout_seconds: Optional[int] = None) -> ToolRunResult:
        run = RunWorkspace.create(self.run_root)
        try:
            stats = materialize_task_workspace(
                gateway, workspace_id, agent_id, source_path,
                target=run.work)
        except Exception as exc:                              # noqa: BLE001
            return self._pre_run_failure(
                tool, run.run_id, f"materialize_failed: {exc}")
        return self._execute(tool, args, run, stats["files"],
                             timeout_seconds)

    def run_from_private(self, workspace: Any, *,
                         tool: str, args: Optional[List[str]] = None,
                         source_path: str = "",
                         timeout_seconds: Optional[int] = None) -> ToolRunResult:
        run = RunWorkspace.create(self.run_root)
        try:
            stats = materialize_private_workspace(
                workspace, source_path, target=run.work)
        except Exception as exc:                              # noqa: BLE001
            return self._pre_run_failure(
                tool, run.run_id, f"materialize_failed: {exc}")
        return self._execute(tool, args, run, stats["files"],
                             timeout_seconds)

    # ================================================================
    # internal
    # ================================================================
    def _pre_run_failure(self, tool: str, run_id: str,
                         message: str) -> ToolRunResult:
        return ToolRunResult(
            ran=False, ok=False, tool=str(tool), run_id=run_id,
            code="pre_run_failed", message=message)

    def _execute(self, tool_name: str, args: Optional[List[str]],
                 run: RunWorkspace, materialized: int,
                 timeout_seconds: Optional[int]) -> ToolRunResult:
        try:
            spec = self.library.get(tool_name)
        except ToolLibraryError as exc:
            return self._pre_run_failure(tool_name, run.run_id, str(exc))

        clean_args = self._clean_args(args)
        if clean_args is None:
            return self._pre_run_failure(
                tool_name, run.run_id, "args 必须是字符串列表（最多 100 项）")

        timeout = self._timeout(timeout_seconds)
        if timeout is None:
            return self._pre_run_failure(
                tool_name, run.run_id,
                f"timeout_seconds 必须是 1..{self.max_timeout_seconds} 的整数")

        try:
            self._bootstrap(spec, run.work)
        except Exception as exc:                              # noqa: BLE001
            return self._pre_run_failure(
                tool_name, run.run_id, f"bootstrap_failed: {exc}")

        # Playwright 的 manifest 已经包含 `test` 子命令；
        # 兼容 Agent 仍然写 args=["test", ...] 的形状。
        if spec.name == "playwright" and clean_args:
            if clean_args[0] == "test":
                clean_args = clean_args[1:]

        command = [*spec.command, *clean_args]
        env = self._env(run, spec)
        started = time.perf_counter()
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(run.work),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return self._pre_run_failure(
                tool_name, run.run_id, f"tool_not_found: {exc}")
        except Exception as exc:                              # noqa: BLE001
            return self._pre_run_failure(
                tool_name, run.run_id, f"spawn_failed: {exc}")

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        duration_ms = round((time.perf_counter() - started) * 1000, 1)

        stdout = stdout or ""
        stderr = stderr or ""
        stdout, cut_out = _clip(stdout)
        stderr, cut_err = _clip(stderr)
        exit_code = proc.returncode
        ok = (not timed_out) and exit_code == 0
        code = "" if ok else ("timeout" if timed_out else "tool_exit_nonzero")
        return ToolRunResult(
            ran=True, ok=ok, tool=spec.name, run_id=run.run_id,
            exit_code=exit_code, stdout=stdout, stderr=stderr,
            timed_out=timed_out, duration_ms=duration_ms,
            code=code, materialized_files=int(materialized),
            output_truncated=cut_out or cut_err)

    @staticmethod
    def _clean_args(args: Optional[List[str]]) -> Optional[List[str]]:
        if args is None:
            return []
        if not isinstance(args, (list, tuple)) or len(args) > 100:
            return None
        out: List[str] = []
        for value in args:
            if not isinstance(value, str):
                return None
            if "\x00" in value or len(value) > 2000:
                return None
            out.append(value)
        return out

    def _timeout(self, value: Optional[int]) -> Optional[int]:
        if value is None:
            return 60
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 1 or value > self.max_timeout_seconds:
            return None
        return value

    def _bootstrap(self, spec: ToolSpec, work: Path) -> None:
        """把公共工具声明的配置模板写进 run 目录（不覆盖 Agent 文件）。"""
        for relative, content in spec.bootstrap_files.items():
            target = (work / relative).resolve()
            if target != work and work not in target.parents:
                raise ToolRunError(f"bootstrap 路径跑出 run 目录：{relative}")
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _env(self, run: RunWorkspace, spec: ToolSpec) -> Dict[str, str]:
        """
        最小环境变量集。

        🔒 不继承 `.env` / LLM API Key / 宿主机凭据。
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "HOME": str(run.home),
            "TMPDIR": str(run.tmp),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(run.work),
            "RUN_DIR": str(run.work),
            "CI": "1",
        }
        env.update({str(k): str(v) for k, v in spec.env.items()})
        return env

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        """超时后杀掉整个进程组，避免子进程残留。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:                                     # noqa: BLE001
            try:
                proc.kill()
            except Exception:                                 # noqa: BLE001
                pass
