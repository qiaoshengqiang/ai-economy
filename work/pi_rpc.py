"""
Pi RPC 最小客户端
=================

Pi 的 RPC 模式是 JSONL：

    stdin  → command
    stdout → response / session event
    stderr → 诊断

这里只做 AIEOS Work 需要的部分：

    start Pi → prompt → 收集事件直到 agent_settled → shutdown

🔒 不是 Pi SDK；AIEOS 是 Python，走进程边界更稳。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PiRunResult:
    events: List[Dict[str, Any]] = field(default_factory=list)
    stderr: str = ""
    exit_code: Optional[int] = None
    aborted: bool = False
    error: str = ""

    @property
    def settled(self) -> bool:
        return any(
            event.get("type") == "agent_settled"
            for event in self.events)


class PiRpcClient:
    def __init__(
        self,
        *,
        cli_path: str,
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        provider: str = "",
        model: str = "",
        system_prompt: str = "",
        session_dir: Optional[Path] = None,
        session_id: str = "",
        tools: str = "read,write,edit,bash,grep,find,ls",
        extra_args: Optional[List[str]] = None,
    ) -> None:
        self.cli_path = str(cli_path or "pi")
        self.cwd = Path(cwd)
        self.env = dict(os.environ if env is None else env)
        self.provider = str(provider or "")
        self.model = str(model or "")
        self.system_prompt = str(system_prompt or "")
        self.session_dir = Path(session_dir) if session_dir else None
        self.session_id = str(session_id or "")
        self.tools = str(tools or "")
        self.extra_args = list(extra_args or [])

    # ------------------------------------------------------------
    def _command(self) -> List[str]:
        cmd = [self.cli_path, "--mode", "rpc"]
        if self.provider:
            cmd += ["--provider", self.provider]
        if self.model:
            cmd += ["--model", self.model]
        if self.tools:
            cmd += ["--tools", self.tools]
        if self.session_dir is not None:
            cmd += ["--session-dir", str(self.session_dir)]
        if self.session_id:
            cmd += ["--session-id", self.session_id]
        if self.system_prompt:
            cmd += ["--append-system-prompt", self.system_prompt]
        cmd += self.extra_args
        return cmd

    # ------------------------------------------------------------
    def run(
        self,
        prompt: str,
        *,
        timeout_seconds: float = 1800.0,
        should_stop: Optional[Callable[[], bool]] = None,
        event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> PiRunResult:
        result = PiRunResult()
        cmd = self._command()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.cwd),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except Exception as exc:                              # noqa: BLE001
            result.error = f"无法启动 Pi：{type(exc).__name__}: {exc}"
            return result

        event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        stderr_lines: List[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError):
                        event = {"type": "parse_error", "line": line[:2000]}
                    if not isinstance(event, dict):
                        event = {"type": "parse_error", "line": line[:2000]}
                    event["_received_at"] = time.time()
                    event_queue.put(event)
            finally:
                event_queue.put({"type": "_eof"})

        def _read_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                text = line.rstrip("\n")
                stderr_lines.append(text)
                if len(stderr_lines) > 2000:
                    del stderr_lines[:500]

        out_thread = threading.Thread(target=_read_stdout, daemon=True)
        err_thread = threading.Thread(target=_read_stderr, daemon=True)
        out_thread.start()
        err_thread.start()

        started = time.monotonic()
        backend_aborted = False
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({
                "id": "aieos-prompt-1",
                "type": "prompt",
                "message": str(prompt),
            }, ensure_ascii=False) + "\n")
            proc.stdin.flush()

            while True:
                if should_stop is not None and should_stop():
                    backend_aborted = True
                    result.aborted = True
                    try:
                        proc.stdin.write(json.dumps({
                            "id": "aieos-abort-1",
                            "type": "abort",
                        }) + "\n")
                        proc.stdin.flush()
                    except Exception:                     # noqa: BLE001
                        pass
                    break
                if (float(timeout_seconds or 0) > 0
                        and time.monotonic() - started > float(timeout_seconds)):
                    result.error = (
                        f"Pi 超时（{timeout_seconds}s）")
                    break
                try:
                    event = event_queue.get(timeout=0.5)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if event.get("type") == "_eof":
                    break
                if event.get("type") == "response":
                    if (event.get("command") == "prompt"
                            and event.get("success") is False):
                        result.error = str(
                            event.get("error") or "Pi 拒绝 prompt")
                        break
                result.events.append(event)
                if event_sink is not None:
                    try:
                        event_sink(event)
                    except Exception:                     # noqa: BLE001
                        pass
                if event.get("type") == "agent_settled":
                    break
        except Exception as exc:                              # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:                                 # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            out_thread.join(timeout=2)
            err_thread.join(timeout=2)
            result.exit_code = proc.returncode

        result.stderr = "\n".join(stderr_lines[-200:])
        if backend_aborted:
            result.error = "Pi 任务已被停止"
        if not result.events and not result.error:
            result.error = "Pi 没有产生任何事件"
        return result


__all__ = ["PiRpcClient", "PiRunResult"]
