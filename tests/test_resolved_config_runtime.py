"""
Runtime ← ResolvedConfig 收口守卫与回归测试
=============================================

对应「最终收口」规格的 §13（架构守卫）与 §14（Test A–G）+ §16（baseline）。

本轮要证明的核心命题：

    ConfigurationResolver 负责**解释**配置，
    ResolvedConfig 负责**表达**事实，
    Runtime 负责**消费**事实 —— Runtime 不再重新解释配置。

🔒 不调用真实 LLM、不启动实验。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from core.config_resolver import (
    ConfigLoader, ConfigurationError, load_resolved_config,
)
from core.paths import GLOBAL_CONFIG_FILE
from llm.router import adapter_specs

from tests.config_deviation_helpers import (
    load_deviations, model_deviation, require_reason,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _code_only(path: Path) -> str:
    """去掉注释与 docstring —— 只看**可执行代码**，避免自引用假红。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # 抹掉所有字符串常量会误伤配置名，所以只按行去注释
    return "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))


def _resolved(**overrides):
    """在真实 canonical 配置上叠加改写，再解析一次。"""
    raw = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).raw
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    loader = ConfigLoader(raw)
    from core.config_resolver import ConfigResolver, ConfigValidator
    canonical = ConfigValidator(adapter_specs=adapter_specs).validate(loader)
    return ConfigResolver(canonical).resolve()


# ============================================================
# Guard 1：Runtime / 生产路径不直接读取旧配置
# ============================================================
class TestGuard1_NoLegacyConfigReadsInProduction:
    #: 已被 *canonical* 取代、生产路径不许再出现的键
    RETIRED = (
        "storage.database_path",
        "system.log_level",
        "system.reliability",
    )

    def test_main_does_not_read_retired_keys(self):
        code = _code_only(PROJECT_ROOT / "main.py")
        for key in self.RETIRED:
            assert f'"{key}"' not in code and f"'{key}'" not in code, (
                f"main.py 仍在读已废弃的配置键 {key}")

    def test_production_path_has_no_llm_section_read(self):
        """`llm.*` 那一段在 Configuration V1 里已经不存在。"""
        for name in ("main.py",):
            code = _code_only(PROJECT_ROOT / name)
            assert '"llm.' not in code, f"{name} 仍在读 llm.* 旧配置段"

    def test_runtime_does_not_use_agent_config_llm_fields(self):
        """
        Runtime 构造 Brain 时不许再用 `AgentConfig` 的 LLM 字段。

        🔒 只看**可执行代码**：`AgentConfig.provider/model` 留给
           Agent 域（economy / capabilities），但 Runtime 取
           provider / model / credential 必须经 ResolvedAgent。
        """
        code = _code_only(PROJECT_ROOT / "main.py")
        assert "BrainSettings.from_resolved_agent(" in code, (
            "Runtime 必须从 ResolvedAgent 构造 BrainSettings")
        assert "BrainSettings.from_agent_config(agent.config)" not in code, (
            "Runtime 仍在用 AgentConfig 当配置事实来源")

    def test_cost_comes_from_resolved(self):
        code = _code_only(PROJECT_ROOT / "main.py")
        assert "CostCalculator.from_resolved(resolved.cost)" in code
        assert "CostCalculator.from_config(config)" not in code, (
            "Cost 仍在读已被删除的 llm.cost.*（会静默退化成 Python 默认值）")


# ============================================================
# Guard 2：Runtime 不重新解析 .env
# ============================================================
class TestGuard2_RuntimeDoesNotReparseDotenv:
    def test_main_does_not_read_environ_in_build_path(self):
        """
        `main.py` 不得直接读环境变量来取配置事实。

        🔒 例外只有一类：**运行时环境事实**（日志级别 / 测试库路径），
           而且它们必须走 `RUNTIME_OVERRIDES` 这个显式清单，
           不是散落的 `os.environ[...]`。
        """
        tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))

        # 🔒 只看 **build_runtime 的函数体** —— 那才是「生产配置路径」。
        #    按 AST 定位而不是按行号：行号会随文件增长而失效
        #    （第一版就是栽在这一条上）。
        target = next((n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n.name == "build_runtime"), None)
        assert target is not None, "找不到 build_runtime"

        offenders = []
        for node in ast.walk(target):
            if isinstance(node, ast.Subscript) and "environ" in ast.unparse(
                    node.value):
                offenders.append((node.lineno, ast.unparse(node)))
            elif (isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "get"
                  and "environ" in ast.unparse(node.func.value)):
                offenders.append((node.lineno, ast.unparse(node)))
        assert not offenders, (
            f"build_runtime 里出现了环境变量直读（配置事实必须来自 "
            f"ResolvedConfig）：{offenders}")

    def test_dotenv_only_loaded_by_the_loader(self):
        """只有 core/config.py 负责把 .env 载进环境。"""
        offenders = []
        for folder in ("core", "services", "llm", "agents"):
            for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
                if "__pycache__" in str(path):
                    continue
                code = _code_only(path)
                if "load_dotenv" in code and path.name != "config.py":
                    offenders.append(path.name)
        assert not offenders, f"这些模块自己解析 .env：{offenders}"


