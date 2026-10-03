"""
Test: Workspace Phase 0 架构守卫
============================================================

这一组守卫锁住 Workspace Phase 0 的边界。

【为什么这一组特别重要】

    Phase 0 修的两件事都是「**删掉也不会让功能测试变红**」的类型：

        · 把 BEGIN IMMEDIATE 改回 BEGIN
              → 单进程测试全绿，只有真多进程才随机 database is locked
        · 把 PID 感知的连接缓存去掉
              → spawn 下全绿，只有 fork 才炸
        · 把租约判断挪到 Handler 里
              → 单进程仍然「能用」，但两个 worker 会各判断各的

    所以必须有守卫专门盯着它们还在、而且位置对。

【守卫清单】

    Guard 1  不能靠 global / singleton 连接实现多进程共享
    Guard 2  Message claim 的正确性不能依赖 SeenMessageTracker
    Guard 3  Lease reclaim 必须在 persistence layer
    Guard 4  Handler 不得自己判断 lease 是否过期
    Guard 5  TaskExecutionService 不得参与 Message claim lifecycle
    Guard 6  Workspace 相关代码必须仍不存在
    Guard 7  不得新增 Retry Scheduler
    Guard 8  不得新增 Heartbeat
    Guard 9  不得新增 Message Queue

    Guard 10 写事务必须是 BEGIN IMMEDIATE（Phase 0 测出来的真实缺陷）
    Guard 11 连接缓存必须是 PID 感知的
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMM_DIR = PROJECT_ROOT / "communication"
CORE_DIR = PROJECT_ROOT / "core"
SERVICES_DIR = PROJECT_ROOT / "services"
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

    文档里会合法地解释「我们**不做**什么」以及贴着真实的 SQL 示例，
    守卫只看代码，否则会被自己的说明文字绊倒。
    （这个坑这一路踩过七次了。）
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


def flat(path: Path) -> str:
    """纯代码 + 压平空白（便于匹配跨行的 SQL）。"""
    return " ".join(code_only(path).split())


def _sql_strings(node: ast.AST) -> list:
    """
    把一段 AST 里**所有字符串参数**取出来（包括 f-string）。

    【为什么要单独一个函数】
        直接用 ast.unparse 会连**文档字符串**一起输出 ——
        而文档里常常贴着真实 SQL 的示例。

        这个函数只走 ast.Call 的参数，所以文档字符串
        （一个 Expr 节点，不是 Call）天然被排除。

        对 f-string，把其中的常量片段拼起来：
            f"UPDATE {TABLE} SET x = ?"  →  "UPDATE  SET x = ?"
        （占位符本身取不到值，但关键字还在，够用来数语句。）
    """
    found = []

    def render(value) -> str:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.JoinedStr):
            return "".join(render(part) for part in value.values)
        if isinstance(value, ast.FormattedValue):
            return f"{{{ast.unparse(value.value)}}}"
        return ""

    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        for arg in call.args:
            text = render(arg)
            if text:
                found.append(text)

    return found


def imported_modules(path: Path) -> list:
    modules = []
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


# ============================================================
# Guard 1：不靠 global / singleton 连接
# ============================================================
class TestGuard1NoGlobalConnection:
    def test_no_module_level_connection(self):
        """
        数据库连接不许做成模块级全局变量 ——
        那样每个进程都会「共享」同一个对象（fork 时尤其危险）。
        """
        for base in (STORAGE_DIR, COMM_DIR, CORE_DIR, SERVICES_DIR):
            for source in sorted(base.glob("*.py")):
                tree = parse(source)
                for node in tree.body:      # 只看模块顶层
                    if not isinstance(node, ast.Assign):
                        continue
                    value = ast.unparse(node.value)
                    assert "sqlite3.connect" not in value, (
                        f"{source.name} 在模块级建立了数据库连接 —— "
                        f"多进程会共享它"
                    )
                    assert "Database(" not in value, (
                        f"{source.name} 在模块级建了 Database —— "
                        f"多进程会共享它"
                    )

    def test_no_global_keyword_for_connections(self):
        for base in (STORAGE_DIR, COMM_DIR, CORE_DIR, SERVICES_DIR):
            for source in sorted(base.glob("*.py")):
                code = code_only(source)
                assert "global _conn" not in code
                assert "global connection" not in code

    def test_database_is_instantiable_per_process(self):
        """Database 应该能被每个进程各自实例化，互不影响。"""
        from storage.database import Database

        a = Database(":memory:")
        b = Database(":memory:")
        a.initialize()
        b.initialize()

        assert a.connect() is not b.connect()
        a.close()
        b.close()

    def test_database_does_not_use_a_connection_pool(self):
        """
        本阶段明确不引入连接池 —— 它是另一个层次的复杂度。
        """
        code = code_only(STORAGE_DIR / "database.py")

        for forbidden in ("pool", "Pool", "singleton", "Singleton",
                          "_instances", "ThreadLocal"):
            assert forbidden not in code, (
                f"storage/database.py 里出现了 {forbidden!r} — "
                f"本阶段不引入连接池 / 单例"
            )


# ============================================================
# Guard 2：claim 正确性不依赖内存 tracker
# ============================================================
class TestGuard2ClaimNotMemoryBased:
    def test_claim_goes_through_recorder(self):
        """
        Manager 的认领必须落到持久化层。

        （查**真正的调用**，而不是方法名 ——
          `_claim_message` 这个名字永远都在。）
        """
        code = flat(COMM_DIR / "manager.py")

        assert "self.recorder.claim_message(" in code, (
            "Manager 没有调用持久化认领 —— 去重会退回内存"
        )

    def test_recorder_claim_is_conditional(self):
        """认领必须是带条件的 UPDATE，不是盲写。"""
        code = flat(COMM_DIR / "recorder.py")

        assert "processed_once = 0" in code
        assert "claim_id IS NULL" in code

    def test_memory_tracker_is_not_the_decider(self, db):
        """
        行为证明：把内存 tracker 换成「永远说没见过」的坏实现，
        去重仍然必须工作。
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
                          message_id="msg_guard2")

        assert comm._claim_message(message, "agent_b").claimed
        recorder.mark_processed(message, "agent_b")

        assert comm._claim_message(message, "agent_b").duplicate, (
            "缓存坏掉时数据库仍然必须拦住重复"
        )


