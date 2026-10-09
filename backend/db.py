"""SQLite database helpers and the shared AutoHeal schema."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_DB_PATH = "data/autoheal.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN (
            'open', 'investigating', 'awaiting_approval', 'resolved', 'escalated'
        )),
    fault_hint_hidden TEXT,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    mttr_s REAL
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    type TEXT NOT NULL,
    thought TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ts TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    cause TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'confirmed', 'rejected')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    fix TEXT NOT NULL,
    risk TEXT NOT NULL,
    decision TEXT NOT NULL
        CHECK (decision IN ('allowed', 'blocked', 'needs_approval')),
    reason TEXT NOT NULL,
    result TEXT,
    ts TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    action_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by TEXT,
    ts TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE,
    FOREIGN KEY (action_id) REFERENCES actions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS health_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error_rate REAL,
    detail TEXT,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rpa_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('create_incident', 'add_note', 'resolve', 'escalate')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed')),
    screenshot_path TEXT,
    ts TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    body_json TEXT NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS remediation_catalog (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    blast_radius TEXT NOT NULL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    reversible INTEGER NOT NULL DEFAULT 1,
    dry_run_supported INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_agent_steps_incident_idx
    ON agent_steps (incident_id, idx);
CREATE INDEX IF NOT EXISTS idx_hypotheses_incident
    ON hypotheses (incident_id);
CREATE INDEX IF NOT EXISTS idx_actions_incident
    ON actions (incident_id);
CREATE INDEX IF NOT EXISTS idx_approvals_incident
    ON approvals (incident_id);
CREATE INDEX IF NOT EXISTS idx_health_checks_incident_ts
    ON health_checks (incident_id, ts);
CREATE INDEX IF NOT EXISTS idx_rpa_tasks_status_ts
    ON rpa_tasks (status, ts);
CREATE INDEX IF NOT EXISTS idx_incidents_status_opened
    ON incidents (status, opened_at);
"""


def _db_path() -> Path:
    raw = os.environ.get("DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
    return Path(raw).expanduser()


def get_conn() -> sqlite3.Connection:
    """Open a configured connection with row objects and WAL support."""
    path = _db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level="DEFERRED",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # WAL improves reader/writer concurrency for the API and RPA worker.
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    """Create the shared schema and indexes if they do not already exist."""
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def execute(
    sql: str,
    params: Iterable[Any] | Mapping[str, Any] = (),
) -> int:
    """Execute one statement, commit it, and return its last inserted row id."""
    conn = get_conn()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def query(
    sql: str,
    params: Iterable[Any] | Mapping[str, Any] = (),
) -> list[dict[str, Any]]:
    """Execute a query and return every row as a plain dictionary."""
    conn = get_conn()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(
    sql: str,
    params: Iterable[Any] | Mapping[str, Any] = (),
) -> dict[str, Any] | None:
    """Execute a query and return one row as a plain dictionary, if present."""
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def now_iso() -> str:
    """Return a millisecond precision ISO-8601 UTC timestamp."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )