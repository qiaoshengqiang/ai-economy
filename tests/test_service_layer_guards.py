"""
Test: Task 应用层架构守卫（Task Application Layer Refactor）
============================================================

这些测试不检查功能，检查的是**重构之后边界有没有守住**。

【为什么重构之后更需要守卫】

    重构的收益是「职责分开了」。但如果没有东西盯着，
    下一个功能（比如 Step 4D 的 SUBMIT）很容易又被塞回门面，
    于是几个月后 TaskService 又变成 God Object，
    这次重构就白做了。

    所以这一组守卫锁住三件事：

        1. 门面**保持薄**（有行数上限，防止重新膨胀）
        2. 各模块**职责不越界**（谁该 import 什么）
        3. **扩展点真的存在**（加消息类型不需要改门面）

【守卫清单】

    薄门面
        · TaskService 的纯代码行数有上限
        · 门面里不许出现业务实现细节（_accept_locally 之类）
        · 门面不直接 import TaskAgreementRecord

    无 God Module
        · 每个模块的纯代码行数有上限
        · 没有模块同时承担「解析协议」和「业务规则」

    职责边界
        · Mapper 不认识 Task / TaskStore
        · Result 是叶子（不依赖任何 services 模块）
        · Negotiation 不依赖 Handlers（方向不能反）
        · Handlers 不直接碰数据库

    扩展点（最重要）
        · 注册一个新 Handler 就能支持新消息类型，门面一行都不用改

    API 兼容
        · 原有公开方法都还在

    不提前实现 4D
        · services/ 里没有 SUBMIT / VERIFY / START 的真实业务实现
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = PROJECT_ROOT / "services"
CORE_DIR = PROJECT_ROOT / "core"


# ============================================================
# 工具
# ============================================================
def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> list:
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def code_only(path: Path) -> str:
    """
    纯代码：去掉文档字符串，再 ast.unparse（注释也一并没有）。

    【为什么必须去掉文档字符串】
        文档里会**合法地**提到 "SubmitHandler"、"payment"、"llm"
        来解释「未来怎么加」或「刻意不做」。

        如果连它们一起检查，守卫会被自己的说明文字绊倒 ——
        这一路已经被绊倒五次了：
            seen_tracker / "ack"
            offer_amounts / "mount"
            is_open / "open"
            agent_directory / "agent_d"
            文档里的 SubmitHandler 示例        ← 本次

        规则很简单：**守卫只看代码**。

    【注意 ast.unparse 本身不会去掉文档字符串】
        文档字符串在 AST 里是一个普通的表达式节点，
        unparse 会把它原样输出。必须先手动删掉那个节点。
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


def code_line_count(path: Path) -> int:
    """
    纯代码行数：去掉注释、空行、文档字符串。

    【为什么用这个指标而不是总行数】
        这个项目的注释比代码还多（有意为之）。
        用总行数会奖励「少写注释」，那是错的方向。
        纯代码行数才反映「这个文件承担了多少逻辑」。
    """
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


# ============================================================
# 夹具（这些在别的测试文件里也有，但守卫文件要自给自足）
# ============================================================
@pytest.fixture
def directory():
    from core.agent_directory import LocalAgentDirectory

    d = LocalAgentDirectory()
    d.register_agent("agent_a", ["task_decomposition"])
    d.register_agent("agent_b", ["python_coding"])
    return d


@pytest.fixture
def policies():
    from core.decision_policy import RuleBasedDecisionPolicy

    return {
        "agent_a": RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=40, walk_away_price=100, max_rounds=2
        ),
        "agent_b": RuleBasedDecisionPolicy(
            accept_price=50, opening_ask=60, walk_away_price=25, max_rounds=2
        ),
    }


SERVICE_MODULES = (
    "task_result.py",
    "task_message_mapper.py",
    "task_negotiation_service.py",
    "task_handlers.py",
    "task_service.py",
)


def service_path(name: str) -> Path:
    return SERVICES_DIR / name


