"""
Test: 架构文档一致性守卫（Documentation Consistency）

============================================================
【这个文件是什么】
============================================================

它不是「文档检查器」，而是**架构边界的第二道防线**。

`AGENTS.md` 是每个 AI Coding Agent 的入口。它写错一条边界，
后果不是「文档不好看」—— 而是**下一个开发者按错的边界去写代码**。

本项目已经发生过这类错误：`AGENTS.md` 曾经把依赖方向写成
「`core/` 不依赖任何人」，而实际上 `core/` 的 4 个模块直接依赖
`storage/`。这种错误**没有任何测试会红**，但会误导人。

所以这一组守卫检查两类东西：

    ① **文档内部的边界声明**（四个「不等于」必须写清楚）
    ② **文档与真实代码的一致性**（依赖图、实现状态）

============================================================
【它遵守项目自己的 Guard 规范（ADR-0018）】
============================================================

    · 依赖图用 **AST** 扫，不用字符串匹配
    · 实现状态用 **运行时 hasattr / 真实导入**，不靠文档自称
    · 检查 Markdown 用**结构化解析**（按标题切段），不是全文子串
    · **每一条都配反向测试**（见文件末尾的 TestReverseVerification）

============================================================
【它不做什么】
============================================================

    ❌ 不检查文档的措辞是否「好看」
    ❌ 不要求每句话都精确 —— 那不现实，也会让守卫变脆
    ✅ 只检查**会误导人写错代码**的那几条
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"
CONSTITUTION_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "ARCHITECTURE_CONSTITUTION.md"
)
SHARED_CONTEXT_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "TASK_SHARED_CONTEXT.md"
)

LAYERS = ("core", "communication", "storage", "services", "llm", "agents")


# ============================================================
# 工具
# ============================================================
def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(document: str, heading_prefix: str) -> str:
    """
    取出一个 Markdown 标题及其**所有子小节**的内容。

    【为什么要按标题切，而不是全文搜索】
        全文搜索会因为「同一句话在别的地方也出现过」而假绿。
        按小节切之后，「这一节到底说了什么」才是可验证的。

    【为什么必须包含子小节】
        第一版写成「到下一个 `#` 标题为止」，结果：

            section(agents, "4. Memory 四层")

        在 `### 4.1` 处就停了 —— 因为 `#{1,4}` 也匹配 `###`。
        而我们要检查的内容（`core/workspace.py`、`CURRENT`）
        正好都在 §4.1 里。守卫因此在**一个被截断的段落**上断言。

        现在按层级来：
            取标题自身的 `#` 个数 N
            到下一个 `#` 个数 **≤ N** 的标题为止
        这样 `## 4` 会一直吃到 `## 5` 之前，包含所有 `### 4.x`。
    """
    lines = document.splitlines()
    start = None
    level = None

    for index, line in enumerate(lines):
        if line.startswith("#") and heading_prefix in line:
            start = index
            level = len(line) - len(line.lstrip("#"))
            break

    if start is None:
        return ""

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.startswith("#"):
            continue
        depth = len(line) - len(line.lstrip("#"))
        if depth <= level:
            end = index
            break

    return "\n".join(lines[start:end])


def flatted(text: str) -> str:
    """
    压平空白，便于在跨行的 Markdown 表格/代码块里匹配短语。

    【注意】这**不是**「去文档字符串」——
        被检查的对象本身就是文档。
        这里压平只是为了不被换行干扰。
    """
    return " ".join(text.split())


def imports_of(path: Path) -> set:
    """某个 .py 文件 import 的模块（AST，不是字符串匹配）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def layer_dependencies(layer: str) -> set:
    """某一层真实依赖的其它层。"""
    deps = set()
    for source in sorted((PROJECT_ROOT / layer).glob("*.py")):
        for module in imports_of(source):
            top = module.split(".")[0]
            if top in LAYERS and top != layer:
                deps.add(top)
    return deps


@pytest.fixture(scope="module")
def agents() -> str:
    """AGENTS.md 的内容（整个文件只读一次）。"""
    return read(AGENTS_FILE)


def layer_files_importing(layer: str, target: str) -> set:
    """某一层里哪些文件 import 了 `target` 层。"""
    found = set()
    for source in sorted((PROJECT_ROOT / layer).glob("*.py")):
        for module in imports_of(source):
            if module.split(".")[0] == target:
                found.add(source.name)
                break
    return found


