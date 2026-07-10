"""
routes_calendar.py — Court calendar sync UI.

Paste SD Superior Court calendar text -> parse & store -> compare against
Clio calendar entries -> read-only diff (no writes to Clio; matches
calendar-check's "shows what would change" behavior).
"""

import os
from datetime import date

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from court_calendar.client_list import build_client_list, render_docx, render_html
from court_calendar.clio_calendar import fetch_calendar_entries
from court_calendar.matcher import compare_events
from court_calendar.normalizer import parse_court_calendar_line
from court_calendar.store import fetch_date_range, fetch_upcoming_events, get_purpose_mappings, upsert_court_events
from matter_matching import fetch_open_matters, index_by_last_name
from web.auth import require_auth

router = APIRouter(prefix="/calendar", tags=["calendar"])


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {os.getenv('CLIO_ACCESS_TOKEN', '')}",
        "Content-Type": "application/json",
    })
    return s


@router.get("", response_class=HTMLResponse)
async def calendar_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render
    events = fetch_upcoming_events()
    return render(request, "calendar_sync.html", events=events, results=None, orphaned=None, error=None)


@router.post("/import", response_class=HTMLResponse)
async def calendar_import(request: Request, calendar_text: str = Form(...), _: None = Depends(require_auth)):
    from web.app import render

    parsed = [parse_court_calendar_line(line) for line in calendar_text.splitlines()]
    parsed = [ce for ce in parsed if ce]
    upsert_court_events(parsed)

    events = fetch_upcoming_events()
    return render(request, "calendar_sync.html", events=events, results=None, orphaned=None,
                  error=None, imported_count=len(parsed))


@router.post("/compare", response_class=HTMLResponse)
async def calendar_compare(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    events = fetch_upcoming_events()
    date_range = fetch_date_range()
    if not date_range:
        return render(request, "calendar_sync.html", events=events, results=None, orphaned=None,
                      error="No upcoming court events stored — paste the court calendar text first.")

    from_date, to_date = date_range
    court_events = [parse_court_calendar_line(row["raw_line"]) for row in events]
    court_events = [ce for ce in court_events if ce]

    session = _session()
    matters = index_by_last_name(fetch_open_matters(session))
    purpose_mappings = get_purpose_mappings()
    clio_entries = fetch_calendar_entries(from_date, to_date, session)

    results, orphaned = compare_events(court_events, clio_entries, matters, purpose_mappings)

    return render(request, "calendar_sync.html", events=events, results=results,
                  orphaned=orphaned, error=None, from_date=from_date, to_date=to_date)


@router.get("/client-list", response_class=HTMLResponse)
async def client_list_preview(_: None = Depends(require_auth)):
    clients = build_client_list()
    return HTMLResponse(render_html(clients))


@router.get("/client-list/download")
async def client_list_download(_: None = Depends(require_auth)):
    clients = build_client_list()
    buf = render_docx(clients)
    filename = f"client-court-dates-{date.today().isoformat()}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
