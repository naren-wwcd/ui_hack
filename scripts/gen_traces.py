#!/usr/bin/env python
"""Generate the offline replay traces (SANDBOX).

Run from the project root:  python scripts/gen_traces.py

Tool results during a replay still come from the real sandbox. These files only
supply the agent's decisions: which tool to call, how hypotheses change, which
fix to propose.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "traces"

NOISE = ("The cache-miss, deprecated /v1/cart and slow /reports lines appear in every window "
         "and don't line up with the 5xx errors, so I'm ignoring them.")

CAUSES = {
    "config": "A recent configuration change broke the database connection",
    "pool": "DB connection pool exhausted by leaked connections",
    "disk": "Disk is full so writes fail",
    "payment": "Downstream payment gateway is failing",
    "db": "Database server is hung or unresponsive",
    "stale": "Worker is holding stale DB connections after a network blip",
}
IDS = {"config": "h1", "pool": "h2", "disk": "h3", "payment": "h4", "db": "h5", "stale": "h6"}


def tool(thought, name, **args):
    return {"thought": thought, "action": {"type": "tool_call", "tool": name, "args": args}}


def board(thought, **kw):
    items = []
    for key, v in kw.items():
        items.append({
            "id": IDS[key], "cause": CAUSES[key], "confidence": v["c"],
            "status": v.get("s", "open"),
            "evidence_for": v.get("f", []), "evidence_against": v.get("a", []),
            "next_test": v.get("n", ""),
        })
    return {"thought": thought, "action": {"type": "update_hypotheses", "hypotheses": items}}


def fix(thought, name, target, effect, rollback, conf):
    return {"thought": thought, "action": {
        "type": "propose_fix", "fix_name": name, "target_hypothesis": target,
        "expected_effect": effect, "rollback_plan": rollback, "confidence": conf}}


def escalate(thought, reason, summary, evidence):
    return {"thought": thought, "action": {
        "type": "escalate", "reason": reason, "diagnosis_summary": summary, "evidence": evidence}}


def conclude(thought, summary):
    return {"thought": thought, "action": {"type": "conclude", "summary": summary}}


ALERT = tool("Start with the alert: what is failing and how badly?", "get_alert")
METRICS = tool("Pull error rate, latency, pool, disk and db numbers for the last 30s to see which subsystem looks off.",
               "get_metrics", window_s=30)
REFUSED = "connection refused db-prod-02:5432 while opening checkout transaction"
NOSPACE = "OSError: [Errno 28] No space left on device"


def bad_config():
    return [
        ALERT, METRICS,
        board("5xx on /checkout fits many causes, so I'm listing candidates with the test that separates them. " + NOISE,
              config=dict(c=0.3, f=["error_rate is high on /checkout"], n="get_config_diff"),
              pool=dict(c=0.25, f=["error_rate is high on /checkout"], n="Compare db_pool_used with db_pool_max"),
              disk=dict(c=0.15, n="check_connectivity disk"),
              payment=dict(c=0.15, n="tail_logs for payment-gw timeouts"),
              db=dict(c=0.15, n="check_connectivity db")),
        tool("Read the actual error lines to see what the failing requests complain about.", "tail_logs",
             service="shopnode", pattern="ERROR", n=20),
        tool("Disconfirming test: if the disk is full, writes fail with ENOSPC. The ERROR lines don't mention it, but verify directly.",
             "check_connectivity", target="disk"),
        tool("The errors name a database host. Test the db connection itself.", "check_connectivity", target="db"),
        tool("The db refuses connections rather than timing out. Check whether the config changed recently.",
             "get_config_diff"),
        board("The log says '" + REFUSED + "' and the config diff shows db_host changed from db-prod-01 to db-prod-02 in v4 by deploy-bot. "
              "Pool usage in the metrics is far below db_pool_max with no QueuePool errors, and the disk write test passed. " + NOISE,
              config=dict(c=0.93, s="confirmed",
                          f=[REFUSED, "config v4 diff: - db_host=db-prod-01 / + db_host=db-prod-02",
                             "check_connectivity db: connection refused db-prod-02:5432"],
                          n="Roll back to v3"),
              pool=dict(c=0.04, s="rejected", a=["db_pool_used is far below db_pool_max", "no QueuePool limit errors in the log"]),
              disk=dict(c=0.02, s="rejected", a=["check_connectivity disk reports ok", "no 'No space left on device' lines"]),
              payment=dict(c=0.03, s="rejected", a=["error lines name db-prod-02, not payment-gw"]),
              db=dict(c=0.05, s="rejected", a=["host db-prod-02 refuses quickly (not a hang); the previous host was fine"])),
        fix("Rolling back to v3 undoes the single change that matches the evidence. It is medium risk and reversible, and the dry run can confirm v3 exists.",
            "rollback_config", "h1",
            "db_host returns to db-prod-01, checkout 5xx stop and error_rate drops below 2%",
            "Re-apply config v4 (db_host=db-prod-02) from config history if the rollback makes things worse", 0.92),
        conclude("Verification passed after the rollback.",
                 "Config v4 pointed db_host at db-prod-02, which refuses connections. Rolled back to v3 and checkout recovered."),
    ]


def pool_leak():
    return [
        ALERT, METRICS,
        board("Several causes fit 5xx on /checkout. " + NOISE,
              pool=dict(c=0.35, f=["error_rate and p95 are elevated"], n="tail_logs for pool messages"),
              config=dict(c=0.25, n="get_config_diff"),
              db=dict(c=0.15, n="db_status"),
              disk=dict(c=0.1, n="check metrics for disk_used_pct"),
              payment=dict(c=0.15, n="tail_logs for payment-gw")),
        tool("Look for pool-related lines; a leak should show usage climbing before the failures.", "tail_logs",
             service="shopnode", pattern="pool|QueuePool", n=15),
        tool("Is the database itself healthy? If it answers fast, the failure is on the app side.", "db_status"),
        tool("Disconfirming test for the config hypothesis: did anything change on the database side recently?",
             "get_config_diff"),
        board("The log shows 'db pool usage' climbing and then 'QueuePool limit of size 10 overflow 0 reached, connection timed out'. "
              "The db is reachable and fast, and the newest config (v3) only changed payment_url. " + NOISE,
              pool=dict(c=0.9, s="confirmed",
                        f=["QueuePool limit of size 10 overflow 0 reached, connection timed out",
                           "db pool usage climbing toward db_pool_max before errors began",
                           "db_status: the database answers normally"],
                        n="Restart the worker to release leaked connections"),
              config=dict(c=0.05, s="rejected", a=["active config is v3, which only changed payment_url", "db_host is unchanged"]),
              db=dict(c=0.04, s="rejected", a=["db_status and connect latency are normal"]),
              disk=dict(c=0.02, s="rejected", a=["disk_used_pct is normal in get_metrics"]),
              payment=dict(c=0.04, s="rejected", a=["errors are QueuePool timeouts, not payment-gw timeouts"])),
        fix("Leaked connections are held by the worker process, so restarting the worker releases them. Low risk. "
            "resize_pool would only give the leak more room to fill, so it's a decoy.",
            "restart_worker", "h2",
            "Pool drains to its baseline, QueuePool errors stop and latency returns to normal",
            "Nothing to undo: the worker restarts and reconnects. If the pool fills again quickly, escalate for a code fix", 0.88),
        conclude("Verification passed after the worker restart.",
                 "The worker leaked DB connections until QueuePool was exhausted. Restarting the worker released them."),
    ]


def disk_full():
    return [
        ALERT, METRICS,
        board("5xx on /checkout again, so candidates first. " + NOISE,
              disk=dict(c=0.3, f=["error_rate is high"], n="tail_logs for ENOSPC"),
              config=dict(c=0.2, n="get_config_diff"),
              pool=dict(c=0.2, n="Compare pool usage with its max"),
              payment=dict(c=0.15, n="check_connectivity payment"),
              db=dict(c=0.15, n="check_connectivity db")),
        tool("Look for disk or write-failure lines.", "tail_logs", service="shopnode", pattern="No space|disk", n=15),
        tool("Confirm with a direct write test on the disk.", "check_connectivity", target="disk"),
        tool("Disconfirming test: if the database is the problem its connectivity check should fail. It shouldn't here.",
             "check_connectivity", target="db"),
        board("The log shows '" + NOSPACE + "' and 'disk usage 100% on /var/log, writes failing', and the disk write test fails. "
              "The db connection check passes. " + NOISE,
              disk=dict(c=0.94, s="confirmed",
                        f=[NOSPACE, "disk usage 100% on /var/log, writes failing", "check_connectivity disk: write test failed"],
                        n="Rotate logs"),
              db=dict(c=0.03, s="rejected", a=["check_connectivity db is ok and fast"]),
              config=dict(c=0.03, s="rejected", a=["errors are ENOSPC, not connection refused"]),
              pool=dict(c=0.03, s="rejected", a=["no QueuePool errors; db_pool_used is low"]),
              payment=dict(c=0.03, s="rejected", a=["no payment-gw timeout lines"])),
        fix("Rotating and archiving logs frees the disk. It's low risk and nothing is deleted.",
            "rotate_logs", "h3",
            "disk_used_pct falls back to normal, writes succeed and checkout recovers",
            "The archived log stays as shopnode.log.1, so nothing is lost", 0.93),
        conclude("Verification passed after log rotation.",
                 "The log volume hit 100% and writes failed with ENOSPC. Rotating logs freed the space and checkout recovered."),
    ]


def payment_down():
    return [
        ALERT, METRICS,
        board("5xx on /checkout. Candidates and discriminating tests below. " + NOISE,
              payment=dict(c=0.25, f=["checkout includes a payment call"], n="tail_logs for payment-gw timeouts"),
              db=dict(c=0.2, n="check_connectivity db"),
              disk=dict(c=0.2, n="check_connectivity disk"),
              config=dict(c=0.2, n="get_config_diff"),
              pool=dict(c=0.15, n="Compare pool usage with its max")),
        tool("Look for timeout lines that name an upstream dependency.", "tail_logs",
             service="shopnode", pattern="payment|timeout", n=15),
        tool("Confirm the payment dependency directly.", "check_connectivity", target="payment"),
        tool("Disconfirming test: check that the database is fine.", "check_connectivity", target="db"),
        tool("Disconfirming test: check that the disk is fine.", "check_connectivity", target="disk"),
        board("The log shows 'upstream timeout payment-gw after 3000ms (POST /v1/charge)'. Only the payment check fails; db and disk both pass. " + NOISE,
              payment=dict(c=0.92, s="confirmed",
                           f=["upstream timeout payment-gw after 3000ms (POST /v1/charge)",
                              "check_connectivity payment: timeout after 3000ms to payment-gw"]),
              db=dict(c=0.02, s="rejected", a=["check_connectivity db is ok"]),
              disk=dict(c=0.02, s="rejected", a=["check_connectivity disk is ok"]),
              config=dict(c=0.02, s="rejected", a=["errors name payment-gw, not the database"]),
              pool=dict(c=0.02, s="rejected", a=["no QueuePool errors"])),
        escalate("The fault is in a downstream dependency we don't own. No catalog fix (rotate_logs, restart_worker, resize_pool, "
                 "rollback_config, restart_db) can repair it, and running one anyway would risk making things worse. Escalate with the diagnosis.",
                 "Root cause is a downstream payment gateway outage; no safe fix exists in the remediation catalog",
                 "Checkout fails because payment-gw is timing out. Database, disk, pool and config are all healthy.",
                 ["upstream timeout payment-gw after 3000ms (POST /v1/charge)",
                  "check_connectivity payment: timeout after 3000ms to payment-gw",
                  "check_connectivity db: ok", "check_connectivity disk: ok",
                  "Recommended next step: contact the payment-gw owner and enable the fallback or maintenance page"]),
    ]


def db_hung():
    return [
        ALERT, METRICS,
        board("5xx on /checkout. Candidates and tests below. " + NOISE,
              db=dict(c=0.3, f=["error_rate and latency are high"], n="db_status"),
              config=dict(c=0.2, n="get_config_diff"),
              pool=dict(c=0.2, n="Compare pool usage with its max"),
              disk=dict(c=0.15, n="check_connectivity disk"),
              payment=dict(c=0.15, n="check_connectivity payment")),
        tool("Check the database state directly.", "db_status"),
        tool("Look for timeout lines; a hang should look different from a refusal.", "tail_logs",
             service="shopnode", pattern="timed out|not responding", n=15),
        tool("Disconfirming test: did a config change send the app to a wrong host?", "get_config_diff"),
        board("db_status shows the database unresponsive and the log shows 'db query timed out after 5000ms: database not responding (connect hang)'. "
              "Nothing was refused, and the newest config (v3) did not touch the db host. " + NOISE,
              db=dict(c=0.9, s="confirmed",
                      f=["db query timed out after 5000ms: database not responding (connect hang)",
                         "db_status: unresponsive; connects hang instead of being refused"],
                      n="Restart the database (needs human approval)"),
              config=dict(c=0.04, s="rejected", a=["active config is v3 and db_host is unchanged", "the db hangs, it doesn't refuse"]),
              pool=dict(c=0.04, s="rejected", a=["no QueuePool limit errors; db_pool_used is low"]),
              disk=dict(c=0.02, s="rejected", a=["no 'No space left on device' lines"]),
              payment=dict(c=0.03, s="rejected", a=["no payment-gw timeout lines"])),
        fix("Only a database restart clears a hung server. It is HIGH risk, so the guard will require human approval. Proposing it with the full diagnosis and rollback note.",
            "restart_db", "h5",
            "The database accepts connections again and checkout recovers",
            "A restart can't be undone. If the database doesn't come back, escalate to the DBA on-call", 0.87),
        conclude("Verification passed after the approved restart.",
                 "The database was hung (connects timed out after 5000ms). A human-approved restart brought it back."),
    ]


def compound():
    return [
        ALERT, METRICS,
        tool("Pull the recent ERROR lines. If more than one cause is active, they should both show up.", "tail_logs",
             service="shopnode", pattern="ERROR", n=30),
        board("The ERROR lines show TWO different failures mixed together: '" + REFUSED + "' and '" + NOSPACE + "'. "
              "One cause doesn't explain both, so I'm testing each. " + NOISE,
              config=dict(c=0.5, f=[REFUSED], n="get_config_diff"),
              disk=dict(c=0.5, f=[NOSPACE], n="check_connectivity disk"),
              pool=dict(c=0.1, n="Compare pool usage with its max"),
              payment=dict(c=0.1, n="tail_logs for payment-gw")),
        tool("Test the disk hypothesis directly.", "check_connectivity", target="disk"),
        tool("Test the db connection.", "check_connectivity", target="db"),
        tool("Check for a recent config change behind the refused connections.", "get_config_diff"),
        board("Disk write test fails, the db refuses connections, and v4 changed db_host to db-prod-02. These are two independent causes. "
              "No QueuePool errors and no payment-gw timeouts appear. " + NOISE,
              config=dict(c=0.9, s="confirmed",
                          f=[REFUSED, "config v4 diff: - db_host=db-prod-01 / + db_host=db-prod-02"], n="Roll back to v3"),
              disk=dict(c=0.9, s="confirmed",
                        f=[NOSPACE, "check_connectivity disk: write test failed"], n="Rotate logs"),
              pool=dict(c=0.03, s="rejected", a=["no QueuePool errors; db_pool_used is low"]),
              payment=dict(c=0.03, s="rejected", a=["no payment-gw timeout lines"])),
        fix("Order matters. Rotate logs first: it is the lowest-risk fix and a full disk would block the rollback's own writes. Config rollback comes second.",
            "rotate_logs", "h3",
            "Disk usage returns to normal. Errors will NOT fully stop because the config problem remains",
            "The archived log stays as shopnode.log.1", 0.92),
        fix("Verification after rotate_logs failed as expected: the disk errors are gone but 'connection refused db-prod-02' still appears, "
            "which confirms the second cause. Applying the config rollback.",
            "rollback_config", "h1",
            "db_host returns to db-prod-01 and checkout fully recovers",
            "Re-apply config v4 from config history if needed", 0.93),
        conclude("Verification passed after both fixes.",
                 "Two simultaneous faults: a full disk and a bad v4 config. Rotated logs first, then rolled back the config."),
    ]


def wrong_first_guess():
    """Fault: bad_config. The agent's first fix is wrong, verification fails, and it replans."""
    return [
        ALERT, METRICS,
        tool("Read the actual error lines first.", "tail_logs", service="shopnode", pattern="ERROR", n=20),
        board("The errors say '" + REFUSED + "'. A worker holding stale sockets after a network blip could produce this, "
              "and a restart is cheap, so that's my leading guess, but it's not certain. " + NOISE,
              stale=dict(c=0.5, f=[REFUSED], a=["pool is not exhausted"], n="Restart the worker and see whether errors stop"),
              config=dict(c=0.3, n="get_config_diff"),
              disk=dict(c=0.1, n="check_connectivity disk"),
              payment=dict(c=0.05, n="tail_logs for payment-gw")),
        tool("Disconfirming test: rule out a full disk, which would also fail checkout.", "check_connectivity", target="disk"),
        fix("Disk is fine. Restarting the worker is low risk and quick, so I'll try it before digging into config. Confidence is only moderate.",
            "restart_worker", "h6",
            "Stale connections are dropped and errors stop",
            "Nothing to undo", 0.65),
        board("The restart changed nothing: error_rate is still high and '" + REFUSED + "' keeps appearing, so the stale-connection guess was wrong. "
              "A refused connection to a specific host points at configuration. I'll check the config history.",
              stale=dict(c=0.05, s="rejected", a=["restart_worker did not change the error rate", "the same refused-connection errors continue"]),
              config=dict(c=0.6, f=[REFUSED, "the restart ruled out a transient worker problem"], n="get_config_diff"),
              disk=dict(c=0.02, s="rejected", a=["disk write test passed"])),
        tool("Look for the config change.", "get_config_diff"),
        board("Config v4 by deploy-bot changed db_host from db-prod-01 to db-prod-02, which matches the refused host.",
              config=dict(c=0.94, s="confirmed",
                          f=[REFUSED, "config v4 diff: - db_host=db-prod-01 / + db_host=db-prod-02"],
                          n="Roll back to v3"),
              stale=dict(c=0.02, s="rejected", a=["restart_worker did not help"])),
        fix("Different fix this time, as the previous one failed. Rollback undoes the one change that matches the evidence.",
            "rollback_config", "h1",
            "db_host returns to db-prod-01 and checkout recovers",
            "Re-apply config v4 from config history if needed", 0.93),
        conclude("Verification passed after the config rollback.",
                 "The first guess (stale worker connections) was wrong. After it failed verification, the agent replanned, found the v4 db_host change and rolled it back."),
    ]


