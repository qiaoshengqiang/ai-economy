"""
Test: Step 4D.1 架构守卫
============================================================

这一组守卫锁住 4D.1 的十条边界。

【为什么 4D.1 特别需要守卫】

    这次修的是「不报错但会悄悄变错」的问题：

        · 去掉持久化去重，代码**照样跑**，只是重启后重复处理
        · 去掉乐观锁，代码**照样跑**，只是并发时静默覆盖
        · 去掉事务包裹，代码**照样跑**，只是崩溃时半成功

    换句话说：这三样东西**删掉了也不会让任何功能测试变红**。
    所以必须有守卫专门盯着它们还在。

【守卫清单】

    Guard 1   Communication 去重不允许只依赖内存 tracker
    Guard 2   SeenMessageTracker 只能当缓存，不能是正确性唯一来源
    Guard 3   Handler 不得直接操作 SQLite
    Guard 4   TaskService 不得直接操作 SQLite
    Guard 5   Task Core 不得依赖 Communication storage
    Guard 6   TaskExecutionService 不得自己建数据库连接
    Guard 7   并发保护必须在真正的 persistence boundary
    Guard 8   不得通过自动 retry 掩盖 optimistic lock conflict
    Guard 9   4D.1 不得引入 Workspace
    Guard 10  4D.1 不得引入 subprocess / Git / Payment / Redis / Kafka / Queue
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = PROJECT_ROOT / "services"
CORE_DIR = PROJECT_ROOT / "core"
COMM_DIR = PROJECT_ROOT / "communication"
STORAGE_DIR = PROJECT_ROOT / "storage"

SERVICE_MODULES = tuple(
    path.name for path in sorted(SERVICES_DIR.glob("*.py"))
    if path.name != "__init__.py"
)


# ============================================================
# 工具
# ============================================================
def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def code_only(path: Path) -> str:
    """
    纯代码：去掉文档字符串，再 ast.unparse（注释也一并没有）。

    【为什么必须这样】
        文档里会合法地解释「我们**不做**什么」：

            ❌ 不自动重试
            ❌ 不引入 subprocess

        守卫只看代码，否则会被自己的说明文字绊倒。
        （这个坑这一路踩过六次了。）
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


