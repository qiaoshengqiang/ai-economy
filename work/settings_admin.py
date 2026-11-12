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
from core.paths import (
    ENV_FILE, GLOBAL_CONFIG_FILE, GLOBAL_LLM_CONFIG_FILE,
)
from llm.router import adapter_specs

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


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
            os.chmod(tmp, mode)
        except OSError:
            pass
    tmp.replace(path)


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
        self._initial_hash = self._hash_llm()

    # ------------------------------------------------------------
    # read
    # ------------------------------------------------------------
    def _hash_llm(self) -> str:
        try:
            return hashlib.sha256(self.llm_path.read_bytes()).hexdigest()
        except OSError:
            return ""

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
                "requires_base_url": bool(spec.get("requires_base_url", True)),
                "requires_api_key": bool(spec.get("requires_api_key", True)),
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
                or (self._initial_hash and self._hash_llm() != self._initial_hash)
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
        if specs[adapter].get("requires_base_url", True) and not base_url:
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
        models[profile] = {
            "provider": provider,
            "model_id": model_id,
            "context_window": context_window,
            "max_output_tokens": max_output,
            "supports_reasoning": bool(payload.get("supports_reasoning")),
            "thinking_levels": levels,
        }
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
