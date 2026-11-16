"""
系统设置写入口 —— providers / models / credentials
==================================================

🔒 唯一权威：`config/llm.yaml` + `.env`
🔒 config/config.yaml 不再定义这三段；同一事实只有一个来源。
🔒 secret 只写入 `.env`，UI 只返回环境变量名与「是否存在」。

写流程：

    read llm.yaml
      → typed patch
      → 与 config.yaml 合并
      → ConfigValidator + ConfigResolver 全量校验
      → 写临时文件 / 备份 / 原子替换
      → 标记 restart_required（Runtime 在启动时构建）
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

from core.config_resolver import (
    ConfigLoader, ConfigResolver, ConfigValidator, ConfigurationError,
    LLM_CONFIG_SECTIONS, THINKING_LEVELS, merge_llm_sections,
)
from core.dsml import TOOL_CALL_FORMATS
from core.paths import (
    ENV_FILE, GLOBAL_CONFIG_FILE, GLOBAL_LLM_CONFIG_FILE,
)
from core.platform_support import default_platform
from llm.router import adapter_specs
from work.life_policy import LifePolicy

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
_MAX_AGENT_INITIAL_BALANCE = 1000.0

#: Agent 设置页直接暴露的 life 字段。
#: 其他字段（例如 allowed_actions / reply_max_chars）只有在配置里已存在时
#: 才继续保留，避免 UI 保存一次就写出一堆默认值噪音。
_AGENT_LIFE_UI_KEYS = (
    "enabled",
    "wake_on_message",
    "wake_on_alarm",
    "accept_delegated_alarms",
    "max_turns_per_hour",
    "min_interval_seconds",
    "max_tokens_per_turn",
    "max_cost_per_turn",
    "max_conversation_turns",
)


class SettingsAdminError(ValueError):
    """系统设置写入失败。"""


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsAdminError(f"读取 {path} 失败：{exc}") from exc
    if not isinstance(data, dict):
        raise SettingsAdminError(f"{path} 必须是映射。")
    return data


def _atomic_write(path: Path, text: str, *, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        try:
            default_platform().chmod(tmp, mode)
        except OSError:
            pass
    tmp.replace(path)


def _agent_block_bounds(lines: List[str], agent_id: str) -> tuple:
    """返回 `agents.<id>:` 的 `[start, end)` 行号。"""
    agents_index = None
    for index, line in enumerate(lines):
        if re.fullmatch(r"agents:\s*", line):
            agents_index = index
            break
    if agents_index is None:
        raise SettingsAdminError("config.yaml 缺少顶层 agents: 段。")

    agent_start = None
    for index in range(agents_index + 1, len(lines)):
        line = lines[index]
        if (line.strip() and not line.startswith(" ")
                and not line.lstrip().startswith("#")):
            break
        if re.fullmatch(rf"  {re.escape(agent_id)}:\s*", line):
            agent_start = index
            break
    if agent_start is None:
        raise SettingsAdminError(f"config.yaml 里找不到 Agent：{agent_id}")

    agent_end = len(lines)
    for index in range(agent_start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if (line.startswith("  ") and not line.startswith("    ")
                and re.fullmatch(r"  [A-Za-z0-9_.-]+:\s*", line)):
            agent_end = index
            break
        if not line.startswith(" "):
            agent_end = index
            break
    return agent_start, agent_end


def _block_end(
    lines: List[str], start: int, limit: int, *, indent: int,
) -> int:
    """返回一个 YAML 映射块（`indent` 空格）的结束行号。"""
    for index in range(start + 1, limit):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        current = len(line) - len(line.lstrip(" "))
        if current <= indent:
            return index
    return limit


def _patch_agent_brain(
    text: str, agent_id: str, *, model: str, credential: str,
) -> str:
    """只改 `agents.<id>.brain.model / credential`，保留其余文本与注释。

    为什么不 `yaml.safe_load` + `safe_dump` 整个 config.yaml：
    那会把所有注释、空行和手工排版全部抹掉。配置文件是给人看的，
    这里只需要替换两行，所以用最小文本补丁。
    """
    lines = text.splitlines()
    agent_start, agent_end = _agent_block_bounds(lines, agent_id)

    brain_index = None
    for index in range(agent_start + 1, agent_end):
        if re.fullmatch(r"    brain:\s*", lines[index]):
            brain_index = index
            break
    if brain_index is None:
        raise SettingsAdminError(
            f"Agent {agent_id} 的配置缺少 brain: 段。")

    brain_end = agent_end
    for index in range(brain_index + 1, agent_end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 4:
            brain_end = index
            break

    replaced_model = replaced_credential = False
    new_brain: List[str] = []
    for index in range(brain_index + 1, brain_end):
        line = lines[index]
        if re.match(r"      model:\s*", line):
            new_brain.append(f"      model: {model}")
            replaced_model = True
        elif re.match(r"      credential:\s*", line):
            new_brain.append(f"      credential: {credential}")
            replaced_credential = True
        else:
            new_brain.append(line)

    prefix: List[str] = []
    if not replaced_model:
        prefix.append(f"      model: {model}")
    if not replaced_credential:
        prefix.append(f"      credential: {credential}")
    if prefix:
        new_brain = prefix + new_brain
    lines[brain_index + 1:brain_end] = new_brain

    updated = "\n".join(lines)
    if text.endswith("\n"):
        updated += "\n"
    return updated


def _patch_agent_life(
    text: str, agent_id: str, life: Mapping[str, Any],
) -> str:
    """写入 / 替换 `agents.<id>.life`，保留 Agent 的其余配置与注释。"""
    lines = text.splitlines()
    agent_start, agent_end = _agent_block_bounds(lines, agent_id)

    dumped = yaml.safe_dump(
        dict(life), allow_unicode=True, sort_keys=False, width=1000,
    ).rstrip()
    block = ["    life:"] + [
        "      " + line for line in dumped.splitlines()
    ]

    life_index = None
    for index in range(agent_start + 1, agent_end):
        if re.fullmatch(r"    life:\s*", lines[index]):
            life_index = index
            break
    if life_index is not None:
        life_end = _block_end(lines, life_index, agent_end, indent=4)
        lines[life_index:life_end] = block
    else:
        insert_at = agent_end
        for index in range(agent_start + 1, agent_end):
            if re.fullmatch(r"    economy:\s*", lines[index]):
                insert_at = index
                break
        lines[insert_at:insert_at] = block

    updated = "\n".join(lines)
    if text.endswith("\n"):
        updated += "\n"
    return updated


def _read_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class SystemConfigAdmin:
    """GUI 系统设置的读写门面。"""

    def __init__(
        self,
        *,
        config_path: Path = GLOBAL_CONFIG_FILE,
        llm_path: Path = GLOBAL_LLM_CONFIG_FILE,
        env_path: Path = ENV_FILE,
        backup_dir: Optional[Path] = None,
        adapter_specs_fn=adapter_specs,
    ) -> None:
        self.config_path = Path(config_path)
        self.llm_path = Path(llm_path)
        self.env_path = Path(env_path)
        self.backup_dir = (
            Path(backup_dir) if backup_dir is not None
            else self.llm_path.parent / "backups"
        )
        self.adapter_specs_fn = adapter_specs_fn
        self.restart_required = False
        self._initial_hash = self._hash_sources()

    # ------------------------------------------------------------
    # read
    # ------------------------------------------------------------
    def _hash_sources(self) -> str:
        digest = hashlib.sha256()
        for path in (self.config_path, self.llm_path):
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
        return digest.hexdigest()

    def _llm_data(self) -> Dict[str, Any]:
        data = _read_yaml(self.llm_path)
        return {key: data.get(key) or {}
                for key in LLM_CONFIG_SECTIONS}

    def _merged(self, llm: Mapping[str, Any]) -> Dict[str, Any]:
        base = _read_yaml(self.config_path)
        return merge_llm_sections(
            base, llm,
            config_source=str(self.config_path),
            llm_source=str(self.llm_path),
        )

    def _validate(self, llm: Mapping[str, Any]) -> None:
        merged = self._merged(llm)
        try:
            validator = ConfigValidator(adapter_specs=self.adapter_specs_fn)
            canonical = validator.validate(ConfigLoader(merged))
            ConfigResolver(canonical).resolve()
        except ConfigurationError as exc:
            raise SettingsAdminError(str(exc)) from exc

    def _credential_presence(self, env: str) -> bool:
        if str(os.environ.get(env) or "").strip():
            return True
        return bool(str(_read_env(self.env_path).get(env) or "").strip())

    def snapshot(self) -> Dict[str, Any]:
        llm = self._llm_data()
        providers = dict(llm.get("providers") or {})
        models = dict(llm.get("models") or {})
        credentials = {}
        for profile, raw in (llm.get("credentials") or {}).items():
            raw = raw if isinstance(raw, Mapping) else {}
            env = str(raw.get("env") or "")
            credentials[str(profile)] = {
                "env": env,
                "present": self._credential_presence(env) if env else False,
            }
        specs = self.adapter_specs_fn()
        adapters = [
            {
                "name": name,
                "label": str(spec.get("label") or name),
                "requires_base_url": bool(spec.get("requires_base_url", True)),
                "requires_api_key": bool(spec.get("requires_api_key", True)),
                "default_base_url": str(
                    spec.get("default_base_url") or ""),
            }
            for name, spec in sorted(specs.items())
        ]
        return {
            "providers": providers,
            "models": models,
            "credentials": credentials,
            "adapters": adapters,
            "paths": {
                "config": str(self.config_path),
                "llm": str(self.llm_path),
                "env": str(self.env_path),
            },
            "restart_required": bool(
                self.restart_required
                or (self._initial_hash
                    and self._hash_sources() != self._initial_hash)
            ),
        }

    # ------------------------------------------------------------
    # write
    # ------------------------------------------------------------
    def _backup(self) -> None:
        if not self.llm_path.exists():
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.backup_dir / f"llm.{stamp}.yaml"
        try:
            target.write_text(
                self.llm_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    def _save_llm(self, llm: Mapping[str, Any]) -> None:
        payload = {
            "version": 1,
            **{key: dict(llm.get(key) or {}) for key in LLM_CONFIG_SECTIONS},
        }
        self._backup()
        text = yaml.safe_dump(
            payload, allow_unicode=True, sort_keys=False, width=1000)
        _atomic_write(self.llm_path, text)
        self.restart_required = True

    def _write_secret(self, env: str, value: str) -> None:
        env = env.strip()
        if not _ENV_NAME_RE.fullmatch(env):
            raise SettingsAdminError(
                "环境变量名必须是大写字母开头的大写字母/数字/下划线，"
                "例如 DEEPSEEK_KEY_AGENT_C。")
        value = str(value or "").strip()
        if not value:
            raise SettingsAdminError("API Key 不能为空。")
        lines: List[str] = []
        if self.env_path.exists():
            try:
                lines = self.env_path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise SettingsAdminError(f"读取 .env 失败：{exc}") from exc
        replaced = False
        out: List[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                out.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key == env:
                out.append(f"{env}={value}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"{env}={value}")
        _atomic_write(self.env_path, "\n".join(out).rstrip() + "\n",
                      mode=0o600)

    # ------------------------------------------------------------
    # typed actions
    # ------------------------------------------------------------
    def apply(self, action: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(payload or {})
        if action == "agent.create":
            return self._create_agent(payload)
        if action == "agent.update":
            return self._update_agent(payload)
        llm = self._llm_data()
        if action == "provider.upsert":
            self._upsert_provider(llm, payload)
        elif action == "provider.delete":
            self._delete_provider(llm, payload)
        elif action == "model.upsert":
            self._upsert_model(llm, payload)
        elif action == "model.delete":
            self._delete_model(llm, payload)
        elif action == "credential.upsert":
            self._upsert_credential(llm, payload)
        elif action == "credential.delete":
            self._delete_credential(llm, payload)
        else:
            raise SettingsAdminError(f"未知系统设置动作：{action!r}")
        self._validate(llm)
        self._save_llm(llm)
        api_key = str(payload.get("api_key") or "").strip()
        if action == "credential.upsert" and api_key:
            env = str((llm["credentials"].get(
                str(payload.get("profile") or "")) or {}).get("env") or "")
            self._write_secret(env, api_key)
        return self.snapshot()

    def _backup_config(self) -> None:
        if not self.config_path.exists():
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.backup_dir / f"config.{stamp}.yaml"
        try:
            target.write_text(
                self.config_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    def _create_agent(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """新增 Agent：只写 canonical `config.yaml`，重启后装配。"""
        agent_id = str(payload.get("agent_id") or "").strip()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise SettingsAdminError(
                "Agent ID 必须以小写字母开头，只包含小写字母/数字/下划线，"
                "长度 3–32。")
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 40:
            raise SettingsAdminError("Agent 名称不能为空，且不能超过 40 个字符。")
        model = str(payload.get("model") or "").strip()
        credential = str(payload.get("credential") or "").strip()
        try:
            balance = float(payload.get("initial_balance"))
        except (TypeError, ValueError) as exc:
            raise SettingsAdminError("初始余额必须是数字。") from exc
        if balance < 0 or balance > _MAX_AGENT_INITIAL_BALANCE:
            raise SettingsAdminError(
                f"初始余额必须在 0–{_MAX_AGENT_INITIAL_BALANCE:g} AIC 之间。")

        llm = self._llm_data()
        merged = self._merged(llm)
        agents = dict(merged.get("agents") or {})
        if agent_id in agents:
            raise SettingsAdminError(f"Agent 已存在：{agent_id}")
        if model not in (merged.get("models") or {}):
            raise SettingsAdminError(f"未知 model profile：{model!r}")
        if credential not in (merged.get("credentials") or {}):
            raise SettingsAdminError(f"未知 credential profile：{credential!r}")

        block = {
            "name": name,
            "runtime": True,
            "brain": {
                "model": model,
                "credential": credential,
                "system_prompt": (
                    f"你是 {name}，一个独立的经济主体。\n"
                    "你有自己的钱包、记忆和声誉。\n"
                    "你的智力来自外部 LLM，但经济决策必须遵守经济系统的规则。\n"
                ),
            },
            "economy": {"initial_balance": balance},
            "capabilities": {"skills": ["generalist"]},
        }
        snippet = yaml.safe_dump(
            {agent_id: block}, allow_unicode=True, sort_keys=False, width=1000)
        snippet = "\n".join(
            "  " + line for line in snippet.splitlines()) + "\n"

        try:
            original = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SettingsAdminError(f"读取 {self.config_path} 失败：{exc}") from exc
        if not re.search(r"^agents:\s*$", original, re.MULTILINE):
            raise SettingsAdminError("config.yaml 缺少顶层 agents: 段。")
        updated = original
        if not updated.endswith("\n"):
            updated += "\n"
        updated += "\n" + snippet

        self._backup_config()
        _atomic_write(self.config_path, updated)
        try:
            self._validate(llm)
        except Exception:
            _atomic_write(self.config_path, original)
            raise
        self.restart_required = True
        result = self.snapshot()
        result["created_agent"] = agent_id
        return result

    def _update_agent(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """修改 Agent 的模型 / 凭据 / life：只改 canonical `config.yaml`。"""
        agent_id = str(payload.get("agent_id") or "").strip()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise SettingsAdminError("Agent ID 不合法。")
        model = str(payload.get("model") or "").strip()
        credential = str(payload.get("credential") or "").strip()
        raw_life = payload.get("life")
        if raw_life is not None and not isinstance(raw_life, Mapping):
            raise SettingsAdminError("life 必须是对象。")

        llm = self._llm_data()
        merged = self._merged(llm)
        agent_raw = dict((merged.get("agents") or {}).get(agent_id) or {})
        if not agent_raw:
            raise SettingsAdminError(f"未知 Agent：{agent_id!r}")
        brain_raw = dict(agent_raw.get("brain") or {})
        # 只改 life 时允许省略 model / credential：沿用当前值。
        if not model:
            model = str(brain_raw.get("model") or "").strip()
        if not credential:
            credential = str(brain_raw.get("credential") or "").strip()
        if not model:
            raise SettingsAdminError("model 不能为空。")
        if not credential:
            raise SettingsAdminError("credential 不能为空。")

        models = dict(merged.get("models") or {})
        credentials = dict(merged.get("credentials") or {})
        if model not in models:
            raise SettingsAdminError(f"未知 model profile：{model!r}")
        if credential not in credentials:
            raise SettingsAdminError(
                f"未知 credential profile：{credential!r}")

        life = None
        if raw_life is not None:
            # 合并已有 life，避免 UI 没暴露的字段（例如 allowed_actions）
            # 在一次保存里被静默删除。
            existing_life = dict(agent_raw.get("life") or {})
            incoming = dict(raw_life)
            policy = LifePolicy.from_raw({**existing_life, **incoming})
            normalized = policy.to_dict()
            life = dict(existing_life)
            for key in _AGENT_LIFE_UI_KEYS:
                if key in incoming or key in existing_life:
                    life[key] = normalized[key]
            # 只有配置里原本就有这些扩展字段时才写回，避免默认值噪音。
            for key in ("reply_max_chars", "allowed_actions"):
                if key in existing_life:
                    life[key] = normalized[key]

        try:
            original = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SettingsAdminError(
                f"读取 {self.config_path} 失败：{exc}") from exc
        updated = _patch_agent_brain(
            original, agent_id, model=model, credential=credential)
        if life is not None:
            updated = _patch_agent_life(updated, agent_id, life)
        if updated == original:
            raise SettingsAdminError(
                f"Agent {agent_id} 的配置没有发生变化。")

        self._backup_config()
        _atomic_write(self.config_path, updated)
        try:
            self._validate(llm)
        except Exception:
            _atomic_write(self.config_path, original)
            raise
        self.restart_required = True
        result = self.snapshot()
        result["updated_agent"] = agent_id
        return result

    def _upsert_provider(
        self, llm: Dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        name = str(payload.get("name") or "").strip()
        if not _PROFILE_RE.fullmatch(name):
            raise SettingsAdminError("服务商 ID 不合法。")
        adapter = str(payload.get("adapter") or "").strip()
        specs = self.adapter_specs_fn()
        if adapter not in specs:
            raise SettingsAdminError(
                f"未知 adapter：{adapter!r}；已知：{sorted(specs)}")
        base_url = str(payload.get("base_url") or "").strip()
        spec = specs[adapter]
        if not base_url:
            # 留空 = 使用 adapter 自带的默认端点。这样选择 deepseek /
            # doubao / bailian 时不必重复抄一遍官方 URL；没有默认端点的
            # openai_compatible / mock 仍然要求显式填写（或不需要）。
            base_url = str(spec.get("default_base_url") or "").strip()
        if spec.get("requires_base_url", True) and not base_url:
            raise SettingsAdminError(f"adapter {adapter} 需要 base_url。")
        timeout = float(payload.get("timeout_seconds") or 300)
        if timeout <= 0:
            raise SettingsAdminError("timeout_seconds 必须 > 0。")
        request = payload.get("request") or {}
        if isinstance(request, str):
            try:
                request = yaml.safe_load(request) or {}
            except yaml.YAMLError as exc:
                raise SettingsAdminError(f"request JSON 不合法：{exc}") from exc
        if not isinstance(request, dict):
            raise SettingsAdminError("request 必须是对象。")
        raw = {
            "adapter": adapter,
            "timeout_seconds": timeout,
        }
        if base_url:
            raw["base_url"] = base_url
        default_model = str(payload.get("default_model") or "").strip()
        if default_model:
            raw["default_model"] = default_model
        if request:
            raw["request"] = request
        providers = dict(llm.get("providers") or {})
        providers[name] = raw
        llm["providers"] = providers

    def _delete_provider(
        self, llm: Dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        name = str(payload.get("name") or "").strip()
        providers = dict(llm.get("providers") or {})
        if name not in providers:
            raise SettingsAdminError(f"未知服务商：{name!r}")
        users = [
            profile for profile, raw in (llm.get("models") or {}).items()
            if str((raw or {}).get("provider") or "") == name
        ]
        if users:
            raise SettingsAdminError(
                f"服务商 {name} 仍被模型使用：{users}。请先迁移或删除模型。")
        providers.pop(name)
        llm["providers"] = providers

    def _upsert_model(
        self, llm: Dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        profile = str(payload.get("profile") or "").strip()
        if not _PROFILE_RE.fullmatch(profile):
            raise SettingsAdminError("模型 Profile ID 不合法。")
        provider = str(payload.get("provider") or "").strip()
        if provider not in (llm.get("providers") or {}):
            raise SettingsAdminError(f"未知服务商：{provider!r}")
        model_id = str(payload.get("model_id") or "").strip()
        if not model_id:
            raise SettingsAdminError("model_id 不能为空。")
        context_window = int(payload.get("context_window") or 0)
        max_output = int(payload.get("max_output_tokens") or 0)
        if context_window <= 0:
            raise SettingsAdminError("context_window 必须 > 0。")
        if max_output <= 0:
            raise SettingsAdminError("max_output_tokens 必须 > 0。")
        if max_output > context_window:
            raise SettingsAdminError(
                "max_output_tokens 不能大于 context_window。")
        models = dict(llm.get("models") or {})
        raw_levels = payload.get("thinking_levels")
        if raw_levels is None:
            raw_levels = ["off"]
        if isinstance(raw_levels, str):
            raw_levels = [x.strip() for x in raw_levels.split(",") if x.strip()]
        if not isinstance(raw_levels, list,):
            raise SettingsAdminError("thinking_levels 必须是列表。")
        levels = [str(x) for x in raw_levels]
        unknown = [x for x in levels if x not in THINKING_LEVELS]
        if unknown:
            raise SettingsAdminError(
                f"未知 thinking level：{unknown}；允许：{list(THINKING_LEVELS)}。")
        if not levels:
            raise SettingsAdminError("thinking_levels 不能为空。")

        # 🔒 这里必须**合并**，不能整体覆盖：
        #    UI 表单只编辑一部分字段（例如没有 supports_image_input），
        #    整体覆盖会把用户手工写的 / 未来新增的字段静默删掉。
        existing = dict(models.get(profile) or {})
        updates = {
            "provider": provider,
            "model_id": model_id,
            "context_window": context_window,
            "max_output_tokens": max_output,
            "supports_reasoning": bool(payload.get("supports_reasoning")),
            "thinking_levels": levels,
        }
        raw_tool_call_format = payload.get("tool_call_format")
        if raw_tool_call_format is not None and str(
                raw_tool_call_format).strip():
            # 显式声明才写入；空值 / 缺省 = 保留 existing 里的值，
            # 原本没有就交给 Provider 默认值（`llm/` 的知识）。
            tool_call_format = str(raw_tool_call_format).strip().lower()
            if tool_call_format not in TOOL_CALL_FORMATS:
                raise SettingsAdminError(
                    f"未知 tool_call_format：{raw_tool_call_format!r}；"
                    f"允许：{list(TOOL_CALL_FORMATS)}。")
            updates["tool_call_format"] = tool_call_format
        existing.update(updates)
        models[profile] = existing
        llm["models"] = models

    def _delete_model(
        self, llm: Dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        profile = str(payload.get("profile") or "").strip()
        models = dict(llm.get("models") or {})
        if profile not in models:
            raise SettingsAdminError(f"未知模型：{profile!r}")
        base = _read_yaml(self.config_path)
        agents = base.get("agents") or {}
        users = [
            aid for aid, raw in agents.items()
            if str(((raw or {}).get("brain") or {}).get("model") or "")
            == profile
        ]
        providers = dict(llm.get("providers") or {})
        defaults = [
            name for name, raw in providers.items()
            if str((raw or {}).get("default_model") or "") == profile
        ]
        if users or defaults:
            raise SettingsAdminError(
                f"模型 {profile} 仍被引用：agents={users}；"
                f"provider default={defaults}。")
        models.pop(profile)
        llm["models"] = models

    def _upsert_credential(
        self, llm: Dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        profile = str(payload.get("profile") or "").strip()
        if not _PROFILE_RE.fullmatch(profile):
            raise SettingsAdminError("凭据 Profile ID 不合法。")
        env = str(payload.get("env") or "").strip()
        if not _ENV_NAME_RE.fullmatch(env):
            raise SettingsAdminError(
                "环境变量名必须是大写字母开头的大写字母/数字/下划线。")
        api_key = str(payload.get("api_key") or "").strip()
        credentials = dict(llm.get("credentials") or {})
        # 新凭据或换了 env 名时必须提供 key；编辑旧凭据可留空表示不替换。
        if api_key == "" and not self._credential_presence(env):
            raise SettingsAdminError(
                f"环境变量 {env} 还没有值；请填写 API Key。")
        credentials[profile] = {"env": env}
        llm["credentials"] = credentials

    def _delete_credential(
        self, llm: Dict[str, Any], payload: Mapping[str, Any],
    ) -> None:
        profile = str(payload.get("profile") or "").strip()
        credentials = dict(llm.get("credentials") or {})
        if profile not in credentials:
            raise SettingsAdminError(f"未知凭据：{profile!r}")
        base = _read_yaml(self.config_path)
        users = [
            aid for aid, raw in (base.get("agents") or {}).items()
            if str(((raw or {}).get("brain") or {}).get("credential") or "")
            == profile
        ]
        if users:
            raise SettingsAdminError(
                f"凭据 {profile} 仍被 Agent 使用：{users}。")
        credentials.pop(profile)
        llm["credentials"] = credentials


__all__ = ["SettingsAdminError", "SystemConfigAdmin"]