# ============================================================
# Guard 1：去重不能只靠内存
# ============================================================
class TestGuard1DedupIsPersistent:
    """
    【这条守卫要防的是「悄悄退回去」】

        把 _claim_message 改成只用内存 tracker，代码照样跑、
        功能测试照样绿 —— 只有重启之后才会重复处理。
        那正是 4D.1 要修的问题，不能让它回来。
    """

    def test_manager_consults_persistence(self):
        """
        Manager 必须**真的调用**持久化认领。

        【为什么不能只查 'claim_message' 这个字符串】
            方法自己就叫 _claim_message —— 那个名字永远都在，
            所以「字符串存在」这件事证明不了任何东西。
            （反向测试时就发现了：把持久化调用删掉，
              这条守卫照样绿。）

            必须查**这次调用**：self.recorder.claim_message(...)
        """
        code = code_only(COMM_DIR / "manager.py")

        assert "self.recorder.claim_message(" in code, (
            "Manager 没有调用持久化认领 —— 去重会退回内存里"
        )

    def test_manager_has_a_degraded_path(self):
        """
        内存 tracker **只能**出现在明确的降级分支里。

        如果它成了主路径，去重就退回进程内了。
        """
        code = code_only(COMM_DIR / "manager.py")

        assert "if self.recorder is None:" in code, (
            "必须有明确的降级分支 —— 而不是无条件用内存 tracker"
        )
        assert "check_and_add" in code, "降级分支里应该用内存 tracker"

    def test_recorder_exposes_persistent_facts(self):
        from communication.recorder import CommunicationRecorder

        for method in ("claim_message", "mark_processed", "mark_failed",
                       "was_processed", "processing_state"):
            assert hasattr(CommunicationRecorder, method), (
                f"recorder 少了 {method}() —— 持久化事实需要它"
            )

    def test_processed_once_is_a_real_column(self, db):
        columns = set(db.columns_of("communication_messages"))

        assert "processed_once" in columns, (
            "processed_once 必须真的存在于表里 —— 它不是内存里的一个标志"
        )

    def test_claim_uses_atomic_conditional_update(self):
        """
        认领必须是**带条件的 UPDATE**，而不是「先查再写」。

        「先查再写」有 TOCTOU 窗口：
            两个 worker 都查到「没人认领」→ 都去处理。
        """
        # 【看纯代码，不看原文】
        #     原文的**文档字符串**里有一个 SQL 示例，
        #     里面也写着 `AND claim_id IS NULL`。
        #     如果查原文，把真正的条件删掉之后守卫照样绿 ——
        #     因为文档里的示例还在。
        #     （反向测试时正是这么发现的。）
        # 【先把空白压平】
        #     认领条件现在是多行的：
        #         AND (
        #              claim_id IS NULL
        #              OR claimed_at IS NULL
        #              OR claimed_at < ?
        #         )
        #     直接匹配 "AND claim_id IS NULL" 会匹配不到 ——
        #     但那不是「条件没了」，只是换行了。
        #     所以压平空白再检查语义片段。
        code = " ".join(code_only(COMM_DIR / "recorder.py").split())

        assert "claim_id IS NULL" in code, (
            "认领的 UPDATE 必须带 `claim_id IS NULL` 条件 —— "
            "否则两个 worker 会同时认领成功"
        )
        assert "processed_once = 0" in code, (
            "认领必须排除已经处理过的消息"
        )
        # 【Workspace Phase 0 新增】租约过期也必须能接手
        assert "claimed_at < ?" in code, (
            "认领条件必须包含租约过期判断（claimed_at < ?）—— "
            "否则崩溃的 worker 会永久占住消息"
        )
        assert "claimed_at IS NULL" in code, (
            "claimed_at 缺失（老数据 / 半写状态）时也应该可以被认领，"
            "否则那条消息永远抢不到"
        )

    def test_processed_once_is_monotonic(self):
        """
        【真正要防的是这件事】

            记日志用的 UPSERT 会更新 status。如果它**顺手也更新了
            processed_once**，那么一条重复投递的日志就会把
            「处理过」这个事实抹掉 —— 而去重依赖它。

        【为什么不能直接搜 "processed_once = 0"】
            那个字符串合法地出现在**WHERE 条件**里：

                UPDATE ... WHERE message_id = ? AND processed_once = 0

            那是「只在还没处理过时才写」，正是我们要的语义。
            所以守卫要看的是 UPSERT 的 SET 列表，不是全文。
        """
        source = (COMM_DIR / "recorder.py").read_text(encoding="utf-8")

        # 找到 record() 里那个 UPSERT 的 DO UPDATE SET 块
        marker = "ON CONFLICT(message_id) DO UPDATE SET"
        assert marker in source, "找不到消息表的 UPSERT"
        start = source.index(marker)
        end = source.index('"""', start)
        set_block = source[start:end]

        assert "processed_once" not in set_block, (
            "记日志的 UPSERT 动了 processed_once —— "
            "一条重复投递的日志就能把「处理过」的事实抹掉"
        )
        assert "claim_id" not in set_block, (
            "记日志的 UPSERT 动了 claim_id —— 它会干扰原子认领"
        )


