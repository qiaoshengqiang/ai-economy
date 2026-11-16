"""
PiWorkRuntime —— Pi 作为 Work 层执行底座
=========================================

    Task Core（AIEOS）
        ↓
    真实 Task 目录 + memory 文件协议
        ↓
    Pi RPC（原生 read/write/edit/bash）
        ↓
    agent_settled
        ↓
    manifest / RESULT.md / memory ingest
        ↓
    Task Core COMPLETED

🔒 这条路径**不使用** Task Shared Context / Alignment / AIEOS action dispatcher。
    Task Core 仍然是状态权威；Pi 只负责执行和落盘。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core.identity import format_agent_identity
from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef
from llm.base import LLMResponse, TOKEN_SOURCE_API
from llm.session import build_session_id, task_scope
from work.llm_proxy import AIEOSLLMProxy
from work.lifecycle import WorkLifecycle
from work.pi_comm import PiInboxBridge, PiOutboxBridge
from work.pi_memory import PiMemoryBridge
from work.pi_rpc import PiRpcClient, PiRunResult
from work.prompt import render_brief_sections
from work.roles import WorkResult
from work.task_snapshot import SnapshotRecord, TaskSnapshotStore
from work.worker_roles import render_role_prompt
from work.task_directory import TaskDirectory


@dataclass(frozen=True)
class PiProviderSpec:
    """Pi LLM Proxy 需要的一个**已解析端点**。

    🔒 不再根据 provider 名字写死 base_url。每个 Agent 的
       model profile 解析出 provider 后，直接使用这个 provider
       自己的 base_url / chat_path / request（extra_headers 等）。
    """

    provider: str
    model: str
    adapter: str
    base_url: str
    chat_path: str
    request: Mapping[str, Any]

    @property
    def chat_url(self) -> str:
        base = str(self.base_url or "").rstrip("/")
        path = str(self.chat_path or "/chat/completions")
        if not path.startswith("/"):
            path = "/" + path
        return base + path if base else ""


class PiWorkRuntime:
    def __init__(
        self,
        runtime: Any,
        task: Task,
        *,
        task_directory: TaskDirectory,
        pi_cli_path: str = "pi",
        pi_provider: str = "",
        pi_model: str = "",
        timeout_seconds: float = 1800.0,
        proxy_timeout_seconds: float = 300.0,
        should_stop: Any = None,
        round_sink: Any = None,
        event_sink: Any = None,
        pi_agent_dir: Optional[Path] = None,
        use_llm_proxy: bool = True,
        upstream_base_url: str = "",
        inbox_bridge: Any = None,
        role_prompt: str = "",
        role: Any = None,
        extra_args: Optional[List[str]] = None,
        phase_plan: Optional[List[str]] = None,
        thinking: str = "auto",
        work_profile: str = "",
        work_mode: str = "workflow",
        rework_scope: str = "",
        project_lock_manager: Any = None,
    ) -> None:
        if task.mode != TaskMode.SOLO:
            raise ValueError(
                f"PiWorkRuntime 只接受 SOLO Task；{task.task_id} 是 {task.mode}")
        self.rt = runtime
        self.task = task
        self.task_directory = task_directory
        #: project 模式 = 用户项目目录；否则 = Task Workspace。
        self.workdir = Path(task_directory.workdir)
        self.project_mode = bool(task_directory.project_mode)
        self.project_lock_manager = project_lock_manager
        self.pi_cli_path = str(pi_cli_path or "pi")
        self.pi_provider = str(pi_provider or "")
        self.pi_model = str(pi_model or "")
        self.timeout_seconds = float(timeout_seconds)
        self.proxy_timeout_seconds = float(proxy_timeout_seconds)
        self.should_stop = should_stop or (lambda: False)
        self.round_sink = round_sink
        self.event_sink = event_sink
        self.pi_agent_dir = Path(pi_agent_dir) if pi_agent_dir else None
        self.use_llm_proxy = bool(use_llm_proxy)
        self.upstream_base_url = str(upstream_base_url or "")
        self.role_prompt = str(role_prompt or "").strip()
        self.role = role
        # Work 只加载 AIEOS 自己的角色/任务上下文：
        #   --no-skills        不加载用户全局 skills（网文/写作等与任务无关）
        #   --no-context-files 不加载项目 AGENTS.md / CLAUDE.md
        self.extra_args = list(
            extra_args if extra_args is not None
            else ["--no-skills", "--no-context-files"])
        self._explicit_phase_plan = (
            list(phase_plan) if phase_plan is not None else None)
        self.phase_plan = list(
            self._explicit_phase_plan
            or ["prototype", "engineering", "evolution"])
        acceptance = getattr(task, "acceptance", None) or {}
        self.work_profile = str(
            work_profile
            or (acceptance.get("work_profile") if isinstance(acceptance, dict) else "")
            or "standard").strip().lower()
        mode = str(work_mode or "workflow").strip().lower()
        if mode not in ("workflow", "autonomous"):
            mode = "workflow"
        self.work_mode = mode
        self.rework_scope = str(rework_scope or "").strip().lower()
        self.thinking = str(thinking or "auto").strip().lower()
        self._work_state: Dict[str, Any] = {}
        self._session_id: str = ""
        self._recent_tool_results: List[str] = []
        self._resumed = False
        self._resume_reason = ""
        communication = getattr(runtime, "communication", None)
        actor = task.contractor or task.creator
        # Phase 2：UI 会注入一个 Agent 级常驻 Inbox Bridge。
        # 没注入时（测试 / 旧调用）仍使用任务级临时 bridge。
        self._owns_inbox_bridge = inbox_bridge is None
        self._inbox_bridge = inbox_bridge
        if self._inbox_bridge is None and communication is not None:
            self._inbox_bridge = PiInboxBridge(
                communication,
                agent_id=actor,
                aieos_dir=task_directory.aieos,
            )
        self._outbox_bridge = (
            PiOutboxBridge(
                communication,
                actor=actor,
                task_id=task.task_id,
                aieos_dir=task_directory.aieos,
            )
            if communication is not None else None
        )
        self.lifecycle = WorkLifecycle(
            runtime.tasks,
            communication=communication,
            task_service=getattr(runtime, "task_service", None),
        )
        self.memory = PiMemoryBridge(
            runtime,
            agent_id=task.contractor or task.creator,
            task=task,
            task_workspace=self.workdir,
        )
        # Task 私有快照仓库：不写 AIEOS 主仓库，也不在 workspace 里放 .git。
        self.snapshots = TaskSnapshotStore(task_directory)
        self._snapshot_error = ""

    # ------------------------------------------------------------
    def _agent(self) -> Any:
        actor = self.task.contractor or self.task.creator
        try:
            return self.rt.economy.agent(actor)
        except Exception:                                     # noqa: BLE001
            return None

    def _provider_and_model(self) -> tuple[str, str]:
        agent = self._agent()
        brain = getattr(agent, "brain", None)
        provider = self.pi_provider or str(
            getattr(brain, "provider", "") or "deepseek")
        model = self.pi_model or str(
            getattr(brain, "model", "") or "deepseek-flash")
        return provider, model

    def _resolved_agent(self) -> Any:
        resolved = getattr(self.rt, "resolved_config", None)
        if resolved is None:
            return None
        actor = self.task.contractor or self.task.creator
        try:
            return resolved.agent(actor)
        except Exception:                                     # noqa: BLE001
            return None

    def _agent_name(self) -> str:
        """显示名：resolved config 优先，其次 Economy 身份。"""
        actor = self.task.contractor or self.task.creator
        resolved_agent = self._resolved_agent()
        if resolved_agent is not None:
            name = str(getattr(resolved_agent, "name", "") or "").strip()
            if name:
                return name
        return str(getattr(self._agent(), "name", "") or "").strip()

    def _agent_identity(self) -> str:
        actor = self.task.contractor or self.task.creator
        return format_agent_identity(self._agent_name(), actor)

    def _brief_sections(self) -> str:
        """Pi 也能看到 Front Brief 的来源分层（来自 Task.acceptance）。"""
        acceptance = getattr(self.task, "acceptance", None)
        brief = (
            acceptance.get("brief")
            if isinstance(acceptance, dict) else None)
        if not isinstance(brief, dict) or not brief:
            return ""
        return render_brief_sections(brief)

    def _provider_spec(self) -> PiProviderSpec:
        """解析当前 Agent 真正要用的 provider 端点。

        优先级：
            1. CLI 覆盖 `--pi-provider/--pi-model`（如果设置了）
            2. Agent 的 resolved model/profile
            3. 旧 runtime 的 router.provider_config（测试 / 兼容）

        🔒 base_url / chat_path / request 全部来自 provider 自己的配置，
           不再按 provider 名字写死 DeepSeek。
        """
        resolved = getattr(self.rt, "resolved_config", None)
        resolved_agent = self._resolved_agent()
        provider_name = ""
        model = ""
        if resolved_agent is not None:
            provider_name = str(getattr(
                getattr(resolved_agent, "provider", None), "name", "") or "")
            model = str(getattr(resolved_agent, "model_id", "") or "")
        provider_name = self.pi_provider or provider_name
        model = self.pi_model or model

        provider_obj = None
        if resolved is not None and provider_name:
            try:
                provider_obj = resolved.provider(provider_name)
            except Exception:                                 # noqa: BLE001
                provider_obj = None

        if provider_obj is not None:
            base_url = str(getattr(provider_obj, "base_url", "") or "")
            if self.upstream_base_url:
                base_url = self.upstream_base_url.rstrip("/")
            return PiProviderSpec(
                provider=provider_name,
                model=model,
                adapter=str(getattr(provider_obj, "adapter", "") or ""),
                base_url=base_url,
                chat_path=str(getattr(
                    provider_obj, "chat_path", "") or "/chat/completions"),
                request=dict(getattr(provider_obj, "request", None) or {}),
            )

        # 兼容没有 resolved_config 的测试/旧调用方。
        fallback_provider, fallback_model = self._provider_and_model()
        provider_name = self.pi_provider or fallback_provider
        model = self.pi_model or fallback_model
        config: Dict[str, Any] = {}
        router = getattr(self.rt, "router", None)
        if router is not None:
            try:
                config = dict(router.provider_config(provider_name) or {})
            except Exception:                                 # noqa: BLE001
                config = {}
        request = {
            key: config[key]
            for key in ("extra_headers", "extra_body", "max_tokens_field")
            if key in config
        }
        return PiProviderSpec(
            provider=provider_name,
            model=model,
            adapter=str(config.get("adapter") or ""),
            base_url=(
                self.upstream_base_url.rstrip("/")
                if self.upstream_base_url
                else str(config.get("base_url") or "")),
            chat_path=str(config.get("chat_path") or "/chat/completions"),
            request=request,
        )

    def _upstream_base_url(self) -> str:
        """兼容旧调用：返回解析出的 provider base_url。"""
        if self.upstream_base_url:
            return self.upstream_base_url
        return self._provider_spec().base_url

    def _pi_env(
        self, provider: str, *, include_key: bool = True,
    ) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({
            "AIEOS_TASK_WORKSPACE": str(self.workdir),
            "AIEOS_PRIVATE_WORKSPACE": str(
                self.memory.private_workspace or ""),
            "AIEOS_MEMORY_DIR": str(self.memory.memory_dir or ""),
            "AIEOS_TASK_ID": self.task.task_id,
            "AIEOS_AGENT_ID": self.task.contractor or self.task.creator,
        })
        if self.pi_agent_dir is not None:
            env["PI_CODING_AGENT_DIR"] = str(self.pi_agent_dir)
        # Pi 的 provider 凭据：AIEOS 仍然持有 Agent 的 credential。
        agent = self._agent()
        brain = getattr(agent, "brain", None)
        credential = None
        try:
            credential = brain.credential_for(provider) if brain else None
        except Exception:                                     # noqa: BLE001
            credential = None
        material = str(getattr(credential, "material", "") or "")
        if include_key and material:
            key = (
                "DEEPSEEK_API_KEY" if provider == "deepseek"
                else "OPENAI_API_KEY")
            env[key] = material
        return env

    def _credential_material(self, provider: str) -> str:
        agent = self._agent()
        brain = getattr(agent, "brain", None)
        try:
            credential = brain.credential_for(provider) if brain else None
        except Exception:                                     # noqa: BLE001
            return ""
        return str(getattr(credential, "material", "") or "")

    def _model_limits(self) -> tuple[int, int, bool, tuple]:
        """模型上下文 / 最大输出 / reasoning / 思考档位。"""
        agent = self._agent()
        settings = getattr(getattr(agent, "brain", None), "settings", None)
        context_window = int(
            getattr(settings, "context_window", None) or 600000)
        # Pi Work 统一使用模型的最大输出上限；Agent 的较低 max_tokens
        # 不在这里二次截断。阶段可以通过 prompt 自行输出更短。
        max_output = int(
            getattr(settings, "max_output_tokens", None) or 16384)
        reasoning = bool(getattr(settings, "supports_reasoning", True))
        levels = tuple(getattr(settings, "thinking_levels", ()) or ())
        if not levels:
            levels = (("off", "low", "medium", "high", "max")
                      if reasoning else ("off",))
        return context_window, max_output, reasoning, levels

    def _write_pi_models(self, base_url: str, model: str) -> Path:
        directory = self.task_directory.aieos / "pi-agent"
        context_window, max_output, reasoning, levels = self._model_limits()
        level_map = {
            level: (level if level in levels else None)
            for level in ("off", "minimal", "low", "medium",
                          "high", "xhigh", "max")
        }
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "$schema": "https://pi.dev/schemas/models.schema.json",
            "providers": {
                "aieos-proxy": {
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "apiKey": "aieos-proxy",
                    "models": [{
                        "id": model,
                        "name": f"AIEOS proxy / {model}",
                        "reasoning": reasoning,
                        "thinkingLevelMap": level_map,
                        "input": ["text"],
                        "contextWindow": context_window,
                        "maxTokens": max_output,
                        "cost": {
                            "input": 0, "output": 0,
                            "cacheRead": 0, "cacheWrite": 0,
                        },
                    }],
                },
            },
        }
        (directory / "models.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8")
        # 显式固定 Pi 的自动压缩设置，不依赖 Pi 未来默认值。
        # 阈值 = contextWindow - reserveTokens；keepRecentTokens 保留最近上下文。
        (directory / "settings.json").write_text(
            json.dumps({
                "compaction": {
                    "enabled": True,
                    "reserveTokens": 16384,
                    "keepRecentTokens": 20000,
                },
            }, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return directory

    # ------------------------------------------------------------
    # Work phases
    # ------------------------------------------------------------
    _PHASE_LABELS = {
        "direct": "直接完成",
        "prototype": "原型生成",
        "engineering": "实现验证",
        "evolution": "持续迭代",
        "autonomous": "自主工作",
    }
    _THINKING_ORDER = (
        "max", "xhigh", "high", "medium", "low", "minimal", "off")

    def _base_phase_plan(self) -> List[str]:
        if self.work_mode == "autonomous":
            return ["autonomous"]
        if self._explicit_phase_plan is not None:
            return list(self._explicit_phase_plan)
        profile = self.work_profile or "standard"
        return list({
            "direct": ["direct"],
            "standard": ["prototype", "engineering"],
            "creative_product": ["prototype", "engineering", "evolution"],
        }.get(profile, ["prototype", "engineering"]))

    @staticmethod
    def _pi_session_id(
        task_id: str, iteration: int, state: Dict[str, Any],
    ) -> str:
        """同一 iteration 复用 session；新 iteration 开新 session。

        中断恢复必须复用原 session，才能接着上一段对话；
        返工 / Reopen 则使用新 session，旧成果通过
        RESULT / ENGINEERING / PROTOTYPE / TASK_DOC 交接，
        避免把整个旧 transcript 反复塞进新 iteration。
        """
        task_id = str(task_id or "")
        iteration = int(iteration or 1)
        state = state or {}
        state_iter = int(state.get("iteration") or 0)
        existing = str(state.get("session_id") or "").strip()
        if existing and state_iter == iteration:
            return existing
        if iteration <= 1:
            return task_id
        return f"{task_id}.i{iteration}"

    def _phase_plan_for_run(
        self, state: Dict[str, Any],
    ) -> tuple[List[str], bool, str]:
        """返回 (phase_plan, resumed, resume_reason)。"""
        if self.work_mode == "autonomous":
            phase = str(state.get("phase") or "")
            status = str(
                state.get("phase_status") or state.get("status") or "")
            state_iter = int(state.get("iteration") or 0)
            if (phase == "autonomous" and status != "complete"
                    and state_iter == int(self.task.iteration or 1)):
                return ["autonomous"], True, (
                    str(state.get("resume_reason") or "task_resume"))
            if state_iter and int(self.task.iteration or 1) > state_iter:
                scope = self.rework_scope or "enhance"
                return ["autonomous"], True, f"rework:{scope}"
            return ["autonomous"], False, ""
        phases = self._base_phase_plan()
        known = ["direct"] + [p for p in phases if p != "direct"]
        phase = str(state.get("phase") or "")
        status = str(state.get("phase_status") or state.get("status") or "")
        state_iter = int(state.get("iteration") or 0)
        if (phase in known and status != "complete"
                and state_iter == int(self.task.iteration or 1)):
            return known[known.index(phase):], True, (
                str(state.get("resume_reason") or "task_resume"))
        if state_iter and int(self.task.iteration or 1) > state_iter:
            scope = self.rework_scope or "enhance"
            plan = {
                "fix": ["direct"],
                "enhance": ["engineering", "evolution"],
                "redesign": ["prototype", "engineering", "evolution"],
            }.get(scope, ["engineering", "evolution"])
            return plan, True, f"rework:{scope}"
        profile = self.work_profile or "standard"
        plan = {
            "direct": ["direct"],
            "standard": ["prototype", "engineering"],
            "creative_product": ["prototype", "engineering", "evolution"],
        }.get(profile, ["prototype", "engineering"])
        return plan, False, ""

    def _phase_thinking(self, phase: str) -> str:
        levels = tuple(self._model_limits()[3])
        supported = set(levels)
        rank = {
            "off": 0, "minimal": 1, "low": 2, "medium": 3,
            "high": 4, "xhigh": 5, "max": 6,
        }

        def pick(preferred: str, fallback: str = "off") -> str:
            if preferred in supported:
                return preferred
            if not supported:
                return fallback
            target = rank.get(preferred, 3)
            # 优先选择离目标最近的一档；距离相同则选更高的一档，
            # 避免把 engineering 的 high 静默降成 low。
            return min(
                supported,
                key=lambda level: (
                    abs(rank[level] - target),
                    rank[level] < target,
                ),
            )

        # 自主模式没有阶段策略：按 Agent 卡片；Auto 用 high。
        if self.work_mode == "autonomous":
            if self.thinking and self.thinking != "auto":
                return pick(self.thinking)
            return pick("high")
        # Prototype 固定最高档；Agent 卡片只影响后续阶段。
        if phase == "prototype":
            return pick("max")
        if self.thinking and self.thinking != "auto":
            return pick(self.thinking)
        defaults = {
            "direct": "high", "engineering": "high", "evolution": "medium",
        }
        return pick(defaults.get(phase, "medium"))

    def _workspace_brief(self) -> str:
        """工作目录 / 私有 workspace / 记忆目录的用途（正向表述）。"""
        paths = self.memory.paths()
        if self.project_mode:
            return f"""你的工作目录是用户已有的项目目录：
{self.workdir}

