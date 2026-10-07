"""
Artifact Write Modes + Dynamic Artifact Creation（§十三 的 23 项矩阵）
================================================================

    CREATE / REPLACE / APPEND + 动态创建 + EMPTY_CONTENT 安全

【本文件锁定的不变式】

    · `write_mode` **必须显式给出** —— Runtime 不猜
    · CREATE 撞已有文件 → **拒绝**，绝不悄悄降级成 REPLACE
    · APPEND 后 `new_content.startswith(old_content)` —— 旧内容完整保留
    · 空内容三种模式一律拒绝，且**不产生 revision**
    · 动态创建走**既有** Lock / Permission 语义，不新增权限系统
"""

from __future__ import annotations

import pytest

from services.artifacts import (
    MODE_APPEND, MODE_CREATE, MODE_REPLACE, VALID_WRITE_MODES,
    provision_namespaces, validate_artifact_path, write_artifact,
)
from tests.workspace_harness import WorkspaceHarness

W = "WS-TEST-001"
OWNER = "novel_world_builder"
AUTHOR = "novel_author"
EDITOR = "novel_editor"
READER = "novel_reader"
AGENTS = [OWNER, "novel_plot_architect", AUTHOR, EDITOR, READER]
OUTSIDER = "outsider"


@pytest.fixture()
def rig():
    h = WorkspaceHarness.in_memory(
        participants={W: set(AGENTS)}, agents=set(AGENTS) | {OUTSIDER})
    gw = h.gateway()
    gw.ensure_workspace(W, OWNER)
    return h, gw


@pytest.fixture()
def gw(rig):
    return rig[1]


def _rev(gw, path, agent=OWNER):
    got = gw.read(W, "/" + path.lstrip("/"), agent)
    return (got.result or {}).get("revision") if got.success else None


def _content(gw, path, agent=OWNER):
    got = gw.read(W, "/" + path.lstrip("/"), agent)
    return (got.result or {}).get("content") if got.success else None


# ================================================================
# CREATE（1–5）
# ================================================================
class TestCreate:
    def test_01_create_new_artifact_succeeds(self, gw):
        r = write_artifact(gw, W, OWNER, "characters.md", "# 人物\n\n主角。",
                           write_mode=MODE_CREATE)
        assert r["ok"] is True and r["mode"] == MODE_CREATE
        assert _content(gw, "characters.md") == "# 人物\n\n主角。"

    def test_02_create_existing_is_rejected(self, gw):
        write_artifact(gw, W, OWNER, "a.md", "first", write_mode=MODE_CREATE)
        r = write_artifact(gw, W, OWNER, "a.md", "second", write_mode=MODE_CREATE)

        assert r["ok"] is False and r["code"] == "already_exists"
        # 🔴 关键：**没有**悄悄降级成 replace
        assert _content(gw, "a.md") == "first"
        assert _rev(gw, "a.md") == 1

    @pytest.mark.parametrize("bad", [
        "/etc/passwd", "../escape.md", "C:/host.md", "a\\b.md", "~/.ssh/id",
        "", "   ",
    ])
    def test_03_illegal_path_is_rejected(self, gw, bad):
        r = write_artifact(gw, W, OWNER, bad, "x", write_mode=MODE_CREATE)
        assert r["ok"] is False and r["code"] == "bad_path"

    def test_04_unauthorized_agent_is_rejected(self, gw):
        """🔒 有 AIC ≠ 有 Workspace 写权。非 Participant 一律拒绝。"""
        r = write_artifact(gw, W, OUTSIDER, "characters.md", "x",
                           write_mode=MODE_CREATE)
        assert r["ok"] is False
        assert r["code"] == "not_participant"

    def test_05_create_non_empty_bumps_revision(self, gw):
        before = _rev(gw, "/")
        r = write_artifact(gw, W, OWNER, "b.md", "hello", write_mode=MODE_CREATE)
        assert r["revision"] == 1
        # 父目录（根）也 +1 —— Workspace Core 的既有语义
        assert _rev(gw, "/") == before + 1


