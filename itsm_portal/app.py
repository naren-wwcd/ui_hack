"""HTML-only mock legacy ITSM portal.

This portal deliberately has no JSON API. The RPA bot must use its browser
forms. Its SQLite database is separate from the AutoHeal backend database.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("ITSM_DB_PATH", str(ROOT / "data" / "itsm_portal.db")))
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
from dotenv import load_dotenv

load_dotenv()
PORTAL_USER = os.getenv("PORTAL_USER", "autoheal_bot")
PORTAL_PASS = os.getenv("PORTAL_PASS", "sandbox-demo-password")
SESSION_SECRET = os.getenv("PORTAL_SESSION_SECRET", "local-autoheal-sandbox-session-key")

app = FastAPI(title="ShopNode ITSM SANDBOX Portal")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="autoheal_itsm_session",
    same_site="lax",
    https_only=False,
)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

_orig_template_response = templates.TemplateResponse


def _template_response_compat(*args, **kwargs):
    """Accept both the old TemplateResponse(name, context) and the new (request, name, context)."""
    if args and isinstance(args[0], str):
        name = args[0]
        context = args[1] if len(args) > 1 else kwargs.pop("context", {})
        return _orig_template_response(context["request"], name, context, *args[2:], **kwargs)
    return _orig_template_response(*args, **kwargs)


templates.TemplateResponse = _template_response_compat
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    if str(DB_PATH) != ":memory:":
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_portal_db() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS portal_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_incident_id INTEGER,
                short_description TEXT NOT NULL,
                category TEXT NOT NULL,
                priority TEXT NOT NULL,
                diagnosis TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                work_notes TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'New',
                resolution_code TEXT,
                resolution_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_portal_source_incident
                ON portal_incidents(source_incident_id)
                WHERE source_incident_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS change_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portal_incident_id INTEGER,
                short_description TEXT NOT NULL,
                justification TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(portal_incident_id)
                    REFERENCES portal_incidents(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS pager_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portal_incident_id INTEGER,
                change_request_id INTEGER,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(portal_incident_id)
                    REFERENCES portal_incidents(id) ON DELETE SET NULL,
                FOREIGN KEY(change_request_id)
                    REFERENCES change_requests(id) ON DELETE SET NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _write(sql: str, params: tuple[Any, ...] = ()) -> int:
    conn = _connect()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def _authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.on_event("startup")
async def startup() -> None:
    init_portal_db()


@app.get("/", response_class=HTMLResponse, response_model=None)
async def home(request: Request) -> RedirectResponse:
    return RedirectResponse("/incidents" if _authenticated(request) else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None, "sandbox": True},
    )


@app.post("/login", response_class=HTMLResponse, response_model=None)
async def login(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if username == PORTAL_USER and password == PORTAL_PASS:
        request.session.clear()
        request.session["authenticated"] = True
        request.session["username"] = username
        return RedirectResponse("/incidents", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Invalid portal credentials.",
            "sandbox": True,
        },
        status_code=401,
    )


@app.get("/logout", response_model=None)
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/incidents", response_class=HTMLResponse, response_model=None)
async def incident_list(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    rows = _all("SELECT * FROM portal_incidents ORDER BY id DESC")
    return templates.TemplateResponse(
        "incidents.html",
        {
            "request": request,
            "incidents": rows,
            "username": request.session.get("username"),
            "sandbox": True,
        },
    )


@app.get("/incident/new", response_class=HTMLResponse, response_model=None)
async def incident_new_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    return templates.TemplateResponse(
        "incident_new.html",
        {"request": request, "sandbox": True},
    )


@app.post("/incident/new", response_model=None)
async def incident_new(request: Request) -> RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    form = await request.form()
    now = _now()
    source_value = str(form.get("source_incident_id", "")).strip()
    source_id: int | None = None
    if source_value:
        try:
            source_id = int(source_value)
        except ValueError:
            source_id = None

    short_description = str(form.get("short_description", "")).strip()
    if not short_description:
        short_description = "SANDBOX incident"

    portal_id = _write(
        """
        INSERT INTO portal_incidents (
            source_incident_id, short_description, category, priority,
            diagnosis, evidence, work_notes, state, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'New', ?, ?)
        """,
        (
            source_id,
            short_description,
            str(form.get("category", "Application")),
            str(form.get("priority", "3 - Moderate")),
            str(form.get("diagnosis", "")),
            str(form.get("evidence", "")),
            str(form.get("work_notes", "")),
            now,
            now,
        ),
    )
    return RedirectResponse(f"/incident/{portal_id}/update", status_code=303)


@app.get("/incident/{incident_id}/update", response_class=HTMLResponse, response_model=None)
async def incident_update_page(
    request: Request,
    incident_id: int,
) -> HTMLResponse | RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    incident = _one("SELECT * FROM portal_incidents WHERE id = ?", (incident_id,))
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)
    return templates.TemplateResponse(
        "incident_update.html",
        {"request": request, "incident": incident, "sandbox": True},
    )


@app.post("/incident/{incident_id}/update", response_model=None)
async def incident_update(request: Request, incident_id: int) -> RedirectResponse | HTMLResponse:
    if not _authenticated(request):
        return _login_redirect()
    incident = _one("SELECT id FROM portal_incidents WHERE id = ?", (incident_id,))
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)

    form = await request.form()
    new_note = str(form.get("work_notes", "")).strip()
    state = str(form.get("state", "In Progress"))
    current = _one("SELECT work_notes FROM portal_incidents WHERE id = ?", (incident_id,))
    prior_notes = str((current or {}).get("work_notes", ""))
    notes = "\n".join(part for part in (prior_notes, new_note) if part)
    _write(
        """
        UPDATE portal_incidents
        SET work_notes = ?, state = ?, updated_at = ?
        WHERE id = ?
        """,
        (notes, state, _now(), incident_id),
    )
    return RedirectResponse(f"/incident/{incident_id}/update", status_code=303)


@app.get("/incident/{incident_id}/resolve", response_class=HTMLResponse, response_model=None)
async def incident_resolve_page(
    request: Request,
    incident_id: int,
) -> HTMLResponse | RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    incident = _one("SELECT * FROM portal_incidents WHERE id = ?", (incident_id,))
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)
    return templates.TemplateResponse(
        "incident_resolve.html",
        {"request": request, "incident": incident, "sandbox": True},
    )


@app.post("/incident/{incident_id}/resolve", response_model=None)
async def incident_resolve(request: Request, incident_id: int) -> RedirectResponse | HTMLResponse:
    if not _authenticated(request):
        return _login_redirect()
    incident = _one("SELECT id FROM portal_incidents WHERE id = ?", (incident_id,))
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)
    form = await request.form()
    _write(
        """
        UPDATE portal_incidents
        SET state = 'Closed', resolution_code = ?, resolution_notes = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            str(form.get("resolution_code", "Solved (Work Around)")),
            str(form.get("resolution_notes", "")),
            _now(),
            incident_id,
        ),
    )
    return RedirectResponse("/incidents", status_code=303)


