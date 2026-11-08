"""
Public Tool Library —— 只读工具清单
=====================================

【它是什么】

    `resources/public/tools/manifests/*.json` 的加载器。
    工具由**项目/人类**预装，Agent 只能使用，不能改写、下载或安装。

【为什么工具库不是 Agent 私有空间】

    · 公共工具库是**只读**的；Agent 没有写入口。
    · 私有空间只是宿主机目录，不是权限沙箱；
      把任意下载/安装塞进去会绕过 allowlist 与 checksum。
    · 每次执行都创建独立 run 目录；工具二进制共享，运行状态不共享。

【V1 边界】

    ✅ 只执行 manifest 中声明的 `command`
    ✅ `subprocess` 列表参数，`shell=False`
    ✅ 每次 run 独立 cwd / HOME / TMPDIR
    ✅ 超时、输出上限、环境变量最小化
    ❌ 任意 URL 下载 / pip/npm install / 任意 shell / 宿主机服务控制
    ❌ 不是 OS 级安全沙箱（见 TOOL_RUNTIME_V1_DESIGN.md）
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


class ToolLibraryError(Exception):
    """工具清单不合法或工具不存在。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个公共工具的不可变声明。"""

    name: str
    command: tuple[str, ...]
    description: str = ""
    concurrency: str = "shared-readonly"
    env: Mapping[str, str] = field(default_factory=dict)
    bootstrap_files: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not str(self.name).strip():
            raise ToolLibraryError("tool.name 不能为空")
        if not self.command or not all(isinstance(c, str) and c for c in self.command):
            raise ToolLibraryError(f"{self.name}: command 必须是非空字符串列表")
        for key, value in self.bootstrap_files.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ToolLibraryError(f"{self.name}: bootstrap_files 必须是 str→str")
            if key.startswith(("/", "\\", "..")) or "\\" in key:
                raise ToolLibraryError(
                    f"{self.name}: bootstrap 路径必须是相对路径：{key!r}")

    def public_catalog(self) -> Dict[str, Any]:
        """给 Agent/公共资料看的视图（不含宿主机绝对路径）。"""
        return {
            "name": self.name,
            "description": self.description,
            "concurrency": self.concurrency,
        }


class ToolLibrary:
    """从目录加载工具清单；只读。"""

    def __init__(self, project_root: Path, manifests_dir: Path):
        self.project_root = Path(project_root).resolve()
        self.manifests_dir = Path(manifests_dir).resolve()
        self._tools: Dict[str, ToolSpec] = {}

    @classmethod
    def from_directory(cls, project_root: Path,
                       manifests_dir: Optional[Path] = None) -> "ToolLibrary":
        lib = cls(project_root, manifests_dir or
                  Path(project_root) / "resources" / "public" / "tools" / "manifests")
        lib.reload()
        return lib

    # ------------------------------------------------------------
    def reload(self) -> None:
        tools: Dict[str, ToolSpec] = {}
        if self.manifests_dir.is_dir():
            for path in sorted(self.manifests_dir.glob("*.json")):
                spec = self._load_one(path)
                if spec.name in tools:
                    raise ToolLibraryError(
                        f"工具名重复：{spec.name}（{path.name}）")
                tools[spec.name] = spec
        self._tools = tools

    def _load_one(self, path: Path) -> ToolSpec:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                              # noqa: BLE001
            raise ToolLibraryError(f"读取工具清单失败 {path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ToolLibraryError(f"{path.name}: 顶层必须是对象")

        command = raw.get("command")
        if not isinstance(command, list) or not command:
            raise ToolLibraryError(f"{path.name}: command 必须是非空列表")

        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise ToolLibraryError(f"{path.name}: env 必须是对象")
        bootstrap = raw.get("bootstrap_files") or {}
        if not isinstance(bootstrap, dict):
            raise ToolLibraryError(f"{path.name}: bootstrap_files 必须是对象")

        return ToolSpec(
            name=str(raw.get("name") or path.stem),
            command=tuple(self._resolve_token(str(c)) for c in command),
            description=str(raw.get("description") or ""),
            concurrency=str(raw.get("concurrency") or "shared-readonly"),
            env={str(k): self._resolve_env_value(str(v)) for k, v in env.items()},
            bootstrap_files={str(k): str(v) for k, v in bootstrap.items()},
        )

    def _resolve_token(self, token: str) -> str:
        """把项目相对路径解析成绝对路径；裸命令名留给 PATH。

        🔒 用 absolute() 而**不是** resolve()：`.venv/bin/python` 是
           symlink，resolve() 会把它变成 Homebrew 的裸 Python，
           从而丢掉 venv 的 site-packages（pytest 就找不到了）。
        """
        if not token or token.startswith("-"):
            return token
        candidate = Path(token)
        if candidate.is_absolute():
            return str(candidate)
        project_candidate = (self.project_root / candidate).absolute()
        if project_candidate.exists():
            return str(project_candidate)
        return token

    def _resolve_env_value(self, value: str) -> str:
        if not value:
            return value
        candidate = Path(value)
        if candidate.is_absolute():
            return value
        project_candidate = (self.project_root / candidate).absolute()
        return str(project_candidate) if project_candidate.exists() else value

    # ------------------------------------------------------------
    def names(self) -> List[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(str(name or ""))
        if spec is None:
            raise ToolLibraryError(
                f"工具 {name!r} 不在公共工具库中；可用：{self.names()}")
        return spec

    def available(self, name: str) -> bool:
        try:
            spec = self.get(name)
        except ToolLibraryError:
            return False
        for token in spec.command:
            if Path(token).is_absolute() and not Path(token).is_file():
                return False
        first = spec.command[0]
        if Path(first).is_absolute():
            return True
        return shutil.which(first) is not None

    def public_catalog(self) -> List[Dict[str, Any]]:
        return [self._tools[n].public_catalog() for n in self.names()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return str(name) in self._tools