# ============================================================
# Guard 3：lease reclaim 在持久化层
# ============================================================
class TestGuard3LeaseInPersistenceLayer:
    def test_lease_decision_is_in_sql(self):
        """
        租约过期的判断必须在 SQL 的 WHERE 里。

        放在 Python 里（先查再改）会有 TOCTOU 窗口：
        两个进程同时判断「过期了」→ 都去改。
        """
        code = flat(COMM_DIR / "recorder.py")

        assert "claimed_at < ?" in code, (
            "租约过期判断必须在 SQL 的 WHERE 里"
        )
        assert "claimed_at IS NULL" in code, (
            "claimed_at 缺失时也该可以被认领"
        )

    def test_only_one_update_in_claim_message(self):
        """
        claim_message 只应该有一条 UPDATE —— 多条意味着先查再改。

        【这条守卫原来查错了东西 —— 记下来免得再犯】

            原来写的是：

                body = ast.unparse(claim_fn)
                assert body.count("UPDATE communication_messages") == 1

            它确实通过了，但**不是因为代码正确**：

              · `ast.unparse` **不会去掉文档字符串**
              · 而 claim_message 的文档里贴了一段 SQL 示例，
                里面正好有 "UPDATE communication_messages"

            所以那个 1 是**文档里的**，不是代码里的。
            真正的 SQL 用的是 f-string 常量：

                UPDATE {TABLE_COMM_MESSAGES} SET ...

            unparse 之后它长成 `UPDATE {TABLE_COMM_MESSAGES}` ——
            根本匹配不到 "communication_messages"。

            【现在的查法】
                只数**真正传给 execute() 的 SQL 字符串**里
                有几条 UPDATE。文档字符串不是 Call，自然被排除。
        """
        tree = parse(COMM_DIR / "recorder.py")
        claim_fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "claim_message"
        )

        # 【必须是「以 UPDATE 开头」，不能只查「含 UPDATE」】
        #     认领那一段里还有一条 INSERT：
        #         INSERT INTO ... ON CONFLICT(...) DO UPDATE SET ...
        #     它里面也有 "UPDATE" 这个词 ——
        #     只查「含 UPDATE」会把它也算成一条更新语句。
        #
        #     按语句开头判断才准：这条 execute() 到底是不是
        #     一条独立的 UPDATE 语句。
        updates = [
            sql for sql in _sql_strings(claim_fn)
            if sql.strip().upper().startswith("UPDATE")
        ]

        assert len(updates) == 1, (
            f"claim_message 里的 UPDATE 语句有 {len(updates)} 条，应该恰好 1 条 —— "
            f"多条意味着「先查再改」的 TOCTOU 窗口。\n"
            f"（找到的：{[u.strip()[:60] for u in updates]}）"
        )

    def test_lease_config_lives_on_the_recorder(self):
        """租约时长是持久化层的配置，不是业务层的。"""
        from communication.recorder import CommunicationRecorder

        import inspect

        signature = inspect.signature(CommunicationRecorder.__init__)
        assert "claim_lease_seconds" in signature.parameters

    def test_lease_does_not_reset_processed_once(self):
        """
        租约相关的地方**不许**动 processed_once。

        （认领的 UPDATE 的 SET 列表里不能有它 ——
          否则一次超时回收就会把「成功处理过」的事实抹掉。）
        """
        source = (COMM_DIR / "recorder.py").read_text(encoding="utf-8")

        marker = "SET claim_id = ?, claimed_at = ?, updated_at = ?"
        assert marker in source, "找不到认领的 UPDATE"

        start = source.index(marker)
        end = source.index("WHERE", start)
        set_block = source[start:end]

        assert "processed_once" not in set_block, (
            "认领 / 回收的 SET 列表里动了 processed_once —— "
            "租约绝不能改变成功事实"
        )
        assert "failure_count" not in set_block, (
            "认领 / 回收的 SET 列表里动了 failure_count —— "
            "worker 崩溃不算业务失败"
        )

    def test_lease_does_not_delete_messages(self):
        """超时回收不是删除消息。"""
        code = flat(COMM_DIR / "recorder.py")

        assert "DELETE FROM communication_messages" not in code, (
            "recorder 里出现了删除消息的语句 —— "
            "超时回收只该让占用失效，不该删数据"
        )


