"""Workspace 只读搜索（workspace_search）的回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

from actions import (
    ACTION_WORKSPACE_SEARCH,
    ActionIntent,
    ActionResult,
    WorkspaceSearchExecutor,
    render_read_results,
)
from services.artifacts import search_artifacts


class _Gateway:
    def __init__(self, files):
        self.files = {("/" + k.lstrip("/")): v for k, v in files.items()}

    def list(self, workspace_id, path, agent_id):
        path = "/" + str(path or "").strip("/") if path != "/" else "/"
        if path in self.files:
            return SimpleNamespace(success=False, result=None, error=None)
        prefix = "/" if path == "/" else path.rstrip("/") + "/"
        names = {}
        for p, content in self.files.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if not rest:
                continue
            head, _, tail = rest.partition("/")
            names[head] = "directory" if tail else "file"
        if not names:
            return SimpleNamespace(success=False, result=None, error=None)
        return SimpleNamespace(
            success=True,
            result={"entries": [
                {"name": n, "type": t} for n, t in sorted(names.items())
            ]},
            error=None)

    def read(self, workspace_id, path, agent_id):
        path = "/" + str(path or "").strip("/")
        if path not in self.files:
            return SimpleNamespace(success=False, result=None, error=None)
        c = self.files[path]
        return SimpleNamespace(
            success=True, result={"content": c, "revision": 1}, error=None)


FILES = {
    "src/app.py": "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
    "src/util.py": "TOKEN = 'refresh_token'\n# refresh_token appears again\n",
    "README.md": "refresh_token docs\n",
    "tests/test_app.py": "def test_add():\n    assert add(1, 2) == 3\n",
}


class TestSearchArtifacts:
    def test_literal_search_across_files(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "refresh_token")
        assert out["ok"] is True
        assert out["total_matches"] == 3
        paths = [m["path"] for m in out["matches"]]
        assert "src/util.py" in paths
        assert "README.md" in paths
        assert all(isinstance(m["line"], int) for m in out["matches"])

    def test_glob_filter(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "refresh_token",
                               glob="*.py")
        assert all(m["path"].endswith(".py") for m in out["matches"])
        assert out["total_matches"] == 2

    def test_path_subtree(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "refresh_token",
                               path="src")
        assert all(m["path"].startswith("src/") for m in out["matches"])
        assert out["total_matches"] == 2

    def test_path_single_file(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "refresh_token",
                               path="README.md")
        assert out["total_matches"] == 1
        assert out["matches"][0]["path"] == "README.md"

    def test_case_sensitive_default(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "REFRESH_TOKEN")
        assert out["total_matches"] == 0
        out2 = search_artifacts(_Gateway(FILES), "WS", "agent_a", "REFRESH_TOKEN",
                                case_sensitive=False)
        assert out2["total_matches"] == 3

    def test_max_results_truncated(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "refresh_token",
                               max_results=1)
        assert len(out["matches"]) == 1
        assert out["total_matches"] == 3
        assert out["truncated"] is True

    def test_context_lines(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "refresh_token",
                               path="README.md", context_lines=1)
        assert out["matches"][0]["context"]

    def test_bad_query(self):
        out = search_artifacts(_Gateway(FILES), "WS", "agent_a", "")
        assert out["ok"] is False
        assert out["code"] == "bad_query"


class TestSearchExecutor:
    def test_executor_returns_structured_matches(self):
        seen = {}

        def search_fn(agent_id, query, **kwargs):
            seen.update(agent_id=agent_id, query=query, **kwargs)
            return {"ok": True, "query": query, "path": "", "glob": "",
                    "matches": [{"path": "a.py", "line": 2, "snippet": "x"}],
                    "total_matches": 1, "files_scanned": 1,
                    "truncated": False}

        ex = WorkspaceSearchExecutor(search_fn)
        result = ex.execute(ActionIntent(
            action_type=ACTION_WORKSPACE_SEARCH, agent_id="agent_a",
            parameters={"query": "x", "max_results": 5}))
        assert result.success is True
        assert seen["agent_id"] == "agent_a"
        assert result.output["total_matches"] == 1

    def test_executor_rejects_empty_query(self):
        ex = WorkspaceSearchExecutor(lambda *a, **k: {})
        result = ex.execute(ActionIntent(
            action_type=ACTION_WORKSPACE_SEARCH, agent_id="agent_a",
            parameters={"query": ""}))
        assert result.success is False
        assert result.metadata["code"] == "bad_query"

    def test_search_result_is_rendered(self):
        result = ActionResult(
            success=True,
            action_type=ACTION_WORKSPACE_SEARCH,
            output={"query": "x", "total_matches": 2, "files_scanned": 1,
                    "truncated": False,
                    "matches": [
                        {"path": "a.py", "line": 2, "snippet": "x"},
                        {"path": "b.py", "line": 9, "snippet": "x"},
                    ]},
        )
        intent = ActionIntent(action_type=ACTION_WORKSPACE_SEARCH,
                              agent_id="agent_a",
                              parameters={"query": "x"})
        text = render_read_results([(intent, result)])
        assert isinstance(text, str)
        assert "a.py:2" in text
        assert "b.py:9" in text
        assert "total=2" in text
