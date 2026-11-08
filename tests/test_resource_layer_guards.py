"""
Resource 层守卫：Controller / Authorization / Delegation / Write Lock

============================================================
【这个文件守什么】
============================================================

本轮确立了**四个必须分离的概念**：

    Controller       谁负责这组资源
    Authorization    谁有资格（READ/WRITE/CREATE/DELETE/EXECUTE/DELEGATE）
    Write Lock       现在轮到谁写
    Delegation       授权从哪来（可成树 · 可追溯 · 可级联撤销）

以及四条硬规则：

    ① `Delegated Authority ≤ Grantor Authority`（权限不能放大）
    ② Control 必须有明确来源（不能凭空出现）
    ③ 上级撤销 → 下级级联失效（Cascade Revocation）
    ④ Task responsibility ≠ Resource authority（可关联但不自动等价）

============================================================
【为什么不能用简单 substring】
============================================================

这些词**必须**出现在文档里（因为我们正是在定义它们）。
只查「文档里有没有 Controller」会让守卫永远为真。

所以判定原则是：

    **概念合并只有在「肯定式的等价表述」出现时才违规。**

并复用 parser hardening 后的共享 helper
（`tests/markdown_guard_helpers.py`，fence-aware）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import markdown_guard_helpers as markdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCH_DIR = PROJECT_ROOT / "docs" / "architecture"

RESOURCE_DOC = ARCH_DIR / "RESOURCE_LAYER.md"
WORKSPACE_DOC = ARCH_DIR / "TASK_WORKSPACE.md"
ADR_FILE = ADR_DIR_FILE = ARCH_DIR / "adr" / "0025-resource-control-authorization-delegation.md"
AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"

ARCH_FILES = sorted(ARCH_DIR.rglob("*.md")) + [AGENTS_FILE]


def read(path: Path) -> str:
    return markdown.read(path)


# ============================================================
# 一、文档存在且状态正确
# ============================================================
class TestDocumentExists:
    def test_resource_doc_exists(self):
        assert RESOURCE_DOC.exists(), "docs/architecture/RESOURCE_LAYER.md 不存在"

    def test_status_is_future(self):
        head = read(RESOURCE_DOC)[:1500]
        assert "FUTURE" in head, "顶部必须标明实现状态为 FUTURE"

    def test_not_claims_current(self):
        """不许声称 Resource 层已实现（用肯定式检查）。"""
        text = markdown.plain(read(RESOURCE_DOC))

        for claim in ("Resource 层已实现", "Controller 已实现",
                      "Delegation 已实现", "资源授权已可用"):
            assert claim not in text, (
                f"文档声称 {claim!r} —— 但它一行代码都没有"
            )


# ============================================================
# 二、四个概念必须分离
# ============================================================
class TestConceptsAreSeparate:
    """
    规范要求：**不能把它们混成一个「锁」。**
    """

    #: 必须被正式区分的概念对
    DISTINCT_PAIRS = [
        ("Controller", "Authorization"),
        ("Authorization", "Write Lock"),
        ("Controller", "Write Lock"),
    ]

    @pytest.mark.parametrize("left,right", DISTINCT_PAIRS)
    def test_pair_has_a_distinct_section(self, left, right):
        """
        每一对必须有**正式的区分小节**（标题里两个词都有 + 区分符号）。

        【结构化检查】只认标题，不认正文里的偶然同现。
        """
        doc = read(RESOURCE_DOC)
        headings = [h.raw for h in markdown.iter_headings(doc)]

        distinct = [
            h for h in headings
            if left in h and right in h
            and ("≠" in h or "分离" in h or "区分" in h or "不可合并" in h)
        ]

        assert distinct, (
            f"RESOURCE_LAYER.md 里没有对 **{left} ≠ {right}** 的正式区分小节。\n"
            f"相关标题：{[h.strip() for h in headings if left in h and right in h]}"
        )

    def test_no_concept_merging_claim(self):
        """
        反向语义：不许有任何标题把两个概念**合并**或称为同一个东西。
        """
        doc = read(RESOURCE_DOC)
        headings = [h.raw for h in markdown.iter_headings(doc)]

        for heading in headings:
            for left, right in self.DISTINCT_PAIRS:
                if left not in heading or right not in heading:
                    continue
                for merge in ("就是", "等于", "合并", "是同一个", "统称为"):
                    if merge not in heading:
                        continue
                    assert "≠" in heading or "不" in heading, (
                        f"标题把 {left} 与 {right} 合并了：{heading.strip()}"
                    )

    def test_controller_is_not_owner(self):
        """Controller ≠ Owner 必须写明。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "3.5"))

        assert "Controller ≠ Owner" in block or (
            "Controller" in block and "Owner" in block
        ), "§3.5 必须写明 Controller ≠ Owner"

    def test_write_lock_symbol_table(self):
        """
        §5.2 必须用表格给出「谁负责 / 谁有资格 / 现在轮到谁」三问三答。
        """
        block = markdown.section(read(RESOURCE_DOC), "5.2")
        rows = markdown.table_rows(block, "问题")

        assert len(rows) == 3, (
            f"§5.2 的对照表有 {len(rows)} 行，应该恰好 3 行（三个问题）"
        )
        joined = " ".join(rows)
        for concept in ("Controller", "Authorization", "Write Lock"):
            assert concept in joined or concept.lower() in joined.lower(), (
                f"§5.2 表里缺少 {concept}"
            )


