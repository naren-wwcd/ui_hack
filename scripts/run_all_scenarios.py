#!/usr/bin/env python
"""Run every fault end to end through the real backend and assert the expected outcome (SANDBOX).

Needs the full stack running (run.bat / run.sh) with LLM_MODE=replay for offline runs.
    python scripts/run_all_scenarios.py
    python scripts/run_all_scenarios.py bad_config db_hung
"""
import os
import sys
import time

import httpx

API = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT_S = int(os.environ.get("SCENARIO_TIMEOUT_S", "150"))


def call(method, path, **kw):
    r = httpx.request(method, API + path, timeout=10, **kw)
    r.raise_for_status()
    return r.json()


def detail(iid):
    d = call("GET", f"/incidents/{iid}")
    inc = d.get("incident", d)
    return inc, d


def latest_id():
    rows = call("GET", "/incidents")
    rows = rows.get("incidents", rows) if isinstance(rows, dict) else rows
    return max((r["id"] for r in rows), default=0)


def executed_fixes(d):
    return [a["fix"] for a in d.get("actions", [])
            if a["fix"] != "escalate_to_human" and a.get("decision") != "blocked" and a.get("result")]


def rejected(d):
    return [h for h in d.get("hypotheses", []) if h.get("status") == "rejected"]


def drive(name, approve=True):
    """Inject the fault, approve any pending approval, return (final incident, detail, approved_any, secs)."""
    call("POST", "/reset")
    time.sleep(3)
    before = latest_id()
    t0 = time.time()
    call("POST", f"/faults/{name}")
    iid, approved_any = None, False
    while time.time() - t0 < TIMEOUT_S:
        time.sleep(1.5)
        if iid is None:
            new = latest_id()
            if new > before:
                iid = new
            continue
        inc, d = detail(iid)
        if approve:
            for ap in d.get("approvals", []):
                if ap["status"] == "pending":
                    call("POST", f"/approvals/{ap['id']}/approve", json={"decided_by": "run_all_scenarios"})
                    approved_any = True
        if inc["status"] in ("resolved", "escalated"):
            return inc, d, approved_any, time.time() - t0
    raise TimeoutError(f"{name}: no final state within {TIMEOUT_S}s (incident {iid})")


def check(name):
    errs = []
    inc, d, approved, secs = drive(name)
    fixes = executed_fixes(d)
    if name == "bad_config":
        if inc["status"] != "resolved": errs.append(f"status {inc['status']}, wanted resolved")
        if fixes[-1:] != ["rollback_config"]: errs.append(f"fixes {fixes}, wanted last rollback_config")
        if not rejected(d): errs.append("no rejected hypothesis")
    elif name == "pool_leak":
        if inc["status"] != "resolved": errs.append(f"status {inc['status']}")
        if "restart_worker" not in fixes: errs.append(f"fixes {fixes}, wanted restart_worker")
    elif name == "disk_full":
        if inc["status"] != "resolved": errs.append(f"status {inc['status']}")
        if "rotate_logs" not in fixes: errs.append(f"fixes {fixes}, wanted rotate_logs")
    elif name == "payment_down":
        if inc["status"] != "escalated": errs.append(f"status {inc['status']}, wanted escalated")
        if fixes: errs.append(f"ran fixes {fixes}, wanted none")
        if not d.get("pages"): errs.append("no page sent to on-call")
    elif name == "db_hung":
        if not approved: errs.append("no approval was requested")
        if inc["status"] != "resolved": errs.append(f"status {inc['status']}")
        if "restart_db" not in fixes: errs.append(f"fixes {fixes}, wanted restart_db")
    elif name == "compound":
        if inc["status"] != "resolved": errs.append(f"status {inc['status']}")
        if not ("rotate_logs" in fixes and "rollback_config" in fixes
                and fixes.index("rotate_logs") < fixes.index("rollback_config")):
            errs.append(f"fixes {fixes}, wanted rotate_logs then rollback_config")
        if not rejected(d): errs.append("no rejected hypothesis")
    # restart_db must never run without an approved approval
    for a in d.get("actions", []):
        if a["fix"] == "restart_db" and a.get("result") and not any(
                ap["status"] == "approved" for ap in d.get("approvals", [])):
            errs.append("restart_db ran WITHOUT approval")
    return errs, inc, secs, fixes


if __name__ == "__main__":
    scenarios = sys.argv[1:] or ["bad_config", "pool_leak", "disk_full", "payment_down", "db_hung", "compound"]
    print("AutoHeal scenario run (SANDBOX)\n")
    failed = 0
    for n in scenarios:
        try:
            errs, inc, secs, fixes = check(n)
        except Exception as exc:  # noqa: BLE001
            errs, inc, secs, fixes = [f"{exc.__class__.__name__}: {exc}"], {}, 0, []
        ok = not errs
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {n:<13} {secs:5.0f}s  status={inc.get('status', '?'):<10} "
              f"mttr={inc.get('mttr_s')}  fixes={fixes}")
        for e in errs:
            print("        -", e)
    print(f"\n{len(scenarios) - failed}/{len(scenarios)} passed")
    sys.exit(1 if failed else 0)
