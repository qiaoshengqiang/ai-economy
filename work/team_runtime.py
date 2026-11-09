"""
TeamWorkRuntime —— Leader 计划 / 并行执行 / 汇总收尾
=====================================================

用户定义的团队工作方式：

    Leader 做计划与架构
        ↓
    分配 item 给多个成员
        ↓
    多 Agent 并行完成
        ↓
    (可选) QA 验收
        ↓
    Leader 汇总、接线、收尾 → COMPLETED

🔒 设计边界：

    · Task 就是团队容器，不新增 Team 实体；
    · Leader = Task Creator = contractor / 最终收尾人；
    · Work 层没有 Agent 间消息；成员用 `work_item_done` /
      `work_item_blocked` 结构化回报；
    · QA 是否设置由 Leader 决定；设置后必须通过才能完成；
    · 没有 QA 时 Leader 自己收尾（显式 Team 例外）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef
from services.assignment_ops import assign_acceptance, revoke_acceptance
from services.resource_control import create_directory, handoff_resource

from work.actions import build_work_dispatcher
from work.drafts import TaskDraftStore
from work.lifecycle import WorkLifecycle, WorkLifecycleError
from work.plan import WorkItem, WorkPlan, parse_work_plan
from work.prompt import (
    build_lead_integration_prompt,
    build_lead_plan_prompt,
    build_leader_replan_prompt,
    build_qa_prompt,
    build_team_worker_prompt,
)
from work.replan import ReplanError, apply_plan_operations
from work.roles import WorkContext, WorkResult, WorkRole


#: item 状态（runtime 内部，不是 Task 状态）
class ItemStatus:
    TODO = "TODO"
    WORKING = "WORKING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"


class TeamWorkRuntime:
    """一个 Team Task 的计划 / 并行 / 汇总执行循环。"""

    def __init__(
        self,
        runtime: Any,
        task: Task,
        *,
        brief: Optional[Dict[str, Any]] = None,
        initial_plan: Optional[WorkPlan] = None,
        drafts: Any = None,
        max_plan_turns: int = 2,
        max_worker_turns: int = 8,
        max_integration_turns: int = 8,
        max_qa_turns: int = 3,
        max_parallel_workers: int = 4,
        max_replans: int = 3,
        cognitive_budget: int = 32768,
        event_sink: Any = None,
    ) -> None:
        if task.mode != TaskMode.TEAM:
            raise ValueError(
                f"TeamWorkRuntime 只接受 TEAM Task；"
                f"{task.task_id} 是 {task.mode}")
        if task.creator not in (task.participants or []):
            raise ValueError("Team Leader 必须在 participants 里")
        self.rt = runtime
        self.task = task
        self.lead = task.creator
        self.brief = brief
        self.initial_plan = initial_plan
        self.max_plan_turns = max(1, int(max_plan_turns))
        self.max_worker_turns = max(1, int(max_worker_turns))
        self.max_integration_turns = max(1, int(max_integration_turns))
        self.max_qa_turns = max(1, int(max_qa_turns))
        self.max_parallel_workers = max(1, int(max_parallel_workers))
        self.max_replans = max(0, int(max_replans))
        self.cognitive_budget = max(1, int(cognitive_budget))
        self.event_sink = event_sink
        self.lifecycle = WorkLifecycle(runtime.tasks)
        self.drafts = drafts or TaskDraftStore(
            task_id=task.task_id, workspace_of=self._workspace_of)

        self.plan: Optional[WorkPlan] = None
        self._item_state: Dict[str, Dict[str, Any]] = {}
        self._qa_verdict: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        self._tokens = 0
        self._actions = 0
        self._turn_count = 0
        self._events: List[Dict[str, Any]] = []
        self._completion_summary = ""
        self._plan_history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------
    def run(self) -> WorkResult:
        result = WorkResult(
            task_id=self.task.task_id,
            status=self.task.status,
            iteration=self.task.iteration,
        )
        try:
            if self.task.status == TaskStatus.COMPLETED:
                result.summary = self.task.note or "Task 已经处于 COMPLETED"
                return result
            self._start_if_needed()
            self.plan = self.initial_plan or self._plan()
            if self.plan is None:
                result.error = "Leader 没有产出可执行计划"
                result.status = self.task.status
                return result

            self._record_plan(self.plan)
            self._publish_plan_to_public_memory(self.plan)
            self._provision_workspace(self.plan)
            self._init_item_state(self.plan)
            self._plan_history.append(self.plan.to_dict())

            self._run_workers(self.plan)
            self._run_replan_loop()

            if self.plan.qa:
                if not self._run_qa():
                    before_repair = self.plan.revision
                    self._run_leader_repair()
                    if self.plan.revision != before_repair:
                        self._sync_item_state(self.plan)
                    self._run_workers(self.plan)
                    self._run_qa()

            self._run_leader_finalize()

            result.status = self.task.status
            result.turns = self._turns()
            result.tokens = self._tokens
            result.actions = self._actions
            result.events = list(self._events)
            result.summary = self._final_summary()
            result.artifacts = self._artifacts()
            result.plan_history = list(self._plan_history)

            if self.task.status != TaskStatus.COMPLETED:
                result.error = result.error or "Team 未完成（Task 未进入 COMPLETED）"
            return result
        except Exception as exc:                              # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = self.task.status
            result.tokens = self._tokens
            result.actions = self._actions
            result.events = list(self._events)
            return result

    def reopen_and_run(self, *, reason: str = "") -> WorkResult:
        """Human 通过 Front 要求返工后，由 Team Leader 重新打开。"""
        self.lifecycle.reopen(self.task, actor=self.lead, reason=reason)
        self._event("team_reopened", agent_id=self.lead, reason=reason)
        return self.run()

    # ------------------------------------------------------------
    # 阶段 1：Leader 计划
    # ------------------------------------------------------------
    def _start_if_needed(self) -> None:
        task = self.task
        if task.status == TaskStatus.COMPLETED:
            return
        if task.status in (TaskStatus.CREATED, TaskStatus.ACCEPTED):
            self.lifecycle.start(task, actor=self.lead)
        if task.contractor and task.contractor != self.lead:
            raise WorkLifecycleError(
                f"Team Leader {self.lead} 不是 contractor "
                f"（{task.contractor}）；V1 要求 Leader 同时负责最终收尾。")
        if task.status not in (
            TaskStatus.IN_PROGRESS,
            TaskStatus.SUBMITTED,
            TaskStatus.VERIFIED,
        ):
            raise WorkLifecycleError(
                f"Task 当前是 {task.status}，不能开始 Team Work run。")

    def _plan(self) -> Optional[WorkPlan]:
        agent = self._agent(self.lead)
        if agent is None or not getattr(agent, "has_brain", False):
            raise WorkLifecycleError(f"Team Leader {self.lead} 没有 Brain")

        context = self._context(self.lead, WorkRole.LEAD)
        participants = list(self.task.participants or [])
        last_error = ""
        for attempt in range(1, self.max_plan_turns + 1):
            prompt = build_lead_plan_prompt(
                context,
                task_description=self.task.description,
                participants=participants,
                workspace_facts="",
            )
            try:
                raw = agent.think(prompt)
            except Exception as exc:                          # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            plan, error = parse_work_plan(
                raw, participants=participants,
                default_goal=self.task.description)
            if plan is not None and plan.qa == self.lead:
                plan = None
                error = "qa_is_leader"
            self._event(
                "team_plan_attempt", attempt=attempt,
                ok=plan is not None, error=error)
            if plan is not None:
                return plan
            last_error = error

        self._event("team_plan_failed", error=last_error)
        return None

    def _record_plan(self, plan: WorkPlan) -> None:
        """把 Leader 的分工写进 Shared Context（如果可用）。"""
        alignment = getattr(self.rt, "alignment", None)
        if alignment is None:
            return
        for item in plan.items:
            try:
                current = alignment.current(self.task.task_id)
                change = alignment.propose(
                    self.task.task_id,
                    proposed_by=self.lead,
                    change_type="ASSIGN",
                    new_value={
                        "agent_id": item.agent_id,
                        "responsibility": f"work:{item.item_id}",
                        "notes": item.goal,
                    },
                    expected_revision=current.revision,
                    reason=f"Team plan: {item.item_id}",
                )
                alignment.accept(
                    self.task.task_id,
                    getattr(change, "change_id", None),
                    decided_by=self.lead,
                    reason="ASSIGN 声明类，允许 Leader 自批",
                )
            except Exception as exc:                          # noqa: BLE001
                self._event(
                    "team_plan_record_failed", item_id=item.item_id,
                    error=f"{type(exc).__name__}: {exc}")

        if plan.qa and plan.qa != self.lead:
            try:
                assigned = assign_acceptance(
                    alignment,
                    self.task.task_id,
                    by=self.lead,
                    to=plan.qa,
                    reason="Team Leader 指定 QA",
                    is_participant=lambda tid, aid: (
                        tid == self.task.task_id
                        and aid in (self.task.participants or [])),
                )
                if not assigned.get("ok"):
                    self._event(
                        "team_qa_assign_failed",
                        error=str(assigned.get("message") or assigned))
            except Exception as exc:                          # noqa: BLE001
                self._event(
                    "team_qa_assign_failed",
                    error=f"{type(exc).__name__}: {exc}")

    def _publish_plan_to_public_memory(self, plan: WorkPlan) -> None:
        """
        把**抽象架构**写入 Public Working Memory。

        🔒 Public WM 不是 plan 副本，也不是任务书：
           只存目录 / 文件归属 / 接口 / 依赖 / 进度等抽象结构；
           goal / acceptance / notes 留在 WorkPlan 和对应 worker prompt 里，
           不重复塞进公共记忆。

        这样每个 worker 的 prompt 都能拿到一张很小的架构白板，
        而不是 10KB 的具体任务描述。
        """
        service = getattr(self.rt, "public_working_memory", None)
        if service is None:
            return
        try:
            current = service.read(self.task.task_id, self.lead)
            existing = set(current.items)
            items = {}
            desired = set()

            # 1) 计划元信息：只保留 revision / QA / item 数。
            desired.add("team.plan")
            items["team.plan"] = {
                "kind": "plan",
                "value": {
                    "revision": plan.revision,
                    "qa": plan.qa,
                    "item_count": len(plan.items),
                },
            }

            # 2) 目录与文件归属：真正的架构白板。
            files = {}
            for item in plan.items:
                for path in item.paths:
                    files[str(path)] = item.agent_id
            desired.add("team.architecture")
            items["team.architecture"] = {
                "kind": "plan",
                "value": {
                    "directories": dict(plan.directories),
                    "files": files,
                },
            }

            # 3) 每个 item 只保留 owner + paths，不复制 goal/acceptance/notes。
            for item in plan.items:
                key = f"item.{item.item_id}"
                desired.add(key)
                items[key] = {
                    "kind": "assignment",
                    "value": {
                        "owner": item.agent_id,
                        "paths": list(item.paths),
                    },
                }

            if plan.qa:
                desired.add("team.qa")
                items["team.qa"] = {
                    "kind": "note",
                    "value": {"qa": plan.qa},
                }
            remove = sorted(
                key for key in existing
                if key.startswith("item.") and key not in desired
            )
            remove += ["team.qa"] if "team.qa" in existing and not plan.qa else []
            service.update(
                self.task.task_id,
                self.lead,
                expected_revision=current.revision,
                items=items,
                remove_keys=remove,
            )
            self._event(
                "team_plan_published", revision=plan.revision,
                items=sorted(desired))
        except Exception as exc:                              # noqa: BLE001
            self._event(
                "team_plan_publish_failed",
                error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------
    # 阶段 2：Workspace 目录准备
    # ------------------------------------------------------------
    def _provision_workspace(self, plan: WorkPlan) -> None:
        """
        按 Leader 的 directories 计划创建目录并移交锁。

        没有声明时，为每个 worker 建一个默认目录 `work/<agent>`。
        """
        workspace_id = self.task.workspace_id or ""
        if not workspace_id:
            return
        gateway = self.rt.workspace_gateway
        directories = dict(plan.directories)
        if not directories:
            for agent_id in plan.workers():
                if agent_id != self.lead:
                    directories[f"work/{agent_id}"] = agent_id

        # 先全部创建（Leader 持有 root lock），再统一 handoff。
        for path in sorted(directories, key=lambda p: (p.count("/"), p)):
            try:
                created = create_directory(
                    gateway, workspace_id, self.lead, path)
            except Exception as exc:                          # noqa: BLE001
                created = {"ok": False,
                           "message": f"{type(exc).__name__}: {exc}"}
            if not created.get("ok"):
                self._event(
                    "team_directory_create_failed",
                    path=path, error=str(
                        created.get("message") or created.get("code")))
                continue

        for path in sorted(directories, key=lambda p: (-p.count("/"), p)):
            owner = directories[path]
            if owner == self.lead:
                continue
            try:
                moved = handoff_resource(
                    gateway, workspace_id, self.lead, path, owner,
                    reason="Team plan directory handoff",
                    task_id=self.task.task_id)
            except Exception as exc:                          # noqa: BLE001
                moved = {"ok": False,
                         "message": f"{type(exc).__name__}: {exc}"}
            if not moved.get("ok"):
                self._event(
                    "team_directory_handoff_failed",
                    path=path, owner=owner,
                    error=str(moved.get("message") or moved.get("code")))
            else:
                self._event(
                    "team_directory_handoff", path=path, owner=owner)

    # ------------------------------------------------------------
    # 阶段 3：并行执行
    # ------------------------------------------------------------
    def _init_item_state(self, plan: WorkPlan) -> None:
        with self._lock:
            self._item_state = {
                item.item_id: {
                    "item_id": item.item_id,
                    "agent_id": item.agent_id,
                    "goal": item.goal,
                    "status": ItemStatus.TODO,
                    "summary": "",
                    "paths": list(item.paths),
                }
                for item in plan.items
            }

    def _run_workers(self, plan: WorkPlan) -> None:
        groups: Dict[str, List[WorkItem]] = {}
        with self._lock:
            active_ids = {
                item_id for item_id, state in self._item_state.items()
                if state.get("status") not in (
                    ItemStatus.DONE, ItemStatus.CANCELLED)
            }
        for item in plan.items:
            if item.item_id not in active_ids:
                continue
            groups.setdefault(item.agent_id, []).append(item)
        if not groups:
            return

        with ThreadPoolExecutor(
            max_workers=min(self.max_parallel_workers, len(groups))
        ) as pool:
            futures = {
                pool.submit(self._run_worker, agent_id, items): agent_id
                for agent_id, items in groups.items()
            }
            for future in as_completed(futures):
                agent_id = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:                      # noqa: BLE001
                    outcome = {"agent_id": agent_id,
                               "error": f"{type(exc).__name__}: {exc}"}
                self._event("team_worker_finished", **outcome)

        # 未回报的 item 标成 PENDING，交给 Leader 收尾时判断。
        with self._lock:
            for state in self._item_state.values():
                if state["status"] in (ItemStatus.TODO, ItemStatus.WORKING):
                    state["status"] = ItemStatus.PENDING

    # ------------------------------------------------------------
    # 阶段 3.5：Leader 重新计划
    # ------------------------------------------------------------
    def _needs_replan(self) -> bool:
        with self._lock:
            return any(
                state.get("status") in (
                    ItemStatus.BLOCKED, ItemStatus.PENDING)
                for state in self._item_state.values()
            )

    def _run_replan_loop(self) -> None:
        """
        两轮之间允许 Leader 改计划。

        只调整计划；当前轮不强行中断。改完后再开下一轮 worker。
        """
        for attempt in range(1, self.max_replans + 1):
            if not self._needs_replan():
                return
            before = self.plan.revision if self.plan else 0
            self._run_leader_replan()
            after = self.plan.revision if self.plan else before
            if after == before:
                self._event(
                    "team_replan_no_change", attempt=attempt,
                    revision=before)
                return
            self._event(
                "team_replan_applied", attempt=attempt,
                previous_revision=before, revision=after)
            self._provision_workspace(self.plan)
            self._sync_item_state(self.plan)
            self._run_workers(self.plan)

    def _sync_item_state(self, plan: WorkPlan) -> None:
        """把新计划同步到 runtime 的 item 状态。"""
        plan_ids = {item.item_id for item in plan.items}
        by_id = {item.item_id: item for item in plan.items}
        with self._lock:
            for item_id, state in self._item_state.items():
                if item_id not in plan_ids:
                    state["status"] = ItemStatus.CANCELLED
            for item_id in plan_ids:
                item = by_id[item_id]
                state = self._item_state.get(item_id)
                if state is None:
                    self._item_state[item_id] = {
                        "item_id": item.item_id,
                        "agent_id": item.agent_id,
                        "goal": item.goal,
                        "status": ItemStatus.TODO,
                        "summary": "",
                        "paths": list(item.paths),
                    }
                    continue
                previous_agent = state.get("agent_id")
                state["agent_id"] = item.agent_id
                state["goal"] = item.goal
                state["paths"] = list(item.paths)
                if previous_agent != item.agent_id:
                    state["status"] = ItemStatus.TODO
                    state["summary"] = (
                        f"reassigned from {previous_agent} to {item.agent_id}")

    def _run_leader_replan(self) -> None:
        agent = self._agent(self.lead)
        if agent is None or not getattr(agent, "has_brain", False):
            raise WorkLifecycleError(f"Team Leader {self.lead} 没有 Brain")
        dispatcher = build_work_dispatcher(
            self.rt,
            task=self.task,
            drafts=self.drafts,
            promote_drafts=self._promote_drafts,
            plan_update_fn=self._plan_update_fn,
            include_complete=False,
        )
        seen_reads: set = set()
        empty_streak = 0
        before = self.plan.revision
        for turn in range(1, self.max_integration_turns + 1):
            qa_feedback = ""
            if self._qa_verdict and not self._qa_verdict.get("passed"):
                qa_feedback = str(self._qa_verdict.get("feedback") or "")
            context = self._context(self.lead, WorkRole.LEAD)
            prompt = build_leader_replan_prompt(
                context,
                task_description=self.task.description,
                item_state=self._item_snapshot(),
                plan=self.plan,
                participants=list(self.task.participants or []),
                qa_feedback=qa_feedback,
                runtime=self.rt,
            )
            run = agent.act(
                messages=prompt,
                dispatcher=dispatcher,
                execute=True,
                task_id=self.task.task_id,
                cognitive_budget_cap=self.cognitive_budget,
                call_purpose="work",
                follow_up_on_reads=True,
                work_loop=True,
                max_work_steps=2,
                initial_phase="work",
                seen_reads=seen_reads,
                repair_malformed=True,
            )
            _segments, intents = self._consume_run(run)
            self._event(
                "team_replan_turn", turn=turn,
                action_types=[getattr(i, "action_type", "") for i in intents])
            if self.plan.revision != before:
                return
            if not intents:
                empty_streak += 1
                if empty_streak >= 2:
                    return
            else:
                empty_streak = 0

    def _plan_update_fn(
        self,
        *,
        agent_id: str,
        expected_revision: int,
        operations: Any,
        qa: Any,
        qa_provided: bool,
        reason: str,
    ) -> Dict[str, Any]:
        if agent_id != self.lead:
            return {"ok": False, "code": "not_leader",
                    "message": f"只有 Leader {self.lead} 可以更新计划"}
        if self.plan is None:
            return {"ok": False, "code": "no_plan",
                    "message": "还没有计划"}
        if expected_revision != self.plan.revision:
            return {
                "ok": False, "code": "stale_plan_revision",
                "message": (
                    f"expected_revision={expected_revision}，"
                    f"当前是 {self.plan.revision}；请重新读取计划"),
            }
        with self._lock:
            status = {
                item_id: str(state.get("status") or "")
                for item_id, state in self._item_state.items()
            }
        try:
            new_plan, applied = apply_plan_operations(
                self.plan,
                operations,
                participants=self.task.participants or [],
                leader=self.lead,
                item_status=status,
                qa=qa,
                qa_provided=qa_provided,
                reason=reason,
            )
        except ReplanError as exc:
            return {"ok": False, "code": "replan_rejected",
                    "message": str(exc)}

        previous = self.plan.revision
        previous_qa = self.plan.qa
        self.plan = new_plan
        self._plan_history.append(new_plan.to_dict())
        self._record_plan_revision(new_plan, applied, reason)
        self._publish_plan_to_public_memory(new_plan)
        if qa_provided and new_plan.qa != previous_qa:
            self._qa_verdict = None
            self._update_qa_assignment(previous_qa, new_plan.qa)
        self._event(
            "team_plan_updated", revision=new_plan.revision,
            previous_revision=previous, applied=applied, reason=reason)
        return {
            "ok": True,
            "revision": new_plan.revision,
            "previous_revision": previous,
            "applied": applied,
            "summary": f"plan revision {previous} → {new_plan.revision}",
        }

    def _update_qa_assignment(
        self, previous_qa: Optional[str], new_qa: Optional[str],
    ) -> None:
        alignment = getattr(self.rt, "alignment", None)
        if alignment is None:
            return
        try:
            if new_qa:
                assigned = assign_acceptance(
                    alignment,
                    self.task.task_id,
                    by=self.lead,
                    to=new_qa,
                    reason="Team Leader 更新 QA",
                    is_participant=lambda tid, aid: (
                        tid == self.task.task_id
                        and aid in (self.task.participants or [])),
                )
                if not assigned.get("ok"):
                    self._event(
                        "team_qa_assign_failed",
                        error=str(assigned.get("message") or assigned))
            elif previous_qa:
                revoked = revoke_acceptance(
                    alignment,
                    self.task.task_id,
                    by=self.lead,
                    target=previous_qa,
                    reason="Team Leader 清除 QA",
                )
                if not revoked.get("ok"):
                    self._event(
                        "team_qa_revoke_failed",
                        error=str(revoked.get("message") or revoked))
        except Exception as exc:                              # noqa: BLE001
            self._event(
                "team_qa_assignment_error",
                error=f"{type(exc).__name__}: {exc}")

    def _record_plan_revision(
        self,
        plan: WorkPlan,
        applied: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """把计划 revision 写进 Shared Context（如果可用）。"""
        alignment = getattr(self.rt, "alignment", None)
        if alignment is None:
            return
        try:
            current = alignment.current(self.task.task_id)
            statement = (
                f"WorkPlan revision {plan.revision}: "
                f"{len(plan.items)} items; applied={applied}; "
                f"reason={reason or 'replan'}")
            change = alignment.propose(
                self.task.task_id,
                proposed_by=self.lead,
                change_type="FACT",
                new_value={
                    "statement": statement,
                    "source": f"team_plan:{plan.revision}",
                },
                expected_revision=current.revision,
                reason="记录 Team 计划修订",
            )
            alignment.accept(
                self.task.task_id,
                getattr(change, "change_id", None),
                decided_by=self.lead,
                reason="FACT 声明类，允许自批",
            )
        except Exception as exc:                              # noqa: BLE001
            self._event(
                "team_plan_record_failed", revision=plan.revision,
                error=f"{type(exc).__name__}: {exc}")

    def _run_worker(self, agent_id: str, items: List[WorkItem]) -> Dict[str, Any]:
        agent = self._agent(agent_id)
        if agent is None or not getattr(agent, "has_brain", False):
            return {"agent_id": agent_id, "error": "no_brain"}

        dispatcher = build_work_dispatcher(
            self.rt,
            task=self.task,
            drafts=self.drafts,
            promote_drafts=self._promote_drafts,
            item_done_fn=self._item_done,
            item_blocked_fn=self._item_blocked,
            include_complete=False,
        )
        seen_reads: set = set()
        empty_streak = 0
        turns = 0

        for turn in range(1, self.max_worker_turns + 1):
            turns = turn
            context = self._context(agent_id, WorkRole.MEMBER)
            prompt = build_team_worker_prompt(
                context,
                task_description=self.task.description,
                items=items,
                item_state=self._item_snapshot_for(agent_id),
                runtime=self.rt,
            )
            run = agent.act(
                messages=prompt,
                dispatcher=dispatcher,
                execute=True,
                task_id=self.task.task_id,
                cognitive_budget_cap=self.cognitive_budget,
                call_purpose="work",
                follow_up_on_reads=True,
                work_loop=True,
                max_work_steps=2,
                initial_phase="work",
                seen_reads=seen_reads,
                repair_malformed=True,
            )
            segments, intents = self._consume_run(run)
            self._event(
                "team_worker_turn", agent_id=agent_id, turn=turn,
                action_types=[getattr(i, "action_type", "")
                              for i in intents])
            if self._items_finished(agent_id):
                break
            if not intents:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0

        return {
            "agent_id": agent_id,
            "turns": turns,
            "items": [
                self._item_state.get(item.item_id, {}) for item in items
            ],
        }

    def _item_done(
        self, *, agent_id: str, item_id: str,
        summary: str = "", paths: Any = None,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._item_state.get(item_id)
            if state is None:
                return {"ok": False, "code": "unknown_item",
                        "message": f"没有 item {item_id!r}"}
            if state["agent_id"] != agent_id:
                return {"ok": False, "code": "not_your_item",
                        "message": f"{item_id} 不属于 {agent_id}"}
            state["status"] = ItemStatus.DONE
            state["summary"] = str(summary or "")
            if paths:
                state["paths"] = [str(p) for p in paths]
            self._event(
                "team_item_done", agent_id=agent_id,
                item_id=item_id, summary=summary)
            return {"ok": True, "item_id": item_id,
                    "status": ItemStatus.DONE, "summary": summary}

    def _item_blocked(
        self, *, agent_id: str, item_id: str, reason: str,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._item_state.get(item_id)
            if state is None:
                return {"ok": False, "code": "unknown_item",
                        "message": f"没有 item {item_id!r}"}
            if state["agent_id"] != agent_id:
                return {"ok": False, "code": "not_your_item",
                        "message": f"{item_id} 不属于 {agent_id}"}
            state["status"] = ItemStatus.BLOCKED
            state["summary"] = str(reason or "")
            self._event(
                "team_item_blocked", agent_id=agent_id,
                item_id=item_id, reason=reason)
            return {"ok": True, "item_id": item_id,
                    "status": ItemStatus.BLOCKED, "summary": reason}

    def _items_finished(self, agent_id: str) -> bool:
        with self._lock:
            states = [
                s for s in self._item_state.values()
                if s["agent_id"] == agent_id
            ]
        return bool(states) and all(
            s["status"] in (
                ItemStatus.DONE, ItemStatus.BLOCKED, ItemStatus.CANCELLED)
            for s in states)

    # ------------------------------------------------------------
    # 阶段 4：QA
    # ------------------------------------------------------------
    def _run_qa(self) -> bool:
        qa = self.plan.qa if self.plan else None
        if not qa:
            return True
        agent = self._agent(qa)
        if agent is None or not getattr(agent, "has_brain", False):
            self._event("team_qa_no_brain", agent_id=qa)
            return False

        self._qa_verdict = None
        dispatcher = build_work_dispatcher(
            self.rt,
            task=self.task,
            drafts=self.drafts,
            promote_drafts=self._promote_drafts,
            qa_verdict_fn=self._qa_verdict_fn,
            include_complete=False,
        )
        seen_reads: set = set()
        for turn in range(1, self.max_qa_turns + 1):
            context = self._context(qa, WorkRole.QA)
            prompt = build_qa_prompt(
                context,
                task_description=self.task.description,
                item_state=self._item_snapshot(),
                runtime=self.rt,
            )
            run = agent.act(
                messages=prompt,
                dispatcher=dispatcher,
                execute=True,
                task_id=self.task.task_id,
                cognitive_budget_cap=self.cognitive_budget,
                call_purpose="work",
                follow_up_on_reads=True,
                work_loop=True,
                max_work_steps=2,
                initial_phase="work",
                seen_reads=seen_reads,
                repair_malformed=True,
            )
            _segments, intents = self._consume_run(run)
            self._event(
                "team_qa_turn", turn=turn,
                action_types=[getattr(i, "action_type", "") for i in intents])
            if self._qa_verdict is not None:
                return bool(self._qa_verdict.get("passed"))
        self._force_qa_verdict(agent, dispatcher)
        return bool(self._qa_verdict and self._qa_verdict.get("passed"))

    def _force_qa_verdict(self, agent: Any, dispatcher: Any) -> None:
        """QA 只回文本、没有 verdict 时，再给一次最小 schema 机会。"""
        prompt = f"""QA 结论必须落成一个动作，不能只回文字。

