"""
DemandRunner —— 人类 Demand → Agent 自建 Task → 串行执行
============================================================

V1 边界：

- 一个 DemandRun 只创建一个 Root Task；
- front agent 先收到 Demand + Agent Directory，自己调用 `task_create`；
- 之后 RunScheduler 轮询 participants；
- Task Core 是唯一生命周期权威；
- 不设固定 producer/acceptor，由 Agent 自己决定/指派。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.identity import utc_now_iso
from demand.actions import build_intake_dispatcher, build_task_dispatcher
from demand.channels import SyncBridge, drain
from demand.drafts import DraftBuffer
from demand.images import make_image_resolver
from demand.intake import fallback_brief, parse_brief
from demand.models import DemandRun, TurnOutcome
from demand.prompt import (
    build_brief_prompt, build_intake_prompt, build_task_prompt,
)
from demand.scheduler import RunScheduler
from demand.task_creation import TaskCreationError, TaskCreationService
from services.artifacts import write_artifact

#: 这些消息类型的“新消息”**不算真实进展**：
#: 它们的价值应体现在 Task 状态变化上；被拒绝时也不该延长 run。
#: 否则一条非法 START + ERROR 往返就能骗过 NO_PROGRESS。
_PROTOCOL_MESSAGE_TYPES = frozenset({
    "START", "SUBMIT", "VERIFY", "COMPLETE",
    "TASK_OFFER", "COUNTER_OFFER", "ACCEPT", "REJECT", "ERROR",
})

#: 只有这些才算**强进展**（能重置 NO_PROGRESS）。
#: 普通消息只是弱进展 —— 否则协调/解释会无限延长 run。
_STRONG_PROGRESS_REASONS = frozenset({
    "task_status", "workspace_write", "workspace_edit", "resource_create",
    "workspace_promote_drafts",
})

#: 只有这些动作是“只读/查询”；连续出现时给 Agent 一个动作事实。
#: 🔒 不包含 message_send / resource_handoff / 写入 —— 那些都会重置 streak。
_READ_ONLY_ACTIONS = frozenset({
    "workspace_read", "workspace_search", "public_query",
    "public_working_memory_read", "private_working_memory_read",
    "private_read", "private_list", "short_term_memory_read",
    "long_term_memory_search", "long_term_memory_read",
    "shared_context_read",
})


class DemandRunner:
    def __init__(
        self,
        runtime: Any,
        run: DemandRun,
        *,
        cognitive_budget: int = 16384,
        quiet: bool = False,
    ) -> None:
        self.rt = runtime
        self.run = run
        self.quiet = bool(quiet)
        self.cognitive_budget = int(cognitive_budget)
        self.bridge = SyncBridge()
        self.image_resolver = make_image_resolver(runtime)
        self.task: Any = None
        self.task_dispatcher: Any = None
        self._creation = TaskCreationService(
            runtime, run, max_agents=run.max_agents)
        #: Agent 私有草稿区（内存，随 run 结束销毁；不是项目 artifact）。
        self.drafts = DraftBuffer()
        self._done = False
        #: 上一 turn 的可观测事实（按 Agent）。下一轮 prompt 会显示它 ——
        #: 尤其是截断 / 预算裁剪 / zero-action，否则 Agent 无法自主调节。
        self._last_turn_by_agent: Dict[str, Dict[str, Any]] = {}
        #: 已经见过的消息签名：重复发送同一条消息不算“有意义的进展”。
        self._message_signatures: set = set()
        #: 最近的消息（给 MAX_TURNS / TIME_LIMIT 的阻塞报告用）。
        self._recent_messages: List[Dict[str, Any]] = []
        #: 最近一次真实进展发生在第几 turn（0 = 还没有）。
        self._last_progress_turn = 0
        #: 最近一次**强进展**发生在第几 turn（0 = 还没有）。
        self._last_strong_progress_turn = 0
        self._turn_index = 0
        #: 工作单元压力（截断 / 预算裁剪）的连续与累计计数。
        self._pressure_streak: Dict[str, int] = {}
        self._pressure_total: Dict[str, int] = {}
        self._pressure_last_turn: Dict[str, int] = {}
        #: 已经回喂过的 workspace_read 指纹（按 Agent），避免重复读同一版本。
        self._seen_reads: Dict[str, set] = {}
        #: 尚未回应的 REQUEST/QUESTION（按 Agent）。
        self._pending_requests: Dict[str, List[Dict[str, Any]]] = {}
        #: 每个 Agent 的“连续只读 turn 数”——只描述动作历史，不是墙钟。
        self._read_only_streak: Dict[str, int] = {}
        #: 每个 Agent 上次 turn 结束时的锁快照（用于显示锁变更事实）。
        self._last_lock_snapshot: Dict[str, Dict[str, str]] = {}
        #: 最近一次 turn 的模型原文（Front brief 解析用）。
        self._last_turn_content = ""
        #: 最近一次 task_create 失败原因（Creator 重试时可见）。
        self._last_create_error = ""

    # ------------------------------------------------------------
    def execute(self) -> Dict[str, Any]:
        started = time.monotonic()
        self.run.status = "INTAKE"
        self._write_run()
        self._event("run_started", run=self.run.to_dict())

        try:
            self._intake()
            if not self.run.root_task_id:
                self.run.status = "FAILED"
                self.run.exit_reason = "NO_TASK_CREATED"
                return self._finish()
            self._run_task(started)
        except Exception as exc:                              # noqa: BLE001
            self.run.status = "FAILED"
            self.run.exit_reason = f"{type(exc).__name__}: {exc}"
            self._event("run_error", error=self.run.exit_reason)
        finally:
            try:
                self.bridge.close()
            except Exception:                                 # noqa: BLE001
                pass
        return self._finish()

    # ------------------------------------------------------------
    def _intake(self) -> None:
        """
        V1 的入口拆成两个阶段：

        ① Front / 解读：只产出 brief，不碰架构、不建 Task；
        ② Creator / 规划：拿 brief + 锁规则产出 workspace_plan，
           再调用 task_create。

        同一个 Agent 可以承担两个阶段，但**不能在同一次 prompt 里
        既解读 Human Demand，又决定架构/分工**。
        """
        brief = self._intake_brief()
        self.run.brief = brief
        self._write_run()
        self._event("intake_brief", brief=brief)

        dispatcher = build_intake_dispatcher(self._create_task)
        self._last_create_error = ""
        for attempt in range(1, 4):
            if self.run.root_task_id:
                return
            prompt = build_intake_prompt(
                run=self.run, runtime=self.rt,
                front_agent=self.run.front_agent,
                budget_cap=self.cognitive_budget,
                budget_feedback=self._last_turn_by_agent.get(
                    self.run.front_agent),
                brief=brief,
                previous_error=self._last_create_error)
            outcome = self._turn(
                self.run.front_agent, prompt, dispatcher,
                task_id=None, initial_phase="intake",
                work_loop=False, follow_up=False,
                attempt=attempt)
            if self.run.root_task_id:
                self.task = self.rt.tasks.get(self.run.root_task_id)
                self._event("task_created", task_id=self.run.root_task_id)
                return
            if not outcome.ok:
                break
        self._event("intake_finished_without_task")

    def _intake_brief(self) -> Dict[str, Any]:
        """Front 阶段：只解读 Human Demand，不创建 Task。"""
        last_error = ""
        for attempt in range(1, 3):
            prompt = build_brief_prompt(
                run=self.run, front_agent=self.run.front_agent,
                previous_error=last_error)
            outcome = self._turn(
                self.run.front_agent, prompt, None,
                task_id=None, initial_phase="intake",
                work_loop=False, follow_up=False,
                attempt=attempt, execute=False)
            if not outcome.ok:
                last_error = outcome.error or "brief_turn_failed"
                continue
            brief, error = parse_brief(self._last_turn_content)
            if brief is not None:
                self._event(
                    "intake_brief_parsed", attempt=attempt, brief=brief)
                return brief
            last_error = error
        fallback = fallback_brief(self.run.demand)
        self._event(
            "intake_brief_fallback", error=last_error, brief=fallback)
        return fallback

    def _create_task(self, **kwargs: Any) -> Dict[str, Any]:
        try:
            result = self._creation.create(**kwargs)
        except Exception as exc:                              # noqa: BLE001
            self._last_create_error = f"{type(exc).__name__}: {exc}"
            raise
        if result.get("ok"):
            self.run.root_task_id = str(result.get("task_id") or "")
            self.run.workspace_id = str(result.get("workspace_id") or "")
            self.run.status = "RUNNING"
            self._write_run()
        else:
            self._last_create_error = str(
                result.get("message") or result.get("code") or "task_create 失败")
        return result

    # ------------------------------------------------------------
    def _run_task(self, started: float) -> None:
        task = self._status_task()
        if task is None:
            self.run.status = "FAILED"
            self.run.exit_reason = "TASK_NOT_FOUND"
            return
        self.task = task
        self.task_dispatcher = build_task_dispatcher(
            self.rt, task=task, bridge=self.bridge,
            update_plan=self._update_workspace_plan,
            record_checkpoint=self._record_plan_checkpoint,
            drafts=self.drafts,
            promote_drafts=self._promote_drafts)

        order: List[str] = []
        if task.contractor:
            order.append(task.contractor)
        if task.creator not in order:
            order.append(task.creator)
        for aid in task.participants:
            if aid not in order:
                order.append(aid)

        deadline = started + float(self.run.wall_limit_seconds)
        scheduler = RunScheduler(
            participants=order,
            run_turn=self._task_turn,
            task_status=lambda: self._status_string(),
            max_turns=self.run.max_turns,
            deadline=deadline,
        )
        result = scheduler.run()
        self.run.exit_reason = result.exit_reason
        self.run.turns = result.turns
        if result.status == "COMPLETED":
            self.run.status = "COMPLETED"
        elif result.exit_reason.startswith("TASK_"):
            self.run.status = "FAILED"
        else:
            self.run.status = "STOPPED"
        self._event(
            "run_scheduler_finished",
            exit_reason=result.exit_reason,
            status=result.status,
            turns=result.turns)

    def _task_turn(self, agent_id: str) -> TurnOutcome:
        inbox = drain(self.rt.communication, self.bridge, agent_id)
        task = self._status_task()
        if task is None:
            return TurnOutcome(
                agent_id=agent_id, ok=False, error="task disappeared")
        messages = inbox.get("received") or []
        self._collect_pending_requests(agent_id, messages)
        workspace_id = (
            task.workspace.workspace_id if task.workspace else "")
        current_locks = self._lock_snapshot(workspace_id)
        previous_locks = self._last_lock_snapshot.get(agent_id)
        lock_changes = (
            self._diff_locks(previous_locks, current_locks)
            if previous_locks is not None else [])
        held = sorted(
            path for path, holder in current_locks.items()
            if holder == agent_id)
        pending = self._pending_requests.get(agent_id) or []
        prompt = build_task_prompt(
            run=self.run, task=task, runtime=self.rt,
            agent_id=agent_id, inbox=messages,
            budget_cap=self.cognitive_budget,
            budget_feedback=self._last_turn_by_agent.get(agent_id),
            pending_requests=pending,
            lock_changes=lock_changes,
            read_only_streak=self._read_only_streak.get(agent_id, 0),
            held_locks=held,
            stalled_agents=(
                self._stalled_agents(task, current_locks, exclude=agent_id)
                if agent_id == task.creator else []),
            drafts=self.drafts.meta(agent_id))
        try:
            return self._turn(
                agent_id, prompt, self.task_dispatcher,
                task_id=task.task_id, initial_phase="decision",
                work_loop=True, follow_up=True, attempt=0)
        finally:
            # 🔒 记录**本 turn 结束后**的锁快照；下一轮只显示相对它的变化。
            self._last_lock_snapshot[agent_id] = self._lock_snapshot(
                workspace_id)

    # ------------------------------------------------------------
    def _update_workspace_plan(self, agent_id: str, *, plan: Any,
                               reason: str = "") -> Dict[str, Any]:
        """
        Creator 更新 `run.workspace_plan`。

        🔒 只允许 creator；复用 task_create 的同一套结构校验。
           这不是 Task Core 状态，也不是工作记忆。
        """
        task = self._status_task()
        if task is None:
            return {"ok": False, "code": "task_not_found",
                    "message": "Task 不存在，无法更新 workspace_plan"}
        if agent_id != task.creator:
            return {"ok": False, "code": "not_creator",
                    "message": ("只有 creator 可以更新 workspace_plan；"
                                f"你是 {agent_id}")}
        producer = task.contractor or task.creator
        try:
            validated = self._creation.validate_workspace_plan(
                plan, people=list(task.participants), producer=producer)
        except TaskCreationError as exc:
            return {"ok": False, "code": exc.code, "message": exc.message}
        old_phases = {
            str(item.get("name")): json.dumps(item, ensure_ascii=False,
                                               sort_keys=True)
            for item in ((self.run.workspace_plan or {}).get("phases") or [])
        }
        new_phases = {
            str(item.get("name")): json.dumps(item, ensure_ascii=False,
                                               sort_keys=True)
            for item in (validated.get("phases") or [])
        }
        checkpoints = dict(
            getattr(self.run, "plan_checkpoints", None) or {})
        for name in list(checkpoints):
            # phase 被删除 / 定义改变 ⇒ 旧检查点失效。
            if name not in new_phases or old_phases.get(name) != new_phases[name]:
                checkpoints.pop(name, None)
        self.run.plan_checkpoints = checkpoints or None
        self.run.workspace_plan = validated
        self._write_run()
        self._event(
            "workspace_plan_updated", agent_id=agent_id,
            plan=validated, reason=str(reason or ""))
        return {"ok": True, "workspace_plan": validated}

    def _record_plan_checkpoint(
        self, agent_id: str, *, phase: str, passed: bool,
        evidence: Optional[Dict[str, Any]] = None,
        note: str = "",
    ) -> Dict[str, Any]:
        """
        记录一个 phase 的检查结果。

        🔒 这是 Agent 自己的阶段检查记录，不是 Task 验收。
           检查方式由 Agent 决定（run_tool / read / 其他）；系统只做
           phase 存在性与可选 artifact evidence 的结构校验。
        """
        task = self._status_task()
        if task is None:
            return {"ok": False, "code": "task_not_found",
                    "message": "Task 不存在，无法记录 checkpoint"}
        if not task.is_participant(agent_id):
            return {"ok": False, "code": "not_participant",
                    "message": f"Agent {agent_id!r} 不是 Task participant"}
        phases = {
            str(item.get("name")): item
            for item in ((self.run.workspace_plan or {}).get("phases") or [])
        }
        if phase not in phases:
            return {"ok": False, "code": "unknown_phase",
                    "message": (f"workspace_plan 没有 phase {phase!r}；"
                                f"可用：{sorted(phases)}")}
        evidence_data = dict(evidence or {})
        path = str(evidence_data.get("path") or "").strip()
        if path:
            workspace_id = (
                task.workspace.workspace_id if task.workspace else "")
            try:
                got = self.rt.workspace_gateway.read(
                    workspace_id, "/" + path.lstrip("/"), agent_id)
            except Exception as exc:                          # noqa: BLE001
                return {"ok": False, "code": "evidence_error",
                        "message": f"{type(exc).__name__}: {exc}"}
            if not got.success:
                return {"ok": False, "code": "evidence_not_found",
                        "message": f"checkpoint evidence 指向不存在的 "
                                   f"artifact：{path!r}"}
            expected = evidence_data.get("revision")
            if expected is not None:
                actual = (got.result or {}).get("revision")
                try:
                    expected_int = int(expected)
                except (TypeError, ValueError):
                    return {"ok": False, "code": "bad_evidence_revision",
                            "message": "evidence.revision 必须是整数"}
                if actual is None or int(actual) != expected_int:
                    return {"ok": False, "code": "evidence_revision_mismatch",
                            "message": (f"evidence.revision={expected_int} "
                                        f"与当前 revision={actual} 不一致")}
        record = {
            "phase": phase,
            "passed": bool(passed),
            "by": agent_id,
            "at": utc_now_iso(),
            "note": str(note or "")[:2000],
            "evidence": evidence_data or None,
        }
        checkpoints = dict(getattr(self.run, "plan_checkpoints", None) or {})
        checkpoints[phase] = record
        self.run.plan_checkpoints = checkpoints
        self._write_run()
        self._event("workspace_plan_checkpoint", **record)
        return {"ok": True, "checkpoint": record}

    def _promote_drafts(
        self, agent_id: str, items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        把 Agent 的多个草稿一次落盘到 Task Workspace。

        🔒 内容来自私有草稿，不经过模型输出；仍然走 write_artifact，
           所以锁 / revision / audit / 参与者边界全部照旧。
        """
        task = self._status_task()
        if task is None:
            return {"ok": False, "summary": "Task 不存在",
                    "results": []}
        workspace_id = (
            task.workspace.workspace_id if task.workspace else "")
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
            result = {
                "draft_id": draft_id,
                "path": path,
                "ok": bool(raw.get("ok")),
                "code": raw.get("code"),
                "message": raw.get("message") or "",
            }
            for key in ("mode", "revision", "chars", "appended_chars"):
                if key in raw:
                    result[key] = raw[key]
            results.append(result)
        ok_count = sum(1 for item in results if item.get("ok"))
        ok = ok_count == len(results)
        failures = [
            f"{item.get('path')}: {item.get('message') or item.get('code')}"
            for item in results if not item.get("ok")
        ]
        summary = f"{ok_count}/{len(results)} 个文件 promotion 成功"
        if failures:
            summary += "；失败：" + " | ".join(failures[:5])
        return {"ok": ok, "summary": summary, "results": results}

    def _lock_snapshot(self, workspace_id: str) -> Dict[str, str]:
        if not workspace_id:
            return {}
        try:
            return dict(self.rt.workspace_gateway.lock_snapshot(workspace_id))
        except Exception:                                     # noqa: BLE001
            return {}

    @staticmethod
    def _next_read_only_streak(current: Any,
                               action_types: List[str]) -> int:
        if action_types and all(
                a in _READ_ONLY_ACTIONS for a in action_types):
            return int(current or 0) + 1
        return 0

    @staticmethod
    def _diff_locks(previous: Dict[str, str],
                    current: Dict[str, str]) -> List[Dict[str, Any]]:
        changes: List[Dict[str, Any]] = []
        for path in sorted(set(previous) | set(current)):
            old = previous.get(path, "none")
            new = current.get(path, "none")
            if old == new:
                continue
            changes.append({"path": path, "from": old, "to": new})
        return changes

    def _stalled_agents(
        self, task: Any, current_locks: Dict[str, str], *,
        exclude: str,
    ) -> List[Dict[str, Any]]:
        """
        给 creator 的**动作事实**：哪些其他 Agent 连续只读、持有什么锁、
        有多少未回应请求。不是指令，也不包含墙钟。
        """
        out: List[Dict[str, Any]] = []
        for aid in task.participants:
            if aid == exclude:
                continue
            streak = int(self._read_only_streak.get(aid, 0))
            if streak < 3:
                continue
            out.append({
                "agent_id": aid,
                "read_only_streak": streak,
                "held_locks": sorted(
                    path for path, holder in current_locks.items()
                    if holder == aid),
                "pending_requests": len(
                    self._pending_requests.get(aid) or []),
            })
        out.sort(key=lambda item: (-item["read_only_streak"],
                                   item["agent_id"]))
        return out

    # ------------------------------------------------------------
    def _turn(
        self,
        agent_id: str,
        prompt: str,
        dispatcher: Any,
        *,
        task_id: Optional[str],
        initial_phase: str,
        work_loop: bool,
        follow_up: bool,
        attempt: int,
        execute: bool = True,
    ) -> TurnOutcome:
        before = self._status_string()
        agent = None
        try:
            agent = self.rt.economy.agent(agent_id)
        except Exception:                                     # noqa: BLE001
            agent = None
        if agent is None or not getattr(agent, "has_brain", False):
            return TurnOutcome(
                agent_id=agent_id, ok=False, status_before=before,
                status_after=before, error=f"{agent_id} 没有可用的 Brain")

        try:
            result = agent.act(
                messages=prompt,
                dispatcher=dispatcher,
                execute=execute,
                task_id=task_id,
                # 🔒 Human 给的是**硬上限**，不是“请求值”；
                #    Agent 可以自己选择更低的偏好，或请求更高（会被 cap）。
                cognitive_budget_cap=self.cognitive_budget,
                call_purpose="cognition",
                experiment_run_id=self.run.run_id,
                follow_up_on_reads=follow_up,
                work_loop=work_loop,
                max_work_steps=2,
                image_resolver=self.image_resolver,
                initial_phase=initial_phase,
                seen_reads=self._seen_reads.setdefault(agent_id, set()),
                # 🔴 与生产实验对齐：信封坏了先修一次格式。
                repair_malformed=True,
            )
        except Exception as exc:                              # noqa: BLE001
            after = self._status_string()
            self._event(
                "turn_error", agent_id=agent_id, error=
                f"{type(exc).__name__}: {exc}", status=after)
            return TurnOutcome(
                agent_id=agent_id, ok=False, status_before=before,
                status_after=after, error=f"{type(exc).__name__}: {exc}")

        self._turn_index += 1
        segments = list(getattr(result, "segments", None) or [])
        self._last_turn_content = (
            str(getattr(segments[-1], "content", "") or "")
            if segments else "")
        tokens = sum(int(getattr(s, "tokens", 0) or 0) for s in segments)
        intents = list(getattr(result, "action_intents", None) or [])
        results = list(getattr(result, "action_results", None) or [])
        actions = len(intents)
        self.run.tokens += tokens
        self.run.status = self.run.status if self.run.status != "INTAKE" else "RUNNING"
        after = self._status_string()

        action_types: List[str] = []
        for intent in intents:
            action_types.append(str(getattr(intent, "action_type", "")))

        # 🔒 只描述**动作历史**：连续只有读/搜/查询的 turn 数。
        #    出现写/编辑/handoff/消息/计划更新等任何结构性动作就清零。
        self._read_only_streak[agent_id] = self._next_read_only_streak(
            self._read_only_streak.get(agent_id, 0), action_types)

        budget = self._budget_feedback(segments, initial_phase, agent_id)
        envelope_errors = self._record_envelope_errors(agent_id, segments)
        new_messages = self._record_messages(agent_id, intents, results)
        progress, progress_reason = self._progress_of(
            before, after, intents, results, new_messages)
        progress_reasons = {
            r for r in progress_reason.split(",") if r}
        strong_progress = (
            after != before
            or bool(progress_reasons & _STRONG_PROGRESS_REASONS))
        if progress:
            self._last_progress_turn = self._turn_index
        if strong_progress:
            self._last_strong_progress_turn = self._turn_index

        self._last_turn_by_agent[agent_id] = {
            "turn": self._turn_index,
            "phase": initial_phase,
            "status_before": before,
            "status_after": after,
            "tokens": tokens,
            "actions": actions,
            "action_types": action_types,
            "progress": progress,
            "progress_reason": progress_reason,
            "strong_progress": strong_progress,
            "budget": budget,
            "envelope_errors": envelope_errors,
        }
        self._event(
            "turn",
            agent_id=agent_id,
            phase=initial_phase,
            attempt=attempt,
            tokens=tokens,
            actions=actions,
            action_types=action_types,
            status_before=before,
            status_after=after,
            progress=progress,
            progress_reason=progress_reason,
            strong_progress=strong_progress,
            budget=budget,
            envelope_errors=len(envelope_errors),
        )
        self._clear_answered_requests(agent_id, intents, results, after)
        return TurnOutcome(
            agent_id=agent_id,
            ok=bool(segments),
            tokens=tokens,
            actions=actions,
            status_before=before,
            status_after=after,
            progress=progress,
            progress_reason=progress_reason,
            strong_progress=strong_progress,
            truncated=bool(budget.get("truncated")),
        )

    # ------------------------------------------------------------
    def _budget_feedback(self, segments: List[Any],
                         initial_phase: str,
                         agent_id: str = "") -> Dict[str, Any]:
        """把这一段（最后一次调用）的预算 / 截断事实变成下一轮可见的观测。"""
        last = segments[-1] if segments else None
        truncated = [s for s in segments if getattr(s, "is_truncated", False)]
        repaired_truncated = [
            s for s in segments if getattr(s, "repair_truncated", False)
        ]
        clipped_segments = [
            s for s in segments
            if getattr(s, "budget_clipped", None)
            in ("above_caller_limit", "above_max")
            or getattr(s, "original_budget_clipped", None)
            in ("above_caller_limit", "above_max")
        ]
        # 🔒 `finish_reason/truncated` 取**最后一个被截断的段**；
        #    `requested/actual/clipped` 取**最后一个被裁剪的段**。
        #    两者分开取，避免“截断段正常、裁剪段别的”时字段互相错配。
        source = (truncated[-1] if truncated
                  else repaired_truncated[-1] if repaired_truncated
                  else clipped_segments[-1] if clipped_segments
                  else last)
        budget_source = (clipped_segments[-1] if clipped_segments
                         else source)
        clipped = getattr(budget_source, "budget_clipped", None)
        if clipped is None:
            clipped = getattr(budget_source, "original_budget_clipped", None)
        # 🔴 2026-11-09：工作单元压力 = 输出撞上限（含修复前截断），
        #    或请求预算被压到 `above_*`。`below_min` 不是“工作太大”。
        kinds: List[str] = []
        if truncated or repaired_truncated:
            kinds.append("truncated")
        if clipped_segments:
            kinds.append("clipped")
        pressure = bool(kinds)
        if pressure:
            self._pressure_streak[agent_id] = \
                self._pressure_streak.get(agent_id, 0) + 1
            self._pressure_total[agent_id] = \
                self._pressure_total.get(agent_id, 0) + 1
            self._pressure_last_turn[agent_id] = self._turn_index
        else:
            self._pressure_streak[agent_id] = 0
        return {
            "phase": str(getattr(source, "phase", initial_phase)
                         or initial_phase),
            "finish_reason": getattr(source, "finish_reason", None),
            "output_status": getattr(source, "output_status", None),
            "cognitive_status": getattr(source, "cognitive_status", None),
            "truncated": bool(truncated),
            "truncated_segments": len(truncated),
            "repair_truncated": bool(repaired_truncated),
            "repair_truncated_segments": len(repaired_truncated),
            "requested": getattr(
                budget_source, "requested_cognitive_budget", None),
            "actual": getattr(budget_source, "actual_max_tokens", None),
            "clipped": clipped,
            "segments": len(segments),
            "pressure": pressure,
            "pressure_kinds": ",".join(kinds),
            "pressure_streak": self._pressure_streak.get(agent_id, 0),
            "pressure_total": self._pressure_total.get(agent_id, 0),
            "pressure_last_turn": self._pressure_last_turn.get(agent_id, 0),
        }

    def _record_envelope_errors(self, agent_id: str,
                                segments: List[Any]) -> List[Dict[str, Any]]:
        """
        把信封解析失败的**原始输出 head/tail** 落盘。

        🔒 之前只有一行 warning，无法回答「为什么解析失败」。
           这里只存有界片段，不存整段超长内容。
        """
        records: List[Dict[str, Any]] = []
        for seg in segments:
            decision = getattr(seg, "decision", None)
            error = str(getattr(decision, "envelope_error", "") or "")
            if not error:
                continue
            content = str(getattr(seg, "content", "") or "")
            record = {
                "agent_id": agent_id,
                "index": int(getattr(seg, "index", 0) or 0),
                "phase": str(getattr(seg, "phase", "") or ""),
                "error": error,
                "finish_reason": getattr(seg, "finish_reason", None),
                "output_status": getattr(seg, "output_status", None),
                "content_chars": len(content),
                "head": content[:1200],
                "tail": content[-1200:] if len(content) > 1200 else "",
            }
            records.append(record)
            self._event("envelope_error", **record)
        return records

    def _record_messages(self, agent_id: str, intents: List[Any],
                         results: List[Any]) -> List[int]:
        """
        记录成功发送的消息，并返回**首次出现**的消息对应的 intent id 集合。

        🔒 重复发送同一条消息不算进展 —— 否则「反复发同一句阻塞报告」
           会骗过 NO_PROGRESS。
        """
        new_ids: List[int] = []
        for intent, result in zip(intents, results):
            if result is None or not getattr(result, "success", False):
                continue
            if str(getattr(intent, "action_type", "")) != "message_send":
                continue
            params = dict(getattr(intent, "parameters", None) or {})
            to_agent = str(params.get("to") or "")
            message_type = str(params.get("message_type") or "TEXT")
            text = str(params.get("text") or "")
            payload = params.get("payload") or {}
            try:
                payload_text = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                payload_text = str(payload)
            signature = (agent_id, to_agent, message_type, text, payload_text)
            is_new = signature not in self._message_signatures
            self._message_signatures.add(signature)
            record = {
                "turn": self._turn_index,
                "from": agent_id,
                "to": to_agent,
                "type": message_type,
                "text": text[:400],
                "new": is_new,
            }
            self._recent_messages.append(record)
            self._recent_messages = self._recent_messages[-20:]
            if is_new:
                new_ids.append(id(intent))
        return new_ids

    def _progress_of(self, before: str, after: str, intents: List[Any],
                     results: List[Any], new_messages: List[int]) -> tuple:
        """
        判定这一 turn 是否有**真实进展**。

        🔒 判据刻意不包含 `workspace_read` / `run_tool`：
           它们是观察，不是状态改变。两个真实 run 都是被
           “反复读 + 反复跑工具”拖到 MAX_TURNS 的。
        """
        reasons: List[str] = []
        if after != before:
            reasons.append("task_status")
        new_message_ids = set(new_messages)
        for intent, result in zip(intents, results):
            if result is None:
                continue
            action_type = str(getattr(intent, "action_type", ""))
            # 批量 promotion 即使是 partial failure，只要有一个文件落盘，
            # 就是强进展；其余失败在 output.results/summary 里可见。
            if action_type == "workspace_promote_drafts":
                output = getattr(result, "output", None) or {}
                promoted = output.get("results") or []
                if any(isinstance(item, dict) and item.get("ok")
                       for item in promoted):
                    reasons.append("workspace_promote_drafts")
                continue
            if not getattr(result, "success", False):
                continue
            if action_type in ("workspace_write", "workspace_edit"):
                reasons.append(action_type)
            elif action_type == "resource_create":
                reasons.append("resource_create")
            elif action_type == "message_send" and id(intent) in new_message_ids:
                params = dict(getattr(intent, "parameters", None) or {})
                message_type = str(
                    params.get("message_type") or "TEXT").strip().upper()
                # 🔒 生命周期/协议消息的进展必须体现在 Task 状态上。
                #    非法 START / ERROR 往返不能算“新消息进展”。
                if message_type not in _PROTOCOL_MESSAGE_TYPES:
                    reasons.append("new_message")
        # 去重但保留第一次出现的顺序
        seen = set()
        unique = [r for r in reasons if not (r in seen or seen.add(r))]
        return bool(unique), ",".join(unique)

    # ------------------------------------------------------------
    def _collect_pending_requests(self, agent_id: str,
                                  messages: List[Any]) -> None:
        """
        记录收到的 REQUEST/QUESTION，直到 Agent 回应或采取结构动作。

        🔒 真实 run 里 E 明确请求 `parts/`，A 下一轮仍然继续读文件。
           这里把“未回应的请求”持续放到 prompt 顶部，而不是只在
           inbox 里出现一次。
        """
        pending = self._pending_requests.setdefault(agent_id, [])
        known = {str(p.get("message_id") or "") for p in pending}
        for message in messages:
            message_type = str(
                getattr(message, "message_type", "") or "").upper()
            if message_type not in ("REQUEST", "QUESTION"):
                continue
            if str(getattr(message, "to_agent", "") or "") != agent_id:
                continue
            message_id = str(getattr(message, "message_id", "") or "")
            if not message_id or message_id in known:
                continue
            text = str(getattr(message, "text", "") or "")
            if not text:
                payload = getattr(message, "payload", None) or {}
                text = str(payload.get("text") or payload)
            pending.append({
                "message_id": message_id,
                "from_agent": str(getattr(message, "from_agent", "") or ""),
                "message_type": message_type,
                "text": text[:600],
                "turn": self._turn_index,
            })
            known.add(message_id)
        # 只保留最近 3 条，避免 prompt 无限增长。
        self._pending_requests[agent_id] = pending[-3:]

    def _clear_answered_requests(self, agent_id: str, intents: List[Any],
                                 results: List[Any], after: str) -> None:
        """
        回应或采取结构动作后，清掉对应请求。

        🔒 判据是**动作**，不是模型说自己会做：
            · 给请求方发了消息；
            · 或创建/交接了目录（拆件动作）；
            · 或 Task 已进入 SUBMITTED/COMPLETED。
        """
        pending = self._pending_requests.get(agent_id) or []
        if not pending:
            return
        if after in ("SUBMITTED", "COMPLETED"):
            pending.clear()
            return

        answered_from = set()
        for intent, result in zip(intents, results):
            if result is None:
                continue
            action_type = str(getattr(intent, "action_type", "") or "")
            if action_type == "workspace_promote_drafts":
                output = getattr(result, "output", None) or {}
                if any(isinstance(item, dict) and item.get("ok")
                       for item in (output.get("results") or [])):
                    pending.clear()
                    return
                continue
            if not getattr(result, "success", False):
                continue
            if action_type == "message_send":
                params = dict(getattr(intent, "parameters", None) or {})
                target = str(params.get("to") or "")
                if target:
                    answered_from.add(target)
            elif action_type in ("resource_create", "resource_handoff"):
                # 这是明确的“开始拆/交接”动作；清掉所有待回应请求。
                pending.clear()
                return
            elif action_type == "workspace_promote_drafts":
                output = getattr(result, "output", None) or {}
                if any(isinstance(item, dict) and item.get("ok")
                       for item in (output.get("results") or [])):
                    pending.clear()
                    return
        if answered_from:
            pending[:] = [
                p for p in pending
                if str(p.get("from_agent") or "") not in answered_from
            ]

    # ------------------------------------------------------------
    def _status_task(self) -> Any:
        if not self.run.root_task_id:
            return None
        try:
            task = self.rt.tasks.get(self.run.root_task_id)
        except Exception:                                     # noqa: BLE001
            return None
        if task is not None:
            self.task = task
        return task

    def _status_string(self) -> str:
        if not self.run.root_task_id:
            return ""
        task = self._status_task()
        return str(getattr(task, "status", "") or "")

    # ------------------------------------------------------------
    def _finish(self) -> Dict[str, Any]:
        if self.run.ended_at:
            return self._read_result()
        self.run.ended_at = utc_now_iso()
        self.run.cost_ai = self._cost()
        task = self._status_task()
        deliverable = self._export_deliverable(task)
        blocking = self._blocking_report(task)
        result = {
            "run": self.run.to_dict(),
            "task": self._task_dict(task),
            "cost": {"tokens": self.run.tokens, "aic": self.run.cost_ai},
            "deliverable": str(deliverable) if deliverable else "",
            "files": self._file_list(deliverable),
            "blocking": blocking,
        }
        self._write_json("result.json", result)
        self._write_run()
        self._event("run_finished", **{
            "status": self.run.status,
            "exit_reason": self.run.exit_reason,
            "task_id": self.run.root_task_id,
            "tokens": self.run.tokens,
            "cost_ai": self.run.cost_ai,
        })
        if not self.quiet:
            print()
            print("=" * 68)
            print(f"  Demand run: {self.run.run_id}")
            print(f"  Task      : {self.run.root_task_id or '(none)'}")
            print(f"  状态      : {self.run.status} / {self.run.exit_reason}")
            print(f"  Tokens    : {self.run.tokens}")
            print(f"  Cost      : {self.run.cost_ai:.6f} AIC")
            print(f"  输出      : {self.run.run_dir}")
            if deliverable:
                print(f"  交付物    : {deliverable}")
            if self.run.status != "COMPLETED" and blocking:
                if blocking.get("waiting_for"):
                    print(f"  阻塞      : {blocking.get('waiting_for')}")
                if blocking.get("waiting_action"):
                    print(f"  等待动作  : {blocking.get('waiting_action')}")
                print(f"  最后进展  : turn "
                      f"{blocking.get('last_progress_turn') or 0}")
                for msg in (blocking.get("recent_messages") or [])[-3:]:
                    print(f"  最近消息  : {msg.get('from')} → "
                          f"{msg.get('to')} [{msg.get('type')}] "
                          f"{msg.get('text')}")
            print("=" * 68)
        return result

    def _task_dict(self, task: Any) -> Dict[str, Any]:
        if task is None:
            return {}
        acceptors = []
        try:
            acceptors = sorted(self.rt.task_execution.acceptors_of(task))
        except Exception:                                     # noqa: BLE001
            pass
        return {
            "task_id": task.task_id,
            "status": task.status,
            "creator": task.creator,
            "contractor": task.contractor,
            "participants": list(task.participants),
            "acceptors": acceptors,
            "workspace_id": task.workspace.workspace_id
            if task.workspace else "",
            "description": task.description,
        }

    def _blocking_report(self, task: Any) -> Dict[str, Any]:
        """
        结束时回答「卡在哪」—— 不是只说 `STOPPED / MAX_TURNS`。

        🔒 这里只读 Task Core / 本地观测，不推断 Agent 的动机。
        """
        if task is None:
            return {}
        status = str(getattr(task, "status", "") or "")
        contractor = str(getattr(task, "contractor", "") or "")
        creator = str(getattr(task, "creator", "") or "")
        acceptors = []
        try:
            acceptors = sorted(self.rt.task_execution.acceptors_of(task))
        except Exception:                                     # noqa: BLE001
            pass
        waiting_for = ""
        waiting_action = ""
        if status in ("CREATED", "ACCEPTED"):
            waiting_for = contractor or creator
            waiting_action = "START"
        elif status == "IN_PROGRESS":
            waiting_for = contractor
            waiting_action = "SUBMIT"
        elif status == "SUBMITTED":
            waiting_for = ", ".join(acceptors) or "（还没有验收人）"
            waiting_action = "VERIFY"
        elif status == "VERIFIED":
            waiting_for = creator
            if acceptors:
                waiting_for = f"{creator} 或 {', '.join(acceptors)}"
            waiting_action = "COMPLETE"
        return {
            "task_status": status,
            "waiting_for": waiting_for,
            "waiting_action": waiting_action,
            "acceptors": acceptors,
            "last_progress_turn": self._last_progress_turn,
            "last_strong_progress_turn": self._last_strong_progress_turn,
            "recent_messages": list(self._recent_messages[-5:]),
            "last_turns": {
                agent_id: {
                    key: value
                    for key, value in facts.items()
                    if key in ("turn", "phase", "status_after", "actions",
                               "action_types", "progress", "progress_reason",
                               "strong_progress", "budget",
                               "envelope_errors")
                }
                for agent_id, facts in self._last_turn_by_agent.items()
            },
        }

    def _cost(self) -> float:
        try:
            with self.rt.economy.db.transaction(write=False) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_ai), 0) FROM llm_calls "
                    "WHERE experiment_run_id = ?",
                    (self.run.run_id,)).fetchone()
            return float((row or [0])[0] or 0.0)
        except Exception:                                     # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------
    def _export_deliverable(self, task: Any) -> Optional[Path]:
        if task is None or not task.workspace:
            return None
        ws_id = task.workspace.workspace_id
        reader = task.creator
        root = Path(self.run.run_dir) / "deliverable"
        root.mkdir(parents=True, exist_ok=True)
        gateway = self.rt.workspace_gateway

        def walk(path: str) -> None:
            listed = gateway.list(ws_id, path, reader)
            if not listed.success:
                return
            for entry in (listed.result or {}).get("entries", []) or []:
                name = str(entry.get("name") or "")
                if not name:
                    continue
                child = f"{path.rstrip('/')}/{name}" if path != "/" else f"/{name}"
                if str(entry.get("type") or "") == "directory":
                    walk(child)
                    continue
                got = gateway.read(ws_id, child, reader)
                if not got.success:
                    continue
                content = str((got.result or {}).get("content") or "")
                target = root / child.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        try:
            walk("/")
        except Exception:                                     # noqa: BLE001
            return root
        return root

    def _file_list(self, deliverable: Optional[Path]) -> List[str]:
        if deliverable is None or not deliverable.exists():
            return []
        return sorted(
            str(p.relative_to(deliverable))
            for p in deliverable.rglob("*") if p.is_file())

    # ------------------------------------------------------------
    def _write_run(self) -> None:
        self._write_json("run.json", self.run.to_dict())

    def _write_json(self, name: str, payload: Any) -> None:
        path = Path(self.run.run_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    def _read_result(self) -> Dict[str, Any]:
        try:
            return json.loads(
                (Path(self.run.run_dir) / "result.json").read_text(
                    encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            return {}

    def _event(self, event_type: str, **payload: Any) -> None:
        row = {"ts": utc_now_iso(), "event": event_type}
        row.update(payload)
        path = Path(self.run.run_dir) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if not self.quiet and event_type == "turn":
            bits = [f"[{payload.get('agent_id')}]",
                    f"tokens={payload.get('tokens')}",
                    f"actions={payload.get('actions')}",
                    f"status={payload.get('status_before')}"
                    f"→{payload.get('status_after')}"]
            if payload.get("action_types"):
                bits.append("types=" + ",".join(payload["action_types"]))
            if (payload.get("budget") or {}).get("truncated"):
                bits.append("TRUNCATED")
            if payload.get("progress_reason"):
                bits.append("progress=" + str(payload["progress_reason"]))
            print("  " + "  ".join(bits))
