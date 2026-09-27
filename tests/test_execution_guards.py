"""
Test: Step 4D 架构守卫
============================================================

这一组守卫锁住 Step 4D 的十条边界。

【为什么 4D 特别需要守卫】

    4D 引入的四种消息（START / SUBMIT / VERIFY / COMPLETE）
    看起来都很像「让系统多做点事」的邀请：

        SUBMIT → 要不要顺手跑个测试？
        VERIFY → 要不要顺手问模型「这份成果行不行」？
        COMPLETE → 要不要顺手把钱结了？

    这三件事**每一件都不属于 4D**。而且它们都是「顺手加上去很自然」的那种，
    所以必须用守卫把门堵死，而不是靠记性。

【守卫清单】

    Guard 1   TaskService 不能重新出现大量业务代码
    Guard 2   TaskNegotiationService 不能承担 4D Execution
    Guard 3   TaskExecutionService 不得依赖 Handler（避免循环依赖）
    Guard 4   Handler 不得直接调用 sqlite / filesystem
    Guard 5   4D 不得引入 subprocess
    Guard 6   4D 不得修改 Wallet / Ledger
    Guard 7   VERIFY 不得隐式调用 LLM
    Guard 8   TaskWorkspaceRef 不得获得文件 I/O 能力
    Guard 9   默认注册表包含 4C + 4D 八种消息类型
    Guard 10  TaskService 的路由仍然通过 Registry，而不是硬编码分支
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = PROJECT_ROOT / "services"
CORE_DIR = PROJECT_ROOT / "core"

EXECUTION_FILE = SERVICES_DIR / "task_execution_service.py"
NEGOTIATION_FILE = SERVICES_DIR / "task_negotiation_service.py"
FACADE_FILE = SERVICES_DIR / "task_service.py"
HANDLERS_FILE = SERVICES_DIR / "task_handlers.py"


# ============================================================
# 工具
# ============================================================
@pytest.fixture
def policies():
    """一份最小可用的议价策略（注册表构造需要它）。"""
    from core.decision_policy import RuleBasedDecisionPolicy

    return {
        "agent_a": RuleBasedDecisionPolicy(accept_price=50),
        "agent_b": RuleBasedDecisionPolicy(accept_price=50),
    }


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def code_only(path: Path) -> str:
    """
    纯代码：去掉文档字符串，再 ast.unparse（注释也一并没有）。

    【为什么必须去掉文档字符串】
        文档里会合法地解释「我们**不做**什么」：

            ❌ 不执行代码（没有 subprocess）
            ❌ 不调 LLM

        守卫只看代码，否则会被自己的说明文字绊倒。
        （这个坑这一路踩过五次了。）
    """
    tree = parse(path)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:]
    return ast.unparse(tree)


def imported_modules(path: Path) -> list:
    modules = []
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def code_line_count(path: Path) -> int:
    """纯代码行数（去掉注释、空行、文档字符串）。"""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                value = body[0].value
                for number in range(value.lineno, (value.end_lineno or value.lineno) + 1):
                    doc_lines.add(number)

    kept = [line for index, line in enumerate(lines, 1) if index not in doc_lines]
    return sum(
        1 for line in kept if line.strip() and not line.strip().startswith("#")
    )


def methods_of(path: Path, class_name: str) -> set:
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


# ============================================================
# Guard 1：TaskService 不能重新膨胀
# ============================================================
class TestGuard1FacadeStaysThin:
    """
    【加四种消息之后，门面变胖了吗？】

        这是 4D 最容易出错的地方：
        四个新消息各自的业务很容易被「顺手」写进门面。

        所以除了行数上限，还要检查门面里
        **没有**任何执行阶段的业务实现。
    """

    #: 4D 之后门面的纯代码行数上限。
    #: 重构后是 158 行，4D 加了四个兼容转发方法（每个 2 行）→ 约 170。
    #: 上限 250 给了一点点余量，但装不下任何业务实现。
    FACADE_CODE_LIMIT = 250

    def test_facade_did_not_bloat(self):
        count = code_line_count(FACADE_FILE)

        assert count <= self.FACADE_CODE_LIMIT, (
            f"TaskService 门面的纯代码行数 {count} 超过上限 "
            f"{self.FACADE_CODE_LIMIT}。\n"
            f"4D 加了四种消息 —— 如果门面因此长了上百行，说明架构做错了："
            f"业务应该在 Handler / ExecutionService 里。"
        )

    @pytest.mark.parametrize(
        "forbidden",
        [
            "def apply_start", "def apply_submit",
            "def apply_verify", "def apply_complete",
            "def validate_role", "def validate_state",
            "def record_execution",
        ],
    )
    def test_execution_logic_is_not_in_the_facade(self, forbidden):
        """执行阶段的业务方法一个都不许出现在门面里。"""
        source = FACADE_FILE.read_text(encoding="utf-8")

        assert forbidden not in source, (
            f"task_service.py 里出现了 {forbidden!r} —— "
            f"执行业务属于 TaskExecutionService"
        )

    def test_facade_does_not_import_task_execution_domain(self):
        """
        门面不该直接碰执行事件 / 成果引用这些领域类型 ——
        它只转发，不落地。
        """
        modules = imported_modules(FACADE_FILE)

        assert not any("core.task_execution" in m for m in modules), (
            "task_service.py 导入了 core.task_execution —— "
            "执行领域类型应该由 TaskExecutionService 使用"
        )

    def test_facade_added_only_forwarding_methods(self):
        """
        4D 给门面加的方法必须是「一行转发」。

        检查方式：这四个方法的函数体里
        不许出现任何状态判断 / 校验 / 直接改 store。
        """
        tree = parse(FACADE_FILE)
        found = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TaskService":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name in (
                        "handle_start", "handle_submit",
                        "handle_verify", "handle_complete",
                    ):
                        found[item.name] = item

        assert set(found) == {
            "handle_start", "handle_submit", "handle_verify", "handle_complete"
        }, "门面缺少执行阶段的兼容入口"

        for name, func in found.items():
            body = [n for n in func.body if not isinstance(n, ast.Expr)]
            assert len(body) == 1, (
                f"{name}() 的函数体有 {len(body)} 条语句 —— "
                f"兼容入口应该只有一条 return，业务在 Handler 里"
            )
            assert isinstance(body[0], ast.Return)


# ============================================================
# Guard 2：议价服务不得吸收执行
# ============================================================
class TestGuard2NegotiationStaysNegotiation:
    """
    【为什么这条特别重要】

        TaskNegotiationService 已经有 apply_accept()。
        往里再加 apply_start / apply_submit / apply_verify / apply_complete
        是「最省事」的做法 —— 但也是最错的做法：

            议价怎么谈  → 会随着 Agent 的智能程度不断演化
            执行怎么走  → 由协议和状态机固定

        两者变化原因完全不同，混在一起就会
        「想改议价策略时得动执行代码」。
    """

    @pytest.mark.parametrize(
        "forbidden",
        ["apply_start", "apply_submit", "apply_verify", "apply_complete",
         "start_task", "submit_task", "verify_task", "complete_task"],
    )
    def test_negotiation_has_no_execution_methods(self, forbidden):
        methods = methods_of(NEGOTIATION_FILE, "TaskNegotiationService")

        assert forbidden not in methods, (
            f"TaskNegotiationService 上出现了 {forbidden}() —— "
            f"执行业务属于 TaskExecutionService"
        )

    def test_negotiation_does_not_import_execution_service(self):
        modules = imported_modules(NEGOTIATION_FILE)

        assert not any("task_execution" in m for m in modules), (
            "task_negotiation_service.py 导入了执行相关模块 —— "
            "议价和执行必须互相不知道"
        )

    def test_negotiation_does_not_import_execution_domain(self):
        modules = imported_modules(NEGOTIATION_FILE)

        assert not any("core.task_execution" in m for m in modules), (
            "议价服务导入了执行领域类型 —— 两个阶段应该分开"
        )

    def test_negotiation_still_owns_negotiation(self):
        """反过来：议价的方法必须还在（不能为了分家把议价搬走）。"""
        methods = methods_of(NEGOTIATION_FILE, "TaskNegotiationService")

        for expected in ("apply_counter_offer", "apply_accept", "apply_reject",
                         "counter_locally", "reject_locally", "decide"):
            assert expected in methods, (
                f"TaskNegotiationService 少了 {expected}() —— 议价职责被搬走了"
            )

    def test_execution_service_does_not_do_negotiation(self):
        """执行服务也不许碰议价。"""
        modules = imported_modules(EXECUTION_FILE)

        assert not any("task_negotiation" in m for m in modules), (
            "task_execution_service.py 导入了议价服务 —— 方向不该存在"
        )

        methods = methods_of(EXECUTION_FILE, "TaskExecutionService")
        for forbidden in ("apply_accept", "apply_counter_offer", "decide",
                          "negotiate", "counter_locally"):
            assert forbidden not in methods, (
                f"TaskExecutionService 上出现了 {forbidden}() —— 议价不是它的事"
            )


# ============================================================
# Guard 3：执行服务不得依赖 Handler
# ============================================================
class TestGuard3NoCircularDependency:
    def test_execution_service_does_not_import_handlers(self):
        """
            handlers → execution   ✅
            execution → handlers   ❌（会成环）
        """
        modules = imported_modules(EXECUTION_FILE)

        assert not any("task_handlers" in m for m in modules), (
            "task_execution_service.py 导入了 handlers —— 依赖方向反了，会成环"
        )

    def test_services_package_has_no_cycles(self):
        """整个 services 包的 import 图必须是有向无环的。"""
        modules = sorted(path.name for path in SERVICES_DIR.glob("*.py")
                         if path.name != "__init__.py")

        edges = {}
        for name in modules:
            deps = set()
            for module in imported_modules(SERVICES_DIR / name):
                if module.startswith("services."):
                    deps.add(module.split(".", 1)[1] + ".py")
            edges[name] = deps

        remaining = dict(edges)
        resolved = set()
        while remaining:
            ready = [n for n, deps in remaining.items() if deps <= resolved]
            if not ready:
                raise AssertionError(f"services 内部存在循环依赖：{remaining}")
            for name in ready:
                resolved.add(name)
                del remaining[name]

    def test_application_base_does_not_know_subclasses(self):
        """基类不许认识任何具体应用服务。"""
        modules = imported_modules(SERVICES_DIR / "task_application_service.py")

        for forbidden in ("task_negotiation", "task_execution", "task_handlers"):
            assert not any(forbidden in m for m in modules), (
                f"task_application_service.py 导入了 {forbidden} —— "
                f"基类不该认识子类"
            )


# ============================================================
# Guard 4：Handler 不得直接调用 sqlite / filesystem
# ============================================================
class TestGuard4HandlersDoNotTouchStorage:
    @pytest.mark.parametrize(
        "forbidden",
        ["sqlite3", "INSERT ", "UPDATE ", "DELETE ", "SELECT ",
         "db.execute", "db.transaction", "conn.execute",
         "open(", "read_text", "write_text", "Path("],
    )
    def test_handlers_do_not_touch_storage_or_filesystem(self, forbidden):
        code = code_only(HANDLERS_FILE)

        assert forbidden not in code, (
            f"task_handlers.py 的代码里出现了 {forbidden!r} —— "
            f"Handler 只能通过 Service → TaskStore 这一条路"
        )

    def test_execution_service_does_not_use_sqlite_directly(self):
        modules = imported_modules(EXECUTION_FILE)
        assert not any(m.startswith("sqlite3") for m in modules)
        assert not any("storage" in m for m in modules)

        code = code_only(EXECUTION_FILE)
        for forbidden in ("sqlite3", "INSERT ", "SELECT ", "UPDATE "):
            assert forbidden not in code, (
                f"task_execution_service.py 里出现了 {forbidden!r} —— "
                f"持久化是 TaskStore 的事"
            )

    def test_execution_service_uses_store_api(self):
        """它应该只调用 store 的公开方法。"""
        code = code_only(EXECUTION_FILE)

        assert "self.store." in code, "执行服务应该通过 TaskStore 持久化"


# ============================================================
# Guard 5：4D 不得引入 subprocess
# ============================================================
class TestGuard5NoCodeExecution:
    @pytest.mark.parametrize(
        "forbidden",
        ["subprocess", "Popen", "os.system", "os.exec", "shutil.which",
         "docker", "sandbox", "run_tests", "pytest.main", "compile(",
         "eval(", "exec("],
    )
    def test_no_code_execution_anywhere_in_services(self, forbidden):
        """
        【4D 的「执行」是任务生命周期，不是真的跑代码】

            这个区别极其容易被误解 ——
            所以用守卫把「真的执行」的入口全部封掉。
        """
        for path in sorted(SERVICES_DIR.glob("*.py")):
            code = code_only(path)
            assert forbidden not in code, (
                f"services/{path.name} 里出现了 {forbidden!r} —— "
                f"4D 不执行代码"
            )

    @pytest.mark.parametrize(
        "forbidden",
        ["subprocess", "os.system", "Popen"],
    )
    def test_no_code_execution_in_core(self, forbidden):
        for path in sorted(CORE_DIR.glob("*.py")):
            code = code_only(path)
            assert forbidden not in code, (
                f"core/{path.name} 里出现了 {forbidden!r}"
            )


# ============================================================
# Guard 6：4D 不得修改 Wallet / Ledger
# ============================================================
class TestGuard6NoEconomyChanges:
    def test_execution_service_does_not_depend_on_economy(self):
        """
        【「活干完了」和「钱给了」是两件事】

            4D 只做前者。结算是一个独立的后续阶段。
            真的碰钱必然要先依赖经济系统 —— 所以看依赖方向。
        """
        for path in (EXECUTION_FILE, HANDLERS_FILE,
                     SERVICES_DIR / "task_application_service.py"):
            for module in imported_modules(path):
                for forbidden in ("core.wallet", "core.ledger", "core.economy"):
                    assert not (module == forbidden
                                or module.startswith(forbidden + ".")), (
                        f"{path.name} 导入了 {module} —— "
                        f"4D 不产生任何经济后果"
                    )

    @pytest.mark.parametrize(
        "forbidden",
        ["transfer(", "settle(", "escrow", "debit(", "credit(", "mint("],
    )
    def test_no_settlement_calls(self, forbidden):
        for path in sorted(SERVICES_DIR.glob("*.py")):
            code = code_only(path)
            assert forbidden not in code, (
                f"services/{path.name} 里出现了 {forbidden!r} —— 结算不是 4D 的事"
            )

    def test_full_lifecycle_does_not_change_wallet(self, db, economy,
                                                   funded_agents):
        """
        【这是本 Guard 最重要的一条 —— 直接跑一遍看钱有没有动】

            完整 4D 流程（含返工）前后：
                Wallet A / Wallet B / Ledger 全部不变。
        """
        from core.money import Money
        from core.task import Task
        from core.task_store import TaskStore
        from core.task_execution import TaskResultRef, TaskVerification
        from communication import Message, MessageType
        from services import TaskExecutionService

        store = TaskStore(economy.db)
        execution = TaskExecutionService(store)

        ledger_before = economy.ledger.count()
        a_before = economy.agent("agent_a").balance
        b_before = economy.agent("agent_b").balance

        task = Task.create(task_id="T-GUARD", creator="agent_a",
                           description="d", budget=Money(100),
                           participants=["agent_a", "agent_b"])
        task.offer()
        task.accept(Money(25), contractor="agent_b")
        store.save(task)

        def m(sender, recipient, message_type):
            return Message(from_agent=sender, to_agent=recipient,
                           message_type=message_type,
                           payload={"task_id": "T-GUARD"})

        execution.apply_start(store.get("T-GUARD"),
                              incoming=m("agent_b", "agent_a", MessageType.START))
        execution.apply_submit(store.get("T-GUARD"),
                               TaskResultRef(result_ref="v1"),
                               incoming=m("agent_b", "agent_a", MessageType.SUBMIT))
        execution.apply_verify(store.get("T-GUARD"),
                               TaskVerification(passed=False, feedback="改"),
                               incoming=m("agent_a", "agent_b", MessageType.VERIFY))
        execution.apply_submit(store.get("T-GUARD"),
                               TaskResultRef(result_ref="v2"),
                               incoming=m("agent_b", "agent_a", MessageType.SUBMIT))
        execution.apply_verify(store.get("T-GUARD"),
                               TaskVerification(passed=True, feedback="过"),
                               incoming=m("agent_a", "agent_b", MessageType.VERIFY))
        execution.apply_complete(store.get("T-GUARD"),
                                 incoming=m("agent_a", "agent_b", MessageType.COMPLETE))

        assert economy.ledger.count() == ledger_before, "4D 流程动了账本"
        assert economy.agent("agent_a").balance == a_before, "4D 流程动了 A 的钱包"
        assert economy.agent("agent_b").balance == b_before, "4D 流程动了 B 的钱包"
        assert economy.audit()["balanced"] is True


# ============================================================
# Guard 7：VERIFY 不得隐式调用 LLM
# ============================================================
class TestGuard7VerifyDoesNotCallLLM:
    def test_execution_service_does_not_import_llm(self):
        """
        【不能「收到 SUBMIT 就自动问模型行不行」】

            第一版验收是**人工**的：passed 由发起方显式给出。
            LLM 验收是后续独立步骤，不是 4D。
        """
        for path in (EXECUTION_FILE, HANDLERS_FILE):
            for module in imported_modules(path):
                assert not module.startswith("llm"), (
                    f"{path.name} 导入了 {module} —— "
                    f"4D 的验收不许调用模型"
                )

    def test_no_llm_in_services_at_all(self):
        for path in sorted(SERVICES_DIR.glob("*.py")):
            for module in imported_modules(path):
                assert not module.startswith("llm"), (
                    f"services/{path.name} 导入了 {module}"
                )

    def test_verify_handler_uses_explicit_passed(self):
        """
        VERIFY 的结论必须来自消息里的 passed ——
        而不是任何「算出来」的判断。
        """
        code = code_only(HANDLERS_FILE)

        assert "parse_verify" in code, "VerifyHandler 应该用 Mapper 解析 passed"
        for forbidden in ("llm", "model", "brain", "infer", "judge"):
            assert forbidden not in code.lower(), (
                f"task_handlers.py 里出现了 {forbidden!r} —— "
                f"验收结论只能来自消息"
            )

    def test_verification_outcome_is_explicit(self):
        """TaskVerification 的结论只有两个出口，没有「模型判断」。"""
        from core.task_execution import TaskVerification

        assert TaskVerification(passed=True).describe()["outcome"] == "verified"
        assert TaskVerification(
            passed=False, feedback="改"
        ).describe()["outcome"] == "rework"


# ============================================================
# Guard 8：TaskWorkspaceRef 不得获得文件 I/O
# ============================================================
class TestGuard8WorkspaceStaysAReference:
    @pytest.mark.parametrize(
        "forbidden",
        ["open_file", "read_file", "write_file", "mkdir", "unlink",
         "listdir", "glob", "resolve", "exists", "is_dir", "is_file",
         "chmod", "grant", "authorize", "checkout", "clone"],
    )
    def test_workspace_ref_has_no_io(self, forbidden):
        """它只是一个 workspace_id —— 不是路径，不是权限。"""
        from core.task_workspace import TaskWorkspaceRef

        assert not hasattr(TaskWorkspaceRef, forbidden), (
            f"TaskWorkspaceRef 不该有 {forbidden!r} —— 它只是引用"
        )

    def test_workspace_module_has_no_filesystem_imports(self):
        modules = imported_modules(CORE_DIR / "task_workspace.py")

        for forbidden in ("os", "pathlib", "shutil", "subprocess", "io",
                          "tempfile", "glob"):
            assert not any(m == forbidden or m.startswith(forbidden + ".")
                           for m in modules), (
                f"core/task_workspace.py 导入了 {forbidden} —— "
                f"它不该有文件系统能力"
            )

    def test_workspace_ref_is_still_just_an_id(self):
        from core.task_workspace import TaskWorkspaceRef

        ref = TaskWorkspaceRef("WS-001")
        assert ref.workspace_id == "WS-001"
        assert not hasattr(ref, "path")

    def test_4d_does_not_create_workspace_tables(self, db):
        for forbidden in ("workspace_members", "workspace_permissions",
                          "workspace_files", "workspace_acl"):
            assert forbidden not in db.table_names(), (
                f"4D 不该创建 {forbidden} 表"
            )

    def test_result_ref_is_not_a_path_or_permission(self):
        """
        【4D 引入的新类型也要守这条边界】

            result_ref 是逻辑名字，不是路径、不是 URL、不是权限。
        """
        from core.task_execution import TaskResultRef

        info = TaskResultRef(result_ref="analysis-v1").describe()
        assert info["is_permission"] is False
        assert info["is_filesystem_path"] is False
        assert info["is_url"] is False

        for bad in ("/etc/passwd", "../x", "~/x", "https://x/y"):
            with pytest.raises(ValueError):
                TaskResultRef(result_ref=bad)


# ============================================================
# Guard 9：默认注册表包含八种消息类型
# ============================================================
class TestGuard9RegistryHasEightTypes:
    def test_default_registry_has_all_eight(self, db):
        from core.task_store import TaskStore
        from services import (
            TaskExecutionService, TaskNegotiationService,
            default_handler_registry,
        )

        store = TaskStore(db)
        registry = default_handler_registry(
            TaskNegotiationService(store), TaskExecutionService(store)
        )

        assert registry.types() == [
            "ACCEPT", "COMPLETE", "COUNTER_OFFER", "REJECT",
            "START", "SUBMIT", "TASK_OFFER", "VERIFY",
        ]

    def test_4c_types_are_preserved(self, db):
        """加了 4D 不许删掉 4C 的四种 —— 协议是 additive 的。"""
        from core.task_store import TaskStore
        from services import (
            TaskExecutionService, TaskNegotiationService,
            default_handler_registry,
        )

        store = TaskStore(db)
        registry = default_handler_registry(
            TaskNegotiationService(store), TaskExecutionService(store)
        )

        for message_type in ("TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT"):
            assert message_type in registry, f"{message_type} 被删掉了"

    def test_registry_backward_compatible(self, db):
        """
        只传 negotiation 仍然可用（4C 的调用方式）——
        这让已有的代码和测试不用改。
        """
        from core.task_store import TaskStore
        from services import TaskNegotiationService, default_handler_registry

        store = TaskStore(db)
        registry = default_handler_registry(TaskNegotiationService(store))

        assert registry.types() == [
            "ACCEPT", "COUNTER_OFFER", "REJECT", "TASK_OFFER"
        ]

    def test_protocol_version_is_still_1(self):
        """
        【4D 是 additive 变更，协议版本不升】

            只增加了新的消息类型，没有改任何已有类型的
            语义、字段或形状 —— 所以 v1 的通信双方仍然能互通。
        """
        from communication.message import PROTOCOL_VERSION

        assert PROTOCOL_VERSION == "1"

    def test_new_message_types_do_not_change_existing_ones(self):
        """
        新增 START / COMPLETE 不许影响 4C 已有的常量。
        """
        from communication.message import MessageType

        # 4C 的四个值必须一字不变
        assert MessageType.TASK_OFFER == "TASK_OFFER"
        assert MessageType.COUNTER_OFFER == "COUNTER_OFFER"
        assert MessageType.ACCEPT == "ACCEPT"
        assert MessageType.REJECT == "REJECT"
        # 3.6 预留的两个也没变
        assert MessageType.SUBMIT == "SUBMIT"
        assert MessageType.VERIFY == "VERIFY"
        # 4D 新增的
        assert MessageType.START == "START"
        assert MessageType.COMPLETE == "COMPLETE"


# ============================================================
# Guard 10：路由仍然通过 Registry
# ============================================================
class TestGuard10RoutingIsNotHardcoded:
    def test_facade_has_no_message_type_branches(self):
        """
        【不能用 if / elif 按消息类型分派】

            否则每加一个类型都要改门面，
            门面迟早又变成 God Object。
        """
        source = FACADE_FILE.read_text(encoding="utf-8")

        assert "if message.message_type ==" not in source
        assert "elif message.message_type" not in source
        assert "if message.message_type in" not in source

    def test_facade_routes_through_registry(self):
        code = code_only(FACADE_FILE)

        assert "self.handlers.require" in code or "self.handlers.get" in code, (
            "门面必须通过注册表路由"
        )

    def test_registry_is_the_only_dispatch_point(self):
        """
        整个 services 包里，只有两个地方可以提到 message_type：

            task_handlers.py        Handler 声明「我负责哪种类型」（声明，不是分派）
            task_message_mapper.py  协议层回答「这条消息在说什么」

        其它任何模块都不许按消息类型分支 ——
        那是分派，应该走注册表。
        """
        allowed = {"task_handlers.py", "task_message_mapper.py"}

        for path in sorted(SERVICES_DIR.glob("*.py")):
            if path.name in allowed:
                continue
            code = code_only(path)
            assert "message_type ==" not in code, (
                f"services/{path.name} 里出现了按 message_type 的分支 —— "
                f"分派应该只在注册表里"
            )

    @pytest.mark.parametrize(
        "message_type",
        ["START", "SUBMIT", "VERIFY", "COMPLETE"],
    )
    def test_每个4d类型都能被路由(self, message_type, db, policies):
        """注册表里真的能查到每一种 4D 消息的 handler。"""
        from core.task_store import TaskStore
        from services import (
            TaskExecutionService, TaskNegotiationService,
            default_handler_registry,
        )

        store = TaskStore(db)
        registry = default_handler_registry(
            TaskNegotiationService(store, policies), TaskExecutionService(store)
        )

        handler = registry.get(message_type)
        assert handler is not None, f"{message_type} 没有 handler"
        assert handler.message_type == message_type
        assert handler.event_type == message_type

    def test_handler_class_names_match_their_types(self, db, policies):
        from core.task_store import TaskStore
        from services import (
            TaskExecutionService, TaskNegotiationService,
            default_handler_registry,
        )

        store = TaskStore(db)
        registry = default_handler_registry(
            TaskNegotiationService(store, policies), TaskExecutionService(store)
        )

        expected = {
            "START": "StartHandler",
            "SUBMIT": "SubmitHandler",
            "VERIFY": "VerifyHandler",
            "COMPLETE": "CompleteHandler",
        }
        for message_type, class_name in expected.items():
            assert type(registry.get(message_type)).__name__ == class_name