Task: {self.task.task_id}
目标: {self.task.description[:1200]}

只输出一个 JSON 动作：
{{"decision":"act","reasoning":"...",
 "action":{{"type":"work_qa_verdict","parameters":{{
   "passed":true,
   "feedback":"如果通过可以写简短说明"
 }}}}}}
如果不通过，把 passed 改成 false，并在 feedback 写清楚要修什么。
"""
        for attempt in range(1, 3):
            try:
                run = agent.act(
                    messages=prompt,
                    dispatcher=dispatcher,
                    execute=True,
                    task_id=self.task.task_id,
                    cognitive_budget_cap=self.cognitive_budget,
                    call_purpose="work",
                    follow_up_on_reads=False,
                    work_loop=False,
                    max_work_steps=0,
                    initial_phase="decision",
                    repair_malformed=True,
                )
            except Exception as exc:                          # noqa: BLE001
                self._event(
                    "team_force_qa_error", attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}")
                continue
            _segments, intents = self._consume_run(run)
            self._event(
                "team_force_qa_turn", attempt=attempt,
                action_types=[
                    getattr(i, "action_type", "") for i in intents])
            if self._qa_verdict is not None:
                return

    def _qa_verdict_fn(
        self, *, agent_id: str, passed: bool, feedback: str,
    ) -> Dict[str, Any]:
        qa = self.plan.qa if self.plan else None
        if agent_id != qa:
            return {"ok": False, "code": "not_qa",
                    "message": f"{agent_id} 不是本任务的 QA"}
        self._qa_verdict = {
            "passed": bool(passed),
            "feedback": str(feedback or ""),
            "by": agent_id,
        }
        self._event(
            "team_qa_verdict", agent_id=agent_id,
            passed=bool(passed), feedback=feedback)
        return {"ok": True, "passed": bool(passed), "feedback": feedback}

    # ------------------------------------------------------------
    # 阶段 5：Leader 修复 / 汇总收尾
    # ------------------------------------------------------------
    def _run_leader_repair(self) -> None:
        """QA 未通过时，Leader 先修复 / 重新计划；不暴露 work_complete。"""
        self._run_leader_turns(
            include_complete=False,
            include_plan_update=True,
        )

    def _run_leader_finalize(self) -> None:
        self._run_leader_turns(include_complete=True)

    def _run_leader_turns(
        self,
        *,
        include_complete: bool,
        include_plan_update: bool = False,
    ) -> None:
        agent = self._agent(self.lead)
        if agent is None or not getattr(agent, "has_brain", False):
            raise WorkLifecycleError(f"Team Leader {self.lead} 没有 Brain")

        dispatcher = build_work_dispatcher(
            self.rt,
            task=self.task,
            drafts=self.drafts,
            promote_drafts=self._promote_drafts,
            complete_fn=self._complete_fn,
            plan_update_fn=(
                self._plan_update_fn if include_plan_update else None),
            include_complete=include_complete,
        )
        seen_reads: set = set()
        empty_streak = 0
        for turn in range(1, self.max_integration_turns + 1):
            qa_feedback = ""
            if self._qa_verdict and not self._qa_verdict.get("passed"):
                qa_feedback = str(self._qa_verdict.get("feedback") or "")
            context = self._context(self.lead, WorkRole.LEAD)
            prompt = build_lead_integration_prompt(
                context,
                task_description=self.task.description,
                item_state=self._item_snapshot(),
                qa_feedback=qa_feedback,
                runtime=self.rt,
                include_plan_update=include_plan_update,
            )
            run = agent.act(
                messages=prompt,
                dispatcher=dispatcher,
                execute=True,
                task_id=self.task.task_id,
                cognitive_budget_cap=self.cognitive_budget,
                call_purpose="work",
                follow_up_on_reads=True,
                work_loop=True,
                max_work_steps=2,
                initial_phase="work",
                seen_reads=seen_reads,
                repair_malformed=True,
            )
            _segments, intents = self._consume_run(run)
            self._event(
                "team_leader_turn", turn=turn,
                include_complete=include_complete,
                action_types=[getattr(i, "action_type", "") for i in intents])
            if self.task.status == TaskStatus.COMPLETED:
                return
            if not intents:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0

        if (
            include_complete
            and self.task.status != TaskStatus.COMPLETED
            and self._all_items_settled()
            and self._qa_allows_completion()
        ):
            self._force_leader_completion()

    def _all_items_settled(self) -> bool:
        with self._lock:
            return all(
                state.get("status") in (
                    ItemStatus.DONE, ItemStatus.CANCELLED)
                for state in self._item_state.values()
            )

    def _qa_allows_completion(self) -> bool:
        if not self.plan or not self.plan.qa:
            return True
        return bool(self._qa_verdict and self._qa_verdict.get("passed"))

    def _force_leader_completion(self) -> None:
        """
        所有 item 已 DONE 后，Leader 可能只回了一段总结而没有调用
        work_complete。这里用**最小完成 prompt**再推进一次：
        只允许输出 work_complete 动作。
        """
        agent = self._agent(self.lead)
        if agent is None or not getattr(agent, "has_brain", False):
            return
        dispatcher = build_work_dispatcher(
            self.rt,
            task=self.task,
            drafts=self.drafts,
            promote_drafts=self._promote_drafts,
            complete_fn=self._complete_fn,
            include_complete=True,
        )
        prompt = f"""所有 item 已经 DONE。现在只需要完成 Team Task。

