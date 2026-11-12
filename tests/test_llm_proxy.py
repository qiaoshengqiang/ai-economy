"""
AIEOS LLM Proxy：预算检查 + usage 回写。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from work.llm_proxy import AIEOSLLMProxy


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({
            "id": "chatcmpl-test",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Recorder:
    PURPOSE_COGNITION = "cognition"

    def __init__(self):
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)
        return "call_1"


def _upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_proxy_records_usage_and_rejects_exhausted_budget():
    upstream, upstream_url = _upstream()
    recorder = _Recorder()
    budget = SimpleNamespace(
        snapshot=lambda _task_id: SimpleNamespace(remaining_aic=1.0))
    runtime = SimpleNamespace(
        recorder=recorder, budget_service=budget,
        experiment_run_id="exp")
    proxy = AIEOSLLMProxy(
        runtime, agent_id="agent_c", task_id="task_x",
        api_key="fake", provider="deepseek", model="deepseek-flash",
        upstream_base_url=upstream_url)
    base = proxy.start()
    try:
        request = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({"model": "deepseek-flash",
                             "messages": []}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        response = urllib.request.urlopen(request, timeout=5)
        payload = json.loads(response.read())
        assert payload["usage"]["total_tokens"] == 15
        assert recorder.calls
        recorded = recorder.calls[0]
        assert recorded["agent_id"] == "agent_c"
        assert recorded["task_id"] == "task_x"
        assert recorded["response"].input_tokens == 10
        assert recorded["response"].output_tokens == 5
        assert recorded["response"].total_tokens == 15

        budget.snapshot = lambda _task_id: SimpleNamespace(remaining_aic=0.0)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 429
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
