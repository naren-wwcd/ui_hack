"""ShopNode: the broken-able SANDBOX app (port 8001).

Real in-memory state, a real log file, a live traffic generator, injectable
faults and real remediation actions. Nothing here touches anything real.
"""

from __future__ import annotations

import os
import random
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

LOG_PATH = Path(os.environ.get("LOG_PATH", "logs/shopnode.log"))
GOOD_DB_HOST = "db-prod-01"
BAD_DB_HOST = "db-prod-02"
FAULT_NAMES = ("bad_config", "pool_leak", "disk_full", "payment_down", "db_hung")
SCENARIOS = FAULT_NAMES + ("compound",)
COMPOUND = ("bad_config", "disk_full")
BASE_DISK = 41.0

_lock = threading.RLock()
_stop = threading.Event()
_window: deque = deque()  # (timestamp, ok, latency_ms)
S: dict[str, Any] = {}


def _iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _log(level: str, message: str) -> None:
    with _lock:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{_iso()} {level} shopnode {message}\n")


def _fresh_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "config": {
            "db_host": GOOD_DB_HOST,
            "db_pool_size": 10,
            "payment_url": "https://payment-gw.internal/v1/charge",
        },
        "active_version": 3,
        "history": [
            {"version": 1, "author": "alice", "time": _iso(now - timedelta(days=30)),
             "summary": "initial production config",
             "diff": f"+ db_host={GOOD_DB_HOST}\n+ db_pool_size=5\n+ payment_url=https://payment-gw.internal/charge"},
            {"version": 2, "author": "bob", "time": _iso(now - timedelta(days=12)),
             "summary": "raise connection pool size",
             "diff": "- db_pool_size=5\n+ db_pool_size=10"},
            {"version": 3, "author": "carol", "time": _iso(now - timedelta(days=3)),
             "summary": "move payment client to /v1/charge",
             "diff": "- payment_url=https://payment-gw.internal/charge\n+ payment_url=https://payment-gw.internal/v1/charge"},
        ],
        "faults": set(),
        "pool_used": 2,
        "disk_used_pct": BASE_DISK,
        "worker_status": "running",
        "db_status": "ok",
        "payment_status": "ok",
    }


def _reset_state() -> None:
    with _lock:
        S.clear()
        S.update(_fresh_state())
        _window.clear()


_reset_state()


# ------------------------------------------------------------ fault logic
def _pool_max() -> int:
    return int(S["config"]["db_pool_size"])


def _active_causes() -> list[str]:
    causes: list[str] = []
    if "db_hung" in S["faults"] or S["db_status"] != "ok":
        causes.append("db_hung")
    if S["config"]["db_host"] != GOOD_DB_HOST:
        causes.append("bad_config")
    if S["disk_used_pct"] >= 100:
        causes.append("disk_full")
    if S["payment_status"] != "ok":
        causes.append("payment_down")
    if "pool_leak" in S["faults"] and S["pool_used"] >= _pool_max():
        causes.append("pool_leak")
    return causes


def _inject(name: str) -> None:
    with _lock:
        names = COMPOUND if name == "compound" else (name,)
        for n in names:
            S["faults"].add(n)
            if n == "bad_config" and S["active_version"] != 4:
                S["config"]["db_host"] = BAD_DB_HOST
                S["history"].append({
                    "version": 4, "author": "deploy-bot", "time": _iso(),
                    "summary": "switch database host",
                    "diff": f"- db_host={GOOD_DB_HOST}\n+ db_host={BAD_DB_HOST}",
                })
                S["active_version"] = 4
                _log("WARN", f"config v4 applied by deploy-bot: db_host {GOOD_DB_HOST} -> {BAD_DB_HOST}")
            elif n == "pool_leak":
                S["pool_used"] = max(S["pool_used"], 2)
            elif n == "disk_full":
                S["disk_used_pct"] = max(S["disk_used_pct"], 55.0)
            elif n == "payment_down":
                S["payment_status"] = "down"
            elif n == "db_hung":
                S["db_status"] = "unresponsive"


def _checkout() -> tuple[int, dict]:
    with _lock:
        # pool leak: connections are taken and never released
        if "pool_leak" in S["faults"] and random.random() < 0.2 and S["pool_used"] < _pool_max():
            S["pool_used"] += 1
            pct = int(100 * S["pool_used"] / _pool_max())
            if pct >= 50:
                _log("WARN", f"db pool usage {S['pool_used']}/{_pool_max()} ({pct}%)")

        causes = _active_causes()
        if causes:
            cause = random.choice(causes)
            if cause == "db_hung":
                latency = 5000 + random.uniform(0, 300)
                _log("ERROR", "db query timed out after 5000ms: database not responding (connect hang)")
            elif cause == "bad_config":
                latency = random.uniform(15, 40)
                _log("ERROR", f"connection refused {S['config']['db_host']}:5432 while opening checkout transaction")
            elif cause == "disk_full":
                latency = random.uniform(60, 120)
                _log("ERROR", "OSError: [Errno 28] No space left on device: '/var/log/shopnode/orders.wal'")
            elif cause == "payment_down":
                latency = 3000 + random.uniform(0, 200)
                _log("ERROR", "upstream timeout payment-gw after 3000ms (POST /v1/charge)")
            else:  # pool_leak
                latency = 2500 + random.uniform(0, 300)
                _log("ERROR", f"QueuePool limit of size {_pool_max()} overflow 0 reached, connection timed out, timeout 30")
            _window.append((time.time(), False, latency))
            return 503, {"error": "checkout failed", "status": 503}

        latency = 60 + random.uniform(0, 40)
        if "pool_leak" in S["faults"]:
            latency += (S["pool_used"] / _pool_max()) ** 3 * 1500
        _window.append((time.time(), True, latency))
        if random.random() < 0.07:
            _log("INFO", f"order created id={uuid.uuid4().hex[:8]} latency={int(latency)}ms")
        return 200, {"order_id": uuid.uuid4().hex[:10], "status": "created"}


