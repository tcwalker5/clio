"""
routes_calendar.py — Court calendar sync UI.

Fetch (or paste) SD Superior Court calendar text -> parse & store -> compare
against Clio calendar entries. The comparison itself is read-only (matches
calendar-check's "shows what would change" behavior); the one write path is
/update-case-number, fired only by an explicit button click on a mismatched
or missing case-number row.
"""

import os
from datetime import date

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from clio_users import get_staff_directory
from court_calendar.client_list import build_client_list, render_docx, render_html
from court_calendar.clio_calendar import fetch_calendar_entries
from court_calendar.clio_matter_update import update_matter_case_number
from court_calendar.court_fetch import fetch_court_calendar_text
from court_calendar.matcher import compare_events
from court_calendar.matter_fields import (
    MATTER_SYNC_FIELDS,
    index_case_numbers_by_matter_id,
    index_matter_owner_by_matter_id,
)
from court_calendar.normalizer import parse_court_calendar_line
from court_calendar.store import fetch_date_range, fetch_upcoming_events, get_purpose_mappings, upsert_court_events
from matter_matching import fetch_open_matters, index_by_last_name_all
from web.auth import require_auth

router = APIRouter(prefix="/calendar", tags=["calendar"])

DEFAULT_ATTORNEY_LAST_NAME = "Collier"
CLIO_BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {os.getenv('CLIO_ACCESS_TOKEN', '')}",
        "Content-Type": "application/json",
    })
    return s


def _staff_names() -> list[str]:
    return sorted({
        info["name"] for info in get_staff_directory().values()
        if info.get("name") and info.get("is_attorney")
    })


def _default_attorney(names: list[str]) -> str:
    for name in names:
        if name.split()[-1].lower() == DEFAULT_ATTORNEY_LAST_NAME.lower():
            return name
    return names[0] if names else ""


def _render_calendar_page(request: Request, **overrides):
    from web.app import render

    names = _staff_names()
    context = {
        "events": fetch_upcoming_events(),
        "results": None,
        "error": None,
        "calendar_text": "",
        "fetch_stats": None,
        "staff_names": names,
        "selected_attorney": _default_attorney(names),
        "clio_base_url": CLIO_BASE_URL,
    }
    context.update(overrides)
    return render(request, "calendar_sync.html", **context)


@router.get("", response_class=HTMLResponse)
async def calendar_home(request: Request, _: None = Depends(require_auth)):
    return _render_calendar_page(request)


@router.post("/fetch", response_class=HTMLResponse)
async def calendar_fetch(request: Request, attorney: str = Form(...), _: None = Depends(require_auth)):
    error = None
    calendar_text = ""
    fetch_stats = None
    try:
        last_name = attorney.strip().split()[-1]
        calendar_text, fetch_stats = fetch_court_calendar_text(last_name, attorney)
        if fetch_stats["filtered_events"] == 0:
            error = f"No court events found for {attorney} on the court website right now."
    except (RuntimeError, requests.RequestException) as e:
        error = f"Failed to fetch court calendar: {e}"

    return _render_calendar_page(request, error=error, calendar_text=calendar_text,
                                  fetch_stats=fetch_stats, selected_attorney=attorney)


@router.post("/import", response_class=HTMLResponse)
async def calendar_import(request: Request, calendar_text: str = Form(...), _: None = Depends(require_auth)):
    parsed = [parse_court_calendar_line(line) for line in calendar_text.splitlines()]
    parsed = [ce for ce in parsed if ce]
    upsert_court_events(parsed)

    return _render_calendar_page(request, imported_count=len(parsed))


@router.post("/compare", response_class=HTMLResponse)
async def calendar_compare(request: Request, _: None = Depends(require_auth)):
    events = fetch_upcoming_events()
    date_range = fetch_date_range()
    if not date_range:
        return _render_calendar_page(
            request, error="No upcoming court events stored — fetch or paste the court calendar text first.",
        )

    from_date, to_date = date_range
    court_events = [parse_court_calendar_line(row["raw_line"]) for row in events]
    court_events = [ce for ce in court_events if ce]

    session = _session()
    matters = fetch_open_matters(session, fields=MATTER_SYNC_FIELDS)
    matters_by_name = index_by_last_name_all(matters)
    case_numbers_by_matter = index_case_numbers_by_matter_id(matters)
    matter_owner_by_matter = index_matter_owner_by_matter_id(matters)
    purpose_mappings = get_purpose_mappings()
    clio_entries = fetch_calendar_entries(from_date, to_date, session)
    staff_directory = get_staff_directory()

    results = compare_events(court_events, clio_entries, matters_by_name, purpose_mappings,
                              staff_directory, case_numbers_by_matter, matter_owner_by_matter)

    return _render_calendar_page(request, results=results, from_date=from_date, to_date=to_date)


@router.post("/update-case-number")
async def update_case_number(
    matter_id: int = Form(...),
    case_number: str = Form(...),
    _: None = Depends(require_auth),
):
    try:
        update_matter_case_number(_session(), matter_id, case_number)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"success": True})


@router.get("/client-list", response_class=HTMLResponse)
async def client_list_preview(_: None = Depends(require_auth)):
    clients = build_client_list(_session())
    return HTMLResponse(render_html(clients))


@router.get("/client-list/download")
async def client_list_download(_: None = Depends(require_auth)):
    clients = build_client_list(_session())
    buf = render_docx(clients)
    filename = f"client-court-dates-{date.today().isoformat()}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