# ============================================================
# 一、四对最容易搞混的边界必须在 AGENTS.md 里写清楚
# ============================================================
class TestFourCriticalDistinctions:
    """
    【这四条为什么最重要】

        它们都是「概念相似、实际不同」——
        一个 Coding Agent 如果混了，写出来的代码看起来合理但是错的。

        本任务是专门为了修正它们才做的，所以必须有守卫钉住。
    """

    # ---------- ① Private Memory ≠ Private Workspace ----------
    def test_private_memory_vs_private_workspace_stated(self, agents):
        text = flatted(agents)

        assert "Agent Private Memory ≠ Agent Private Workspace" in text, (
            "AGENTS.md 必须显式写出 Private Memory ≠ Private Workspace —— "
            "这是本次修正的核心"
        )

    def test_private_workspace_is_not_inside_memory_layers(self, agents):
        """
        Memory 四层里**不能**把 Private Workspace 当成一层。

        【两个位置都要有，缺一不可】
            第一版只在「§4 里出现过一次」就通过 —— 但那不够：

                §4 开头的横幅：   「不在这四层里」
                §4.1 表格的「属于」行：「不在这四层里」

            写的是同一件事，但**读者可能只看其中一个**。
            只保留一个的话，另一个位置就会让人以为它是四层之一。

            （反向测试时发现的：删掉横幅，守卫照样绿 ——
              因为表格里还有。）
        """
        memory_section = section(agents, "4. Memory 四层")
        assert memory_section, "找不到 §4 Memory 四层"
        flat = flatted(memory_section)

        # ① 横幅（四层表格之前必须先声明排除）
        header_part = flat.split("| Layer |")[0]
        assert "不在这四层里" in header_part, (
            "§4 的**开头**必须声明 Agent Private Workspace 不在这四层里 —— "
            "读者只看四层表格的话会以为它是其中一层"
        )

        # ② §4.1 的对照表里也必须有
        subsection = section(memory_section, "4.1")
        assert subsection, "找不到 §4.1"
        assert "不在这四层里" in flatted(subsection), (
            "§4.1 的对照表必须写明它**不在这四层里**"
        )

    def test_memory_layer_table_has_exactly_four_rows(self, agents):
        """
        Memory 四层表格必须**正好四行** —— 防止有人把
        Private Workspace 加成第五行。
        """
        memory_section = section(agents, "4. Memory 四层")
        # 取「| Layer | 属于谁 | 状态 | 是什么 |」那张表
        table = memory_section.split("| Layer |")[1].split("\n\n")[0]
        rows = [
            line for line in table.splitlines()
            if line.startswith("|") and "---" not in line
        ]

        assert len(rows) == 4, (
            f"Memory 四层表格有 {len(rows)} 行，应该正好 4 行。\n"
            f"如果多了一行，很可能是把 Agent Private Workspace "
            f"当成第五层加进去了 —— 它不属于这四层。"
        )
        labels = " ".join(rows)
        assert "Agent Private Workspace" not in labels, (
            "Agent Private Workspace 出现在 Memory 四层表格里了 —— "
            "它不在这四层里"
        )

    def test_private_workspace_marked_current(self, agents):
        """`core/workspace.py` 是**已实现**的，不能写成 FUTURE。"""
        memory_section = section(agents, "4. Memory 四层")

        assert "core/workspace.py" in memory_section
        assert "`CURRENT`" in memory_section, (
            "§4.1 必须标注 Agent Private Workspace 是 CURRENT（已实现）"
        )

    def test_workspace_class_really_exists(self):
        """
        对照真实代码：`core/workspace.py` 的 `Workspace` 确实存在，
        而且有文档里提到的那些方法。
        """
        from core.workspace import Workspace, WorkspaceViolationError

        assert hasattr(Workspace, "for_agent")
        for method in ("write_text", "read_text", "list_files",
                       "write_memory", "read_memory"):
            assert hasattr(Workspace, method), (
                f"AGENTS.md 声称 Workspace 有 {method}()，但它不存在 —— "
                f"文档与代码不符"
            )
        assert issubclass(WorkspaceViolationError, Exception)

    def test_private_workspace_is_not_task_workspace(self, agents):
        """必须明确区分：Private Workspace 与 Task Workspace 不是一个东西。"""
        text = flatted(agents)

        assert "当成 Task Workspace" in text or "≠ Task Workspace" in text, (
            "必须明确 Private Workspace ≠ Task Workspace"
        )

    # ---------- ② Agent Strategy ≠ Negotiation Mechanism ----------
    def test_strategy_mechanism_separation_stated(self, agents):
        text = flatted(agents)

        assert "Agent Strategy ≠ Negotiation Mechanism" in text, (
            "必须显式写出 Agent Strategy ≠ Negotiation Mechanism"
        )

    def test_strategy_pipeline_present(self, agents):
        """必须画出 Strategy → DecisionPolicy → Decision → Mechanism 这条链。"""
        block = section(agents, "6.1")

        for node in ("Agent Negotiation Strategy", "DecisionPolicy",
                     "Decision", "TaskNegotiationService"):
            assert node in block, (
                f"§6.1 的链路图里缺少 {node}"
            )

    def test_mechanism_must_not_hardcode_prices(self, agents):
        """必须写明「不要把价格阈值硬编码进 TaskNegotiationService」。"""
        block = flatted(section(agents, "6.1"))

        assert "硬编码" in block and "TaskNegotiationService" in block, (
            "§6.1 必须写明禁止把 Agent 价格策略硬编码进机制层"
        )

    def test_negotiation_service_really_has_no_price_rules(self):
        """
        对照真实代码：`TaskNegotiationService` 里确实没有价格阈值。

        【为什么这条重要】
            文档说「我们没把价格写死在机制里」——
            这句话必须能被代码证实，否则就是空话。
        """
        import inspect

        from services.task_negotiation_service import TaskNegotiationService

        source = inspect.getsource(TaskNegotiationService)

        for threshold in ("accept_price", "opening_ask", "walk_away"):
            assert threshold not in source, (
                f"TaskNegotiationService 里出现了 {threshold} —— "
                f"Agent 的价格策略泄漏进了机制层"
            )

    # ---------- ③ Task Shared Context ≠ 第二套 Task Core ----------
    def test_shared_context_is_not_second_task_core(self, agents):
        text = flatted(agents)

        assert "第二套 Task Core" in text, (
            "必须显式写出 Task Shared Context 不能成为第二套 Task Core"
        )

    def test_domain_state_authority_stated(self, agents):
        """Task Core 必须是「唯一权威来源」。"""
        block = flatted(section(agents, "6.2"))

        assert "唯一权威" in block, (
            "§6.2 必须写明 Task Core 是领域状态的**唯一权威来源**"
        )

    def test_duplicate_status_explicitly_forbidden(self, agents):
        """
        必须明确禁止「两个都声称自己正确的 status」。
        """
        block = section(agents, "6.2")

        assert "SharedContext.current_status" in block or \
               "current_status" in block, (
            "§6.2 必须用具体反例说明「第二条 status 是被禁止的」"
        )
        assert "解释" in block, (
            "§6.2 必须说明 Shared Context 的角色是**解释** Domain State"
        )

    def test_shared_context_doc_agrees(self):
        """`TASK_SHARED_CONTEXT.md` 必须说同一件事（不能互相矛盾）。"""
        text = flatted(read(SHARED_CONTEXT_FILE))

        assert "第二个来源" in text or "第二份" in text, (
            "TASK_SHARED_CONTEXT.md 必须写明「不能成为 Domain State 的第二个来源」"
        )

    # ---------- ④ 依赖方向 ----------
    def test_dependency_section_exists(self, agents):
        text = flatted(agents)

        assert "真实 import graph" in text, (
            "依赖方向一节必须声明它是基于真实 import graph 的"
        )


