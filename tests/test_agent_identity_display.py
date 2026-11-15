"""Agent 显示名 / Agent ID 的渲染契约。

🔒 名字是称呼，Agent ID 是技术标识：
    自我介绍必须是两条事实，不能合并成一句。
"""

from __future__ import annotations

from types import SimpleNamespace

from core.identity import (
    agent_display_name, format_agent_identity, format_agent_label,
)
from work.roles import resolve_agent_name


def test_identity_lines_keep_name_and_id_separate():
    text = format_agent_identity("小艾", "agent_a")
    assert text == "你的名字是：小艾\n你的 Agent ID 是：agent_a"
    assert "小艾（agent_a）" not in text


def test_identity_lines_support_cognitive_pronoun():
    text = format_agent_identity("小艾", "agent_a", pronoun="我的")
    assert text == "我的名字是：小艾\n我的 Agent ID 是：agent_a"


def test_identity_falls_back_to_agent_id():
    assert format_agent_identity("", "agent_a") == (
        "你的名字是：agent_a\n你的 Agent ID 是：agent_a")
    assert agent_display_name("", "agent_a") == "agent_a"
    assert agent_display_name("小艾", "agent_a") == "小艾"


def test_compact_label_marks_the_technical_id():
    assert format_agent_label("小艾", "agent_a") == (
        "小艾（Agent ID: agent_a）")
    assert format_agent_label("agent_a", "agent_a") == "agent_a"
    assert format_agent_label("", "agent_a") == "agent_a"


def test_resolve_agent_name_prefers_canonical_config():
    runtime = SimpleNamespace(
        resolved_config=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(name="小艾")),
        economy=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(name="旧名字")),
    )
    assert resolve_agent_name(runtime, "agent_a") == "小艾"


def test_resolve_agent_name_falls_back_to_economy_identity():
    runtime = SimpleNamespace(
        resolved_config=SimpleNamespace(
            agent=lambda _aid: (_ for _ in ()).throw(KeyError("missing"))),
        economy=SimpleNamespace(
            agent=lambda _aid: SimpleNamespace(name="小艾")),
    )
    assert resolve_agent_name(runtime, "agent_a") == "小艾"


def test_resolve_agent_name_returns_empty_when_unknown():
    runtime = SimpleNamespace(
        resolved_config=None,
        economy=SimpleNamespace(
            agent=lambda _aid: (_ for _ in ()).throw(KeyError("missing"))),
    )
    assert resolve_agent_name(runtime, "agent_a") == ""
