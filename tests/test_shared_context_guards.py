"""
Step 4F 架构守卫 —— Shared Context 的边界

规范 §25 要求做完之后做一次架构一致性审查，检查五件事：

    Boundary Check      Shared Context ≠ Task Core / Message History
                        / Private Memory / Task Workspace / Permission
    Dependency Check    没有产生不允许的新依赖方向
    Source of Truth    没有出现第二套 Task Lifecycle
    Concurrency Check   没有静默覆盖
    Naming Check        Private Workspace 与 Task Workspace 没有混用

这个文件把那五项**变成可执行的断言**，而不是一次性的人工检查 ——
人工检查会随时间腐烂，守卫不会。

============================================================
【遵守本项目的 Guard 规范（ADR-0018）】
============================================================

    · 用 AST，不用字符串匹配
    · 去文档字符串（`ast.unparse` **不会**帮你做这件事）
    · 检查真实调用点，不是「文件里有没有这个词」
    · 每条都配反向测试（见文件末尾）
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DOMAIN = PROJECT_ROOT / "core" / "task_shared_context.py"
STORE = PROJECT_ROOT / "core" / "task_context_store.py"
SERVICE = PROJECT_ROOT / "services" / "task_alignment_service.py"
MODELS = PROJECT_ROOT / "storage" / "models.py"

LAYERS = ("core", "communication", "storage", "services", "llm", "agents")


# ============================================================
# 工具
# ============================================================
def code_only(path: Path) -> str:
    """
    **去掉文档字符串**之后的代码（`ast.unparse`）。

    【为什么必须有这个函数】
        本项目在「文档字符串被当成代码」上踩过 8 次以上。
        `ast.unparse` 默认**保留 docstring**，所以必须手动删掉
        每个函数/类的 body[0]（如果是字符串常量）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:]
    return ast.unparse(tree)


def imports_of(path: Path) -> set:
    """文件 import 的模块（AST）。"""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def function_named(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name}()")


