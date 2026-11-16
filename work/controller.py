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
import logging
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
from typing import Any, Dict, List, Mapping, Optional

from llm import prompt_log
from llm.session import alarm_scope, front_scope
from llm.web_search import WebSearchClient, WebSearchError
from commit.models import CommitMode, CommitStatus
from commit.service import CommitError, OutputCommitService
from commit.store import CommitStore
from core.alarm import WakeupEvent, WakeupResult
from core.alarm_store import AlarmStore
from core.task import Task, TaskMode, TaskStatus
from core.timezones import DEFAULT_TIMEZONE, normalize_timezone
from demand.task_creation import TaskCreationService
from services.alarm_service import AlarmService, WakeupDispatcher
from services.artifacts import read_artifact
from work.artifact_export import (
    ArtifactExportError, export_task_workspace,
)
from front import (
    FRONT_SYSTEM_PROMPT, FrontAgentRuntime, FrontIntent, FrontResult,
    FrontTurn, FrontUnavailableError,
)
from communication.inbox import InboxPolicy
from communication.inbox_runtime import InboxConsumer
from work.availability import AgentAvailability
from work.checkpoint import (
    RunCheckpoint, checkpoint_from_rounds, dumps as checkpoint_dumps,
)
from work.drafts import TaskDraftStore
from work.front_actions import (
    build_front_dispatcher, execute_front_actions,
)
from work.front_wakeup import FrontWakeupHandler
from work.handoff import HandoffRuntime
from work.life_inbox_service import LifeInboxService
from work.pi_inbox_service import PiInboxService
from work.pi_runtime import PiWorkRuntime
from work.project_context import render_project_context
from work.project_lock import ProjectLockManager
from work.task_directory import TaskDirectoryManager
from work.task_snapshot import TaskSnapshotStore
from work.worker_roles import WorkerRoleError, WorkerRoleStore
from work.plan import WorkPlan, parse_work_plan
from work.prompt import WORK_SYSTEM_PROMPT, build_lead_plan_prompt
from work.roles import WorkContext, WorkRole, resolve_agent_name
from work.runtime import SoloWorkRuntime
from work.team_runtime import TeamWorkRuntime


logger = logging.getLogger(__name__)


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
    #: 父 Job：一次派发多个子任务，自己不执行 Task。
    BATCH_DISPATCHED = "BATCH_DISPATCHED"
    #: LLM 超时/中断：Task 仍是 IN_PROGRESS，后续可以 follow_up 续跑。
    INTERRUPTED = "INTERRUPTED"
    #: Agent 在 Work 中途等待人类回答；Task / 阶段都保持原状态。
    WAITING_HUMAN = "WAITING_HUMAN"
    DIRECT_REPLY = "DIRECT_REPLY"
    CLARIFY = "CLARIFY"
    #: 闹钟唤醒 Job；不占用 Task 的 live 状态。
    ALARM_RUNNING = "ALARM_RUNNING"
    #: 闹钟唤醒后决定不打扰人类；Activity Record 仍会写入。
    NO_NOTIFY = "NO_NOTIFY"
    #: 闹钟唤醒派发了 Task；具体执行由子 Job 负责。
    DISPATCHED = "DISPATCHED"