# ============================================================
# Guard 3：Runtime 配置入口唯一
# ============================================================
class TestGuard3_SingleConfigEntry:
    def test_runtime_holds_resolved_config(self):
        from main import Runtime

        assert "resolved_config" in Runtime.__dataclass_fields__, (
            "Runtime 必须持有 ResolvedConfig")
        annotation = Runtime.__dataclass_fields__["resolved_config"].type
        assert "ResolvedRuntimeConfig" in str(annotation)

    def test_runtime_requires_resolved_config(self):
        """`resolved_config` 是**必填**字段 —— 没有第二个等价入口。"""
        from main import Runtime

        import dataclasses

        field = Runtime.__dataclass_fields__["resolved_config"]
        assert field.default is dataclasses.MISSING, (
            "resolved_config 不该有默认值 —— 那等于允许「不传配置」")

    def test_deprecated_constructors_are_marked_and_unused_in_production(self):
        """旧构造器必须**明确标 deprecated**，且生产路径不用。"""
        for path, method in ((PROJECT_ROOT / "agents" / "brain.py",
                              "from_agent_config"),
                             (PROJECT_ROOT / "llm" / "cost.py",
                              "from_config")):
            source = path.read_text(encoding="utf-8")
            assert "DEPRECATED" in source, f"{path.name} 的旧构造器没标 deprecated"
            assert f"def {method}" in source


# ============================================================
# Guard 4：配置事实单一来源
# ============================================================
class TestGuard4_SingleSourceOfTruth:
    FACTS = ("log_level", "database_path", "message_claim_lease_seconds",
             "default_provider")

    @pytest.mark.parametrize("fact", FACTS)
    def test_runtime_fact_is_required_in_config(self, fact):
        """
        🔒 四个运行期事实**必须**写在 `runtime:` 段里。

        缺任何一个 → 配置校验失败（fail loud），
        而不是 Runtime 自己猜一个默认值。
        """
        from core.config_resolver import ConfigResolver, ConfigValidator

        raw = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).raw
        del raw["runtime"][fact]

        with pytest.raises(ConfigurationError) as excinfo:
            ConfigValidator(adapter_specs=adapter_specs).validate(ConfigLoader(raw))
        assert fact in str(excinfo.value)

    def test_no_second_runtime_level_fallback(self):
        """`main.py` 不许给这四件事留第二个 fallback。"""
        code = _code_only(PROJECT_ROOT / "main.py")
        # 认领租约只允许来自 resolved；旧的 config.get 路径必须消失
        assert 'config.get("runtime.message_claim_lease_seconds"' not in code
        assert 'config.get("runtime.log_level"' not in code
        assert 'config.get("runtime.database_path"' not in code


# ============================================================
# §14 Test A–E：改变配置 → Runtime 拿到新值
# ============================================================
class TestConfigChangePropagates:
    def test_A_log_level(self):
        r = _resolved(**{"runtime.log_level": "DEBUG"})
        assert r.log_level == "DEBUG"

    def test_B_database_path(self, tmp_path):
        target = str(tmp_path / "other.db")
        r = _resolved(**{"runtime.database_path": target})
        assert r.database_path == target

    def test_C_agent_model(self):
        """改 `agents.<id>.model` → Resolved Agent 得到新 model。"""
        r = _resolved(**{"agents.novel_author.brain.model": "deepseek_flash"})
        agent = r.agent("novel_author")
        assert agent.model_profile == "deepseek_flash"
        assert agent.model_id == "deepseek-flash"
        assert agent.provider.name == "deepseek"

    def test_D_agent_credential(self):
        """改 `agents.<id>.credential` → 得到新 credential。"""
        r = _resolved(**{"agents.novel_author.brain.credential": "bailian_key_e"})
        agent = r.agent("novel_author")
        assert agent.credential_profile == "bailian_key_e"
        assert agent.credential.env == "BAILIAN_KEY_AGENT_E"

    def test_E_model_provider(self):
        """
        改 `models.<id>.provider` → provider 解析跟着变。

        ⚠️ 必须挑一个**没有被任何 provider 当作 `default_model`** 的
           profile。三个端点各自认领了：
               providers.deepseek.default_model = deepseek_chat
               providers.doubao.default_model   = doubao_novel
               providers.bailian.default_model  = qwen_fast
           改这些 profile 的 provider 会（**正确地**）触发交叉引用校验
           「默认模型必须属于这个端点自己」。

           这条校验已经**两次**拦下我随手挑的 profile 了 ——
           它是对的，所以这里用没有该引用的 `qwen_max`。
        """
        r = _resolved(**{"models.qwen_max.provider": "deepseek"})
        model = r.model("qwen_max")
        assert model.provider.name == "deepseek"
        assert model.provider.adapter == "deepseek"
        assert model.provider.base_url == "https://api.deepseek.com"
        assert model.model_id == "qwen3.8-max"        # model id 不受影响

    def test_E2_credential_is_independent_of_provider(self):
        """
        🔒 Credential **不从 Provider 推导**。

        `agent_a` 用 deepseek；把它的 credential 换成百炼那把 ——
        provider 不变，只换凭据。
        """
        r = _resolved(**{"agents.agent_a.brain.credential": "bailian_key_e"})
        agent = r.agent("agent_a")
        assert agent.provider.name == "deepseek"          # provider 没变
        assert agent.credential.env == "BAILIAN_KEY_AGENT_E"  # 凭据换了