def function_code_only(path: Path, name: str) -> str:
    """
    某个函数**去掉文档字符串**之后的代码。

    【为什么必须有这个（第三次踩同一个坑）】

        `test_update_before_insert` 第一版写的是：

            source = ast.unparse(commit)
            assert source.find("UPDATE") < source.find("_insert_revision")

        ast.unparse **保留文档字符串**。而 commit_revision 的文档里
        为了解释「为什么必须先 UPDATE 再 INSERT」，**大量提到了 UPDATE**。

        于是 `find("UPDATE")` 命中的是文档里那一次，
        永远在函数最前面 —— 无论真实代码顺序如何，断言都成立。

        注入「先 INSERT 再 UPDATE」之后守卫照样绿，就是这个原因。

        这和 ADR-0018 记的另外两次假绿是**同一个根因**：
            · Guard 1  查 "claim_message"，命中方法名自己
            · Guard 10 查原文，命中文档里对 BEGIN IMMEDIATE 的解释
            · 这一条    查 unparse 输出，命中文档里的 UPDATE
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:]
            return ast.unparse(node)
    raise AssertionError(f"{path.name} 里找不到函数 {name}()")


def sql_strings(node: ast.AST) -> list:
    """
    取出传给调用的**字符串参数**（含 f-string 拼出的常量片段）。

    【为什么不直接用 ast.unparse】
        那会把文档字符串一起输出 —— 而文档里常常贴着真实 SQL 的示例。
        这里只走 ast.Call 的参数，docstring（Expr 节点）天然被排除。
    """
    out = []

    def render(value) -> str:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.JoinedStr):
            return "".join(render(p) for p in value.values)
        if isinstance(value, ast.FormattedValue):
            return f"{{{ast.unparse(value.value)}}}"
        return ""

    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            for arg in call.args:
                text = render(arg)
                if text:
                    out.append(text)
    return out


def called_names(path: Path) -> set:
    """
    文件里**真实调用**的函数/方法名（AST，不是子串匹配）。

    【为什么不能用 `"open(" in source`】
        本项目的经典误报：`is_open(self)` 里含有 `open(`。
        子串匹配会把一个属性方法当成文件打开操作。

        这里只看真正的 `ast.Call` 节点的函数名 ——
        `is_open()` 的名字是 `is_open`，不是 `open`。
    """
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def imported_module_names(path: Path) -> set:
    """顶层模块名（`os.path` → `os`）。"""
    return {m.split(".")[0] for m in imports_of(path)}


def layer_dependencies(layer: str) -> set:
    deps = set()
    for source in sorted((PROJECT_ROOT / layer).glob("*.py")):
        for module in imports_of(source):
            top = module.split(".")[0]
            if top in LAYERS and top != layer:
                deps.add(top)
    return deps


# ============================================================
# 一、Boundary：Shared Context ≠ 其他五个东西
# ============================================================
class TestBoundary:
    """规范 §25 的 Boundary Check。"""

    # ---------- ≠ Task Core ----------
    def test_domain_has_no_lifecycle_fields(self):
        """
        **Shared Context ≠ Task Core**：领域对象不得携带 Task 生命周期字段。

        如果它有一个 `status`，就必然会出现「两套状态各自更新」——
        那是规范 §6 明确禁止的。
        """
        from core import task_shared_context as domain

        for name in dir(domain.TaskSharedContext):
            if name.startswith("_"):
                continue
            assert name not in (
                "status", "state", "task_status", "lifecycle_state",
                "agreed_price", "contractor", "budget",
            ), (
                f"TaskSharedContext 上有 {name} —— "
                f"那是 Task Core 的权威字段，不能有第二个来源"
            )

    def test_domain_does_not_import_task(self):
        """
        **≠ Task Core**：领域层不认识 Task 对象。

        所以它**结构上**不可能去改 Task 的状态。
        """
        assert "core.task" not in imports_of(DOMAIN), (
            "core/task_shared_context.py import 了 core.task —— "
            "领域层不该认识 Task 对象"
        )

    def test_service_never_saves_task(self):
        """
        **≠ Task Core**：应用服务不得写 Task。

        Snapshot 会**读** Task（引用领域状态），但绝不能写。
        """
        source = code_only(SERVICE)

        assert "tasks.save" not in source, (
            "TaskAlignmentService 调用了 tasks.save —— "
            "Shared Context 的操作绝不能修改 Task Core"
        )
        assert "tasks.record_offer" not in source
        assert ".transition_to(" not in source

    def test_service_only_reads_task(self):
        """读是允许的（Snapshot 要引用领域状态），但只能是读。"""
        source = code_only(SERVICE)

        assert "tasks.require" in source, (
            "Snapshot 应该通过 tasks.require() **读** Task Core "
            "来引用领域状态"
        )

    # ---------- ≠ Message History ----------
    def test_domain_does_not_import_communication(self):
        """
        **≠ Message History**：领域层不认识 Message。

        所以「消息自动变成事实」在**结构上**就不可能 ——
        没有代码路径能从 Message 走到 Context。
        """
        assert "communication" not in {
            m.split(".")[0] for m in imports_of(DOMAIN)
        }, "core/task_shared_context.py import 了 communication"

    def test_service_does_not_import_communication(self):
        """
        **≠ Message History**：应用服务也不认识 Message。

        规范 §11：Message → 自动同步 → Shared Context 是**禁止**的模式。
        所以对齐服务里不该有任何消息处理入口。
        """
        assert "communication" not in {
            m.split(".")[0] for m in imports_of(SERVICE)
        }, (
            "TaskAlignmentService import 了 communication —— "
            "那意味着可能有一条「收到消息就写 Context」的路径"
        )

    def test_no_message_type_handlers(self):
        """没有任何 `MessageType` 的分支 —— 消息不能直接驱动对齐。"""
        source = code_only(SERVICE)

        assert "MessageType" not in source
        assert "register_type_handler" not in source

    # ---------- ≠ Private Memory ----------
    def test_no_memory_write_path(self):
        """
        **≠ Private Memory**：Shared Context 不得读写 Agent 私有记忆。

        私有认知要进入共享层，只能走 Proposal → Alignment。
        """
        for path in (DOMAIN, STORE, SERVICE):
            source = code_only(path)
            for forbidden in ("write_memory", "read_memory", "list_memories",
                              "memory_path", "memory_dir"):
                assert forbidden not in source, (
                    f"{path.name} 里出现了 {forbidden} —— "
                    f"Shared Context 不该直接碰 Agent 私有记忆"
                )

    # ---------- ≠ Task Workspace ----------
    def test_no_filesystem_access(self):
        """
        **≠ Task Workspace**：不得有任何文件系统访问。

        【这条守卫自己踩过一次误报】
            第一版写的是 `"open(" in source` ——
            结果匹配到了 `AssumptionRecord.is_open()`。

            `is_open(` 里确实含有 `open(`。这是本项目的经典坑
            （ADR-0018 记录的 4 次子串误报之一）。

            现在改成 AST：只看**真正的调用点**和**真的 import 的模块**。
        """
        forbidden_imports = {"os", "pathlib", "shutil", "glob", "tempfile"}

        for path in (DOMAIN, STORE, SERVICE):
            used = imported_module_names(path) & forbidden_imports
            assert not used, (
                f"{path.name} import 了 {sorted(used)} —— "
                f"Shared Context 不该访问文件系统"
            )

            called = called_names(path)
            for forbidden_call in ("mkdir", "read_text", "write_text",
                                   "unlink", "rmdir", "listdir"):
                assert forbidden_call not in called, (
                    f"{path.name} 调用了 {forbidden_call}() —— "
                    f"Shared Context 不该访问文件系统"
                )

            # 内建 open() —— 必须精确匹配函数名，不能子串
            assert "open" not in called, (
                f"{path.name} 调用了内建 open()"
            )

    def test_artifact_refs_are_logical_only(self):
        """
        **≠ Task Workspace**：artifact_refs 只是逻辑名字。

        和 result_ref 共用同一条安全规则：不是路径、不是 URL。
        """
        from core.logical_ref import validate_logical_ref

        for bad in ("/etc/passwd", "../x", "~/y", "https://e.com/z",
                    "a/../b"):
            with pytest.raises(Exception):
                validate_logical_ref(bad)

        assert validate_logical_ref("parser-design-b") == "parser-design-b"

    # ---------- ≠ Permission ----------
    def test_no_permission_system(self):
        """
        **≠ Permission**：参与方检查复用 `Task.is_participant()`，
        没有自建 ACL / 角色 / 文件权限。
        """
        source = code_only(SERVICE)

        for forbidden in ("Permission", "ACL", "Role", "acl",
                          "check_permission", "has_permission"):
            assert forbidden not in source, (
                f"TaskAlignmentService 里出现了 {forbidden} —— "
                f"那是在自建 Permission System（4F 禁止）"
            )

    def test_participant_check_reuses_task(self):
        """参与方判定必须复用 Task 已有的方法，不是另写一套。"""
        source = code_only(SERVICE)

        assert "is_participant" in source, (
            "应该复用 Task.is_participant()，而不是自己判断参与方"
        )


# ============================================================
# 二、Dependency：没有新的违规依赖方向
# ============================================================
class TestDependency:
    """规范 §25 的 Dependency Check。"""

    def test_services_does_not_import_storage(self):
        """
        **这是 4F 开发中真实踩到并修掉的一个问题。**

            第一版 TaskAlignmentService 为了 `with self.db.transaction()`
            import 了 `storage.database` —— 依赖图变成 services → storage，
            而文档和守卫都要求 services 只依赖 core + communication。

            修法：把事务边界下移到 `TaskContextStore`，
            service 完全不需要知道 Database。
        """
        offenders = [
            source.name
            for source in sorted((PROJECT_ROOT / "services").glob("*.py"))
            if any(m.split(".")[0] == "storage" for m in imports_of(source))
        ]

        assert not offenders, (
            f"services 直接依赖了 storage：{offenders}\n"
            f"（持久化应该经 core 里的 store 间接发生。）"
        )

    def test_services_dependencies_unchanged(self):
        """4F 没有改变 services 的依赖集合。"""
        assert layer_dependencies("services") == {"core", "communication"}

    def test_core_does_not_import_services(self):
        """方向仍然是单向的。"""
        for layer in ("core", "communication"):
            assert "services" not in layer_dependencies(layer)

    def test_store_owns_the_transaction(self):
        """
        **事务边界必须只有一个归属地。**

        service 不管事务，store 管 ——
        这样「原子性」这件事只在一个文件里需要被理解。
        """
        service_source = code_only(SERVICE)

        assert "db.transaction" not in service_source, (
            "TaskAlignmentService 里出现了 db.transaction —— "
            "事务边界应该只在 store 里"
        )
        assert "commit_accepted_change" in service_source, (
            "service 应该调用 store.commit_accepted_change() "
            "来获得原子性"
        )

    def test_store_uses_shared_database(self):
        """复用现有 Database，没有自建一套（规范 §19）。"""
        for forbidden in ("sqlite3.connect", "SharedContextDatabase",
                          "SharedContextTransaction", "redis", "kafka"):
            assert forbidden not in code_only(STORE), (
                f"store 里出现了 {forbidden} —— 应该复用现有 Database"
            )

        assert "from storage.database import Database" in STORE.read_text(
            encoding="utf-8"
        )


# ============================================================
# 三、Source of Truth：没有第二套生命周期
# ============================================================
class TestSourceOfTruth:
    """规范 §25 的 Source of Truth Check。"""

    def test_shared_context_tables_have_no_status_column(self):
        """
        `task_shared_contexts` 表**没有** status 列。

        有的话就会出现：
            Task.status = SUBMITTED
            Context.status = IN_PROGRESS
        两个都声称正确。
        """
        source = MODELS.read_text(encoding="utf-8")
        block = source[
            source.index("CREATE TABLE IF NOT EXISTS task_shared_contexts"):
            source.index("CREATE TABLE IF NOT EXISTS task_context_revisions")
        ]

        assert "status" not in block, (
            "task_shared_contexts 表里出现了 status 列 —— "
            "那是第二套 Task 生命周期"
        )

    def test_domain_has_no_state_machine(self):
        """
        领域模块里**没有** Task 状态机。

        `TRANSITIONS` 只该在 `core/task.py` 里存在一份。
        """
        source = code_only(DOMAIN)

        assert "TRANSITIONS" not in source, (
            "core/task_shared_context.py 里出现了 TRANSITIONS —— "
            "Task 状态机只能有一份（在 core/task.py）"
        )

        from core.task import TRANSITIONS as task_transitions
        from core import task_shared_context as domain

        assert not hasattr(domain, "TRANSITIONS")
        assert len(task_transitions) == 12, "Task 状态机被改动了"

    def test_reject_cannot_change_revision(self):
        """
        **行为层面**：被拒绝的变更不得改变当前 Context。

        （这里做的是静态+运行时双重确认：
          静态确认 reject() 不碰 commit；
          运行时确认由 test_task_shared_context.py 覆盖。）
        """
        reject_fn = function_named(SERVICE, "reject")
        source = ast.unparse(reject_fn)

        assert "commit_revision" not in source, (
            "reject() 里出现了 commit_revision —— "
            "拒绝一个变更绝不能推进版本"
        )
        assert "commit_accepted_change" not in source

    def test_snapshot_is_derived(self):
        """
        Snapshot 是派生视图，没有独立写入路径。

        它的 `domain_state` 明确标注来源是 task_core（引用，不是副本）。
        """
        from core.task_shared_context import ContextSnapshot

        assert "domain_state_source" in {
            f.name for f in ContextSnapshot.__dataclass_fields__.values()
        }
        # 默认值必须表明它来自 Task Core
        import dataclasses

        fields = {f.name: f for f in dataclasses.fields(ContextSnapshot)}
        assert fields["domain_state_source"].default == "task_core"


# ============================================================
# 四、Concurrency：没有静默覆盖
# ============================================================
class TestConcurrency:
    """规范 §25 的 Concurrency Check。"""

    def test_commit_uses_conditional_update(self):
        """
        推进版本必须是**带条件**的 UPDATE（compare-and-set）。

        条件里必须有 `current_revision = ?` ——
        没有它就没有乐观锁，就是静默覆盖。
        """
        commit = function_named(STORE, "commit_revision")
        updates = [
            sql for sql in sql_strings(commit)
            if sql.strip().upper().startswith("UPDATE")
        ]

        assert len(updates) == 1, f"commit_revision 里的 UPDATE 有 {len(updates)} 条"
        sql = " ".join(updates[0].split())
        assert "current_revision = ?" in sql.split("WHERE")[-1], (
            f"commit_revision 的 WHERE 里没有 current_revision 条件 —— "
            f"那就没有乐观锁。SQL: {sql}"
        )

    def test_update_before_insert(self):
        """
        **顺序必须是 UPDATE 再 INSERT。**

        反过来的话，冲突时那条 revision 已经写进去了 ——
        会留下一条「从没被 current 指向过」的孤立历史版本。
        """
        # 【必须用去掉文档字符串的版本】
        #     第一版用 ast.unparse(commit)，而文档里为了解释这个顺序
        #     本身就提到 "UPDATE" —— 于是 find("UPDATE") 命中文档，
        #     断言永远成立（假绿）。
        source = function_code_only(STORE, "commit_revision")

        update_at = source.find("UPDATE")
        insert_at = source.find("_insert_revision")

        assert update_at != -1, "commit_revision 里找不到 UPDATE"
        assert insert_at != -1, "commit_revision 里找不到 _insert_revision"
        assert update_at < insert_at, (
            "commit_revision 里 _insert_revision 出现在 UPDATE 之前 —— "
            "冲突时会留下孤立的历史版本"
        )

    def test_no_automatic_retry_or_merge(self):
        """
        规范 §13：禁止自动 retry / merge / rebase / LLM merge。

        冲突必须**明确失败**，由调用方显式基于新版本重来。
        """
        for path in (STORE, SERVICE):
            source = code_only(path)
            for forbidden in ("retry", "auto_merge", "rebase", "llm_merge",
                              "backoff", "max_attempts"):
                assert forbidden not in source, (
                    f"{path.name} 里出现了 {forbidden} —— "
                    f"4F 不做自动冲突解决"
                )

    def test_conflict_raises_domain_error(self):
        """冲突必须抛明确的领域错误。"""
        from core.task_shared_context import (
            ConcurrentContextModification,
            ContextError,
        )

        assert issubclass(ConcurrentContextModification, ContextError)

        error = ConcurrentContextModification("t", 3, 5)
        assert "3" in str(error) and "5" in str(error)

    def test_revisions_and_changes_are_append_only(self):
        """
        revisions / changes 表只能 INSERT / SELECT。

        有任何 UPDATE 或 DELETE 都会让历史可以被改写 ——
        而「Revision 18 取代 17」绝不等于「17 可以删掉」。

        ============================================================
        【这条守卫第一版是**假的**（反向测试时被发现）】
        ============================================================

            第一版从**表名**推常量名：

                pattern = f"{verb} {{{table.upper()}}}"
                # table = "task_context_revisions"
                # → "UPDATE {TASK_CONTEXT_REVISIONS}"

            但代码里用的是常量：

                f"UPDATE {TABLE_CONTEXT_REVISIONS} SET payload = ?"

            常量名是 `TABLE_CONTEXT_REVISIONS`（有 TABLE_ 前缀），
            表和常量名**并不相同** —— 所以这个模式**永远匹配不到**。

            注入一条真实的 `UPDATE {TABLE_CONTEXT_REVISIONS}` 之后，
            守卫照样绿。

        ------------------------------------------------------------
        【现在的查法】
            不再靠猜名字，而是**遍历所有真实 SQL 字符串**，
            看有没有哪一条是「写操作 且 指向这两张表」——
            同时认出常量占位符和字面表名两种写法。
        """
        tree = ast.parse(STORE.read_text(encoding="utf-8"))
        flat_sql = [" ".join(s.split()) for s in sql_strings(tree)]

        def touches(sql: str, constant: str, table: str) -> bool:
            return f"{{{constant}}}" in sql or table in sql

        violations = []

        # ---- revisions：**纯 append-only** ----
        for sql in flat_sql:
            if not touches(sql, "TABLE_CONTEXT_REVISIONS",
                           "task_context_revisions"):
                continue
            upper = sql.upper()
            if upper.startswith("UPDATE") or "DELETE" in upper:
                violations.append(f"revisions 被改写: {sql[:70]}")

        # ---- changes：不许 DELETE；UPDATE 只能改 status ----
        for sql in flat_sql:
            if not touches(sql, "TABLE_CONTEXT_CHANGES",
                           "task_context_changes"):
                continue
            upper = sql.upper()
            if "DELETE" in upper:
                violations.append(f"changes 被删除: {sql[:70]}")

        assert not violations, (
            "revisions 必须纯 append-only、changes 不许删除，但发现：\n  "
            + "\n  ".join(violations)
            + "\n（历史一旦可以被改写，「Revision 18 取代 17」就退化成"
              "「17 被删了」。）"
        )

    def test_changes_status_update_is_conditional(self):
        """
        `changes` 表**允许**一次 status 转移（PROPOSED → ACCEPTED/REJECTED），
        但那条 UPDATE **必须带 `AND status = ?` 条件**。

        【为什么】
            先 SELECT 看是不是 PROPOSED，再 UPDATE ——
            两个进程同时裁决时都会看到 PROPOSED，然后都写成功。
            这就是 TOCTOU。

            带条件的 UPDATE 让数据库来裁决：影响行数 0 = 别人先改了。
            这和 Phase 0 修消息认领用的是同一个手法。
        """
        decide = function_named(STORE, "decide_change")
        updates = [
            s for s in sql_strings(decide)
            if s.strip().upper().startswith("UPDATE")
        ]

        assert len(updates) == 1, (
            f"decide_change 里应该有且只有一条 UPDATE，"
            f"找到 {len(updates)} 条"
        )
        sql = " ".join(updates[0].split())
        where = sql.split("WHERE")[-1]

        assert "status = ?" in where, (
            f"decide_change 的 UPDATE 没有 `AND status = ?` 条件 —— "
            f"两个进程同时裁决会都成功。SQL: {sql}"
        )

    def test_append_only_detection_recognises_the_constants(self):
        """
        反向：确认检测**真的认得**那两种写法。

        不然 `test_revisions_and_changes_are_append_only` 可能
        只是因为「什么也没匹配到」而永远为真。
        """
        for sample in (
            "UPDATE {TABLE_CONTEXT_REVISIONS} SET payload = ?",
            "DELETE FROM {TABLE_CONTEXT_REVISIONS} WHERE task_id = ?",
            "DELETE FROM task_context_changes",
            "DELETE FROM {TABLE_CONTEXT_CHANGES} WHERE change_id = ?",
        ):
            upper = sample.upper()
            assert upper.startswith("UPDATE") or "DELETE" in upper

            matched = any(
                target in sample
                for target in ("{TABLE_CONTEXT_REVISIONS}",
                               "{TABLE_CONTEXT_CHANGES}",
                               "task_context_revisions",
                               "task_context_changes")
            )
            assert matched, f"检测漏掉了：{sample}"

        # INSERT / SELECT 不该被误判成写操作
        for safe in (
            "INSERT INTO {TABLE_CONTEXT_REVISIONS} (...) VALUES (...)",
            "SELECT * FROM {TABLE_CONTEXT_REVISIONS} WHERE task_id = ?",
        ):
            upper = safe.upper()
            assert not (upper.startswith("UPDATE") or "DELETE" in upper), (
                f"只读语句被误判成写操作：{safe}"
            )

    def test_schema_has_no_cascade_delete_for_revisions(self):
        """revisions 表不该有级联删除（那会静默丢历史）。"""
        source = MODELS.read_text(encoding="utf-8")
        block = source[
            source.index("CREATE TABLE IF NOT EXISTS task_context_revisions"):
            source.index("CREATE TABLE IF NOT EXISTS task_context_changes")
        ]

        assert "ON DELETE CASCADE" not in block


# ============================================================
# 五、Naming：Private Workspace 与 Task Workspace 没有混用
# ============================================================
class TestNaming:
    """规范 §25 的 Naming Check。"""

    def test_shared_context_does_not_touch_workspace(self):
        """
        Shared Context 不得碰 Workspace（无论哪个 Workspace）。

        `workspace_id` 仍然只是逻辑标识 —— 4F 没有给它任何新含义。
        """
        for path in (DOMAIN, STORE, SERVICE):
            source = code_only(path)
            for forbidden in ("Workspace", "workspace_id", "for_agent"):
                assert forbidden not in source, (
                    f"{path.name} 里出现了 {forbidden} —— "
                    f"4F 不该引入 Workspace 概念"
                )

    def test_core_workspace_not_refactored(self):
        """
        规范 §4：不要重命名、重构或重新解释现有 `core/workspace.py`。
        """
        from core.workspace import Workspace, WorkspaceViolationError

        for method in ("for_agent", "write_text", "read_text", "list_files",
                       "write_memory", "read_memory", "list_memories"):
            assert hasattr(Workspace, method), (
                f"core/workspace.py 的 {method}() 不见了 —— "
                f"4F 不该动它"
            )
        assert issubclass(WorkspaceViolationError, Exception)

    def test_task_workspace_ref_unchanged(self):
        """`TaskWorkspaceRef` 仍然只是逻辑引用。"""
        from core.task_workspace import TaskWorkspaceRef

        for forbidden in ("path", "read_file", "write_file", "mkdir"):
            assert not hasattr(TaskWorkspaceRef, forbidden)

    def test_no_workspace_tables_added(self, db):
        """
        4F 没有建任何 **Task Workspace** 相关的表。

        【⚠️ 这条守卫踩过「子串匹配误报」】

            原来写的是 `"worktree" not in names` ——
            那是**子串**匹配。第二阶段引入 `git_worktrees`
            （Change Pipeline 的 Git worktree，**不是** Task Workspace）
            之后，它就**假红**了。

            这是本项目反复记录的那一类错误
            （`agent_directory` 含 `agent_d`、`seen_release` 含 `lease`、
              现在 `git_worktrees` 含 `worktree`）。

            改成**精确表名**匹配 —— 顺带把守卫变得更强：
            它现在真的能区分「Task Workspace 的表」和「Git worktree 的表」。
        """
        names = set(db.table_names())

        for forbidden in ("workspace_permissions", "workspace_members",
                          "workspace_files", "workspace_acl", "artifacts",
                          "workspaces", "workspace_backends"):
            assert forbidden not in names, (
                f"出现了 {forbidden} 表 —— 那属于 Task Workspace，4F 禁止"
            )


# ============================================================
# 六、Forbidden：规范 §28 的禁止项
# ============================================================
class TestForbidden:
    """4F 明确禁止顺手实现的东西。"""

    @pytest.mark.parametrize(
        "module_path",
        ["core/task_shared_context.py", "core/task_context_store.py",
         "services/task_alignment_service.py"],
    )
    def test_no_forbidden_imports(self, module_path):
        modules = imports_of(PROJECT_ROOT / module_path)
        tops = {m.split(".")[0] for m in modules}

        for forbidden in ("redis", "kafka", "celery", "numpy", "requests",
                          "subprocess", "socket", "urllib"):
            assert forbidden not in tops, (
                f"{module_path} import 了 {forbidden} —— 4F 禁止"
            )

    def test_no_execution_or_git(self):
        for path in (DOMAIN, STORE, SERVICE):
            source = code_only(path)
            for forbidden in ("subprocess", "Popen", "git", "worktree",
                              "Sandbox", "exec(", "eval("):
                assert forbidden not in source, (
                    f"{path.name} 里出现了 {forbidden} —— 4F 禁止"
                )

    def test_no_llm_import(self):
        """
        规范 §11：不能用 LLM 从消息里「猜出一个决定」。
        所以对齐层完全不认识 LLM。
        """
        for path in (DOMAIN, STORE, SERVICE):
            assert "llm" not in {
                m.split(".")[0] for m in imports_of(path)
            }, f"{path.name} import 了 llm"

    def test_no_voting_or_consensus(self):
        """规范 §9：不实现 voting / quorum / CRDT / blockchain。"""
        for path in (DOMAIN, STORE, SERVICE):
            source = code_only(path)
            for forbidden in ("quorum", "vote", "voting", "crdt",
                              "blockchain", "consensus", "majority"):
                assert forbidden not in source.lower(), (
                    f"{path.name} 里出现了 {forbidden} —— 4F 不做共识算法"
                )

    def test_no_task_manager_or_scheduler(self):
        """
        `AssignmentRecord` **只是记录**，不是 Scheduler（§7.7）。
        """
        for path in (DOMAIN, STORE, SERVICE):
            source = code_only(path)
            for forbidden in ("TaskManager", "Coordinator", "Scheduler",
                              "WorkerPool", "dispatch", "rebalance"):
                assert forbidden not in source, (
                    f"{path.name} 里出现了 {forbidden} —— "
                    f"那是在往 TaskManager 方向漂移"
                )


# ============================================================
# 七、反向测试
# ============================================================
class TestReverseVerification:
    """
    本项目的硬规定（ADR-0018）：新增守卫**必须**配反向测试。

    这里验证的是**检测机制本身**有效，
    而不是重新断言一遍业务规则。
    """

    def test_code_only_strips_docstrings(self):
        """
        `code_only()` 必须真的去掉文档字符串。

        【为什么这条最关键】
            本项目在「文档字符串被当成代码」上踩过 8 次以上。
            如果 `code_only` 是假的，上面一大半守卫都会假绿。
        """
        stripped = code_only(DOMAIN)

        # 领域模块的文档字符串里有大量中文说明
        assert "纯领域，零 I/O" not in stripped, (
            "code_only 没有去掉文档字符串 —— 所有基于它的守卫都不可信"
        )
        # 但真实代码必须在
        assert "class TaskSharedContext" in stripped

    def test_imports_detection_works(self):
        """AST import 检测必须真的能发现依赖。"""
        assert "core.logical_ref" in imports_of(DOMAIN), (
            "import 检测说领域模块不依赖 logical_ref —— 那是错的"
        )

    def test_imports_negative_case(self):
        """反向：已知不存在的依赖必须检测为没有。"""
        assert "core.task" not in imports_of(DOMAIN)

    def test_sql_extraction_finds_the_update(self):
        """SQL 提取必须能找到那条带条件的 UPDATE。"""
        commit = function_named(STORE, "commit_revision")
        updates = [
            s for s in sql_strings(commit)
            if s.strip().upper().startswith("UPDATE")
        ]
        assert len(updates) == 1

    def test_sql_extraction_excludes_docstrings(self):
        """
        SQL 提取必须**排除**文档字符串。

        【这是本项目真实踩过的坑】
            `ast.unparse` 保留 docstring，而文档里贴着 SQL 示例 ——
            于是守卫数到的是文档里的那条，不是代码里的那条。
        """
        assert "纯领域" not in " ".join(sql_strings(
            function_named(STORE, "commit_revision")
        )), "SQL 提取把文档字符串也算进来了"

    def test_function_lookup_raises_for_missing(self):
        """找不到函数时必须**报错**，不能静默返回。"""
        with pytest.raises(AssertionError, match="找不到"):
            function_named(STORE, "this_function_does_not_exist")

    def test_function_code_only_strips_the_update_mention(self):
        """
        反向：证明「文档里的 UPDATE」确实是陷阱，而新取码方式躲开了。

        【这是第三次同根因的假绿】
            commit_revision 的文档里解释了「为什么先 UPDATE 再 INSERT」，
            所以文档里**必然**含 "UPDATE" 这个词。
            用 ast.unparse(commit) 去 find("UPDATE") 一定命中文档。
        """
        with_doc = ast.unparse(function_named(STORE, "commit_revision"))
        without_doc = function_code_only(STORE, "commit_revision")

        # ---- ① 陷阱存在：文档里确实在解释 UPDATE 的顺序 ----
        marker = "先 UPDATE 再 INSERT"
        assert marker in with_doc, (
            "文档里本该有对 UPDATE 顺序的解释 —— 测试前提不成立"
        )

        # ---- ② 新取码方式确实把文档去掉了 ----
        assert marker not in without_doc, (
            "function_code_only 没有去掉文档字符串"
        )
        assert without_doc.lstrip().startswith("def commit_revision"), (
            "取出来的应该是函数本身"
        )

        # ---- ③ 去掉文档后，UPDATE 只出现在真实 SQL 里 ----
        #     （不要比较两个不同长度字符串的绝对下标 ——
        #       去掉文档后所有位置都会前移，那个比较没有意义。）
        first = without_doc.find("UPDATE")
        assert first != -1
        prefix = without_doc[:first]
        assert "conn.execute" in prefix or 'f"""' in prefix, (
            f"去掉文档后，UPDATE 之前应该只有 execute 调用，实际前缀："
            f"{prefix[-80:]!r}"
        )

    def test_function_code_only_raises_for_missing(self):
        with pytest.raises(AssertionError, match="找不到"):
            function_code_only(STORE, "nope_not_here")

    def test_status_column_check_would_catch_a_status_column(self):
        """
        反向：如果 `task_shared_contexts` 真的有 status 列，
        `test_shared_context_tables_have_no_status_column` 必须变红。

        这里用一段**伪造的** DDL 验证检测逻辑本身。
        """
        fake = """
        CREATE TABLE IF NOT EXISTS task_shared_contexts (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            current_revision INTEGER NOT NULL
        )
        CREATE TABLE IF NOT EXISTS task_context_revisions ( )
        """
        block = fake[
            fake.index("CREATE TABLE IF NOT EXISTS task_shared_contexts"):
            fake.index("CREATE TABLE IF NOT EXISTS task_context_revisions")
        ]
        assert "status" in block, "检测逻辑本身有问题"

    def test_append_only_check_would_catch_an_update(self):
        """反向：伪造一条 UPDATE，确认检测模式能匹配到。"""
        fake = "UPDATE {TABLE_CONTEXT_REVISIONS} SET payload = ?"
        flat = " ".join(fake.split())
        pattern = "UPDATE {TABLE_CONTEXT_REVISIONS}"

        assert pattern in flat, "append-only 检测模式匹配不到伪装的 UPDATE"

    def test_filesystem_guard_distinguishes_is_open_from_open(self):
        """
        反向：`is_open` 不能被当成 `open`。

        【这条测试在钉一个**真实发生过的**误报】

            第一版守卫写的是：

                assert "open(" not in source

            结果在 `def is_open(self)` 上变红了 ——
            因为 `is_open(` 里确实含有 `open(`。

            这个测试同时确认两件事：
                ① 陷阱**真实存在**（原文里确实有 "open("）
                ② 新检测**不再上当**（called_names 里没有 "open"）
        """
        raw = DOMAIN.read_text(encoding="utf-8")
        called = called_names(DOMAIN)
        defined = {
            node.name
            for node in ast.walk(ast.parse(raw))
            if isinstance(node, ast.FunctionDef)
        }

        # ① 陷阱存在：原文里确实有 "open("，而且 is_open 是个**定义**
        assert "open(" in raw, (
            "领域模块里本该有 is_open(...) —— 测试前提不成立"
        )
        assert "is_open" in defined
        assert "is_open" not in called, "is_open 是被定义，不是被调用"

        # ② 新检测不上当
        assert "open" not in called, (
            "AST 检测把 is_open 也算成了 open —— 检测坏了"
        )

    def test_workspace_forbidden_import_check_is_live(self):
        """
        反向：确认 `Workspace` 这个词在**其他地方**是存在的 ——
        否则「领域模块里不许出现 Workspace」这条断言可能只是
        因为整个项目都没有这个词而永远为真。
        """
        economy = code_only(PROJECT_ROOT / "core" / "economy.py")

        assert "Workspace" in economy, (
            "core/economy.py 里没有 Workspace —— "
            "那说明检测目标本身有问题"
        )


