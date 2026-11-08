"""
Read / Visibility 与 Communication 守卫

============================================================
【这个文件守什么】
============================================================

本轮确立：

    Read  = Task Participation（第一版无 READ ACL）
    Write = Lock
    Communication = Agent 基础能力（不绑 Task）
    Workspace Event ≠ Agent Message
    Runtime Inbox（异步、不丢消息）

风险：文档回退 —— 重新引入 READ ACL，或把 Communication
写回「Task Message Bus」。

============================================================
【为什么不能只查关键词】
============================================================

`READ` / `Task` / `Authorization` **必须**能出现
（因为我们要说「不引入 READ」「不要求绑定 Task」）。

判定原则：**逐行否定检查** —— 含关键词的行必须自带否定标记。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import markdown_guard_helpers as markdown

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCH_DIR = PROJECT_ROOT / "docs" / "architecture"

COMM_DOC = ARCH_DIR / "COMMUNICATION_MODEL.md"
WORKSPACE_DOC = ARCH_DIR / "TASK_WORKSPACE.md"
RESOURCE_DOC = ARCH_DIR / "RESOURCE_LAYER.md"
ADR_0028 = ARCH_DIR / "adr" / "0028-read-visibility-and-native-communication.md"
AGENTS_FILE = PROJECT_ROOT / "AGENTS.md"

#: 否定 / 禁止标记
NEGATION = ("不", "❌", "禁止", "不要", "不得", "无需", "没有", "null")

#: 「可读」类肯定词 —— 排除项行里不许出现
_AFFIRMATIVE_READ = ("可读", "默认可读", "可以读", "允许读", "READ =", "可访问")


def read(path: Path) -> str:
    return markdown.read(path)


def assert_lines_negated(document: str, keyword: str, why: str):
    """
    **逐行否定检查**：含 `keyword` 的行必须自带否定标记。

    【为什么必须逐行】
        查 `"不引入" in text and "READ" in text` 会被**别处**的词蒙混。
        本项目已多次踩到（`也不要规定` 含 `不要规定`）。
    """
    lines = [l for l in document.splitlines() if keyword in l]
    assert lines, f"文档里本该提到 {keyword}（{why}）—— 否则守卫是空转的"

    for line in lines:
        if line.strip().startswith(("|", "#")) and "→" not in line:
            # 表格/标题行可能是对照表的一部分，放宽但仍要求同行有否定或对比
            pass
        assert any(n in line for n in NEGATION), (
            f"这一行提到 {keyword} 但没有否定标记（{why}）：\n    {line.strip()}"
        )


@pytest.fixture(scope="module")
def comm() -> str:
    return read(COMM_DOC)


@pytest.fixture(scope="module")
def workspace() -> str:
    return read(WORKSPACE_DOC)


# ============================================================
# 一、Read / Visibility
# ============================================================
class TestReadModel:
    def test_section_exists(self, workspace):
        assert markdown.section(workspace, "7.5")

    def test_no_resource_level_read_acl(self, workspace):
        """
        🔒 第一版不建立资源级 Read ACL。

        【必须排除标题行】
            第一版查 `"不建立资源级 Read ACL" in block` ——
            但 §7.5.1 的**标题**就是
            `### 7.5.1 第一版**不建立资源级 Read ACL**`。
            把正文改成「建立资源级 READ ACL」后标题还在 → 假绿。
            → 只查**正文行**（排除以 `#` 开头的行）。
        """
        block = markdown.plain(markdown.section(workspace, "7.5.1"))

        body = "\n".join(
            l for l in markdown.section(workspace, "7.5.1").splitlines()
            if not l.strip().startswith("#")
        )
        body = " ".join(body.split())

        assert "不建立资源级 Read ACL" in body, (
            "§7.5.1 的**正文**必须写出「不建立资源级 Read ACL」"
            "（标题不算 —— 标题可能保留旧措辞而正文被改）"
        )

    def test_no_read_permission_family(self, workspace):
        """不许引入 READ / READ_FILE / READ_DIRECTORY / READ_RESOURCE。"""
        block = markdown.section(workspace, "7.5.1")

        for name in ("READ_FILE", "READ_DIRECTORY", "READ_RESOURCE"):
            assert name in block, f"§7.5.1 必须点名禁止 {name}"

        # 【块级检查，不逐行】
        #   文档里那一句是**折行**的：
        #       ❌ **不要**重新引入 `READ` / `READ_FILE` / `READ_DIRECTORY` /
        #       `READ_RESOURCE` 之类的独立 Authorization 权限系统。
        #   第二行不带 ❌ —— 逐行检查会把它误判成「肯定式」。
        #   （这正是本项目反复出现的「跨行块」问题。）
        flat_block = " ".join(block.split())
        assert "不要" in flat_block or "❌" in flat_block, (
            "§7.5.1 必须以否定形式禁止 READ 家族"
        )

    def test_participation_grants_read(self, workspace):
        """Task Participation → Read。"""
        block = markdown.plain(markdown.section(workspace, "7.5.2"))

        assert "Participants(T1)" in block, (
            "§7.5.2 必须给出 Participants 的推导"
        )
        assert "自动获得" in block, (
            "§7.5.2 必须写明「自动获得 READ」"
        )

    def test_read_not_write(self, workspace):
        """🔒 Read 与 Write 必须彻底分开。"""
        block = markdown.section(workspace, "7.5.3")

        assert "Read 与 Write 必须彻底分开" in block or (
            "彻底分开" in block
        ), "§7.5.3 必须写出 Read / Write 分离"
        # 【必须检查这句**否定式**规范句】
        #   只查「彻底分开」会被标题蒙混；只查「参与 Task」会被别处蒙混。
        assert "不能因为参与 Task 就自动修改" in markdown.plain(block), (
            "§7.5.3 必须写出否定式规范句："
            "「不能因为参与 Task 就自动修改所有内容」"
        )

    def test_private_and_system_excluded(self, workspace):
        """🔒 Private / System Workspace 不在默认 Read 范围。"""
        block = markdown.section(workspace, "7.5.4")

        for excluded in ("Agent Private Workspace", "Agent Private Memory",
                         "System Workspace"):
            assert excluded in block, f"§7.5.4 必须排除 {excluded}"

        # 【逐行检查：排除项所在的行不能带「可读」类肯定词】
        #   否则把「| Agent Private Workspace | |」改成
        #   「| Agent Private Workspace（默认可读） | |」时守卫会假绿。
        from tests.test_read_communication_guards import _AFFIRMATIVE_READ
        rows = [
            l for l in markdown.section(workspace, "7.5.4").splitlines()
            if "Agent Private" in l or "System Workspace" in l
        ]
        assert rows, "§7.5.4 必须有排除项的行"

        for line in rows:
            for word in _AFFIRMATIVE_READ:
                assert word not in line, (
                    f"§7.5.4 的排除项行出现了肯定词 {word!r}：{line.strip()!r}"
                )

    def test_private_read_example(self, workspace):
        block = workspace_section(workspace, "7.5.4")

        assert "不能读取 A Private Workspace" in block, (
            "§7.5.4 必须给出「B 不能读 A Private Workspace」的例子"
        )

    def test_when_read_is_granted(self, workspace):
        """在**正式成为 Task Participant** 时获得 Read。"""
        block = markdown.plain(markdown.section(workspace, "7.5.5"))

        assert "正式成为" in block and "Task Participant" in block, (
            "§7.5.5 必须写明 Read 在正式成为 Participant 时获得"
        )

    def test_reuse_existing_lifecycle(self, workspace):
        """🔒 优先复用现有 Task 模型，不重复建机制。"""
        block = markdown.plain(markdown.section(workspace, "7.5.5"))

        assert "优先复用" in block, (
            "§7.5.5 必须写明「优先复用现有 Task 模型」"
        )

    def test_human_governance_unchanged(self, workspace):
        """🔒 不因 Read 模型而改变 Human Governance。"""
        block = markdown.plain(markdown.section(workspace, "7.5.6"))

        assert "不要因为 Read 模型而改变" in block, (
            "§7.5.6 必须写明不改变既有 Human Governance"
        )

    def test_formal_principle(self, workspace):
        block = markdown.plain(markdown.section(workspace, "7.5.7"))

        assert "不实现资源级 Read ACL" in block, (
            "§7.5.7 必须写出正式原则"
        )
        assert "两个独立概念" in block, (
            "§7.5.7 必须写明 Read Visibility 与 Write Responsibility 独立"
        )


def workspace_section(doc, prefix):
    return markdown.section(doc, prefix)


# ============================================================
# 二、Communication 定位
# ============================================================
class TestCommunicationPositioning:
    def test_doc_exists(self):
        assert COMM_DOC.exists(), "COMMUNICATION_MODEL.md 不存在"

    def test_not_task_message_bus(self, comm):
        """🔒 Communication ≠ Task Message Bus。"""
        block = markdown.plain(markdown.section(comm, "0.1"))

        assert "Communication ≠ Task Message Bus" in block, (
            "§0.1 必须写出「Communication ≠ Task Message Bus」"
        )
        assert "❌" in markdown.section(comm, "0.1"), (
            "§0.1 的旧理解必须标 ❌"
        )

    def test_native_agent_capability(self, comm):
        text = markdown.plain(comm)

        assert "Communication 是 Agent 的基础能力" in text, (
            "必须写出「Communication 是 Agent 的基础能力」"
        )
        assert "不以 Task 为前提" in text, (
            "必须写出「不以 Task 为前提」"
        )

    def test_task_is_a_scenario_not_premise(self, comm):
        text = markdown.plain(comm)

        assert "Task 是 Communication 的一个重要使用场景" in text, (
            "必须写出「Task 是使用场景，而不是前提」"
        )

    def test_message_need_not_bind_task(self, comm):
        """🔒 Message 不要求绑定 Task。"""
        block = markdown.plain(markdown.section(comm, "1."))

        assert "不要要求每条 Message 都必须绑定 Task" in block, (
            "§1 必须写明不要求每条 Message 绑定 Task"
        )

    def test_null_fields_allowed(self, comm):
        """§1.2 必须给出 task_id / workspace_id / resource_id = null。"""
        block = markdown.section(comm, "1.2")

        for field in ("task_id", "workspace_id", "resource_id"):
            assert field in block, f"§1.2 必须列出 {field}"
        assert block.count("null") >= 3, "§1.2 必须写明三者都可以是 null"

    def test_conversation_concept(self, comm):
        block = markdown.section(comm, "2. Conversation")

        assert "Conversation" in block, "§2 必须定义 Conversation"
        assert "null" in block or "FUTURE" in block

    def test_no_conversation_manager_required(self, comm):
        block = markdown.plain(markdown.section(comm, "2."))

        assert "不要求" in block and "Conversation Manager" in block, (
            "§2 必须写明当前不要求完整实现 Conversation Manager"
        )


# ============================================================
# 三、Workspace Event ≠ Agent Message
# ============================================================
class TestEventVsMessage:
    def test_distinguished(self, comm):
        """
        §3 必须对照两者。

        【不能用 `table_rows(block, "回答")`】
            "回答" 是**数据行标签**，不是**表头** ——
            `table_rows` 找的是含该单元格的**表头行**，所以返回空。
            （实测：返回 `[]`，守卫直接失败。）
        """
        block = markdown.plain(markdown.section(comm, "3. Workspace Event"))

        assert "系统自动" in block and "Agent 主动" in block, (
            "§3 必须写明「系统自动产生」vs「Agent 主动发送」"
        )
        assert "发生了什么" in block, "§3 必须给出 Workspace Event 回答的问题"
        assert "我认为这个变化为什么与你有关" in block, (
            "§3 必须给出 Agent Message 回答的问题"
        )

    def test_cannot_be_merged(self, comm):
        block = markdown.plain(markdown.section(comm, "3."))

        assert "不能混成一个机制" in block, (
            "§3 必须写明二者不能混成一个机制"
        )

    def test_no_auto_broadcast(self, comm):
        """🔒 不要自动广播所有 Workspace Change。"""
        block = markdown.plain(markdown.section(comm, "3.1"))

        assert "不要自动广播" in block, (
            "§3.1 必须写明不要自动广播"
        )
        assert "Communication Flood" in block, (
            "§3.1 必须说明后果是 Communication Flood"
        )

    def test_system_does_not_judge_impact(self, comm):
        """🔒 系统不判断「这个修改一定影响谁」。"""
        block = markdown.plain(markdown.section(comm, "3.2"))

        assert "系统不判断" in block or "系统记录变化" in block, (
            "§3.2 必须写明系统只记录、Agent 自己判断"
        )
        assert "Agent 自己判断语义影响" in block, (
            "§3.2 必须写明 Agent 自己判断语义影响"
        )

    def test_agent_can_initiate(self, comm):
        """Agent 可以主动通知。"""
        block = markdown.plain(markdown.section(comm, "4."))

        assert "Agent 自己负责判断是否值得主动沟通" in block, (
            "§4 必须写明 Agent 自己判断是否主动沟通"
        )


# ============================================================
# 四、Runtime / Inbox
# ============================================================
class TestRuntimeInbox:
    def test_llm_need_not_respond_realtime(self, comm):
        block = markdown.plain(markdown.section(comm, "5.1"))

        assert "不要要求" in block and "立即中断" in block, (
            "§5.1 必须写明不要求 LLM 立即中断响应"
        )

    def test_inbox_held_by_runtime(self, comm):
        """🔒 Inbox 必须由 Runtime 持有，不是 LLM Invocation。"""
        block = markdown.plain(markdown.section(comm, "5.3"))

        # 【不能用反引号】：block 已经过 `plain()`，
        #   反引号被剥掉了 → `\`Runtime\`` 匹配不到。
        assert "必须由 Runtime 持有" in block, (
            "§5.3 必须写明 Inbox 由 Runtime 持有"
        )
        assert "而不是由 LLM Invocation 本身持有" in block, (
            "§5.3 必须明确不是由 LLM Invocation 持有"
        )

    def test_inbox_survives_llm(self, comm):
        """🔒 Message 不能因 LLM 运行而丢失。"""
        block = markdown.plain(markdown.section(comm, "5.4"))

        assert "不能因为 LLM 正在运行而丢失" in block, (
            "§5.4 必须写明消息不会丢失"
        )
        assert "持久化" in block, "§5.4 必须写明持久化"

    def test_checkpoint_model(self, comm):
        block = markdown.section(comm, "5.2")

        assert "Checkpoint" in block, "§5.2 必须给出 Checkpoint 模型"
        assert "Inbox" in block, "§5.2 必须给出 Inbox"

    def test_core_principle(self, comm):
        text = markdown.plain(comm)

        assert "可以异步工作，但不能异步丢失" in text, (
            "必须写出「Agent 可以异步工作，但不能异步丢失通信与事实」"
        )


# ============================================================
# 五、Communication 与 Workspace 解耦
# ============================================================
class TestDecoupling:
    def test_cannot_modify_workspace(self, comm):
        """🔒 Communication 不能直接修改 Workspace。"""
        block = markdown.plain(markdown.section(comm, "6."))

        assert "Communication 本身不能直接修改 Workspace" in block, (
            "§6 必须写明 Communication 不能直接改 Workspace"
        )
        assert "Workspace Gateway" in block, (
            "§6 必须写明资源修改仍经 Workspace / Gateway"
        )


# ============================================================
# 六、四个概念各司其职
# ============================================================
class TestFourConcepts:
    def test_table_present(self, comm):
        block = markdown.section(comm, "7. 协作时四个概念")
        rows = markdown.table_rows(block, "概念")

        assert len(rows) == 4, (
            f"§7 的对照表有 {len(rows)} 行，应该恰好 4 行"
        )
        joined = " ".join(rows)
        for concept in ("Revision", "Workspace Event", "Agent Message",
                        "Task Shared Context"):
            assert concept in joined, f"§7 缺少 {concept}"

    def test_cannot_be_merged(self, comm):
        block = markdown.plain(markdown.section(comm, "7."))

        assert "不要混在一起" in block, "§7 必须写明四者不能混在一起"


# ============================================================
# 七、Protocol 不需要修改（实测）
# ============================================================
class TestProtocolUnchanged:
    def test_three_checks_documented(self, comm):
        block = markdown.section(comm, "8. Protocol 影响")

        assert "不需要修改" in block, (
            "§8 必须明确说明协议不需要修改"
        )
        rows = markdown.table_rows(block, "检查项")
        assert len(rows) == 3, (
            f"§8 的检查表有 {len(rows)} 行，应该恰好 3 行"
        )

    def test_message_fields_are_task_optional(self):
        """实测：Task/PROTOCOL 的 Message 里 task_id 是可选派生属性。"""
        import dataclasses

        from communication.message import Message

        fields = {f.name for f in dataclasses.fields(Message)}

        assert "task_id" not in fields, (
            "task_id 不该是 Message 的必填字段 —— "
            "它应该是 payload 派生的可选属性"
        )
        for required in ("from_agent", "to_agent", "message_type",
                         "payload", "message_id", "timestamp"):
            assert required in fields, f"Message 缺少基础字段 {required}"

    def test_conversation_id_exists(self):
        """实测：conversation_id 已经存在。"""
        import dataclasses

        from communication.message import Message

        fields = {f.name for f in dataclasses.fields(Message)}
        assert "conversation_id" in fields, (
            "Message 必须有 conversation_id 才能支持一般 Conversation"
        )

    def test_no_new_message_types(self):
        """不新增十几个 Message Type。"""
        for path in (PROJECT_ROOT / "communication").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for banned in ("GENERAL_MESSAGE", "SOCIAL_MESSAGE",
                           "CONVERSATION_START", "KNOWLEDGE_EXCHANGE"):
                assert banned not in source, (
                    f"{path.name} 里出现了 {banned} —— 本轮不新增 Message Type"
                )

    def test_protocol_doc_not_modified_for_this(self):
        """PROTOCOL.md 本轮未因 Communication 重新定位而改动。"""
        protocol = PROJECT_ROOT / "PROTOCOL.md"
        if not protocol.exists():
            pytest.skip("PROTOCOL.md 不存在")
        # 只是记录：不该出现「必须绑定 Task」式的强制表述
        text = markdown.plain(read(protocol))
        assert "每条消息必须绑定 Task" not in text, (
            "PROTOCOL.md 不该要求每条消息绑定 Task"
        )


# ============================================================
# 八、Economy 留接口
# ============================================================
class TestEconomy:
    def test_not_assumed_free(self, comm):
        """🔒 不假定通信永远免费。"""
        block = markdown.plain(markdown.section(comm, "9."))

        assert "不是免费的抽象 API" in block, (
            "§9 必须写明 Communication 不是免费抽象 API"
        )

    def test_cost_types_listed(self, comm):
        block = markdown.section(comm, "9.")

        for cost in ("Model Token Cost", "Tool Cost",
                     "Message Processing Cost", "Storage Cost"):
            assert cost in block, f"§9 必须列出成本类型：{cost}"

    def test_reuse_existing_currency(self, comm):
        """🔒 不新建 Currency。"""
        block = markdown.plain(markdown.section(comm, "9."))

        assert "不要为了 Communication 重新创建一套 Currency" in block, (
            "§9 必须写明不新建 Currency"
        )
        assert "AI Coin" in block, "§9 必须沿用 AI Coin"

    def test_not_a_task_bot(self, comm):
        block = markdown.section(comm, "9.1")

        assert "Task Bot" in block, "§9.1 必须对比 Task Bot"


# ============================================================
# 九、Revision / Stale Read（概念保留）
# ============================================================
class TestRevisionConcepts:
    def test_stale_read_concept(self, comm):
        block = markdown.section(comm, "10.1")

        assert "STALE READ" in block.upper().replace("`", ""), (
            "§10.1 必须保留 Stale Read 概念"
        )

    def test_not_decided_this_round(self, comm):
        """⚠️ 不自行决定完整 Stale Read 提交规则。"""
        block = markdown.plain(markdown.section(comm, "10.1"))

        assert "不要" in block and "自行决定" in block, (
            "§10.1 必须写明本轮不自行决定 Stale Read 规则"
        )

    def test_work_set_future(self, comm):
        block = markdown.section(comm, "10.2")

        assert "Work Set" in block, "§10.2 必须记录 Work Set 概念"


# ============================================================
# 十、Open Questions + ADR + 反向测试
# ============================================================
class TestOpenQuestionsAndADR:
    def test_open_questions_present(self, comm):
        block = markdown.section(comm, "12. Open Questions")
        rows = markdown.table_rows(block, "问题")

        assert len(rows) >= 8, (
            f"§12 只有 {len(rows)} 个 Open Question，应该至少 8 个"
        )

    def test_ack_is_open(self, comm):
        """是否需要 ACK / 回复 必须是 OPEN。"""
        block = markdown.section(comm, "12.")

        assert "ACK" in block, "§12 必须把 ACK / 回复列为 Open Question"

    def test_adr_exists(self):
        assert ADR_0028.exists(), "ADR-0028 不存在"

    def test_adr_sections(self):
        text = read(ADR_0028)
        for h in ("## Context", "## Decision", "## Consequences"):
            assert h in text, f"ADR-0028 缺少 {h}"

    def test_adr_indexed(self):
        index = read(ARCH_DIR / "adr" / "README.md")
        assert "0028" in index
        assert "0028-read-visibility-and-native-communication.md" in index

    def test_agents_navigation(self):
        text = read(AGENTS_FILE)
        assert "COMMUNICATION_MODEL.md" in text
        assert "ADR-0028" in text

    def test_reverse_negation_check_is_strict(self):
        """
        反向：`assert_lines_negated` 必须**真的**能抓到缺否定的行。
        """
        good = "❌ 不要引入 READ ACL\n"
        # 不该抛
        assert_lines_negated(good, "READ", "test")

        bad = "✅ 引入 READ ACL\n"
        with pytest.raises(AssertionError):
            assert_lines_negated(bad, "READ", "test")

    def test_all_used_sections_nonempty(self, comm, workspace):
        for prefix in ("0.1", "1.", "1.2", "2. Conversation",
                       "3. Workspace Event", "3.1", "3.2", "4.", "5.1",
                       "5.2", "5.3", "5.4", "6.", "7. 协作时四个概念",
                       "8. Protocol 影响", "9.", "9.1", "10.1", "10.2",
                       "12. Open Questions"):
            assert markdown.section(comm, prefix), f"COMM §{prefix} 是空的"

        for prefix in ("7.5.1", "7.5.2", "7.5.3", "7.5.4", "7.5.5",
                       "7.5.6", "7.5.7"):
            assert markdown.section(workspace, prefix), (
                f"WORKSPACE §{prefix} 是空的"
            )


# ============================================================
# 十一、Scope
# ============================================================
class TestNoImplementation:
    FORBIDDEN = (
        "conversation_manager.py", "messaging_service.py",
        "inbox_store.py", "communication_economy.py",
        "visibility_engine.py", "read_acl.py",
        "authorization_engine.py", "social_graph.py",
        "dependency_graph.py",
    )
    FORBIDDEN_CLASSES = (
        "ConversationManager", "MessagingService", "InboxStore",
        "CommunicationEconomyEngine", "VisibilityEngine", "ReadACL",
        "AuthorizationEngine", "SocialGraph", "DependencyGraph",
    )

    def test_no_modules(self):
        for layer in ("core", "services", "communication", "storage", "agents"):
            for name in self.FORBIDDEN:
                assert not (PROJECT_ROOT / layer / name).exists(), (
                    f"{layer}/{name} 出现了 —— 本轮禁止实现"
                )

    def test_no_classes(self):
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
        # + 2（Private Working Memory）= 25
        assert len(names) == 25, (
            f"表数应为 13 + 5 + 3 + 2 + 2 = 25，实际 {len(names)}："
            f"{sorted(names)}"
        )