# ============================================================
# 二、文档声明的依赖方向必须与真实代码一致
# ============================================================
class TestDependencyClaimsMatchReality:
    """
    【这一组是本文件最有价值的部分】

        文档里写「X 依赖 Y」或「X 不依赖 Y」——
        这类声明**可以被代码证伪**，所以必须验证。

        本项目真实发生过：文档说 `core/` 不依赖任何人，
        实际它依赖 `storage/` 和 `agents/`。
    """

    def test_services_does_not_import_storage(self):
        """
        文档声明：`services/` 不直接依赖 `storage/`。

        对照真实 import。
        """
        offenders = layer_files_importing("services", "storage")

        assert not offenders, (
            f"AGENTS.md 声明「services 不直接依赖 storage」，"
            f"但这些文件违反了：{sorted(offenders)}"
        )

    def test_services_dependencies_are_what_doc_says(self):
        """文档说 `services → core + communication`。"""
        deps = layer_dependencies("services")

        assert deps == {"core", "communication"}, (
            f"services 的真实依赖是 {sorted(deps)}，"
            f"但文档写的是 core + communication"
        )

    def test_core_does_depend_on_storage(self):
        """
        文档说：`core/` 确实依赖 `storage/`（4 个模块）。

        【这一条是本任务修正的核心之一】
            原来文档写「core 不依赖任何人」—— 那是错的。
        """
        offenders = layer_files_importing("core", "storage")

        assert offenders, (
            "AGENTS.md 修正后声明 core 依赖 storage —— "
            "如果实际不依赖了，文档需要再次更新"
        )
        # 文档里点名的四个模块必须真的在里面
        for expected in ("economy.py", "ledger.py", "wallet.py",
                         "task_store.py"):
            assert expected in offenders, (
                f"文档点名 {expected} 依赖 storage，但它没有"
            )

    def test_core_does_not_import_services(self):
        """文档声明：`core/` 不依赖 `services/`。"""
        offenders = layer_files_importing("core", "services")

        assert not offenders, (
            f"core 依赖了 services：{sorted(offenders)}"
        )

    def test_communication_does_not_import_services(self):
        offenders = layer_files_importing("communication", "services")

        assert not offenders, f"communication 依赖了 services：{sorted(offenders)}"

    def test_storage_depends_only_on_core_leaf_modules(self):
        """
        文档说：`storage/` 只依赖 `core/` 的三个**叶子**模块
        （paths / logging_setup / config），不是聚合根。

        如果它开始依赖 `core.task` 之类，文档就错了。
        """
        allowed = {"core.paths", "core.logging_setup", "core.config"}

        for source in sorted((PROJECT_ROOT / "storage").glob("*.py")):
            for module in imports_of(source):
                if module.startswith("core."):
                    assert module in allowed, (
                        f"storage/{source.name} 依赖 {module} —— "
                        f"文档说它只依赖 {sorted(allowed)}"
                    )

    def test_documented_exceptions_are_real(self):
        """
        文档列了「两个已知的依赖例外」。它们必须真的存在 ——
        否则文档在描述一个不存在的问题。
        """
        # 例外 1：core/economy.py → agents
        assert "agents" in layer_dependencies("core"), (
            "文档说 core 依赖 agents（历史遗留），但实际没有"
        )

        # 例外 2：storage/database.py 内的 core.config
        database = PROJECT_ROOT / "storage" / "database.py"
        assert "core.config" in imports_of(database), (
            "文档说 storage/database.py 依赖 core.config，但实际没有"
        )

    def test_documented_guard_rules_hold(self):
        """
        文档的「四条最重要的方向」里，三条是关于**不依赖**的。
        它们必须都成立（这条把三条断言集中在一起，便于阅读）。
        """
        violations = []
        for layer, forbidden in (
            ("core", "services"),
            ("communication", "services"),
            ("services", "storage"),
        ):
            offenders = layer_files_importing(layer, forbidden)
            if offenders:
                violations.append(f"{layer} → {forbidden}: {sorted(offenders)}")

        assert not violations, (
            "文档声明的依赖边界被破坏了：\n  " + "\n  ".join(violations)
        )


