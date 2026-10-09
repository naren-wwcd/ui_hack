"""Read-only investigation tools for the AutoHeal agent (SANDBOX).

Every tool returns compact JSON-safe dicts. run_tool() never raises.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional
    pass

log = logging.getLogger("autoheal.tools")

MAX_LINE_CHARS = 300
MAX_TOTAL_CHARS = 4000
SCAN_LINES = 5000
CALL_LOG: deque = deque(maxlen=500)  # in-memory audit of tool calls


def _base() -> str:
    return os.environ.get("SANDBOX_URL", "http://localhost:8001").rstrip("/")


def _log_path() -> Path:
    return Path(os.environ.get("LOG_PATH", "logs/shopnode.log"))


def _threshold() -> float:
    try:
        return float(os.environ.get("ERROR_RATE_THRESHOLD", "0.05"))
    except ValueError:
        return 0.05


def _get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(_base() + path, params=params, timeout=4.0)
    r.raise_for_status()
    return r.json()


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ tools
def get_alert() -> dict:
    m = _get("/metrics", {"window_s": 10})
    er = m.get("error_rate", 0.0)
    return {
        "title": "5xx on /checkout",
        "endpoint": "/checkout",
        "status": "firing" if er > _threshold() else "ok",
        "severity": "critical" if er >= 0.5 else "warning" if er > _threshold() else "info",
        "error_rate": er,
        "p95_ms": m.get("p95_ms"),
        "window_s": 10,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def tail_logs(service: str = "shopnode", pattern: str | None = None, n: int = 50) -> dict:
    if service != "shopnode":
        return {"error": f"unknown service '{service}'", "available_services": ["shopnode"]}
    n = _clamp(n, 1, 100, 50)
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return {"error": f"invalid regex: {exc}"}
    path = _log_path()
    if not path.exists():
        return {"service": service, "pattern": pattern, "count": 0, "lines": []}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = deque(fh, maxlen=SCAN_LINES)
    lines = [ln.rstrip("\n")[:MAX_LINE_CHARS] for ln in raw]
    if rx:
        lines = [ln for ln in lines if rx.search(ln)]
    lines = lines[-n:]
    total = sum(len(x) for x in lines)
    while lines and total > MAX_TOTAL_CHARS:
        total -= len(lines.pop(0))
    return {"service": service, "pattern": pattern, "count": len(lines), "lines": lines}


def get_metrics(window_s: int = 10) -> dict:
    window_s = _clamp(window_s, 1, 120, 10)
    m = _get("/metrics", {"window_s": window_s})
    m["window_s"] = window_s
    return m


def get_config_diff() -> dict:
    cfg = _get("/admin/config")
    hist = cfg.get("history", [])
    if not hist:
        return {"active_version": cfg.get("active_version"), "latest_change": None}
    latest = hist[-1]
    return {
        "active_version": cfg.get("active_version"),
        "latest_change": {
            "version": latest["version"],
            "author": latest["author"],
            "time": latest["time"],
            "summary": latest.get("summary"),
            "diff": latest["diff"],
            "reverted": bool(latest.get("reverted")),
            "is_active": latest["version"] == cfg.get("active_version"),
        },
    }


def get_config_history() -> dict:
    cfg = _get("/admin/config")
    return {
        "active_version": cfg.get("active_version"),
        "versions": [
            {
                "version": h["version"],
                "author": h["author"],
                "time": h["time"],
                "summary": h.get("summary"),
                "reverted": bool(h.get("reverted")),
            }
            for h in cfg.get("history", [])
        ],
    }


def check_connectivity(target: str) -> dict:
    if target not in ("db", "payment", "disk"):
        raise ValueError("target must be one of: db, payment, disk")
    return _get(f"/admin/connectivity/{target}")


def db_status() -> dict:
    m = _get("/metrics", {"window_s": 5})
    conn = _get("/admin/connectivity/db")
    return {
        "status": m.get("db_status"),
        "pool_used": m.get("db_pool_used"),
        "pool_max": m.get("db_pool_max"),
        "connect_ok": conn.get("ok"),
        "connect_latency_ms": conn.get("latency_ms"),
        "detail": conn.get("detail"),
    }


# --------------------------------------------------------------- registry
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_alert": {
        "description": "Current alert summary (title, error rate, p95). Does not reveal the cause.",
        "args_schema": {"type": "object", "properties": {}, "required": []},
    },
    "tail_logs": {
        "description": "Last n log lines of a service, optionally filtered by a case-insensitive regex.",
        "args_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "default": "shopnode"},
                "pattern": {"type": "string", "description": "optional regex"},
                "n": {"type": "integer", "default": 50, "maximum": 100},
            },
            "required": [],
        },
    },
    "get_metrics": {
        "description": "error_rate, p95_ms, db_pool_used/max, disk_used_pct, worker_status, db_status over a window.",
        "args_schema": {
            "type": "object",
            "properties": {"window_s": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
    "get_config_diff": {
        "description": "The most recent configuration change and its diff.",
        "args_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_config_history": {
        "description": "List of configuration versions with author and time.",
        "args_schema": {"type": "object", "properties": {}, "required": []},
    },
    "check_connectivity": {
        "description": "Test connectivity from the app to a dependency.",
        "args_schema": {
            "type": "object",
            "properties": {"target": {"type": "string", "enum": ["db", "payment", "disk"]}},
            "required": ["target"],
        },
    },
    "db_status": {
        "description": "Database status, pool usage and connect latency.",
        "args_schema": {"type": "object", "properties": {}, "required": []},
    },
}

_TOOLS: dict[str, Callable[..., dict]] = {
    "get_alert": get_alert,
    "tail_logs": tail_logs,
    "get_metrics": get_metrics,
    "get_config_diff": get_config_diff,
    "get_config_history": get_config_history,
    "check_connectivity": check_connectivity,
    "db_status": db_status,
}


def run_tool(name: str, args: dict | None = None) -> dict:
    """Run a tool by name. Never raises; returns {"error": ...} on failure."""
    args = {} if args is None else args
    started = time.time()
    try:
        if name not in _TOOLS:
            result: dict = {"error": f"unknown tool '{name}'", "available_tools": sorted(_TOOLS)}
        elif not isinstance(args, dict):
            result = {"error": "args must be a JSON object"}
        else:
            allowed = set(TOOL_REGISTRY[name]["args_schema"]["properties"])
            extra = sorted(set(args) - allowed)
            if extra:
                result = {"error": f"unexpected args {extra} for {name}", "allowed_args": sorted(allowed)}
            else:
                result = _TOOLS[name](**args)
    except httpx.HTTPError as exc:
        result = {"error": f"sandbox unreachable: {exc.__class__.__name__}: {exc}"}
    except (ValueError, TypeError) as exc:
        result = {"error": f"bad arguments: {exc}"}
    except Exception as exc:  # last-resort guard so the agent loop never crashes
        result = {"error": f"{exc.__class__.__name__}: {exc}"}
    CALL_LOG.append(
        {"tool": name, "args": args, "ms": int((time.time() - started) * 1000), "error": "error" in result}
    )
    log.info("tool=%s args=%s error=%s", name, args, "error" in result)
    return result