# ============================================================
# Guard 2：内存 tracker 只能是缓存
# ============================================================
class TestGuard2TrackerIsOnlyACache:
    def test_tracker_is_not_the_source_of_truth(self, db):
        """
        【行为证明】

            把 tracker 清空，去重仍然必须工作。
            它是缓存，不是事实来源。
        """
        from communication import CommunicationManager, LocalTransport
        from communication.dedup import SeenMessageTracker
        from communication.recorder import CommunicationRecorder

        recorder = CommunicationRecorder(db)
        comm = CommunicationManager(LocalTransport(), recorder=recorder)
        comm.register("agent_b")

        from communication import Message, MessageType

        message = Message(from_agent="agent_a", to_agent="agent_b",
                          message_type=MessageType.SUBMIT,
                          payload={"task_id": "T"},
                          message_id="msg_guard2")

        first = comm._claim_message(message, "agent_b")
        assert first.claimed

        recorder.mark_processed(message, "agent_b")

        # 各种「内存丢了」的情况
        comm._seen["agent_b"] = SeenMessageTracker(1024)
        second = comm._claim_message(message, "agent_b")

        assert second.duplicate, (
            "清掉内存 tracker 之后仍然必须识别为重复 —— "
            "否则正确性就依赖内存了"
        )

    def test_tracker_class_documents_its_role(self):
        source = (COMM_DIR / "dedup.py").read_text(encoding="utf-8")

        assert "缓存" in source, (
            "SeenMessageTracker 的文档必须写清楚它只是缓存"
        )

    def test_tracker_failure_does_not_break_dedup(self, db):
        """
        缓存出错不该影响正确性 —— 用一个「永远说没见过」的 tracker。
        """
        from communication import CommunicationManager, LocalTransport, Message
        from communication import MessageType
        from communication.recorder import CommunicationRecorder

        class BrokenCache:
            def seen(self, message_id):
                return False

            def check_and_add(self, message_id):
                return True

            def remember(self, message_id):
                pass

        recorder = CommunicationRecorder(db)
        comm = CommunicationManager(LocalTransport(), recorder=recorder)
        comm.register("agent_b")
        comm._seen["agent_b"] = BrokenCache()

        message = Message(from_agent="agent_a", to_agent="agent_b",
                          message_type=MessageType.SUBMIT,
                          payload={"task_id": "T"},
                          message_id="msg_broken_cache")

        assert comm._claim_message(message, "agent_b").claimed
        recorder.mark_processed(message, "agent_b")

        result = comm._claim_message(message, "agent_b")

        assert result.duplicate, (
            "缓存坏掉时数据库仍然必须拦住重复 —— "
            "说明缓存不是正确性来源"
        )


# ============================================================
# Guard 3 / 4：应用层不许直接碰 SQLite
# ============================================================
class TestGuard3And4NoSqlInApplicationLayer:
    @pytest.mark.parametrize("name", SERVICE_MODULES)
    @pytest.mark.parametrize(
        "forbidden",
        ["sqlite3", "INSERT ", "UPDATE ", "DELETE ", "SELECT ",
         "conn.execute", "cursor.execute"],
    )
    def test_services_do_not_use_sql(self, name, forbidden):
        """
        Handler / TaskService / ExecutionService 都不许写 SQL。

        它们只能经过 TaskStore —— 那是数据库的唯一边界。
        """
        code = code_only(SERVICES_DIR / name)

        assert forbidden not in code, (
            f"services/{name} 里出现了 {forbidden!r} —— "
            f"应用层只能通过 TaskStore 持久化"
        )

    def test_services_do_not_import_sqlite(self):
        for name in SERVICE_MODULES:
            for module in imported_modules(SERVICES_DIR / name):
                assert not module.startswith("sqlite3"), (
                    f"{name} 导入了 sqlite3"
                )

    def test_transaction_boundary_is_used_not_bypassed(self):
        """
        执行服务要包事务，但用的是 `store.db.transaction()` ——
        也就是走的是既有的持久化边界，而不是自己开一个连接。
        """
        code = code_only(SERVICES_DIR / "task_execution_service.py")

        assert "store.db.transaction" in code, (
            "执行服务应该通过 store 的事务边界包住「状态 + 事件」"
        )
        assert "sqlite3.connect" not in code


# ============================================================
# Guard 5：Task Core 不许依赖通信存储
# ============================================================
class TestGuard5TaskCoreIndependent:
    @pytest.mark.parametrize(
        "name",
        ["task.py", "task_store.py", "task_execution.py",
         "task_agreement.py", "money.py", "capability.py",
         "agent_directory.py", "decision_policy.py"],
    )
    def test_core_does_not_import_communication(self, name):
        for module in imported_modules(CORE_DIR / name):
            assert not module.startswith("communication"), (
                f"core/{name} 导入了 {module} —— 领域层不该依赖通信层"
            )

    def test_core_does_not_import_services(self):
        violations = []
        for source in sorted(CORE_DIR.rglob("*.py")):
            for module in imported_modules(source):
                if module.startswith("services"):
                    violations.append(f"{source.name} → {module}")

        assert not violations, f"core 依赖了 services：{violations}"

    def test_task_execution_stays_a_value_module(self):
        """
        core/task_execution.py 必须保持纯值类型：
        无 I/O、不认识 Message、不认识 SQLite、不认识 Services。
        """
        modules = imported_modules(CORE_DIR / "task_execution.py")

        for forbidden in ("communication", "services", "storage", "sqlite3",
                          "os", "pathlib", "subprocess"):
            assert not any(
                m == forbidden or m.startswith(forbidden + ".")
                for m in modules
            ), f"core/task_execution.py 导入了 {forbidden}"

    def test_tracker_not_in_task_execution(self):
        """
        SeenMessageTracker 属于通信层 —— 不许塞进领域值类型。
        """
        source = (CORE_DIR / "task_execution.py").read_text(encoding="utf-8")

        for forbidden in ("SeenMessageTracker", "claim_message",
                          "processed_once", "transaction"):
            assert forbidden not in source, (
                f"core/task_execution.py 里出现了 {forbidden!r} —— "
                f"它不是「持久化/并发」的容器"
            )