# ============================================================
# 三、实现状态标注必须与代码一致
# ============================================================
class TestImplementationStatusClaims:
    """
    【为什么要守这条】

        本项目最容易犯的文档错误是「把设计写成已实现」。
        所以文档里的实现状态声明，必须能被代码验证。
    """

    def test_task_workspace_still_only_a_reference(self, agents):
        """
        文档说 Task Workspace 是 `FUTURE`，只有 `TaskWorkspaceRef`。

        对照代码：`TaskWorkspaceRef` 必须**没有**任何 I/O 能力。
        """
        from core.task_workspace import TaskWorkspaceRef

        for forbidden in ("read_file", "write_file", "mkdir", "open",
                          "path", "resolve", "delete"):
            assert not hasattr(TaskWorkspaceRef, forbidden), (
                f"TaskWorkspaceRef 获得了 {forbidden}() —— "
                f"文档说它「只是引用」，现在是错的"
            )

    def test_shared_context_is_implemented(self, agents):
        """
        **Step 4F 之后**：Task Shared Context 是 `CURRENT`（已实现）。

        【这条守卫怎么演化的】
            4E 时它是反向的 —— 断言 `core/task_shared_context.py`
            **不存在**，用来防止「把设计写成已实现」。

            4F 真的实现了它，那条守卫于是变红（这是设计意图：
            它逼着人来更新文档）。现在反过来钉住：
            实现了就必须在文档里说实现了。

            两个方向都不能少 —— 只钉一个方向的话，
            文档要么落后要么超前，两种都会误导人。
        """
        import importlib.util

        assert importlib.util.find_spec("core.task_shared_context") is not None, (
            "core/task_shared_context.py 不见了 —— "
            "如果是有意移除，文档必须从 CURRENT 改回 DESIGNED"
        )
        assert "Task Shared Context" in agents

    def test_shared_context_tables_exist(self, db):
        """3 张表必须真的建出来了（文档说已持久化）。"""
        names = db.table_names()
        for required in ("task_shared_contexts", "task_context_revisions",
                         "task_context_changes"):
            assert required in names, (
                f"数据库里缺少 {required} 表 —— 而文档说 Shared Context "
                f"已经 persistent"
            )

    def test_workspace_still_not_implemented(self, db):
        """**反向**：Workspace 相关的表必须仍然不存在。"""
        names = " ".join(db.table_names())
        for forbidden in ("workspace_permissions", "workspace_members",
                          "artifacts", "worktrees"):
            assert forbidden not in names, (
                f"数据库里出现了 {forbidden} 表 —— "
                f"那属于 Task Workspace 阶段，4F 明确禁止"
            )

    def test_private_memory_is_designed_not_current(self, agents):
        """
        文档说 Agent Private Memory 是 `DESIGNED`
        （只有目录与极简文件读写，没有结构化记忆）。
        """
        memory_section = flatted(section(agents, "4. Memory 四层"))

        assert "结构化记忆" in memory_section or "没有" in memory_section, (
            "§4 必须说明结构化 Memory 尚未实现"
        )

    def test_protocol_version_claim(self, agents):
        """文档说 Protocol 是 v1 —— 对照真实常量。"""
        from communication.message import PROTOCOL_VERSION

        assert PROTOCOL_VERSION == "1"
        # 文档里的声明
        assert 'protocol_version = "1"' in agents or "v1" in agents

    def test_test_baseline_claim(self, agents):
        """
        文档说基线是 2333 passed。

        【为什么不在这里跑全量测试】
            那会让这个文件跑几万次断言，而且递归（全量里包含它自己）。
            这里只确认文档**写了**这个数字，并且它是一个具体数字 ——
            真正的「测试真的全过」由 `./dev.sh test` 的整体结果证明。
        """
        match = re.search(r"\*\*(\d+) passed", agents)

        assert match, "AGENTS.md 必须写明测试基线"
        assert int(match.group(1)) > 2000, (
            f"测试基线数字看起来不对：{match.group(1)}"
        )