def _noise() -> None:
    """Always-on red herrings. None of these cause the 5xx errors."""
    if random.random() < 0.12:
        _log("WARN", f"cache miss ratio high ({random.uniform(0.55, 0.8):.2f}) for key prefix catalog:*")
    if random.random() < 0.08:
        _log("WARN", "deprecated API /v1/cart used by client mobile-3.1 (use /v2/cart)")
    if random.random() < 0.05:
        _log("INFO", random.choice(["GET /v1/cart/legacy 404 Not Found", "GET /favicon.ico 404 Not Found"]))
    if random.random() < 0.05:
        _log("WARN", "slow query on /reports 1.8s (SELECT * FROM orders ORDER BY created_at)")


def _traffic_loop() -> None:
    while not _stop.is_set():
        try:
            _checkout()
            _noise()
        except Exception as exc:  # keep the generator alive
            _log("ERROR", f"traffic generator error: {exc}")
        _stop.wait(0.2)  # about 5 req/s


def _tick_loop() -> None:
    while not _stop.is_set():
        with _lock:
            if "disk_full" in S["faults"] and S["disk_used_pct"] < 100:
                before = S["disk_used_pct"]
                S["disk_used_pct"] = min(100.0, before + 5.0)
                if before < 90 <= S["disk_used_pct"]:
                    _log("WARN", f"disk usage {S['disk_used_pct']:.0f}% on /var/log")
                if S["disk_used_pct"] >= 100:
                    _log("ERROR", "disk usage 100% on /var/log, writes failing")
            elif "disk_full" not in S["faults"]:
                S["disk_used_pct"] = round(BASE_DISK + random.uniform(-0.4, 0.4), 1)
            if S["worker_status"] != "stopped":
                leaking = "pool_leak" in S["faults"] and S["pool_used"] / _pool_max() >= 0.8
                S["worker_status"] = "degraded" if leaking else "running"
        _stop.wait(1.0)


def _metrics(window_s: float = 5.0) -> dict:
    window_s = max(1.0, min(120.0, float(window_s)))
    now = time.time()
    with _lock:
        while _window and now - _window[0][0] > 120:
            _window.popleft()
        rows = [r for r in _window if now - r[0] <= window_s]
        total = len(rows)
        fails = sum(1 for r in rows if not r[1])
        lat = sorted(r[2] for r in rows)
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else 0.0
        return {
            "error_rate": round(fails / total, 4) if total else 0.0,
            "p95_ms": round(p95, 1),
            "db_pool_used": S["pool_used"],
            "db_pool_max": _pool_max(),
            "disk_used_pct": round(S["disk_used_pct"], 1),
            "worker_status": S["worker_status"],
            "db_status": S["db_status"],
            "payment_status": S["payment_status"],
            "config_version": S["active_version"],
            "requests": total,
        }


# ------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    _stop.clear()
    _log("INFO", "ShopNode started (SANDBOX): nothing here touches real systems")
    for target in (_traffic_loop, _tick_loop):
        threading.Thread(target=target, daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="ShopNode (SANDBOX)", lifespan=lifespan)


@app.get("/health")
def health():
    with _lock:
        if _active_causes():
            return JSONResponse({"ok": False, "reason": "checkout failing"}, status_code=503)
    return {"ok": True}


@app.get("/checkout")
def checkout():
    status, body = _checkout()
    return JSONResponse(body, status_code=status)


@app.get("/reports")
def reports():
    _log("WARN", "slow query on /reports 1.8s (SELECT * FROM orders ORDER BY created_at)")
    return {"rows": 120, "took_ms": 1800}


@app.get("/metrics")
def metrics(window_s: float = 5.0):
    return _metrics(window_s)


# ------------------------------------------------------------- admin API
@app.post("/admin/fault/{scenario}")
def inject_fault(scenario: str):
    if scenario not in SCENARIOS:
        return JSONResponse({"ok": False, "detail": f"unknown scenario; choose from {list(SCENARIOS)}"}, status_code=400)
    _inject(scenario)
    return {"ok": True, "injected": scenario}


@app.post("/admin/reset")
def reset():
    _reset_state()
    _log("INFO", "sandbox reset to healthy state")
    return {"ok": True}


