"""DeepSeek Search 客户端与 Front web_search 动作测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from llm.web_search import WebSearchClient, WebSearchError
from work.front_actions import build_front_dispatcher, execute_front_actions


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/tools/search":
            body = {
                "results": [{
                    "title": "Result",
                    "url": "https://example.com/a",
                    "snippet": "snippet",
                    "summary": "summary",
                    "content": "",
                    "site_name": "Example",
                    "date": "2026-01-01T00:00:00+00:00",
                    "authority": 3,
                    "rerank_score": 2.1,
                }]
            }
            if payload.get("query") == "empty":
                body = {"results": []}
        elif self.path == "/v1/tools/fetch":
            body = {"title": "Fetched", "content": "# Page"}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_search_normalizes_results(upstream):
    client = WebSearchClient("fake-key", base_url=upstream)
    result = client.search("hello", count=5)
    assert result["query"] == "hello"
    assert result["results"][0]["title"] == "Result"
    assert result["results"][0]["url"] == "https://example.com/a"


def test_search_validates_count(upstream):
    client = WebSearchClient("fake-key", base_url=upstream)
    with pytest.raises(WebSearchError):
        client.search("hello", count=51)


def test_fetch_rejects_private_urls(upstream):
    client = WebSearchClient("fake-key", base_url=upstream)
    with pytest.raises(WebSearchError):
        client.fetch("http://127.0.0.1/secret")


def test_fetch_rejects_overlong_urls(upstream):
    client = WebSearchClient("fake-key", base_url=upstream)
    with pytest.raises(WebSearchError):
        client.fetch("https://example.com/" + "a" * 9000)


def test_front_web_search_action_uses_service():
    calls = []

    def search(query, **kwargs):
        calls.append((query, kwargs))
        return {"query": query, "results": [{"title": "T", "url": "u"}]}

    dispatcher = build_front_dispatcher(
        workspace_of=lambda _aid: None,
        drafts=None,
        web_search_fn=search,
    )
    result = execute_front_actions(
        dispatcher,
        [{"type": "web_search",
          "parameters": {"query": "hello", "count": 3}}],
        agent_id="agent_a",
    )
    assert calls
    assert calls[0][0] == "hello"
    assert calls[0][1]["count"] == 3
    assert "Result" not in result
    assert "T" in result


def test_front_web_fetch_action_uses_service():
    calls = []

    def fetch(url):
        calls.append(url)
        return {"url": url, "title": "Fetched", "content": "# Page"}

    dispatcher = build_front_dispatcher(
        workspace_of=lambda _aid: None,
        drafts=None,
        web_fetch_fn=fetch,
    )
    result = execute_front_actions(
        dispatcher,
        [{"type": "web_fetch",
          "parameters": {"url": "https://example.com/a"}}],
        agent_id="agent_a",
    )
    assert calls == ["https://example.com/a"]
    assert "Fetched" in result


def test_cli_bridge_uses_local_token(monkeypatch):
    seen = {}

    class _Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            seen["path"] = self.path
            seen["token"] = self.headers.get("X-AIEOS-Search-Token")
            seen["payload"] = json.loads(self.rfile.read(length) or b"{}")
            raw = json.dumps({
                "ok": True,
                "results": [{
                    "title": "CLI result",
                    "url": "https://example.com/cli",
                    "summary": "summary",
                }],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "AIEOS_WEB_SEARCH_URL",
            f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("AIEOS_WEB_SEARCH_TOKEN", "local-token")
        monkeypatch.setenv("AIEOS_AGENT_ID", "agent_a")
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "aieos_web_search.py")
        completed = subprocess.run(
            [sys.executable, str(script), "search", "hello",
             "--count", "3", "--json"],
            capture_output=True, text=True, check=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    data = json.loads(completed.stdout)
    assert data["results"][0]["title"] == "CLI result"
    assert seen["path"] == "/internal/web-search"
    assert seen["token"] == "local-token"
    assert seen["payload"] == {
        "query": "hello",
        "count": 3,
        "need_content": False,
        "ori_question": "",
        "agent_id": "agent_a",
    }


def test_ui_internal_endpoint_requires_token():
    from ui.server import Handler

    calls = []

    class _Controller:
        front_agent = "agent_a"

        def web_search(self, agent_id, query, **kwargs):
            calls.append((agent_id, query, kwargs))
            return {"results": [{"title": "UI result"}]}

    old_controller = Handler.controller
    old_token = Handler.web_search_token
    Handler.controller = _Controller()
    Handler.web_search_token = "expected-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/internal/web-search"
    body = json.dumps({"query": "hello", "count": 2}).encode()
    try:
        denied = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(denied, timeout=5)
        assert exc.value.code == 403

        allowed = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AIEOS-Search-Token": "expected-token",
            })
        with urllib.request.urlopen(allowed, timeout=5) as response:
            data = json.loads(response.read().decode())
    finally:
        Handler.controller = old_controller
        Handler.web_search_token = old_token
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert data["ok"] is True
    assert data["results"][0]["title"] == "UI result"
    assert calls == [
        ("agent_a", "hello",
         {"count": 2, "need_content": False, "ori_question": ""}),
    ]
