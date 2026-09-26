"""
配置加载器
============================================================

【原则】经常变化的东西不许硬编码。

层级关系：
    全局配置  config/config.yaml
        ↑ 被覆盖
    Agent 配置 agents/<id>/config.yaml

读取优先级（从低到高）：
    1. 代码里的默认值
    2. config/config.yaml  （全局规则）
    3. agents/<id>/config.yaml （该 Agent 的私人覆盖）
    4. 环境变量（最优先，比如 AI_ECONOMY_LOG_LEVEL）

关于 API Key：
    Key 只从环境变量 / .env 读，绝不从 yaml 读，绝不写进代码。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from core.paths import ENV_FILE, GLOBAL_CONFIG_FILE, agent_config_file


# ============================================================
# .env 加载
# ============================================================
def load_env_file(env_path: Optional[Path] = None) -> None:
    """
    把 .env 里的键值对加载进 os.environ。

    优先用 python-dotenv；如果没装，退化成一个极简解析器，
    保证「没装依赖也能启动」——这对新手很友好：
    不需要先 pip install 才能看到启动画面。
    """
    path = env_path or ENV_FILE
    if not path.exists():
        return

    try:
        from dotenv import load_dotenv  # type: ignore

        # override=False：已经存在于系统环境里的变量优先，不动它
        load_dotenv(dotenv_path=path, override=False)
        return
    except ImportError:
        pass  # 退化到下面的手写解析

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # setdefault：不覆盖已经存在的真实环境变量
        if key and key not in os.environ:
            os.environ[key] = value


# ============================================================
# YAML 加载
# ============================================================
def load_yaml(path: Path) -> Dict[str, Any]:
    """
    读取一个 YAML 文件。文件不存在返回空字典。
    """
    if not path.exists():
        return {}

    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少 PyYAML。请先运行：\n"
            "    python -m pip install -r requirements.txt\n"
            "（提示：记得先激活虚拟环境）"
        ) from exc

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误，顶层必须是字典：{path}")
    return data


# ============================================================
# 点号取值
# ============================================================
def get_nested(data: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """
    按 "economy.tokens_per_ai_coin" 这种路径从嵌套字典里取值。

    好处：以后配置结构改了，调用方代码不用改一堆 [] 索引。
    """
    node: Any = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _safe_float(value: Any, default: float) -> float:
    """把配置里的值安全转成 float，转不了就用默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ============================================================
# 错误类型
# ============================================================
class ConfigError(Exception):
    """配置缺失或者格式错误。"""


# 缺少兑换比例时，给用户看的填写示例。
#
# 注意这里【故意不写具体数字】。
# 因为具体数字只应该存在于 config/config.yaml 里 ——
# 连错误提示里的举例都不带数字，就不会给以后的人
# 「这里好像有个可以照抄的默认值」的错觉。
# 有一条测试（test_exchange_rate_is_not_hardcoded_in_source）
# 会强制保证这一点。
TOKENS_PER_AI_COIN_EXAMPLE = (
    "economy:\n"
    "  tokens_per_ai_coin: <整数>    # 1 AI币 = 多少个 token"
)


