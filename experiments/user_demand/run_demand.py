#!/usr/bin/env python3
"""
用户 Demand → 多 Agent 协作生产（**测试入口**）
=================================================

🔒 **这是一个入口脚本，不修改任何项目主代码。**
   它复用现有的生产机制（`experiments/heterogeneous_production/production.py`），
   只是在**外部**覆盖它的几个模块级参数（Demand / Agent 集 / 墙钟 / 数据库路径）。

用法
----

    # 最简：直接给一句 Demand
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand "写一个 500 字的科幻短篇：一个 Agent 发现自己的记忆被改过。"

    # 从文件读（Demand 很长时方便）
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand-file my_demand.txt --minutes 6

    # 只想先看看会不会动起来（1 轮，5 次调用，很便宜）
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand "..." --rounds 1 --minutes 3

    # 换一组参与者（默认是 5 个小说 Agent）
    ./.venv/bin/python experiments/user_demand/run_demand.py \
        --demand "..." --agents novel_author,novel_editor,novel_reader

发生了什么
----------
完全走**现有机制**，没有任何专用旁路：

    build_runtime()                 ← 真实 Runtime（配置 / 经济 / 通信 / 工作区）
        ↓
    Task.create(creator=第一个 Agent, description=<你的 Demand>)
        ↓
    Workspace + 根 Stage            ← 共享 Task Workspace
        ↓
    轮次循环：每个 Agent 一轮
        · 收到 prompt：Demand + Stage + **真实 Workspace 事实** + 花名册 + 信箱
        · LLM 返回结构化 JSON：assessment / artifact / message
        · 写文件 → WorkspaceGateway（受锁与 revision 约束）
        · 发消息 → CommunicationManager（对端会被 wakeup）
        · Experience / CognitiveContext / 成本记账 全部照常
        ↓
    结果 JSON + 退出原因

🔒 会花真实 API 的钱。墙钟有硬上限（`--minutes`，默认 6 分钟）。

跑完看什么
----------
🔴 **本次运行的 stdout 会完整落到 `<输出目录>/run.log`**，
   而且结束时额外打印一段「本次运行发生了什么」，专门暴露
   **不产生报错、但也没做成事**的情况：

       · 哪些 Agent **从没轮到**        （终端上只是少一行，看不出异常）
       · 哪些 turn **没成功写入**        （`locked_by_other` / 没提交 artifact）
       · Agent 之间**真实发了什么消息**   （终端上只有一个 ✉️ 箭头）

   🔒 这三项的数据**本来就存在**（results JSON + `communication_messages`），
      以前只是**没有出口**。不是新增记录，是把已有记录露出来。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # 项目根（ai-economy/）

# 🔒 runpy / 直接跑时 sys.path[0] 是脚本目录 —— 必须显式加项目根，
#    否则 `import communication` 之类会找不到。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 🔒 必须在 import 配置体系**之前**把 .env 装进 os.environ。
#    生产脚本自己**不**加载 .env（与 main.py 的分工一致）。
from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")


#: 默认参与者 —— 就是那 5 个已经验证过能协作的 Agent。
#: 它们与 `production.py` 里的 PRODUCER / ACCEPTOR 常量一致。
DEFAULT_AGENTS = ["novel_world_builder", "novel_plot_architect",
                  "novel_author", "novel_editor", "novel_reader"]


# ======================================================================
# A：把 stdout 同时写到控制台和 run.log
# ======================================================================
class _Tee:
    """
    一份输出，两个去处。

    🔴 为什么需要：以前**控制台输出完全不落盘**。于是"我跑的时候看到
       一行怪东西"这件事无法复核 —— 事后只能靠数据库反推，
       而数据库里没有"用户看到了什么"。

    🔒 只转发 `write` / `flush`；不做缓冲合并，保证崩溃时前面已经落盘。
    """

    def __init__(self, path: Path, stream):
        self._f = open(path, "w", encoding="utf-8")
        self._out = stream

    def write(self, s: str) -> int:
        try:
            self._out.write(s)
            self._out.flush()
        except Exception:                                     # noqa: BLE001
            pass                       # 控制台断了也不能影响落盘
        self._f.write(s)
        self._f.flush()
        return len(s)

    def flush(self) -> None:
        try:
            self._out.flush()
        except Exception:                                     # noqa: BLE001
            pass
        self._f.flush()

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:                                     # noqa: BLE001
            pass


# ======================================================================
# B + C：本次运行到底发生了什么
# ======================================================================
def _payload_text(payload) -> str:
    """从通信 payload 里取出人能读的消息正文。"""
    if not payload:
        return "(空 payload)"
    try:
        p = json.loads(payload)
    except Exception:                                         # noqa: BLE001
        return str(payload)[:400]
    if isinstance(p, dict):
        for key in ("text", "content", "body", "message"):
            v = p.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(p, ensure_ascii=False)[:400]
    return str(p)[:400]


def _report_messages(db_path: Path) -> None:
    """B：Agent 之间**真实发了什么**。终端上以前只有一个 ✉️ 箭头。"""
    print()
    print("-" * 68)
    print("  Agent 之间的消息（`communication_messages`，以前没有出口）")
    print("-" * 68)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT created_at, from_agent, to_agent, message_type, status, "
            "payload FROM communication_messages ORDER BY created_at").fetchall()
        con.close()
    except Exception as exc:                                  # noqa: BLE001
        print(f"  （读取失败：{exc}）")
        return

    if not rows:
        print("  ⚠️ 一条消息都没有 —— Agent 之间**完全没有沟通**")
        return

    for at, frm, to, mtype, status, payload in rows:
        print()
        print(f"  [{str(at)[11:19]}] {frm} → {to}"
              f"   ({mtype} / {status})")
        for line in _payload_text(payload).splitlines():
            print(f"      {line}")


def _report_turns(results_dir: Path, agents) -> None:
    """
    C：哪些 Agent 从没轮到 · 哪些 turn 没成功写入。

    🔒 这两类**都不报错**，所以从终端上完全看不出来。
    """
    print()
    print("-" * 68)
    print("  执行情况（results JSON，以前只打了「成功」的那些）")
    print("-" * 68)

    hits = sorted(results_dir.glob("*.json"))
    if not hits:
        print("  （没有结果 JSON —— 可能没跑到收尾）")
        return
    try:
        log = json.loads(hits[0].read_text(encoding="utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        print(f"  （读取 {hits[0].name} 失败：{exc}）")
        return

    turns = [t for s in log.get("sessions", []) for t in (s.get("turns") or [])]
    print(f"  退出原因 : {log.get('exit_reason')}")
    print(f"  轮次     : {len(turns)} 个 turn / {log.get('sessions_run')} 个 session")

    # ---- 谁从没轮到 ----
    seen = {t.get("agent_id") for t in turns}
    missing = [a for a in agents if a not in seen]
    if missing:
        print()
        print("  🔴 从没轮到的参与者（终端上只是少一行）:")
        for a in missing:
            print(f"       {a}")
        print("       ⇒ 它没有产出、没有发言、也没有报错。")
        print("         通常是因为墙钟/轮数在它之前用尽。")
    else:
        print("  ✅ 所有参与者都至少轮到过一次")

    # ---- 这一轮**什么都没做**的 turn ----
    #
    #   🔴 2026-10-09：实验改走**生产信封**之后，「没有 write」**不再**
    #      等于「什么都没做」—— Agent 这一轮可能选择的是
    #      **读 / 发消息 / 查资料**。
    #
    #      旧判据（`w is None` ⇒ NO_WRITE）会把那些正常轮次全部报成 🔴
    #      —— 报告开始**说谎**。判据改成「**整轮没有发起任何动作**」。
    #
    #   🔴 2026-10-10：**一轮可以有多个动作**了（多动作信封）。
    #      所以判据必须看**整个动作列表**，不能只看 `turn["action"]`
    #      （那只是第一个）—— 「有第一个」不代表「没有第二个」。
    from experiments.heterogeneous_production import production as P

    silent = []
    for t in turns:
        w = t.get("write")
        if w is not None and not w.get("ok"):
            silent.append((t, str(w.get("code") or "WRITE_FAILED"),
                           f"想写 {w.get('path')}（mode={w.get('mode')}）但被拒"))
        elif not P._turn_actions(t):
            silent.append((t, "NO_ACTION",
                           "这一轮没有发起任何动作（也没有写产物）"))

    if silent:
        print()
        print(f"  🔴 整轮没有动作的 turn：{len(silent)}/{len(turns)}"
              f"（每一轮都被记为 ok={True}，所以不看这里发现不了）")
        for t, code, why in silent:
            print(f"       r{t.get('round')} {t.get('agent_id'):22s} "
                  f"{code:16s} {why}")
    else:
        print(f"  ✅ 每个 turn 都至少发起了一个动作（{len(turns)}/{len(turns)}）")


def _report_calls(db_path: Path) -> None:
    """模型调用汇总（原样保留，加上 finish_reason 分布）。"""
    print()
    print("-" * 68)
    print("  模型调用")
    print("-" * 68)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT created_at, agent_id, finish_reason, total_tokens, "
            "actual_max_tokens FROM llm_calls ORDER BY created_at").fetchall()
        con.close()
    except Exception as exc:                                  # noqa: BLE001
        print(f"  （读取调用记录失败：{exc}）")
        return

    print(f"  共 {len(rows)} 次调用")
    for r in rows:
        flag = "  ⚠️ 撞到输出上限" if r[2] == "length" else ""
        print(f"    {str(r[0])[11:19]}  {r[1]:22s} {str(r[2]):8s} "
              f"{r[3]:>6} tok  max={r[4]}{flag}")
    n_len = sum(1 for r in rows if r[2] == "length")
    if n_len:
        print()
        print(f"  🔴 有 {n_len} 次 finish_reason=length（撞上输出 token 上限）。")
        print("     这类响应会解析失败 → **整轮不写入**，而且不报错。")
        print("     调大 --cognitive-budget 可以缓解。")


def _report(db_path: Path, results_dir: Path, agents) -> None:
    """把「本次运行发生了什么」一次说清 —— 重点是**没做成**的那些。"""
    print()
    print("=" * 68)
    print("  本次运行发生了什么")
    print("=" * 68)
    _report_calls(db_path)
    _report_turns(results_dir, agents)
    _report_messages(db_path)
    print()
    print("=" * 68)


# ======================================================================
def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="run_demand.py",
        description="给一个 Demand，让多个 Agent 按现有机制协作生产。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--demand", help="直接给一句 Demand")
    src.add_argument("--demand-file", help="从文本文件读 Demand")

    ap.add_argument("--agents", default=",".join(DEFAULT_AGENTS),
                    help=f"参与者，逗号分隔（默认：{','.join(DEFAULT_AGENTS)}）")
    ap.add_argument("--minutes", type=float, default=6.0,
                    help="总墙钟硬上限（分钟），默认 6")
    ap.add_argument("--session-minutes", type=float, default=3.0,
                    help="单个 Session 上限（分钟），默认 3")
    ap.add_argument("--rounds", type=int, default=3,
                    help="每个 Session 最多几轮，默认 3")
    ap.add_argument("--sessions", type=int, default=1,
                    help="最多跑几个 Session，默认 1（精确生效，见下方说明）")
    ap.add_argument("--budget", type=float, default=20.0,
                    help="生产预算（AIC），默认 20")
    ap.add_argument("--cognitive-budget", type=int, default=None,
                    help="单次响应的输出上限（tokens）。默认沿用实验默认值"
                         "（现为 16384；见 production.py 的 COGNITIVE_BUDGET）")
    ap.add_argument("--out", default=None,
                    help="输出目录（默认 experiments/user_demand/runs/<run_id>）")
    ap.add_argument("--reuse-db", action="store_true",
                    help="复用同一份 DB（默认每次新开，避免污染历史数据）")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    demand = args.demand
    if args.demand_file:
        demand = Path(args.demand_file).read_text(encoding="utf-8").strip()
    if not demand:
        print("❌ Demand 不能为空")
        return 2

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not agents:
        print("❌ 至少要有一个参与者")
        return 2

    # ---- 输出目录：每次运行一个独立目录（默认不复用 DB）----
    run_id = uuid.uuid4().hex[:8]
    outdir = (Path(args.out).expanduser().resolve() if args.out
              else HERE / "runs" / (f"{datetime.now():%m%d-%H%M}-{run_id}"))
    outdir.mkdir(parents=True, exist_ok=True)
    db_path = (outdir / "demand.db" if not args.reuse_db
               else HERE / "runs" / "demand.db")
    results_dir = outdir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # ---- 覆盖实验参数（**只改这个进程内的模块全局，不碰源码**）----
    from experiments.heterogeneous_production import production as P

    P.DEMAND = demand
    P.AGENTS = list(agents)
    P.OWNER = agents[0]
    P.TASK_ID = f"task_demand_{run_id}"
    P.WS_ID = f"WS-DEMAND-{run_id}"
    P.DB = db_path
    P.RESULTS = results_dir
    P.EXPERIMENT_ID = f"user_demand_{run_id}"
    P.TOTAL_WALL_LIMIT = max(60.0, args.minutes * 60.0)
    P.SESSION_LIMIT = max(30.0, args.session_minutes * 60.0)
    P.MAX_ROUNDS_PER_SESSION = max(1, args.rounds)
    P.BUDGET_AIC = float(args.budget)
    # 🔒 None = 不改，沿用 production.py 的默认（单一来源）。
    if args.cognitive_budget is not None:
        P.COGNITIVE_BUDGET = int(args.cognitive_budget)

    # 生产者 / 验收责任人必须是参与者。默认集合本来就包含它们；
    # 用户换 Agent 集时按位置对齐，避免完成链因为「不在参与者里」而失败。
    P.PRODUCER = agents[0]
    P.ACCEPTOR = agents[-1] if len(agents) > 1 else agents[0]

    # ---- 精确限制 session 数（§：外层循环只看总墙钟，没有 session 上限）----
    #
    # 背景：`production.main()` 的外层循环**唯一**的「不再开新 session」
    # 条件就是总墙钟：
    #
    #       budget_left = TOTAL_WALL_LIMIT - elapsed_total
    #       limit       = min(SESSION_LIMIT, budget_left)
    #       while True:
    #           if elapsed_total >= TOTAL_WALL_LIMIT: break
    #
    # 所以只给 `--minutes == --session-minutes` 是**脆的**：
    # session 1 若因为轮数用尽而**提前**结束，总墙钟还没到 → 会再开一个。
    #
    # 🔒 这里包一层 `_run_session`：跑满 N 个之后把总墙钟置 0，
    #    下一次循环判断必然成立 → 立刻退出。
    #    **不改 production.py**，只在本进程内替换模块全局。
    target_sessions = max(1, int(args.sessions))
    _orig_run_session = P._run_session
    _session_calls = {"n": 0}

    def _counting_run_session(rt_, bridge_, index, limit, log_, stats_):
        _session_calls["n"] += 1
        sess, bridge_, s_log = _orig_run_session(
            rt_, bridge_, index, limit, log_, stats_)
        if _session_calls["n"] >= target_sessions:
            # 让外层循环下一轮立刻退出（它读的是模块全局）
            P.TOTAL_WALL_LIMIT = 0.0
        return sess, bridge_, s_log

    P._run_session = _counting_run_session

    # ---- 🔒 这里**不再**包装 `_ensure_environment` ----
    #
    # 以前这里要"抢建"一个 Task，好把 `_ensure_environment` 里
    # **写死的那份** description（那部 10000 字科幻小说）顶掉。
    #
    # 现在不需要了 —— `production.py` 已经改成：
    #
    #     Task.description ← DEMAND        （唯一来源）
    #     Stage.goal       ← DEMAND        （唯一来源）
    #     Task.creator     ← PRODUCER      （ADR-0024：Creator 始终是 Agent）
    #
    # 于是覆盖 `P.DEMAND` 就等于同时改了两处，**一条创建路径**。
    #
    # 🔒 这本身修掉了一个真 bug：以前 Task 说"修仙第一章"、
    #    Stage 说"10000 字科幻悬疑小说"，Agent 收到两个矛盾目标。
    #    你那次运行里 `novel_reader` **自己在消息里指出了这个冲突**
    #    并裁决「以当前 Task 为目标」—— 它处理得比我好。

    # ---- A：从这里开始，stdout 同时落盘 ----
    log_path = outdir / "run.log"
    tee = _Tee(log_path, sys.stdout)
    original_stdout = sys.stdout
    sys.stdout = tee
    rc = 0
    try:
        print("=" * 68)
        print("  用户 Demand → 多 Agent 协作生产")
        print("=" * 68)
        print(f"  Demand  : {demand[:100]}{'…' if len(demand) > 100 else ''}")
        print(f"  参与者  : {', '.join(agents)}")
        print(f"  墙钟上限: {P.TOTAL_WALL_LIMIT / 60:.1f} 分钟 "
              f"（单 Session {P.SESSION_LIMIT / 60:.1f} 分钟，最多 "
              f"{P.MAX_ROUNDS_PER_SESSION} 轮/Session）")
        print(f"  Session : 最多 {target_sessions} 个（精确生效）")
        print(f"  预算    : {P.BUDGET_AIC} AIC")
        print(f"  认知预算: {P.COGNITIVE_BUDGET} tokens/次（输出上限，"
              f"reasoning 与正文共享）")
        print(f"  输出    : {outdir}")
        print(f"  数据库  : {db_path}")
        print(f"  本次日志: {log_path}")
        print("=" * 68)
        print()

        # Demand 先落盘 —— 即使后面崩了，也知道当时要的是什么。
        (outdir / "demand.txt").write_text(demand, encoding="utf-8")

        rc = P.main()
    finally:
        # ---- B + C：把"没做成的事"露出来 ----
        #
        # 🔴 放在 `finally` 里：**崩掉的时候正是最需要看这个的时候**。
        #    只放在成功路径上的话，最需要证据的那一次恰好没有报告。
        try:
            _report(db_path, results_dir, agents)
        except Exception as exc:                              # noqa: BLE001
            print(f"  （生成运行报告失败：{exc}）")

        print()
        print("=" * 68)
        print("  产物位置")
        print("=" * 68)
        print(f"  结果 JSON : {results_dir / 'heterogeneous_production_result.json'}")
        print(f"  数据库    : {db_path}")
        print(f"  Demand    : {outdir / 'demand.txt'}")
        print(f"  完整日志  : {log_path}")
        print()
        print("  看产物全文：")
        print(f"    ./.venv/bin/python experiments/user_demand/view_result.py "
              f"--path {outdir}")
        print("=" * 68)

        sys.stdout = original_stdout
        tee.close()
        # 控制台也提一句，免得用户不知道日志在哪。
        # （这一句故意**不进** run.log —— 此时 stdout 已还原。）
        print(f"\n  （本次运行的完整输出已保存到 {log_path}）")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