# ============================================================
# 十、守卫：Task 诞生时，必须同时诞生它的 Shared Context
# ============================================================
#
# 【为什么有这一节 —— 用户指出的缺陷】
#
#     「这明显之前实现任务创建的时候把它漏了。
#       Agent 创建任务的时候就该把这个给创建好了。」
#
#   之前建 Task 的地方散落在 `main.py` / `experiments/`，
#   各自直接 `rt.tasks.save(Task.create(...))` ⇒ **谁也没建容器**。
#
#   后果（实测）：观察实验里 agent_a 想指派验收人，
#   `assign_acceptance` 报 `no_shared_context` 失败 ——
#   它想合作，环境只给了它「会失败」的告知，没给「能做到」的路径。
#
# 【这条守卫防什么】
#   防「新写的 runner / 脚本又绕过一次」。
#   文本匹配做不到 —— `task = Task.create(...)` 和
#   `rt.tasks.save(task)` 隔着几行，子串匹配看不出来。所以用 AST。
#
# 【判定规则】每个 `Task.create(...)` 必须满足其中一条：
#
#     (a) 直接嵌套       `rt.create_task(Task.create(...))`
#     (b) 同函数内交接   `X = Task.create(...)` 且调用了 `create_task(X)`
#     (c) 自己建了容器   同函数内调用了 `create_context(`
#     (d) 裸 store 演示  同函数内自建 `TaskStore(` —— 那是**独立数据库**，
#                        根本接不到 Runtime 的 Alignment
#
#   否则 ⇒ 违规（除非函数在豁免名单里，且**豁免本身被反向测试证明**）。

