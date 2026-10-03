"""
V1 范围与 Future Front Agent 兼容性守卫

============================================================
【这个文件守什么】
============================================================

本轮确立了：

    CURRENT / V1     Professional User → Agent → Task → 协作 → Result
    FUTURE           Human → Front Agent → Agent Economy

风险是**文档漂移** —— 有人（人或 AI）把 FUTURE 写成 CURRENT：

    "Front Agent is required in V1."
    "当前架构需要 Front Agent。"

那会让项目在还没跑通 Agent Economy 核心闭环之前，
就去实现一个 Human-facing 组件。

============================================================
【为什么不能用简单 substring 扫描】
============================================================

本项目在同类检查上已经踩过 10+ 次假绿/误报。关键是：

    文档里**必须**能出现 "Front Agent" 这个词 ——
    因为我们要说「它不实现」「它是 FUTURE」。

如果只查「文档里有没有 Front Agent」，守卫会**永远为真**（因为到处都有）。

所以判定原则是：

    **一个 Future 组件名只有在「肯定式、非历史、非否定、
      非 §Future 小节」的语境里才违规。**

并且必须理解：
    · CURRENT / FUTURE 标注
    · 示例（Example）
    · 否定（NOT / 不 / ❌）
    · 架构演进说明（Architecture Evolution）
    · 禁止清单（FORBIDDEN NOW）

============================================================
【本文件依赖 parser hardening 后的共享 helper】
============================================================

`section()` / `iter_headings()` / `plain()` 都用
`tests/markdown_guard_helpers.py`（fence-aware），
并且有独立单元测试 `test_markdown_guard_helpers.py`。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import markdown_guard_helpers as markdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCH_DIR = PROJECT_ROOT / "docs" / "architecture"
AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"

#: 需要检查的架构文档
ARCH_FILES = sorted(ARCH_DIR.rglob("*.md")) + [AGENTS_FILE]

#: Future 组件名（V1 不实现）
FUTURE_COMPONENTS = (
    "Front Agent",
    "Human Gateway",
    "Human-facing Agent",
    "Human Intent Agent",
    "Human Coordinator",
    "Human Interaction Service",
)

#: 表示「这是未来 / 不实现 / 禁止 / 否定 / 历史」的标记
FUTURE_OR_NEGATION = (
    # 状态标注
    "FUTURE", "Future", "future", "OPEN DESIGN", "DESIGNED",
    # 否定
    "不实现", "不新增", "不创建", "不要", "不是", "不得", "禁止", "❌",
    "未实现", "NOT", "not required", "暂不", "以后", "未来", "将来",
    "不需要", "无需", "可选", "不是必需",
    # 禁止清单
    "FORBIDDEN NOW",
    # 演进 / 历史
    "演进", "修正", "旧模型", "曾经", "本轮",
    # 注意：**不要把章节标题放进这里**。
    #   "Current V1 vs Future" 曾经在这里，结果 §2.5 里任何一句话
    #   都因为「所在小节标题含 Future」而被放过 —— 守卫在该节内**全盲**。
    #   （反向测试 test_guard_f_inject_old_model_fails 抓到了这一点。）
)


def read(path: Path) -> str:
    return markdown.read(path)


def code_only(document: str) -> str:
    """
    去掉围栏代码块与行内代码，只留 Markdown 正文。

    【为什么】
        架构图里会有 `Human → Front Agent → Agent Economy`，
        那是**图示**，不是「要求实现」的断言。
        另外代码块里可能有 `FrontAgent` 之类的类名示例。
    """
    return markdown.strip_fenced_blocks(document)


def is_affirmative_requirement(document: str, index: int) -> bool:
    """
    判断 `index` 处的 Future 组件名是不是**肯定式的要求**。

    ============================================================
    【判定规则：只查**本行**，不查所在小节】
    ============================================================

        第一版同时查「本行」和「所在小节」。结果：

            小节标题 = "## 2.5 Current V1 vs Future Front Agent"
            小节内每一行都因为标题含 "Future" 而被放过
            → **守卫在这一节里完全失效**

        反向测试 `test_guard_f_inject_old_model_fails` 抓到了这一点。

    ------------------------------------------------------------
        为什么「只看本行」对**要求句式**是正确且更严的：

            "Front Agent is required in V1."

        这种句子**不存在**「因为在讲 Future 所以合法」的情况。
        要么它是违规的断言，要么它被**本行**明确否定：

            "Front Agent is NOT required in V1."     ← 本行有 NOT
            "V1 不需要 Front Agent。"                 ← 本行有 不需要

        所以对要求句式来说，本行判定既足够又更严格。
    """
    line_start = document.rfind("\n", 0, index) + 1
    line_end = document.find("\n", index)
    line = document[line_start: line_end if line_end != -1 else len(document)]

    return not any(marker in line for marker in FUTURE_OR_NEGATION)


#: 「要求现在就实现」的断言模式
REQUIREMENT_PATTERNS = (
    r"requires?\s+(?:a\s+)?{c}",
    r"当前架构需要\s*{c}",
    r"V1\s*(?:需要|必须有|要求)\s*{c}",
    r"{c}\s+is\s+required",
    r"{c}\s+是必需的",
    r"{c}\s+必须(?:存在|实现)",
)


def find_future_as_current(document: str):
    """找出所有**肯定式**把 Future 组件当 CURRENT 的写法。"""
    text = code_only(document)
    hits = []

    for component in FUTURE_COMPONENTS:
        # ① 显式的要求句式
        for template in REQUIREMENT_PATTERNS:
            pattern = template.format(c=re.escape(component))
            for match in re.finditer(pattern, text):
                if is_affirmative_requirement(text, match.start()):
                    hits.append((component, match.group(0)))

        # ② 位置绑定：`CURRENT ... Front Agent` 同行
        for match in re.finditer(re.escape(component), text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.start())
            line = text[line_start: line_end if line_end != -1 else len(text)]

            if "CURRENT" not in line:
                continue
            if any(m in line for m in FUTURE_OR_NEGATION):
                # 允许 `CURRENT ... | FUTURE ...` 同一行（表格对照）
                continue
            hits.append((component, line.strip()))

    return hits


# ============================================================
# 一、Guard：Future 不许被写成 CURRENT
# ============================================================
class TestFutureStaysFuture:
    @pytest.mark.parametrize(
        "path", ARCH_FILES, ids=[p.name for p in ARCH_FILES]
    )
    def test_no_future_component_required_now(self, path):
        """架构正文不许把 Front Agent 等写成「当前需要」。"""
        hits = find_future_as_current(read(path))

        assert not hits, (
            f"{path.relative_to(PROJECT_ROOT)} 里出现了"
            f"**肯定式**的「Future 组件是当前要求」：\n"
            + "\n".join(f"    {c!r}: {s!r}" for c, s in hits)
        )

    def test_detection_would_catch_the_violation(self):
        """
        **Guard F 反向测试（正向方向）**：注入违规 → 检测必须命中。
        """
        bad = "## 某节\n\nFront Agent is required in V1.\n"

        hits = find_future_as_current(bad)
        assert hits, "检测逻辑抓不到「Front Agent is required in V1」—— 守卫是假的"

    def test_detection_would_catch_chinese_violation(self):
        bad = "## 某节\n\n当前架构需要 Front Agent。\n"

        assert find_future_as_current(bad), "抓不到中文写法的违规"

    def test_detection_would_catch_current_binding(self):
        bad = "| Front Agent | CURRENT | 已实现 |\n"

        assert find_future_as_current(bad), (
            "抓不到「Front Agent 标为 CURRENT」"
        )


# ============================================================
# 二、Guard F：反向测试（写入 → 变红；恢复 → 变绿）
# ============================================================
class TestGuardF_ReverseTests:
    def test_guard_f_inject_old_model_fails(self):
        """注入旧模型 → 必须变红。"""
        correct = (
            "## 2.5 Current V1 vs Future Front Agent\n\n"
            "Front Agent 是 `FUTURE`，V1 不实现。\n"
        )
        assert not find_future_as_current(correct)

        regressed = (
            "## 2.5 Current V1 vs Future Front Agent\n\n"
            "Front Agent is required in V1.\n"
        )
        assert find_future_as_current(regressed), (
            "把「V1 需要 Front Agent」写回去之后守卫没有变红"
        )

    def test_guard_f_restore_passes(self):
        """恢复正确模型 → 必须变绿。"""
        for path in ARCH_FILES:
            assert not find_future_as_current(read(path)), (
                f"{path.name} 里还有肯定式的 Future-as-CURRENT"
            )

    @pytest.mark.parametrize(
        "line",
        [
            "Front Agent 是 `FUTURE`，本轮不实现。",
            "❌ 不实现 Front Agent。",
            "| 是否要 Front Agent | ❌ **不要** | 可选扩展 |",
            "未来可能有 Front Agent（见 §2.9.5）。",
            "**Front Agent 不是**中央 Coordinator。",
            "不要把 Front Agent 设计成特殊的「上帝组件」。",
            "禁止：Front Agent · Human Gateway Service。",
            "旧模型曾经包含 Human Gateway。",
        ],
    )
    def test_correct_and_negated_statements_are_allowed(self, line):
        """
        **必须允许**这些写法 —— 否则守卫会误杀正确文档。
        """
        doc = f"## 某节\n\n{line}\n"

        assert not find_future_as_current(doc), (
            f"这一行被误判成「Future as CURRENT」：{line!r}"
        )

    def test_architecture_diagram_is_not_a_requirement(self):
        """架构图里的 `Human → Front Agent` **不是**「要求实现」。"""
        doc = markdown.read(ARCH_DIR / "TASK_ORIGIN_LINEAGE.md")
        section = markdown.section(doc, "2.9.5")

        assert section, "找不到 §2.9.5"
        assert "Front Agent" in section
        assert not find_future_as_current(section), (
            "§2.9.5 的架构图被误判成「要求实现 Front Agent」"
        )


# ============================================================
# 三、V1 模型必须存在且标 CURRENT
# ============================================================
class TestV1ModelIsPresent:
    @pytest.fixture(scope="class")
    def lineage(self) -> str:
        return read(ARCH_DIR / "TASK_ORIGIN_LINEAGE.md")

    def test_v1_section_exists(self, lineage):
        assert markdown.section(lineage, "2.9"), "§2.9 不存在"

    def test_professional_user_is_the_v1_entry(self, lineage):
        """V1 入口必须是 Professional User。"""
        block = markdown.plain(markdown.section(lineage, "2.9.1"))

        assert "Professional User" in block or "专业的 Agent 使用者" in block, (
            "§2.9.1 必须写明 V1 面向专业 Agent 使用者"
        )

    def test_v1_chain_is_documented(self, lineage):
        """V1 链路必须写出。"""
        block = markdown.plain(markdown.section(lineage, "2.9.1"))

        for element in ("Agent A", "Child Task", "Agent B",
                        "Root Task Result"):
            assert element in block, f"§2.9.1 的 V1 链路缺少 {element}"

    def test_not_a_central_task_manager(self, lineage):
        """必须明确 V1 **不是**中央调度。"""
        block = markdown.section(lineage, "2.9.2")

        assert "中央 Task Manager" in block, (
            "§2.9.2 必须明确否定「Human → 中央 Task Manager → 调度」"
        )
        assert "自动拆任务" in block or "自动指定 Agent" in block

    def test_four_wrong_assumptions_documented(self, lineage):
        """§2.9.4 必须列出 4 个错误假设（结构化：表格 4 行）。"""
        block = markdown.section(lineage, "2.9.4")
        rows = markdown.table_rows(block, "错误假设")

        assert len(rows) == 4, (
            f"§2.9.4 的错误假设表有 {len(rows)} 行，应该恰好 4 行"
        )

    def test_front_agent_is_still_an_agent(self, lineage):
        """Front Agent 本质上仍是 Agent。"""
        block = markdown.plain(markdown.section(lineage, "2.9.6"))

        assert "本质上仍然是 Agent" in block or "仍然是 Agent" in block
        for capability in ("Identity", "Capability", "Memory", "Tools"):
            assert capability in block, (
                f"§2.9.6 必须列出 Front Agent 也具备：{capability}"
            )

    def test_front_agent_not_central_controller(self, lineage):
        """Front Agent 不是中央控制器。"""
        block = markdown.plain(markdown.section(lineage, "2.9.7"))

        assert "中央控制器" in block
        assert "Agent Initiative" in block, (
            "§2.9.7 必须写明 Agent Initiative 不经过 Front Agent"
        )

    def test_four_compatibility_requirements(self, lineage):
        """§2.9.8 必须列出 4 条兼容性要求（表格 4 行）。"""
        block = markdown.section(lineage, "2.9.8")
        rows = markdown.table_rows(block, "要求")

        assert len(rows) == 4, (
            f"§2.9.8 的兼容性要求表有 {len(rows)} 行，应该恰好 4 行"
        )
        joined = " ".join(rows)
        for banned in ("HumanAgent", "HumanTask", "Front-created Task"):
            assert banned in joined, (
                f"§2.9.8 必须明确禁止：{banned}"
            )

    def test_demand_stays_open_design(self, lineage):
        """Demand 实体仍然是 OPEN DESIGN。"""
        block = markdown.plain(markdown.section(lineage, "2.9.9"))

        assert "OPEN DESIGN" in block
        for forbidden in ("不创建", "Demand 表", "Demand Service"):
            assert forbidden in block, (
                f"§2.9.9 必须明确不创建：{forbidden}"
            )

    def test_v1_acceptance_scenario(self, lineage):
        """§2.9.11 必须写出 V1 核心验收场景。"""
        block = markdown.plain(markdown.section(lineage, "2.9.11"))

        assert "Human 不需要管理 Agent B" in block, (
            "§2.9.11 必须写明「Human 不需要管理 Agent B」"
        )


# ============================================================
# 四、AGENTS.md 的 V1/Future 小节
# ============================================================
class TestAgentsNavigation:
    @pytest.fixture(scope="class")
    def agents(self) -> str:
        return read(AGENTS_FILE)

    def test_v1_section_exists(self, agents):
        assert "## 2.5" in agents, "AGENTS.md 缺少 §2.5"
        assert "Current V1 vs Future" in agents, (
            "§2.5 必须叫「Current V1 vs Future ...」"
        )

    def test_agents_marks_front_agent_future(self, agents):
        block = markdown.section(agents, "2.5")

        assert "Front Agent" in block
        assert "FUTURE" in block, "§2.5 必须把 Front Agent 标为 FUTURE"

    def test_agents_lists_four_wrong_assumptions(self, agents):
        block = markdown.section(agents, "2.5")

        for n in ("①", "②", "③", "④"):
            assert n in block, f"§2.5 缺少错误假设 {n}"

    def test_agents_still_under_line_limit(self, agents):
        """AGENTS.md 必须保持精简（导航文档，不是规范本身）。

        （`test_documentation_consistency.py` 里也有这条，
          这里复述是为了在改动 §2.5 时**就近**得到反馈。）
        """
        lines = len(agents.splitlines())

        assert lines <= 560, (
            f"AGENTS.md 有 {lines} 行，超过 560 —— "
            f"§2.5 加了内容就必须在别处删掉同等篇幅"
        )


# ============================================================
# 五、Scope：不许提前实现
# ============================================================
class TestNoPrematureImplementation:
    FORBIDDEN_MODULES = (
        "front_agent.py", "human_gateway.py", "demand.py",
        "human_interaction.py", "front_desk.py",
    )

    FORBIDDEN_CLASSES = (
        "FrontAgent", "HumanGateway", "DemandService", "DemandRecord",
        "HumanCoordinator", "HumanTaskManager", "HumanAgent",
        "HumanInteractionService",
    )

    def test_no_front_agent_modules(self):
        for layer in ("core", "services", "communication", "storage", "agents"):
            for name in self.FORBIDDEN_MODULES:
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 本轮禁止实现 Front Agent / Demand"
                )

    def test_no_front_agent_classes(self):
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

    def test_no_human_special_case_in_production(self):
        """
        §2.9.8 要求 1：不许把 Agent A 写死成「Human Agent」。

        检查生产代码里没有 `agent_id == "agent_a"` 这类硬编码分支。
        """
        pattern = re.compile(r'agent_id\s*==\s*["\']agent_?a["\']', re.I)

        for layer in ("core", "services", "communication", "storage", "agents"):
            for path in (PROJECT_ROOT / layer).glob("*.py"):
                source = path.read_text(encoding="utf-8")
                assert not pattern.search(source), (
                    f"{path.name} 里硬编码了 `agent_id == 'agent_a'` —— "
                    f"V1 的部署方式不能被写成架构规则"
                )

    def test_no_demand_tables(self, db):
        names = set(db.table_names())

        for forbidden in ("demands", "demand_records", "front_agents"):
            assert forbidden not in names, (
                f"数据库里出现了 {forbidden} 表 —— 本轮禁止实现"
            )
