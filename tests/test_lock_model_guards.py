"""
Lock 层级守卫：Directory/File Lock · Explicit/Implicit · Edit Session · Handoff

============================================================
【这个文件守什么】
============================================================

本轮确立的核心规则：

    FILE        是内容修改 Lock 的最小粒度
    EDIT END    ≠ LOCK RELEASE          ← 最核心
    EXPLICIT    > INHERITED
    Effective Lock = Explicit ?? nearest ancestor Directory Lock
    CREATE      不自动生成 Explicit Lock
    父级 Handoff 不吞回已有的 Explicit Lock
    父目录控制不能绕过 Delegated Resource Boundary
    Lock Request ≠ Authorization Request
    第一版不用 Git Branch 解决并行

============================================================
【为什么不能用 simple substring】
============================================================

这些词**必须**出现在文档里（因为我们正在定义它们）。
判定原则：**只有「肯定式的违规表述」才算违规**，
并复用 parser hardening 后的共享 helper（fence-aware）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import markdown_guard_helpers as markdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCH_DIR = PROJECT_ROOT / "docs" / "architecture"

LOCK_DOC = ARCH_DIR / "LOCK_MODEL.md"
WORKSPACE_DOC = ARCH_DIR / "TASK_WORKSPACE.md"
RESOURCE_DOC = ARCH_DIR / "RESOURCE_LAYER.md"
ADR_0026 = ARCH_DIR / "adr" / "0026-lock-hierarchy-and-edit-session.md"
AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"


def read(path: Path) -> str:
    return markdown.read(path)


@pytest.fixture(scope="module")
def lock_doc() -> str:
    return read(LOCK_DOC)


# ============================================================
# 一、文档存在且状态正确
# ============================================================
class TestDocumentExists:
    def test_exists(self):
        assert LOCK_DOC.exists(), "docs/architecture/LOCK_MODEL.md 不存在"

    def test_status_future(self):
        assert "FUTURE" in read(LOCK_DOC)[:1200]

    def test_not_claims_implemented(self):
        text = markdown.plain(read(LOCK_DOC))

        for claim in ("Lock 已实现", "Directory Lock 已实现",
                      "Edit Session 已实现", "Handoff 已实现"):
            assert claim not in text, f"声称 {claim!r} —— 但零实现"


# ============================================================
# 二、🔒 最核心的规则：EDIT END ≠ LOCK RELEASE
# ============================================================
class TestEditEndIsNotRelease:
    def test_core_rule_stated(self, lock_doc):
        text = markdown.plain(lock_doc)

        assert "EDIT END ≠ LOCK RELEASE" in text, (
            "必须写出核心规则「EDIT END ≠ LOCK RELEASE」"
        )

    def test_edit_session_does_not_release(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "0.3"))

        assert "Edit Session 结束" in block and "File Lock" in block, (
            "§0.3 必须给出「Edit Session 结束 → Lock 仍然不变」的流程"
        )
        assert "自动 Release" in block, (
            "§0.3 必须明确标记「自动 Release」是被禁止的"
        )

    def test_lock_persists_after_edit(self, lock_doc):
        """
        🔒 §9 的规范句：**Lock 不因一次编辑操作结束而自动释放。**

        （反向测试发现：把这句话反过来说之后没有任何测试变红 ——
          因为别的守卫只查 §0.2 的核心规则句与 §9 的操作清单。）
        """
        block = markdown.plain(markdown.section(lock_doc, "9. Lock 的持续性"))

        assert "Lock 不因一次编辑操作结束而自动释放" in block, (
            "§9 必须写出那句规范句："
            "「Lock 不因一次编辑操作结束而自动释放」"
        )

    def test_release_requires_explicit_operation(self, lock_doc):
        """只有四种操作才能改变 Lock。"""
        block = markdown.section(lock_doc, "9. Lock 的持续性")
        assert block, "找不到 §9 Lock 的持续性"

        for op in ("Release", "Handoff", "Revoke", "Force Unlock"):
            assert op in block, f"§9 必须列出改变 Lock 的操作：{op}"

    def test_edit_session_states(self, lock_doc):
        """§12 必须给出 Edit Session 状态机。"""
        block = markdown.section(lock_doc, "12. Edit Session")

        for state in ("LOCKED(A)", "EDITING(A)", "EDIT SESSION START"):
            assert state in block, f"§12 缺少状态：{state}"

    def test_editing_never_goes_unlocked(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "12.1"))

        assert "EDITING(A)" in block and "UNLOCKED" in block, (
            "§12.1 必须明确禁止 `EDITING(A) → UNLOCKED`"
        )


# ============================================================
# 三、最小粒度与并行模型
# ============================================================
class TestGranularityAndParallel:
    def test_file_is_minimum_granularity(self, lock_doc):
        text = markdown.plain(lock_doc)

        assert "File 是内容修改 Lock 的最小粒度" in text, (
            "必须写明 File 是最小粒度"
        )

    def test_lock_is_not_a_traditional_mutex(self, lock_doc):
        """
        🔒 **Lock 不是 `lock() → edit() → unlock()` 那种临时 mutex。**

        （反向测试发现这条之前**完全没有守卫** ——
          把它改成肯定式 mutex 后没有任何测试变红。）
        """
        text = markdown.plain(lock_doc)

        assert "不要把 Lock 理解成" in text and "临时 mutex" in text, (
            "必须写明「不要把 Lock 理解成一次编辑操作的临时 mutex」"
        )
        assert "持续的、独占的修改责任" in text, (
            "必须写明 Lock 表达的是「持续的、独占的修改责任」"
        )

    def test_same_file_one_holder(self, lock_doc):
        text = markdown.plain(lock_doc)

        assert "最多只能有一个有效的 File Lock Holder" in text, (
            "必须写明同一 File 最多一个 Holder"
        )

    def test_same_file_blocked_even_with_authorization(self, lock_doc):
        """即使都有 WRITE Authorization 也不能并发。"""
        text = markdown.plain(lock_doc)

        assert "即使 A 和 B 都拥有 `WRITE` Authorization 也不允许" in text or (
            "即使 A 和 B 都拥有" in text and "Authorization" in text
        ), "必须写明「即使都有 WRITE 也不允许并发」"

    def test_different_files_parallel(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "13.1"))

        assert "不同文件可以并行修改" in block, (
            "§13.1 必须写明不同文件可并行"
        )

    def test_no_git_branch_in_v1(self, lock_doc):
        """🔒 第一版不许用 Git Branch / Merge Conflict 解决并行。"""
        block = markdown.section(lock_doc, "13.2")

        # 【不能只查「Git Branch」这个词】
        #   反向测试发现：把 `- ❌ Git Branch 并行修改`
        #   改成 `- ✅ ...` 之后，词还在 → 守卫照样绿。
        #   必须检查它被标为❌禁止。
        lines = [
            l for l in block.splitlines()
            if "Git Branch" in l or "Merge Conflict" in l
        ]
        assert lines, "§13.2 必须点名 Git Branch / Merge Conflict"

        for line in lines:
            assert "❌" in line, (
                f"§13.2 这一行必须标 ❌（禁止）：{line.strip()!r}"
            )

    def test_single_file_single_owner_model(self, lock_doc):
        text = markdown.plain(lock_doc)

        assert "单文件单责任人" in text, (
            "必须写出「单文件单责任人模型」"
        )


# ============================================================
# 四、Directory Lock vs File Lock
# ============================================================
class TestDirectoryVsFileLock:
    def test_semantics_differ(self, lock_doc):
        block = markdown.section(lock_doc, "3.2")
        rows = markdown.table_rows(block, "Directory Lock")

        assert len(rows) >= 2, "§3.2 必须用表格对比两种 Lock"

        joined = " ".join(rows)
        assert "Namespace" in joined or "Structure" in joined, (
            "§3.2 必须写明 Directory Lock 控制 Namespace / Structure"
        )
        assert "Content" in joined, (
            "§3.2 必须写明 File Lock 控制 Content"
        )

    def test_directory_lock_covers_structure_ops(self, lock_doc):
        block = markdown.section(lock_doc, "3.2")

        for op in ("CREATE", "DELETE", "RENAME", "MOVE"):
            assert op in block, f"§3.2 的 Directory Lock 必须覆盖 {op}"

    def test_directory_lock_does_not_lock_whole_dir(self, lock_doc):
        """🔒 Directory Lock **不等于**锁死整个目录。"""
        block = markdown.plain(markdown.section(lock_doc, "3.3"))

        assert "不等于" in block and "锁死整个目录" in block, (
            "§3.3 必须写明 Directory Lock ≠ 锁死整个目录"
        )

    def test_parallel_file_locks_in_one_directory(self, lock_doc):
        """同一个目录下不同 File 可以有不同 Holder。"""
        block = markdown.section(lock_doc, "3.3")

        assert "data/b.csv" in block and "File Lock = B" in block, (
            "§3.3 必须给出同目录下不同 File 不同 Holder 的例子"
        )


# ============================================================
# 五、Explicit / Implicit / Effective Lock
# ============================================================
class TestLockResolution:
    def test_explicit_priority(self, lock_doc):
        """🔒 Explicit 优先于 Inherited。"""
        block = markdown.plain(markdown.section(lock_doc, "4.3"))

        assert "Explicit Lock` 优先于 `Inherited Lock" in block or (
            "优先于" in block
        ), "§4.3 必须写明 Explicit 优先于 Inherited"

    def test_four_resolution_rules(self, lock_doc):
        """§5.1 必须有 Rule 1–4（结构化：表格 4 行）。"""
        block = markdown.section(lock_doc, "5.1")
        rows = markdown.table_rows(block, "Rule")

        assert len(rows) == 4, (
            f"§5.1 的解析规则表有 {len(rows)} 行，应该恰好 4 行"
        )
        joined = " ".join(rows)
        for n in ("Rule 1", "Rule 2", "Rule 3", "Rule 4"):
            assert n in joined, f"§5.1 缺少 {n}"

    def test_explicit_vs_effective_distinguished(self, lock_doc):
        """必须明确区分 Explicit Lock 与 Effective Lock。"""
        text = markdown.plain(lock_doc)

        assert "Explicit Lock" in text and "Effective Lock" in text, (
            "必须同时出现 Explicit Lock 与 Effective Lock"
        )

    def test_resolution_example(self, lock_doc):
        """§5.2 必须给出三层目录的解析例子。"""
        block = markdown.section(lock_doc, "5.2")

        for path in ("data/x.csv", "data/processed/x.csv",
                     "data/processed/reports/x.csv"):
            assert path in block, f"§5.2 缺少解析例子：{path}"

    def test_nearest_ancestor_wins(self, lock_doc):
        """Rule 4：更近的 Directory Lock 胜出。"""
        block = markdown.plain(markdown.section(lock_doc, "5.1"))

        assert "更近" in block or "nearest" in block, (
            "§5.1 必须写明「使用更近的那个」"
        )


# ============================================================
# 六、创建资源不生成 Explicit Lock（本轮修正）
# ============================================================
class TestNoAutoExplicitLock:
    def test_rule_stated(self, lock_doc):
        """
        **查那句规范句，不查「不自动」+「创建 Explicit Lock」两个词。**

        【这条第一版是弱的（反向测试发现）】
            第一版查 `"不自动" in text and "创建 Explicit Lock" in text`。
            但 §6.1 的**标题**就是「创建资源时不自动创建 Explicit Lock」——
            把 §6.1 的正文改成「立即生成 Explicit Lock」之后，
            标题里那两个词还在 → 守卫照样绿。
        """
        text = markdown.plain(lock_doc)

        assert ("新创建 Resource 默认没有独立 Explicit Lock" in text), (
            "必须写出那句规范句：新创建 Resource 默认没有独立 Explicit Lock"
        )

    def test_reason_given(self, lock_doc):
        """必须给出理由（避免冗余）。"""
        block = markdown.plain(markdown.section(lock_doc, "6.2"))

        assert "冗余" in block or "没有必要" in block, (
            "§6.2 必须说明理由：创建者已是父目录控制者，额外显式锁冗余"
        )

    def test_no_auto_explicit_in_new_dir(self, lock_doc):
        """§13.4 也必须写明目录中新资源的默认行为。"""
        block = markdown.plain(markdown.section(lock_doc, "13.4"))

        assert "inherited" in block or "继承" in block, (
            "§13.4 必须写明新资源继承父 Directory Lock"
        )
        assert "不要因为创建而自动生成" in block or "不自动生成" in block or (
            "不要" in block and "Explicit File Lock" in block
        ), "§13.4 必须禁止自动生成 Explicit File Lock"

    def test_reconciles_with_workspace_2_4(self, lock_doc):
        """
        **必须显式处理与 TASK_WORKSPACE.md §2.4 的关系。**

        规范要求：不要静默覆盖旧语义。
        """
        block = markdown.section(lock_doc, "6.3")

        assert "TASK_WORKSPACE.md" in block, (
            "§6.3 必须显式讨论与 TASK_WORKSPACE.md §2.4 的关系"
        )
        assert "2.4" in block, "§6.3 必须点名 §2.4"
        assert "原子" in block, "§6.3 必须说明原子性要求继续成立"
        assert "澄清" in block or "不是冲突" in block, (
            "§6.3 必须明确这是澄清而不是冲突"
        )


# ============================================================
# 七、目录控制权传递与 Resource Boundary
# ============================================================
class TestBoundaryAndHandoff:
    def test_inherited_follows_parent_handoff(self, lock_doc):
        block = markdown.section(lock_doc, "7.")

        assert "自动生效" in block, (
            "§7 必须写明继承锁随父级 Handoff 自动生效"
        )

    def test_explicit_lock_not_swallowed(self, lock_doc):
        """🔒 显式锁不被父目录 Handoff 吞回。"""
        block = markdown.plain(markdown.section(lock_doc, "7.1"))

        # 查**那句结论**，不查「吞回去」/「不变」这两个词
        # （反向测试发现：把结论句反过来说之后，那两个词仍在别处）
        assert "B 不能因为获得父目录 Lock 就自动获得" in block, (
            "§7.1 必须写出那句结论："
            "「B 不能因为获得父目录 Lock 就自动获得 production.yaml 的内容修改权」"
        )
        assert "production.yaml" in block, (
            "§7.1 必须给出 production.yaml 的例子"
        )

    def test_follow_matrix(self, lock_doc):
        """§7.2 必须给出「跟随 / 不跟随」的规则表。"""
        block = markdown.section(lock_doc, "7.2")
        rows = markdown.table_rows(block, "行为")

        assert len(rows) == 2, (
            f"§7.2 的表有 {len(rows)} 行，应该恰好 2 行"
        )
        joined = " ".join(rows)
        assert "跟随" in joined and "保持不动" in joined

    def test_resource_boundary(self, lock_doc):
        """§8.1 必须写明 Delegated Resource Boundary。"""
        block = markdown.section(lock_doc, "8.1")

        assert "Resource Boundary" in block, (
            "§8.1 必须命名 Delegated Resource Boundary"
        )

    def test_parent_cannot_bypass_boundary(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "8.1"))

        # 查**例外句**本身 —— 它才是「不能绕过」的可执行表述
        assert "除非先正常处理相应的 Delegation / Lock / Control 关系" in block, (
            "§8.1 必须写出那句例外句："
            "「除非先正常处理相应的 Delegation / Lock / Control 关系」"
        )

    def test_delete_is_not_ordinary_parent_op(self, lock_doc):
        """🔒 Delete 不是普通父目录操作。"""
        block = markdown.plain(markdown.section(lock_doc, "8.2"))

        assert "不能直接 DELETE" in block or (
            "Delete" in block and "不是普通父目录操作" in block
        ), "§8.2 必须写明不能直接删除他人负责的资源"
        assert "Resource Responsibility" in block or "责任" in block

    def test_delete_requires_formal_mechanism(self, lock_doc):
        block = markdown.section(lock_doc, "8.2")

        for mechanism in ("Recall", "Revoke", "Handoff", "Release"):
            assert mechanism in block, (
                f"§8.2 必须列出正式机制：{mechanism}"
            )


# ============================================================
# 八、Lock Handoff
# ============================================================
class TestHandoff:
    def test_handoff_is_formal(self, lock_doc):
        block = markdown.section(lock_doc, "10. Lock Handoff")

        assert "正式操作" in block, "§10 必须写明 Handoff 是正式操作"

    def test_two_trigger_modes(self, lock_doc):
        block = markdown.section(lock_doc, "10.1")

        assert "Lock Request" in block, "§10.1 必须给出接收方发起的路径"
        assert "Handoff" in block, "§10.1 必须给出控制者主动的路径"

    def test_handoff_audit_fields(self, lock_doc):
        """§10.2 必须列出审计字段。"""
        block = markdown.section(lock_doc, "10.2")

        for field in ("from", "to", "resource", "previous revision",
                      "new responsibility", "timestamp", "related task",
                      "authorization source"):
            assert field in block, f"§10.2 的 Handoff 审计缺少 {field}"

    def test_receiver_cannot_seize(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "10.3"))

        assert "不能直接获取" in block or "不能直接夺取" in block, (
            "§10.3 必须写明接收方不能直接夺取 Lock"
        )


# ============================================================
# 九、Authorization 与 Lock Request 分离
# ============================================================
class TestAuthorizationLockSeparation:
    def test_three_gateway_checks(self, lock_doc):
        """Gateway 至少分别判断三件事。"""
        block = markdown.section(lock_doc, "11.2")

        for check in ("Authorization", "Effective Lock", "Edit Session"):
            assert check in block, (
                f"§11.2 的 Gateway 判定缺少：{check}"
            )

    def test_lock_request_not_authorization_request(self, lock_doc):
        # 这条区分写在 §11.2（不是 §11.1）
        block = markdown.plain(markdown.section(lock_doc, "11.2"))

        assert "Lock Request" in block and "Authorization Request" in block, (
            "§11.1 必须区分 Lock Request 与 Authorization Request"
        )

    def test_write_without_lock_blocked(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "11.1"))

        assert "有资格修改，但目前不能修改" in block or (
            "不能修改" in block
        ), "§11.1 必须有「有 WRITE 但无 Lock → 不能修改」"

    def test_lock_without_authorization_blocked(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "11.1"))

        assert "不能绕过 Authorization" in block or (
            "也不能绕过" in block
        ), "§11.1 必须有「有 Lock 但无 Authorization → 也不能绕过」"


# ============================================================
# 十、Explicit Lock ≠ 永久所有权
# ============================================================
class TestNotPermanentOwnership:
    def test_stated(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "15."))

        assert "不是永久不可撤销的所有权" in block or (
            "Current" in block and "最终" in block
        ) or ("当前独占修改责任" in block), (
            "§15 必须写明 Explicit Lock ≠ 永久所有权"
        )

    def test_still_revocable(self, lock_doc):
        block = markdown.section(lock_doc, "15.")

        for op in ("Release", "Handoff", "Revoke", "Force Unlock"):
            assert op in block, f"§15 必须仍允许 {op}"

    def test_control_not_owner_preserved(self, lock_doc):
        text = markdown.plain(lock_doc)

        assert "Control ≠ Owner" in text, (
            "必须保持上一轮的 `Control ≠ Owner` 原则"
        )


# ============================================================
# 十一、Open Questions
# ============================================================
class TestOpenQuestions:
    """规范点名 12 个问题，必须全部登记且不自行拍板。"""

    REQUIRED = [
        "Controller 转移的完整协议",
        "Task 完成后 Lock",
        "Git Worktree",
        "Permission inheritance",
        "Human 在 Delegation Tree",
        "Workspace Gateway",
        "Lock Recovery",
        "崩溃",
        "LockRequest", "LockHandoff", "Message Type",
        "Lock Store",
        "Delete Lock",
        "multi-resource atomic handoff",
    ]

    @pytest.mark.parametrize("topic", REQUIRED)
    def test_recorded(self, lock_doc, topic):
        block = markdown.section(lock_doc, "18. Open Questions")
        assert block, "找不到 §18 Open Questions"

        assert topic in block, f"§18 必须记录：{topic}"

    def test_does_not_pretend_solved(self, lock_doc):
        block = markdown.plain(markdown.section(lock_doc, "18."))

        assert "没有决定" in block or "假装解决" in block, (
            "§18 必须明确说明本轮没有决定，不要假装解决"
        )


# ============================================================
# 十二、测试需求登记
# ============================================================
class TestTestRequirements:
    def test_has_33_rows(self, lock_doc):
        block = markdown.section(lock_doc, "19. 测试需求")
        assert block, "找不到 §19 测试需求"

        rows = markdown.table_rows(block, "场景")
        # 表里混有分类行（**基础 Lock** 等），只数编号行
        numbered = [
            r for r in rows
            if re.match(r"^\|\s*\d+\s*\|", r)
        ]
        assert len(numbered) == 33, (
            f"§19 有 {len(numbered)} 条编号测试需求，应该有 33 条"
        )

    @pytest.mark.parametrize(
        "keyword",
        [
            "不自动",
            "不随父级 Handoff 改变",
            "不能绕过",
            "不能直接删除",
            "不自动释放",
            "级联失效",
            "不能同时修改",
        ],
    )
    def test_key_scenarios(self, lock_doc, keyword):
        # 【必须用 plain()】：文档里是 `**不能**同时修改`，
        # 不剥强调标记就匹配不到「不能同时修改」。
        block = markdown.plain(markdown.section(lock_doc, "19."))

        assert keyword in block, f"§19 缺少关键场景：{keyword}"


# ============================================================
# 十三、与既有文档的关系
# ============================================================
class TestRelationshipWithExisting:
    def test_workspace_doc_cross_references(self):
        doc = read(WORKSPACE_DOC)
        assert "LOCK_MODEL.md" in doc, (
            "TASK_WORKSPACE.md 必须交叉引用 LOCK_MODEL.md"
        )

    def test_workspace_2_4_clarified(self):
        """TASK_WORKSPACE.md §2.4 必须加上机制精确化的标注。"""
        doc = read(WORKSPACE_DOC)
        block = markdown.section(doc, "2.4 新建资源")

        assert "LOCK_MODEL.md" in block, (
            "§2.4 必须指向 LOCK_MODEL.md 的机制澄清"
        )
        assert "原子" in block, "§2.4 的原子性要求必须仍然可见"
        assert "继承" in block, "§2.4 必须说明机制改为继承"

    def test_adr_0021_not_contradicted(self):
        """LOCK_MODEL.md 必须声明不推翻 ADR-0021。"""
        text = markdown.plain(read(LOCK_DOC))

        assert "ADR-0021" in text, "必须引用 ADR-0021"
        assert "不推翻" in text or "继续沿用" in text or "增量补充" in text, (
            "必须声明与 ADR-0021 是增量关系而不是推翻"
        )

    def test_lock_states_unchanged(self):
        """必须保持 ADR-0021 的三态（无 EXPIRED）。"""
        text = markdown.plain(read(LOCK_DOC))

        assert "ACTIVE" in text or "RELEASED" in text, (
            "应提及 ADR-0021 的锁状态"
        )
        assert "EXPIRED" not in text or "无" in text, (
            "不许引入 EXPIRED 状态"
        )

    def test_resource_layer_linked(self):
        doc = read(LOCK_DOC)
        assert "RESOURCE_LAYER.md" in doc, (
            "LOCK_MODEL.md 必须引用 RESOURCE_LAYER.md"
        )

    def test_delegated_authority_rule_consistent(self, lock_doc):
        """§14 必须与 RESOURCE_LAYER 的权限不放大规则一致。"""
        block = markdown.section(lock_doc, "14.")

        assert "Delegated Authority ≤ Grantor Authority" in block, (
            "§14 必须引用 `Delegated Authority ≤ Grantor Authority`"
        )

    def test_agents_navigation(self):
        assert "LOCK_MODEL.md" in read(AGENTS_FILE), (
            "AGENTS.md 必须加上 LOCK_MODEL.md 导航"
        )


# ============================================================
# 十四、反向测试
# ============================================================
class TestReverseTests:
    def test_edit_end_release_would_be_caught(self):
        """
        反向：如果文档写成「Edit Session 结束 → 自动释放 Lock」，
        守卫必须变红。

        【检测逻辑】核心规则句必须存在。
        """
        fake = "# Lock\n\nEdit Session 结束 → 自动 Release Lock。\n"
        text = markdown.plain(fake)

        assert "EDIT END ≠ LOCK RELEASE" not in text, (
            "伪造文本里本不该有核心规则句 —— 测试前提不成立"
        )

    def test_section_scoping(self, lock_doc):
        a = markdown.section(lock_doc, "3.2")
        b = markdown.section(lock_doc, "13.1")

        assert "Namespace" in a
        assert "不同文件可以并行修改" in b
        assert "Namespace" not in b, "section() 范围太大"

    def test_all_used_sections_are_nonempty(self, lock_doc):
        """
        反向：本文件用到的每个 section 都必须**真的非空**。
        空 section 会让内容断言变成空转（false-green）。
        """
        for prefix in ("0.3", "3.2", "3.3", "4.3", "5.1", "5.2", "6.2",
                       "6.3", "7.1", "7.2", "8.1", "8.2", "9. Lock",
                       "10. Lock Handoff", "10.1", "10.2", "10.3",
                       "11.1", "11.2", "12. Edit Session", "12.1",
                       "13.1", "13.2", "13.4", "15.", "18.", "19."):
            assert markdown.section(lock_doc, prefix), (
                f"§{prefix} 是空的 —— 针对它的守卫是空转的"
            )

    def test_core_rule_is_live(self, lock_doc):
        """反向：确认核心规则句确实在文档里。"""
        assert "EDIT END ≠ LOCK RELEASE" in markdown.plain(lock_doc)

    def test_no_thirty_three_claim_is_live(self, lock_doc):
        """反向：§19 确实有编号行（否则「33 条」那条是空转的）。"""
        rows = markdown.table_rows(
            markdown.section(lock_doc, "19."), "场景"
        )
        numbered = [r for r in rows if re.match(r"^\|\s*\d+\s*\|", r)]

        assert numbered, "§19 里没有编号行 —— 计数守卫会误判"


# ============================================================
# 十五、ADR
# ============================================================
class TestADR:
    def test_exists(self):
        assert ADR_0026.exists(), "ADR-0026 不存在"

    def test_sections(self):
        text = read(ADR_0026)
        for h in ("## Context", "## Decision", "## Consequences"):
            assert h in text, f"ADR-0026 缺少 {h}"

    def test_indexed(self):
        index = read(ARCH_DIR / "adr" / "README.md")
        assert "0026" in index
        assert "0026-lock-hierarchy-and-edit-session.md" in index

    def test_declares_incremental(self):
        """ADR-0026 必须声明是 ADR-0021 的增量。"""
        text = markdown.plain(read(ADR_0026))

        assert "增量" in text or "不推翻" in text, (
            "ADR-0026 必须声明不推翻 ADR-0021"
        )

    def test_reconciles_workspace_2_4(self):
        """ADR-0026 必须显式处理 §2.4 的关系（不静默覆盖）。"""
        text = read(ADR_0026)

        assert "§2.4" in text or "2.4" in text, (
            "ADR-0026 必须显式讨论 TASK_WORKSPACE.md §2.4"
        )

    def test_adr_count_not_inflated(self):
        """
        ADR 编号必须**连续**，且本轮新增的 0026 存在。

        【这条第一版是错的 —— 与项目里另外两条同样的毛病】
            第一版写 `assert max(numbers) == 26` —— 把最大值写死了。
            结果下一个 ADR（0027）一加进来就失败。

            守卫名字是「不许膨胀」，那就该测**连续性**（没有空洞、
            没有跳号），而不是测**具体最大值**。
        """
        numbers = sorted(
            int(p.name[:4]) for p in (ARCH_DIR / "adr").glob("*.md")
            if p.name[:4].isdigit()
        )

        assert 26 in numbers, "ADR-0026 不存在"

        # 0018 之后（这是本会话开始新增 ADR 的起点）不许有空洞
        recent = [n for n in numbers if n >= 18]
        gaps = [
            n for n in range(min(recent), max(recent) + 1)
            if n not in recent
        ]
        assert not gaps, (
            f"0018 之后的 ADR 编号有空洞：{gaps}\n（实际存在：{recent}）"
        )


# ============================================================
# 十六、Scope：不许提前实现
# ============================================================
class TestNoImplementation:
    # 【设计阶段守卫 → 实现阶段的过渡】
    #   原来还禁止 `workspace_gateway.py` —— 当时 Workspace 是 FUTURE。
    #   Workspace Core 第一阶段已授权实现 → 放行。
    #   继续禁止：**可被任意抢的 lock 管理器**（任务书 §43）、
    #   以及被 ADR-0027 移除/降级的概念。
    FORBIDDEN_MODULES = (
        "lock_manager.py", "lock_store.py",
        "resource_manager.py", "delegation_service.py",
        "controller_registry.py", "authorization_engine.py",
        "resource_store.py",
    )
    FORBIDDEN_CLASSES = (
        # 🔒 没有可被任意抢的 lock() 管理器（任务书 §43）
        "LockManager", "LockStore",
        # 🔒 ADR-0027 移除/降级的概念
        "ResourceManager", "DelegationService", "ControllerRegistry",
        "AuthorizationEngine", "ResourceStore", "DelegationTree",
        "PermissionEngine", "EditSession", "LockHandoff",
    )

    def test_no_lock_modules(self):
        for layer in ("core", "services", "communication", "storage", "agents"):
            for name in self.FORBIDDEN_MODULES:
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 本轮禁止实现"
                )

    def test_no_lock_classes(self):
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

    def test_no_lock_tables(self, db):
        names = set(db.table_names())

        for forbidden in ("locks", "resources", "delegations", "controllers",
                          "authorization_requests", "lock_requests",
                          "edit_sessions"):
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
            # Private Working Memory（2026-11-08 任务书授权）
            "agent_private_working_memory",
            "agent_private_working_memory_revisions",
            # Private Short / Long-Term Memory（2026-11-08）
            "agent_short_term_memory",
            "agent_short_term_memory_archive",
            "agent_long_term_memory",
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
        # + 2（Public Working Memory）
        # + 2（Private Working Memory）
        # + 3（Private Short / Long-Term Memory）
        # + 1（Commit Requests）= 29
        assert len(names) == 29, (
            f"表数应为 13 + 5 + 3 + 2 + 2 + 3 + 1 = 29，"
            f"实际 {len(names)}：{sorted(names)}"
        )

    def test_no_lock_message_types(self):
        """本轮不新增 LockRequest / LockHandoff Message Type。"""
        banned = ("LOCK_REQUEST", "LOCK_HANDOFF", "LOCK_GRANT",
                  "LOCK_RELEASE")

        for path in (PROJECT_ROOT / "communication").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for name in banned:
                assert name not in source, (
                    f"{path.name} 里出现了 {name} —— "
                    f"§18 说它仍是 OPEN，本轮不实现"
                )
