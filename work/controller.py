"""
AIEOSController —— Human → Front → Leader → Team 的持续运行控制面
================================================================

GUI / CLI 共用的应用层：

    Human 输入
      ↓
    FrontAgentRuntime（固定 Front Agent）
      ├─ direct_reply / clarify → 直接回给 Human
      └─ handoff → Brief
                    ↓
              选择一个 IDLE Agent 作为 Leader
                    ↓
              Leader 规划 + 从 IDLE Agent 中选择成员
                    ↓
              创建 TEAM Task
                    ↓
              TeamWorkRuntime（计划 / 并行 / QA / 汇总）
                    ↓
              FrontResult → Human

🔒 这里不重复实现 Task / Workspace / Memory / Work runtime；
   只负责编排已经存在的机制。
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from core.task import TaskMode, TaskStatus
from demand.task_creation import TaskCreationService
from front import FrontAgentRuntime, FrontIntent, FrontResult
from work.availability import AgentAvailability
from work.plan import WorkPlan, parse_work_plan
from work.prompt import build_lead_plan_prompt
from work.roles import WorkContext, WorkRole
from work.runtime import SoloWorkRuntime
from work.team_runtime import TeamWorkRuntime


class JobStatus:
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    FRONT_ANALYZING = "FRONT_ANALYZING"
    WAITING_FOR_LEADER = "WAITING_FOR_LEADER"
    LEADER_PLANNING = "LEADER_PLANNING"
    WAITING_FOR_MEMBERS = "WAITING_FOR_MEMBERS"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DIRECT_REPLY = "DIRECT_REPLY"
    CLARIFY = "CLARIFY"


#: Job 仍占用 Task 的状态；其余状态下的开放 Task 视为「已中断」。
_RUNNING_JOB_STATUSES = frozenset({
    JobStatus.FRONT_ANALYZING,
    JobStatus.WAITING_FOR_LEADER,
    JobStatus.LEADER_PLANNING,
    JobStatus.WAITING_FOR_MEMBERS,
    JobStatus.RUNNING,
    JobStatus.CANCELLING,
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
            "cancelled": self.cancelled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


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
        cognitive_budget: int = 32768,
        settings_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
    ) -> None:
        self.rt = runtime
        self.front_agent = str(front_agent)
        self.max_parallel_teams = max(1, int(max_parallel_teams))
        self.max_agents_per_team = max(2, int(max_agents_per_team))
        self.max_worker_turns = max(1, int(max_worker_turns))
        self.max_integration_turns = max(1, int(max_integration_turns))
        self.cognitive_budget = max(1, int(cognitive_budget))
        self.settings_path = Path(settings_path) if settings_path else None
        self.log_dir = Path(log_dir) if log_dir else None
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
        self._load_settings()

        # 固定 Front Agent 不参与组队，也不显示为 WORKING。
        if self.front_agent not in self._agent_ids:
            raise ValueError(
                f"Front Agent {self.front_agent!r} 不在 Agent Directory 里")

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

    def update_settings(
        self,
        *,
        front_agent: Optional[str] = None,
        max_parallel_teams: Optional[int] = None,
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
            self._persist_settings()
            self._notify()
            return self.settings_dict()

    def settings_dict(self) -> Dict[str, Any]:
        return {
            "front_agent": self.front_agent,
            "max_parallel_teams": self.max_parallel_teams,
            "max_agents_per_team": self.max_agents_per_team,
        }

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
                decision = front.decide(
                    job.human_text,
                    active_tasks=active,
                    completed_tasks=completed,
                )
            job.front_intent = decision.intent
            self._set_job(job, job.status)

            if decision.intent == FrontIntent.DIRECT_REPLY:
                self._message("front", decision.reply, job_id=job.job_id)
                job.status = JobStatus.DIRECT_REPLY
                job.summary = decision.reply
                self._touch(job)
                return
            if decision.intent == FrontIntent.CLARIFY:
                self._message("front", decision.question, job_id=job.job_id)
                job.status = JobStatus.CLARIFY
                job.summary = decision.question
                self._touch(job)
                return
            if decision.intent == FrontIntent.CANCEL:
                self._cancel_work(job, decision)
                return
            if decision.intent in (
                FrontIntent.REWORK, FrontIntent.FOLLOW_UP,
            ):
                leader, members = self._resume_or_rework(
                    job, decision, front)
                return

            # ---- handoff ----
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
            created = self._create_task(
                job, leader, members, plan, decision, mode=mode)
            if not created.get("ok"):
                self._fail(job, created.get("message") or "Task 创建失败")
                return
            task_id = str(created["task_id"])
            job.task_id = task_id
            self._bind_task_to_agents([leader, *members], task_id)
            self._set_job(job, JobStatus.RUNNING)
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
                    max_turns=(
                        self.max_worker_turns + self.max_integration_turns),
                    cognitive_budget=self.cognitive_budget,
                    should_stop=lambda: job.cancelled,
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
                    self._unbind_agent(aid)
            if admitted:
                with self._condition:
                    self._active_jobs = max(0, self._active_jobs - 1)
            self._write_job_log(job)
            self._notify()

    # ------------------------------------------------------------
    # Front / Leader / Task
    # ------------------------------------------------------------
    def _front_runtime(self) -> Optional[FrontAgentRuntime]:
        agent = self._agent(self.front_agent)
        if agent is None or not getattr(agent, "has_brain", False):
            return None
        return FrontAgentRuntime(self.front_agent, agent.think)

    def _acquire_leader(self, job: WorkJob) -> Optional[str]:
        candidates = [
            aid for aid in self._agent_ids
            if aid != self.front_agent and self._agent_has_brain(aid)
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
                raw = agent.think(prompt)
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
            or [decision.brief.goal]
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

        if action == "rework":
            turn = int(getattr(task, "iteration", 1) or 1) + 1
            if decision.reply:
                self._message(
                    "front", decision.reply,
                    job_id=job.job_id, task_id=task.task_id)
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
            runner = SoloWorkRuntime(
                self.rt, task,
                max_turns=(
                    self.max_worker_turns + self.max_integration_turns),
                cognitive_budget=self.cognitive_budget,
                should_stop=lambda: job.cancelled,
            )
        result = (
            runner.reopen_and_run(reason=reason)
            if action == "rework" else runner.run())
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
        front_result = front.result_from_work(result)
        self._message(
            "front",
            front.format_result(front_result),
            job_id=job.job_id, task_id=task_id)
        job.summary = front_result.summary or front_result.status
        job.status = (
            JobStatus.COMPLETED if result.ok else JobStatus.FAILED)
        if not result.ok:
            job.error = result.error or f"Task {task_id} 未完成"
        self._touch(job)

    def _reply_direct(
        self, job: WorkJob, text: str, task_id: str = "",
    ) -> None:
        self._message(
            "front", text, job_id=job.job_id, task_id=task_id)
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
                    job, f"任务 {task.task_id} 已停止。", task.task_id)
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
            f"正在停止任务 {task.task_id}。已经通知执行中的团队，"
            "他们会在当前步骤结束后停下。",
            task.task_id,
        )

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
            "messages": [
                message for message in self._messages
                if message.get("task_id") == task_id
            ],
        }

    def _llm_summary(self, task_id: str) -> Dict[str, Any]:
        try:
            row = self.rt.recorder.db.fetchone(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_ai), 0) AS cost_ai
                  FROM llm_calls
                 WHERE task_id = ?
                """,
                (str(task_id),),
            )
        except Exception:                                     # noqa: BLE001
            return {"calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "cost_ai": 0.0}
        if row is None:
            return {"calls": 0, "input_tokens": 0,
                    "output_tokens": 0, "cost_ai": 0.0}
        return {
            "calls": int(row["calls"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cost_ai": round(float(row["cost_ai"] or 0), 4),
        }

    def _workspace_files(self, task: Any) -> List[Dict[str, Any]]:
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
        for task in tasks:
            cards.append({
                "task_id": task.task_id,
                "status": task.status,
                "mode": task.mode,
                "iteration": task.iteration,
                "description": (task.description or "")[:300],
                "leader": task.creator,
                "members": list(task.participants or []),
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

    def _message(
        self,
        role: str,
        text: str,
        *,
        job_id: str = "",
        task_id: str = "",
    ) -> None:
        with self._lock:
            self._messages.append({
                "role": role,
                "text": str(text),
                "job_id": job_id,
                "task_id": task_id,
                "at": _now(),
            })

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
                    "job": job.to_dict(),
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

    def _agent(self, aid: str) -> Any:
        try:
            return self.rt.economy.agent(aid)
        except Exception:                                     # noqa: BLE001
            return None

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


def _description_from_brief(brief: Any) -> str:
    lines = [brief.goal]
    if brief.deliverables:
        lines.append("交付物：\n" + "\n".join(
            f"- {item}" for item in brief.deliverables))
    if brief.constraints:
        lines.append("约束：\n" + "\n".join(
            f"- {item}" for item in brief.constraints))
    if brief.non_goals:
        lines.append("非目标：\n" + "\n".join(
            f"- {item}" for item in brief.non_goals))
    return "\n\n".join(lines)


def _now() -> str:
    from core.identity import utc_now_iso
    return utc_now_iso()
