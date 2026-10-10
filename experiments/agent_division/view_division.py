#!/usr/bin/env python3
"""
查看观察实验（agent_division）跑出来的结果（**只读**）
========================================================

【为什么需要它】

    用户 2026-10-10：「把实验结果查询的命令再发我一下我想看看实验结果。」

    实测发现：这个实验跑完之后，**除了 `division.db` 和一份
    `division_result.json`，什么都没有** ——

        · 控制台那份分析**没有落盘**（连 `run.log` 都没有）
        · 也没有任何「看结果」的命令
        （`user_demand/` 有 `view_result.py`，这里没有）

    ⇒ 想看结果只能自己去翻 JSON。这个脚本补上那一步。

用法
----

    # ① 列出所有 run（按**真实活动时间**倒序）
    ./.venv/bin/python experiments/agent_division/view_division.py --list

    # ② 看最近一次（不加参数就是它）
    ./.venv/bin/python experiments/agent_division/view_division.py

    # ③ 看某一次（名字从 --list 里抄）
    ./.venv/bin/python experiments/agent_division/view_division.py \
        --run 1010-1009-b2a8f4fb

    # ④ 逐轮细节：每一轮它想了什么、做了什么（含**两段判断**）
    ./.venv/bin/python experiments/agent_division/view_division.py --turns

    # ⑤ 只要结论，不要细节
    ./.venv/bin/python experiments/agent_division/view_division.py --summary

    # ⑥ 原始 JSON（要自己 jq 的时候）
    ./.venv/bin/python experiments/agent_division/view_division.py --json

🔒 只读：不写任何文件、不碰数据库（`mode=ro`）、不调用 LLM。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"


# ----------------------------------------------------------------------
def _last_activity(db: Path) -> str:
    """
    run 的**真实**最后活动时间（取自数据库，不是文件 mtime）。

    🔴 为什么不用 mtime：`view_result.py` 已经踩过这个坑 ——
       别的进程一碰 mtime 就全变成同一秒，按它排序等于随机。
       数据库里 `llm_calls.created_at` 才是这个 run 真的跑到哪。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            got = con.execute(
                "SELECT MAX(created_at) FROM llm_calls").fetchone()
            return got[0] if got and got[0] else ""
        finally:
            con.close()
    except Exception:                                         # noqa: BLE001
        return ""


def _runs() -> list:
    """有 `division.db` 的 run 目录，按真实活动时间倒序。"""
    if not RUNS_DIR.is_dir():
        return []
    found = [d for d in RUNS_DIR.iterdir() if (d / "division.db").exists()]
    return sorted(found, key=lambda d: _last_activity(d / "division.db"),
                  reverse=True)


def _load(run: Path) -> dict:
    hits = sorted((run / "results").glob("*.json"))
    if not hits:
        raise SystemExit(f"❌ {run}/results/ 里没有 JSON —— 这次跑到收尾了吗？")
    raw = json.loads(hits[0].read_text(encoding="utf-8"))
    return raw.get("log") or raw


def _cost(db: Path) -> dict:
    """真实成本 —— 从 `llm_calls` 表算（每次调用一行）。"""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            r = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), "
                "COALESCE(SUM(cost_ai),0) FROM llm_calls").fetchone()
            return {"calls": int(r[0]), "tokens": int(r[1]),
                    "aic": round(float(r[2]), 4)}
        finally:
            con.close()
    except Exception:                                         # noqa: BLE001
        return {"calls": 0, "tokens": 0, "aic": 0.0}