# ============================================================
# 1. 门面必须保持薄
# ============================================================
class TestFacadeStaysThin:
    """
    重构前 TaskService 是 1453 行、1174 行方法体、19 项职责。
    重构后它必须**明显变薄**，而且要有个东西防止它重新膨胀。
    """

    #: 门面的纯代码行数上限。
    #: 现在 158 行 —— 给 250 的余量，够加几个转发方法，
    #: 但绝对装不下「再塞回一个业务实现」。
    FACADE_CODE_LIMIT = 250

    def test_facade_is_thin(self):
        count = code_line_count(service_path("task_service.py"))

        assert count <= self.FACADE_CODE_LIMIT, (
            f"TaskService 门面的纯代码行数 {count} 超过上限 "
            f"{self.FACADE_CODE_LIMIT}。\n"
            f"这通常意味着又有业务实现被塞回了门面 —— "
            f"它应该放到 Handler / NegotiationService / Mapper 里。"
        )

    def test_facade_is_much_smaller_than_before(self):
        """相对重构前的 1453 行，门面必须显著变小。"""
        total = len(
            service_path("task_service.py").read_text(encoding="utf-8").splitlines()
        )

        assert total < 1453 * 0.5, (
            f"门面 {total} 行，没比重构前的 1453 行小多少 —— "
            f"这看起来像是「把代码剪切到另一个文件」而不是真正的拆分"
        )

    @pytest.mark.parametrize(
        "moved_away",
        [
            "_accept_locally",      # 成交落地 → negotiation service
            "_counter_locally",     # 我方还价 → negotiation service
            "_reject_locally",      # 我方拒绝 → negotiation service
            "_validate_response",   # 角色/轮次规则 → negotiation service
            "_already_recorded",    # 幂等 → negotiation service
            "_sync_trace",          # trace 缝合 → negotiation service
            "_money_from_payload",  # 金额解析 → mapper
        ],
    )
    def test_implementation_details_moved_out(self, moved_away):
        """
        这些实现细节应该已经搬出 `task_service.py`。

        守卫的是**源码文本**而不是 hasattr：
        因为它们本来就不该在门面里出现（哪怕是私有方法）。
        """
        source = service_path("task_service.py").read_text(encoding="utf-8")

        assert f"def {moved_away}" not in source, (
            f"task_service.py 里又出现了 {moved_away}() —— "
            f"它是业务实现细节，应该留在对应的服务模块里"
        )

    def test_facade_does_not_import_agreement(self):
        """
        成交凭证是议价服务的事，门面不该直接管它。

        （门面可以**读到** agreement_id —— 那是通过 HandlerResult.details。）
        """
        modules = imported_modules(parse(service_path("task_service.py")))

        assert not any("task_agreement" in m for m in modules), (
            "task_service.py 直接 import 了成交凭证 —— "
            "它应该由 TaskNegotiationService 负责"
        )


# ============================================================
# 2. 不许出现 God Module
# ============================================================
class TestNoGodModule:
    @pytest.mark.parametrize("name", SERVICE_MODULES)
    def test_no_module_is_too_large(self, name):
        """
        单个模块的纯代码行数上限。

        【阈值怎么定的】
            现在最大的模块（议价服务）是 507 行纯代码。
            上限设 700：给 4D 加业务留出余量，
            但一个 2000 行的 God Module 一定会被拦下来。
        """
        count = code_line_count(service_path(name))

        assert count <= 700, (
            f"services/{name} 的纯代码行数 {count} 超过 700。\n"
            f"如果它承担了不止一个职责，请按变化原因拆开。"
        )

    def test_total_is_reasonably_distributed(self):
        """
        最大的模块不该吃掉整个包。

        【防的是这种情况】
            门面变薄了，但 90% 的代码都堆进一个新文件 ——
            那只是搬家，不是重构。
        """
        counts = {name: code_line_count(service_path(name))
                  for name in SERVICE_MODULES}
        total = sum(counts.values())
        largest = max(counts.values())

        assert largest / total <= 0.55, (
            f"最大的模块占了整个 services 包的 "
            f"{largest / total:.0%}，分布太集中：{counts}"
        )

    def test_mapper_does_not_do_business_rules(self):
        """
        Mapper 只解析形状，不做业务判断。

        【怎么用守卫表达「不做业务判断」】
            业务判断需要领域知识 —— 所以 Mapper **不许认识 Task**。
            它连 Task 是什么都不知道，自然判不了「这个价格允不允许」。
        """
        modules = imported_modules(parse(service_path("task_message_mapper.py")))

        for forbidden in ("core.task", "core.task_store", "core.task_agreement"):
            assert not any(m.startswith(forbidden) for m in modules), (
                f"task_message_mapper.py 导入了 {forbidden} —— "
                f"Mapper 只做协议形状解析，不碰领域对象"
            )


