"""Evidence-first incident agent for the AutoHeal SANDBOX."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import db
from .catalog import get_catalog
from .events import publish
from .guard import (
    approve,
    check as guard_check,
    is_execution_permitted,
    record_action_result,
    reject,
)
from .llm import next_step
from .tools import TOOL_REGISTRY, run_tool

MAX_STEPS = 12
SANDBOX_URL = os.getenv("SANDBOX_URL", "http://localhost:8001").rstrip("/")
TEST_TOOLS = {"check_connectivity", "db_status", "get_config_diff", "get_metrics"}
_sessions: dict[int, "AgentSession"] = {}


@dataclass
class AgentSession:
    incident_id: int
    messages: list[dict[str, Any]]
    idx: int = 0
    disconfirming_test_ran: bool = False
    failed_fixes: set[str] = field(default_factory=set)
    replan_count: int = 0
    awaiting_replan_hypothesis: bool = False
    replan_hypothesis_updated: bool = False
    pending_fix: dict[str, Any] | None = None
    done: bool = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _initial_messages(incident_id: int) -> list[dict[str, Any]]:
    incident = db.query_one(
        "SELECT id, title, severity FROM incidents WHERE id = ?",
        (incident_id,),
    )
    catalog = get_catalog()
    tools = {
        name: entry
        for name, entry in TOOL_REGISTRY.items()
    }
    return [
        {
            "role": "system",
            "content": (
                "You are the evidence-first AutoHeal agent operating only in a "
                "SANDBOX. Follow Observe -> Investigate -> Hypothesize -> Test -> "
                "Decide -> Guard -> Act -> Verify -> Replan/Escalate. The same "
                "5xx alert can have different causes; rely on actual tool evidence. "
                "Do not use or ask for hidden fault metadata. Quote relevant log "
                "lines. Never blame these red herrings: cache miss ratio high, "
                "deprecated /v1/cart, slow /reports query, or occasional 404s. "
                "Use only the registered tools and remediation catalog. Prefer "
                "the least-risk fix. Reject unsupported hypotheses visibly. "
                "Run at least one disconfirming test before proposing a fix. "
                "Escalate if confidence is below 0.6 or no safe catalog fix fits. "
                "Return one strict JSON agent step."
            ),
        },
        {
            "role": "user",
            "content": _json(
                {
                    "incident": incident or {"id": incident_id},
                    "alert": {
                        "title": "5xx on /checkout",
                        "affected_endpoint": "/checkout",
                    },
                    "registered_tools": tools,
                    "remediation_catalog": catalog,
                    "instruction": "Start by collecting evidence. Do not infer cause from the alert alone.",
                }
            ),
        },
    ]


def _next_idx(incident_id: int) -> int:
    row = db.query_one(
        "SELECT COALESCE(MAX(idx), 0) + 1 AS n FROM agent_steps WHERE incident_id = ?",
        (incident_id,),
    )
    return int((row or {}).get("n", 1))


def _save_step(
    session: AgentSession,
    action: dict[str, Any],
    thought: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {"action": action}
    if extra:
        payload.update(extra)
    idx = _next_idx(session.incident_id)
    step_type = str(action.get("type", "invalid"))
    step_id = db.execute(
        """
        INSERT INTO agent_steps (incident_id, idx, type, thought, payload_json, ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session.incident_id,
            idx,
            step_type,
            thought,
            _json(payload),
            db.now_iso(),
        ),
    )
    session.idx = idx
    publish(
        "agent_step",
        {
            "id": step_id,
            "incident_id": session.incident_id,
            "idx": idx,
            "type": step_type,
            "thought": thought,
            "payload": payload,
        },
    )


def _append_message(session: AgentSession, role: str, content: Any, **extra: Any) -> None:
    message = {"role": role, "content": content}
    message.update(extra)
    session.messages.append(message)