#: 一次 Front 回合最多派发多少个独立 Task。
MAX_BATCH_TASKS = 8


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
    JobStatus.BATCH_DISPATCHED,
    JobStatus.NO_NOTIFY,
    JobStatus.DISPATCHED,
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
    #: 批量派发：父 Job ID / 子 Job 序号；普通 Job 为空。
    batch_id: str = ""
    parent_job_id: str = ""
    batch_index: int = 0
    batch_size: int = 0
    #: 批量派发时预先占用的 Worker；子 Job 不再重新竞争。
    assigned_worker: str = ""
    #: 子 Job 直接使用的 FrontDecision（跳过再次调用 Front）。
    precomputed_decision: Any = None
    #: project 模式：Agent 直接编辑的用户项目目录；空 = Task Workspace。
    workdir: str = ""
    #: 本次 Task 选定的输出仓库；空 = 使用全局默认。
    commit_repo: str = ""
    #: Human Gateway 写入 Journal 后返回的消息 ID / 会话 ID。
    human_message_id: str = ""
    conversation_id: str = ""
    #: human / alarm。alarm Job 不来自 Human 消息。
    source: str = "human"
    wakeup_id: str = ""
    wakeup_event: Dict[str, Any] = field(default_factory=dict)
    #: Front → Worker 的 TASK_OFFER 消息 ID / handoff 会话 ID。
    handoff_message_id: str = ""
    handoff_conversation_id: str = ""
    #: UI 客户端幂等键；同一个键重试不会创建第二个 Job。
    client_message_id: str = ""
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
    #: cancel / interrupt。决定停止后 Job 的终态与 Task 是否保留。
    stop_mode: str = ""
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
            "batch_id": self.batch_id,
            "parent_job_id": self.parent_job_id,
            "batch_index": self.batch_index,
            "batch_size": self.batch_size,
            "workdir": self.workdir,
            "commit_repo": self.commit_repo,
            "human_message_id": self.human_message_id,
            "conversation_id": self.conversation_id,
            "source": self.source,
            "wakeup_id": self.wakeup_id,
            "handoff_message_id": self.handoff_message_id,
            "handoff_conversation_id": self.handoff_conversation_id,
            "client_message_id": self.client_message_id,
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
            "stop_mode": self.stop_mode,
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
        commit_mode: str = "manual",
        commit_output_repo: str = "",
        commit_base_ref: str = "main",
        commit_auto_merge: bool = False,
        commit_max_file_bytes: int = 20 * 1024 * 1024,
        commit_max_total_bytes: int = 50 * 1024 * 1024,
        commit_service: Any = None,
        task_root: Optional[Path] = None,
        settings_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        round_log: Any = None,
        export_root: Optional[Path] = None,
        worker_roles_path: Optional[Path] = None,
        worker_roles_backup_dir: Optional[Path] = None,
        prompt_log_path: Optional[Path] = None,
        system_settings: Any = None,
        human_gateway: Any = None,
        alarms_enabled: bool = False,
        alarm_min_interval_seconds: int = 60,
        alarm_max_active_per_agent: int = 5,
        alarm_guard_max_per_agent: int = 0,
        alarm_guard_max_total: int = 0,
    ) -> None:
        self.rt = runtime
        self.front_agent = str(front_agent)
        self.human_gateway = human_gateway
        self.alarms_enabled = bool(alarms_enabled)
        self.alarm_min_interval_seconds = max(
            1, int(alarm_min_interval_seconds))
        self.alarm_max_active_per_agent = max(
            1, int(alarm_max_active_per_agent))
        self.alarm_guard_max_per_agent = max(
            0, int(alarm_guard_max_per_agent))
        self.alarm_guard_max_total = max(0, int(alarm_guard_max_total))
        #: Wakeup V1：单进程内的闹钟服务。
        self.alarm_service: Optional[AlarmService] = None
        self._alarm_dispatcher = WakeupDispatcher()
        #: Activity 未读水位：agent_id -> ISO 时间。
        self._activity_seen: Dict[str, str] = {}
        #: UI 客户端幂等：client_message_id → 已创建的 Job。
        self._submit_lock = threading.Lock()
        self._client_submissions: Dict[str, Dict[str, str]] = {}
        archive_base = (
            Path(settings_path).parent if settings_path
            else (Path(log_dir).parent if log_dir else None)
        )
        self._archive_path = (
            archive_base / "archived_tasks.json"
            if archive_base is not None else None)
        self._archived_tasks = self._load_archived_tasks()
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
        #: 归档 Agent：从默认列表隐藏，并强制停用；历史保留。
        self.archived_agents: set[str] = set()
        #: Agent 卡片选择的思考强度；"auto" = 阶段策略。
        self.agent_thinking: Dict[str, str] = {}
        #: Work 模式：workflow = 阶段化；autonomous = 单阶段自主工作。
        self.work_mode = "autonomous"
        #: Front 模式：autonomous = 当前会补全 brief；relay = 只传达。
        self.front_mode = "autonomous"
        #: Front prompt 里的「当前时间」时区；存储仍然是 UTC。
        self.timezone = DEFAULT_TIMEZONE
        self.pi_cli_path = str(pi_cli_path or "pi")
        self.pi_provider = str(pi_provider or "")
        self.pi_model = str(pi_model or "")
        self.pi_agent_dir = Path(pi_agent_dir) if pi_agent_dir else None
        self.pi_timeout_seconds = float(pi_timeout_seconds)
        self.pi_timeout_enabled = bool(pi_timeout_enabled)
        mode = str(commit_mode or "manual").strip().lower()
        if mode not in (CommitMode.MANUAL.value, CommitMode.LIMITED.value,
                        CommitMode.AUTO.value):
            mode = CommitMode.MANUAL.value
        self.commit_mode = mode
        self.commit_output_repo = str(commit_output_repo or "").strip()
        self.commit_base_ref = str(commit_base_ref or "main").strip() or "main"
        self.commit_auto_merge = bool(commit_auto_merge)
        self.commit_max_file_bytes = max(1, int(commit_max_file_bytes))
        self.commit_max_total_bytes = max(1, int(commit_max_total_bytes))
        self.task_directories = TaskDirectoryManager(task_root)
        self.project_locks = ProjectLockManager(
            self.task_directories.root.parent / "runs" / "project_locks")
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
        #: Phase 2：Pi Agent 的常驻收件箱服务。
        self._pi_inbox_service: Optional[PiInboxService] = None
        #: Phase 3：Front 的常驻收件箱消费者。
        self._front_inbox_consumer: Optional[InboxConsumer] = None
        #: Phase 5：Life 层空闲 Agent 的可选自动回复。
        self._life_inbox_service: Optional[LifeInboxService] = None
        #: Front 自己的持久草稿（按 agent 私有空间隔离）。
        self._front_drafts_store: Optional[TaskDraftStore] = None
        self._load_settings()
        self._sync_inbox_services()
        self._apply_prompt_log_config()
        if commit_service is not None:
            self.commit_service = commit_service
        else:
            self.commit_service = self._build_commit_service()

        # 固定 Front Agent 不参与组队，也不显示为 WORKING。
        if self.front_agent not in self._agent_ids:
            raise ValueError(
                f"Front Agent {self.front_agent!r} 不在 Agent Directory 里")
        self._configure_front_brain()
        self._init_alarms()

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
        archived = data.get("archived_agents")
        if isinstance(archived, list):
            self.archived_agents = {
                str(item).strip() for item in archived
                if str(item).strip() in self._agent_ids
                and str(item).strip() != self.front_agent
            }
        # 归档必须同时停用：否则调度器仍可能把任务给它。
        self.disabled_agents |= set(self.archived_agents)
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
        raw_seen = data.get("activity_seen")
        if isinstance(raw_seen, dict):
            self._activity_seen = {
                str(aid): str(value)
                for aid, value in raw_seen.items()
                if str(aid) in self._agent_ids and str(value).strip()
            }
        mode = str(data.get("work_mode") or "workflow").strip().lower()
        if mode in ("workflow", "autonomous"):
            self.work_mode = mode
        front_mode = str(data.get("front_mode") or "autonomous").strip().lower()
        if front_mode in ("autonomous", "relay"):
            self.front_mode = front_mode
        try:
            self.timezone = normalize_timezone(data.get("timezone"))
        except ValueError:
            self.timezone = DEFAULT_TIMEZONE
        commit_mode = str(
            data.get("commit_mode") or "manual").strip().lower()
        if commit_mode in (
            CommitMode.MANUAL.value, CommitMode.LIMITED.value,
            CommitMode.AUTO.value,
        ):
            self.commit_mode = commit_mode
        if "commit_output_repo" in data:
            self.commit_output_repo = str(
                data.get("commit_output_repo") or "").strip()
        if "commit_base_ref" in data:
            self.commit_base_ref = (
                str(data.get("commit_base_ref") or "main").strip()
                or "main")
        if "commit_auto_merge" in data:
            self.commit_auto_merge = bool(data.get("commit_auto_merge"))
        for key, attr in (
            ("commit_max_file_bytes", "commit_max_file_bytes"),
            ("commit_max_total_bytes", "commit_max_total_bytes"),
        ):
            try:
                value = int(data.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                setattr(self, attr, value)

    def update_settings(
        self,
        *,
        front_agent: Optional[str] = None,
        max_parallel_teams: Optional[int] = None,
        max_solo_turns: Optional[int] = None,
        pi_work_agents: Optional[List[str]] = None,
        disabled_agents: Optional[List[str]] = None,
        archived_agents: Optional[List[str]] = None,
        prompt_log_enabled: Optional[bool] = None,
        agent_thinking: Optional[Dict[str, str]] = None,
        pi_timeout_enabled: Optional[bool] = None,
        pi_timeout_seconds: Optional[float] = None,
        work_mode: Optional[str] = None,
        front_mode: Optional[str] = None,
        commit_mode: Optional[str] = None,
        commit_output_repo: Optional[str] = None,
        commit_base_ref: Optional[str] = None,
        commit_auto_merge: Optional[bool] = None,
        commit_max_file_bytes: Optional[int] = None,
        commit_max_total_bytes: Optional[int] = None,
        timezone: Optional[str] = None,
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
            if archived_agents is not None:
                wanted = {
                    str(item).strip() for item in archived_agents
                    if str(item).strip()
                }
                unknown = wanted - set(self._agent_ids)
                if unknown:
                    raise ValueError(
                        f"未知 Agent：{sorted(unknown)}")
                if self.front_agent in wanted:
                    raise ValueError("Front Agent 不能被归档")
                self.archived_agents = wanted
                self.disabled_agents |= wanted - {self.front_agent}
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
            if commit_mode is not None:
                value = str(commit_mode).strip().lower()
                if value not in (
                    CommitMode.MANUAL.value,
                    CommitMode.LIMITED.value,
                    CommitMode.AUTO.value,
                ):
                    raise ValueError(
                        "commit_mode 必须是 manual / limited / auto")
                self.commit_mode = value
            if commit_output_repo is not None:
                self.commit_output_repo = str(
                    commit_output_repo or "").strip()
            if commit_base_ref is not None:
                value = str(commit_base_ref or "main").strip() or "main"
                if ".." in value or value.startswith("-"):
                    raise ValueError("commit_base_ref 不合法")
                self.commit_base_ref = value
            if commit_auto_merge is not None:
                self.commit_auto_merge = bool(commit_auto_merge)
            if commit_max_file_bytes is not None:
                value = int(commit_max_file_bytes)
                if value <= 0:
                    raise ValueError("commit_max_file_bytes 必须 > 0")
                self.commit_max_file_bytes = value
            if commit_max_total_bytes is not None:
                value = int(commit_max_total_bytes)
                if value <= 0:
                    raise ValueError("commit_max_total_bytes 必须 > 0")
                self.commit_max_total_bytes = value
            if timezone is not None:
                self.timezone = normalize_timezone(timezone)
            self._persist_settings()
            self._sync_inbox_services()
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
            "archived_agents": sorted(self.archived_agents),
            "prompt_log_enabled": bool(self.prompt_log_enabled),
            "agent_thinking": dict(self.agent_thinking),
            "pi_timeout_enabled": bool(self.pi_timeout_enabled),
            "pi_timeout_seconds": float(self.pi_timeout_seconds),
            "work_mode": str(self.work_mode),
            "front_mode": str(self.front_mode),
            "commit_mode": str(self.commit_mode),
            "commit_output_repo": str(self.commit_output_repo),
            "commit_base_ref": str(self.commit_base_ref),
            "commit_auto_merge": bool(self.commit_auto_merge),
            "commit_max_file_bytes": int(self.commit_max_file_bytes),
            "commit_max_total_bytes": int(self.commit_max_total_bytes),
            "timezone": str(self.timezone),
            "activity_seen": dict(self._activity_seen),
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

    def _commit_settings(self) -> Dict[str, Any]:
        return {
            "output_repo": self.commit_output_repo,
            "base_ref": self.commit_base_ref,
            "auto_merge": self.commit_auto_merge,
            "max_file_bytes": self.commit_max_file_bytes,
            "max_total_bytes": self.commit_max_total_bytes,
        }

    def _build_commit_service(self) -> Optional[OutputCommitService]:
        """组合根注入真实 DB 时才启用；轻量测试可以不注入。"""
        db = getattr(getattr(self.rt, "economy", None), "db", None)
        if db is None:
            return None
        return OutputCommitService(
            task_directories=self.task_directories,
            store=CommitStore(db),
            settings=self._commit_settings,
        )

    def _task_directory(self, task: Any) -> Any:
        """按 Task 的 acceptance 选择 Task Workspace 或用户项目目录。"""
        acceptance = getattr(task, "acceptance", None) or {}
        if not isinstance(acceptance, dict):
            acceptance = {}
        if str(acceptance.get("workspace_mode") or "") == "project":
            project_path = str(acceptance.get("project_path") or "").strip()
            if project_path:
                return self.task_directories.for_task(
                    task.task_id, workdir=Path(project_path))
        return self.task_directories.for_task(task.task_id)

    def _open_project_task(self, path: str) -> Optional[Any]:
        target = str(Path(path).expanduser().resolve())
        try:
            tasks = self.rt.tasks.list_tasks(limit=200)
        except Exception:                                     # noqa: BLE001
            return None
        for task in tasks:
            if not getattr(task, "is_open", False):
                continue
            # 只有真正有运行中 Job 的 Task 才占用项目。
            # 旧的中断/等待任务不应永久阻塞同一项目的新任务；
            # 真正并发执行由 ProjectLockManager 再挡一次。
            if not self._is_live(task.task_id):
                continue
            acceptance = getattr(task, "acceptance", None) or {}
            if not isinstance(acceptance, dict):
                continue
            if str(acceptance.get("workspace_mode") or "") != "project":
                continue
            project = str(acceptance.get("project_path") or "").strip()
            if project and str(Path(project).expanduser().resolve()) == target:
                return task
        return None

    def workdir_info(self, path: str = "") -> Dict[str, Any]:
        raw = str(path or "").strip()
        if not raw:
            return {
                "path": "", "exists": False, "is_dir": False,
                "error": "",
            }
        target = Path(raw).expanduser().resolve()
        return {
            "path": str(target),
            "exists": target.exists(),
            "is_dir": target.is_dir(),
            "error": (
                "" if target.is_dir()
                else ("目录不存在" if not target.exists() else "不是目录")
            ),
        }

    def _validate_workdir(self, path: str) -> str:
        raw = str(path or "").strip()
        if not raw:
            return ""
        if not Path(raw).is_absolute():
            raise ValueError("工作目录需要绝对路径")
        target = Path(raw).expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"工作目录不存在或不是目录：{target}")
        if target == self.task_directories.root or (
            self.task_directories.root in target.parents
        ):
            raise ValueError("工作目录不能位于 AIEOS 的 tasks/ 目录内")
        if target == self.task_directories.root.parent:
            raise ValueError("工作目录不能是 AIEOS 主仓库根目录")
        return str(target)

    def commit_repo_info(self, path: str = "") -> Dict[str, Any]:
        """给 UI 的仓库事实：默认仓库 / 用户输入路径。"""
        if self.commit_service is None:
            return {
                "path": "", "exists": False, "is_dir": False,
                "is_git_repo": False, "git_root": "", "repo_mode": "",
                "error": "提交功能未启用",
            }
        return self.commit_service.inspect_repo(
            str(path or self.commit_output_repo))

    def _validate_commit_repo(self, path: str) -> str:
        target = str(path or "").strip()
        if not target:
            return ""
        if not Path(target).is_absolute():
            raise ValueError("输出仓库需要绝对路径")
        info = self.commit_repo_info(target)
        if not info.get("is_git_repo"):
            raise ValueError(
                f"目标目录不是 Git 仓库：{info.get('error') or target}")
        resolved = str(info.get("git_root") or info.get("path") or target)
        if self.commit_service is not None and Path(resolved).resolve() == (
            self.commit_service.project_root
        ):
            raise ValueError("输出仓库不能是 AIEOS 主仓库")
        return resolved

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

    def _agent_name(self, agent_id: str) -> str:
        name = resolve_agent_name(self.rt, agent_id)
        return name or str(agent_id)

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
            archived = aid in self.archived_agents
            facts.append({
                "agent_id": aid,
                "name": self._agent_name(aid),
                "enabled": (
                    aid not in self.disabled_agents
                    and not archived
                ),
                "archived": archived,
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

    # ------------------------------------------------------------
    # Agent Inbox V1：只读观测（Phase 1 不启动消费者）
    # ------------------------------------------------------------
    def _inbox_recorder(self) -> Any:
        communication = getattr(self.rt, "communication", None)
        return getattr(communication, "recorder", None)

    @staticmethod
    def _inbox_payload(row: Dict[str, Any]) -> Any:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except (TypeError, ValueError):
                return payload
        return payload

    def inbox_stats(self) -> Dict[str, Any]:
        recorder = self._inbox_recorder()
        resolved = getattr(self.rt, "resolved_config", None)
        enabled = bool(getattr(resolved, "inbox_enabled", False))
        agents: Dict[str, Any] = {}
        if recorder is not None:
            for agent_id in sorted(self._agent_ids):
                try:
                    agents[agent_id] = recorder.inbox_stats(agent_id)
                except Exception as exc:                      # noqa: BLE001
                    agents[agent_id] = {
                        "agent_id": agent_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
        return {"enabled": enabled, "agents": agents}

    def inbox_pending(
        self, agent_id: str, *, limit: int = 50,
    ) -> Dict[str, Any]:
        key = str(agent_id or "").strip()
        if key not in self._agent_ids:
            raise ValueError(f"未知 Agent：{key!r}")
        recorder = self._inbox_recorder()
        if recorder is None:
            return {
                "agent_id": key,
                "stats": {},
                "messages": [],
            }
        rows = recorder.list_pending(key, limit=max(1, min(200, int(limit))))
        messages = [
            {
                "message_id": row.get("message_id"),
                "from_agent": row.get("from_agent"),
                "to_agent": row.get("to_agent"),
                "message_type": row.get("message_type"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "processed_once": bool(int(row.get("processed_once") or 0)),
                "processed_at": row.get("processed_at"),
                "error_code": row.get("error_code"),
                "error_message": row.get("error_message"),
                "payload": self._inbox_payload(row),
            }
            for row in rows
        ]
        return {
            "agent_id": key,
            "stats": recorder.inbox_stats(key),
            "messages": messages,
        }

    def activities(self, agent_id: str, *, limit: int = 50) -> Dict[str, Any]:
        key = str(agent_id or "").strip()
        if key not in self._agent_ids:
            raise ValueError(f"未知 Agent：{key!r}")
        service = self.alarm_service
        items = service.recent_runs(key, limit=limit) if service else []
        seen = self._activity_seen.get(key, "")
        unread = service.count_runs(key, since=seen) if service else 0
        for item in items:
            started = str(item.get("started_at") or "")
            item["unread"] = (not seen) or started > seen
        return {
            "agent_id": key,
            "seen_at": seen,
            "unread": int(unread),
            "activities": items,
        }

    def agent_memory(self, agent_id: str) -> Dict[str, Any]:
        """Agent 的短期 / 长期记忆快照（只读）。"""
        key = str(agent_id or "").strip()
        if key not in self._agent_ids:
            raise ValueError(f"未知 Agent：{key!r}")
        short_service = getattr(
            self.rt, "private_short_term_memory", None)
        long_service = getattr(
            self.rt, "private_long_term_memory", None)
        memory = short_service.read(key) if short_service else None
        entries = [entry.to_dict() for entry in memory.entries] if memory else []
        long_entries = (
            [entry.to_dict() for entry in long_service.all(key)]
            if long_service else [])
        return {
            "agent_id": key,
            "short": {
                "revision": int(memory.revision) if memory else 0,
                "entries": entries,
                "needs_compaction": bool(memory.needs_compaction)
                if memory else False,
            },
            "long": long_entries,
        }

    def delete_agent_memory(
        self, agent_id: str, *, kind: str, entry_id: str,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """删除一条短期 / 长期记忆。"""
        key = str(agent_id or "").strip()
        if key not in self._agent_ids:
            raise ValueError(f"未知 Agent：{key!r}")
        target = str(entry_id or "").strip()
        if not target:
            raise ValueError("缺少 entry_id")
        if kind == "short":
            service = getattr(self.rt, "private_short_term_memory", None)
            if service is None:
                raise ValueError("短期记忆服务未启用")
            memory = service.read(key)
            revision = (
                int(expected_revision)
                if expected_revision is not None
                else int(memory.revision))
            service.update(
                key, expected_revision=revision,
                entry_id=target, remove=True)
        elif kind == "long":
            service = getattr(self.rt, "private_long_term_memory", None)
            if service is None:
                raise ValueError("长期记忆服务未启用")
            if not service.delete(key, target):
                raise ValueError("长期记忆不存在")
        else:
            raise ValueError("kind 必须是 short / long")
        return self.agent_memory(key)

    def clear_agent_memory(self, agent_id: str) -> Dict[str, Any]:
        """清空该 Agent 的短期 + 归档 + 长期记忆；不可恢复。"""
        key = str(agent_id or "").strip()
        if key not in self._agent_ids:
            raise ValueError(f"未知 Agent：{key!r}")
        short_service = getattr(
            self.rt, "private_short_term_memory", None)
        long_service = getattr(
            self.rt, "private_long_term_memory", None)
        deleted: Dict[str, int] = {
            "short_term": 0, "archived": 0, "long_term": 0}
        if short_service is not None:
            deleted.update(short_service.clear(key))
        if long_service is not None:
            deleted["long_term"] = int(long_service.clear(key))
        return {
            "agent_id": key,
            "deleted": deleted,
            "memory": self.agent_memory(key),
        }

    def activity_detail(self, run_id: str) -> Dict[str, Any]:
        """单条 Activity 详情：打开时取一次，不参与轮询。"""
        key = str(run_id or "").strip()
        service = self.alarm_service
        run = service.run_detail(key) if (service and key) else None
        if run is None or str(run.get("agent_id") or "") != self.front_agent:
            # 不区分「不存在」和「不属于当前 Front」，避免泄露其他 Agent。
            raise ValueError("活动记录不存在")
        return {"ok": True, "activity": run}

    def mark_activities_seen(
        self, agent_id: str, *, run_id: str = "",
    ) -> Dict[str, Any]:
        """把未读水位推进到 `run_id` 的 started_at；不传则推进到现在。"""
        key = str(agent_id or "").strip()
        if key not in self._agent_ids:
            raise ValueError(f"未知 Agent：{key!r}")
        seen = _now()
        target = str(run_id or "").strip()
        if target:
            service = self.alarm_service
            run = service.run_detail(target) if service else None
            if run is None or str(run.get("agent_id") or "") != key:
                raise ValueError("活动记录不存在")
            seen = str(run.get("started_at") or "") or seen
            current = self._activity_seen.get(key, "")
            if current and current >= seen:
                seen = current
        with self._lock:
            self._activity_seen[key] = seen
        self._persist_settings()
        service = self.alarm_service
        unread = service.count_runs(key, since=seen) if service else 0
        return {"agent_id": key, "seen_at": seen, "unread": int(unread)}

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
    def submit(
        self,
        human_text: str,
        *,
        commit_repo: str = "",
        workdir: str = "",
        client_message_id: str = "",
    ) -> str:
        text = str(human_text or "").strip()
        if not text:
            raise ValueError("输入不能为空")
        # 工作目录在这里只保存；路径校验和「同项目只有一个任务」放到
        # 真正创建 Task 的 handoff 阶段。否则像“提交”这样的控制消息
        # 会因为当前选区里还有未完成项目而被 400 拒绝。
        target_workdir = str(workdir or "").strip()
        target_repo = str(commit_repo or "").strip()
        if target_repo:
            target_repo = self._validate_commit_repo(target_repo)
        client_id = str(client_message_id or "").strip()[:128]

        with self._submit_lock:
            if client_id:
                existing = self._client_submissions.get(client_id)
                if existing is not None:
                    if (
                        existing.get("text") != text
                        or existing.get("workdir") != target_workdir
                        or existing.get("commit_repo") != target_repo
                    ):
                        raise ValueError(
                            "同一个 client_message_id 不能对应不同的提交内容")
                    return existing["job_id"]

            job = WorkJob(
                job_id=f"job_{uuid.uuid4().hex[:10]}",
                human_text=text,
                workdir=target_workdir,
                commit_repo=target_repo,
                client_message_id=client_id,
                created_at=_now(),
                updated_at=_now(),
            )
            # 先登记 Job，再发消息：Front Inbox Handler 可能立刻消费，
            # 必须能按 payload.job_id 找到它。
            with self._lock:
                self._jobs[job.job_id] = job
                self._job_order.append(job.job_id)

            conversation_id = ""
            human_message_id = ""
            if self.human_gateway is not None:
                try:
                    conversation_id = self.human_gateway.conversation_id(
                        self.front_agent)
                    job.conversation_id = conversation_id
                    human_message_id = self.human_gateway.send_human_message(
                        text,
                        to_agent=self.front_agent,
                        job_id=job.job_id,
                        conversation_id=conversation_id,
                        client_message_id=client_id,
                    )
                except Exception as exc:                      # noqa: BLE001
                    logger.warning("Human Gateway 写入失败：%s", exc)
            job.conversation_id = conversation_id
            job.human_message_id = human_message_id
            with self._lock:
                self._message(
                    "human", text, job_id=job.job_id,
                    message_id=human_message_id)
            if client_id:
                self._client_submissions[client_id] = {
                    "job_id": job.job_id,
                    "text": text,
                    "workdir": target_workdir,
                    "commit_repo": target_repo,
                }

            # Phase 3：Front Inbox 消费到 human_message 后派发 Job。
            # 没有 Front consumer / Gateway 失败时保留直连回退，
            # 避免测试、CLI 和老部署中的 Job 卡死。
            if (self._front_inbox_consumer is not None
                    and self._front_inbox_consumer.running
                    and human_message_id):
                return job.job_id
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
        leader = str(getattr(job, "assigned_worker", "") or "")
        members: List[str] = []
        admitted = False
        prompt_log.bind_job(job.job_id)
        try:
            if not self._admit_job(job):
                return
            admitted = True
            # 批量派发的子 Job：Front 已在本轮完成决策，直接执行 handoff。
            precomputed = getattr(job, "precomputed_decision", None)
            if precomputed is not None:
                self._set_job(job, JobStatus.RUNNING)
                if precomputed.intent in (
                        FrontIntent.REWORK, FrontIntent.FOLLOW_UP):
                    self._resume_or_rework(job, precomputed, None)
                else:
                    self._run_handoff_solo(job, precomputed, None)
                return
            self._set_job(job, JobStatus.FRONT_ANALYZING)
            front = self._front_runtime(
                self._front_project_root(job),
                llm_session_scope=front_scope(
                    job.conversation_id or job.job_id,
                    agent_id=self.front_agent),
            )
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
                        "attempts": list(
                            getattr(front, "last_attempts", []) or []),
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
            front_data["attempts"] = list(
                getattr(turn, "attempts", []) or [])
            job.front_decision = front_data
            job.front_actions = list(
                getattr(turn, "action_history", None)
                or getattr(front, "last_action_history", [])
                or [])
            self._set_job(job, job.status)

            # 控制类动作可以先执行多个；执行类动作：
            #   · 一个 worker 一回合最多一个；
            #   · 多个 handoff 走批量派发（每个 worker 一个）；
            #   · rework / follow_up 仍然最多一个。
            for item in control:
                self._apply_control_decision(job, item)
            if len(work) > 1:
                self._dispatch_batch(job, work)
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

    def _front_project_root(self, job: WorkJob) -> str:
        """Front 只读项目能力绑定的目录；无效时返回空字符串。"""
        raw = str(getattr(job, "workdir", "") or "").strip()
        if not raw:
            return ""
        try:
            return self._validate_workdir(raw)
        except ValueError:
            return ""

    def _front_memory_facts(self) -> str:
        """Front 的跨对话记忆：短期 + 长期 top N。

        🔒 只读注入；写入由 Front 自己的记忆动作完成。
        """
        agent_id = self.front_agent
        parts: List[str] = []
        short_service = getattr(
            self.rt, "private_short_term_memory", None)
        if short_service is not None:
            try:
                memory = short_service.read(agent_id)
                if memory.entries:
                    parts.append(
                        "短期记忆（最近的经验）：\n"
                        + short_service.render(agent_id))
            except Exception:                                 # noqa: BLE001
                pass
        long_service = getattr(
            self.rt, "private_long_term_memory", None)
        if long_service is not None:
            try:
                if long_service.top(agent_id, limit=5):
                    parts.append(
                        "长期记忆（top 5）：\n"
                        + long_service.render_top(agent_id, limit=5))
            except Exception:                                 # noqa: BLE001
                pass
        return "\n\n".join(parts)

    def _front_runtime(
        self, project_root: str = "", *, llm_session_scope: str = "",
    ) -> Optional[FrontAgentRuntime]:
        agent = self._agent(self.front_agent)
        if agent is None or not getattr(agent, "has_brain", False):
            return None

        dispatcher = build_front_dispatcher(
            workspace_of=self._workspace_of_agent,
            drafts=self._front_drafts(),
            artifact_read_fn=self._front_artifact_read,
            web_search_fn=lambda query, **kwargs: self.web_search(
                self.front_agent, query, **kwargs),
            web_fetch_fn=lambda url: self.web_fetch(
                self.front_agent, url),
            project_root=project_root,
            alarm_service=self.alarm_service,
            short_term_memory=getattr(
                self.rt, "private_short_term_memory", None),
            long_term_memory=getattr(
                self.rt, "private_long_term_memory", None),
        )
        system_prompt = FRONT_SYSTEM_PROMPT
        if "short_term_memory_add" in dispatcher.action_types:
            system_prompt += (
                " 当用户要求你记住偏好、约定或结论时，"
                "必须先调用 short_term_memory_add 写入记忆，再回复；"
                "只回复「记住了」不算记住。")

        def _front_think(prompt: str) -> str:
            # 🔒 Front 不注入 Agent 的身份 / 经济 / Constitution，
            #    也不看自我模型。它的判断材料已经在 prompt 里。
            return agent.think(
                prompt,
                cognitive=False,
                system=system_prompt,
                call_purpose="front",
                llm_session_scope=llm_session_scope,
            )

        def _execute(actions: List[Dict[str, Any]]) -> str:
            return execute_front_actions(
                dispatcher, actions, agent_id=self.front_agent)

        return FrontAgentRuntime(
            self.front_agent, _front_think,
            agent_name=self._agent_name(self.front_agent),
            inspect_fn=self._front_inspect_facts,
            roles=self.worker_roles.catalog(),
            execute_actions=_execute,
            action_types=dispatcher.action_types,
            front_mode=self.front_mode,
            timezone=self.timezone)

    def _pi_enabled(self, agent_id: str) -> bool:
        return str(agent_id or "") in self.pi_work_agents

    def _stop_front_inbox(self) -> None:
        consumer = self._front_inbox_consumer
        if consumer is None:
            return
        agent_id = consumer.agent_id
        consumer.stop()
        self._front_inbox_consumer = None
        recorder = self._inbox_recorder()
        if recorder is not None:
            try:
                recorder.set_inbox_enabled(agent_id, False)
            except Exception:                                 # noqa: BLE001
                logger.warning(
                    "Front inbox state 关闭失败：%s", agent_id, exc_info=True)

    def _inbox_policy_for(self, agent_id: str) -> InboxPolicy:
        """从 Agent 原始配置读取 inbox 策略；缺省永不自动回复。"""
        resolved = getattr(self.rt, "resolved_config", None)
        if resolved is None:
            return InboxPolicy()
        try:
            raw = getattr(resolved.agent(agent_id), "raw", None) or {}
        except Exception:                                     # noqa: BLE001
            return InboxPolicy()
        block = raw.get("inbox") if isinstance(raw, Mapping) else None
        if not isinstance(block, Mapping):
            return InboxPolicy()
        try:
            return InboxPolicy(
                reply_mode=str(block.get("reply_mode") or "never"),
                max_auto_replies_per_conversation=int(
                    block.get("max_auto_replies_per_conversation", 3)),
                max_cost_per_message=float(
                    block.get("max_cost_per_message", 0.0)),
            )
        except (TypeError, ValueError):
            logger.warning(
                "Agent inbox 策略不合法，回退 never：%s", agent_id,
                exc_info=True)
            return InboxPolicy()

    def _sync_inbox_services(self) -> None:
        """按当前设置启停 Pi 常驻 Inbox 与 Front Inbox。"""
        resolved = getattr(self.rt, "resolved_config", None)
        enabled = bool(getattr(resolved, "inbox_enabled", False))
        communication = getattr(self.rt, "communication", None)

        # ---- Pi Agent 持久 Inbox ----
        if not enabled or communication is None:
            if self._pi_inbox_service is not None:
                self._pi_inbox_service.stop_all()
                self._pi_inbox_service = None
        else:
            if self._pi_inbox_service is None:
                self._pi_inbox_service = PiInboxService(communication)
            wanted = sorted(
                aid for aid in self.pi_work_agents
                if aid in self._agent_ids
                and aid != self.front_agent
                and aid not in self.disabled_agents
                and aid not in self.archived_agents
            )
            self._pi_inbox_service.sync(wanted)

        # ---- Front Inbox（Phase 3）----
        if (not enabled or communication is None
                or self.human_gateway is None):
            self._stop_front_inbox()
        else:
            current = self._front_inbox_consumer
            if current is not None and (
                not current.running or current.agent_id != self.front_agent
            ):
                self._stop_front_inbox()
            if self._front_inbox_consumer is None:
                consumer = InboxConsumer(
                    communication,
                    agent_id=self.front_agent,
                    handler=self._on_front_inbox_message,
                    name="front-inbox",
                )
                if consumer.start():
                    self._front_inbox_consumer = consumer
                    recorder = self._inbox_recorder()
                    if recorder is not None:
                        try:
                            recorder.set_inbox_enabled(
                                self.front_agent, True)
                        except Exception:                 # noqa: BLE001
                            logger.warning(
                                "Front inbox state 开启失败：%s",
                                self.front_agent, exc_info=True)

        # ---- Solo Inbox（Phase 4）----
        # Solo 只入队 / 观测，不启动消费者，也不自动调用 Brain。
        if communication is None:
            return
        recorder = self._inbox_recorder()
        if recorder is None:
            return
        for agent_id in sorted(self._agent_ids):
            if agent_id == self.front_agent:
                continue
            if agent_id in self.pi_work_agents:
                continue
            desired = (
                enabled
                and agent_id not in self.disabled_agents
                and agent_id not in self.archived_agents
            )
            try:
                state = recorder.get_inbox_state(agent_id)
                if state is None or bool(state.get("enabled")) != desired:
                    recorder.set_inbox_enabled(agent_id, desired)
            except Exception:                             # noqa: BLE001
                logger.warning(
                    "Solo inbox state 更新失败：%s", agent_id,
                    exc_info=True)

        # ---- Life 层自动回复（Phase 5，默认 never）----
        if not enabled or communication is None:
            if self._life_inbox_service is not None:
                self._life_inbox_service.stop()
                self._life_inbox_service = None
            return
        auto_policies: Dict[str, InboxPolicy] = {}
        for agent_id in sorted(self._agent_ids):
            if agent_id == self.front_agent:
                continue
            if agent_id in self.pi_work_agents:
                # Pi 的持久 Inbox 已由 PiInboxService 消费；
                # Life 自动回复 V1 暂不与它叠加。
                continue
            if (agent_id in self.disabled_agents
                    or agent_id in self.archived_agents):
                continue
            policy = self._inbox_policy_for(agent_id)
            if policy.reply_mode == "auto":
                auto_policies[agent_id] = policy
        if auto_policies:
            if self._life_inbox_service is None:
                self._life_inbox_service = LifeInboxService(
                    self.rt, self.availability)
            self._life_inbox_service.sync(auto_policies)
        elif self._life_inbox_service is not None:
            self._life_inbox_service.stop()
            self._life_inbox_service = None

    def _on_front_inbox_message(self, message: Any) -> None:
        """Front Inbox → 把 human_message 对应的 Job 交给执行器。"""
        payload = getattr(message, "payload", None) or {}
        if not isinstance(payload, dict):
            return
        if str(payload.get("kind") or "") != "human_message":
            return
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                logger.warning("Front Inbox：找不到 Job %s", job_id)
                return
            if job.status != JobStatus.RECEIVED:
                # 重复消息 / 已经派发：不重复执行。
                return
            job.status = JobStatus.QUEUED
            self._touch(job)
        try:
            self._executor.submit(self._run_job, job)
        except Exception as exc:                              # noqa: BLE001
            self._fail(
                job, f"Front Inbox 派发 Job 失败：{type(exc).__name__}: {exc}")

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
            task_directory=self._task_directory(task),
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
            inbox_bridge=(
                self._pi_inbox_service.bridge_for(
                    task.contractor or task.creator)
                if self._pi_inbox_service is not None else None
            ),
            role_prompt=role_prompt,
            role=role,
            thinking=self._thinking_for_agent(
                task.contractor or task.creator),
            work_mode=str(work_mode or "workflow"),
            project_lock_manager=self.project_locks,
        )

    def _front_turn(
        self,
        front: Any,
        job: WorkJob,
        active: List[Dict[str, Any]],
        completed: List[Dict[str, Any]],
        *,
        wakeup: Optional[Dict[str, Any]] = None,
    ) -> FrontTurn:
        """优先走多动作接口；旧 Front 实现仍按单动作兼容。"""
        method = getattr(front, "decide_turn", None)
        commit_context = ""
        if self.commit_service is not None:
            try:
                commit_context = self.commit_service.front_context(
                    mode=self.commit_mode)
            except Exception:                                 # noqa: BLE001
                commit_context = ""
        context = (
            f"当前 Front 模式：{self.front_mode}\n"
            f"当前 Work 模式：{self.work_mode}\n"
            "可用 set_mode 修改；修改后对新任务生效。")
        if commit_context:
            context += "\n" + commit_context
        project_context = "当前没有选择项目目录。"
        workdir = str(getattr(job, "workdir", "") or "").strip()
        if workdir:
            try:
                project_context = render_project_context(
                    self._validate_workdir(workdir))
            except ValueError as exc:
                project_context = f"当前项目目录不可用：{exc}"
        kwargs = {
            "active_tasks": active,
            "completed_tasks": completed,
            "conversation": self._conversation(job),
            "agents": self._agent_facts(),
            "context": context,
            "project_context": project_context,
            "memory_facts": self._front_memory_facts(),
        }
        if wakeup is not None:
            kwargs["wakeup"] = wakeup
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
        if intent == FrontIntent.NO_NOTIFY:
            if getattr(job, "source", "") != "alarm":
                raise ValueError("no_notify 只允许用于闹钟唤醒")
            job.status = JobStatus.NO_NOTIFY
            job.summary = str(getattr(decision, "reason", "") or "no_notify")
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
        if intent == FrontIntent.INTERRUPT:
            self._interrupt_work(job, decision)
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
        if intent == FrontIntent.COMMIT_CONFIRM:
            self._apply_commit_confirm(job, decision)
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

    def _apply_commit_confirm(self, job: WorkJob, decision: Any) -> None:
        """人类确认提交；Front 只转达，不执行 git。"""
        task_id = str(getattr(decision, "task_id", "") or "").strip()
        code = str(getattr(decision, "commit_code", "") or "").strip()
        if self.commit_service is None:
            self._reply_system(
                job, "当前没有配置输出仓库，不能提交。", task_id)
            return
        if not task_id:
            self._reply_system(job, "缺少要提交的 Task。")
            return
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            self._reply_system(
                job, f"找不到任务 {task_id}。", task_id)
            return
        try:
            request = self.commit_service.confirm(
                task, code, actor="human")
        except CommitError as exc:
            self._reply_system(job, str(exc), task_id)
            return
        if request.status == CommitStatus.COMMITTED.value:
            text = self._format_commit_text(
                request,
                heading=(
                    f"任务 {request.task_id} 已提交到输出仓库的任务分支。"))
        elif request.status in (
            CommitStatus.FAILED.value, CommitStatus.BLOCKED.value,
            CommitStatus.STALE.value,
        ):
            text = f"提交未完成：{request.error or request.status}"
        else:
            text = f"提交状态：{request.status}"
        self._reply_system(job, text, task_id)

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
                "name": self._agent_name(aid),
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
            try:
                task_obj = self.rt.tasks.get(task_id)
                task_dir = self._task_directory(task_obj)
            except Exception:                                 # noqa: BLE001
                task_dir = self.task_directories.for_task(task_id)
            if task_dir is not None and task_dir.exists():
                workdir = getattr(task_dir, "workdir", None)
                if workdir is None:
                    workdir = task_dir.workspace
                task["workspace_path"] = str(workdir)
                task["workspace_mode"] = (
                    "project"
                    if getattr(task_dir, "project_mode", False) else "task")
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

    def _batch_eligible_workers(self) -> List[str]:
        return [
            aid for aid in self._agent_ids
            if aid != self.front_agent
            and aid not in self.disabled_agents
            and aid not in self.archived_agents
            and self._agent_has_brain(aid)
        ]

    def _dispatch_batch(self, job: WorkJob, decisions: List[Any]) -> None:
        """一次 Front 回合派发多个 handoff；每个 Worker 只接一个。

        🔒 不创建“多 Task Job”。父 Job 只负责派发，子 Job 各走现有
           `_run_handoff_solo()`，Task Core 仍然是每个 Task 的权威。
        """
        if len(decisions) > MAX_BATCH_TASKS:
            self._fail(
                job, f"一次最多派发 {MAX_BATCH_TASKS} 个任务")
            return
        if any(item.intent != FrontIntent.HANDOFF for item in decisions):
            self._fail(
                job, "批量派发只支持 handoff；"
                "rework / follow_up 一次只能一个")
            return
        if not self.solo_only:
            self._fail(job, "批量 handoff 仅在 Solo 模式可用")
            return
        if str(getattr(job, "workdir", "") or "").strip():
            self._fail(
                job,
                "同一项目目录一次只能运行一个任务；"
                "请拆成多轮，或使用 Task Workspace")
            return

        eligible = self._batch_eligible_workers()
        if not eligible:
            self._fail(job, "没有可用的 Worker")
            return

        # 先校验所有 decision，再占用 Worker；避免部分派发。
        preferred: Dict[int, str] = {}
        for index, item in enumerate(decisions):
            role = self.worker_roles.resolve(getattr(item, "role", ""))
            if role is None:
                self._fail(
                    job,
                    f"Front 选择了未知 Worker 角色："
                    f"{getattr(item, 'role', '')!r}")
                return
            item.role = role.role_id
            profile = str(
                getattr(item, "work_profile", "") or "standard").strip()
            if profile not in ("direct", "standard", "creative_product"):
                self._fail(job, f"未知 work_profile：{profile!r}")
                return
            item.work_profile = profile

            agent = str(
                getattr(item, "preferred_agent", "") or "").strip()
            if not agent:
                continue
            if agent not in eligible:
                self._fail(
                    job, f"preferred_agent_unavailable: {agent}")
                return
            if agent in preferred.values():
                self._fail(
                    job,
                    f"preferred_agent_duplicate: {agent}；"
                    "同一 Worker 一回合只能接一个执行动作")
                return
            preferred[index] = agent

        assigned: List[str] = [""] * len(decisions)
        acquired: List[str] = []
        explicit = set(preferred.values())

        def abort(message: str) -> None:
            for agent_id in acquired:
                self.availability.release(agent_id)
            self._fail(job, message)

        # 1) 显式指定的 Worker 先占位；忙则整批拒绝。
        for index, agent_id in preferred.items():
            if not self.availability.try_acquire(agent_id):
                abort(f"preferred_agent_busy: {agent_id}")
                return
            assigned[index] = agent_id
            acquired.append(agent_id)

        # 2) 没有显式指定的 handoff：分配不同的空闲 Worker。
        auto_candidates = [
            aid for aid in sorted(eligible) if aid not in explicit
        ]
        for index, _item in enumerate(decisions):
            if assigned[index]:
                continue
            chosen = ""
            for agent_id in auto_candidates:
                if agent_id in acquired:
                    continue
                if self.availability.try_acquire(agent_id):
                    chosen = agent_id
                    break
            if not chosen:
                abort(
                    "没有足够的空闲 Worker 承接全部任务；"
                    "请减少任务数或先释放 Worker")
                return
            assigned[index] = chosen
            acquired.append(chosen)

        job.status = JobStatus.BATCH_DISPATCHED
        job.batch_id = job.job_id
        job.batch_size = len(decisions)
        job.summary = f"已派发 {len(decisions)} 个任务"
        job.front_intent = "batch"
        lines = []
        for index, (item, agent_id) in enumerate(
                zip(decisions, assigned), 1):
            brief = getattr(item, "brief", None)
            goal = str(
                getattr(brief, "goal", "")
                or getattr(brief, "task_text", "")
                or "任务"
            ).strip()
            lines.append(
                f"{index}. {goal[:80]} → "
                f"{self._agent_name(agent_id)}（Agent ID: {agent_id}）")
        reply = str(
            getattr(decisions[0], "reply", "") or "").strip()
        headline = reply or "好，我把这次需求拆成多个任务："
        self._message(
            "front", headline + "\n\n" + "\n".join(lines),
            job_id=job.job_id)

        for index, (item, agent_id) in enumerate(
                zip(decisions, assigned), 1):
            child = WorkJob(
                job_id=f"{job.job_id}_b{index}",
                human_text=job.human_text,
                status=JobStatus.QUEUED,
                front_intent=FrontIntent.HANDOFF,
                workdir=job.workdir,
                commit_repo=job.commit_repo,
                human_message_id=job.human_message_id,
                conversation_id=job.conversation_id,
                batch_id=job.job_id,
                parent_job_id=job.job_id,
                batch_index=index,
                batch_size=len(decisions),
                assigned_worker=agent_id,
                precomputed_decision=item,
                front_thinking=job.front_thinking,
                source=job.source,
                wakeup_id=job.wakeup_id,
                created_at=_now(),
                updated_at=_now(),
            )
            child.front_decision = item.to_dict()
            with self._lock:
                self._jobs[child.job_id] = child
                self._job_order.append(child.job_id)
            self._executor.submit(self._run_job, child)

        self._touch(job)
        self._notify()

    def _dispatch_alarm_work(
        self, job: WorkJob, decisions: List[Any],
    ) -> Dict[str, Any]:
        """闹钟唤醒派发 handoff / rework / follow_up。

        🔒 只创建子 Job，不在 AlarmService 线程里执行 Task。
        """
        if not decisions:
            return {"ok": True, "dispatched": 0, "children": []}
        allowed = (
            FrontIntent.HANDOFF, FrontIntent.REWORK, FrontIntent.FOLLOW_UP)
        if any(item.intent not in allowed for item in decisions):
            self._fail(job, "alarm_wakeup_bad_work_intent")
            return {"ok": False, "error": job.error, "children": []}
        # relay 模式同样只保留核心 task_text。
        for item in decisions:
            self._apply_front_mode(
                item, front_mode=self._effective_front_mode(decisions))

        if len(decisions) > 1:
            # 机制事实：批量派发只支持 handoff；rework / follow_up
            # 一次只能一个（它们会改动已有 Task）。
            if any(
                item.intent != FrontIntent.HANDOFF
                for item in decisions
            ):
                self._fail(job, "rework / follow_up 一次只能派发一个")
                return {"ok": False, "error": job.error, "children": []}
            self._dispatch_batch(job, decisions)
            if job.status == JobStatus.FAILED:
                return {"ok": False, "error": job.error, "children": []}
            return {
                "ok": True,
                "mode": "batch",
                "dispatched": len(decisions),
                "children": [
                    f"{job.job_id}_b{index}"
                    for index in range(1, len(decisions) + 1)
                ],
            }

        item = decisions[0]
        child_id = f"{job.job_id}_w1"
        child = WorkJob(
            job_id=child_id,
            human_text=job.human_text,
            status=JobStatus.QUEUED,
            front_intent=item.intent,
            workdir=job.workdir,
            commit_repo=job.commit_repo,
            human_message_id=job.human_message_id,
            conversation_id=job.conversation_id,
            parent_job_id=job.job_id,
            batch_index=1,
            batch_size=1,
            precomputed_decision=item,
            front_thinking=job.front_thinking,
            source=job.source,
            wakeup_id=job.wakeup_id,
            created_at=_now(),
            updated_at=_now(),
        )
        child.front_decision = item.to_dict()
        with self._lock:
            self._jobs[child.job_id] = child
            self._job_order.append(child.job_id)
        self._executor.submit(self._run_job, child)
        job.status = JobStatus.DISPATCHED
        job.front_intent = item.intent
        job.summary = f"已派发 1 个任务（{item.intent}）"
        self._touch(job)
        self._notify()
        return {
            "ok": True,
            "mode": "single",
            "dispatched": 1,
            "children": [child.job_id],
        }

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
        assigned = str(
            getattr(job, "assigned_worker", "") or "").strip()
        if assigned:
            if (
                assigned not in self._agent_ids
                or assigned in self.disabled_agents
                or assigned in self.archived_agents
                or assigned == self.front_agent
                or not self._agent_has_brain(assigned)
            ):
                self._fail(
                    job, f"assigned_worker_unavailable: {assigned}")
                return "", []
            # 批量派发时 Worker 已在派发前占位；这里不再重复 acquire。
            worker = assigned
        elif preferred:
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

        if getattr(job, "workdir", "") and not self._pi_enabled(worker):
            self._fail(job, "项目目录模式只支持 Pi Work")
            return worker, []

        handoff = HandoffRuntime(
            getattr(self.rt, "communication", None),
            front_agent=self.front_agent,
            worker=worker,
            job=job,
        )
        created = handoff.negotiate(
            decision,
            lambda: self._create_solo_task(job, worker, decision),
        )
        job.handoff_message_id = str(
            getattr(handoff.offer_message, "message_id", "") or "")
        job.handoff_conversation_id = handoff.conversation_id
        if not created.get("ok"):
            self._fail(job, created.get("message") or "Task 创建失败")
            return worker, []

        task_id = str(created["task_id"])
        job.task_id = task_id
        self._bind_task_to_agents([worker], task_id)
        if decision.reply and not job.parent_job_id:
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
        criteria = [_task_text_from_brief(decision.brief)]
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
        try:
            workdir = self._validate_workdir(
                str(getattr(job, "workdir", "") or ""))
        except ValueError as exc:
            return {
                "ok": False, "code": "invalid_workdir",
                "message": str(exc),
            }
        if workdir:
            busy = self._open_project_task(workdir)
            if busy is not None:
                return {
                    "ok": False, "code": "project_busy",
                    "message": (
                        f"项目还有未完成的任务：{busy.task_id}；"
                        "请先完成或停止它"),
                }
            job.workdir = workdir
        criteria = [_task_text_from_brief(decision.brief)]
        acceptance = {
            "criteria": list(criteria),
            # 🔒 Pi 只看 Task.description；来源分层必须随 Task 持久化。
            "brief": (
                decision.brief.to_dict()
                if getattr(decision, "brief", None) is not None else {}),
            "role": str(getattr(decision, "role", "") or "generalist"),
            "work_profile": str(
                getattr(decision, "work_profile", "") or "standard"),
            "work_mode": str(self.work_mode),
            "front_mode": str(self.front_mode),
            "workspace_mode": (
                "project"
                if getattr(job, "workdir", "") else "task"),
            "project_path": str(
                getattr(job, "workdir", "") or ""),
            "commit_mode": str(self.commit_mode),
            "commit_output_repo": str(
                getattr(job, "commit_repo", "") or ""),
            "commit_output_repo_mode": (
                "project"
                if getattr(job, "commit_repo", "") else "output"),
        }
        try:
            task = Task.create(
                creator=worker,
                description=_description_from_brief(decision.brief),
                participants=[worker],
                acceptance=acceptance,
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
                agent_name=resolve_agent_name(self.rt, leader),
                agent_names={
                    aid: resolve_agent_name(self.rt, aid) for aid in idle
                },
                role=WorkRole.LEAD,
                iteration=1,
                brief=brief.to_dict(),
                acceptance_criteria=[_task_text_from_brief(brief)],
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
        criteria = [_task_text_from_brief(decision.brief)]
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
        brief = getattr(decision, "brief", None)
        brief_dict = (
            brief.to_dict() if brief is not None else {
                "goal": job.human_text,
                "human_text": job.human_text,
                "task_text": job.human_text,
                "front_notes": "",
                "reply_style": "",
            }
        )
        acceptance["role"] = role.role_id
        acceptance["brief"] = dict(brief_dict)
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
        instruction = str(
            brief_dict.get("task_text") or job.human_text).strip()
        if action == "rework":
            if self.commit_service is not None:
                try:
                    self.commit_service.invalidate_task(task.task_id)
                except Exception:                             # noqa: BLE001
                    pass
            turn = int(getattr(task, "iteration", 1) or 1) + 1
            # Reopen 不覆盖旧历史；Task description 只追加本轮**执行说明**。
            # Human 原话 / Front 建议在 acceptance["brief"] 中分层保存。
            task.description = (
                f"{task.description}\n\n返工要求（第 {turn} 轮）：\n"
                f"{instruction}")
            if callable(save_task):
                save_task(task)
            self._message(
                "system", f"原团队开始第 {turn} 轮返工。",
                job_id=job.job_id, task_id=task.task_id)
            reason = instruction
        else:
            if instruction:
                task.description = (
                    f"{task.description}\n\n续跑要求：\n{instruction}")
                if callable(save_task):
                    save_task(task)
            self._message(
                "system",
                f"任务 {task.task_id} 之前中断了，从现有状态继续。",
                job_id=job.job_id, task_id=task.task_id)
            reason = ""

        if task.mode == TaskMode.TEAM:
            runner = TeamWorkRuntime(
                self.rt, task,
                brief=brief_dict,
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
                    brief=brief_dict,
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
        # 🔒 Work 结果不是 Front 的 LLM 回复。
        #    当前没有让 Front 重新处理这条结果，所以不能把它标成 front；
        #    用 system 明确表示「系统结果通知」。
        self._message(
            "system",
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
        if result.ok:
            self._after_task_completed(job, task_id)

    @staticmethod
    def _format_commit_text(
        request: Any, *, heading: str, awaiting: bool = False,
    ) -> str:
        """把 Output Commit 结果写成不含歧义的人类可读文本。

        🔒 明确区分：
            · 输出仓库路径
            · 输出仓库内的任务子树路径
            · 任务分支
            · 是否合并到基线 / 是否检出到仓库工作区
        """
        target = (
            str(getattr(request, "target_ref", "") or "")
            .removeprefix("refs/heads/") or "main")
        task_id = str(getattr(request, "task_id", "") or "")
        lines = [
            heading,
            f"输出仓库：{getattr(request, 'output_repo', '')}",
            f"仓库内路径：tasks/{task_id}/",
            f"任务分支：{getattr(request, 'branch', '')}",
        ]
        if awaiting:
            lines.append(f"快照：{getattr(request, 'code', '')}")
            lines.append("需要提交时直接说“提交”即可。")
            return "\n".join(lines)
        sha = str(getattr(request, "commit_sha", "") or "")
        if sha:
            lines.append(f"提交：{sha[:8]}")
        merge_sha = str(getattr(request, "merge_sha", "") or "")
        if merge_sha:
            lines.append(
                f"已合并到基线：{target}（{merge_sha[:8]}）")
        else:
            lines.append(
                f"未合并到基线 {target}；任务分支也未检出到仓库目录，"
                "所以仓库目录里暂时看不到这些文件。")
        return "\n".join(lines)

    def _after_task_completed(self, job: WorkJob, task_id: str) -> None:
        """Task COMPLETED 之后触发提交请求；失败不回滚 Task。"""
        if self.commit_service is None:
            return
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            return
        if task is None or task.status != TaskStatus.COMPLETED:
            return
        acceptance = getattr(task, "acceptance", None) or {}
        if not isinstance(acceptance, dict):
            acceptance = {}
        # Project 模式已经直接修改用户目录；不再把同一批变更复制到
        # Output Repo。项目分支/提交属于 Project Repo Mode（FUTURE）。
        if str(acceptance.get("workspace_mode") or "") == "project":
            return
        mode = str(
            acceptance.get("commit_mode") or self.commit_mode
        ).strip().lower()
        target_repo = str(
            acceptance.get("commit_output_repo") or "").strip()
        repo_mode = str(
            acceptance.get("commit_output_repo_mode")
            or ("project" if target_repo else "output")
        ).strip().lower()
        try:
            request = self.commit_service.on_task_completed(
                task, mode=mode, output_repo=target_repo,
                repo_mode=repo_mode)
        except Exception as exc:                              # noqa: BLE001
            self._message(
                "system",
                f"任务 {task_id} 的提交请求失败："
                f"{type(exc).__name__}: {exc}",
                job_id=job.job_id, task_id=task_id)
            return
        if request is None:
            return
        if request.status == CommitStatus.AWAITING_HUMAN.value:
            self._message(
                "system",
                self._format_commit_text(
                    request,
                    heading=(
                        f"任务 {task_id} 已完成，结果待提交。"),
                    awaiting=True),
                job_id=job.job_id, task_id=task_id)
        elif request.status == CommitStatus.COMMITTED.value:
            self._message(
                "system",
                self._format_commit_text(
                    request,
                    heading=(
                        f"任务 {task_id} 已提交到输出仓库的任务分支。")),
                job_id=job.job_id, task_id=task_id)
        elif request.status in (
            CommitStatus.FAILED.value, CommitStatus.BLOCKED.value,
            CommitStatus.STALE.value,
        ):
            self._message(
                "system",
                f"任务 {task_id} 提交未完成：{request.error or request.status}",
                job_id=job.job_id, task_id=task_id)

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

    def _reply_system(
        self, job: WorkJob, text: str, task_id: str = "",
    ) -> None:
        """系统动作结果：不是 Front 的 LLM 回复，不能标成 front。"""
        self._message(
            "system", text, job_id=job.job_id, task_id=task_id,
            duration_ms=self._job_duration_ms(job))
        job.status = JobStatus.DIRECT_REPLY
        job.summary = text
        self._touch(job)

    # ------------------------------------------------------------
    # cancel：真正停止执行中的 Task
    # ------------------------------------------------------------
    def _interrupt_work(self, job: WorkJob, decision: Any) -> None:
        """暂停执行中的 Task：Job 停下，Task 保持 IN_PROGRESS。

        与 cancel 的区别：
            cancel    → Task CANCELLED，不能 follow_up
            interrupt → Task 保持 IN_PROGRESS，可以 follow_up 继续
        """
        task_id = str(getattr(decision, "task_id", "") or "").strip()
        try:
            task = self.rt.tasks.get(task_id)
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            self._reply_direct(
                job, "找不到要中断的任务；它可能已经被清理了。", task_id)
            return
        if task.status == TaskStatus.COMPLETED:
            self._reply_direct(
                job, f"任务 {task.task_id} 已经完成了，不需要中断。",
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
            self._reply_direct(
                job,
                f"任务 {task.task_id} 当前没有在运行（{task.status}），"
                "可以直接告诉我继续。",
                task.task_id,
            )
            return

        target.cancelled = True
        target.stop_mode = "interrupt"
        self._set_job(target, JobStatus.CANCELLING)
        self._reply_direct(
            job,
            self._front_reply_text(
                decision,
                f"正在中断任务 {task.task_id}。它会在当前步骤结束后停下，"
                "Task 保持 IN_PROGRESS，之后可以继续。"),
            task.task_id,
        )

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
        target.stop_mode = "cancel"
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
        # Pi Work 的真实目录：直接复制 workspace / 变更文件到 export 根。
        try:
            task_dir = self._task_directory(task)
        except Exception:                                     # noqa: BLE001
            task_dir = None
        if task_dir is not None and task_dir.exists():
            destination = self.export_root / task.task_id
            try:
                if task_dir.project_mode:
                    files = self._project_changed_files(task_dir)
                    for item in files:
                        source = task_dir.workdir / item["path"]
                        target = destination / item["path"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                else:
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
        if job.stop_mode == "interrupt":
            job.status = JobStatus.INTERRUPTED
            job.summary = "任务已中断，可以继续"
            job.error = ""
            self._message(
                "system",
                f"任务 {task_id} 已中断；状态保持 IN_PROGRESS，"
                "可以用 follow_up 继续。",
                job_id=job.job_id, task_id=task_id)
            self._touch(job)
            return
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
                    "name": self._agent_name(aid),
                    "archived": aid in self.archived_agents,
                    "status": state,
                    "role": role,
                    "task_id": task.get("task_id", ""),
                    "has_brain": self._agent_has_brain(aid),
                    "enabled": (
                        aid not in self.disabled_agents
                        and aid not in self.archived_agents
                    ),
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
            messages = self._merged_messages()
            tasks = self._task_cards()
            activity_unread = 0
            if self.alarm_service is not None:
                activity_unread = self.alarm_service.count_runs(
                    self.front_agent,
                    since=self._activity_seen.get(self.front_agent, ""))
            return {
                "front_agent": self.front_agent,
                "settings": self.settings_dict(),
                "agents": agents,
                "tasks": tasks,
                "jobs": jobs,
                "messages": messages,
                "activity_unread": int(activity_unread),
                "running_jobs": sum(
                    1 for job in self._jobs.values()
                    if job.status in (
                        JobStatus.FRONT_ANALYZING,
                        JobStatus.WAITING_FOR_AGENT,
                        JobStatus.WAITING_FOR_LEADER,
                        JobStatus.LEADER_PLANNING,
                        JobStatus.WAITING_FOR_MEMBERS,
                        JobStatus.RUNNING,
                        JobStatus.ALARM_RUNNING,
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
        acceptance = getattr(task, "acceptance", None) or {}
        if not isinstance(acceptance, dict):
            acceptance = {}
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
                "workspace_mode": str(
                    acceptance.get("workspace_mode") or "task"),
                "project_path": str(acceptance.get("project_path") or ""),
                "role": str(acceptance.get("role") or ""),
                "work_profile": str(acceptance.get("work_profile") or ""),
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
                       COALESCE(SUM(cache_read_tokens), 0)
                           AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0)
                           AS cache_write_tokens,
                       COALESCE(SUM(allowance_usd), 0) AS allowance_usd,
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
                    "avg_latency_ms": 0.0, "cost_ai": 0.0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "cache_hit_rate": 0.0, "allowance_usd": 0.0}
        if row is None:
            return {"calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "latency_ms": 0.0,
                    "avg_latency_ms": 0.0, "cost_ai": 0.0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "cache_hit_rate": 0.0, "allowance_usd": 0.0}
        def _value(name: str) -> Any:
            try:
                return row[name]
            except (KeyError, IndexError):
                return 0

        calls = int(row["calls"] or 0)
        latency_ms = round(float(row["latency_ms"] or 0), 1)
        input_tokens = int(row["input_tokens"] or 0)
        cache_read = int(_value("cache_read_tokens") or 0)
        cache_write = int(_value("cache_write_tokens") or 0)
        # 🔒 `input_tokens` 来自 `prompt_tokens`，已经包含 cache_read /
        #    cache_write（DeepSeek 与 OpenCode 实测如此）。
        #    命中率的分母就是它；不能再加一次 cache_read，
        #    否则 98% 会被算成 49%。
        cache_hit_rate = (
            min(1.0, cache_read / input_tokens)
            if input_tokens else 0.0)
        return {
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": int(row["output_tokens"] or 0),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_hit_rate": round(cache_hit_rate, 4),
            "allowance_usd": round(float(_value("allowance_usd") or 0), 6),
            "latency_ms": latency_ms,
            "avg_latency_ms": (
                round(latency_ms / calls, 1) if calls else 0.0),
            "cost_ai": round(float(row["cost_ai"] or 0), 4),
        }

    def _project_changed_files(
        self, task_dir: Any,
    ) -> List[Dict[str, Any]]:
        """project 模式只返回 baseline → final 的变更文件。"""
        try:
            snapshots = TaskSnapshotStore(task_dir)
            records = snapshots.list()
            if len(records) < 2:
                return []
            changed = snapshots.changed_files(
                records[0].seq, records[-1].seq)
        except Exception:                                     # noqa: BLE001
            return []
        files: List[Dict[str, Any]] = []
        for item in changed:
            if str(item.get("status") or "") == "D":
                continue
            path = str(item.get("path") or "")
            if not path:
                continue
            target = task_dir.workdir / path
            try:
                stat = target.stat()
            except OSError:
                continue
            try:
                chars: Optional[int] = len(
                    target.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                chars = None
            files.append({
                "path": path,
                "chars": chars,
                "bytes": stat.st_size,
                "status": item.get("status") or "M",
            })
        return files

    def _workspace_files(self, task: Any) -> List[Dict[str, Any]]:
        # Pi Work 的真实目录优先；旧 Task 继续走逻辑 Workspace。
        try:
            task_dir = self._task_directory(task)
            if task_dir.project_mode:
                return self._project_changed_files(task_dir)
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

    def _load_archived_tasks(self) -> Dict[str, str]:
        if self._archive_path is None or not self._archive_path.is_file():
            return {}
        try:
            raw = json.loads(
                self._archive_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(task_id): str(at or "")
            for task_id, at in raw.items()
            if str(task_id).strip()
        }

    def _save_archived_tasks(self) -> None:
        if self._archive_path is None:
            return
        try:
            self._archive_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._archive_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._archived_tasks, ensure_ascii=False, indent=2),
                encoding="utf-8")
            tmp.replace(self._archive_path)
        except OSError:
            pass

    def archive_task(self, task_id: str) -> Dict[str, Any]:
        key = str(task_id or "").strip()
        task = self.rt.tasks.get(key) if key else None
        if task is None:
            raise ValueError(f"找不到 Task：{task_id}")
        if self._is_live(key):
            raise ValueError("任务正在运行，不能归档；请先停止任务")
        self._archived_tasks[key] = _now()
        self._save_archived_tasks()
        return {"ok": True, "task_id": key, "archived": True}

    def unarchive_task(self, task_id: str) -> Dict[str, Any]:
        key = str(task_id or "").strip()
        if self.rt.tasks.get(key) is None:
            raise ValueError(f"找不到 Task：{task_id}")
        self._archived_tasks.pop(key, None)
        self._save_archived_tasks()
        return {"ok": True, "task_id": key, "archived": False}

    def delete_task(self, task_id: str) -> Dict[str, Any]:
        key = str(task_id or "").strip()
        task = self.rt.tasks.get(key) if key else None
        if task is None:
            raise ValueError(f"找不到 Task：{task_id}")
        if self._is_live(key):
            raise ValueError("任务正在运行，不能删除；请先停止任务")
        terminal = {
            TaskStatus.COMPLETED, TaskStatus.CANCELLED,
            TaskStatus.REJECTED, TaskStatus.EXPIRED, TaskStatus.FAILED,
        }
        if task.status not in terminal:
            raise ValueError(
                f"只有已结束的 Task 可以删除；当前是 {task.status}")
        deleted = self.rt.tasks.delete_task(key)
        try:
            directory = self.task_directories.for_task(key)
            shutil.rmtree(directory.root, ignore_errors=True)
        except (OSError, ValueError):
            pass
        self._archived_tasks.pop(key, None)
        self._save_archived_tasks()
        return {"ok": True, "task_id": key, "deleted": bool(deleted)}

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
                "workspace_mode": str(
                    acceptance.get("workspace_mode") or "task"),
                "project_path": str(
                    acceptance.get("project_path") or ""),
                "updated_at": task.updated_at,
                "archived": task.task_id in self._archived_tasks,
                "live": self._is_live(task.task_id),
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
            # 归档 = 从 Front / UI 的默认上下文隐藏。
            # 否则 Front 会把一个用户在界面上看不到的 Task 当成
            # “可继续”对象（例如已归档的 INTERRUPTED 任务）。
            if task.task_id in self._archived_tasks:
                continue
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
            items = self._merged_messages()
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
        message_id: str = "",
    ) -> None:
        if role == "front" and self.human_gateway is not None:
            mirrored = self._mirror_front_message(
                text, job_id=job_id, task_id=task_id)
            if mirrored:
                message_id = mirrored
        with self._lock:
            self._messages.append({
                "role": role,
                "text": str(text),
                "job_id": job_id,
                "task_id": task_id,
                "message_id": str(message_id or ""),
                "duration_ms": (
                    max(0, int(duration_ms))
                    if duration_ms is not None else None),
                "thinking": (
                    dict(thinking) if isinstance(thinking, dict) else None),
                "retryable": bool(retryable),
                "at": _now(),
            })

    def _mirror_front_message(
        self, text: str, *, job_id: str, task_id: str,
    ) -> str:
        with self._lock:
            job = self._jobs.get(str(job_id)) if job_id else None
            conversation_id = (
                getattr(job, "conversation_id", "") if job else "")
            reply_to = (
                getattr(job, "human_message_id", "") if job else "")
        if not conversation_id:
            conversation_id = self.human_gateway.conversation_id(
                self.front_agent)
        try:
            return self.human_gateway.send_front_reply(
                text,
                front_agent=self.front_agent,
                job_id=job_id,
                task_id=task_id,
                reply_to=reply_to,
                conversation_id=conversation_id,
            )
        except Exception as exc:                              # noqa: BLE001
            logger.warning("Front 回复写入 Human Gateway 失败：%s", exc)
            return ""

    def _journal_messages(self) -> list[Dict[str, Any]]:
        if self.human_gateway is None:
            return []
        try:
            return self.human_gateway.journal_messages(
                front_agent=self.front_agent, limit=200)
        except Exception as exc:                              # noqa: BLE001
            logger.warning("读取 Human Gateway Journal 失败：%s", exc)
            return []

    def _merged_messages(self) -> list[Dict[str, Any]]:
        base = list(self._messages[-100:])
        seen = {
            str(item.get("message_id") or "")
            for item in base
            if item.get("message_id")
        }
        for item in self._journal_messages():
            message_id = str(item.get("message_id") or "")
            if message_id and message_id in seen:
                continue
            base.append(item)
            if message_id:
                seen.add(message_id)
        base.sort(key=lambda item: str(item.get("at") or ""))
        return base[-100:]

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

    def _init_alarms(self) -> None:
        """V1：单进程内启动一个 deadline-driven AlarmScheduler。"""
        if not self.alarms_enabled:
            return
        economy = getattr(self.rt, "economy", None)
        db = getattr(economy, "db", None)
        if db is None:
            logger.warning("AlarmScheduler 未启动：Runtime 没有数据库")
            return
        if self.front_agent not in self._activity_seen:
            # 第一次启用 Activity：历史记录不算「未读」，从这一刻开始计。
            self._activity_seen[self.front_agent] = _now()
            self._persist_settings()
        service = AlarmService(
            AlarmStore(db),
            self._alarm_dispatcher,
            min_interval_seconds=self.alarm_min_interval_seconds,
            max_active_per_agent=self.alarm_max_active_per_agent,
            task_status_fn=self._alarm_task_status,
            guard_max_per_agent=self.alarm_guard_max_per_agent,
            guard_max_total=self.alarm_guard_max_total,
        )
        self._alarm_dispatcher.register(
            self.front_agent,
            FrontWakeupHandler(self.run_front_wakeup),
        )
        self.alarm_service = service
        service.start()

    def _alarm_task_status(self, task_id: str) -> Dict[str, Any]:
        try:
            task = self.rt.tasks.get(str(task_id))
        except Exception:                                     # noqa: BLE001
            task = None
        if task is None:
            return {"task_id": str(task_id), "found": False, "status": ""}
        return {
            "task_id": str(task_id),
            "found": True,
            "status": str(getattr(task, "status", "") or ""),
        }

    @staticmethod
    def _wakeup_calls(turn: Any) -> List[Dict[str, Any]]:
        """一次唤醒里的每次模型输出 —— 原样保存，不做摘要。"""
        receipts: Dict[str, str] = {}
        for item in getattr(turn, "action_history", None) or ():
            if not isinstance(item, dict):
                continue
            raw = str(item.get("raw_output") or "")
            if raw and raw not in receipts:
                receipts[raw] = str(item.get("receipt") or "")
        calls: List[Dict[str, Any]] = []
        for index, attempt in enumerate(
                getattr(turn, "attempts", None) or (), 1):
            if not isinstance(attempt, dict):
                continue
            raw = str(attempt.get("raw_output") or "")
            calls.append({
                "step": int(attempt.get("step") or index),
                "raw_output": raw,
                "decision_error": str(
                    attempt.get("decision_error") or ""),
                "action_error": str(attempt.get("action_error") or ""),
                "decisions": [
                    str(value)
                    for value in (attempt.get("decisions") or ())],
                "actions": [
                    str(value)
                    for value in (attempt.get("actions") or ())],
                "receipt": receipts.get(raw, ""),
            })
        return calls

    def run_front_wakeup(self, event: WakeupEvent) -> WakeupResult:
        """AlarmService 的 Front handler：同步执行一次 Front wake turn。"""
        event_dict = event.to_dict()
        task_ids = list(event.task_ids or [])
        job = WorkJob(
            job_id=f"job_alarm_{uuid.uuid4().hex[:10]}",
            human_text="",
            status=JobStatus.ALARM_RUNNING,
            source="alarm",
            wakeup_id=event.wakeup_id,
            wakeup_event=event_dict,
            task_id=(task_ids[0] if task_ids else ""),
            conversation_id="",
            created_at=_now(),
            updated_at=_now(),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)
        try:
            return self._run_front_wakeup(job)
        finally:
            self._write_job_log(job)
            self._notify()

    def _run_front_wakeup(self, job: WorkJob) -> WakeupResult:
        prompt_log.bind_job(job.job_id)
        try:
            front = self._front_runtime(
                "",
                llm_session_scope=alarm_scope(
                    job.wakeup_id, agent_id=self.front_agent),
            )
            if front is None:
                self._fail(job, "Front Agent 没有可用的 Brain")
                return WakeupResult(
                    outcome="failed", summary="front_unavailable",
                    job_id=job.job_id,
                    detail={"error": "front_unavailable", "calls": []})
            active, completed = self._task_refs()
            with self._front_lock:
                started = time.perf_counter()
                try:
                    turn = self._front_turn(
                        front, job, active, completed,
                        wakeup=job.wakeup_event)
                except FrontUnavailableError as exc:
                    job.front_ms = max(0, int(
                        (time.perf_counter() - started) * 1000))
                    self._fail(job, str(exc))
                    return WakeupResult(
                        outcome="failed", summary=str(exc),
                        job_id=job.job_id,
                        detail={"error": str(exc), "calls": []})
                job.front_ms = max(0, int(
                    (time.perf_counter() - started) * 1000))
                job.front_thinking = self._thinking_for(
                    self.front_agent, "decision")
            calls = self._wakeup_calls(turn)
            decisions = list(turn.decisions or [])
            if not decisions:
                self._fail(job, "Front 闹钟唤醒没有给出任何决定")
                return WakeupResult(
                    outcome="failed", summary="no_decision",
                    job_id=job.job_id,
                    detail={"error": "no_decision", "calls": calls})
            job.front_decision = {
                "decisions": [item.to_dict() for item in decisions],
                "raw_output": str(getattr(turn, "raw_output", "") or ""),
                "attempts": list(getattr(turn, "attempts", []) or []),
            }
            job.front_actions = list(
                getattr(turn, "action_history", None) or [])
            work_intents = (
                FrontIntent.HANDOFF, FrontIntent.REWORK,
                FrontIntent.FOLLOW_UP)
            work = [item for item in decisions if item.intent in work_intents]
            control = [
                item for item in decisions if item.intent not in work_intents]
            allowed_control = {
                FrontIntent.DIRECT_REPLY, FrontIntent.NO_NOTIFY}
            rejected = [
                item for item in control
                if item.intent not in allowed_control]
            if rejected:
                names = ", ".join(item.intent for item in rejected)
                self._fail(
                    job, f"alarm_wakeup_cannot_do: {names}")
                return WakeupResult(
                    outcome="rejected",
                    summary=f"alarm_wakeup_cannot_do: {names}",
                    job_id=job.job_id,
                    detail={
                        "error": f"alarm_wakeup_cannot_do: {names}",
                        "calls": calls,
                    })
            replied = False
            for item in control:
                if item.intent == FrontIntent.DIRECT_REPLY:
                    self._message(
                        "front", item.reply,
                        job_id=job.job_id, task_id=job.task_id,
                        duration_ms=job.front_ms,
                        thinking=job.front_thinking)
                    job.status = JobStatus.DIRECT_REPLY
                    job.summary = item.reply
                    replied = True
                elif item.intent == FrontIntent.NO_NOTIFY:
                    job.status = JobStatus.NO_NOTIFY
                    job.summary = item.reason or "no_notify"
                self._touch(job)

            dispatched = 0
            dispatch_detail: Dict[str, Any] = {}
            if work:
                result = self._dispatch_alarm_work(job, work)
                dispatch_detail = dict(result)
                if not result.get("ok"):
                    if job.status != JobStatus.FAILED:
                        self._fail(
                            job,
                            str(result.get("error") or "派发失败"))
                    return WakeupResult(
                        outcome="failed",
                        summary=job.error or "dispatch_failed",
                        job_id=job.job_id,
                        detail={
                            "error": job.error or "dispatch_failed",
                            "calls": calls,
                            **dispatch_detail,
                        })
                dispatched = int(result.get("dispatched") or 0)
                if not replied:
                    self._message(
                        "system",
                        f"闹钟已派发 {dispatched} 个任务。",
                        job_id=job.job_id)
                self._touch(job)

            self._write_job_log(job)
            if dispatched:
                return WakeupResult(
                    outcome="dispatched",
                    summary=job.summary or f"已派发 {dispatched} 个任务",
                    job_id=job.job_id,
                    detail={"calls": calls, **dispatch_detail})
            return WakeupResult(
                outcome="replied" if replied else "no_notify",
                summary=job.summary,
                job_id=job.job_id,
                detail={"calls": calls})
        finally:
            prompt_log.clear_job()
            self._notify()

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
            task_dir = self._task_directory(task)
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
        if self.alarm_service is not None:
            self.alarm_service.stop()
            self.alarm_service = None
        if self._pi_inbox_service is not None:
            self._pi_inbox_service.stop_all()
            self._pi_inbox_service = None
        self._notify()
        self._executor.shutdown(wait=False, cancel_futures=True)
        prompt_log.clear_job()
        prompt_log.configure(False, None)


def _task_text_from_brief(brief: Any) -> str:
    """Worker 唯一看到的任务文本：Front 选择的 `task_text`。"""
    return str(
        getattr(brief, "task_text", "")
        or getattr(brief, "human_text", "")
        or getattr(brief, "goal", "")
    ).strip()


def _description_from_brief(brief: Any) -> str:
    """Task description 与 Worker prompt 都只使用 `task_text`。"""
    return _task_text_from_brief(brief)


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