这是用户提供的现有项目。直接读取、修改、运行其中的代码；
在项目原有结构、依赖和约定上增量修改。
你的私有 workspace 是：
{paths['private_workspace']}
你的记忆目录是：
{paths['memory_dir']}

项目目录用于真实的代码与交付物；私有 workspace 放你自己的草稿、笔记、
中间产物和可复用的小脚本，可以按 notes/、scratch/、tools/ 分目录。
操作范围：当前项目目录和自己的私有 workspace。
"""
        return f"""你的工作目录是 Task Workspace：
{paths['task_workspace']}

你的私有 workspace 是：
{paths['private_workspace']}
你的记忆目录是：
{paths['memory_dir']}

Task Workspace 放本次任务的交付物和验收产物。
私有 workspace 放你自己的草稿、笔记、中间产物和可复用的小脚本，
可以按 notes/、scratch/、tools/ 分目录，也可以直接用 bash 运行里面的脚本。
大段内容先写进私有 workspace，再分步写入 Task Workspace。
操作范围：当前 Task Workspace 和自己的私有 workspace。
"""

    def _document_path(self, name: str) -> Path:
        """Task 文档位置：project 模式写 AIEOS 元数据，避免污染项目。"""
        if self.project_mode:
            return self.task_directory.aieos / Path(name).name
        return self.task_directory.workspace / Path(name).name

    def _write_document(self, name: str, content: str) -> None:
        target = self._document_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")

    def _communication_section(self) -> str:
        if self._inbox_bridge is None or self._outbox_bridge is None:
            return ""
        return f"""
