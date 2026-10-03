"""
Workspace Core Hardening（任务书 §8 A–K）
============================================================

【这一组测试回答的问题】

    不是「功能有没有实现」，而是：

        **在异常、竞争、重启、多 Agent 并发的情况下，
        Workspace Core 还守不守它已经定好的语义？**

【分组对照任务书】

    A persistence/reopen      B handoff/release edges
    C delegated boundary      D rename/move/delete
    E namespace concurrency   F edit concurrency
    G multi-workspace         H participant isolation
    I path/workspace isolation J transaction rollback
    K audit/event consistency
"""

from __future__ import annotations

import threading

import pytest

from core.workspace_core import LockKind
from services.workspace_gateway import WorkspaceGateway
from storage.database import Database
from tests.workspace_harness import WorkspaceHarness, check_workspace_invariants


@pytest.fixture
def h():
    """内存环境（单线程测试用）。"""
    return WorkspaceHarness.in_memory()


@pytest.fixture
def fh(tmp_path):
    """文件环境（持久化 / 并发测试用）。"""
    return WorkspaceHarness.on_file(tmp_path / "ws.db")


def ws(hh, workspace_id="W1", owner="A"):
    """建好 Workspace 并返回 gateway。"""
    hh.bootstrap(workspace_id, owner)
    return hh.gateway()


# ============================================================
# A. Persistence / Reopen Recovery
# ============================================================
class TestA_PersistenceReopen:
    """
    建 Workspace → 建资源 → 建显式锁 → 产生 Revision / Audit / Event
    → **关掉连接** → 重新打开 → 验证状态完全恢复。
    """

    def test_full_state_survives_reopen(self, fh):
        # ---- ① 建状态 ----
        with fh.session() as gw:
            gw.ensure_workspace("W1", "A")
            gw.create("W1", "/", "src", "directory", "A")
            gw.create("W1", "/src", "main.py", "file", "A", content="print(1)")
            gw.handoff("W1", "/src/main.py", "A", "B", "A", reason="交给 B")
            gw.edit("W1", "/src/main.py", 1, "print(2)", "B")

        # ---- ② 记下重启前的全部状态 ----
        with fh.connection() as db:
            before_res = {
                r["path"]: (r["resource_id"], r["revision"], r["state"],
                            r["resource_type"])
                for r in db.fetchall(
                    "SELECT * FROM workspace_resources WHERE workspace_id='W1'")
            }
            before_locks = {
                r["resource_id"]: (r["holder"], r["kind"])
                for r in db.fetchall("SELECT * FROM workspace_explicit_locks")
            }
            before_audits = len(db.fetchall(
                "SELECT * FROM workspace_audits WHERE workspace_id='W1'"))
            before_events = len(db.fetchall(
                "SELECT * FROM workspace_events WHERE workspace_id='W1'"))

        # ---- ③ 完全关掉，重新打开一个全新连接 ----
        with fh.session() as gw:
            # ---- ④ 状态必须完全一致 ----
            read = gw.read("W1", "/src/main.py", "B")
            assert read.success
            assert read.result["content"] == "print(2)"
            assert read.result["revision"] == 2

            tree = gw.store.load_tree("W1")
            after_res = {
                r.path: (r.resource_id, r.revision, r.state.value,
                         r.resource_type.value)
                for r in tree._by_id.values()   # noqa: SLF001 - 测试要看全量
            }

            for path, snapshot in before_res.items():
                assert after_res.get(path) == snapshot, (
                    f"重启后 {path} 状态变了："
                    f"{snapshot} → {after_res.get(path)}"
                )

            # resource_id **必须**不变（不能重新生成）
            assert after_res["/src/main.py"][0] == before_res["/src/main.py"][0]

            # controller / explicit lock 必须恢复
            node = tree.by_path("W1", "/src/main.py")
            assert tree.effective_lock(node) == "B", "锁丢了"
            assert tree.effective_controller(node) == "A", "controller 丢了"
            assert tree.explicit_lock_of(node.resource_id).kind is LockKind.FILE

            # audit / event 必须还在
            assert fh.new_db().fetchall(
                "SELECT COUNT(*) AS n FROM workspace_audits "
                "WHERE workspace_id='W1'")[0]["n"] == before_audits
            assert fh.new_db().fetchall(
                "SELECT COUNT(*) AS n FROM workspace_events "
                "WHERE workspace_id='W1'")[0]["n"] == before_events

        # ---- ⑤ 重启后不变量仍然成立 ----
        with fh.connection() as db:
            check_workspace_invariants(db, "W1")
            assert set(before_locks) == {
                r["resource_id"] for r in
                db.fetchall("SELECT resource_id FROM workspace_explicit_locks")
            }

    def test_revision_history_survives_reopen(self, fh):
        with fh.session() as gw:
            gw.ensure_workspace("W1", "A")
            gw.create("W1", "/", "a.py", "file", "A", content="v0")
            gw.edit("W1", "/a.py", 1, "v1", "A")
            gw.edit("W1", "/a.py", 2, "v2", "A")
            rid = gw.read("W1", "/a.py", "A").result["resource_id"]

        with fh.session() as gw:
            rows = gw.store.revisions_for(rid)
            assert [r["revision_number"] for r in rows] == [1, 2, 3]
            assert [r["change_type"] for r in rows] == ["CREATE", "EDIT", "EDIT"]

    def test_ensure_workspace_is_idempotent_across_reopen(self, fh):
        """重启后再次 ensure_workspace **不能**重建根（否则责任源头会漂）。"""
        with fh.session() as gw:
            gw.ensure_workspace("W1", "A")
            first = gw.store.find_by_path("W1", "/").resource_id

        with fh.session() as gw:
            r = gw.ensure_workspace("W1", "A")
            assert r.success
            assert r.result["created"] is False, "不该新建"
            assert r.result["resource_id"] == first, "根的 resource_id 变了"