# ============================================================
# Guard 6：执行服务不许自建数据库连接
# ============================================================
class TestGuard6NoPrivateDatabase:
    def test_execution_service_uses_injected_store(self):
        from services import TaskExecutionService

        import inspect

        signature = inspect.signature(TaskExecutionService.__init__)
        assert "store" in signature.parameters, (
            "TaskExecutionService 必须接受注入的 store"
        )

    def test_execution_service_does_not_create_database(self):
        source = (SERVICES_DIR / "task_execution_service.py").read_text(
            encoding="utf-8"
        )

        for forbidden in ("Database(", "sqlite3.connect", "create_memory"):
            assert forbidden not in source, (
                f"task_execution_service.py 里出现了 {forbidden!r} —— "
                f"它不该自己建数据库连接"
            )

    def test_only_one_database_boundary(self):
        """
        `storage/` 是唯一知道 sqlite3 的地方（加上 TaskStore 通过它）。

        communication/ 与 services/ 都不许直接 import sqlite3。
        """
        for base in (SERVICES_DIR, COMM_DIR):
            for source in sorted(base.glob("*.py")):
                for module in imported_modules(source):
                    assert not module.startswith("sqlite3"), (
                        f"{source.name} 导入了 sqlite3"
                    )


# ============================================================
# Guard 7：并发保护必须在持久化边界
# ============================================================
class TestGuard7LockingAtPersistenceBoundary:
    def test_version_column_exists(self, db):
        assert "version" in set(db.columns_of("tasks")), (
            "tasks 必须有 version 列"
        )

    def test_save_is_compare_and_set(self):
        """
        save() 的 UPDATE 必须带 `AND version = ?` ——
        放在应用层比对（先查再写）会有 TOCTOU 窗口。
        """
        source = (CORE_DIR / "task_store.py").read_text(encoding="utf-8")

        assert "AND version = ?" in source, (
            "save() 必须用带 version 条件的 UPDATE（compare-and-set）"
        )
        assert "ConcurrentTaskModification" in source

    def test_conflict_is_raised_not_returned(self):
        """冲突必须是异常，不能是一个被忽略的返回值。"""
        from core.task_store import ConcurrentTaskModification

        assert issubclass(ConcurrentTaskModification, Exception)

    def test_locking_is_not_in_the_service_layer(self):
        """
        应用层不该自己实现并发保护 ——
        那会变成「每个调用方各写一套」，迟早漏掉一个。
        """
        for name in SERVICE_MODULES:
            code = code_only(SERVICES_DIR / name)
            assert "AND version" not in code, (
                f"services/{name} 里出现了 version 比对 —— "
                f"并发保护属于 TaskStore"
            )

    def test_no_new_task_states_for_conflicts(self):
        """并发冲突不是 Task 状态。"""
        from core.task import TaskStatus

        for forbidden in ("CONFLICT", "CONFLICTED", "LOCKED", "STALE",
                          "PROCESSING", "RETRY"):
            assert not TaskStatus.is_known(forbidden), (
                f"TaskStatus 里出现了 {forbidden}"
            )

    def test_task_state_machine_unchanged(self):
        """
        4D.1 不许重新设计状态机。
        """
        from core.task import TRANSITIONS

        # TRANSITIONS 的值是 frozenset，不是 list
        assert set(TRANSITIONS["ACCEPTED"]) == {
            "IN_PROGRESS", "CANCELLED", "FAILED"
        }
        assert "SUBMITTED" in TRANSITIONS["IN_PROGRESS"]
        assert "VERIFIED" in TRANSITIONS["SUBMITTED"]
        assert "IN_PROGRESS" in TRANSITIONS["SUBMITTED"]
        assert "COMPLETED" in TRANSITIONS["VERIFIED"]


