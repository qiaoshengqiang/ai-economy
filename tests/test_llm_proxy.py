"""
AIEOS LLM Proxy：预算检查 + usage 回写。
"""

from __future__ import annotations

import json
import threading
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from llm import prompt_log
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


def test_proxy_can_record_prompt_input(tmp_path):
    upstream, upstream_url = _upstream()
    recorder = _Recorder()
    runtime = SimpleNamespace(
        recorder=recorder, experiment_run_id="exp")
    proxy = AIEOSLLMProxy(
        runtime, agent_id="agent_c", task_id="task_x",
        api_key="fake", provider="deepseek", model="deepseek-flash",
        upstream_base_url=upstream_url)
    prompt_log.configure(True, tmp_path)
    base = proxy.start()
    try:
        request = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "PI-PROMPT-MARKER"}],
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(request, timeout=5).read()
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
        prompt_log.configure(False, None)

    files = list(tmp_path.glob("prompts-*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text("utf-8").splitlines()[0])
    assert record["agent_id"] == "agent_c"
    assert record["task_id"] == "task_x"
    assert record["phase"] == "pi"
    assert record["messages"][0]["content"] == "PI-PROMPT-MARKER"


def test_proxy_records_usage_without_budget_gate():
    upstream, upstream_url = _upstream()
    recorder = _Recorder()
    runtime = SimpleNamespace(
        recorder=recorder, experiment_run_id="exp")
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
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()
