"""The tools must surface DIFFERENT evidence for the same 5xx alert (SANDBOX).

Starts its own sandbox on port 8011 with a temp log file, so it never touches a running stack.
Takes about a minute because the pool leak and disk fill ramp up over time.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from backend.tools import run_tool

PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
LOG = Path(tempfile.gettempdir()) / "autoheal_test_shopnode.log"
WAIT = {"bad_config": 4, "pool_leak": 16, "disk_full": 13, "payment_down": 4, "db_hung": 4, "compound": 13}
_cache: dict = {}


@pytest.fixture(scope="module", autouse=True)
def sandbox():
    env = dict(os.environ, LOG_PATH=str(LOG))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sandbox_app.main:app", "--port", str(PORT), "--log-level", "warning"],
        env=env)
    saved = {k: os.environ.get(k) for k in ("SANDBOX_URL", "LOG_PATH")}
    os.environ["SANDBOX_URL"] = BASE
    os.environ["LOG_PATH"] = str(LOG)
    try:
        for _ in range(60):
            try:
                if httpx.get(BASE + "/metrics", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            pytest.fail("sandbox did not start")
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def ev(name: str) -> dict:
    """Inject a fault, wait for evidence to build up, and snapshot what the tools see."""
    if name in _cache:
        return _cache[name]
    httpx.post(f"{BASE}/admin/reset", timeout=5)
    LOG.write_text("")
    httpx.post(f"{BASE}/admin/fault/{name}", timeout=5)
    time.sleep(WAIT[name])
    _cache[name] = {
        "alert": run_tool("get_alert"),
        "metrics": run_tool("get_metrics", {"window_s": 10}),
        "db": run_tool("check_connectivity", {"target": "db"}),
        "disk": run_tool("check_connectivity", {"target": "disk"}),
        "payment": run_tool("check_connectivity", {"target": "payment"}),
        "cfg": run_tool("get_config_diff"),
        "dbs": run_tool("db_status"),
        "logs": "\n".join(run_tool("tail_logs", {"n": 100})["lines"]),
    }
    return _cache[name]


def test_unknown_tool_and_bad_args():
    assert "error" in run_tool("does_not_exist")
    assert "error" in run_tool("tail_logs", {"bogus": 1})
    assert "error" in run_tool("check_connectivity", {"target": "mars"})
    assert "error" in run_tool("tail_logs", {"pattern": "("})


def test_alert_does_not_reveal_cause():
    a = ev("bad_config")["alert"]
    assert a["title"] == "5xx on /checkout"
    assert "config" not in str(a).lower()


def test_bad_config():
    e = ev("bad_config")
    assert "connection refused db-prod-02" in e["logs"]
    assert e["cfg"]["latest_change"]["version"] == 4 and e["cfg"]["latest_change"]["is_active"]
    assert e["db"]["ok"] is False and "refused" in e["db"]["detail"]
    assert e["disk"]["ok"] and e["payment"]["ok"]


def test_pool_leak():
    e = ev("pool_leak")
    assert e["metrics"]["db_pool_used"] >= e["metrics"]["db_pool_max"]
    assert "QueuePool limit" in e["logs"]
    assert e["db"]["ok"] and e["cfg"]["active_version"] == 3


def test_disk_full():
    e = ev("disk_full")
    assert "No space left on device" in e["logs"]
    assert e["disk"]["ok"] is False
    assert e["db"]["ok"] and e["payment"]["ok"]


def test_payment_down():
    e = ev("payment_down")
    assert "upstream timeout payment-gw" in e["logs"]
    assert e["payment"]["ok"] is False
    assert e["db"]["ok"] and e["disk"]["ok"]


def test_db_hung():
    e = ev("db_hung")
    assert e["dbs"]["status"] == "unresponsive"
    assert "not responding" in e["logs"]
    assert e["db"]["ok"] is False and "refused" not in e["db"]["detail"]


def test_compound_shows_both_causes():
    e = ev("compound")
    assert "connection refused db-prod-02" in e["logs"]
    assert "No space left on device" in e["logs"]
    assert e["cfg"]["latest_change"]["is_active"] and e["disk"]["ok"] is False


def test_same_alert_different_evidence():
    sigs = {}
    for name in ("bad_config", "pool_leak", "disk_full", "payment_down", "db_hung"):
        e = ev(name)
        assert e["alert"]["title"] == "5xx on /checkout"
        assert e["alert"]["status"] == "firing", name
        m = e["metrics"]
        sigs[name] = (e["db"]["ok"], e["disk"]["ok"], e["payment"]["ok"], e["cfg"]["active_version"],
                      m["db_pool_used"] >= m["db_pool_max"], m["db_status"])
    assert len(set(sigs.values())) == 5, f"faults are not distinguishable: {sigs}"