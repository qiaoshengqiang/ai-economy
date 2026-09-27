"""
Test: 议价层架构守卫（Step 4C）
============================================================

【这一组守卫在守什么】

    Step 4C 引入了 DecisionPolicy，它必须待在正确的位置：

        Agent
          ↓
        DecisionPolicy     ← 只做决定，没有任何副作用
          ↓
        TaskService        ← 校验 + 执行
          ↓
        Task Core          ← 改状态

    以及三条最重要的边界：

        1. 决策层**不调用 LLM**（否则谈判成本会高于任务价值）
        2. 策略**不认 Agent 的名字**（参数来自各自的配置）
        3. ACCEPT **不转账、不建 Workspace、不碰 Git**

【守卫清单】

    DecisionPolicy 不能依赖：
        llm · wallet · ledger · communication · workspace · 文件系统 · git

    Task Core 不能依赖：
        DecisionPolicy 的任何实现

    TaskService 可以连接 Communication 和 Task Core，
        但不能直接付款、不能创建 Workspace

    Communication 不能依赖：
        Task Core · DecisionPolicy · Economy
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = PROJECT_ROOT / "core"
SERVICES_DIR = PROJECT_ROOT / "services"
COMM_DIR = PROJECT_ROOT / "communication"

POLICY_FILE = CORE_DIR / "decision_policy.py"
AGREEMENT_FILE = CORE_DIR / "task_agreement.py"
#: 【重构后】services/ 现在是多个模块，守卫必须覆盖全部 ——
#: 只看 task_service.py 的话，把逻辑挪到别的文件就能绕过守卫。
SERVICE_FILES = (
    "task_service.py",
    "task_handlers.py",
    "task_negotiation_service.py",
    "task_message_mapper.py",
    "task_result.py",
)


def service_sources():
    return [SERVICES_DIR / name for name in SERVICE_FILES]


def all_service_code() -> str:
    """整个 services 包的纯代码（去文档字符串与注释）。"""
    return "\n".join(code_only(path) for path in service_sources())


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
    取纯代码：去掉文档字符串和注释。

    注释里会合法地提到 "llm"、"wallet"、"Workspace" 来解释为什么不用它们。
    守卫必须只看代码，否则会被自己的说明文字绊倒。
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


def name_segments(name: str) -> set:
    """按 snake_case / 驼峰切词 —— 子串匹配误报过太多次了。"""
    segments = set()
    for part in str(name).split("_"):
        segments.update(
            token.lower()
            for token in re.findall(r"[a-z0-9]+|[A-Z][a-z0-9]*", part)
        )
    return segments


def matches(name: str, words) -> bool:
    return bool(name_segments(name) & {w.lower() for w in words})


# ============================================================
# 1. DecisionPolicy 必须是纯的
# ============================================================
class TestDecisionPolicyIsPure:
    def test_no_llm_dependency(self):
        """
        【§3 / §25 的核心】

        不能让 Agent 每一轮谈判都去问大模型 ——
        谈判成本可能高于任务本身的价值。
        """
        for module in imported_modules(parse(POLICY_FILE)):
            assert not module.startswith("llm"), (
                f"core/decision_policy.py 导入了 {module}"
            )

    def test_no_wallet_ledger_communication(self):
        """策略不碰钱、不发消息。"""
        forbidden = ("core.wallet", "core.ledger", "core.economy",
                     "communication", "agents", "storage", "services")
        violations = []

        for module in imported_modules(parse(POLICY_FILE)):
            for prefix in forbidden:
                if module == prefix or module.startswith(prefix + "."):
                    violations.append(module)

        assert not violations, (
            "DecisionPolicy 依赖了不该依赖的模块：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_filesystem_or_git(self):
        code = code_only(POLICY_FILE)

        for forbidden in ("open(", "mkdir", "subprocess", "git ",
                          "read_text", "write_text", "requests", "urllib",
                          "socket"):
            assert forbidden not in code, (
                f"core/decision_policy.py 里出现了 {forbidden!r}"
            )

    def test_no_side_effect_methods(self):
        """
        策略上不该有「执行」「发送」「转账」「授权」这类方法。
        它只**返回**决策。
        """
        from core.decision_policy import DecisionPolicy, RuleBasedDecisionPolicy

        forbidden = {"apply", "execute", "mutate", "send", "transfer",
                     "grant", "save", "write", "commit", "pay", "settle"}

        for cls in (DecisionPolicy, RuleBasedDecisionPolicy):
            found = [
                name for name in dir(cls)
                if not name.startswith("_") and matches(name, forbidden)
            ]
            assert not found, f"{cls.__name__} 上有副作用方法：{found}"

    def test_only_stdlib_dependencies(self):
        """策略是纯计算 —— 只该依赖标准库和 Money。"""
        allowed_prefixes = ("__future__", "dataclasses", "typing", "abc",
                            "core.money")
        for module in imported_modules(parse(POLICY_FILE)):
            assert any(
                module == p or module.startswith(p + ".") for p in allowed_prefixes
            ), f"core/decision_policy.py 依赖了 {module}"

    def test_no_vendor_names(self):
        code = code_only(POLICY_FILE).lower()

        for vendor in ("deepseek", "doubao", "grok", "openai", "claude",
                       "qwen", "gemini"):
            assert vendor not in code


# ============================================================
# 2. 不许硬编码 Agent（§6 / §17 / §31）
# ============================================================
class TestNoHardcodedAgents:
    """
    §6 明确禁止：

        if agent_id == "agent_b":
            minimum_price = 20

    策略参数必须来自 Agent **自己的配置**。
    """

    @pytest.mark.parametrize(
        "path",
        ["core/decision_policy.py", "core/task_agreement.py",
         "services/task_service.py", "core/task.py"],
    )
    def test_no_agent_name_literals(self, path):
        """
        找的是**带引号的字面量**，比如 "agent_b"。

        【为什么不能直接搜子串】
            `agent_directory` 里含有 `agent_d`（agent_d + irectory）——
            这一路已经因为子串匹配误报四次了：
                seen_tracker / "ack"
                offer_amounts / "mount"
                is_open / "open"
                agent_directory / "agent_d"
            所以要找的是一个**完整的字符串字面量**。
        """
        code = code_only(PROJECT_ROOT / path)

        found = re.findall(r"""["']agent_[a-z]["']""", code)
        assert not found, (
            f"{path} 里硬编码了 Agent 名：{set(found)} —— "
            f"策略参数应该来自各自配置，判断应该靠任务字段"
        )

    @pytest.mark.parametrize(
        "forbidden",
        ["minimum_price_for", "price_for_agent", "policy_for_agent_b",
         "default_partner", "preferred_agent"],
    )
    def test_no_agent_specific_policy_hooks(self, forbidden):
        for path in ("core/decision_policy.py", "services/task_service.py"):
            code = code_only(PROJECT_ROOT / path)
            assert forbidden not in code, f"{path} 里出现了 {forbidden!r}"

    def test_service_layer_has_no_price_rules(self):
        """
        §31：不许把某个 Agent 的具体价格策略写进应用层。

        【重构后这条守卫覆盖整个 services/】
            原来只看 task_service.py —— 那么把逻辑挪到
            task_negotiation_service.py 就能绕过它。
            现在整个包一起扫。
        """
        for path in service_sources():
            code = code_only(path)
            for pattern, label in (
                (r"accept_price", "接受线"),
                (r"opening_ask", "开口价"),
                (r"walk_away", "底线"),
            ):
                assert not re.search(pattern, code), (
                    f"{path.name} 里出现了 {label}（{pattern}）—— "
                    f"价格策略属于 Agent 自己的 DecisionPolicy"
                )

    def test_service_layer_only_looks_policies_up(self):
        """应用层只能按 agent_id 查策略，不能自己造一个。"""
        for path in service_sources():
            code = code_only(path)
            assert "RuleBasedDecisionPolicy" not in code, (
                f"{path.name} 里出现了具体策略类 —— "
                f"它应该只依赖 DecisionPolicy 这个抽象"
            )


# ============================================================
# 3. Task Core 不依赖策略实现
# ============================================================
class TestTaskCoreIsIndependentOfPolicy:
    def test_task_does_not_import_policy(self):
        for module in imported_modules(parse(CORE_DIR / "task.py")):
            assert not module.startswith("core.decision_policy"), (
                f"core/task.py 导入了 {module} —— "
                f"Task 不该知道决策策略的存在"
            )

    def test_task_has_no_decision_methods(self):
        from core.task import Task

        forbidden = {"decide", "evaluate", "policy", "negotiate_price",
                     "should_accept", "should_reject"}

        found = [
            name for name in dir(Task)
            if not name.startswith("_") and matches(name, forbidden)
        ]
        assert not found, f"Task 上出现了决策类方法：{found}"

    def test_task_agreement_does_not_import_economy(self):
        """成交凭证不是账本 —— 它不该依赖钱包 / 账本。"""
        for module in imported_modules(parse(AGREEMENT_FILE)):
            for forbidden in ("core.wallet", "core.ledger", "core.economy",
                              "communication", "llm"):
                assert not module.startswith(forbidden), (
                    f"core/task_agreement.py 导入了 {module}"
                )

    def test_agreement_is_not_a_payment_record(self):
        from core.task_agreement import TaskAgreementRecord

        for forbidden in ("transfer", "pay", "settle", "wallet", "ledger",
                          "escrow", "debit", "credit"):
            assert not hasattr(TaskAgreementRecord, forbidden), (
                f"TaskAgreementRecord 不该有 {forbidden!r} —— "
                f"它是成交凭证，不是支付记录"
            )


# ============================================================
# 4. TaskService 的边界
# ============================================================
class TestTaskServiceBoundaries:
    def test_can_connect_communication_and_task(self):
        """
        应用层可以同时连通信和 Task Core —— 它就是适配层。

        【重构后】不是每一个模块都要同时连两边，
        而是**整个 services 包**合起来能连两边：
            task_handlers / task_service      → communication + core.task
            task_negotiation_service          → communication + core.task
            task_message_mapper               → communication（不碰 Task）
            task_result                       → communication
        """
        all_modules = []
        for path in service_sources():
            all_modules.extend(imported_modules(parse(path)))

        assert any(m.startswith("communication") for m in all_modules)
        assert any(m.startswith("core.task") for m in all_modules)

    def test_does_not_pay_or_grant(self):
        """但它不能直接付款、不能创建 Workspace。"""
        for path in service_sources():
            code = code_only(path).lower()
            for forbidden in ("wallet", "ledger", "transfer(", "escrow",
                              "mkdir", "create_workspace", "grant(",
                              "subprocess", "git "):
                assert forbidden not in code, (
                    f"services/{path.name} 里出现了 {forbidden!r}"
                )

    def test_does_not_import_llm(self):
        for path in service_sources():
            for module in imported_modules(parse(path)):
                assert not module.startswith("llm"), (
                    f"services/{path.name} 导入了 {module} —— "
                    f"§25：不要让每轮谈判自动唤醒 LLM"
                )

    def test_no_workspace_permission_api(self):
        from services import TaskService

        for forbidden in ("open_workspace", "grant", "permission",
                          "create_workspace", "checkout", "read_file",
                          "write_file"):
            assert not hasattr(TaskService, forbidden)


# ============================================================
# 5. Communication 保持业务无关
# ============================================================
class TestCommunicationStaysBusinessFree:
    def test_does_not_import_task_policy_or_economy(self):
        violations = []

        for source in sorted(COMM_DIR.rglob("*.py")):
            for module in imported_modules(parse(source)):
                for prefix in ("core.task", "core.decision_policy",
                               "core.task_agreement", "core.wallet",
                               "core.ledger", "core.economy", "services"):
                    if module == prefix or module.startswith(prefix + "."):
                        violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "communication/ 依赖了业务层：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_does_not_understand_price_rules(self):
        """
        §15：报价超过预算可以正常到达 Communication ——
        业务校验在 TaskService，不在通信层。
        """
        code = " ".join(
            code_only(source) for source in sorted(COMM_DIR.rglob("*.py"))
        ).lower()

        for forbidden in ("budget", "agreed_price", "accept_price",
                          "walk_away", "negotiat"):
            assert forbidden not in code, (
                f"communication/ 里出现了 {forbidden!r} —— "
                f"它只负责传递消息，不理解价格"
            )


# ============================================================
# 6. 不许实现后续步骤
# ============================================================
class TestFutureStepsAreNotImplemented:
    @pytest.mark.parametrize(
        "forbidden",
        # 【注意这里没有裸的 "payment" / "Payment"】
        #     因为成交凭证里有一句**故意写的** `"is_payment": False` ——
        #     那是明确声明「这不是支付记录」，属于正确用法。
        #     所以只禁止真正实现支付的符号。
        ["Escrow", "escrow", "PaymentRecord", "PaymentService",
         "create_payment", "process_payment", "settle_payment",
         "WorkspaceGateway", "WorkspaceManager", "WorkspacePermission",
         "GitWorktree", "git_commit", "worktree",
         "TaskManager", "Coordinator", "Scheduler",
         "Reputation", "reputation", "leaderboard",
         "LocalModelDecisionPolicy", "BrainDecisionPolicy"],
    )
    def test_not_implemented(self, forbidden):
        """
        谁实现了它们，这里会红 —— 从而被迫先想清楚那属于哪一步。

        （注意 LocalModelDecisionPolicy / BrainDecisionPolicy
          只允许出现在**注释**里作为未来方向，所以这里检查纯代码。）
        """
        for path in ("core/decision_policy.py", "core/task_agreement.py",
                     "services/task_service.py"):
            code = code_only(PROJECT_ROOT / path)
            assert forbidden not in code, (
                f"{path} 里出现了 {forbidden!r} —— 属于后续步骤"
            )

    def test_no_distributed_infrastructure(self):
        """§35：不要引入 Redis / Kafka / MQ / 微服务。"""
        for path in ("core/decision_policy.py", "core/task_agreement.py",
                     "services/task_service.py"):
            code = code_only(PROJECT_ROOT / path).lower()

            for forbidden in ("redis", "kafka", "rabbitmq", "celery",
                              "websocket", "grpc", "sqlalchemy"):
                assert forbidden not in code, (
                    f"{path} 里出现了 {forbidden!r}"
                )

    def test_no_new_tables_for_workspace_or_payment(self, db):
        tables = set(db.table_names())

        for forbidden in (
            "workspace_members", "workspace_permissions", "workspace_files",
            "workspace_capabilities", "payments", "escrows",
            "contracts", "reputation_scores",
        ):
            assert forbidden not in tables, (
                f"Step 4C 不该创建 {forbidden} 表"
            )

    def test_agreements_table_exists(self, db):
        """但它**应该**建了成交凭证表。"""
        assert "task_agreements" in db.table_names()


# ============================================================
# 7. 依赖方向
# ============================================================
class TestDependencyDirection:
    def test_nothing_in_core_depends_on_services(self):
        violations = []
        for source in sorted(CORE_DIR.rglob("*.py")):
            for module in imported_modules(parse(source)):
                if module.startswith("services"):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "core/ 不能依赖 services/：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_policy_dependency_graph_is_flat(self):
        """策略只依赖 Money —— 最底层的值类型。"""
        allowed = {"__future__", "dataclasses", "typing", "abc", "core.money"}

        for module in imported_modules(parse(POLICY_FILE)):
            assert module in allowed, (
                f"core/decision_policy.py 依赖了 {module}"
            )

    def test_agreement_depends_only_on_leaf_types(self):
        allowed = {"__future__", "dataclasses", "typing", "core.identity",
                   "core.money"}

        for module in imported_modules(parse(AGREEMENT_FILE)):
            assert module in allowed, (
                f"core/task_agreement.py 依赖了 {module}"
            )


# ============================================================
# 8. 成交的副作用必须为零
# ============================================================
class TestAcceptHasNoSideEffects:
    def test_accept_path_mentions_no_economic_calls(self):
        """
        用源码级守卫再加一道：ACCEPT 的落地点不该出现经济调用。
        """
        code = all_service_code()

        for forbidden in ("ledger.", "wallet.", "transfer",
                          "record_transaction", "mint"):
            assert forbidden not in code, (
                f"TaskService 里出现了 {forbidden!r} —— "
                f"§20：ACCEPT 只意味着合同成立，不是付款"
            )

    def test_accept_locally_returns_no_payment(self):
        """
        成交结果里必须明说「没有付款、没有授权」。

        【重构后】ACCEPT 的落地点从 TaskService 搬到了
        TaskNegotiationService.apply_accept —— 守卫跟着搬，
        保护的内容完全一样。
        """
        import inspect

        from services import TaskNegotiationService

        source = inspect.getsource(TaskNegotiationService.apply_accept)

        assert '"payment": None' in source
        assert '"workspace": None' in source
        assert '"llm_calls": 0' in source

    def test_offers_table_has_no_payment_columns(self, db):
        columns = set(db.columns_of("task_offers"))

        for forbidden in ("paid", "payment_id", "settled", "tx_id"):
            assert forbidden not in columns, (
                f"task_offers 里出现了 {forbidden!r} —— "
                f"报价不是支付"
            )

    def test_agreements_table_has_no_payment_columns(self, db):
        columns = set(db.columns_of("task_agreements"))

        for forbidden in ("paid", "payment_id", "settled", "tx_id"):
            assert forbidden not in columns