# ============================================================
# 严格取参数（不给默认值）
# ============================================================
def require_positive_int(
    holder: Any,
    dotted_key: str,
    example: str = "",
) -> int:
    """
    从配置里取一个【必须存在】且必须为正整数的参数。

    为什么有些参数必须存在、不许有默认值？

        因为「给个默认值」等于在代码里偷偷写死了一个参数。
        一旦配置文件和代码里的值不一致，就没人说得清
        系统到底用的是哪一个 —— 这是经济系统里非常危险的模糊地带。

        所以像「AI币 ↔ token 兑换比例」这种核心经济模型参数，
        如果配置里没有，就应该在启动时立刻报错，
        而不是悄悄用一个代码里写死的数字继续跑。

        holder 可以是 Config 或 AgentConfig（它们都有 .get 方法）。
    """
    value = holder.get(dotted_key)

    if value is None:
        message = (
            f"配置缺少必需参数：{dotted_key}\n"
            f"请在 config/config.yaml 里补上它。"
        )
        if example:
            message += f"\n\n例如：\n{example}"
        raise ConfigError(message)

    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{dotted_key} 必须是整数，收到：{value!r}"
        )

    if number <= 0:
        raise ConfigError(
            f"{dotted_key} 必须大于 0，收到：{number}"
        )

    return number


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    递归合并两个字典，override 覆盖 base。

    为什么要 deep merge 而不是整体替换？
        因为 Agent B 的 config.yaml 里只写了 brain.provider，
        它不应该把全局的 brain.temperature 之类全部抹掉。
    """
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ============================================================
# 全局配置
# ============================================================
class Config:
    """
    配置容器。支持 config.get("economy.tokens_per_ai_coin") 和 config["system"]["name"]。
    """

    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None):
        self._data = data
        self.source = source

    # --- 访问 ---
    def get(self, dotted_key: str, default: Any = None) -> Any:
        value = get_nested(self._data, dotted_key, default)
        # 环境变量覆盖：AI_ECONOMY_LOG_LEVEL 覆盖 system.log_level
        if dotted_key == "system.log_level":
            env_value = os.environ.get("AI_ECONOMY_LOG_LEVEL")
            if env_value:
                return env_value
        if dotted_key == "storage.database_path":
            env_value = os.environ.get("AI_ECONOMY_DB_PATH")
            if env_value:
                return env_value
        return value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def as_agent_config(self) -> "AgentConfig":
        return AgentConfig(self._data)

    def __repr__(self) -> str:
        return f"Config(keys={list(self._data.keys())}, source={self.source})"


def load_global_config(config_path: Optional[Path] = None) -> Config:
    """加载全局配置。"""
    load_env_file()
    path = config_path or GLOBAL_CONFIG_FILE
    return Config(load_yaml(path), source=path)


# ============================================================
# Agent 配置
# ============================================================
class AgentConfig:
    """
    单个 Agent 的配置视图：全局配置 + 该 Agent 的私人覆盖。

    它提供一组语义化的属性（agent_id / provider / initial_balance ...），
    这样 Agent 代码里不需要到处写 config.get("brain.provider")。
    """

    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None):
        self._data = data
        self.source = source

    # --- 原始访问 ---
    def get(self, dotted_key: str, default: Any = None) -> Any:
        return get_nested(self._data, dotted_key, default)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    # --- 身份 ---
    @property
    def agent_id(self) -> str:
        value = self.get("identity.agent_id")
        if not value:
            raise ValueError("配置缺少 identity.agent_id，无法确定这是哪个 Agent")
        return str(value)

    @property
    def agent_name(self) -> str:
        return str(self.get("identity.name") or self.agent_id)

    # --- 经济 ---
    @property
    def initial_balance(self) -> float:
        # 注意不能用 `value or default`：初始余额为 0 是合法值，
        # 而 0 是 falsy，会被错误地替换成默认值。
        raw = self.get("economy.initial_balance")
        if raw is None:
            raw = 0
        return _safe_float(raw, 0.0)

    @property
    def currency(self) -> str:
        return str(self.get("economy.currency", "AI"))

    @property
    def tokens_per_ai_coin(self) -> int:
        """
        【核心经济模型参数】1 AI币 = 多少 token。

        这里【故意不给默认值】：配置里没有就报错。
        因为给默认值等于把经济参数偷偷写死在代码里，
        那样你就无法确定系统实际用的是配置里的数还是代码里的数。
        """
        return require_positive_int(
            self,
            "economy.tokens_per_ai_coin",
            example=TOKENS_PER_AI_COIN_EXAMPLE,
        )

    @property
    def amount_precision(self) -> int:
        return int(self.get("economy.amount_precision", 4))

    # --- 大脑 ---
    @property
    def provider(self) -> str:
        """这个 Agent 主要用哪家模型。默认取全局 default_provider。"""
        return str(
            self.get("brain.provider")
            or self.get("llm.default_provider")
            or "mock"
        ).lower()

    @property
    def model(self) -> str:
        """
        Agent 指定的模型；为空时回退到全局注册表里该 provider 的默认模型。
        """
        explicit = self.get("brain.model")
        if explicit:
            return str(explicit)
        return str(self.get(f"llm.providers.{self.provider}.model", "") or "")

    @property
    def fallback_provider(self) -> Optional[str]:
        value = self.get("brain.fallback_provider")
        return str(value).lower() if value else None

    @property
    def system_prompt(self) -> str:
        return str(self.get("brain.system_prompt", "") or "").strip()

    @property
    def temperature(self) -> float:
        return float(self.get("brain.temperature", 0.7))

    @property
    def max_tokens(self) -> int:
        return int(self.get("brain.max_tokens", 2048))

    @property
    def estimated_tokens_per_task(self) -> int:
        return int(self.get("capabilities.estimated_tokens_per_task", 25000))

    @property
    def skills(self) -> list:
        value = self.get("capabilities.skills", [])
        return list(value) if isinstance(value, list) else []

    # --- 行为 ---
    @property
    def allow_outsourcing(self) -> bool:
        return bool(self.get("behavior.allow_outsourcing", False))

    @property
    def allow_bidding(self) -> bool:
        return bool(self.get("behavior.allow_bidding", True))

    def __repr__(self) -> str:
        return (
            f"AgentConfig(agent_id={self.agent_id!r}, "
            f"provider={self.provider!r}, "
            f"initial_balance={self.initial_balance})"
        )


def load_agent_config(
    agent_id: str,
    global_config: Optional[Config] = None,
    agent_config_path: Optional[Path] = None,
) -> AgentConfig:
    """
    加载某个 Agent 的完整配置 = 全局配置 ⊕ 该 Agent 的私人配置。
    """
    if global_config is None:
        global_config = load_global_config()

    path = agent_config_path or agent_config_file(agent_id)
    agent_specific = load_yaml(path)

    merged = deep_merge(global_config.to_dict(), agent_specific)

    # 保险：如果 Agent 配置文件里漏了 identity.agent_id，用目录名兜底
    merged.setdefault("identity", {})
    merged["identity"].setdefault("agent_id", agent_id)
    merged["identity"].setdefault("name", agent_id)

    return AgentConfig(merged, source=path)
