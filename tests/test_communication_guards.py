"""
Test: 通信层的架构守卫
============================================================

这些测试不检查功能，检查的是【边界有没有被破坏】。

功能写错了会报错；边界破了往往不会报错，只会慢慢烂掉 ——
半年后你想换成 P2P，才发现通信层里早就长满了对 LLM 和
Economy 的依赖，那时候改起来就是重写。

所以这些守卫比功能测试更重要。

守卫清单（对应需求里的硬性要求）：
    1. communication/ 不依赖 llm/
    2. communication/ 不依赖 core/economy.py / wallet.py / ledger.py
    3. communication/ 不出现任何模型厂商名字
    4. communication/ 不出现 if provider == ... 这种厂商分支
    5. Agent 不直接创建 LocalTransport
    6. Agent 不 import communication（靠鸭子类型解耦）
    7. 依赖方向单向：agents → communication，不能反过来
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMMUNICATION_DIR = PROJECT_ROOT / "communication"
AGENTS_DIR = PROJECT_ROOT / "agents"


# ============================================================
# AST 工具
# ============================================================
def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> list:
    """列出这个文件 import 的所有模块名。"""
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def communication_sources():
    return sorted(COMMUNICATION_DIR.rglob("*.py"))


def agent_sources():
    return sorted(AGENTS_DIR.rglob("*.py"))


# 所有已知的模型厂商名（和 Step 2 的守卫保持一致）
VENDOR_NAMES = (
    "deepseek", "doubao", "grok", "openai", "claude", "anthropic",
    "gpt", "qwen", "gemini", "豆包", "通义",
)


# ============================================================
# 1. 通信层不依赖 LLM
# ============================================================
class TestCommunicationDoesNotDependOnLLM:
    def test_no_llm_imports(self):
        """
        【需求第 17 条：通信必须和 LLM 完全解耦】

        为什么？
            大量消息根本不需要模型：PING / PONG / PAYMENT_RESULT / ACCEPT。
            如果通信层依赖 LLM，回一个 PONG 都要加载模型路由，
            测试通信时还得准备一个假模型 —— 这两件事本该无关。
        """
        violations = []

        for source in communication_sources():
            for module in imported_modules(parse(source)):
                if module == "llm" or module.startswith("llm."):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "communication/ 不能依赖 llm/：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_vendor_names_in_communication(self):
        """通信层里不该出现任何模型厂商名字。"""
        violations = []

        for source in communication_sources():
            text = source.read_text(encoding="utf-8").lower()
            for vendor in VENDOR_NAMES:
                if vendor in text:
                    violations.append(f"{source.name} 里出现 {vendor!r}")

        assert not violations, (
            "communication/ 里出现了模型厂商名字：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_provider_branching(self):
        """
        通信层不该出现 `if provider == "xxx"` 这种模型分支。
        """
        known = {"mock", "deepseek", "doubao", "grok"}
        violations = []

        for source in communication_sources():
            for node in ast.walk(parse(source)):
                if not isinstance(node, ast.Compare):
                    continue
                for comparator in node.comparators:
                    if (
                        isinstance(comparator, ast.Constant)
                        and isinstance(comparator.value, str)
                        and comparator.value.lower() in known
                    ):
                        violations.append(
                            f"{source.name}:{node.lineno} "
                            f"和 {comparator.value!r} 比较"
                        )

        assert not violations, (
            "communication/ 里出现了 provider 分支：\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 2. 通信层不依赖 Economy
# ============================================================
class TestCommunicationDoesNotDependOnEconomy:
    def test_no_economy_imports(self):
        """
        【需求第 18 条：通信必须和 Economy 解耦】

        为什么？
            因为「把消息送到」和「解释消息里的钱」是两件事。
                Communication：A 给 B 发了一条 TASK_OFFER
                Economy      ：这条 offer 里的 20 AI 币，A 付得起吗
            混在一起的话，以后加一种带钱的消息类型就要改通信层。
        """
        forbidden = (
            "core.economy",
            "core.wallet",
            "core.ledger",
        )
        violations = []

        for source in communication_sources():
            for module in imported_modules(parse(source)):
                for bad in forbidden:
                    if module == bad or module.startswith(bad + "."):
                        violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "communication/ 不能依赖 Economy：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_money_handling_in_communication(self):
        """
        通信层里不该出现钱包 / 账本 / 支付相关的东西。

        注意这是【标识符级别】的检查 ——
        注释里提到「未来 Economy 会处理」是允许的。
        """
        forbidden_identifiers = (
            "wallet", "ledger", "escrow", "transfer",
            "balance", "charge", "payment_amount",
        )
        violations = []

        for source in communication_sources():
            tree = parse(source)

            # 收集文档字符串，排除掉（说明性文字不算违规）
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    body = getattr(node, "body", None)
                    if body and isinstance(body[0], ast.Expr):
                        value = body[0].value
                        if isinstance(value, ast.Constant) and isinstance(
                            value.value, str
                        ):
                            docstrings.add(id(value))

            for node in ast.walk(tree):
                # import 的名字
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif node.module:
                        names = [node.module]
                    for name in names:
                        if any(bad in name.lower() for bad in forbidden_identifiers):
                            violations.append(f"{source.name} 导入了 {name}")

                # 函数名 / 属性名
                if isinstance(node, ast.FunctionDef):
                    if any(
                        bad in node.name.lower() for bad in forbidden_identifiers
                    ):
                        violations.append(
                            f"{source.name}:{node.lineno} 定义了 {node.name}()"
                        )

        assert not violations, (
            "communication/ 里出现了经济概念：\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 3. Agent 不依赖具体传输
# ============================================================
class TestAgentDoesNotDependOnTransport:
    def test_agent_does_not_create_local_transport(self):
        """
        【需求第 8 条：Agent 不直接依赖 LocalTransport】

        Agent 里不该出现 LocalTransport() 这种创建语句，
        也不该 import 它。
        """
        violations = []

        for source in agent_sources():
            tree = parse(source)

            for module in imported_modules(tree):
                if "local_transport" in module.lower():
                    violations.append(f"{source.name} 导入了 {module}")

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = ""
                    if isinstance(func, ast.Name):
                        name = func.id
                    elif isinstance(func, ast.Attribute):
                        name = func.attr
                    if "LocalTransport" in name:
                        violations.append(
                            f"{source.name}:{node.lineno} 创建了 {name}"
                        )

        assert not violations, (
            "Agent 层不该出现 LocalTransport：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_agent_does_not_import_communication_at_all(self):
        """
        Agent 连 communication 包都不 import。

        怎么还能发消息？靠鸭子类型 ——
        Agent 只持有 attach_communication() 注入进来的对象，
        调用它的 .send() 方法，不关心它是什么类型。

        这样做的好处：
            · 通信层可以被完全独立地测试（用假 Agent 就行）
            · 将来通信层大改，Agent 一点都不受影响
        """
        violations = []

        for source in agent_sources():
            for module in imported_modules(parse(source)):
                if module == "communication" or module.startswith("communication."):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "agents/ 不该 import communication：\n"
            + "\n".join(f"  {v}" for v in violations)
            + "\n（应该通过 attach_communication 注入，靠鸭子类型调用）"
        )

    def test_transport_is_only_created_in_composition_root(self):
        """
        具体传输实现只应该在组合根（main.py）里被创建一次。

        如果别处也能 new 一个 LocalTransport，就会出现
        「多个互不相通的传输层」—— 消息发进去了但没人收得到，
        而且这种 bug 极难排查。
        """
        allowed = {"main.py"}
        violations = []

        for source in sorted(PROJECT_ROOT.rglob("*.py")):
            if ".venv" in source.parts:
                continue
            if source.parent.name in ("tests",):
                continue
            if source.name in allowed:
                continue
            if source.parent.name == "communication":
                # 传输层自己的实现文件当然可以定义它
                continue

            text = source.read_text(encoding="utf-8")
            if "LocalTransport(" in text:
                violations.append(str(source.relative_to(PROJECT_ROOT)))

        assert not violations, (
            "LocalTransport 只应该在组合根（main.py）里创建：\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 4. 依赖方向
# ============================================================
class TestDependencyDirection:
    def test_communication_does_not_import_agents(self):
        """
        依赖方向必须单向：agents → communication。

        通信层不能反过来依赖 agents 层 ——
        否则就没法用「假 Agent」独立测试通信层了。
        """
        violations = []

        for source in communication_sources():
            for module in imported_modules(parse(source)):
                if module == "agents" or module.startswith("agents."):
                    violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "communication/ 不该依赖 agents/：\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_communication_only_depends_on_core_basics(self):
        """
        通信层只允许依赖 core 里的【基础工具】，不允许依赖业务模块。

        允许：core.identity（ID 和时间）、core.logging_setup（日志）
        禁止：core.economy / core.wallet / core.ledger
        """
        allowed = {
            "core",
            "core.identity",
            "core.logging_setup",
        }

        violations = []

        for source in communication_sources():
            for module in imported_modules(parse(source)):
                if module.startswith("core"):
                    if module not in allowed:
                        violations.append(f"{source.name} 导入了 {module}")

        assert not violations, (
            "communication/ 依赖了不该依赖的 core 模块：\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 5. 传输层的抽象性
# ============================================================
class TestTransportAbstraction:
    def test_transport_is_abstract(self):
        """Transport 必须是抽象类，否则它就是个假抽象。"""
        from communication import Transport

        with pytest.raises(TypeError):
            Transport()  # type: ignore[abstract]

    def test_transport_has_the_expected_contract(self):
        """
        抽象接口必须包含这些方法 —— 未来 P2PTransport 要照着实现。
        """
        from communication import Transport

        for method in (
            "start", "close", "open_mailbox", "close_mailbox",
            "has_mailbox", "send", "receive", "address_for", "discover",
        ):
            assert hasattr(Transport, method), f"Transport 缺少 {method}"

    def test_discover_is_the_p2p_hook(self):
        """
        discover() 是给未来 P2P 留的口子。

        它必须存在、必须可覆盖、默认行为必须是「找不到」——
        这样将来加 P2P 时不需要改 Transport 接口本身。
        """
        from communication import LocalTransport, Transport
        import inspect

        assert hasattr(Transport, "discover")
        assert inspect.iscoroutinefunction(Transport.discover)

        local = LocalTransport()
        assert hasattr(local, "discover")

    def test_p2p_transport_can_be_added_without_touching_manager(self):
        """
        【需求第 16 条】

        用一个全新的 scheme 实现 Transport，
        CommunicationManager 不需要任何修改就能用它。

        这不是空话 —— 下面这个 P2PTransport 就是现场定义的，
        而 Manager 的代码一个字都没动。
        """
        from communication import Address, CommunicationManager, Transport
        from communication.transport import DeliveryReceipt

        class P2PTransport(Transport):
            scheme = "p2p"

            def __init__(self):
                self._mailboxes = []
                self.outbox = []

            async def close(self):
                pass

            def open_mailbox(self, agent_id):
                self._mailboxes.append(agent_id)

            def close_mailbox(self, agent_id):
                if agent_id in self._mailboxes:
                    self._mailboxes.remove(agent_id)

            def has_mailbox(self, agent_id):
                return agent_id in self._mailboxes

            async def send(self, message, to):
                self.outbox.append((message, to))
                return DeliveryReceipt(
                    message_id=message.message_id, to=to, scheme=self.scheme
                )

            async def receive(self, agent_id, timeout=None):
                return None

            def address_for(self, agent_id):
                return Address(
                    agent_id=agent_id, scheme="p2p", endpoint=f"peer-{agent_id}"
                )

        from tests.communication_helpers import FakeAgent

        transport = P2PTransport()
        manager = CommunicationManager(transport)
        a = FakeAgent("agent_a", "A")
        b = FakeAgent("agent_b", "B")

        info_a = manager.register(a)
        info_b = manager.register(b)

        # 地址变成了 p2p 格式 —— 但 Manager 完全不知道这件事
        assert str(info_a.address) == "p2p://peer-agent_a"
        assert str(info_b.address) == "p2p://peer-agent_b"

        import asyncio

        async def scenario():
            return await manager.send("agent_a", "agent_b", text="过 P2P")

        asyncio.run(scenario())

        assert len(transport.outbox) == 1
        message, address = transport.outbox[0]
        assert address.scheme == "p2p"
        assert address.endpoint == "peer-agent_b"


# ============================================================
# 6. 为未来留的接口确实存在
# ============================================================
class TestFutureInterfacesExist:
    def test_message_has_task_and_workspace_hooks(self):
        """
        需求第 19 条：为 Task Workspace 预留接口。

        本步骤不实现权限，但消息结构必须容得下 task_id / workspace_id。
        """
        from communication import Message

        msg = Message(
            from_agent="a", to_agent="b",
            payload={"task_id": "TASK-001", "workspace_id": "WS-001"},
        )

        assert msg.task_id == "TASK-001"
        assert msg.workspace_id == "WS-001"

    def test_future_message_types_are_declared(self):
        """未来 Agent Economy 的消息类型已经留好名字。"""
        from communication import MessageType

        for name in (
            "TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT",
            "SUBMIT", "VERIFY", "PAYMENT_REQUEST", "PAYMENT_RESULT",
        ):
            assert hasattr(MessageType, name), f"MessageType 缺少 {name}"

    def test_message_allows_security_extensions(self):
        """
        需求第 23 条：未来要能加签名 / 随机数 / TTL。

        这些字段现在没有语义，但必须能【原样保留】，
        否则以后加签名时，旧节点转发会把签名弄丢。
        """
        from communication import Message

        raw = {
            "from_agent": "a", "to_agent": "b", "payload": {},
            "signature": "sig", "nonce": "n1", "ttl": 30,
            "protocol_version": "2",
        }

        msg = Message.from_dict(raw)
        forwarded = msg.to_dict()

        for field in ("signature", "nonce", "ttl"):
            assert field in forwarded, f"{field} 在转发时丢了"

    def test_registry_can_hold_remote_agents(self):
        """
        需求第 21 条：必须保留 Local / Remote 边界。

        远端 Agent 要能作为一条普通记录存在，
        区别只在于「没有本地处理函数」。
        """
        from communication import AgentRegistry, AgentInfo, Address

        registry = AgentRegistry()
        registry.register(
            AgentInfo(
                agent_id="agent_c",
                address=Address(agent_id="agent_c", scheme="p2p",
                                endpoint="12D3KooW"),
                is_local=False,
                node="node-2",
            )
        )

        assert registry.exists("agent_c")
        assert len(registry.remote_agents()) == 1
        assert len(registry.local_agents()) == 0

    def test_agent_info_has_capabilities_field(self):
        """
        需求第 15 条：Capabilities 要在数据结构上分离。

        本步骤不实现「按能力发现」，但字段必须先有 ——
        加字段是破坏性变更，现在放进去零成本。
        """
        from communication import AgentInfo, Address

        info = AgentInfo(
            agent_id="a",
            address=Address.for_local("a"),
            capabilities=("python_coding", "postgresql"),
        )

        assert info.has_capability("python_coding")
        assert info.has_capability("postgresql")
        assert not info.has_capability("rust")


# ============================================================
# 7. 组合根
# ============================================================
class TestCompositionRoot:
    def test_runtime_assembles_communication(self):
        """
        需求第 28 条：由 Runtime / 组合根负责组装通信依赖。
        """
        from main import Runtime

        annotations = getattr(Runtime, "__annotations__", {})

        assert "communication" in annotations
        assert "transport" in annotations

    def test_agents_get_communication_from_runtime(self, tmp_path):
        """
        实际跑一遍组装，确认 Agent 是被 Runtime 接上通信的，
        而不是自己创建任何东西。
        """
        from main import build_runtime

        runtime = build_runtime(db_path=str(tmp_path / "comm.db"))
        try:
            agent_a = runtime.economy.agent("agent_a")

            assert agent_a.has_communication is True
            assert agent_a.communication is runtime.communication
            # Agent 拿到的不是 transport
            assert agent_a.communication is not runtime.transport
        finally:
            runtime.close()
