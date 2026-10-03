"""
`agent_exists` 的 fail-closed 语义（任务书任务二）
============================================================

【为什么需要这一组测试】

    `WorkspaceGateway` 原来把 `agent_exists` 做成可选的，
    `None` 表示「跳过存在性校验」—— 那是 **fail-open**：

        handoff(A → 一个完全不存在的 Agent) → **成功**

    于是资源责任挂在一个虚构的 holder 上，
    而 Effective Lock 解析会忠实地返回那个名字。

    这与 Workspace Core 已经确定的 responsibility 语义不一致：
    **责任必须交给一个真实存在的主体。**

【fail-closed 的两种形态】

    `is_participant`  可省略 → 省略时**拒绝一切**（安全的默认值）
    `agent_exists`    **必需** → 省略时 Gateway **构造不出来**

    不对称是有意的：前者有语义正确的"最严格默认值"，
    后者没有（默认 True = 没校验；默认 False = 误导性错误信息）。
"""

from __future__ import annotations

import pytest

from services.workspace_gateway import WorkspaceGateway
from storage.database import create_memory_database
from tests.workspace_harness import WorkspaceHarness


@pytest.fixture
def h():
    harness = WorkspaceHarness.in_memory(
        participants={"W1": {"A", "B"}},
        agents={"A", "B"},
    )
    harness.bootstrap("W1", "A")
    return harness


# ============================================================
# 1. valid Agent
# ============================================================
class TestValidAgent:
    def test_handoff_to_existing_agent_succeeds(self, h):
        gw = h.gateway()
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        assert h.agent_exists("B") is True

        r = gw.handoff("W1", "/a.py", "A", "B", "A", reason="交给 B")

        assert r.success, f"合法接收方必须成功：{r.error_code}"
        assert r.result["new_holder"] == "B"
        assert h.gateway().store.load_tree("W1").effective_lock(
            h.gateway().store.load_tree("W1").by_path("W1", "/a.py")) == "B"

    def test_directory_handoff_to_existing_agent_succeeds(self, h):
        gw = h.gateway()
        gw.create("W1", "/", "fe", "directory", "A")

        r = gw.handoff("W1", "/fe", "A", "B", "A", reason="交接目录")

        assert r.success
        tree = h.gateway().store.load_tree("W1")
        node = tree.by_path("W1", "/fe")
        assert tree.effective_lock(node) == "B"
        assert tree.effective_controller(node) == "B"


# ============================================================
# 2. nonexistent Agent
# ============================================================
class TestNonexistentAgent:
    def test_handoff_to_ghost_fails_unknown_agent(self, h):
        gw = h.gateway()
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        assert h.agent_exists("ghost") is False

        r = gw.handoff("W1", "/a.py", "A", "ghost", "A", reason="t")

        assert not r.success
        assert r.error_code == "UNKNOWN_AGENT", (
            f"必须明确报 UNKNOWN_AGENT，得到 {r.error_code}"
        )

    def test_ghost_handoff_leaves_responsibility_untouched(self, h):
        """
        🔒 失败的 handoff **不能**留下任何痕迹 ——
           责任必须还在原持有人手里。
        """
        gw = h.gateway()
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        gw.handoff("W1", "/a.py", "A", "ghost", "A", reason="t")

        tree = h.gateway().store.load_tree("W1")
        node = tree.by_path("W1", "/a.py")
        assert tree.effective_lock(node) == "A", "责任不该被交给一个不存在的 Agent"

        with h.connection() as db:
            holders = [r["holder"] for r in db.fetchall(
                "SELECT holder FROM workspace_explicit_locks")]
        assert "ghost" not in holders, "数据库里不该出现虚构的 holder"

    def test_ghost_handoff_is_audited(self, h):
        """失败的尝试仍要留 Audit（与既有策略一致）。"""
        gw = h.gateway()
        gw.create("W1", "/", "a.py", "file", "A", content="x")
        gw.handoff("W1", "/a.py", "A", "ghost", "A", reason="t")

        with h.connection() as db:
            rows = db.fetchall(
                "SELECT action, result, error_code FROM workspace_audits "
                "WHERE action='HANDOFF_ATTEMPT'")
        assert rows, "失败尝试应该有 Audit"
        assert rows[0]["result"] == "FAILED"
        assert rows[0]["error_code"] == "UNKNOWN_AGENT"

    def test_ghost_receiver_checked_before_touching_resource(self, h):
        """身份校验发生在**解析资源之前** —— 连路径都不用存在。"""
        gw = h.gateway()

        r = gw.handoff("W1", "/does/not/exist.py", "A", "ghost", "A",
                       reason="t")

        assert not r.success
        assert r.error_code == "UNKNOWN_AGENT", (
            "身份不合法时应该先报 UNKNOWN_AGENT，"
            "而不是先报 RESOURCE_NOT_FOUND"
        )