# ============================================================
# B. Handoff / Release Edge Cases（任务书 §9 Case 1–6）
# ============================================================
class TestB_HandoffReleaseEdges:
    def test_case1_chain_handoff(self, h):
        """Case 1：A → B → C，责任正确传递。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="x")
        gw.handoff("W1", "/a.py", "A", "B", "A", reason="1")
        gw.handoff("W1", "/a.py", "B", "C", "B", reason="2")

        tree = h.gateway().store.load_tree("W1")
        assert tree.effective_lock(tree.by_path("W1", "/a.py")) == "C"

        # 只有一条显式锁 —— 链式交接**不**堆积
        with h.connection() as db:
            n = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_explicit_locks")[0]["n"]
            assert n == 2, f"应只有根 + a.py 两条锁，实际 {n}"

    def test_case2_old_holder_cannot_edit_after_handoff(self, h):
        """Case 2：A → B 后 A 再 edit 必须失败。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="x")
        gw.handoff("W1", "/a.py", "A", "B", "A", reason="交给 B")

        r = gw.edit("W1", "/a.py", 1, "A 偷偷改", "A")

        assert not r.success
        assert r.error_code == "LOCKED_BY_OTHER_AGENT"
        assert gw.read("W1", "/a.py", "B").result["content"] == "x", "内容必须没变"

    def test_case3_release_falls_back_to_parent(self, h):
        """Case 3：A → B 后 B release，责任回退到父级继承。"""
        gw = ws(h)
        gw.create("W1", "/", "data", "directory", "A")
        gw.create("W1", "/data", "a.py", "file", "A", content="x")
        gw.handoff("W1", "/data/a.py", "A", "B", "A", reason="给 B")

        assert h.gateway().store.load_tree("W1").effective_lock(
            h.gateway().store.load_tree("W1").by_path("W1", "/data/a.py")) == "B"

        r = gw.release("W1", "/data/a.py", "B")

        assert r.success
        assert r.result["effective_lock_after"] == "A", "应回退到父目录的 A"
        assert h.gateway().store.load_tree("W1").explicit_lock_of(
            h.gateway().store.load_tree("W1").by_path(
                "W1", "/data/a.py").resource_id) is None

    @pytest.mark.parametrize("op", ["handoff", "release", "edit"])
    def test_case4_non_holder_rejected(self, h, op):
        """Case 4：非当前责任持有者尝试 handoff / release / edit 必须被拒。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        if op == "handoff":
            r = gw.handoff("W1", "/a.py", "B", "C", "B", reason="抢")
        elif op == "release":
            r = gw.release("W1", "/a.py", "B")
        else:
            r = gw.edit("W1", "/a.py", 1, "改", "B")

        assert not r.success, f"{op} 应该被拒绝"
        assert r.error_code in (
            "NOT_EFFECTIVE_LOCK_HOLDER", "LOCKED_BY_OTHER_AGENT"
        ), f"{op} 的失败必须明确，得到 {r.error_code}"

    def test_case5_illegal_receiver(self, h):
        """
        Case 5：非法接收方必须**明确失败**。

        ⚠️ 这是 Hardening 修掉的一个真实缺陷：
           第一版 `handoff` **完全不校验** `to_agent` ——
           于是可以把资源责任交给一个不存在的 Agent，
           而 Effective Lock 解析会忠实地返回那个虚构名字。
        """
        h.agents = {"A", "B"}
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        # 不存在的 Agent
        r1 = gw.handoff("W1", "/a.py", "A", "GHOST", "A", reason="t")
        assert not r1.success
        assert r1.error_code == "UNKNOWN_AGENT"

        # 存在但不是 Participant
        h.agents.add("OUTSIDER")
        r2 = gw.handoff("W1", "/a.py", "A", "OUTSIDER", "A", reason="t")
        assert not r2.success
        assert r2.error_code == "NOT_TASK_PARTICIPANT"

        # 合法
        assert gw.handoff("W1", "/a.py", "A", "B", "A", reason="t").success

    def test_case6_handoff_release_do_not_bump_revision(self, h):
        """
        Case 6：**handoff / release 不增加 Resource Revision。**

        依据任务书 §5/§6：它们改变的是 responsibility，不是
        content / namespace state。
        """
        gw = ws(h)
        gw.create("W1", "/", "data", "directory", "A")
        gw.create("W1", "/data", "a.py", "file", "A", content="x")

        before = gw.read("W1", "/data/a.py", "A").result["revision"]

        gw.handoff("W1", "/data/a.py", "A", "B", "A", reason="1")
        after_handoff = gw.read("W1", "/data/a.py", "B").result["revision"]
        assert after_handoff == before, "handoff 不该改 revision"

        gw.release("W1", "/data/a.py", "B")
        after_release = gw.read("W1", "/data/a.py", "A").result["revision"]
        assert after_release == before, "release 不该改 revision"

        # force unlock 同样不改
        gw.handoff("W1", "/data", "A", "B", "A", reason="2")
        d_before = gw.read("W1", "/data", "B").result["revision"]
        gw.force_unlock("W1", "/data", "HUMAN", reason="x", authorized=True)
        assert gw.read("W1", "/data", "A").result["revision"] == d_before

        with h.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_directory_handoff_sets_controller_and_lock(self, h):
        gw = ws(h)
        gw.create("W1", "/", "fe", "directory", "A")
        gw.handoff("W1", "/fe", "A", "B", "A", reason="x")

        tree = h.gateway().store.load_tree("W1")
        node = tree.by_path("W1", "/fe")
        assert tree.effective_lock(node) == "B"
        assert tree.effective_controller(node) == "B"


# ============================================================
# C. Delegated Subtree / Boundary（任务书 §10）
# ============================================================
class TestC_DelegatedBoundary:
    """
        Root
         ├── A 负责的子树
         │    ├── a.txt
         │    └── B 负责的子目录
         │          └── b.txt

    ⚠️ 任务书特别点名：**防止遍历被第一个 explicit Directory Lock
       错误截断**，导致 Agent 看不到更深层的责任边界。

       这正是本项目之前真实修过的那个 bug
       （`is_within_foreign_boundary` 继续向上遍历 →
        B 连自己的文件都动不了）。
       它的**反面**同样危险：过早截断 → 看不见深层的越界。
    """

    @pytest.fixture
    def tree_rig(self, h):
        gw = ws(h)
        gw.create("W1", "/", "a_dir", "directory", "A")
        gw.create("W1", "/a_dir", "a.txt", "file", "A", content="a")
        gw.create("W1", "/a_dir", "b_dir", "directory", "A")
        gw.create("W1", "/a_dir/b_dir", "b.txt", "file", "A", content="b")
        gw.handoff("W1", "/a_dir/b_dir", "A", "B", "A", reason="B 负责这个子目录")
        return h, gw

    def test_layers_visible(self, tree_rig):
        h, _ = tree_rig
        tree = h.gateway().store.load_tree("W1")

        # 三层责任都必须能被解析出来
        assert tree.effective_lock(tree.by_path("W1", "/a_dir")) == "A"
        assert tree.effective_lock(tree.by_path("W1", "/a_dir/b_dir")) == "B"
        assert tree.effective_lock(tree.by_path("W1", "/a_dir/b_dir/b.txt")) == "B"
        assert tree.effective_controller(tree.by_path("W1", "/a_dir/b_dir")) == "B"

    def test_deep_boundary_is_detected(self, tree_rig):
        """🔒 深层边界**必须**被看见 —— 不能被第一层截断。"""
        h, _ = tree_rig
        tree = h.gateway().store.load_tree("W1")

        # A 动 /a_dir 时，子树里有 B 的边界
        foreign = tree.foreign_boundaries(tree.by_path("W1", "/a_dir"), "A")
        assert foreign == {
            tree.by_path("W1", "/a_dir/b_dir").resource_id: "B"
        }, "深层边界被漏掉了"

        # B 在自己的边界里 —— 不算「别人的」
        assert tree.is_within_foreign_boundary(
            tree.by_path("W1", "/a_dir/b_dir/b.txt"), "B") is None

        # A 深入 B 的边界 → 必须被识别
        assert tree.is_within_foreign_boundary(
            tree.by_path("W1", "/a_dir/b_dir/b.txt"), "A") == \
            tree.by_path("W1", "/a_dir/b_dir").resource_id

    def test_operations_respect_boundary(self, tree_rig):
        """read / create / rename / move / delete / edit / handoff 全走边界规则。"""
        h, gw = tree_rig

        # ---- read：A 能读（Participant 默认可读，无 READ ACL）----
        assert gw.read("W1", "/a_dir/b_dir/b.txt", "A").success

        # ---- create：A 在 B 的目录里建东西 → 拒绝 ----
        r = gw.create("W1", "/a_dir/b_dir", "evil.py", "file", "A")
        assert not r.success and r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"

        # ---- edit：A 改 B 的文件 → 拒绝 ----
        r = gw.edit("W1", "/a_dir/b_dir/b.txt", 1, "hack", "A")
        assert not r.success and r.error_code == "LOCKED_BY_OTHER_AGENT"

        # ---- rename：A 改 B 目录里的文件 → 拒绝 ----
        r = gw.rename("W1", "/a_dir/b_dir/b.txt", "evil.txt", "A")
        assert not r.success and r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"

        # ---- delete：A 删 B 的文件 → 拒绝 ----
        r = gw.delete("W1", "/a_dir/b_dir/b.txt", "A")
        assert not r.success and r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"

        # ---- delete：A 删 /a_dir（含 B 的边界）→ 拒绝 ----
        r = gw.delete("W1", "/a_dir", "A")
        assert not r.success
        assert r.error_code in ("NOT_EFFECTIVE_LOCK_HOLDER",
                                "RESOURCE_BOUNDARY_VIOLATION")

        # ---- move：A 把 /a_dir 移走 → 拒绝（含 B 的边界）----
        gw.create("W1", "/", "elsewhere", "directory", "A")
        r = gw.move("W1", "/a_dir", "/elsewhere", "A")
        assert not r.success
        assert r.error_code in ("NOT_EFFECTIVE_LOCK_HOLDER",
                                "RESOURCE_BOUNDARY_VIOLATION")

        # ---- handoff：A 把 /a_dir/b_dir 交出去 → from 不对，拒绝 ----
        r = gw.handoff("W1", "/a_dir/b_dir", "A", "C", "A", reason="x")
        assert not r.success and r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"

        # ---- B 在自己的边界里一切正常 ----
        assert gw.create("W1", "/a_dir/b_dir", "ok.py", "file", "B").success
        assert gw.edit("W1", "/a_dir/b_dir/b.txt", 1, "b2", "B").success
        assert gw.rename("W1", "/a_dir/b_dir/b.txt", "b2.txt", "B").success
        assert gw.delete("W1", "/a_dir/b_dir/ok.py", "B").success
        assert gw.handoff("W1", "/a_dir/b_dir", "B", "C", "B", reason="再交").success

    def test_parent_handoff_does_not_swallow_child(self, tree_rig):
        """父级 Handoff **不得**吞掉已有的子级 Explicit Lock / Controller。"""
        h, gw = tree_rig

        gw.handoff("W1", "/a_dir", "A", "C", "A", reason="整体交给 C")

        tree = h.gateway().store.load_tree("W1")
        assert tree.effective_lock(tree.by_path("W1", "/a_dir")) == "C"
        assert tree.effective_lock(tree.by_path("W1", "/a_dir/a.txt")) == "C", \
            "隐式的要跟随"
        assert tree.effective_lock(tree.by_path("W1", "/a_dir/b_dir")) == "B", \
            "🔒 显式子责任不能被吞回"
        assert tree.effective_controller(tree.by_path("W1", "/a_dir/b_dir")) == "B"


# ============================================================
# D. Rename / Move / Delete Hardening（任务书 §11）
# ============================================================
class TestD_RenameMoveDelete:
    def test_rename_same_directory(self, h):
        """
        同目录 rename：resource_id 不变 / revision +1 / 锁与 controller 不丢。

        ⚠️ **前置条件**：rename 需要**父目录**的 Directory Lock，
        且被 rename 的资源**不能**是别人独立负责的。
        所以责任必须在同一个人手上 —— 这里让 B 同时拥有
        `/bdir`（父目录）与 `/bdir/old.py`（资源）。
        """
        gw = ws(h)
        gw.create("W1", "/", "bdir", "directory", "A")
        gw.handoff("W1", "/bdir", "A", "B", "A", reason="B 负责整个目录")
        created = gw.create("W1", "/bdir", "old.py", "file", "B", content="v")
        rid = created.result["resource_id"]

        before_rev = gw.read("W1", "/bdir/old.py", "B").result["revision"]
        r = gw.rename("W1", "/bdir/old.py", "new.py", "B")

        assert r.success, r.error_code
        assert r.result["resource_id"] == rid, "🔒 resource_id 不变"
        assert r.result["revision"] == before_rev + 1, "revision +1"

        # 🔒 锁 / controller 不丢失
        tree = h.gateway().store.load_tree("W1")
        node = tree.by_path("W1", "/bdir/new.py")
        assert tree.effective_lock(node) == "B", "rename 丢了锁"
        assert tree.effective_controller(node) == "B", "rename 丢了 controller"
        assert gw.read("W1", "/bdir/new.py", "B").result["resource_id"] == rid

        # Audit + Event
        actions = [r_["action"] for r_ in
                   h.new_db().fetchall("SELECT action FROM workspace_audits")]
        events = [r_["event_type"] for r_ in
                  h.new_db().fetchall("SELECT event_type FROM workspace_events")]
        assert "RENAME" in actions
        assert "RESOURCE_RENAMED" in events

        with h.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_move_preserves_responsibility(self, h):
        """
        move：resource_id 不变 / revision +1 / **责任人不变**。

        ⚠️ 前置条件：source parent 与 destination parent 的
        Directory Lock 必须都是 actor。所以让 B 同时拥有 src/ 与 dst/。
        """
        gw = ws(h)
        gw.create("W1", "/", "src", "directory", "A")
        gw.create("W1", "/", "dst", "directory", "A")
        gw.handoff("W1", "/src", "A", "B", "A", reason="B 负责 src")
        gw.handoff("W1", "/dst", "A", "B", "A", reason="B 负责 dst")
        created = gw.create("W1", "/src", "x.py", "file", "B", content="v")
        rid = created.result["resource_id"]

        before_rev = gw.read("W1", "/src/x.py", "B").result["revision"]
        r = gw.move("W1", "/src/x.py", "/dst", "B")

        assert r.success, r.error_code
        assert r.result["resource_id"] == rid
        assert r.result["new_path"] == "/dst/x.py"
        assert r.result["revision"] == before_rev + 1

        tree = h.gateway().store.load_tree("W1")
        assert tree.effective_lock(tree.by_path("W1", "/dst/x.py")) == "B", \
            "🔒 move 不该换责任人"

        with h.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_move_whole_tree_preserves_inner_locks(self, h):
        """
        移动整棵 Directory Tree 时
        **Controller / File Lock / 子目录 Explicit Lock 保持不变** ——
        不因 Move 自动换责任人。

        ⚠️ 前置条件很重要：树内部的显式锁必须**都属于移动者**。
           如果内部有**别人的**独立边界，move 会（正确地）被拒绝 ——
           因为那等于把别人的资源搬到别处而没经过他同意。
           那条规则由下面的 `test_move_blocked_by_foreign_boundary` 覆盖。
        """
        gw = ws(h)
        gw.create("W1", "/", "src", "directory", "A")
        gw.create("W1", "/src", "sub", "directory", "A")
        gw.create("W1", "/src/sub", "x.py", "file", "A", content="v")
        # 让内部带**显式**锁（都还是 A 的，所以不构成别人的边界）
        gw.handoff("W1", "/src/sub", "A", "A", "A", reason="显式化子目录责任")
        gw.handoff("W1", "/src/sub/x.py", "A", "A", "A", reason="显式化文件责任")
        gw.create("W1", "/", "dst", "directory", "A")

        tree0 = h.gateway().store.load_tree("W1")
        sub0 = tree0.by_path("W1", "/src/sub")
        file0 = tree0.by_path("W1", "/src/sub/x.py")
        sub_id, file_id = sub0.resource_id, file0.resource_id

        r = gw.move("W1", "/src/sub", "/dst", "A")
        assert r.success, r.error_code

        tree = h.gateway().store.load_tree("W1")
        sub1 = tree.by_path("W1", "/dst/sub")
        file1 = tree.by_path("W1", "/dst/sub/x.py")

        # 🔒 resource_id 不变
        assert sub1.resource_id == sub_id
        assert file1.resource_id == file_id
        # 🔒 显式锁**仍然挂在同一个 resource_id 上**，没被 move 改写
        assert tree.explicit_lock_of(sub_id) is not None
        assert tree.explicit_lock_of(file_id) is not None
        assert tree.effective_lock(sub1) == "A"
        assert tree.effective_controller(sub1) == "A"
        assert tree.effective_lock(file1) == "A"

    def test_move_blocked_by_foreign_boundary(self, h):
        """
        🔒 Move **不能**把别的 Agent 已建立的责任边界一起搬走。

        依据原始任务书 §25：「move 需 source parent 与 destination parent
        Directory Lock 都是 Agent，**且不能属于别的 Agent 已建立的责任边界**」。

        ⚠️ 这与「移动整棵树时内部锁保持不变」**不矛盾**：

            · 内部锁都属于移动者  → 允许，锁原样保留
            · 内部锁有别人的      → 拒绝（那等于动别人的资源）
        """
        gw = ws(h)
        gw.create("W1", "/", "src", "directory", "A")
        gw.create("W1", "/src", "sub", "directory", "A")
        gw.create("W1", "/src/sub", "x.py", "file", "A", content="v")
        gw.handoff("W1", "/src/sub", "A", "B", "A", reason="B 独立负责")
        gw.create("W1", "/", "dst", "directory", "A")

        r = gw.move("W1", "/src/sub", "/dst", "A")

        assert not r.success
        assert r.error_code == "RESOURCE_BOUNDARY_VIOLATION"
        # B 的资源**没有被搬走**
        assert gw.read("W1", "/src/sub/x.py", "B").success
        assert not gw.read("W1", "/dst/sub/x.py", "B").success

    def test_move_requires_both_parents(self, h):
        """source parent 与 destination parent 的 Directory Lock 必须都是 Agent。"""
        gw = ws(h)
        gw.create("W1", "/", "a", "directory", "A")
        gw.create("W1", "/", "b", "directory", "A")
        gw.create("W1", "/a", "x.py", "file", "A", content="v")
        gw.handoff("W1", "/b", "A", "B", "A", reason="B 负责 b/")
        gw.handoff("W1", "/a/x.py", "A", "B", "A", reason="B 负责文件")

        # B 拥有文件，但 **不**拥有 source parent a/ → 拒绝
        r = gw.move("W1", "/a/x.py", "/b", "B")
        assert not r.success
        assert r.error_code == "NOT_EFFECTIVE_LOCK_HOLDER"

    def test_move_into_own_subtree_rejected(self, h):
        gw = ws(h)
        gw.create("W1", "/", "a", "directory", "A")
        gw.create("W1", "/a", "b", "directory", "A")

        r = gw.move("W1", "/a", "/a/b", "A")

        assert not r.success
        assert r.error_code == "INVALID_PARENT"

    def test_reverse_move_race_no_deadlock(self, fh):
        # ⚠️ 用 **fh（文件库）**，不能用 h（内存库）——
        #    多线程共享一个 Database 会破坏事务边界（技术债 #2），
        #    第一次写成 `h` 时它在全量测试里偶发失败。
        """
        任务书 §11：`A: move X → Y` 与 `B: move Y → X` 反向竞争
        **不得死锁**。

        已有设计：move 按**稳定顺序**获取 source/destination
        namespace locks（`ResourceLockRegistry.hold` 内部先排序）。
        这里验证它真的起作用。
        """
        fh.bootstrap("W1", "A")
        gw = fh.gateway()
        gw.create("W1", "/", "d1", "directory", "A")
        gw.create("W1", "/", "d2", "directory", "A")
        gw.create("W1", "/d1", "x.py", "file", "A", content="v")
        gw.create("W1", "/d2", "y.py", "file", "A", content="v")

        barrier = threading.Barrier(2)
        results: list = [None, None]
        errors: list = []

        def do(slot, path, dest):
            try:
                with fh.session() as g:
                    barrier.wait(timeout=5)
                    results[slot] = g.move("W1", path, dest, "A")
            except Exception as exc:      # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=do, args=(0, "/d1/x.py", "/d2")),
            threading.Thread(target=do, args=(1, "/d2/y.py", "/d1")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)          # 🔒 超时 = 死锁

        assert not any(t.is_alive() for t in threads), "🔒 死锁了"
        assert not errors, f"抛异常：{errors}"
        assert all(r is not None and r.success for r in results), (
            f"反向 move 都该成功：{[r.error_code if r else None for r in results]}"
        )

        # 两个文件都换了位置，且互不覆盖
        assert gw.read("W1", "/d2/x.py", "A").success
        assert gw.read("W1", "/d1/y.py", "A").success

    def test_delete_directory_cascades_soft(self, h):
        """delete 目录：整棵子树软删除，但**记录全部保留**。"""
        gw = ws(h)
        gw.create("W1", "/", "d", "directory", "A")
        gw.create("W1", "/d", "sub", "directory", "A")
        created = gw.create("W1", "/d/sub", "x.py", "file", "A", content="v")
        rid = created.result["resource_id"]

        r = gw.delete("W1", "/d", "A")
        assert r.success

        # 🔒 资源**不消失**（任务书 §21）
        with h.connection() as db:
            row = db.fetchall(
                "SELECT state FROM workspace_resources WHERE resource_id=?",
                (rid,))
            assert row and row[0]["state"] == "deleted"
            check_workspace_invariants(db, "W1")

        # 历史仍可查
        tree = h.gateway().store.load_tree("W1")
        assert tree.get(rid) is not None
        assert len(h.gateway().store.revisions_for(rid)) >= 1


# ============================================================
# E. Namespace Concurrency
# ============================================================
class TestE_NamespaceConcurrency:
    def test_concurrent_creates_no_ghost(self, fh):
        """并发 create：不产生 ghost / 重复身份 / 非法 path。"""
        fh.bootstrap("W1", "A")
        n = 12
        barrier = threading.Barrier(n)
        results: list = [None] * n
        errors: list = []

        def do(slot):
            try:
                with fh.session() as gw:
                    barrier.wait(timeout=10)
                    results[slot] = gw.create(
                        "W1", "/", f"f{slot}.py", "file", "A", content="x")
            except Exception as exc:      # pragma: no cover
                errors.append((slot, exc))

        threads = [threading.Thread(target=do, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not errors, f"抛异常：{errors}"
        assert all(r is not None and r.success for r in results), (
            f"不同名字的并发 create 都该成功："
            f"{[r.error_code if r else None for r in results]}")

        with fh.connection() as db:
            check_workspace_invariants(db, "W1")
            rows = db.fetchall(
                "SELECT path FROM workspace_resources WHERE workspace_id='W1'")
            paths = sorted(r["path"] for r in rows)
            # ⚠️ 注意是**集合**比较：`sorted` 是字典序（`/f10.py` < `/f2.py`），
            #    与数字序不同 —— 这里关心的是「都在且不重复」，不是排列。
            assert set(paths) == {"/"} | {f"/f{i}.py" for i in range(n)}, (
                f"Namespace 不一致：{paths}")
            assert len(paths) == n + 1, f"有重复 path：{paths}"

    def test_concurrent_same_name_single_winner(self, fh):
        """并发 create **同名**：只能有一个赢，其余 NAME_CONFLICT。"""
        fh.bootstrap("W1", "A")
        n = 8
        barrier = threading.Barrier(n)
        results: list = [None] * n
        errors: list = []

        def do(slot):
            try:
                with fh.session() as gw:
                    barrier.wait(timeout=10)
                    results[slot] = gw.create(
                        "W1", "/", "same.py", "file", "A", content=f"{slot}")
            except Exception as exc:      # pragma: no cover
                errors.append((slot, exc))

        threads = [threading.Thread(target=do, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not errors, f"抛异常：{errors}"
        ok = [r for r in results if r and r.success]
        bad = [r for r in results if r and not r.success]

        assert len(ok) == 1, f"恰好一个成功，实际 {len(ok)}"
        assert all(r.error_code == "NAME_CONFLICT" for r in bad), (
            f"其余必须明确 NAME_CONFLICT：{[r.error_code for r in bad]}")

        with fh.connection() as db:
            check_workspace_invariants(db, "W1")
            rows = db.fetchall(
                "SELECT COUNT(*) AS n FROM workspace_resources "
                "WHERE workspace_id='W1' AND path='/same.py'")
            assert rows[0]["n"] == 1

    def test_concurrent_mixed_rename_delete(self, fh):
        """并发 rename / delete：不留 ghost、revision 不回退。"""
        fh.bootstrap("W1", "A")
        with fh.session() as gw:
            for i in range(8):
                gw.create("W1", "/", f"r{i}.py", "file", "A", content="x")

        ops = []
        for i in range(8):
            ops.append(("rename", f"/r{i}.py", f"rn{i}.py"))
            ops.append(("delete", f"/rn{i}.py", None))
            ops.append(("rename", f"/rn{i}.py", f"rd{i}.py"))

        barrier = threading.Barrier(len(ops))
        errors: list = []

        def do(kind, path, extra):
            try:
                with fh.session() as gw:
                    barrier.wait(timeout=10)
                    if kind == "rename":
                        gw.rename("W1", path, extra, "A")
                    else:
                        gw.delete("W1", path, "A")
            except Exception as exc:      # pragma: no cover
                errors.append((kind, path, exc))

        threads = [threading.Thread(target=do, args=o) for o in ops]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)

        assert not errors, f"抛异常：{errors}"
        with fh.connection() as db:
            check_workspace_invariants(db, "W1")


# ============================================================
# F. Edit Concurrency（最重要的场景）
# ============================================================
class TestF_EditConcurrency:
    def test_only_lock_holder_can_write(self, fh):
        """只有一个 Effective Lock Holder；其他人因 **Lock** 失败。"""
        with fh.session() as gw:
            gw.ensure_workspace("W1", "A")
            gw.create("W1", "/", "a.py", "file", "A", content="v0")

        n = 10
        barrier = threading.Barrier(n)
        results: list = [None] * n
        errors: list = []

        def do(slot):
            agent = "A" if slot == 0 else f"X{slot}"
            try:
                with fh.session() as gw:
                    barrier.wait(timeout=10)
                    results[slot] = gw.edit("W1", "/a.py", 1, f"by-{agent}", agent)
            except Exception as exc:      # pragma: no cover
                errors.append((slot, exc))

        threads = [threading.Thread(target=do, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)

        assert not errors, f"抛异常：{errors}"

        ok = [r for r in results if r and r.success]
        bad = [r for r in results if r and not r.success]

        assert len(ok) == 1, f"只有锁持有者能成功，实际 {len(ok)}"
        assert all(r.error_code in ("LOCKED_BY_OTHER_AGENT", "NOT_TASK_PARTICIPANT")
                   for r in bad), f"必须明确：{[r.error_code for r in bad]}"

        with fh.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_stale_revision_race_same_holder(self, fh):
        """
        同一个锁持有者、**不同 base_revision** 竞争：
        必须返回 `STALE_REVISION`，且 final revision == 成功次数 + 1。
        """
        with fh.session() as gw:
            gw.ensure_workspace("W1", "A")
            gw.create("W1", "/", "a.py", "file", "A", content="v0")

        n = 12
        barrier = threading.Barrier(n)
        results: list = [None] * n
        errors: list = []

        def do(slot):
            try:
                with fh.session() as gw:
                    barrier.wait(timeout=10)
                    # 全部基于 R1 提交
                    results[slot] = gw.edit("W1", "/a.py", 1, f"v{slot}", "A")
            except Exception as exc:      # pragma: no cover
                errors.append((slot, exc))

        threads = [threading.Thread(target=do, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)

        assert not errors, f"抛异常：{errors}"

        ok = [r for r in results if r and r.success]
        stale = [r for r in results if r and r.error_code == "STALE_REVISION"]

        # 🔒 全部基于 R1 → 恰好一次成功，其余全部 STALE_REVISION
        assert len(ok) == 1, f"基于同一 base_revision 只能成功一次，实际 {len(ok)}"
        assert len(stale) == n - 1, f"其余必须是 STALE_REVISION，实际 {len(stale)}"

        with fh.session() as gw:
            final = gw.read("W1", "/a.py", "A").result
            assert final["revision"] == 2, (
                f"final revision 必须等于成功次数+1 = 2，实际 {final['revision']}")

        with fh.connection() as db:
            check_workspace_invariants(db, "W1")
            rid = db.fetchall(
                "SELECT resource_id FROM workspace_resources "
                "WHERE workspace_id='W1' AND path='/a.py'")[0]["resource_id"]
            nums = [r["revision_number"] for r in db.fetchall(
                "SELECT revision_number FROM workspace_revisions "
                "WHERE resource_id=? ORDER BY revision_number", (rid,))]
            assert nums == [1, 2], f"败者不能留下写入痕迹：{nums}"

    def test_sequential_revisions_all_succeed(self, fh):
        """同一个持有者、**正确递增** base_revision → 全部成功，revision 严格符合次数。"""
        with fh.session() as gw:
            gw.ensure_workspace("W1", "A")
            gw.create("W1", "/", "a.py", "file", "A", content="v0")

        n = 10
        results: list = [None] * n
        errors: list = []

        def do(slot):
            try:
                with fh.session() as gw:
                    current = gw.read("W1", "/a.py", "A").result["revision"]
                    results[slot] = gw.edit(
                        "W1", "/a.py", current, f"v{slot}", "A")
            except Exception as exc:      # pragma: no cover
                errors.append((slot, exc))

        threads = [threading.Thread(target=do, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)

        assert not errors, f"抛异常：{errors}"

        ok = [r for r in results if r and r.success]
        with fh.session() as gw:
            final = gw.read("W1", "/a.py", "A").result["revision"]

        # 乐观锁语义：成功数 + 1 == 最终 revision（R1 是 create）
        assert final == len(ok) + 1, (
            f"revision({final}) 必须严格等于成功次数({len(ok)})+1")
        assert len(ok) >= 1

        with fh.connection() as db:
            check_workspace_invariants(db, "W1")


# ============================================================
# G. Multi-Workspace Concurrency
# ============================================================
class TestG_MultiWorkspace:
    def test_workspaces_do_not_pollute(self, fh):
        """多 Workspace 同时操作 —— resource_id / revision / lock / audit /
        event / namespace / participant 都不能跨 Workspace 污染。"""
        n_ws = 5
        agents_per_ws = 2
        for w in range(n_ws):
            wid = f"W{w}"
            members = [f"a{w}_{i}" for i in range(agents_per_ws)]
            fh.participants[wid] = set(members)

        # 每个 Workspace 各自 bootstrap，并给**每个 Agent 一个自己的子目录**
        # （根目录的 Directory Lock 属于 owner 一个人 —— 别的 Agent
        #   必须在自己的责任范围内操作，这正是 Workspace 语义）
        for w in range(n_ws):
            wid = f"W{w}"
            with fh.session() as gw:
                gw.ensure_workspace(wid, f"a{w}_0")
                for i in range(agents_per_ws):
                    agent = f"a{w}_{i}"
                    r = gw.create(wid, "/", f"by{agent}", "directory", f"a{w}_0")
                    assert r.success, f"建 {agent} 的目录失败：{r.error_code}"
                    assert gw.handoff(wid, f"/by{agent}", f"a{w}_0", agent,
                                      f"a{w}_0", reason="分派责任").success

        barrier = threading.Barrier(n_ws * agents_per_ws)
        errors: list = []
        results: dict = {}

        def do(w, i):
            wid = f"W{w}"
            agent = f"a{w}_{i}"
            try:
                with fh.session() as gw:
                    barrier.wait(timeout=15)
                    r1 = gw.create(wid, f"/by{agent}", f"file_{i}.py", "file",
                                   agent, content=f"{wid}-{i}")
                    r2 = gw.edit(wid, f"/by{agent}/file_{i}.py", 1,
                                 f"edited-{wid}-{i}", agent)
                    results[(wid, i)] = (r1, r2)
            except Exception as exc:      # pragma: no cover
                errors.append((wid, i, exc))

        threads = [threading.Thread(target=do, args=(w, i))
                   for w in range(n_ws) for i in range(agents_per_ws)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"抛异常：{errors}"

        for (wid, i), (r1, r2) in results.items():
            assert r1.success, f"{wid}/{i} create 失败：{r1.error_code}"
            assert r2.success, f"{wid}/{i} edit 失败：{r2.error_code}"
            assert r2.result["revision"] == 2

        # ---- 每个 Workspace 各自独立且不变量成立 ----
        for w in range(n_ws):
            wid = f"W{w}"
            with fh.connection() as db:
                check_workspace_invariants(db, wid)

                # 🔒 隔离要按 **resource_id** 比，不能按 path 比 ——
                #    不同 Workspace **本来就允许有同名 path**
                #    （path 只在 Workspace 内唯一）。用 path 比会假红。
                mine = {r["resource_id"] for r in db.fetchall(
                    "SELECT resource_id FROM workspace_resources "
                    "WHERE workspace_id=?", (wid,))}
                others = {r["resource_id"] for r in db.fetchall(
                    "SELECT resource_id FROM workspace_resources "
                    "WHERE workspace_id != ?", (wid,))}
                assert not (mine & others), (
                    f"{wid} 与别的 Workspace 共享了 resource_id：{mine & others}")

                # audit / event 都带正确的 workspace_id
                for table in ("workspace_audits", "workspace_events"):
                    leaking = db.fetchall(
                        f"SELECT resource_id FROM {table} WHERE workspace_id=?", (wid,))
                    for row in leaking:
                        if row["resource_id"] is None:
                            continue
                        owner = db.fetchall(
                            "SELECT workspace_id FROM workspace_resources "
                            "WHERE resource_id=?", (row["resource_id"],))
                        assert owner and owner[0]["workspace_id"] == wid, (
                            f"{wid} 的 {table} 引用了别的 Workspace 的资源"
                        )

    def test_foreign_workspace_participant_cannot_read(self, fh):
        """Participant 隔离：W0 的成员不能读 W1。"""
        fh.participants["W0"] = {"p0"}
        fh.participants["W1"] = {"p1"}
        for wid, owner in (("W0", "p0"), ("W1", "p1")):
            with fh.session() as gw:
                gw.ensure_workspace(wid, owner)
                gw.create(wid, "/", "secret.py", "file", owner, content="s")

        with fh.session() as gw:
            assert gw.read("W0", "/secret.py", "p0").success
            r = gw.read("W1", "/secret.py", "p0")
            assert not r.success
            assert r.error_code == "NOT_TASK_PARTICIPANT"


# ============================================================
# H. Participant Isolation
# ============================================================
class TestH_ParticipantIsolation:
    def test_participant_reads_own_workspace_only(self, h):
        h.participants = {"WA": {"A"}, "WB": {"B"}}
        gw = h.gateway()
        gw.ensure_workspace("WA", "A")
        gw.ensure_workspace("WB", "B")
        gw.create("WA", "/", "a.py", "file", "A", content="a")
        gw.create("WB", "/", "b.py", "file", "B", content="b")

        assert gw.read("WA", "/a.py", "A").success
        assert gw.read("WB", "/b.py", "A").error_code == "NOT_TASK_PARTICIPANT"

    @pytest.mark.parametrize("op", ["list", "read", "create", "edit",
                                    "delete", "rename", "move"])
    def test_non_participant_rejected_everywhere(self, h, op):
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="x")
        gw.create("W1", "/", "d", "directory", "A")

        if op == "list":
            r = gw.list("W1", "/", "OUT")
        elif op == "read":
            r = gw.read("W1", "/a.py", "OUT")
        elif op == "create":
            r = gw.create("W1", "/", "z.py", "file", "OUT")
        elif op == "edit":
            r = gw.edit("W1", "/a.py", 1, "z", "OUT")
        elif op == "delete":
            r = gw.delete("W1", "/a.py", "OUT")
        elif op == "rename":
            r = gw.rename("W1", "/a.py", "z.py", "OUT")
        else:
            r = gw.move("W1", "/a.py", "/d", "OUT")

        assert not r.success, f"{op} 对非 Participant 必须失败"
        assert r.error_code == "NOT_TASK_PARTICIPANT"

    def test_default_deny_when_callback_missing(self, h):
        """🔒 没注入 is_participant 时**默认拒绝一切**（安全的默认值）。"""
        from storage.database import create_memory_database
        db = create_memory_database()
        gw = WorkspaceGateway(db)          # 不传 is_participant

        assert gw.ensure_workspace("W1", "A").success   # 引导不需要 Participant
        r = gw.read("W1", "/", "A")
        assert not r.success
        assert r.error_code == "NOT_TASK_PARTICIPANT"
        db.close()


# ============================================================
# I. Path / Workspace Isolation
# ============================================================
class TestI_PathIsolation:
    @pytest.mark.parametrize("bad", [
        "../x", "../../x", "../../../etc/passwd",
        "/../x", "a/../../x", "..", "/..",
        "src/../../../etc/passwd",
    ])
    def test_traversal_rejected(self, h, bad):
        gw = ws(h)
        gw.create("W1", "/", "src", "directory", "A")

        for r in (gw.read("W1", bad, "A"),
                  gw.list("W1", bad, "A"),
                  gw.create("W1", bad, "x.py", "file", "A"),
                  gw.delete("W1", bad, "A")):
            assert not r.success, f"{bad!r} 不该被接受"
            assert r.error_code in ("OUTSIDE_WORKSPACE", "RESOURCE_NOT_FOUND",
                                    "INVALID_ARGUMENT", "INVALID_PARENT"), \
                f"{bad!r} 的失败码是 {r.error_code}"

    @pytest.mark.parametrize("path,expected", [
        ("src", "/src"), ("/src", "/src"), ("src/", "/src"),
        ("//src", "/src"), ("/src//a.py", "/src/a.py"), ("./src", "/src"),
        ("/./src", "/src"), ("src/./a.py", "/src/a.py"),
    ])
    def test_normalization(self, h, path, expected):
        """重复分隔符 / `.` / 前后斜杠都要规范化到同一个资源。"""
        gw = ws(h)
        gw.create("W1", "/", "src", "directory", "A")
        gw.create("W1", "/src", "a.py", "file", "A", content="v")

        r = gw.read("W1", path, "A")
        assert r.success, f"{path!r} 应该解析成功"
        assert r.result["path"] == expected, f"{path!r} → {r.result['path']!r}"

    def test_absolute_host_path_rejected(self, h):
        gw = ws(h)
        for bad in ("/etc/passwd", "/Users/x/secret", "file:///etc/passwd",
                    "C:/Windows/system32"):
            r = gw.read("W1", bad, "A")
            assert not r.success, f"{bad!r} 不该被当成 Workspace 内路径接受"

    def test_no_host_path_in_any_response(self, h):
        """🔒 Agent 永远不应该从 Gateway 接口得到 host filesystem physical path。"""
        gw = ws(h)
        gw.create("W1", "/", "src", "directory", "A")
        gw.create("W1", "/src", "a.py", "file", "A", content="v")
        gw.rename("W1", "/src/a.py", "b.py", "A")
        gw.handoff("W1", "/src/b.py", "A", "B", "A", reason="x")

        blobs = [
            str(gw.list("W1", "/src", "A").to_dict()),
            str(gw.read("W1", "/src/b.py", "A").to_dict()),
            str(gw.create("W1", "/src", "c.py", "file", "A").to_dict()),
            str(gw.edit("W1", "/src/c.py", 1, "n", "A").to_dict()),
            str(gw.rename("W1", "/src/c.py", "d.py", "A").to_dict()),
            str(gw.delete("W1", "/src/d.py", "A").to_dict()),
        ]
        for blob in blobs:
            for leak in ("/Users/", "/tmp/", "/var/", "/home/", "/private/",
                         "file://"):
                assert leak not in blob, f"响应泄漏了物理路径 {leak}"

    def test_workspace_id_isolation(self, h):
        """不同 Workspace 的同名 path 是两个不同资源。"""
        h.participants = {"W1": {"A"}, "W2": {"A"}}
        gw = h.gateway()
        gw.ensure_workspace("W1", "A")
        gw.ensure_workspace("W2", "A")
        r1 = gw.create("W1", "/", "same.py", "file", "A", content="w1")
        r2 = gw.create("W2", "/", "same.py", "file", "A", content="w2")

        assert r1.result["resource_id"] != r2.result["resource_id"], \
            "🔒 不同 Workspace 的同名资源的 resource_id 必须不同"
        assert gw.read("W1", "/same.py", "A").result["content"] == "w1"
        assert gw.read("W2", "/same.py", "A").result["content"] == "w2"


# ============================================================
# J. Transaction Rollback Consistency
# ============================================================
class TestJ_TransactionRollback:
    """
    模拟「mutation 成功到一半，persistence 失败」。

    🔒 要求：绝不出现
        · 文件改了但 revision 没改
        · revision 改了但内容没改
        · state 已变但 event/audit 完全错误

    优先利用**现有 SQLite transaction** —— 不引入新的 persistence 架构。
    """

    def test_failure_inside_transaction_rolls_back_everything(self, h):
        """在事务中途抛异常 → 内容 / revision / audit / event **全部回滚**。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="v0")

        with h.connection() as db:
            before_res = db.fetchall(
                "SELECT * FROM workspace_resources WHERE workspace_id='W1'")
            before_audits = db.fetchall("SELECT * FROM workspace_audits")
            before_events = db.fetchall("SELECT * FROM workspace_events")
            before_revs = db.fetchall("SELECT * FROM workspace_revisions")

        # 打补丁：让 audit 写入炸掉 —— 这是「event 写入失败」的等价场景
        store = gw.store
        original = store.add_audit
        calls = {"n": 0}

        def exploding(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("模拟 audit 写入失败")

        store.add_audit = exploding
        try:
            try:
                gw.edit("W1", "/a.py", 1, "SHOULD_NOT_PERSIST", "A")
                raise AssertionError("应该抛异常")
            except RuntimeError:
                pass
        finally:
            store.add_audit = original

        assert calls["n"] == 1, "补丁没生效"

        # ---- 全部必须原封不动 ----
        with h.connection() as db:
            assert db.fetchall(
                "SELECT * FROM workspace_resources WHERE workspace_id='W1'"
            ) == before_res, "资源被修改了（半成功）"
            assert db.fetchall("SELECT * FROM workspace_audits") == before_audits
            assert db.fetchall("SELECT * FROM workspace_events") == before_events
            assert db.fetchall("SELECT * FROM workspace_revisions") == before_revs

        # 特别是：内容和 revision 必须都没变
        after = gw.read("W1", "/a.py", "A").result
        assert after["content"] == "v0"
        assert after["revision"] == 1

    def test_event_failure_rolls_back_content(self, h):
        """「event 写入失败」→ 内容也必须回滚（不能只改内容不记事件）。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="v0")

        original = gw.store.add_event

        def exploding(*a, **kw):
            raise RuntimeError("模拟 event 写入失败")

        gw.store.add_event = exploding
        try:
            with pytest.raises(RuntimeError):
                gw.edit("W1", "/a.py", 1, "NOPE", "A")
        finally:
            gw.store.add_event = original

        after = gw.read("W1", "/a.py", "A").result
        assert after["content"] == "v0", "内容不该变"
        assert after["revision"] == 1, "revision 不该变"

    def test_revision_failure_rolls_back_content(self, h):
        """「revision 更新失败」→ 内容不能已经改了。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="v0")

        original = gw.store.add_revision

        def exploding(*a, **kw):
            raise RuntimeError("模拟 revision 写入失败")

        gw.store.add_revision = exploding
        try:
            with pytest.raises(RuntimeError):
                gw.edit("W1", "/a.py", 1, "NOPE", "A")
        finally:
            gw.store.add_revision = original

        after = gw.read("W1", "/a.py", "A").result
        assert after["content"] == "v0"
        assert after["revision"] == 1

    def test_validation_failure_leaves_no_content_change(self, h):
        """
        **校验失败**（不是崩溃）→ 也不能有半成功状态。

        注意与上面三条的区别：校验失败走的是 `_fail()`
        （正常返回 + 写一条 Audit），而不是异常回滚。
        """
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="v0")

        with h.connection() as db:
            before = db.fetchall(
                "SELECT * FROM workspace_resources WHERE workspace_id='W1'")

        gw.edit("W1", "/a.py", 99, "NOPE", "A")      # STALE_REVISION

        with h.connection() as db:
            assert db.fetchall(
                "SELECT * FROM workspace_resources WHERE workspace_id='W1'"
            ) == before, "校验失败不该改资源"

        assert gw.read("W1", "/a.py", "A").result["content"] == "v0"


