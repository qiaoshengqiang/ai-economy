#!/usr/bin/env python3
"""
实验：对旧 run（1107-1602）的产物做一次**独立验收回合**
========================================================

⚠️ **历史脚本，不用于 1107-1714 的 human-command run。**
   它针对 1107-1602 那个 3-agent 网页任务；那次主运行没有走到
   `qa/review.md`。1107-1714 的扫雷运行已经由 Agent 自己完成验收，
   不需要这个补跑脚本。

背景：旧的 `run_web.py` 主运行在墙钟内产出了 `site/index.html`，
但没有走到 `qa/review.md`。本脚本**复用同一个 Task / Workspace / DB**，
只把验收人 `agent_c` 唤醒一个回合，让它读交付物并写验收结论。

🔒 不创建新 Task，不改任何生产源码。
🔒 只用于实验观察；它的存在本身就是「主运行没走到验收」的证据。
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")

# 与 run_web.py 保持一致
AGENTS = ["agent_a", "agent_b", "agent_c"]
TASK_ID = "task_web_8c42c6d6"
WS_ID = "WS-WEB-8c42c6d6"
PRODUCER = "agent_b"
ACCEPTOR = "agent_c"

REVIEW_DEMAND = """【验收回合】

`site/index.html` 已经由实现者 agent_b 交付。
你是本 Task 的独立验收人 agent_c，且你不是实现者。

请读取：

- `site/index.html`（交付物）
- `brief/content.md`（文案与信息结构的权威来源）
- `design/layout.md`（布局与交互的权威来源）

逐条对照本 Task 的 Definition of Done，形成**你自己的**验收结论。
必须产出 `qa/review.md`，至少包含：

- 检查项
- 实际结果
- 每项 PASS / FAIL
- 若发现 brief 与 design（或页面）互相冲突，如实写出冲突点
- 验收人 / 被验收人

🔒 不要修改 `site/index.html` —— 你只验收，不实现。
"""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def main() -> int:
    run_dir = (Path(sys.argv[1]).expanduser().resolve()
               if len(sys.argv) > 1 else
               HERE / "runs" / "1107-1602-8c42c6d6")
    db = run_dir / "web.db"
    results = run_dir / "results"
    if not db.exists():
        print(f"❌ 找不到 {db}")
        return 2

    from experiments.web_collab.run_web import (
        DEMAND, DEFINITION_OF_DONE, NAMESPACES, _deliverable_ref, _STATE,
    )
    from experiments.heterogeneous_production import production as P
    import channels
    from main import build_runtime
    from stage import StageStore
    from stage.context import render_stage

    # ---- 让 production.py 的模块全局指向**已有的** Task/Workspace ----
    P.DEMAND = DEMAND
    P.AGENTS = list(AGENTS)
    P.OWNER = "agent_a"
    P.PRODUCER = PRODUCER
    P.ACCEPTOR = ACCEPTOR
    P.TASK_ID = TASK_ID
    P.WS_ID = WS_ID
    P.DB = db
    P.RESULTS = results
    P.EXPERIMENT_ID = "web_collab_review"
    P.ARTIFACT_NAMESPACES = dict(NAMESPACES)
    P.DEFINITION_OF_DONE = DEFINITION_OF_DONE
    P.COGNITIVE_BUDGET = 16384
    P.TOTAL_WALL_LIMIT = 600
    P.SESSION_LIMIT = 300
    P.MAX_ROUNDS_PER_SESSION = 4
    P.MIN_PRODUCTIVE_CHARS_PER_SESSION = 100

    _STATE["workspace_id"] = WS_ID
    _STATE["producer"] = PRODUCER
    P._manuscript_ref = _deliverable_ref

    rt = build_runtime(db_path=str(db), agent_ids=list(AGENTS))
    bridge = channels.SyncBridge()
    log = {"experiment_run_id": f"review_{uuid.uuid4().hex[:8]}",
           "turns": [], "comm_events": []}
    P.EXPERIMENT_RUN_ID = log["experiment_run_id"]
    stats = {a: {"calls": 0, "valid": 0, "empty": 0, "tokens": 0,
                 "writes_ok": 0, "writes_fail": 0, "messages": 0,
                 "chars_written": 0, "created": 0, "appended": 0,
                 "replaced": 0} for a in AGENTS}
    try:
        dispatcher, experience_store = P._action_seam(rt, bridge)
        P._install_context_recorder(rt)
        st = StageStore(rt.economy.db)
        stages = st.list_stages(TASK_ID)
        stage = next((s for s in stages if s.is_open), stages[0] if stages else None)
        ckpt = st.latest_checkpoint(stage.stage_id) if stage else None
        stage_txt = (render_stage(stage, checkpoint=ckpt,
                                  limit_seconds=600, elapsed_seconds=0,
                                  rounds_left=3) if stage else "(no stage)")

        qa_path = run_dir / "qa_review.md"
        for rnd in range(1, 4):
            turn = P._turn(
                rt, bridge, st, stage, ACCEPTOR, rnd, stage_txt,
                log, stats, dispatcher=dispatcher,
                experience_store=experience_store,
                demand=REVIEW_DEMAND,
                prompt_template=P.PROMPT,
            )
            P._print_turn(ACCEPTOR, turn)
            log["turns"].append(turn)
            got = rt.workspace_gateway.read(WS_ID, "/qa/review.md", ACCEPTOR)
            if got.success and ((got.result or {}).get("content") or "").strip():
                qa_path.write_text((got.result or {}).get("content") or "",
                                   encoding="utf-8")
                print(f"\n  ✅ qa/review.md 已生成：{qa_path}")
                break
        log["qa_exists"] = qa_path.exists()
        log["task_status"] = str(getattr(rt.tasks.get(TASK_ID), "status", "?"))
    finally:
        bridge.close()
        rt.close()

    out = results / "web_review_result.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  结果 JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
