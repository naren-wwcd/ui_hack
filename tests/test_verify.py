"""Verification tests for incident resolution behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import db, verify
from scripts.seed import seed


@pytest.fixture
def incident_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "verify-test.db"))
    db.init_db()
    seed()
    return db.execute(
        """
        INSERT INTO incidents (title, severity, status, opened_at)
        VALUES (?, ?, ?, ?)
        """,
        ("SANDBOX verify test", "high", "investigating", db.now_iso()),
    )


@pytest.mark.asyncio
async def test_failed_verification_does_not_resolve(
    incident_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "CHECK_COUNT", 5)
    monkeypatch.setattr(verify, "CHECK_INTERVAL_S", 0)

    def failed_check(_fix_name: str) -> dict:
        return {
            "ok": False,
            "error_rate": 0.25,
            "detail": {"health_status": 503, "error_rate": 0.25},
        }

    monkeypatch.setattr(verify, "_one_check", failed_check)
    result = await verify.run(incident_id, "restart_worker")

    assert result["ok"] is False
    assert len(result["checks"]) == 5
    incident = db.query_one("SELECT status, resolved_at FROM incidents WHERE id = ?", (incident_id,))
    assert incident["status"] == "investigating"
    assert incident["resolved_at"] is None
    assert len(db.query("SELECT id FROM health_checks WHERE incident_id = ?", (incident_id,))) == 5


@pytest.mark.asyncio
async def test_successful_verification_resolves_incident(
    incident_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "CHECK_COUNT", 5)
    monkeypatch.setattr(verify, "CHECK_INTERVAL_S", 0)
    monkeypatch.setattr(
        verify,
        "_one_check",
        lambda _fix_name: {
            "ok": True,
            "error_rate": 0.0,
            "detail": {"health_status": 200, "error_rate": 0.0},
        },
    )

    result = await verify.run(incident_id, "restart_worker")

    assert result["ok"] is True
    incident = db.query_one(
        "SELECT status, resolved_at, mttr_s FROM incidents WHERE id = ?",
        (incident_id,),
    )
    assert incident["status"] == "resolved"
    assert incident["resolved_at"] is not None
    assert incident["mttr_s"] is not None