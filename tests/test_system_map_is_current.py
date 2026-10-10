"""
🔴 守卫：`SYSTEM_MAP.md` 必须与**代码里真正注册的动作**一致
==============================================================

【为什么需要它（用户 2026-10-10 的决定）】

    「决定B，要更新 SYSTEM_MAP，并且以后修改代码增加功能时需要更新它。」

【为什么必须靠守卫，而不是靠人记得】

    实测：环境**注册了 13 个动作**，而 `SYSTEM_MAP.md` 只列了 **4 个** ——
    少了 9 个，其中 4 个是 `private_*`。

    更糟的是它同时写着：

        Private Workspace … 只有你能读写（**但当前还没有动作通道**）

    ⇒ 一份**直接与事实相反**的公共资料。
       而实测 Agent **真的会去读它**（1010-1009 运行：3 个 turn 都调了
       `public_query`；`agent_a` 的推理原文是「顺手读 SYSTEM_MAP
       **摸清验收链的规则**」—— 而里面根本没有验收规则）。

    ⚠️ 它不会让任何测试变红：动作确实注册了、也确实能跑，
       只是**没有 Agent 会从这份公共资料里知道它能用**。
       这就是「注册了但没投递 = 那个能力等于不存在」。

【这个文件钉住四件事】

    ① 注册了的动作，SYSTEM_MAP 里**必须**有（防「注册了没说」）
    ② SYSTEM_MAP 里写的动作，必须**真的注册了**（防「说了没有」）
    ③ 执行器从 `parameters` 读的**每一个** Agent 能控制的键，
       都要在表里写出来（防漏参数 —— 实测 `write_mode` 就这么漏过）
    ④ 它真的**够得着**（`public_query` 能读到它）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.action_registry_helpers import (                     # noqa: E402
    action_type_to_executor, executor_param_keys, registered_action_types,
)

SYSTEM_MAP = ROOT / "resources" / "public" / "SYSTEM_MAP.md"

#: 表格行：`| \`action_type\` | 作用 | 参数 |`
_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|(.*)\|\s*$")

#: 反引号里的标识符。
_TICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")

#: 🔴 **由环境拥有、Agent 控制不了的参数** —— 不写进 Agent 面向的资料。
#:
#:     `resource_handoff` 的 `task_id` 是 Workspace 审计记录上的标签。
#:     `ResourceHandoffExecutor` 会从 `parameters` 读它，但
#:     **组合根 `_bind_gw` 会把它覆盖成当前 TASK_ID** ——
#:     所以 Agent 给的值**无效**，它也就不是 Agent 面向的参数。
#:
#:     🔒 这不是"白名单免检"：`test_the_env_really_owns_the_handoff_task_id`
#:        用**真实派发**证明覆盖真的发生。豁免是**挣来的**。
ENV_OWNED_PARAMS = {
    "resource_handoff": {"task_id"},
}


def _text() -> str:
    return SYSTEM_MAP.read_text(encoding="utf-8")


def _sections(text: str) -> list:
    """按 `## ` 切成 [(标题, [行…])]。"""
    out, title, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                out.append((title, buf))
            title, buf = line, []
        elif title is not None:
            buf.append(line)
    if title is not None:
        out.append((title, buf))
    return out


def declared_actions(text: str = None) -> dict:
    """
    从「你能发起的动作」那一节里扫出 `{action_type: [表里写的参数名…]}`。

    🔒 只认**表格行**（`| \\`xxx\\` | … |`）—— 不认正文里出现的词。
       否则注释里提一句也算，守卫就退化成
       「文件里有没有这个词」（本项目记录过的假绿 bug 类）。

    🔒 找不到那一节就**报错**，不返回空 dict ——
       返回空会让所有断言变成空转。
    """
    text = _text() if text is None else text
    for title, lines in _sections(text):
        if "动作" not in title:
            continue
        out = {}
        for line in lines:
            m = _ROW.match(line.rstrip())
            if m:
                out[m.group(1)] = sorted(set(_TICKED.findall(m.group(2))))
        return out
    raise AssertionError(
        "SYSTEM_MAP.md 里找不到「你能发起的动作」那一节 —— "
        "结构被改了，这个守卫已经失效（**不是**通过）")


# ============================================================
# ①② 动作集合必须两边一致
# ============================================================
def test_every_registered_action_is_in_the_system_map():
    registered = registered_action_types()
    assert len(registered) >= 10, (
        f"只扫到 {sorted(registered)} 个动作 —— 守卫可能没在工作")

    listed = set(declared_actions())
    missing = sorted(registered - listed)

    assert not missing, (
        f"这些动作**注册了但 SYSTEM_MAP 没写**：{missing}\n"
        f"⇒ Agent 读公共资料时看不到它们 = 那些能力等于不存在。\n"
        f"   请更新 {SYSTEM_MAP.relative_to(ROOT)}")


def test_no_action_is_listed_that_is_not_registered():
    """反向：公共资料不许写一个**根本不存在**的动作 —— 那是文档在说谎。"""
    listed = set(declared_actions())
    phantom = sorted(listed - registered_action_types())

    assert not phantom, (
        f"SYSTEM_MAP 里写了这些动作，但环境**没有注册**：{phantom}\n"
        f"⇒ Agent 会照着去调用，然后拿到 unknown_action")


# ============================================================
# ③ 参数：执行器读的每一个键都要写出来
# ============================================================
def test_every_agent_facing_parameter_is_documented():
    """
    🔴 实测漏过参数：`workspace_write` 的 `write_mode` 曾只写在 schema 里，
       Agent 漏填 ⇒ `bad_mode` ⇒ 那一次机会用掉了（见
       `test_prompt_delivers_registered_actions.py` 的记录）。

       参数漏写 = Agent 不知道该填 = 动作大概率失败。
    """
    mapping = action_type_to_executor()
    declared = declared_actions()

    problems = []
    for action, cls_name in sorted(mapping.items()):
        real = executor_param_keys(cls_name)
        owned = ENV_OWNED_PARAMS.get(action, set())
        agent_facing = real - owned
        undocumented = sorted(agent_facing - set(declared.get(action, [])))
        if undocumented:
            problems.append(
                f"  `{action}`（{cls_name}）读了 {undocumented}，"
                f"但表里只写了 {declared.get(action)}")

    assert not problems, (
        "SYSTEM_MAP 的动作表漏了参数：\n" + "\n".join(problems))


# ============================================================
# ④ 它真的够得着
# ============================================================
def test_agents_can_actually_read_it():
    """一份 Agent 读不到的公共资料，等于不存在。"""
    from services.public_reference import PublicReference

    pub = PublicReference()
    assert "SYSTEM_MAP.md" in pub.list_names(), (
        "公共资料区里没有 SYSTEM_MAP.md —— Agent 查不到它")

    got = pub.query("SYSTEM_MAP.md")
    assert got["ok"] is True
    assert got["chars"] > 500, "内容太短 —— 可能被写坏了"


# ============================================================
# 反向测试 —— 每条守卫都要证明自己**真的会红**
# ============================================================
class TestTheGuardsAreNotVacuous:
    def test_the_action_set_guard_catches_a_missing_action(self):
        """
        删掉表里的**那一行** ⇒ 必须解析不到它。

        🔒 只删表格行 —— 不能全文 `replace`：
           动作名在前言里也会出现（§1 就提了 `assign_acceptance`），
           全文换第一个会把前言换掉、表格行还在，测试就假绿了。
           （第一版就是这么写的，被这条自己抓住。）
        """
        text = _text()
        victim = sorted(declared_actions())[0]
        broken = re.sub(rf"^\|\s*`{victim}`.*$", "", text,
                        count=1, flags=re.M)

        assert victim not in declared_actions(broken), (
            "删掉表里的一行，解析结果却没变 —— 解析坏了")

    def test_the_action_set_guard_catches_a_phantom_action(self):
        """插一行**不存在**的动作 ⇒ 解析结果里必须出现它。"""
        text = _text()
        patched = text.replace(
            "\n\n**消息类型**",
            "\n| `teleport` | 不存在的动作 | `x` |\n\n**消息类型**", 1)

        assert "teleport" in declared_actions(patched)

    def test_the_parser_only_trusts_table_rows(self):
        """正文里提一句不算 —— 必须是真的表格行。"""
        fake = (
            "## 2. 你能发起的动作\n\n"
            "正文里提到 `teleport` 这个词，但它不是表格行。\n\n"
            "| `real_action` | 作用 | `p` |\n"
        )
        assert declared_actions(fake) == {"real_action": ["p"]}

    def test_a_missing_section_is_an_error_not_an_empty_pass(self):
        with pytest.raises(AssertionError):
            declared_actions("# 系统地图\n\n没有动作那一节。\n")

    def test_the_param_guard_catches_a_missing_parameter(self):
        """把 `write_mode` 从表里删掉 ⇒ 必须被判为「漏参数」。"""
        mapping = action_type_to_executor()
        cls = mapping["workspace_write"]
        real = executor_param_keys(cls) - ENV_OWNED_PARAMS.get(
            "workspace_write", set())
        assert "write_mode" in real, "前提不成立"

        declared = declared_actions()
        declared["workspace_write"] = [
            p for p in declared["workspace_write"] if p != "write_mode"]

        assert "write_mode" in (real - set(declared["workspace_write"])), (
            "删掉 write_mode 后没被判为漏参数 —— 检查逻辑坏了")


# ============================================================
# 豁免是**挣来的** —— 证明环境真的覆盖了 task_id
# ============================================================
def test_the_env_really_owns_the_handoff_task_id(tmp_path):
    """
    `resource_handoff` 的 `task_id` 被豁免了（不写进 Agent 资料）——
    理由是**组合根会覆盖它**。这条用**真实派发**证明那个覆盖真的发生。

    🔒 不证明的话，豁免就变成白名单：一个 Agent 真能设的参数
       却从表里消失，Agent 反而更不知道。
    """
    import os
    import tempfile

    from actions.intent import ActionIntent
    from experiments.heterogeneous_production import production as P
    from main import build_runtime

    rt = build_runtime(db_path=os.path.join(tempfile.mkdtemp(), "h.db"),
                       agent_ids=["agent_a", "agent_b"])
    try:
        class _B:
            def __getattr__(self, name):
                return lambda *a, **kw: None

        dispatcher, _ = P._action_seam(rt, _B())

        # 🔒 Workspace 的 `is_participant` 查的是 **TaskStore** ——
        #    所以必须先有一条真的 Task，否则谁都建不了目录。
        from core.task import Task

        rt.create_task(Task.create(task_id=P.TASK_ID, creator="agent_a",
                                   description="d", workspace_id=P.WS_ID,
                                   participants=["agent_a", "agent_b"]))

        # 🔒 必须有一份**真实存在**的资源才交接得动 ——
        #    否则 `handoff_resource` 在碰到 gateway 之前就返回 not_found，
        #    spy 根本不会被调用（第一版就是这么假绿的）。
        rt.workspace_gateway.ensure_workspace(P.WS_ID, "agent_a")
        made = dispatcher.dispatch(ActionIntent(
            intent_id="c", agent_id="agent_a",
            action_type="resource_create", parameters={"path": "d"}))
        assert made.success is True, f"建目录失败：{made.error}"

        captured = {}

        # 🔒 签名必须与 `WorkspaceGateway.handoff` 一致 ——
        #    写错了会抛 TypeError，被执行器吞成结构化失败，
        #    于是 `captured` 是空的（第一版就是这么假绿的）。
        def _spy(workspace_id, path, from_agent, to_agent, actor, **kw):
            captured.update(kw)
            captured["_path"] = path
            captured["_actor"] = actor
            return {"ok": False, "code": "spy", "message": "只观察"}

        rt.workspace_gateway.handoff = _spy

        dispatcher.dispatch(ActionIntent(
            intent_id="i", agent_id="agent_a",
            action_type="resource_handoff",
            parameters={"path": "d", "to": "agent_b",
                        # 🔴 Agent **尝试**伪造一个别的 task_id
                        "task_id": "TASK-OF-SOMEONE-ELSE"}))

        assert captured, (
            "gateway.handoff 根本没被调用 —— 这条测试没测到东西"
            "（资源没建好 / 路径不对）")
        assert captured.get("task_id") == P.TASK_ID, (
            f"Agent 指定的 task_id 没被覆盖（拿到 {captured.get('task_id')!r}）"
            f"—— 它能把责任交接记到别的 Task 名下")
    finally:
        rt.close()