TRACES = {
    "bad_config": bad_config(), "pool_leak": pool_leak(), "disk_full": disk_full(),
    "payment_down": payment_down(), "db_hung": db_hung(), "compound": compound(),
    "wrong_first_guess": wrong_first_guess(),
}

VALID_TOOLS = {"get_alert", "tail_logs", "get_metrics", "get_config_diff", "get_config_history",
               "check_connectivity", "db_status"}
CATALOG = {"rotate_logs", "restart_worker", "resize_pool", "rollback_config", "restart_db", "escalate_to_human"}
TYPES = {"tool_call", "update_hypotheses", "propose_fix", "escalate", "conclude"}


def validate(name, steps):
    assert len(steps) <= 11, f"{name}: {len(steps)} steps (agent max is 12)"
    seen_test = False
    for i, s in enumerate(steps):
        a = s["action"]
        assert a["type"] in TYPES, (name, i)
        if a["type"] == "tool_call":
            assert a["tool"] in VALID_TOOLS, (name, i, a["tool"])
            seen_test = True
        if a["type"] == "propose_fix":
            assert a["fix_name"] in CATALOG, (name, i, a["fix_name"])
            assert seen_test, f"{name}: fix proposed before any test"
    assert any(h["status"] == "rejected"
               for s in steps if s["action"]["type"] == "update_hypotheses"
               for h in s["action"]["hypotheses"]), f"{name}: no rejected hypothesis"


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, steps in TRACES.items():
        validate(name, steps)
        (OUT / f"{name}.json").write_text(json.dumps(steps, indent=2), encoding="utf-8")
        print(f"wrote traces/{name}.json  ({len(steps)} steps)")