# ============================================================
# Guard 8：不许自动 retry 掩盖冲突
# ============================================================
class TestGuard8NoAutomaticRetry:
    def test_task_store_has_no_retry_loop(self):
        """
        save() 冲突时直接抛 —— 不许偷偷重新读取再重放。

        【为什么这条重要】
            自动重放的后果：状态变化被应用两次、
            消息被发两次，或者系统替 Agent 做了决定。

            而且它会**掩盖**冲突 —— 你再也看不到「这里撞过」。
        """
        code = code_only(CORE_DIR / "task_store.py")

        for forbidden in ("while True", "for attempt in", "retry",
                          "backoff", "sleep("):
            assert forbidden not in code, (
                f"task_store.py 里出现了 {forbidden!r} —— "
                f"不许自动重试冲突"
            )

    def test_save_does_not_recatch_conflict(self):
        """
        ConcurrentTaskModification 抛出去之后不许被自己接住。
        """
        source = (CORE_DIR / "task_store.py").read_text(encoding="utf-8")

        assert "except ConcurrentTaskModification" not in source, (
            "save() 自己接住了冲突异常 —— 那等于吞掉它"
        )

    def test_communication_has_no_retry_queue(self):
        """本阶段也没有实现通信重试。"""
        for source in sorted(COMM_DIR.glob("*.py")):
            code = code_only(source)
            for forbidden in ("retry_queue", "RetryQueue", "max_retries",
                              "backoff", "requeue"):
                assert forbidden not in code, (
                    f"communication/{source.name} 里出现了 {forbidden!r} —— "
                    f"4D.1 不实现重试系统"
                )


# ============================================================
# Guard 9 / 10：禁止项
# ============================================================
class TestGuard9NoWorkspace:
    @pytest.mark.parametrize(
        "forbidden",
        ["workspace_permissions", "workspace_members", "workspace_files",
         "WorkspaceManager", "WorkspaceGateway", "ArtifactManager",
         "open_file", "read_file", "write_file", "mkdir", "glob(",
         "listdir", "shutil"],
    )
    def test_no_workspace_concepts(self, forbidden):
        for name in SERVICE_MODULES:
            code = code_only(SERVICES_DIR / name)
            assert forbidden not in code, (
                f"services/{name} 里出现了 {forbidden!r} —— 4D.1 不碰 Workspace"
            )

    @pytest.mark.parametrize(
        "forbidden",
        ["workspace_permissions", "workspace_members", "workspace_files",
         "workspace_acl", "artifacts"],
    )
    def test_no_workspace_tables(self, forbidden, db):
        assert forbidden not in db.table_names(), (
            f"4D.1 不该创建 {forbidden} 表"
        )

    def test_result_ref_still_a_logical_reference(self):
        """result_ref 仍然只是逻辑标识 —— 不许变成路径。"""
        from core.task_execution import TaskResultRef

        for bad in ("/etc/passwd", "../x", "~/x", "https://x/y"):
            with pytest.raises(ValueError):
                TaskResultRef(result_ref=bad)