# ============================================================
# Guard 4 / 5：业务层不许碰 claim lifecycle
# ============================================================
class TestGuard4And5NoClaimLogicInServices:
    @pytest.mark.parametrize("name", SERVICE_MODULES)
    @pytest.mark.parametrize(
        "forbidden",
        ["claim_message", "claim_id", "claimed_at", "lease",
         "processed_once", "mark_processed", "mark_failed",
         "lease_seconds", "is_expired"],
    )
    def test_services_do_not_touch_claim_lifecycle(self, name, forbidden):
        """
        Handler / TaskService / ExecutionService 都不参与消息认领。

        认领是**通信持久化层**的事 —— 业务层不该知道
        「这条消息谁认领了、租约过期没有」。
        """
        code = code_only(SERVICES_DIR / name)

        # 【⚠️ 必须用**词边界**匹配】
        #   `release()` 包含子串 "lease" —— 直接查子串会**假红**
        #   （`services/workspace_gateway.py` 实测踩到）。
        #   这类「子串匹配误报」在本项目已出现过多次：
        #       `agent_directory` 含 `agent_d`、`seen_tracker` 含 `ack`。
        import re as _re
        assert not _re.search(rf"\b{_re.escape(forbidden)}\b", code), (
            f"services/{name} 里出现了 {forbidden!r} —— "
            f"消息认领生命周期属于 communication 持久化层"
        )

    def test_execution_service_does_not_import_recorder(self):
        for name in SERVICE_MODULES:
            for module in imported_modules(SERVICES_DIR / name):
                assert "recorder" not in module, (
                    f"services/{name} 导入了 {module} —— "
                    f"执行业务不该参与消息认领"
                )

    def test_services_have_no_lease_predicate(self):
        """业务层里不许出现「这个租约过期了吗」这种判断。"""
        for name in SERVICE_MODULES:
            code = flat(SERVICES_DIR / name)
            for forbidden in ("expired", "is_expired", "lease_expired"):
                assert forbidden not in code, (
                    f"services/{name} 里出现了 {forbidden!r}"
                )


