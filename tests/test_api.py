"""Focused API tests; background polling is disabled for deterministic tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import api, db
from scripts.seed import seed


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "api-test.db"))
    monkeypatch.setenv("AUTOHEAL_DISABLE_BACKGROUND", "1")
    db.init_db()
    seed()
    with TestClient(api.app) as test_client:
        yield test_client


def test_catalog_endpoint_returns_seeded_catalog(client: TestClient) -> None:
    response = client.get("/catalog")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert {
        "rotate_logs",
        "restart_worker",
        "resize_pool",
        "rollback_config",
        "restart_db",
        "escalate_to_human",
    } <= names


def test_open_incident_hides_fault_hint_until_closed(client: TestClient) -> None:
    incident_id = db.execute(
        """
        INSERT INTO incidents
            (title, severity, status, fault_hint_hidden, opened_at)
        VALUES (?, ?, 'investigating', ?, ?)
        """,
        ("5xx on /checkout", "high", "bad_config", db.now_iso()),
    )

    open_response = client.get(f"/incidents/{incident_id}")
    assert open_response.status_code == 200
    assert "fault_hint_hidden" not in open_response.json()["incident"]

    db.execute(
        "UPDATE incidents SET status = 'resolved' WHERE id = ?",
        (incident_id,),
    )
    closed_response = client.get(f"/incidents/{incident_id}")
    assert closed_response.json()["incident"]["fault_hint_hidden"] == "bad_config"


def test_incident_list_hides_hint_for_active_incident(client: TestClient) -> None:
    db.execute(
        """
        INSERT INTO incidents
            (title, severity, status, fault_hint_hidden, opened_at)
        VALUES (?, ?, 'open', ?, ?)
        """,
        ("5xx on /checkout", "high", "payment_down", db.now_iso()),
    )

    response = client.get("/incidents")
    assert response.status_code == 200
    assert "fault_hint_hidden" not in response.json()[0]


def test_reset_clears_incident_rows(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    incident_id = db.execute(
        """
        INSERT INTO incidents (title, severity, status, opened_at)
        VALUES (?, ?, 'open', ?)
        """,
        ("SANDBOX test", "low", db.now_iso()),
    )

    monkeypatch.setattr(api, "_sandbox_request", lambda _path, _method="GET": (200, {"ok": True}))
    response = client.post("/reset")

    assert response.status_code == 200
    assert db.query_one("SELECT id FROM incidents WHERE id = ?", (incident_id,)) is None