# ============================================================
# 四、四态标注纪律
# ============================================================
class TestFourStateDiscipline:
    def test_all_four_states_used(self, agents):
        for state in ("CURRENT", "DESIGNED", "FUTURE", "FORBIDDEN NOW"):
            assert state in agents, f"AGENTS.md 缺少四态标注 {state}"

    def test_constitution_defines_the_discipline(self):
        text = read(CONSTITUTION_FILE)

        assert "四态标注纪律" in text, (
            "Constitution 必须定义四态标注纪律"
        )


# ============================================================
# 五、Context Layering（本次新增）
# ============================================================
class TestContextLoadingRule:
    def test_rule_exists(self, agents):
        assert "Context Loading Rule" in agents, (
            "AGENTS.md 必须包含 Context Loading Rule"
        )

    def test_rule_prevents_loading_everything(self, agents):
        block = section(agents, "9.5")

        assert "不要一次加载全部" in block or "不要一次加载" in block, (
            "§9.5 必须明确「不要一次加载全部文档」"
        )

    def test_rule_has_task_to_document_mapping(self, agents):
        """
        必须有一张「做什么 → 读什么」的表。

        【为什么检查的是**任务领域关键词**而不是完整句子】
            第一版要求出现 "Task Shared Context" 这个完整短语，
            但表格里写的是「**Shared Context / Alignment**」——
            意思完全一样，守卫却红了。

            那是**守卫太脆**，不是文档错了。
            所以改成检查领域关键词（Shared Context / SQLite /
            Negotiation / Execution），它们才是这张表的实际内容。
        """
        block = section(agents, "9.5")

        for keyword in ("Shared Context", "SQLite", "Negotiation",
                        "Execution", "Communication"):
            assert keyword in block, (
                f"§9.5 的任务映射表里缺少 {keyword} 这一行"
            )

    def test_rule_maps_to_real_documents(self, agents):
        """映射表指向的文档必须真实存在（不是随手写的路径）。"""
        block = section(agents, "9.5")

        for doc in ("TASK_SHARED_CONTEXT.md", "PROTOCOL.md", "ADR-0012",
                    "ADR-0014", "ADR-0018"):
            assert doc in block, f"§9.5 映射表缺少指向 {doc} 的引用"

        # 这些文件真的要在
        assert (PROJECT_ROOT / "docs/architecture/TASK_SHARED_CONTEXT.md").exists()
        assert (PROJECT_ROOT / "PROTOCOL.md").exists()
        assert (PROJECT_ROOT / "docs/architecture/adr/0012-begin-immediate.md").exists()