def _update_hypotheses(
    session: AgentSession,
    hypotheses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []
    for hypothesis in hypotheses:
        cause = str(hypothesis["cause"])
        confidence = max(0.0, min(1.0, float(hypothesis["confidence"])))
        evidence = {
            "id": hypothesis.get("id"),
            "evidence_for": hypothesis.get("evidence_for", []),
            "evidence_against": hypothesis.get("evidence_against", []),
            "next_test": hypothesis.get("next_test", ""),
        }
        requested_status = str(hypothesis.get("status", "")).lower()
        if requested_status not in {"open", "confirmed", "rejected"}:
            if confidence >= 0.8:
                status = "confirmed"
            elif confidence <= 0.2 and evidence["evidence_against"]:
                status = "rejected"
            else:
                status = "open"
        else:
            status = requested_status

        existing = db.query_one(
            """
            SELECT id FROM hypotheses
            WHERE incident_id = ? AND cause = ?
            ORDER BY id DESC LIMIT 1
            """,
            (session.incident_id, cause),
        )
        evidence_json = _json(evidence)
        if existing:
            hypothesis_id = existing["id"]
            db.execute(
                """
                UPDATE hypotheses
                SET confidence = ?, status = ?, evidence_json = ?
                WHERE id = ?
                """,
                (confidence, status, evidence_json, hypothesis_id),
            )
        else:
            hypothesis_id = db.execute(
                """
                INSERT INTO hypotheses
                    (incident_id, cause, confidence, status, evidence_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session.incident_id, cause, confidence, status, evidence_json),
            )
        stored.append(
            {
                "id": hypothesis_id,
                "agent_id": hypothesis.get("id"),
                "cause": cause,
                "confidence": confidence,
                "status": status,
                **evidence,
            }
        )

    if session.awaiting_replan_hypothesis and stored:
        session.replan_hypothesis_updated = True

    publish(
        "hypotheses_updated",
        {"incident_id": session.incident_id, "hypotheses": stored},
    )
    return stored


def _sandbox_action(fix_name: str) -> dict[str, Any]:
    request = Request(
        f"{SANDBOX_URL}/admin/action/{fix_name}",
        data=b"",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {"ok": response.status < 400}
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = {"detail": str(exc)}
        return {"ok": False, "detail": payload}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


async def _execute_approved(
    session: AgentSession,
    proposal: dict[str, Any],
    action_id: int,
) -> bool:
    fix_name = proposal["fix_name"]
    if not is_execution_permitted(action_id):
        reason = f"Guard did not permit execution of {fix_name}."
        record_action_result(action_id, reason, False)
        session.failed_fixes.add(fix_name)
        _append_message(session, "user", reason)
        return False

    result = await asyncio.to_thread(_sandbox_action, fix_name)
    success = result.get("ok") is True
    detail = str(result.get("detail", result))
    record_action_result(action_id, detail, success)
    _save_step(
        session,
        {"type": "act", "fix_name": fix_name},
        f"Executed {fix_name} through the sandbox admin endpoint.",
        {"result": result},
    )
    _append_message(
        session,
        "tool",
        _json(result),
        name="sandbox_action",
        fix=fix_name,
    )

    if not success:
        session.failed_fixes.add(fix_name)
        await _verification_failed(
            session,
            fix_name,
            f"Sandbox action failed: {detail}",
        )
        return False

    from .verify import run as verify_run

    verification = await verify_run(session.incident_id, fix_name)
    _save_step(
        session,
        {"type": "verify", "fix_name": fix_name},
        "Checked sandbox health after the remediation action.",
        {"verification": verification},
    )
    _append_message(session, "tool", _json(verification), name="verification")

    if verification.get("ok") is True:
        session.failed_fixes.clear()
        session.awaiting_replan_hypothesis = False
        session.replan_hypothesis_updated = False
        session.done = True
        return True

    session.failed_fixes.add(fix_name)
    await _verification_failed(
        session,
        fix_name,
        _json(verification.get("detail", verification)),
    )
    return False


async def _verification_failed(
    session: AgentSession,
    fix_name: str,
    detail: str,
) -> None:
    if session.replan_count >= 1:
        await _escalate(
            session,
            "Verification failed after the one permitted replan.",
            f"Remediation {fix_name} did not restore sandbox health. {detail}",
            [detail],
        )
        return

    session.replan_count += 1
    session.awaiting_replan_hypothesis = True
    session.replan_hypothesis_updated = False
    _append_message(
        session,
        "user",
        (
            f"Verification failed after {fix_name}. Evidence: {detail}. "
            "Replan once: update or add a hypothesis based on new evidence, "
            "then propose a different catalog fix. Never repeat a failed fix."
        ),
    )


def _quoted_evidence(session: AgentSession) -> list[str]:
    evidence: list[str] = []
    for message in session.messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            value = content
        if isinstance(value, dict):
            lines = value.get("lines")
            if isinstance(lines, list):
                evidence.extend(f'"{line}"' for line in lines if isinstance(line, str))
            detail = value.get("detail")
            if isinstance(detail, str):
                evidence.append(f'"{detail}"')
    # Keep the page useful and bounded.
    return list(dict.fromkeys(evidence))[-30:]


async def _escalate(
    session: AgentSession,
    reason: str,
    diagnosis_summary: str,
    evidence: list[Any] | None = None,
) -> None:
    from .escalation import create_page

    page = create_page(
        session.incident_id,
        reason,
        diagnosis_summary,
        evidence if evidence is not None else _quoted_evidence(session),
    )
    _save_step(
        session,
        {"type": "escalate", "reason": reason},
        diagnosis_summary,
        {"page": page},
    )
    session.done = True


async def _run_session(session: AgentSession) -> None:
    incident_id = session.incident_id
    db.execute(
        "UPDATE incidents SET status = 'investigating' WHERE id = ?",
        (incident_id,),
    )
    publish("incident_updated", {"incident_id": incident_id, "status": "investigating"})

    while session.idx < MAX_STEPS and not session.done:
        result = next_step(incident_id, session.messages)
        if not isinstance(result, dict) or result.get("error"):
            raw = result.get("raw", result) if isinstance(result, dict) else result
            await _escalate(
                session,
                "The LLM returned an invalid or unavailable step.",
                "The agent could not safely continue; raw model evidence is attached.",
                [f"LLM error: {raw}"],
            )
            return

        thought = result.get("thought")
        action = result.get("action")
        if not isinstance(thought, str) or not isinstance(action, dict):
            _append_message(
                session,
                "user",
                "Invalid step shape. Return a JSON object with string thought and valid action.",
            )
            continue

        action_type = action.get("type")
        if action_type not in {
            "tool_call",
            "update_hypotheses",
            "propose_fix",
            "escalate",
            "conclude",
        }:
            _save_step(session, {"type": "invalid"}, thought, {"received": action})
            _append_message(
                session,
                "user",
                f"Invalid action type {action_type!r}. Choose a documented action type.",
            )
            continue

        _save_step(session, action, thought)
        _append_message(session, "assistant", _json(result))

        if action_type == "tool_call":
            tool_name = action.get("tool")
            args = action.get("args", {})
            if not isinstance(tool_name, str) or not isinstance(args, dict):
                tool_result = {"error": "tool_call requires a string tool and object args"}
            else:
                tool_result = run_tool(tool_name, args)
                if tool_name in TEST_TOOLS:
                    session.disconfirming_test_ran = True
            _append_message(
                session,
                "tool",
                _json(tool_result)[:2200],
                name=tool_name,
            )
            continue

        if action_type == "update_hypotheses":
            hypotheses = action.get("hypotheses")
            if not isinstance(hypotheses, list):
                _append_message(session, "user", "hypotheses must be a list; correct it.")
                continue
            try:
                stored = _update_hypotheses(session, hypotheses)
            except (KeyError, TypeError, ValueError) as exc:
                _append_message(session, "user", f"Invalid hypothesis data: {exc}")
                continue
            _append_message(session, "tool", _json(stored), name="hypotheses_updated")
            continue

        if action_type == "propose_fix":
            fix_name = action.get("fix_name")
            try:
                confidence = float(action.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0

            if not session.disconfirming_test_ran:
                _append_message(
                    session,
                    "user",
                    "A disconfirming test has not run. Use a read-only test tool before proposing a fix.",
                )
                continue
            if session.awaiting_replan_hypothesis and not session.replan_hypothesis_updated:
                _append_message(
                    session,
                    "user",
                    "Replan requires a new or updated hypothesis before proposing a fix.",
                )
                continue
            if not isinstance(fix_name, str):
                _append_message(session, "user", "fix_name must be a string.")
                continue
            if confidence < 0.6:
                await _escalate(
                    session,
                    f"Proposed fix confidence {confidence:.2f} is below 0.6.",
                    str(action.get("expected_effect", "No safe fix can be selected confidently.")),
                )
                return
            if fix_name in session.failed_fixes:
                _append_message(
                    session,
                    "user",
                    f"{fix_name} already failed. Propose a different catalog fix or escalate.",
                )
                continue

            decision = guard_check(
                incident_id,
                fix_name,
                confidence,
                action.get("target_hypothesis"),
            )
            _append_message(session, "tool", _json(decision), name="guard_decision")
            if decision["decision"] == "needs_approval":
                session.pending_fix = dict(action)
                session.pending_fix["action_id"] = decision["action_id"]
                session.pending_fix["approval_id"] = decision.get("approval_id")
                return
            if decision["decision"] != "allowed":
                _append_message(
                    session,
                    "user",
                    f"Guard blocked {fix_name}: {decision['reason']}. Select another fix or escalate.",
                )
                continue
            await _execute_approved(session, action, decision["action_id"])
            continue

        if action_type == "escalate":
            await _escalate(
                session,
                str(action.get("reason", "Agent requested human review.")),
                str(action.get("diagnosis_summary", "Diagnosis unavailable.")),
                action.get("evidence", _quoted_evidence(session)),
            )
            return

        if action_type == "conclude":
            if session.awaiting_replan_hypothesis:
                _append_message(
                    session,
                    "user",
                    "A failed verification requires one replan with a new hypothesis and a different fix before concluding.",
                )
                continue
            session.done = True
            return

    if not session.done:
        await _escalate(
            session,
            f"Agent reached the {MAX_STEPS}-step limit.",
            "The investigation did not reach a verified resolution within the step limit.",
        )


async def run_incident(incident_id: int) -> None:
    """Run the agent loop for one incident in the caller's async task."""
    incident = db.query_one("SELECT id FROM incidents WHERE id = ?", (incident_id,))
    if not incident:
        return
    session = AgentSession(incident_id=incident_id, messages=_initial_messages(incident_id))
    _sessions[incident_id] = session
    await _run_session(session)


async def resume_after_approval(approval_id: int) -> None:
    """Resume the paused incident after an approval decision."""
    approval_row = db.query_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval_row:
        return

    session = _sessions.get(int(approval_row["incident_id"]))
    if not session or not session.pending_fix:
        return

    if approval_row["status"] == "approved":
        proposal = session.pending_fix
        action_id = int(proposal["action_id"])
        session.pending_fix = None
        success = await _execute_approved(session, proposal, action_id)
        if not success and not session.done:
            await _run_session(session)
        return

    if approval_row["status"] == "rejected":
        reject_message = (
            f"Human rejected proposed fix {session.pending_fix.get('fix_name')}. "
            "Replan with a different safe fix or escalate."
        )
        session.pending_fix = None
        _append_message(session, "user", reject_message)
        if session.replan_count >= 1:
            await _escalate(
                session,
                "The proposed remediation was rejected after replanning.",
                reject_message,
            )
        else:
            session.replan_count += 1
            session.awaiting_replan_hypothesis = True
            session.replan_hypothesis_updated = False
            await _run_session(session)


def _cli_inject_fault(scenario: str) -> None:
    request = Request(
        f"{SANDBOX_URL}/admin/fault/{scenario}",
        data=b"",
        method="POST",
    )
    with urlopen(request, timeout=5):
        pass


async def _cli_run(scenario: str) -> None:
    from .db import init_db
    from .guard import approve as approve_action

    init_db()
    _cli_inject_fault(scenario)
    incident_id = db.execute(
        """
        INSERT INTO incidents
            (title, severity, status, fault_hint_hidden, opened_at)
        VALUES (?, ?, 'open', ?, ?)
        """,
        ("SANDBOX CLI incident: 5xx on /checkout", "high", scenario, db.now_iso()),
    )
    await run_incident(incident_id)

    while True:
        incident = db.query_one("SELECT status FROM incidents WHERE id = ?", (incident_id,))
        if not incident or incident["status"] != "awaiting_approval":
            break
        pending = db.query_one(
            "SELECT id FROM approvals WHERE incident_id = ? AND status = 'pending' ORDER BY id DESC",
            (incident_id,),
        )
        if not pending:
            break
        answer = await asyncio.to_thread(
            input,
            f"Approval required for incident {incident_id}. Approve in SANDBOX? [y/N] ",
        )
        if answer.strip().lower() in {"y", "yes"}:
            approve_action(pending["id"], "cli_operator")
            await resume_after_approval(pending["id"])
        else:
            reject(pending["id"], "cli_operator")
            await resume_after_approval(pending["id"])
            break

    print(json.dumps(
        db.query_one("SELECT id, status, resolved_at, mttr_s FROM incidents WHERE id = ?", (incident_id,)),
        indent=2,
        default=str,
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one AutoHeal SANDBOX incident.")
    parser.add_argument("command", choices=["run"])
    parser.add_argument(
        "scenario",
        choices=["bad_config", "pool_leak", "disk_full", "payment_down", "db_hung", "compound"],
    )
    args = parser.parse_args()
    asyncio.run(_cli_run(args.scenario))


if __name__ == "__main__":
    main()