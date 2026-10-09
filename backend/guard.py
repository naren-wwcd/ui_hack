"""Deterministic guardrails for sandbox remediation actions."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

from . import db
from .catalog import get_fix
from .events import publish

_SANDBOX_URL = os.getenv("SANDBOX_URL", "http://localhost:8001").rstrip("/")
_MAX_ATTEMPTS = 2


def _sandbox_json(path: str) -> dict[str, Any]:
    try:
        with urlopen(f"{_SANDBOX_URL}{path}", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            return {"error": f"HTTP {exc.code}"}


def _dry_run(fix_name: str) -> tuple[bool, str]:
    """Validate medium-risk changes without changing sandbox state."""
    if fix_name == "rollback_config":
        history = _sandbox_json("/admin/config/history")
        versions = history.get("versions", [])
        if not isinstance(versions, list) or len(versions) < 2:
            return False, "Dry run failed: no previous configuration version exists."
        latest_diff = _sandbox_json("/admin/config/diff")
        return True, (
            "Dry run passed: a previous configuration version exists; "
            f"latest diff is {json.dumps(latest_diff.get('diff', {}), ensure_ascii=False)}"
        )

    if fix_name == "resize_pool":
        metrics = _sandbox_json("/metrics")
        try:
            current = int(metrics["db_pool_max"])
        except (KeyError, TypeError, ValueError):
            return False, "Dry run failed: database pool metrics are unavailable."
        proposed = current + 10
        if current < 1 or proposed > 100:
            return False, (
                f"Dry run failed: proposed pool size {proposed} is outside "
                "the allowed range 1..100."
            )
        return True, (
            f"Dry run passed: pool size would change from {current} to {proposed}; "
            "no state was changed."
        )

    return False, f"No dry-run implementation exists for {fix_name}."


def _attempt_count(incident_id: int) -> int:
    row = db.query_one(
        """
        SELECT COUNT(*) AS n
        FROM actions
        WHERE incident_id = ?
          AND fix != 'escalate_to_human'
          AND decision != 'blocked'
        """,
        (incident_id,),
    )
    return int((row or {}).get("n", 0))


def _failed_before(incident_id: int, fix_name: str) -> bool:
    rows = db.query(
        """
        SELECT result
        FROM actions
        WHERE incident_id = ?
          AND fix = ?
          AND result IS NOT NULL
        """,
        (incident_id, fix_name),
    )
    for row in rows:
        value = str(row.get("result") or "").strip().lower()
        if value.startswith("failed:") or value.startswith("failure:"):
            return True
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                return True
        except (ValueError, TypeError):
            if '"ok": false' in value or "'ok': false" in value:
                return True
    return False


def _record_decision(
    incident_id: int,
    fix_name: str,
    risk: str,
    decision: str,
    reason: str,
) -> int:
    action_id = db.execute(
        """
        INSERT INTO actions (incident_id, fix, risk, decision, reason, ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (incident_id, fix_name, risk, decision, reason, db.now_iso()),
    )
    publish(
        "guard_decision",
        {
            "incident_id": incident_id,
            "action_id": action_id,
            "fix": fix_name,
            "risk": risk,
            "decision": decision,
            "reason": reason,
        },
    )
    return action_id


def _request_approval(
    incident_id: int,
    action_id: int,
    fix_name: str,
    catalog_item: dict[str, Any],
    reason: str,
    target_hypothesis: str | int | None,
) -> int:
    approval_id = db.execute(
        """
        INSERT INTO approvals (incident_id, action_id, status, ts)
        VALUES (?, ?, 'pending', ?)
        """,
        (incident_id, action_id, db.now_iso()),
    )
    db.execute(
        "UPDATE incidents SET status = 'awaiting_approval' WHERE id = ?",
        (incident_id,),
    )

    hypothesis = None
    if target_hypothesis is not None:
        hypothesis = db.query_one(
            """
            SELECT id, cause, confidence, status
            FROM hypotheses
            WHERE incident_id = ? AND (id = ? OR CAST(id AS TEXT) = ?)
            LIMIT 1
            """,
            (incident_id, target_hypothesis, str(target_hypothesis)),
        )

    publish(
        "approval_requested",
        {
            "incident_id": incident_id,
            "approval_id": approval_id,
            "action_id": action_id,
            "diagnosis": hypothesis,
            "fix": fix_name,
            "risk": catalog_item.get("risk"),
            "blast_radius": catalog_item.get("blast_radius"),
            "rollback_plan": reason,
            "reason": reason,
        },
    )
    return approval_id


