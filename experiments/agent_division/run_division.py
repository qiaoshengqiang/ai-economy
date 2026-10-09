#!/usr/bin/env python3
"""
观察实验：**人类 Demand → 第一个 Agent → 看它能不能把工作分出去**
================================================================

【🔴 最高原则（§0.5）】

    **不要设计合作行为本身，要设计让合作值得发生的环境。**

    所以这个实验**不推进、不指派、不催、不排班**。它只做三件事：

        ① 给第一个 Agent 一个**能干活的环境**
           （Task + 它当 root Controller 的 Workspace）
        ② 把人类 Demand 递给**它一个人**
        ③ **记录**接下来发生了什么

    ⇒ 我们**不干涉**他们的分工。分不分、分给谁、分几份 —— 全是观察对象。

【🔴 为什么循环是「消息驱动」，不是「轮转」】

    轮转（`for aid in AGENTS`）等于**系统在替它们排班**。
    而 §22.27 说：**消息到达本身就是足以触发 Agent Wakeup 的事件**。

    ⇒ 这里只跑**被消息唤醒**的 Agent：
           `comm.is_woken(agent_id)` 为真的人

    🔑 **没人被唤醒 = 生产停住了 = 这本身就是观察结果**
       （第一个 Agent 没有把工作分出去），
       而不是"实验跑失败了"。所以 `stop_reason = NOBODY_WOKEN`。

【🔴 不预设什么（这些正是"干涉"）】

    ❌ `ARTIFACT_NAMESPACES` —— 不预先给每个 Agent 分命名空间。
       谁来建目录、建了给谁，是**他们的**决定。
       （第一个 Agent 是 root Controller，它建得出来，也交得出去。）
    ❌ DoD / 验收责任 —— 不预先建。谁负责验收由 Agent 自己
       用 `assign_acceptance` 决定（依据：`task_execution_service`
       「谁负责验收由 **Agent 自己**通过现有的 ASSIGN 能力决定」）。
    ❌ START / SUBMIT / VERIFY / COMPLETE —— runner **一律不发**。
       这一整条链应该由 Agent 自己走；走不通就是这次观察的发现。
    ❌ 每个 Agent 都拿到人类命令 —— 只有第一个 Agent 拿到。

【⚠️ 如实说明一处事实投递】

    `Task.description` 仍然是 Demand，而参与者的 prompt 里会看到
    `Task 目标：…`（`_completion_facts`）。那是 **Task 的定义**，
    不是"人类直接命令你" —— 「人类命令」这个通道只通向第一个 Agent。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                      # ai-economy/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "experiments" / "novel_production") not in sys.path:
    sys.path.insert(0, str(ROOT / "experiments" / "novel_production"))

from dotenv import load_dotenv                             # noqa: E402

load_dotenv(ROOT / ".env")

from experiments.agent_division.prompt import (  # noqa: E402
    NO_DIRECT_ORDER, PROMPT as DIVISION_PROMPT,
)
from experiments.heterogeneous_production import production as P   # noqa: E402

# ================================================================
# 设置（会被 CLI 覆盖）
# ================================================================
# 🔴 **无角色的名字**（平台默认就是它们）。
#
#    为什么不用 `novel_world_builder` 这类名字：
#    **名字本身就在泄露分工** —— 它告诉所有人"我是世界构建者"，
#    于是"该找谁"根本不需要涌现，照着名字念就行了。
#    （实测已证实：`novel_world_builder` 的 reasoning 几乎逐句复述
#      它 config 里那句「你负责故事世界…为写手 novel_author 提供地基」。）
NEUTRAL_AGENTS = ["agent_a", "agent_b", "agent_c", "agent_d", "agent_e"]

AGENTS: List[str] = list(NEUTRAL_AGENTS)
# 🔴 **中性 id** —— 不许把实验意图写进 id。
#
#    实测抓到的泄露：原先是 `WS-DIVISION-001`，
#    而 agent_a 的思考里出现了
#        「the task says **"division"** — maybe agents are supposed to
#          divide work? But nothing says that.」
#    ⇒ **是我们自己的 id 在提示它"这个实验要分工"**，
#      和 `novel_world_builder` 那种"名字即分工"是同一类问题。
TASK_ID = "task_0001"
WS_ID = "WS-0001"
#: 🔴 **本实验自己的根 Stage id** —— 不许借 `production.ROOT_STAGE_ID`。
#:
#:    【为什么（实测踩到）】
#:        `production.ROOT_STAGE_ID` 是**固定常量** `"stage_hetero_root"`，
#:        而 `production_stages.stage_id` 是 **UNIQUE** 的。
#:        于是同一个 DB 里换一个 `TASK_ID` 再跑：
#:            `list_stages(TASK_ID)` 返回空（按 **task_id** 查）
#:            → 去建 stage → **撞上别的 task 留下的同一个 stage_id**
#:            → `UNIQUE constraint failed`
#:        ⇒ stage_id 必须**跟着 task 走**。
ROOT_STAGE_ID = f"stage_{TASK_ID}_root"
EXPERIMENT_ID = "agent_division"
#: 🔴 **每次运行一个独立目录** —— 这是**观察实验**，不是累计系统。
#:
#:    默认 DB 跨运行共用的话，第二次跑会**继承上一次的
#:    Task / Workspace / Stage** ⇒ 观察结果被污染
#:    （实测踩到：第二次跑直接 `UNIQUE constraint failed`）。
#:
#:    ⇒ 与 `experiments/user_demand/run_demand.py` 同一做法：
#:      默认在 `runs/<HHMM>-<hash>/` 下开一份全新的 DB。
#:      想复用请显式给 `--out`。
RUNS: Path = ROOT / "experiments" / "agent_division" / "runs"
DB: Path = RUNS / "_unset.db"
RESULTS: Path = RUNS / "_unset"

#: 人类 Demand（由 CLI 覆盖）
DEMAND = "写一个短篇小说的第一章。"

WALL_LIMIT = float(os.environ.get("DIVISION_WALL", "900"))       # 总墙钟（秒）
MAX_ROUNDS = int(os.environ.get("DIVISION_ROUNDS", "40"))        # 轮数上限
BUDGET_AIC = float(os.environ.get("DIVISION_BUDGET", "50"))
COGNITIVE_BUDGET = int(os.environ.get("DIVISION_COGNITIVE", "16384"))

# ================================================================
# 🔴 **测试注入的假履历** —— 随机生成，**不是真实历史**
# ================================================================
#
# 【为什么要注入】
#
#   §0.5.8 说：**OS 提供：Agent 信息 · 价格 · 信誉 · 历史 · 资源**
#   §0.5.3 说：激励应包含 `Reputation · 历史成功率 · 合作记录`
#   §0.5.7 说：专业化应由 `能力 · 成本 · 信誉 · 历史` 长期反馈涌现
#
#   🔴 **但我们还没有真实的反馈回路** —— 审计 A-11 原文：
#        「`Reputation` 存在但**没有真实反馈回路**
#          （无结算、无成功率累积）」
#
#   而 §0.5.7 又说「不要预先规定 A 是程序员、B 是测试 Agent」——
#   所以也不能拿 `skills` 标签去充当区分度（那是预设角色表）。
#
# 【🔴 为什么改成随机生成】
#
#   原来是 5 份**固定**的履历。实测 3 次运行，Agent 每次都选择自己完成 ——
#   但那**说明不了履历没影响**：固定履历下，「换个履历会不会改变结果」
#   这个问题**根本没被问到**。
#
#   ⇒ 随机生成，才能观测 **履历差异是否影响选择**。
#     每次运行都不一样 —— 多跑几次就能看出结果有没有跟着履历变。
#
#   🔒 **seed 会被记录**（进结果 JSON + 控制台）：
#      `--history-seed N` 可以**复现**某一次；不给就是随机的。
#
#   ⚠️ 它**仍然不是真实数据** —— 每一条都进 `injected_fake_history`，
#      控制台也会打印。就是为了让任何人回头看时都不会当成真的。


def _make_fake_history(agents: List[str], *,
                       seed: Any = None) -> tuple:
    """
    随机生成一组假履历。返回 `(履历表, 实际使用的 seed)`。

    🔒 形态跟着 `Reputation` 走（它就三个最朴素的计数）：
       `score` / `completed_tasks` / `failed_tasks`。

    🔒 `score` **大体跟成功率相关**（真实声誉会相关），但带一点噪声 ——
       完全无关的话，Agent 面对的就是一组自相矛盾的数字，
       那测的就不是「看不看履历」而是「能不能容忍矛盾」了。
    """
    import random

    if seed is None:
        seed = random.randrange(1 << 30)
    rnd = random.Random(int(seed))

    out: Dict[str, Dict[str, Any]] = {}
    for aid in agents:
        total = rnd.choice([0, 0, 1, 2, 3, 4, 6, 8, 10])
        failed = rnd.randint(0, min(4, total))
        completed = total - failed
        if total == 0:
            score = 0.0
        else:
            rate = completed / total
            score = round(min(1.0, max(0.0, rate + rnd.uniform(-0.12, 0.12))), 2)
        out[aid] = {"reputation_score": score,
                    "completed_tasks": completed,
                    "failed_tasks": failed}
    return out, int(seed)


#: 人类这个 Actor 的名字（ADR-0022：Human 是正式协作 Actor）
HUMAN = "human"


# ================================================================
# ① 环境：**只建"能干活"的最小结构**
# ================================================================
def _setup(rt: Any) -> None:
    """
    建 Task + Workspace + 一个根 Stage。

    🔴 **刻意不做的**：
        · 不 `provision_namespaces` —— 命名空间由 Agent 自己决定
        · 不建 DoD / 不指派验收责任 / 不发 START —— 完成链归 Agent
    """
    from core.task import Task
    from stage import STATUS_ACTIVE, StageStore

    first = AGENTS[0]
    if rt.tasks.get(TASK_ID) is None:
        rt.tasks.save(Task.create(
            creator=first,                 # 第一个 Agent = Creator
            description=DEMAND,            # Task 的目标（共享事实）
            workspace_id=WS_ID, task_id=TASK_ID,
            participants=list(AGENTS)))

    # 🔴 **第一个 Agent 就是 root Controller** ——
    #    所以只有它能在根下建目录，也只有它能 handoff 给别人。
    #    **这就是"分工"必须由它发起**的结构原因（不是我们规定的）。
    rt.workspace_gateway.ensure_workspace(WS_ID, first)

    st = StageStore(rt.economy.db)
    if not st.list_stages(TASK_ID):
        st.create_stage(TASK_ID, DEMAND.strip(),
                        deliverables=[{"name": "task deliverables"}],
                        status=STATUS_ACTIVE,
                        stage_id=ROOT_STAGE_ID)

    print(f"  环境就绪：")
    print(f"    Task       : {TASK_ID}")
    print(f"    Workspace  : {WS_ID}")
    print(f"    Creator    : {first}   ← 它也是 **root Controller**")
    print(f"    Participants: {', '.join(AGENTS)}")
    print(f"    🔴 未预设命名空间 · 未建 DoD · 未指派验收责任 · 未发 START")
    # 🔴 把「模型 + 输出预算」早打印出来 ——
    #    推理模型 + 小 max_tokens 会让 content 为空（实测踩过）。
    for a in AGENTS:
        try:
            cfg = rt.economy.agent(a).config
            print(f"      {a:10s} model={getattr(cfg, 'provider', '?'):10s} "
                  f"max_tokens={getattr(cfg, 'max_tokens', None)}")
        except Exception:                                      # noqa: BLE001
            pass


# ================================================================
# ② 把人类 Demand 递给**第一个 Agent**
# ================================================================
def _seed(rt: Any, bridge: Any) -> None:
    """
    把人类 Demand 交给第一个 Agent。

    🔴 **这里曾经假扮 `human` 发一条消息 —— 那是错的，已改。**

        通信层**只接受已登记 Agent 作为发送方**：

            AgentNotFoundError: 发送方 'human' 没有接入通信层

        设计说「Human 是正式协作 Actor」（ADR-0022），但**实现里没有
        Human 入口** —— 那是一个真实缺口，属于设计决定（人类消息怎么进来），
        **不在本实验里悄悄绕过去**。

    ⇒ V1 的诚实做法：Demand 经 **prompt 的 `demand` 槽位**投递
       （Human → Agent 的通道），而 runner **把第一个 Agent 放进第 1 轮**。

       🔒 这**不是排班**：它是本实验的**输入**（"人类命令给到第一个 Agent"）。
          第 1 轮之后，只跑**被消息唤醒**的人。
    """
    print(f"\n  人类 Demand → {AGENTS[0]}（经 prompt 的 demand 槽位）")
    print(f"  ⚠️ 通信层只接受已登记 Agent 作为发送方，"
          f"所以 runner **没有**替 Human 发消息")
    print(f"  🔒 第 1 轮之后，只跑**被消息唤醒**的人；runner 不再指定任何人")


# ================================================================
# ③ 只跑**被消息唤醒**的 Agent
# ================================================================
def _inject_fake_history(rt: Any, *, seed: Any = None
                        ) -> tuple:
    """
    随机生成一组假履历，注入成每个 Agent 的**公开履历**。

    🔒 注入的是 `Reputation` 对象本身（`Reputation` 有三个最朴素的计数：
       `score` / `completed_tasks` / `failed_tasks`，还有 `success_rate`）。
       注入后 `_roster_facts` 就会把它当作公开事实投递给其他 Agent。

    🔴 **这不是真实历史。** 每一条都被记进结果 JSON 的
       `injected_fake_history`，控制台也会打印 ——
       就是为了**任何人回头看时都不会把它当成真实数据**。
    """
    from agents.base_agent import Reputation

    history, seed = _make_fake_history(list(AGENTS), seed=seed)
    done: Dict[str, Any] = {}
    for aid, fake in history.items():
        if aid not in AGENTS:
            continue
        try:
            agent = rt.economy.agent(aid)
        except Exception:                                      # noqa: BLE001
            continue
        agent.reputation = Reputation(
            score=float(fake["reputation_score"]),
            completed_tasks=int(fake["completed_tasks"]),
            failed_tasks=int(fake["failed_tasks"]))
        rep = agent.reputation
        done[aid] = {"reputation_score": rep.score,
                     "completed_tasks": rep.completed_tasks,
                     "failed_tasks": rep.failed_tasks,
                     "success_rate": rep.success_rate}
    if done:
        print(f"\n  🔴 **测试注入的假履历**（随机生成；不是真实历史，"
              f"不得当作真实数据）· seed={seed}")
        for aid, v in done.items():
            print(f"       {aid:10s} 完成任务={v['completed_tasks']} "
                  f"失败={v['failed_tasks']} "
                  f"成功率={v['success_rate']} 声誉={v['reputation_score']}")
        print("    依据 §0.5.8「OS 提供…信誉·历史」；真实反馈回路尚未实现"
              "（审计 A-11）")
        print(f"    🔒 随机 seed={seed} —— 复现同一次请加 `--history-seed {seed}`")
    return done, seed


def _is_woken(rt: Any, agent_id: str) -> bool:
    """只看唤醒**信号**，不消费 —— 消费由 `_turn` 里的 drain 负责。"""
    try:
        return bool(rt.communication.is_woken(agent_id))
    except Exception:                                          # noqa: BLE001
        return False


def main() -> int:
    global DEMAND, AGENTS, WALL_LIMIT, MAX_ROUNDS, BUDGET_AIC
    global COGNITIVE_BUDGET, DB, RESULTS

    ap = argparse.ArgumentParser(
        description="观察实验：Demand → 第一个 Agent → 看它能否分工")
    ap.add_argument("--demand", help="直接给一句 Demand")
    ap.add_argument("--demand-file", help="从文本文件读 Demand")
    ap.add_argument("--agents", default=",".join(NEUTRAL_AGENTS))
    ap.add_argument("--minutes", type=float, default=WALL_LIMIT / 60,
                    help="总墙钟上限（分钟）")
    ap.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    ap.add_argument("--budget", type=float, default=BUDGET_AIC)
    ap.add_argument("--cognitive-budget", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--reuse-db", action="store_true")
    ap.add_argument("--history-seed", type=int, default=None,
                    help="固定随机履历的 seed（便于复现某一次）；"
                         "不给就是每次随机")
    args = ap.parse_args()
    # 🔒 seed 只影响**注入什么履历**，不影响任何落盘路径
    history_seed = args.history_seed

    if args.demand and args.demand_file:
        ap.error("--demand 与 --demand-file 只能给一个")
    if args.demand_file:
        DEMAND = Path(args.demand_file).read_text(encoding="utf-8").strip()
    elif args.demand:
        DEMAND = args.demand
    AGENTS = [a.strip() for a in args.agents.split(",") if a.strip()]
    WALL_LIMIT = args.minutes * 60
    MAX_ROUNDS = args.rounds
    BUDGET_AIC = args.budget
    if args.cognitive_budget:
        COGNITIVE_BUDGET = args.cognitive_budget
    if args.out:
        out = Path(args.out)
        DB, RESULTS = out / "division.db", out / "results"
    else:
        # 🔒 每次一个全新目录 —— 别让上一次的观察污染这一次
        import hashlib
        import time as _time
        stamp = _time.strftime("%m%d-%H%M")
        tag = hashlib.sha1(DEMAND.encode("utf-8")).hexdigest()[:8]
        out = RUNS / f"{stamp}-{tag}"
        DB, RESULTS = out / "division.db", out / "results"
    DB.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    P.DB, P.RESULTS = DB, RESULTS
    P.DEMAND, P.AGENTS, P.WS_ID, P.TASK_ID = DEMAND, AGENTS, WS_ID, TASK_ID
    # 🔴 不给这个实验预设任何命名空间 —— `_workspace_facts` 会如实显示"（空）"
    P.ARTIFACT_NAMESPACES = {}
    P.OWNER = AGENTS[0]

    from main import build_runtime

    print("=" * 72)
    print("  观察实验：Demand → 第一个 Agent → 看它能否把工作分出去")
    print("=" * 72)
    print(f"  Demand   : {DEMAND.splitlines()[0][:60]}")
    print(f"  第一个 Agent: {AGENTS[0]}")
    print(f"  上限     : {WALL_LIMIT/60:.0f} 分钟 / {MAX_ROUNDS} 轮 / {BUDGET_AIC} AIC")
    print(f"  本次目录 : {DB.parent}   ← **每次运行独立**，不与上次混淆")
    print(f"  履历 seed: {history_seed if history_seed is not None else '（随机）'}")

    rt = build_runtime(db_path=DB, agent_ids=list(AGENTS))
    log: Dict[str, Any] = {"experiment_id": EXPERIMENT_ID,
                           "agents": list(AGENTS), "demand": DEMAND,
                           "workspace_id": WS_ID, "task_id": TASK_ID,
                           "scheduler": "message-driven (只跑被唤醒的)",
                           "turns": [], "comm_events": [],
                           "first_agent": AGENTS[0]}
    stats = {a: {"calls": 0, "tokens": 0, "valid": 0, "empty": 0,
                 "writes_ok": 0, "writes_fail": 0, "messages": 0,
                 "created": 0, "replaced": 0, "appended": 0,
                 "chars_written": 0} for a in AGENTS}
    import channels
    bridge = channels.SyncBridge()
    t0 = time.time()
    stop_reason = "UNKNOWN"
    try:
        _setup(rt)
        injected, history_seed = _inject_fake_history(rt, seed=history_seed)
        log["injected_fake_history"] = injected
        log["history_seed"] = history_seed
        dispatcher, experience_store = P._action_seam(rt, bridge)
        # 🔴 **必须装** —— `production.py` 的 main() 里装了，我这里漏了，
        #    于是 `turn["context"]` 一直是 `{}`（"它这一轮看到了什么"没有证据）。
        #    它记录的是**真正渲染进 system 的那一份**（不是"库里有没有"）。
        _n_wrapped = P._install_context_recorder(rt)
        print(f"  context 遥测: 已包装 {_n_wrapped} 个 Agent 的 context_builder")
        _seed(rt, bridge)

        from stage import StageStore
        from stage.context import render_stage
        st = StageStore(rt.economy.db)
        stage = next((s for s in st.list_stages(TASK_ID) if s.is_open), None)

        rnd, idle_rounds = 0, 0
        while rnd < MAX_ROUNDS and (time.time() - t0) < WALL_LIMIT:
            rnd += 1
            # 🔴 第 1 轮 = **实验的输入**（人类命令给到第一个 Agent）。
            #    第 2 轮起 = 只跑被消息唤醒的人。
            if rnd == 1:
                woken = [AGENTS[0]]
                log["seed"] = {"to": AGENTS[0], "round": 1,
                               "kind": "prompt-demand-slot",
                               "note": "通信层只接受已登记 Agent 作为发送方；"
                                       "Human 入口是设计缺口，未在此绕过"}
            else:
                woken = [a for a in AGENTS if _is_woken(rt, a)]
            if not woken:
                stop_reason = "NOBODY_WOKEN"
                print(f"\n  ── 第 {rnd} 轮：**没有任何 Agent 被唤醒**")
                ran = {x.get("agent_id") for x in log["turns"]}
                if AGENTS[0] in ran:
                    print(f"     ⇒ 生产停住了。这是观察结果 ——")
                    print(f"        {AGENTS[0]} 跑过，但**没有把工作分给任何人**。")
                else:
                    print(f"     🔴 但**第一个 Agent 根本没跑过** ——")
                    print(f"        这不是「它没找人」，是**这次没获得观察**。")
                break
            ckpt = st.latest_checkpoint(stage.stage_id) if stage else None
            stage_txt = render_stage(

                stage, checkpoint=ckpt, limit_seconds=WALL_LIMIT,

                elapsed_seconds=time.time() - t0,

                # 🔴 「还能给你几个回合」—— Agent **能算**的事实

                rounds_left=max(0, MAX_ROUNDS - rnd + 1))
            print(f"\n  ── 第 {rnd} 轮：被唤醒的是 {', '.join(woken)}")
            for aid in woken:
                if (time.time() - t0) >= WALL_LIMIT:
                    stop_reason = "WALL_LIMIT"
                    break
                turn = P._turn(rt, bridge, st, stage, aid, rnd, stage_txt,
                               log, stats, dispatcher=dispatcher,
                               experience_store=experience_store,
                               # 🔴 **只给第一个 Agent 人类命令**
                               demand=(DEMAND if aid == AGENTS[0]
                                       else NO_DIRECT_ORDER),
                               # 🔴 **用本实验自己的模板** —— 不复用
                               #    `production.PROMPT`（它塞了 Stage /
                               #    Session / 结项待办 / 「轮」）。
                               prompt_template=DIVISION_PROMPT)
                log["turns"].append(turn)
                P._print_turn(aid, turn)
            if stop_reason == "WALL_LIMIT":
                break
        else:
            stop_reason = ("MAX_ROUNDS_REACHED" if rnd >= MAX_ROUNDS
                           else "WALL_LIMIT")

        log["stop_reason"] = stop_reason
        log["rounds"] = rnd
        log["task_status"] = str(getattr(rt.tasks.get(TASK_ID), "status", "?"))
        log["final_workspace"] = _workspace_tree(rt.workspace_gateway,
                                                 AGENTS[0])
    except Exception as exc:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        log["stop_reason"] = f"ERROR: {type(exc).__name__}: {exc}"
    finally:
        log["wall_seconds"] = round(time.time() - t0, 1)
        log["per_agent"] = dict(stats)
        log["budget"] = _safe_budget(rt)
        rt.close()

    log["report"] = _build_report(log)
    _report(log)
    out = RESULTS / "division_result.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  结果 JSON : {out}")
    return 0


def _workspace_tree(gw: Any, agent: str, path: str = "/",
                    depth: int = 0) -> List[Dict[str, Any]]:
    """
    从根开始递归列出工作区里**真实存在**的东西。

    🔒 不用 `production._snapshot()` —— 它只遍历 `ARTIFACT_NAMESPACES`，
       而本实验**刻意不预设**命名空间，所以它永远返回空。
    """
    out: List[Dict[str, Any]] = []
    if depth > 4:
        return out
    try:
        got = gw.list(WS_ID, path, agent)
    except Exception:                                          # noqa: BLE001
        return out
    if not got.success:
        return out
    for e in (got.result or {}).get("entries", []) or []:
        child = path.rstrip("/") + "/" + str(e.get("name"))
        out.append({"path": child, "type": e.get("type"),
                    "revision": e.get("revision"),
                    "holder": e.get("effective_lock_holder"),
                    "controller": e.get("effective_controller")})
        if e.get("type") == "directory":
            out.extend(_workspace_tree(gw, agent, child, depth + 1))
    return out


def _safe_budget(rt: Any) -> Any:
    try:
        return rt.budget_service.audit(TASK_ID)
    except Exception:                                          # noqa: BLE001
        return None


# ================================================================
# 观察报告 —— **只陈述发生了什么**
# ================================================================
def AGENTS_FIRST(log: Dict[str, Any]) -> str:
    return str(log.get("first_agent") or "")


def _build_report(log: Dict[str, Any]) -> Dict[str, Any]:
    """
    把控制台那份分析做成**机器可读**的，进结果 JSON。

    【为什么（用户反馈）】
        「结果给的 json 里面没有什么有用信息」——
        因为分析只打印在终端上，JSON 里只有一堆原始 turn。
        ⇒ 结论必须和原始数据**在一起**，否则事后没法复算。
    """
    turns = log.get("turns") or []
    first = str(log.get("first_agent") or "")

    broken = [t for t in turns
              if t.get("output_status") == "EMPTY_CONTENT"
              or t.get("cognitive_status") == "TRUNCATED"]
    acted = []
    for t in turns:
        if t.get("agent_id") not in acted:
            acted.append(t.get("agent_id"))

    sent = [(e.get("agent_id"), e.get("to"))
            for e in (log.get("comm_events") or []) if e.get("kind") == "sent"]
    from collections import Counter
    per_action: Dict[str, Any] = {}
    for t in turns:
        aid = t.get("agent_id")
        per_action.setdefault(aid, Counter())
        act = t.get("action") or {}
        per_action[aid][f"{act.get('type') or '(无动作)'}"
                        f"{'' if act.get('ok') else '(失败)'}"] += 1

    first_ran = first in acted
    first_broken = any(t.get("agent_id") == first for t in broken)
    return {
        # 🔴 **这次有没有获得观察** —— 后面所有结论的前提
        "observed": bool(first_ran and not first_broken),
        "why_not_observed": (None if (first_ran and not first_broken) else
                             ("第一个 Agent 没跑过" if not first_ran
                              else "第一个 Agent 那一轮模型没返回内容")),
        "first_agent_ran": first_ran,
        "broken_turns": [{"round": t.get("round"),
                          "agent_id": t.get("agent_id"),
                          "finish_reason": t.get("finish_reason"),
                          "output_status": t.get("output_status"),
                          "cognitive_status": t.get("cognitive_status")}
                         for t in broken],
        # 分工有没有发生（三个独立证据）
        "agents_acted": acted,
        "agents_never_woken": [a for a in (log.get("agents") or [])
                               if a not in acted],
        "messages": [{"from": s, "to": d} for s, d in sent],
        "anyone_contacted": bool(sent),
        "workspace_tree": log.get("final_workspace") or [],
        "per_agent_actions": {k: dict(v) for k, v in per_action.items()},
        # 「它打算怎么做」—— 模型在信封里自己写的判断
        "reasoning_by_turn": [{"round": t.get("round"),
                               "agent_id": t.get("agent_id"),
                               "reasoning": t.get("reasoning")}
                              for t in turns if t.get("reasoning")],
    }


def _report(log: Dict[str, Any]) -> None:
    turns = log.get("turns") or []
    first = log.get("first_agent")
    print("\n" + "=" * 72)
    print("  观察结果")
    print("=" * 72)
    print(f"  结束原因 : {log.get('stop_reason')}")
    print(f"  轮次     : {log.get('rounds')} 轮 / {len(turns)} 个 turn")
    print(f"  墙钟     : {log.get('wall_seconds')}s")
    print(f"  Task 状态: {log.get('task_status')}")
    fake = log.get("injected_fake_history") or {}
    if fake:
        print(f"  🔴 假履历 : 已注入 {len(fake)} 个 Agent 的"
              f"**测试用**信誉/历史（不是真实数据）")

    # ---- 0. 🔴 先看**这次有没有获得观察** ----
    #
    #   【必须区分两件长得一样的事】
    #
    #       「Agent 跑过，**选择**不做动作」      ← 这是观察结果
    #       「Agent 跑过，但**模型没返回内容**」  ← 这**不是**观察结果
    #
    #   实测（03:08）：`agent_a` 的 turn 是
    #       finish_reason=length · output_status=EMPTY_CONTENT ·
    #       cognitive_status=TRUNCATED · chars=0
    #   而报告却说「agent_a 跑过，但**没有把工作分给任何人**」——
    #   **它什么都没产出，凭什么说它"选择不分工"？**
    #
    #   🔒 这正是本项目反复警告的那一类：**报告在替 Agent 编故事**。
    broken = [t for t in turns
              if t.get("output_status") == "EMPTY_CONTENT"
              or t.get("cognitive_status") == "TRUNCATED"]
    acted = []
    for t in turns:
        if t.get("agent_id") not in acted:
            acted.append(t.get("agent_id"))

    if broken:
        print(f"\n  🔴 **有 {len(broken)} 个 turn 是「模型没返回内容」**"
              f"（不是 Agent 选择不做）：")
        for t in broken:
            print(f"       r{t.get('round')} {t.get('agent_id'):10s} "
                  f"finish_reason={t.get('finish_reason')} "
                  f"output={t.get('output_status')} "
                  f"cognitive={t.get('cognitive_status')} "
                  f"tokens={t.get('tokens')}")
        print("     ⇒ 那不是它的决定 —— **它根本没获得机会**。")
        print("     ⇒ 本次数据**不能**用来回答「它有没有分工」。")
        print("     ⚠️ 常见成因：推理模型把输出上限全用在 reasoning 上"
              "（`finish_reason=length` + content 为空）。")

    if AGENTS_FIRST(log) not in acted:
        print(f"\n  🔴 **没有获得观察**（`stop_reason={log.get('stop_reason')}`）")
        print(f"     第一个 Agent（{AGENTS_FIRST(log)}）**一次都没跑过** ——")
        print(f"     所以「它有没有把工作分出去」这个问题**这次没有被问到**。")
        print(f"     ⚠️ 不要把它读成「它没找人」。")
        print("\n" + "=" * 72)
        return
    print(f"\n  ── ① 参与过生产的 Agent：{len(acted)} / {len(log['agents'])}")
    for a in log["agents"]:
        mark = "✅" if a in acted else "⬜ 从没轮到（**没人叫它**）"
        print(f"       {a:22s} {mark}")
    first_turn_broken = any(t.get("agent_id") == AGENTS_FIRST(log)
                            for t in broken)
    if first_turn_broken:
        print(f"\n  ⚠️ 第一个 Agent（{AGENTS_FIRST(log)}）那一轮**没有产出**，"
              f"所以下面「谁没轮到」只能读作**事实**，")
        print(f"     不能读作「它选择不找人」。")

    never = [a for a in log["agents"] if a not in acted]
    if never:
        print(f"     🔴 从没被唤醒的：{', '.join(never)}")
        print("        —— 这不是「排班不公平」，是**没人把工作分给它们**。")

    # ---- 2. 消息图：谁找了谁 ----
    print(f"\n  ── ② 消息图（分工的直接证据）")
    sent = [(e.get("agent_id"), e.get("to")) for e in log.get("comm_events") or []
            if e.get("kind") == "sent"]
    if not sent:
        print(f"       **一条消息都没有** —— 第一个 Agent 谁也没找。")
    for src, dst in sent:
        print(f"       {src:22s} → {dst}")
    targets = sorted({d for _, d in sent})
    print(f"     被找过的人：{', '.join(targets) if targets else '（无）'}")

    # ---- 3. 每个 Agent 做了什么（动作分布）----
    print(f"\n  ── ③ 每个 Agent 发起的动作")
    from collections import Counter
    per: Dict[str, Counter] = {}
    for t in turns:
        aid = t.get("agent_id")
        per.setdefault(aid, Counter())
        act = t.get("action") or {}
        if act:
            per[aid][f"{act.get('type')}{'' if act.get('ok') else '(失败)'}"] += 1
        else:
            per[aid]["（没有发起动作）"] += 1
    for a, c in per.items():
        print(f"       {a:22s} {dict(c)}")

    # ---- 4. 分工的**结构证据**：谁建了目录、交给了谁 ----
    print(f"\n  ── ④ 工作区的结构与责任变化")
    handoffs = [t for t in turns
                if (t.get("action") or {}).get("type") == "resource_handoff"
                and (t.get("action") or {}).get("ok")]
    creates = [t for t in turns
               if (t.get("action") or {}).get("type") == "resource_create"
               and (t.get("action") or {}).get("ok")]
    print(f"       建目录 : {len(creates)} 次 "
          f"{[ (t['agent_id'], (t['action'] or {}).get('path')) for t in creates ]}")
    print(f"       交接   : {len(handoffs)} 次 "
          f"{[ (t['agent_id'], (t['action'] or {}).get('path')) for t in handoffs ]}")
    if not handoffs:
        print("       ⇒ **没有任何交接** —— 分工没走到「把责任交出去」这一步。")

    # ---- 5. 完成链有没有被 Agent 自己走 ----
    print(f"\n  ── ⑤ 完成链（runner **一律没发**）")
    for want in ("workspace_write", "message_send", "assign_acceptance"):
        n = sum(1 for t in turns
                if (t.get("action") or {}).get("type") == want)
        print(f"       {want:20s} {n} 次")
    print(f"       Task 状态 = {log.get('task_status')}")
    print(f"       （runner 没有发过 START / SUBMIT / VERIFY / COMPLETE）")

    # ---- 6. 工作区最后长什么样 ----
    fw = log.get("final_workspace") or []
    print(f"\n  ── ⑥ 最终工作区（{len(fw)} 个条目）")
    if not fw:
        print("       **空的** —— 没有任何人写下任何东西。")
    for e in sorted(fw, key=lambda x: x["path"]):
        print(f"       {e['path']:44s} {e.get('type'):9s} "
              f"责任人={e.get('holder')}")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    raise SystemExit(main())
