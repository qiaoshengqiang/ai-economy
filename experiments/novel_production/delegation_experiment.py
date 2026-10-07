#!/usr/bin/env python3
"""
Communication vs Task Delegation —— 真实 Agent 经济边界实验
================================================================

    ./.venv/bin/python experiments/novel_production/delegation_experiment.py

【本轮要回答的唯一问题】

    AIECO 能否把「生产性任务委托」从普通 Communication 中清晰分离出来，
    并让它真正进入 AIC 经济系统？

【🔴 实验纪律（§十七 / §二十一）】

    ❌ 不硬编码价格（没有 `Editor = 5 AIC`）
    ❌ 不自动接受（没有 `if price < X: accept()`）
    ❌ 不自动结算（Author 要**自己决定**交付物是否可接受）
    ❌ 不告诉 Agent「请考虑 AIC」
    ✅ Runtime 只做一件事：**验证并执行已经由 Agent 决定的交易**

    如果真实 Agent 没有主动形成委托 —— **如实报告**，
    绝不通过 Runner 强迫它成交。

【⚠️ 一个必须说清楚的区分（§十五）】

    「沟通不产生 AIC Transfer」**不是**「沟通不花钱」。
    实验 A 里的每次 LLM 调用都照常产生 `LLM_COST`。
    我们验证的是：**Agent 之间没有发生经济交易**。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channels                                            # noqa: E402

DB = HERE / "data" / "novel.db"
RESULTS = HERE / "results"

TASK_ID = "task_novel_001"
WS_ID = "WS-NOVEL-001"
AUTHOR = "novel_author"
EDITOR = "novel_editor"
READER = "novel_reader"
AGENTS = [AUTHOR, EDITOR, READER]

TOPIC = ("在一个由 AI Agent 参与运行的数字世界里，"
         "身份、记忆、资产和历史究竟属于谁？")


# ================================================================
# JSON 抽取（宽容解析，严格执行）
# ================================================================
def parse_json(text: str) -> dict:
    if not text:
        return {}
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    cands = [fence.group(1)] if fence else []
    cands.append(raw)
    if "{" in raw and "}" in raw:
        cands.append(raw[raw.find("{"): raw.rfind("}") + 1])
    for c in cands:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except Exception:                                     # noqa: BLE001
            continue
    return {}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, default=2)
    args = ap.parse_args()

    from main import build_runtime

    rt = build_runtime(db_path=str(DB), agent_ids=AGENTS)
    bridge = channels.SyncBridge()
    comm = rt.communication
    svc = rt.delegation_service

    log: dict = {"task_id": TASK_ID, "agents": AGENTS,
                 "started_at": time.time(), "comm_events": [],
                 "run": args.run, "experiment_a": {}, "experiment_b": {}}

    def bal(a: str) -> float:
        return rt.economy.ledger.balance_of(a, rt.economy.currency)

    def ledger_head() -> str:
        row = rt.economy.db.fetchone(
            "SELECT COALESCE(MAX(rowid), 0) AS m FROM ledger_transactions")
        return str(int(row["m"]))

    def tx_since(mark: str) -> list:
        rows = rt.economy.db.fetchall(
            "SELECT * FROM ledger_transactions WHERE rowid > ? ORDER BY rowid",
            (int(mark),))
        return [dict(r) for r in rows]

    def roster_fact(exclude: str) -> str:
        """
        🔒 **系统事实**，不是建议。

        Run 1 的负结果暴露出：Author 拒绝委托的理由之一是
        「我**不掌握**一个明确可承接该工作、并可对其交付质量负责的
        agent_id」。那不是它不愿交易，而是**它没有这个事实** ——
        与上一轮「Agent 看不到经济世界」是同一类缺口。

        所以这里把 **Agent Directory** 里的真实花名册给它
        （agents/<id>/config.yaml 的 skills，由组合根注册进目录）。
        这只是补一个可观察事实，**不是**告诉它「你应该委托给谁」。
        """
        lines = []
        for entry in rt.directory.all():
            aid = getattr(entry, "agent_id", "")
            if not aid or aid == exclude:
                continue
            caps = ", ".join(str(c) for c in (entry.capabilities or ()))
            lines.append(f"  - {aid}: capabilities={caps or '(none)'}")
        return "\n".join(lines) or "  (none)"

    def ask(agent_id: str, prompt: str) -> dict:
        """让一个 Agent 产生一轮输出（走真实 Context，含经济事实）。"""
        a = next(x for x in rt.economy.all_agents() if x.agent_id == agent_id)
        a.brain.preflight_credential()
        t0 = time.perf_counter()
        r = a.generate(messages=prompt)
        return {"agent_id": agent_id, "ok": bool((r.content or "").strip()),
                "output_status": r.output_status, "tokens": r.total_tokens,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "chars": len(r.content or ""), "content": r.content or ""}

    try:
        # ============================================================
        # Experiment A —— Communication（信息交换，**不是**经济行为）
        # ============================================================
        print("\n===== Experiment A：Communication =====")
        a_log: dict = {"exchanges": [], "wakeups": 0, "messages": 0,
                       "ledger_before": ledger_head(),
                       "balance_before": {a: bal(a) for a in AGENTS}}

        exchanges = [
            (AUTHOR, EDITOR,
             "你对第三章里主角的动机有什么看法？"),
            (AUTHOR, READER,
             "作为读者，你读到第三章时的感受是什么？"),
        ]
        for sender, receiver, question in exchanges:
            # ① 发一条**普通沟通**消息（不是委托）
            rec = channels.send(comm, bridge, from_agent=sender,
                                to_agent=receiver, text=question)
            got = channels.drain(comm, bridge, receiver)
            if got["wakeup"]:
                a_log["wakeups"] += 1
            a_log["messages"] += 1

            # ② 让对方**基于收到的消息**回答
            reply = ask(receiver,
                        f"{question}\n\n请直接回答，不超过 120 字。")

            ex = {"from": sender, "to": receiver, "question": question,
                  "message_id": rec.get("message_id"),
                  "wakeup": got["wakeup"],
                  "received": [m.message_id for m in got["received"]],
                  "reply_chars": reply["chars"],
                  "reply_head": reply["content"][:200]}
            a_log["exchanges"].append(ex)
            print(f"  {sender} → {receiver}  wakeup={got['wakeup']} "
                  f"reply={reply['chars']}字")

        a_log["balance_after"] = {a: bal(a) for a in AGENTS}
        a_log["ledger_tx_created"] = tx_since(a_log["ledger_before"])
        a_log["agent_to_agent_transfer"] = [
            t for t in a_log["ledger_tx_created"]
            if t["tx_type"] == "TRANSFER"]
        a_log["delegations_created"] = rt.delegations.count()
        log["experiment_a"] = a_log

        print(f"  → AIC TRANSFER (agent↔agent) = "
              f"{len(a_log['agent_to_agent_transfer'])}")
        print(f"  → delegations created = {a_log['delegations_created']}")

        # ============================================================
        # Experiment B —— Task Delegation（生产能力委托 = 经济行为）
        # ============================================================
        print("\n===== Experiment B：Task Delegation =====")
        b_log: dict = {"steps": [], "ledger_before": ledger_head(),
                       "balance_before": {a: bal(a) for a in AGENTS}}

        # ---- ① Author 自己决定要不要发起正式委托 ----
        step1 = ask(AUTHOR, (
            f"你是这次小说生产的参与者之一。任务主题：{TOPIC}\n"
            f"当前这一章已经有一份初稿在 manuscript.md。\n\n"
            f"现在请你判断：你**是否需要**把一项**正式的生产委托**"
            f"交给另一位参与者（例如请人做一次正式文学编辑、"
            f"并把结构化编辑报告写进 Workspace）？\n\n"
            f"注意：「问一句看法」**不是**委托。「请对方实际完成一份"
            f"需要生产投入的交付物」才是委托 —— 那会形成一次经济交换。\n\n"
            f"当前其他参与者（来自 Agent Directory，这是系统事实）：\n"
            f"{roster_fact(AUTHOR)}\n\n"
            f"只输出 JSON：\n"
            f'{{"delegate": true/false, "provider": "收件人 agent_id", '
            f'"description": "要对方交付什么", "reason": "为什么"}}'))
        d1 = parse_json(step1["content"])
        b_log["steps"].append({"step": "author_decides", **{
            k: step1[k] for k in ("ok", "tokens", "chars")},
            "parsed": d1, "head": step1["content"][:300]})
        print(f"  ① Author 决定：delegate={d1.get('delegate')} "
              f"provider={d1.get('provider')}")

        if not d1.get("delegate"):
            b_log["outcome"] = "no_delegation_emerged"
            print("  ⚠️ Author 没有发起委托 —— 如实记录，不强迫成交")
        else:
            provider = str(d1.get("provider") or "").strip()
            if provider not in (EDITOR, READER):
                provider = EDITOR          # 只做**解析兜底**，不替它选内容
                b_log["provider_fallback"] = True

            offer = svc.request(
                task_id=TASK_ID, requester_agent_id=AUTHOR,
                provider_agent_id=provider,
                description=str(d1.get("description") or "正式编辑委托"))
            b_log["offer"] = offer.to_dict()
            print(f"  → Offer {offer.offer_id} → {provider}"
                  f"（价格：还没有）")

            # ---- ② Provider **自己报价或拒绝**（不硬编码价格）----
            step2 = ask(provider, (
                f"你是 {provider}。**生产性委托请求**：\n"
                f"  委托方：{AUTHOR}\n"
                f"  要求你交付：{offer.description}\n\n"
                f"这是**正式委托**，不是随口问一句 —— 如果你接受，"
                f"你需要真的投入生产并交付一份写入 Workspace 的产物。\n\n"
                f"只输出 JSON：\n"
                f'{{"action": "accept" 或 "reject", '
                f'"price_aic": 你要求的报酬数字（接受时必填，自己决定）, '
                f'"reason": "为什么"}}'))
            d2 = parse_json(step2["content"])
            b_log["steps"].append({"step": "provider_decides",
                                   "parsed": d2,
                                   "head": step2["content"][:300]})
            action2 = str(d2.get("action") or "").lower()
            print(f"  ② Provider 决定：action={action2} "
                  f"price_aic={d2.get('price_aic')}")

            if action2 != "accept":
                svc.provider_respond(offer.offer_id, accept=False,
                                     reason=str(d2.get("reason") or "reject"))
                b_log["outcome"] = "provider_rejected"
                print("  → Provider 拒绝：**没有经济交易**")
            else:
                svc.provider_respond(offer.offer_id, accept=True,
                                     price_aic=d2.get("price_aic"))
                priced = svc.store.get(offer.offer_id)
                b_log["quoted_price_aic"] = priced.price_aic
                print(f"  → Provider 报价 {priced.price_aic} AIC")

                # ---- ③ Requester **自己决定接受还是拒绝** ----
                step3 = ask(AUTHOR, (
                    f"{provider} 对你的委托报了价："
                    f"{priced.price_aic} {priced.currency}。\n"
                    f"它要交付的是：{priced.description}\n\n"
                    f"只输出 JSON：\n"
                    f'{{"action": "accept" 或 "reject", "reason": "为什么"}}'))
                d3 = parse_json(step3["content"])
                b_log["steps"].append({"step": "requester_decides",
                                       "parsed": d3,
                                       "head": step3["content"][:300]})
                action3 = str(d3.get("action") or "").lower()
                print(f"  ③ Requester 决定：action={action3}")

                if action3 != "accept":
                    svc.requester_respond(offer.offer_id, accept=False,
                                          reason=str(d3.get("reason") or ""))
                    b_log["outcome"] = "requester_rejected"
                    print("  → Requester 拒绝：**没有经济交易**")
                else:
                    svc.requester_respond(offer.offer_id, accept=True)
                    b_log["accepted"] = True
                    print("  → ACCEPTED（注意：**钱还没动**）")

                    # ---- ④ Provider 真正生产 ----
                    # 🔒 Run 2 暴露出：Provider 拿不到**原料**，只能交
                    #    「我无法作业」的说明。原料就在共享 Task Workspace 里，
                    #    而读对全部 Participant 开放 —— 所以这里把**真实内容**
                    #    交给它。这只是补一个它本可以自己读到的**事实**，
                    #    不是替它写结论。
                    import shared_workspace as sw
                    src = rt.workspace_gateway.read(WS_ID, "/manuscript.md",
                                                    provider)
                    src_text = ((src.result or {}).get("content") or "") \
                        if src.success else "(manuscript.md 不可读)"
                    b_log["source_supplied_chars"] = len(src_text)

                    step4 = ask(provider, (
                        f"委托已成交。请现在**实际完成**这项工作：\n"
                        f"  {priced.description}\n\n"
                        f"以下是委托方当前在共享 Task Workspace 里的正文"
                        f"（manuscript.md，你可以读它）：\n"
                        f"----------\n{src_text[:4000]}\n----------\n\n"
                        f"请给出你的**正式编辑报告**正文，"
                        f"必须落在上面的真实段落上。只要正文，不要 JSON。"))
                    deliverable = step4["content"]
                    b_log["deliverable_chars"] = len(deliverable)

                    # 写进共享 Workspace（provider 拥有 editor_notes.md）
                    import shared_workspace as sw
                    wr = sw.apply_write(rt.workspace_gateway, WS_ID,
                                        provider, "editor_notes.md",
                                        deliverable)
                    b_log["deliverable_write"] = {
                        "ok": wr["ok"], "path": wr.get("path"),
                        "revision": wr.get("revision"),
                        "error": wr.get("error")}
                    print(f"  ④ Provider 交付 → {wr.get('path')} "
                          f"ok={wr['ok']} R{wr.get('revision')}")

                    # ---- ⑤ Requester **自己验收**（Runtime 不替它决定）----
                    step5 = ask(AUTHOR, (
                        f"{provider} 已交付：\n\n{deliverable[:800]}\n\n"
                        f"只输出 JSON：\n"
                        f'{{"accept_deliverable": true/false, '
                        f'"reason": "为什么"}}'))
                    d5 = parse_json(step5["content"])
                    b_log["steps"].append({"step": "requester_accepts_work",
                                           "parsed": d5,
                                           "head": step5["content"][:300]})
                    ok5 = bool(d5.get("accept_deliverable"))
                    print(f"  ⑤ Requester 验收：{ok5}")

                    if not ok5:
                        svc.cancel(offer.offer_id, reason="交付未被接受")
                        b_log["outcome"] = "deliverable_rejected"
                        print("  → 交付未被接受：**不结算，不转账**")
                    else:
                        svc.complete(offer.offer_id,
                                     deliverable_ref="/editor_notes.md")
                        settled = svc.settle(offer.offer_id)
                        b_log["settled"] = settled.to_dict()
                        b_log["outcome"] = "settled"
                        print(f"  → SETTLED  tx={settled.settled_tx_id}")

        b_log["balance_after"] = {a: bal(a) for a in AGENTS}
        b_log["ledger_tx_created"] = tx_since(b_log["ledger_before"])
        b_log["agent_to_agent_transfer"] = [
            t for t in b_log["ledger_tx_created"]
            if t["tx_type"] == "TRANSFER"]
        b_log["delegations_total"] = rt.delegations.count()
        log["experiment_b"] = b_log

    finally:
        try:
            bridge.close()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            rows = rt.economy.db.fetchall(
                "SELECT SUM(total_tokens) t, SUM(cost_ai) c FROM llm_calls")
            log["cumulative_cost"] = {"tokens": int(rows[0]["t"] or 0),
                                      "aic": round(float(rows[0]["c"] or 0), 6)}
            rt.economy.close()
        except Exception:                                     # noqa: BLE001
            pass

    log["wall_seconds"] = round(time.time() - log["started_at"], 1)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"delegation_experiment_run{args.run}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n===== 汇总 =====")
    a = log["experiment_a"]
    print(f"  A 沟通：messages={a['messages']} wakeups={a['wakeups']} "
          f"agent↔agent AIC transfer={len(a['agent_to_agent_transfer'])}")
    b = log["experiment_b"]
    print(f"  B 委托：outcome={b.get('outcome')} "
          f"offers={b.get('delegations_total')} "
          f"settled={'yes' if b.get('settled') else 'no'} "
          f"transfer={len(b['agent_to_agent_transfer'])}")
    print(f"\n  → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