# ============================================================
# 3. missing dependency → 构造失败
# ============================================================
class TestMissingDependencyFailsConstruction:
    def test_omitting_agent_exists_raises_type_error(self):
        """
        🔒 不提供 `agent_exists` → **Gateway 构造不出来**。

        不要等到 runtime 某次 handoff 才发现 —— 那是 fail-open 的温床。
        """
        db = create_memory_database()
        try:
            with pytest.raises(TypeError):
                WorkspaceGateway(db)          # type: ignore[call-arg]
        finally:
            db.close()

    def test_explicit_none_raises_value_error(self):
        """显式传 `None` 也不行 —— 关键字必需挡不住这个。"""
        db = create_memory_database()
        try:
            with pytest.raises(ValueError, match="agent_exists"):
                WorkspaceGateway(db, agent_exists=None)   # type: ignore[arg-type]
        finally:
            db.close()

    def test_non_callable_raises_type_error(self):
        """传一个不可调用的东西也应当在构造期失败。"""
        db = create_memory_database()
        try:
            with pytest.raises(TypeError, match="可调用"):
                WorkspaceGateway(db, agent_exists="not-callable")  # type: ignore[arg-type]
        finally:
            db.close()

    def test_non_callable_message_is_actionable(self):
        """错误信息要能指导修复，而不是只说"参数错了"。"""
        db = create_memory_database()
        try:
            with pytest.raises(ValueError) as excinfo:
                WorkspaceGateway(db, agent_exists=None)   # type: ignore[arg-type]
            msg = str(excinfo.value)
            assert "必需依赖" in msg
            assert "handoff" in msg, "要说明后果"
            assert "lambda" in msg, "要给出可操作的替代写法"
        finally:
            db.close()

    def test_composition_root_helper_also_requires_it(self):
        """组合根的组装函数同样必需 —— 漏传在最外层就失败。"""
        import main as main_module

        db = create_memory_database()
        try:
            with pytest.raises(TypeError):
                main_module.build_workspace_services(db)   # type: ignore[call-arg]
        finally:
            db.close()

    def test_providing_it_constructs_successfully(self):
        """正向：提供了就能构造。"""
        db = create_memory_database()
        try:
            gw = WorkspaceGateway(db, agent_exists=lambda a: True)
            assert gw is not None
        finally:
            db.close()


# ============================================================
# 4. participant 与 agent_exists 职责仍然分开
# ============================================================
class TestResponsibilitiesStaySeparate:
    def test_existing_non_participant_is_rejected_as_participant(self, h):
        """
        🔒 **两个 callback 是两个问题：**

            agent_exists   → 这个 Agent 真实存在吗？
            is_participant → 它能参与这个 Workspace 吗？

        一个真实存在但不在 Workspace 里的 Agent：
            · agent_exists   → True
            · is_participant → False
            · 结果           → NOT_TASK_PARTICIPANT（**不是** UNKNOWN_AGENT）
        """
        gw = h.gateway()
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        # OUTSIDER 真实存在（加进已知名单），但不是 W1 的 Participant
        h.extra_agents.add("OUTSIDER")
        assert h.agent_exists("OUTSIDER") is True
        assert "OUTSIDER" not in h.participants["W1"]

        r = gw.handoff("W1", "/a.py", "A", "OUTSIDER", "A", reason="t")

        assert not r.success
        assert r.error_code == "NOT_TASK_PARTICIPANT", (
            f"应当是 NOT_TASK_PARTICIPANT，得到 {r.error_code} —— "
            f"两个 callback 的职责不能混"
        )

    def test_ghost_is_unknown_not_participant(self, h):
        """不存在的 Agent 报 UNKNOWN_AGENT，不是 NOT_TASK_PARTICIPANT。"""
        gw = h.gateway()
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        r = gw.handoff("W1", "/a.py", "A", "ghost", "A", reason="t")

        assert r.error_code == "UNKNOWN_AGENT"

    def test_the_two_callbacks_are_used_independently(self, h):
        """
        用一个探针证明**两个 callback 都被真的调用了**，
        而且问的是不同的问题。
        """
        calls: list = []

        def probing_exists(agent_id: str) -> bool:
            calls.append(("exists", agent_id))
            return agent_id in {"A", "B"}

        def probing_participant(workspace_id: str, agent_id: str) -> bool:
            calls.append(("participant", agent_id))
            return agent_id in {"A", "B"}

        gw = h.gateway()
        gw.create("W1", "/", "a.py", "file", "A", content="x")

        real_gateway = WorkspaceGateway(
            h.new_db(), lock_registry=h.locks,
            is_participant=probing_participant,
            agent_exists=probing_exists,
        )
        real_gateway.handoff("W1", "/a.py", "A", "B", "A", reason="t")

        kinds = {k for k, _ in calls}
        assert kinds == {"exists", "participant"}, (
            f"两个 callback 都该被调用，实际只调了 {kinds}"
        )

    def test_no_new_permission_concept_introduced(self):
        """
        🔒 确认没有把两个 callback 合并成"新的权限体系"。

        Gateway 只有这两个依赖回调 ——
        没有 Authorization / ACL / Permission / Role。
        """
        import ast
        from pathlib import Path as _Path

        tree = ast.parse(_Path("services/workspace_gateway.py").read_text(
            encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "WorkspaceGateway":
                init = next(
                    (n for n in node.body
                     if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
                    None,
                )
                assert init is not None
                params = {a.arg for a in init.args.args + init.args.kwonlyargs}
                for banned in ("authorization", "acl", "permission",
                               "permissions", "role", "roles", "iam"):
                    assert banned not in params, (
                        f"Gateway 引入了权限概念参数 {banned!r}"
                    )
                # 只有这两个判定回调
                checks = params & {"is_participant", "agent_exists"}
                assert checks == {"is_participant", "agent_exists"}
                return
        pytest.fail("找不到 WorkspaceGateway")
