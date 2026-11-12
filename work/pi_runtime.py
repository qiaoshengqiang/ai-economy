"""
PiWorkRuntime —— Pi 作为 Work 层执行底座
=========================================

    Task Core（AIEOS）
        ↓
    真实 Task 目录 + memory 文件协议
        ↓
    Pi RPC（原生 read/write/edit/bash）
        ↓
    agent_settled
        ↓
    manifest / RESULT.md / memory ingest
        ↓
    Task Core COMPLETED

🔒 这条路径**不使用** Task Shared Context / Alignment / AIEOS action dispatcher。
    Task Core 仍然是状态权威；Pi 只负责执行和落盘。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef
from llm.base import LLMResponse, TOKEN_SOURCE_API
from work.llm_proxy import AIEOSLLMProxy
from work.lifecycle import WorkLifecycle
from work.pi_memory import PiMemoryBridge
from work.pi_rpc import PiRpcClient, PiRunResult
from work.roles import WorkResult
from work.task_directory import TaskDirectory


class PiWorkRuntime:
    def __init__(
        self,
        runtime: Any,
        task: Task,
        *,
        task_directory: TaskDirectory,
        pi_cli_path: str = "pi",
        pi_provider: str = "",
        pi_model: str = "",
        timeout_seconds: float = 1800.0,
        should_stop: Any = None,
        round_sink: Any = None,
        event_sink: Any = None,
        pi_agent_dir: Optional[Path] = None,
        use_llm_proxy: bool = True,
        upstream_base_url: str = "",
        role_prompt: str = "",
    ) -> None:
        if task.mode != TaskMode.SOLO:
            raise ValueError(
                f"PiWorkRuntime 只接受 SOLO Task；{task.task_id} 是 {task.mode}")
        self.rt = runtime
        self.task = task
        self.task_directory = task_directory
        self.pi_cli_path = str(pi_cli_path or "pi")
        self.pi_provider = str(pi_provider or "")
        self.pi_model = str(pi_model or "")
        self.timeout_seconds = float(timeout_seconds)
        self.should_stop = should_stop or (lambda: False)
        self.round_sink = round_sink
        self.event_sink = event_sink
        self.pi_agent_dir = Path(pi_agent_dir) if pi_agent_dir else None
        self.use_llm_proxy = bool(use_llm_proxy)
        self.upstream_base_url = str(upstream_base_url or "")
        self.role_prompt = str(role_prompt or "").strip()
        self.lifecycle = WorkLifecycle(runtime.tasks)
        self.memory = PiMemoryBridge(
            runtime,
            agent_id=task.contractor or task.creator,
            task=task,
            task_workspace=task_directory.workspace,
        )

    # ------------------------------------------------------------
    def _agent(self) -> Any:
        actor = self.task.contractor or self.task.creator
        try:
            return self.rt.economy.agent(actor)
        except Exception:                                     # noqa: BLE001
            return None

    def _provider_and_model(self) -> tuple[str, str]:
        agent = self._agent()
        brain = getattr(agent, "brain", None)
        provider = self.pi_provider or str(
            getattr(brain, "provider", "") or "deepseek")
        model = self.pi_model or str(
            getattr(brain, "model", "") or "deepseek-flash")
        return provider, model

    def _pi_env(
        self, provider: str, *, include_key: bool = True,
    ) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({
            "AIEOS_TASK_WORKSPACE": str(self.task_directory.workspace),
            "AIEOS_PRIVATE_WORKSPACE": str(
                self.memory.private_workspace or ""),
            "AIEOS_MEMORY_DIR": str(self.memory.memory_dir or ""),
            "AIEOS_TASK_ID": self.task.task_id,
            "AIEOS_AGENT_ID": self.task.contractor or self.task.creator,
        })
        if self.pi_agent_dir is not None:
            env["PI_CODING_AGENT_DIR"] = str(self.pi_agent_dir)
        # Pi 的 provider 凭据：AIEOS 仍然持有 Agent 的 credential。
        agent = self._agent()
        brain = getattr(agent, "brain", None)
        credential = None
        try:
            credential = brain.credential_for(provider) if brain else None
        except Exception:                                     # noqa: BLE001
            credential = None
        material = str(getattr(credential, "material", "") or "")
        if include_key and material:
            key = (
                "DEEPSEEK_API_KEY" if provider == "deepseek"
                else "OPENAI_API_KEY")
            env[key] = material
        return env

    def _credential_material(self, provider: str) -> str:
        agent = self._agent()
        brain = getattr(agent, "brain", None)
        try:
            credential = brain.credential_for(provider) if brain else None
        except Exception:                                     # noqa: BLE001
            return ""
        return str(getattr(credential, "material", "") or "")

    def _upstream_base_url(self) -> str:
        if self.upstream_base_url:
            return self.upstream_base_url
        provider = self._provider_and_model()[0]
        return {
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com",
        }.get(provider, "https://api.deepseek.com")

    def _write_pi_models(self, base_url: str, model: str) -> Path:
        directory = self.task_directory.aieos / "pi-agent"
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "$schema": "https://pi.dev/schemas/models.schema.json",
            "providers": {
                "aieos-proxy": {
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "apiKey": "aieos-proxy",
                    "models": [{
                        "id": model,
                        "name": f"AIEOS proxy / {model}",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": 128000,
                        "maxTokens": 16384,
                        "cost": {
                            "input": 0, "output": 0,
                            "cacheRead": 0, "cacheWrite": 0,
                        },
                    }],
                },
            },
        }
        (directory / "models.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return directory

    # ------------------------------------------------------------
    def run(self) -> WorkResult:
        task = self.task
        actor = task.contractor or task.creator
        result = WorkResult(
            task_id=task.task_id,
            status=task.status,
            iteration=task.iteration,
        )
        try:
            if self.should_stop():
                result.error = "任务已被停止"
                return result
            if task.status == TaskStatus.COMPLETED:
                result.summary = task.note or "任务已经处于 COMPLETED"
                return result
            if task.status in (TaskStatus.CREATED, TaskStatus.ACCEPTED):
                self.lifecycle.start(task, actor=actor)
            if task.status not in (
                TaskStatus.IN_PROGRESS,
                TaskStatus.SUBMITTED,
                TaskStatus.VERIFIED,
            ):
                result.error = (
                    f"Task 当前是 {task.status}，不能开始 Pi Work run")
                result.status = task.status
                return result

            self.task_directory.ensure()
            self.task_directory.write_meta(
                "TASK.md",
                f"# Task {task.task_id}\n\n{task.description or ''}\n")
            memory_context = self.memory.prepare()
            system_prompt = self._system_prompt(memory_context)
            provider, model = self._provider_and_model()
            proxy = None
            try:
                if self.use_llm_proxy:
                    material = self._credential_material(provider)
                    if not material:
                        result.error = (
                            f"Agent 没有可用的 {provider} credential，"
                            "无法启动 Pi LLM proxy")
                        result.status = task.status
                        return result
                    proxy = AIEOSLLMProxy(
                        self.rt,
                        agent_id=actor,
                        task_id=task.task_id,
                        api_key=material,
                        provider=provider,
                        model=model,
                        upstream_base_url=self._upstream_base_url(),
                        timeout_seconds=self.timeout_seconds,
                    )
                    try:
                        proxy_base = proxy.start()
                    except Exception as exc:              # noqa: BLE001
                        result.error = (
                            f"无法启动 AIEOS LLM Proxy："
                            f"{type(exc).__name__}: {exc}")
                        result.status = task.status
                        return result
                    pi_agent_dir = self._write_pi_models(proxy_base, model)
                    env = self._pi_env("aieos-proxy", include_key=False)
                    env["PI_CODING_AGENT_DIR"] = str(pi_agent_dir)
                    client_provider = "aieos-proxy"
                    client_model = model
                else:
                    env = self._pi_env(provider)
                    if self.pi_agent_dir is not None:
                        env["PI_CODING_AGENT_DIR"] = str(self.pi_agent_dir)
                    client_provider = provider
                    client_model = model

                client = PiRpcClient(
                    cli_path=self.pi_cli_path,
                    cwd=self.task_directory.workspace,
                    env=env,
                    provider=client_provider,
                    model=client_model,
                    system_prompt=system_prompt,
                    session_dir=self.task_directory.session_dir,
                    session_id=task.task_id,
                )
                run = client.run(
                    task.description or f"Task {task.task_id}",
                    timeout_seconds=self.timeout_seconds,
                    should_stop=self.should_stop,
                    event_sink=self.event_sink,
                )
            finally:
                if proxy is not None:
                    proxy.stop()
            if run.aborted or self.should_stop():
                result.error = run.error or "任务已被停止"
                result.status = task.status
                return result
            if run.error:
                result.error = run.error
                result.status = task.status
                return result

            manifest = self.task_directory.write_manifest()
            if run.stderr:
                self.task_directory.write_meta("pi-stderr.log", run.stderr)
            if not manifest:
                result.error = "Pi 已结束，但 Task Workspace 没有任何文件"
                result.status = task.status
                return result

            summary = self._summary(run.events)
            if not summary:
                result.error = (
                    "Pi 已结束，但没有产出总结："
                    "既没有 RESULT.md，也没有可用的 assistant 文本")
                result.status = task.status
                return result

            memory_result = self.memory.ingest()
            if not self.use_llm_proxy:
                self._record_usage(actor, run.events, provider, model)
            self._emit_rounds(run.events, actor)
            result.turns = self._turn_count(run.events)
            result.tokens = self._token_count(run.events)
            result.actions = self._tool_count(run.events)
            result.summary = summary
            result.artifacts = [item["path"] for item in manifest]
            result.events = [{
                "type": "pi_settled",
                "memory": memory_result,
                "files": len(manifest),
            }]
            self.task_directory.write_json("result.json", {
                "task_id": task.task_id,
                "engine": "pi",
                "summary": summary,
                "files": manifest,
                "memory": memory_result,
            })

            self.lifecycle.complete_solo(
                task, actor=actor,
                result=TaskResultRef(
                    result_ref=f"pi_task_{task.task_id}",
                    result_type="artifact",
                    summary=summary,
                    metadata={
                        "engine": "pi",
                        "manifest": manifest,
                        "memory": memory_result,
                    },
                ),
            )
            result.status = task.status
            return result
        except Exception as exc:                              # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = task.status
            return result

    def reopen_and_run(self, *, reason: str = "") -> WorkResult:
        actor = self.task.creator
        self.lifecycle.reopen(self.task, actor=actor, reason=reason)
        return self.run()

    # ------------------------------------------------------------
    def _system_prompt(self, memory_context: str) -> str:
        paths = self.memory.paths()
        role_section = (
            "\n" + self.role_prompt + "\n" if self.role_prompt else "")
        return f"""You are {self.task.contractor or self.task.creator}, a Work Agent in AIEOS.
{role_section}


