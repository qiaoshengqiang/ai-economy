"""Laya smoke test —— 只跑一次，不 preload（任务书 §三/§六）。"""
import os, time, json, sys
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

def mem():
    import subprocess
    out = {}
    try:
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
        for l in mp.splitlines():
            if "free percentage" in l:
                out["free_pct"] = l.split(":")[-1].strip()
    except Exception:
        pass
    try:
        out["swap"] = subprocess.run(["sysctl","-n","vm.swapusage"],
                                     capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    return out

print("  [1] import laya ...", flush=True)
from laya import Router
print(f"  [2] Router() 构造（**不** preload）...", flush=True)
t0 = time.perf_counter()
router = Router()                      # 🔒 不加 preload=True
t_ctor = time.perf_counter() - t0
print(f"      ctor={t_ctor:.2f}s  mem_after_ctor={mem()}", flush=True)

state = ("Agent A has 30 AIC. Self execution costs 12 AIC. "
         "Agent B costs 4 AIC. Agent B success probability is high.")
# 🔒 官方 schema（读 laya/agent.py:1868 的 docstring 得到，不是猜的）：
#     choice: {"type":"choice","instructions":"...","criteria":{"optA":"...",...}}
#     score:  {"type":"score", "instructions":"...","criteria":["lvl0",...]}
#     noul:   {"type":"noul",  "instructions":"...","criteria":{"false":"..","true":".."}}
#
#  实测踩到的两个错误（都已修）：
#    ① 漏 type        → ValueError: unknown type None
#    ② 用 options 而不是 criteria → "no 'instructions'"
questions = {"strategy": {
    "type": "choice",
    "instructions": "Which strategy should be selected?",
    "criteria": {"SELF": "Agent A does it itself",
                 "DELEGATE_B": "Agent A delegates to Agent B",
                 "WAIT": "do nothing for now"},
}}

print("  [3] 第一次 predict（会触发模型下载+加载）...", flush=True)
t1 = time.perf_counter()
try:
    out = router.predict(state, questions, model="multilingual")
    dt = (time.perf_counter() - t1) * 1000
    print(f"  [4] ✅ 成功  latency={dt:.0f}ms", flush=True)
    print(f"      raw = {json.dumps(out, ensure_ascii=False, default=str)[:600]}",
          flush=True)
except Exception as exc:
    print(f"  [4] ❌ {type(exc).__name__}: {str(exc)[:500]}", flush=True)
    import traceback; traceback.print_exc()
print(f"  [5] mem_final={mem()}", flush=True)
