"""Front 只读项目上下文。"""

from __future__ import annotations

import pytest

from work.project_context import (
    ProjectAccessError,
    list_project,
    project_summary,
    read_project_file,
    render_project_context,
)


def test_summary_shows_root_files_and_web_candidates(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><head><title>Main</title></head></html>", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    (site / "page.html").write_text(
        "<html><head><title>Sub</title></head></html>", encoding="utf-8")
    (tmp_path / "readme.md").write_text("# Hi", encoding="utf-8")
    heavy = tmp_path / "node_modules"
    heavy.mkdir()
    (heavy / "x.js").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"noise")

    text = render_project_context(str(tmp_path))

    assert "index.html" in text
    assert "Main" in text
    assert "site/page.html" in text
    assert "Sub" in text
    assert "readme.md" in text
    assert "node_modules" not in text
    assert "SECRET" not in text
    assert ".DS_Store" not in text


def test_list_project_respects_depth_and_skips_secret(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "c.txt").write_text("c", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")

    result = list_project(str(tmp_path), depth=1)
    paths = [item["path"] for item in result["entries"]]

    assert "a" in paths
    assert "a/b" not in paths
    assert ".env" not in paths


def test_read_project_file_returns_requested_lines(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text(
        "\n".join(f"line{index}" for index in range(10)), encoding="utf-8")

    result = read_project_file(
        str(tmp_path), "a.txt", start_line=3, max_lines=2)

    assert result["content"] == "line2\nline3"
    assert result["total_lines"] == 10
    assert result["start_line"] == 3
    assert result["max_lines"] == 2
    assert result["next_start_line"] == 5


def test_read_project_file_keeps_old_aliases(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text(
        "\n".join(f"line{index}" for index in range(10)), encoding="utf-8")

    result = read_project_file(str(tmp_path), "a.txt", offset=3, limit=2)

    assert result["content"] == "line2\nline3"
    assert result["start_line"] == 3
    assert result["next_start_line"] == 5


def test_read_project_file_rejects_path_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(ProjectAccessError):
        read_project_file(str(root), "../outside.txt")


def test_read_project_file_rejects_secret_and_binary(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "data.txt").write_bytes(b"abc\x00def")

    with pytest.raises(ProjectAccessError):
        read_project_file(str(tmp_path), ".env")
    with pytest.raises(ProjectAccessError):
        read_project_file(str(tmp_path), "data.txt")


def test_read_project_file_rejects_symlink_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.html").write_text("outside", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectAccessError):
        read_project_file(str(root), "link/secret.html")


def test_project_summary_reports_missing_directory(tmp_path):
    summary = project_summary(str(tmp_path / "missing"))

    assert summary["ok"] is False
    assert "不存在" in summary["error"]