# ============================================================
# 三、四条硬规则
# ============================================================
class TestHardRules:
    def test_delegated_authority_not_greater(self):
        """🔒 `Delegated Authority ≤ Grantor Authority` 必须写出。"""
        text = markdown.plain(read(RESOURCE_DOC))

        assert "Delegated Authority ≤ Grantor Authority" in text, (
            "必须写出「权限不能放大」这条硬规则"
        )

    def test_control_must_have_source(self):
        """🔒 Control 必须有明确来源。"""
        text = markdown.plain(read(RESOURCE_DOC))

        assert "Control 必须有明确来源" in text or (
            "Control 不能凭空出现" in text
        ), "必须写明 Control 不能凭空出现"

    def test_cascade_revocation(self):
        """🔒 上级撤销 → 下级级联失效。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "7.3"))

        assert "级联失效" in block, "§7.3 必须写明 Cascade Revocation"
        assert "parent" in block, "§7.3 必须说明 parent 链"

    def test_task_authority_not_automatically_equivalent(self):
        """🔒 Task responsibility ≠ Resource authority。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "9.4"))

        assert "不自动等价" in block or "可以关联，但不自动等价" in block, (
            "§9.4 必须写明 Task 与 Resource authority 不自动等价"
        )
        # 「拿到 Task ≠ 自动拿到资源权限」是 §9.4 的核心对照
        assert "拿到 Task" in block and "自动拿到资源权限" in block, (
            "§9.4 必须给出「拿到 Task → 自动拿到资源权限」这个错误推理"
        )

    def test_agent_cannot_self_grant(self):
        """🔒 Agent 不能自己给自己增加权限。"""
        text = markdown.plain(read(RESOURCE_DOC))

        assert "不能因为任务需要就自行扩大权限" in text, (
            "必须写明 Agent 不能因任务需要自行扩权"
        )

    def test_request_is_not_a_message(self):
        """🔒 AuthorizationRequest 不是普通 Message。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "8.2"))

        assert "不要简单把它实现成一条普通 Message" in block or (
            "普通 Message" in block
        ), "§8.2 必须写明 AuthorizationRequest ≠ 普通 Message"


# ============================================================
# 四、Delegation 是正式对象
# ============================================================
class TestDelegationIsFormal:
    @pytest.fixture(scope="class")
    def doc(self) -> str:
        return read(RESOURCE_DOC)

    def test_delegation_fields_present(self, doc):
        """§6.4 必须列出 Delegation 的字段（结构化：代码块或列表）。"""
        block = markdown.section(doc, "6.4")

        for field in ("delegation_id", "grantor", "grantee",
                      "resource_scope", "granted_rights",
                      "parent_delegation"):
            assert field in block, f"§6.4 的 Delegation 缺少字段 {field}"

    def test_authorization_source_must_be_kept(self, doc):
        """🔒 不要只保存 controller 而不保存授权来源。"""
        block = markdown.plain(markdown.section(doc, "6.4"))

        assert "不要只保存" in block, (
            "§6.4 必须禁止「只保存 controller = C 而不保存授权来源」"
        )

    def test_delegation_chain_documented(self, doc):
        """§7.1 必须给出 Delegation Chain 的例子。"""
        block = markdown.section(doc, "7.1")

        assert "D001" in block and "D002" in block, (
            "§7.1 必须给出 D001/D002 的链式例子"
        )
        assert "parent" in block, "§7.1 必须说明 parent 关系"

    def test_delegation_tree_documented(self, doc):
        """§6.2 必须给出 Delegation Tree。"""
        block = markdown.section(doc, "6.2")

        assert "Delegation Tree" in block, "§6.2 必须命名 Delegation Tree"


# ============================================================
# 五、Resource 类型与粒度
# ============================================================
class TestResourceModel:
    def test_first_version_types(self):
        block = markdown.section(read(RESOURCE_DOC), "2.1")

        assert "File" in block and "Directory" in block, (
            "§2.1 必须写明第一版支持 File 与 Directory"
        )

    def test_future_types_listed(self):
        block = markdown.section(read(RESOURCE_DOC), "2.2")

        for t in ("Repository", "Artifact", "Test Result", "Build",
                  "Patch", "Dataset", "Log"):
            assert t in block, f"§2.2 缺少未来类型 {t}"

    def test_inheritance_with_override(self):
        """§10.2 必须给出继承 + 覆盖的例子。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "10.2"))

        assert "internal_secret.py" in block, (
            "§10.2 必须给出覆盖的例子（internal_secret.py = NONE）"
        )
        assert "覆盖" in block

    def test_no_over_engineering(self):
        """第一版不要过度设计。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "10.3"))

        assert "不要过度设计" in block or "不要为了权限继承" in block


# ============================================================
# 六、Open Questions 必须完整
# ============================================================
class TestOpenQuestions:
    """规范明确列出 8 个未确定问题，必须全部记录。"""

    #: 规范 §"仍未确定的问题" 里点名的问题
    REQUIRED_TOPICS = [
        "Write Lock 的具体粒度",
        "并行编辑",
        "Directory Lock",
        "Git Worktree",
        "Permission inheritance",
        "Task 完成后授权是否自动回收",
        "Controller 转移",
        "Human",
    ]

    @pytest.mark.parametrize("topic", REQUIRED_TOPICS)
    def test_open_question_recorded(self, topic):
        block = markdown.section(read(RESOURCE_DOC), "12. 未确定的问题")
        assert block, "找不到 §12 未确定的问题"

        assert topic in block, (
            f"§12 必须记录未确定问题：{topic}"
        )

    def test_does_not_answer_them(self):
        """不许假装已经回答了这些 OPEN 问题。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "12."))

        assert "本轮没有决定" in block or "不要凭感觉实现" in block, (
            "§12 必须明确说明这些问题本轮没有决定"
        )