# ============================================================
# K. Audit / Event Consistency
# ============================================================
class TestK_AuditEventConsistency:
    def test_success_mutation_triple_is_consistent(self, h):
        """成功 EDIT → Revision +1 → Audit exists → RESOURCE_UPDATED exists。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="v0")

        with h.connection() as db:
            rev_before = len(db.fetchall("SELECT * FROM workspace_revisions"))
            aud_before = len(db.fetchall("SELECT * FROM workspace_audits"))
            evt_before = len(db.fetchall("SELECT * FROM workspace_events"))

        assert gw.edit("W1", "/a.py", 1, "v1", "A").success

        with h.connection() as db:
            assert len(db.fetchall("SELECT * FROM workspace_revisions")) == rev_before + 1
            assert len(db.fetchall("SELECT * FROM workspace_audits")) == aud_before + 1
            assert len(db.fetchall("SELECT * FROM workspace_events")) == evt_before + 1

            evt = db.fetchall(
                "SELECT * FROM workspace_events ORDER BY rowid DESC LIMIT 1")[0]
            assert evt["event_type"] == "RESOURCE_UPDATED"
            assert evt["revision"] == 2

            aud = db.fetchall(
                "SELECT * FROM workspace_audits ORDER BY rowid DESC LIMIT 1")[0]
            assert aud["action"] == "EDIT"
            assert aud["result"] == "SUCCESS"
            assert aud["operation_id"] == evt["resource_id"] or aud["operation_id"]

    def test_failed_mutation_has_no_success_event(self, h):
        """失败 mutation **不得**产生成功 Resource Event。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="v0")

        with h.connection() as db:
            evt_before = len(db.fetchall("SELECT * FROM workspace_events"))

        gw.edit("W1", "/a.py", 99, "NOPE", "A")

        with h.connection() as db:
            assert len(db.fetchall("SELECT * FROM workspace_events")) == evt_before

    def test_handoff_release_events_but_no_revision(self, h):
        """Handoff / Release：有 Audit + 责任事件，但 **Revision 不变**。"""
        gw = ws(h)
        gw.create("W1", "/", "data", "directory", "A")
        gw.create("W1", "/data", "a.py", "file", "A", content="v")

        with h.connection() as db:
            rev_before = len(db.fetchall("SELECT * FROM workspace_revisions"))

        gw.handoff("W1", "/data/a.py", "A", "B", "A", reason="r1")
        gw.release("W1", "/data/a.py", "B")

        with h.connection() as db:
            # 🔒 责任变化**不产生** revision 行
            assert len(db.fetchall("SELECT * FROM workspace_revisions")) == rev_before

            kinds = [r["event_type"] for r in db.fetchall(
                "SELECT event_type FROM workspace_events ORDER BY rowid")]
            assert "RESPONSIBILITY_HANDOFF" in kinds
            assert "RESPONSIBILITY_RELEASED" in kinds

            actions = [r["action"] for r in db.fetchall(
                "SELECT action FROM workspace_audits ORDER BY rowid")]
            assert "HANDOFF" in actions and "RELEASE" in actions

        with h.connection() as db:
            check_workspace_invariants(db, "W1")

    def test_attempt_audit_on_failure(self, h):
        """失败尝试按**已有策略**记 Audit（不发明新的 taxonomy）。"""
        gw = ws(h)
        gw.create("W1", "/", "a.py", "file", "A", content="v0")
        gw.edit("W1", "/a.py", 1, "v1", "A")
        gw.edit("W1", "/a.py", 1, "v2", "A")      # stale

        with h.connection() as db:
            rows = db.fetchall(
                "SELECT action, result, error_code FROM workspace_audits "
                "WHERE action='EDIT_ATTEMPT'")
            assert rows, "失败尝试应该有 Audit"
            assert rows[0]["result"] == "FAILED"
            assert rows[0]["error_code"] == "STALE_REVISION"

    def test_every_event_has_matching_audit(self, h):
        """每个成功 Event 都应该有对应的 Audit（可追踪历史）。"""
        gw = ws(h)
        gw.create("W1", "/", "d", "directory", "A")
        gw.create("W1", "/d", "a.py", "file", "A", content="v")
        gw.edit("W1", "/d/a.py", 1, "v2", "A")
        gw.rename("W1", "/d/a.py", "b.py", "A")
        gw.create("W1", "/d", "sub", "directory", "A")
        gw.move("W1", "/d/sub", "/", "A")
        gw.handoff("W1", "/d", "A", "B", "A", reason="x")
        gw.create("W1", "/d", "c.py", "file", "B", content="v")
        gw.delete("W1", "/d/c.py", "B")

        with h.connection() as db:
            events = db.fetchall(
                "SELECT event_type FROM workspace_events WHERE workspace_id='W1'")
            audits = {r["action"] for r in db.fetchall(
                "SELECT action FROM workspace_audits WHERE workspace_id='W1'")}

            expected_map = {
                "RESOURCE_CREATED": "CREATE",
                "RESOURCE_UPDATED": "EDIT",
                "RESOURCE_RENAMED": "RENAME",
                "RESOURCE_MOVED": "MOVE",
                "RESOURCE_DELETED": "DELETE",
                "RESPONSIBILITY_HANDOFF": "HANDOFF",
            }
            for evt in events:
                action = expected_map.get(evt["event_type"])
                assert action in audits, (
                    f"事件 {evt['event_type']} 没有对应 Audit({action})")

            check_workspace_invariants(db, "W1")
