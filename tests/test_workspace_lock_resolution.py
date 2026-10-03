"""
Effective Lock Resolution —— **纯函数**单元测试
============================================================

【为什么单独一个文件、密集地测】

    `resolve_effective_lock` / `ResourceTree` 是整个 Workspace Core
    里**最容易写错**、也**最值得单独验证**的部分。

    它不碰数据库、没有副作用 —— 所以可以凭空构造资源树来测，
    不需要建表、不需要事务、不需要 Agent 记录。

【这一组测试对应任务书的哪些条目】

    Lock 9 项（§45 的第 10–18 条）：
        10 Directory Lock · 11 File Lock · 12 Implicit Lock ·
        13 Explicit Lock · 14 Effective Lock Resolution ·
        15 Explicit Child Lock 覆盖 Parent ·
        16 新资源不自动 Explicit Lock ·
        17 Edit End 不 Release · 18 同 File 单 Holder
"""

from __future__ import annotations

import pytest

from core.workspace_core import (
    ExplicitLock,
    LockKind,
    Resource,
    ResourceState,
    ResourceTree,
    ResourceType,
    WorkspacePathError,
    normalize_path,
)
from core.workspace_core.resource import (
    basename_of,
    join_path,
    parent_path_of,
)


# ============================================================
# 构造辅助 —— 让测试读起来像在描述一棵树
# ============================================================
def res(
    rid: str,
    path: str,
    *,
    parent: str | None,
    kind: ResourceType = ResourceType.DIRECTORY,
    workspace: str = "W1",
    state: ResourceState = ResourceState.ACTIVE,
    revision: int = 1,
) -> Resource:
    return Resource(
        resource_id=rid,
        workspace_id=workspace,
        parent_id=parent,
        name=basename_of(path),
        path=path,
        resource_type=kind,
        state=state,
        revision=revision,
        content="body" if kind is ResourceType.FILE else None,
        created_by="A",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


def lock(
    rid: str,
    holder: str,
    kind: LockKind = LockKind.DIRECTORY,
) -> ExplicitLock:
    return ExplicitLock(
        resource_id=rid,
        holder=holder,
        kind=kind,
        acquired_at="2024-01-01T00:00:00",
        acquired_by=holder,
    )


def tree(*resources: Resource, locks: tuple[ExplicitLock, ...] = ()) -> ResourceTree:
    return ResourceTree(resources, locks)


# ============================================================
# 一、逻辑路径
# ============================================================
class TestLogicalPath:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", "/"),
            (".", "/"),
            ("/", "/"),
            ("src", "/src"),
            ("src/", "/src"),
            ("/src", "/src"),
            ("/src/main.py", "/src/main.py"),
            ("src//main.py", "/src/main.py"),
            ("//src///a.txt", "/src/a.txt"),
            ("src\\main.py", "/src/main.py"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_path(raw) == expected

    @pytest.mark.parametrize("bad", ["../etc/passwd", "src/../../x", "..", "a/../b"])
    def test_rejects_dotdot(self, bad):
        """
        🔒 Workspace 是**封闭命名空间** —— 不允许用 `..` 逃出去。

        （与 `core/workspace.py` 的 `WorkspaceViolationError` 同一条边界精神。）
        """
        with pytest.raises(WorkspacePathError):
            normalize_path(bad)

    def test_rejects_non_string(self):
        with pytest.raises(WorkspacePathError):
            normalize_path(None)
        with pytest.raises(WorkspacePathError):
            normalize_path(123)

    def test_join_and_parent(self):
        assert join_path("/src", "a.py") == "/src/a.py"
        assert join_path("/", "src") == "/src"
        assert parent_path_of("/src/main.py") == "/src"
        assert parent_path_of("/src") == "/"
        assert parent_path_of("/") == "/"
        assert basename_of("/src/main.py") == "main.py"
        assert basename_of("/") == ""

    def test_join_rejects_nested_name(self):
        """name 必须是单层 —— 否则 create 会绕过父目录责任。"""
        with pytest.raises(WorkspacePathError):
            join_path("/src", "a/b.py")


# ============================================================
# 二、Rule 1 —— 自身 Explicit Lock 优先
# ============================================================
class TestRule1OwnExplicitWins:
    def test_file_own_explicit_wins(self):
        """Rule 1：自身有 Explicit File Lock → 用它。"""
        root = res("R0", "/", parent=None)
        data = res("R1", "/data", parent="R0")
        f = res("R2", "/data/a.csv", parent="R1", kind=ResourceType.FILE)

        t = tree(root, data, f, locks=(lock("R2", "C", LockKind.FILE),))

        assert t.effective_lock(f) == "C"

    def test_explicit_beats_inherited(self):
        """🔒 **Explicit > Inherited**（任务书 §10）。"""
        root = res("R0", "/", parent=None)
        data = res("R1", "/data", parent="R0")
        f = res("R2", "/data/a.csv", parent="R1", kind=ResourceType.FILE)

        t = tree(
            root, data, f,
            locks=(
                lock("R1", "A", LockKind.DIRECTORY),   # 父目录 = A
                lock("R2", "C", LockKind.FILE),        # 自己 = C
            ),
        )

        assert t.effective_lock(f) == "C", "自身 Explicit 必须压过父目录继承"

    def test_rule4_nearer_wins(self):
        """
        Rule 4：**更近的** Directory Lock 胜出。

            data/                     Lock A
            data/processed/           Lock B
            data/processed/reports/   Lock C
        """
        root = res("R0", "/", parent=None)
        data = res("R1", "/data", parent="R0")
        proc = res("R2", "/data/processed", parent="R1")
        reports = res("R3", "/data/processed/reports", parent="R2")
        deep = res("R4", "/data/processed/reports/x.csv", parent="R3",
                   kind=ResourceType.FILE)

        t = tree(
            root, data, proc, reports, deep,
            locks=(
                lock("R1", "A"),
                lock("R2", "B"),
                lock("R3", "C"),
            ),
        )

        mid = res("R5", "/data/processed/x.csv", parent="R2", kind=ResourceType.FILE)
        t2 = tree(root, data, proc, reports, deep, mid,
                  locks=(lock("R1", "A"), lock("R2", "B"), lock("R3", "C")))

        assert t2.effective_lock(mid) == "B", "应继承最近的 data/processed = B"
        assert t2.effective_lock(deep) == "C", "应继承最近的 reports = C"


# ============================================================
# 三、Rule 2/3 —— Implicit Lock 继承
# ============================================================
class TestRule23Implicit:
    def test_inherits_nearest_directory_lock(self):
        """§12：没有 Explicit Lock 的 File 继承最近 Directory Lock。"""
        root = res("R0", "/", parent=None)
        configs = res("R1", "/configs", parent="R0")
        app = res("R2", "/configs/app.yaml", parent="R1", kind=ResourceType.FILE)

        t = tree(root, configs, app, locks=(lock("R1", "A"),))

        assert t.effective_lock(app) == "A"
        assert t.explicit_lock_of("R2") is None, (
            "🔒 创建新资源**不产生** Explicit Lock（任务书 §11）"
        )

    def test_subdirectory_inherits(self):
        """§7：没有 Explicit Lock 的**子目录**也继承。"""
        root = res("R0", "/", parent=None)
        configs = res("R1", "/configs", parent="R0")
        dev = res("R2", "/configs/dev", parent="R1")

        t = tree(root, configs, dev, locks=(lock("R1", "A"),))

        assert t.effective_lock(dev) == "A"

    def test_deep_chain_inherits_from_top(self):
        """一条很深的链，只有根目录有锁 —— 最深的文件仍然继承它。"""
        root = res("R0", "/", parent=None)
        a = res("R1", "/a", parent="R0")
        b = res("R2", "/a/b", parent="R1")
        c = res("R3", "/a/b/c", parent="R2")
        f = res("R4", "/a/b/c/d.txt", parent="R3", kind=ResourceType.FILE)

        t = tree(root, a, b, c, f, locks=(lock("R0", "A"),))

        assert t.effective_lock(f) == "A"

    def test_no_lock_anywhere_returns_none(self):
        """整条链上都没有显式锁 → 无人负责（不是错误，是状态）。"""
        root = res("R0", "/", parent=None)
        f = res("R1", "/a.txt", parent="R0", kind=ResourceType.FILE)

        assert tree(root, f).effective_lock(f) is None


# ============================================================
# 四、File 的 Explicit **File** Lock 不被继承
# ============================================================
class TestFileLockIsNotInheritable:
    def test_file_lock_does_not_leak_to_siblings(self):
        """
        🔒 一个 File 的 Explicit **File** Lock 只对它自己有效。

            data/            Directory Lock = A
            data/a.csv       Explicit File Lock = B
            data/b.csv       （无）→ 应该继承 A，**不是** B
        """
        root = res("R0", "/", parent=None)
        data = res("R1", "/data", parent="R0")
        a = res("R2", "/data/a.csv", parent="R1", kind=ResourceType.FILE)
        b = res("R3", "/data/b.csv", parent="R1", kind=ResourceType.FILE)

        t = tree(
            root, data, a, b,
            locks=(lock("R1", "A"), lock("R2", "B", LockKind.FILE)),
        )

        assert t.effective_lock(a) == "B"
        assert t.effective_lock(b) == "A", (
            "b.csv 不该继承 a.csv 的 File Lock —— 继承来源必须是 Directory"
        )


# ============================================================
# 五、Controller（Directory 才有）
# ============================================================
class TestController:
    def test_directory_controller_equals_its_lock(self):
        """
        Controller 与 Directory Lock 同源（设计简化后的结果 —— 见 lock.py docstring）。
        """
        root = res("R0", "/", parent=None)
        frontend = res("R1", "/frontend", parent="R0")

        t = tree(root, frontend, locks=(lock("R1", "B"),))

        assert t.effective_controller(frontend) == "B"
        assert t.effective_lock(frontend) == "B"

    def test_file_has_no_own_controller(self):
        """
        🔒 **File 不需要 Controller**（任务书 §6）。

        File 的 `effective_controller` 取自它所在的目录链，
        但**不代表 File 拥有自己的 Controller** ——
        即使给 File 挂了一条 Explicit **File** Lock，
        它的 Controller 仍然来自父目录。
        """
        root = res("R0", "/", parent=None)
        src = res("R1", "/src", parent="R0")
        main = res("R2", "/src/main.py", parent="R1", kind=ResourceType.FILE)

        t = tree(
            root, src, main,
            locks=(lock("R1", "A"), lock("R2", "C", LockKind.FILE)),
        )

        assert t.effective_lock(main) == "C", "锁是 C 的"
        assert t.effective_controller(main) == "A", (
            "但 Controller 仍来自父目录 —— File 没有自己的 Controller"
        )

    def test_file_controller_follows_parent_chain(self):
        root = res("R0", "/", parent=None)
        data = res("R1", "/data", parent="R0")
        proc = res("R2", "/data/processed", parent="R1")
        f = res("R3", "/data/processed/r.csv", parent="R2", kind=ResourceType.FILE)

        t = tree(root, data, proc, f,
                 locks=(lock("R1", "A"), lock("R2", "B")))

        assert t.effective_controller(f) == "B", "最近的目录 Controller 是 B"


# ============================================================
# 六、父级责任转移：隐式跟随、显式不跟随
# ============================================================
class TestParentTransfer:
    def test_implicit_children_follow_parent(self):
        """§12：父级 Directory Lock A→B，隐式后代自动 → B。"""
        root = res("R0", "/", parent=None)
        configs = res("R1", "/configs", parent="R0")
        app = res("R2", "/configs/app.yaml", parent="R1", kind=ResourceType.FILE)
        dev = res("R3", "/configs/dev", parent="R1")

        before = tree(root, configs, app, dev, locks=(lock("R1", "A"),))
        after = tree(root, configs, app, dev, locks=(lock("R1", "B"),))

        assert before.effective_lock(app) == "A"
        assert after.effective_lock(app) == "B", "隐式后代必须跟随"
        assert after.effective_lock(dev) == "B"

    def test_explicit_child_is_not_swallowed(self):
        """
        🔒 **第一版最重要的规则之一**（任务书 §12）：

            父级 Handoff **不能吞回**已有的 Explicit Child Lock。

                configs/                 Lock A → B
                configs/app.yaml         implicit    → B   ✅ 跟随
                configs/production.yaml  Explicit C  → **仍 C**  ✅ 不跟随
        """
        root = res("R0", "/", parent=None)
        configs = res("R1", "/configs", parent="R0")
        app = res("R2", "/configs/app.yaml", parent="R1", kind=ResourceType.FILE)
        prod = res("R3", "/configs/production.yaml", parent="R1",
                   kind=ResourceType.FILE)

        before = tree(root, configs, app, prod,
                      locks=(lock("R1", "A"), lock("R3", "C", LockKind.FILE)))

        # A 把 configs/ 交给 B —— production.yaml 的显式锁**不动**
        after = tree(root, configs, app, prod,
                     locks=(lock("R1", "B"), lock("R3", "C", LockKind.FILE)))

        assert before.effective_lock(prod) == "C"
        assert after.effective_lock(prod) == "C", (
            "🔒 显式子资源责任不能被父级转移吞回"
        )
        assert after.effective_lock(app) == "B", "隐式的要跟随"


# ============================================================
# 七、Release 后重新解析
# ============================================================
class TestReleaseRecomputes:
    def test_release_falls_back_to_parent(self):
        """
        §14：C release(result.csv) → Explicit 移除 → Effective 回到 B。
        """
        root = res("R0", "/", parent=None)
        data = res("R1", "/data", parent="R0")
        proc = res("R2", "/data/processed", parent="R1")
        result = res("R3", "/data/processed/result.csv", parent="R2",
                     kind=ResourceType.FILE)

        with_explicit = tree(
            root, data, proc, result,
            locks=(lock("R2", "B"), lock("R3", "C", LockKind.FILE)),
        )
        after_release = tree(
            root, data, proc, result,
            locks=(lock("R2", "B"),),          # R3 的显式锁被移除
        )

        assert with_explicit.effective_lock(result) == "C"
        assert after_release.effective_lock(result) == "B", (
            "Release 后必须**重新按父级规则解析**"
        )
        assert after_release.explicit_lock_of("R3") is None


# ============================================================
# 八、责任边界（delete / move / rename 的安全阀）
# ============================================================
class TestResourceBoundary:
    def _fixture(self):
        """
            data/             Directory Lock = A
            data/processed/   Directory Lock = B   ← B 的独立边界
            data/processed/r.csv
            data/other.csv
        """
        root = res("R0", "/", parent=None)
        data = res("R1", "/data", parent="R0")
        proc = res("R2", "/data/processed", parent="R1")
        r = res("R3", "/data/processed/r.csv", parent="R2",
                kind=ResourceType.FILE)
        other = res("R4", "/data/other.csv", parent="R1", kind=ResourceType.FILE)

        t = tree(root, data, proc, r, other,
                 locks=(lock("R1", "A"), lock("R2", "B")))
        return t, {"root": root, "data": data, "proc": proc, "r": r,
                   "other": other}

    def test_boundary_holders_lists_subtree_explicit_locks(self):
        t, n = self._fixture()

        holders = t.boundary_holders(n["data"])

        assert holders == {"R1": "A", "R2": "B"}, (
            "子树里的显式锁就是责任边界"
        )

    def test_foreign_boundary_detected(self):
        """A 想动 data/ 时，子树里有 B 的边界 → 必须被看见。"""
        t, n = self._fixture()

        foreign = t.foreign_boundaries(n["data"], "A")

        assert foreign == {"R2": "B"}, "B 的边界必须被识别为「别人的」"

    def test_own_boundary_is_not_foreign(self):
        t, n = self._fixture()

        assert t.foreign_boundaries(n["data"], "A") == {"R2": "B"}
        # A 自己的那条不算「别人的」
        assert "R1" not in t.foreign_boundaries(n["data"], "A")

    def test_is_within_foreign_boundary(self):
        """`data/processed/r.csv` 落在 B 的边界里 → A 不能直接动它。"""
        t, n = self._fixture()

        assert t.is_within_foreign_boundary(n["r"], "A") == "R2"
        assert t.is_within_foreign_boundary(n["other"], "A") is None
        assert t.is_within_foreign_boundary(n["r"], "B") is None, (
            "B 自己在边界里，不算「别人的」"
        )

    def test_ancestors_are_near_to_far(self):
        """🔒 顺序必须「近 → 远」，Rule 4 依赖它。"""
        t, n = self._fixture()

        chain = t.ancestors(n["r"])

        assert [a.resource_id for a in chain] == ["R2", "R1", "R0"]


# ============================================================
# 九、树的其他能力
# ============================================================
class TestTreeBasics:
    def test_path_of_reconstructed_from_chain(self):
        """
        `path_of` 用 parent 链重算路径 —— 用于检测表里的 path 缓存漂移。
        """
        root = res("R0", "/", parent=None)
        src = res("R1", "/src", parent="R0")
        f = res("R2", "/src/main.py", parent="R1", kind=ResourceType.FILE)

        t = tree(root, src, f)

        assert t.path_of(f) == "/src/main.py"
        assert t.path_of(src) == "/src"
        assert t.path_of(root) == "/"

    def test_descendants(self):
        root = res("R0", "/", parent=None)
        src = res("R1", "/src", parent="R0")
        sub = res("R2", "/src/sub", parent="R1")
        deep = res("R3", "/src/sub/x.py", parent="R2", kind=ResourceType.FILE)

        t = tree(root, src, sub, deep)

        ids = {r.resource_id for r in t.descendants(src)}
        assert ids == {"R2", "R3"}

    def test_ancestors_stops_on_broken_chain(self):
        """数据破损（parent 指向不存在的 id）时不能死循环。"""
        orphan = res("R1", "/x", parent="MISSING")

        t = tree(orphan)

        assert t.ancestors(orphan) == []

    def test_ancestors_stops_on_cycle(self):
        """即使数据里出现环，也不能无限循环。"""
        a = res("R1", "/a", parent="R2")
        b = res("R2", "/b", parent="R1")

        t = tree(a, b)

        chain = t.ancestors(a)
        assert len(chain) <= 2, f"环导致遍历过长：{chain}"

    def test_by_path_and_get(self):
        root = res("R0", "/", parent=None)
        f = res("R1", "/a.txt", parent="R0", kind=ResourceType.FILE)

        t = tree(root, f)

        assert t.by_path("W1", "/a.txt").resource_id == "R1"
        assert t.by_path("W1", "a.txt").resource_id == "R1"
        assert t.by_path("W2", "/a.txt") is None, "不同 workspace 不串"
        assert t.get("R0").resource_id == "R0"
        assert t.root_of("W1").resource_id == "R0"

    def test_workspaces_do_not_leak(self):
        """两个 workspace 的资源在同一棵树里也不互相影响。"""
        w1root = res("R0", "/", parent=None, workspace="W1")
        w1f = res("R1", "/a.txt", parent="R0", kind=ResourceType.FILE,
                  workspace="W1")
        w2root = res("R2", "/", parent=None, workspace="W2")
        w2f = res("R3", "/a.txt", parent="R2", kind=ResourceType.FILE,
                  workspace="W2")

        t = tree(w1root, w1f, w2root, w2f,
                 locks=(lock("R0", "A"), lock("R2", "B")))

        assert t.effective_lock(w1f) == "A"
        assert t.effective_lock(w2f) == "B"


# ============================================================
# 十、Edit End ≠ Lock Release（语义层验证）
# ============================================================
class TestEditEndDoesNotRelease:
    def test_editing_does_not_change_lock(self):
        """
        🔒 `EDIT END ≠ LOCK RELEASE`（任务书 §7）。

        这条在**纯函数层**的表现是：

            一个 File 的显式锁是否变化，**只取决于显式锁集合**，
            与「它的 revision 变了几次」**完全无关**。

        所以下面两次解析（revision 不同、锁相同）
        必须给出**相同**的 holder。
        """
        root = res("R0", "/", parent=None)
        src = res("R1", "/src", parent="R0")
        r1 = res("R2", "/src/page.tsx", parent="R1",
                 kind=ResourceType.FILE, revision=1)
        r9 = res("R2", "/src/page.tsx", parent="R1",
                 kind=ResourceType.FILE, revision=9)

        locks = (lock("R1", "A"),)

        assert tree(root, src, r1, locks=locks).effective_lock(r1) == "A"
        assert tree(root, src, r9, locks=locks).effective_lock(r9) == "A", (
            "编辑（revision 变化）不改变锁 —— EDIT END ≠ LOCK RELEASE"
        )

    def test_same_file_single_holder_by_construction(self):
        """
        🔒 同一 File **最多一个** Effective Lock Holder。

        在领域层，这由 `ExplicitLock` 以 `resource_id` 为主键保证 ——
        一个资源不可能有两条显式锁。
        这里验证「后一条覆盖前一条」的映射语义。
        """
        root = res("R0", "/", parent=None)
        f = res("R1", "/a.py", parent="R0", kind=ResourceType.FILE)

        # 模拟持久层的唯一约束：同一个 resource_id 只能有一条
        t = ResourceTree([root, f], [lock("R1", "A", LockKind.FILE)])

        assert t.effective_lock(f) == "A"

        # 换一个新的持锁人 —— 仍然只有**一个** holder
        t2 = ResourceTree([root, f], [lock("R1", "B", LockKind.FILE)])

        assert t2.effective_lock(f) == "B"
        assert t2.explicit_lock_of("R1").holder == "B"