class TestGuard10NoForbiddenInfrastructure:
    @pytest.mark.parametrize(
        "forbidden",
        ["subprocess", "Popen", "os.system", "docker", "sandbox",
         "redis", "kafka", "rabbitmq", "celery", "websocket",
         "sqlalchemy", "worktree", "git ", "TaskManager", "Coordinator",
         "Scheduler", "escrow", "PaymentService", "transfer("],
    )
    def test_no_forbidden_infrastructure(self, forbidden):
        for name in SERVICE_MODULES:
            code = code_only(SERVICES_DIR / name)
            assert forbidden not in code, (
                f"services/{name} 里出现了 {forbidden!r}"
            )

    def test_no_economy_dependency(self):
        """
        4D.1 不产生任何经济后果 —— 真的碰钱必须先依赖经济系统。
        """
        targets = list(SERVICES_DIR.glob("*.py")) + list(COMM_DIR.glob("*.py"))

        for source in targets:
            for module in imported_modules(source):
                for forbidden in ("core.wallet", "core.ledger", "core.economy"):
                    assert not (module == forbidden
                                or module.startswith(forbidden + ".")), (
                        f"{source.name} 导入了 {module}"
                    )

    def test_no_strategy_changes(self):
        """
        DecisionPolicy 保持不变。

        【看纯代码】
            文档里会**合法地**提到 BrainDecisionPolicy 作为未来方向
            （「以后可以让模型来决策，接口已经在那儿了」）。
            那是说明，不是实现。守卫只能看代码。
        """
        code = code_only(CORE_DIR / "decision_policy.py")

        for forbidden in ("learn", "adaptive", "reinforce", "reward",
                          "BrainDecisionPolicy", "update_weights",
                          "train(", "gradient"):
            assert forbidden not in code, (
                f"core/decision_policy.py 的代码里出现了 {forbidden!r} —— "
                f"4D.1 不改 Agent Strategy"
            )

    def test_protocol_version_is_still_1(self):
        """
        4D.1 全部在存储 / 应用层解决 —— 没有改协议。
        """
        from communication.message import PROTOCOL_VERSION

        assert PROTOCOL_VERSION == "1"

    def test_wallet_and_ledger_untouched(self):
        """
        4D.1 只动 Task 和通信的可靠性，不动经济系统。

        【为什么只查这三个字段名】
            `version` 这种词在经济系统里可能有别的合法含义
            （比如配置版本），直接查会误报。
            而 processed_once / claim_id / failure_count
            是 4D.1 独有的名字，出现就说明改动越界了。
        """
        for name in ("wallet.py", "ledger.py", "economy.py"):
            path = CORE_DIR / name
            if path.exists():
                code = code_only(path)
                for forbidden in ("processed_once", "claim_id",
                                  "failure_count", "ConcurrentTaskModification"):
                    assert forbidden not in code, (
                        f"core/{name} 的代码里出现了 {forbidden!r} —— "
                        f"4D.1 不动经济系统"
                    )