#: 只要函数里出现这些调用，「整函数」就算过关（对应规则 c / d）。
#:
#: 🔴 **`create_task` 刻意不在这里。**
#:    它是规则 (a)/(b)，必须**精确到每一次创建** ——
#:    放进来的话，「函数里给别的 task 建了容器」就会让整函数免检。
#:    第一版就是这么写的，被反向测试
#:    `test_a_container_for_SOMEONE_ELSE_does_not_count` 抓住了。
FUNCTION_LEVEL_CONTAINER_CALLS = frozenset({"create_context"})

#: 允许「建 Task 但不建容器」的函数 —— 每条都由 `TestExemptionsAreEarned`
#: 证明它确实另有出路。**这不是白名单，是待证明的断言。**
CREATE_WITHOUT_CONTAINER_EXEMPTIONS = {
    "open_offer": (
        "收到 TASK_OFFER 这条路径：容器由紧跟在 `store.save(task)` 之后的 "
        "`_ensure_shared_context(task)` 建立。**顺序不能反** —— "
        "`create_context` 会 `_require_task()` 去库里查，Task 还没落盘就 "
        "`KeyError`（实测踩到过，而且当时被 `try/except` 吞了，"
        "多绕了好几步才看出来）。"
    ),
}


def _callee_name(node):
    """`f(...)` → "f" · `a.b(...)` → "b" · 其余 → None。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _is_task_create(node):
    """
    是不是 `Task.create(...)`。

    【为什么不能只看 `.create`】
        `create_stage(` / `create_context(` 里都没有 `create`，
        但 `Task.create` 必须是 `Task` 这个**名字**上的 `create` ——
        否则 `Something.create(...)` 会被误判。
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "create"
        and isinstance(func.value, ast.Name)
        and func.value.id == "Task"
    )


