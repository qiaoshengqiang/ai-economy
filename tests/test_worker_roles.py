"""Worker Role 注册表测试。"""

from work.worker_roles import (
    DEFAULT_ROLE, ROLES, render_role_prompt, resolve_role, role_catalog,
)


def test_role_catalog_lists_front_visible_fields():
    catalog = role_catalog()

    assert len(catalog) == len(ROLES)
    assert {item["id"] for item in catalog} == set(ROLES)
    for item in catalog:
        assert item["id"]
        assert item["name"]
        assert item["description"]


def test_unknown_role_is_not_silently_downgraded():
    assert resolve_role("not_a_real_role") is None


def test_empty_role_defaults_to_generalist():
    assert resolve_role("").role_id == DEFAULT_ROLE
    assert resolve_role(None).role_id == DEFAULT_ROLE
    assert resolve_role("   ").role_id == DEFAULT_ROLE


def test_role_prompt_includes_identity_and_summary_additions():
    prompt = render_role_prompt(ROLES["game_developer"])

    assert "role_id: game_developer" in prompt
    assert "game_developer" in prompt
    assert "角色额外总结项" in prompt
    assert "操作方式与玩法" in prompt
