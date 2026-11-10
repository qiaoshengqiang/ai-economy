"""DSML 适配层：真实双竖线样本 + 格式变体 + fail-closed。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.dsml import (
    looks_like_dsml, normalize_dsml, parse_dsml_tool_calls,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dsml"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _calls(result):
    return [c["function"]["name"] for c in result.tool_calls]


def test_vendor_file_is_present_with_mit_license():
    vendor = Path(__file__).resolve().parent.parent / "llm" / "vendor" / "deepseek"
    assert (vendor / "encoding.py").is_file()
    assert (vendor / "LICENSE").is_file()
    assert "MIT License" in (vendor / "README.md").read_text(encoding="utf-8") \
        or "MIT" in (vendor / "README.md").read_text(encoding="utf-8")


def test_real_double_bar_workspace_write_is_parsed():
    text = _fixture("workspace_write_double_bar.txt")
    assert looks_like_dsml(text)

    result = parse_dsml_tool_calls(text)

    assert result.found is True
    assert result.error == ""
    assert _calls(result) == ["workspace_write"]
    args = json.loads(result.tool_calls[0]["function"]["arguments"])
    assert args["path"] == "index.html"
    assert "<!DOCTYPE html>" in args["content"]


def test_real_double_bar_tool_help_is_parsed():
    text = _fixture("tool_help_double_bar.txt")

    result = parse_dsml_tool_calls(text, allowed_actions={"tool_help"})

    assert result.error == ""
    assert _calls(result) == ["tool_help", "tool_help"]
    tools = [
        json.loads(c["function"]["arguments"])["tool"]
        for c in result.tool_calls
    ]
    assert tools == ["playwright", "node"]


def test_single_bar_and_ascii_variants_are_normalized():
    text = _fixture("tool_help_double_bar.txt")

    for variant in (
        text.replace("｜｜DSML｜｜", "｜DSML｜"),
        text.replace("｜｜DSML｜｜", "||DSML||"),
    ):
        result = parse_dsml_tool_calls(variant)
        assert result.error == "", variant[:40]
        assert len(result.tool_calls) == 2


def test_missing_string_attribute_gets_default_true():
    text = _fixture("tool_help_double_bar.txt").replace(' string="true"', "")

    result = parse_dsml_tool_calls(text)

    assert result.error == ""
    args = json.loads(result.tool_calls[0]["function"]["arguments"])
    assert args["tool"] == "playwright"


def test_missing_wrapper_is_added():
    text = "\n".join(
        line for line in _fixture("tool_help_double_bar.txt").splitlines()
        if " calls>" not in line
    )

    result = parse_dsml_tool_calls(text)

    assert result.error == ""
    assert len(result.tool_calls) == 2


def test_truncated_block_fails_closed():
    text = _fixture("workspace_write_double_bar.txt")
    truncated = text[: len(text) // 2]

    result = parse_dsml_tool_calls(truncated)

    assert result.found is True
    assert result.error != ""
    assert result.tool_calls == [], "残缺块绝不能产生可执行动作"


def test_unknown_action_is_rejected():
    text = _fixture("tool_help_double_bar.txt")

    result = parse_dsml_tool_calls(text, allowed_actions={"workspace_write"})

    assert result.error != ""
    assert result.tool_calls == []


def test_nested_arguments_wrapper_is_unwrapped():
    text = (
        "<｜DSML｜ calls>\n"
        '<｜DSML｜ invoke name="workspace_write">\n'
        '<｜DSML｜ parameter name="arguments" string="false">'
        '{"path": "a.txt", "content": "x"}'
        "</｜DSML｜ parameter>\n"
        "</｜DSML｜ invoke>\n"
        "</｜DSML｜ calls>"
    )

    result = parse_dsml_tool_calls(text)

    assert result.error == ""
    args = json.loads(result.tool_calls[0]["function"]["arguments"])
    assert args == {"path": "a.txt", "content": "x"}


def test_parsed_block_is_removed_from_content():
    text = _fixture("tool_help_double_bar.txt")

    result = parse_dsml_tool_calls(text)

    assert "DSML" not in result.content
    assert result.content == ""


def test_normalize_preserves_prose_around_block():
    text = "先说一句。\n" + _fixture("tool_help_double_bar.txt")

    result = parse_dsml_tool_calls(text)

    assert result.error == ""
    assert "先说一句" in result.content
    assert "DSML" not in result.content