========== Agent 通信 ==========
- 每轮开始前读取 `{self._inbox_bridge.inbox_md}`；
  里面是其他 Agent 发来的消息。
- 需要给其他 Agent 发消息时，在 `{self._outbox_bridge.pending}` 下
  写一个独立 JSON 文件（文件名任意唯一，例如 msg-<时间戳>.json）：
  {{"to":"agent_c","text":"...","task_id":"{self.task.task_id}"}}
- 一条消息一个文件；不要写进 Task Workspace。
- 消息只用于 Agent 沟通；Task 状态仍由 Task Core 负责。
"""

    def _autonomous_system_prompt(self) -> str:
        """自主模式的最小系统提示：环境边界、私有空间用途与工具使用。"""
        paths = self.memory.paths()
        result_path = self._document_path("RESULT.md")
        search_cmd = os.environ.get("AIEOS_WEB_SEARCH_CMD", "").strip()
        search_section = ""
        if search_cmd:
            search_section = f"""
需要实时外部信息时，可以运行：
{search_cmd} search "query" --count 5
需要读取具体公开网页正文时，可以运行：
{search_cmd} fetch "https://example.com/page"
"""
        scope = "项目目录" if self.project_mode else "Task Workspace"
        provenance = self._brief_sections()
        provenance_section = (
            f"\n========== 任务来源 ==========\n{provenance}\n"
            if provenance else "")
        return f"""{self._agent_identity()}