@app.get("/admin/state")
def state():
    """Debug view for tests and scripts. The agent's tools do NOT use this."""
    with _lock:
        return {
            "faults": sorted(S["faults"]),
            "causes": _active_causes(),
            "config": dict(S["config"]),
            "active_version": S["active_version"],
            **{k: S[k] for k in ("pool_used", "disk_used_pct", "worker_status", "db_status", "payment_status")},
        }


@app.get("/admin/config")
def config():
    with _lock:
        return {"active_version": S["active_version"], "config": dict(S["config"]), "history": list(S["history"])}


@app.get("/admin/connectivity/{target}")
def connectivity(target: str):
    with _lock:
        if target == "db":
            host = S["config"]["db_host"]
            if S["db_status"] != "ok":
                return {"target": "db", "ok": False, "latency_ms": 5000,
                        "detail": f"connect timeout after 5000ms to {host}:5432"}
            if host != GOOD_DB_HOST:
                return {"target": "db", "ok": False, "latency_ms": 12,
                        "detail": f"connection refused {host}:5432"}
            return {"target": "db", "ok": True, "latency_ms": 3, "detail": f"{host}:5432 reachable"}
        if target == "payment":
            if S["payment_status"] != "ok":
                return {"target": "payment", "ok": False, "latency_ms": 3000,
                        "detail": "timeout after 3000ms to payment-gw"}
            return {"target": "payment", "ok": True, "latency_ms": 41, "detail": "payment-gw reachable"}
        if target == "disk":
            pct = S["disk_used_pct"]
            full = pct >= 100
            return {"target": "disk", "ok": not full, "used_pct": round(pct, 1),
                    "detail": f"{pct:.0f}% used on /var/log; write test "
                              + ("failed: No space left on device" if full else "ok")}
    return JSONResponse({"ok": False, "detail": "target must be db, payment or disk"}, status_code=400)


def _fix_rotate_logs(_body: dict) -> tuple[bool, str, bool]:
    changed = "disk_full" in S["faults"] or S["disk_used_pct"] > 80
    if LOG_PATH.exists():
        archive = LOG_PATH.with_name(LOG_PATH.name + ".1")
        os.replace(LOG_PATH, archive)
    S["faults"].discard("disk_full")
    S["disk_used_pct"] = BASE_DISK
    _log("INFO", f"log rotation completed; archived to {LOG_PATH.name}.1, disk usage now {BASE_DISK:.0f}%")
    return True, "logs rotated and archived; disk usage back to normal", changed


def _fix_restart_worker(_body: dict) -> tuple[bool, str, bool]:
    changed = "pool_leak" in S["faults"] or S["worker_status"] != "running"
    S["faults"].discard("pool_leak")
    S["pool_used"] = 2
    S["worker_status"] = "running"
    _log("INFO", "worker restarted; connection pool drained")
    if changed:
        return True, "worker restarted; connection pool drained", True
    return True, "worker restarted (it was healthy, so nothing changed)", False


def _fix_resize_pool(body: dict) -> tuple[bool, str, bool]:
    old = _pool_max()
    try:
        new = int(body.get("size", old + 10))
    except (TypeError, ValueError):
        new = old + 10
    new = max(5, min(50, new))
    S["config"]["db_pool_size"] = new
    _log("INFO", f"db pool resized {old} -> {new}")
    return True, f"pool size {old} -> {new} (does not release leaked connections)", new != old


def _fix_rollback_config(_body: dict) -> tuple[bool, str, bool]:
    if S["active_version"] <= 3:
        return False, f"no newer config version to roll back; active version is v{S['active_version']}", False
    cur = S["active_version"]
    S["config"]["db_host"] = GOOD_DB_HOST
    S["faults"].discard("bad_config")
    S["active_version"] = 3
    for h in S["history"]:
        if h["version"] == cur:
            h["reverted"] = True
    _log("INFO", f"config rolled back v{cur} -> v3 (db_host={GOOD_DB_HOST})")
    return True, f"config rolled back v{cur} -> v3", True


def _fix_restart_db(_body: dict) -> tuple[bool, str, bool]:
    changed = S["db_status"] != "ok"
    S["faults"].discard("db_hung")
    S["db_status"] = "ok"
    _log("INFO", "database restarted; accepting connections")
    return True, "database restarted; accepting connections", changed


_FIXES = {
    "rotate_logs": _fix_rotate_logs,
    "restart_worker": _fix_restart_worker,
    "resize_pool": _fix_resize_pool,
    "rollback_config": _fix_rollback_config,
    "restart_db": _fix_restart_db,
}


@app.post("/admin/action/{fix}")
def action(fix: str, body: dict | None = Body(default=None)):
    if fix not in _FIXES:
        return JSONResponse({"ok": False, "detail": f"unknown fix '{fix}'"}, status_code=404)
    with _lock:
        ok, detail, changed = _FIXES[fix](body or {})
        if changed:
            _window.clear()  # post-fix metrics must reflect post-fix reality
    return {"ok": ok, "detail": detail}