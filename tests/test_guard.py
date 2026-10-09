"""Guard rule tests using an isolated temporary SQLite database."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import db, guard
from scripts.seed import seed


@pytest.fixture
def incident_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "guard-test.db"))
    db.init_db()
    seed()
    return db.execute(
        """
        INSERT INTO incidents (title, severity, status, opened_at)
        VALUES (?, ?, ?, ?)
        """,
        ("SANDBOX guard test", "high", "investigating", db.now_iso()),
    )


def test_off_catalog_fix_is_blocked(incident_id: int) -> None:
    result = guard.check(incident_id, "delete_production", 0.99)

    assert result["decision"] == "blocked"
    assert "not in the remediation catalog" in result["reason"]
    assert result["action_id"] > 0


def test_restart_db_requires_approval_before_execution(incident_id: int) -> None:
    result = guard.check(incident_id, "restart_db", 0.99)

    assert result["decision"] == "needs_approval"
    assert result["approval_id"] is not None
    assert guard.is_execution_permitted(result["action_id"]) is False

    approved = guard.approve(result["approval_id"], "test_operator")

    assert approved["ok"] is True
    assert guard.is_execution_permitted(result["action_id"]) is True


def test_attempt_limit_blocks_third_remediation(incident_id: int) -> None:
    for fix in ("rotate_logs", "restart_worker"):
        result = guard.check(incident_id, fix, 0.95)
        assert result["decision"] == "allowed"

    third = guard.check(incident_id, "restart_db", 0.99)

    assert third["decision"] == "blocked"
    assert "2-attempt remediation limit" in third["reason"]
    assert third["approval_id"] is None


def test_medium_risk_with_low_confidence_is_not_auto_run(
    incident_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guard,
        "_dry_run",
        lambda fix_name: (True, "Dry run passed for test."),
    )
    result = guard.check(incident_id, "rollback_config", 0.65)

    assert result["decision"] == "needs_approval"
    assert result["approval_id"] is not None
    assert guard.is_execution_permitted(result["action_id"]) is False


def test_rejected_approval_never_permits_execution(incident_id: int) -> None:
    result = guard.check(incident_id, "restart_db", 0.99)
    rejected = guard.reject(result["approval_id"], "test_operator")

    assert rejected["ok"] is True
    assert rejected["status"] == "rejected"
    assert guard.is_execution_permitted(result["action_id"]) is False