你是 AIEOS 中的 Work 执行 Agent。
{provenance_section}
{self._workspace_brief()}
直接使用原生 read/write/edit/bash/grep/find/ls 工具在{scope}中完成工作。
读取 `{paths['memory_dir']}/context.md` 获取记忆快照。
{search_section}
{self._communication_section()}
========== 结束方式 ==========
任务结束时，在 `{result_path}` 写总结：
- 交付内容
- 验证结果
- 已知限制

系统会优先读取这个文件作为任务总结。
最后一行输出 `TASK_COMPLETE`，然后结束。
"""

    def _minimal_system_prompt(self) -> str:
        if self.work_mode == "autonomous":
            return self._autonomous_system_prompt()
        paths = self.memory.paths()
        scope = "项目目录" if self.project_mode else "Task Workspace"
        question_path = self._document_path("QUESTION.json")
        result_path = self._document_path("RESULT.md")
        search_cmd = os.environ.get("AIEOS_WEB_SEARCH_CMD", "").strip()
        search_section = ""
        if search_cmd:
            search_section = f"""
需要实时外部信息时，可以运行：
{search_cmd} search "query" --count 5
需要读取某个具体公开网页的 Markdown 正文时，运行：
{search_cmd} fetch "https://example.com/page"
搜索与抓取结果作为外部证据；引用 URL，并以{scope}的实际状态为准。
"""
        provenance = self._brief_sections()
        provenance_section = (
            f"\n========== 任务来源 ==========\n{provenance}\n"
            if provenance else "")
        return f"""{self._agent_identity()}

你是 AIEOS 中的 Work 执行 Agent。
{provenance_section}
{self._workspace_brief()}
请直接使用原生 read/write/edit/bash/grep/find/ls 工具在{scope}中完成工作。
读取 `{paths['memory_dir']}/context.md` 获取记忆快照。
{search_section}
{self._communication_section()}

如果任务中确实需要人类确认（需求歧义、关键设计选择、不可逆操作），
请写 `{question_path}`，格式：

{{"question":"需要确认的问题","options":["选项 A","选项 B"],
  "allow_free_text":true}}

写完结束本轮，AIEOS 会暂停任务，等待人类回答后从当前
阶段继续。把问题留给真正的分歧点，能自主推进的工作直接推进。
任务完成后，把总结写入 `{result_path}`。
"""

    def _bounded_text(self, relative_path: str, limit: int = 6000) -> str:
        try:
            text = self._document_path(relative_path).read_text(
                encoding="utf-8").strip()
        except (OSError, ValueError):
            return ""
        if len(text) > limit:
            return text[:limit] + "\n…（已截断）"
        return text

    def _write_task_doc(self) -> None:
        """维护跨阶段 / 跨迭代的任务文档。"""
        state = self._work_state or {}
        files = [] if self.project_mode else self.task_directory.list_files()
        history = state.get("history") or []
        history_lines = [
            f"- {item.get('phase')}: {item.get('status')} "
            f"(thinking={item.get('thinking')})"
            for item in history[-12:]
        ] or ["- （还没有阶段历史）"]
        prototype = self._bounded_text("PROTOTYPE.md", 2500)
        engineering = self._bounded_text("ENGINEERING.md", 2500)
        open_issues = state.get("open_issues") or []
        issue_lines = [f"- {item}" for item in open_issues] or ["- （暂无）"]
        if state.get("status") == "waiting_human":
            try:
                pending = json.loads(
                    (self.task_directory.aieos / "question.json").read_text(
                        "utf-8"))
                if isinstance(pending, dict):
                    issue_lines.insert(
                        0, f"- 等待人类回答：{pending.get('question', '')}")
            except (OSError, ValueError):
                pass
        file_lines = [f"- {item['path']}" for item in files] or [
            "- （项目目录：变更见 final snapshot）"
            if self.project_mode else "- （空）"
        ]
        content = f"""# 任务文档

## 任务目标
{self.task.description or ''}

## 当前状态
- iteration: {state.get('iteration', self.task.iteration)}
- phase: {state.get('phase', '')}（{state.get('phase_label', '')}）
- status: {state.get('status', '')}
- thinking: {state.get('thinking', '')}
- session: {state.get('session_id', '')}
- last_error: {state.get('last_error', '') or '（无）'}
- updated_at: {state.get('updated_at', '')}

## 迭代 / 阶段历史
{chr(10).join(history_lines)}

## 设计意图
{prototype or '（还没有 PROTOTYPE.md）'}

## 工程记录
{engineering or '（还没有 ENGINEERING.md）'}

## 未解决问题
{chr(10).join(issue_lines)}

## 当前文件
{chr(10).join(file_lines)}
"""
        self._write_document("TASK_DOC.md", content)

    def _read_question(self) -> Optional[Dict[str, Any]]:
        path = self._document_path("QUESTION.json")
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            question = str(raw.get("question") or "").strip()
            options = raw.get("options") or []
            allow_free_text = bool(raw.get("allow_free_text", True))
        else:
            question = str(raw).strip()
            options = []
            allow_free_text = True
        question = question or "Agent 需要人类确认后才能继续。"
        clean_options = [
            str(item).strip() for item in options
            if str(item).strip()
        ] if isinstance(options, list) else []
        payload = {
            "task_id": self.task.task_id,
            "question": question,
            "options": clean_options,
            "allow_free_text": allow_free_text,
            "asked_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            path.unlink()
        except OSError:
            pass
        self.task_directory.write_json("question.json", payload)
        return payload

    def _write_question_history(
        self, question: Dict[str, Any], answer: str,
    ) -> None:
        path = self.task_directory.aieos / "questions.json"
        try:
            history = json.loads(path.read_text("utf-8")) if path.exists() else []
        except (OSError, ValueError):
            history = []
        if not isinstance(history, list):
            history = []
        history.append({
            **question,
            "answer": str(answer),
            "answered_at": datetime.now(timezone.utc).isoformat(),
        })
        self.task_directory.write_json("questions.json", history)

    def _clear_answer(self) -> None:
        path = self.task_directory.aieos / "ANSWER.json"
        try:
            path.unlink()
        except OSError:
            pass

    def _answer_text(self) -> str:
        path = self.task_directory.aieos / "ANSWER.json"
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return ""
        if isinstance(data, dict):
            return str(data.get("answer") or "").strip()
        return str(data).strip()

    def _remember_tool_results(self, events: List[Dict[str, Any]]) -> None:
        items: List[str] = []
        for event in events:
            if event.get("type") != "tool_execution_end":
                continue
            name = str(event.get("toolName") or "tool")
            result = event.get("result")
            if isinstance(result, list):
                text = " ".join(
                    str(block.get("text") or "")
                    for block in result
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = str(result or "")
            text = " ".join(text.split())
            if text:
                items.append(f"- {name}: {text[:400]}")
        if items:
            self._recent_tool_results = (
                self._recent_tool_results + items)[-12:]

    def _environment_facts(self, phase: str) -> str:
        if self.project_mode:
            lines = [
                f"项目目录: {self.workdir}",
                "这是已有项目；用 ls / grep / find / read 了解现有结构。",
                "代码和交付物直接修改项目目录；阶段变更记录在 AIEOS 元数据目录。",
            ]
        else:
            files = self.task_directory.list_files()
            lines = [f"workspace: {self.task_directory.workspace}"]
            if files:
                lines.append("当前文件：")
                for item in files[:80]:
                    chars = item.get("chars")
                    suffix = f" ({chars} chars)" if chars is not None else ""
                    lines.append(f"- {item['path']}{suffix}")
                if len(files) > 80:
                    lines.append(f"- …（还有 {len(files) - 80} 个文件）")
            else:
                lines.append("当前 Workspace 为空。")
        lines.append("可用工具：read / write / edit / bash / grep / find / ls。")
        if self._resumed:
            history = self._work_state.get("history") or []
            latest = history[-1] if history else {}
            lines.append("\n========== 恢复简报 ==========")
            lines.append(
                f"当前不是从头开始。resume_reason={self._resume_reason}；"
                f"上次阶段={latest.get('phase', '?')}；"
                f"上次状态={latest.get('status', '?')}。")
            last_error = str(self._work_state.get("last_error") or "").strip()
            if last_error:
                lines.append(f"上次错误：{last_error[:800]}")
            lines.append(
                "先确认当前文件和运行状态，再从现有成果继续，"
                "在已有原型上增量推进。")
        task_doc = self._bounded_text("TASK_DOC.md", 4000)
        if task_doc:
            lines.append("\n========== TASK_DOC.md ==========\n" + task_doc)
        if self._recent_tool_results:
            lines.append("\n========== 最近工具结果 ==========")
            lines.extend(self._recent_tool_results)
        answer = self._answer_text()
        if answer:
            lines.append("\n========== 人类回答 ==========\n" + answer)
        if phase in ("engineering", "evolution"):
            prototype = self._bounded_text("PROTOTYPE.md")
            if prototype:
                lines.append("\n========== PROTOTYPE.md ==========\n" + prototype)
        if phase == "evolution":
            engineering = self._bounded_text("ENGINEERING.md")
            if engineering:
                lines.append("\n========== ENGINEERING.md ==========\n" + engineering)
        return "\n".join(lines)

    def _role_phase_section(self, phase: str) -> str:
        if self.role is not None:
            return render_role_prompt(self.role, phase=phase).strip()
        if self.role_prompt:
            return (
                "========== Worker 角色 ==========\n"
                + self.role_prompt
            ).strip()
        return ""

    def _phase_prompt(self, phase: str, memory_context: str) -> str:
        task_text = self.task.description or f"Task {self.task.task_id}"
        if self.work_mode == "autonomous":
            text = task_text
            answer = self._answer_text()
            if answer:
                text = (
                    f"{text}\n\n"
                    f"========== 人类回答 ==========\n{answer}")
            return text
        env = self._environment_facts(phase)
        scope = "项目目录" if self.project_mode else "Task Workspace"
        result_path = self._document_path("RESULT.md")
        role_section = self._role_phase_section(phase)
        role_block = (
            f"\n========== Worker 角色（本阶段要求） ==========\n{role_section}\n"
            if role_section else ""
        )
        if phase == "direct":
            return f"""直接完成这个任务，产出集中在任务需要的阶段成果上。
