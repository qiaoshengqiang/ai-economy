"""
Agent 独立 Credential —— 定向测试
============================================================

【🔒 本文件绝不调用真实 API】

    真实多 Key 测试只经人工命令（`./dev.sh credential-demo`）。

【这个文件的核心证明方式】

    不是「看代码觉得对」，而是**看真正发出去的 HTTP 头**：

        FakeHTTP 记录每一次请求的 Authorization
            ↓
        断言 Agent A 的请求带的是 Bearer key-A，
             Agent B 的请求带的是 Bearer key-B

    这是「不串 Key」最直接的证据 —— 它检查的是**实际发出的字节**，
    而不是某个中间变量。
"""

from __future__ import annotations

import threading

import pytest

from llm.credential import (
    SOURCE_AGENT_ENV,
    SOURCE_AGENT_INLINE,
    SOURCE_GLOBAL_ENV,
    SOURCE_NONE,
    Credential,
    CredentialResolver,
)
from llm.deepseek import DeepSeekProvider
from llm.base import LLMRequestError
from llm.router import LLMRouter

from llm.openai_compatible import HTTPResult

from tests.llm_test_helpers import FakeHTTP, openai_success_body


# ============================================================
# 脚手架
# ============================================================
GLOBAL_ENV = "AIECO_TEST_GLOBAL_KEY"
KEY_A = "sk-agent-a-1111111111111111"
KEY_B = "sk-agent-b-2222222222222222"


def base_config(**provider_over):
    provider = {
        "enabled": True,
        "model": "spy-model",
        "api_key_env": GLOBAL_ENV,
        "base_url": "https://example.invalid",
    }
    provider.update(provider_over)
    return {"llm": {"default_provider": "spy", "providers": {"spy": provider}}}


class Spy:
    """一个把 HTTP 换掉、并记录每次请求头的 router。"""

    def __init__(self, config=None, environ=None):
        # ⚠️ 必须是 HTTPResult，不是裸字符串 ——
        #    第一版传了 `openai_success_body(...)` 的返回值（str），
        #    于是 provider 里 `result.ok` 直接 AttributeError。
        self.http = FakeHTTP(HTTPResult(status=200,
                                        body=openai_success_body("ok")))
        self.router = LLMRouter(config or base_config())
        self.router.registry.register(
            "spy",
            lambda cfg, model="", api_key_override=None: DeepSeekProvider(
                cfg, model, http_post=self.http,
                api_key_override=api_key_override,
            ),
        )
        self.resolver = CredentialResolver(config or base_config(),
                                           environ=environ or {})

    def auth_headers(self) -> list:
        return [c["headers"].get("Authorization") for c in self.http.calls]

    def keys_sent(self) -> list:
        out = []
        for h in self.auth_headers():
            out.append((h or "").replace("Bearer ", ""))
        return out

    def generate(self, credential, provider="spy", messages="hi"):
        return self.router.generate(messages=messages, provider=provider,
                                    credential=credential)


def env(**kv):
    return dict(kv)