# ============================================================
# 3. 职责边界（import 方向）
# ============================================================
class TestResponsibilityBoundaries:
    def test_result_is_a_leaf(self):
        """
        HandlerResult 是叶子：谁都依赖它，它不依赖任何 services 模块。

        （如果它反过来依赖 handlers，就会出现循环导入。）
        """
        modules = imported_modules(parse(service_path("task_result.py")))

        for module in modules:
            assert not module.startswith("services"), (
                f"task_result.py 导入了 {module} —— 它必须是叶子"
            )

    def test_mapper_is_a_leaf_within_services(self):
        """Mapper 只依赖 core.money 和 communication.message。"""
        modules = imported_modules(parse(service_path("task_message_mapper.py")))

        for module in modules:
            assert not module.startswith("services"), (
                f"task_message_mapper.py 导入了 {module} —— 它不该依赖别的服务"
            )

    def test_negotiation_does_not_depend_on_handlers(self):
        """
        【方向不能反】

            handlers → negotiation   ✅（handler 用协商服务落地状态）
            negotiation → handlers   ❌（会形成循环）

        """
        modules = imported_modules(
            parse(service_path("task_negotiation_service.py"))
        )

        assert not any("task_handlers" in m for m in modules), (
            "task_negotiation_service.py 导入了 handlers —— 依赖方向反了"
        )

    def test_handlers_do_not_touch_the_database_directly(self):
        """
        Handler 不许直接操作数据库。

        它只能通过 TaskNegotiationService → TaskStore 这条路。
        """
        source = service_path("task_handlers.py").read_text(encoding="utf-8")

        for forbidden in ("sqlite3", "INSERT ", "UPDATE ", "DELETE ",
                          "db.execute", "db.transaction"):
            assert forbidden not in source, (
                f"task_handlers.py 里出现了 {forbidden!r} —— "
                f"持久化是 TaskStore 的事"
            )

    def test_only_negotiation_service_imports_task_store(self):
        """
        services 里只有议价服务需要 TaskStore 类型。

        （门面持有 store 并把它传下去，所以它也 import —— 这是允许的。
          但 Mapper / Result / Handlers 都不该碰它。）
        """
        for name in ("task_result.py", "task_message_mapper.py",
                     "task_handlers.py"):
            modules = imported_modules(parse(service_path(name)))
            assert not any("task_store" in m for m in modules), (
                f"{name} 导入了 TaskStore —— 它不该关心持久化"
            )

    def test_core_does_not_depend_on_services(self):
        """
        最根本的一条：core 不能反向依赖 services。

        一旦反过来，「领域模型」就被应用层污染了。
        """
        violations = []
        for source in sorted(CORE_DIR.rglob("*.py")):
            for module in imported_modules(parse(source)):
                if module.startswith("services"):
                    violations.append(f"{source.name} → {module}")

        assert not violations, (
            "core/ 依赖了 services/（方向反了）：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_circular_imports_inside_services(self):
        """
        services 包内部不能有环。

        用「谁 import 谁」建一张图，然后检查它是不是 DAG。
        """
        edges = {}
        for name in SERVICE_MODULES:
            deps = set()
            for module in imported_modules(parse(service_path(name))):
                if module.startswith("services."):
                    deps.add(module.split(".", 1)[1] + ".py")
            edges[name] = deps

        # 拓扑排序：能排完就没有环
        remaining = dict(edges)
        resolved = set()
        while remaining:
            ready = [n for n, deps in remaining.items()
                     if deps <= resolved]
            if not ready:
                raise AssertionError(
                    f"services 内部存在循环依赖：{remaining}"
                )
            for name in ready:
                resolved.add(name)
                del remaining[name]


# ============================================================
# 4. 扩展点：加消息类型不需要改门面（最重要的一组）
# ============================================================
class TestExtensionPoint:
    """
    【本次重构最重要的验收点】

        Step 4D 要加 START / SUBMIT / VERIFY / COMPLETE。
        如果加一个类型就要动 TaskService，那这次重构就白做了。

    下面这一组**现场验证**扩展点是真的：
    定义一个假的 Handler，注册进去，门面一行都不用改。
    """

    def test_registry_accepts_a_new_handler(self, db, directory, policies):
        """注册一个新 Handler 就能支持一个新消息类型。"""
        from core.task_store import TaskStore

        from communication import Message, MessageType

        from services import (
            HandlerResult,
            TaskHandlerRegistry,
            TaskMessageHandler,
            TaskNegotiationService,
            TaskService,
        )

        class _RoutingProbe(TaskMessageHandler):
            """
            只用来说明「路由能不能通」，**不实现任何 4D 业务**。

            它不碰任务状态、不写数据库、不做验收判断 ——
            只是记下「我被调用了」然后返回一个固定结果。
            """

            message_type = "SUBMIT"

            def __init__(self, negotiation):
                super().__init__(negotiation)
                self.calls = []

            def handle(self, message):
                self.calls.append(message.message_id)
                return HandlerResult(
                    ok=True, action="probe", task_id="T-PROBE"
                )

        negotiation = TaskNegotiationService(TaskStore(db), policies)
        probe = _RoutingProbe(negotiation)

        registry = TaskHandlerRegistry()
        registry.register(probe)

        service = TaskService(
            store=TaskStore(db), directory=directory, policies=policies,
            handlers=registry,
        )

        message = Message(
            from_agent="agent_a", to_agent="agent_b",
            message_type="SUBMIT", payload={"task_id": "T-PROBE"},
        )
        result = service.handle_message(message)

        assert result.ok is True
        assert result.action == "probe"
        assert probe.calls == [message.message_id]

    def test_default_registry_covers_the_four_4c_types(self, db, policies):
        """默认注册表包含 4C 的四种消息类型 —— 一个不多一个不少。"""
        from core.task_store import TaskStore

        from services import TaskNegotiationService, default_handler_registry

        registry = default_handler_registry(
            TaskNegotiationService(TaskStore(db), policies)
        )

        assert registry.types() == [
            "ACCEPT", "COUNTER_OFFER", "REJECT", "TASK_OFFER"
        ]

    def test_unknown_message_type_is_loud(self, db, directory, policies):
        """
        没注册的类型必须**明确报错**，不能静默忽略。

        静默忽略会让「消息发出去了但没人处理」变成查不出的 bug。
        """
        from core.task_store import TaskStore

        from communication import Message

        from services import TaskService

        service = TaskService(
            store=TaskStore(db), directory=directory, policies=policies
        )
        message = Message(
            from_agent="a", to_agent="b",
            message_type="完全没注册的类型", payload={},
        )

        with pytest.raises(KeyError, match="没有处理"):
            service.handle_message(message)

    def test_registry_rejects_duplicate_without_replace(self, db, policies):
        """重复注册同一类型要报错（除非显式 replace）—— 避免悄悄覆盖。"""
        from core.task_store import TaskStore

        from services import (
            HandlerResult,
            OfferHandler,
            TaskHandlerRegistry,
            TaskMessageHandler,
            TaskNegotiationService,
        )

        class _Fake(TaskMessageHandler):
            message_type = "TASK_OFFER"

            def handle(self, message):
                return HandlerResult(ok=True, action="fake")

        negotiation = TaskNegotiationService(TaskStore(db), policies)
        registry = TaskHandlerRegistry([OfferHandler(negotiation)])

        with pytest.raises(ValueError):
            registry.register(_Fake(negotiation))

    def test_handler_without_message_type_rejected(self, db, policies):
        from core.task_store import TaskStore

        from services import (
            HandlerResult,
            TaskHandlerRegistry,
            TaskMessageHandler,
            TaskNegotiationService,
        )

        class _NoType(TaskMessageHandler):
            def handle(self, message):
                return HandlerResult(ok=True, action="x")

        negotiation = TaskNegotiationService(TaskStore(db), policies)

        with pytest.raises(ValueError):
            TaskHandlerRegistry([_NoType(negotiation)])

    def test_facade_does_not_hardcode_the_message_types(self):
        """
        门面里不该出现具体消息类型的字符串 ——
        否则加类型时又得改门面。

        （转发方法 `handle_offer` 里会写 "TASK_OFFER" 作为注册表键，
          那是**兼容入口**，不是路由逻辑。所以这里只禁止 if/elif 分支。）
        """
        source = service_path("task_service.py").read_text(encoding="utf-8")

        assert "elif message.message_type" not in source
        assert "if message.message_type ==" not in source


# ============================================================
# 5. API 兼容
# ============================================================
class TestApiCompatibility:
    """
    重构不能悄悄破坏公开接口。

    下面这些方法在重构前就存在，调用方（main.py、测试）
    依赖它们 —— 必须原样保留。
    """

    @pytest.mark.parametrize(
        "method",
        [
            # Step 4B：委派侧
            "candidates_for",
            "can_be_done_by",
            "explain_candidates",
            # Step 4B：接收侧
            "handle_offer",
            "handle_counter_offer",
            # Step 4C：接收侧
            "handle_accept",
            "handle_reject",
            # Step 4C：决策
            "policy_for",
            "build_context",
            "decide",
            "respond",
            "execute",
            # 杂项
            "describe",
        ],
    )
    def test_public_method_still_exists(self, method):
        from services import TaskService

        assert hasattr(TaskService, method), (
            f"重构后 TaskService 少了公开方法 {method}() —— "
            f"这会破坏调用方"
        )
        assert callable(getattr(TaskService, method))

    def test_policies_is_still_a_mutable_dict(self):
        """
        组合根会临时替换某个 Agent 的策略：

            task_service.policies["agent_b"] = ...

        所以它必须仍然是一个**可变的**、和内部共享的字典。
        """
        from core.task_store import TaskStore

        from storage.database import create_memory_database

        from services import TaskService

        db = create_memory_database()
        service = TaskService(store=TaskStore(db), policies={"agent_x": None})

        assert service.policies["agent_x"] is None
        service.policies["agent_y"] = None          # 必须可写
        assert "agent_y" in service.policies

        db.close()

    def test_handler_result_importable_from_both_places(self):
        """HandlerResult 仍然能从两个历史位置导入。"""
        from services import HandlerResult as from_package
        from services.task_service import HandlerResult as from_module

        assert from_package is from_module

    def test_task_service_still_accepts_original_constructor_args(self, db):
        """原来的构造方式仍然可用（store / directory / policies）。"""
        from core.task_store import TaskStore

        from services import TaskService

        service = TaskService(store=TaskStore(db))
        assert service.store is not None
        assert service.directory is None

    def test_handler_result_shape_unchanged(self):
        """HandlerResult 的字段一个都不能少（调用方按名字取）。"""
        from dataclasses import fields

        from services import HandlerResult

        names = {f.name for f in fields(HandlerResult)}

        assert names == {
            "ok", "action", "task_id", "errors", "warnings",
            "reply_type", "reply_payload", "reply_message", "details",
        }


# ============================================================
# 6. 不许提前实现 4D
# ============================================================
class TestStep4DIsNotImplemented:
    @pytest.mark.parametrize(
        "forbidden",
        ["SubmitHandler", "VerifyHandler", "StartHandler", "CompleteHandler",
         "submit_task", "verify_task", "run_tests", "execute_tests",
         "auto_verify"],
    )
    def test_no_4d_handlers(self, forbidden):
        """
        本次是重构，不实现 4D。

        【看的是纯代码，不是原文】
            文档里会**合法地**举例子说明扩展点：
                registry.register(SubmitHandler(negotiation))
            那是在解释「未来怎么加」，不是实现它。
            守卫必须只看代码，否则会被自己的说明文字绊倒。
        """
        code = code_only(service_path("task_handlers.py"))

        assert forbidden not in code, (
            f"task_handlers.py 的代码里出现了 {forbidden!r} —— 属于 Step 4D"
        )

    @pytest.mark.parametrize(
        "forbidden",
        # 【注意这里没有裸的 payment / wallet / ledger】
        #     因为成交结果里有一句**故意写的**标记：
        #         "payment": None, "workspace": None, "llm_calls": 0
        #     那是明确声明「这些都没有发生」，属于正确用法。
        #     所以只禁止真正实现它们的符号。
        ["escrow", "Escrow", "PaymentService", "create_payment",
         "settle_payment", "WalletRepository", "transfer(",
         "worktree", "GitWorktree", "WorkspacePermission",
         "TaskManager", "Coordinator", "Scheduler",
         "subprocess", "socket", "redis", "kafka"],
    )
    def test_no_forbidden_concepts(self, forbidden):
        """重构不许顺手引入后续步骤的概念。"""
        for name in SERVICE_MODULES:
            code = code_only(service_path(name))
            assert forbidden not in code, (
                f"services/{name} 的代码里出现了 {forbidden!r}"
            )

    def test_no_new_message_types_wired(self):
        """没有新增任何消息类型的处理器（4C 是四个，一个不多）。"""
        from core.task_store import TaskStore

        from storage.database import create_memory_database

        from services import TaskNegotiationService, default_handler_registry

        db = create_memory_database()
        registry = default_handler_registry(TaskNegotiationService(TaskStore(db)))

        assert set(registry.types()) == {
            "TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT"
        }
        db.close()