完成时在 `{result_path}` 写总结，说明交付内容、文件路径和验证情况。
最后一行输出 `TASK_COMPLETE`。

========== 任务 ==========
{task_text}

========== 环境事实 ==========
{env}
"""
        if phase == "prototype":
            return f"""{role_block}这是原型阶段。先把想法变成实际成果，再补充计划。

请尽可能构建一个完整、有实际体验的初始原型。你可以自主决定成果的
形式、结构、内容、交互或实现方式；优先让核心体验真正成立。

完成时：
1. 确保{scope}中有具体、可检查、可交付的产物
   （代码、页面、文档、报告、设计或其他符合任务的形式）；
2. 建议写一份简短的 `PROTOTYPE.md`，记录设计意图和关键取舍；
   如果任务不适合阶段文档，可以省略；
3. 最后一行输出 `PROTOTYPE_READY`。

========== 任务 ==========
{task_text}

========== 环境事实 ==========
{env}
"""
        if phase == "engineering":
            return f"""{role_block}原型阶段已经结束。现在进入工程阶段。

请先理解上一阶段记录的设计意图，然后检查现有产物的内容、结构和实际效果。
实际运行、使用或检查产物，发现问题并修复；必要时调整结构。
沿用现有设计，只在发现根本性问题时推翻部分设计。
不默认补测试；只有任务明确要求，或确有风险需要验证时才补。

第一阶段完整会话在 `.aieos/pi-session/`。需要理解原型阶段的设计意图和
关键取舍时，可以读取该会话，并在上下文里保留结论和依据。

完成时（建议）：
1. 写一份简短的 `ENGINEERING.md`，记录检查了什么、发现了什么、改了什么、
   还剩什么；长度随内容需要，测试报告按实际情况取舍；
2. 最后一行输出 `ENGINEERING_READY`。

========== 任务 ==========
{task_text}

========== 环境事实 ==========
{env}
"""
        if phase == "evolution":
            issues = self._work_state.get("open_issues") or []
            issue_text = "\n".join(f"- {item}" for item in issues) or "（暂无记录）"
            return f"""{role_block}进入持续迭代阶段。继续基于当前产物、设计记录、最近工具结果和未解决问题
工作，自主决定下一步，直到原始目标满足。

在阶段性修改后做整体验证，而不是每次改动都跑完整回归。
发现新问题时记录到 `ENGINEERING.md` 或 `RESULT.md` 的“已知限制”中。

完成时：
1. 在 `{result_path}` 写最终总结；
2. 最后一行输出 `TASK_COMPLETE`。

========== 任务 ==========
{task_text}

========== 环境事实 ==========
{env}

