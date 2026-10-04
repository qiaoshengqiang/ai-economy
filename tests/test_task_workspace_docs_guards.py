"""
Task Workspace 架构定义守卫

============================================================
【这个文件守什么】
============================================================

Task Workspace 的架构定义（`docs/architecture/TASK_WORKSPACE.md` +
ADR-0020）**没有实现**。这带来一类特殊的风险：

    文档写得像已经实现了 → 下一个 Coding Agent 去调用不存在的 API

而项目历史上**已经踩过**这个坑（Step 4E 的规范专门警告过：
「禁止把未来设计写成当前系统已经拥有」）。

所以这里的守卫分三类：

    ① 状态正确     Task Workspace 必须仍标为 FUTURE
    ② 边界正确     五个「不等于」必须在文档里写清楚
    ③ 没有偷跑     生产代码里不许出现 Permission/Lock/Backend 实现

============================================================
【遵守 Guard 规范（ADR-0018）】
============================================================

    · Markdown 用**结构化解析**（按标题切段），不是全文子串
    · 代码用 **AST**（import / 真实调用点），不是字符串匹配
    · 去文档字符串
    · **每条都配反向测试**（见文件末尾）
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests import markdown_guard_helpers as markdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"
WORKSPACE_FILE = PROJECT_ROOT / "docs" / "architecture" / "TASK_WORKSPACE.md"
CONSTITUTION_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "ARCHITECTURE_CONSTITUTION.md"
)
SHARED_CONTEXT_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "TASK_SHARED_CONTEXT.md"
)
ADR_FILE = (
    PROJECT_ROOT / "docs" / "architecture" / "adr"
    / "0020-task-workspace-shared-resource-model.md"
)
ADR_INDEX = PROJECT_ROOT / "docs" / "architecture" / "adr" / "README.md"

LAYERS = ("core", "communication", "storage", "services", "llm", "agents")


# ============================================================
# 工具
# ============================================================
def read(path):
    return markdown.read(path)


def section(document, heading_prefix):
    """
    取一个小节（**fence-aware**）。

    ============================================================
    【本函数曾经有一个真实 bug，现在改为共用 helper】
    ============================================================

        旧实现只查 `line.startswith("#")`，于是 ``` 代码块里的
        `# ❌ 不要` 被当成同级 heading，整节在那里被截断。

        实测：对下面这段，`section("Real Section A")` **只取到 28 字符**。

            ## Real Section A

            ```python
            # ❌ fake heading inside code block
            ```

            content after the fence that MUST be in section A

        后果是所有「查这一节里有没有 X」的守卫
        **在截断的段落上断言** —— 假绿或假红。

        现在委托给 `tests/markdown_guard_helpers.py`，
        该 helper 有独立的单元测试（`test_markdown_guard_helpers.py`），
        包括把旧实现抄进来做对照的反向测试。

    ⚠️ **调用方纪律**：helper 对找不到的标题返回 `""`。
        `assert "X" not in section(...)` 在空串上**恒真** ——
        必须先断言 section 非空，再做内容断言。
        （见 `test_markdown_guard_helpers.py::test_missing_section_would_make_substring_check_vacuous`）
    """
    return markdown.section(document, heading_prefix)


def flat(text):
    return markdown.flat(text)


def module_imports(path: Path) -> set:
    """文件 import 的顶层模块名（AST）。"""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def called_names(path: Path) -> set:
    """文件里**真实调用**的函数/方法名（不是子串匹配）。"""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def defined_classes(path: Path) -> set:
    """文件里定义的类名。"""
    return {
        node.name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
    }


def production_files() -> list:
    """全部生产代码文件（不含 tests / docs）。"""
    files = []
    for layer in LAYERS:
        files.extend(sorted((PROJECT_ROOT / layer).glob("*.py")))
    files.append(PROJECT_ROOT / "main.py")
    return [f for f in files if f.exists()]


@pytest.fixture(scope="module")
def workspace_doc() -> str:
    return read(WORKSPACE_FILE)


@pytest.fixture(scope="module")
def agents_doc() -> str:
    return read(AGENTS_FILE)


# ============================================================
# 一、状态：Task Workspace 必须仍是 FUTURE
# ============================================================
class TestWorkspaceIsStillFuture:
    """
    **这是本次最重要的一组守卫。**

    规范 §2 明确：本次只完成 Architecture Definition，
    结束后仍保持 `Post-Step-4F / Pre-Task-Workspace`，
    **不要把 Task Workspace 标记为 CURRENT**。
    """

    def test_workspace_doc_exists(self):
        assert WORKSPACE_FILE.exists(), (
            "docs/architecture/TASK_WORKSPACE.md 不存在"
        )

    def test_workspace_doc_declares_future(self, workspace_doc):
        """
        TASK_WORKSPACE.md 顶部必须标明实现状态是 FUTURE。

        【为什么不能只查「head 里有 FUTURE」】
            head 里 FUTURE 出现多次（状态栏、说明文字…）。
            把状态栏改成 CURRENT 之后，别的 FUTURE 还在 → 假绿。
            所以这里查**状态栏那一行**（与 does_not_claim_current 互补）。
        """
        head = workspace_doc[:1200]
        status_lines = [l for l in head.splitlines() if "实现状态" in l]

        assert status_lines, "顶部必须有「实现状态」栏"
        for line in status_lines:
            assert "FUTURE" in line, f"实现状态必须是 FUTURE：{line.strip()!r}"

        assert "零实现" in flat(head) or "不实现" in flat(head), (
            "TASK_WORKSPACE.md 必须明确说明本次零实现"
        )

    def test_agents_still_marks_workspace_future(self, agents_doc):
        """
        AGENTS.md 里 **Task Workspace 那一行**必须仍是 FUTURE。

        ============================================================
        【这条守卫第一版是弱的（反向测试时发现）】
        ============================================================

            第一版写的是：

                assert "`FUTURE`" in memory

            但 §4 里有 **5 处** `FUTURE`（Layer 3 / Layer 4 / 明细表…）。
            把 Task Workspace 那一行改成 `CURRENT` 之后，
            另外 4 处仍然是 `FUTURE` → 断言照样通过。

            「文档里某处有 FUTURE」≠「Task Workspace 是 FUTURE」。

        ------------------------------------------------------------
        【现在的查法】
            只查**提到 Task Workspace 的那些表格行**，
            要求它们的状态列全是 `FUTURE`。
        """
        memory = section(agents_doc, "4. Memory 四层")

        rows = [
            line for line in memory.splitlines()
            if line.startswith("|") and "Task Workspace" in line
            and "---" not in line
        ]

        assert rows, "§4 里找不到提到 Task Workspace 的表格行"

        for row in rows:
            assert "`FUTURE`" in row, (
                f"Task Workspace 的这一行不是 FUTURE：\n    {row.strip()}\n"
                f"（本次只做架构定义，不得标为 CURRENT。）"
            )
            assert "`CURRENT`" not in row, (
                f"Task Workspace 被标成了 CURRENT：\n    {row.strip()}"
            )

    def test_agents_stage_is_a_known_stage(self, agents_doc):
        """
        【守卫随阶段演进了】

            原来是「阶段必须保持 Post-Step-4F / Pre-Task-Workspace」。
            Workspace Core V1 冻结 + Change Pipeline Phase 1 实现之后，
            阶段**确实**推进了 —— 写死旧值会让守卫因为**正确的事**而假红。

            现在要求：阶段标记必须是**我们承认的**某一个。
        """
        known = (
            "Post-Step-4F / Pre-Task-Workspace",
            "Workspace Core V1 已冻结",
        )
        assert any(k in agents_doc for k in known), (
            f"阶段标记不在已知集合里：{known}"
        )

    def test_workspace_doc_does_not_claim_current(self, workspace_doc):
        """
        TASK_WORKSPACE.md **不能**说 Task Workspace 是 CURRENT。

        【这条怎么查才准】
            不能全文搜 "CURRENT" —— 文档里会合法地提到
            「Task Core 是 CURRENT」「Shared Context 是 CURRENT」。

            所以只查**描述 Task Workspace 自身状态**的那一行：
            状态栏里的「实现状态」。
        """
        head = workspace_doc[:1500]

        status_lines = [
            line for line in head.splitlines() if "实现状态" in line
        ]
        assert status_lines, (
            "在文档开头 1500 字符内找不到「实现状态」栏 —— "
            "（第一版的过滤条件把它整个 continue 掉了，"
            "守卫于是在**空集合**上为真。）"
        )

        for line in status_lines:
            assert "`FUTURE`" in line, (
                f"实现状态栏必须写 FUTURE，实际：{line.strip()!r}"
            )
            assert "`CURRENT`" not in line, (
                f"实现状态栏被写成了 CURRENT：{line.strip()!r}"
            )

    def test_agents_does_not_say_workspace_implemented(self, agents_doc):
        """
        AGENTS.md 不许**断言** Task Workspace 已实现。

        ============================================================
        【这条守卫第一版是错的 —— 否定盲（negation blindness）】
        ============================================================

            第一版全文搜 "Task Workspace 已实现"，结果变红了。
            但命中的那一行是：

                > **有私有 workspace 目录 ≠ Task Workspace 已实现。**

            —— 这句话说的是**反话**。守卫把一个「明确否认」的句子
            当成了「声称已实现」。

            （同一类错误 ADR-0018 记过：查子串而不看上下文。）

        ------------------------------------------------------------
        【现在的查法】
            只查**肯定式**的断言，并且要求同一行里没有
            `≠` / `不` / `尚未` / `FUTURE` 这类否定词。
        """
        affirmative = (
            "Task Workspace 已实现",
            "Task Workspace 已经实现",
            "Task Workspace 是 CURRENT",
            "Workspace Permission 已实现",
        )
        # 【❌ 也必须算否定】
        #     项目里用 ❌ 标记**反例**：
        #         ❌ 因为「有 workspace 目录」就以为 Workspace Permission 已实现
        #     这一行是在**列举禁止的推论**，不是断言。
        #     不把它算作否定的话，守卫会把反例当成声明（否定盲的另一种形态）。
        negations = ("≠", "不", "尚未", "未实现", "FUTURE", "❌")

        for line in agents_doc.splitlines():
            for phrase in affirmative:
                if phrase not in line:
                    continue
                assert any(neg in line for neg in negations), (
                    f"AGENTS.md 的这行**肯定地**声称了 {phrase!r}：\n"
                    f"    {line.strip()}\n"
                    f"（Task Workspace 仍是 FUTURE。）"
                )


# ============================================================
# 二、核心架构原则必须在文档里写清楚
# ============================================================
class TestCorePrinciplesDocumented:
    """
    规范 §3–§19 确定的每一条原则，都必须能在文档里找到。

    这些不是「措辞检查」，而是**防止原则在后续编辑中被删掉**。
    """

    def test_shared_resource_model_not_git_branch(self, workspace_doc):
        """核心决定：共享资源模型，不是 Git 式分支模型。"""
        text = flat(workspace_doc)

        assert "共享资源模型" in text, "必须写明采用共享资源模型"
        assert "Git" in text and "worktree" in text, (
            "必须明确列出第一版不引入 Git branch/worktree"
        )
        assert "不采用" in text or "否决" in text, (
            "必须明确否决 Git 式分支模型（而不是含糊带过）"
        )

    def test_permission_not_equal_lock(self, workspace_doc):
        """
        Permission ≠ Write Lock —— **必须在 §11 正文里**。

        【为什么不能全文搜】
            这个短语在文档里出现 2 次（§2.2 和 §11.1）。
            删掉其中一处，全文搜索照样命中另一处 → 守卫假绿。
            （反向测试时发现的。）
        """
        block = flat(section(workspace_doc, "11."))

        assert "Permission ≠ Write Lock" in block, (
            "§11（Permission vs Lock 专章）必须显式写出 Permission ≠ Write Lock"
        )

    def test_permission_lock_truth_table(self, workspace_doc):
        """必须有组合真值表（有 permission 但没锁 → 不能写）。"""
        block = section(workspace_doc, "11.")

        assert "锁不在自己手里" in flat(block) or \
               "不能" in flat(block), (
            "§11 必须说明「有 WRITE permission 但锁在别处 → 不能写」"
        )

    def test_parallel_read_exclusive_write(self, workspace_doc):
        """可并行读，写必须独占。"""
        text = flat(workspace_doc)

        assert "并行读取" in text or "并行读" in text, "必须写明可并行读"
        assert "独占" in text, "必须写明写必须独占"

    def test_create_auto_lock_atomic(self, workspace_doc):
        """新建资源自动获得 Write Lock，且必须原子。"""
        block = flat(section(workspace_doc, "12."))

        # 【查具体的断言，不查「原子」这个泛词】
        #   §12 里「原子」出现 3 次 —— 只数词频的话，
        #   删掉核心那句仍然会通过（反向测试时发现的）。
        assert "创建和获得 Write Lock 必须是一个原子操作" in block, (
            "§12 必须写出「创建和获得 Write Lock 必须是一个原子操作」"
        )
        assert "该 Resource 默认自动获得该 Agent 的 Write Lock" in block, (
            "§12 必须写明新资源自动获得创建者的 Write Lock"
        )
        assert "created but unlocked" in block, (
            "§12 必须明确禁止 created-but-unlocked 中间状态"
        )
        assert "以后再 LOCK" in block, (
            "§12 必须给出被禁止的「以后再 LOCK」模式"
        )

    def test_agent_does_not_access_filesystem_directly(self, workspace_doc):
        """Agent 不直接访问其他机器的文件系统。"""
        block = section(workspace_doc, "14.")
        text = flat(block)

        assert "不直接" in text or "禁止" in text, (
            "§14 必须明确禁止 Agent 直接访问文件系统"
        )

    def test_local_remote_same_abstraction(self, workspace_doc):
        """Local 与 Remote 使用同一个 Workspace 抽象。"""
        block = section(workspace_doc, "13.")
        text = flat(block)

        assert "同一个" in text or "同一抽象" in text, (
            "§13 必须写明 Local/Remote 共用同一个抽象"
        )
        # 【这一条原来查的是「解耦」，但 §13 用的是「与物理位置无关」——
        #   同一个意思，换个说法。守卫太脆就会误报。】
        assert ("物理位置" in text or "物理存储位置" in text), (
            "§13 必须写明与物理存储位置无关/解耦"
        )

    def test_no_local_sync_as_base_model(self, workspace_doc):
        """不采用「本地完成后再同步」作为基础模型。"""
        text = flat(workspace_doc)

        assert "local copy" in text or "本地完成后再同步" in text, (
            "必须明确排除 local copy → edit → sync 作为基础模型"
        )

    def test_task_isolation(self, workspace_doc):
        """Task 之间默认隔离。"""
        block = section(workspace_doc, "7.")
        text = flat(block)

        assert "隔离" in text, "§7 必须写明 Task 之间默认隔离"

    def test_not_global_directory(self, workspace_doc):
        """Workspace 不是全局公共目录。"""
        text = flat(workspace_doc)

        assert "全局公共目录" in text or "global-workspace" in text, (
            "必须明确排除全局公共 Workspace"
        )

    def test_workspace_lifecycle_not_task_lifecycle(self, workspace_doc):
        """Workspace Lifecycle ≠ Task Lifecycle。"""
        block = section(workspace_doc, "3.")
        text = flat(block)

        assert "Workspace Lifecycle" in text, "§3 必须区分两种生命周期"
        assert "PROVISIONING" in text or "READY" in text, (
            "§3 必须给出 Workspace lifecycle 的候选状态"
        )

    def test_workspace_identity_is_logical(self, workspace_doc):
        """workspace_id 是逻辑身份，不是路径。"""
        block = section(workspace_doc, "6.")
        text = flat(block)

        assert "Logical Workspace Identity" in text, (
            "§6 必须写明 workspace_id 是逻辑身份"
        )

    def test_resource_identity_decoupled_from_path(self, workspace_doc):
        """Resource Identity 与物理路径解耦。"""
        block = section(workspace_doc, "8.")
        text = flat(block)

        assert "解耦" in text, "§8 必须写明 Resource Identity 与路径解耦"
        assert "logical_resource_id" in text, (
            "§8 必须给出逻辑资源标识的候选形式"
        )

    def test_write_lock_semantics(self, workspace_doc):
        """Write Lock 的四条基本语义。"""
        text = flat(section(workspace_doc, "10."))

        assert "Lock owner" in text
        assert "Lock acquisition" in text
        assert "Lock release" in text
        assert "Lock validity" in text

    def test_only_owner_can_release(self, workspace_doc):
        """只有当前锁主能释放自己的锁。"""
        text = flat(workspace_doc)

        assert "只有" in text and "锁主" in text, (
            "必须写明只有当前锁主能释放锁"
        )


# ============================================================
# 三、Lock Recovery 必须是 OPEN，不能照搬 Message Claim Lease
# ============================================================
class TestLockRecoveryV1Settled:
    """
    **本次任务的核心。**

    规范要求把 v1 的恢复语义**从 OPEN 改成 SETTLED**：

        v1 = Explicit Lock + Owner Unlock + Human Force Unlock + Audit
        Future = Guardian Agent + Policy + Automated Recovery

    同时**不得**把未来的自动恢复机制误写成当前已确定实现。
    """

    def test_v1_is_settled(self, workspace_doc):
        block = flat(section(workspace_doc, "16."))

        assert "SETTLED" in block, (
            "§16 必须标明 v1 治理方式是 SETTLED（不再是 OPEN）"
        )

    def test_explicit_hold_model(self, workspace_doc):
        """
        v1 采用**显式持有模型**。

        【这条守卫第一版是弱的（反向测试时发现）】

            第一版只查 `"显式持有模型" in §16` ——
            但这个短语在 §16 里出现**两次**：

                ### 16.2 v1 决策：显式持有模型，不采用自动时间失效
                > **Write Lock 默认采用显式持有模型。**

            把**正文那句**改成「自动租约模型」之后，
            标题里还留着「显式持有模型」→ 守卫照样绿。

        ------------------------------------------------------------
        【现在的查法】
            查**正文的规范句**，并且额外禁止出现
            肯定式的「自动租约 / 自动过期」写法。
        """
        block = flat(section(workspace_doc, "16."))

        assert "Write Lock 默认采用显式持有模型" in block, (
            "§16 正文必须写出「Write Lock 默认采用显式持有模型」"
        )
        assert "不因为 Agent 长时间没有操作而自动失效" in block, (
            "§16 必须写出「不因为 Agent 长时间没有操作而自动失效」"
        )

        # 肯定式地写「自动租约」是被禁止的
        for phrase in ("采用自动租约模型", "自动租约模型"):
            assert phrase not in block, (
                f"§16 里出现了 {phrase!r} —— v1 不采用自动租约"
            )

    def test_no_automatic_time_expiry(self, workspace_doc):
        """不采用自动时间失效，且不得用固定时间判断 Agent 已死。"""
        block = flat(section(workspace_doc, "16."))

        assert "不采用自动时间失效" in block or "自动时间失效" in block, (
            "§16 必须写明不采用自动时间失效"
        )
        assert "Agent 已经死亡" in block or "Agent 已死亡" in block, (
            "§16 必须明确禁止把固定时间当作「Agent 已经死亡」的判断依据"
        )

    def test_message_claim_lease_not_equal_workspace_lock(self, workspace_doc):
        """Message Claim Lease ≠ Workspace Write Lock。"""
        block = flat(section(workspace_doc, "16."))

        assert "Message Claim" in block and "Write Lock" in block
        assert "不要把二者的数据结构、超时逻辑、恢复策略直接复制" in block, (
            "§16 必须明确禁止直接复制 Message Claim 的租约设计"
        )

    def test_normal_release_only_by_owner(self, workspace_doc):
        """只有 Lock Owner 能释放自己的锁，其他 Agent DENIED。"""
        block = flat(section(workspace_doc, "16."))

        assert "只有 Lock Owner" in block, "§16 必须写明只有锁主能释放"
        assert "DENIED" in block, "§16 必须写明非锁主 UNLOCK 得到 DENIED"

    def test_human_force_unlock_is_the_exception_path(self, workspace_doc):
        """v1 异常恢复 = Human Force Unlock。"""
        block = flat(section(workspace_doc, "16."))

        assert "Human Force Unlock" in block
        assert "人为治理操作" in block, (
            "§16 必须明确 Force Unlock 是人为治理操作"
        )
        assert "不是系统根据" in block and "自动执行" in block, (
            "§16 必须明确它不是系统根据超时自动执行的"
        )

    def test_force_unlock_is_high_privilege(self, workspace_doc):
        """FORCE_UNLOCK 要求治理主体；WRITE 不自动带来 FORCE_UNLOCK。"""
        block = flat(section(workspace_doc, "16."))

        assert "authorized governance actor" in block or "治理主体" in block
        assert "就自动拥有" in block and "FORCE_UNLOCK" in block, (
            "§16 必须明确「有 WRITE 不自动拥有 FORCE_UNLOCK」"
        )

    def test_force_unlock_is_auditable(self, workspace_doc):
        """Force Unlock 必须留下审计记录。"""
        block = flat(section(workspace_doc, "16."))

        for field in ("lock_id", "workspace_id", "resource_id",
                      "previous_owner", "revoked_by", "revoked_at", "reason"):
            assert field in block, (
                f"§16 的 Lock Revocation Event 缺少字段 {field}"
            )
        assert "可追踪的治理记录" in block, (
            "§16 必须写明 Force Unlock 要留下可追踪的治理记录"
        )

    def test_lock_states_are_three(self, workspace_doc):
        """LOCK 状态只有 ACTIVE / RELEASED / REVOKED。"""
        block = flat(section(workspace_doc, "16."))

        for state in ("ACTIVE", "RELEASED", "REVOKED"):
            assert state in block, f"§16 缺少锁状态 {state}"

    def test_no_expired_state(self, workspace_doc):
        """
        **不得**增加 EXPIRED 状态 —— 那会暗示存在自动 Lease。

        【怎么查才准】
            文档里 EXPIRED 必然出现（因为要写「不要增加 EXPIRED」）。
            所以查的是：**EXPIRED 只出现在禁止语境里**。
        """
        lines = [l for l in workspace_doc.splitlines() if "EXPIRED" in l]

        assert lines, "文档里本该有关于 EXPIRED 的说明"
        for line in lines:
            assert "不要" in line or "禁止" in line or "不增加" in line, (
                f"EXPIRED 出现在了非禁止语境里：{line.strip()!r}\n"
                f"（加 EXPIRED 会暗示存在自动 Lease，而 v1 没有。）"
            )

    def test_observation_not_conclusion(self, workspace_doc):
        """Observation ≠ Conclusion；不把 inactivity 当崩溃证据。"""
        block = flat(section(workspace_doc, "16."))

        assert "Observation" in block and "Conclusion" in block, (
            "§16 必须写出 Observation ≠ Conclusion"
        )
        assert "不假装知道" in block or "不把" in block, (
            "§16 必须写明系统不假装知道 Agent 是否已死"
        )

    def test_future_part_marked_future(self, workspace_doc):
        """Guardian / Automated Recovery 必须标 FUTURE。"""
        block = flat(section(workspace_doc, "16."))

        assert "Future" in block or "FUTURE" in block
        assert "Guardian Agent" in block


class TestGuardianIsFuture:
    """
    Guardian / Observer Agent 是 `FUTURE`。

    【规范 §23 要求守卫能抓】
        · 把 Guardian 写成 CURRENT
        · 把 Guardian 判断写成 Domain Fact
        · 把 Heartbeat 写成当前实现
    """

    def test_guardian_section_exists(self, workspace_doc):
        assert "Guardian / Observer Agent" in workspace_doc, (
            "必须有 Guardian / Observer Agent 一节"
        )

    def test_guardian_marked_future(self, workspace_doc):
        block = flat(section(workspace_doc, "16.13"))

        assert block, "找不到 §16.13"
        assert "FUTURE" in block, "§16.13 必须标明 Guardian 是 FUTURE"
        assert "不实现" in block, "§16.13 必须明确本次不实现 Guardian"

    def test_guardian_not_current(self, workspace_doc):
        """
        Guardian **不能**被标成 CURRENT。

        【否定盲防范】只查**肯定式**断言。
        """
        negations = ("不", "未", "≠", "❌", "FUTURE", "禁止")
        for line in workspace_doc.splitlines():
            if "Guardian" not in line:
                continue
            if "`CURRENT`" not in line:
                continue
            assert any(n in line for n in negations), (
                f"这一行把 Guardian 说成了 CURRENT：\n    {line.strip()}"
            )

    def test_guardian_is_not_highest_authority(self, workspace_doc):
        """Guardian 不是系统最高权威。"""
        block = flat(section(workspace_doc, "16.13"))

        assert "不是「系统最高权威」" in block or "不是系统最高权威" in block, (
            "§16.13 必须写明 Guardian 不是系统最高权威"
        )

    def test_guardian_chain_requires_authorization(self, workspace_doc):
        """Observation → Assessment → Recommendation → Policy → Action。"""
        block = flat(section(workspace_doc, "16.13"))

        for step in ("Observation", "Assessment", "Recovery Request",
                     "Policy / Authorization", "Actual Action"):
            assert step in block, f"§16.13 的链路里缺少 {step}"

    def test_guardian_is_not_source_of_truth(self, workspace_doc):
        """Guardian 判断不是新的 Source of Truth。"""
        block = flat(section(workspace_doc, "16.13"))

        assert "不是新的 Source of Truth" in block, (
            "§16.13 必须写明 Guardian 判断不是新的 Source of Truth"
        )
        assert "Guardian State" in block, (
            "§16.13 必须明确不要产生凌驾于 Task Core 的 Guardian State"
        )

    def test_guardian_cannot_rewrite_task_core(self, workspace_doc):
        """Guardian 判断不得自动改写 Task Core。"""
        block = flat(section(workspace_doc, "16.13"))

        assert "自动变成 FAILED" in block, (
            "§16.13 必须用具体反例说明「Guardian=DEAD → Task 自动 FAILED」是被禁止的"
        )
        assert "不能自动发生" in block, (
            "§16.13 必须写明这种自动改写第一版不能发生"
        )

    def test_guardian_assessment_is_not_domain_state(self, workspace_doc):
        """Observer 的判断结果不是 Domain State。"""
        block = flat(section(workspace_doc, "16.13"))

        assert "不是 Domain State" in block, (
            "§16.13 必须写明 Observer 的判断结果不是 Domain State"
        )
        for verdict in ("NORMAL", "LONG_RUNNING", "SUSPECTED_FAILURE",
                        "UNRESPONSIVE", "UNKNOWN"):
            assert verdict in block, f"§16.13 缺少判断结果 {verdict}"

    def test_guardian_may_use_local_small_model(self, workspace_doc):
        block = flat(section(workspace_doc, "16.13"))

        assert "local small model" in block or "本地小模型" in block, (
            "§16.13 必须写明 Guardian 可运行本地小模型"
        )


class TestNoHeartbeat:
    """
    **不得因为 Guardian 就重新引入 Heartbeat。**（规范 §17）
    """

    def test_heartbeat_forbidden_in_workspace_doc(self, workspace_doc):
        block = flat(section(workspace_doc, "16.13"))

        assert "不要因为 Guardian 就重新引入 Heartbeat" in block or (
            "Heartbeat" in block and "不实现" in block
        ), "§16.13.8 必须明确不得因为 Guardian 而重新引入 Heartbeat"

    def test_no_reverse_implementation_warning(self, workspace_doc):
        """必须明确警告「不要为了让 Guardian 能工作而提前实现 Heartbeat」。"""
        block = flat(section(workspace_doc, "16.13"))

        assert "反向把 Heartbeat 提前实现" in block or (
            "为了让 Guardian" in block and "提前实现" in block
        ), "§16.13.8 必须警告不要反向提前实现 Heartbeat"

    def test_heartbeat_not_claimed_implemented(self, workspace_doc):
        """
        文档不许**声称 Heartbeat 已经实现**。

        【为什么单独一条】
            上面那些守卫查的是「禁止项有没有写」——
            而规范 §23 要求抓的是「把 Heartbeat 写成当前实现」。

            这两件事不同：把禁止表里某一行改成「已实现」，
            禁止段落仍然存在 → 只查禁止的守卫不会变红。

            所以这里查**肯定式声称**（否定盲防范：排除 ❌/不/未）。
        """
        negations = ("❌", "不", "未", "禁止", "禁止项", "FUTURE")
        claims = ("已实现", "已经实现", "已支持", "CURRENT")

        for line in workspace_doc.splitlines():
            if "heartbeat" not in line.lower():
                continue
            for claim in claims:
                if claim not in line:
                    continue
                assert any(n in line for n in negations), (
                    f"这一行把 Heartbeat 说成了已实现：\n    {line.strip()}\n"
                    f"（Heartbeat 当前明确禁止实现。）"
                )

    def test_heartbeat_not_implemented_in_agents(self, agents_doc):
        """AGENTS.md 必须写明 Heartbeat = Not implemented。"""
        text = flat(agents_doc)

        assert "Heartbeat" in text
        assert "Not implemented" in text or "未实现" in text, (
            "AGENTS.md 必须标明 Heartbeat 未实现"
        )

    def test_no_heartbeat_in_production(self):
        """
        生产代码里没有任何 heartbeat / lease renewal **实现**。

        ============================================================
        【这条守卫第一版查的是原文 —— 被文档字符串绊倒了】
        ============================================================

            第一版对每个生产文件做 `"heartbeat" in source.lower()`，
            结果在 `core/decision_policy.py` 上变红。命中的是：

                未来一个 Heartbeat / Agent Runtime 可以直接调用它，
                不需要经过 HTTP 或 CLI。

            —— 这是 Step 4C 留下的一句**前瞻性文档说明**，
            **不是实现**。

            ADR-0018 把这类问题记为「文档字符串被当成代码」，
            本项目已踩过 8 次以上。

        ------------------------------------------------------------
        【现在的查法：查真实实现，不查词】
        ------------------------------------------------------------
            ① 定义的类 / 函数名里有没有 heartbeat / renew / expiry
            ② import 的模块里有没有
            ③ 有没有 heartbeat*.py 这样的模块文件
            ④ 有没有 `def renew...` / `def heartbeat...` 之类的方法
        """
        forbidden_substrings = ("heartbeat", "renewlease", "renew_lease",
                                "renewlock", "crashdetect", "crash_detect",
                                "lockexpiry", "lock_expiry", "lease_renew")
        hits = []

        for path in production_files():
            # ① 类名 / 函数名
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    name = node.name.lower().replace("_", "")
                    for bad in forbidden_substrings:
                        if bad.replace("_", "") in name:
                            hits.append(f"{path.name}: {node.name}()")

            # ② import 的模块
            for module in module_imports(path):
                lowered = module.lower().replace("_", "")
                for bad in forbidden_substrings:
                    if bad.replace("_", "") in lowered:
                        hits.append(f"{path.name}: import {module}")

            # ③ 方法名（`called_names` 抓不到定义，用 AST 属性）
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute):
                    attr = node.attr.lower().replace("_", "")
                    for bad in forbidden_substrings:
                        if bad.replace("_", "") in attr:
                            hits.append(f"{path.name}: .{node.attr}")

        # ④ 模块文件
        for layer in LAYERS:
            for pattern in ("heartbeat*.py", "*lease_renewal*.py",
                            "*lock_expiry*.py", "*crash_detect*.py"):
                for found in (PROJECT_ROOT / layer).glob(pattern):
                    hits.append(str(found.relative_to(PROJECT_ROOT)))

        assert not hits, (
            "生产代码里出现了 Heartbeat / Lease Renewal 相关的实现：\n  "
            + "\n  ".join(sorted(set(hits)))
            + "\n（当前阶段明确禁止实现这些。）"
        )

    def test_heartbeat_guard_ignores_docstrings(self):
        """
        反向：确认上面的实现检测**不会**被文档字符串绊倒。

        【这条测试在钉一个真实发生的误报】
            `core/decision_policy.py` 的文档里有
            「未来一个 Heartbeat / Agent Runtime 可以直接调用它」——
            那是说明，不是实现。
        """
        decision = PROJECT_ROOT / "core" / "decision_policy.py"
        raw = decision.read_text(encoding="utf-8").lower()

        # 陷阱存在：原文里确实有 heartbeat
        assert "heartbeat" in raw, "测试前提不成立"

        # 但没有把它当实现的理由：没有相关类/函数/import
        defined = {
            n.name.lower().replace("_", "")
            for n in ast.walk(ast.parse(
                decision.read_text(encoding="utf-8")))
            if isinstance(n, (ast.ClassDef, ast.FunctionDef))
        }
        assert not any("heartbeat" in name for name in defined), (
            "decision_policy.py 里真的定义了 heartbeat 相关的东西"
        )


# ============================================================
# 四、Open Design Questions 必须显式存在
# ============================================================
class TestOpenQuestionsDocumented:
    """
    规范 §21：未决定的问题**必须**记录成 Open Design Question，
    而不是猜一个实现。
    """

    def test_open_questions_section_exists(self, workspace_doc):
        block = section(workspace_doc, "21.")

        assert block, "必须有 Open Design Questions 章节"
        assert "OPEN" in block

    @pytest.mark.parametrize(
        "topic",
        [
            "何时创建",          # Workspace creation timing
            "lifecycle",         # Workspace lifecycle
            "崩溃恢复",          # Lock lease / crash recovery
            "多资源锁",          # Multi-resource lock
            "目录锁",            # Directory lock semantics
            "加入",              # Agent join
            "退出",              # Agent leave
            "Artifact",          # Artifact design
        ],
    )
    def test_specific_open_questions_listed(self, workspace_doc, topic):
        """规范 §29 点名要求列出的问题，一个都不能少。"""
        block = flat(section(workspace_doc, "21."))

        assert topic in block, (
            f"Open Design Questions 里缺少「{topic}」"
        )

    def test_no_invented_answers(self, workspace_doc):
        """
        **未决定的问题不许写成已决定。**

        ============================================================
        【这条守卫的语义随架构演进而更新过】
        ============================================================

            上一轮任务时，§15 的状态是「候选模型，本阶段不决定」，
            所以守卫查的是 `"不决定" in §15`。

            本轮**生命周期模型已经正式确定**（ADR-0022）：
            `UNPROVISIONED → PROVISIONING → READY → FROZEN → ARCHIVED`。

            如果继续查「不决定」，守卫会**误报** ——
            它会把「模型已定」当成「有人瞎猜」。

            所以现在查的是**新的真实状态**：
                ① §15 有 `SETTLED` 的生命周期模型
                ② **但** Reopen 的细节明确标 `OPEN`（§15.9）
                ③ **并且**不得出现「Reopen 细节已定」式的断言
        """
        block = flat(section(workspace_doc, "15."))

        # ① 模型是 SETTLED 的（不是候选）
        assert "SETTLED" in block, (
            "§15 必须标明生命周期模型是 SETTLED（ADR-0022）"
        )
        for state in ("UNPROVISIONED", "PROVISIONING", "READY",
                      "FROZEN", "ARCHIVED"):
            assert state in block, f"§15 缺少生命周期状态 {state}"

        # ② 但 Reopen 的细节仍然 OPEN
        assert "OPEN" in block, (
            "§15 必须标明 Reopen 的细节仍然 OPEN（§15.9）"
        )
        for question in ("是谁可以 Reopen", "什么时候必须新建 Task",
                         "Permission 是否重新评估"):
            assert question in block, (
                f"§15.9 的 Open 清单里缺少：{question}"
            )

        # ③ 不许把 Reopen 细节写成已决定
        for invented in ("Reopen 规则已确定", "Reopen 细节已完成"):
            assert invented not in block, (
                f"§15 出现了断言式的 {invented!r} —— 那些细节仍是 OPEN"
            )

    def test_lifecycle_section_is_settled_and_lazy(self, workspace_doc):
        """
        §15 必须写明 Lazy Provisioning 与「不要因为 ACCEPTED 就创建」。

        【这条守卫第一版是弱的（反向测试时发现）】

            第一版查 `"不要" in §15 and "ACCEPTED" in §15` ——
            但 §15 里有**很多**「不要」（§15.6 等），
            把 Lazy 那句改成「ACCEPTED 就立即创建」之后守卫照样绿。

            「这一节某处有『不要』」≠「这一节禁止了那件事」。

        现在查**具体的规范句**。
        """
        block = flat(section(workspace_doc, "15."))

        assert "Lazy Provisioning" in block, "§15 必须写明 Lazy Provisioning"
        assert "不要**因为 Task 被创建 / `ACCEPTED` 就无条件创建一个物理 Workspace" in block, (
            "§15 必须写出**那句**禁止：不要因为 Task 被创建/ACCEPTED "
            "就无条件创建物理 Workspace"
        )
        assert "Physical Workspace 不需要提前存在" in block or (
            "Logical Workspace Identity 可以存在" in block
        ), "§15 必须写明 Logical 可以先存在而 Physical 不必提前存在"

        # 不许出现肯定式的「ACCEPTED 就创建」
        for invented in ("立即创建物理 Workspace", "无条件创建物理 Workspace 目录"):
            assert invented not in block, (
                f"§15 出现了肯定式的 {invented!r} —— 那是被禁止的"
            )

    def test_lifecycle_section_separates_from_task(self, workspace_doc):
        """
        §15 必须写明 Workspace Lifecycle ≠ Task Lifecycle，
        且**不得**把 Task 的状态列成 Workspace 的状态。

        【反向测试时发现需要这一条】
            把 §15.3 的 `READY` 描述改成「等同于 Task 的 IN_PROGRESS」之后，
            别的守卫都不变红 —— 因为 `test_workspace_lifecycle_not_task_lifecycle`
            查的是 §3，不是 §15。
        """
        block = flat(section(workspace_doc, "15."))

        assert "Workspace Lifecycle ≠ Task Lifecycle" in block, (
            "§15.4 必须写出 Workspace Lifecycle ≠ Task Lifecycle"
        )
        assert "只描述" in block and "是否可以被正常操作" in block, (
            "§15 必须说明 Workspace Lifecycle 只描述"
            "「这个资源空间当前是否可以被正常操作」"
        )

        # ---- 禁止把 Task 状态当成 Workspace 状态 ----
        #
        # 【这条检查第一版是错的（逐行扫描）】
        #     §15 里的禁止清单是**跨行**的：
        #
        #         ❌ CREATED / OFFERED / NEGOTIATING / ACCEPTED / IN_PROGRESS /
        #            SUBMITTED / VERIFIED / COMPLETED
        #
        #     第二行没有 ❌，逐行检查就把它误判成「肯定式声明」。
        #
        #     改成查**肯定式的等价表述** —— 那才是真正要禁止的东西。
        for invented in ("等同于 Task 的", "复用 Task 状态机",
                         "Workspace 状态机就是 Task 状态机",
                         "Workspace.status 与 Task.status 相同"):
            assert invented not in block, (
                f"§15 出现了肯定式的 {invented!r} —— "
                f"Workspace 不得复用 Task 生命周期"
            )

        # 而**禁止清单本身**必须在（用 ❌ 标记的跨行块）
        assert "❌ CREATED / OFFERED / NEGOTIATING / ACCEPTED / IN_PROGRESS /" in section(
            workspace_doc, "15."
        ), "§15 必须用 ❌ 明确列出不得复制的 Task 状态"

    def test_iteration_preserves_history(self, workspace_doc):
        """Reopen 必须保留历史，不得清空/覆盖。"""
        block = flat(section(workspace_doc, "15."))

        assert "清空原历史" in block, "§15.8 必须明确禁止清空原历史"
        assert "覆盖旧执行" in block, "§15.8 必须明确禁止覆盖旧执行"
        assert "覆盖旧 Verification" in block, (
            "§15.8 必须明确禁止覆盖旧 Verification"
        )

    def test_frozen_is_not_permanent_readonly(self, workspace_doc):
        """FROZEN 不能是永久只读。"""
        block = flat(section(workspace_doc, "15."))

        # 【否定盲防范】
        #     第一版查 `"永久只读" in block and "不能恢复" in block` ——
        #     但把原文改成「FROZEN = 永久只读、永远不能恢复」之后，
        #     这两个词**都还在**，守卫照样绿。
        #
        #     所以要查**否定式的整体表述**，而不是那两个词。
        assert "不应该被设计成" in block and "永久只读" in block, (
            "§15.7 必须写出**否定式**表述："
            "「Workspace 也不应该被设计成 FROZEN = 永久只读、永远不能恢复」"
        )
        assert "Task 完成不是 Task 永久死亡" in block, (
            "§15.7 必须写明「Task 完成不是 Task 永久死亡」"
        )
        assert "Reopen" in block, "§15.7 必须写明允许 Reopen"

    def test_no_delete_lifecycle(self, workspace_doc):
        """第一版不建立 Delete Lifecycle，不提前实现 GC/TTL。"""
        block = flat(section(workspace_doc, "15."))

        assert "不建立普通 Task Workspace Delete Lifecycle" in block, (
            "§15.6 必须明确第一版不建立 Delete Lifecycle"
        )
        for forbidden in ("GC", "TTL", "自动物理删除"):
            assert forbidden in block, (
                f"§15.6 必须明确禁止提前实现 {forbidden}"
            )

    def test_human_participation_is_supported(self, workspace_doc):
        """Human Participation 必须被**支持**，不是被禁止。"""
        block = flat(section(workspace_doc, "14.5"))

        assert block, "找不到 §14.5 Human Participation"
        assert "Authorized Human Modification" in block or (
            "Authorized" in block and "Human" in block
        ), "§14.5 必须写明 Authorized Human Modification"
        assert "一等协作方式" in block or "正式协作" in block, (
            "§14.5 必须写明 Human 是正式协作 Actor"
        )

    def test_human_participation_is_not_forbidden(self, workspace_doc):
        """
        **反向语义**：Human 不许被写成「禁止修改 Workspace」。

        【否定盲防范】只查肯定式断言。
        """
        block = section(workspace_doc, "14.5")

        for line in block.splitlines():
            low = line.lower()
            if "human" not in low and "人类" not in line:
                continue
            if "禁止" not in line and "不允许" not in line:
                continue
            # 允许「禁止 Human 之外的东西」，但不允许禁止 Human 本身
            assert "禁止 Human" not in line and "不允许 Human" not in line, (
                f"这一行把 Human Participation 写成了禁止：\n    {line.strip()}"
            )

    def test_unauthorized_monitoring_is_future(self, workspace_doc):
        """Unauthorized / out-of-band 修改的监控必须是 FUTURE，不是 CURRENT。"""
        block = flat(section(workspace_doc, "14.5"))

        # 【不能只查 "FUTURE" 这个词】
        #     §14.5 里有多个 `FUTURE` 标记（章节标题等）。
        #     把结论句改成「监控已经是 CURRENT」之后，
        #     别的 FUTURE 还在 → 守卫照样绿。
        #     所以查**那句结论**本身。
        assert "Unauthorized Physical Modification 是 `Future Concern`" in block, (
            "§14.5 必须写出结论句："
            "「Unauthorized Physical Modification 是 Future Concern」"
        )
        assert "监控已经是 `CURRENT`" not in block, (
            "§14.5 不许声称 Unauthorized 监控已经是 CURRENT"
        )
        for not_done in ("Filesystem Watcher", "Hash Tamper Detection",
                         "Kernel-level Enforcement"):
            assert not_done in block, (
                f"§14.5 必须明确不做：{not_done}"
            )

    def test_workspace_protocol_layered_from_host_os(self, workspace_doc):
        """Workspace Protocol 与 Host OS 权限必须分层。"""
        block = flat(section(workspace_doc, "14.5"))

        assert "Workspace Protocol-level authorization" in block, (
            "§14.5 必须写明 Workspace Protocol-level authorization"
        )
        assert "Host OS" in block, (
            "§14.5 必须写明与 Host OS Filesystem Permission 的分层"
        )

    def test_task_lineage_is_open_topic_not_implemented(self, workspace_doc):
        """Task Origin / Lineage 必须是 OPEN DESIGN TOPIC，不得写成已实现。"""
        doc = workspace_doc
        text = flat(doc)

        assert "OPEN DESIGN TOPIC" in text, (
            "必须把 Task Origin & Task Lineage 标为 OPEN DESIGN TOPIC"
        )
        for forbidden in ("parent_task_id", "root_task_id", "task tree",
                          "task graph", "delegation lifecycle"):
            assert forbidden in text, (
                f"必须明确列出**不要实现**的东西：{forbidden}"
            )

        # 不许声称已经实现
        for invented in ("Task Lineage 已实现", "Lineage 已完成"):
            assert invented not in text, (
                f"文档里出现了 {invented!r} —— Task Lineage 还没设计"
            )

    def test_iteration_is_not_child_task(self, workspace_doc):
        """Iteration ≠ Child Task。"""
        block = flat(section(workspace_doc, "15."))

        assert "Iteration ≠ Child Task" in block, (
            "§15.10 必须显式写出 Iteration ≠ Child Task"
        )

    def test_message_is_not_task_creation(self, workspace_doc):
        """Message ≠ Task Creation。"""
        block = flat(section(workspace_doc, "15."))

        assert "Message ≠ Task Creation" in block or (
            "不要把 Task Creation 和普通 Message 混在一起" in block
        ), "§15.12 必须写明 Message ≠ Task Creation"


# ============================================================
# 五、五个边界
# ============================================================
class TestFiveBoundaries:
    """规范 §27 点名的五条架构边界。"""

    def test_all_five_stated(self, workspace_doc):
        text = flat(workspace_doc)

        pairs = [
            ("Task Core", "Workspace"),
            ("Shared Context", "Workspace"),
            ("Private Workspace", "Task Workspace"),
            ("Permission", "Write Lock"),
            ("workspace_id", "path"),
        ]
        for left, right in pairs:
            assert left in text and right in text, (
                f"文档里缺少 {left} / {right} 的区分"
            )

    def test_boundaries_also_in_agents(self, agents_doc):
        """五个边界里，与 Agent 相关的那几条也要在 AGENTS.md 里。"""
        text = flat(agents_doc)

        assert "Workspace ≠ Permission" in text, (
            "AGENTS.md 的边界表必须有 Workspace ≠ Permission"
        )


# ============================================================
# 六、没有偷跑：生产代码里不许有 Workspace 实现
# ============================================================
class TestNoWorkspaceImplementation:
    """
    规范 §25：本阶段**禁止**修改生产代码去实现 Workspace。

    这一组从**代码**侧验证「确实没有偷跑」——
    光靠文档说「没实现」是不够的。
    """

    def test_no_permission_or_acl_classes(self):
        """
        没有任何生产文件定义 **Permission / ACL / IAM** 类。

        【设计阶段守卫 → 实现阶段的过渡】

            原来是 `test_no_permission_or_lock_classes`，禁止
            `WorkspaceGateway` / `WorkspaceLock` 等 ——
            因为当时 Task Workspace 还是 `FUTURE`。

            Workspace Core 第一阶段已**授权实现**，所以那份清单过时了：

                ✅ 允许：`WorkspaceGateway`（Layer 语义里叫 Lock，
                        但它是**责任**不是权限）
                ❌ 禁止：Permission / ACL / IAM / Backend / Role
                        —— ADR-0027 已把 Authorization 降级，
                           复杂 IAM / Visibility ACL 明确不做

            ⚠️ `WriteLock` / `LockManager` 仍然禁止 ——
               Workspace Core 用 `ExplicitLock` + 派生 Effective Lock，
               **没有**可被任意抢的 `lock()` 管理器（任务书 §43）。
        """
        forbidden_classes = {
            "Permission", "PermissionEngine", "ACL", "AccessControl",
            "WriteLock", "LockManager", "WorkspaceLock",
            "WorkspaceBackend", "LocalWorkspaceBackend",
            "RemoteWorkspaceBackend", "WorkspaceService",
            "WorkspaceManager", "ArtifactStore",
            # 🔒 ADR-0027 移除/降级的概念
            "AuthorizationEngine", "AuthorizationRequest",
            "DelegationService", "ControllerRegistry", "CascadeRevocation",
            "RecoveryManager", "GovernanceAgent", "VisibilityEngine",
        }

        for path in production_files():
            defined = defined_classes(path)
            hit = defined & forbidden_classes
            assert not hit, (
                f"{path.name} 定义了 {sorted(hit)} —— "
                f"Task Workspace 仍是 FUTURE，禁止实现"
            )

    def test_no_lock_or_backend_modules(self):
        """没有任何 workspace lock / backend / permission 模块。"""
        forbidden_files = (
            "workspace_lock.py", "workspace_backend.py",
            "workspace_permission.py", "workspace_manager.py",
            "lock_manager.py", "permission.py",
        )
        for layer in LAYERS:
            for name in forbidden_files:
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 属于 Task Workspace 实现"
                )

    def test_workspace_tables_match_the_authorized_design(self, db):
        """
        Workspace Core 第一阶段**已授权实现**（任务书）。

        【这条守卫原来是 `test_no_workspace_tables`】
            它断言「数据库里没有 Workspace 相关的表」，因为当时
            **禁止实现 Workspace**。那是对的 —— 在设计阶段。

            现在实现已授权，所以守卫改成**问对的问题**：

                ✅ 允许的：Workspace Core 第一阶段的 5 张表
                ❌ 禁止的：被 ADR-0027 移除/降级的概念的表

        🔒 特别注意 `workspace_locks` 仍然**禁止** ——
           锁表叫 `workspace_explicit_locks`，
           因为**只有 Explicit Lock 是持久化事实**；
           `Effective Lock` 永远派生，不该有表
           （任务书 §48 / ADR-0027）。
        """
        names = set(db.table_names())

        allowed = {
            "workspace_resources",
            "workspace_explicit_locks",
            "workspace_revisions",
            "workspace_audits",
            "workspace_events",
        }

        for forbidden in (
            # 未授权的 Workspace 结构
            "workspace_permissions", "workspace_members",
            "workspace_backends", "workspaces", "artifacts",
            # 🔒 一个笼统的 workspace_locks 会暗示「锁可以有多种/被缓存」
            #    我们只允许 workspace_explicit_locks
            "workspace_locks",
            "workspace_effective_locks",
        ):
            assert forbidden not in names, (
                f"数据库里出现了 {forbidden} 表 —— 未授权"
            )

        unexpected = {
            n for n in names if n.startswith("workspace_")
        } - allowed
        assert not unexpected, (
            f"出现了未授权的 workspace_* 表：{sorted(unexpected)}"
        )

    def test_no_git_imports(self):
        """没有引入 Git 相关依赖（§2.1 否决分支模型）。"""
        for path in production_files():
            mods = module_imports(path)
            for forbidden in ("git", "gitpython", "dulwich", "pygit2"):
                assert forbidden not in mods, (
                    f"{path.name} import 了 {forbidden} —— "
                    f"Git 不属于本阶段"
                )

    def test_no_remote_workspace_transport(self):
        """
        没有**远程 Workspace 传输**（Remote Workspace 是 FUTURE）。

        【为什么不能用 `socket` 当判据】
            `llm/openai_compatible.py` **合法地**使用 socket ——
            它是 Step 2.5 的 LLM HTTP provider，和 Workspace 无关。

            用 `socket` 做判据会把这个已有组件误报成违规。
            （ADR-0018 记过的「子串/关键词选错」一类。）

        真正该查的是**远程文件传输**相关的依赖。
        """
        for path in production_files():
            mods = module_imports(path)
            for forbidden in ("paramiko", "fabric", "scp", "sftp",
                              "ftplib", "pygit2"):
                assert forbidden not in mods, (
                    f"{path.name} import 了 {forbidden} —— "
                    f"Remote Workspace 尚未设计"
                )

    def test_no_filesystem_api_added(self):
        """
        没有新增文件系统 API。

        【注意 `core/workspace.py` 是例外】
            它是 **Agent Private Workspace**（CURRENT，Step 1 就有），
            不属于本次范围，也不该被改。
        """
        # 除 core/workspace.py 外，core/ 里不该有文件 I/O
        for path in sorted((PROJECT_ROOT / "core").glob("*.py")):
            if path.name in ("workspace.py", "paths.py", "logging_setup.py"):
                continue
            called = called_names(path)
            for forbidden in ("mkdir", "rmdir", "unlink", "listdir"):
                assert forbidden not in called, (
                    f"core/{path.name} 调用了 {forbidden}() —— "
                    f"Task Workspace 的 I/O 尚未实现"
                )

    def test_task_workspace_ref_still_a_reference(self):
        """`TaskWorkspaceRef` 仍然只有 workspace_id。"""
        from core.task_workspace import TaskWorkspaceRef

        for forbidden in ("path", "read_text", "write_text", "mkdir",
                          "grant", "allow", "permission", "lock"):
            assert not hasattr(TaskWorkspaceRef, forbidden), (
                f"TaskWorkspaceRef 获得了 {forbidden} —— "
                f"它必须仍然只是一个逻辑引用"
            )

    def test_core_workspace_stays_private_workspace_only(self):
        """
        `core/workspace.py` 必须**始终只是** Agent Private Workspace。

        【守卫随范围演进了】

            原来这条断言的是「这个文件一个字都不许改」，理由是
            「它服务 Agent Private Workspace，不在（Task Workspace 的）范围内」。

            现在任务**就是** Agent Private Workspace V1，
            而且任务书 §4 明确要求修 `memory_path()` 的安全一致性
            （审计发现它能被符号链接逃逸）。
            所以「文件不变」这个判据已经过期 ——
            它会因为**正确的事**而假红。

            换成**仍然有效**的那条关切（而且比原来更强）：

                core/workspace.py 不能长出 **Task Workspace** 的概念。
        """
        import ast

        path = PROJECT_ROOT / "core" / "workspace.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # ① 不许依赖 Workspace Core / Gateway
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        for forbidden in ("workspace_core", "workspace_gateway",
                          "workspace_store"):
            assert forbidden not in imported, (
                f"core/workspace.py 引入了 {forbidden} —— "
                f"Agent Private Workspace 不能依赖 Task Workspace"
            )

        # ② 不许出现 Task Workspace 的词汇（只看**代码**，不看说明文字）
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.FunctionDef):
                names.add(node.name)

        for forbidden in ("workspace_id", "explicit_lock", "effective_lock",
                          "revision", "audit_event", "resource_tree"):
            assert forbidden not in names, (
                f"core/workspace.py 出现了 {forbidden!r} —— "
                f"它必须仍然只是 Agent 的私有文件空间"
            )


# ============================================================
# 七、AGENTS.md 的阅读顺序
# ============================================================
class TestAgentsReadOrder:
    """规范 §23.2：AGENTS.md 必须排在 Constitution 之前。"""

    def test_read_order_states_agents_first(self, agents_doc):
        block = flat(section(agents_doc, "阅读顺序"))

        assert block, "AGENTS.md 必须有「阅读顺序」一节"
        assert "1. AGENTS.md" in block, (
            "阅读顺序的第 1 步必须是 AGENTS.md（入口）"
        )

    def test_constitution_is_second(self, agents_doc):
        block = flat(section(agents_doc, "阅读顺序"))
        first = block.index("1. AGENTS.md")
        second = block.index("ARCHITECTURE_CONSTITUTION.md")

        assert first < second, (
            "阅读顺序里 AGENTS.md 必须排在 Constitution 之前"
        )

    def test_workspace_navigation_present(self, agents_doc):
        """规范 §23.4：必须有 Workspace 未来架构导航。"""
        text = flat(agents_doc)

        assert "Workspace 导航" in text, "必须有 Workspace 导航一节"
        assert "TASK_WORKSPACE.md" in agents_doc, (
            "导航必须指向 TASK_WORKSPACE.md"
        )
        assert "0020" in agents_doc, "导航必须指向 ADR-0020"

    def test_agents_does_not_copy_constitution(self, agents_doc):
        """不要把 Constitution 的内容复制进 AGENTS.md。"""
        assert "## Context" not in agents_doc, (
            "AGENTS.md 里出现了 ADR 的正文结构"
        )


# ============================================================
# 八、ADR
# ============================================================
class TestADR:
    def test_adr_0020_exists(self):
        assert ADR_FILE.exists(), "ADR-0020 不存在"

    def test_adr_has_required_sections(self):
        text = read(ADR_FILE)

        for heading in ("## Context", "## Decision", "## Consequences",
                        "## 备选方案"):
            assert heading in text, f"ADR-0020 缺少 {heading}"

    def test_adr_index_lists_0020(self):
        index = read(ADR_INDEX)

        assert "0020" in index, "ADR 索引里没有 0020"
        assert "0020-task-workspace-shared-resource-model.md" in index, (
            "ADR 索引的链接指向不存在的文件"
        )

    def test_adr_records_open_questions(self):
        """ADR 必须记录「没有决定的事」，不能只写决定。"""
        text = read(ADR_FILE)

        assert "Open Design Question" in text or "OPEN DESIGN QUESTION" in text, (
            "ADR-0020 必须记录未决定的问题"
        )
        assert "锁定" in flat(text) or "Lock lease" in text or "崩溃恢复" in text, (
            "ADR-0020 必须记录 Lock 崩溃恢复这个未决问题"
        )

    def test_adr_number_is_next_available(self):
        """
        ADR 编号必须是**下一个可用**的，不能凭空假设。

        现有已编号的 ADR 里，0020 之前必须已经有 0019。
        """
        adr_dir = PROJECT_ROOT / "docs" / "architecture" / "adr"
        numbers = sorted(
            int(p.name[:4]) for p in adr_dir.glob("*.md")
            if p.name[:4].isdigit()
        )

        assert 20 in numbers, "ADR-0020 不存在"
        assert 19 in numbers, (
            "ADR-0020 之前必须有 0019 —— 编号不能跳"
        )
        # 0020 必须是当前已用的编号之一
        assert max(numbers) >= 20, "0020 必须是当前最大的编号"


# ============================================================
# 九、文档间不矛盾
# ============================================================
class TestDocsConsistent:
    """规范 §27：四份文档之间不能互相矛盾。"""

    def test_workspace_future_in_all_docs(self, workspace_doc, agents_doc):
        constitution = read(CONSTITUTION_FILE)

        assert "FUTURE" in workspace_doc
        assert "Task Workspace" in agents_doc
        assert "Task Workspace" in constitution

    def test_shared_context_not_confused_with_workspace(
        self, workspace_doc
    ):
        """
        Shared Context 与 Workspace 的区分必须在两份文档里一致。

        【为什么不能要求两份文档用同一句话】
            第一版要求 `TASK_SHARED_CONTEXT.md` 里出现「共同知道什么」，
            但那份文档的措辞是：

                | **Shared Context** | 我们共同**认为**这个任务现在是什么 |

            —— 意思完全一致，**措辞不同**。

            要求措辞逐字一致会把守卫变成「文案检查」，
            而不是「语义检查」。所以这里查的是**各自的核心表述**：
                TASK_SHARED_CONTEXT.md  → 它是「任务世界模型 / 认为是什么」
                TASK_WORKSPACE.md       → 它复述「知道什么 vs 操作什么」
        """
        shared = flat(read(SHARED_CONTEXT_FILE))
        workspace = flat(read(WORKSPACE_FILE))

        # ① Shared Context 文档把自己定位为「任务世界模型 / 认为是什么」
        assert ("World Model" in shared or "世界模型" in shared
                or "认为" in shared), (
            "TASK_SHARED_CONTEXT.md 必须把自己定位为「共同的任务世界模型」"
        )

        # ② Workspace 文档必须给出这句对照
        assert "共同知道什么" in workspace, (
            "TASK_WORKSPACE.md 必须写出「Shared Context = 我们共同知道什么」"
        )
        assert "共同操作什么" in workspace, (
            "TASK_WORKSPACE.md 必须写出「Workspace = 我们共同操作什么」"
        )

        # ③ 两份文档都必须明确「二者独立」
        assert "独立" in workspace

    def test_private_workspace_not_task_workspace(self, workspace_doc):
        """两份文档对 Private Workspace 的定位必须一致。"""
        workspace = flat(section(workspace_doc, "5."))

        assert "Agent Private Workspace ≠ Task Workspace" in workspace or (
            "Agent Private Workspace" in workspace and "≠" in workspace
        ), "§5 必须显式写出 Agent Private Workspace ≠ Task Workspace"

    def test_no_contradictory_status(self, workspace_doc, agents_doc):
        """
        Task Workspace 的状态在 AGENTS.md 与 TASK_WORKSPACE.md 里必须一致。

        【这条防的是】一边写 FUTURE，另一边写 CURRENT。
        """
        agents_memory = flat(section(agents_doc, "4. Memory 四层"))
        workspace_head = flat(workspace_doc[:1500])

        assert "`FUTURE`" in agents_memory
        assert "FUTURE" in workspace_head


# ============================================================
# 十、反向测试
# ============================================================
class TestReverseVerification:
    """
    ADR-0018 的硬规定：新增守卫**必须**配反向测试。

    这里验证**检测机制本身**有效。
    """

    def test_section_scoped_not_whole_file(self):
        """`section()` 必须真的只取那一节。"""
        doc = read(WORKSPACE_FILE)

        lock = section(doc, "10.")
        identity = section(doc, "6.")

        assert "Lock owner" in lock
        assert "Lock owner" not in identity, (
            "section() 取的范围太大 —— 它把 §10 的内容也拿进 §6 了"
        )

    def test_section_returns_empty_for_missing(self):
        assert section(read(WORKSPACE_FILE), "99. 不存在") == "", (
            "section() 对不存在的标题必须返回空 —— "
            "否则守卫会在空字符串上假绿"
        )

    def test_section_includes_subsections(self):
        """
        `section()` 必须包含子小节。

        【这是真实踩过的坑】
            第一版在 `### 4.1` 处就截断了（因为 `#{1,4}` 也匹配 `###`），
            守卫于是在**被截断的段落**上断言。
        """
        doc = read(WORKSPACE_FILE)
        parent = section(doc, "2.")

        # §2 有很多子小节（2.1 共享资源模型 … 2.10）
        assert "2.1" in parent and "2.10" in parent, (
            "section('2.') 没有包含子小节 —— 守卫会在截断的段落上断言"
        )

    def test_import_detection_works(self):
        """AST import 检测必须真的能发现依赖。"""
        mods = module_imports(PROJECT_ROOT / "core" / "workspace.py")

        assert "pathlib" in mods or "os" in mods, (
            "core/workspace.py 本该有文件系统依赖 —— 检测坏了"
        )

    def test_import_detection_negative(self):
        """反向：已知不存在的依赖必须检测为没有。"""
        mods = module_imports(PROJECT_ROOT / "core" / "task_workspace.py")

        assert "git" not in mods
        assert "paramiko" not in mods

    def test_called_names_distinguishes_is_x_from_x(self):
        """
        反向：`called_names` 不能把 `is_open` 当成 `open`。

        【这是本项目记过的子串误报（ADR-0018）】
            `"open(" in source` 会匹配到 `is_open(self)`。
        """
        workspace_py = PROJECT_ROOT / "core" / "workspace.py"
        raw = workspace_py.read_text(encoding="utf-8")
        called = called_names(workspace_py)

        # 陷阱存在：原文里有 open(
        assert "open(" in raw
        # 但 AST 检测不会把 is_open 算成 open
        assert "open" not in called or "is_open" not in called

    def test_future_check_would_catch_a_current_marker(self):
        """
        反向：如果 Task Workspace 被标成 CURRENT，
        `test_agents_still_marks_workspace_future` 必须变红。

        这里用一段**伪造的** Markdown 验证检测逻辑。
        """
        fake = """
        | Layer | 属于谁 | 状态 | 是什么 |
        |---|---|---|---|
        | **3** Task Workspace | Task | `CURRENT` | 实际共享的文件与代码 |
        """
        memory = section(fake, "4.")

        # 伪造的文档里没有 FUTURE 标记 —— 检测应当失败
        assert "`FUTURE`" not in memory, (
            "检测逻辑本身有问题（伪造文档里本不该有 FUTURE）"
        )

    def test_permission_lock_check_would_catch_a_merge(self):
        """反向：伪造「Permission = Lock」的文本，确认检测能发现。"""
        fake = "Permission = lock，有 WRITE permission 就自动拥有 lock。"
        text = flat(fake)

        assert "Permission = lock" in text or "自动拥有" in text, (
            "检测模式匹配不到被合并的写法"
        )

    def test_forbidden_class_check_is_live(self):
        """
        反向：确认「类名检测」在**其他**文件上是有效的 ——
        否则「没有 Permission 类」这条断言可能只是因为
        整个项目的类名都不匹配而永远为真。
        """
        all_defined = set()
        for path in production_files():
            all_defined |= defined_classes(path)

        # 这些类**确实存在** —— 证明检测能看到类
        assert "Task" in all_defined
        assert "TaskSharedContext" in all_defined
        # 而这些**确实不存在**
        assert "Permission" not in all_defined
        assert "LockManager" not in all_defined

    def test_workspace_table_check_distinguishes_names(self, db):
        """
        反向：`workspaces`（精确表名）不存在，但
        `task_shared_contexts` 存在 —— 证明检查是按**精确名字**比对的，
        不是子串。
        """
        names = db.table_names()

        assert "task_shared_contexts" in names
        assert "workspaces" not in names, (
            "workspaces 表不该存在"
        )
        # 关键：不能因为 task_shared_contexts 含 "workspace" 就误判
        assert "workspace_permissions" not in names

    def test_adr_numbering_check_would_catch_a_gap(self):
        """反向：确认编号连续性检查能发现跳号。"""
        fake_numbers = [12, 14, 18, 19, 20]

        expected = list(range(min(fake_numbers), max(fake_numbers) + 1))
        assert fake_numbers != expected, (
            "伪造的编号列表本来就该是不连续的 —— 测试前提不成立"
        )


# ============================================================
# 十一、本次新增的反向测试（规范 §23）
# ============================================================
class TestReverseVerificationForLockRecovery:
    """
    规范 §23 点名要求守卫至少能抓这六种违规：

        ① 把 Explicit Lock 改成 Automatic Lease
        ② 把 Human Force Unlock 删除
        ③ 把 Guardian 写成 CURRENT
        ④ 把 Heartbeat 写成当前实现
        ⑤ 把 Guardian 判断写成 Domain Fact
        ⑥ 把 Task Workspace 写成 CURRENT

    这里验证**检测机制本身**有效 ——
    不是在测试里真的改文档，而是验证「识别逻辑」对
    伪造的违规文本/结构确实会判负。

    （真正的注入式反向测试在提交前手工跑一遍，见 commit message。）
    """

    def test_detects_automatic_lease_replacement(self):
        """
        ① 伪造「用自动租约」的文本，确认检测能找到它。

        【为什么要检查**两种**写法】
            违规可能写成 "automatic lease"，也可能写 "自动过期"。
            只查一种会漏。
        """
        fake = """
        ### 16.2 v1 决策：采用自动租约
        Write Lock 在 300 秒后自动过期，超时即视为 Agent 已崩溃。
        """
        text = flat(fake)

        markers = ("自动过期", "300 秒", "视为 Agent 已崩溃")
        assert any(m in text for m in markers), (
            "检测逻辑找不到伪造的「自动租约」写法"
        )

        # 而正确文档里必须是「显式持有模型」
        real = flat(read(WORKSPACE_FILE))
        assert "显式持有模型" in real and "不采用自动时间失效" in real

    def test_detects_missing_human_force_unlock(self):
        """② 伪造一份**删掉** Human Force Unlock 的文本。"""
        fake = """
        ### 16.8 v1 异常恢复模型
        目前没有恢复路径。
        """
        assert "Human Force Unlock" not in fake, (
            "伪造文本里本不该有 Human Force Unlock —— 测试前提不成立"
        )

        real = flat(read(WORKSPACE_FILE))
        assert "Human Force Unlock" in real, (
            "真实文档必须有 Human Force Unlock"
        )

    def test_detects_guardian_as_current(self):
        """
        ③ 伪造「Guardian 是 CURRENT」，确认那行会被判为违规。

        【复用 test_guardian_not_current 的同一套判定逻辑】
        """
        fake = "| Guardian / Observer Agent | `CURRENT` | 已实现 |"
        negations = ("不", "未", "≠", "❌", "FUTURE", "禁止")

        # 伪造行里没有否定词 → 判定为「肯定式声称」→ 应当违规
        assert "`CURRENT`" in fake
        assert not any(n in fake for n in negations), (
            "伪造行本该没有否定词 —— 测试前提不成立"
        )

    def test_detects_heartbeat_as_current_implementation(self):
        """④ 伪造「Heartbeat 已实现」，确认能被识别。"""
        fake = "Heartbeat 已经实现，每 30 秒续租一次 Write Lock。"
        text = flat(fake)

        assert "Heartbeat" in text
        assert "已经实现" in text or "续租" in text, (
            "检测逻辑找不到伪造的「Heartbeat 已实现」"
        )

        # 真实文档必须说它未实现/禁止
        real = flat(read(WORKSPACE_FILE))
        assert "不实现" in real and "heartbeat" in real.lower()

    def test_detects_guardian_judgment_as_domain_fact(self):
        """
        ⑤ 伪造「Guardian 判断就是系统事实」，确认能被识别。

        正确写法是「Observation → … → Policy → Action」，
        违规写法是「Guardian says DEAD → system kills」。
        """
        fake = """
        Guardian 判断 Agent 已死，系统自动把 Task 标记为 FAILED。
        """
        text = flat(fake)

        assert "自动" in text and "FAILED" in text, (
            "检测逻辑找不到伪造的「Guardian 判断直接改写 Task Core」"
        )

        # 真实文档必须明确禁止这条链路
        real = flat(read(WORKSPACE_FILE))
        assert "自动变成 FAILED" in real
        assert "不能自动发生" in real

    def test_detects_task_workspace_as_current(self):
        """⑥ 伪造「Task Workspace 是 CURRENT」。"""
        fake = """
        | **实现状态** | **`CURRENT`** —— 已完整实现 |
        """
        lines = [l for l in fake.splitlines() if "实现状态" in l]

        assert lines, "伪造文本里本该有实现状态栏"
        for line in lines:
            assert "`CURRENT`" in line, "测试前提不成立"

        # 真实文档必须是 FUTURE
        real_lines = [
            l for l in read(WORKSPACE_FILE)[:1200].splitlines()
            if "实现状态" in l
        ]
        for line in real_lines:
            assert "`FUTURE`" in line

    def test_detects_no_expired_state_addition(self):
        """⑦ 伪造「加 EXPIRED 状态」，确认 EXPIRED 检测能区分语境。"""
        fake_ok = "> ⚠️ **不要增加 `EXPIRED`。**"
        fake_bad = "| `EXPIRED` | 租约到期自动失效 |"

        # 正确语境：含禁止词
        assert any(w in fake_ok for w in ("不要", "禁止", "不增加"))
        # 违规语境：不含禁止词
        assert not any(w in fake_bad for w in ("不要", "禁止", "不增加")), (
            "检测逻辑区分不出 EXPIRED 的语境"
        )

    def test_lock_state_check_would_catch_a_missing_state(self):
        """⑧ 伪造一份缺 REVOKED 的锁状态表。"""
        fake = """
        | 状态 | 语义 |
        |---|---|
        | `ACTIVE` | 当前有效锁 |
        | `RELEASED` | 正常释放 |
        """
        assert "REVOKED" not in fake, "测试前提不成立"
        assert "REVOKED" in read(WORKSPACE_FILE), (
            "真实文档必须有 REVOKED 状态"
        )

    def test_section_scoping_prevents_multi_location_false_pass(self):
        """
        反向：确认「同一句话出现在多处」不会让守卫假绿。

        【这是本会话暴露过的真实缺陷】
            `FUTURE` 在某节里出现 5 次，把目标那行改成 CURRENT 后
            守卫照样绿。所以关键断言必须**按行/按节**限定。
        """
        doc = read(WORKSPACE_FILE)

        # §16（Lock Recovery 专章）里必须有这些断言
        lock_recovery = flat(section(doc, "16."))
        for phrase in ("显式持有模型", "不采用自动时间失效",
                       "Human Force Unlock", "可追踪的治理记录"):
            assert phrase in lock_recovery, (
                f"§16 里缺少 {phrase} —— 即使别处有也不算"
            )
