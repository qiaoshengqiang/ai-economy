"""
Workspace **读**动作 —— 测试
================================

【它解决的真实问题】

    `novel_reader` 在 `/reader/chapter1_reader_acceptance.md` 里写道：

        「我目前只能看到 /manuscript/chapter_001.md 的路径和锁定状态，
          无法读取正文，所以不能负责任地给出通过。」

    它**拒绝给自己读不到的东西盖章** —— 那是对的行为。缺的是通道：
    Gateway 早有 `read()`，但「Agent 能发起的动作」里只有 `write`。

【本文件要钉住的三件事】

    ① **读不受 Write Lock 影响** —— 锁挡的是修改与删除，不是阅读。
       这是设计（ADR-0021），不是遗漏。
    ② **执行器是叶子层** —— `actions/` 结构上不许 import `services/`。
       以前这条只是文档约定，**没有守卫**；本次补上并配反向测试。
    ③ **它只执行、不决策** —— 不挑文件、不重试、不改路径。

🔒 全部测试**不调用任何 LLM**。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                  # noqa: E402

load_dotenv(ROOT / ".env")


# ======================================================================
# ① 执行器本身（注入假 read_fn，不碰 Gateway）
# ======================================================================
def _executor(fn, **kw):
    from actions.workspace_read import WorkspaceReadExecutor

    return WorkspaceReadExecutor(fn, **kw)


def _intent(path: str, agent: str = "novel_reader"):
    from actions.intent import ActionIntent

    return ActionIntent(action_type="workspace_read",
                        parameters={"path": path}, agent_id=agent)


def test_action_type_is_explicit():
    from actions.workspace_read import ACTION_WORKSPACE_READ, WorkspaceReadExecutor

    assert ACTION_WORKSPACE_READ == "workspace_read"
    assert WorkspaceReadExecutor.action_types == frozenset({"workspace_read"})


def test_action_type_is_not_globally_builtin():
    """
    🔒 读**不**登记成全局内置动作。

    `BUILTIN_ACTION_TYPES` 是「任何调用方都自动具备」的集合 ——
    把读登记进去，等于替**所有**调用方决定它们能读东西。
    `workspace_write` 也不在里面；读写都应由组合根按需注册。
    """
    from actions.intent import BUILTIN_ACTION_TYPES
    from actions.workspace_read import ACTION_WORKSPACE_READ

    assert ACTION_WORKSPACE_READ not in BUILTIN_ACTION_TYPES


def test_requires_callable_read_fn():
    from actions.workspace_read import WorkspaceReadExecutor

    with pytest.raises(ValueError):
        WorkspaceReadExecutor(None)
    with pytest.raises(ValueError):
        WorkspaceReadExecutor("not callable")             # type: ignore[arg-type]


def test_success_carries_the_content():
    seen = {}

    def fake_read(gateway, workspace_id, agent_id, path):
        seen.update(gateway=gateway, workspace_id=workspace_id,
                    agent_id=agent_id, path=path)
        return {"ok": True, "path": path, "revision": "R3",
                "chars": 5, "content": "正文内容"}

    ex = _executor(fake_read, gateway="GW", workspace_id="WS-1")
    res = ex.execute(_intent("manuscript/chapter_001.md"))

    assert res.success is True
    assert res.error is None
    assert res.metadata.get("code") is None, "成功不该编一个错误码"
    assert res.output["content"] == "正文内容", "内容没带出来 = 读没发生"
    assert res.output["revision"] == "R3"
    assert res.action_type == "workspace_read"
    # 🔒 注入的依赖被原样透传，本层不认识它
    assert seen == {"gateway": "GW", "workspace_id": "WS-1",
                    "agent_id": "novel_reader",
                    "path": "manuscript/chapter_001.md"}


def test_failure_is_structured_not_an_exception():
    def fake_read(*_a, **_kw):
        return {"ok": False, "path": "x.md", "code": "not_found",
                "message": "文件不存在", "gateway_code": "RESOURCE_NOT_FOUND"}

    res = _executor(fake_read).execute(_intent("x.md"))

    assert res.success is False
    assert "文件不存在" in res.error
    assert res.metadata["code"] == "not_found"
    assert res.metadata["gateway_code"] == "RESOURCE_NOT_FOUND"
    assert res.output is None, "失败时 output 必须留空 —— 否则像读到了东西"


def test_a_crashing_read_fn_still_returns_a_result():
    """§十七：执行失败是 ActionResult，不是 Runtime 崩溃。"""
    def boom(*_a, **_kw):
        raise RuntimeError("gateway 炸了")

    res = _executor(boom).execute(_intent("x.md"))

    assert res.success is False
    assert "RuntimeError" in res.error and "gateway 炸了" in res.error
    assert res.metadata["code"] == "executor_error"


def test_non_dict_return_is_rejected():
    res = _executor(lambda *a, **k: "裸字符串").execute(_intent("x.md"))
    assert res.success is False
    assert res.metadata["code"] == "invalid_read_result"


def test_does_not_invent_or_retry_a_path():
    """
    🔒 只执行、不决策：路径为空就原样传空，**不**替 Agent 挑一个文件，
       也**不**因为失败就换路径重试。
    """
    calls = []

    def fake_read(gateway, workspace_id, agent_id, path):
        calls.append(path)
        return {"ok": False, "code": "not_found", "message": "没有"}

    ex = _executor(fake_read)
    ex.execute(_intent(""))

    assert calls == [""], f"不该重试或换路径，实际调用了：{calls}"


# ======================================================================
# ② 叶子层：actions/ 结构上不许 import services/
# ======================================================================
def _services_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "services" or mod.startswith("services."):
                hits.append(f"{path.name}:{node.lineno} from {mod}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "services" or a.name.startswith("services."):
                    hits.append(f"{path.name}:{node.lineno} import {a.name}")
    return hits


def test_actions_layer_does_not_import_services():
    """
    🔴 这是补上的守卫。

    `actions/__init__.py` 的 docstring 一直声称叶子层属性
    「由两个 AST 守卫 + 源码守卫共同保证」—— 但那两个守卫只覆盖
    `selfmodel` 与 `DecisionEngine`，**services 这条从来没被守过**。

    而 `workspace_read.py` 正是最容易写错的地方：
    顺手 `from services.artifacts import read_artifact` 就把
    「叶子层」变成「认识数据库的一层」。
    """
    offenders = []
    for py in sorted((ROOT / "actions").glob("*.py")):
        offenders += _services_imports(py)
    assert not offenders, (
        f"actions/ 依赖了 services/ —— 它就不再是叶子层了：{offenders}\n"
        f"正确做法：只拿一个**注入的** read_fn / write_fn。")


def test_leaf_layer_guard_really_goes_red(tmp_path):
    """
    🔒 反向测试：把违规写进一个临时文件，确认上面那条守卫**真的**能发现。

    不这么做的话，守卫可能是绿的但什么都没测（本项目踩过两次）。
    """
    bad = tmp_path / "fake_action.py"
    bad.write_text("from services.artifacts import read_artifact\n",
                   encoding="utf-8")
    assert _services_imports(bad), "守卫没认出 `from services... import`"

    bad2 = tmp_path / "fake_action2.py"
    bad2.write_text("import services.artifacts as A\n", encoding="utf-8")
    assert _services_imports(bad2), "守卫没认出 `import services...`"

    ok = tmp_path / "ok_action.py"
    ok.write_text("from typing import Any\n", encoding="utf-8")
    assert not _services_imports(ok), "守卫误报了无关模块"


# ======================================================================
# ③ 真 Gateway：读**不受 Write Lock 影响**，且验收人真的能读到
# ======================================================================
W = "WS-READ"
AUTHOR = "novel_author"
READER = "novel_reader"
OUTSIDER = "agent_e"

MANUSCRIPT = "manuscript/chapter_001.md"


@pytest.fixture
def ws():
    """
    真实 Gateway + 真实 Workspace，但**内存库**。

    🔒 用 `WorkspaceHarness`（测试脚手架的既有入口），它注入
       `is_participant` —— 所以不需要建 Runtime、也不需要建 Task。
       这样这些测试只验证 **Gateway/服务层** 的行为，不牵进 LLM 或经济。
    """
    from tests.workspace_harness import WorkspaceHarness

    h = WorkspaceHarness.in_memory(
        participants={W: {AUTHOR, READER}},
        agents={AUTHOR, READER, OUTSIDER})
    try:
        gw = h.gateway()
        gw.ensure_workspace(W, AUTHOR)
        yield gw
    finally:
        h.close()


def _write(gw, path, who, body):
    from services.artifacts import MODE_CREATE, write_artifact

    return write_artifact(gw, W, who, path, body, write_mode=MODE_CREATE)


def test_reader_can_read_another_agents_artifact(ws):
    """🔴 这条就是验收卡死的那个缺口 —— 验收人必须能读到正文。"""
    from services.artifacts import read_artifact

    body = "# 第一章 废脉少年\n\n晨钟三响，震散了青岚峰最后一缕雾气。\n（第一章完）"
    got = _write(ws, MANUSCRIPT, AUTHOR, body)
    assert got.get("ok"), got

    res = read_artifact(ws, W, READER, MANUSCRIPT)

    assert res["ok"] is True, f"验收人读不到：{res}"
    assert "晨钟三响" in res["content"]
    assert res["chars"] > 0
    assert res["revision"] == got["revision"]


def test_read_is_not_blocked_by_the_write_lock(ws):
    """
    🔒 核心语义：**Write Lock 挡的是修改与删除，不是阅读。**

    作者写完 → 锁在作者手上 → 验收人**仍然必须能读到**。
    """
    from services.artifacts import read_artifact

    _write(ws, MANUSCRIPT, AUTHOR, "锁着的正文")

    # 先确认前提成立：锁**确实**在作者手上（而不是「压根没锁」）
    described = ws.list(W, "/manuscript", READER)
    entries = (described.result or {}).get("entries", [])
    holder = next((e.get("effective_lock_holder") for e in entries
                   if e.get("name") == "chapter_001.md"), None)
    assert holder == AUTHOR, f"前提不成立，锁不在作者手上：{entries}"

    res = read_artifact(ws, W, READER, MANUSCRIPT)
    assert res["ok"] is True, (
        "🔴 读被写锁挡住了 —— 但锁只该挡修改/删除（ADR-0021）")


def test_non_participant_still_cannot_read(ws):
    """读不查锁，但**仍然**要求是 Task 参与者（设计：Read 靠 Participation）。"""
    from services.artifacts import read_artifact

    _write(ws, MANUSCRIPT, AUTHOR, "正文")

    res = read_artifact(ws, W, OUTSIDER, MANUSCRIPT)
    assert res["ok"] is False
    assert res["code"] == "not_participant", res


def test_missing_file_is_a_classified_failure(ws):
    from services.artifacts import read_artifact

    res = read_artifact(ws, W, READER, "manuscript/does_not_exist.md")
    assert res["ok"] is False
    assert res["code"] == "not_found", res


def test_absolute_path_is_rejected(ws):
    """读不扩张 Path Policy —— 逻辑路径规则与写一致。"""
    from services.artifacts import read_artifact

    res = read_artifact(ws, W, READER, "/etc/passwd")
    assert res["ok"] is False
    assert res["code"] == "bad_path", res


def test_read_result_shape_matches_write(ws):
    """
    🔒 返回结构必须与 `write_artifact` **统一** ——
       否则两个执行器的翻译逻辑就会分叉（那正是耦合的来源）。

    两者的**公共骨架**：ok / path / revision / chars
    各自多出来的字段，都能说清为什么：

        写多出 → `mode`（读没有「写入模式」这个概念）
                 `previous_revision`（读没有「上一个版本」这个概念）
        读多出 → `content`（没有它就等于没读）
    """
    from services.artifacts import read_artifact

    w = _write(ws, MANUSCRIPT, AUTHOR, "正文")
    r = read_artifact(ws, W, READER, MANUSCRIPT)

    common = {"ok", "path", "revision", "chars"}
    assert common <= set(w), f"写的返回缺公共字段：{sorted(common - set(w))}"
    assert common <= set(r), f"读的返回缺公共字段：{sorted(common - set(r))}"

    assert set(r) - set(w) == {"content"}, (
        f"读多出的字段应当只有 content，实际：{sorted(set(r) - set(w))}")
    assert set(w) - set(r) == {"mode", "previous_revision"}, (
        f"写多出的字段应当只有 mode / previous_revision，"
        f"实际：{sorted(set(w) - set(r))}")

    # 成功都没有错误码 —— 显式缺省，不编一个 "ok" 出来
    assert w.get("code") is None and r.get("code") is None


# ======================================================================
# ④ 实验侧接线：Agent 能**表达**读，且解析器不枚举动作类型
# ======================================================================
PROD = ROOT / "experiments" / "heterogeneous_production" / "production.py"


def _prod():
    from experiments.heterogeneous_production import production as P

    return P


def test_parse_intent_recognizes_a_generic_action():
    """🔴 Agent 的 JSON 契约里，读**能被表达出来** —— 这是以前缺的一环。"""
    raw = ('{"assessment":"我要核对正文",'
           '"action":{"type":"workspace_read",'
           '"parameters":{"path":"manuscript/chapter_001.md"}}}')
    got = _prod().parse_intent(raw)

    assert got["parsed"] is True
    assert got["action"] == {
        "type": "workspace_read",
        "parameters": {"path": "manuscript/chapter_001.md"}}


def test_parser_does_not_enumerate_action_types():
    """
    🔒 **不过于耦合的关键**：解析器不认识具体有哪些动作。

    它只把 `type` 当**自由字符串**透传 —— 合法性由「组合根注册了
    哪些执行器」决定（没注册 → dispatcher 返回明确的失败）。

    一旦解析器开始枚举/白名单，每加一个动作都要改解析器 ——
    那正是「加一个 read 要动六处」的来源。
    """
    raw = ('{"action":{"type":"some_future_action_v9",'
           '"parameters":{"whatever":1}}}')
    got = _prod().parse_intent(raw)

    assert got["action"]["type"] == "some_future_action_v9", (
        "解析器把未知动作类型拒掉了 —— 它开始替 Agent 划定能力边界了")
    assert got["action"]["parameters"] == {"whatever": 1}


def test_parser_ignores_a_malformed_action():
    for bad in ('{"action":"workspace_read"}',
                '{"action":{}}',
                '{"action":{"parameters":{}}}',
                '{"action":null}'):
        got = _prod().parse_intent(bad)
        assert got["action"] is None, f"不该把 {bad} 认成动作"


def test_action_absent_is_none_not_an_error():
    got = _prod().parse_intent('{"assessment":"继续"}')
    assert got["action"] is None
    assert got["parsed"] is True


def test_action_type_falls_back_to_action_type_key():
    """`build_intent` 同时认 `type` 与 `action_type` —— 解析器保持一致。"""
    got = _prod().parse_intent(
        '{"action":{"action_type":"workspace_read","parameters":{}}}')
    assert got["action"]["type"] == "workspace_read"


def test_action_parameters_default_to_empty_dict():
    got = _prod().parse_intent('{"action":{"type":"workspace_read"}}')
    assert got["action"]["parameters"] == {}


def test_composition_root_registers_the_read_executor():
    """
    🔒 组合根（本实验的 `_action_seam`）必须**注册**读执行器。

    这是 AST 检查，不是行为检查 —— 但它是唯一能在不跑 LLM 的前提下
    钉住「这个实验确实把读交给了 Agent」的办法。
    """
    tree = ast.parse(PROD.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_action_seam"),
              None)
    assert fn is not None, "找不到 _action_seam"

    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "WorkspaceReadExecutor" in names, (
        "_action_seam 没有注册 WorkspaceReadExecutor —— "
        "Agent 又只剩写动作了")
    assert "WorkspaceWriteExecutor" in names, "写执行器丢了"


def test_prompt_states_the_read_capability_as_a_fact():
    """
    🔒 契约要把「有这个能力」讲清楚 —— 但只作为**环境事实**。

    同时断言它**没有**变成行为规定：不能出现「你必须读」
    「如果你不读就…」这类指令（§1：系统不替 Agent 决定）。
    """
    src = PROD.read_text(encoding="utf-8")
    assert "workspace_read" in src, "prompt 里没告诉 Agent 有读这个能力"
    for banned in ("你必须读", "一定要读", "必须先读", "你必须先读",
                   "你应当先读"):
        assert banned not in src, f"prompt 开始规定行为了：{banned!r}"


def test_prompt_forbids_dumping_prose_into_messages():
    """
    🔒 现有 prompt 明确写着「不要整篇复制进 message —— 正文属于 Workspace」。

       这条正好说明：**正文的正规通道是 Workspace，不是消息**。
       所以「让作者把正文贴进消息」那种做法不但违反 §1，
       连现有契约都不同意它。
    """
    src = PROD.read_text(encoding="utf-8")
    assert "不要整篇复制进 message" in src, (
        "这条约束被删掉了？它正是读动作存在的理由")
