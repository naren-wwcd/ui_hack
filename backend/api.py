"""AutoHeal SANDBOX REST API and server-sent event endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import random
from contextlib import suppress
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import db, watcher
from .catalog import get_catalog
from .events import subscribe, unsubscribe
from .guard import approve as approve_action
from .guard import reject as reject_action
from .llm import get_llm_status

SANDBOX_URL = os.getenv("SANDBOX_URL", "http://localhost:8001").rstrip("/")
SCENARIOS = (
    "bad_config",
    "pool_leak",
    "disk_full",
    "payment_down",
    "db_hung",
    "compound",
)
app = FastAPI(title="AutoHeal SANDBOX API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_watcher_task: asyncio.Task[Any] | None = None


def _sandbox_request(path: str, method: str = "GET") -> tuple[int, dict[str, Any]]:
    request = Request(f"{SANDBOX_URL}{path}", data=b"" if method == "POST" else None, method=method)
    try:
        with urlopen(request, timeout=4) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"detail": body}
        return exc.code, payload
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _incident_payload(incident_id: int) -> dict[str, Any] | None:
    incident = db.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not incident:
        return None

    status = incident.get("status")
    if status not in {"resolved", "escalated"}:
        incident.pop("fault_hint_hidden", None)

    result: dict[str, Any] = {"incident": incident}
    for table, key in (
        ("agent_steps", "steps"),
        ("hypotheses", "hypotheses"),
        ("actions", "actions"),
        ("approvals", "approvals"),
        ("health_checks", "health_checks"),
        ("rpa_tasks", "rpa_tasks"),
        ("pages", "pages"),
    ):
        result[key] = db.query(
            f"SELECT * FROM {table} WHERE incident_id = ? ORDER BY id",
            (incident_id,),
        )
    return result


async def _insert_fault(name: str) -> dict[str, Any]:
    scenario = name
    if name == "random":
        scenario = random.choice(SCENARIOS)
    if scenario not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown sandbox fault: {name}")

    watcher.set_pending_fault_hint(scenario)
    status, payload = await asyncio.to_thread(
        _sandbox_request,
        f"/admin/fault/{scenario}",
        "POST",
    )
    if status < 200 or status >= 300 or payload.get("ok") is False:
        watcher.clear_pending_fault_hint()
        raise HTTPException(
            status_code=502,
            detail={"message": "Could not inject SANDBOX fault", "sandbox": payload},
        )
    # Do not include `scenario` in a random fault response; it is hidden metadata.
    return {
        "ok": True,
        "detail": "SANDBOX fault injected; the alert watcher will open an incident.",
    }


@app.on_event("startup")
async def startup() -> None:
    global _watcher_task
    db.init_db()
    from scripts.seed import seed

    seed()
    if os.getenv("AUTOHEAL_DISABLE_BACKGROUND", "").lower() in {"1", "true", "yes"}:
        return
    timeout = float(os.getenv("SANDBOX_STARTUP_WAIT_S", "30"))
    await watcher.wait_for_sandbox(timeout)
    _watcher_task = asyncio.create_task(watcher.watch_loop(), name="autoheal-watcher")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _watcher_task
    watcher.cancel_agent_tasks()
    if _watcher_task:
        _watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await _watcher_task
        _watcher_task = None


@app.post("/faults/{name}")
async def inject_fault(name: str) -> dict[str, Any]:
    return await _insert_fault(name)


@app.post("/reset")
async def reset() -> dict[str, Any]:
    status, payload = await asyncio.to_thread(_sandbox_request, "/admin/reset", "POST")
    if status < 200 or status >= 300:
        raise HTTPException(status_code=502, detail={"sandbox": payload})

    watcher.cancel_agent_tasks()
    watcher.reset_watch_state()
    # Removing incidents cascades to their steps, approvals, pages and queued RPA
    # tasks. The remediation catalog is shared seed data and is retained.
    db.execute("DELETE FROM incidents")
    publish_data = {"ok": True, "detail": "SANDBOX reset; incident state cleared."}
    from .events import publish

    publish("incident_updated", {"status": "reset"})
    return publish_data


@app.get("/incidents")
async def list_incidents() -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM incidents ORDER BY id DESC")
    for row in rows:
        if row.get("status") not in {"resolved", "escalated"}:
            row.pop("fault_hint_hidden", None)
    return rows


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: int) -> dict[str, Any]:
    result = _incident_payload(incident_id)
    if result is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return result


@app.get("/stream")
async def stream() -> StreamingResponse:
    queue = subscribe()

    async def events():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: int, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    decided_by = str(body.get("decided_by", "human"))
    result = approve_action(approval_id, decided_by)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    from .agent import resume_after_approval

    asyncio.create_task(resume_after_approval(approval_id))
    return result


@app.post("/approvals/{approval_id}/reject")
async def reject(approval_id: int, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    decided_by = str(body.get("decided_by", "human"))
    result = reject_action(approval_id, decided_by)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    from .agent import resume_after_approval

    asyncio.create_task(resume_after_approval(approval_id))
    return result


@app.get("/catalog")
async def catalog() -> list[dict[str, Any]]:
    return get_catalog()


@app.get("/health-summary")
async def health_summary() -> dict[str, Any]:
    status, metrics = await asyncio.to_thread(_sandbox_request, "/metrics")
    health_status, health = await asyncio.to_thread(_sandbox_request, "/health")

    error_rate = float(metrics.get("error_rate", 0.0) or 0.0)
    p95_ms = float(metrics.get("p95_ms", 0.0) or 0.0)
    app_state = "healthy" if health_status == 200 and health.get("ok") else "down"
    db_state = str(metrics.get("db_status", "unknown")).lower()
    worker_state = str(metrics.get("worker_status", "unknown")).lower()
    disk_pct = float(metrics.get("disk_used_pct", 0.0) or 0.0)
    payment_state = str(metrics.get("payment_status", "unknown")).lower()

    services = {
        "app": app_state,
        "db": "healthy" if db_state == "ok" else db_state,
        "worker": worker_state,
        "disk": "healthy" if disk_pct < 90 else ("degraded" if disk_pct < 100 else "down"),
        "payment": "healthy" if payment_state == "ok" else payment_state,
    }

    if status == 0 or health_status == 0 or "down" in services.values():
        overall = "DOWN"
    elif error_rate > 0.02 or disk_pct >= 90 or any(
        value not in {"healthy", "running", "ok"} for value in services.values()
    ):
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    return {
        "overall": overall,
        "services": services,
        "error_rate": error_rate,
        "p95_ms": p95_ms,
        "llm_mode": get_llm_status(),
        "sandbox": True,
    }


@app.post("/demo/run/{scenario}")
async def demo_run(scenario: str) -> dict[str, Any]:
    """Inject a named SANDBOX fault and open its incident immediately."""
    if scenario not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario: {scenario}")
    await _insert_fault(scenario)
    incident_id = watcher.open_incident_now(scenario)
    if incident_id is None:
        active = db.query_one(
            """
            SELECT id FROM incidents
            WHERE status IN ('open', 'investigating', 'awaiting_approval')
            ORDER BY id DESC LIMIT 1
            """
        )
        incident_id = active["id"] if active else None
    return {
        "ok": True,
        "incident_id": incident_id,
        "detail": "SANDBOX demo started.",
    }