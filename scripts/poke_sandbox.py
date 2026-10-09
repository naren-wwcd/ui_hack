#!/usr/bin/env python
"""Poke the ShopNode SANDBOX: inject each fault, show the evidence, apply the fix, show recovery.

Start the sandbox alone first:  uvicorn sandbox_app.main:app --port 8001
Then:  python scripts/poke_sandbox.py            (all scenarios)
       python scripts/poke_sandbox.py bad_config (one scenario)
"""
import os
import sys
import time

import httpx

BASE = os.environ.get("SANDBOX_URL", "http://127.0.0.1:8001").rstrip("/")

# scenario: (seconds to let the fault ramp up, fixes that really repair it, in order)
SCENARIOS = {
    "bad_config": (4, ["rollback_config"]),
    "pool_leak": (16, ["restart_worker"]),
    "disk_full": (13, ["rotate_logs"]),
    "payment_down": (4, []),  # no safe fix exists: this one must be escalated to a human
    "db_hung": (4, ["restart_db"]),
    "compound": (13, ["rotate_logs", "rollback_config"]),
}


def get(path):
    return httpx.get(BASE + path, timeout=6).json()


def post(path):
    return httpx.post(BASE + path, timeout=6).json()


def show(label):
    m = get("/metrics?window_s=5")
    h = httpx.get(BASE + "/health", timeout=6).status_code
    print(f"  {label:<14} health={h} error_rate={m['error_rate']:.2f} p95={m['p95_ms']:.0f}ms "
          f"pool={m['db_pool_used']}/{m['db_pool_max']} disk={m['disk_used_pct']}% "
          f"worker={m['worker_status']} db={m['db_status']} payment={m['payment_status']} cfg=v{m['config_version']}")


def wait_healthy(timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if httpx.get(BASE + "/health", timeout=6).status_code == 200 and get("/metrics?window_s=3")["error_rate"] < 0.02:
            return True
        time.sleep(1)
    return False


def run(name):
    ramp, fixes = SCENARIOS[name]
    print(f"\n=== {name} (SANDBOX) ===")
    post("/admin/reset")
    time.sleep(2)
    show("baseline")
    post(f"/admin/fault/{name}")
    time.sleep(ramp)
    show("faulty")
    for line in httpx.get(BASE + "/metrics", timeout=6).json() and open(
            os.environ.get("LOG_PATH", "logs/shopnode.log"), encoding="utf-8", errors="replace").readlines()[-60:]:
        if "ERROR" in line:
            print("  log:", line.strip()[:160])
            break
    if name == "bad_config":
        post("/admin/action/restart_worker")
        time.sleep(3)
        show("wrong fix")
        print("  restart_worker did not help (a wrong fix really fails).")
    if not fixes:
        print("  no catalog fix can repair this: the agent must ESCALATE with its diagnosis.")
        return True
    for fx in fixes:
        r = post(f"/admin/action/{fx}")
        print(f"  applied {fx}: {r}")
        time.sleep(1)
    ok = wait_healthy()
    show("after fix")
    print("  RECOVERED" if ok else "  STILL FAILING")
    return ok


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(SCENARIOS)
    results = {n: run(n) for n in wanted}
    print("\nSummary:", ", ".join(f"{n}={'ok' if v else 'FAIL'}" for n, v in results.items()))
    sys.exit(0 if all(results.values()) else 1)