# ============================================================
# 1-5  Credential 解析与优先级
# ============================================================
class TestCredentialResolution:
    def test_inline_api_key(self):
        """§2 模式 B：配置文件直接写 key（测试阶段合法方式）。"""
        r = CredentialResolver(base_config(), environ={})
        c = r.resolve(provider="spy", agent_id="agent_a",
                      credential={"api_key": KEY_A})
        assert c.available
        assert c.material == KEY_A
        assert c.source == SOURCE_AGENT_INLINE

    def test_api_key_env(self):
        """§2 模式 A：从环境变量读。"""
        r = CredentialResolver(base_config(), environ={"KEY_A_ENV": KEY_A})
        c = r.resolve(provider="spy", agent_id="agent_a",
                      credential={"api_key_env": "KEY_A_ENV"})
        assert c.available
        assert c.material == KEY_A
        assert c.source == SOURCE_AGENT_ENV

    def test_env_wins_over_inline(self):
        """
        🔒 §3 优先级：`api_key_env` **优先于** `api_key`。

        动机：开发者本地同时留着正式的环境变量和一个旧的测试 key 时，
        正式运行**不应该**意外继续用旧测试 key。
        """
        r = CredentialResolver(base_config(),
                               environ={"KEY_A_ENV": KEY_A})
        c = r.resolve(provider="spy", agent_id="agent_a",
                      credential={"api_key_env": "KEY_A_ENV",
                                  "api_key": "sk-OLD-TEST-KEY"})
        assert c.material == KEY_A
        assert c.source == SOURCE_AGENT_ENV

    def test_inline_used_when_env_absent(self):
        """`api_key_env` 指向的变量**没设置** → 落到 `api_key`。"""
        r = CredentialResolver(base_config(), environ={})
        c = r.resolve(provider="spy", agent_id="agent_a",
                      credential={"api_key_env": "NOT_SET_ANYWHERE",
                                  "api_key": KEY_A})
        assert c.material == KEY_A
        assert c.source == SOURCE_AGENT_INLINE

    def test_env_present_but_empty_falls_through(self):
        """环境变量存在但是**空串** → 不算有值。"""
        r = CredentialResolver(base_config(), environ={"EMPTY": "   "})
        c = r.resolve(provider="spy", agent_id="agent_a",
                      credential={"api_key_env": "EMPTY",
                                  "api_key": KEY_A})
        assert c.material == KEY_A
        assert c.source == SOURCE_AGENT_INLINE

    def test_falls_back_to_global(self):
        """§6：Agent 没有自己的 credential → 用全局的（旧配置继续工作）。"""
        r = CredentialResolver(base_config(), environ={GLOBAL_ENV: "sk-global"})
        c = r.resolve(provider="spy", agent_id="agent_a", credential=None)
        assert c.available
        assert c.material == "sk-global"
        assert c.source == SOURCE_GLOBAL_ENV

    def test_unavailable_when_nothing_configured(self):
        """§3 第 3 条：两者都没有 → Credential 不可用（不是假成功）。"""
        r = CredentialResolver({"llm": {"providers": {"spy": {}}}}, environ={})
        c = r.resolve(provider="spy", agent_id="agent_a", credential=None)
        assert not c.available
        assert c.source == SOURCE_NONE
        assert c.material == ""

    def test_resolver_does_not_touch_other_domains(self):
        """§11：Resolver 很薄 —— 不碰 LLM / Agent / Task / Economy。"""
        import inspect
        from llm import credential as mod

        # ⚠️ 第一版查整个源码文本 —— 被模块**文档字符串**里的
        #    「它不碰 … Agent · Task · Economy」绊倒（假红）。
        #    改成 AST：只看**真的 import 和调用**。
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= {"__future__", "hashlib", "os", "secrets",
                           "dataclasses", "typing"}, (
            f"CredentialResolver 引入了不该有的依赖：{imported}"
        )