Your working directory is the Task Workspace:
{paths['task_workspace']}

Your private workspace is:
{paths['private_workspace']}
Your memory directory is:
{paths['memory_dir']}

Write deliverables directly into the Task Workspace. Use your native
read/write/edit/bash tools. Do not use AIEOS Shared Context; it is not
available for this Work task.

Read `{paths['memory_dir']}/context.md` for the memory snapshot.
You may update `{paths['memory_dir']}/working.md` and write candidate
memories to `{paths['memory_dir']}/inbox/*.md`.

When the task is complete, write a human-facing summary in `RESULT.md`
in the Task Workspace. Use these sections:
- What was delivered
- Files and paths
- How to open / run / verify
- Verification results
- Known limitations
- Next steps (if any)

Do not modify other Agents' private directories.

Task description:
{self.task.description or ''}
"""

    def _summary(self, events: List[Dict[str, Any]]) -> str:
        result_path = self.task_directory.workspace / "RESULT.md"
        if result_path.is_file():
            try:
                text = result_path.read_text(encoding="utf-8").strip()
                if text:
                    return text[:8000]
            except OSError:
                pass
        text = self._last_assistant_text(events)
        return text[:8000]

    @staticmethod
    def _last_assistant_text(events: List[Dict[str, Any]]) -> str:
        for event in reversed(events):
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                texts = [
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(item for item in texts if item.strip())
                if text.strip():
                    return text.strip()
        return ""

    @staticmethod
    def _turn_count(events: List[Dict[str, Any]]) -> int:
        return sum(1 for event in events if event.get("type") == "turn_start")

    @staticmethod
    def _tool_count(events: List[Dict[str, Any]]) -> int:
        return sum(
            1 for event in events
            if event.get("type") == "tool_execution_end")

    @staticmethod
    def _token_count(events: List[Dict[str, Any]]) -> int:
        total = 0
        for event in events:
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            total += int(usage.get("totalTokens") or 0)
        return total

    # ------------------------------------------------------------
    def _record_usage(
        self,
        actor: str,
        events: List[Dict[str, Any]],
        provider: str,
        model: str,
    ) -> None:
        recorder = getattr(self.rt, "recorder", None)
        if recorder is None:
            return
        turn_started = 0.0
        for event in events:
            if event.get("type") == "turn_start":
                turn_started = float(event.get("_received_at") or 0.0)
                continue
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            received = float(event.get("_received_at") or 0.0)
            latency_ms = (
                max(0.0, (received - turn_started) * 1000.0)
                if turn_started and received else 0.0)
            input_tokens = (
                int(usage.get("input") or 0)
                + int(usage.get("cacheRead") or 0)
                + int(usage.get("cacheWrite") or 0)
            )
            output_tokens = int(usage.get("output") or 0)
            total_tokens = int(
                usage.get("totalTokens")
                or (input_tokens + output_tokens))
            response = LLMResponse(
                content="",
                provider=str(message.get("provider") or provider),
                model=str(message.get("model") or model),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                finish_reason=message.get("stopReason"),
                token_source=TOKEN_SOURCE_API,
                raw_response={"pi_usage": usage},
                metadata={"source": "pi"},
            )
            try:
                recorder.record(
                    agent_id=actor,
                    response=response,
                    task_id=self.task.task_id,
                    call_purpose=getattr(
                        recorder, "PURPOSE_COGNITION", "cognition"),
                    experiment_run_id=getattr(
                        self.rt, "experiment_run_id", None),
                )
            except Exception:                             # noqa: BLE001
                pass

    # ------------------------------------------------------------
    def _emit_rounds(
        self, events: List[Dict[str, Any]], actor: str,
    ) -> None:
        if self.round_sink is None:
            return
        rounds = self._rounds_from_events(events, actor)
        for payload in rounds:
            try:
                self.round_sink(payload)
            except Exception:                             # noqa: BLE001
                pass

    def _rounds_from_events(
        self, events: List[Dict[str, Any]], actor: str,
    ) -> List[Dict[str, Any]]:
        rounds: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        turn_no = 0
        tool_starts: Dict[str, Dict[str, Any]] = {}
        for event in events:
            kind = event.get("type")
            if kind == "turn_start":
                turn_no += 1
                current = {
                    "task_id": self.task.task_id,
                    "run_id": f"pi_{self.task.task_id}",
                    "iteration": int(self.task.iteration or 1),
                    "round": turn_no,
                    "status_before": self.task.status,
                    "status_after": self.task.status,
                    "summary": "",
                    "tokens": 0,
                    "latency_ms": 0.0,
                    "segments": [],
                    "_started": event.get("_received_at") or 0.0,
                }
                rounds.append(current)
            elif kind == "tool_execution_start":
                call_id = str(event.get("toolCallId") or "")
                tool_starts[call_id] = {
                    "toolName": event.get("toolName") or "",
                    "args": event.get("args"),
                }
            elif kind == "message_end":
                if current is None:
                    continue
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                content = message.get("content")
                text = ""
                thinking = ""
                tool_calls: List[Dict[str, Any]] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text += str(block.get("text") or "")
                        elif block.get("type") == "thinking":
                            thinking += str(block.get("thinking") or "")
                        elif block.get("type") == "toolCall":
                            tool_calls.append({
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(
                                        block.get("arguments") or {},
                                        ensure_ascii=False),
                                },
                            })
                elif isinstance(content, str):
                    text = content
                segment = {
                    "phase": "work",
                    "content": text,
                    "model_reasoning": thinking,
                    "tool_calls": tool_calls,
                    "tool_calls_source": "pi",
                    "decision": None,
                    "actions": [],
                    "results": [],
                    "tokens": int(usage.get("totalTokens") or 0),
                    "latency_ms": 0.0,
                    "finish_reason": message.get("stopReason"),
                    "truncated": str(
                        message.get("stopReason") or "").lower() == "length",
                }
                current["segments"].append(segment)
                current["tokens"] += int(usage.get("totalTokens") or 0)
                current["summary"] = text.strip() or current["summary"]
            elif kind == "tool_execution_end":
                if current is None:
                    continue
                call_id = str(event.get("toolCallId") or "")
                started = tool_starts.pop(call_id, {})
                name = str(
                    event.get("toolName") or started.get("toolName") or "tool")
                result = event.get("result")
                is_error = bool(event.get("isError"))
                action = {
                    "action_id": call_id,
                    "action_type": name,
                    "parameters": started.get("args") or {},
                    "agent_id": actor,
                }
                action_result = {
                    "action_id": call_id,
                    "action_type": name,
                    "success": not is_error,
                    "output": result,
                    "error": None if not is_error else str(result),
                    "duration_ms": event.get("durationMs") or 0.0,
                }
                segment = (current["segments"][-1]
                           if current["segments"] else None)
                if segment is None:
                    segment = {
                        "phase": "work", "content": "",
                        "model_reasoning": "", "tool_calls": [],
                        "tool_calls_source": "pi", "decision": None,
                        "actions": [], "results": [], "tokens": 0,
                        "latency_ms": 0.0, "finish_reason": None,
                        "truncated": False,
                    }
                    current["segments"].append(segment)
                segment["actions"].append(action)
                segment["results"].append(action_result)
            elif kind == "turn_end":
                if current is None:
                    continue
                received = event.get("_received_at") or 0.0
                started = current.pop("_started", 0.0) or 0.0
                current["latency_ms"] = round(
                    max(0.0, (received - started) * 1000.0), 1)
                current["summary"] = current["summary"] or (
                    f"Pi turn {current['round']}")
                current = None
        return rounds


__all__ = ["PiWorkRuntime"]
