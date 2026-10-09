"""Polling worker that runs queued ITSM portal tasks with retries."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from backend import db
from backend.events import publish
from rpa.bot import process_task

logger = logging.getLogger("autoheal.rpa")
POLL_INTERVAL_S = float(os.getenv("RPA_POLL_INTERVAL_S", "1"))
MAX_ATTEMPTS = max(1, int(os.getenv("RPA_MAX_ATTEMPTS", "3")))


def _claim_next() -> dict[str, Any] | None:
    """Atomically mark one queued task running and return its row."""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM rpa_tasks
            WHERE status = 'queued'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        conn.execute(
            "UPDATE rpa_tasks SET status = 'running' WHERE id = ? AND status = 'queued'",
            (row["id"],),
        )
        conn.commit()
        task = dict(row)
        task["status"] = "running"
        return task
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _payload(task: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(task.get("payload_json") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _run_with_retries(task: dict[str, Any]) -> dict[str, Any]:
    last_result: dict[str, Any] = {"ok": False, "detail": "Task was not attempted."}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            last_result = process_task(task)
        except Exception as exc:
            last_result = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "screenshot_path": None,
            }
        if last_result.get("ok") is True:
            last_result["attempts"] = attempt
            return last_result
        logger.warning(
            "RPA task %s attempt %s/%s failed: %s",
            task["id"],
            attempt,
            MAX_ATTEMPTS,
            last_result.get("detail"),
        )
    last_result["attempts"] = MAX_ATTEMPTS
    return last_result


def process_one_task() -> bool:
    """Claim and process at most one queued task; return whether one was found."""
    task = _claim_next()
    if task is None:
        return False

    task_payload = _payload(task)
    task["payload"] = task_payload
    publish(
        "rpa_task",
        {
            "id": task["id"],
            "incident_id": task["incident_id"],
            "kind": task["kind"],
            "status": "running",
        },
    )

    result = _run_with_retries(task)
    status = "done" if result.get("ok") is True else "failed"
    task_payload["worker_result"] = result
    db.execute(
        """
        UPDATE rpa_tasks
        SET status = ?, screenshot_path = ?, payload_json = ?
        WHERE id = ?
        """,
        (
            status,
            result.get("screenshot_path"),
            json.dumps(task_payload, ensure_ascii=False),
            task["id"],
        ),
    )
    event = {
        "id": task["id"],
        "incident_id": task["incident_id"],
        "kind": task["kind"],
        "status": status,
        "screenshot_path": result.get("screenshot_path"),
        "detail": result.get("detail"),
        "attempts": result.get("attempts"),
    }
    publish("rpa_task", event)
    return True


async def worker_loop() -> None:
    """Poll the shared SQLite queue until the process is stopped."""
    db.init_db()
    logger.info("RPA SANDBOX worker started")
    while True:
        try:
            found = await asyncio.to_thread(process_one_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("RPA worker loop encountered an error")
            found = False
        if not found:
            await asyncio.sleep(POLL_INTERVAL_S)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        logger.info("RPA SANDBOX worker stopped")


if __name__ == "__main__":
    main()