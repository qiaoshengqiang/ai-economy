"""Life 工具层的路径 / 读取边界。"""

from __future__ import annotations

from work.life_tools import clip_text, list_task_files, read_text_slice


def test_clip_text_marks_truncation():
    assert clip_text("abc", 10) == "abc"
    assert clip_text("abcdef", 3).startswith("abc")
    assert "截断" in clip_text("abcdef", 3)


def test_read_text_slice_returns_requested_lines(tmp_path):
    path = tmp_path / "index.html"
    path.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

    result = read_text_slice(tmp_path, "index.html", start_line=2, max_lines=2)

    assert result["ok"] is True
    assert result["start_line"] == 2
    assert "line2" in result["content"]
    assert "line3" in result["content"]
    assert "line4" not in result["content"]
    assert result["truncated"] is True


def test_read_text_slice_rejects_path_escape(tmp_path):
    for raw in ("../escape.txt", "/tmp/escape.txt", ".env"):
        result = read_text_slice(tmp_path, raw)
        assert result["ok"] is False
        assert result["code"] == "bad_path"


def test_read_text_slice_rejects_binary(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")

    result = read_text_slice(tmp_path, "blob.bin")

    assert result["ok"] is False
    assert result["code"] == "binary_file"


def test_list_task_files_manifest_and_skip(tmp_path):
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("no", encoding="utf-8")

    result = list_task_files(
        tmp_path, [{"path": "index.html"}, {"path": "../escape.txt"}])

    assert result["ok"] is True
    assert result["source"] == "manifest"
    assert [item["path"] for item in result["paths"]] == ["index.html"]


def test_list_task_files_scan_skips_internal_dirs(tmp_path):
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    (tmp_path / ".aieos").mkdir()
    (tmp_path / ".aieos" / "work_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")

    result = list_task_files(tmp_path)

    assert result["ok"] is True
    assert result["source"] == "scan"
    assert [item["path"] for item in result["paths"]] == ["index.html"]
