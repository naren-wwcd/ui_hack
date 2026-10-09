"""Metric watcher that automatically opens sandbox incidents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

from . import db
from .events import publish

logger = logging.getLogger(__name__)

SANDBOX_URL = os.getenv("SANDBOX_URL", "http://localhost:8001").rstrip("/")
ERROR_RATE_THRESHOLD = float(os.getenv("ERROR_RATE_THRESHOLD", "0.05"))
POLL_INTERVAL_S = 1.0
CONSECUTIVE_SAMPLES = 3
SCENARIOS = (
    "bad_config",
    "pool_leak",
    "disk_full",
    "payment_down",
    "db_hung",
    "compound",
)
_OPEN_STATUSES = ("open", "investigating", "awaiting_approval")

_hint_lock = threading.Lock()
_pending_fault_hint: str | None = None
_agent_tasks: set[asyncio.Task[Any]] = set()
_watch_task: asyncio.Task[Any] | None = None
_consecutive_bad_samples = 0


def set_pending_fault_hint(scenario: str | None) -> None:
    """Associate an injected fault with the next incident, internally only."""
    global _pending_fault_hint
    with _hint_lock:
        _pending_fault_hint = scenario


def clear_pending_fault_hint() -> None:
    set_pending_fault_hint(None)


def reset_watch_state() -> None:
    """Reset watcher debounce state and any pending hidden hint."""
    global _consecutive_bad_samples
    _consecutive_bad_samples = 0
    clear_pending_fault_hint()


def _take_pending_fault_hint() -> str | None:
    global _pending_fault_hint
    with _hint_lock:
        value = _pending_fault_hint
        _pending_fault_hint = None
        return value


def _fetch_metrics_sync() -> dict[str, Any]:
    try:
        with urlopen(f"{SANDBOX_URL}/metrics", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        logger.warning("Sandbox metrics request failed with HTTP %s", exc.code)
    except Exception as exc:
        logger.debug("Sandbox metrics are not available yet: %s", exc)
    return {}


def _has_open_incident() -> bool:
    placeholders = ",".join("?" for _ in _OPEN_STATUSES)
    row = db.query_one(
        f"SELECT id FROM incidents WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        _OPEN_STATUSES,
    )
    return row is not None


def _enqueue_rpa_create(incident_id: int, payload: dict[str, Any]) -> int | None:
    try:
        return db.execute(
            """
            INSERT INTO rpa_tasks (incident_id, kind, status, ts, payload_json)
            VALUES (?, 'create_incident', 'queued', ?, ?)
            """,
            (incident_id, db.now_iso(), json.dumps(payload, ensure_ascii=False)),
        )
    except Exception:
        logger.exception("Could not enqueue RPA create_incident task")
        return None


async def _run_agent(incident_id: int) -> None:
    try:
        from .agent import run_incident

        await run_incident(incident_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Agent task failed for incident %s", incident_id)
        publish(
            "incident_updated",
            {
                "incident_id": incident_id,
                "status": "investigating",
                "error": "Agent task failed; inspect backend logs.",
            },
        )


def start_agent(incident_id: int) -> asyncio.Task[Any]:
    """Start the incident agent as an asyncio task and retain it until done."""
    task = asyncio.create_task(_run_agent(incident_id), name=f"agent-{incident_id}")
    _agent_tasks.add(task)
    task.add_done_callback(_agent_tasks.discard)
    return task


def open_incident_now(fault_hint_hidden: str | None = None) -> int | None:
    """Open one incident if none is active; omit the hint from published data."""
    if _has_open_incident():
        return None

    hint = fault_hint_hidden if fault_hint_hidden in SCENARIOS else _take_pending_fault_hint()
    incident_id = db.execute(
        """
        INSERT INTO incidents
            (title, severity, status, fault_hint_hidden, opened_at)
        VALUES (?, ?, 'open', ?, ?)
        """,
        (
            "5xx on /checkout",
            "high",
            hint,
            db.now_iso(),
        ),
    )
    payload = {
        "title": "5xx on /checkout",
        "severity": "high",
        "diagnosis": "SANDBOX checkout error-rate threshold exceeded.",
    }
    rpa_task_id = _enqueue_rpa_create(incident_id, payload)
    event = {
        "incident_id": incident_id,
        "title": "5xx on /checkout",
        "severity": "high",
        "status": "open",
        "rpa_task_id": rpa_task_id,
    }
    publish("incident_opened", event)
    start_agent(incident_id)
    return incident_id


async def watch_loop() -> None:
    """Poll sandbox metrics and open one incident after three bad samples."""
    global _consecutive_bad_samples
    while True:
        try:
            metrics = await asyncio.to_thread(_fetch_metrics_sync)
            if metrics:
                try:
                    error_rate = float(metrics.get("error_rate", 0.0))
                except (TypeError, ValueError):
                    error_rate = 0.0

                publish("metrics", metrics)
                if error_rate > ERROR_RATE_THRESHOLD:
                    _consecutive_bad_samples += 1
                else:
                    _consecutive_bad_samples = 0

                if (
                    _consecutive_bad_samples >= CONSECUTIVE_SAMPLES
                    and not _has_open_incident()
                ):
                    _consecutive_bad_samples = 0
                    open_incident_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Watcher iteration failed")
        await asyncio.sleep(POLL_INTERVAL_S)


async def wait_for_sandbox(timeout_s: float = 30.0) -> bool:
    """Wait for the sandbox health endpoint without blocking the event loop."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            def check() -> bool:
                with urlopen(f"{SANDBOX_URL}/health", timeout=2) as response:
                    return response.status == 200

            if await asyncio.to_thread(check):
                return True
        except Exception:
            await asyncio.sleep(0.5)
    return False


def cancel_agent_tasks() -> None:
    """Request cancellation of currently running incident agents."""
    for task in tuple(_agent_tasks):
        task.cancel()