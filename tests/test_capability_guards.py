"""
Test: Capability / Directory 架构守卫（Step 4B）
============================================================

【这一组守卫在守什么】

    Step 4B 引入了两个新模块，它们的位置必须正确：

        core/capability.py       最底层 —— 只做名字的集合运算
        core/agent_directory.py  只回答「谁会这个」，不做决策

    以及一条最重要的边界：

        能力（能不能做）  ≠  权限（被不被允许做）

    功能写错了会报错；边界破了往往不会报错，只会慢慢烂掉 ——
    等到要做 Workspace 权限时，才发现能力模块里早就长出了授权逻辑。

【守卫清单】

    Capability Core 不能依赖：
        llm · wallet · ledger · 文件系统 · git · workspace 权限 · transport

    Agent Directory 可以知道：
        agent id · address · capabilities · status
    但不能：
        调用 LLM · 转账 · 改 Workspace · 改 Task 状态

    Task 不能：
        discover agents · send message · transfer money · open workspace

    Communication 不能：
        解释 capability 业务 · 做经济决策 · 开 Workspace · 操作 Git
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

    文档里会**合法地**提到 "wallet"、"git"、"workspace permission"
    来解释为什么不能用它们。守卫必须忽略说明文字，
    否则它会被自己的文档绊倒，最后被人关掉。
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
    """
    把标识符切成词再比较。

    【为什么不能子串匹配】
        'offer_amounts' 含 'mount'，'seen_tracker' 含 'ack'，
        'is_open' 含 'open' —— 这一路已经误报三次了。
    """
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
# 1. Capability Core 必须是最底层
# ============================================================
class TestCapabilityCoreIsALeaf:
    def test_no_forbidden_imports(self):
        """
        能力模块不能依赖任何东西 —— 它只做名字的集合运算。
        """
        forbidden_prefixes = (
            "llm", "communication", "agents",
            "core.wallet", "core.ledger", "core.economy",
            "core.task", "core.workspace", "core.task_workspace",
            "storage", "services",
        )
        forbidden_exact = {
            "os", "os.path", "pathlib", "shutil", "subprocess",
            "glob", "tempfile", "sqlite3",
        }

        violations = []
        for module in imported_modules(parse(CORE_DIR / "capability.py")):
            if module in forbidden_exact:
                violations.append(module)
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(prefix + "."):
                    violations.append(module)

        assert not violations, (
            "core/capability.py 依赖了不该依赖的模块：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_file_operations(self):
        code = code_only(CORE_DIR / "capability.py")

        for forbidden in ("open(", "mkdir", "read_text", "write_text",
                          "subprocess", "Popen"):
            assert forbidden not in code, (
                f"core/capability.py 里出现了 {forbidden!r}"
            )

    def test_no_scoring_ranking_reputation(self):
        """
        【§8 的硬性要求】

        不要评分、不要排名、不要信誉。
        有了它们，能力和表现就混在一起了。
        """
        code = code_only(CORE_DIR / "capability.py").lower()

        for forbidden in ("score", "rank", "rating", "reputation",
                          "leaderboard", "weight", "confidence"):
            assert forbidden not in code, (
                f"core/capability.py 里出现了 {forbidden!r} —— "
                f"那属于信誉/表现体系，不是能力"
            )

    def test_no_best_agent_selection(self):
        """不许有「选一个最好的」。"""
        code = code_only(CORE_DIR / "capability.py").lower()

        for forbidden in ("best", "select", "choose", "recommend", "pick"):
            assert forbidden not in code, (
                f"core/capability.py 里出现了 {forbidden!r} —— "
                f"能力模块不做选择"
            )


# ============================================================
# 2. Agent Directory 的边界
# ============================================================
class TestAgentDirectoryBoundaries:
    def test_no_llm_or_economy_dependencies(self):
        """
        Directory 可以知道 agent_id / capabilities / address / status，
        但不能调用模型、不能转账。
        """
        forbidden_prefixes = (
            "llm", "agents", "services",
            "core.wallet", "core.ledger", "core.economy",
            "core.task", "storage",
            "communication",
        )

        violations = []
        for module in imported_modules(parse(CORE_DIR / "agent_directory.py")):
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(prefix + "."):
                    violations.append(module)

        assert not violations, (
            "core/agent_directory.py 依赖了不该依赖的模块：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_file_or_git_operations(self):
        code = code_only(CORE_DIR / "agent_directory.py")

        for forbidden in ("open(", "mkdir", "subprocess", "git"):
            assert forbidden not in code

    def test_no_economic_or_permission_terms_in_code(self):
        """
        目录里不该出现转账 / 权限 / 任务状态相关的东西。
        """
        code = code_only(CORE_DIR / "agent_directory.py").lower()

        forbidden = [
            "transfer", "wallet", "ledger", "balance", "payment",
            "grant", "permission", "authorize", "capability_check",
            "task_status", "accept_task", "complete_task",
        ]
        for word in forbidden:
            assert word not in code, (
                f"core/agent_directory.py 里出现了 {word!r} —— "
                f"目录只做能力发现"
            )

    def test_has_no_decision_methods(self):
        from core.agent_directory import AgentDirectory, LocalAgentDirectory

        decision_words = {
            "best", "select", "choose", "pick", "recommend", "assign",
            "dispatch", "schedule", "allocate",
        }

        for cls in (AgentDirectory, LocalAgentDirectory):
            found = [
                name for name in dir(cls)
                if not name.startswith("_") and matches(name, decision_words)
            ]
            assert not found, f"{cls.__name__} 上有决策类方法：{found}"

    def test_directory_is_abstract(self):
        """必须是抽象接口 —— 否则无法替换成网络实现。"""
        from core.agent_directory import AgentDirectory

        with pytest.raises(TypeError):
            AgentDirectory()

    def test_address_is_plain_string_not_communication_type(self):
        """
        Directory 里的地址是**字符串**，不是 communication.Address。

        这样 core/ 不必依赖 communication/（否则会形成循环）。
        """
        from core.agent_directory import DirectoryEntry
        import typing

        hints = typing.get_type_hints(DirectoryEntry)

        # address 的注解应该是 Optional[str]
        assert "str" in str(hints.get("address")), (
            f"address 的类型应该是 str，实际是 {hints.get('address')}"
        )


# ============================================================
# 3. Task 不能越界
# ============================================================
class TestTaskDoesNotOverreach:
    def test_task_does_not_depend_on_directory(self):
        """
        【§9 的核心】Task 只描述需要什么能力，不负责寻找 Agent。
        """
        for module in imported_modules(parse(CORE_DIR / "task.py")):
            assert not module.startswith("core.agent_directory"), (
                f"core/task.py 导入了 {module} —— "
                f"Task 不该知道 AgentDirectory 的存在"
            )

    def test_task_has_no_agent_discovery_methods(self):
        """
        【守卫随 Step 4C 收紧了，值得说明】

        原来这条守卫只要方法名里含 "find" 就报错。但 Step 4C 给 Task 加了
        `find_offer_by_message()` —— 它是在任务**自己的报价历史**里查，
        和「发现 Agent」毫无关系。原来的写法会误报。

        现在改成两条精确规则：
            1. 名字里出现 discover / directory / candidate 这类词 → 违规
            2. 「查找动词 + Agent 相关词」同时出现 → 违规

        这样 find_offer_by_message 放行，而 find_agents / find_capable
        照样会被抓住。保护力没有降低。
        """
        from core.task import Task

        #: 只要出现就违规（这些词本身就带着「发现 Agent」的语义）
        reserved = {"discover", "discovery", "directory", "candidate",
                    "candidates", "capable"}

        #: 查找动词
        discovery_verbs = {"find", "search", "lookup", "match", "list"}

        #: Agent 相关词
        agent_words = {"agent", "agents", "partner", "partners",
                       "contractor_candidates", "helpers"}

        #: 委派动作
        delegation = {"send", "delegate", "dispatch", "outsource",
                      "publish", "broadcast", "offer_to"}

        found = []
        for name in dir(Task):
            if name.startswith("_"):
                continue

            segments = name_segments(name)

            if segments & reserved or segments & delegation:
                found.append(name)
                continue

            if (segments & discovery_verbs) and (segments & agent_words):
                found.append(name)

        assert not found, (
            f"Task 上出现了「发现 Agent / 委派」类方法：{found}\n"
            f"Task 只声明需要什么能力；找 Agent 和发消息都不是它的事。\n"
            f"（在自己历史里查报价是允许的，比如 find_offer_by_message）"
        )

    def test_task_does_not_import_communication(self):
        for module in imported_modules(parse(CORE_DIR / "task.py")):
            assert not module.startswith("communication"), (
                f"core/task.py 导入了 {module}"
            )

    def test_task_does_not_touch_money_or_files(self):
        code = code_only(CORE_DIR / "task.py").lower()

        for forbidden in ("transfer", "wallet", "ledger", "subprocess",
                          "mkdir", "worktree"):
            assert forbidden not in code

    def test_task_still_has_required_capabilities(self):
        """但它**必须**能声明需要什么能力。"""
        from core.task import Task

        task = Task.create(creator="a", required_capabilities=["python"])

        assert task.required_capabilities.names == ["python"]


# ============================================================
# 4. Communication 保持业务无关
# ============================================================
class TestCommunicationStaysBusinessFree:
    def test_communication_does_not_import_capability(self):
        """
        通信层不该知道「能力」是什么 —— 那是业务概念。
        """
        violations = []
        for source in sorted(COMM_DIR.rglob("*.py")):
            for module in imported_modules(parse(source)):
                if module.startswith("core.capability"):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "communication/ 不该依赖 Capability：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_communication_does_not_import_services(self):
        """通信层也不该知道适配层。"""
        violations = []
        for source in sorted(COMM_DIR.rglob("*.py")):
            for module in imported_modules(parse(source)):
                if module.startswith("services"):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations

    def test_communication_does_not_interpret_capability_payload(self):
        """
        通信层不许在代码里出现能力相关的业务判断。
        """
        code = " ".join(
            code_only(source) for source in sorted(COMM_DIR.rglob("*.py"))
        ).lower()

        for forbidden in ("required_capabilities", "find_capable",
                          "capabilityprofile"):
            assert forbidden not in code, (
                f"communication/ 里出现了 {forbidden!r} —— "
                f"通信层只搬运消息，不解释能力语义"
            )

    def test_communication_does_not_do_economic_decisions(self):
        code = " ".join(
            code_only(source) for source in sorted(COMM_DIR.rglob("*.py"))
        ).lower()

        for forbidden in ("budget", "agreed_price", "transfer"):
            assert forbidden not in code, (
                f"communication/ 里出现了 {forbidden!r}"
            )


# ============================================================
# 5. 依赖方向
# ============================================================
class TestDependencyDirection:
    def test_services_depends_on_core_and_communication(self):
        """
        services 是适配层，它**可以**同时依赖 core 和 communication。
        这是单向的 —— 那两个都不依赖它。
        """
        modules = imported_modules(parse(SERVICES_DIR / "task_service.py"))

        assert any(m.startswith("core.") for m in modules)
        assert any(m.startswith("communication") for m in modules)

    def test_nothing_in_core_depends_on_services(self):
        violations = []
        for source in sorted(CORE_DIR.rglob("*.py")):
            for module in imported_modules(parse(source)):
                if module.startswith("services"):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "core/ 不能依赖 services/（方向反了）：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_capability_dependency_graph_is_shallow(self):
        """
        core/capability.py 的依赖必须最少、最稳定。
        """
        allowed = {"__future__", "dataclasses", "typing"}

        for module in imported_modules(parse(CORE_DIR / "capability.py")):
            assert module in allowed, (
                f"core/capability.py 依赖了 {module} —— 应该只有标准库"
            )

    def test_no_circular_dependency_between_capability_and_directory(self):
        """Directory 依赖 Capability（单向），不能反过来。"""
        capability_modules = imported_modules(parse(CORE_DIR / "capability.py"))
        directory_modules = imported_modules(parse(CORE_DIR / "agent_directory.py"))

        assert not any("agent_directory" in m for m in capability_modules)
        assert any("capability" in m for m in directory_modules)


# ============================================================
# 6. 不许把 Agent B 写死
# ============================================================
class TestNoHardcodedAgentB:
    """
    【§32 的明确禁止】

    「不要因为当前只有 A/B 两个 Agent，就写死 Agent B 为默认合作对象。」
    """

    @pytest.mark.parametrize(
        "source",
        [
            "core/capability.py",
            "core/agent_directory.py",
            "core/task.py",
            "services/task_service.py",
        ],
    )
    def test_no_hardcoded_agent_b(self, source):
        code = code_only(PROJECT_ROOT / source)

        assert "agent_b" not in code, (
            f"{source} 里硬编码了 agent_b —— "
            f"候选 Agent 应该由能力匹配得出，不能写死"
        )

    @pytest.mark.parametrize(
        "forbidden",
        ["find_agent_b", "default_partner", "preferred_agent",
         "fallback_agent", "DEFAULT_AGENT"],
    )
    def test_no_default_partner_concept(self, forbidden):
        for source in ("core/capability.py", "core/agent_directory.py",
                       "core/task.py", "services/task_service.py"):
            code = code_only(PROJECT_ROOT / source)
            assert forbidden not in code, (
                f"{source} 里出现了 {forbidden!r}"
            )

    def test_task_service_does_not_pick_for_the_agent(self):
        """
        TaskService 只给候选，绝不自己挑一个。
        """
        from services import TaskService

        for forbidden in ("best", "pick", "choose", "select", "auto_delegate"):
            assert not hasattr(TaskService, forbidden), (
                f"TaskService 不该有 {forbidden!r} —— "
                f"选谁是发起方 Agent 的决定"
            )


# ============================================================
# 7. 不许实现后续步骤的东西
# ============================================================
class TestFutureStepsAreNotImplemented:
    @pytest.mark.parametrize(
        "forbidden",
        [
            "TaskManager", "task_manager",
            "Coordinator", "coordinator",
            "Scheduler", "scheduler",
            "reputation", "Reputation",
            "leaderboard",
            "price_optimiz", "PriceOptimiz",
            "bidding_engine", "BiddingEngine",
            "WorkspaceGateway", "WorkspaceManager",
            "GitWorktree", "escrow", "Escrow",
            "NetworkAgentDirectory", "P2PDiscovery",
        ],
    )
    def test_not_implemented(self, forbidden):
        """
        这一组是「不越界」的可执行版本。
        谁实现了它们，这里会红 —— 从而被迫先想清楚那属于哪一步。

        【注意】NetworkAgentDirectory / P2PDiscovery 只允许出现在**注释**里
        （作为未来方向的说明），所以这里检查的是纯代码。
        """
        for source in ("core/capability.py", "core/agent_directory.py",
                       "core/task.py", "services/task_service.py"):
            code = code_only(PROJECT_ROOT / source)
            assert forbidden not in code, (
                f"{source} 里出现了 {forbidden!r} —— 属于后续步骤"
            )

    def test_no_network_or_p2p_code(self):
        for source in ("core/agent_directory.py", "services/task_service.py"):
            code = code_only(PROJECT_ROOT / source).lower()

            for forbidden in ("socket", "asyncio", "requests", "urllib",
                              "http", "p2p"):
                assert forbidden not in code, (
                    f"{source} 里出现了 {forbidden!r} —— Step 4B 不实现网络"
                )

    def test_no_workspace_permission_tables(self, db):
        tables = set(db.table_names())

        for forbidden in ("workspace_members", "workspace_permissions",
                          "workspace_files", "workspace_capabilities",
                          "agent_capabilities", "capability_scores"):
            assert forbidden not in tables, (
                f"Step 4B 不该创建 {forbidden} 表"
            )

    def test_no_capability_column_in_db(self, db):
        """
        能力存在**目录**里（内存），不是数据库表。

        因为它是「谁在线、会什么」的运行时事实 ——
        和任务、账本那种需要持久化的事实不是一类。
        （将来做网络发现时更明显：远端 Agent 不会写进本地库。）
        """
        columns = set(db.columns_of("tasks"))

        assert "required_capabilities" in columns      # 任务的要求要持久化
        assert "capabilities" not in columns           # Agent 的能力不需要