# ============================================================
# Migration order（§二十八明确要求）
# ============================================================
class TestMigrationOrder:
    """
    【为什么单独一组】

        这个项目**真的**踩过一次：
        把索引建在一个还没补齐的列上，
        结果老库一启动就崩（no such column）。

        所以迁移顺序必须有测试钉住：

            1. CREATE TABLE
            2. COLUMN_MIGRATIONS（补列）
            3. POST_MIGRATION_INDEXES（建依赖新列的索引）
    """

    def test_new_columns_are_in_column_migrations(self):
        from storage.models import COLUMN_MIGRATIONS

        tasks_columns = {name for name, _ in COLUMN_MIGRATIONS.get("tasks", [])}
        comm_columns = {
            name for name, _ in COLUMN_MIGRATIONS.get(
                "communication_messages", []
            )
        }

        assert "version" in tasks_columns
        assert {"processed_once", "claim_id", "claimed_at",
                "failure_count"} <= comm_columns

    def test_new_columns_are_also_in_schema(self):
        """
        新库应该一次成型，不依赖 migrate()。

        （两条路都要有：CREATE TABLE 负责新库，
          COLUMN_MIGRATIONS 负责老库。）
        """
        from storage.models import SCHEMA_STATEMENTS

        schema = "\n".join(SCHEMA_STATEMENTS)

        assert "version" in schema
        assert "processed_once" in schema
        assert "claim_id" in schema

    def test_indexes_come_after_migrations(self):
        """
        POST_MIGRATION_INDEXES 里的每一条，引用的列都必须
        在 COLUMN_MIGRATIONS 或 CREATE TABLE 里 ——
        否则老库会在建索引时崩掉。
        """
        import re

        from storage.models import (
            COLUMN_MIGRATIONS,
            POST_MIGRATION_INDEXES,
            SCHEMA_STATEMENTS,
        )

        schema = "\n".join(SCHEMA_STATEMENTS)
        migrated = {
            f"{table}.{name}"
            for table, columns in COLUMN_MIGRATIONS.items()
            for name, _ in columns
        }

        for statement in POST_MIGRATION_INDEXES:
            match = re.search(r"ON\s+(\w+)\s*\(([^)]+)\)", statement)
            assert match, f"看不懂这条索引语句：{statement}"
            table, columns = match.group(1), match.group(2)
            for column in (c.strip() for c in columns.split(",")):
                assert (
                    f"{table}.{column}" in migrated or column in schema
                ), (
                    f"索引建在 {table}.{column} 上，"
                    f"但这一列既不在迁移里也不在建表语句里 —— "
                    f"老库升级时会崩"
                )

    def test_old_database_upgrades_cleanly(self, tmp_path):
        """
        造一个「老库」（没有新列），跑 initialize()，
        确认升级成功且数据没丢。
        """
        import sqlite3

        from storage.database import Database

        path = tmp_path / "legacy.db"

        # ---- 手工造一个缺列的老库 ----
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, current_trace_id TEXT,
                creator TEXT NOT NULL, contractor TEXT,
                participants TEXT, status TEXT NOT NULL, description TEXT,
                workspace_id TEXT, budget_amount REAL, budget_currency TEXT,
                agreed_price_amount REAL, agreed_price_currency TEXT,
                acceptance TEXT, required_capabilities TEXT, expires_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, note TEXT
            )
        """)
        conn.execute("""
            INSERT INTO tasks (task_id, creator, status, created_at, updated_at)
            VALUES ('T-OLD', 'agent_a', 'CREATED', '2024-01-01', '2024-01-01')
        """)
        # 【为什么要把这些列都写上】
        #     initialize() 会在这些列上建索引。如果「老库」缺列，
        #     建索引就会崩 —— 那是**测试造的老库不真实**，
        #     不是迁移顺序有问题。
        #     真实的历史 schema 是有这些列的（Step 3 就有了）。
        conn.execute("""
            CREATE TABLE communication_messages (
                message_id TEXT PRIMARY KEY,
                trace_id TEXT, conversation_id TEXT,
                from_agent TEXT, to_agent TEXT, message_type TEXT,
                created_at TEXT, sent_at TEXT, received_at TEXT,
                processed_at TEXT, transport TEXT, address TEXT,
                is_remote INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'CREATED',
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, error_message TEXT,
                payload_size INTEGER NOT NULL DEFAULT 0, payload_hash TEXT,
                payload TEXT, protocol_version TEXT, reply_to TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO communication_messages (message_id, from_agent, to_agent,
                message_type, created_at, updated_at)
            VALUES ('msg_old', 'agent_a', 'agent_b', 'SUBMIT',
                    '2024-01-01', '2024-01-01')
        """)
        conn.commit()
        conn.close()

        # ---- 升级 ----
        db = Database(str(path))
        db.initialize()

        assert "version" in set(db.columns_of("tasks"))
        assert "processed_once" in set(
            db.columns_of("communication_messages")
        )

        # ---- 老数据没丢，而且拿到了正确的默认值 ----
        row = db.fetchone("SELECT * FROM tasks WHERE task_id = 'T-OLD'")
        assert row is not None, "老数据丢了"
        assert row["version"] == 0, "老任务的版本号应该从 0 开始"

        row = db.fetchone(
            "SELECT * FROM communication_messages WHERE message_id = 'msg_old'"
        )
        assert row is not None
        assert row["processed_once"] == 0, (
            "老消息的 processed_once 应该是 0 —— "
            "我们不知道它当初处理过没有，不能假设处理过"
        )
        db.close()

    def test_migrate_is_idempotent(self, tmp_path):
        """反复 initialize() 不该报错。"""
        from storage.database import Database

        db = Database(str(tmp_path / "repeat.db"))
        for _ in range(3):
            db.initialize()

        assert "version" in set(db.columns_of("tasks"))
        db.close()

    def test_fresh_database_has_everything(self, db):
        """全新库一次成型。"""
        assert "version" in set(db.columns_of("tasks"))
        for column in ("processed_once", "claim_id", "claimed_at",
                       "failure_count"):
            assert column in set(db.columns_of("communication_messages"))