========== 未解决问题 ==========
{issue_text}
"""
        raise ValueError(f"未知 Work phase：{phase!r}")

    def _write_work_state(self, phase: str, *, status: str,
                          thinking: str, error: str = "") -> None:
        history = list(self._work_state.get("history") or [])
        if status != "running":
            history.append({
                "phase": phase,
                "status": status,
                "thinking": thinking,
                "at": datetime.now(timezone.utc).isoformat(),
            })
            history = history[-20:]
        attempt = int(self._work_state.get("attempt") or 0)
        if status == "running":
            attempt += 1
        previous_error = str(self._work_state.get("last_error") or "")
        if status == "completed":
            last_error = ""
        elif status in ("failed", "stopped"):
            last_error = str(error or previous_error)
        else:
            last_error = previous_error
        self._work_state = {
            "iteration": int(self.task.iteration or 1),
            "phase": phase,
            "phase_label": self._PHASE_LABELS.get(phase, phase),
            "status": status,
            "phase_status": status,
            "attempt": attempt,
            "thinking": thinking,
            "resume_reason": self._resume_reason,
            "session_id": self._session_id or str(
                self._work_state.get("session_id") or ""),
            "last_error": last_error,
            "open_issues": list(self._work_state.get("open_issues") or []),
            "history": history,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.task_directory.write_json("work_state.json", self._work_state)
        self._write_task_doc()

    def _ensure_phase_artifact(
        self, name: str, events: List[Dict[str, Any]], title: str,
    ) -> None:
        """阶段产物缺失时，用该阶段模型最后输出补一个可追溯的交接文件。"""
        target = self._document_path(name)
        if target.is_file():
            return
        text = self._last_assistant_text(events).strip()
        if not text:
            return
        self._write_document(
            name,
            f"# {title}\n\n"
            "> 该文件由 AIEOS 从本阶段模型最后输出提取；"
            "模型没有显式写出阶段文件。\n\n"
            + text[:6000] + "\n")

    def _prototype_ready(self, events: List[Dict[str, Any]]) -> bool:
        text = self._last_assistant_text(events)
        if self.project_mode:
            # 项目里本来就有大量文件；用结束标记，而不是“有没有文件”。
            return "PROTOTYPE_READY" in text
        artifacts = [
            item for item in self.task_directory.list_files()
            if item.get("path") not in (
                "TASK_DOC.md", "PROTOTYPE.md", "ENGINEERING.md", "RESULT.md")
        ]
        if artifacts:
            return True
        return "PROTOTYPE_READY" in text

    def _manifest_entries(self) -> List[Dict[str, Any]]:
        """交付清单。

        Task Workspace：完整文件清单。
        Project：baseline → final 的变更文件，避免扫描整个项目。
        """
        if not self.project_mode:
            return self.task_directory.manifest()
        records = self.snapshots.list()
        if not records:
            return []
        baseline = next(
            (item for item in records if item.label == "baseline"),
            records[0],
        )
        final = records[-1]
        changed = self.snapshots.changed_files(
            baseline.seq, final.seq)
        entries: List[Dict[str, Any]] = []
        for item in changed:
            if str(item.get("status") or "") == "D":
                continue
            path = str(item.get("path") or "")
            if not path:
                continue
            target = self.workdir / path
            try:
                data = target.read_bytes()
            except OSError:
                continue
            entries.append({
                "path": path,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "status": item.get("status") or "M",
            })
        return entries

    def _capture_snapshot(
        self, label: str, *, phase: str = "", turn: int = 0,
    ) -> Optional[SnapshotRecord]:
        """抓快照；失败只记日志，不打断 Task。"""
        try:
            return self.snapshots.capture(
                label, phase=phase, turn=turn)
        except Exception as exc:                              # noqa: BLE001
            self._snapshot_error = f"{type(exc).__name__}: {exc}"
            try:
                self.task_directory.write_meta(
                    "snapshot-error.log", self._snapshot_error + "\n")
            except Exception:                                 # noqa: BLE001
                pass
            return None

    def _dispatch_event(self, event: Dict[str, Any]) -> None:
        if (
            self._outbox_bridge is not None
            and self._outbox_bridge.has_pending()
        ):
            self._outbox_bridge.drain_and_send()
        if self.event_sink is not None:
            self.event_sink(event)

    # ------------------------------------------------------------
    def run(self) -> WorkResult:
        task = self.task
        actor = task.contractor or task.creator
        result = WorkResult(
            task_id=task.task_id,
            status=task.status,
            iteration=task.iteration,
        )
        lock = None
        inbox_started = False
        if self.project_mode and self.project_lock_manager is not None:
            try:
                lock = self.project_lock_manager.acquire(self.workdir)
            except Exception as exc:                          # noqa: BLE001
                result.error = str(exc)
                return result
        try:
            if self.should_stop():
                result.error = "任务已被停止"
                return result
            if task.status == TaskStatus.COMPLETED:
                result.summary = task.note or "任务已经处于 COMPLETED"
                return result
            if task.status in (TaskStatus.CREATED, TaskStatus.ACCEPTED):
                task = self.lifecycle.start(task, actor=actor)
                self.task = task
                result.status = task.status
            if task.status not in (
                TaskStatus.IN_PROGRESS,
                TaskStatus.SUBMITTED,
                TaskStatus.VERIFIED,
            ):
                result.error = (
                    f"Task 当前是 {task.status}，不能开始 Pi Work run")
                result.status = task.status
                return result

            self.task_directory.ensure()
            if self._owns_inbox_bridge and self._inbox_bridge is not None:
                inbox_started = self._inbox_bridge.start()
            self.task_directory.write_meta(
                "TASK.md",
                f"# Task {task.task_id}\n\n{task.description or ''}\n")
            memory_context = self.memory.prepare()
            spec = self._provider_spec()
            provider, model = spec.provider, spec.model
            if self.use_llm_proxy:
                if spec.adapter == "mock" or not spec.chat_url:
                    result.error = (
                        f"Pi Work 无法为 provider {provider!r} 找到可用的 "
                        f"chat completions 端点（adapter={spec.adapter!r}, "
                        f"base_url={spec.base_url!r}）。")
                    result.status = task.status
                    return result
            state_path = self.task_directory.aieos / "work_state.json"
            if state_path.exists():
                try:
                    loaded = json.loads(state_path.read_text("utf-8"))
                    if isinstance(loaded, dict):
                        self._work_state = loaded
                except (OSError, ValueError):
                    self._work_state = {}
            self._session_id = self._pi_session_id(
                task.task_id, task.iteration, self._work_state)
            self._work_state["session_id"] = self._session_id
            phase_plan, resumed, resume_reason = self._phase_plan_for_run(
                self._work_state)
            self._resume_reason = resume_reason
            self._resumed = resumed
            self._write_task_doc()
            try:
                has_snapshots = bool(self.snapshots.list())
            except Exception:                                 # noqa: BLE001
                has_snapshots = False
            if has_snapshots:
                self._capture_snapshot(
                    "resume",
                    phase=str(self._work_state.get("phase") or ""))
            else:
                self._capture_snapshot("baseline")
            all_events: List[Dict[str, Any]] = []
            phase_runs: List[Dict[str, Any]] = []
            all_stderr: List[str] = []
            proxy = None
            try:
                if self.use_llm_proxy:
                    material = self._credential_material(provider)
                    if not material:
                        result.error = (
                            f"Agent 没有可用的 {provider} credential，"
                            "无法启动 Pi LLM proxy")
                        result.status = task.status
                        return result
                    proxy = AIEOSLLMProxy(
                        self.rt,
                        agent_id=actor,
                        task_id=task.task_id,
                        api_key=material,
                        provider=provider,
                        model=model,
                        upstream_base_url=spec.base_url,
                        chat_path=spec.chat_path,
                        provider_request=spec.request,
                        timeout_seconds=self.proxy_timeout_seconds,
                        session_id=build_session_id(
                            task_scope(task.task_id, agent_id=actor),
                            provider=provider,
                            model=model,
                        ),
                    )
                    try:
                        proxy_base = proxy.start()
                    except Exception as exc:              # noqa: BLE001
                        result.error = (
                            f"无法启动 AIEOS LLM Proxy："
                            f"{type(exc).__name__}: {exc}")
                        result.status = task.status
                        return result
                    pi_agent_dir = self._write_pi_models(proxy_base, model)
                    env = self._pi_env("aieos-proxy", include_key=False)
                    env["PI_CODING_AGENT_DIR"] = str(pi_agent_dir)
                    client_provider = "aieos-proxy"
                    client_model = model
                else:
                    env = self._pi_env(provider)
                    if self.pi_agent_dir is not None:
                        env["PI_CODING_AGENT_DIR"] = str(self.pi_agent_dir)
                    client_provider = provider
                    client_model = model

                for phase in phase_plan:
                    thinking = self._phase_thinking(phase)
                    inbox_text = ""
                    if self._inbox_bridge is not None:
                        inbox_text = self._inbox_bridge.drain_for_prompt()
                    prompt = self._phase_prompt(phase, memory_context)
                    if inbox_text:
                        prompt = (
                            "========== Agent 消息 ==========\n"
                            + inbox_text + "\n\n" + prompt)
                    self._write_work_state(
                        phase, status="running", thinking=thinking)
                    client = PiRpcClient(
                        cli_path=self.pi_cli_path,
                        cwd=self.workdir,
                        env=env,
                        provider=client_provider,
                        model=client_model,
                        system_prompt=self._minimal_system_prompt(),
                        session_dir=self.task_directory.session_dir,
                        session_id=self._session_id,
                        extra_args=self.extra_args + [
                            "--thinking", thinking],
                    )
                    run = client.run(
                        prompt,
                        timeout_seconds=self.timeout_seconds,
                        should_stop=self.should_stop,
                        event_sink=self._dispatch_event,
                    )
                    all_events.extend(run.events)
                    self._remember_tool_results(run.events)
                    self._clear_answer()
                    if run.stderr:
                        all_stderr.append(run.stderr)
                    if not self.use_llm_proxy:
                        self._record_usage(actor, run.events, provider, model)
                    self._emit_rounds(run.events, actor, phase=phase)
                    phase_runs.append({
                        "phase": phase,
                        "thinking": thinking,
                        "turns": self._turn_count(run.events),
                        "tokens": self._token_count(run.events),
                        "actions": self._tool_count(run.events),
                        "error": run.error,
                    })
                    if run.aborted or self.should_stop():
                        error_text = run.error or "任务已被停止"
                        self._write_work_state(
                            phase, status="stopped", thinking=thinking,
                            error=error_text)
                        self._capture_snapshot(
                            "interrupted", phase=phase,
                            turn=sum(
                                item["turns"] for item in phase_runs))
                        result.error = error_text
                        result.status = task.status
                        return result
                    if run.error:
                        self._write_work_state(
                            phase, status="failed", thinking=thinking,
                            error=run.error)
                        self._capture_snapshot(
                            "interrupted", phase=phase,
                            turn=sum(
                                item["turns"] for item in phase_runs))
                        result.error = run.error
                        result.status = task.status
                        return result
                    question = self._read_question()
                    if question is not None:
                        self._write_work_state(
                            phase, status="waiting_human",
                            thinking=thinking)
                        self._capture_snapshot(
                            "waiting_human", phase=phase,
                            turn=sum(
                                item["turns"] for item in phase_runs))
                        result.waiting_human = True
                        result.question = question
                        result.summary = str(question.get("question") or "")
                        result.status = task.status
                        result.turns = sum(
                            item["turns"] for item in phase_runs)
                        result.tokens = sum(
                            item["tokens"] for item in phase_runs)
                        result.actions = sum(
                            item["actions"] for item in phase_runs)
                        result.events = [
                            {"type": "pi_phase", **item}
                            for item in phase_runs
                        ] + [{
                            "type": "pi_waiting_human",
                            "phase": phase,
                            "question": question,
                        }]
                        return result
                    if phase == "prototype":
                        if not self._prototype_ready(run.events):
                            error_text = (
                                "原型阶段没有产生可运行产物或 PROTOTYPE_READY")
                            self._write_work_state(
                                phase, status="failed", thinking=thinking,
                                error=error_text)
                            self._capture_snapshot(
                                "phase:prototype:failed", phase=phase,
                                turn=sum(
                                    item["turns"] for item in phase_runs))
                            result.error = error_text
                            result.status = task.status
                            return result
                        self._ensure_phase_artifact(
                            "PROTOTYPE.md", run.events, "原型设计记录")
                    elif phase == "engineering":
                        self._ensure_phase_artifact(
                            "ENGINEERING.md", run.events, "工程验证记录")
                    self._write_work_state(
                        phase, status="completed", thinking=thinking)
                    self._capture_snapshot(
                        f"phase:{phase}:end", phase=phase,
                        turn=sum(
                            item["turns"] for item in phase_runs))
            finally:
                if proxy is not None:
                    proxy.stop()

            self._work_state = {
                **self._work_state,
                "phase": "complete",
                "phase_label": "已完成",
                "status": "completed",
                "phase_status": "complete",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.task_directory.write_json(
                "work_state.json", self._work_state)
            self._write_task_doc()
            snapshot = self._capture_snapshot("final")
            snapshot_info = snapshot.to_dict() if snapshot else None
            manifest = self.task_directory.write_manifest(
                snapshot=snapshot_info, files=self._manifest_entries())
            if all_stderr:
                self.task_directory.write_meta(
                    "pi-stderr.log", "\n".join(all_stderr))
            if not manifest:
                if self.project_mode and self._snapshot_error:
                    result.error = (
                        "项目快照失败，无法确认变更："
                        f"{self._snapshot_error}")
                else:
                    result.error = (
                        "Pi 已结束，但项目目录没有任何变更"
                        if self.project_mode
                        else "Pi 已结束，但 Task Workspace 没有任何文件")
                result.status = task.status
                return result

            summary = self._summary(all_events)
            if not summary:
                upstream_error = self._last_assistant_error(all_events)
                detail = (
                    f"；上游错误：{upstream_error}" if upstream_error else "")
                result.error = (
                    "Pi 已结束，但没有产出总结："
                    "既没有 RESULT.md，也没有可用的 assistant 文本"
                    + detail)
                result.status = task.status
                return result

            memory_result = self.memory.ingest()
            result.turns = sum(item["turns"] for item in phase_runs)
            result.tokens = sum(item["tokens"] for item in phase_runs)
            result.actions = sum(item["actions"] for item in phase_runs)
            result.summary = summary
            result.artifacts = [item["path"] for item in manifest]
            result.events = [
                {"type": "pi_phase", **item} for item in phase_runs
            ] + [{
                "type": "pi_settled",
                "memory": memory_result,
                "files": len(manifest),
            }]
            self.task_directory.write_json("result.json", {
                "task_id": task.task_id,
                "engine": "pi",
                "summary": summary,
                "files": manifest,
                "memory": memory_result,
                "snapshot": snapshot_info,
            })

            self.lifecycle.complete_solo(
                task, actor=actor,
                result=TaskResultRef(
                    result_ref=f"pi_task_{task.task_id}",
                    result_type="artifact",
                    summary=summary,
                    metadata={
                        "engine": "pi",
                        "manifest": manifest,
                        "memory": memory_result,
                        "snapshot": snapshot_info,
                    },
                ),
            )
            task = self.rt.tasks.get(task.task_id)
            self.task = task
            result.status = task.status
            return result
        except Exception as exc:                              # noqa: BLE001
            self._capture_snapshot(
                "interrupted",
                phase=str(self._work_state.get("phase") or ""))
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = task.status
            return result
        finally:
            if self._outbox_bridge is not None:
                self._outbox_bridge.drain_and_send()
            if inbox_started and self._inbox_bridge is not None:
                self._inbox_bridge.stop()
            if lock is not None:
                lock.release()

    def reopen_and_run(
        self, *, reason: str = "", rework_scope: str = "",
    ) -> WorkResult:
        actor = self.task.creator
        self.rework_scope = str(rework_scope or "enhance").strip().lower()
        self.lifecycle.reopen(self.task, actor=actor, reason=reason)
        return self.run()

    # ------------------------------------------------------------
    def _system_prompt(self, memory_context: str) -> str:
        paths = self.memory.paths()
        scope = "项目目录" if self.project_mode else "Task Workspace"
        result_path = self._document_path("RESULT.md")
        role_section = (
            "\n" + self.role_prompt + "\n" if self.role_prompt else "")
        provenance = self._brief_sections()
        provenance_section = (
            f"\n========== 任务来源 ==========\n{provenance}\n"
            if provenance else "")
        return f"""{self._agent_identity()}

