"""Initialize the database and idempotently seed the remediation catalog."""

from __future__ import annotations

from backend.db import execute, init_db, query

CATALOG = (
    {
        "name": "rotate_logs",
        "description": "Rotate sandbox logs and reclaim disk space.",
        "risk": "low",
        "blast_radius": "ShopNode sandbox log files",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 0,
    },
    {
        "name": "restart_worker",
        "description": "Restart the sandbox worker and release leaked connections.",
        "risk": "low",
        "blast_radius": "ShopNode sandbox background worker",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 0,
    },
    {
        "name": "resize_pool",
        "description": "Increase sandbox database connection pool capacity.",
        "risk": "medium",
        "blast_radius": "ShopNode sandbox database connection pool",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 1,
    },
    {
        "name": "rollback_config",
        "description": "Restore the previous ShopNode sandbox configuration.",
        "risk": "medium",
        "blast_radius": "ShopNode sandbox configuration",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 1,
    },
    {
        "name": "restart_db",
        "description": "Restart the ShopNode sandbox database service.",
        "risk": "high",
        "blast_radius": "All ShopNode sandbox database clients",
        "requires_approval": 1,
        "reversible": 0,
        "dry_run_supported": 0,
    },
    {
        "name": "escalate_to_human",
        "description": "Page a human with the sandbox diagnosis and evidence.",
        "risk": "low",
        "blast_radius": "Sandbox on-call notification",
        "requires_approval": 0,
        "reversible": 1,
        "dry_run_supported": 0,
    },
)


def seed() -> None:
    """Initialize schema and insert or refresh all catalog entries."""
    init_db()
    for item in CATALOG:
        execute(
            """
            INSERT INTO remediation_catalog (
                name, description, risk, blast_radius, requires_approval,
                reversible, dry_run_supported
            )
            VALUES (
                :name, :description, :risk, :blast_radius, :requires_approval,
                :reversible, :dry_run_supported
            )
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                risk = excluded.risk,
                blast_radius = excluded.blast_radius,
                requires_approval = excluded.requires_approval,
                reversible = excluded.reversible,
                dry_run_supported = excluded.dry_run_supported
            """,
            item,
        )


def main() -> None:
    seed()
    rows = query(
        """
        SELECT name, risk, requires_approval
        FROM remediation_catalog
        ORDER BY name
        """
    )
    print(f"Seeded {len(rows)} remediation catalog entries.")
    for row in rows:
        approval = "approval required" if row["requires_approval"] else "no approval"
        print(f"- {row['name']}: {row['risk']} risk, {approval}")


if __name__ == "__main__":
    main()