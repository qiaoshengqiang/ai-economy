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
from demand.images import make_image_resolver
from demand.models import DemandRun, TurnOutcome
from demand.prompt import build_intake_prompt, build_task_prompt
from demand.scheduler import RunScheduler
from demand.task_creation import TaskCreationService


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
        self._done = False

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
        dispatcher = build_intake_dispatcher(self._create_task)
        for attempt in range(1, 4):
            if self.run.root_task_id:
                return
            prompt = build_intake_prompt(
                run=self.run, runtime=self.rt,
                front_agent=self.run.front_agent)
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

    def _create_task(self, **kwargs: Any) -> Dict[str, Any]:
        result = self._creation.create(**kwargs)
        if result.get("ok"):
            self.run.root_task_id = str(result.get("task_id") or "")
            self.run.workspace_id = str(result.get("workspace_id") or "")
            self.run.status = "RUNNING"
            self._write_run()
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
            self.rt, task=task, bridge=self.bridge)

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
        task = self._status_task()
        if task is None:
            return TurnOutcome(
                agent_id=agent_id, ok=False, error="task disappeared")
        inbox = drain(self.rt.communication, self.bridge, agent_id)
        prompt = build_task_prompt(
            run=self.run, task=task, runtime=self.rt,
            agent_id=agent_id, inbox=inbox.get("received") or [])
        return self._turn(
            agent_id, prompt, self.task_dispatcher,
            task_id=task.task_id, initial_phase="decision",
            work_loop=True, follow_up=True, attempt=0)

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
                task_id=task_id,
                requested_cognitive_budget=self.cognitive_budget,
                call_purpose="cognition",
                experiment_run_id=self.run.run_id,
                follow_up_on_reads=follow_up,
                work_loop=work_loop,
                max_work_steps=2,
                image_resolver=self.image_resolver,
                initial_phase=initial_phase,
            )
        except Exception as exc:                              # noqa: BLE001
            after = self._status_string()
            self._event(
                "turn_error", agent_id=agent_id, error=
                f"{type(exc).__name__}: {exc}", status=after)
            return TurnOutcome(
                agent_id=agent_id, ok=False, status_before=before,
                status_after=after, error=f"{type(exc).__name__}: {exc}")

        segments = list(getattr(result, "segments", None) or [])
        tokens = sum(int(getattr(s, "tokens", 0) or 0) for s in segments)
        actions = len(getattr(result, "action_intents", None) or [])
        self.run.tokens += tokens
        self.run.status = self.run.status if self.run.status != "INTAKE" else "RUNNING"
        after = self._status_string()

        action_types: List[str] = []
        for intent in (getattr(result, "action_intents", None) or []):
            action_types.append(str(getattr(intent, "action_type", "")))
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
        )
        return TurnOutcome(
            agent_id=agent_id,
            ok=bool(segments),
            tokens=tokens,
            actions=actions,
            status_before=before,
            status_after=after,
        )

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
        result = {
            "run": self.run.to_dict(),
            "task": self._task_dict(task),
            "cost": {"tokens": self.run.tokens, "aic": self.run.cost_ai},
            "deliverable": str(deliverable) if deliverable else "",
            "files": self._file_list(deliverable),
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
            print("  " + "  ".join(bits))
