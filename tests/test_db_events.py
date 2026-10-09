"""Tests for the shared SQLite schema and event bus."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from backend import db, events


def test_init_db_creates_contract_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "autoheal.db"
    monkeypatch.setenv("DB_PATH", str(database))

    db.init_db()
    db.init_db()  # Initialization is safe to repeat.

    conn = db.get_conn()
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        expected = {
            "incidents",
            "agent_steps",
            "hypotheses",
            "actions",
            "approvals",
            "health_checks",
            "rpa_tasks",
            "pages",
            "remediation_catalog",
        }
        assert expected <= tables

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(rpa_tasks)").fetchall()
        }
        assert {
            "id",
            "incident_id",
            "kind",
            "status",
            "screenshot_path",
            "ts",
            "payload_json",
        } <= columns
    finally:
        conn.close()


def test_execute_query_and_query_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "autoheal.db"))
    db.init_db()

    incident_id = db.execute(
        """
        INSERT INTO incidents (title, severity, status, opened_at)
        VALUES (?, ?, ?, ?)
        """,
        ("SANDBOX test incident", "high", "open", db.now_iso()),
    )

    assert incident_id > 0
    row = db.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    assert row is not None
    assert row["title"] == "SANDBOX test incident"

    rows = db.query("SELECT id FROM incidents WHERE status = ?", ("open",))
    assert rows == [{"id": incident_id}]


def test_now_iso_is_utc() -> None:
    stamp = db.now_iso()
    assert stamp.endswith("Z")
    assert "T" in stamp


@pytest.mark.asyncio
async def test_publish_delivers_event_to_subscriber() -> None:
    queue = events.subscribe()
    try:
        events.publish("metrics", {"error_rate": 0.1})
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == "metrics"
        assert event["data"] == {"error_rate": 0.1}
        assert event["ts"].endswith("Z")
    finally:
        events.unsubscribe(queue)


@pytest.mark.asyncio
async def test_publish_from_another_thread_is_non_blocking() -> None:
    queue = events.subscribe()
    try:
        thread = threading.Thread(
            target=events.publish,
            args=("incident_updated", {"id": 42}),
        )
        thread.start()
        thread.join(timeout=1.0)
        assert not thread.is_alive()

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == "incident_updated"
        assert event["data"]["id"] == 42
    finally:
        events.unsubscribe(queue)


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    queue = events.subscribe()
    events.unsubscribe(queue)
    events.publish("metrics", {"error_rate": 0.0})
    await asyncio.sleep(0)

    assert queue.empty()