# ============================================================
# 6  最关键：Key 不串（§16）
# ============================================================
class TestKeysDoNotCrossTalk:
    def test_alternating_calls_use_their_own_key(self):
        """
        🔒 任务书 §16 要求的那个序列：

            A → A_cred
            B → B_cred
            A → A_cred
            B → B_cred
        """
        spy = Spy()
        cfg = base_config()
        cred_a = spy.resolver.resolve(provider="spy", agent_id="agent_a",
                                      credential={"api_key": KEY_A})
        cred_b = spy.resolver.resolve(provider="spy", agent_id="agent_b",
                                      credential={"api_key": KEY_B})

        spy.generate(cred_a)
        spy.generate(cred_b)
        spy.generate(cred_a)
        spy.generate(cred_b)

        assert spy.keys_sent() == [KEY_A, KEY_B, KEY_A, KEY_B]

    def test_same_provider_different_keys_never_share_an_instance(self):
        """§7：不同 Credential 必须产生**独立**的 provider 实例。"""
        spy = Spy()
        cred_a = spy.resolver.resolve(provider="spy", agent_id="agent_a",
                                      credential={"api_key": KEY_A})
        cred_b = spy.resolver.resolve(provider="spy", agent_id="agent_b",
                                      credential={"api_key": KEY_B})

        pa = spy.router.get_provider("spy", credential=cred_a)
        pb = spy.router.get_provider("spy", credential=cred_b)

        assert pa is not pb, "🔒 不同 Credential 绝不能共享 provider 实例"
        assert pa._api_key == KEY_A
        assert pb._api_key == KEY_B

    def test_same_credential_does_share_an_instance(self):
        """同一个凭据则应当复用（否则每 Agent 都新建连接池，浪费）。"""
        spy = Spy()
        cred = spy.resolver.resolve(provider="spy", agent_id="agent_a",
                                    credential={"api_key": KEY_A})
        assert (spy.router.get_provider("spy", credential=cred)
                is spy.router.get_provider("spy", credential=cred))

    def test_identity_is_ref_plus_key_fingerprint(self):
        """
        🔒 身份 = `credential_ref` + 加盐哈希(key)（§8 + §19）。

            · 不同 ref → 不同实例（按 Agent 归属，usage 可分别记账）
            · 同 ref 但 key 不同 → **仍然**不同实例（配置写错也不串）
            · 同 ref 同 key → 复用实例
        """
        spy = Spy()

        def cred(ref, key):
            return spy.resolver.resolve(provider="spy", agent_id=ref,
                                        credential={"api_key": key},
                                        credential_ref=ref)

        a1 = cred("agent_a_spy", KEY_A)
        a2 = cred("agent_a_spy", KEY_A)      # 同 ref 同 key
        b1 = cred("agent_b_spy", KEY_A)      # 不同 ref，同 key
        a3 = cred("agent_a_spy", KEY_B)      # 同 ref，不同 key

        assert a1.identity == a2.identity
        assert a1.identity != b1.identity, "不同 ref 应当是不同的凭据"
        assert a1.identity != a3.identity, "同 ref 换了 key 也必须区分开"

        p = spy.router.get_provider
        assert p("spy", credential=a1) is p("spy", credential=a2)
        assert p("spy", credential=a1) is not p("spy", credential=b1)
        assert p("spy", credential=a1) is not p("spy", credential=a3)


# ============================================================
# 7  Router 缓存隔离（§7）
# ============================================================
class TestRouterCacheIsolation:
    def test_cache_key_is_not_the_raw_key(self):
        """
        🔒 §8：**绝不能用明文 key 当缓存键** ——
           否则 `_instances` 的 key 里就有 key 了。
        """
        spy = Spy()
        cred = spy.resolver.resolve(provider="spy", agent_id="agent_a",
                                    credential={"api_key": KEY_A})
        spy.router.get_provider("spy", credential=cred)

        for cache_key in spy.router._instances:
            assert KEY_A not in str(cache_key), "缓存键里出现了明文 key"
            assert "sk-" not in str(cache_key)

    def test_cached_by_provider_and_credential(self):
        spy = Spy()
        cred_a = spy.resolver.resolve(provider="spy", agent_id="agent_a",
                                      credential={"api_key": KEY_A})
        cred_b = spy.resolver.resolve(provider="spy", agent_id="agent_b",
                                      credential={"api_key": KEY_B})
        spy.router.get_provider("spy", credential=cred_a)
        spy.router.get_provider("spy", credential=cred_b)

        assert len(spy.router._instances) == 2

    def test_no_credential_keeps_old_behaviour(self):
        """不传 credential → 走 provider 自己的全局路径（旧语义不变）。"""
        spy = Spy(environ=None)
        p1 = spy.router.get_provider("spy")
        p2 = spy.router.get_provider("spy")
        assert p1 is p2
        assert len(spy.router._instances) == 1