def _handed_to_create_task(create_call, parents, fn):
    """
    这一次 `Task.create(...)` 有没有**当场**交给 `create_task(...)`。

    【为什么要管到赋值】
        本项目的真实写法是两行，不是嵌套：

            task = Task.create(...)
            runtime.create_task(task)

        所以要看「**这个名字**有没有被传给 `create_task`」，
        而不是「函数里有没有出现 `create_task`」——
        后者会被「给别的 task 建了容器」骗过去。
    """
    parent = parents.get(id(create_call))

    # (a) 直接嵌套：`rt.create_task(Task.create(...))`
    if _callee_name(parent) == "create_task":
        return True

    # 名字来源：`X = Task.create(...)`
    names = set()
    if isinstance(parent, ast.Assign) and parent.value is create_call:
        names = {t.id for t in parent.targets if isinstance(t, ast.Name)}
    if not names:
        return False

    # (b) `create_task(X)` —— 传的必须**就是这个名字**
    for call in ast.walk(fn):
        if _callee_name(call) != "create_task":
            continue
        passed = list(call.args) + [k.value for k in call.keywords]
        if any(isinstance(a, ast.Name) and a.id in names for a in passed):
            return True
    return False


def task_creations_without_a_container(source):
    """
    返回 `[(函数名, 行号)]` —— 建了 Task，却没建它的 Shared Context。

    【为什么两种粒度混用】
        (a)(b) 必须**精确到这一次创建** —— 否则「函数里恰好有
        `create_task`」就蒙混过关（见反向测试
        `test_a_container_for_SOMEONE_ELSE_does_not_count`）。
        (c)(d) 天然是「这个函数整体在做什么」，只能函数级判断。
    """
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        creates = [n for n in ast.walk(fn) if _is_task_create(n)]
        if not creates:
            continue
        if fn.name in CREATE_WITHOUT_CONTAINER_EXEMPTIONS:
            continue

        called = {_callee_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}
        if called & FUNCTION_LEVEL_CONTAINER_CALLS or "TaskStore" in called:
            continue

        for create_call in creates:
            if not _handed_to_create_task(create_call, parents, fn):
                found.append((fn.name, create_call.lineno))
    return found