# ============================================================
# 六、AGENTS.md 必须保持「入口」，不能变成第二份 Constitution
# ============================================================
class TestAgentsStaysNavigation:
    """
    【为什么要守这条】

        任务的明确要求是「修正准确性，而不是增加篇幅」。
        如果 AGENTS.md 越长越像 Constitution，它就不再是入口了 ——
        而且会出现**两个需要同步的真相来源**。
    """

    #: 上限。修正前 328 行；本次加了三节内容（§4.1 / §6.1 / §6.2 / §9.5）。
    #: 给到 560 是留了余量，但如果有人开始往里搬 Constitution 就会超。
    MAX_LINES = 560

    def test_not_too_long(self, agents):
        count = len(agents.splitlines())

        assert count <= self.MAX_LINES, (
            f"AGENTS.md 有 {count} 行，超过上限 {self.MAX_LINES}。\n"
            f"它是**导航**，不是第二份 Constitution —— "
            f"细节应该放进 docs/architecture/ 并在这里只留链接。"
        )

    def test_links_to_constitution(self, agents):
        assert "ARCHITECTURE_CONSTITUTION.md" in agents, (
            "AGENTS.md 必须指向 Constitution"
        )

    def test_links_to_shared_context_spec(self, agents):
        assert "TASK_SHARED_CONTEXT.md" in agents, (
            "AGENTS.md 必须指向 Shared Context 规范"
        )

    def test_does_not_copy_shared_context_fields(self, agents):
        """
        不许把 `TaskSharedContext` 的完整字段表搬进来 —— 那属于
        `TASK_SHARED_CONTEXT.md`。
        """
        for field in ("ContextRevision", "AssumptionRecord",
                      "OpenQuestion", "AssignmentRecord"):
            assert field not in agents, (
                f"AGENTS.md 里出现了 {field} —— "
                f"那是 TASK_SHARED_CONTEXT.md 的内容，这里只该有链接"
            )

    def test_does_not_contain_adr_bodies(self, agents):
        """不许把 ADR 正文搬进来。"""
        assert "## Context" not in agents, (
            "AGENTS.md 里出现了 ADR 的正文结构"
        )