# ============================================================
# 8-9  兼容性：旧配置 / 混合（§23）
# ============================================================
class TestCompatibility:
    def test_old_config_still_works(self, monkeypatch):
        """Old：全局 provider + 全局 api_key_env → 继续工作。"""
        monkeypatch.setenv(GLOBAL_ENV, "sk-global-old")
        spy = Spy()
        resp = spy.router.generate(messages="hi", provider="spy")
        assert resp.content == "ok"
        assert spy.keys_sent() == ["sk-global-old"]

    def test_mixed_agent_and_global(self, monkeypatch):
        """Mixed：Agent A 用自己的，Agent B 用全局的。"""
        monkeypatch.setenv(GLOBAL_ENV, "sk-global")
        spy = Spy()

        cred_a = spy.resolver.resolve(provider="spy", agent_id="agent_a",
                                      credential={"api_key": KEY_A})
        cred_b = spy.resolver.resolve(provider="spy", agent_id="agent_b",
                                      credential=None)

        spy.generate(cred_a)
        spy.generate(cred_b)

        assert spy.keys_sent() == [KEY_A, "sk-global"]

        # ⚠️ 这里记录一个**有意的设计取舍**：
        #    Agent B 和 Agent C 都回退到**同一个**全局 key，
        #    但它们的 `credential_ref` 不同（agent_b_spy / agent_c_spy），
        #    所以拿到的是**两个实例**而不是一个。
        #
        #    为什么故意的：§19 要「usage 能归属到每个 credential」。
        #    多一个 HTTP client 的成本可以忽略；
        #    而如果共用实例，就没法在 provider 层面区分这两个 Agent。
        cred_c = spy.resolver.resolve(provider="spy", agent_id="agent_c",
                                      credential=None)
        assert cred_b.identity != cred_c.identity
        assert (spy.router.get_provider("spy", credential=cred_b)
                is not spy.router.get_provider("spy", credential=cred_c))
        # 但两个 key **确实是同一个**（都来自全局）→ 没有串 key
        assert cred_b.material == cred_c.material

    def test_brain_settings_has_no_api_key_field(self):
        """
        🔒 §9：BrainSettings 里**不该**有 api_key 字段 ——
           Brain 不是 Secret Store。
        """
        import dataclasses
        from agents.brain import BrainSettings

        names = {f.name for f in dataclasses.fields(BrainSettings)}
        assert "credential_ref" in names
        assert "api_key" not in names
        assert "api_key_env" not in names

    def test_brain_generate_has_no_api_key_param(self):
        """§10：`agent.generate(...)` 接口不暴露 key。"""
        import inspect
        from agents.brain import Brain

        params = set(inspect.signature(Brain.generate).parameters)
        assert "api_key" not in params
        assert "credential" not in params, (
            "credential 由 Brain 内部决定，不该从 generate() 传进来"
        )


# ============================================================
# 10  Secret 安全（§12）
# ============================================================
class TestSecretSafety:
    def test_repr_and_str_never_contain_the_key(self):
        c = Credential(ref="agent_a_spy", source=SOURCE_AGENT_INLINE,
                       material=KEY_A, provider="spy")
        for text in (repr(c), str(c), str(c.describe())):
            assert KEY_A not in text
            assert "sk-" not in text

    def test_credential_identity_does_not_contain_the_key(self):
        """§8：identity 是加盐哈希，不可逆、不可跨进程对照。"""
        c = Credential(ref="x", source=SOURCE_AGENT_INLINE,
                       material=KEY_A, provider="spy")
        assert KEY_A not in c.identity
        # 同一个 (ref, key) 稳定
        assert c.identity == Credential(ref="x", source=SOURCE_AGENT_INLINE,
                                        material=KEY_A).identity
        # 换 key 必须换身份（防止同 ref 配错 key 时被错误共享）
        assert c.identity != Credential(ref="x", source=SOURCE_AGENT_INLINE,
                                        material=KEY_B).identity

    def test_credential_is_not_in_the_logger_output(self, caplog):
        import logging
        c = Credential(ref="agent_a_spy", source=SOURCE_AGENT_INLINE,
                       material=KEY_A, provider="spy")
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("aieco.test").debug("using %r", c)
            logging.getLogger("aieco.test").debug("describe %s", c.describe())
        assert KEY_A not in caplog.text

    def test_provider_scrubs_key_from_errors(self):
        """
        §12.4：错误信息里不能出现 key。

        Provider 已有 `_scrub_secrets`，这里验证它确实生效。
        """
        http = FakeHTTP(raises=LLMRequestError(f"boom with {KEY_A}"))
        p = DeepSeekProvider(base_config()["llm"]["providers"]["spy"],
                            "spy-model", http_post=http,
                            api_key_override=KEY_A)
        try:
            p.generate(messages="hi")
        except Exception as exc:                        # noqa: BLE001
            assert KEY_A not in str(exc)
        else:
            pytest.fail("应当抛异常")

    def test_unavailable_reason_does_not_leak_other_keys(self):
        p = DeepSeekProvider(base_config()["llm"]["providers"]["spy"],
                            "spy-model", http_post=FakeHTTP())
        reason = p.unavailable_reason() or ""
        assert KEY_A not in reason and "sk-" not in reason

    def test_router_debug_log_does_not_contain_keys(self, caplog):
        import logging
        spy = Spy()
        cred = spy.resolver.resolve(provider="spy", agent_id="agent_a",
                                    credential={"api_key": KEY_A})
        with caplog.at_level(logging.DEBUG):
            spy.generate(cred)
        assert KEY_A not in caplog.text