@app.get("/change/new", response_class=HTMLResponse, response_model=None)
async def change_new_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    incidents = _all("SELECT id, short_description FROM portal_incidents ORDER BY id DESC")
    return templates.TemplateResponse(
        "change_new.html",
        {"request": request, "incidents": incidents, "sandbox": True},
    )


@app.post("/change/new", response_model=None)
async def change_new(request: Request) -> RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    form = await request.form()
    portal_incident_id: int | None
    raw_id = str(form.get("portal_incident_id", "")).strip()
    try:
        portal_incident_id = int(raw_id) if raw_id else None
    except ValueError:
        portal_incident_id = None

    short_description = str(form.get("short_description", "")).strip() or "SANDBOX change request"
    justification = str(form.get("justification", "")).strip()
    requested_by = str(request.session.get("username", PORTAL_USER))
    now = _now()
    change_id = _write(
        """
        INSERT INTO change_requests (
            portal_incident_id, short_description, justification, requested_by, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (portal_incident_id, short_description, justification, requested_by, now),
    )
    _write(
        """
        INSERT INTO pager_entries
            (portal_incident_id, change_request_id, message, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            portal_incident_id,
            change_id,
            f"ON-CALL SANDBOX PAGE: {short_description}. {justification}",
            now,
        ),
    )
    if portal_incident_id is not None:
        _write(
            "UPDATE portal_incidents SET state = 'Escalated', updated_at = ? WHERE id = ?",
            (now, portal_incident_id),
        )
    return RedirectResponse("/pager", status_code=303)


@app.get("/pager", response_class=HTMLResponse, response_model=None)
async def pager(request: Request) -> HTMLResponse | RedirectResponse:
    if not _authenticated(request):
        return _login_redirect()
    entries = _all(
        """
        SELECT p.*, i.short_description AS incident_description,
               c.short_description AS change_description
        FROM pager_entries p
        LEFT JOIN portal_incidents i ON i.id = p.portal_incident_id
        LEFT JOIN change_requests c ON c.id = p.change_request_id
        ORDER BY p.id DESC
        """
    )
    return templates.TemplateResponse(
        "pager.html",
        {"request": request, "entries": entries, "sandbox": True},
    )