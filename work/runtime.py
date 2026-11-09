"""
SoloWorkRuntime —— Work 层最小执行循环
========================================

目标：让一个 Solo Agent 在**精简上下文**里完成 Task，
而不是每轮注入 Life 层身份 / 经济 / 消息 / 通用验收仪式。

    Front Agent
        ↓ Brief
    Team Lead（= Solo Agent）
        ↓ 创建 SOLO Task
    SoloWorkRuntime
        ├─ 最小 Work prompt
        ├─ Work 动作集合（无消息 / 无经济）
        ├─ Task-scoped memory / persistent draft
        └─ work_complete → COMPLETED

🔒 Front 永不创建 Task；本 runtime 也不创建 Task —— Task 由 Team Lead
   通过 TaskCreationService 创建后传入。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from core.task import Task, TaskMode, TaskStatus
from core.task_execution import TaskResultRef
from services.artifacts import write_artifact

from work.actions import build_work_dispatcher
from work.drafts import TaskDraftStore
from work.lifecycle import WorkLifecycle, WorkLifecycleError
from work.prompt import build_work_prompt
from work.roles import WorkContext, WorkResult, WorkRole


class SoloWorkRuntime:
    """一个 Solo Task 的最小执行循环。"""

    def __init__(
        self,
        runtime: Any,
        task: Task,
        *,
        brief: Optional[Dict[str, Any]] = None,
        drafts: Any = None,
        max_turns: int = 12,
        cognitive_budget: int = 32768,
        event_sink: Any = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        if task.mode != TaskMode.SOLO:
            raise ValueError(
                f"SoloWorkRuntime 只接受 SOLO Task；"
                f"{task.task_id} 是 {task.mode}")
        self.rt = runtime
        self.task = task
        self.brief = brief
        self.max_turns = max(1, int(max_turns))
        self.cognitive_budget = max(1, int(cognitive_budget))
        self.event_sink = event_sink
        self.should_stop = should_stop or (lambda: False)
        self.lifecycle = WorkLifecycle(runtime.tasks)
        self.drafts = drafts or TaskDraftStore(
            task_id=task.task_id,
            workspace_of=self._workspace_of,
        )

    # ------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------
    def run(self) -> WorkResult:
        task = self.task
        actor = task.contractor or task.creator
        result = WorkResult(
            task_id=task.task_id,
            status=task.status,
            iteration=task.iteration,
        )

        try:
            if self._stopped():
                result.error = "任务已被停止"
                result.status = task.status
                return result
            if task.status == TaskStatus.COMPLETED:
                result.summary = task.note or "任务已经处于 COMPLETED"
                return result
            if task.status in (TaskStatus.CREATED, TaskStatus.ACCEPTED):
                self.lifecycle.start(task, actor=actor)
            if task.status not in (
                TaskStatus.IN_PROGRESS,
                TaskStatus.SUBMITTED,
                TaskStatus.VERIFIED,
            ):
                result.error = (
                    f"Task 当前是 {task.status}，不能开始 Work run")
                result.status = task.status
                return result

            result.status = task.status
            result = self._loop(actor, result)
        except Exception as exc:                              # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = task.status
            self._event("work_run_error", agent_id=actor,
                        error=result.error)
        return result

    def reopen_and_run(self, *, reason: str = "") -> WorkResult:
        """Human 通过 Front 要求返工后，由 Task Creator 重新打开。"""
        actor = self.task.creator
        self.lifecycle.reopen(self.task, actor=actor, reason=reason)
        self._event("work_reopened", agent_id=actor, reason=reason)
        return self.run()

    # ------------------------------------------------------------
    def _loop(self, actor: str, result: WorkResult) -> WorkResult:
        task = self.task
        agent = self._agent(actor)
        if agent is None or not getattr(agent, "has_brain", False):
            result.error = f"{actor} 没有可用的 Brain"
            result.status = task.status
            return result

        dispatcher = build_work_dispatcher(
            self.rt,
            task=task,
            drafts=self.drafts,
            promote_drafts=self._promote_drafts,
            complete_fn=self._complete_fn,
        )
        seen_reads: set = set()
        empty_streak = 0
        last_turn_facts: Optional[Dict[str, Any]] = None

        for turn in range(1, self.max_turns + 1):
            if self._stopped():
                result.status = task.status
                result.error = "任务已被停止"
                return result
            context = self._context(actor)
            prompt = build_work_prompt(
                context,
                task_description=task.description,
                runtime=self.rt,
                last_turn=last_turn_facts,
            )
            try:
                run = agent.act(
                    messages=prompt,
                    dispatcher=dispatcher,
                    execute=True,
                    task_id=task.task_id,
                    cognitive_budget_cap=self.cognitive_budget,
                    call_purpose="work",
                    experiment_run_id=getattr(self.rt, "experiment_run_id", None),
                    follow_up_on_reads=True,
                    work_loop=True,
                    max_work_steps=2,
                    initial_phase="work",
                    seen_reads=seen_reads,
                    repair_malformed=True,
                )
            except Exception as exc:                          # noqa: BLE001
                result.error = f"{type(exc).__name__}: {exc}"
                result.status = task.status
                self._event("work_turn_error", turn=turn,
                            agent_id=actor, error=result.error)
                break

            segments = list(getattr(run, "segments", None) or [])
            intents = list(getattr(run, "action_intents", None) or [])
            results = list(getattr(run, "action_results", None) or [])
            tokens = sum(
                int(getattr(s, "tokens", 0) or 0) for s in segments)
            result.turns = turn
            result.tokens += tokens
            result.actions += len(intents)

            action_types = [
                str(getattr(i, "action_type", "")) for i in intents]
            status_after = task.status
            self._event(
                "work_turn", turn=turn, agent_id=actor,
                tokens=tokens, action_types=action_types,
                status_before=result.status, status_after=status_after)
            last_turn_facts = {
                "status_before": result.status,
                "status_after": status_after,
                "action_types": action_types,
                "tokens": tokens,
                "truncated": any(
                    bool(getattr(s, "is_truncated", False))
                    for s in segments),
            }

            if status_after == TaskStatus.COMPLETED:
                result.status = status_after
                result.summary = self._summary_from(
                    results, segments) or task.note or "任务完成"
                return result

            if not intents:
                empty_streak += 1
                if empty_streak >= 2:
                    result.status = status_after
                    result.error = "连续两轮没有动作"
                    break
            else:
                empty_streak = 0

        result.status = task.status
        if not result.error and task.status != TaskStatus.COMPLETED:
            result.error = f"达到 max_turns={self.max_turns}，任务尚未完成"
        return result

    # ------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------
    def _context(self, actor: str) -> WorkContext:
        task = self.task
        workspace_id = task.workspace_id or ""
        return WorkContext(
            task_id=task.task_id,
            agent_id=actor,
            role=WorkRole.SOLO,
            iteration=task.iteration,
            brief=self.brief,
            workspace_id=workspace_id,
            acceptance_criteria=self._acceptance_criteria(),
            memory_facts=self._memory_facts(actor),
            public_memory_facts=self._public_memory_facts(actor),
            draft_facts=self._draft_facts(actor),
        )

    def _public_memory_facts(self, actor: str) -> str:
        try:
            return self.rt.public_working_memory.render(
                self.task.task_id, actor)
        except Exception:                                     # noqa: BLE001
            return ""

    def _acceptance_criteria(self) -> List[str]:
        try:
            current = self.rt.alignment.current(self.task.task_id)
            criteria = getattr(current.context, "acceptance_criteria", ())
            return [str(item) for item in criteria]
        except Exception:                                     # noqa: BLE001
            return []

    def _memory_facts(self, actor: str) -> str:
        lines: List[str] = []
        try:
            working = self.rt.private_working_memory.render(
                actor, self.task.task_id)
            if working:
                lines.append(working)
        except Exception:                                     # noqa: BLE001
            pass
        try:
            short = self.rt.private_short_term_memory.render(actor)
            if short:
                lines.append("短期记忆（跨任务）：\n" + short)
        except Exception:                                     # noqa: BLE001
            pass
        try:
            long = self.rt.private_long_term_memory.render_top(actor)
            if long:
                lines.append("长期记忆（跨任务）：\n" + long)
        except Exception:                                     # noqa: BLE001
            pass
        return "\n\n".join(lines)

    def _draft_facts(self, actor: str) -> str:
        try:
            payload = self.drafts.list(actor)
            drafts = payload.get("drafts") or []
            return str(drafts) if drafts else ""
        except Exception:                                     # noqa: BLE001
            return ""

    # ------------------------------------------------------------
    # 注入给动作层的回调
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
            ref = TaskResultRef(
                result_ref=result_ref,
                result_type=result_type or "artifact",
                summary=summary,
                metadata=dict(metadata or {}),
            )
            return self.lifecycle.complete_solo(
                self.task, actor=actor, result=ref, feedback=feedback)
        except (WorkLifecycleError, ValueError) as exc:
            return {"ok": False, "code": "solo_complete_rejected",
                    "message": str(exc)}

    def _promote_drafts(
        self, agent_id: str, items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
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
        failures = [
            f"{item.get('path')}: {item.get('message') or item.get('code')}"
            for item in results if not item.get("ok")
        ]
        summary = f"{ok_count}/{len(results)} 个文件 promotion 成功"
        if failures:
            summary += "；失败：" + " | ".join(failures[:5])
        return {
            "ok": ok_count == len(results),
            "summary": summary,
            "results": results,
        }

    # ------------------------------------------------------------
    def _agent(self, agent_id: str) -> Any:
        try:
            return self.rt.economy.agent(agent_id)
        except Exception:                                     # noqa: BLE001
            return None

    def _workspace_of(self, agent_id: str) -> Any:
        agent = self._agent(agent_id)
        return getattr(agent, "workspace", None)

    def _stopped(self) -> bool:
        try:
            return bool(self.should_stop())
        except Exception:                                     # noqa: BLE001
            return False

    def _event(self, name: str, **payload: Any) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(name, payload)
        except Exception:                                     # noqa: BLE001
            pass

    @staticmethod
    def _summary_from(results: List[Any], segments: List[Any]) -> str:
        for result in results:
            output = getattr(result, "output", None) or {}
            if isinstance(output, dict) and output.get("summary"):
                return str(output["summary"])
        if segments:
            return str(getattr(segments[-1], "content", "") or "")[:1000]
        return ""
