"""
AIEOSController —— Human → Front → Solo Agent 的持续运行控制面
================================================================

当前阶段只跑 Solo（`solo_only=True`）：

    Human 输入
      ↓
    FrontAgentRuntime（固定 Front Agent）
      ├─ direct_reply / clarify → 直接回给 Human
      ├─ follow_up / rework / cancel → 已有 Task
      └─ handoff → Brief
                    ↓
              选择一个 IDLE Agent
                    ↓
              创建 SOLO Task
                    ↓
              SoloWorkRuntime
                    ↓
              FrontResult → Human

🕘 Team 路径（Leader 规划 / TeamWorkRuntime）保留在代码里，
   但 UI 的 `solo_only=True` 不会触发它；需要时显式设回 False。

🔒 这里不重复实现 Task / Workspace / Memory / Work runtime；
   只负责编排已经存在的机制。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from llm import prompt_log
from llm.web_search import WebSearchClient, WebSearchError
from core.task import Task, TaskMode, TaskStatus
from demand.task_creation import TaskCreationService
from services.artifacts import read_artifact
from work.artifact_export import (
    ArtifactExportError, export_task_workspace,
)
from front import (
    FRONT_SYSTEM_PROMPT, FrontAgentRuntime, FrontIntent, FrontResult,
    FrontTurn, FrontUnavailableError,
)
from work.availability import AgentAvailability
from work.checkpoint import (
    RunCheckpoint, checkpoint_from_rounds, dumps as checkpoint_dumps,
)
from work.drafts import TaskDraftStore
from work.front_actions import (
    build_front_dispatcher, execute_front_actions,
)
from work.pi_runtime import PiWorkRuntime
from work.task_directory import TaskDirectoryManager
from work.worker_roles import WorkerRoleError, WorkerRoleStore
from work.plan import WorkPlan, parse_work_plan
from work.prompt import WORK_SYSTEM_PROMPT, build_lead_plan_prompt
from work.roles import WorkContext, WorkRole
from work.runtime import SoloWorkRuntime
from work.team_runtime import TeamWorkRuntime


class JobStatus:
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    FRONT_ANALYZING = "FRONT_ANALYZING"
    WAITING_FOR_AGENT = "WAITING_FOR_AGENT"
    WAITING_FOR_LEADER = "WAITING_FOR_LEADER"
    LEADER_PLANNING = "LEADER_PLANNING"
    WAITING_FOR_MEMBERS = "WAITING_FOR_MEMBERS"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    #: LLM 超时/中断：Task 仍是 IN_PROGRESS，后续可以 follow_up 续跑。
    INTERRUPTED = "INTERRUPTED"
    #: Agent 在 Work 中途等待人类回答；Task / 阶段都保持原状态。
    WAITING_HUMAN = "WAITING_HUMAN"
    DIRECT_REPLY = "DIRECT_REPLY"
    CLARIFY = "CLARIFY"


#: Job 仍占用 Task 的状态；其余状态下的开放 Task 视为「已中断」。
_RUNNING_JOB_STATUSES = frozenset({
    JobStatus.FRONT_ANALYZING,
    JobStatus.WAITING_FOR_AGENT,
    JobStatus.WAITING_FOR_LEADER,
    JobStatus.LEADER_PLANNING,
    JobStatus.WAITING_FOR_MEMBERS,
    JobStatus.RUNNING,
    JobStatus.CANCELLING,
})

#: Job 已经结束的状态；其余状态下的 duration_ms 继续按当前时间增长。
_ENDED_JOB_STATUSES = frozenset({
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.INTERRUPTED,
    JobStatus.WAITING_HUMAN,
    JobStatus.DIRECT_REPLY,
    JobStatus.CLARIFY,
})


@dataclass
class WorkJob:
    job_id: str
    human_text: str
    status: str = JobStatus.RECEIVED
    front_intent: str = ""
    leader: str = ""
    members: List[str] = field(default_factory=list)
    task_id: str = ""
    summary: str = ""
    error: str = ""
    tokens: int = 0
    turns: int = 0
    #: Front 决策这次 Job 实际花的时间（毫秒）。
    front_ms: int = 0
    #: Front 决策这次调用实际请求的思考强度。
    front_thinking: Optional[Dict[str, Any]] = None
    #: 完整的 FrontDecision（含 brief / task_text / raw_output）。
    #: 🔒 只进 job log，不进高频轮询的 state。
    front_decision: Dict[str, Any] = field(default_factory=dict)
    #: Front 本轮执行过的私有空间 / 草稿动作（含真实结果）。
    front_actions: List[Dict[str, Any]] = field(default_factory=list)
    #: 这次 Job 是否可以直接重试（Front 调用失败 / 输出不可解析）。
    retryable: bool = False
    cancelled: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "human_text": self.human_text,
            "status": self.status,
            "front_intent": self.front_intent,
            "leader": self.leader,
            "members": list(self.members),
            "task_id": self.task_id,
            "summary": self.summary,
            "error": self.error,
            "tokens": self.tokens,
            "turns": self.turns,
            "front_ms": int(self.front_ms or 0),
            "front_thinking": (
                dict(self.front_thinking)
                if isinstance(self.front_thinking, dict) else None),
            "duration_ms": _duration_ms(
                self.created_at,
                self.updated_at
                if self.status in _ENDED_JOB_STATUSES else _now()),
            "retryable": bool(self.retryable),
            "cancelled": self.cancelled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_log_dict(self) -> Dict[str, Any]:
        """Job log 的完整版本：包含 Front 决定 / 原始输出 / 动作结果。"""
        data = self.to_dict()
        data["front_decision"] = dict(self.front_decision)
        data["front_actions"] = list(self.front_actions)
        return data


class AIEOSController:
    """GUI 背后的编排器（线程安全）。"""

    def __init__(
        self,
        runtime: Any,
        *,
        front_agent: str = "agent_a",
        max_parallel_teams: int = 3,
        max_agents_per_team: int = 5,
        max_worker_turns: int = 8,
        max_integration_turns: int = 8,
        max_solo_turns: int = 24,
        cognitive_budget: int = 65536,
        solo_only: bool = True,
        pi_work_agents: Any = (),
        pi_cli_path: str = "pi",
        pi_provider: str = "",
        pi_model: str = "",
        pi_agent_dir: Optional[Path] = None,
        pi_timeout_seconds: float = 7200.0,
        pi_timeout_enabled: bool = True,
        task_root: Optional[Path] = None,
        settings_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        round_log: Any = None,
        export_root: Optional[Path] = None,
        worker_roles_path: Optional[Path] = None,
        worker_roles_backup_dir: Optional[Path] = None,
        prompt_log_path: Optional[Path] = None,
        system_settings: Any = None,
    ) -> None:
        self.rt = runtime
        self.front_agent = str(front_agent)
        #: 当前阶段只允许一个 Agent 独立完成（不组队）。
        self.solo_only = bool(solo_only)
        self.max_parallel_teams = max(1, int(max_parallel_teams))
        self.max_agents_per_team = max(2, int(max_agents_per_team))
        self.max_worker_turns = max(1, int(max_worker_turns))
        self.max_integration_turns = max(1, int(max_integration_turns))
        self.max_solo_turns = max(1, int(max_solo_turns))
        self.cognitive_budget = max(1, int(cognitive_budget))
        self.pi_work_agents = {
            str(item).strip() for item in (pi_work_agents or ())
            if str(item).strip()
        }
        #: 停用的 Agent 不再接新任务；运行中的任务继续完成。
        self.disabled_agents: set[str] = set()
        #: Agent 卡片选择的思考强度；"auto" = 阶段策略。
        self.agent_thinking: Dict[str, str] = {}
        #: Work 模式：workflow = 阶段化；autonomous = 单阶段自主工作。
        self.work_mode = "autonomous"
        #: Front 模式：autonomous = 当前会补全 brief；relay = 只传达。
        self.front_mode = "autonomous"
        self.pi_cli_path = str(pi_cli_path or "pi")
        self.pi_provider = str(pi_provider or "")
        self.pi_model = str(pi_model or "")
        self.pi_agent_dir = Path(pi_agent_dir) if pi_agent_dir else None
        self.pi_timeout_seconds = float(pi_timeout_seconds)
        self.pi_timeout_enabled = bool(pi_timeout_enabled)
        self.task_directories = TaskDirectoryManager(task_root)
        self.settings_path = Path(settings_path) if settings_path else None
        self.log_dir = Path(log_dir) if log_dir else None
        self.round_log = round_log
        self.export_root = (
            Path(export_root).expanduser().resolve()
            if export_root else Path("runs/exports").resolve()
        )
        self.worker_roles = WorkerRoleStore(
            worker_roles_path, backup_dir=worker_roles_backup_dir)
        self.prompt_log_path = (
            Path(prompt_log_path) if prompt_log_path is not None else None)
        self.prompt_log_enabled = False
        #: 系统设置（providers/models/credentials）的写入口。
        #: 由组合根注入；测试不注入时系统设置页只读。
        self.system_settings = system_settings
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        self.availability = AgentAvailability()
        self._agent_ids = self._load_agent_ids()
        for aid in self._agent_ids:
            self.availability.register(aid)

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        # Front Agent 是固定入口；多个 Job 并发时不能同时改同一个 Brain 的
        # thinking/budget 状态。
        self._front_lock = threading.Lock()
        self._jobs: Dict[str, WorkJob] = {}
        self._job_order: List[str] = []
        self._messages: List[Dict[str, Any]] = []
        self._agent_tasks: Dict[str, Dict[str, str]] = {}
        # 线程池只负责承载等待中的 Job；真正的并行上限由
        # _active_jobs + max_parallel_teams 动态控制，这样设置可以即时生效。
        self._executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="aieos-team",
        )
        self._active_jobs = 0
        self._closed = False
        #: Front 自己的持久草稿（按 agent 私有空间隔离）。
        self._front_drafts_store: Optional[TaskDraftStore] = None
        self._load_settings()
        self._apply_prompt_log_config()

        # 固定 Front Agent 不参与组队，也不显示为 WORKING。
        if self.front_agent not in self._agent_ids:
            raise ValueError(
                f"Front Agent {self.front_agent!r} 不在 Agent Directory 里")
        self._configure_front_brain()

    # ------------------------------------------------------------
    # 设置 / 状态
    # ------------------------------------------------------------
    def _load_agent_ids(self) -> List[str]:
        try:
            entries = self.rt.directory.all()
        except Exception:                                     # noqa: BLE001
            entries = []
        ids = []
        for entry in entries:
            aid = str(getattr(entry, "agent_id", "") or "").strip()
            if aid and aid not in ids:
                ids.append(aid)
        return ids

    def _load_settings(self) -> None:
        if self.settings_path is None or not self.settings_path.exists():
            return
        try:
            data = json.loads(self.settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        front = str(data.get("front_agent") or "").strip()
        if front in self._agent_ids:
            self.front_agent = front
        max_teams = data.get("max_parallel_teams")
        if isinstance(max_teams, int) and max_teams > 0:
            self.max_parallel_teams = max_teams
        max_turns = data.get("max_solo_turns")
        if isinstance(max_turns, int) and max_turns > 0:
            self.max_solo_turns = max_turns
        pi_agents = data.get("pi_work_agents")
        if isinstance(pi_agents, list):
            self.pi_work_agents = {
                str(item).strip() for item in pi_agents
                if str(item).strip() in self._agent_ids
            }
        disabled = data.get("disabled_agents")
        if isinstance(disabled, list):
            self.disabled_agents = {
                str(item).strip() for item in disabled
                if str(item).strip() in self._agent_ids
                and str(item).strip() != self.front_agent
            }
        self.prompt_log_enabled = bool(data.get("prompt_log_enabled", False))
        if "pi_timeout_enabled" in data:
            self.pi_timeout_enabled = bool(data.get("pi_timeout_enabled"))
        try:
            timeout = float(data.get("pi_timeout_seconds") or 0)
        except (TypeError, ValueError):
            timeout = 0
        if timeout > 0:
            self.pi_timeout_seconds = timeout
        raw_thinking = data.get("agent_thinking")
        if isinstance(raw_thinking, dict):
            self.agent_thinking = {
                str(aid): str(level)
                for aid, level in raw_thinking.items()
                if str(aid) in self._agent_ids
            }
        mode = str(data.get("work_mode") or "workflow").strip().lower()
        if mode in ("workflow", "autonomous"):
            self.work_mode = mode
        front_mode = str(data.get("front_mode") or "autonomous").strip().lower()
        if front_mode in ("autonomous", "relay"):
            self.front_mode = front_mode

    def update_settings(
        self,
        *,
        front_agent: Optional[str] = None,
        max_parallel_teams: Optional[int] = None,
        max_solo_turns: Optional[int] = None,
        pi_work_agents: Optional[List[str]] = None,
        disabled_agents: Optional[List[str]] = None,
        prompt_log_enabled: Optional[bool] = None,
        agent_thinking: Optional[Dict[str, str]] = None,
        pi_timeout_enabled: Optional[bool] = None,
        pi_timeout_seconds: Optional[float] = None,
        work_mode: Optional[str] = None,
        front_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if front_agent is not None:
                front_agent = str(front_agent).strip()
                if front_agent not in self._agent_ids:
                    raise ValueError(f"未知 Agent：{front_agent!r}")
                if (
                    front_agent != self.front_agent
                    and self.availability.state(front_agent) == AgentAvailability.WORKING
                ):
                    raise ValueError(
                        f"{front_agent} 正在任务中，不能切换为 Front Agent")
                self.front_agent = front_agent
            if max_parallel_teams is not None:
                value = int(max_parallel_teams)
                if value < 1 or value > 8:
                    raise ValueError("max_parallel_teams 必须在 1..8")
                self.max_parallel_teams = value
            if max_solo_turns is not None:
                value = int(max_solo_turns)
                if value < 1 or value > 200:
                    raise ValueError("max_solo_turns 必须在 1..200")
                self.max_solo_turns = value
            if pi_work_agents is not None:
                wanted = {
                    str(item).strip() for item in pi_work_agents
                    if str(item).strip()
                }
                unknown = wanted - set(self._agent_ids)
                if unknown:
                    raise ValueError(
                        f"未知 Agent：{sorted(unknown)}")
                self.pi_work_agents = wanted
            if disabled_agents is not None:
                wanted = {
                    str(item).strip() for item in disabled_agents
                    if str(item).strip()
                }
                unknown = wanted - set(self._agent_ids)
                if unknown:
                    raise ValueError(f"未知 Agent：{sorted(unknown)}")
                if self.front_agent in wanted:
                    raise ValueError("Front Agent 不能被停用")
                self.disabled_agents = wanted
            if prompt_log_enabled is not None:
                self.prompt_log_enabled = bool(prompt_log_enabled)
                self._apply_prompt_log_config()
            if agent_thinking is not None:
                cleaned: Dict[str, str] = {}
                for aid, level in dict(agent_thinking).items():
                    aid = str(aid).strip()
                    level = str(level).strip().lower()
                    if aid not in self._agent_ids:
                        raise ValueError(f"未知 Agent：{aid!r}")
                    if level == "auto":
                        continue
                    allowed = self._agent_thinking_levels(aid)
                    if allowed and level not in allowed:
                        raise ValueError(
                            f"{aid} 的模型不支持 thinking level {level!r}；"
                            f"支持：{list(allowed)}")
                    cleaned[aid] = level
                self.agent_thinking = cleaned
            if pi_timeout_enabled is not None:
                self.pi_timeout_enabled = bool(pi_timeout_enabled)
            if pi_timeout_seconds is not None:
                value = float(pi_timeout_seconds)
                if value <= 0:
                    raise ValueError("pi_timeout_seconds 必须 > 0")
                self.pi_timeout_seconds = value
            if work_mode is not None:
                mode = str(work_mode).strip().lower()
                if mode not in ("workflow", "autonomous"):
                    raise ValueError(
                        "work_mode 必须是 workflow 或 autonomous")
                self.work_mode = mode
            if front_mode is not None:
                mode = str(front_mode).strip().lower()
                if mode not in ("autonomous", "relay"):
                    raise ValueError(
                        "front_mode 必须是 autonomous 或 relay")
                self.front_mode = mode
            self._persist_settings()
            self._configure_front_brain()
            self._notify()
            return self.settings_dict()

    def settings_dict(self) -> Dict[str, Any]:
        return {
            "front_agent": self.front_agent,
            "max_parallel_teams": self.max_parallel_teams,
            "max_agents_per_team": self.max_agents_per_team,
            "max_solo_turns": self.max_solo_turns,
            "pi_work_agents": sorted(self.pi_work_agents),
            "disabled_agents": sorted(self.disabled_agents),
            "prompt_log_enabled": bool(self.prompt_log_enabled),
            "agent_thinking": dict(self.agent_thinking),
            "pi_timeout_enabled": bool(self.pi_timeout_enabled),
            "pi_timeout_seconds": float(self.pi_timeout_seconds),
            "work_mode": str(self.work_mode),
            "front_mode": str(self.front_mode),
        }

    def _pi_timeout(self) -> float:
        return (float(self.pi_timeout_seconds)
                if self.pi_timeout_enabled else 0.0)

    def _agent_thinking_levels(self, agent_id: str) -> tuple:
        agent = self._agent(str(agent_id))
        brain = getattr(agent, "brain", None) if agent is not None else None
        settings = getattr(brain, "settings", None)
        return tuple(getattr(settings, "thinking_levels", ()) or ())

    def _thinking_for_agent(self, agent_id: str) -> str:
        return str(self.agent_thinking.get(str(agent_id)) or "auto")

    def _apply_prompt_log_config(self) -> None:
        prompt_log.configure(
            bool(self.prompt_log_enabled),
            self.prompt_log_path,
        )

    # ------------------------------------------------------------
    # 设置页：系统设置 / Worker Roles
    # ------------------------------------------------------------
    def settings_snapshot(self) -> Dict[str, Any]:
        system = {}
        if self.system_settings is not None:
            try:
                system = self.system_settings.snapshot()
            except Exception as exc:                          # noqa: BLE001
                system = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "regular": self.settings_dict(),
            "system": system,
            "roles": self.worker_roles.to_dict(),
            "agents": self._agent_settings_facts(),
        }

    def _pending_question(self, agent_id: str) -> Optional[Dict[str, Any]]:
        info = self._agent_tasks.get(str(agent_id), {}) or {}
        task_id = str(info.get("task_id") or "")
        if not task_id:
            return None
        path = self.task_directories.for_task(task_id).aieos / "question.json"
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _agent_settings_facts(self) -> List[Dict[str, Any]]:
        facts: List[Dict[str, Any]] = []
        from core.config_resolver import ConfigLoader
        from core.paths import GLOBAL_CONFIG_FILE
        resolved_agents = {}
        try:
            config = ConfigLoader.from_file(GLOBAL_CONFIG_FILE).canonical
            for aid, raw in (config.get("agents") or {}).items():
                brain = (raw or {}).get("brain") or {}
                resolved_agents[aid] = {
                    "model": str(brain.get("model") or ""),
                    "credential": str(brain.get("credential") or ""),
                }
        except Exception:                                     # noqa: BLE001
            resolved_agents = {}
        for aid in self._agent_ids:
            info = resolved_agents.get(aid, {})
            facts.append({
                "agent_id": aid,
                "enabled": aid not in self.disabled_agents,
                "is_front": aid == self.front_agent,
                "engine": "pi" if self._pi_enabled(aid) else "solo",
                "model": info.get("model", ""),
                "credential": info.get("credential", ""),
                "status": self.availability.state(aid),
                "has_brain": self._agent_has_brain(aid),
                "thinking": self._thinking_for_agent(aid),
                "thinking_levels": list(
                    self._agent_thinking_levels(aid)),
            })
        return facts

    def apply_system_settings(
        self, action: str, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self.system_settings is None:
            raise ValueError("系统设置写入未启用")
        with self._lock:
            result = self.system_settings.apply(action, payload or {})
        self._notify()
        return result

    def apply_worker_roles(
        self, action: str, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = dict(payload or {})
        with self._lock:
            if action == "upsert":
                self.worker_roles.upsert(payload)
            elif action == "archive":
                self.worker_roles.archive(str(payload.get("role_id") or ""))
            elif action == "reset":
                self.worker_roles.reset(str(payload.get("role_id") or ""))
            else:
                raise WorkerRoleError(f"未知角色动作：{action!r}")
        self._notify()
        return self.worker_roles.to_dict()

    def _persist_settings(self) -> None:
        if self.settings_path is None:
            return
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(self.settings_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------
    def submit(self, human_text: str) -> str:
        text = str(human_text or "").strip()
        if not text:
            raise ValueError("输入不能为空")
        job = WorkJob(
            job_id=f"job_{uuid.uuid4().hex[:10]}",
            human_text=text,
            created_at=_now(),
            updated_at=_now(),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)
            self._message("human", text, job_id=job.job_id)
        self._executor.submit(self._run_job, job)
        return job.job_id

    def answer_question(
        self, task_id: str, answer: str, *, option_id: str = "",
    ) -> str:
        """Human 直接回答 Agent 的中途问题；不经过 Front。"""
        text = str(answer or "").strip()
        with self._lock:
            try:
                task = self.rt.tasks.get(str(task_id))
            except Exception:                                 # noqa: BLE001
                task = None
            if task is None:
                raise ValueError("找不到这个 Task")
            if not getattr(task, "is_open", False):
                raise ValueError(f"Task 当前是 {task.status}，不能回答问题")
            directory = self.task_directories.for_task(task.task_id)
            qpath = directory.aieos / "question.json"
            try:
                question = json.loads(qpath.read_text("utf-8"))
            except (OSError, ValueError):
                question = None
            if not isinstance(question, dict):
                raise ValueError("这个 Task 当前没有待回答的问题")
            options = question.get("options") or []
            chosen = ""
            if option_id:
                for index, item in enumerate(options):
                    label = str(item)
                    if option_id in (label, str(index), str(index + 1)):
                        chosen = label
                        break
            answer_text = "\n".join(
                item for item in (chosen, text) if item)
            if not answer_text:
                raise ValueError("回答不能为空")
            worker = str(task.contractor or task.creator or "")
            if not worker:
                raise ValueError("Task 没有执行 Agent")
            directory.write_json("ANSWER.json", {
                "task_id": task.task_id,
                "question": question,
                "answer": answer_text,
                "answered_at": _now(),
            })
            try:
                qpath.unlink()
            except OSError:
                pass
            workspace_question = directory.workspace / "QUESTION.json"
            try:
                workspace_question.unlink()
            except OSError:
                pass
            history_path = directory.aieos / "questions.json"
            try:
                history = json.loads(
                    history_path.read_text("utf-8"))
            except (OSError, ValueError):
                history = []
            if not isinstance(history, list):
                history = []
            history.append({
                **question,
                "answer": answer_text,
                "answered_at": _now(),
            })
            directory.write_json("questions.json", history)
            job = WorkJob(
                job_id=f"job_{uuid.uuid4().hex[:10]}",
                human_text=f"回答：{answer_text}",
                task_id=task.task_id,
                front_intent="answer",
                front_decision={
                    "intent": "answer",
                    "task_id": task.task_id,
                    "answer": answer_text,
                    "question": question,
                },
                created_at=_now(),
                updated_at=_now(),
            )
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)
            self._message(
                "human", f"回答：{answer_text}",
                job_id=job.job_id, task_id=task.task_id)
        self._executor.submit(self._run_answer_job, job)
        return job.job_id

    def _run_answer_job(self, job: WorkJob) -> None:
        """回答后直接恢复 Pi Work；不调用 Front。"""
        worker = ""
        admitted = False
        prompt_log.bind_job(job.job_id)
        try:
            if not self._admit_job(job):
                return
            admitted = True
            task = self.rt.tasks.get(job.task_id)
            if task is None:
                self._fail(job, "找不到要恢复的 Task")
                return
            worker = str(task.contractor or task.creator or "")
            if not worker:
                self._fail(job, "Task 没有执行 Agent")
                return
            with self._condition:
                while not self._closed:
                    if self.availability.try_acquire(worker):
                        break
                    job.status = JobStatus.WAITING_FOR_AGENT
                    self._touch(job)
                    self._condition.wait(timeout=0.5)
                else:
                    return
            job.leader = worker
            job.members = [worker]
            self._bind_agent(worker, job, "LEADER")
            self._bind_task_to_agents([worker], task.task_id)
            self._set_job(job, JobStatus.RUNNING)
            self._message(
                "system",
                f"人类已回答，{worker} 继续 Task {task.task_id}。",
                job_id=job.job_id, task_id=task.task_id)
            if not self._pi_enabled(worker):
                self._fail(job, "当前问题回答恢复只支持 Pi Work")
                return
            acceptance = getattr(task, "acceptance", None) or {}
            role_id = str(
                acceptance.get("role") if isinstance(acceptance, dict) else ""
            ).strip()
            role = self.worker_roles.resolve(role_id, include_archived=True)
            if role is None:
                role = self.worker_roles.resolve("generalist")
            role_prompt = self.worker_roles.render(role) if role else ""
            runner = self._pi_runner(
                task,
                round_sink=lambda payload: self._record_round(
                    task.task_id, payload),
                should_stop=lambda: job.cancelled,
                role_prompt=role_prompt,
                role=role,
            )
            result = runner.run()
            if job.cancelled:
                self._finish_cancelled(job, task.task_id)
            else:
                self._finish_work(job, None, task.task_id, result)
        except Exception as exc:                              # noqa: BLE001
            self._fail(job, f"{type(exc).__name__}: {exc}")
        finally:
            if worker:
                self.availability.release(worker)
                if job.status != JobStatus.WAITING_HUMAN:
                    self._unbind_agent(worker)
            if admitted:
                with self._condition:
                    self._active_jobs = max(0, self._active_jobs - 1)
            prompt_log.clear_job()
            self._write_job_log(job)
            self._notify()

    def _admit_job(self, job: WorkJob) -> bool:
        """等待一个并行 Team 槽位；设置修改后可立即生效。"""
        with self._condition:
            while not self._closed:
                if self._active_jobs < self.max_parallel_teams:
                    self._active_jobs += 1
                    return True
                job.status = JobStatus.QUEUED
                self._touch(job)
                self._condition.wait(timeout=0.5)
        return False

    def _run_job(self, job: WorkJob) -> None:
        leader = ""
        members: List[str] = []
        admitted = False
        prompt_log.bind_job(job.job_id)
        try:
            if not self._admit_job(job):
                return
            admitted = True
            self._set_job(job, JobStatus.FRONT_ANALYZING)
            front = self._front_runtime()
            if front is None:
                self._fail(job, "Front Agent 没有可用的 Brain")
                return

            active, completed = self._task_refs()
            with self._front_lock:
                front_started = time.perf_counter()
                try:
                    turn = self._front_turn(
                        front, job, active, completed)
                except FrontUnavailableError as exc:
                    job.front_ms = max(0, int(
                        (time.perf_counter() - front_started) * 1000))
                    job.front_actions = list(
                        getattr(front, "last_action_history", []) or [])
                    job.front_decision = {
                        "error": str(exc),
                        "raw_output": str(
                            getattr(front, "last_raw_output", "") or ""),
                    }
                    self._interrupt_front(job, str(exc))
                    return
                job.front_ms = max(0, int(
                    (time.perf_counter() - front_started) * 1000))
                job.front_thinking = self._thinking_for(
                    self.front_agent, "decision")

            decisions = [item for item in (turn.decisions or [])]
            if not decisions:
                self._fail(job, "Front 没有给出任何决定")
                return
            control = [
                item for item in decisions
                if item.intent not in (
                    FrontIntent.HANDOFF, FrontIntent.REWORK,
                    FrontIntent.FOLLOW_UP,
                )
            ]
            work = [
                item for item in decisions
                if item.intent in (
                    FrontIntent.HANDOFF, FrontIntent.REWORK,
                    FrontIntent.FOLLOW_UP,
                )
            ]
            headline = work[0] if work else decisions[0]
            self._apply_front_mode(
                headline,
                front_mode=self._effective_front_mode(decisions))
            job.front_intent = headline.intent
            front_data = headline.to_dict()
            front_data["decisions"] = [
                item.to_dict() for item in decisions]
            front_data["raw_output"] = (
                str(getattr(turn, "raw_output", "") or "")
                or headline.raw_output)
            job.front_decision = front_data
            job.front_actions = list(
                getattr(turn, "action_history", None)
                or getattr(front, "last_action_history", [])
                or [])
            self._set_job(job, job.status)

            # 控制类动作可以先执行多个；执行类动作最多一个。
            for item in control:
                self._apply_control_decision(job, item)
            if len(work) > 1:
                self._fail(
                    job, "一次最多只能有一个执行类动作"
                    "（handoff / rework / follow_up）")
                return
            if not work:
                return
            decision = work[0]

            if decision.intent in (
                FrontIntent.REWORK, FrontIntent.FOLLOW_UP,
            ):
                leader, members = self._resume_or_rework(
                    job, decision, front)
                return

            # ---- handoff ----
            if self.solo_only:
                leader, members = self._run_handoff_solo(
                    job, decision, front)
                return

            leader = self._acquire_leader(job)
            if not leader:
                self._fail(job, "没有空闲 Agent 可以作为 Leader")
                return
            job.leader = leader
            self._bind_agent(leader, job, "LEADER")
            self._set_job(job, JobStatus.LEADER_PLANNING)

            plan = self._leader_plan(job, leader, decision.brief)
            if plan is None:
                self._fail(job, "Leader 没有产出可执行计划")
                return
            members = [
                item.agent_id for item in plan.items
                if item.agent_id != leader
            ]
            members = list(dict.fromkeys(members))
            # Leader 不选成员 = 它决定独立完成：按 SOLO 执行，
            # 不把「没分配给别人」当成失败。
            if not members:
                self._message(
                    "system",
                    f"Leader {leader} 决定独立完成；按 Solo 执行。",
                    job_id=job.job_id)

            acquired = self._acquire_members(members)
            if acquired is None:
                self._fail(job, "Leader 选择的成员已被占用，请重试")
                return
            job.members = list(members)
            for aid in members:
                self._bind_agent(aid, job, "MEMBER")

            mode = TaskMode.TEAM if members else TaskMode.SOLO
            try:
                self._resolve_brief_files(decision.brief)
            except ValueError as exc:
                self._fail(job, str(exc))
                return
            created = self._create_task(
                job, leader, members, plan, decision, mode=mode)
            if not created.get("ok"):
                self._fail(job, created.get("message") or "Task 创建失败")
                return
            task_id = str(created["task_id"])
            job.task_id = task_id
            self._bind_task_to_agents([leader, *members], task_id)
            self._set_job(job, JobStatus.RUNNING)
            if decision.reply:
                self._message(
                    "front", decision.reply,
                    job_id=job.job_id, task_id=task_id,
                    duration_ms=self._job_duration_ms(job),
                    thinking=job.front_thinking)
            detail = (
                f"已选择成员 {', '.join(members)}" if members
                else "独立执行")
            self._message(
                "system",
                f"Leader {leader} {detail}；Task {task_id} 开始执行。",
                job_id=job.job_id, task_id=task_id)

            task = self.rt.tasks.get(task_id)
            if task is None:
                self._fail(job, "Task 创建后无法读取")
                return
            if members:
                runner = TeamWorkRuntime(
                    self.rt, task,
                    brief=decision.brief.to_dict(),
                    initial_plan=plan,
                    max_worker_turns=self.max_worker_turns,
                    max_integration_turns=self.max_integration_turns,
                    cognitive_budget=self.cognitive_budget,
                    should_stop=lambda: job.cancelled,
                )
            else:
                runner = SoloWorkRuntime(
                    self.rt, task,
                    brief=decision.brief.to_dict(),
                    max_turns=self.max_solo_turns,
                    cognitive_budget=self.cognitive_budget,
                    should_stop=lambda: job.cancelled,
                    round_sink=lambda payload: self._record_round(
                        task_id, payload),
                )
            result = runner.run()
            if job.cancelled:
                self._finish_cancelled(job, task_id)
                return
            self._finish_work(job, front, task_id, result)
        except Exception as exc:                              # noqa: BLE001
            self._fail(job, f"{type(exc).__name__}: {exc}")
        finally:
            for aid in [leader, *members]:
                if aid:
                    self.availability.release(aid)
                    # WAITING_HUMAN 保留 agent→task 绑定，UI 才能继续显示
                    # 气泡并知道回答属于哪个 Task。
                    if job.status != JobStatus.WAITING_HUMAN:
                        self._unbind_agent(aid)
            if admitted:
                with self._condition:
                    self._active_jobs = max(0, self._active_jobs - 1)
            prompt_log.clear_job()
            self._write_job_log(job)
            self._notify()

    # ------------------------------------------------------------
    # Front / Leader / Task
    # ------------------------------------------------------------
    def _web_search_client(self, agent_id: str) -> WebSearchClient:
        agent = self._agent(str(agent_id or ""))
        if agent is None:
            raise WebSearchError(f"未知 Agent：{agent_id!r}")
        brain = getattr(agent, "brain", None)
        credential = None
        try:
            credential = brain.credential_for("deepseek") if brain else None
        except Exception:                                     # noqa: BLE001
            credential = None
        material = str(getattr(credential, "material", "") or "")
        if not material:
            raise WebSearchError(
                f"{agent_id} 没有可用的 DeepSeek credential，无法搜索。")
        return WebSearchClient(
            material,
            base_url=os.environ.get(
                "AIEOS_WEB_SEARCH_BASE_URL", "https://api.deepseek.com"),
            timeout_seconds=30.0,
        )

    def web_search(
        self, agent_id: str, query: str, *,
        count: int = 5, need_content: bool = False,
        ori_question: str = "",
    ) -> Dict[str, Any]:
        return self._web_search_client(agent_id).search(
            query, count=count, need_content=need_content,
            ori_question=ori_question)

    def web_fetch(self, agent_id: str, url: str) -> Dict[str, Any]:
        return self._web_search_client(agent_id).fetch(url)

    def _front_runtime(self) -> Optional[FrontAgentRuntime]:
        agent = self._agent(self.front_agent)
        if agent is None or not getattr(agent, "has_brain", False):
            return None

        def _front_think(prompt: str) -> str:
            # 🔒 Front 不注入 Agent 的身份 / 经济 / Constitution，
            #    也不看自我模型。它的判断材料已经在 prompt 里。
            return agent.think(
                prompt,
                cognitive=False,
                system=FRONT_SYSTEM_PROMPT,
                call_purpose="front",
            )

        dispatcher = build_front_dispatcher(
            workspace_of=self._workspace_of_agent,
            drafts=self._front_drafts(),
            artifact_read_fn=self._front_artifact_read,
            web_search_fn=lambda query, **kwargs: self.web_search(
                self.front_agent, query, **kwargs),
            web_fetch_fn=lambda url: self.web_fetch(
                self.front_agent, url),
        )

        def _execute(actions: List[Dict[str, Any]]) -> str:
            return execute_front_actions(
                dispatcher, actions, agent_id=self.front_agent)

        return FrontAgentRuntime(
            self.front_agent, _front_think,
            inspect_fn=self._front_inspect_facts,
            roles=self.worker_roles.catalog(),
            execute_actions=_execute,
            action_types=dispatcher.action_types,
            front_mode=self.front_mode)

    def _pi_enabled(self, agent_id: str) -> bool:
        return str(agent_id or "") in self.pi_work_agents

    def _pi_runner(
        self,
        task: Any,
        *,
        round_sink: Any = None,
        should_stop: Any = None,
        role_prompt: str = "",
        role: Any = None,
    ) -> PiWorkRuntime:
        acceptance = getattr(task, "acceptance", None)
        work_mode = (
            acceptance.get("work_mode")
            if isinstance(acceptance, dict) else "workflow")
        return PiWorkRuntime(
            self.rt, task,
            task_directory=self.task_directories.for_task(task.task_id),
            pi_cli_path=self.pi_cli_path,
            pi_provider=self.pi_provider,
            pi_model=self.pi_model,
            timeout_seconds=self._pi_timeout(),
            proxy_timeout_seconds=(
                self.pi_timeout_seconds if self.pi_timeout_seconds > 0
                else 300.0),
            should_stop=should_stop or (lambda: False),
            round_sink=round_sink,
            pi_agent_dir=self.pi_agent_dir,
            role_prompt=role_prompt,
            role=role,
            thinking=self._thinking_for_agent(
                task.contractor or task.creator),
            work_mode=str(work_mode or "workflow"),
        )

    def _front_turn(
        self,
        front: Any,
        job: WorkJob,
        active: List[Dict[str, Any]],
        completed: List[Dict[str, Any]],
    ) -> FrontTurn:
        """优先走多动作接口；旧 Front 实现仍按单动作兼容。"""
        method = getattr(front, "decide_turn", None)
        kwargs = {
            "active_tasks": active,
            "completed_tasks": completed,
            "conversation": self._conversation(job),
            "agents": self._agent_facts(),
            "context": (
                f"当前 Front 模式：{self.front_mode}\n"
                f"当前 Work 模式：{self.work_mode}\n"
                "可用 set_mode 修改；修改后对新任务生效。"),
        }
        if callable(method):
            return method(job.human_text, **kwargs)
        decision = front.decide(job.human_text, **kwargs)
        return FrontTurn(
            decisions=[decision],
            raw_output=str(getattr(decision, "raw_output", "") or ""),
        )

    def _apply_control_decision(
        self, job: WorkJob, decision: Any,
    ) -> None:
        """执行不启动 Task 的 Front 动作。"""
        intent = str(getattr(decision, "intent", "") or "")
        if intent == FrontIntent.DIRECT_REPLY:
            text = str(getattr(decision, "reply", "") or "")
            self._message(
                "front", text, job_id=job.job_id,
                duration_ms=job.front_ms,
                thinking=job.front_thinking)
            job.status = JobStatus.DIRECT_REPLY
            job.summary = text
            self._touch(job)
            return
        if intent == FrontIntent.CLARIFY:
            text = str(getattr(decision, "question", "") or "")
            self._message(
                "front", text, job_id=job.job_id,
                duration_ms=job.front_ms,
                thinking=job.front_thinking)
            job.status = JobStatus.CLARIFY
            job.summary = text
            self._touch(job)
            return
        if intent == FrontIntent.CANCEL:
            self._cancel_work(job, decision)
            return
        if intent == FrontIntent.ARTIFACTS:
            self._export_artifacts(job, decision)
            return
        if intent == FrontIntent.SET_MODE:
            self._apply_mode_change(job, decision)
            return
        raise ValueError(f"不支持的 Front 控制动作：{intent!r}")

    def _apply_mode_change(self, job: WorkJob, decision: Any) -> None:
        front_mode = str(
            getattr(decision, "front_mode", "") or "").strip().lower()
        work_mode = str(
            getattr(decision, "work_mode", "") or "").strip().lower()
        if not front_mode and not work_mode:
            self._reply_direct(
                job, "没有指定要修改的模式；请说明 Front 或 Work 模式。")
            return
        try:
            self.update_settings(
                front_mode=front_mode or None,
                work_mode=work_mode or None,
            )
        except ValueError as exc:
            self._reply_direct(job, f"模式修改失败：{exc}")
            return
        text = str(getattr(decision, "reply", "") or "").strip()
        if not text:
            parts = []
            if front_mode:
                parts.append(f"Front 模式 → {front_mode}")
            if work_mode:
                parts.append(f"Work 模式 → {work_mode}")
            text = "已更新：" + "；".join(parts) + "。"
        self._reply_direct(job, text)

    def _acquire_leader(self, job: WorkJob) -> Optional[str]:
        candidates = [
            aid for aid in self._agent_ids
            if aid != self.front_agent
            and aid not in self.disabled_agents
            and self._agent_has_brain(aid)
        ]
        with self._condition:
            while not self._closed:
                for aid in sorted(candidates):
                    if self.availability.try_acquire(aid):
                        return aid
                job.status = JobStatus.WAITING_FOR_LEADER
                self._touch(job)
                self._condition.wait(timeout=1.0)
        return None

    def _acquire_worker(self, job: WorkJob) -> Optional[str]:
        """Solo 模式：等待一个空闲 Agent 承接整个 Task。"""
        candidates = [
            aid for aid in self._agent_ids
            if aid != self.front_agent
            and aid not in self.disabled_agents
            and self._agent_has_brain(aid)
        ]
        with self._condition:
            while not self._closed:
                for aid in sorted(candidates):
                    if self.availability.try_acquire(aid):
                        return aid
                job.status = JobStatus.WAITING_FOR_AGENT
                self._touch(job)
                self._condition.wait(timeout=1.0)
        return None

    def _acquire_preferred_worker(
        self, job: WorkJob, agent_id: str) -> Optional[str]:
        """Human 显式指定执行者：只能用它，忙/不存在就拒绝 handoff。"""
        if agent_id == self.front_agent:
            self._fail(
                job, f"preferred_agent_is_front: {agent_id}")
            return None
        if (agent_id not in self._agent_ids
                or agent_id in self.disabled_agents
                or not self._agent_has_brain(agent_id)):
            self._fail(
                job, f"preferred_agent_unavailable: {agent_id}")
            return None
        if not self.availability.try_acquire(agent_id):
            self._fail(
                job, f"preferred_agent_busy: {agent_id}")
            return None
        return agent_id

    def _agent_facts(self) -> List[Dict[str, Any]]:
        """给 Front 的只读 Agent 事实；不替它做选择。"""
        facts = []
        for aid in self._agent_ids:
            facts.append({
                "agent_id": aid,
                "status": self.availability.state(aid),
                "role": (
                    "FRONT" if aid == self.front_agent else "WORKER"),
                "can_work": (
                    aid != self.front_agent
                    and aid not in self.disabled_agents
                    and self._agent_has_brain(aid)),
            })
        return facts

    def _front_inspect_facts(self, task_id: str) -> Dict[str, Any]:
        """只读 Task 事实（紧凑版），供 Front 的 inspect 使用。"""
        try:
            detail = self.task_detail(task_id)
        except Exception as exc:                              # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

        task = dict(detail.get("task") or {})
        # Inspect 是只读事实入口：把 Task Workspace 的真实宿主机路径
        # 一并交给 Front，避免它为了回答“文件在哪”去走 artifacts 导出。
        try:
            task_dir = self.task_directories.for_task(task_id)
            if task_dir is not None and task_dir.exists():
                task["workspace_path"] = str(task_dir.workspace)
        except Exception:                                     # noqa: BLE001
            pass
        job = dict(detail.get("job") or {})
        rounds = []
        for item in (detail.get("rounds") or [])[-5:]:
            rounds.append({
                "round": item.get("round"),
                "status_before": item.get("status_before"),
                "status_after": item.get("status_after"),
                "tokens": item.get("tokens"),
                "summary": str(item.get("summary") or "")[:800],
            })
        files = []
        for item in (detail.get("workspace_files") or [])[:30]:
            files.append({
                "path": item.get("path"),
                "type": item.get("type"),
                "chars": item.get("chars"),
            })
        return {
            "task": task,
            "job": {
                "status": job.get("status"),
                "error": job.get("error"),
                "turns": job.get("turns"),
                "tokens": job.get("tokens"),
                "front_intent": job.get("front_intent"),
            },
            "rounds": rounds,
            "files": files,
            "llm": detail.get("llm") or {},
        }

    def _effective_front_mode(self, decisions: List[Any]) -> str:
        """同一回合里若 Front 先 set_mode，handoff 按新模式处理。"""
        mode = self.front_mode
        for item in decisions or []:
            if getattr(item, "intent", "") != FrontIntent.SET_MODE:
                continue
            candidate = str(
                getattr(item, "front_mode", "") or "").strip().lower()
            if candidate in ("autonomous", "relay"):
                mode = candidate
        return mode

    def _apply_front_mode(
        self, decision: Any, *, front_mode: str = "",
    ) -> None:
        """relay 模式只保留核心 task_text，不让 Front 扩写进 Task。"""
        if str(front_mode or self.front_mode) != "relay":
            return
        if getattr(decision, "intent", "") != FrontIntent.HANDOFF:
            return
        brief = getattr(decision, "brief", None)
        if brief is None:
            return
        text = str(
            getattr(brief, "task_text", "")
            or getattr(brief, "goal", "")
            or getattr(brief, "human_text", "")
        ).strip()
        if not text:
            return
        brief.task_text = text
        brief.goal = text
        brief.deliverables = []
        brief.constraints = []
        brief.acceptance_expectations = []
        brief.ambiguities = []
        brief.non_goals = []
        brief.context_refs = []
        brief.files = []
        brief.fallback = False

    def _run_handoff_solo(
        self,
        job: WorkJob,
        decision: Any,
        front: Any,
    ) -> tuple[str, List[str]]:
        """Front handoff → 一个 Agent → SOLO Task → SoloWorkRuntime。"""
        role = self.worker_roles.resolve(getattr(decision, "role", ""))
        if role is None:
            self._fail(
                job,
                f"Front 选择了未知 Worker 角色："
                f"{getattr(decision, 'role', '')!r}")
            return "", []
        decision.role = role.role_id
        if isinstance(job.front_decision, dict):
            job.front_decision["role"] = role.role_id
        profile = str(
            getattr(decision, "work_profile", "") or "standard").strip()
        if profile not in ("direct", "standard", "creative_product"):
            self._fail(job, f"未知 work_profile：{profile!r}")
            return "", []
        decision.work_profile = profile
        if isinstance(job.front_decision, dict):
            job.front_decision["work_profile"] = profile
        role_prompt = self.worker_roles.render(role)
        preferred = str(
            getattr(decision, "preferred_agent", "") or "").strip()
        if preferred:
            worker = self._acquire_preferred_worker(job, preferred)
        else:
            worker = self._acquire_worker(job)
        if not worker:
            return "", []

        job.leader = worker
        self._bind_agent(worker, job, "WORKER")
        self._set_job(job, JobStatus.RUNNING)

        try:
            self._resolve_brief_files(decision.brief)
        except ValueError as exc:
            self._fail(job, str(exc))
            return worker, []

        created = self._create_solo_task(job, worker, decision)
        if not created.get("ok"):
            self._fail(job, created.get("message") or "Task 创建失败")
            return worker, []

        task_id = str(created["task_id"])
        job.task_id = task_id
        self._bind_task_to_agents([worker], task_id)
        if decision.reply:
            self._message(
                "front", decision.reply,
                job_id=job.job_id, task_id=task_id,
                duration_ms=self._job_duration_ms(job),
                thinking=job.front_thinking)
        self._message(
            "system",
            f"Agent {worker} 开始 Solo 执行；Task {task_id}。",
            job_id=job.job_id, task_id=task_id)

        task = self.rt.tasks.get(task_id)
        if task is None:
            self._fail(job, "Task 创建后无法读取")
            return worker, []

        if self._pi_enabled(worker):
            runner = self._pi_runner(
                task,
                round_sink=lambda payload: self._record_round(
                    task_id, payload),
                should_stop=lambda: job.cancelled,
                role_prompt=role_prompt,
                role=role)
        else:
            runner = SoloWorkRuntime(
                self.rt, task,
                brief=decision.brief.to_dict(),
                max_turns=self.max_solo_turns,
                cognitive_budget=self.cognitive_budget,
                role_prompt=role_prompt,
                should_stop=lambda: job.cancelled,
                round_sink=lambda payload: self._record_round(
                    task_id, payload),
            )
        result = runner.run()
        if job.cancelled:
            self._finish_cancelled(job, task_id)
        else:
            self._finish_work(job, front, task_id, result)
        return worker, []

    def _resolve_brief_files(self, brief: Any) -> None:
        """把 Front 的 from_private / draft_id 附件解析成真实 content。

        🔒 只读 Front 自己的私有空间 / 草稿；跨 Agent 的默认无权读仍成立。
        """
        files = list(getattr(brief, "files", []) or [])
        if not files:
            return
        workspace = self._workspace_of_agent(self.front_agent)
        drafts = self._front_drafts()
        resolved: List[Dict[str, str]] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            content = item.get("content")
            if not isinstance(content, str):
                source = str(item.get("from_private") or "").strip()
                if source:
                    if workspace is None:
                        raise ValueError(
                            f"Front 没有私有空间，无法读取附件 {source}")
                    try:
                        content = workspace.read_text(source)
                    except Exception as exc:              # noqa: BLE001
                        raise ValueError(
                            f"Front 附件读取失败 {source}："
                            f"{type(exc).__name__}: {exc}") from exc
                else:
                    draft_id = str(item.get("draft_id") or "").strip()
                    if draft_id:
                        read = drafts.read(self.front_agent, draft_id)
                        if not read.get("ok"):
                            raise ValueError(
                                f"Front 草稿读取失败 {draft_id}："
                                f"{read.get('message') or read.get('code')}")
                        content = read.get("content")
            if not isinstance(content, str):
                raise ValueError(f"Front 附件 {path or '?'} 没有内容来源")
            resolved.append({"path": path, "content": content})
        brief.files = resolved

    def _create_solo_task(
        self,
        job: WorkJob,
        worker: str,
        decision: Any,
    ) -> Dict[str, Any]:
        if self._pi_enabled(worker):
            return self._create_solo_task_pi(job, worker, decision)
        run = SimpleNamespace(
            run_id=f"aieos_{job.job_id}",
            root_task_id=None,
            budget_aic=10.0,
            workspace_id="",
            workspace_plan=None,
            status="INIT",
        )
        creation = TaskCreationService(
            self.rt, run, max_agents=self.max_agents_per_team)
        criteria = (
            decision.brief.acceptance_expectations
            or decision.brief.deliverables
            or [str(decision.brief.task_text
                    or decision.brief.human_text
                    or decision.brief.goal).strip()]
        )
        return creation.create(
            creator=worker,
            description=_description_from_brief(decision.brief),
            participants=[worker],
            acceptance_criteria=criteria,
            producer=worker,
            acceptance_assignee=None,
            workspace_plan=None,
            mode=TaskMode.SOLO,
        )

    def _create_solo_task_pi(
        self,
        job: WorkJob,
        worker: str,
        decision: Any,
    ) -> Dict[str, Any]:
        """Pi Work 的 Task：真实目录；不创建 Shared Context / 逻辑 Workspace。"""
        criteria = (
            decision.brief.acceptance_expectations
            or decision.brief.deliverables
            or [str(decision.brief.task_text
                    or decision.brief.human_text
                    or decision.brief.goal).strip()]
        )
        try:
            task = Task.create(
                creator=worker,
                description=_description_from_brief(decision.brief),
                participants=[worker],
                acceptance={
                    "criteria": list(criteria),
                    "role": str(getattr(decision, "role", "") or "generalist"),
                    "work_profile": str(
                        getattr(decision, "work_profile", "") or "standard"),
                    "work_mode": str(self.work_mode),
                    "front_mode": str(self.front_mode),
                },
                note=f"demand_run:aieos_{job.job_id}",
                mode=TaskMode.SOLO,
            )
            task.claim_producer(worker)
            self.rt.tasks.save(task)
            # Work 只负责生产；Task 预算不再是 Pi Work 的前置约束。
        except Exception as exc:                              # noqa: BLE001
            return {
                "ok": False, "code": "task_create_failed",
                "message": f"{type(exc).__name__}: {exc}",
            }
        return {
            "ok": True,
            "task_id": task.task_id,
            "workspace_id": "",
            "description": task.description,
        }

    def _leader_plan(
        self,
        job: WorkJob,
        leader: str,
        brief: Any,
    ) -> Optional[WorkPlan]:
        """Leader 规划，并从当前 IDLE Agent 中选择成员。"""
        for attempt in range(1, 3):
            idle = [
                aid for aid in self._agent_ids
                if aid != self.front_agent
                and self._agent_has_brain(aid)
                and (aid == leader or self.availability.is_idle(aid))
            ]
            # 至少给 Leader 自己 + 一个候选成员
            if len(idle) < 2:
                return None
            context = WorkContext(
                task_id=f"pending_{job.job_id}",
                agent_id=leader,
                role=WorkRole.LEAD,
                iteration=1,
                brief=brief.to_dict(),
                acceptance_criteria=(
                    brief.acceptance_expectations
                    or brief.deliverables
                    or [brief.goal]),
            )
            prompt = build_lead_plan_prompt(
                context,
                task_description=brief.goal,
                participants=idle,
                workspace_facts="",
            )
            agent = self._agent(leader)
            try:
                raw = agent.think(
                    prompt,
                    cognitive=False,
                    system=WORK_SYSTEM_PROMPT,
                )
            except Exception as exc:                          # noqa: BLE001
                self._message(
                    "system", f"Leader 规划失败：{exc}",
                    job_id=job.job_id)
                continue
            plan, error = parse_work_plan(raw, participants=idle)
            if plan is None:
                self._message(
                    "system", f"Leader 计划无效：{error}",
                    job_id=job.job_id)
                continue
            # Leader 不强制拥有 item：
            #   单文件任务常常由成员生产，Leader 只负责检查/汇总/交付。
            assigned = {
                item.agent_id for item in plan.items
                if item.agent_id != leader
            }
            busy = [
                aid for aid in assigned
                if not self.availability.is_idle(aid)
            ]
            if busy:
                self._message(
                    "system",
                    f"成员 {', '.join(sorted(busy))} 已被占用，重新规划。",
                    job_id=job.job_id)
                continue
            return plan
        return None

    def _acquire_members(self, members: List[str]) -> Optional[List[str]]:
        acquired: List[str] = []
        for aid in members:
            if not self.availability.try_acquire(aid):
                for done in acquired:
                    self.availability.release(done)
                return None
            acquired.append(aid)
        return acquired

    def _create_task(
        self,
        job: WorkJob,
        leader: str,
        members: List[str],
        plan: WorkPlan,
        decision: Any,
        *,
        mode: str = TaskMode.TEAM,
    ) -> Dict[str, Any]:
        participants = [leader, *members]
        run = SimpleNamespace(
            run_id=f"aieos_{job.job_id}",
            root_task_id=None,
            budget_aic=10.0,
            workspace_id="",
            workspace_plan=None,
            status="INIT",
        )
        creation = TaskCreationService(
            self.rt, run, max_agents=self.max_agents_per_team)
        criteria = (
            decision.brief.acceptance_expectations
            or decision.brief.deliverables
            or [str(decision.brief.task_text
                    or decision.brief.human_text
                    or decision.brief.goal).strip()]
        )
        # SOLO 没有其他成员，Leader 计划里的成员目录不适用；
        # 交给默认的 workspace 供给。
        workspace_plan = (
            self._workspace_plan(leader, members, plan.directories)
            if members else None
        )
        return creation.create(
            creator=leader,
            description=_description_from_brief(decision.brief),
            participants=participants,
            acceptance_criteria=criteria,
            producer=leader,
            acceptance_assignee=None,
            workspace_plan=workspace_plan,
            mode=mode,
        )

    # ------------------------------------------------------------
    # follow_up / rework：继续或重跑已有 Task
    # ------------------------------------------------------------
    def _resume_or_rework(
        self,
        job: WorkJob,
        decision: Any,
        front: Any,
    ) -> tuple[str, List[str]]:
        """Front 指向已有 Task 时，真正恢复执行 / Reopen，而不是只回一句话。"""
        try:
            task = self.rt.tasks.get(decision.task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            self._reply_direct(
                job, "找不到这个任务；它可能已经被清理了。", decision.task_id)
            return "", []

        role_id = str(getattr(decision, "role", "") or "").strip()
        if not role_id:
            acceptance = getattr(task, "acceptance", None) or {}
            if isinstance(acceptance, dict):
                role_id = str(acceptance.get("role") or "").strip()
        role = self.worker_roles.resolve(role_id, include_archived=True)
        if role is None:
            self._fail(
                job,
                f"Front 选择了未知 Worker 角色：{role_id!r}")
            return "", []
        decision.role = role.role_id
        if isinstance(job.front_decision, dict):
            job.front_decision["role"] = role.role_id
        # 返工 / 续跑也可能换角色：把本轮选择写回 Task，
        # 否则下一次 resume 会退回旧角色。
        acceptance = getattr(task, "acceptance", None)
        if not isinstance(acceptance, dict):
            acceptance = {}
        if acceptance.get("role") != role.role_id:
            acceptance["role"] = role.role_id
            task.acceptance = acceptance
            save_task = getattr(self.rt.tasks, "save", None)
            if callable(save_task):
                save_task(task)
        role_prompt = self.worker_roles.render(role)

        if self.solo_only and task.mode != TaskMode.SOLO:
            self._reply_direct(
                job,
                f"任务 {task.task_id} 是旧的 Team 任务；当前只支持 Solo，"
                "不能继续它。请重新提交为一个新任务；"
                "如果要停止它，直接说停止。",
                task.task_id,
            )
            return "", []

        if self._is_live(task.task_id):
            self._reply_direct(
                job,
                f"任务 {task.task_id} 正在执行中（{task.status}），"
                "完成后我会把结果告诉你。",
                task.task_id,
            )
            return "", []

        if task.status == TaskStatus.COMPLETED:
            if decision.intent != FrontIntent.REWORK:
                self._reply_direct(
                    job,
                    f"任务 {task.task_id} 已经完成了；"
                    "需要修改的话告诉我改什么。",
                    task.task_id,
                )
                return "", []
            action = "rework"
        elif task.is_open:
            action = "resume"
        else:
            self._reply_direct(
                job,
                f"任务 {task.task_id} 当前是 {task.status}，不能再继续或返工。",
                task.task_id,
            )
            return "", []

        participants = [str(p) for p in (task.participants or [])]
        if task.creator not in participants:
            participants.insert(0, task.creator)
        leader = task.creator
        if not self.availability.try_acquire(leader):
            self._reply_direct(
                job,
                f"原团队（{leader}）正在忙，等它空闲后我再继续这个任务。",
                task.task_id,
            )
            return "", []
        members = [aid for aid in participants if aid != leader]
        if self._acquire_members(members) is None:
            self.availability.release(leader)
            self._reply_direct(
                job,
                "原团队成员正在忙，等他们空闲后我再继续这个任务。",
                task.task_id,
            )
            return "", []

        job.leader = leader
        job.members = list(members)
        job.task_id = task.task_id
        self._bind_agent(leader, job, "LEADER")
        for aid in members:
            self._bind_agent(aid, job, "MEMBER")
        self._bind_task_to_agents([leader, *members], task.task_id)
        self._set_job(job, JobStatus.RUNNING)

        if decision.reply:
            self._message(
                "front", decision.reply,
                job_id=job.job_id, task_id=task.task_id,
                duration_ms=self._job_duration_ms(job),
                thinking=job.front_thinking)
        rework_scope = str(
            getattr(decision, "rework_scope", "") or "enhance").strip().lower()
        if rework_scope not in ("fix", "enhance", "redesign"):
            rework_scope = "enhance"
        if action == "rework":
            turn = int(getattr(task, "iteration", 1) or 1) + 1
            # Reopen 不覆盖旧历史；把本轮要求追加到 Task 描述，
            # 让 Leader / 成员都能看到 Human 这次改了什么。
            task.description = (
                f"{task.description}\n\n返工要求（第 {turn} 轮）：\n"
                f"{job.human_text}")
            self.rt.tasks.save(task)
            self._message(
                "system", f"原团队开始第 {turn} 轮返工。",
                job_id=job.job_id, task_id=task.task_id)
            reason = job.human_text
        else:
            self._message(
                "system",
                f"任务 {task.task_id} 之前中断了，从现有状态继续。",
                job_id=job.job_id, task_id=task.task_id)
            reason = ""

        if task.mode == TaskMode.TEAM:
            runner = TeamWorkRuntime(
                self.rt, task,
                max_worker_turns=self.max_worker_turns,
                max_integration_turns=self.max_integration_turns,
                cognitive_budget=self.cognitive_budget,
                should_stop=lambda: job.cancelled,
            )
        else:
            checkpoint = self._read_checkpoint(task.task_id)
            if checkpoint is None:
                rounds = self._rounds(task.task_id)
                if rounds:
                    checkpoint = checkpoint_from_rounds(
                        task.task_id, rounds,
                        iteration=int(getattr(task, "iteration", 1) or 1),
                        status=str(task.status or ""),
                        files=self._workspace_files(task))
            # 只传事实正文；两个 prompt（cognition / first Work）
            # 各自负责加自己的标题，避免标题重复两次。
            resume_context = (
                checkpoint.render() if checkpoint is not None else "")
            if self._pi_enabled(leader):
                runner = self._pi_runner(
                    task,
                    round_sink=lambda payload: self._record_round(
                        task.task_id, payload),
                    should_stop=lambda: job.cancelled,
                    role_prompt=role_prompt,
                    role=role)
            else:
                runner = SoloWorkRuntime(
                    self.rt, task,
                    max_turns=self.max_solo_turns,
                    cognitive_budget=self.cognitive_budget,
                    resume_context=resume_context,
                    role_prompt=role_prompt,
                    should_stop=lambda: job.cancelled,
                    round_sink=lambda payload: self._record_round(
                        task.task_id, payload),
                )
        if action == "rework":
            if isinstance(runner, PiWorkRuntime):
                result = runner.reopen_and_run(
                    reason=reason, rework_scope=rework_scope)
            else:
                result = runner.reopen_and_run(reason=reason)
        else:
            result = runner.run()
        if job.cancelled:
            self._finish_cancelled(job, task.task_id)
        else:
            self._finish_work(job, front, task.task_id, result)
        return leader, members

    def _finish_work(
        self,
        job: WorkJob,
        front: Any,
        task_id: str,
        result: Any,
    ) -> None:
        job.tokens = int(getattr(result, "tokens", 0) or 0)
        job.turns = int(getattr(result, "turns", 0) or 0)
        if getattr(result, "waiting_human", False):
            question = dict(getattr(result, "question", None) or {})
            job.status = JobStatus.WAITING_HUMAN
            job.front_intent = "question"
            job.summary = str(question.get("question") or "等待人类回答")
            job.error = ""
            self._message(
                "system",
                "Agent 需要人类确认：" + job.summary,
                job_id=job.job_id, task_id=task_id,
                duration_ms=self._job_duration_ms(job))
            self._write_checkpoint(task_id, result)
            self._touch(job)
            return
        front_cls = front or FrontAgentRuntime
        front_result = front_cls.result_from_work(result)
        self._message(
            "front" if front is not None else "system",
            front_cls.format_result(front_result),
            job_id=job.job_id, task_id=task_id,
            duration_ms=self._job_duration_ms(job))
        job.summary = front_result.summary or front_result.status
        if result.ok:
            job.status = JobStatus.COMPLETED
        elif _is_interruption(result.error):
            # 🔒 中断不是失败：Task 保持 IN_PROGRESS，
            #    Human 可以 follow_up 续跑。
            job.status = JobStatus.INTERRUPTED
            job.error = result.error or "任务被中断"
        else:
            job.status = JobStatus.FAILED
            job.error = result.error or f"Task {task_id} 未完成"
        self._write_checkpoint(task_id, result)
        self._touch(job)

    def _reply_direct(
        self, job: WorkJob, text: str, task_id: str = "",
    ) -> None:
        self._message(
            "front", text, job_id=job.job_id, task_id=task_id,
            duration_ms=self._job_duration_ms(job),
            thinking=job.front_thinking)
        job.status = JobStatus.DIRECT_REPLY
        job.summary = text
        self._touch(job)

    # ------------------------------------------------------------
    # cancel：真正停止执行中的 Task
    # ------------------------------------------------------------
    def _cancel_work(self, job: WorkJob, decision: Any) -> None:
        task_id = str(getattr(decision, "task_id", "") or "").strip()
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            self._reply_direct(
                job, "找不到要停止的任务；它可能已经被清理了。", task_id)
            return
        if task.status == TaskStatus.COMPLETED:
            self._reply_direct(
                job, f"任务 {task.task_id} 已经完成了，不需要停止。",
                task.task_id)
            return
        if not task.is_open:
            self._reply_direct(
                job,
                f"任务 {task.task_id} 当前是 {task.status}，没有在运行。",
                task.task_id,
            )
            return

        target = None
        with self._lock:
            for other in self._jobs.values():
                if other.job_id == job.job_id:
                    continue
                if other.task_id != task.task_id:
                    continue
                if other.status in _RUNNING_JOB_STATUSES:
                    target = other
                    break

        if target is None:
            if self._mark_task_cancelled(
                    task, reason=f"Human: {job.human_text}"):
                self._reply_direct(
                    job,
                    self._front_reply_text(
                        decision, f"任务 {task.task_id} 已停止。"),
                    task.task_id,
                )
            else:
                self._reply_direct(
                    job,
                    f"任务 {task.task_id} 停止失败，请稍后重试。",
                    task.task_id,
                )
            return

        target.cancelled = True
        self._set_job(target, JobStatus.CANCELLING)
        self._mark_task_cancelled(
            task, reason=f"Human: {job.human_text}")
        self._reply_direct(
            job,
            self._front_reply_text(
                decision,
                f"正在停止任务 {task.task_id}。已经通知执行中的团队，"
                "他们会在当前步骤结束后停下。"),
            task.task_id,
        )

    def _export_artifacts(self, job: WorkJob, decision: Any) -> None:
        task_id = str(getattr(decision, "task_id", "") or "").strip()
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            self._reply_direct(
                job, "找不到这个任务，无法导出产物。", task_id)
            return
        job.task_id = task.task_id
        # Pi Work 的真实目录：直接复制 workspace 到 export 根。
        try:
            task_dir = self.task_directories.for_task(task.task_id)
        except Exception:                                     # noqa: BLE001
            task_dir = None
        if task_dir is not None and task_dir.exists():
            destination = self.export_root / task.task_id
            try:
                shutil.copytree(
                    task_dir.workspace, destination, dirs_exist_ok=True)
                files = task_dir.list_files()
            except Exception as exc:                          # noqa: BLE001
                self._reply_direct(
                    job, f"产物导出失败：{type(exc).__name__}: {exc}",
                    task.task_id)
                return
            reply = str(getattr(decision, "reply", "") or "").strip()
            if reply:
                self._message(
                    "front", reply,
                    job_id=job.job_id, task_id=task.task_id,
                    duration_ms=self._job_duration_ms(job),
                    thinking=job.front_thinking)
            lines = [
                f"产物已从 Task 目录导出到宿主机目录：\n{destination}",
                "",
                f"共 {len(files)} 个文件：",
            ]
            lines.extend(f"- {item['path']}" for item in files)
            self._reply_direct(job, "\n".join(lines), task.task_id)
            job.summary = str(destination)
            return
        actor = task.creator or (
            task.participants[0] if task.participants else "")
        try:
            exported = export_task_workspace(
                getattr(self.rt, "workspace_gateway", None),
                workspace_id=task.workspace_id or "",
                agent_id=actor,
                task_id=task.task_id,
                dest_root=self.export_root,
                task_meta={
                    "status": task.status,
                    "mode": task.mode,
                    "iteration": task.iteration,
                    "creator": task.creator,
                },
            )
        except ArtifactExportError as exc:
            self._reply_direct(
                job,
                f"产物导出失败：{exc.message}",
                task.task_id,
            )
            return
        except Exception as exc:                              # noqa: BLE001
            self._reply_direct(
                job,
                f"产物导出失败：{type(exc).__name__}: {exc}",
                task.task_id,
            )
            return

        reply = str(getattr(decision, "reply", "") or "").strip()
        if reply:
            self._message(
                "front", reply,
                job_id=job.job_id, task_id=task.task_id,
                duration_ms=self._job_duration_ms(job),
                thinking=job.front_thinking)
        lines = [
            f"产物已从数据库导出到宿主机目录：\n{exported['path']}",
            "",
            f"共 {exported['count']} 个文件：",
        ]
        lines.extend(
            f"- {item['path']}" for item in exported["files"])
        self._reply_direct(job, "\n".join(lines), task.task_id)
        job.summary = exported["path"]

    def _mark_task_cancelled(self, task: Any, *, reason: str) -> bool:
        try:
            if task.status in (
                TaskStatus.CREATED,
                TaskStatus.ACCEPTED,
                TaskStatus.IN_PROGRESS,
            ):
                task.cancel(reason=reason)
            elif task.status in (
                TaskStatus.SUBMITTED, TaskStatus.VERIFIED,
            ):
                task.fail(reason=reason)
            else:
                return False
            self.rt.tasks.save(task)
            return True
        except Exception as exc:                              # noqa: BLE001
            self._message(
                "system",
                f"停止任务 {getattr(task, 'task_id', '')} 失败："
                f"{type(exc).__name__}: {exc}",
                task_id=getattr(task, "task_id", ""),
            )
            return False

    def _finish_cancelled(self, job: WorkJob, task_id: str) -> None:
        job.status = JobStatus.CANCELLED
        job.summary = "任务已停止"
        job.error = ""
        self._message(
            "system", f"任务 {task_id} 已停止。",
            job_id=job.job_id, task_id=task_id)
        self._touch(job)

    @staticmethod
    def _workspace_plan(
        leader: str,
        members: List[str],
        directories: Dict[str, str],
    ) -> Dict[str, Any]:
        dirs: Dict[str, str] = {}
        for path, owner in (directories or {}).items():
            # Leader 常用 "." / "/" 表示根目录；root_owner 已经单独表达，
            # 这里必须丢弃这些标记，否则会变成空目录名。
            clean = "/".join(
                part for part in
                str(path).strip().replace("\\", "/").split("/")
                if part and part != "."
            )
            if clean:
                dirs[clean] = str(owner)
        if not dirs:
            for aid in members:
                dirs[f"work/{aid}"] = aid
            dirs["work"] = leader
        # TaskCreationService 要求父目录显式声明；缺失的父目录交给 Leader。
        for path in list(dirs):
            parts = path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                dirs.setdefault(parent, leader)
        dirs["work"] = dirs.get("work", leader)
        return {
            "root_owner": leader,
            "directories": dirs,
            "files": {},
        }

    # ------------------------------------------------------------
    # 状态 / 消息
    # ------------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        with self._lock:
            agents = []
            for aid in self._agent_ids:
                state = self.availability.state(aid)
                role = "FRONT" if aid == self.front_agent else ""
                task = self._agent_tasks.get(aid, {})
                if task:
                    role = task.get("role", "")
                    running_job = self._jobs.get(task.get("job_id"))
                    if (
                        state == "WORKING"
                        and running_job is not None
                        and running_job.status == JobStatus.CANCELLING
                    ):
                        state = "STOPPING"
                agents.append({
                    "agent_id": aid,
                    "status": state,
                    "role": role,
                    "task_id": task.get("task_id", ""),
                    "has_brain": self._agent_has_brain(aid),
                    "enabled": aid not in self.disabled_agents,
                    "thinking": self._thinking_for_agent(aid),
                    "thinking_levels": list(
                        self._agent_thinking_levels(aid)),
                    "question": self._pending_question(aid),
                })
            jobs = [
                self._jobs[jid].to_dict()
                for jid in reversed(self._job_order[-30:])
                if jid in self._jobs
            ]
            messages = list(self._messages[-100:])
            tasks = self._task_cards()
            return {
                "front_agent": self.front_agent,
                "settings": self.settings_dict(),
                "agents": agents,
                "tasks": tasks,
                "jobs": jobs,
                "messages": messages,
                "running_jobs": sum(
                    1 for job in self._jobs.values()
                    if job.status in (
                        JobStatus.FRONT_ANALYZING,
                        JobStatus.WAITING_FOR_AGENT,
                        JobStatus.WAITING_FOR_LEADER,
                        JobStatus.LEADER_PLANNING,
                        JobStatus.WAITING_FOR_MEMBERS,
                        JobStatus.RUNNING,
                    )
                ),
            }

    def task_detail(self, task_id: str) -> Dict[str, Any]:
        """任务卡片点击后的日志数据。"""
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            raise ValueError(f"找不到 Task：{task_id}")

        executions = []
        try:
            for record in self.rt.tasks.executions_for(task_id):
                executions.append({
                    "event_type": record.event_type,
                    "from_agent": record.from_agent,
                    "status_after": record.status_after,
                    "created_at": record.created_at,
                    "result": (
                        record.result.to_dict() if record.result else None),
                    "verification": (
                        record.verification.to_dict()
                        if record.verification else None),
                })
        except Exception:                                     # noqa: BLE001
            executions = []

        job = None
        with self._lock:
            for item in self._jobs.values():
                if item.task_id == task_id:
                    job = item.to_dict()
        calls = self._llm_summary(task_id)
        return {
            "task": {
                "task_id": task.task_id,
                "status": task.status,
                "mode": task.mode,
                "iteration": task.iteration,
                "creator": task.creator,
                "contractor": task.contractor,
                "participants": list(task.participants or []),
                "workspace_id": task.workspace_id,
                "description": task.description,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            },
            "job": job,
            "executions": executions,
            "workspace_files": self._workspace_files(task),
            "public_memory": self._public_memory_text(task),
            "llm": calls,
            "rounds": self._rounds(task_id),
            "messages": [
                message for message in self._messages
                if message.get("task_id") == task_id
            ],
        }

    def _checkpoint_path(self, task_id: str) -> Optional[Path]:
        if self.round_log is None:
            return None
        safe = "".join(
            ch for ch in str(task_id or "")
            if ch.isalnum() or ch in ("_", "-", "."))
        root = Path(self.round_log.root).parent / "checkpoints"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{safe or 'unknown'}.json"

    def _write_checkpoint(self, task_id: str, result: Any) -> None:
        """把这次 run 的事实写成 resume 用的 checkpoint。"""
        path = self._checkpoint_path(task_id)
        if path is None:
            return
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        rounds = self._rounds(task_id)
        files = self._workspace_files(task) if task is not None else []
        checkpoint = checkpoint_from_rounds(
            task_id,
            rounds,
            iteration=int(getattr(task, "iteration", 1) or 1),
            status=str(getattr(task, "status", "") or ""),
            error=str(getattr(result, "error", "") or ""),
            files=files,
        )
        if not checkpoint.summary:
            checkpoint.summary = str(getattr(result, "summary", "") or "")
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(checkpoint_dumps(checkpoint), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def _read_checkpoint(self, task_id: str) -> Optional[RunCheckpoint]:
        path = self._checkpoint_path(task_id)
        if path is None or not path.is_file():
            return None
        try:
            return RunCheckpoint.from_dict(
                json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _record_round(self, task_id: str, payload: Dict[str, Any]) -> None:
        if self.round_log is None:
            return
        try:
            self.round_log.append(task_id, payload)
        except Exception:                                     # noqa: BLE001
            pass

    def _rounds(self, task_id: str) -> List[Dict[str, Any]]:
        if self.round_log is None:
            return []
        try:
            data = self.round_log.read(task_id)
        except Exception:                                     # noqa: BLE001
            return []
        return data if isinstance(data, list) else []

    def _llm_summary(self, task_id: str) -> Dict[str, Any]:
        try:
            row = self.rt.recorder.db.fetchone(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(latency_ms), 0) AS latency_ms,
                       COALESCE(SUM(cost_ai), 0) AS cost_ai
                  FROM llm_calls
                 WHERE task_id = ?
                """,
                (str(task_id),),
            )
        except Exception:                                     # noqa: BLE001
            return {"calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "latency_ms": 0.0,
                    "avg_latency_ms": 0.0, "cost_ai": 0.0}
        if row is None:
            return {"calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "latency_ms": 0.0,
                    "avg_latency_ms": 0.0, "cost_ai": 0.0}
        calls = int(row["calls"] or 0)
        latency_ms = round(float(row["latency_ms"] or 0), 1)
        return {
            "calls": calls,
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "latency_ms": latency_ms,
            "avg_latency_ms": (
                round(latency_ms / calls, 1) if calls else 0.0),
            "cost_ai": round(float(row["cost_ai"] or 0), 4),
        }

    def _workspace_files(self, task: Any) -> List[Dict[str, Any]]:
        # Pi Work 的真实目录优先；旧 Task 继续走逻辑 Workspace。
        try:
            task_dir = self.task_directories.for_task(task.task_id)
            if task_dir.exists():
                return task_dir.list_files()
        except Exception:                                     # noqa: BLE001
            pass
        workspace_id = task.workspace_id or ""
        agent_id = task.creator or (
            task.participants[0] if task.participants else "")
        if not workspace_id or not agent_id:
            return []
        gateway = getattr(self.rt, "workspace_gateway", None)
        if gateway is None:
            return []
        files: List[Dict[str, Any]] = []

        def walk(path: str, depth: int) -> None:
            if len(files) >= 100:
                return
            try:
                result = gateway.list(workspace_id, path, agent_id)
            except Exception:                                 # noqa: BLE001
                return
            if not getattr(result, "success", False):
                return
            payload = getattr(result, "result", None) or {}
            for entry in payload.get("entries", []) or []:
                name = str(entry.get("name") or "")
                if not name:
                    continue
                child = (
                    f"{path.rstrip('/')}/{name}" if path not in ("", "/")
                    else name)
                if str(entry.get("type") or "") == "directory":
                    if depth < 4:
                        walk(child, depth + 1)
                else:
                    files.append({
                        "path": child,
                        "revision": entry.get("revision"),
                        "chars": entry.get("chars"),
                        "type": entry.get("type") or "file",
                    })

        walk("/", 0)
        return files

    def _public_memory_text(self, task: Any) -> str:
        service = getattr(self.rt, "public_working_memory", None)
        if service is None:
            return ""
        try:
            return service.render(task.task_id, task.creator)
        except Exception:                                     # noqa: BLE001
            return ""

    def _task_cards(self) -> List[Dict[str, Any]]:
        cards = []
        try:
            tasks = self.rt.tasks.list_tasks(limit=30)
        except Exception:                                     # noqa: BLE001
            tasks = []
        # 最近更新的在前
        tasks = sorted(
            tasks, key=lambda t: getattr(t, "updated_at", ""), reverse=True)
        live = self._live_task_ids()
        for task in tasks:
            status = task.status
            if task.is_open and task.task_id not in live:
                status = "INTERRUPTED"
            acceptance = getattr(task, "acceptance", None)
            if not isinstance(acceptance, dict):
                acceptance = {}
            cards.append({
                "task_id": task.task_id,
                "status": status,
                "mode": task.mode,
                "iteration": task.iteration,
                "description": (task.description or "")[:300],
                "leader": task.creator,
                "members": list(task.participants or []),
                "front_mode": str(acceptance.get("front_mode") or ""),
                "work_mode": str(acceptance.get("work_mode") or ""),
                "updated_at": task.updated_at,
            })
        return cards

    def _live_task_ids(self) -> set:
        with self._lock:
            return {
                job.task_id for job in self._jobs.values()
                if job.task_id and job.status in _RUNNING_JOB_STATUSES
            }

    def _is_live(self, task_id: str) -> bool:
        return str(task_id) in self._live_task_ids()

    def _task_refs(self):
        active, completed = [], []
        try:
            tasks = self.rt.tasks.list_tasks(limit=50)
        except Exception:                                     # noqa: BLE001
            return active, completed
        live = self._live_task_ids()
        for task in tasks:
            # Solo 模式不把旧的 TEAM 任务当作可继续 / 可返工的对象；
            # 它们只保留在任务卡片里作为历史。
            if self.solo_only and task.mode != TaskMode.SOLO:
                continue
            ref = {
                "task_id": task.task_id,
                "status": task.status,
                "summary": (task.description or "")[:300],
            }
            if task.status == TaskStatus.COMPLETED:
                completed.append(ref)
            elif task.is_open:
                if task.task_id not in live:
                    # 状态仍是 IN_PROGRESS，但没有 Job 在跑（例如进程重启、
                    # 上一轮失败）。不能把它说成「正在处理中」。
                    ref["status"] = "INTERRUPTED"
                active.append(ref)
        return active, completed

    def _conversation(self, job: WorkJob) -> List[Dict[str, str]]:
        """最近 Human / Front 对话；不包含当前这条 Human 输入。"""
        with self._lock:
            items = list(self._messages)
        out: List[Dict[str, str]] = []
        for item in items:
            role = str(item.get("role") or "")
            if role not in ("human", "front"):
                continue
            if role == "human" and item.get("job_id") == job.job_id:
                continue
            text = str(item.get("text") or "").strip()
            if text:
                out.append({"role": role, "text": text})
        return out[-12:]

    def _message(
        self,
        role: str,
        text: str,
        *,
        job_id: str = "",
        task_id: str = "",
        duration_ms: Optional[int] = None,
        thinking: Optional[Dict[str, Any]] = None,
        retryable: bool = False,
    ) -> None:
        with self._lock:
            self._messages.append({
                "role": role,
                "text": str(text),
                "job_id": job_id,
                "task_id": task_id,
                "duration_ms": (
                    max(0, int(duration_ms))
                    if duration_ms is not None else None),
                "thinking": (
                    dict(thinking) if isinstance(thinking, dict) else None),
                "retryable": bool(retryable),
                "at": _now(),
            })

    @staticmethod
    def _front_reply_text(decision: Any, fallback: str) -> str:
        """有 Front 自己的回复就用它；没有才用控制面事实。"""
        text = str(getattr(decision, "reply", "") or "").strip()
        return text or fallback

    @staticmethod
    def _job_duration_ms(job: WorkJob) -> int:
        """从 Human 提交到 Front 回复的真实耗时。"""
        return _duration_ms(job.created_at, _now())

    def _set_job(self, job: WorkJob, status: str) -> None:
        with self._lock:
            job.status = status
            self._touch(job)
        self._write_job_log(job)

    def _touch(self, job: WorkJob) -> None:
        job.updated_at = _now()

    def _write_job_log(self, job: WorkJob) -> None:
        """
        把一次 UI Job 的可见日志写到 `log_dir/<job_id>.json`。

        🔒 只保存已经发生的事实：job / messages。
           Task 的详细执行链仍在 DB，由 Task 卡片按需读取。
        """
        if self.log_dir is None:
            return
        try:
            with self._lock:
                messages = [
                    message for message in self._messages
                    if message.get("job_id") == job.job_id
                ]
                payload = {
                    "job": job.to_log_dict(),
                    "messages": messages,
                }
            self.log_dir.mkdir(parents=True, exist_ok=True)
            (self.log_dir / f"{job.job_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _fail(self, job: WorkJob, message: str) -> None:
        with self._lock:
            job.status = JobStatus.FAILED
            job.error = str(message)
            self._touch(job)
            self._message(
                "system", f"任务失败：{message}", job_id=job.job_id)
        self._write_job_log(job)

    def _interrupt_front(self, job: WorkJob, message: str) -> None:
        """Front 调用失败：可重试，不伪装成 clarify，也不创建 Task。"""
        with self._lock:
            job.status = JobStatus.INTERRUPTED
            job.error = str(message)
            job.retryable = True
            self._touch(job)
            self._message(
                "front",
                "这次 Front 调用失败了，没有创建任务。可以点重试，或把需求再发一次。\n\n"
                f"原因：{message}",
                job_id=job.job_id,
                duration_ms=job.front_ms,
                thinking=job.front_thinking,
                retryable=True,
            )
        self._write_job_log(job)

    def retry(self, job_id: str) -> str:
        """用同一个 Human 输入重试一次 Front 调用。"""
        with self._lock:
            old = self._jobs.get(str(job_id))
            if old is None:
                raise ValueError(f"找不到 Job：{job_id}")
            if not old.retryable and old.status not in (
                    JobStatus.INTERRUPTED, JobStatus.FAILED):
                raise ValueError("这个 Job 当前不需要重试")
            text = str(old.human_text or "").strip()
        if not text:
            raise ValueError("原 Job 没有可重试的输入")
        return self.submit(text)

    def _bind_agent(self, aid: str, job: WorkJob, role: str) -> None:
        with self._lock:
            self._agent_tasks[aid] = {
                "task_id": job.task_id,
                "job_id": job.job_id,
                "role": role,
            }

    def _bind_task_to_agents(self, agents: List[str], task_id: str) -> None:
        with self._lock:
            for aid in agents:
                if aid in self._agent_tasks:
                    self._agent_tasks[aid]["task_id"] = task_id

    def _unbind_agent(self, aid: str) -> None:
        with self._lock:
            self._agent_tasks.pop(aid, None)

    def _configure_front_brain(self) -> None:
        """Front 的决策思考强度：Auto 默认 low，可在 Agent 卡片修改。"""
        agent = self._agent(self.front_agent)
        brain = getattr(agent, "brain", None)
        setter = getattr(brain, "set_thinking", None)
        if not callable(setter):
            return
        level = self._thinking_for_agent(self.front_agent)
        if not level or level == "auto":
            level = "low"
        try:
            if level == "off":
                setter(False, None, phase="decision")
            else:
                setter(True, level, phase="decision")
        except Exception:                                     # noqa: BLE001
            return

    def _thinking_for(
        self, aid: str, phase: str = "decision",
    ) -> Optional[Dict[str, Any]]:
        """读取某个 Agent 某一相当前请求的思考强度（观测用）。"""
        agent = self._agent(aid)
        brain = getattr(agent, "brain", None)
        getter = getattr(brain, "thinking_for", None)
        if not callable(getter):
            return {"enabled": None, "effort": None}
        try:
            policy = getter(phase)
        except Exception:                                     # noqa: BLE001
            policy = None
        if isinstance(policy, dict):
            return {
                "enabled": policy.get("enabled"),
                "effort": policy.get("effort"),
            }
        return {"enabled": None, "effort": None}

    def _agent(self, aid: str) -> Any:
        try:
            return self.rt.economy.agent(aid)
        except Exception:                                     # noqa: BLE001
            return None

    def _workspace_of_agent(self, aid: str) -> Any:
        agent = self._agent(aid)
        return getattr(agent, "workspace", None)

    def _front_artifact_read(
        self,
        task_id: str,
        path: str,
        *,
        offset: Any = None,
        limit: Any = None,
    ) -> Dict[str, Any]:
        """Front 只读一个 Task Workspace 文件；不创建 Task、不读宿主路径。"""
        task_id = str(task_id or "").strip()
        path = str(path or "").strip()
        if not task_id or not path:
            return {"ok": False, "code": "bad_request",
                    "message": "artifact_read 需要 task_id 和 path"}
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            return {"ok": False, "code": "not_found",
                    "message": f"找不到 Task：{task_id}"}
        try:
            task_dir = self.task_directories.for_task(task_id)
        except Exception:                                     # noqa: BLE001
            task_dir = None
        if task_dir is not None and task_dir.exists():
            try:
                full_content = task_dir.read_text(path)
            except FileNotFoundError:
                return {"ok": False, "code": "not_found",
                        "message": f"没有这个文件：{path}"}
            except Exception as exc:                          # noqa: BLE001
                return {"ok": False, "code": "read_failed",
                        "message": f"{type(exc).__name__}: {exc}"}
            lines = full_content.splitlines()
            total_lines = len(lines)
            explicit = offset is not None or limit is not None
            if not explicit:
                content = full_content
                start_line = 1 if total_lines else 0
                end_line = total_lines
            else:
                try:
                    start_line = int(offset) if offset is not None else 1
                    applied_limit = (
                        int(limit) if limit is not None else max(
                            1, total_lines - start_line + 1))
                except (TypeError, ValueError):
                    return {"ok": False, "code": "bad_pagination",
                            "message": "offset / limit 必须是整数"}
                if start_line < 1:
                    start_line = 1
                if applied_limit < 1:
                    return {"ok": False, "code": "bad_pagination",
                            "message": "limit 必须 >= 1"}
                end_line = min(total_lines, start_line + applied_limit - 1)
                content = "\n".join(lines[start_line - 1:end_line])
            return {
                "ok": True, "task_id": task_id, "path": path,
                "content": content, "chars": len(content),
                "total_chars": len(full_content),
                "total_lines": total_lines,
                "start_line": start_line, "end_line": end_line,
                "truncated": explicit and (
                    start_line > 1 or end_line < total_lines),
                "read_as": "pi_task_directory",
            }
        workspace_id = str(getattr(task, "workspace_id", "") or "")
        if not workspace_id:
            return {"ok": False, "code": "no_workspace",
                    "message": f"Task {task_id} 没有 Workspace"}
        participants = list(getattr(task, "participants", None) or [])
        actor = str(
            getattr(task, "creator", "")
            or (participants[0] if participants else ""))
        if not actor:
            return {"ok": False, "code": "no_actor",
                    "message": f"Task {task_id} 没有可用的读取身份"}
        gateway = getattr(self.rt, "workspace_gateway", None)
        if gateway is None:
            return {"ok": False, "code": "no_gateway",
                    "message": "当前 Runtime 没有 Workspace Gateway"}
        # 默认只取 200 行，避免一次读入整份大文件；调用方可用 limit 覆盖。
        effective_limit = 200 if limit is None else limit
        try:
            effective_offset = (
                int(offset) if offset is not None else None)
            effective_limit = int(effective_limit)
        except (TypeError, ValueError):
            return {"ok": False, "code": "bad_pagination",
                    "message": "offset / limit 必须是整数"}
        if effective_offset is not None and effective_offset < 1:
            effective_offset = 1
        try:
            raw = read_artifact(
                gateway, workspace_id, actor, path,
                offset=effective_offset, limit=effective_limit)
        except Exception as exc:                              # noqa: BLE001
            return {"ok": False, "code": "read_failed",
                    "message": f"{type(exc).__name__}: {exc}"}
        if not isinstance(raw, dict):
            return {"ok": False, "code": "invalid_result",
                    "message": "read_artifact 返回的不是对象"}
        if raw.get("ok"):
            raw = dict(raw)
            raw["task_id"] = task_id
            raw["read_as"] = actor
        return raw

    def _front_drafts(self) -> TaskDraftStore:
        if self._front_drafts_store is None:
            # Front 没有 Task；用固定的 personal draft bucket。
            self._front_drafts_store = TaskDraftStore(
                task_id="front",
                workspace_of=self._workspace_of_agent,
            )
        return self._front_drafts_store

    def _agent_has_brain(self, aid: str) -> bool:
        agent = self._agent(aid)
        return bool(agent is not None and getattr(agent, "has_brain", False))

    def _notify(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._notify()
        self._executor.shutdown(wait=False, cancel_futures=True)
        prompt_log.clear_job()
        prompt_log.configure(False, None)


def _description_from_brief(brief: Any) -> str:
    """Work 看到 Front 选择的 `task_text`；它可以是原文或忠实重述。"""
    return str(
        getattr(brief, "task_text", "")
        or getattr(brief, "human_text", "")
        or getattr(brief, "goal", "")
    ).strip()


def _duration_ms(start: Any, end: Any = "") -> int:
    """两个 ISO 时间点之间的毫秒数；解析失败返回 0（不猜）。"""
    try:
        start_dt = datetime.fromisoformat(str(start))
        end_dt = (
            datetime.fromisoformat(str(end)) if end
            else datetime.now(timezone.utc))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        return max(0, int((end_dt - start_dt).total_seconds() * 1000))
    except (TypeError, ValueError):
        return 0


def _is_interruption(error: Any) -> bool:
    """LLM 超时/中断 ≠ 业务失败；Task 可以续跑。"""
    text = str(error or "").lower()
    return (
        "llmtimeouterror" in text
        or "超时" in text
        or "timeout" in text
        or "timed out" in text
        or "max_turns" in text
        or "run_interrupted" in text
    )


def _now() -> str:
    from core.identity import utc_now_iso
    return utc_now_iso()
