"""Remediation catalog access (SANDBOX). The agent can ONLY choose from this catalog."""

from __future__ import annotations

import sqlite3
from typing import Any

from backend.db import execute, init_db, query, query_one

DEFAULT_CATALOG: list[dict[str, Any]] = [
    {
        "name": "rotate_logs",
        "description": "Rotate and archive application logs to free disk space.",
        "risk": "low",
        "blast_radius": "ShopNode log files only; no request is interrupted",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 0,
    },
    {
        "name": "restart_worker",
        "description": "Restart the ShopNode worker process and drain its DB connection pool.",
        "risk": "low",
        "blast_radius": "One worker; in-flight requests may be retried",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 0,
    },
    {
        "name": "resize_pool",
        "description": "Increase the DB connection pool size. Does not release leaked connections.",
        "risk": "medium",
        "blast_radius": "Application DB pool configuration",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 1,
    },
    {
        "name": "rollback_config",
        "description": "Roll the application config back to the previous known-good version.",
        "risk": "medium",
        "blast_radius": "Whole application configuration",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 1,
    },
    {
        "name": "restart_db",
        "description": "Restart the database server. Drops all connections.",
        "risk": "high",
        "blast_radius": "Entire database; every service using it is interrupted",
        "requires_approval": 1,
        "reversible": 0,
        "dry_run_supported": 0,
    },
    {
        "name": "escalate_to_human",
        "description": "Page the on-call engineer with the diagnosis. Always allowed.",
        "risk": "low",
        "blast_radius": "None; notification only",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 0,
    },
]

_RISK_ORDER = "CASE risk WHEN 'low' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("requires_approval", "reversible", "dry_run_supported"):
        out[key] = bool(out.get(key))
    return out


def ensure_seeded() -> None:
    """Insert any missing default catalog rows (idempotent)."""
    init_db()
    for item in DEFAULT_CATALOG:
        execute(
            "INSERT OR IGNORE INTO remediation_catalog "
            "(name, description, risk, blast_radius, requires_approval, reversible, dry_run_supported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item["name"],
                item["description"],
                item["risk"],
                item["blast_radius"],
                item["requires_approval"],
                item["reversible"],
                item["dry_run_supported"],
            ),
        )


def get_catalog() -> list[dict[str, Any]]:
    sql = f"SELECT * FROM remediation_catalog ORDER BY {_RISK_ORDER}, name"
    try:
        rows = query(sql)
    except sqlite3.OperationalError:
        ensure_seeded()
        rows = query(sql)
    if not rows:
        ensure_seeded()
        rows = query(sql)
    return [_normalize(r) for r in rows]


def get_fix(name: str) -> dict[str, Any] | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        row = query_one("SELECT * FROM remediation_catalog WHERE name = ?", (name,))
    except sqlite3.OperationalError:
        ensure_seeded()
        row = query_one("SELECT * FROM remediation_catalog WHERE name = ?", (name,))
    if row is None:
        get_catalog()  # seeds if the table was empty, then retry once
        row = query_one("SELECT * FROM remediation_catalog WHERE name = ?", (name,))
    return _normalize(row) if row else None


def catalog_names() -> list[str]:
    return [c["name"] for c in get_catalog()]