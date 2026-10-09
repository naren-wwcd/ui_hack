"""UI smoke script: create a dummy portal incident, add a note, then close it.

Start the portal first, then run:
    python -m scripts.test_rpa

The script uses the same headed Playwright browser workflow as the RPA bot.
"""

from __future__ import annotations

from rpa.bot import process_task


def main() -> None:
    dummy_backend_id = 9_000_001
    common = {
        "incident_id": dummy_backend_id,
        "payload": {
            "source_incident_id": dummy_backend_id,
            "short_description": "SANDBOX RPA smoke test",
            "severity": "medium",
            "category": "Application",
            "diagnosis": "Manual portal UI smoke test.",
            "evidence": "Synthetic evidence only. No real incident.",
        },
    }

    created = process_task({"id": "manual-create", "kind": "create_incident", **common})
    print("Create:", created)
    if not created.get("ok"):
        raise SystemExit(1)

    note = process_task({
        "id": "manual-note",
        "kind": "add_note",
        "incident_id": dummy_backend_id,
        "payload": {
            "source_incident_id": dummy_backend_id,
            "work_notes": "SANDBOX RPA smoke test: investigation note added.",
        },
    })
    print("Add note:", note)
    if not note.get("ok"):
        raise SystemExit(1)

    closed = process_task({
        "id": "manual-resolve",
        "kind": "resolve",
        "incident_id": dummy_backend_id,
        "payload": {
            "source_incident_id": dummy_backend_id,
            "resolution_code": "Solved (Permanently)",
            "resolution_notes": "SANDBOX RPA smoke test completed and closed.",
        },
    })
    print("Close:", closed)
    if not closed.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()