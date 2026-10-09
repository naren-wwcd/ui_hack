"""Agent loop tests with LLM, sandbox, and verification boundaries stubbed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import agent, db
from scripts.seed import seed


@pytest.fixture
def incident_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "agent-test.db"))
    db.init_db()
    seed()
    return db.execute(
        """
        INSERT INTO incidents (title, severity, status, opened_at)
        VALUES (?, ?, ?, ?)
        """,
        ("SANDBOX agent test: 5xx on /checkout", "high", "open", db.now_iso()),
    )


@pytest.mark.asyncio
async def test_agent_persists_steps_hypotheses_and_rejection(
    incident_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([
        {
            "thought": "Check a read-only database test.",
            "action": {"type": "tool_call", "tool": "db_status", "args": {}},
        },
        {
            "thought": "Record supported and rejected hypotheses.",
            "action": {
                "type": "update_hypotheses",
                "hypotheses": [
                    {
                        "id": "h1",
                        "cause": "database host configuration",
                        "confidence": 0.95,
                        "evidence_for": ["connection refused db-prod-02"],
                        "evidence_against": [],
                        "next_test": "compare config history",
                    },
                    {
                        "id": "h2",
                        "cause": "payment gateway",
                        "confidence": 0.01,
                        "evidence_for": [],
                        "evidence_against": ["database host refusal"],
                        "next_test": "none",
                        "status": "rejected",
                    },
                ],
            },
        },
        {
            "thought": "End this investigation after recording evidence.",
            "action": {"type": "conclude", "summary": "Diagnosis recorded."},
        },
    ])
    monkeypatch.setattr(agent, "next_step", lambda _id, _messages: next(answers))
    monkeypatch.setattr(agent, "run_tool", lambda _name, _args: {"status": "ok", "latency_ms": 3})

    await agent.run_incident(incident_id)

    steps = db.query(
        "SELECT idx, type FROM agent_steps WHERE incident_id = ? ORDER BY idx",
        (incident_id,),
    )
    hypotheses = db.query(
        "SELECT cause, status FROM hypotheses WHERE incident_id = ? ORDER BY id",
        (incident_id,),
    )
    assert [row["type"] for row in steps] == ["tool_call", "update_hypotheses", "conclude"]
    assert any(row["status"] == "rejected" for row in hypotheses)


@pytest.mark.asyncio
async def test_failed_verification_replans_once_then_escalates(
    incident_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([
        {
            "thought": "Test database status.",
            "action": {"type": "tool_call", "tool": "db_status", "args": {}},
        },
        {
            "thought": "State the first hypothesis.",
            "action": {
                "type": "update_hypotheses",
                "hypotheses": [{
                    "id": "h1",
                    "cause": "disk pressure",
                    "confidence": 0.9,
                    "evidence_for": ["disk metric elevated"],
                    "evidence_against": [],
                    "next_test": "rotate logs",
                }],
            },
        },
        {
            "thought": "Try the first remediation.",
            "action": {
                "type": "propose_fix",
                "fix_name": "rotate_logs",
                "target_hypothesis": "h1",
                "confidence": 0.9,
                "expected_effect": "Disk space recovers.",
                "rollback_plan": "Escalate if verification fails.",
            },
        },
        {
            "thought": "Replan with a different hypothesis.",
            "action": {
                "type": "update_hypotheses",
                "hypotheses": [{
                    "id": "h2",
                    "cause": "worker connection leak",
                    "confidence": 0.88,
                    "evidence_for": ["pool pressure remains"],
                    "evidence_against": [],
                    "next_test": "restart worker",
                }],
            },
        },
        {
            "thought": "Try the different remediation.",
            "action": {
                "type": "propose_fix",
                "fix_name": "restart_worker",
                "target_hypothesis": "h2",
                "confidence": 0.88,
                "expected_effect": "Connections are released.",
                "rollback_plan": "Escalate if verification fails.",
            },
        },
    ])
    monkeypatch.setattr(agent, "next_step", lambda _id, _messages: next(answers))
    monkeypatch.setattr(agent, "run_tool", lambda _name, _args: {"status": "ok"})
    monkeypatch.setattr(agent, "_sandbox_action", lambda _fix: {"ok": True, "detail": "applied"})

    async def failed_verify(_incident_id: int, fix_name: str) -> dict:
        return {"ok": False, "checks": [], "detail": {"fix": fix_name, "failed": True}}

    monkeypatch.setattr("backend.verify.run", failed_verify)

    pages: list[dict] = []

    def fake_page(
        incident: int,
        reason: str,
        diagnosis: str,
        evidence: list,
    ) -> dict:
        pages.append({"incident": incident, "reason": reason, "diagnosis": diagnosis})
        db.execute("UPDATE incidents SET status = 'escalated' WHERE id = ?", (incident,))
        return {"ok": True, "body": {"reason": reason}}

    monkeypatch.setattr("backend.escalation.create_page", fake_page)

    await agent.run_incident(incident_id)

    assert len(pages) == 1
    assert "Verification failed" in pages[0]["reason"]
    actions = db.query(
        "SELECT fix, result FROM actions WHERE incident_id = ? ORDER BY id",
        (incident_id,),
    )
    assert [row["fix"] for row in actions] == ["rotate_logs", "restart_worker"]
    assert all(row["result"].startswith("success:") for row in actions)
    assert db.query_one("SELECT status FROM incidents WHERE id = ?", (incident_id,))["status"] == "escalated"