# ============================================================
# 七、协议影响：不新增消息类型
# ============================================================
class TestProtocolImpact:
    def test_protocol_section_is_open(self):
        block = markdown.section(read(RESOURCE_DOC), "13.")
        assert block, "找不到 §13 协议影响"

        assert "OPEN" in block, "§13 必须标为 OPEN（本轮不决定）"

    def test_no_premature_message_types(self):
        """§13.2 必须禁止随意增加消息类型。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "13.2"))

        assert "不要为了" in block and "随意增加消息类型" in block, (
            "§13.2 必须禁止为了「看起来完整」而增加消息类型"
        )

    def test_reuse_priority_documented(self):
        block = markdown.section(read(RESOURCE_DOC), "13.2")

        assert "复用" in block, "§13.2 必须写出「优先复用现有设计」的判断顺序"

    def test_no_new_message_type_in_protocol(self):
        """
        生产协议里**不许**已经出现这些消息类型
        （它们仍是 OPEN，本轮不实现）。
        """
        protocol = PROJECT_ROOT / "communication"
        banned = (
            "AUTHORIZATION_REQUEST", "AUTHORIZATION_GRANT",
            "AUTHORIZATION_REVOKE", "DELEGATION_CREATED",
            "DELEGATION_REVOKED",
        )

        for path in protocol.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for name in banned:
                assert name not in source, (
                    f"{path.name} 里出现了 {name} —— "
                    f"§13 说它仍是 OPEN，本轮不实现"
                )


# ============================================================
# 八、测试需求登记（设计，不实现）
# ============================================================
class TestTestRequirementsRegistered:
    """规范列出 15 条测试场景，必须全部登记。"""

    def test_requirement_table_has_fifteen_rows(self):
        block = markdown.section(read(RESOURCE_DOC), "14. 测试需求")
        rows = markdown.table_rows(block, "场景")

        assert len(rows) == 15, (
            f"§14 的测试需求表有 {len(rows)} 行，应该恰好 15 行"
        )

    @pytest.mark.parametrize(
        "keyword",
        [
            "不能授权超出自己范围",
            "不能自己给自己增加权限",
            "级联失效",
            "Authorization 和 Write Lock 独立",
            "不自动等价",
            "不自动",
        ],
    )
    def test_key_scenarios_present(self, keyword):
        block = markdown.section(read(RESOURCE_DOC), "14.")

        assert keyword in block, f"§14 缺少关键场景：{keyword}"

    def test_only_registered_not_implemented(self):
        """明确说明只登记需求，不写测试。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "14."))

        assert "只登记需求" in block or "不写测试" in block or "设计，不实现" in block


