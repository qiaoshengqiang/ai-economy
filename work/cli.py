"""
Work CLI —— Human → Front → Solo Task → Work
=============================================

用法：

    ./dev.sh work --demand "写一个 hello.txt"
    ./dev.sh work --file /tmp/prompt.txt --lead agent_b

流程：

    Human 输入
      ↓
    Front Agent（direct_reply / handoff / rework / follow_up / clarify）
      ↓ handoff
    Team Lead（默认选择一个非 Front 的 Agent）
      ↓ 创建 SOLO Task
    SoloWorkRuntime（精简 Work prompt / 无消息 / 无经济）
      ↓
    Front 格式化结果 → Human

🔒 Front 永不创建 Task；Task 由 Team Lead 创建。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")

from core.task import TaskStatus                                  # noqa: E402
from demand.task_creation import TaskCreationService              # noqa: E402
from front import FrontAgentRuntime, FrontIntent                  # noqa: E402
from work.availability import AgentAvailability                   # noqa: E402
from work.runtime import SoloWorkRuntime                          # noqa: E402
from work.team_runtime import TeamWorkRuntime                     # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="work",
        description="Human → Front Agent → Work（Solo / Team；无消息 / 无经济）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--demand", help="直接给一段 Human 输入")
    src.add_argument("--file", help="从文本文件读 Human 输入")
    ap.add_argument("--front-agent", default="agent_a",
                    help="Front Agent（默认 agent_a）")
    ap.add_argument("--lead", default=None,
                    help="Team Lead；默认选第一个非 Front 的 Agent")
    ap.add_argument("--team", action="store_true",
                    help="创建 TEAM Task（Leader 计划 / 成员并行 / 汇总）")
    ap.add_argument("--members", default="",
                    help="团队成员 agent_id，逗号分隔；配合 --team")
    ap.add_argument("--budget", type=float, default=10.0,
                    help="生产观测预算（AIC），默认 10")
    ap.add_argument("--max-turns", type=int, default=12,
                    help="Work 最大轮数（Solo / Team 各自使用），默认 12")
    ap.add_argument("--cognitive-budget", type=int, default=32768,
                    help="单次响应硬上限（tokens），默认 32768")
    ap.add_argument("--db", default=None,
                    help="Runtime 数据库路径；默认放在 run 目录下")
    ap.add_argument("--out", default=None,
                    help="run 输出目录；默认 runs/work/<timestamp>-<id>")
    return ap


def _read_demand(args: argparse.Namespace) -> str:
    if args.demand and args.file:
        raise SystemExit("--demand 和 --file 只能二选一")
    if args.demand:
        return str(args.demand).strip()
    if args.file:
        return Path(args.file).expanduser().read_text(
            encoding="utf-8").strip()
    raise SystemExit("必须提供 --demand 或 --file")


def _task_refs(tasks: List[Any]) -> tuple[List[Dict[str, str]],
                                          List[Dict[str, str]]]:
    active: List[Dict[str, str]] = []
    completed: List[Dict[str, str]] = []
    for task in tasks:
        ref = {
            "task_id": task.task_id,
            "status": task.status,
            "summary": (task.description or "")[:300],
        }
        if task.status == TaskStatus.COMPLETED:
            completed.append(ref)
        elif task.is_open:
            active.append(ref)
    return active, completed


def _choose_lead(runtime: Any, front_agent: str,
                 requested: Optional[str]) -> Optional[str]:
    if requested:
        try:
            entry = runtime.directory.get(requested)
        except Exception:                                     # noqa: BLE001
            entry = None
        if entry is None:
            return None
        return requested if requested != front_agent else None
    for entry in runtime.directory.all():
        aid = str(getattr(entry, "agent_id", ""))
        if aid and aid != front_agent:
            return aid
    return None


def _parse_members(raw: str) -> List[str]:
    out: List[str] = []
    for item in str(raw or "").split(","):
        aid = item.strip()
        if aid and aid not in out:
            out.append(aid)
    return out


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


def _criteria_from_brief(brief: Any) -> List[str]:
    if brief.acceptance_expectations:
        return list(brief.acceptance_expectations)
    if brief.deliverables:
        return [f"交付物存在且非空：{item}" for item in brief.deliverables]
    return [brief.goal]


def _print_decision_only(decision: Any) -> int:
    if decision.intent == FrontIntent.DIRECT_REPLY:
        print(decision.reply)
        return 0
    if decision.intent == FrontIntent.CLARIFY:
        print(decision.question)
        return 0
    print(f"（Front 判断：{decision.intent}；CLI 暂不处理该分支）")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    human_text = _read_demand(args)
    if not human_text:
        raise SystemExit("输入不能为空")
    if int(args.cognitive_budget) <= 0:
        raise SystemExit("--cognitive-budget 必须是正整数")

    run_id = f"work_{uuid.uuid4().hex[:8]}"
    if args.out:
        run_dir = Path(args.out).expanduser().resolve()
    else:
        run_dir = ROOT / "runs" / "work" / (
            f"{datetime.now():%Y%m%d-%H%M%S}-{run_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = (Path(args.db).expanduser().resolve()
               if args.db else run_dir / "work.db")

    from main import build_runtime

    runtime = build_runtime(db_path=str(db_path))
    try:
        front_agent = runtime.economy.agent(args.front_agent)
    except Exception:                                         # noqa: BLE001
        front_agent = None
    if front_agent is None or not getattr(front_agent, "has_brain", False):
        print(f"❌ Front Agent {args.front_agent!r} 不存在或没有 Brain")
        return 2

    front = FrontAgentRuntime(
        getattr(front_agent, "agent_id", args.front_agent),
        front_agent.think,
    )
    active, completed = _task_refs(runtime.tasks.list_tasks(limit=50))
    decision = front.decide(
        human_text, active_tasks=active, completed_tasks=completed)

    if decision.intent in (FrontIntent.DIRECT_REPLY, FrontIntent.CLARIFY):
        return _print_decision_only(decision)

    if decision.intent == FrontIntent.FOLLOW_UP:
        task = runtime.tasks.get(decision.task_id)
        if task is None:
            print(f"找不到任务 {decision.task_id}")
            return 1
        print(f"任务 {task.task_id} 当前状态：{task.status}")
        print(task.description[:1000])
        return 0

    if decision.intent == FrontIntent.REWORK:
        task = runtime.tasks.get(decision.task_id)
        if task is None:
            print(f"找不到任务 {decision.task_id}")
            return 1
        if task.mode == "TEAM":
            team = TeamWorkRuntime(
                runtime, task,
                max_worker_turns=int(args.max_turns),
                max_integration_turns=int(args.max_turns),
                cognitive_budget=int(args.cognitive_budget),
            )
            work_result = team.reopen_and_run(
                reason=decision.reply or human_text)
        else:
            solo = SoloWorkRuntime(
                runtime, task,
                max_turns=int(args.max_turns),
                cognitive_budget=int(args.cognitive_budget),
            )
            work_result = solo.reopen_and_run(
                reason=decision.reply or human_text)
        print(front.format_result(
            front.result_from_work(work_result)))
        return 0 if work_result.ok else 1

    # ---- handoff ----
    lead = _choose_lead(runtime, args.front_agent, args.lead)
    if lead is None:
        print("❌ 没有可用的 Team Lead（需要至少一个非 Front 的 Agent）")
        return 2

    members = _parse_members(args.members) if args.team else []
    participants = [lead] + [m for m in members if m != lead]

    availability = AgentAvailability()
    reserved: List[str] = []
    for aid in participants:
        availability.register(aid)
        if not availability.try_acquire(aid):
            for done in reserved:
                availability.release(done)
            print(f"❌ Agent {aid} 当前不是 IDLE")
            return 2
        reserved.append(aid)

    try:
        run = SimpleNamespace(
            run_id=run_id,
            root_task_id=None,
            budget_aic=float(args.budget),
            workspace_id="",
            workspace_plan=None,
            status="INIT",
        )
        mode = "TEAM" if args.team else "SOLO"
        workspace_plan = None
        if args.team:
            workspace_plan = {
                "root_owner": lead,
                "directories": {
                    f"work/{aid}": aid for aid in participants if aid != lead
                },
                "files": {},
            }
        creation = TaskCreationService(
            runtime, run, max_agents=max(1, len(participants)))
        created = creation.create(
            creator=lead,
            description=_description_from_brief(decision.brief),
            participants=participants,
            acceptance_criteria=_criteria_from_brief(decision.brief),
            producer=lead,
            acceptance_assignee=None,
            workspace_plan=workspace_plan,
            mode=mode,
        )
        if not created.get("ok"):
            print(f"❌ Task 创建失败：{created.get('message') or created}")
            return 1

        task = runtime.tasks.get(created["task_id"])
        if task is None:
            print("❌ Task 创建后无法读取")
            return 1

        if args.team:
            team = TeamWorkRuntime(
                runtime, task,
                brief=decision.brief.to_dict(),
                max_worker_turns=int(args.max_turns),
                max_integration_turns=int(args.max_turns),
                cognitive_budget=int(args.cognitive_budget),
            )
            work_result = team.run()
        else:
            solo = SoloWorkRuntime(
                runtime, task,
                brief=decision.brief.to_dict(),
                max_turns=int(args.max_turns),
                cognitive_budget=int(args.cognitive_budget),
            )
            work_result = solo.run()
        print(front.format_result(
            front.result_from_work(work_result)))
        return 0 if work_result.ok else 1
    finally:
        for aid in reserved:
            availability.release(aid)


if __name__ == "__main__":
    raise SystemExit(main())
