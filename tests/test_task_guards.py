"""
Test: Task Core 架构守卫（Step 4A）
============================================================

这些测试不检查功能，检查的是**边界有没有被破坏**。

Step 4A 的价值不在于「Task 能跑」，而在于「Task 跑的时候没有碰到不该碰的东西」。
功能写错了会报错；边界破了往往不会报错，只会慢慢烂掉 ——
等到要做 Workspace 权限时，才发现 Task 里早就长满了文件操作和转账逻辑。

守卫清单（对应 Step 4A 的硬性要求）：

    Task Core
    ❌ 不依赖 llm/*
    ❌ 不依赖 communication/*
    ❌ 不依赖 wallet / ledger
    ❌ 不依赖文件系统
    ❌ 不做任何 I/O（那是 TaskStore 的事）
    ❌ 不创建 Workspace
    ❌ 不授予权限

以及最容易被顺手破坏的一条：

    ❌ 不把 TASK_OFFER / COUNTER_OFFER / ACCEPT 接入 Communication
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = PROJECT_ROOT / "core"

#: Task Core 的全部源码（不含持久化层）
TASK_CORE_FILES = ("task.py", "task_workspace.py", "money.py")

#: 加上持久化层就是 Task 相关的全部
TASK_ALL_FILES = TASK_CORE_FILES + ("task_store.py",)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> list:
    """列出文件里 import 的所有模块名。"""
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def code_without_docstrings(path: Path) -> str:
    """
    取出**纯代码**：去掉文档字符串，也去掉 # 注释。

    【为什么必须去掉这两样】
        因为文档和注释里会**合法地**提到 "llm"、"Escrow"、"MessageStatus"
        这些词 —— 它们正是在解释「为什么不能这么做」。

        如果连它们一起检查，守卫会被自己的说明文字绊倒，
        而那种守卫最后一定会被人关掉（因为它老是误报）。

    【实现方式】
        用 AST 删掉 docstring 节点，再 ast.unparse。
        unparse 不会输出注释，所以注释也一并消失。
        函数名里的词照样保留 —— 我们真正想检查的就是它们。
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
    把一个标识符切成「词」，用于匹配。

    【为什么不能直接用子串匹配】
        'offer_amounts' 里含有 'mount'（a-mount-s），
        于是「不许有 mount（挂载）」这条守卫会误报。
        同类误报之前已经出现过一次：'seen_tracker' 里含有 'ack'。

        所以按 snake_case 和驼峰切开，再逐词比较。
    """
    segments = set()
    for part in str(name).split("_"):
        segments.update(
            token.lower()
            for token in re.findall(r"[a-z0-9]+|[A-Z][a-z0-9]*", part)
        )
    return segments


def matches_any_segment(name: str, words) -> bool:
    """名字里是否有任何一个「词」等于 words 里的某一项。"""
    return bool(name_segments(name) & {w.lower() for w in words})


# ============================================================
# 1. Task Core 不依赖 LLM
# ============================================================
class TestTaskCoreIsIndependentOfLLM:
    def test_no_llm_imports(self):
        """
        【需求：Task Core 必须能完全脱离 llm/* 运行】

        为什么？
            Task 是业务状态，不是 AI 推理。
                谁决定接不接这个任务 → Agent Brain（要用模型）
                系统认为任务处于什么状态 → Task（不用模型）
            混在一起的话，测试 Task 就得先准备一个模型。
        """
        violations = []

        for name in TASK_ALL_FILES:
            for module in imported_modules(parse(CORE_DIR / name)):
                if module == "llm" or module.startswith("llm."):
                    violations.append(f"{name} 导入了 {module}")

        assert not violations, (
            "Task 相关模块不能依赖 llm：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_model_vendor_names(self):
        """Task 里不该出现任何模型厂商的名字。"""
        vendors = ("deepseek", "doubao", "grok", "openai", "claude",
                   "anthropic", "qwen", "gemini")
        violations = []

        for name in TASK_ALL_FILES:
            code = code_without_docstrings(CORE_DIR / name).lower()
            for vendor in vendors:
                if vendor in code:
                    violations.append(f"{name} 里出现 {vendor!r}")

        assert not violations

    def test_task_files_do_not_import_brain(self):
        """Task 也不依赖 Agent Brain。"""
        for name in TASK_ALL_FILES:
            for module in imported_modules(parse(CORE_DIR / name)):
                assert not module.startswith("agents"), (
                    f"{name} 导入了 {module}"
                )


# ============================================================
# 2. Task Core 不依赖 Communication
# ============================================================
class TestTaskCoreIsIndependentOfCommunication:
    def test_task_core_does_not_import_communication(self):
        """
        【需求：Task State Machine 必须与 Communication 状态机分离】

        Task 解释消息的**业务意义**，通信层负责**把消息送到**。
        两者是两个状态机，不能互相依赖。
        """
        violations = []

        for name in TASK_CORE_FILES:
            for module in imported_modules(parse(CORE_DIR / name)):
                if module == "communication" or module.startswith("communication."):
                    violations.append(f"{name} 导入了 {module}")

        assert not violations, (
            "Task Core 不能依赖 communication：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_task_core_does_not_reference_message_class(self):
        """Task Core 里不该出现 Message 这个类型。"""
        violations = []

        for name in TASK_CORE_FILES:
            code = code_without_docstrings(CORE_DIR / name)
            # 匹配 Message( 或 Message. 这种真实用法
            if re.search(r"\bMessage\s*[.(]", code):
                violations.append(name)

            # MessageStatus / MessageType 也不行
            if "MessageStatus" in code or "MessageType" in code:
                violations.append(f"{name}（MessageStatus/MessageType）")

        assert not violations, f"Task Core 里出现了通信类型：{violations}"

    def test_offer_types_are_local_constants_not_imported(self):
        """
        OfferType 必须是自己定义的常量，不是从 communication 借来的。

        两边字符串一致由**测试**保证（test_task.py 有一条），
        而不是靠 import —— 那样 Task Core 就被通信层绑住了。
        """
        tree = parse(CORE_DIR / "task.py")

        # 确认 OfferType 是在这个文件里定义的类
        class_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        assert "OfferType" in class_names


# ============================================================
# 3. Task Core 不依赖 Economy
# ============================================================
class TestTaskCoreIsIndependentOfEconomy:
    def test_task_core_does_not_import_wallet_or_ledger(self):
        """
        【需求：Task Core 不能直接操作 Wallet / Ledger】

        Task 里有 budget 和 agreed_price，但那只是**经济约定**。
        task.accept() 记录「谈好了 25 AIC」，不会真的转 25 AIC。
        """
        forbidden = ("core.wallet", "core.ledger", "core.economy")
        violations = []

        for name in TASK_CORE_FILES:
            for module in imported_modules(parse(CORE_DIR / name)):
                for bad in forbidden:
                    if module == bad or module.startswith(bad + "."):
                        violations.append(f"{name} 导入了 {module}")

        assert not violations, (
            "Task Core 不能依赖 Economy：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_payment_methods_in_task(self):
        """
        Task 上不能有转账 / 结算 / 托管相关的方法。
        """
        from core.task import Task

        forbidden = ("transfer", "pay", "pay_", "settle", "escrow",
                     "charge", "debit", "credit", "deposit", "withdraw")

        found = [
            name for name in dir(Task)
            if not name.startswith("_")
            and matches_any_segment(name, forbidden)
        ]

        assert not found, f"Task 上出现了支付相关方法：{found}"

    def test_money_type_does_not_do_io_either(self):
        """
        Money 也不能碰数据库 —— 它只是算数。
        """
        for module in imported_modules(parse(CORE_DIR / "money.py")):
            assert not module.startswith("storage"), (
                f"core/money.py 导入了 {module}"
            )
            assert not module.startswith("sqlite3"), (
                f"core/money.py 导入了 {module}"
            )


# ============================================================
# 4. Task Core 不依赖文件系统
# ============================================================
class TestTaskCoreIsIndependentOfFilesystem:
    def test_task_core_does_not_import_file_modules(self):
        """
        【需求：Task Core 不能直接操作文件系统】

        Task 只持有一个 workspace_id（逻辑引用）。
        真实的文件访问属于 Workspace 层 —— 而且必须先经过权限检查，
        而权限系统在 Step 4A 里**还不存在**。
        """
        forbidden = {"os", "os.path", "pathlib", "shutil", "subprocess",
                     "glob", "tempfile", "io"}
        violations = []

        for name in TASK_CORE_FILES:
            for module in imported_modules(parse(CORE_DIR / name)):
                if module in forbidden:
                    violations.append(f"{name} 导入了 {module}")

        assert not violations, (
            "Task Core 不能依赖文件系统：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_task_core_does_not_call_open(self):
        """Task Core 不能调用 open()。"""
        violations = []

        for name in TASK_CORE_FILES:
            tree = parse(CORE_DIR / name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    fname = ""
                    if isinstance(func, ast.Name):
                        fname = func.id
                    elif isinstance(func, ast.Attribute):
                        fname = func.attr
                    if fname in ("open", "read_text", "write_text",
                                 "unlink", "mkdir", "rmdir", "remove"):
                        violations.append(f"{name}:{node.lineno} {fname}()")

        assert not violations, (
            "Task Core 里出现了文件操作：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_task_core_does_not_import_storage(self):
        """
        Task Core 不碰数据库 —— 只有 task_store.py 可以。

        这样「Task Core 是纯的」才是可验证的，而不是一句口号。
        """
        violations = []

        for name in TASK_CORE_FILES:
            for module in imported_modules(parse(CORE_DIR / name)):
                if module.startswith("storage"):
                    violations.append(f"{name} 导入了 {module}")

        assert not violations, (
            "只有 core/task_store.py 能碰数据库：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_only_task_store_touches_database(self):
        """反过来说，task_store.py 应该是唯一碰 storage 的 Task 模块。"""
        tree = parse(CORE_DIR / "task_store.py")
        modules = imported_modules(tree)

        assert any(m.startswith("storage") for m in modules), (
            "task_store.py 应该负责持久化，但没有 import storage"
        )


# ============================================================
# 5. workspace_id 不产生权限（Step 4A 的核心承诺）
# ============================================================
class TestWorkspaceIdGrantsNothing:
    def test_task_has_no_workspace_access_methods(self):
        from core.task import Task

        forbidden = ("open_workspace", "grant", "permission", "capability",
                     "read_file", "write_file", "list_files", "access",
                     "authorize", "mount", "checkout")

        found = [
            name for name in dir(Task)
            if not name.startswith("_")
            and matches_any_segment(name, forbidden)
        ]

        assert not found, f"Task 上出现了 Workspace 访问相关方法：{found}"

    def test_task_workspace_ref_has_no_io(self):
        from core.task_workspace import TaskWorkspaceRef

        for forbidden in ("read_text", "write_text", "open", "path",
                          "root", "grant", "permission"):
            assert not hasattr(TaskWorkspaceRef, forbidden)

    def test_no_grant_api_anywhere_in_task_modules(self):
        """
        全面扫一遍 Task 相关模块的源码，不许出现授权类调用。
        """
        patterns = ("grant(", "grant_access", "allow_access",
                    "check_permission", "require_capability")
        violations = []

        for name in TASK_ALL_FILES:
            code = code_without_docstrings(CORE_DIR / name)
            for pattern in patterns:
                if pattern in code:
                    violations.append(f"{name}: {pattern}")

        assert not violations

    def test_create_workspace_is_not_implemented(self):
        """
        Step 4A 不创建真实 Workspace 目录。

        只有 core/workspace.py（Agent 私有空间）会创建目录，
        而那是 Agent 私有的，和 Task Workspace 不是一回事。
        """
        for name in TASK_ALL_FILES:
            code = code_without_docstrings(CORE_DIR / name)
            assert "mkdir" not in code, f"{name} 里出现了 mkdir"
            assert "makedirs" not in code, f"{name} 里出现了 makedirs"


# ============================================================
# 6. 不把经济消息接入 Communication（Step 4A 的明确禁止项）
# ============================================================
class TestNotWiredIntoCommunication:
    """
    【需求原文】

        「不要为了测试 Task 而直接把 TASK_OFFER / COUNTER_OFFER / ACCEPT
          全部接入 Communication。STEP 4A 首先测试 Task Core 本身。」

    这一组守卫盯着这条线。
    """

    def test_no_type_handlers_registered_for_economic_messages(self):
        """
        通信层不能注册 TASK_OFFER / COUNTER_OFFER / ACCEPT 的处理器。
        """
        violations = []

        for source in sorted(PROJECT_ROOT.rglob("*.py")):
            if ".venv" in source.parts or "tests" in source.parts:
                continue
            code = code_without_docstrings(source)

            for match in re.finditer(
                r"register_type_handler\(\s*[\"']?(TASK_OFFER|COUNTER_OFFER|ACCEPT)",
                code,
            ):
                violations.append(
                    f"{source.relative_to(PROJECT_ROOT)}: {match.group(1)}"
                )

        assert not violations, (
            "Step 4A 不该把经济消息接入通信层：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_main_does_not_register_task_handlers(self):
        """组合根里也不能把 Task 接到通信上。"""
        code = code_without_docstrings(PROJECT_ROOT / "main.py")

        assert "register_type_handler" not in code or (
            "TASK_OFFER" not in code and "COUNTER_OFFER" not in code
        ), "main.py 把经济消息接到了通信层"

    def test_communication_layer_does_not_know_about_tasks(self):
        """
        反过来，通信层也不该知道 Task 的存在。
        """
        violations = []

        for source in sorted((PROJECT_ROOT / "communication").rglob("*.py")):
            for module in imported_modules(parse(source)):
                if module.startswith("core.task"):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "communication/ 不该依赖 Task：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_task_store_does_not_send_messages(self):
        """TaskStore 不发消息。"""
        code = code_without_docstrings(CORE_DIR / "task_store.py")

        for forbidden in ("send_message", "manager.send", "transport.send",
                          "CommunicationManager"):
            assert forbidden not in code, f"task_store.py 里出现了 {forbidden}"


# ============================================================
# 7. 不实现后续步骤的东西
# ============================================================
class TestStep4BThingsAreNotImplemented:
    @pytest.mark.parametrize(
        "forbidden",
        [
            "task_market", "TaskMarket",
            "negotiation_engine", "NegotiationEngine",
            "capability_match", "CapabilityMatcher",
            "workspace_gateway", "WorkspaceGateway",
            "workspace_manager", "WorkspaceManager",
            "git_worktree", "GitWorktree",
            "escrow", "Escrow",
        ],
    )
    def test_not_implemented(self, forbidden):
        """
        Step 4A 严格不实现这些。谁加了，这里会红 ——
        从而被迫先想清楚「这属于哪一步」。
        """
        for name in TASK_ALL_FILES:
            code = code_without_docstrings(CORE_DIR / name)
            assert forbidden not in code, (
                f"core/{name} 里出现了 {forbidden!r} —— 属于后续步骤"
            )

    def test_no_workspace_permission_tables(self, db):
        """Workspace 权限相关的表不该被创建。"""
        tables = set(db.table_names())

        for forbidden in (
            "workspace_members", "workspace_permissions",
            "workspace_files", "workspace_capabilities",
            "workspace_grants",
        ):
            assert forbidden not in tables

    def test_no_git_operations_anywhere_in_task_modules(self):
        """Task 里没有任何 Git 操作。"""
        for name in TASK_ALL_FILES:
            code = code_without_docstrings(CORE_DIR / name)

            for forbidden in ("git ", "subprocess", "checkout", "commit(",
                              "worktree"):
                assert forbidden not in code, (
                    f"core/{name} 里出现了 Git 相关代码：{forbidden!r}"
                )


# ============================================================
# 8. 依赖方向
# ============================================================
class TestDependencyDirection:
    def test_money_depends_on_nothing_project_specific(self):
        """
        Money 是最底层 —— 它不该依赖 Task / Workspace。
        """
        for module in imported_modules(parse(CORE_DIR / "money.py")):
            if module.startswith("core."):
                assert module == "core.config" or module == "core.money", (
                    f"core/money.py 依赖了 {module} —— Money 应该是最底层"
                )

    def test_task_workspace_is_standalone(self):
        """TaskWorkspaceRef 不依赖 Task（否则会循环）。"""
        for module in imported_modules(parse(CORE_DIR / "task_workspace.py")):
            assert not module.startswith("core.task"), (
                f"task_workspace.py 依赖了 {module}"
            )

    def test_task_depends_only_on_leaf_modules(self):
        """
        Task Core 的依赖必须是最小、稳定的抽象。

        允许：core.identity（ID 与时间）、core.money、core.task_workspace
        """
        allowed = {
            "core.identity", "core.money", "core.task_workspace", "core.task",
            "__future__",
        }
        violations = []

        for module in imported_modules(parse(CORE_DIR / "task.py")):
            if module.startswith("core.") and module not in allowed:
                violations.append(module)

        assert not violations, (
            "core/task.py 依赖了不该依赖的模块：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_circular_imports(self):
        """Task 相关模块之间不能循环依赖。"""
        # money → 无
        # task_workspace → 无
        # task → money, task_workspace
        # task_store → task, money, storage
        assert not any(
            m.startswith("core.task")
            for m in imported_modules(parse(CORE_DIR / "money.py"))
        )
        assert not any(
            m.startswith("core.task")
            for m in imported_modules(parse(CORE_DIR / "task_workspace.py"))
        )