def check(
    incident_id: int,
    fix_name: str,
    confidence: float,
    target_hypothesis: str | int | None = None,
) -> dict[str, Any]:
    """Check and persist the deterministic decision for a proposed fix."""
    catalog_item = get_fix(fix_name)
    if not catalog_item:
        reason = f"Blocked: {fix_name!r} is not in the remediation catalog."
        action_id = _record_decision(
            incident_id, fix_name, "low", "blocked", reason
        )
        return {
            "decision": "blocked",
            "reason": reason,
            "action_id": action_id,
            "approval_id": None,
        }

    risk = str(catalog_item.get("risk", "high")).lower()

    if _failed_before(incident_id, fix_name):
        reason = f"Blocked: {fix_name} already failed for this incident."
        action_id = _record_decision(
            incident_id, fix_name, risk, "blocked", reason
        )
        return {
            "decision": "blocked",
            "reason": reason,
            "action_id": action_id,
            "approval_id": None,
        }

    if fix_name != "escalate_to_human" and _attempt_count(incident_id) >= _MAX_ATTEMPTS:
        reason = f"Blocked: incident has reached the {_MAX_ATTEMPTS}-attempt remediation limit."
        action_id = _record_decision(
            incident_id, fix_name, risk, "blocked", reason
        )
        return {
            "decision": "blocked",
            "reason": reason,
            "action_id": action_id,
            "approval_id": None,
        }

    if fix_name == "escalate_to_human":
        reason = "Allowed: escalation is always permitted."
        action_id = _record_decision(
            incident_id, fix_name, risk, "allowed", reason
        )
        return {
            "decision": "allowed",
            "reason": reason,
            "action_id": action_id,
            "approval_id": None,
        }

    if risk == "high" or bool(catalog_item.get("requires_approval")):
        reason = f"Human approval is always required for {fix_name} ({risk} risk)."
        action_id = _record_decision(
            incident_id, fix_name, risk, "needs_approval", reason
        )
        approval_id = _request_approval(
            incident_id,
            action_id,
            fix_name,
            catalog_item,
            reason,
            target_hypothesis,
        )
        return {
            "decision": "needs_approval",
            "reason": reason,
            "action_id": action_id,
            "approval_id": approval_id,
        }

    if risk == "medium":
        dry_run_ok, dry_run_reason = _dry_run(fix_name)
        if not dry_run_ok:
            reason = f"Blocked: {dry_run_reason}"
            action_id = _record_decision(
                incident_id, fix_name, risk, "blocked", reason
            )
            return {
                "decision": "blocked",
                "reason": reason,
                "action_id": action_id,
                "approval_id": None,
            }

        if not isinstance(confidence, (int, float)) or confidence < 0.8:
            reason = (
                f"{dry_run_reason} Confidence {confidence!r} is below 0.8; "
                "human approval is required."
            )
            action_id = _record_decision(
                incident_id, fix_name, risk, "needs_approval", reason
            )
            approval_id = _request_approval(
                incident_id,
                action_id,
                fix_name,
                catalog_item,
                reason,
                target_hypothesis,
            )
            return {
                "decision": "needs_approval",
                "reason": reason,
                "action_id": action_id,
                "approval_id": approval_id,
            }

        reason = dry_run_reason + " Confidence is at least 0.8."
        action_id = _record_decision(
            incident_id, fix_name, risk, "allowed", reason
        )
        return {
            "decision": "allowed",
            "reason": reason,
            "action_id": action_id,
            "approval_id": None,
        }

    reason = f"Allowed: {risk}-risk catalog fix."
    action_id = _record_decision(
        incident_id, fix_name, risk, "allowed", reason
    )
    return {
        "decision": "allowed",
        "reason": reason,
        "action_id": action_id,
        "approval_id": None,
    }


