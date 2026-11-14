"""
Configuration System Refactor —— ResolvedConfig 测试
======================================================

对应实施规格 §16 的 12 项测试。

【本文件的结构】

    第 1 组（正向）：解析**应该**成功，且结果**必须**与迁移前一致
        test_01_provider_registry_matches_config
        test_02_five_agents_resolve_to_expected_profiles
        test_09_same_model_two_credentials
        test_10_new_provider_needs_no_python_change
        test_12_migration_preserves_behavior

    第 2 组（反向 / 守卫）：**注入**违规，确认守卫**真的变红**
        test_03_unknown_model_is_an_error
        test_04_unknown_credential_is_an_error
        test_05_unknown_provider_is_an_error
        test_06_unknown_adapter_is_an_error
        test_07_agent_may_not_declare_provider
        test_08_agent_may_not_declare_api_key_env
        test_11_literal_secret_in_config_is_an_error

🔒 按 AGENTS.md §8「新增守卫必须配反向测试」：
   第 2 组每一条都**真的把违规写进配置**，而不是只断言代码里有那个函数。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.config_deviation_helpers import (
    load_deviations, model_deviation, require_reason,
)

from core.config_resolver import (
    ConfigLoader,
    ConfigResolver,
    ConfigValidator,
    ConfigurationError,
    ResolvedRuntimeConfig,
    load_resolved_config,
)
from core.paths import GLOBAL_CONFIG_FILE
from llm.router import adapter_specs


# ============================================================
# 公共工具
# ============================================================
def _validator() -> ConfigValidator:
    return ConfigValidator(adapter_specs=adapter_specs)


def _problems(canonical: dict, *, extra: dict | None = None) -> list:
    """
    跑四条守卫，返回**全部**问题（不抛异常，方便断言具体文案）。

    `extra`：额外合并进**原始配置**的段
    （用来注入「已废弃键回归」这类违规）。
    """
    v = _validator()
    raw = dict(canonical)
    raw.update(extra or {})
    out = list(v.check_no_silent_mock_fallback(canonical))
    out += list(v.check_no_retired_duplicate_keys(raw))
    out += list(v.check_reference_integrity(canonical))
    out += list(v.check_no_secret_in_config(canonical))
    return out


def _must_fail(canonical: dict, *, extra: dict | None = None) -> str:
    """确认这份配置**校验失败**，并返回合并后的错误文案。"""
    problems = _problems(canonical, extra=extra)
    assert problems, "❌ 守卫没有发现这个违规 —— 守卫是假绿的"
    # 🔒 走**完整**的 validate() 路径（不是只调 check_*）——
    #    否则「守卫函数发现了问题、但 validate() 忘了调用它」这种
    #    假绿就抓不到。
    raw = dict(canonical)
    raw.update(extra or {})
    with pytest.raises(ConfigurationError) as excinfo:
        ConfigValidator(adapter_specs=adapter_specs).validate(ConfigLoader(raw))
    return str(excinfo.value)


def base_canonical() -> dict:
    """
    一份**最小但完整**的 canonical 配置 —— 各条测试从它出发注入违规。

    刻意用假名字（alpha / brand_new），避免测试与真实厂商耦合。
    """
    return {
        "version": 1,
        # 🔒 runtime 段必须给出**四个**运行期事实 ——
        #    缺任何一个 Runtime 就只能靠猜，那正是 V1 收口要消灭的。
        "runtime": {"log_level": "INFO", "database_path": "data/x.db",
                    "message_claim_lease_seconds": 300,
                    "default_provider": "mock"},
        "providers": {
            "alpha": {
                "adapter": "openai_compatible",
                "base_url": "https://alpha.example.com/v1",
                "timeout_seconds": 30,
                "default_model": "alpha_one",
            },
            "mock": {
                "adapter": "mock",
                "timeout_seconds": 300,
                "default_model": "mock_model",
            },
        },
        "models": {
            "alpha_one": {"provider": "alpha", "model_id": "alpha-1"},
            "alpha_two": {"provider": "alpha", "model_id": "alpha-2"},
            "mock_model": {"provider": "mock", "model_id": "mock-model-v1"},
        },
        "credentials": {
            "key_a": {"env": "TEST_KEY_A"},
            "key_b": {"env": "TEST_KEY_B"},
        },
        "agents": {
            "agent_one": {
                "name": "One",
                "brain": {"model": "alpha_one", "credential": "key_a"},
            },
            "agent_two": {
                "name": "Two",
                "brain": {"model": "alpha_one", "credential": "key_b"},
            },
        },
    }


# ============================================================
# 第 1 组：正向
# ============================================================
class TestResolvesCorrectly:
    """正向：解析成功，且引用关系正确。"""

    def test_01_provider_registry_matches_config(self):
        """
        `registry.implemented` 必须与 `config.yaml` 的 `providers:` 一致。

        这是「Provider Registry 与配置文件双重注册」被消掉之后的守卫：
        配置里写了 `adapter:` 的 provider，注册表里就必须真的能造出来。
        """
        from core.config import load_global_config
        from llm.router import LLMRouter

        canonical = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).canonical
        declared = set(canonical["providers"])
        assert declared, "canonical providers 段是空的"

        router = LLMRouter(load_global_config())
        implemented = set(router.registry.implemented)

        missing = declared - implemented
        assert not missing, (
            f"配置里声明了这些 provider，但注册表里没有：{sorted(missing)}。\n"
            f"（注册表应该**从 config 的 adapter 字段**建出来。）")

        # 反向：每个已实现的 provider 都必须能真的造出来
        for name in declared:
            provider = router.get_provider(name)
            assert provider.name, f"{name} 造出来没有名字"

        # 反向：adapter 未知的 provider **不能**静默出现在 implemented 里
        broken = copy.deepcopy(canonical)
        broken["providers"]["ghost"] = {
            "adapter": "does_not_exist", "base_url": "https://x.example.com"}
        router2 = LLMRouter({"providers": broken["providers"]})
        assert "ghost" not in router2.registry.implemented

    def test_02_five_agents_resolve_to_expected_profiles(self):
        """
        5 个异构 Agent 必须解析出预期的 model profile / provider / credential。

        🔒 期望值**手写**在这里（不是从代码里算出来的），
           这样如果解析逻辑被改坏，测试会红而不是跟着一起变。
        """
        resolved = load_resolved_config(GLOBAL_CONFIG_FILE,
                                        adapter_specs=adapter_specs)

        EXPECTED = {
            "novel_world_builder": ("deepseek_chat", "deepseek", "deepseek",
                                    "DEEPSEEK_KEY_AGENT_A"),
            "novel_plot_architect": ("deepseek_chat", "deepseek", "deepseek",
                                     "DEEPSEEK_KEY_AGENT_C"),
            "novel_author": ("doubao_novel", "doubao", "doubao",
                             "DOUBAO_API_KEY"),
            "novel_editor": ("qwen_fast", "bailian", "bailian",
                             "BAILIAN_KEY_AGENT_D"),
            "novel_reader": ("qwen_max", "bailian", "bailian",
                             "BAILIAN_KEY_AGENT_E"),
        }
        for agent_id, (profile, provider, adapter, env) in EXPECTED.items():
            a = resolved.agent(agent_id)
            assert a.model_profile == profile, agent_id
            assert a.provider.name == provider, agent_id
            assert a.provider.adapter == adapter, agent_id
            assert a.credential.env == env, agent_id
            assert a.model_id, f"{agent_id} 的 model_id 是空的"

        # 两个百炼 Agent 必须用**同一个 provider、不同的 model**
        editor = resolved.agent("novel_editor")
        reader = resolved.agent("novel_reader")
        assert editor.provider.name == reader.provider.name == "bailian"
        assert editor.model_id != reader.model_id

    def test_09_same_model_two_credentials(self):
        """
        同一个 Model 被两个 Agent 用**不同** Credential —— 必须都能解析。

        这是 `Credential 与 Model 正交`（规格 §1.2）的直接守卫：
        凭据不是模型的属性，是 Agent 的选择。
        """
        resolved = load_resolved_config(GLOBAL_CONFIG_FILE,
                                        adapter_specs=adapter_specs)

        # 同一个 model profile 被两个 Agent 用不同 credential
        a = resolved.agent("novel_plot_architect")   # deepseek_chat / key_c
        b = resolved.agent("novel_world_builder")    # deepseek_chat / key_a
        c = resolved.agent("novel_author")           # doubao_novel  / doubao

        # 构造一对：同 model、不同 credential
        same_model = [a, b]
        assert all(x.model_profile == "deepseek_chat" for x in same_model)
        assert a.credential.env == "DEEPSEEK_KEY_AGENT_C"
        assert b.credential.env == "DEEPSEEK_KEY_AGENT_A"
        assert a.credential.profile != b.credential.profile

        # 反向：不同 model 也可以共用**同一个** credential
        # （agent_a 是 deepseek-flash，novel_world_builder 是 deepseek-chat）
        d = resolved.agent("agent_a")
        assert d.credential.env == b.credential.env == "DEEPSEEK_KEY_AGENT_A"
        assert d.model_profile != b.model_profile
        assert c.credential.env == "DOUBAO_API_KEY"

        # 用假配置做一次**真正**的「同 model 两 credential」正向断言
        cfg = base_canonical()
        cfg["agents"]["agent_two"]["brain"]["credential"] = "key_b"
        rc = ConfigResolver(cfg, environ={"TEST_KEY_A": "x", "TEST_KEY_B": "y"}
                            ).resolve()
        one, two = rc.agent("agent_one"), rc.agent("agent_two")
        assert one.model_profile == two.model_profile == "alpha_one"
        assert one.model_id == two.model_id
        assert one.credential.env == "TEST_KEY_A"
        assert two.credential.env == "TEST_KEY_B"
        assert one.credential.profile != two.credential.profile

    def test_same_model_profile_shares_context_and_output(self):
        """同一 model profile 的所有 Agent 共享 context / output 配置。"""
        resolved = load_resolved_config(GLOBAL_CONFIG_FILE,
                                        adapter_specs=adapter_specs)
        groups = {}
        for agent in resolved.agents.values():
            groups.setdefault(agent.model_profile, set()).add((
                agent.context_window,
                agent.max_output_tokens,
                agent.max_tokens,
            ))
        for profile, values in groups.items():
            assert len(values) == 1, (
                f"{profile} 的 Agent 上下文 / 输出配置不一致：{values}")

    def test_10_new_provider_needs_no_python_change(self):
        """
        **只改配置**就能新增一个使用已有 adapter 的 Provider。

        这直接回答规格 §19 Test 10「是否真的做到不改 Python」。
        """
        from llm.router import LLMRouter

        cfg = base_canonical()
        # ---- 只在配置里加东西：一个新 provider + 一个新 model + 一个 credential
        cfg["providers"]["brand_new"] = {
            "adapter": "openai_compatible",       # ← 复用已有 adapter
            "base_url": "https://brand-new.example.com/v1",
            "timeout_seconds": 45,
            "default_model": "brand_new_model",
        }
        cfg["models"]["brand_new_model"] = {
            "provider": "brand_new", "model_id": "brand-new-1"}
        cfg["credentials"]["brand_new_key"] = {"env": "BRAND_NEW_KEY"}
        cfg["agents"]["brand_new_agent"] = {
            "name": "Brand New",
            "brain": {"model": "brand_new_model", "credential": "brand_new_key"},
        }

        # ① 校验通过（没有引用悬空）
        assert _problems(cfg) == []

        # ② 解析出正确结果
        rc = ConfigResolver(cfg, environ={}).resolve()
        agent = rc.agent("brand_new_agent")
        assert agent.provider.name == "brand_new"
        assert agent.provider.adapter == "openai_compatible"
        assert agent.provider.base_url == "https://brand-new.example.com/v1"
        assert agent.provider.timeout_seconds == 45
        assert agent.model_id == "brand-new-1"
        assert agent.credential.env == "BRAND_NEW_KEY"

        # ③ 注册表**从这份配置**建出来时就认识它 —— 不需要碰 Python
        router = LLMRouter({"providers": cfg["providers"]})
        assert "brand_new" in router.registry.implemented

        # ④ 而且真的造得出来，且 base_url 来自配置（不是类默认）
        provider = router.get_provider("brand_new")
        assert provider.base_url == "https://brand-new.example.com/v1"
        assert provider.timeout == 45

    def test_12_migration_preserves_behavior(self):
        """
        🔴 迁移必须**不改变** Agent 的实际运行行为。

        拿 `tests/config_migration_baseline.json`（重构前从**旧配置系统**
        解析出来的真实值）逐项对照：

            model_id · base_url · timeout · temperature · max_tokens ·
            credential env · system_prompt 长度 · economy/capabilities/
            negotiation/behavior 各段

        这是规格 §17「必须证明没有偷偷改变模型 / endpoint / credential」
        的机械证据，不是人工核对。

        🔒 快照放在 `tests/` 下**随测试一起提交** ——
           它是这次重构的回归基准，不是一个实验产物。
           放在 untracked 目录里会让这条测试在新克隆上 `skip`，
           而「跳过」等于「没验证」。
        """
        baseline_path = Path(__file__).resolve().parent / "config_migration_baseline.json"
        if not baseline_path.exists():
            # 🔒 宁可红也不要 skip：这条测试是行为不变的**唯一**机械证据。
            pytest.fail(
                f"迁移前快照不见了：{baseline_path}\n"
                f"它是 test_12 的基准，必须随测试提交。"
                f"（不要用 skip 掩盖 —— 跳过等于没验证。）")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

        resolved = load_resolved_config(GLOBAL_CONFIG_FILE,
                                        adapter_specs=adapter_specs)

        # 旧配置里的 credential_env 为空 = 当时靠**全局 provider key 回退**。
        # 迁移后必须把那个回退**显式写出来**，且指向**同一个环境变量**。
        GLOBAL_FALLBACK_ENV = {
            "agent_b": "DOUBAO_API_KEY",
        }

        # 🔴 **有意偏离**（`config_deliberate_deviations.json`）
        #
        #    基线是「迁移前快照」，作用是证明配置重构**没有偷偷改变行为**。
        #    而 `agent_a..e` 的 `max_tokens`（以及 2026-11-07 的模型迁移）
        #    是**有意的**变更。**不改快照**，在这里显式登记并**要求理由**。
        _dev = load_deviations()
        require_reason(_dev, "max_tokens")
        require_reason(_dev, "model")

        checked = 0
        for agent_id, before in sorted(baseline.items()):
            a = resolved.agent(agent_id)
            _dev_model = model_deviation(_dev, agent_id)

            # ① 模型：旧配置解析出的真实 model 名 == 新配置的 model_id
            want_model = _dev_model.get("requested_model",
                                        before["requested_model"])
            assert a.model_id == want_model, (
                f"{agent_id}: model_id {a.model_id!r} != "
                f"迁移前/登记值 {want_model!r}")

            # ② endpoint
            want_base = _dev_model.get("base_url", before["base_url"])
            assert a.provider.base_url == want_base, (
                f"{agent_id}: base_url {a.provider.base_url!r} != "
                f"迁移前/登记值 {want_base!r}")

            # ②.5 provider 名（旧 bailian_qwen/glm 已合并为 bailian）
            want_provider = _dev_model.get("provider", before["provider"])
            assert (a.provider.name == want_provider
                    or before["provider"].startswith("bailian")), agent_id

            # ③ 超时
            assert float(a.provider.timeout_seconds) == float(
                before["timeout_seconds"]), f"{agent_id}: timeout 变了"

            # ④ 生成参数
            assert float(a.temperature) == float(before["temperature"]), agent_id

            _dev_mt = _dev.get("max_tokens") or {}
            _want_mt = int(_dev_mt.get(a.agent_id,
                                      before["max_tokens"]))
            assert int(a.max_tokens) == _want_mt, a.agent_id

            # ⑤ 凭据：指向**同一个环境变量**（或登记过的迁移值）
            expected_env = (_dev_model.get("credential_env")
                            or before["credential_env"]
                            or GLOBAL_FALLBACK_ENV.get(agent_id, ""))
            if expected_env:
                assert a.credential.env == expected_env, (
                    f"{agent_id}: credential env {a.credential.env!r} != "
                    f"迁移前/登记值 {expected_env!r}")

            # ⑥ system_prompt 一个字都不能丢
            assert len(a.system_prompt) == before["system_prompt_len"], (
                f"{agent_id}: system_prompt 长度变了")

            # ⑦ 其它 Agent 段原样保留（迁移不允许丢字段）
            raw = a.raw
            assert raw["economy"]["initial_balance"] == before["initial_balance"], agent_id
            assert list(raw["capabilities"]["skills"]) == before["skills"], agent_id
            assert (raw["capabilities"]["estimated_tokens_per_task"]
                    == before["estimated_tokens_per_task"]), agent_id
            assert dict(raw["negotiation"]) == before["negotiation"], agent_id
            assert raw["behavior"]["allow_outsourcing"] == before["allow_outsourcing"], agent_id
            assert raw["behavior"]["allow_bidding"] == before["allow_bidding"], agent_id
            checked += 1

        assert checked == 10, f"应该核对 10 个 Agent，实际 {checked}"


# ============================================================
# 第 2 组：反向 / 守卫
# ============================================================
class TestGuardsGoRedWhenViolated:
    """每一条都**真的注入违规**，确认守卫变红。"""

    # ---- 反向 1：不存在的 model ----
    def test_03_unknown_model_is_an_error(self):
        cfg = base_canonical()
        cfg["agents"]["agent_one"]["brain"]["model"] = "no_such_model"
        msg = _must_fail(cfg)
        assert "no_such_model" in msg
        assert "models" in msg.lower() or "model profile" in msg

        # 反向的反向：把它改回合法值 → 必须通过（证明上面红的是这一处）
        cfg2 = base_canonical()
        cfg2["agents"]["agent_one"]["brain"]["model"] = "alpha_two"
        assert _problems(cfg2) == []
        assert ConfigResolver(cfg2, environ={}).agent("agent_one").model_id == "alpha-2"

    # ---- 反向 2：不存在的 credential ----
    def test_04_unknown_credential_is_an_error(self):
        cfg = base_canonical()
        cfg["agents"]["agent_one"]["brain"]["credential"] = "no_such_key"
        msg = _must_fail(cfg)
        assert "no_such_key" in msg
        assert "credential" in msg.lower()

        cfg2 = base_canonical()
        cfg2["agents"]["agent_one"]["brain"]["credential"] = "key_b"
        assert _problems(cfg2) == []

    # ---- 反向 2b：**缺** credential（不许再自动找 provider 全局 key）----
    def test_04b_missing_credential_is_an_error(self):
        """
        规格 §8：非 mock Agent **必须**显式引用 credential。

        🔒 这条是「凭据回退到 provider 全局 key」被删掉的守卫 ——
           以前 agent_b 就是靠这个回退拿到 DOUBAO_API_KEY 的。
        """
        cfg = base_canonical()
        cfg["agents"]["agent_one"]["brain"].pop("credential")
        msg = _must_fail(cfg)
        assert "credential" in msg.lower()
        assert "agent_one" in msg

    # ---- 反向 3：不存在的 provider ----
    def test_05_unknown_provider_is_an_error(self):
        cfg = base_canonical()
        cfg["models"]["alpha_one"]["provider"] = "no_such_provider"
        msg = _must_fail(cfg)
        assert "no_such_provider" in msg
        assert "provider" in msg.lower()

        # 反向的反向：provider 段里删掉这个 provider → 也要报（悬空引用）
        cfg2 = base_canonical()
        del cfg2["providers"]["alpha"]
        msg2 = _must_fail(cfg2)
        assert "alpha" in msg2

    # ---- 反向 4：不存在的 adapter ----
    def test_06_unknown_adapter_is_an_error(self):
        cfg = base_canonical()
        cfg["providers"]["alpha"]["adapter"] = "no_such_adapter"
        msg = _must_fail(cfg)
        assert "no_such_adapter" in msg
        assert "adapter" in msg.lower()
        # 🔒 必须**明确说不会退回 mock**
        assert "mock" in msg

        # 反向的反向：换成真实存在的 adapter → 通过
        cfg2 = base_canonical()
        cfg2["providers"]["alpha"]["adapter"] = "deepseek"
        assert _problems(cfg2) == []

    # ---- 反向 4b：缺 adapter ----
    def test_06b_missing_adapter_is_an_error(self):
        cfg = base_canonical()
        cfg["providers"]["alpha"].pop("adapter")
        msg = _must_fail(cfg)
        assert "adapter" in msg.lower()

    # ---- 反向 4c：default_model 指向别人的模型 ----
    def test_06c_default_model_must_belong_to_its_own_provider(self):
        """
        `providers.X.default_model` 必须指向一个 `provider == X` 的 model profile。

        🔒 否则「不显式选模型」时会静默用到**别的端点**的模型 ——
           这正是 B1 那一类错误的另一种形态。
        """
        cfg = base_canonical()
        cfg["providers"]["alpha"]["default_model"] = "mock_model"  # 属于 mock
        msg = _must_fail(cfg)
        assert "default_model" in msg

        cfg2 = base_canonical()
        cfg2["providers"]["alpha"]["default_model"] = "no_such_model"
        assert _must_fail(cfg2)

    # ---- 反向 5：Agent 里出现 provider ----
    def test_agent_may_not_override_max_tokens(self):
        """输出上限由 model profile 统一决定，Agent 不再单独声明。"""
        cfg = base_canonical()
        cfg["agents"]["agent_one"]["brain"]["max_tokens"] = 4096
        msg = _must_fail(cfg)
        assert "max_tokens" in msg
        assert "max_output_tokens" in msg

        cfg2 = base_canonical()
        assert "max_tokens" not in cfg2["agents"]["agent_one"]["brain"]
        assert _problems(cfg2) == []

    def test_07_agent_may_not_declare_provider(self):
        """
        规格 §1.3：Agent 只引用 model / credential，
        **禁止**在 Agent 配置里写 provider。
        """
        cfg = base_canonical()
        cfg["agents"]["agent_one"]["brain"]["provider"] = "alpha"
        msg = _must_fail(cfg)
        assert "provider" in msg
        assert "agent_one" in msg
        # 必须说清楚「provider 由 model 解析出来」
        assert "model" in msg

        # 反向的反向：删掉就通过 —— 证明红的是这一处，不是别的
        cfg2 = base_canonical()
        assert "provider" not in cfg2["agents"]["agent_one"]["brain"]
        assert _problems(cfg2) == []

    # ---- 反向 6：Agent 里出现 api_key_env ----
    def test_08_agent_may_not_declare_api_key_env(self):
        """
        规格 §1.3：Agent 里**禁止**出现 `api_key_env`
        （secret 的引用只能经 credential profile）。
        """
        cfg = base_canonical()
        cfg["agents"]["agent_one"]["brain"]["api_key_env"] = "TEST_KEY_A"
        msg = _must_fail(cfg)
        assert "api_key_env" in msg
        assert "agent_one" in msg

    # ---- 反向 6b：Agent 里出现 base_url ----
    def test_08b_agent_may_not_declare_base_url(self):
        cfg = base_canonical()
        cfg["agents"]["agent_one"]["brain"]["base_url"] = "https://evil.example.com"
        msg = _must_fail(cfg)
        assert "base_url" in msg

    # ---- 反向 7：配置文件里出现疑似明文 key ----
    def test_11_literal_secret_in_config_is_an_error(self):
        """
        🔒 Guard 3：配置文件里**不许出现 secret 本身**，只许出现变量名。

        三种可疑形状都要抓到：
            ① 字段名就是 `api_key`
            ② 值以真实密钥前缀开头（`sk-…`）
            ③ 值是一段长随机串
        """
        # ① 字段名
        cfg = base_canonical()
        cfg["credentials"]["key_a"]["api_key"] = "whatever"
        assert _must_fail(cfg)

        # ② 密钥前缀
        cfg = base_canonical()
        cfg["credentials"]["key_a"]["value"] = "sk-" + "a" * 40
        msg = _must_fail(cfg)
        assert "sk-" in msg

        # ③ 长随机串
        cfg = base_canonical()
        cfg["credentials"]["key_a"]["material"] = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
        assert _must_fail(cfg)

        # ---- 反面的反面：**合法的**引用不能误报 ----
        cfg = base_canonical()
        assert _problems(cfg) == [], (
            "❌ 误报：正常的 env 变量名 / URL 被当成 secret 了。\n"
            "   误报比漏报更糟 —— 它会让**正确**的配置无法启动。")

        # 具体几种正常形状
        for key, value in (
            ("env", "SOME_API_KEY"),                  # 默认的环境变量名（后缀 _env 之外）
            ("api_key_env", "SOME_API_KEY"),          # 变量名引用
            ("token_env", "TOKEN_X"),                 # 变量名引用
            ("base_url", "https://api.deepseek.com"), # URL
            ("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
            ("model_id", "ep-20261004151436-sgwpt"),  # 长得像随机串但是 model id
            ("model_id", "qwen3.8-flash"),
        ):
            assert ConfigValidator.check_no_secret_in_config({key: value}) == [], (
                f"❌ {key}={value!r} 被误报成 secret")


# ============================================================
# 第 3 组：ResolvedRuntimeConfig 的行为
# ============================================================
class TestResolvedConfigBehaviour:
    def test_lookup_raises_instead_of_returning_none(self):
        """🔒 查询不到必须**抛异常**，不许返回 None 让调用方静默继续。"""
        rc = ConfigResolver(base_canonical(), environ={}).resolve()
        for fn, bad in ((rc.provider, "nope"), (rc.model, "nope"),
                        (rc.credential, "nope"), (rc.agent, "nope")):
            with pytest.raises(ConfigurationError):
                fn(bad)

    def test_credentials_never_expose_material(self):
        """🔒 credential 的序列化结果里**不许有 secret 的值**。"""
        secret = "SUPER_SECRET_VALUE_1234567890"
        rc = ConfigResolver(base_canonical(),
                            environ={"TEST_KEY_A": secret}).resolve()
        cred = rc.credential("key_a")
        assert cred.present is True
        text = json.dumps(rc.to_dict(), ensure_ascii=False)
        assert secret not in text, "❌ ResolvedConfig 里泄漏了 secret 的值"
        assert "TEST_KEY_A" in text, "变量名应该出现（它才是可显示的）"
        assert secret not in repr(cred)

    def test_missing_secret_is_reported_not_raised(self):
        """
        凭据**没配**不是配置错误（`present=False`）——
        真正的失败发生在调用前（`preflight_credential`）。
        这里只确认它被**如实报告**，而不是假装配好了。
        """
        rc = ConfigResolver(base_canonical(), environ={}).resolve()
        assert rc.credential("key_a").present is False
        assert rc.credential("key_a").env == "TEST_KEY_A"

    def test_sources_are_recorded_for_every_reference(self):
        """每个可配项都要能回答「这个值来自哪里」（GUI / 排障用）。"""
        rc = ConfigResolver(base_canonical(), environ={}).resolve()
        for key in ("providers.alpha", "models.alpha_one", "credentials.key_a",
                    "agents.agent_one"):
            assert key in rc.sources
            assert rc.sources[key].startswith("config.yaml") or "::" in rc.sources[key]

    def test_loader_rejects_a_missing_file_loudly(self, tmp_path):
        """🔒 Guard 4：配置文件不存在 → 报错，**不许**降级到默认/mock。"""
        with pytest.raises(ConfigurationError) as excinfo:
            load_resolved_config(tmp_path / "nope.yaml",
                                 adapter_specs=adapter_specs)
        assert "不存在" in str(excinfo.value)


# ============================================================
# 第 4 组：迁移冲突检测（Guard 1）
# ============================================================
class TestNoDuplicateFacts:
    """
    🔒 **一个事实只能有一个权威来源。**

    这条守卫守的是「**已废弃键不许回归**」：

        system.log_level       → runtime.log_level
        system.reliability     → runtime.message_claim_lease_seconds
        storage.database_path  → runtime.database_path

    【为什么必须守它】
        这三个键合并进 `runtime.*` 之后，**所有**读取路径都改读
        canonical 位置。如果哪天有人把旧键加回来，
        它会被**静默忽略** —— 变成最危险的状态：
        「看起来改了，其实没生效」。

    【为什么不是「值不同才报错」】
        迁移期这里比的是「canonical 与旧段的值是否一致」。
        旧段已经全部删除，那个比较没有对象了。
        所以改成**禁止回归**：旧键一旦出现就报错，并指出该写在哪。
    """

    RETIRED = (
        ("system.log_level", "runtime.log_level",
         {"log_level": "DEBUG"}),
        ("system.reliability", "runtime.message_claim_lease_seconds",
         {"reliability": {"message_claim_lease_seconds": 1}}),
        ("storage", "runtime.database_path",
         {"database_path": "data/other.db"}),
    )

    @pytest.mark.parametrize("retired,home,value",
                             [(r, h, v) for r, h, v in RETIRED])
    def test_retired_key_is_an_error(self, retired, home, value):
        """反向：把已废弃键加回来 → 必须变红，并指出它现在该写在哪。"""
        head = retired.split(".")[0]
        msg = _must_fail(base_canonical(), extra={head: value})
        assert home in msg, f"报错里应该指出 canonical 位置 {home}"
        assert "权威来源" in msg

    def test_adding_none_of_them_passes(self):
        """正向的反面：不加这些键 → 通过（证明上面红的是这一处）。"""
        assert _problems(base_canonical()) == []

    # ---- 真实配置必须干净 ----
    def test_real_config_has_no_retired_keys(self):
        """🔴 仓库里的 `config/config.yaml` 不许有任何已废弃键。"""
        loader = ConfigLoader.from_file(GLOBAL_CONFIG_FILE)
        assert ConfigValidator.check_no_retired_duplicate_keys(loader.raw) == []

    def test_real_config_storage_section_is_gone(self):
        raw = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).raw
        assert "storage" not in raw, "storage 段应该已经删除"

    def test_real_config_system_is_metadata_only(self):
        """`system` 只放系统元信息（name / version）。"""
        raw = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).raw
        assert set(raw["system"]) == {"name", "version"}, raw["system"]

    def test_real_config_runtime_is_the_single_authority(self):
        """`runtime` 是 log_level / database_path / reliability / default_provider 的唯一权威。"""
        raw = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).raw
        assert set(raw["runtime"]) == {
            "log_level", "database_path",
            "message_claim_lease_seconds", "default_provider",
            "inbox_enabled"}, raw["runtime"]
        assert raw["runtime"]["inbox_enabled"] is False

    def test_no_duplicate_top_level_keys(self):
        """
        🔒 YAML 允许重复顶层键（后者静默覆盖前者）——
           那会让「同一事实两份定义」重新出现，而且**不报错**。
           所以这里显式检查一遍。
        """
        from pathlib import Path as _P
        text = _P(GLOBAL_CONFIG_FILE).read_text(encoding="utf-8")
        tops = [l.split(":", 1)[0].strip() for l in text.splitlines()
                if l[:1].isalpha() and ":" in l]
        dups = sorted({k for k in tops if tops.count(k) > 1})
        assert not dups, f"config.yaml 里有重复顶层键：{dups}"

    def test_no_read_path_still_uses_retired_keys(self):
        """
        🔒 守卫的**另一半**：源码里也不许再读旧键。

        只测「配置里没有旧键」是不够的 —— 如果代码还在读
        `system.log_level`，它会**静默拿到代码里的默认值**，
        问题就被藏起来了。那正是这条守卫要防的。

        ⚠️ 只匹配「作为 `.get(...)` 参数出现的键」，
           不匹配任何字符串字面量 —— 否则 `RETIRED_KEYS` 表
           自己（它合法地列出这些键名）会造成**自引用假红**。
        """
        import re
        from pathlib import Path as _P
        root = _P(GLOBAL_CONFIG_FILE).resolve().parent.parent
        patterns = [r'\.get\(\s*["\']' + re.escape(k) + r'["\']'
                    for k in ("system.log_level", "system.reliability",
                              "storage.database_path")]
        offenders = []
        for target in ("core", "storage", "services", "communication",
                       "agents", "llm", "main.py"):
            node = root / target
            files = [node] if node.is_file() else sorted(node.rglob("*.py"))
            for f in files:
                if "__pycache__" in str(f):
                    continue
                text = f.read_text(encoding="utf-8")
                for pat in patterns:
                    if re.search(pat, text):
                        offenders.append(
                            f"{f.relative_to(root)} 仍在读旧键（会静默拿默认值）")
        assert not offenders, "\n  ".join(offenders)


# ============================================================
# 第 5 组：真实配置本身必须干净
# ============================================================
class TestRealConfigIsValid:
    def test_real_config_passes_all_guards(self):
        """🔴 仓库里的 `config/config.yaml` 必须通过全部四条守卫。"""
        loader = ConfigLoader.from_file(GLOBAL_CONFIG_FILE)
        ConfigValidator(adapter_specs=adapter_specs).validate(loader)

    def test_real_config_has_no_literal_secret(self):
        canonical = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).canonical
        assert ConfigValidator.check_no_secret_in_config(canonical) == []

    def test_real_config_agents_have_no_provider_or_env(self):
        """🔒 真实配置里 10 个 Agent 都不许出现 provider / api_key_env / base_url。"""
        canonical = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).canonical
        for agent_id, agent in canonical["agents"].items():
            brain = agent.get("brain") or {}
            for banned in ("provider", "api_key_env", "base_url"):
                assert banned not in brain, f"{agent_id} 里出现了 {banned}"

    def test_real_config_every_agent_has_model_and_credential(self):
        canonical = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).canonical
        for agent_id, agent in canonical["agents"].items():
            brain = agent.get("brain") or {}
            assert brain.get("model"), f"{agent_id} 缺 model"
            assert brain.get("credential"), f"{agent_id} 缺 credential"


# ============================================================
# 第 6 组：运行时覆盖必须是**显式清单**
# ============================================================
class TestRuntimeOverridesAreAnAllowlist:
    """
    🔒 `RUNTIME_OVERRIDES` 是一份**显式清单**，不是「任何配置都能被
       环境变量覆盖」。

    为什么这里要专门守它：本轮的 Configuration System Refactor
    **改过这个机制** —— 重构前它是 `Config.get` 里两条硬编码 `if`，
    重构后是一张有名字的表。按 AGENTS.md §8「新增/改动守卫必须配反向测试」，
    这里既测正向（能覆盖）也测反向（**不许**越界）。

    ⚠️ 审计报告 `CONFIGURATION_SYSTEM_AUDIT.md` §I1-8 指出：
       重构**前**没有任何测试锁定这个行为，属于既有的覆盖缺口。
       本类补上。
    """

    def test_only_listed_keys_can_be_overridden(self, monkeypatch):
        """正向：清单里的键**真的**能被覆盖。"""
        from core.config import load_global_config

        cfg = load_global_config()

        monkeypatch.setenv("AI_ECONOMY_LOG_LEVEL", "DEBUG")
        assert cfg.get("runtime.log_level") == "DEBUG"

        monkeypatch.setenv("AI_ECONOMY_DB_PATH", "/tmp/override-check.db")
        assert cfg.get("runtime.database_path") == "/tmp/override-check.db"

    def test_unlisted_keys_are_never_overridden(self, monkeypatch):
        """
        反向：**没有**被列进清单的键，即使有同名环境变量也不受影响。

        🔒 这是这个类存在的核心理由 —— 防止有人把「显式清单」
           又变回「什么都能覆盖」。
        """
        from core.config import load_global_config, load_yaml
        from core.paths import GLOBAL_CONFIG_FILE

        raw = load_yaml(GLOBAL_CONFIG_FILE)
        cfg = load_global_config()

        # 造几个「名字很像、但语义不是覆盖口」的环境变量
        monkeypatch.setenv("ECONOMY_TOKENS_PER_AI_COIN", "1")
        monkeypatch.setenv("TOKENS_PER_AI_COIN", "1")
        monkeypatch.setenv("ECONOMY_INITIAL_BALANCE", "999999")
        monkeypatch.setenv("SYSTEM_NAME", "hacked")

        assert cfg.get("economy.tokens_per_ai_coin") == raw["economy"]["tokens_per_ai_coin"]
        assert cfg.get("economy.initial_balance") == raw["economy"]["initial_balance"]
        assert cfg.get("system.name") == raw["system"]["name"]

    def test_empty_string_is_not_an_override(self, monkeypatch):
        """
        反向：**空字符串不算覆盖**。

        🔒 否则 CI 里一句 `AI_ECONOMY_LOG_LEVEL=` 就会把配置清空成
           空日志级别 —— 那是「静默地把配置弄坏」，正是本项目要避免的。
        """
        from core.config import load_global_config, load_yaml
        from core.paths import GLOBAL_CONFIG_FILE

        raw = load_yaml(GLOBAL_CONFIG_FILE)
        cfg = load_global_config()

        monkeypatch.setenv("AI_ECONOMY_LOG_LEVEL", "")
        monkeypatch.setenv("AI_ECONOMY_DB_PATH", "")

        assert cfg.get("runtime.log_level") == raw["runtime"]["log_level"]
        assert cfg.get("runtime.database_path") == raw["runtime"]["database_path"]

    def test_allowlist_stays_small_and_named(self):
        """
        反向：清单**不许悄悄变大**。

        它只该有 2 个变量 / 4 个 dotted key（`runtime.*` 与旧的
        `system.*` / `storage.*` 各一对）。谁要往里面加东西，
        必须先改这条测试 —— 也就是必须先有一次明确的讨论。
        """
        from core.config import RUNTIME_OVERRIDES

        assert set(RUNTIME_OVERRIDES.values()) == {
            "AI_ECONOMY_LOG_LEVEL", "AI_ECONOMY_DB_PATH"}, (
            "运行时覆盖的环境变量集合变了 —— 如果是有意的，"
            "请同时更新这条测试与 ./dev.sh config 的说明")
        # 🔒 只有 2 项：runtime.log_level / runtime.database_path。
        #    以前还成对列了 system.log_level / storage.database_path ——
        #    那两份定义已删除（所有读取路径都改读 runtime.*）。
        assert set(RUNTIME_OVERRIDES) == {
            "runtime.log_level", "runtime.database_path"}, RUNTIME_OVERRIDES

    def test_agent_config_get_does_not_apply_runtime_overrides(self, monkeypatch):
        """
        记录一个**已知的不一致**（不是 bug，但必须写下来）：

        `Config.get` 吃运行时覆盖，`AgentConfig.get` **不吃**。

        当前无实际影响 —— 被覆盖的两个键（`runtime.log_level` /
        `runtime.database_path`）不走 `AgentConfig`。
        这条测试的作用是：**如果哪天有人让 AgentConfig 也开始吃覆盖，
        或者让被覆盖的键流进 AgentConfig，这里会红**。
        """
        from core.config import AgentConfig, load_global_config

        cfg = load_global_config()
        agent = AgentConfig(cfg.to_dict())

        monkeypatch.setenv("AI_ECONOMY_LOG_LEVEL", "DEBUG")
        assert cfg.get("runtime.log_level") == "DEBUG"
        # AgentConfig 看到的是**原始值**（不受运行时覆盖影响）
        assert agent.get("runtime.log_level") == "INFO"