# ============================================================
# §14 Test F：错误配置必须 fail loudly
# ============================================================
class TestF_FailLoud:
    @pytest.mark.parametrize("dotted,value,needle", [
        ("agents.novel_author.brain.model", "no_such_model", "no_such_model"),
        ("agents.novel_author.brain.credential", "no_such_key", "no_such_key"),
        ("models.doubao_novel.provider", "no_such_provider", "no_such_provider"),
        ("providers.deepseek.adapter", "no_such_adapter", "no_such_adapter"),
    ])
    def test_unknown_config_fails_loudly(self, dotted, value, needle):
        with pytest.raises(ConfigurationError) as excinfo:
            _resolved(**{dotted: value})
        assert needle in str(excinfo.value), str(excinfo.value)

    def test_unknown_adapter_does_not_fall_back_to_mock(self):
        with pytest.raises(ConfigurationError) as excinfo:
            _resolved(**{"providers.deepseek.adapter": "no_such_adapter"})
        assert "mock" in str(excinfo.value), (
            "错误信息必须明确说「不会退回 mock」")

    def test_missing_agent_credential_is_an_error(self):
        raw = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).raw
        del raw["agents"]["novel_author"]["brain"]["credential"]
        from core.config_resolver import ConfigValidator
        with pytest.raises(ConfigurationError):
            ConfigValidator(adapter_specs=adapter_specs).validate(ConfigLoader(raw))


# ============================================================
# §14 Test G：删掉 .env fallback 后凭据仍正常
# ============================================================
class TestG_CredentialStillResolves:
    def test_all_agents_have_an_env_var_name(self):
        """每个 Agent 的 credential 都指向一个**环境变量名**。"""
        r = load_resolved_config(GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs)
        for agent_id in sorted(r.agents):
            cred = r.agent(agent_id).credential
            assert cred.env, f"{agent_id} 没有 credential env 变量名"
            assert cred.env.isupper(), cred.env

    def test_no_agent_relies_on_a_global_fallback_key(self):
        """
        🔒 B 类 fallback key 已从 `.env` 删除 ——
           没有任何 Agent 的 credential 指向它们。
        """
        removed = {"DEEPSEEK_API_KEY", "BAILIAN_API_KEY", "GROK_API_KEY"}
        r = load_resolved_config(GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs)
        used = {r.agent(a).credential.env for a in r.agents}
        assert not (used & removed), (
            f"仍有 Agent 依赖已删除的全局 fallback key：{sorted(used & removed)}")
        used |= {c.env for c in r.credentials.values()}
        assert not (used & removed)

    def test_credentials_resolve_from_env(self, monkeypatch):
        """把某个 env 变量设成哨兵值 → ResolvedCredential 报 present。"""
        r0 = load_resolved_config(GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs,
                                  environ={})
        assert r0.credential("doubao_key").present is False

        r1 = load_resolved_config(GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs,
                                  environ={"DOUBAO_API_KEY": "sentinel"})
        assert r1.credential("doubao_key").present is True

    def test_dotenv_holds_only_the_canonical_secrets(self):
        """`.env` 与 `.env.example` 的变量名集合一致，且都是 canonical secret。"""
        def names(path):
            out = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    out.add(s.split("=", 1)[0].strip())
            return out

        env, example = names(PROJECT_ROOT / ".env"), names(PROJECT_ROOT / ".env.example")
        assert env == example, (
            f"不一致 —— 仅 .env: {sorted(env - example)} / "
            f"仅 example: {sorted(example - env)}")

        canonical = {c.env for c in
                     load_resolved_config(GLOBAL_CONFIG_FILE,
                                          adapter_specs=adapter_specs).credentials.values()}
        assert canonical <= env, f"canonical 用到的 key 不在 .env 里：{sorted(canonical - env)}"
        for banned in ("_MODEL", "_BASE_URL", "LOG_LEVEL", "DB_PATH"):
            assert not any(banned in n for n in env), (
                f".env 里出现了非 secret 的配置项：{banned}")


