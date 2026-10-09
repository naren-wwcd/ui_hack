"""Mock on-call paging for the AutoHeal sandbox."""

from __future__ import annotations

import json
from typing import Any

from . import db
from .events import publish


def _load_incident(incident_id: int) -> dict[str, Any]:
    return db.query_one(
        "SELECT * FROM incidents WHERE id = ?",
        (incident_id,),
    ) or {}


def _enqueue_escalation(incident_id: int, payload: dict[str, Any]) -> int | None:
    try:
        return db.execute(
            """
            INSERT INTO rpa_tasks (incident_id, kind, status, ts, payload_json)
            VALUES (?, 'escalate', 'queued', ?, ?)
            """,
            (
                incident_id,
                db.now_iso(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    except Exception:
        return None


def create_page(
    incident_id: int,
    reason: str,
    diagnosis_summary: str,
    evidence: list[Any],
) -> dict[str, Any]:
    """Persist and publish an on-call page with investigation history."""
    incident = _load_incident(incident_id)
    if not incident:
        return {"ok": False, "error": f"incident {incident_id} not found"}

    hypotheses = db.query(
        """
        SELECT id, cause, confidence, status, evidence_json
        FROM hypotheses
        WHERE incident_id = ?
        ORDER BY id
        """,
        (incident_id,),
    )
    actions = db.query(
        """
        SELECT id, fix, risk, decision, reason, result, ts
        FROM actions
        WHERE incident_id = ?
        ORDER BY id
        """,
        (incident_id,),
    )

    page_body = {
        "sandbox": True,
        "incident_id": incident_id,
        "title": incident.get("title"),
        "severity": incident.get("severity"),
        "reason": reason,
        "diagnosis": diagnosis_summary,
        "hypotheses_tested": hypotheses,
        "evidence": evidence if isinstance(evidence, list) else [str(evidence)],
        "actions_tried": actions,
        "recommended_next_step": (
            "On-call engineer should review the diagnosis and sandbox evidence, "
            "then choose an approved next step."
        ),
    }
    body_json = json.dumps(page_body, ensure_ascii=False)
    page_id = db.execute(
        "INSERT INTO pages (incident_id, ts, body_json) VALUES (?, ?, ?)",
        (incident_id, db.now_iso(), body_json),
    )
    db.execute(
        "UPDATE incidents SET status = 'escalated' WHERE id = ?",
        (incident_id,),
    )

    rpa_task_id = _enqueue_escalation(incident_id, page_body)
    page = {
        "ok": True,
        "id": page_id,
        "incident_id": incident_id,
        "body": page_body,
        "rpa_task_id": rpa_task_id,
    }
    publish("page_sent", page)
    publish(
        "incident_escalated",
        {
            "incident_id": incident_id,
            "status": "escalated",
            "reason": reason,
            "page_id": page_id,
            "rpa_task_id": rpa_task_id,
        },
    )
    publish(
        "incident_updated",
        {"incident_id": incident_id, "status": "escalated"},
    )
    return page