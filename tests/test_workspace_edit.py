"""Workspace 范围读取 + old/new 局部编辑的回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

from actions import (
    ACTION_WORKSPACE_EDIT,
    ACTION_WORKSPACE_READ,
    ActionIntent,
    ActionResult,
    WorkspaceEditExecutor,
    WorkspaceReadExecutor,
    render_read_results,
)
from services.artifacts import edit_artifact, read_artifact


class _Gateway:
    def __init__(self, content: str, revision: int = 3):
        self.content = content
        self.revision = revision
        self.edits = []

    def read(self, workspace_id, path, agent_id):
        return SimpleNamespace(
            success=True,
            result={"content": self.content, "revision": self.revision,
                    "path": path},
            error=None)

    def edit(self, workspace_id, path, base_revision, content, agent_id):
        self.edits.append((workspace_id, path, base_revision, content, agent_id))
        if int(base_revision) != self.revision:
            return SimpleNamespace(
                success=False,
                error={"code": "REVISION_CONFLICT", "message": "stale"})
        self.content = content
        self.revision += 1
        return SimpleNamespace(
            success=True, result={"revision": self.revision}, error=None)


class TestRangeRead:
    def test_full_read_is_backward_compatible(self):
        gw = _Gateway("a\nb\nc\n")
        out = read_artifact(gw, "WS", "agent_a", "x.py")
        assert out["ok"] is True
        assert out["content"] == "a\nb\nc\n"
        assert out["total_lines"] == 3
        assert out["truncated"] is False

    def test_offset_and_limit(self):
        gw = _Gateway("a\nb\nc\nd\ne")
        out = read_artifact(gw, "WS", "agent_a", "x.py", offset=2, limit=2)
        assert out["content"] == "b\nc"
        assert out["start_line"] == 2
        assert out["end_line"] == 3
        assert out["total_lines"] == 5
        assert out["truncated"] is True

    def test_start_line_and_max_lines(self):
        gw = _Gateway("a\nb\nc\nd\ne")
        out = read_artifact(
            gw, "WS", "agent_a", "x.py", start_line=2, max_lines=2)
        assert out["content"] == "b\nc"
        assert out["start_line"] == 2
        assert out["max_lines"] == 2
        assert out["end_line"] == 3
        assert out["next_start_line"] == 4
        assert out["truncated"] is True

        rest = read_artifact(
            gw, "WS", "agent_a", "x.py", start_line=4, max_lines=2)
        assert rest["content"] == "d\ne"
        assert rest["next_start_line"] is None

    def test_start_line_and_offset_conflict(self):
        gw = _Gateway("a\nb")
        out = read_artifact(
            gw, "WS", "agent_a", "x.py", start_line=1, offset=2)
        assert out["ok"] is False
        assert out["code"] == "bad_pagination"

    def test_offset_beyond_end(self):
        gw = _Gateway("a\nb")
        out = read_artifact(gw, "WS", "agent_a", "x.py", offset=10, limit=5)
        assert out["content"] == ""
        assert out["truncated"] is True
        assert out["total_lines"] == 2

    def test_limit_is_capped(self):
        gw = _Gateway("\n".join(str(i) for i in range(500)))
        out = read_artifact(gw, "WS", "agent_a", "x.py", offset=1, limit=1000)
        assert out["content"].count("\n") + 1 == 400
        assert out["end_line"] == 400
        assert out["truncated"] is True

    def test_bad_offset(self):
        gw = _Gateway("a")
        out = read_artifact(gw, "WS", "agent_a", "x.py", offset=0)
        assert out["ok"] is False
        assert out["code"] == "bad_pagination"


class TestEditArtifact:
    def test_unique_replace(self):
        gw = _Gateway("a\nb\nc", revision=3)
        out = edit_artifact(gw, "WS", "agent_a", "x.py", "b", "B")
        assert out["ok"] is True
        assert out["matched"] == 1
        assert out["replaced"] == 1
        assert out["previous_revision"] == 3
        assert out["revision"] == 4
        assert gw.content == "a\nB\nc"

    def test_no_match(self):
        gw = _Gateway("a\nb")
        out = edit_artifact(gw, "WS", "agent_a", "x.py", "zzz", "Y")
        assert out["ok"] is False
        assert out["code"] == "no_match"
        assert not gw.edits

    def test_multiple_matches_fail_by_default(self):
        gw = _Gateway("x\nx")
        out = edit_artifact(gw, "WS", "agent_a", "x.py", "x", "y")
        assert out["ok"] is False
        assert out["code"] == "multiple_matches"
        assert out["matched"] == 2
        assert not gw.edits

    def test_replace_all(self):
        gw = _Gateway("x\nx")
        out = edit_artifact(gw, "WS", "agent_a", "x.py", "x", "y",
                            replace_all=True)
        assert out["ok"] is True
        assert out["replaced"] == 2
        assert gw.content == "y\ny"

    def test_stale_revision(self):
        gw = _Gateway("a", revision=5)
        out = edit_artifact(gw, "WS", "agent_a", "x.py", "a", "b",
                            expected_revision=4)
        assert out["ok"] is False
        assert out["code"] == "stale_revision"
        assert not gw.edits

    def test_no_change(self):
        gw = _Gateway("a")
        out = edit_artifact(gw, "WS", "agent_a", "x.py", "a", "a")
        assert out["ok"] is False
        assert out["code"] == "no_change"

    def test_empty_old_string(self):
        gw = _Gateway("a")
        out = edit_artifact(gw, "WS", "agent_a", "x.py", "", "b")
        assert out["ok"] is False
        assert out["code"] == "bad_argument"


class TestExecutors:
    def test_read_executor_passes_range(self):
        seen = {}

        def read_fn(gateway, ws, agent, path, *, start_line=None,
                    max_lines=None):
            seen.update(start_line=start_line, max_lines=max_lines)
            return {"ok": True, "path": path, "revision": 1,
                    "chars": 3, "content": "b\nc",
                    "total_chars": 10, "total_lines": 5,
                    "start_line": 2, "max_lines": 2,
                    "end_line": 3, "next_start_line": None,
                    "truncated": True}

        ex = WorkspaceReadExecutor(read_fn, gateway=object(), workspace_id="WS")
        result = ex.execute(ActionIntent(
            action_type=ACTION_WORKSPACE_READ, agent_id="agent_a",
            parameters={"path": "x.py", "start_line": 2, "max_lines": 2}))
        assert result.success is True
        assert seen == {"start_line": 2, "max_lines": 2}
        assert result.output["start_line"] == 2

    def test_edit_executor_uses_intent_agent(self):
        seen = {}

        def edit_fn(agent_id, path, old, new, **kwargs):
            seen["agent_id"] = agent_id
            return {"ok": True, "path": path, "mode": "edit",
                    "revision": 4, "previous_revision": 3,
                    "previous_chars": 10, "chars": 10,
                    "matched": 1, "replaced": 1}

        ex = WorkspaceEditExecutor(edit_fn)
        result = ex.execute(ActionIntent(
            action_type=ACTION_WORKSPACE_EDIT, agent_id="agent_a",
            parameters={"path": "x.py", "old_string": "a",
                        "new_string": "b"}))
        assert result.success is True
        assert seen["agent_id"] == "agent_a"

    def test_edit_executor_rejects_empty_old(self):
        ex = WorkspaceEditExecutor(lambda *a, **k: {})
        result = ex.execute(ActionIntent(
            action_type=ACTION_WORKSPACE_EDIT, agent_id="agent_a",
            parameters={"path": "x.py", "old_string": "",
                        "new_string": "b"}))
        assert result.success is False
        assert result.metadata["code"] == "bad_argument"


def test_range_header_is_rendered_for_agent():
    result = ActionResult(
        success=True,
        action_type=ACTION_WORKSPACE_READ,
        output={"content": "b\nc", "start_line": 2, "end_line": 3,
                "total_lines": 5, "total_chars": 10,
                "chars": 3, "path": "x.py", "revision": 1},
    )
    intent = ActionIntent(action_type=ACTION_WORKSPACE_READ,
                          agent_id="agent_a",
                          parameters={"path": "x.py", "start_line": 2, "max_lines": 2})
    text = render_read_results([(intent, result)])
    assert isinstance(text, str)
    assert "[lines 2-3 of 5]" in text
    assert "b" in text and "c" in text
