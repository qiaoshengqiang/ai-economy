#!/usr/bin/env python3
"""
查看 run_demand.py 跑出来的产物（**只读**）
=============================================

用法
----

    # ① 列出所有 run（最近的在最上面）
    ./.venv/bin/python experiments/user_demand/view_result.py --list

    # ② 看最近一次 run 的**全部文件**
    ./.venv/bin/python experiments/user_demand/view_result.py

    # ③ 只看手稿（路径里含 manuscript 的）
    ./.venv/bin/python experiments/user_demand/view_result.py --file manuscript

    # ④ 看指定的某一次 run（名字从 --list 里抄）
    ./.venv/bin/python experiments/user_demand/view_result.py \
        --run 1009-1642-9366aaf7

    # ⑤ 看任意目录（比如 --out 指到过别处）
    ./.venv/bin/python experiments/user_demand/view_result.py --path /tmp/demand_1session

    # ⑥ 只要概览，不要正文
    ./.venv/bin/python experiments/user_demand/view_result.py --summary

🔒 只读：不写任何文件、不碰数据库、不调用 LLM。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"


# ----------------------------------------------------------------------
def _last_activity(db: Path) -> str:
    """
    run 的**真实**最后活动时间（取自数据库，不是文件 mtime）。

    🔴 为什么不能用 mtime：实测五个 run 的 mtime 会全部变成同一秒
       （别的进程一碰就被刷新），按 mtime 排序等于随机。
       数据库里的 `llm_calls.created_at` 才是这个 run 真的跑到哪。
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            got = con.execute("SELECT MAX(created_at) FROM llm_calls").fetchone()
            return (got[0] if got and got[0] else "")
        finally:
            con.close()
    except Exception:                                     # noqa: BLE001
        return ""


def _runs(include_tmp: bool = False) -> list:
    """候选 run 目录，按**真实活动时间**倒序（最近的在最前）。"""
    found = []
    if RUNS_DIR.is_dir():
        found += [d for d in RUNS_DIR.iterdir() if (d / "demand.db").exists()]
    if include_tmp:
        tmp = Path("/tmp")
        if tmp.is_dir():
            found += [d for d in tmp.glob("demand*")
                      if (d / "demand.db").exists()]
    return sorted(found, key=lambda d: _last_activity(d / "demand.db"),
                  reverse=True)


def _summary(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        files = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(COALESCE(content,''))), 0) "
            "FROM workspace_resources WHERE resource_type='file'").fetchone()
        calls = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens),0) FROM llm_calls"
        ).fetchone()
        reasons = dict(con.execute(
            "SELECT COALESCE(finish_reason,'(null)'), COUNT(*) "
            "FROM llm_calls GROUP BY finish_reason"))
        return {"files": files[0], "chars": files[1],
                "calls": calls[0], "tokens": calls[1], "reasons": reasons}
    finally:
        con.close()


def _calls(db: Path) -> list:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [dict(zip(
            ("agent", "finish", "out_tok", "in_tok", "max_tok", "at"), r))
            for r in con.execute(
                "SELECT agent_id, finish_reason, output_tokens, input_tokens, "
                "actual_max_tokens, created_at FROM llm_calls "
                "ORDER BY created_at")]
    finally:
        con.close()


def _files(db: Path, pattern: str | None) -> list:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sql = ("SELECT path, content, revision, created_by, updated_at "
               "FROM workspace_resources WHERE resource_type='file'")
        rows = con.execute(sql + " ORDER BY path").fetchall()
    finally:
        con.close()
    if pattern:
        low = pattern.lower()
        rows = [r for r in rows if low in (r[0] or "").lower()]
    return rows


# ----------------------------------------------------------------------
def cmd_list(include_tmp: bool = False) -> int:
    runs = _runs(include_tmp)
    if not runs:
        print("没有找到任何 run。先跑一次 run_demand.py。")
        return 1
    print(f"{'最后活动':19s} {'目录名':22s} {'文件':>4s} {'字符':>6s} "
          f"{'调用':>4s} {'tokens':>7s}  finish_reason")
    print("-" * 100)
    for d in runs:
        s = _summary(d / "demand.db")
        rs = ", ".join(f"{k}={v}" for k, v in sorted(s["reasons"].items()))
        print(f"{_last_activity(d / 'demand.db')[:19]:19s} {d.name:22s} "
              f"{s['files']:4d} {s['chars']:6d} {s['calls']:4d} "
              f"{s['tokens']:7d}  {rs}")
    print()
    print("看最近一次：直接跑（不加参数）     看某一次：--run <目录名>")
    if not include_tmp:
        print("（默认只列你自己的 runs/。加 --all 连 /tmp/demand* 一起列。）")
    return 0


def cmd_show(run: Path, pattern: str | None, summary_only: bool) -> int:
    db = run / "demand.db"
    if not db.exists():
        print(f"❌ 找不到 {db}")
        return 1

    s = _summary(db)
    print("=" * 74)
    print(f"  {run}")
    print("=" * 74)
    print(f"  文件 {s['files']} 个 / 共 {s['chars']} 字符"
          f"   ·   模型调用 {s['calls']} 次 / {s['tokens']} tokens")
    print(f"  finish_reason: {s['reasons']}")
    if s["reasons"].get("length"):
        print("  ⚠️ 出现了 finish_reason=length（有调用撞到输出 token 上限）")
    demand = run / "demand.txt"
    if demand.exists():
        first = demand.read_text(encoding="utf-8").strip().splitlines()
        if first:
            print(f"  Demand: {first[0][:80]}")

    print()
    print("  ── 调用明细 ──")
    for c in _calls(db):
        print(f"    {c['at'][11:19]}  {c['agent']:22s} finish={str(c['finish']):6s}"
              f" out={c['out_tok']:>5} in={c['in_tok']:>5} max={c['max_tok']}")

    if summary_only:
        print()
        print("  （--summary：不打印正文。去掉它就能看全文。）")
        return 0

    rows = _files(db, pattern)
    if not rows:
        print(f"\n  （没有匹配 {pattern!r} 的文件）")
        return 0

    for path, content, rev, who, at in rows:
        content = content or ""
        print()
        print("=" * 74)
        print(f"  {path}")
        print(f"  R{rev}  {len(content)} 字符  by {who}  {at}")
        print("=" * 74)
        print(content)

    print()
    print("=" * 74)
    return 0


# ----------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="view_result.py",
        description="查看 run_demand.py 的产物（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="列出所有 run")
    ap.add_argument("--all", action="store_true",
                    help="连 /tmp/demand* 一起列（默认只看 runs/）")
    ap.add_argument("--run", help="run 目录名（从 --list 里抄）")
    ap.add_argument("--path", help="直接指定 run 目录")
    ap.add_argument("--file", help="只看路径里含这个子串的文件，如 manuscript")
    ap.add_argument("--summary", action="store_true", help="只要概览，不打印正文")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list(args.all)

    if args.path:
        return cmd_show(Path(args.path).expanduser().resolve(),
                        args.file, args.summary)

    if args.run:
        cand = RUNS_DIR / args.run
        if not (cand / "demand.db").exists():
            hits = [d for d in _runs() if d.name == args.run]
            if not hits:
                print(f"❌ 找不到 run {args.run!r}。用 --list 看看有哪些。")
                return 1
            cand = hits[0]
        return cmd_show(cand, args.file, args.summary)

    runs = _runs(args.all)
    if not runs:
        print("没有找到任何 run。先跑一次 run_demand.py。")
        return 1
    return cmd_show(runs[0], args.file, args.summary)


if __name__ == "__main__":
    raise SystemExit(main())