def approve(approval_id: int, decided_by: str = "human") -> dict[str, Any]:
    """Approve a pending action and return its identifiers."""
    approval = db.query_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return {"ok": False, "error": f"approval {approval_id} not found"}
    if approval["status"] != "pending":
        return {
            "ok": False,
            "error": f"approval {approval_id} is already {approval['status']}",
        }

    db.execute(
        """
        UPDATE approvals
        SET status = 'approved', decided_by = ?, ts = ?
        WHERE id = ?
        """,
        (decided_by, db.now_iso(), approval_id),
    )
    db.execute(
        "UPDATE incidents SET status = 'investigating' WHERE id = ?",
        (approval["incident_id"],),
    )
    result = {
        "ok": True,
        "approval_id": approval_id,
        "action_id": approval["action_id"],
        "incident_id": approval["incident_id"],
        "status": "approved",
        "decided_by": decided_by,
    }
    publish("approval_decided", result)
    return result


def reject(approval_id: int, decided_by: str = "human") -> dict[str, Any]:
    """Reject a pending action and return its identifiers."""
    approval = db.query_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return {"ok": False, "error": f"approval {approval_id} not found"}
    if approval["status"] != "pending":
        return {
            "ok": False,
            "error": f"approval {approval_id} is already {approval['status']}",
        }

    db.execute(
        """
        UPDATE approvals
        SET status = 'rejected', decided_by = ?, ts = ?
        WHERE id = ?
        """,
        (decided_by, db.now_iso(), approval_id),
    )
    db.execute(
        "UPDATE incidents SET status = 'investigating' WHERE id = ?",
        (approval["incident_id"],),
    )
    result = {
        "ok": True,
        "approval_id": approval_id,
        "action_id": approval["action_id"],
        "incident_id": approval["incident_id"],
        "status": "rejected",
        "decided_by": decided_by,
    }
    publish("approval_decided", result)
    return result


def record_action_result(action_id: int, text: str, success: bool) -> None:
    """Save the sandbox execution outcome on its action row."""
    prefix = "success" if success else "failed"
    result_text = f"{prefix}: {text}"
    db.execute(
        "UPDATE actions SET result = ? WHERE id = ?",
        (result_text, action_id),
    )
    action = db.query_one(
        "SELECT incident_id, fix, decision FROM actions WHERE id = ?",
        (action_id,),
    )
    publish(
        "action_result",
        {
            "action_id": action_id,
            "incident_id": action.get("incident_id") if action else None,
            "fix": action.get("fix") if action else None,
            "decision": action.get("decision") if action else None,
            "success": success,
            "result": text,
        },
    )


def is_execution_permitted(action_id: int) -> bool:
    """Return true only for an allowed action or one with explicit approval."""
    action = db.query_one(
        "SELECT id, fix, decision FROM actions WHERE id = ?",
        (action_id,),
    )
    if not action:
        return False
    if action["fix"] == "restart_db":
        row = db.query_one(
            """
            SELECT 1 AS approved
            FROM approvals
            WHERE action_id = ? AND status = 'approved'
            LIMIT 1
            """,
            (action_id,),
        )
        return bool(row)

    if action["decision"] == "allowed":
        return True

    row = db.query_one(
        """
        SELECT 1 AS approved
        FROM approvals
        WHERE action_id = ? AND status = 'approved'
        LIMIT 1
        """,
        (action_id,),
    )
    return bool(row)