"""Sandbox health verification following a remediation action."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from . import db
from .events import publish

SANDBOX_URL = os.getenv("SANDBOX_URL", "http://localhost:8001").rstrip("/")
CHECK_COUNT = 5
CHECK_INTERVAL_S = 2.0


def _get_json(path: str, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    try:
        with urlopen(f"{SANDBOX_URL}{path}", timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"detail": body}
    except (URLError, TimeoutError, OSError) as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _relevant_metric(fix_name: str, metrics: dict[str, Any]) -> tuple[bool, str]:
    if fix_name == "rotate_logs":
        disk = float(metrics.get("disk_used_pct", 100.0))
        return disk < 90.0, f"disk_used_pct={disk:g} (must be < 90)"

    if fix_name == "restart_worker":
        used = int(metrics.get("db_pool_used", 0))
        maximum = max(1, int(metrics.get("db_pool_max", 1)))
        worker = str(metrics.get("worker_status", "unknown")).lower()
        ok = worker == "running" and used <= max(1, int(maximum * 0.8))
        return ok, (
            f"worker_status={worker}, db_pool_used={used}, db_pool_max={maximum} "
            "(worker must run and pool use must be <= 80%)"
        )

    if fix_name == "rollback_config":
        db_status = str(metrics.get("db_status", "unknown")).lower()
        status, state = _get_json("/admin/state")
        config = state.get("config", {}) if status == 200 else {}
        host_ok = config.get("db_host") == "db-prod-01"
        ok = db_status == "ok" and host_ok
        return ok, (
            f"db_status={db_status}, db_host={config.get('db_host', 'unknown')} "
            "(database must be healthy on the baseline host)"
        )

    if fix_name == "restart_db":
        db_status = str(metrics.get("db_status", "unknown")).lower()
        return db_status == "ok", f"db_status={db_status} (must be ok)"

    if fix_name == "resize_pool":
        used = int(metrics.get("db_pool_used", 0))
        maximum = max(1, int(metrics.get("db_pool_max", 1)))
        return used < maximum, f"db_pool_used={used}, db_pool_max={maximum}"

    return True, "No fix-specific metric check."


def _one_check(fix_name: str) -> dict[str, Any]:
    health_status, health = _get_json("/health")
    metrics_status, metrics = _get_json("/metrics")
    error_rate = metrics.get("error_rate")

    try:
        error_rate_value = float(error_rate)
    except (TypeError, ValueError):
        error_rate_value = 1.0

    health_ok = health_status == 200 and health.get("ok") is True
    error_ok = error_rate_value < 0.02
    metric_ok, metric_detail = _relevant_metric(fix_name, metrics)
    ok = health_ok and error_ok and metric_ok

    detail = {
        "health_status": health_status,
        "health": health,
        "metrics_status": metrics_status,
        "error_rate": error_rate_value,
        "error_rate_below_2_percent": error_ok,
        "relevant_metric_ok": metric_ok,
        "relevant_metric": metric_detail,
    }
    if metrics_status != 200:
        detail["metrics_error"] = metrics.get("error", metrics.get("detail"))
    return {"ok": ok, "error_rate": error_rate_value, "detail": detail}


def _enqueue_rpa_resolve(incident_id: int, detail: str) -> int | None:
    try:
        return db.execute(
            """
            INSERT INTO rpa_tasks (incident_id, kind, status, ts, payload_json)
            VALUES (?, 'resolve', 'queued', ?, ?)
            """,
            (
                incident_id,
                db.now_iso(),
                json.dumps({"verification": detail}, ensure_ascii=False),
            ),
        )
    except Exception:
        return None


async def run(incident_id: int, fix_name: str) -> dict[str, Any]:
    """Run five health checks, persist them, and resolve only when all pass."""
    incident = db.query_one(
        "SELECT opened_at, status FROM incidents WHERE id = ?",
        (incident_id,),
    )
    if not incident:
        return {"ok": False, "checks": [], "detail": f"incident {incident_id} not found"}

    checks: list[dict[str, Any]] = []
    for index in range(CHECK_COUNT):
        if index:
            await asyncio.sleep(CHECK_INTERVAL_S)
        check = await asyncio.to_thread(_one_check, fix_name)
        checks.append(check)
        detail_text = json.dumps(check["detail"], ensure_ascii=False, separators=(",", ":"))
        check_id = db.execute(
            """
            INSERT INTO health_checks (incident_id, ts, ok, error_rate, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                db.now_iso(),
                int(check["ok"]),
                check["error_rate"],
                detail_text,
            ),
        )
        publish(
            "health_check",
            {
                "id": check_id,
                "incident_id": incident_id,
                "index": index + 1,
                "total": CHECK_COUNT,
                **check,
            },
        )

    all_ok = len(checks) == CHECK_COUNT and all(item["ok"] for item in checks)
    detail = {
        "fix": fix_name,
        "passed": sum(bool(item["ok"]) for item in checks),
        "total": len(checks),
        "checks": checks,
    }

    if all_ok:
        resolved_at = db.now_iso()
        try:
            from datetime import datetime

            opened = datetime.fromisoformat(
                str(incident["opened_at"]).replace("Z", "+00:00")
            )
            resolved = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
            mttr = max(0.0, (resolved - opened).total_seconds())
        except (TypeError, ValueError):
            mttr = None

        db.execute(
            """
            UPDATE incidents
            SET status = 'resolved', resolved_at = ?, mttr_s = ?
            WHERE id = ?
            """,
            (resolved_at, mttr, incident_id),
        )
        task_id = _enqueue_rpa_resolve(incident_id, "All five sandbox health checks passed.")
        event = {
            "incident_id": incident_id,
            "status": "resolved",
            "resolved_at": resolved_at,
            "mttr_s": mttr,
            "rpa_task_id": task_id,
        }
        publish("incident_resolved", event)
        publish("incident_updated", event)
        return {"ok": True, "checks": checks, "detail": detail, "mttr_s": mttr}

    return {"ok": False, "checks": checks, "detail": detail}