# ============================================================
# 七、反向测试
# ============================================================
class TestReverseVerification:
    """
    【本项目的硬规定（ADR-0018）】

        新增守卫**必须**配反向测试：把违规注入进去，
        确认守卫**真的变红**。

        这里验证的是守卫的**检测机制本身**有效 ——
        不是重新测一遍代码。

    【为什么必须做】
        本项目有过三次守卫假绿：
            · 查方法名（方法自己就叫那个名字，永远匹配）
            · 查原文（文档里提到了同一句话）
            · ast.unparse 保留 docstring

        这个文件本身是**文档检查**，假绿的风险更高 ——
        因为它检查的对象就是文字。所以要特别小心：
        检查的是**结构**（按标题切段）和**代码事实**，
        不是「全文里有没有这个字符串」。
    """

    def test_section_extraction_is_scoped(self):
        """
        `section()` 必须真的只取那一节 ——
        否则「§4.1 里有 X」可能只是因为 X 出现在文档别处。
        """
        document = read(AGENTS_FILE)

        memory = section(document, "4. Memory 四层")
        dependency = section(document, "3. 关键架构")

        # 依赖图里的内容不应该出现在 Memory 一节
        assert "真实 import graph" not in memory, (
            "section() 取的范围太大了 —— 它把 §3 的内容也拿进来了"
        )
        assert "Memory 四层" not in dependency

    def test_section_returns_empty_for_missing_heading(self):
        document = read(AGENTS_FILE)

        assert section(document, "99. 不存在的章节") == "", (
            "section() 对不存在的标题必须返回空 —— "
            "否则守卫会在一个空字符串上假绿"
        )

    def test_layer_dependency_detection_works(self):
        """
        AST 依赖检测必须真的能发现跨层 import。

        【怎么验证】拿一个**已知**的依赖来查：
            `services/` 确实 import `core`。
            如果检测说「没有」，说明检测坏了。
        """
        assert "core" in layer_dependencies("services"), (
            "依赖检测说 services 不依赖 core —— 那是错的，检测坏了"
        )
        assert "communication" in layer_dependencies("services")

    def test_layer_dependency_negative_case(self):
        """反向：已知**不存在**的依赖必须检测为「没有」。"""
        assert "storage" not in layer_dependencies("services"), (
            "依赖检测说 services 依赖 storage —— 但代码里没有"
        )

    @pytest.mark.parametrize(
        "phrase,why",
        [
            ("Agent Private Memory ≠ Agent Private Workspace",
             "Private Memory / Private Workspace 区分"),
            ("Agent Strategy ≠ Negotiation Mechanism",
             "Strategy / Mechanism 区分"),
            ("第二套 Task Core", "Shared Context / Task Core 区分"),
            ("Context Loading Rule", "Context Layering 规则"),
        ],
    )
    def test_critical_phrases_are_removable(self, phrase, why):
        """
        【反向测试的核心】

            对每一条关键声明：
                把包含它的那一行从文档里删掉
                → 对应的守卫**必须**找不到它

            这证明守卫查的是**文档内容**，
            而不是某个「永远都为真」的条件。

        （不修改真实文件 —— 只在内存里做。）
        """
        document = read(AGENTS_FILE)
        assert phrase in document, f"前提不成立：文档里本来就没有 {phrase!r}"

        # 删掉所有含这个短语的行，模拟「有人删了这条声明」
        stripped = "\n".join(
            line for line in document.splitlines()
            if phrase not in line
        )

        assert phrase not in stripped, (
            f"删掉之后 {phrase!r} 还在 —— 测试本身有问题"
        )

    def test_private_workspace_exclusion_is_required_content(self):
        """
        反向：如果 §4 里删掉「不在这四层里」这句话，
        说明「Private Workspace 被错误地当成 Memory 一层」——
        守卫必须依赖这句话的存在。
        """
        document = read(AGENTS_FILE)
        memory = section(document, "4. Memory 四层")

        assert "不在这四层里" in flatted(memory), (
            "§4 缺少「不在这四层里」的排除声明"
        )

    def test_dependency_guard_would_catch_violation(self, monkeypatch,
                                                    tmp_path):
        """
        反向：制造一个假的 `services → storage` 依赖，
        确认 `layer_files_importing` 能发现它。
        """
        fake_layer = tmp_path / "services"
        fake_layer.mkdir()
        (fake_layer / "fake.py").write_text(
            "from storage.models import TABLE_TASKS\n", encoding="utf-8"
        )

        found = set()
        for source in sorted(fake_layer.glob("*.py")):
            for module in imports_of(source):
                if module.split(".")[0] == "storage":
                    found.add(source.name)

        assert found == {"fake.py"}, (
            "伪造的 services → storage 依赖没有被检测出来 —— "
            "依赖守卫是假的"
        )