# ============================================================
# 11  20 Agent 并发（§17）
# ============================================================
class TestTwentyAgentConcurrency:
    def test_twenty_agents_do_not_cross_keys(self):
        """
        🔒 20 个 Agent、20 个不同的 key、**并发**调用，
           每个请求都必须带上自己的 key。

        不需要真实 LLM —— HTTP 层是假的。
        """
        spy = Spy()
        http = spy.http
        http.calls = []
        lock = threading.Lock()

        # 让 FakeHTTP 的 append 在并发下安全
        original = http.__call__
        seen: dict = {}

        def safe_call(url, payload, headers, timeout):
            with lock:
                result = original(url, payload, headers, timeout)
            return result

        http.__call__ = safe_call  # type: ignore[method-assign]

        n = 20
        creds = {}
        for i in range(n):
            key = f"sk-key-{i:02d}-{'x' * 16}"
            creds[i] = spy.resolver.resolve(
                provider="spy", agent_id=f"agent_{i:02d}",
                credential={"api_key": key})
            seen[i] = key

        errors = []

        def work(i):
            try:
                spy.generate(creds[i], messages=f"from agent {i:02d}")
            except Exception as exc:                    # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, errors
        assert len(http.calls) == n

        # 🔒 每个请求的 key 必须是「它自己那个」
        sent = spy.keys_sent()
        assert sorted(sent) == sorted(seen.values()), "有 Agent 串了 key"

        # 再严格一点：按请求体里的 agent 序号核对
        for call, header in zip(http.calls, spy.auth_headers()):
            body = str(call["payload"])
            idx = int(body.split("from agent ")[1][:2])
            assert header == f"Bearer {seen[idx]}", (
                f"agent_{idx:02d} 的请求带错了 key"
            )

    def test_twenty_agents_get_twenty_instances(self):
        spy = Spy()
        for i in range(20):
            cred = spy.resolver.resolve(
                provider="spy", agent_id=f"agent_{i:02d}",
                credential={"api_key": f"sk-key-{i:02d}-{'y' * 16}"})
            spy.router.get_provider("spy", credential=cred)
        assert len(spy.router._instances) == 20


# ============================================================
# 12  真实 API 不进入 pytest（§18）
# ============================================================
class TestRealApiIsManualOnly:
    def test_no_real_provider_is_called_here(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords or ():
                    if kw.arg != "provider":
                        continue
                    v = kw.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        assert v.value == "spy", (
                            f"本文件只该用 spy provider，看到 {v.value!r}"
                        )

    def test_no_real_api_key_env_is_read(self):
        """本文件只用测试专用的环境变量名。"""
        # ⚠️ 这条守卫**改过两次**，两次都是自引用假红
        #    （查文本 → 被自己的字面量绊倒；查常量 → 被 banned 元组绊倒）。
        #
        #    正解：把「真实 key 环境变量名」**从真实配置里读出来**，
        #    测试文件里不该出现那些名字。
        import ast
        from pathlib import Path

        from core.config import load_global_config

        real_names = set()
        for pcfg in (load_global_config().get("llm.providers", {}) or {}).values():
            if isinstance(pcfg, dict) and pcfg.get("api_key_env"):
                real_names.add(str(pcfg["api_key_env"]))
        assert real_names, "配置里应当有 api_key_env，否则这条守卫没意义"

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        hits = [
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value in real_names
        ]
        assert not hits, f"本文件不该引用真实 key 环境变量：{hits}"
