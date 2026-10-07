#!/usr/bin/env python3
"""
Heterogeneous 5-Agent 真实 API 预检（§十六）
================================================================

    ./.venv/bin/python -u experiments/heterogeneous_production/preflight.py

目的：在正式长跑**之前**，用 **1～2 次真实调用/Agent** 确认：
  · simulated=False
  · Credential source / env 正确
  · **5 个 fingerprint 互不相同**
  · requested_model 正确
  · response_model 被记录
  · output_status = VALID_CONTENT
  · Token / AIC 正常
  · requested_cognitive_budget 生效

🔒 绝不打印任何 key。
🔒 预检**不通过就不进正式生产**（§二十七）。
🔒 使用**独立** DB（§十七），不污染 novel.db。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent

DB = HERE / "data" / "hetero.db"
OUT = HERE / "results" / "heterogeneous_preflight.json"

AGENTS = ["novel_world_builder", "novel_plot_architect", "novel_author",
          "novel_editor", "novel_reader"]

#: 每个 Agent 请求的认知预算（§九）。Runtime 会裁剪。
COGNITIVE_BUDGET = 8192

PROMPT = ("请用一句话说明：你在这次小说生产里负责什么？"
          "（不超过 30 字，直接回答，不要 JSON。）")


def main() -> int:
    from main import build_runtime

    DB.parent.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rt = build_runtime(db_path=str(DB), agent_ids=AGENTS)
    rows = []
    try:
        for aid in AGENTS:
            a = next(x for x in rt.economy.all_agents()
                     if x.agent_id == aid)
            b = a.brain
            prov = b.settings.provider

            # --- 预检（不发 API）：凭据 + 模型 ---
            info = b.preflight_credential()
            cred = b._credential_for(prov)
            fp = cred.describe()["fingerprint"]

            t0 = time.perf_counter()
            try:
                r = a.generate(messages=PROMPT,
                               requested_cognitive_budget=COGNITIVE_BUDGET)
                err = None
            except Exception as exc:                          # noqa: BLE001
                r, err = None, f"{type(exc).__name__}: {exc}"

            row = {
                "agent_id": aid,
                "provider": prov,
                "configured_model": b.settings.model or "(env)",
                "requested_model": getattr(r, "requested_model", None),
                "response_model": getattr(r, "response_model", None),
                "model_mismatch": getattr(r, "model_mismatch", None),
                "credential_ref": info.get("credential_ref"),
                "credential_source": info.get("credential_source"),
                "credential_env": (b._credential_config or {}).get(
                    "api_key_env"),
                "credential_fingerprint": fp[:8],
                "credential_available": info.get("credential_available"),
                "calls": 1 if r else 0,
                "simulated": getattr(r, "simulated", None),
                "input_tokens": getattr(r, "input_tokens", None),
                "output_tokens": getattr(r, "output_tokens", None),
                "total_tokens": getattr(r, "total_tokens", None),
                "token_source": getattr(r, "token_source", None),
                "finish_reason": getattr(r, "finish_reason", None),
                "output_status": getattr(r, "output_status", None),
                "requested_cognitive_budget": getattr(
                    r, "requested_cognitive_budget", None),
                "actual_max_tokens": getattr(r, "actual_max_tokens", None),
                "chars": len(getattr(r, "content", "") or ""),
                "content_head": (getattr(r, "content", "") or "")[:120],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": err,
            }
            rows.append(row)
            rpm = row["requested_model"] or "?"
            rsm = row["response_model"] or "?"
            print(f"  {aid:22s} {prov:13s} req={rpm:16s} resp={rsm:16s} "
                  f"fp={fp[:8]} {row['output_status'] or 'ERR'} "
                  f"{row['total_tokens']}tok")

        # --- 判定 ---
        distinct_fp = len({r["credential_fingerprint"] for r in rows})
        checks = {
            "all_have_calls": all(r["calls"] == 1 for r in rows),
            "all_simulated_false": all(r["simulated"] is False for r in rows),
            "all_credentials_available": all(
                r["credential_available"] for r in rows),
            "five_distinct_fingerprints": distinct_fp == len(rows),
            "all_valid_content": all(
                r["output_status"] == "VALID_CONTENT" for r in rows),
            "all_requested_model_recorded": all(
                r["requested_model"] for r in rows),
            "all_response_model_recorded": all(
                r["response_model"] for r in rows),
            "all_have_tokens": all(r["total_tokens"] for r in rows),
            "all_have_finish_reason": all(
                r["finish_reason"] for r in rows),
            "budget_recorded": all(
                r["requested_cognitive_budget"] == COGNITIVE_BUDGET
                for r in rows),
            "distinct_providers": len({r["provider"] for r in rows}),
        }
        ok = all(v is True for k, v in checks.items()
                 if k not in ("distinct_providers",))
        checks["PASS"] = ok

        cost = rt.economy.db.fetchall(
            "SELECT COUNT(*) n, SUM(total_tokens) t, ROUND(SUM(cost_ai),6) c "
            "FROM llm_calls")[0]
        report = {
            "purpose": ("Heterogeneous 5-Agent 真实 API 预检。"
                        "预检不通过则不进入正式生产（§二十七）。"),
            "db": str(DB),
            "cognitive_budget_requested": COGNITIVE_BUDGET,
            "agents": rows,
            "checks": checks,
            "totals": {"calls": int(cost["n"] or 0),
                       "tokens": int(cost["t"] or 0),
                       "aic": float(cost["c"] or 0.0)},
            "distinct_fingerprints": distinct_fp,
            "distinct_providers": checks["distinct_providers"],
        }
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    finally:
        try:
            rt.economy.close()
        except Exception:                                     # noqa: BLE001
            pass

    print("\n=== 预检判定 ===")
    for k, v in report["checks"].items():
        print(f"  {k:34s} {v}")
    print(f"\n  不同 fingerprint 数 = {distinct_fp} / {len(rows)}")
    print(f"  合计 {report['totals']['calls']} 次调用 / "
          f"{report['totals']['tokens']} tokens / "
          f"{report['totals']['aic']} AIC")
    print(f"\n  → {OUT}")
    return 0 if report["checks"]["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