你是 AIEOS 中的 Work 执行 Agent。
{provenance_section}{role_section}
{self._workspace_brief()}
请直接把交付物写入{scope}，使用原生 read/write/edit/bash 工具。
本 Work 任务以{scope}作为唯一协作面。

读取 `{paths['memory_dir']}/context.md` 获取记忆快照。
你可以更新 `{paths['memory_dir']}/working.md`，并把候选记忆写入
`{paths['memory_dir']}/inbox/*.md`。

任务完成后，在 `{result_path}` 写总结，作为给人类的总结。
使用以下章节：
- 交付内容
- 文件与路径
- 如何打开 / 运行 / 验证
- 验证结果
- 已知限制
- 下一步（如有）

任务描述：
{self.task.description or ''}
"""

    def _summary(self, events: List[Dict[str, Any]]) -> str:
        result_path = self._document_path("RESULT.md")
        if result_path.is_file():
            try:
                text = result_path.read_text(encoding="utf-8").strip()
                if text:
                    return text[:8000]
            except OSError:
                pass
        text = self._last_assistant_text(events)
        return text[:8000]

    @staticmethod
    def _last_assistant_text(events: List[Dict[str, Any]]) -> str:
        for event in reversed(events):
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                texts = [
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(item for item in texts if item.strip())
                if text.strip():
                    return text.strip()
        return ""

    @staticmethod
    def _last_assistant_error(events: List[Dict[str, Any]]) -> str:
        """取最近一条 assistant error 的原始上游错误。

        Pi 在模型调用失败时会发 `stopReason: "error"` + `errorMessage`。
        如果只看「没有 RESULT.md」，真正原因（401 / 400 / 超时）会丢失。
        """
        for event in reversed(events):
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            if str(message.get("stopReason") or "").lower() != "error":
                continue
            text = str(message.get("errorMessage") or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _turn_count(events: List[Dict[str, Any]]) -> int:
        return sum(1 for event in events if event.get("type") == "turn_start")

    @staticmethod
    def _tool_count(events: List[Dict[str, Any]]) -> int:
        return sum(
            1 for event in events
            if event.get("type") == "tool_execution_end")

    @staticmethod
    def _token_count(events: List[Dict[str, Any]]) -> int:
        total = 0
        for event in events:
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            total += int(usage.get("totalTokens") or 0)
        return total

    # ------------------------------------------------------------
    def _record_usage(
        self,
        actor: str,
        events: List[Dict[str, Any]],
        provider: str,
        model: str,
    ) -> None:
        recorder = getattr(self.rt, "recorder", None)
        if recorder is None:
            return
        turn_started = 0.0
        for event in events:
            if event.get("type") == "turn_start":
                turn_started = float(event.get("_received_at") or 0.0)
                continue
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            received = float(event.get("_received_at") or 0.0)
            latency_ms = (
                max(0.0, (received - turn_started) * 1000.0)
                if turn_started and received else 0.0)
            input_tokens = (
                int(usage.get("input") or 0)
                + int(usage.get("cacheRead") or 0)
                + int(usage.get("cacheWrite") or 0)
            )
            output_tokens = int(usage.get("output") or 0)
            total_tokens = int(
                usage.get("totalTokens")
                or (input_tokens + output_tokens))
            response = LLMResponse(
                content="",
                provider=str(message.get("provider") or provider),
                model=str(message.get("model") or model),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                finish_reason=message.get("stopReason"),
                token_source=TOKEN_SOURCE_API,
                raw_response={"pi_usage": usage},
                metadata={"source": "pi"},
            )
            try:
                recorder.record(
                    agent_id=actor,
                    response=response,
                    task_id=self.task.task_id,
                    call_purpose=getattr(
                        recorder, "PURPOSE_COGNITION", "cognition"),
                    experiment_run_id=getattr(
                        self.rt, "experiment_run_id", None),
                )
            except Exception:                             # noqa: BLE001
                pass

    # ------------------------------------------------------------
    def _emit_rounds(
        self, events: List[Dict[str, Any]], actor: str,
        *, phase: str = "",
    ) -> None:
        if self.round_sink is None:
            return
        rounds = self._rounds_from_events(events, actor, phase=phase)
        for payload in rounds:
            try:
                self.round_sink(payload)
            except Exception:                             # noqa: BLE001
                pass

    def _rounds_from_events(
        self, events: List[Dict[str, Any]], actor: str,
        *, phase: str = "",
    ) -> List[Dict[str, Any]]:
        rounds: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        turn_no = 0
        tool_starts: Dict[str, Dict[str, Any]] = {}
        for event in events:
            kind = event.get("type")
            if kind == "turn_start":
                turn_no += 1
                current = {
                    "task_id": self.task.task_id,
                    "run_id": f"pi_{self.task.task_id}",
                    "iteration": int(self.task.iteration or 1),
                    "round": turn_no,
                    "phase": phase,
                    "status_before": self.task.status,
                    "status_after": self.task.status,
                    "summary": "",
                    "tokens": 0,
                    "latency_ms": 0.0,
                    "segments": [],
                    "_started": event.get("_received_at") or 0.0,
                }
                rounds.append(current)
            elif kind == "tool_execution_start":
                call_id = str(event.get("toolCallId") or "")
                tool_starts[call_id] = {
                    "toolName": event.get("toolName") or "",
                    "args": event.get("args"),
                }
            elif kind == "message_end":
                if current is None:
                    continue
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                content = message.get("content")
                text = ""
                thinking = ""
                tool_calls: List[Dict[str, Any]] = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text += str(block.get("text") or "")
                        elif block.get("type") == "thinking":
                            thinking += str(block.get("thinking") or "")
                        elif block.get("type") == "toolCall":
                            tool_calls.append({
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(
                                        block.get("arguments") or {},
                                        ensure_ascii=False),
                                },
                            })
                elif isinstance(content, str):
                    text = content
                segment = {
                    "phase": "work",
                    "content": text,
                    "model_reasoning": thinking,
                    "tool_calls": tool_calls,
                    "tool_calls_source": "pi",
                    "decision": None,
                    "actions": [],
                    "results": [],
                    "tokens": int(usage.get("totalTokens") or 0),
                    "latency_ms": 0.0,
                    "finish_reason": message.get("stopReason"),
                    "truncated": str(
                        message.get("stopReason") or "").lower() == "length",
                }
                current["segments"].append(segment)
                current["tokens"] += int(usage.get("totalTokens") or 0)
                current["summary"] = text.strip() or current["summary"]
            elif kind == "tool_execution_end":
                if current is None:
                    continue
                call_id = str(event.get("toolCallId") or "")
                started = tool_starts.pop(call_id, {})
                name = str(
                    event.get("toolName") or started.get("toolName") or "tool")
                result = event.get("result")
                is_error = bool(event.get("isError"))
                action = {
                    "action_id": call_id,
                    "action_type": name,
                    "parameters": started.get("args") or {},
                    "agent_id": actor,
                }
                action_result = {
                    "action_id": call_id,
                    "action_type": name,
                    "success": not is_error,
                    "output": result,
                    "error": None if not is_error else str(result),
                    "duration_ms": event.get("durationMs") or 0.0,
                }
                segment = (current["segments"][-1]
                           if current["segments"] else None)
                if segment is None:
                    segment = {
                        "phase": "work", "content": "",
                        "model_reasoning": "", "tool_calls": [],
                        "tool_calls_source": "pi", "decision": None,
                        "actions": [], "results": [], "tokens": 0,
                        "latency_ms": 0.0, "finish_reason": None,
                        "truncated": False,
                    }
                    current["segments"].append(segment)
                segment["actions"].append(action)
                segment["results"].append(action_result)
            elif kind == "turn_end":
                if current is None:
                    continue
                received = event.get("_received_at") or 0.0
                started = current.pop("_started", 0.0) or 0.0
                current["latency_ms"] = round(
                    max(0.0, (received - started) * 1000.0), 1)
                current["summary"] = current["summary"] or (
                    f"Pi turn {current['round']}")
                current = None
        return rounds


__all__ = ["PiWorkRuntime"]
