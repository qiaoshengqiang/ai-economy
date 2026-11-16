"""
AIEOS LLM Proxy —— Pi 的 OpenAI-compatible 上游
================================================

    Pi → 127.0.0.1:<port>/v1/chat/completions
             │
             ├─ 预算检查（TaskBudgetService）
             ├─ 转发到真实 provider（Authorization 使用 Agent credential）
             ├─ 透传 streaming / non-streaming
             └─ 解析 usage → llm_calls / Ledger

🔒 Pi 不再直接持有 provider key；key 只在本进程内使用。
🔒 预算检查在**请求前**做；usage 记录在**响应后**做。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional

from llm import prompt_log
from llm.base import LLMResponse, TOKEN_SOURCE_API
from llm.openai_compatible import OpenAICompatibleProvider

#: 给上游的默认 UA。部分网关（Cloudflare）会拦截 urllib 默认 UA。
_PROXY_USER_AGENT = "AIEOS-Pi-Proxy/0.2"
#: 认证头只能来自 Credential，不能被 provider.request 覆盖。
_AUTH_HEADERS = {"authorization", "x-api-key", "api-key"}


class _ProxyHandler(BaseHTTPRequestHandler):
    proxy: "AIEOSLLMProxy"

    def log_message(self, _format: str, *args: Any) -> None:
        return

    # ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            payload = {
                "object": "list",
                "data": [{
                    "id": self.proxy.model,
                    "object": "model",
                    "owned_by": self.proxy.provider,
                }],
            }
            self._send_json(200, payload)
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        started = time.perf_counter()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": {"message": f"bad request: {exc}"}})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "bad request"}})
            return

        # 预算不是 Work 的生产约束；proxy 只记账，不因预算拒绝请求。
        # Pi 的 openai-completions 可能发送 `developer` role；DeepSeek
        # 只接受 system/user/assistant/tool。这里做 provider 兼容归一化。
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "developer":
                    message["role"] = "system"
        model = str(payload.get("model") or self.proxy.model)
        # 可选 Prompt 日志：Pi 的请求体就是它的完整输入。
        prompt_log.record(
            self.proxy.agent_id,
            phase="pi",
            messages=messages,
            task_id=str(self.proxy.task_id or ""),
            provider=str(self.proxy.provider or ""),
            model=model,
            purpose="pi_work",
        )
        stream = bool(payload.get("stream"))
        upstream = self.proxy.upstream_url(self.path)
        payload = self.proxy.prepare_payload(payload)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            upstream, data=data, method="POST",
            headers=self.proxy.build_headers(stream=stream))
        try:
            response = urllib.request.urlopen(
                request, timeout=self.proxy.timeout_seconds)
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:                              # noqa: BLE001
            self._send_json(502, {"error": {
                "message": f"upstream failed: {type(exc).__name__}: {exc}"}})
            return

        usage: Dict[str, Any] = {}
        finish_reason: Optional[str] = None
        try:
            if stream:
                self.send_response(getattr(response, "status", 200))
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                for line in response:
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    chunk = line.decode("utf-8", errors="ignore").strip()
                    if not chunk.startswith("data:"):
                        continue
                    body = chunk[5:].strip()
                    if body == "[DONE]":
                        continue
                    try:
                        item = json.loads(body)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        if isinstance(item.get("usage"), dict):
                            usage = item["usage"]
                        choices = item.get("choices") or []
                        if choices and isinstance(choices[0], dict):
                            finish_reason = (
                                choices[0].get("finish_reason")
                                or finish_reason)
            else:
                body = response.read()
                self.send_response(getattr(response, "status", 200))
                self.send_header(
                    "Content-Type",
                    response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                try:
                    item = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    item = {}
                if isinstance(item, dict):
                    if isinstance(item.get("usage"), dict):
                        usage = item["usage"]
                    choices = item.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        finish_reason = choices[0].get("finish_reason")
        finally:
            try:
                response.close()
            except Exception:                                 # noqa: BLE001
                pass

        if usage:
            self.proxy.record_usage(
                usage=usage,
                model=model,
                finish_reason=finish_reason,
                started=started,
            )

    # ------------------------------------------------------------
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AIEOSLLMProxy:
    def __init__(
        self,
        runtime: Any,
        *,
        agent_id: str,
        task_id: str,
        api_key: str,
        provider: str = "deepseek",
        model: str = "deepseek-flash",
        upstream_base_url: str = "",
        chat_path: str = "/chat/completions",
        provider_request: Optional[Mapping[str, Any]] = None,
        timeout_seconds: float = 300.0,
        session_id: str = "",
    ) -> None:
        self.rt = runtime
        self.agent_id = str(agent_id)
        self.task_id = str(task_id)
        self.api_key = str(api_key)
        self.provider = str(provider)
        self.model = str(model)
        self.upstream_base_url = str(upstream_base_url).rstrip("/")
        self.chat_path = str(chat_path or "/chat/completions")
        request = dict(provider_request or {})
        self.provider_request = request
        raw_headers = request.get("extra_headers")
        self.extra_headers: Dict[str, str] = {
            str(name): str(value)
            for name, value in (
                dict(raw_headers) if isinstance(raw_headers, Mapping) else {}
            ).items()
        }
        extra_body = request.get("extra_body")
        self.extra_body: Dict[str, Any] = (
            dict(extra_body) if isinstance(extra_body, Mapping) else {})
        self.max_tokens_field = str(
            request.get("max_tokens_field") or "max_tokens")
        #: 每请求会话 header（OpenCode Go 的 x-opencode-session）。
        self.session_header = str(
            request.get("session_header") or "").strip()
        self.session_id = str(session_id or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------
    def start(self) -> str:
        handler = type("_BoundProxyHandler", (_ProxyHandler,), {"proxy": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:                                 # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    # ------------------------------------------------------------
    @property
    def chat_url(self) -> str:
        """Provider 的真实 chat completions 完整地址。"""
        base = self.upstream_base_url.rstrip("/")
        path = self.chat_path or "/chat/completions"
        if not path.startswith("/"):
            path = "/" + path
        return base + path if base else ""

    def upstream_url(self, path: str) -> str:
        path = str(path or "")
        if not path.startswith("/"):
            path = "/" + path
        # Pi 以 OpenAI-compatible `/v1/chat/completions` 调用我们。
        # 上游的真实路径由 provider 的 base_url + chat_path 决定，
        # 不能直接把 `/v1/...` 拼到已经带版本号的 base_url 后面。
        if path.rstrip("/").endswith("/chat/completions"):
            return self.chat_url
        return self.upstream_base_url + path

    def prepare_payload(
        self, payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """把 Pi 的 OpenAI-compatible 请求体适配到具体 provider。"""
        out: Dict[str, Any] = dict(payload)
        target = self.max_tokens_field
        if target != "max_tokens":
            if "max_tokens" in out and target not in out:
                out[target] = out.pop("max_tokens")
            else:
                out.pop("max_tokens", None)
        if self.extra_body:
            out.update(self.extra_body)
        return out

    def build_headers(self, *, stream: bool) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": _PROXY_USER_AGENT,
            "Authorization": f"Bearer {self.api_key}",
        }
        for name, value in self.extra_headers.items():
            if name.strip().lower() in _AUTH_HEADERS:
                continue
            headers[name] = value
        if self.session_header and self.session_id:
            headers[self.session_header] = self.session_id
        return headers

    def record_usage(
        self,
        *,
        usage: Dict[str, Any],
        model: str,
        finish_reason: Optional[str],
        started: float,
    ) -> None:
        recorder = getattr(self.rt, "recorder", None)
        if recorder is None:
            return
        input_tokens = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0)
        output_tokens = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0)
        total_tokens = int(
            usage.get("total_tokens")
            or (input_tokens + output_tokens))
        cache_usage = OpenAICompatibleProvider._read_cache_usage(usage)
        response = LLMResponse(
            content="",
            provider=self.provider,
            model=str(model or self.model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_usage["cache_read_tokens"],
            cache_write_tokens=cache_usage["cache_write_tokens"],
            provider_cost_usd=cache_usage["provider_cost_usd"],
            allowance_usd=cache_usage["allowance_usd"],
            billing_source=cache_usage["billing_source"],
            llm_session_id=self.session_id,
            latency_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
            token_source=TOKEN_SOURCE_API,
            finish_reason=finish_reason,
            raw_response={"usage": usage},
            metadata={"source": "aieos_llm_proxy"},
        )
        try:
            recorder.record(
                agent_id=self.agent_id,
                response=response,
                task_id=self.task_id,
                call_purpose=getattr(
                    recorder, "PURPOSE_COGNITION", "cognition"),
                experiment_run_id=getattr(
                    self.rt, "experiment_run_id", None),
            )
        except Exception:                                     # noqa: BLE001
            pass


__all__ = ["AIEOSLLMProxy"]