# ============================================================
# Guard 6：Workspace 仍然不存在
# ============================================================
class TestGuard6WorkspaceStillAbsent:
    @pytest.mark.parametrize(
        "forbidden",
        ["workspace_permissions", "workspace_members", "workspace_files",
         "workspace_acl", "WorkspaceManager", "WorkspaceGateway",
         "ArtifactManager", "open_file", "read_file", "write_file",
         "mkdir", "listdir", "shutil"],
    )
    def test_no_workspace_concepts(self, forbidden):
        """
        【设计阶段守卫 → 实现阶段的过渡】

            这条原来表达「Phase 0 不实现 Workspace」。
            Workspace Core 第一阶段已**授权实现**，
            所以「不存在」这个断言过时了。

            但仍要防**被移除的概念** ——
            允许 Workspace Core 的正式接口，
            禁止 Authorization / Delegation / Cascade Revocation 之类。
        """
        allowed = {
            "WorkspaceGateway", "WorkspaceStore", "ResourceTree",
            "Resource", "ExplicitLock", "ResourceLockRegistry",
            "workspace_resources", "workspace_explicit_locks",
        }
        if forbidden in allowed:
            pytest.skip(
                f"{forbidden!r} 是 Workspace Core v1 的正式接口（已授权实现）"
            )

        for base in (SERVICES_DIR, COMM_DIR):
            for source in sorted(base.glob("*.py")):
                assert forbidden not in code_only(source), (
                    f"{source.name} 里出现了 {forbidden!r} —— "
                    f"该概念已被 ADR-0027 移除/降级"
                )

    @pytest.mark.parametrize(
        "forbidden",
        ["workspace_permissions", "workspace_members", "workspace_files",
         "workspace_acl", "artifacts"],
    )
    def test_no_workspace_tables(self, forbidden, db):
        assert forbidden not in db.table_names()

    def test_task_workspace_ref_is_still_a_reference(self):
        from core.task_workspace import TaskWorkspaceRef

        for forbidden in ("read_file", "write_file", "mkdir", "open_file",
                          "path", "resolve", "exists"):
            assert not hasattr(TaskWorkspaceRef, forbidden)


# ============================================================
# Guard 7 / 8 / 9：不许引入的重型机制
# ============================================================
class TestGuard7NoRetryScheduler:
    @pytest.mark.parametrize(
        "forbidden",
        ["retry_queue", "RetryQueue", "retry_scheduler", "RetryScheduler",
         "schedule_retry", "backoff", "exponential", "requeue",
         "next_attempt_at", "scheduled_at"],
    )
    def test_no_retry_scheduler(self, forbidden):
        for base in (COMM_DIR, SERVICES_DIR, CORE_DIR):
            for source in sorted(base.glob("*.py")):
                assert forbidden not in code_only(source), (
                    f"{source.name} 里出现了 {forbidden!r} —— "
                    f"Phase 0 不实现重试调度"
                )

    def test_lease_expiry_does_not_schedule_anything(self):
        """
        租约过期只让占用失效 —— **不会**安排一次重试。

        （没有调度器，所以「过期之后谁来处理」仍然需要外部再投递。
          这是如实的状态，不是骗人的。）
        """
        code = flat(COMM_DIR / "recorder.py")

        for forbidden in ("schedule", "enqueue", "retry_at", "next_retry"):
            assert forbidden not in code


class TestGuard8NoHeartbeat:
    @pytest.mark.parametrize(
        "forbidden",
        ["heartbeat", "Heartbeat", "renew_lease", "renewal",
         "keepalive", "keep_alive", "liveness", "ping_loop"],
    )
    def test_no_heartbeat_or_lease_renewal(self, forbidden):
        for base in (COMM_DIR, SERVICES_DIR, CORE_DIR):
            for source in sorted(base.glob("*.py")):
                assert forbidden not in code_only(source), (
                    f"{source.name} 里出现了 {forbidden!r} —— "
                    f"Phase 0 只做「一次认领 + 超时回收」，不做续租"
                )


class TestGuard9NoMessageQueue:
    @pytest.mark.parametrize(
        "forbidden",
        ["kafka", "redis", "rabbitmq", "celery", "broker",
         "message_queue", "amqp", "pika", "nats"],
    )
    def test_no_message_queue(self, forbidden):
        for base in (COMM_DIR, SERVICES_DIR, CORE_DIR, STORAGE_DIR):
            for source in sorted(base.glob("*.py")):
                assert forbidden not in code_only(source).lower(), (
                    f"{source.name} 里出现了 {forbidden!r}"
                )

    def test_no_sqlalchemy_or_orm(self):
        for base in (COMM_DIR, SERVICES_DIR, CORE_DIR, STORAGE_DIR):
            for source in sorted(base.glob("*.py")):
                for module in imported_modules(source):
                    for forbidden in ("sqlalchemy", "peewee", "django",
                                      "pydantic", "asyncio_pool"):
                        assert not module.startswith(forbidden), (
                            f"{source.name} 导入了 {module}"
                        )