def runtime_entry_files():
    """真正会「建 Task」的入口：组合根 + 实验 + 应用服务。"""
    files = [PROJECT_ROOT / "main.py"]
    for folder in ("experiments", "services"):
        files.extend(sorted((PROJECT_ROOT / folder).rglob("*.py")))
    return [p for p in files if p.is_file()]


def _called_names_in(node):
    return {_callee_name(n) for n in ast.walk(node) if isinstance(n, ast.Call)}


class TestEveryTaskGetsItsContainer:
    def test_no_entry_point_creates_a_task_without_its_container(self):
        offenders = []
        for path in runtime_entry_files():
            for fn_name, lineno in task_creations_without_a_container(
                path.read_text(encoding="utf-8")
            ):
                rel = path.relative_to(PROJECT_ROOT)
                offenders.append(
                    f"  {rel}:{lineno}  in {fn_name}()\n"
                    f"      → 请用 `Runtime.create_task(...)`，"
                    f"否则这个 Task 永远没有 Shared Context，"
                    f"它之后**连验收人都指派不了**"
                )
        assert not offenders, (
            "有 Task 被建出来，却没有同时建它的 Shared Context：\n\n"
            + "\n".join(offenders)
        )

    def test_the_scan_is_not_vacuous(self):
        """
        反向：守卫真的扫到了 `Task.create`。

        否则「一个违规都没有」可能只是因为扫错了路径 ——
        这类**空转的守卫**比没有守卫更危险。
        """
        total = 0
        for path in runtime_entry_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            total += sum(1 for n in ast.walk(tree) if _is_task_create(n))

        assert total >= 10, (
            f"扫描范围里只找到 {total} 个 `Task.create` —— "
            f"扫描路径或 AST 判定坏了，断言变成空转"
        )

    def test_a_store_level_demo_must_not_touch_the_runtime_alignment(self):
        """
        规则 (d) 不是漏洞：豁免掉的那两个 demo **真的**只操作独立库。

        它们自建 `TaskStore(db_b)` —— 那是一个独立 SQLite 文件，
        上面根本没有 Alignment / Shared Context 这回事。
        但如果它们同时又去用 `runtime.tasks` / `runtime.alignment`，
        那规则 (d) 就成了后门。
        """
        tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
        demos = {
            "cmd_reliability_demo",
            "cmd_reliability_multiprocess_demo",
        }
        checked = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name not in demos:
                continue
            checked += 1
            touched = {
                n.attr
                for n in ast.walk(fn)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id in ("runtime", "rt")
            }
            assert "tasks" not in touched and "alignment" not in touched, (
                f"`{fn.name}` 既自建裸 TaskStore，又碰 runtime.tasks/"
                f"runtime.alignment —— 规则 (d) 在这里是漏洞"
            )
            assert "TaskStore" in _called_names_in(fn), (
                f"`{fn.name}` 没有自建 TaskStore —— 规则 (d) 不成立"
            )
        assert checked == len(demos), (
            f"只找到 {checked}/{len(demos)} 个裸 store 演示 —— "
            f"函数被改名或删掉了，这条测试不再有效"
        )


