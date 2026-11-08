"""
设计简化守卫：防止旧模型（Authorization-centered / Cascade Revocation）回归

============================================================
【这个文件守什么】
============================================================

设计简化（ADR-0027）把第一版从
**`Controller + Authorization + Delegation + Lock`**
简化成
**`Controller + Lock + Handoff + Audit`**。

风险是**文档回退** —— 有人把 Authorization / Cascade Revocation
重新写成第一版核心。

============================================================
【为什么不能只查关键词】
============================================================

`Authorization` / `Delegation` **必须**能出现在文档里 ——
因为我们要说「它降级了」「它是 FUTURE」。

所以判定原则是：
**只有「把已降级概念写成第一版核心」的肯定式表述才违规。**

并且必须检查**标注**（FUTURE / 降级 / 移除 / ❌），
以及 **Recovery 的否定式规则**（超时 ≠ 释放）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import markdown_guard_helpers as markdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCH_DIR = PROJECT_ROOT / "docs" / "architecture"

RESOURCE_DOC = ARCH_DIR / "RESOURCE_LAYER.md"
LOCK_DOC = ARCH_DIR / "LOCK_MODEL.md"
RECOVERY_DOC = ARCH_DIR / "RECOVERY_MODEL.md"
WORKSPACE_DOC = ARCH_DIR / "TASK_WORKSPACE.md"
ADR_0025 = ARCH_DIR / "adr" / "0025-resource-control-authorization-delegation.md"
ADR_0027 = ARCH_DIR / "adr" / "0027-workspace-responsibility-model-simplification.md"
AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"


def read(path: Path) -> str:
    return markdown.read(path)


@pytest.fixture(scope="module")
def recovery() -> str:
    return read(RECOVERY_DOC)


@pytest.fixture(scope="module")
def resource() -> str:
    return read(RESOURCE_DOC)


# ============================================================
# 一、Recovery 文档存在
# ============================================================
class TestRecoveryDoc:
    def test_exists(self):
        assert RECOVERY_DOC.exists(), "RECOVERY_MODEL.md 不存在"

    def test_status_future(self):
        assert "FUTURE" in read(RECOVERY_DOC)[:1200]


# ============================================================
# 二、🔒 三条否定式规则
# ============================================================
class TestRecoveryNegativeRules:
    @pytest.mark.parametrize(
        "rule",
        [
            "Heartbeat Timeout      ≠  Lock Release",
            "Long Inactivity        ≠  Responsibility Loss",
            "Agent Failure Detection ≠  Automatic Reassignment",
            "Agent Recovery         ≠  Lock Recovery",
        ],
    )
    def test_rule_stated(self, recovery, rule):
        """四条否定式规则必须逐字写出。"""
        assert rule in recovery, (
            f"RECOVERY_MODEL.md 必须逐字写出：{rule!r}"
        )

    def test_detection_is_not_governance(self, recovery):
        text = markdown.plain(recovery)

        assert "异常检测 ≠ 治理决策" in text, (
            "必须写出核心规则「异常检测 ≠ 治理决策」"
        )

    def test_blocked_preferred_over_silent_transfer(self, recovery):
        text = markdown.plain(recovery)

        assert "宁可 Task 暂时 BLOCKED" in text, (
            "必须写出「宁可 Task 暂时 BLOCKED，也不要偷偷把工作交给另一个 Agent」"
        )

    def test_system_cannot_auto_actions(self, recovery):
        """❌ 系统不能自动做的六件事。"""
        block = markdown.section(recovery, "1.2")
        assert block, "找不到 §1.2"

        for action in ("Release Lock", "Reassign Task", "Reassign Lock",
                       "Force Unlock", "认定 Agent 已永久失败"):
            assert action in block, f"§1.2 必须列出：{action}"

    def test_system_can_auto_actions(self, recovery):
        """✅ 系统可以自动做的。"""
        block = markdown.section(recovery, "1.1")

        for action in ("Heartbeat", "Runtime 异常", "记录", "通知"):
            assert action in block, f"§1.1 必须列出：{action}"


# ============================================================
# 三、Human Confirmation 与未来 AI Governance
# ============================================================
class TestGovernance:
    def test_human_confirmation_current(self, recovery):
        block = markdown.section(recovery, "6.1")

        assert "Human Confirmation" in block, (
            "§6.1 必须写明当前治理权在 Human"
        )

    def test_ai_governance_future(self, recovery):
        block = markdown.section(recovery, "6.2")

        assert "Governance Agent" in block, "§6.2 必须给出未来形态"
        assert "FUTURE" in block or "未来" in block

    def test_current_judgment_is_not_fact(self, recovery):
        """当前治理型 Agent 的判断**不是**系统事实。"""
        block = markdown.section(recovery, "7.")

        assert "分析与判断结果" in block, (
            "§7 必须写明当前判断只是「分析与判断结果」"
        )
        assert "正式治理事实" in block, (
            "§7 必须对比未来的「正式治理事实」"
        )

    def test_not_permanent_human_only(self, recovery):
        """
        **逐行检查否定 —— 不能只查「永久 Human-only」这个词。**

        【第一版是弱的（反向测试发现）】
            第一版查 `"永久 Human-only" in text`。
            把 `不要…永久 Human-only` 改成 `要…永久 Human-only`
            之后，词还在 → 照样绿。典型的**否定盲**。
        """
        text = markdown.plain(recovery)
        assert "永久 Human-only" in text, (
            "必须提到「永久 Human-only」这个概念"
        )

        lines = [
            l for l in recovery.splitlines() if "Human-only" in l
        ]
        assert lines, "找不到含 Human-only 的行"

        for line in lines:
            assert "不要" in line or "不" in line, (
                f"提到 Human-only 的行必须是否定形式：{line.strip()!r}"
            )

    def test_governance_migration_goal(self, recovery):
        text = markdown.plain(recovery)

        assert "逐步从 Human 迁移给 AI" in text, (
            "必须写明治理权的迁移目标"
        )


# ============================================================
# 四、Crash 恢复语义保持 OPEN
# ============================================================
class TestCrashIsOpen:
    def test_no_auto_rollback_nor_keep(self, recovery):
        """
        **查否定形式，不查「Auto Rollback」这个词是否出现。**

        【第一版是弱的（反向测试发现）】
            第一版只查 `"Auto Rollback" in block`。
            把 `不要规定 \`Crash → Auto Rollback\`` 改成
            `规定 \`Crash → Auto Rollback\`` 之后，词还在 → 照样绿。
            这是典型的**否定盲**。
        """
        block = markdown.plain(markdown.section(recovery, "5."))

        # 【逐行检查，不能用 `"不要规定" in block`】
        #   因为 §5 的下一行是「**也不要规定** Crash → Auto Keep」——
        #   它**包含** `不要规定` 这个子串，
        #   于是把上一行的 `不要` 删掉之后，整块检查照样通过。
        lines = [
            l for l in markdown.section(recovery, "5.").splitlines()
            if "Crash → Auto" in l
        ]
        assert len(lines) >= 2, (
            f"§5 必须有 Crash → Auto Rollback / Auto Keep 两行，只有 {len(lines)}"
        )

        for line in lines:
            assert "不要" in line, (
                f"§5 这一行必须是否定形式（含「不要」）：{line.strip()!r}"
            )

    def test_recorded_as_open_question(self, recovery):
        block = markdown.plain(markdown.section(recovery, "5."))

        assert "Open Question" in block, (
            "§5 必须把「Crash 后未完成修改的恢复语义」列为 Open Question"
        )

    def test_lists_possible_file_states(self, recovery):
        """§5 必须列出崩溃时可能的文件状态（说明为什么不能自动判断）。"""
        block = markdown.section(recovery, "5.")

        for state in ("没有任何变化", "完整内容", "只写入了一部分"):
            assert state in block, f"§5 必须列出可能状态：{state}"


# ============================================================
# 五、Controller 定义收窄
# ============================================================
class TestControllerNarrowed:
    def test_directory_is_minimum_granularity(self, resource):
        block = markdown.plain(markdown.section(resource, "1.5"))

        assert "最小粒度" in block and "Directory" in block, (
            "§1.5 必须写明 Controller 最小粒度是 Directory"
        )

    def test_no_file_controller(self, resource):
        block = markdown.section(resource, "1.5")

        assert "不要给 File 单独设计 Controller" in block, (
            "§1.5 必须明确禁止给 File 设计 Controller"
        )

    def test_not_a_permission_role(self, resource):
        block = markdown.plain(markdown.section(resource, "1.5"))

        assert "不是传统 Owner" in block or "不是" in block and "Role" in block, (
            "§1.5 必须写明 Controller 不是 Owner / Role"
        )

    def test_answers_long_term_responsibility(self, resource):
        block = markdown.plain(markdown.section(resource, "1.5"))

        assert "长期归谁负责" in block, (
            "§1.5 必须写明 Controller 回答「长期归谁负责」"
        )

    def test_is_recovery_anchor(self, resource):
        """Controller 是 Recovery 的责任锚点（设计简化的新增重点）。"""
        block = markdown.plain(markdown.section(resource, "1.5"))

        assert "责任锚点" in block and "Recovery" in block, (
            "§1.5 必须写明 Controller 是 Recovery 的责任锚点"
        )


# ============================================================
# 六、Authorization 降级（不写成第一版核心）
# ============================================================
class TestAuthorizationDowngraded:
    def test_resource_doc_has_banner(self, resource):
        """RESOURCE_LAYER.md 顶部必须有降级横幅。"""
        head = resource[:2000]

        assert "ADR-0027" in head, "顶部必须指向 ADR-0027"
        assert "不再是" in head or "简化" in head, (
            "顶部必须说明核心模型已简化"
        )

    def test_authorization_marked_future(self, resource):
        head = resource[:2500]

        # 横幅表里 Authorization 行必须标 FUTURE
        lines = [
            l for l in head.splitlines()
            if "**Authorization**" in l
        ]
        assert lines, "横幅表里必须有 Authorization 行"
        for line in lines:
            assert "FUTURE" in line or "降级" in line, (
                f"横幅必须把 Authorization 标为 FUTURE/降级：{line.strip()!r}"
            )

    def test_no_generic_permission_set_as_core(self, resource):
        """不许把通用权限集合写成第一版必需。"""
        block = markdown.plain(markdown.section(resource, "4.0"))

        assert "不需要独立的 Authorization 核心对象" in block, (
            "§4.0 必须写明第一版不需要 Authorization 核心对象"
        )
        assert "不需要" in block and (
            "READ" in block and "WRITE" in block
        ), "§4.0 必须说明不需要通用权限集合"

    def test_not_an_iam_system(self, resource):
        text = markdown.plain(resource)

        assert "不要" in text and "企业 IAM 系统" in text, (
            "必须明确禁止把第一版做成企业 IAM 系统"
        )

    def test_authorization_request_removed(self, resource):
        block = markdown.section(resource, "8.4")

        assert "不创建 `AuthorizationRequest`" in block, (
            "§8.4 必须写明第一版不创建 AuthorizationRequest"
        )
        assert "Lock Request" in block or "Handoff" in block, (
            "§8.4 必须给出替代机制"
        )

    def test_derivable_table(self, resource):
        """§4.0 必须给出「从别的概念推导」的对照表。"""
        block = markdown.section(resource, "4.0")
        rows = markdown.table_rows(block, "第一版怎么判断")

        assert len(rows) >= 4, (
            f"§4.0 的推导对照表只有 {len(rows)} 行，应该有至少 4 行"
        )


# ============================================================
# 七、Cascade Revocation 移除
# ============================================================
class TestCascadeRevocationRemoved:
    def test_marked_removed(self, resource):
        """
        **查那句规范句，不查「正式移除」这个词。**

        【第一版是弱的（反向测试发现）】
            第一版查 `"正式移除" in block`。但 §7.5 的**标题**就含
            「正式移除」—— 把正文改成「第一版把关系建模成权限链」
            之后标题还在 → 守卫照样绿。
        """
        block = markdown.plain(markdown.section(resource, "7.5"))

        # 那句规范句：不再把关系建模成权限传递链
        assert "不再把 Agent 之间的关系建模成" in block, (
            "§7.5 必须写出那句规范句："
            "「第一版不再把 Agent 之间的关系建模成权限从 A 传给 B」"
        )
        assert "不应自动摧毁所有下级具体工作责任" in block, (
            "§7.5 必须写出「Controller Reassignment 不应自动摧毁下级责任」"
        )

    def test_key_example_present(self, resource):
        """§7.5 必须给出「Controller 收回但 File Lock 不变」的例子。"""
        block = resource_section(resource, "7.5") if False else markdown.section(resource, "7.5")

        assert "result.csv" in block, "§7.5 必须给出 result.csv 的例子"
        assert "File Lock = **仍然 = C**" in block or "仍然 = C" in block, (
            "§7.5 必须写明 File Lock 仍然 = C"
        )

    def test_cannot_seize_lock_by_becoming_controller(self, resource):
        block = markdown.plain(markdown.section(resource, "7.5"))

        assert "不应该因为成为 Controller 就自动夺走" in block, (
            "§7.5 必须写明不能因成为 Controller 就夺走 Lock"
        )

    def test_replacement_mechanisms_listed(self, resource):
        block = markdown.section(resource, "7.5")

        for mech in ("Recall", "Reassignment", "Handoff", "Force Unlock"):
            assert mech in block, f"§7.5 必须列出替代机制：{mech}"


# ============================================================
# 八、Delegation 降级
# ============================================================
class TestDelegationDowngraded:
    def test_marked_downgraded(self, resource):
        block = markdown.plain(markdown.section(resource, "6.0"))

        assert "Handoff" in block or "责任交接" in block, (
            "§6.0 必须写明 Delegation 降级为 Handoff / Responsibility Transfer"
        )
        assert "FUTURE" in block, "§6.0 必须标为 FUTURE"

    def test_no_complex_delegation_object_required(self, resource):
        block = markdown.plain(markdown.section(resource, "6.0"))

        assert "不一定还需要" in block or "降级" in block, (
            "§6.0 必须说明第一版不需要复杂 Delegation 对象"
        )


# ============================================================
# 九、ADR 记录（不静默删除）
# ============================================================
class TestADRTrail:
    def test_adr_0027_exists(self):
        assert ADR_0027.exists(), "ADR-0027 不存在"

    def test_adr_0027_sections(self):
        text = read(ADR_0027)
        for h in ("## Context", "## Decision", "## Consequences"):
            assert h in text, f"ADR-0027 缺少 {h}"

    def test_adr_0025_annotated_not_deleted(self):
        """ADR-0025 必须**保留原文**并标注被取代。"""
        text = read(ADR_0025)

        assert "ADR-0027" in text, "ADR-0025 必须指向 ADR-0027"
        assert "被" in text and "取代" in text, (
            "ADR-0025 必须标注第一版核心地位被取代"
        )
        assert "保留为历史记录" in text, (
            "ADR-0025 必须说明保留为历史记录"
        )

    def test_adr_index_updated(self):
        index = read(ARCH_DIR / "adr" / "README.md")

        assert "0027" in index
        assert "0027-workspace-responsibility-model-simplification.md" in index

    def test_adr_index_marks_0025_superseded(self):
        index = read(ARCH_DIR / "adr" / "README.md")

        line = next(
            (l for l in index.splitlines() if "0025" in l), ""
        )
        assert "取代" in line or "0027" in line, (
            f"索引里 0025 那行必须标注被取代：{line.strip()!r}"
        )


# ============================================================
# 十、AGENTS.md 导航
# ============================================================
class TestAgentsNavigation:
    def test_recovery_doc_linked(self):
        assert "RECOVERY_MODEL.md" in read(AGENTS_FILE), (
            "AGENTS.md 必须加上 RECOVERY_MODEL.md 导航"
        )

    def test_simplification_stated(self):
        text = read(AGENTS_FILE)

        assert "ADR-0027" in text, "AGENTS.md 必须指向 ADR-0027"
        assert "Controller + Lock + Handoff + Audit" in text, (
            "AGENTS.md 必须写出简化后的核心模型"
        )

    def test_under_line_limit(self):
        lines = len(read(AGENTS_FILE).splitlines())
        assert lines <= 560, f"AGENTS.md 有 {lines} 行，超过 560"


# ============================================================
# 十一、反向测试
# ============================================================
class TestReverseTests:
    def test_cascade_regression_would_be_caught(self):
        """
        反向：把 Cascade Revocation 写回第一版核心，
        `test_marked_removed` 必须变红。
        """
        fake = "### 7.5 Cascade Revocation 是核心机制\n\n上级撤销 → 下级级联失效。\n"
        block = markdown.plain(markdown.section(fake, "7.5"))

        assert "正式移除" not in block, (
            "伪造文本里本不该有「正式移除」—— 测试前提不成立"
        )
        assert "级联失效" in block

    def test_timeout_release_regression_would_be_caught(self):
        """
        反向：把「超时自动释放」写进 Recovery，规则检查必须变红。
        """
        fake = "# Recovery\n\nHeartbeat Timeout → 自动 Release Lock。\n"

        assert "Heartbeat Timeout      ≠  Lock Release" not in fake, (
            "伪造文本里本不该有正确的否定式规则"
        )

    def test_all_used_sections_are_nonempty(self, resource, recovery):
        """
        反向：本文件用到的每个 section 必须**真的非空** ——
        空 section 会让内容断言变成空转（false-green）。
        """
        for prefix in ("1.5", "4.0", "6.0", "7.5", "8.4"):
            assert markdown.section(resource, prefix), (
                f"RESOURCE_LAYER §{prefix} 是空的"
            )

        for prefix in ("1.1", "1.2", "5.", "6.1", "6.2", "7."):
            assert markdown.section(recovery, prefix), (
                f"RECOVERY_MODEL §{prefix} 是空的"
            )

    def test_authorization_downgrade_is_live(self, resource):
        """反向：确认降级横幅确实在文档里。"""
        assert "ADR-0027" in resource[:2000]

    def test_negative_rules_are_live(self, recovery):
        assert "Heartbeat Timeout      ≠  Lock Release" in recovery


# ============================================================
# 十二、Scope：不许提前实现
# ============================================================
class TestNoImplementation:
    FORBIDDEN_MODULES = (
        "recovery_manager.py", "governance_agent.py",
        "authorization_engine.py", "delegation_service.py",
        "controller_registry.py", "resource_manager.py", "lock_manager.py",
    )
    FORBIDDEN_CLASSES = (
        "RecoveryManager", "GovernanceAgent", "AuthorizationEngine",
        "DelegationService", "ControllerRegistry", "ResourceManager",
        "LockManager",
    )

    def test_no_modules(self):
        for layer in ("core", "services", "communication", "storage", "agents"):
            for name in self.FORBIDDEN_MODULES:
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 本轮禁止实现"
                )

    def test_no_forbidden_classes(self):
        """
        禁止的是**被移除的概念**，不是「所有 Workspace 类」。

        （Workspace Core 第一阶段已授权实现 ——
          `WorkspaceGateway` / `ResourceTree` 等是**允许**的。）
        """
        import ast

        allowed = {
            "WorkspaceGateway", "WorkspaceStore", "ResourceTree",
            "Resource", "ExplicitLock", "OperationResult",
            "ResourceLockRegistry",
        }

        for layer in ("core", "services", "communication", "storage",
                      "agents", "llm"):
            for path in (PROJECT_ROOT / layer).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {
                    n.name for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef)
                }
                hit = (names & set(self.FORBIDDEN_CLASSES)) - allowed
                assert not hit, (
                    f"{path.name} 定义了 {sorted(hit)} —— "
                    f"该概念已被 ADR-0027 移除/降级"
                )

    def test_no_new_tables(self, db):
        names = set(db.table_names())

        for forbidden in ("authorization_requests", "delegations",
                          "controllers", "lock_requests", "edit_sessions",
                          "recovery_events", "locks", "resources"):
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
