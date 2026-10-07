"""
协作生产接线测试 —— §十四 的 10 项要求 + 3 条架构守卫
========================================================

本文件验证的是**接线是否真的通了**，不是「协作是否自然发生」。
后者只能在真实 Runtime 里观察（见 `experiments/novel_production/collab_session.py`）。

【为什么这些测试要存在】

    在接线之前，这条多 Agent 生产线有两处**看起来在工作、其实没有**：

        · 5 个 Agent 各自产出 → 只进 Python 内存 → **没有共享生产资料**
        · `Checkpoint.revision = "rev-session-N"` → **实验器编的字符串**，
          不是 Workspace 的 revision

    「接口可调用」和「事实真的发生了」是两件事 ——
    本文件两项都测：既能发出去，也要能证明**对方真的收到了**。

【边界】

    🔒 本文件**不**断言「Agent 一定会通信」。
       强制通信 = Workflow，是任务书 §三 明令禁止的。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXP_DIR = PROJECT_ROOT / "experiments" / "novel_production"
if str(EXP_DIR) not in sys.path:
    sys.path.insert(0, str(EXP_DIR))

import channels                                       # noqa: E402
import shared_workspace as sw                         # noqa: E402

from tests.communication_helpers import FakeAgent     # noqa: E402
from tests.workspace_harness import WorkspaceHarness  # noqa: E402

W = "WS-NOVEL-001"
OWNER = "novel_world_builder"
AUTHOR = "novel_author"
READER = "novel_reader"
AGENTS = [OWNER, "novel_plot_architect", AUTHOR, "novel_editor", READER]


# ================================================================
# 夹具
# ================================================================
@pytest.fixture
def bridge():
    b = channels.SyncBridge()
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def comm():
    """真实的 CommunicationManager + LocalTransport（不接 recorder）。"""
    from communication import CommunicationManager, LocalTransport
    mgr = CommunicationManager(transport=LocalTransport())
    for aid in AGENTS:
        mgr.register(FakeAgent(aid), capabilities=())
    return mgr


@pytest.fixture
def ws_rig():
    """
    真实 Workspace：root Controller 建 5 个文件并 handoff 给责任人。

    🔒 用 `WorkspaceHarness`（生产同构的 Gateway + participant 判定），
       不自己造一个假的 revision 系统。
    """
    h = WorkspaceHarness.in_memory(
        participants={W: set(AGENTS)},
        agents=set(AGENTS) | {"outsider"},
    )
    gw = h.gateway()
    gw.ensure_workspace(W, OWNER)
    results = sw.provision(gw, W, OWNER, agent_ids=AGENTS)
    return h, gw, results


# ================================================================
# 1–6：通信接线
# ================================================================
class TestCommunicationWiring:
    """§十四 1–6：能发、能收、能看见、能唤醒、不需要 Coordinator、不碰 Self Model。"""

    # ---- 1. A 可以向 B 发消息 ----
    def test_01_agent_a_can_send_message_to_agent_b(self, comm, bridge):
        rec = channels.send(comm, bridge, from_agent=AUTHOR, to_agent=OWNER,
                            text="人物 X 的童年经历不明确，可以确认吗？")

        assert rec["ok"] is True
        assert rec["from"] == AUTHOR and rec["to"] == OWNER
        assert rec["message_id"].startswith("msg")
        # 消息真的进了对方的信箱（不是只返回一个对象）
        assert comm.pending(OWNER) == 1

    # ---- 2. B 可以收到 ----
    def test_02_agent_b_receives_it(self, comm, bridge):
        channels.send(comm, bridge, from_agent=AUTHOR, to_agent=OWNER,
                      text="请求确认人物 X 的设定")

        got = channels.drain(comm, bridge, OWNER)

        assert [m.from_agent for m in got["received"]] == [AUTHOR]
        assert "人物 X" in got["received"][0].payload["text"]
        assert comm.pending(OWNER) == 0, "取件后信箱必须空了"

    # ---- 3. B 的 Cognitive Context 可以看到 ----
    def test_03_b_cognitive_context_can_see_it(self, comm, bridge):
        channels.send(comm, bridge, from_agent=AUTHOR, to_agent=OWNER,
                      text="请求确认人物 X 的童年经历")
        got = channels.drain(comm, bridge, OWNER)

        rendered = channels.render_inbox(got["received"])

        assert "请求确认人物 X 的童年经历" in rendered
        assert AUTHOR in rendered
        assert got["received"][0].message_id in rendered

        # 它作为「材料」进入 Cognitive Context（不是指令）
        from cognition.context import CognitiveContext, ContextSource
        ctx = CognitiveContext(current_stimulus="(本轮刺激)")
        ctx.other.append(ContextSource(source="communication",
                                       content=rendered, priority=0.5))
        assert not ctx.is_empty
        assert any("人物 X" in s.content for s in ctx.sources())

    def test_03b_rendered_inbox_is_not_an_instruction(self, comm, bridge):
        """🔒 §四：可以说「别人对你说了什么」，不能说「你应该做什么」。"""
        channels.send(comm, bridge, from_agent=AUTHOR, to_agent=OWNER,
                      text="请更新设定")
        rendered = channels.render_inbox(
            channels.drain(comm, bridge, OWNER)["received"]).lower()

        for forbidden in ("you must", "you should", "必须回复", "你应该"):
            assert forbidden not in rendered

    # ---- 4. Event Wakeup 能触发 ----
    def test_04_event_wakeup_fires(self, comm, bridge):
        assert comm.is_woken(OWNER) is False

        rec = channels.send(comm, bridge, from_agent=AUTHOR, to_agent=OWNER,
                            text="请求确认设定")

        # wakeup 是投递的副作用（LocalTransport：先入信箱，再 signal）
        assert rec["wakeup_signalled"] is True
        assert comm.is_woken(OWNER) is True

        got = channels.drain(comm, bridge, OWNER)
        assert got["wakeup"] is True, "取件前必须能证明它被叫醒过"

    def test_04b_wakeup_carries_no_content(self, comm, bridge):
        """
        🔒 `communication/wakeup.py` 的边界：Wakeup ≠ Message。

            wakeup 只是「有事值得看一眼」，它**不携带内容**。
            消息只能从信箱取 —— 清掉信号不影响待取的消息。
        """
        channels.send(comm, bridge, from_agent=AUTHOR, to_agent=OWNER,
                      text="只存在于信箱里的正文")
        comm.clear_wakeup(OWNER)

        assert comm.is_woken(OWNER) is False
        assert comm.pending(OWNER) == 1, "清信号不该丢消息"
        got = channels.drain(comm, bridge, OWNER)
        assert got["wakeup"] is False          # 信号确实没了
        assert len(got["received"]) == 1        # 但消息还在

    # ---- 5. 不需要中央 Coordinator ----
    def test_05_no_central_coordinator_needed(self, comm, bridge):
        """
        两个 Agent 直接对发，中间**没有**任何第三方。

        🔒 如果存在 Coordinator，消息必然经过它的某个方法。
           这里断言：所有投递都走 `CommunicationManager.send`，
           而 Manager 只做「校验 + 投递」，**不决定谁该联系谁**。
        """
        channels.send(comm, bridge, from_agent=OWNER, to_agent=AUTHOR,
                      text="设定如下：……")
        channels.send(comm, bridge, from_agent=AUTHOR, to_agent=READER,
                      text="我按新设定改了正文")

        assert comm.pending(AUTHOR) == 1
        assert comm.pending(READER) == 1
        assert channels.drain(comm, bridge, AUTHOR)["received"][0].from_agent == OWNER
        assert channels.drain(comm, bridge, READER)["received"][0].from_agent == AUTHOR

    # ---- 6. Message 不自动改变 Self Model ----
    def test_06_message_does_not_touch_self_model_automatically(
            self, comm, bridge):
        """
        🔒 「收到消息」和「改变自己」之间必须隔着 Agent 自己的判断。

        这里用一个**真的** SelfModelStore：投递 10 条消息，
        不调用任何写入服务，断言 Self Model 一条都没变。
        """
        from selfmodel import SelfModelStore
        from storage.database import create_memory_database

        db = create_memory_database()
        store = SelfModelStore(db)
        before = store.retrieve(READER)

        for i in range(10):
            channels.send(comm, bridge, from_agent=AUTHOR, to_agent=READER,
                          text=f"第 {i} 条建议：你应该改成悬念驱动")
        drain = channels.drain(comm, bridge, READER)

        assert len(drain["received"]) == 10
        after = store.retrieve(READER)
        assert len(before) == len(after) == 0, (
            "收到消息就让 Self Model 变了 —— 那是把建议当成了决定")

    def test_06b_receive_reaches_agent_handler_but_does_not_reply(
            self, comm, bridge):
        """默认 `handle_message` 只记录、不回复（否则会无限互相回复）。"""
        channels.send(comm, bridge, from_agent=AUTHOR, to_agent=OWNER, text="在吗")

        channels.drain(comm, bridge, OWNER)

        assert comm.pending(AUTHOR) == 0, "默认行为不该自动回一条消息"


# ================================================================
# 7–9：Workspace 真实 revision
# ================================================================
class TestRealWorkspaceRevision:
    """§十四 7–9：写入真的变 revision；Checkpoint 存真值；Resume 读得回来。"""

    # ---- 7. Workspace 写入后 revision 真正变化 ----
    def test_07_write_actually_changes_revision(self, ws_rig):
        _h, gw, prov = ws_rig
        assert all(p["ok"] for p in prov)

        before = sw.snapshot(gw, W, OWNER)
        assert before["files"]["manuscript.md"] == 1

        res = sw.apply_write(gw, W, AUTHOR, "manuscript.md", "第一段正文。")

        assert res["ok"] is True
        after = sw.snapshot(gw, W, OWNER)
        assert after["files"]["manuscript.md"] == 2
        assert after["revision"] != before["revision"], (
            "内容变了但 checkpoint 指纹没变 —— revision 不是真实的")

    def test_07b_revision_comes_from_gateway_not_the_experimenter(self, ws_rig):
        """🔒 §六：不许再出现实验器自己编的 revision 标签。"""
        _h, gw, _prov = ws_rig
        res = sw.apply_write(gw, W, AUTHOR, "manuscript.md", "正文 v1")

        real = gw.read(W, "/manuscript.md", AUTHOR).result["revision"]
        assert res["revision"] == real
        assert isinstance(res["revision"], int)

    def test_07c_other_agent_cannot_write_your_file(self, ws_rig):
        """
        §十七「是否重复劳动 / 是否拒绝建议」的结构性前提：

            🔒 一个 Agent **写不了**别人负责的文件。
               它只能去沟通，由对方决定改不改。
               代码**不**替它重定向 —— 于是协作是逼出来的，不是编排出来的。
        """
        _h, gw, _prov = ws_rig

        res = sw.apply_write(gw, W, AUTHOR, "world_bible.md", "我来改世界观")

        assert res["ok"] is False
        assert res["error"]["code"] == "LOCKED_BY_OTHER_AGENT"
        assert res["error"]["lock_holder"] == OWNER

    def test_07d_read_is_open_to_every_participant(self, ws_rig):
        """读靠 Participation（无 READ ACL）—— 与「写要有责任」不对称。"""
        _h, gw, _prov = ws_rig
        sw.apply_write(gw, W, AUTHOR, "manuscript.md", "正文")

        got = gw.read(W, "/manuscript.md", READER)
        assert got.success and got.result["content"] == "正文"

    # ---- 8. Checkpoint 保存真实 Workspace revision ----
    def test_08_checkpoint_stores_real_workspace_revision(self, ws_rig):
        from stage import STATUS_ACTIVE, StageStore

        h, gw, _prov = ws_rig
        st = StageStore(h.new_db())
        st.create_stage("task_novel_001", "写小说", stage_id="s1",
                        status="ACTIVE", deliverables=[{"name": "manuscript.md"}])

        sw.apply_write(gw, W, AUTHOR, "manuscript.md", "第一段")
        snap = sw.snapshot(gw, W, OWNER)
        ck = st.save_checkpoint("s1", reason="hard_limit", workspace_id=W,
                                revision=snap["revision"])

        stored = st.get_checkpoint(ck.checkpoint_id)
        assert stored.revision == snap["revision"]
        assert stored.revision.startswith(f"ws:{W}@")
        assert "rev-session" not in stored.revision, "不许再用假标签"
        # Checkpoint 只引用，不复制内容（§六）
        assert "第一段" not in stored.revision

    def test_08b_checkpoint_revision_moves_only_when_content_moves(self, ws_rig):
        h, gw, _prov = ws_rig
        from stage import STATUS_ACTIVE, StageStore
        st = StageStore(h.new_db())
        st.create_stage("task_novel_001", "写小说", stage_id="s1",
                             status=STATUS_ACTIVE)

        r1 = sw.snapshot(gw, W, OWNER)["revision"]
        c1 = st.save_checkpoint("s1", workspace_id=W, revision=r1)
        # 没写任何东西 → 同一个 revision
        r2 = sw.snapshot(gw, W, OWNER)["revision"]
        assert r1 == r2

        sw.apply_write(gw, W, AUTHOR, "manuscript.md", "有内容了")
        r3 = sw.snapshot(gw, W, OWNER)["revision"]
        assert r3 != r1, "内容变了 revision 必须变"
        assert st.get_checkpoint(c1.checkpoint_id).revision == r1

    # ---- 9. Resume 后能读取正确 Workspace revision ----
    def test_09_resume_reads_back_the_same_real_revision(self):
        """
        🔒 用**文件库**（内存库关掉就没了）—— 这才是真实 Resume：
           新连接、新 Gateway，从持久状态读回 revision。
        """
        import tempfile, os
        from stage import STATUS_ACTIVE, StageStore
        from storage.database import Database

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "resume.db")

            def open_gw():
                db = Database(path)
                db.connect()
                db.initialize()
                return db, WorkspaceHarness.on_file(path, participants={W: set(AGENTS)},
                                                    agents=set(AGENTS)).gateway(db)

            # ---- Session 1：写 + checkpoint ----
            db1, gw1 = open_gw()
            gw1.ensure_workspace(W, OWNER)
            sw.provision(gw1, W, OWNER, agent_ids=AGENTS)
            sw.apply_write(gw1, W, AUTHOR, "manuscript.md", "Session 1 的正文")
            snap1 = sw.snapshot(gw1, W, OWNER)
            st1 = StageStore(db1)
            st1.create_stage("task_novel_001", "写小说", stage_id="s1",
                             status="ACTIVE")
            ck = st1.save_checkpoint("s1", reason="hard_limit", workspace_id=W,
                                     revision=snap1["revision"])
            db1.close()

            # ---- Session 2：新连接，读**持久**状态 ----
            db2, gw2 = open_gw()
            st2 = StageStore(db2)
            resumed = st2.get_checkpoint(ck.checkpoint_id)
            snap2 = sw.snapshot(gw2, W, OWNER)

            assert resumed.revision == snap1["revision"] == snap2["revision"]
            assert snap2["files"]["manuscript.md"] == 2
            assert gw2.read(W, "/manuscript.md", AUTHOR).result["content"] \
                == "Session 1 的正文"
            db2.close()


# ================================================================
# 10：Message 与 Workspace 不互为第二事实源
# ================================================================
class TestNotSecondSourceOfTruth:
    """§十四 10 / §五：Message 管沟通，Workspace 管共享事实。"""

    def test_10_message_does_not_become_the_workspace(self, ws_rig, comm, bridge):
        """
        发一条消息**不会**改变 Workspace；改写 Workspace **不会**产生消息。
        """
        _h, gw, _prov = ws_rig
        before = sw.snapshot(gw, W, OWNER)

        channels.send(comm, bridge, from_agent=OWNER, to_agent=AUTHOR,
                      text="世界观已经更新，把正文改一下")

        after = sw.snapshot(gw, W, OWNER)
        assert after["revision"] == before["revision"], (
            "发了消息 Workspace 就变了 —— Message 变成了第二事实源")
        assert comm.pending(AUTHOR) == 1

        # 反向：写 Workspace 不产生任何消息
        channels.drain(comm, bridge, AUTHOR)
        sw.apply_write(gw, W, AUTHOR, "manuscript.md", "新正文")
        assert comm.pending(OWNER) == 0, (
            "写了 Workspace 就自动通知别人 —— 那是 Workflow 编排")

    def test_10b_manuscript_body_is_not_copied_into_messages(self):
        """
        🔒 结构守卫：协作消息的载荷字段里**没有**承载整篇正文的通道。

        Message 只放「请求 / 意见 / 说明」，正文属于 Workspace。
        """
        src = (EXP_DIR / "channels.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        # channels.py 里不出现任何「读取/写入 Workspace 内容」的调用
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not (names & {"create", "edit", "read", "workspace_gateway"}), (
            "channels.py 碰了 Workspace —— 通信层不该有这个能力")

        ws_src = (EXP_DIR / "shared_workspace.py").read_text(encoding="utf-8")
        ws_tree = ast.parse(ws_src)
        ws_names = {n.attr for n in ast.walk(ws_tree)
                    if isinstance(n, ast.Attribute)}
        assert not (ws_names & {"send", "send_message", "communication"}), (
            "shared_workspace.py 碰了通信 —— Workspace 不该自己发通知")


# ================================================================
# 架构守卫：不许有固定协作顺序 / 中央协调者
# ================================================================
FORBIDDEN_COLLAB_TYPES = (
    "CollaborationEngine", "Coordinator", "AgentOrchestrator", "RoleManager",
    "CollaborationPlanner", "TaskCoordinator", "WorkflowEngine",
)


def scan_collab_offenders(root: Path) -> list:
    """
    扫一棵源码树，返回所有撞上 `FORBIDDEN_COLLAB_TYPES` 的标识符。

    🔒 按 **AST 标识符**比对，不做子串匹配 ——
       本项目真实发生过 `agent_directory` 含 `agent_d`、
       `seen_tracker` 含 `ack` 这类误报。
    """
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if set(path.parts) & {".venv", "__pycache__", "tests", ".git"}:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                names.append(node.name)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            for name in names:
                if name in FORBIDDEN_COLLAB_TYPES:
                    offenders.append(f"{path.name}:{name}")
    return offenders


class TestCollaborationGuards:
    """§三：Agent 角色只说「它擅长什么」，不能说「它什么时候必须找谁」。"""

    def test_no_coordinator_or_collaboration_engine_exists(self):
        offenders = scan_collab_offenders(PROJECT_ROOT)
        assert offenders == [], (
            "出现了中央协调者 / 协作引擎 —— §三 明令禁止：\n  "
            + "\n  ".join(offenders))

    def test_reverse_the_guard_actually_fires(self, tmp_path):
        """
        **反向测试**：把违规代码真的写进一棵源码树，
        用**同一个**扫描函数跑，确认守卫**真的会红**。

        🔒 本项目反复栽在「守卫假绿」上 —— 反向测试必须调用
           与正向测试**完全同一段**判定逻辑，而不是另写一个近似断言。
        """
        # 干扰项：形似但合法的名字（子串误报的老坑）
        clean = tmp_path / "clean"
        clean.mkdir()
        (clean / "ok.py").write_text(
            "class AgentDirectory:\n"
            "    def agent_d(self):\n"
            "        return seen_tracker_ack\n",
            encoding="utf-8")
        assert scan_collab_offenders(clean) == [], "合法名字被误报了"

        evil = tmp_path / "evil"
        evil.mkdir()
        (evil / "bad.py").write_text(
            "class CollaborationEngine:\n"
            "    def orchestrate(self):\n"
            "        return Coordinator\n",
            encoding="utf-8")
        found = " ".join(scan_collab_offenders(evil))
        assert "CollaborationEngine" in found, "守卫认不出注入的协作引擎 —— 它是假绿的"
        assert "Coordinator" in found

    def test_session_loop_is_rotation_not_dependency_order(self):
        """
        🔒 循环体必须是**轮转**，不能是「A 完成 → 通知 B」。

        AST 检查：`collab_session.py` 的循环直接遍历 `AGENTS`，
        而不是遍历某种「被上一个 Agent 修改过的」列表。
        """
        src = (EXP_DIR / "collab_session.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
        iterates_roster = any(
            isinstance(n.iter, ast.Name) and n.iter.id == "AGENTS"
            for n in loops)
        assert iterates_roster, "循环没有直接轮转 AGENTS —— 可能被编排了"

    def test_no_auto_forwarding_of_messages(self):
        """
        🔒 代码不得「A 做完就替它通知 B」。

        `collab_session.py` 里发消息只发生在一个地方，
        而且其内容 100% 来自 `intent["message"]`（Agent 自己写的）。
        """
        src = (EXP_DIR / "collab_session.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        sends = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "send"]
        assert len(sends) == 1, (
            f"有 {len(sends)} 处发消息 —— 应当只有一处（由 Agent 的意图驱动）")


# ================================================================
# §十三：凭据预检不能被这次接线破坏
# ================================================================
class TestCredentialPreflight:
    """`Brain.preflight_credential()` 保留，且**不含厂商名分支**。"""

    def test_provider_declares_whether_it_needs_a_key(self):
        from llm.router import LLMRouter

        router = LLMRouter({"llm": {"default_provider": "mock",
                                    "providers": {"mock": {"enabled": True}}}})

        assert router.requires_api_key("mock") is False
        assert router.requires_api_key("deepseek") is True

    def test_unknown_provider_fails_closed(self):
        """🔒 查不到的 provider 一律当作「需要 key」，不能反过来。"""
        from llm.router import LLMRouter

        router = LLMRouter({"llm": {"default_provider": "mock"}})

        assert router.requires_api_key("因为拼错而不存在的 provider") is True

    def test_brain_has_no_provider_name_branch(self):
        """
        **反向测试的另一半**：`agents/brain.py` 里不许出现
        与任何 provider 名字的**比较**。

        🔒 这曾经真的红过：`preflight_credential()` 第一版写的是
           `if provider == "mock"` —— 那是厂商绑定。
           修法是让 Provider 自己用 `requires_api_key` 声明。
        """
        tree = ast.parse((PROJECT_ROOT / "agents" / "brain.py")
                         .read_text(encoding="utf-8"))
        known = {"mock", "deepseek", "doubao", "grok",
                 "bailian_qwen", "bailian_glm", "openai_compatible"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for comparator in node.comparators:
                if (isinstance(comparator, ast.Constant)
                        and isinstance(comparator.value, str)):
                    assert comparator.value.lower() not in known, (
                        f"brain.py 里出现了和 {comparator.value!r} 的比较")


# ================================================================
# 模板守卫：Prompt 的占位符必须真的能被填满
# ================================================================
class TestPromptTemplateIsComplete:
    """
    **实测抓到的 bug**：`collab_session.PROMPT` 里写着 `{ws}`，
    但 `_one_turn` 调 `.format()` 时没传 `ws=` ——
    于是第一轮第一次调用就 `KeyError: 'ws'`，整场 Session 直接崩掉。

    🔒 这类「模板和调用点对不上」的问题**不会**被任何业务测试抓到：
       它只在真实跑起来的那一刻才炸。
          所以要有一条**专门比对模板占位符与实参**的测试。
    """

    @staticmethod
    def _template_fields(template: str) -> set:
        """抽出模板里真正的占位符（`{{` / `}}` 是转义，不算）。"""
        import string
        return {name for _, name, _, _ in string.Formatter().parse(template)
                if name is not None and name != ""}

    def test_all_placeholders_are_supplied(self):
        src = (EXP_DIR / "collab_session.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        template = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PROMPT"
                    for t in node.targets):
                template = ast.literal_eval(node.value)
        assert template, "找不到 PROMPT 模板"

        # 找 `.format(...)` 调用，取出实际传进去的关键字
        supplied = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "format"):
                continue
            supplied |= {kw.arg for kw in node.keywords if kw.arg}

        fields = self._template_fields(template)
        missing = fields - supplied
        assert not missing, (
            f"PROMPT 里的占位符 {sorted(missing)} 没有被 .format() 填上 —— "
            f"真实运行时会 KeyError")

    def test_reverse_the_template_guard_fires(self):
        """**反向测试**：造一个真的缺参数的模板，确认上面那条判定会红。"""
        template = "任务：{topic}\nWorkspace：{ws}\n"
        supplied = {"topic"}
        missing = self._template_fields(template) - supplied
        assert missing == {"ws"}, "模板守卫认不出缺参数 —— 它是假绿的"


# ================================================================
# 🔴 空产出 vs 主动沉默 —— 判据已经上移到 Provider 契约
# ================================================================
class TestEmptyOutputDelegatesToProviderContract:
    """
    **本类是一次「前提过期后的迁移」，不是新增。**

    原先这里有一个本地判据 `is_empty_completion(content, output_tokens)`
    （空 content **且** 花了 output token 才算失败）。

    它救过急，但**位置错了**：「content 空不空」是 Provider 输出契约的事，
    不该由每个调用方各自猜一遍 —— 第二个调用方忘了写，同一个 Bug 就回来了。
    现在已经上移到 `LLMResponse.output_status`（见 `llm/base.py`）。

    🔒 迁移带来的**语义变化**（刻意承认，不是偷偷改）：
       旧判据要求 `output_tokens > 0`，所以「空 content + 0 token」
       当时被算作「主动沉默」。
       新契约**不看 token** —— 空就是 EMPTY_CONTENT。
       契约描述「拿到了什么」；「为什么没拿到」是
       `reasoning_only` / `finish_reason` 的诊断职责。
    """

    def test_delegates_to_llm_response_contract(self):
        import collab_session as cs
        from llm.base import (
            OUTPUT_EMPTY_CONTENT, OUTPUT_VALID_CONTENT, LLMResponse)

        empty = LLMResponse(content="", provider="p", model="m")
        full = LLMResponse(content="hello", provider="p", model="m")

        assert empty.output_status == OUTPUT_EMPTY_CONTENT
        assert full.output_status == OUTPUT_VALID_CONTENT
        assert cs.is_empty_output(empty) is True
        assert cs.is_empty_output(full) is False

    def test_blank_content_counts_as_empty(self):
        import collab_session as cs
        from llm.base import LLMResponse

        assert cs.is_empty_output(
            LLMResponse(content="   \n ", provider="p", model="m")) is True

    def test_reverse_the_delegation_fires(self):
        """
        **反向测试**：确认 `is_empty_output` 真的在看契约，
        而不是恒返回 False（那正是「守卫假绿」的形状）。
        """
        import collab_session as cs
        from llm.base import LLMResponse

        # Session 1 里 world_builder 第 2 轮的真实形状
        observed = LLMResponse(content="", provider="deepseek",
                               model="deepseek-flash", output_tokens=4348,
                               reasoning="（很长的推理）")
        assert cs.is_empty_output(observed) is True

        # editor 第 1 轮有真实内容，不能被误判
        assert cs.is_empty_output(
            LLMResponse(content="x" * 1521, provider="deepseek",
                        model="deepseek-flash", output_tokens=4358)) is False


# ================================================================
# 🔴 空产出不得覆盖共享生产资料（实测事故）
# ================================================================
class TestEmptyContentMustNotOverwriteSharedArtifacts:
    """
    真实实验 `delegation_experiment --run 3` 的事故：

        Provider 的 LLM 调用返回 `EMPTY_CONTENT`（推理模型把 4096 token
        全用在 reasoning 上），而 `apply_write()` **照样写入** ——
        把 `editor_notes.md` 从有内容覆盖成 **0 字节**，
        而且 revision 从 2 涨到 3，看起来像一次**成功**的写入。

    这与本项目反复栽的「接口成功掩盖语义 no-op」是同一类，
    只不过这次它**真的毁掉了共享生产资料**。
    """

    def test_empty_write_is_refused(self, ws_rig):
        import shared_workspace as sw

        _h, gw, _prov = ws_rig
        sw.apply_write(gw, W, AUTHOR, "manuscript.md", "有内容的正文")
        before = gw.read(W, "/manuscript.md", AUTHOR).result["revision"]

        res = sw.apply_write(gw, W, AUTHOR, "manuscript.md", "")

        assert res["ok"] is False
        assert res["error"]["code"] == "EMPTY_CONTENT_NOT_WRITTEN"
        after = gw.read(W, "/manuscript.md", AUTHOR)
        assert after.result["revision"] == before, "空写入仍然涨了 revision"
        assert after.result["content"] == "有内容的正文", "已有内容被覆盖了"

    def test_whitespace_only_is_also_refused(self, ws_rig):
        import shared_workspace as sw

        _h, gw, _prov = ws_rig
        assert sw.apply_write(gw, W, AUTHOR, "manuscript.md", "   \n ")[
            "ok"] is False

    def test_explicit_allow_empty_still_works(self, ws_rig):
        """确实想清空时，必须**显式说出来**。"""
        import shared_workspace as sw

        _h, gw, _prov = ws_rig
        res = sw.apply_write(gw, W, AUTHOR, "manuscript.md", "",
                             allow_empty=True)
        assert res["ok"] is True

    def test_reverse_the_guard_fires(self, ws_rig):
        """**反向测试**：非空内容照常写入，不被守卫误杀。"""
        import shared_workspace as sw

        _h, gw, _prov = ws_rig
        assert sw.apply_write(gw, W, AUTHOR, "manuscript.md", "x")["ok"] is True