class TestTheGuardIsNotFooled:
    """反向测试 —— 把违规注入进去，确认守卫**真的变红**。"""

    def test_it_flags_a_direct_save_of_a_fresh_task(self):
        src = "def f(rt):\n    rt.tasks.save(Task.create(creator='x'))\n"

        assert task_creations_without_a_container(src) == [("f", 2)]

    def test_it_flags_the_two_statement_form_without_create_task(self):
        """真实踩到的形状：先建、几行后再 save。"""
        src = (
            "def f(rt):\n"
            "    task = Task.create(creator='x')\n"
            "    task.offer()\n"
            "    rt.tasks.save(task)\n"
        )

        assert task_creations_without_a_container(src) == [("f", 2)]

    def test_it_accepts_the_nested_form(self):
        src = "def f(rt):\n    rt.create_task(Task.create(creator='x'))\n"

        assert task_creations_without_a_container(src) == []

    def test_it_accepts_the_two_statement_form(self):
        src = (
            "def f(rt):\n"
            "    task = Task.create(creator='x')\n"
            "    rt.create_task(task)\n"
        )

        assert task_creations_without_a_container(src) == []

    def test_a_container_for_SOMEONE_ELSE_does_not_count(self):
        """
        本守卫最容易写歪的一条。

        第一版规则是「函数里出现 `create_task` 就算过」——
        那样下面这段就能蒙混过关。正确行为是**只放过第 2 行**。
        """
        src = (
            "def f(rt):\n"
            "    other = Task.create(creator='y')\n"
            "    rt.create_task(other)\n"
            "    mine = Task.create(creator='x')\n"
            "    rt.tasks.save(mine)\n"
        )

        assert task_creations_without_a_container(src) == [("f", 4)]

    def test_a_similar_method_name_is_not_mistaken_for_an_exemption(self):
        """
        `create_stage(` 里没有 `create`，但反向也要成立：
        它**不能**被当成建了容器。
        """
        src = (
            "def f(rt):\n"
            "    create_stage(stage_id='S')\n"
            "    rt.tasks.save(Task.create(creator='x'))\n"
        )

        assert task_creations_without_a_container(src) == [("f", 3)]

    def test_other_classes_create_is_not_task_create(self):
        """`Workspace.create(...)` 不是 `Task.create(...)`。"""
        src = (
            "def f(rt):\n"
            "    Workspace.create(agent_id='a')\n"
            "    rt.tasks.save(Task.create(creator='x'))\n"
        )

        assert task_creations_without_a_container(src) == [("f", 3)]