# ============================================================
# Guard 10：写事务必须是 BEGIN IMMEDIATE
# ============================================================
class TestGuard10WriteTransactionsAreImmediate:
    """
    【这条守卫来自一个真实测出来的缺陷】

        Workspace Phase 0 用真进程测试时发现：
        两个进程并发 `save()` 时，其中一个拿到的是

            OperationalError: database is locked

        而不是 `ConcurrentTaskModification`。

        原因：`BEGIN`（延迟事务）时先拿读锁，写的时候要**升级**。
        两个进程都持读锁、都要升级 → 死锁 →
        SQLite 立刻返回 BUSY，**不理会 busy_timeout**。

        改成 `BEGIN IMMEDIATE` 之后，写锁在事务开始就拿，
        不存在升级步骤，并发保护才真正生效。

        如果谁把它改回 `BEGIN`，单进程测试**全部照绿** ——
        所以必须有这条守卫。
    """

    def test_write_transaction_uses_begin_immediate(self):
        """
        【必须看纯代码】

            这个文件的文档里为了解释「为什么改成 IMMEDIATE」，
            大量提到了 `BEGIN IMMEDIATE`。

            如果查原文，把代码改回 `BEGIN` 之后守卫照样绿 ——
            因为注释里的说明还在。
            （反向测试时正是这么发现的。）
        """
        code = code_only(STORAGE_DIR / "database.py")

        assert "BEGIN IMMEDIATE" in code, (
            "写事务必须用 BEGIN IMMEDIATE —— "
            "用 BEGIN 会在多进程下变成 database is locked，"
            "从而静默绕过并发保护"
        )

    def test_read_only_transactions_can_defer(self):
        """只读事务走普通 BEGIN（可以并发读）。"""
        code = code_only(STORAGE_DIR / "database.py")

        assert "'BEGIN'" in code or '"BEGIN"' in code, (
            "只读事务应该还能用普通 BEGIN"
        )
        assert "if write else" in code or "write else" in code, (
            "应该按 write 参数在两种 BEGIN 之间切换"
        )

    def test_transaction_defaults_to_write(self):
        """
        默认必须是写事务。

        把默认设成「读」的代价是：忘了传参数的写路径会在
        多进程下随机 database is locked —— 而且很难查。
        """
        import inspect

        from storage.database import Database

        signature = inspect.signature(Database.transaction)
        assert "write" in signature.parameters
        assert signature.parameters["write"].default is True, (
            "transaction() 的默认必须是写事务（write=True）"
        )

    def test_read_only_callers_declare_themselves(self):
        """真正只读的方法应该显式声明 write=False。"""
        source = (CORE_DIR / "task_store.py").read_text(encoding="utf-8")

        assert "transaction(write=False)" in source, (
            "至少 TaskStore 的只读方法应该显式声明 write=False"
        )

    def test_journal_mode_is_not_set_unconditionally(self):
        """
        `PRAGMA journal_mode = WAL` 需要**独占访问**。

        每个连接都无条件执行它，多进程同时连接时会互相撞成
        database is locked（这也是真测出来的）。

        必须先读当前模式，只有不是 WAL 时才切换。
        """
        # 【看纯代码，不看原文】
        #     原文的注释里贴着真实的 SQL 示例
        #     （"原来是无条件执行：PRAGMA journal_mode = WAL"）——
        #     查原文会定位到那段说明文字上。
        code = flat(STORAGE_DIR / "database.py")

        # 【标记要不带引号】
        #     ast.unparse 会把字符串统一成单引号：
        #         conn.execute('PRAGMA journal_mode').fetchone()[0]
        #     所以 "PRAGMA journal_mode)" 匹配不到（中间隔了一个引号）。
        #     用不带引号的前缀最稳。
        marker = "PRAGMA journal_mode"
        set_marker = "PRAGMA journal_mode = WAL"

        assert set_marker in code, "找不到 journal_mode 设置"
        assert code.count(marker) >= 2, (
            "设置 WAL 之前必须先读一次当前模式 —— "
            "否则每个连接都做一次排他操作，多进程下互相撞死。"
            f"（纯代码里 'PRAGMA journal_mode' 只出现了 "
            f"{code.count(marker)} 次，应该至少 2 次：读一次 + 设一次）"
        )
        assert code.index(marker) < code.index(set_marker), (
            "读取当前 journal_mode 必须发生在**设置 WAL 之前**"
        )


