"""Playwright UI-only bot for the legacy ITSM SANDBOX portal."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORTAL_URL = os.getenv("PORTAL_URL", "http://localhost:8002").rstrip("/")
PORTAL_USER = os.getenv("PORTAL_USER", "autoheal_bot")
PORTAL_PASS = os.getenv("PORTAL_PASS", "sandbox-demo-password")
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", str(ROOT / "screenshots")))
SLOW_MO_MS = int(os.getenv("RPA_SLOW_MO_MS", "400"))
HEADLESS = os.getenv("RPA_HEADLESS", "false").lower() in {"1", "true", "yes"}


def _payload(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("payload_json", task.get("payload", {}))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _priority(severity: str) -> str:
    value = severity.strip().lower()
    if value in {"critical", "sev1", "p1"}:
        return "1 - Critical"
    if value in {"high", "sev2", "p2"}:
        return "2 - High"
    if value in {"low", "sev4", "p4"}:
        return "4 - Low"
    return "3 - Moderate"


def _screenshot(page: Page, task_id: Any, label: str) -> str:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or "step"
    filename = f"rpa_{task_id}_{safe_label}_{int(time.time() * 1000)}.png"
    path = SCREENSHOT_DIR / filename
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def _login(page: Page) -> None:
    page.goto(f"{PORTAL_URL}/login", wait_until="domcontentloaded")
    page.locator("#username").fill(PORTAL_USER)
    page.locator("#password").fill(PORTAL_PASS)
    page.locator("#submit").click()
    page.wait_for_url(re.compile(r".*/incidents(?:\?.*)?$"), timeout=15000)


def _source_incident_id(task: dict[str, Any], payload: dict[str, Any]) -> str:
    value = payload.get("source_incident_id", task.get("incident_id", ""))
    return str(value) if value is not None else ""


def _find_portal_incident(page: Page, source_id: str, payload: dict[str, Any]) -> str:
    explicit = payload.get("portal_incident_id")
    if explicit is not None:
        return str(explicit)

    page.goto(f"{PORTAL_URL}/incidents", wait_until="domcontentloaded")
    row = page.locator(f'tr[data-source-id="{source_id}"]')
    if row.count() == 0:
        # A task can be a manual test with no backend source mapping.
        raise RuntimeError(f"Portal incident for backend incident {source_id} was not found")
    link = row.locator("a.update-link")
    href = link.get_attribute("href")
    match = re.search(r"/incident/(\d+)/update", href or "")
    if not match:
        raise RuntimeError("Could not determine portal incident id from the incident list")
    return match.group(1)


def _create_incident(page: Page, task: dict[str, Any], payload: dict[str, Any]) -> str:
    page.goto(f"{PORTAL_URL}/incident/new", wait_until="domcontentloaded")
    severity = str(payload.get("severity", "medium"))
    page.locator("#short_description").fill(
        str(payload.get("short_description", payload.get("title", "SANDBOX 5xx on /checkout")))
    )
    source_id = _source_incident_id(task, payload)
    if source_id:
        page.locator("#source_incident_id").fill(source_id)

    category = str(payload.get("category", "Application"))
    page.locator("#category").select_option(label=category)
    page.locator("#priority").select_option(label=_priority(severity))
    page.locator("#diagnosis").fill(str(payload.get("diagnosis", "SANDBOX investigation started.")))
    evidence = payload.get("evidence", "")
    if isinstance(evidence, list):
        evidence = "\n".join(str(line) for line in evidence)
    page.locator("#evidence").fill(str(evidence))
    page.locator("#work_notes").fill(
        str(payload.get("work_notes", "AutoHeal SANDBOX bot created the incident."))
    )
    page.locator("#submit").click()
    page.wait_for_url(re.compile(r".*/incident/\d+/update$"), timeout=15000)
    match = re.search(r"/incident/(\d+)/update", page.url)
    if not match:
        raise RuntimeError("Portal did not redirect to the created incident")
    _screenshot(page, task.get("id", "manual"), "incident_created")
    return match.group(1)


def _add_note(page: Page, task: dict[str, Any], payload: dict[str, Any]) -> None:
    portal_id = _find_portal_incident(
        page,
        _source_incident_id(task, payload),
        payload,
    )
    page.goto(f"{PORTAL_URL}/incident/{portal_id}/update", wait_until="domcontentloaded")
    page.locator("#work_notes").fill(
        str(payload.get("work_notes", payload.get("note", "AutoHeal SANDBOX stage update.")))
    )
    state = payload.get("state")
    if state:
        page.locator("#state").select_option(label=str(state))
    page.locator("#submit").click()
    page.wait_for_load_state("domcontentloaded")
    _screenshot(page, task.get("id", "manual"), "work_note")


def _resolve(page: Page, task: dict[str, Any], payload: dict[str, Any]) -> None:
    portal_id = _find_portal_incident(
        page,
        _source_incident_id(task, payload),
        payload,
    )
    page.goto(f"{PORTAL_URL}/incident/{portal_id}/resolve", wait_until="domcontentloaded")
    page.locator("#resolution_code").select_option(
        label=str(payload.get("resolution_code", "Solved (Permanently)"))
    )
    page.locator("#resolution_notes").fill(
        str(payload.get("resolution_notes", "SANDBOX health checks passed; incident closed."))
    )
    page.locator("#submit").click()
    page.wait_for_url(re.compile(r".*/incidents$"), timeout=15000)
    _screenshot(page, task.get("id", "manual"), "incident_closed")


def _escalate(page: Page, task: dict[str, Any], payload: dict[str, Any]) -> None:
    portal_id = _find_portal_incident(
        page,
        _source_incident_id(task, payload),
        payload,
    )
    page.goto(f"{PORTAL_URL}/incident/{portal_id}/update", wait_until="domcontentloaded")
    page.locator("#work_notes").fill(
        str(payload.get("work_notes", "AutoHeal SANDBOX escalation sent to on-call."))
    )
    page.locator("#state").select_option(label="Escalated")
    page.locator("#submit").click()
    page.wait_for_load_state("domcontentloaded")
    _screenshot(page, task.get("id", "manual"), "incident_escalated")

    page.goto(f"{PORTAL_URL}/change/new", wait_until="domcontentloaded")
    page.locator("#portal_incident_id").select_option(value=portal_id)
    page.locator("#short_description").fill(
        str(payload.get("short_description", "SANDBOX remediation requires human review"))
    )
    justification = payload.get("justification") or payload.get("diagnosis") or payload.get("reason")
    if isinstance(justification, list):
        justification = "\n".join(str(value) for value in justification)
    page.locator("#justification").fill(str(justification or "Please review the sandbox page and evidence."))
    page.locator("#submit").click()
    page.wait_for_url(re.compile(r".*/pager$"), timeout=15000)
    _screenshot(page, task.get("id", "manual"), "on_call_paged")


def process_task(task: dict[str, Any]) -> dict[str, Any]:
    """Run one queued portal task through visible browser forms only."""
    task_payload = _payload(task)
    kind = str(task.get("kind", ""))
    screenshots: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge",
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            _login(page)
            screenshots.append(_screenshot(page, task.get("id", "manual"), "logged_in"))

            if kind == "create_incident":
                _create_incident(page, task, task_payload)
            elif kind == "add_note":
                _add_note(page, task, task_payload)
            elif kind == "resolve":
                _resolve(page, task, task_payload)
            elif kind == "escalate":
                _escalate(page, task, task_payload)
            else:
                raise ValueError(f"Unsupported RPA task kind: {kind}")

            # Capture a final audit frame after each task, in addition to any
            # stage-specific screenshots.
            screenshots.append(_screenshot(page, task.get("id", "manual"), kind))
            return {
                "ok": True,
                "detail": f"Portal task {kind} completed through the UI.",
                "screenshot_path": screenshots[-1],
                "screenshots": screenshots,
            }
        except (PlaywrightTimeoutError, Exception) as exc:
            try:
                screenshots.append(_screenshot(page, task.get("id", "manual"), "failure"))
            except Exception:
                pass
            return {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "screenshot_path": screenshots[-1] if screenshots else None,
                "screenshots": screenshots,
            }
        finally:
            context.close()
            browser.close()
