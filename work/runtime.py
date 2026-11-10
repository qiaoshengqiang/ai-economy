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

from actions.read_payload import render_read_results
from work.actions import (
    build_cognition_dispatcher, build_work_dispatcher,
)
from work.drafts import TaskDraftStore
from work.images import make_image_resolver
from work.lifecycle import WorkLifecycle, WorkLifecycleError
from work.prompt import build_cognition_prompt, build_work_prompt
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
        max_cognition_steps: int = 2,
        cognitive_budget: int = 65536,
        event_sink: Any = None,
        should_stop: Optional[Callable[[], bool]] = None,
        round_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        if task.mode != TaskMode.SOLO:
            raise ValueError(
                f"SoloWorkRuntime 只接受 SOLO Task；"
                f"{task.task_id} 是 {task.mode}")
        self.rt = runtime
        self.task = task
        self.brief = brief
        self.max_turns = max(1, int(max_turns))
        self.max_cognition_steps = max(1, int(max_cognition_steps))
        self.cognitive_budget = max(1, int(cognitive_budget))
        self.event_sink = event_sink
        self.should_stop = should_stop or (lambda: False)
        self.round_sink = round_sink
        #: Playwright 等工具产出的截图 → image_url content part。
        self.image_resolver = make_image_resolver(runtime)
        #: 统计整文件 replace 次数，用来提示 Agent 改用增量编辑。
        self._write_counts: Dict[str, int] = {}
        #: 启动时生成的任务认知报告；注入后续所有 Work prompt。
        self._cognition_report = ""
        self._cognition_tokens = 0
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
            # 先做任务认知：环境 / 需求 / 方案 / 风险。
            if not self._cognition_report:
                self._cognition_report = self._run_cognition_phase(actor)
                result.tokens += self._cognition_tokens
            if self._stopped():
                result.error = "任务已被停止"
                result.status = task.status
                return result
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
        # 返工后的环境可能已经变化，重新做一次任务认知。
        self._cognition_report = ""
        self._cognition_tokens = 0
        self.lifecycle.reopen(self.task, actor=actor, reason=reason)
        self._event("work_reopened", agent_id=actor, reason=reason)
        return self.run()

    # ------------------------------------------------------------
    def _run_cognition_phase(self, actor: str) -> str:
        """启动前的任务认知：读环境、想方案、输出完整报告。"""
        agent = self._agent(actor)
        if agent is None or not getattr(agent, "has_brain", False):
            return ""
        brain = getattr(agent, "brain", None)
        setter = getattr(brain, "set_thinking", None)
        if callable(setter):
            try:
                setter(True, "max", phase="cognition")
            except Exception:                                 # noqa: BLE001
                pass
        budget_setter = getattr(brain, "set_cognitive_budget", None)
        if callable(budget_setter):
            try:
                budget_setter(32768, phase="cognition")
            except Exception:                                 # noqa: BLE001
                pass

        try:
            dispatcher = build_cognition_dispatcher(
                self.rt, task=self.task, drafts=self.drafts)
            catalog = []
            runner = getattr(self.rt, "tool_runner", None)
            catalog_fn = getattr(runner, "catalog", None)
            if callable(catalog_fn):
                catalog = catalog_fn()
            context = self._context(actor)
            messages = [{
                "role": "user",
                "content": build_cognition_prompt(
                    context,
                    task_description=self.task.description,
                    runtime=self.rt,
                    tool_catalog=catalog,
                    max_steps=self.max_cognition_steps,
                ),
            }]
            seen_reads: set = set()
            segments: List[Any] = []
            tokens = 0
            report = ""
            last_content = ""

            for _step in range(1, self.max_cognition_steps + 1):
                if self._stopped():
                    break
                run = agent.act(
                    messages=messages,
                    dispatcher=dispatcher,
                    execute=True,
                    task_id=self.task.task_id,
                    cognitive_budget_cap=self.cognitive_budget,
                    call_purpose="work",
                    follow_up_on_reads=False,
                    work_loop=False,
                    max_work_steps=0,
                    initial_phase="cognition",
                    image_resolver=self.image_resolver,
                    seen_reads=seen_reads,
                    repair_malformed=True,
                )
                segs = list(getattr(run, "segments", None) or [])
                segments.extend(segs)
                tokens += sum(
                    int(getattr(s, "tokens", 0) or 0) for s in segs)
                last_content = str(
                    getattr(segs[-1], "content", "") or "") if segs else ""
                intents = list(getattr(run, "action_intents", None) or [])
                results = list(getattr(run, "action_results", None) or [])
                if not intents:
                    report = last_content
                    break
                feedback = render_read_results(
                    list(zip(intents, results)),
                    image_resolver=self.image_resolver,
                    seen_reads=seen_reads,
                )
                if not feedback:
                    break
                messages.append({
                    "role": "assistant", "content": last_content})
                messages.append({"role": "user", "content": feedback})

            if not report:
                messages.append({
                    "role": "assistant", "content": last_content})
                messages.append({
                    "role": "user",
                    "content": (
                        "现在输出完整的任务认知报告，只输出文字，"
                        "不要输出 JSON 动作。"),
                })
                run = agent.act(
                    messages=messages,
                    dispatcher=dispatcher,
                    execute=False,
                    task_id=self.task.task_id,
                    cognitive_budget_cap=self.cognitive_budget,
                    call_purpose="work",
                    follow_up_on_reads=False,
                    work_loop=False,
                    max_work_steps=0,
                    initial_phase="cognition",
                    image_resolver=self.image_resolver,
                    seen_reads=seen_reads,
                    repair_malformed=True,
                )
                segs = list(getattr(run, "segments", None) or [])
                segments.extend(segs)
                tokens += sum(
                    int(getattr(s, "tokens", 0) or 0) for s in segs)
                report = str(
                    getattr(segs[-1], "content", "") or "") if segs else ""

            self._cognition_tokens = tokens
            report = report.strip()
            self._emit_round({
                "task_id": self.task.task_id,
                "round": 0,
                "cognition": True,
                "status_before": self.task.status,
                "status_after": self.task.status,
                "summary": report,
                "tokens": tokens,
                "segments": [
                    {
                        "phase": str(getattr(seg, "phase", "") or ""),
                        "content": str(getattr(seg, "content", "") or ""),
                        "decision": (
                            seg.decision.to_dict()
                            if getattr(seg, "decision", None) else None),
                        "actions": [
                            intent.to_dict()
                            for intent in (
                                getattr(seg, "action_intents", None) or [])
                        ],
                        "results": [
                            result_.to_dict() if result_ else None
                            for result_ in (
                                getattr(seg, "action_results", None) or [])
                        ],
                        "tokens": getattr(seg, "tokens", None),
                        "finish_reason": getattr(seg, "finish_reason", None),
                        "truncated": bool(
                            getattr(seg, "is_truncated", False)),
                    }
                    for seg in segments
                ],
            })
            self._event(
                "work_cognition", agent_id=actor,
                tokens=tokens, report_chars=len(report))
            return report
        except Exception as exc:                              # noqa: BLE001
            self._event(
                "work_cognition_error", agent_id=actor,
                error=f"{type(exc).__name__}: {exc}")
            return ""

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
            include_public_memory=False,
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
                    # 一轮 = decision → work（原始回喂）→ summary。
                    initial_phase="decision",
                    summary_phase=True,
                    image_resolver=self.image_resolver,
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
            action_feedback = []
            for intent, action_result in zip(intents, results):
                params = getattr(intent, "parameters", None) or {}
                metadata = getattr(action_result, "metadata", None) or {}
                action_feedback.append({
                    "action": str(getattr(intent, "action_type", "")),
                    "target": str(
                        params.get("path")
                        or params.get("query")
                        or params.get("item_id")
                        or ""),
                    "ok": bool(getattr(action_result, "success", False)),
                    "executed": action_result is not None,
                    "code": str(
                        metadata.get("code") or "")
                    if isinstance(metadata, dict) else "",
                    "error": str(getattr(action_result, "error", "") or ""),
                })
            for intent in intents:
                if getattr(intent, "action_type", "") != "workspace_write":
                    continue
                params = getattr(intent, "parameters", None) or {}
                path = str(params.get("path") or "")
                mode = str(params.get("write_mode") or "").strip().lower()
                if path and mode == "replace":
                    self._write_counts[path] = (
                        self._write_counts.get(path, 0) + 1)
            rewrites = {
                path: count
                for path, count in self._write_counts.items()
                if count > 1
            }
            status_after = task.status
            self._event(
                "work_turn", turn=turn, agent_id=actor,
                tokens=tokens, action_types=action_types,
                status_before=result.status, status_after=status_after)
            last_seg = segments[-1] if segments else None
            last_decision = getattr(last_seg, "decision", None)
            last_turn_facts = {
                "status_before": result.status,
                "status_after": status_after,
                "action_types": action_types,
                "decision_kind": str(
                    getattr(last_decision, "kind", "") or ""),
                "envelope_error": str(
                    getattr(last_decision, "envelope_error", "") or ""),
                "tokens": tokens,
                "truncated": any(
                    bool(getattr(s, "is_truncated", False))
                    for s in segments),
                "thinking": getattr(run, "thinking", None),
                "cognitive_budget": getattr(
                    getattr(agent, "brain", None),
                    "last_cognitive_budget", None),
                "rewrites": rewrites,
                "action_feedback": action_feedback,
                "round_summary": str(getattr(run, "summary", "") or ""),
            }
            self._emit_round({
                "task_id": task.task_id,
                "round": turn,
                "status_before": result.status,
                "status_after": status_after,
                "summary": str(getattr(run, "summary", "") or ""),
                "tokens": tokens,
                "thinking": getattr(run, "thinking", None),
                "cognitive_budget": getattr(
                    getattr(agent, "brain", None),
                    "last_cognitive_budget", None),
                "segments": [
                    {
                        "phase": str(getattr(seg, "phase", "") or ""),
                        "content": str(getattr(seg, "content", "") or ""),
                        "decision": (
                            seg.decision.to_dict()
                            if getattr(seg, "decision", None) else None),
                        "actions": [
                            intent.to_dict()
                            for intent in (
                                getattr(seg, "action_intents", None) or [])
                        ],
                        "results": [
                            result_.to_dict() if result_ else None
                            for result_ in (
                                getattr(seg, "action_results", None) or [])
                        ],
                        "tokens": getattr(seg, "tokens", None),
                        "finish_reason": getattr(
                            seg, "finish_reason", None),
                        "truncated": bool(
                            getattr(seg, "is_truncated", False)),
                    }
                    for seg in segments
                ],
            })

            if status_after == TaskStatus.COMPLETED:
                result.status = status_after
                result.summary = self._summary_from(
                    results, segments) or task.note or "任务完成"
                return result

            if not intents:
                empty_streak += 1
                if empty_streak >= 2:
                    last_content = str(
                        getattr(last_seg, "content", "") or "")
                    recovered = self._force_action(
                        actor, agent, dispatcher, result,
                        last_content=last_content, turn=turn)
                    if recovered and task.status == TaskStatus.COMPLETED:
                        result.status = task.status
                        result.summary = (
                            result.summary or task.note or "任务完成")
                        return result
                    if recovered:
                        empty_streak = 0
                        continue
                    result.status = status_after
                    result.error = "连续两轮没有动作（恢复提示也未产生动作）"
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
            public_memory_facts="",
            draft_facts=self._draft_facts(actor),
            cognition_report=self._cognition_report,
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

    def _force_action(
        self,
        actor: str,
        agent: Any,
        dispatcher: Any,
        result: WorkResult,
        *,
        last_content: str,
        turn: int,
    ) -> bool:
        """连续两轮没动作时，用最小恢复提示再要一次明确动作。"""
        if self._stopped():
            return False
        context = self._context(actor)
        prompt = build_work_prompt(
            context,
            task_description=self.task.description,
            runtime=self.rt,
            extra_instruction=(
                "\n========== 恢复提示 ==========\n"
                "你连续两轮没有输出任何动作（只回了文字或格式不可解析）。\n"
                "不要再只回复文字。\n"
                "- 如果工作已经完成，必须调用 work_complete；\n"
                "- 如果还没完成，给出一个具体动作继续。\n\n"
                "你上一轮的文字（截断）：\n"
                f"{last_content[:1200]}\n"
            ),
        )
        try:
            run = agent.act(
                messages=prompt,
                dispatcher=dispatcher,
                execute=True,
                task_id=self.task.task_id,
                cognitive_budget_cap=self.cognitive_budget,
                call_purpose="work",
                experiment_run_id=getattr(
                    self.rt, "experiment_run_id", None),
                follow_up_on_reads=False,
                work_loop=False,
                max_work_steps=0,
                initial_phase="decision",
                image_resolver=self.image_resolver,
                seen_reads=set(),
                repair_malformed=True,
            )
        except Exception as exc:                              # noqa: BLE001
            self._event(
                "work_recovery_error",
                turn=turn, error=f"{type(exc).__name__}: {exc}")
            return False

        segments = list(getattr(run, "segments", None) or [])
        intents = list(getattr(run, "action_intents", None) or [])
        results = list(getattr(run, "action_results", None) or [])
        result.tokens += sum(
            int(getattr(s, "tokens", 0) or 0) for s in segments)
        result.actions += len(intents)
        self._event(
            "work_recovery_turn", turn=turn,
            action_types=[
                str(getattr(i, "action_type", "")) for i in intents])
        if self.task.status == TaskStatus.COMPLETED:
            result.summary = self._summary_from(
                results, segments) or self.task.note or "任务完成"
        return bool(intents) or self.task.status == TaskStatus.COMPLETED

    def _emit_round(self, payload: Dict[str, Any]) -> None:
        if self.round_sink is None:
            return
        try:
            self.round_sink(payload)
        except Exception:                                     # noqa: BLE001
            # UI 日志失败不能破坏实际工作。
            pass

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
