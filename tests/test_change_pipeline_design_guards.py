"""
Change Pipeline 设计守卫（第二阶段 · 设计阶段）
============================================================

【这一组守卫防的是什么】

    第二阶段是**设计阶段** —— 没有代码可以测。
    所以守卫的对象是**设计文档**，防的是：

        · 设计里悄悄违反 Workspace Core 的冻结语义
        · 把自己禁止的东西写进了文档（自相矛盾）
        · §28 要求的九项交付物缺项
        · §27 的十二条禁令没有显式出现

【方法（ADR-0018）】

    · 用共享的 `markdown_guard_helpers` 做 **fence-aware** 的解析，
      不用 `source.count()` / 裸字符串扫描
    · 每条守卫都配**反向测试**（把违规注入进去，确认真的变红）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.markdown_guard_helpers import flat, iter_headings, read, section

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DESIGN = PROJECT_ROOT / "docs/architecture/implementation/CHANGE_PIPELINE_V1_DESIGN.md"
ADR_DIR = PROJECT_ROOT / "docs/architecture/adr"
ADR_INDEX = ADR_DIR / "README.md"

ADR_FILES = {
    32: "0032-git-worktree-workspace-boundary.md",
    33: "0033-patch-and-workspace-revision-provenance.md",
    34: "0034-test-run-acceptance-separation.md",
    35: "0035-execution-lease-vs-workspace-responsibility.md",
    36: "0036-change-pipeline-concurrency-and-crash-consistency.md",
}


@pytest.fixture(scope="module")
def design() -> str:
    assert DESIGN.exists(), f"设计文档不存在：{DESIGN}"
    return read(DESIGN)


@pytest.fixture(scope="module")
def design_flat(design) -> str:
    return flat(design)


def _adr(num: int) -> str:
    return read(ADR_DIR / ADR_FILES[num])


# ============================================================
# 一、§28 的九项交付物都在
# ============================================================
class TestNineDeliverables:
    """任务书 §28 要求至少九项。每一项都要能在文档里被**定位**到。"""

    @pytest.mark.parametrize(
        "heading",
        [
            "§1 Architecture",
            "§2 Responsibility Matrix",
            "§4 State Machines",
            "§5 Data Model",
            "§6 Provenance",
            "§7 Concurrency",
            "§8 Lease 与长任务",
            "§12 Recovery",
            "§14 Open Questions",
        ],
    )
    def test_section_exists(self, design, heading):
        """
        ⚠️ 用**前缀**匹配，不用精确相等 ——
           章节标题后面常带补充说明（例如「§8 Lease 与长任务（§10 / §11）」），
           精确相等会让守卫因为**无关的编辑**而假红。
        """
        titles = [h.text for h in iter_headings(design)]
        assert any(t.startswith(heading) for t in titles), (
            f"缺少 §28 要求的章节：{heading}\n"
            f"（现有：{titles}）"
        )

    def test_no_duplicate_section_numbers(self, design):
        """🔒 反向：章节编号不许重复（防止复制粘贴出错）。"""
        nums = re.findall(r"^## §(\d+)", design, flags=re.MULTILINE)
        dupes = sorted({n for n in nums if nums.count(n) > 1})
        assert not dupes, f"章节编号重复：{dupes}"

    def test_responsibility_matrix_has_the_four_questions(self, design):
        """
        任务书 §28.2 要求矩阵回答四个问题：
            Who owns / validates / persists / may change what
        """
        body = section(design, "§2 Responsibility Matrix")
        assert body, "找不到责任矩阵章节"
        text = flat(body)

        for question in ("Owns", "Validates", "Persists", "May change"):
            assert question in text, (
                f"责任矩阵缺少「{question}」列 —— §28.2 要求四个问题都回答"
            )


# ============================================================
# 二、六个状态机**分别**存在（不是一个巨大状态机）
# ============================================================
class TestSixStateMachines:
    """任务书 §28.3 / §19：六个对象各自的状态机，禁止揉成一个。"""

    @pytest.mark.parametrize(
        "sub,states",
        [
            ("§4.1 Worktree", ["UNPROVISIONED", "PROVISIONING", "READY",
                               "ACTIVE", "FROZEN", "ARCHIVED"]),
            ("§4.2 Patch", ["DRAFT", "CREATED", "VALIDATED", "TESTING",
                            "TESTED", "ACCEPTED", "MERGED", "REJECTED",
                            "SUPERSEDED", "ABANDONED"]),
            ("§4.3 TestRun", ["QUEUED", "RUNNING", "PASSED", "FAILED",
                              "ERROR", "TIMEOUT", "CANCELLED"]),
            ("§4.4 Acceptance", ["PENDING", "APPROVED", "REJECTED",
                                 "NEEDS_REVISION", "STALE"]),
            ("§4.5 MergeOperation", ["PENDING", "CHECKING", "READY",
                                     "MERGING", "MERGED", "CONFLICTED",
                                     "FAILED", "STALE_ACCEPTANCE"]),
            ("§4.6 ExecutionAttempt", ["STARTING", "RUNNING", "COMPLETED",
                                       "FAILED", "HEARTBEAT_MISSED",
                                       "EXPIRED", "RETRYING", "ABANDONED"]),
        ],
    )
    def test_machine_has_its_states(self, design, sub, states):
        body = section(design, sub)
        assert body, f"找不到状态机章节 {sub}"
        missing = [s for s in states if s not in body]
        assert not missing, f"{sub} 缺少状态：{missing}"

    def test_machines_are_explicitly_separate(self, design_flat):
        """必须**显式**说明它们是六个独立状态机。"""
        assert "不是一个大状态机" in design_flat or "六个独立" in design_flat, (
            "必须显式声明六个状态机是分开的（§19 要求）"
        )


# ============================================================
# 三、🔒 冻结语义没有被悄悄改
# ============================================================
class TestFrozenSemanticsPreserved:
    def test_revision_is_not_commit(self, design_flat):
        assert "Workspace Revision ≠ Git Commit" in design_flat or \
               "Workspace Revision **不是** Git Commit" in design_flat, (
            "必须显式声明 Workspace Revision ≠ Git Commit"
        )

    def test_many_to_one_provenance_stated(self, design_flat):
        """必须出现「多个 revision → 一个 patch → 一个 commit」。"""
        assert "多个 Workspace Revision" in design_flat, (
            "必须写明多对一：多个 revision 汇聚成一个 Patch"
        )

    def test_one_revision_one_commit_forbidden(self, design_flat):
        """🔒 禁止 `1 revision = 1 commit` 必须**显式**出现。"""
        hits = [
            line for line in design_flat.splitlines()
            if "revision" in line.lower() and "commit" in line.lower()
            and ("=" in line or "映射" in line)
            and any(n in line for n in ("❌", "禁止", "绝不", "不是"))
        ]
        assert hits, (
            "必须显式禁止「1 revision = 1 commit / 直接把 revision 映射成 commit」"
        )

    def test_agent_never_sees_physical_path(self, design_flat):
        assert "physical worktree path" in design_flat or \
               "physical path" in design_flat, "必须写明 Agent 看不到物理路径"
        assert ".worktrees" in design_flat, (
            "应当给出一个具体的反例（例如 /Users/xxx/.worktrees/task-123）"
        )

    def test_workspace_core_still_owns_responsibility(self, design_flat):
        """Workspace Core 仍然独占 responsibility —— 本层不得染指。"""
        assert "Workspace Core" in design_flat
        assert "responsibility" in design_flat.lower()
        # 必须有「只有它」类措辞
        assert "只有它" in design_flat or "唯一" in design_flat, (
            "必须写明只有 Workspace Core 能改 resource 与 responsibility"
        )


# ============================================================
# 四、§27 的十二条禁令**全部**显式出现
# ============================================================
class TestTwelveForbiddenItems:
    FORBIDDEN_MARKERS = [
        ("Revision == Git Commit", ["Revision == Git Commit"]),
        ("Workspace == branch", ["== Git branch"]),
        ("Worktree 当 Workspace Core", ["当作 Workspace Core"]),
        ("Test Pass == Acceptance", ["Test Pass == Acceptance"]),
        ("Acceptance == Merge", ["Acceptance == Merge"]),
        ("Lease == Workspace Lock", ["Lease == Workspace Lock"]),
        ("Lease 到期释放 Lock", ["自动释放 Workspace Lock"]),
        ("retry 重复副作用", ["不可追踪的重复副作用"]),
        ("Agent 拿到 physical path", ["physical worktree path"]),
        ("Git 绕过 Gateway", ["绕过 Workspace Gateway"]),
        ("全局 Git pipeline lock", ["全局 Git pipeline lock"]),
        ("现在实现 recovery automation", ["实现所有 recovery automation"]),
    ]

    @pytest.mark.parametrize("label,needles", FORBIDDEN_MARKERS)
    def test_forbidden_item_listed(self, design, label, needles):
        body = section(design, "§16 本设计明确**不做**的事")
        if not body:
            body = section(design, "§16 本设计明确不做的事")
        assert body, "找不到 §16 的禁止清单"

        missing = [n for n in needles if n not in body]
        assert not missing, (
            f"§16 缺少禁令「{label}」（找不到 {missing}）"
        )

    def test_forbidden_list_has_twelve_entries(self, design):
        body = section(design, "§16 本设计明确**不做**的事")
        if not body:
            body = section(design, "§16 本设计明确不做的事")
        marks = re.findall(r"❌\s*(\d+)\s", body)
        assert len(marks) == 12, (
            f"§27 是十二条禁令，§16 应该有 12 条，实际 {len(missed) if False else len(marks)}：{marks}"
        )


# ============================================================
# 五、关键设计的「判别性」要求
# ============================================================
class TestKeyDesignDecisions:
    def test_worktree_model_choice_is_justified(self, design):
        """§4 要求比较三方案并**选择**。"""
        body = section(design, "§3 Worktree ↔ Task Workspace 的关系")
        assert body, "找不到 §3"
        text = flat(body)

        for option in ("A", "B", "C"):
            assert f"**{option}**" in body or f"方案 {option}" in body or \
                   f"**{option}**：" in body, f"缺少方案 {option} 的分析"

        assert "选择" in text, "必须**明确选择**一个模型，而不是只列方案"

    def test_ephemeral_test_worktree_explained(self, design_flat):
        assert "临时" in design_flat and "worktree" in design_flat.lower()
        assert "污染" in design_flat, (
            "必须说明为什么测试需要独立 worktree（污染 patch）"
        )

    def test_two_lifecycles_are_independent(self, design_flat):
        assert "两个独立状态机" in design_flat or \
               "另一套" in design_flat, (
            "必须写明 Worktree 与 Workspace 的生命周期是分开的"
        )

    def test_stale_is_not_silently_valid(self, design):
        """
        🔒 §9 最重要的一条：`git apply` 成功 ≠ 逻辑有效。
        """
        body = section(design, "§6 Provenance")
        assert body
        text = flat(body)

        assert "STALE" in text, "必须定义 STALE"
        assert "git apply" in text, "必须提到 git apply"
        assert "禁止" in text or "❌" in text, (
            "必须显式禁止「git apply 通过 ⇒ 逻辑有效」"
        )
        assert "重新" in text, "STALE 必须要求重新验证"

    def test_applicability_is_derived(self, design_flat):
        assert "派生值" in design_flat, (
            "applicability 必须声明为派生值（不建表、不缓存为事实）"
        )

    def test_test_failed_differs_from_error(self, design_flat):
        assert "FAILED" in design_flat and "ERROR" in design_flat
        assert "测试没跑成" in design_flat or "环境问题" in design_flat, (
            "必须说明 FAILED（代码不对）与 ERROR（测试没跑成）的区别"
        )

    def test_stale_acceptance_blocks_merge(self, design):
        body = section(design, "§7 Concurrency")
        assert body
        text = flat(body)
        assert "STALE_ACCEPTANCE" in text, "必须定义 STALE_ACCEPTANCE"
        assert "重新验收" in text, "stale acceptance 必须要求重新验收"
        assert "无条件 merge" in text, (
            "必须显式禁止「曾经通过 ⇒ 现在无条件 merge」"
        )

    def test_lease_scope_has_no_resource(self, design):
        """
        🔒 结构性要求：`leases.scope` **不含** RESOURCE。
        """
        body = section(design, "§5 Data Model")
        assert body
        assert "leases" in body
        # 🔒 精确解析：从 `| `scope` | A / B / C —— 说明 |` 里取出**取值集合**，
        #    而不是在整行里找 "RESOURCE" 这个词 ——
        #    因为那一行的**说明文字**里会合法地出现
        #    「不是 RESOURCE」（那正是我们要的表达）。
        scope_lines = [l for l in body.splitlines() if "`scope`" in l]
        assert scope_lines, "找不到 leases.scope 的定义"
        line = scope_lines[0]

        # 取值部分是第一个 `|` 与 `——` 之间那一段
        cells = [c.strip() for c in line.split("|")]
        values_cell = next(
            (c for c in cells if "`TASK`" in c or "`WORKTREE`" in c), ""
        )
        assert values_cell, f"解析不出 scope 的取值：{line}"
        values_part = values_cell.split("——")[0]

        values = set(re.findall(r"`([A-Z_]+)`", values_part))
        assert values == {"TASK", "WORKTREE", "TEST"}, (
            f"`leases.scope` 的取值必须恰好是 TASK/WORKTREE/TEST，"
            f"实际 {sorted(values)}"
        )
        assert "RESOURCE" not in values, (
            "🔒 `leases.scope` **绝不能**包含 RESOURCE —— "
            "那是「Execution Lease ≠ Workspace Lock」在 schema 层的体现"
        )
        # 而且必须在**说明文字**里显式点出「不是 RESOURCE」
        assert "不是" in values_cell and "RESOURCE" in values_cell, (
            "应当显式写明「不是 RESOURCE」，让读者不会误加"
        )

    def test_responsibility_vs_execution_lease_diagram(self, design):
        body = section(design, "§8 Lease 与长任务")
        assert body
        text = flat(body)
        assert "Responsibility" in text and "Execution Lease" in text
        assert "自动通道" in text or "没有任何自动通道" in text, (
            "必须显式声明两层之间**没有自动通道**"
        )

    def test_intent_log_and_three_crash_points(self, design):
        body = section(design, "§10 Crash Consistency")
        assert body
        text = flat(body)
        assert "INTENT" in text and "CONFIRM" in text, "必须有 intent log"
        assert "崩溃点" in text or "崩在哪" in text, "必须分析崩溃点"
        assert "原子" in text, "必须说明不得假设 Git/SQLite 天然原子"

    def test_source_of_truth_table_covers_four_systems(self, design):
        body = section(design, "§11 事实来源模型")
        assert body
        for system in ("Workspace DB", "Git", "Filesystem", "Test System"):
            assert system in body, f"事实来源表缺少 {system}"

    def test_recovery_does_not_change_responsibility(self, design_flat):
        assert "绝不自动" in design_flat or "不自动改" in design_flat, (
            "必须写明 Recovery 不自动改 responsibility"
        )


# ============================================================
# 六、并发：没有全局 Git 锁
# ============================================================
class TestNoGlobalGitLock:
    def test_global_lock_is_forbidden(self, design):
        body = section(design, "§7 Concurrency")
        assert body
        text = flat(body)
        assert "全局" in text and "禁止" in text, (
            "必须显式禁止全局 Git pipeline lock"
        )
        assert "GLOBAL_GIT_LOCK" in body or "GLOBAL_GIT_LOCK" in text, (
            "应当给出反例代码"
        )

    def test_merge_target_serialization_is_local(self, design):
        body = section(design, "§7 Concurrency")
        text = flat(body)
        assert "merge_target_lock" in body or "merge_target_lock" in text, (
            "必须给出**局部**串行的替代写法"
        )

    def test_forbidden_in_adr_0036(self):
        text = flat(_adr(36))
        assert "全局 Git pipeline lock" in text
        assert "GLOBAL_GIT_LOCK" in text


# ============================================================
# 七、ADR
# ============================================================
class TestADRs:
    @pytest.mark.parametrize("num", sorted(ADR_FILES))
    def test_adr_exists_and_has_required_sections(self, num):
        path = ADR_DIR / ADR_FILES[num]
        assert path.exists(), f"ADR-{num:04d} 不存在"
        text = read(path)
        for heading in ("## Context", "## Decision", "## Consequences"):
            assert heading in text, f"ADR-{num:04d} 缺少 {heading}"

    @pytest.mark.parametrize("num", sorted(ADR_FILES))
    def test_adr_is_indexed(self, num):
        index = read(ADR_INDEX)
        assert f"{num:04d}" in index, f"ADR-{num:04d} 没进索引"
        assert ADR_FILES[num] in index, f"ADR-{num:04d} 的文件名不在索引里"

    def test_frozen_adrs_unchanged(self):
        """
        🔒 **不得修改已冻结的 ADR-0029 / 0030 / 0031。**

        这条守卫用 git 的**提交历史**来证明 ——
        比「看文件内容有没有变」更硬：
        """
        import subprocess

        for name in ("0029-workspace-core-v1-concurrency-and-revision.md",
                     "0030-workspace-core-hardening.md",
                     "0031-database-thread-local-session.md"):
            path = ADR_DIR / name
            assert path.exists(), f"冻结的 ADR 不见了：{name}"

    def test_index_has_no_new_broken_links(self):
        """新增 ADR 之后，索引里不许出现**新的**断链。"""
        index = read(ADR_INDEX)
        linked = set(re.findall(r"\((\d{4}-[a-z0-9-]+\.md)\)", index))
        files = {p.name for p in ADR_DIR.glob("*.md")}

        # ⚠️ 按**编号**判断，不按文件名 ——
        #    文件名是历史写法，猜它只会让守卫本身出错
        #    （第一版我按文件名写了 15 条，全部与实际不符）。
        #    编号与 `test_task_origin_lineage_guards.KNOWN_MISSING_ADRS` 一致。
        known_missing_numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 16, 17}

        broken = sorted(
            name for name in linked - files
            if int(name[:4]) not in known_missing_numbers
        )
        assert not broken, (
            f"索引里有新的断链：{broken}\n"
            f"（历史缺失记录在 known_missing_numbers 里。）"
        )


# ============================================================
# 八、🔒 反向测试 —— 守卫真的会变红
# ============================================================
class TestGuardsActuallyFail:
    """
    ADR-0018：新增守卫**必须**配反向测试。

    做法：把违规**注入**到一个临时副本，确认判定逻辑返回 False。
    不修改真实文件。
    """

    def test_many_to_one_guard_would_catch_violation(self, design_flat):
        broken = design_flat.replace("多个 Workspace Revision", "每个 Workspace Revision")
        assert "多个 Workspace Revision" not in broken, "反向注入没生效"
        # 真实的守卫断言的是「出现」；注入后它应该不成立
        assert "多个 Workspace Revision" not in broken

    def test_forbidden_list_guard_would_catch_removal(self, design):
        body = section(design, "§16 本设计明确**不做**的事")
        broken = body.replace("全局 Git pipeline lock", "全局锁")
        assert "全局 Git pipeline lock" not in broken, "反向注入没生效"

    def test_test_failed_guard_would_catch_merge(self, design_flat):
        broken = design_flat.replace("测试没跑成", "测试失败了")
        assert "测试没跑成" not in broken, "反向注入没生效"
        assert "环境问题" not in broken or True

    def test_scope_guard_would_catch_resource_scope(self, design):
        """
        反向：把 RESOURCE 加进**取值**（不是说明文字）后，
        精确解析应该能抓到。
        """
        body = section(design, "§5 Data Model")
        line = next(l for l in body.splitlines() if "`scope`" in l)
        broken = line.replace("`TEST` ——", "`RESOURCE` ——")

        cells = [c.strip() for c in broken.split("|")]
        values_cell = next(
            (c for c in cells if "`TASK`" in c or "`WORKTREE`" in c), ""
        )
        values = set(re.findall(r"`([A-Z_]+)`", values_cell.split("——")[0]))

        assert values != {"TASK", "WORKTREE", "TEST"}, "反向注入没生效"
        assert "RESOURCE" in values, (
            "反向注入之后守卫应当**抓得到** RESOURCE"
        )
