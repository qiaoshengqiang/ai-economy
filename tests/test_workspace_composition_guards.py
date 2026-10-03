"""
组合根接线守卫（任务书 §22 / §26 G、I）
============================================================

【这一组守卫防的是什么】

    任务书 §22 明确列了几条**不能**发生的事：

        ❌ 不允许 `WorkspaceGateway` 自己偷偷创建全局 singleton
        ❌ 不允许 Agent 直接创建 `WorkspaceStore`
        ❌ 不允许 Agent 直接访问 DB

    以及 §26 I「架构完整性」：
        没有重新引入 Authorization / Delegation / Cascade Revocation /
        ACL / IAM / generic lock() / automatic recovery

【守卫方法（ADR-0018）】

    优先用 **AST / import graph / 真实调用点**，
    不靠 `source.count()` 这种脆弱手段。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = PROJECT_ROOT / "agents"
SERVICES_DIR = PROJECT_ROOT / "services"
CORE_DIR = PROJECT_ROOT / "core"
MAIN = PROJECT_ROOT / "main.py"


def _defs(path: Path) -> set[str]:
    """这个文件定义了哪些**名字**（类 / 函数 / 顶层赋值）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _imports(path: Path) -> set[str]:
    """这个文件 import 了哪些**模块路径**。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
    return mods


# ============================================================
# 一、组合根真的组装了 Workspace Core
# ============================================================
class TestCompositionRootWiresWorkspace:
    def test_build_workspace_services_exists(self):
        assert "build_workspace_services" in _defs(MAIN), (
            "main.py 里必须有组装 Workspace Core 三件套的入口"
        )

    def test_build_runtime_calls_it(self):
        """`build_runtime` 必须**真实调用**组装函数（不能只是定义）。"""
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "build_runtime"):
                called = {
                    n.func.id for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                assert "build_workspace_services" in called, (
                    "build_runtime 没有调用 build_workspace_services"
                )
                return
        pytest.fail("找不到 build_runtime")

    def test_runtime_has_workspace_fields(self):
        """Runtime 必须真的持有这三样东西。"""
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Runtime":
                fields = {
                    stmt.target.id
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                }
                for expected in ("workspaces", "workspace_gateway",
                                 "workspace_locks"):
                    assert expected in fields, f"Runtime 缺字段 {expected}"
                return
        pytest.fail("找不到 Runtime")

    def test_participant_checker_is_injected(self):
        """组合根必须真的注入 is_participant —— 不是留空。"""
        src = MAIN.read_text(encoding="utf-8")
        assert "build_participant_checker" in src
        assert "is_participant=build_participant_checker" in src, (
            "必须把真实参与者判定注入 Gateway"
        )

    def test_identity_checker_is_injected(self):
        """
        🔒 身份存在性也必须注入 ——
           否则 Handoff 可以指向一个**虚构的**接收方
           （这是 Hardening 修掉的一个真实缺陷）。
        """
        src = MAIN.read_text(encoding="utf-8")
        assert "agent_exists=build_agent_exists_checker" in src, (
            "必须注入 agent_exists，否则 handoff 可以指向不存在的 Agent"
        )


# ============================================================
# 二、🔒 不允许全局 singleton
# ============================================================
class TestNoGlobalSingleton:
    def test_gateway_module_has_no_module_level_instance(self):
        """
        `services/workspace_gateway.py` **不得**在模块层创建实例。
        """
        tree = ast.parse(
            (SERVICES_DIR / "workspace_gateway.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assert not isinstance(node.value, ast.Call), (
                            f"模块层出现了实例 {target.id} —— "
                            f"不允许全局 singleton"
                        )

    def test_build_workspace_services_builds_fresh_objects(self):
        """
        🔒 每次调用都造**新的** store / gateway。

        这条很重要：如果它是「第一次调用建好、之后返回同一个」，
        测试就不再能独立构造 —— 那是 singleton 的伪装。
        """
        import inspect

        import main as main_module

        src = inspect.getsource(main_module.build_workspace_services)
        assert "WorkspaceStore(db=db)" in src, "必须每次新建 store"
        assert "WorkspaceGateway(" in src, "必须每次新建 gateway"
        # 不能有缓存字典 / global 声明
        for banned in ("global ", "_INSTANCE", "_instance", "lru_cache"):
            assert banned not in src, f"build_workspace_services 里有 {banned!r}"

    def test_two_calls_give_independent_objects(self):
        """实际验证：两次组装互不影响。"""
        import main as main_module
        from storage.database import create_memory_database

        db1, db2 = create_memory_database(), create_memory_database()
        try:
            s1, r1, g1 = main_module.build_workspace_services(
                db1, is_participant=lambda w, a: True)
            s2, r2, g2 = main_module.build_workspace_services(
                db2, is_participant=lambda w, a: True)

            assert g1 is not g2
            assert r1 is not r2
            assert s1 is not s2

            # 在一个里建东西，另一个完全看不到
            g1.ensure_workspace("W-A", "A")
            assert g2.store.find_by_path("W-A", "/") is None
        finally:
            db1.close()
            db2.close()


# ============================================================
# 三、🔒 Agent 不能直接碰 Store / DB / Gateway 内部
# ============================================================
class TestAgentsDoNotTouchWorkspaceInternals:
    def test_agents_do_not_import_workspace_store(self):
        """Agent 不得直接创建 / import `WorkspaceStore`。"""
        offenders = []
        for path in AGENT_DIR.rglob("*.py"):
            mods = _imports(path)
            if any("workspace_store" in m for m in mods):
                offenders.append(path.name)
        assert not offenders, (
            f"Agent 直接依赖 WorkspaceStore：{offenders} —— "
            f"必须经 Runtime / Gateway"
        )

    def test_agents_do_not_import_gateway_internals(self):
        """
        Agent 不得直接 import Gateway 的**内部实现**。

        （`agents/` 目前完全不知道 Workspace 的存在 ——
          这是比「允许它拿 gateway」更强的边界，
          也正是任务书 §22 要的「不要把 Gateway 塞进 Agent 内部」。）
        """
        offenders = []
        for path in AGENT_DIR.rglob("*.py"):
            mods = _imports(path)
            if any("workspace_gateway" in m for m in mods):
                offenders.append(path.name)
        assert not offenders, f"Agent 直接依赖 Gateway：{offenders}"

    def test_agents_do_not_touch_database_directly(self):
        """Agent 不得直接访问 DB（不得 import storage.database）。"""
        offenders = []
        for path in AGENT_DIR.rglob("*.py"):
            mods = _imports(path)
            if "storage.database" in mods:
                offenders.append(path.name)
        assert not offenders, f"Agent 直接访问 DB：{offenders}"

    def test_gateway_does_not_know_task_or_economy(self):
        """
        🔒 **依赖倒置**：Gateway 不得 import Task / Economy。

        它只接受两个**纯回调** —— 这是让 Gateway
        能脱离整个 Task 系统单独测试的原因。
        """
        mods = _imports(SERVICES_DIR / "workspace_gateway.py")

        for banned in ("core.task", "core.task_store", "core.economy",
                       "agents"):
            hit = [m for m in mods if m == banned or m.startswith(banned + ".")]
            assert not hit, (
                f"Gateway 直接 import 了 {hit} —— "
                f"参与者判定必须通过注入的回调"
            )


# ============================================================
# 四、🔒 架构完整性：没有重新引入被移除的概念（§26 I）
# ============================================================
class TestArchitectureIntegrity:
    FORBIDDEN_CLASSES = {
        "AuthorizationEngine", "AuthorizationRequest", "DelegationService",
        "DelegationTree", "CascadeRevocation", "PermissionEngine",
        "AccessControl", "ACL", "Role", "IAMPolicy",
        "RecoveryManager", "AutoRecovery", "LockManager", "LockStore",
        "WorkspaceBackend", "LocalWorkspaceBackend", "EditSession",
    }

    def test_no_forbidden_classes_anywhere(self):
        found: dict[str, list[str]] = {}
        for layer in ("core", "services", "communication", "storage",
                      "agents", "llm"):
            base = PROJECT_ROOT / layer
            if not base.exists():
                continue
            for path in base.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {
                    n.name for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef)
                }
                hit = names & self.FORBIDDEN_CLASSES
                if hit:
                    found[str(path.relative_to(PROJECT_ROOT))] = sorted(hit)
        assert not found, f"重新引入了已移除的概念：{found}"

    def test_no_lock_api_on_gateway(self):
        """
        🔒 **不提供 `lock(resource)`**（任务书 §7）。

        Lock 是工作责任，不是 mutex；
        「先锁起来再说」的无责任占用是不允许的。
        """
        tree = ast.parse(
            (SERVICES_DIR / "workspace_gateway.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "WorkspaceGateway":
                methods = {
                    n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                assert "lock" not in methods, "出现了 lock() API"
                assert "unlock" not in methods, "出现了 unlock() API"
                for public in ("acquire", "claim_lock", "try_lock"):
                    assert public not in methods, f"出现了 {public}()"

    def test_no_automatic_timeout_release(self):
        """
        🔒 **没有自动超时释放锁**（任务书 §7 / §21 错误 3）。

        允许出现的 `expire` 只与**资源过期**（task expires_at）有关，
        不能与 lock / holder 组合成自动释放逻辑。
        """
        src = (SERVICES_DIR / "workspace_gateway.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        # 没有定时器 / 后台线程
        for banned in ("threading.Timer", "schedule", "setTimeout",
                       "after(", "cron"):
            assert banned not in src, f"出现了自动执行机制 {banned!r}"

        # 方法名里不能有 timeout/expire 相关的释放
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                low = node.name.lower()
                assert not ("timeout" in low and "release" in low), (
                    f"出现了超时释放 {node.name}"
                )

    def test_no_git_operations_exposed(self):
        """🔒 不暴露任何 Git 操作（任务书 §14）。"""
        tree = ast.parse(
            (SERVICES_DIR / "workspace_gateway.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                low = node.name.lower()
                for banned in ("git", "commit", "checkout", "branch", "merge"):
                    assert banned not in low, (
                        f"Gateway 暴露了 Git 相关方法 {node.name}"
                    )


# ============================================================
# 五、Demo 走真实链路，不绕开组合根
# ============================================================
class TestDemoUsesCompositionRoot:
    def test_demo_uses_runtime_gateway(self):
        """
        🔒 Demo 必须用 `runtime.workspace_gateway`，
           **不得**自己 `WorkspaceGateway(...)` —— 那样证明不了任何事。
        """
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "cmd_workspace_demo"):
                # 不能自己构造 Gateway
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        if (isinstance(call.func, ast.Name)
                                and call.func.id in ("WorkspaceGateway",
                                                     "WorkspaceStore")):
                            pytest.fail(
                                "cmd_workspace_demo 自己创建了 "
                                f"{call.func.id} —— 必须走组合根"
                            )
                src = ast.unparse(node)
                assert "runtime.workspace_gateway" in src, (
                    "demo 必须用组合根装好的 gateway"
                )
                return
        pytest.fail("找不到 cmd_workspace_demo")

    def test_demo_is_registered(self):
        src = MAIN.read_text(encoding="utf-8")
        assert '"workspace-demo"' in src, "workspace-demo 子命令没注册"


# ============================================================
# 六、🔒 元守卫：并发测试**必须**用「每线程一个 Database」
# ============================================================
class TestConcurrencyTestsUseFileDatabase:
    """
    防止「共享内存 `Database` 做并发测试」这个错误**再次出现**。

    【为什么值得一条守卫】

        这是本项目**已记录的技术债 #2**：

            `Database._tx_depth` 是**实例状态**，
            跨线程共享会被交错修改 → 事务边界错乱。

        我在这一阶段**犯了两次**同样的错：

            第一次：`tests/test_workspace_gateway.py` 的并发测试
            第二次：`test_reverse_move_race_no_deadlock` 用了内存 fixture

        两次都表现为「单独跑通过、全量跑偶发失败」——
        那是最难查的一类症状。所以把它变成一条守卫。

    【判定规则】

        一个测试函数如果**创建线程 / 用线程池**，
        它的 fixture 就不能是内存库（`h` / `rig`），
        必须是文件库（`fh` / `file_rig` / `h20` / `multi`）。
    """

    #: 内存库 fixture —— 并发测试里**禁止**使用
    IN_MEMORY_FIXTURES = {"h", "rig"}
    #: 文件库 fixture —— 每线程一条连接
    FILE_FIXTURES = {"fh", "file_rig", "h20", "multi"}

    def _concurrency_tests(self):
        import ast

        for path in sorted((PROJECT_ROOT / "tests").glob("test_workspace*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                for fn in [n for n in cls.body
                           if isinstance(n, ast.FunctionDef)]:
                    src = ast.unparse(fn)
                    if ("threading.Thread" in src
                            or "ThreadPoolExecutor" in src):
                        params = {a.arg for a in fn.args.args}
                        yield path.name, cls.name, fn.name, params

    def test_no_in_memory_fixture_in_concurrency_tests(self):
        offenders = [
            f"{f}::{c}.{n} (fixtures={sorted(p)})"
            for f, c, n, p in self._concurrency_tests()
            if p & self.IN_MEMORY_FIXTURES
        ]
        assert not offenders, (
            "并发测试用了**内存库** fixture —— 多线程共享一个 `Database` "
            "会破坏事务边界（技术债 #2），表现为「单独跑过、全量偶发失败」：\n  "
            + "\n  ".join(offenders)
        )

    def test_file_fixtures_are_actually_file_based(self):
        """文件库 fixture 必须真的用文件路径（不是 `:memory:`）。"""
        src = (PROJECT_ROOT / "tests" / "workspace_harness.py").read_text(
            encoding="utf-8")
        assert "on_file" in src and "Database(self.db_path)" in src, (
            "文件库 fixture 必须给每个线程开**自己的连接**"
        )
        # 反向验证：内存模式必须被标注为不安全
        assert "不安全" in src or "unsafe" in src.lower(), (
            "内存模式必须明确标注「不能用于多线程」"
        )