def _shared_context(db: Path) -> list:
    """
    这个 Task 的共同认知**当前**长什么样。

    🔴 为什么单独列：`assign_acceptance` 的成败就写在这里 ——
       「Agent 有没有真的把活分出去」最直接的证据之一。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            out = []
            for row in con.execute(
                    "SELECT revision, payload, created_by, change_reason "
                    "FROM task_context_revisions ORDER BY revision"):
                try:
                    payload = json.loads(row["payload"])
                except Exception:                             # noqa: BLE001
                    payload = {}
                out.append({"revision": row["revision"],
                            "by": row["created_by"],
                            "why": row["change_reason"],
                            "assignments": payload.get("assignments") or [],
                            "objective": payload.get("objective") or ""})
            return out
        finally:
            con.close()
    except Exception:                                         # noqa: BLE001
        return []


# ----------------------------------------------------------------------
def cmd_list() -> int:
    runs = _runs()
    if not runs:
        print("没有找到任何 run。先跑一次：")
        print("  ./.venv/bin/python experiments/agent_division/run_division.py")
        return 1

    print(f"{'最后活动':20s} {'目录':22s} {'轮':>3s} {'turn':>4s} "
          f"{'调用':>4s} {'tokens':>7s} {'AIC':>8s}  结束原因")
    print("-" * 104)
    for d in runs:
        try:
            log = _load(d)
        except SystemExit:
            continue
        c = _cost(d / "division.db")
        print(f"{_last_activity(d / 'division.db')[:19]:20s} {d.name:22s} "
              f"{log.get('rounds', 0):3d} {len(log.get('turns') or []):4d} "
              f"{c['calls']:4d} {c['tokens']:7d} {c['aic']:8.3f}  "
              f"{log.get('stop_reason')}")

    print()
    print("看最近一次：直接跑（不加参数）     看某一次：--run <目录名>")
    print("逐轮细节：--turns                  只要结论：--summary")
    return 0


def _print_head(log: dict, run: Path, cost: dict) -> None:
    cp = log.get("report") or {}
    print("=" * 76)
    print(f"  {run.name}   ({run})")
    print("=" * 76)
    print(f"  Demand     : {str(log.get('demand') or '').splitlines()[0][:70]}")
    print(f"  第一个 Agent: {log.get('first_agent')}")
    print(f"  结束原因   : {log.get('stop_reason')}   "
          f"（{log.get('rounds')} 轮 / {len(log.get('turns') or [])} 个 turn）")
    print(f"  墙钟       : {log.get('wall_seconds')}s")
    print(f"  Task 状态  : {log.get('task_status')}")
    print(f"  🔴 成本    : {cost['calls']} 次模型调用 / "
          f"{cost['tokens']} tokens / {cost['aic']} AIC")
    print()

    # ---- 这次有没有获得观察（其余一切结论的前提）----
    if cp.get("observed"):
        print("  ✅ **获得观察**：第一个 Agent 跑过，而且那一轮有产出")
    else:
        print(f"  🔴 **没有获得观察**：{cp.get('why_not_observed')}")
        print("     ⇒ 「它有没有把工作分出去」这个问题**这次没有被问到**。")
        print("     ⚠️ 不要把它读成「它没找人」。")

    fake = log.get("injected_fake_history") or {}
    if fake:
        print()
        print(f"  🔴 **假履历**：已注入 {len(fake)} 个 Agent 的"
              f"**测试用**信誉/历史（不是真实数据）"
              f" · seed={log.get('history_seed')}")
        for aid, v in sorted(fake.items()):
            print(f"       {aid:10s} 完成任务={v.get('completed_tasks')} "
                  f"失败={v.get('failed_tasks')} "
                  f"成功率={v.get('success_rate')} "
                  f"声誉={v.get('reputation_score')}")


def _print_verdict(log: dict) -> None:
    """分工有没有发生 —— 三个独立证据。"""
    cp = log.get("report") or {}
    print()
    print("  ── 分工的三个独立证据 ──")
    acted = cp.get("agents_acted") or []
    msgs = cp.get("messages") or []
    print(f"   ① 参与过生产的 Agent : {len(acted)} / "
          f"{len(log.get('agents') or [])}   {acted}")
    never = cp.get("agents_never_woken") or []
    if never:
        print(f"      从没被唤醒的       : {never}"
              f"   ← 不是排班不公，是**没人把活分给它们**")
    print(f"   ② 消息（谁找了谁）   : "
          f"{'**一条都没有**' if not msgs else ''}")
    for m in msgs:
        print(f"       {str(m.get('from')):22s} → {m.get('to')}")
    print(f"   ③ 每个 Agent 的动作  :")
    for aid, acts in sorted((cp.get("per_agent_actions") or {}).items()):
        print(f"       {aid:22s} {acts}")

    assigns = []
    for t in log.get("turns") or []:
        for a in (t.get("actions") or []):
            if a.get("type") == "assign_acceptance":
                assigns.append((t.get("agent_id"), a.get("ok"),
                                a.get("code")))
    print()
    if assigns:
        for who, ok, code in assigns:
            mark = "✅ 成功" if ok else f"❌ 失败（{code}）"
            print(f"   🔴 **指派验收责任**：{who} → {mark}")
    else:
        print("   ⬜ 没有任何人指派过验收责任（assign_acceptance）")


def _print_workspace(log: dict) -> None:
    tree = log.get("final_workspace") or log.get("report", {}).get(
        "workspace_tree") or []
    print()
    print(f"  ── 最终工作区（{len(tree)} 个条目）──")
    if not tree:
        print("       **空的** —— 没有任何人写下任何东西。")
        return
    for e in tree:
        print(f"       {str(e.get('path')):44s} {str(e.get('type')):10s} "
              f"R{e.get('revision')}  "
              f"责任人={e.get('holder')}")


def _print_context(rows: list) -> None:
    print()
    print(f"  ── Task 共同认知（Shared Context，{len(rows)} 版）──")
    if not rows:
        print("       ⚠️ **没有 Shared Context** —— "
              "这条 Task 的对齐动作全都会失败")
        return
    for r in rows:
        print(f"       rev{r['revision']}  by {r['by']}   「{r['why']}」")
        if r["objective"]:
            print(f"           objective: {r['objective'][:70]}")
        for a in r["assignments"]:
            print(f"           🔴 指派 {a.get('responsibility')} "
                  f"→ {a.get('agent_id')}  "
                  f"（by {a.get('assigned_by')}，{a.get('status')}）")


def _print_turns(log: dict) -> None:
    turns = log.get("turns") or []
    print()
    print("=" * 76)
    print(f"  逐轮细节（{len(turns)} 个 turn）")
    print("=" * 76)

    for t in turns:
        print()
        print(f"── 第 {t.get('round')} 轮  {t.get('agent_id')}  "
              f"ok={t.get('ok')}  tokens={t.get('tokens')}  "
              f"finish={t.get('finish_reason')} "
              f"output={t.get('output_status')} ──")

        if t.get("error"):
            print(f"   🔴 出错：{t['error']}")

        # 🔴 读到内容的时候一轮有**两段判断** —— 第二段才是「读完之后的决定」
        segs = t.get("segments") or []
        reasonings = t.get("reasonings") or (
            [t["reasoning"]] if t.get("reasoning") else [])
        if t.get("follow_up_used"):
            print(f"   🔁 **读到内容 ⇒ 当场回喂、又调了一次**"
                  f"（{len(segs)} 段 · tokens/段={t.get('tokens_by_segment')}）")
        for i, r in enumerate(reasonings):
            tag = "读之前" if i == 0 else "读之后"
            print(f"   第{i}段判断（{tag}）：{r}")

        acts = t.get("actions") or ([t["action"]] if t.get("action") else [])
        if not acts:
            print("   动作：**（没有发起任何动作）**")
        for a in acts:
            mark = "✅" if a.get("ok") else f"❌{a.get('code')}"
            extra = ""
            if a.get("path"):
                extra += f"  path={a['path']}"
            if a.get("chars"):
                extra += f"  chars={a['chars']}"
            if a.get("to"):
                extra += f"  to={a['to']}"
            print(f"   动作：{mark}  {a.get('type')}{extra}")


# ----------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="查看观察实验结果（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[-1] if __doc__ else None)
    ap.add_argument("--list", action="store_true", help="列出所有 run")
    ap.add_argument("--run", help="run 目录名（从 --list 里抄）")
    ap.add_argument("--path", help="直接指定 run 目录")
    ap.add_argument("--turns", action="store_true", help="打印逐轮细节")
    ap.add_argument("--summary", action="store_true", help="只要结论")
    ap.add_argument("--json", action="store_true", help="打印原始 JSON")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list()

    if args.path:
        run = Path(args.path).resolve()
    elif args.run:
        run = RUNS_DIR / args.run
    else:
        runs = _runs()
        if not runs:
            print("没有找到任何 run。先跑一次：")
            print("  ./.venv/bin/python "
                  "experiments/agent_division/run_division.py")
            return 1
        run = runs[0]

    if not run.is_dir():
        print(f"❌ 找不到 {run}")
        return 1

    log = _load(run)

    if args.json:
        print(json.dumps(log, ensure_ascii=False, indent=2))
        return 0

    db = run / "division.db"
    _print_head(log, run, _cost(db))
    _print_verdict(log)
    if not args.summary:
        _print_workspace(log)
        _print_context(_shared_context(db))

    if args.turns and not args.summary:
        _print_turns(log)

    print()
    print("─" * 76)
    print(f"  原始 JSON : {run}/results/")
    print(f"  数据库    : {db}")
    print(f"  逐轮细节  : 加 --turns     只要结论：--summary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