# ============================================================
# Guard 11：连接缓存必须 PID 感知
# ============================================================
class TestGuard11ConnectionCacheIsPidAware:
    """
    【这条也来自真实测试】

        fork 之后子进程会继承父进程的 `self._conn`。
        SQLite 明确说明跨 fork 复用连接不安全。

        原来看不出问题，因为 macOS 默认 spawn（不可能继承）。
        但 Linux 默认 fork —— 那时就会踩到。
    """

    def test_database_records_owner_pid(self, db):
        """
        【为什么从「查源码字符串」改成「查行为」】

            原来断言的是 `"_owner_pid" in source` ——
            那是**查实现细节**，而不是查行为（ADR-0018 明确反对）。

            结果：把连接缓存从「一个 `_owner_pid`」重构成
            「每线程一个 session（session.pid）」之后，
            保护的行为**完全没变**，守卫却**假红**了。

            现在断言真实行为：`owner_pid` 真的能报出当前进程。
        """
        import os

        assert db.owner_pid == os.getpid(), (
            "Database 必须能报出连接是哪个进程建的"
        )

    def test_session_records_pid(self, db):
        """PID 必须记在 session 上，且来自 os.getpid()。"""
        import os

        db.connect()
        session = db._peek_session()
        assert session is not None, "连接之后必须有 session"
        assert session.pid == os.getpid()

    def test_owner_pid_is_exposed(self, db):
        """给测试用的只读属性。"""
        import os

        assert db.owner_pid == os.getpid()

    def test_connect_does_not_reuse_across_pid(self):
        """
        行为证明：pid 变了就必须重建连接。

        ⚠️ 用**真实的机制**伪造「我是另一个进程」——
           改当前 session 的 pid，而不是改一个已经不存在的外部字段。
           （这也是为什么行为断言比源码字符串断言更耐用：
             它跟着重构走，而字符串断言只会假红。）
        """
        import os

        from storage.database import Database

        database = Database(":memory:")
        database.initialize()
        original = database.connect()
        original_session = database._peek_session()
        assert original_session is not None

        # 伪造「我是另一个进程」—— session 上的 pid 才是指挥这个行为的字段
        original_session.pid = -1

        rebuilt = database.connect()

        assert rebuilt is not original, (
            "PID 变了之后必须重建连接，而不是复用继承来的那个"
        )
        assert rebuilt is not original_session.conn
        new_session = database._peek_session()
        assert new_session is not None and new_session is not original_session
        assert new_session.pid == os.getpid(), "新 session 必须记新的 pid"

        database.close()


# ============================================================
# 迁移（老库升级仍然要正常）
# ============================================================
class TestMigrationStillWorks:
    def test_phase0_added_no_schema_columns(self):
        """
        Phase 0 **没有**改 schema —— 租约复用了 4D.1 的
        claim_id / claimed_at，没有新增字段。

        如果将来要加，这条测试会提醒你同时更新迁移测试。
        """
        from storage.models import COLUMN_MIGRATIONS

        comm = {name for name, _ in COLUMN_MIGRATIONS.get(
            "communication_messages", [])}

        assert comm == {"processed_once", "claim_id", "claimed_at",
                        "failure_count"}, (
            f"Phase 0 不该新增 communication_messages 的列，实际：{comm}"
        )

    def test_columns_still_exist(self, db):
        columns = set(db.columns_of("communication_messages"))
        for column in ("processed_once", "claim_id", "claimed_at",
                       "failure_count"):
            assert column in columns

    def test_old_lease_data_is_readable(self, db):
        """
        4D.1 写的 claim（有 claim_id / claimed_at、没有租约概念）
        必须仍然能被 Phase 0 的代码正确解读。
        """
        from core.identity import utc_now_iso

        from communication.recorder import CommunicationRecorder

        # 模拟 4D.1 时期留下的数据
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO communication_messages "
                "(message_id, from_agent, to_agent, message_type, "
                " created_at, updated_at, status, claim_id, claimed_at) "
                "VALUES ('msg_legacy','a','b','SUBMIT','t','t','PROCESSING',"
                "        'old-worker','2020-01-01T00:00:00+00:00')"
            )

        recorder = CommunicationRecorder(db, claim_lease_seconds=60)

        # 那个 claim 早就过期了 → 应该可以接手
        claim = recorder.claim_message(
            type("M", (), {
                "message_id": "msg_legacy", "trace_id": None,
                "conversation_id": None, "from_agent": "a",
                "to_agent": "b", "message_type": "SUBMIT",
            })(),
            "new-worker",
        )

        assert claim.claimed is True
        assert claim.reclaimed is True
        assert claim.previous_owner == "old-worker"