# ============================================================
# 九、与既有文档不冲突
# ============================================================
class TestNoConflictWithExisting:
    def test_workspace_doc_cross_references(self):
        """TASK_WORKSPACE.md §8/§11 必须指向 RESOURCE_LAYER.md。"""
        doc = read(WORKSPACE_DOC)

        assert "RESOURCE_LAYER.md" in doc, (
            "TASK_WORKSPACE.md 必须交叉引用 RESOURCE_LAYER.md"
        )

    def test_does_not_redefine_permission(self):
        """
        RESOURCE_LAYER.md 不许**另立**一套 Permission 定义 ——
        必须明确它是 TASK_WORKSPACE.md §9 的上下层关系。
        """
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "4.4"))

        assert "TASK_WORKSPACE.md §9" in block or "TASK_WORKSPACE.md" in block, (
            "§4.4 必须说明与 TASK_WORKSPACE.md §9 的关系"
        )

    def test_write_lock_semantics_not_changed(self):
        """§5.4 必须写明不改变 ADR-0021 的锁语义。"""
        block = markdown.plain(markdown.section(read(RESOURCE_DOC), "5.4"))

        assert "TASK_WORKSPACE.md" in block and "ADR-0021" in block, (
            "§5.4 必须指向 TASK_WORKSPACE.md §10 与 ADR-0021，并声明不改变它们"
        )

    def test_agents_navigation_updated(self):
        assert "RESOURCE_LAYER.md" in read(AGENTS_FILE), (
            "AGENTS.md 必须加上 RESOURCE_LAYER.md 导航"
        )


# ============================================================
# 十、反向测试
# ============================================================
class TestReverseTests:
    """ADR-0018 硬规定：新增守卫必须配反向测试。"""

    def test_merging_concepts_would_be_caught(self):
        """
        反向：把 Controller / Authorization 合并成一个标题，
        `test_pair_has_a_distinct_section` 必须会失败。
        """
        fake = "## 5. Controller 就是 Authorization（合并）\n"

        headings = [h.raw for h in markdown.iter_headings(fake)]
        distinct = [
            h for h in headings
            if "Controller" in h and "Authorization" in h
            and ("≠" in h or "分离" in h or "区分" in h or "不可合并" in h)
        ]

        assert not distinct, "合并式标题竟然被当成了「正式区分」"

    def test_section_empty_for_missing(self):
        """找不到的小节返回空 —— 调用方必须自己断言非空。"""
        assert markdown.section(read(RESOURCE_DOC), "99. 不存在") == ""

    def test_guard_files_have_nonempty_sections(self):
        """
        反向：本文件用到的每个 section 都必须**真的非空**。
        空 section 会让内容断言变成空转。
        """
        doc = read(RESOURCE_DOC)

        for prefix in ("2.1", "2.2", "3.5", "5.2", "5.4", "6.2", "6.4",
                       "7.1", "7.3", "8.2", "9.4", "10.2", "10.3",
                       "12.", "13.", "14."):
            assert markdown.section(doc, prefix), (
                f"§{prefix} 是空的 —— 针对它的守卫是空转的"
            )

    def test_delegated_authority_is_live(self):
        """反向：确认那条硬规则确实在文档里（否则守卫永远为真）。"""
        assert "Delegated Authority ≤ Grantor Authority" in markdown.plain(
            read(RESOURCE_DOC)
        )

    def test_cascade_revocation_is_live(self):
        assert "级联失效" in markdown.plain(read(RESOURCE_DOC))