class TestExemptionsAreEarned:
    """豁免名单不是白名单 —— 每条都要证明它**确实另有出路**。"""

    def test_every_exemption_still_points_at_a_real_function(self):
        names = set()
        for path in runtime_entry_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names |= {
                n.name
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        missing = set(CREATE_WITHOUT_CONTAINER_EXEMPTIONS) - names

        assert not missing, (
            f"豁免名单指向了不存在的函数：{sorted(missing)} —— "
            f"函数被改名/删掉了，豁免就变成了永久免罪符"
        )

    def test_open_offer_really_builds_the_container_itself(self):
        service = PROJECT_ROOT / "services" / "task_negotiation_service.py"
        called = _called_names_in(function_named(service, "open_offer"))

        assert "_ensure_shared_context" in called, (
            "`open_offer` 被豁免了，却没有自己建容器 —— 豁免失效"
        )

    def test_the_context_demo_builds_its_own_container_too(self):
        """
        `cmd_context_demo` 走规则 (c)：它自己 `create_context(...)`。

        它演示的就是 Alignment 机制本身，所以**故意**手工建 Revision 1
        并带上 objective / constraints / acceptance_criteria ——
        结束后状态是完整的（Task + 带内容的容器），不是缺一块。
        """
        called = _called_names_in(function_named(PROJECT_ROOT / "main.py",
                                                "cmd_context_demo"))

        assert "create_context" in called, (
            "`cmd_context_demo` 既不交给 create_task 也没自己 create_context —— "
            "那它建的 Task 就是没有容器的"
        )


# ============================================================
# 十一、守卫：清 Task 的清单必须覆盖**所有**指向 tasks 的表
# ============================================================
#
# 【这条守卫在钉一个**真实发生过的崩溃**（2026-10-10）】
#
#     `_reset_demo_tasks` 里那张清单的注释自己写着
#     「顺序很重要：先删子表，再删 tasks」—— 作者是知道这个坑的。
#     但 Step 4F 加的 `task_shared_contexts` 系列**从没加进去**。
#
#     以前不暴露，是因为以前建 Task 根本不建 Shared Context。
#     `Runtime.create_task` 开始建容器之后：
#         · `workspace-demo`  跑完十步，死在**最后一行清理**
#         · `negotiation-demo` **第二次**跑必崩
#     错误都是 `IntegrityError: FOREIGN KEY constraint failed`。
#
# 【为什么用 PRAGMA 而不是把表名抄进测试】
#     手写表名会随新表再次腐烂 —— 那正是这个 bug 的成因。
#     PRAGMA 直接问数据库「谁指向 tasks」，答案永远是最新的。


def cleanup_lists_covering_tasks(source):
    """
    `main.py` 里所有「包含 `'tasks'` 的表名元组」→ `[(函数名, [表名…])]`。

    形状是 `for table in ("a", "b", "tasks"):`。用 AST 找，
    不看注释、不看字符串匹配。
    """
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Tuple):
            continue
        names = [
            e.value
            for e in node.iter.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if "tasks" not in names:
            continue

        owner, cursor = "<module>", parents.get(id(node))
        while cursor is not None:
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = cursor.name
                break
            cursor = parents.get(id(cursor))
        found.append((owner, names))
    return found


def tables_pointing_at_tasks(db):
    """数据库里**所有**外键指向 `tasks` 的表名（PRAGMA 问出来的）。"""
    everything = {
        row["name"]
        for row in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    children = set()
    for table in sorted(everything):
        for fk in db.fetchall(f"PRAGMA foreign_key_list({table})"):
            if fk["table"] == "tasks":
                children.add(table)
    return children


class TestTaskCleanupStaysInSyncWithTheSchema:
    def test_every_cleanup_list_covers_every_table_that_points_at_tasks(
        self, tmp_path
    ):
        from storage.database import Database

        db = Database(str(tmp_path / "schema.db"))
        db.initialize()
        try:
            children = tables_pointing_at_tasks(db)
        finally:
            db.close()

        assert children, "没有任何表指向 tasks —— 测试前提不成立"

        lists = cleanup_lists_covering_tasks(
            (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        )
        assert lists, (
            "main.py 里找不到任何「清 Task」的表名清单 —— "
            "函数被改名或重构成别的形状了，这条守卫已经失效"
        )

        problems = []
        for owner, declared in lists:
            missing = children - set(declared)
            if missing:
                problems.append(
                    f"  {owner}() 漏了：{sorted(missing)}\n"
                    f"      当前清单：{declared}"
                )
            elif declared[-1] != "tasks":
                problems.append(
                    f"  {owner}() 的 `tasks` 不在最后 —— "
                    f"必须先删子表（现在最后一项是 {declared[-1]!r}）"
                )

        assert not problems, (
            "清 Task 的清单和 schema 不一致，演示重跑会崩在\n"
            "`IntegrityError: FOREIGN KEY constraint failed`：\n\n"
            + "\n".join(problems)
            + f"\n\n  数据库里指向 tasks 的表：{sorted(children)}"
        )


class TestTheCleanupGuardIsNotFooled:
    def test_it_finds_the_list(self):
        src = (
            "def f(conn):\n"
            "    for table in ('a', 'tasks'):\n"
            "        conn.execute(table)\n"
        )

        assert cleanup_lists_covering_tasks(src) == [("f", ["a", "tasks"])]

    def test_a_tuple_without_tasks_is_ignored(self):
        src = (
            "def f(conn):\n"
            "    for table in ('a', 'b'):\n"
            "        conn.execute(table)\n"
        )

        assert cleanup_lists_covering_tasks(src) == []

    def test_it_is_not_fooled_by_a_comment_mentioning_tasks(self):
        """注释里写 `tasks` 不算 —— 必须是真的元组元素。"""
        src = (
            "def f(conn):\n"
            "    # 顺序很重要：先删子表，再删 tasks\n"
            "    for table in ('a', 'b'):\n"
            "        conn.execute(table)\n"
        )

        assert cleanup_lists_covering_tasks(src) == []

    def test_a_missing_table_is_really_reported(self, tmp_path):
        """
        反向：故意漏一张表，确认**真的会被判为不一致**。

        这条很关键 —— 上面那个「全过」的断言可能只是因为
        `children` 算空了，而不是因为清单真的完整。
        """
        from storage.database import Database

        db = Database(str(tmp_path / "schema.db"))
        db.initialize()
        try:
            children = tables_pointing_at_tasks(db)
        finally:
            db.close()

        # 抄一份**故意漏掉** `task_shared_contexts` 的假清单
        fake = [c for c in sorted(children) if c != "task_shared_contexts"] + [
            "tasks"
        ]

        assert children - set(fake) == {"task_shared_contexts"}, (
            "反向测试前提不成立：`task_shared_contexts` 没有指向 tasks"
        )