# ================================================================
# REPLACE（6–8）
# ================================================================
class TestReplace:
    def test_06_replace_existing_succeeds(self, gw):
        write_artifact(gw, W, OWNER, "a.md", "old", write_mode=MODE_CREATE)
        r = write_artifact(gw, W, OWNER, "a.md", "new", write_mode=MODE_REPLACE)
        assert r["ok"] is True and r["mode"] == MODE_REPLACE

    def test_07_content_is_replaced_not_merged(self, gw):
        write_artifact(gw, W, OWNER, "a.md", "old-content",
                       write_mode=MODE_CREATE)
        write_artifact(gw, W, OWNER, "a.md", "new", write_mode=MODE_REPLACE)
        assert _content(gw, "a.md") == "new"     # 旧内容**不在**里面

    def test_08_replace_bumps_revision(self, gw):
        write_artifact(gw, W, OWNER, "a.md", "old", write_mode=MODE_CREATE)
        r = write_artifact(gw, W, OWNER, "a.md", "new", write_mode=MODE_REPLACE)
        assert r["previous_revision"] == 1 and r["revision"] == 2

    def test_replace_missing_is_rejected(self, gw):
        r = write_artifact(gw, W, OWNER, "nope.md", "x",
                           write_mode=MODE_REPLACE)
        assert r["ok"] is False and r["code"] == "not_found"


# ================================================================
# APPEND（9–12）—— 本轮最关键的一组
# ================================================================
class TestAppend:
    def test_09_append_to_existing_succeeds(self, gw):
        write_artifact(gw, W, OWNER, "m.md", "第一章", write_mode=MODE_CREATE)
        r = write_artifact(gw, W, OWNER, "m.md", "第二章", write_mode=MODE_APPEND)
        assert r["ok"] is True and r["mode"] == MODE_APPEND

    def test_10_old_content_is_preserved(self, gw):
        """
        🔒 **本轮最重要的断言。**

        这直接对应用户要验证的东西：
            R1 content = X
            R2 content = X + Y     ← 而不是 R2 = Y
        """
        old = "第一章：他在账本里发现自己的童年被标记为已删除。"
        write_artifact(gw, W, OWNER, "m.md", old, write_mode=MODE_CREATE)
        write_artifact(gw, W, OWNER, "m.md", "第二章：他去找那个签名的人。",
                       write_mode=MODE_APPEND)

        after = _content(gw, "m.md")
        assert after.startswith(old), "旧内容被吃掉了 —— APPEND 没有生效"
        assert old in after

    def test_11_new_content_is_appended_at_the_end(self, gw):
        write_artifact(gw, W, OWNER, "m.md", "AAA", write_mode=MODE_CREATE)
        new = "BBB"
        write_artifact(gw, W, OWNER, "m.md", new, write_mode=MODE_APPEND)
        after = _content(gw, "m.md")
        assert after.endswith(new)
        assert after == "AAA\n\nBBB"

    def test_12_append_bumps_revision(self, gw):
        write_artifact(gw, W, OWNER, "m.md", "A", write_mode=MODE_CREATE)
        r = write_artifact(gw, W, OWNER, "m.md", "B", write_mode=MODE_APPEND)
        assert r["previous_revision"] == 1 and r["revision"] == 2
        assert r["previous_chars"] == 1 and r["appended_chars"] == 1
        assert r["chars"] == len("A\n\nB")

    def test_append_grows_monotonically(self, gw):
        """
        10,000 字目标的**结构前提**：连续 APPEND 必须单调增长。
        """
        write_artifact(gw, W, OWNER, "chapter.md", "x" * 100,
                       write_mode=MODE_CREATE)
        lengths = [100]
        for i in range(5):
            write_artifact(gw, W, OWNER, "chapter.md", "y" * 100,
                           write_mode=MODE_APPEND)
            lengths.append(len(_content(gw, "chapter.md")))

        assert lengths == sorted(lengths), f"没有单调增长：{lengths}"
        assert lengths[-1] > lengths[0]

    def test_append_to_missing_is_rejected(self, gw):
        r = write_artifact(gw, W, OWNER, "nope.md", "x", write_mode=MODE_APPEND)
        assert r["ok"] is False and r["code"] == "not_found"