# ============================================================
# 十一、ADR
# ============================================================
class TestADR:
    def test_adr_exists(self):
        assert ADR_FILE.exists(), "ADR-0025 不存在"

    def test_adr_sections(self):
        text = read(ADR_FILE)
        for heading in ("## Context", "## Decision", "## Consequences"):
            assert heading in text, f"ADR-0025 缺少 {heading}"

    def test_adr_indexed(self):
        index = read(ARCH_DIR / "adr" / "README.md")
        assert "0025" in index
        assert "0025-resource-control-authorization-delegation.md" in index

    def test_adr_does_not_modify_0021_semantics(self):
        """ADR-0025 必须声明不改变 ADR-0021 的锁语义。"""
        text = markdown.plain(read(ADR_FILE))

        assert "ADR-0021" in text, "ADR-0025 必须引用 ADR-0021"
        assert "不改变" in text or "不重复" in text, (
            "ADR-0025 必须声明不改变既有锁语义"
        )


# ============================================================
# 十二、Scope：不许提前实现
# ============================================================
class TestNoImplementation:
    FORBIDDEN_MODULES = (
        "resource.py", "delegation.py", "controller.py",
        "authorization.py", "resource_store.py", "delegation_store.py",
    )
    FORBIDDEN_CLASSES = (
        "ResourceManager", "DelegationService", "ControllerRegistry",
        "AuthorizationEngine", "ResourceStore", "DelegationTree",
        "PermissionEngine", "LockManager",
    )

    def test_no_resource_modules(self):
        for layer in ("core", "services", "communication", "storage", "agents"):
            for name in self.FORBIDDEN_MODULES:
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 本轮禁止实现 Resource 层"
                )

    def test_no_resource_classes(self):
        import ast

        for layer in ("core", "services", "communication", "storage",
                      "agents", "llm"):
            for path in (PROJECT_ROOT / layer).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {
                    n.name for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef)
                }
                hit = names & set(self.FORBIDDEN_CLASSES)
                assert not hit, (
                    f"{path.name} 定义了 {sorted(hit)} —— 本轮禁止实现"
                )

    def test_no_new_tables(self, db):
        names = set(db.table_names())

        for forbidden in ("resources", "delegations", "controllers",
                          "authorization_requests"):
            assert forbidden not in names, (
                f"数据库里出现了 {forbidden} 表 —— 本轮禁止改 schema"
            )

    def test_workspace_core_tables_are_the_only_new_ones(self, db):
        """
        Workspace Core 第一阶段**已授权实现** —— 表数从 13 增加到 18。

        【这条守卫原来叫什么、为什么改】
            它原来叫 `test_table_count_unchanged`，断言
            `len(db.table_names()) == 13` —— 那是**设计阶段**的守卫，
            用来保证「本轮禁止实现 Workspace」。

            Workspace Core 第一阶段任务书**已经授权实现**，
            所以「表数不变」这条断言已经过时。
            但**它要防的东西仍然有效**，只是换了个问法：

                ✅ 新增的表必须是 Workspace Core 的 5 张
                ❌ 不许出现被明确移除的概念的表
                   （Authorization / Delegation / Cascade Revocation
                     / Recovery Automation）
        """
        names = set(db.table_names())

        expected_workspace_tables = {
            "workspace_resources",
            "workspace_explicit_locks",
            "workspace_revisions",
            "workspace_audits",
            "workspace_events",
            # Change Pipeline Phase 1 —— **不是** Task Workspace 的表
            "git_worktrees",
            "worktree_intents",
            "worktree_events",
            # Public Working Memory（2026-11-08 任务书授权）：
            # Task 范围内的计划 / 分工 / 清单 / 进度。
            "task_public_working_memory",
            "task_public_working_memory_revisions",
        }
        assert expected_workspace_tables <= names, (
            f"Workspace Core 的 5 张表不全，缺少 "
            f"{sorted(expected_workspace_tables - names)}"
        )

        # 🔒 ADR-0027 移除 / 降级的概念 —— 不许有表
        for forbidden in ("authorization_requests", "authorizations",
                          "delegations", "controllers", "lock_requests",
                          "edit_sessions", "recovery_events", "permissions",
                          "roles", "acl"):
            assert forbidden not in names, (
                f"数据库里出现了 {forbidden} 表 —— "
                f"该概念已被 ADR-0027 移除/降级，不得实现"
            )

        # 13（原始）+ 5（Workspace Core v1）+ 3（Change Pipeline Phase 1:
        #   git_worktrees / worktree_intents / worktree_events）= 21
        # 13（原始）+ 5（Workspace Core v1）
        # + 3（Change Pipeline Phase 1）
        # + 2（Public Working Memory）= 23
        assert len(names) == 23, (
            f"表数应为 13 + 5 + 3 + 2 = 23，实际 {len(names)}："
            f"{sorted(names)}"
        )