Task: {self.task.task_id}
目标: {self.task.description[:1200]}

不要再解释、不要再读取文件。只输出一个 JSON 动作：
{{"decision":"act","reasoning":"团队工作已完成，进入收尾",
 "action":{{"type":"work_complete","parameters":{{
   "result_ref":"team-result-{self.task.task_id}",
   "summary":"一句话总结最终交付"
 }}}}}}
"""
        for attempt in range(1, 3):
            try:
                run = agent.act(
                    messages=prompt,
                    dispatcher=dispatcher,
                    execute=True,
                    task_id=self.task.task_id,
                    cognitive_budget_cap=self.cognitive_budget,
                    call_purpose="work",
                    follow_up_on_reads=False,
                    work_loop=False,
                    max_work_steps=0,
                    initial_phase="decision",
                    repair_malformed=True,
                )
            except Exception as exc:                          # noqa: BLE001
                self._event(
                    "team_force_complete_error",
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}")
                continue
            _segments, intents = self._consume_run(run)
            self._event(
                "team_force_complete_turn",
                attempt=attempt,
                action_types=[
                    getattr(i, "action_type", "") for i in intents])
            if self.task.status == TaskStatus.COMPLETED:
                return

    # ------------------------------------------------------------
    # 完成回调
    # ------------------------------------------------------------
    def _complete_fn(
        self,
        *,
        actor: str,
        result_ref: str,
        summary: str,
        result_type: str,
        metadata: Any,
        feedback: str,
    ) -> Dict[str, Any]:
        try:
            with self._lock:
                unfinished = [
                    item_id for item_id, state in self._item_state.items()
                    if state.get("status") in (
                        ItemStatus.TODO, ItemStatus.WORKING,
                        ItemStatus.BLOCKED, ItemStatus.PENDING,
                    )
                ]
            if unfinished:
                return {
                    "ok": False,
                    "code": "items_unfinished",
                    "message": (
                        "还有未完成 item：" + ", ".join(sorted(unfinished)) +
                        "；请先 reassign / add / cancel，或完成它们。"),
                }
            if self.plan and self.plan.qa:
                if not self._qa_verdict or not self._qa_verdict.get("passed"):
                    return {
                        "ok": False,
                        "code": "qa_not_passed",
                        "message": "设置了 QA；QA 未通过前不能完成 Task。",
                    }
            ref = TaskResultRef(
                result_ref=result_ref,
                result_type=result_type or "artifact",
                summary=summary,
                metadata=dict(metadata or {}),
            )
            outcome = self.lifecycle.complete_team(
                self.task,
                actor=actor,
                result=ref,
                qa_passed=True if (self.plan and self.plan.qa) else None,
                feedback=feedback,
            )
            if outcome.get("ok"):
                self._completion_summary = str(summary or "")
            return outcome
        except (WorkLifecycleError, ValueError) as exc:
            return {"ok": False, "code": "team_complete_rejected",
                    "message": str(exc)}

    # ------------------------------------------------------------
    # 上下文 / 工具
    # ------------------------------------------------------------
    def _context(self, agent_id: str, role: str) -> WorkContext:
        task = self.task
        return WorkContext(
            task_id=task.task_id,
            agent_id=agent_id,
            role=role,
            iteration=task.iteration,
            brief=self.brief,
            workspace_id=task.workspace_id or "",
            acceptance_criteria=self._acceptance_criteria(),
            memory_facts=self._memory_facts(agent_id),
            public_memory_facts=self._public_memory_facts(agent_id),
            draft_facts=self._draft_facts(agent_id),
        )

    def _public_memory_facts(self, agent_id: str) -> str:
        try:
            return self.rt.public_working_memory.render(
                self.task.task_id, agent_id)
        except Exception:                                     # noqa: BLE001
            return ""

    def _acceptance_criteria(self) -> List[str]:
        try:
            current = self.rt.alignment.current(self.task.task_id)
            criteria = getattr(current.context, "acceptance_criteria", ())
            return [str(item) for item in criteria]
        except Exception:                                     # noqa: BLE001
            return []

    def _memory_facts(self, agent_id: str) -> str:
        lines: List[str] = []
        try:
            working = self.rt.private_working_memory.render(
                agent_id, self.task.task_id)
            if working:
                lines.append(working)
        except Exception:                                     # noqa: BLE001
            pass
        try:
            short = self.rt.private_short_term_memory.render(agent_id)
            if short:
                lines.append("短期记忆（跨任务）：\n" + short)
        except Exception:                                     # noqa: BLE001
            pass
        try:
            long = self.rt.private_long_term_memory.render_top(agent_id)
            if long:
                lines.append("长期记忆（跨任务）：\n" + long)
        except Exception:                                     # noqa: BLE001
            pass
        return "\n\n".join(lines)

    def _draft_facts(self, agent_id: str) -> str:
        try:
            drafts = self.drafts.list(agent_id).get("drafts") or []
            return str(drafts) if drafts else ""
        except Exception:                                     # noqa: BLE001
            return ""

    def _agent(self, agent_id: str) -> Any:
        try:
            return self.rt.economy.agent(agent_id)
        except Exception:                                     # noqa: BLE001
            return None

    def _workspace_of(self, agent_id: str) -> Any:
        agent = self._agent(agent_id)
        return getattr(agent, "workspace", None)

    def _promote_drafts(
        self, agent_id: str, items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        from services.artifacts import write_artifact

        workspace_id = self.task.workspace_id or ""
        results: List[Dict[str, Any]] = []
        for item in items:
            draft_id = str(item.get("draft_id") or "")
            path = str(item.get("path") or "")
            mode = str(item.get("write_mode") or "")
            draft = self.drafts.get(agent_id, draft_id)
            if draft is None:
                results.append({
                    "draft_id": draft_id, "path": path, "ok": False,
                    "code": "draft_not_found",
                    "message": f"没有草稿 {draft_id!r}",
                })
                continue
            try:
                raw = write_artifact(
                    self.rt.workspace_gateway, workspace_id, agent_id,
                    path, draft.content, write_mode=mode)
            except Exception as exc:                          # noqa: BLE001
                raw = {"ok": False, "code": "executor_error",
                       "message": f"{type(exc).__name__}: {exc}"}
            results.append({
                "draft_id": draft_id, "path": path,
                "ok": bool(raw.get("ok")),
                "code": raw.get("code"),
                "message": raw.get("message") or "",
            })
        ok_count = sum(1 for item in results if item.get("ok"))
        failures = [
            f"{item.get('path')}: {item.get('message') or item.get('code')}"
            for item in results if not item.get("ok")
        ]
        summary = f"{ok_count}/{len(results)} 个文件 promotion 成功"
        if failures:
            summary += "；失败：" + " | ".join(failures[:5])
        return {"ok": ok_count == len(results),
                "summary": summary, "results": results}

    def _item_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                item_id: dict(state)
                for item_id, state in self._item_state.items()
            }

    def _item_snapshot_for(self, agent_id: str) -> Dict[str, Dict[str, Any]]:
        """Worker 只看到自己的 item；Leader / QA 看全部。"""
        with self._lock:
            return {
                item_id: dict(state)
                for item_id, state in self._item_state.items()
                if state.get("agent_id") == agent_id
                and state.get("status") != ItemStatus.CANCELLED
            }

    def _consume_run(self, run: Any) -> Tuple[List[Any], List[Any]]:
        segments = list(getattr(run, "segments", None) or [])
        intents = list(getattr(run, "action_intents", None) or [])
        tokens = sum(int(getattr(s, "tokens", 0) or 0) for s in segments)
        with self._lock:
            self._tokens += tokens
            self._actions += len(intents)
            self._turn_count = getattr(self, "_turn_count", 0) + 1
        return segments, intents

    def _turns(self) -> int:
        return int(getattr(self, "_turn_count", 0))

    def _event(self, name: str, **payload: Any) -> None:
        record = {"event": name, **payload}
        with self._lock:
            self._events.append(record)
        if self.event_sink is None:
            return
        try:
            self.event_sink(name, payload)
        except Exception:                                     # noqa: BLE001
            pass

    def _artifacts(self) -> List[str]:
        out: List[str] = []
        with self._lock:
            for state in self._item_state.values():
                for path in state.get("paths") or []:
                    text = str(path or "").strip()
                    if text and text not in out:
                        out.append(text)
        return out

    def _final_summary(self) -> str:
        if self._completion_summary:
            return self._completion_summary
        if self._events:
            for record in reversed(self._events):
                if record.get("event") == "team_item_done" and record.get(
                        "summary"):
                    return str(record["summary"])
        return self.task.note or "Team 工作完成"