# ================================================================
# EMPTY（13–17）
# ================================================================
class TestEmptyContent:
    @pytest.mark.parametrize("mode", VALID_WRITE_MODES)
    def test_13_14_15_empty_is_rejected_for_all_modes(self, gw, mode):
        if mode != MODE_CREATE:
            write_artifact(gw, W, OWNER, "e.md", "x", write_mode=MODE_CREATE)
        r = write_artifact(gw, W, OWNER, "e.md", "", write_mode=mode)
        assert r["ok"] is False and r["code"] == "empty_content"

    def test_16_allow_empty_only_when_explicit(self, gw):
        write_artifact(gw, W, OWNER, "e.md", "content", write_mode=MODE_CREATE)
        r = write_artifact(gw, W, OWNER, "e.md", "", write_mode=MODE_REPLACE,
                           allow_empty=True)
        assert r["ok"] is True
        assert _content(gw, "e.md") == ""

    def test_17_empty_reject_does_not_bump_revision(self, gw):
        write_artifact(gw, W, OWNER, "e.md", "keep me", write_mode=MODE_CREATE)
        before = _rev(gw, "e.md")

        for mode in (MODE_REPLACE, MODE_APPEND):
            r = write_artifact(gw, W, OWNER, "e.md", "   \n ", write_mode=mode)
            assert r["ok"] is False

        assert _rev(gw, "e.md") == before, "空写入涨了 revision"
        assert _content(gw, "e.md") == "keep me"

    def test_append_empty_never_creates_a_meaningless_revision(self, gw):
        """§十五 特别要求：APPEND + empty 不得产生无意义 revision。"""
        write_artifact(gw, W, OWNER, "m.md", "X", write_mode=MODE_CREATE)
        before = _rev(gw, "m.md")
        write_artifact(gw, W, OWNER, "m.md", "", write_mode=MODE_APPEND)
        assert _rev(gw, "m.md") == before


# ================================================================
# Dynamic Artifact Creation（18–23）
# ================================================================
class TestDynamicArtifactCreation:
    def test_18_agent_can_create_in_its_namespace(self, gw):
        prov = provision_namespaces(gw, W, OWNER)
        created = [p for p in prov if p["ok"]]
        assert len(created) == 5

        r = write_artifact(gw, W, AUTHOR, "manuscript/chapter_01.md",
                           "第一章正文", write_mode=MODE_CREATE)
        assert r["ok"] is True, r

    def test_19_new_artifact_shows_up_in_workspace(self, gw):
        provision_namespaces(gw, W, OWNER)
        write_artifact(gw, W, OWNER, "world/characters.md", "# 人物",
                       write_mode=MODE_CREATE)
        entries = gw.list(W, "/world", OWNER).result["entries"]
        # `list()` 给的是**目录项**（name + revision），不是完整路径
        names = {e.get("name"): e for e in entries}
        assert "characters.md" in names, f"新 Artifact 没出现在目录里：{names}"
        assert names["characters.md"]["revision"] == 1
        # 而且能用**完整逻辑路径**读回来
        got = gw.read(W, "/world/characters.md", OWNER)
        assert got.success and got.result["content"] == "# 人物"

    def test_20_new_artifact_has_revision(self, gw):
        provision_namespaces(gw, W, OWNER)
        r = write_artifact(gw, W, OWNER, "world/locations.md", "地点",
                           write_mode=MODE_CREATE)
        assert r["revision"] == 1
        assert _rev(gw, "world/locations.md") == 1

    def test_21_illegal_path_fails(self, gw):
        provision_namespaces(gw, W, OWNER)
        r = write_artifact(gw, W, OWNER, "world/../../etc/passwd", "x",
                           write_mode=MODE_CREATE)
        assert r["ok"] is False and r["code"] == "bad_path"

    def test_22_outside_workspace_path_fails(self, gw):
        for bad in ("/tmp/x.md", "/Users/x/secret.md", "D:/host.md"):
            r = write_artifact(gw, W, OWNER, bad, "x", write_mode=MODE_CREATE)
            assert r["ok"] is False and r["code"] == "bad_path", bad

    def test_23_unauthorized_agent_cannot_create(self, gw):
        provision_namespaces(gw, W, OWNER)
        r = write_artifact(gw, W, OUTSIDER, "world/x.md", "x",
                           write_mode=MODE_CREATE)
        assert r["ok"] is False

    def test_agent_cannot_create_in_another_agents_namespace(self, gw):
        """🔒 Lock 模型不回归：`/world/` 归 world_builder，author 建不了。"""
        provision_namespaces(gw, W, OWNER)
        r = write_artifact(gw, W, AUTHOR, "world/intruder.md", "x",
                           write_mode=MODE_CREATE)
        assert r["ok"] is False and r["code"] == "locked_by_other"

    def test_namespaces_are_idempotent(self, gw):
        a = provision_namespaces(gw, W, OWNER)
        b = provision_namespaces(gw, W, OWNER)
        assert all(p["status"] == "exists" for p in b)
        assert {p["revision"] for p in a} == {p["revision"] for p in b}