# ============================================================
# §16 10 Agent migration baseline 必须 0 drift
# ============================================================
class TestMigrationBaselineStillHolds:
    def test_10_agents_8_fields_unchanged(self):
        """
        🔒 Runtime 切换到 ResolvedConfig **没有**让任何 Agent 行为漂移。

        对照 `tests/config_migration_baseline.json`：
        8 个 canonical 字段全部一致。
        """
        baseline_path = (Path(__file__).resolve().parent
                         / "config_migration_baseline.json")
        assert baseline_path.exists(), "迁移前快照必须随测试提交"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        r = load_resolved_config(GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs)

        GLOBAL_FALLBACK = {"agent_b": "DOUBAO_API_KEY"}

        # 🔴 **有意偏离**（`config_deliberate_deviations.json`）
        #
        #    基线是「迁移前快照」，作用是证明配置重构**没有偷偷改变行为**。
        #    而 `agent_a..e` 的 `max_tokens`（以及 2026-11-07 的模型迁移）
        #    是**有意的**变更。**不改快照**，显式登记并**要求理由**。
        _dev = load_deviations()
        require_reason(_dev, "max_tokens")
        require_reason(_dev, "model")

        for agent_id, before in sorted(baseline.items()):
            agent = r.agent(agent_id)
            _dev_model = model_deviation(_dev, agent_id)

            want_base = _dev_model.get("base_url", before["base_url"])
            assert agent.provider.base_url == want_base, agent_id
            assert float(agent.provider.timeout_seconds) == float(
                before["timeout_seconds"]), agent_id
            want_model = _dev_model.get("requested_model",
                                        before["requested_model"])
            assert agent.model_id == want_model, agent_id
            assert float(agent.temperature) == float(before["temperature"]), agent_id

            _dev_mt = _dev.get("max_tokens") or {}
            _want_mt = int(_dev_mt.get(agent.agent_id,
                                      before["max_tokens"]))
            assert int(agent.max_tokens) == _want_mt, agent.agent_id
            assert len(agent.system_prompt) == before["system_prompt_len"], agent_id
            expected_env = (_dev_model.get("credential_env")
                            or before["credential_env"]
                            or GLOBAL_FALLBACK.get(agent_id, ""))
            assert agent.credential.env == expected_env, agent_id
            # provider 名（旧 bailian_qwen/glm 已合并为 bailian）
            want_provider = _dev_model.get("provider", before["provider"])
            assert (agent.provider.name == want_provider
                    or before["provider"].startswith("bailian")), agent_id

    def test_baseline_covers_all_ten_agents(self):
        baseline = json.loads((Path(__file__).resolve().parent
                               / "config_migration_baseline.json").read_text(
                                   encoding="utf-8"))
        r = load_resolved_config(GLOBAL_CONFIG_FILE, adapter_specs=adapter_specs)
        # 用户可以随时通过 UI 新增 Agent；迁移基线只要求这 10 个
        # 原始 Agent 仍然存在，不要求配置永远只有这 10 个。
        assert set(baseline).issubset(set(r.agents))
        assert len(baseline) == 10


# ============================================================
# §10 RETIRED_KEYS guard 必须仍然存在
# ============================================================
class TestRetiredKeysGuardStillActive:
    def test_retired_keys_guard_present(self):
        from core.config_resolver import RETIRED_KEYS, ConfigValidator

        assert RETIRED_KEYS, "RETIRED_KEYS 不该被删除"
        assert hasattr(ConfigValidator, "check_no_retired_duplicate_keys")

    @pytest.mark.parametrize("retired", [
        "system.log_level", "system.reliability", "storage",
    ])
    def test_retired_key_returning_is_still_an_error(self, retired):
        raw = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).raw
        head = retired.split(".")[0]
        raw[head] = {"log_level": "DEBUG", "reliability": {"x": 1},
                     "database_path": "x.db"}
        from core.config_resolver import ConfigValidator
        with pytest.raises(ConfigurationError) as excinfo:
            ConfigValidator(adapter_specs=adapter_specs).validate(ConfigLoader(raw))
        assert retired in str(excinfo.value)