# ================================================================
# 结构守卫
# ================================================================
class TestWriteModeGuards:
    def test_mode_must_be_explicit(self, gw):
        """§三：没有默认推断 —— 非法 mode 直接拒绝。"""
        r = write_artifact(gw, W, OWNER, "x.md", "c", write_mode="smart")
        assert r["ok"] is False and r["code"] == "bad_mode"

    def test_no_heuristic_mode_inference(self):
        """
        🔒 结构守卫：写入层**不许**根据 path / revision / content 长度
           推断 write_mode。扫源码里有没有这种分支。
        """
        src = (__import__("pathlib").Path(__file__).resolve().parent.parent
               / "services" / "artifacts.py").read_text("utf-8")
        for forbidden in ("if len(content)", "if revision", "infer_mode",
                          "guess_mode", "auto_mode"):
            assert forbidden not in src, f"出现了 mode 推断：{forbidden!r}"

    def test_reverse_the_inference_guard_fires(self):
        evil = "def f(content, revision):\n    if len(content) > 100:\n        return 'append'\n"
        assert "if len(content)" in evil

    def test_service_is_not_a_pipeline(self):
        """§一：不许变成 WorkflowEngine / ArtifactPipeline。"""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "services" / "artifacts.py").read_text("utf-8")
        for forbidden in ("class ArtifactPipeline", "class ChapterManager",
                          "class ProductionManager", "class WorkflowEngine"):
            assert forbidden not in src

    def test_no_host_filesystem_access(self):
        """§八：Artifact 层**没有**任何主机文件系统调用。"""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "services" / "artifacts.py").read_text("utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                assert m not in ("os", "shutil", "pathlib", "subprocess"), (
                    f"services/artifacts.py 导入了 {m} —— 它不该碰宿主文件系统")


# ================================================================
# 🔴 真实 smoke 抓到的 bug：深路径 CREATE
# ================================================================
class TestDeepPathCreation:
    """
    实测：Agent 自主发明了 `novel/chapters/chapter-01-opening.md`，
    而 `novel/chapters/` 不存在 → `not_found`。

    §七 的示例只有一层，但 Agent **很自然会发明更深的结构**。
    只支持一层 = 动态创建事实上不可用。
    """

    def test_deep_path_creates_intermediate_directories(self, gw):
        provision_namespaces(gw, W, OWNER)
        r = write_artifact(gw, W, OWNER, "world/regions/north/cities.md",
                           "北方城市", write_mode=MODE_CREATE)
        assert r["ok"] is True, r
        assert _content(gw, "world/regions/north/cities.md") == "北方城市"

    def test_reverse_intermediate_dirs_respect_ownership(self, gw):
        """
        🔒 建中间目录**不绕过** Lock：
            author 不能在 world_builder 的命名空间下造目录。
        """
        provision_namespaces(gw, W, OWNER)
        r = write_artifact(gw, W, AUTHOR, "world/regions/cities.md", "x",
                           write_mode=MODE_CREATE)
        assert r["ok"] is False
        assert r["code"] == "locked_by_other"

    def test_repeat_deep_create_is_idempotent_on_parents(self, gw):
        provision_namespaces(gw, W, OWNER)
        a = write_artifact(gw, W, OWNER, "world/regions/a.md", "A",
                           write_mode=MODE_CREATE)
        b = write_artifact(gw, W, OWNER, "world/regions/b.md", "B",
                           write_mode=MODE_CREATE)
        assert a["ok"] and b["ok"]

    def test_missing_parent_is_detected_by_read_not_by_create(self):
        """
        🔒 回归守卫：`_ensure_parents` **不许**再用
           「create 返回 NAME_CONFLICT」来判断目录是否已存在
           —— 那个检查顺序会让「已存在」被误读成「无权限」。
        """
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "services" / "artifacts.py").read_text("utf-8")
        block = src[src.index("def _ensure_parents"):src.index("def _create")]
        assert "gateway.read(" in block, (
            "_ensure_parents 没有先用 read 探测存在性 —— 会重现那个 bug")
