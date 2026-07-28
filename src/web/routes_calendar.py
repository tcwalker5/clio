"""
routes_calendar.py — Court calendar sync UI.

Fetch (or paste) SD Superior Court calendar text -> parse & store -> compare
against Clio calendar entries, all in one step — "Fetch Calendar" pulls from
the court website and immediately compares; pasting text + "Parse & Compare"
does the same for hand-copied text (or to re-run after manually editing the
textarea — editing alone doesn't refresh the comparison, submitting does).
The comparison itself is read-only (matches calendar-check's "shows what
would change" behavior); the one write path is /update-case-number, fired
only by an explicit button click on a mismatched or missing case-number row.
"""

import os
import uuid
from datetime import date

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

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
from web.preview_store import PREVIEWS

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
        "results": None,
        "error": None,
        "calendar_text": "",
        "fetch_stats": None,
        "staff_names": names,
        "selected_attorney": _default_attorney(names),
        "clio_base_url": CLIO_BASE_URL,
        "today": date.today().strftime("%B %d, %Y"),
        "events": fetch_upcoming_events(),  # plain, unfiltered — the "all events" copy/paste table
    }
    context.update(overrides)
    response = render(request, "calendar_sync.html", **context)
    # Prevents the browser's back/forward cache from replaying a previous
    # comparison instantly on page load — without this, navigating back
    # after running a comparison can restore that exact page from memory
    # with no fresh request at all, which looks like (but isn't) an
    # automatic re-fetch/re-compare on load.
    response.headers["Cache-Control"] = "no-store"
    return response


def _flash(context: dict) -> str:
    """Stashes a render-context dict for the *next* GET / only, keyed by a
    one-time token — the Post/Redirect/Get pattern. Without this, the POST
    handlers used to render the results page directly, leaving the browser
    sitting on a POST-only URL (e.g. /calendar/fetch); refreshing there
    necessarily resubmits that POST and reruns everything, which looked like
    (but wasn't) the page auto-fetching/comparing on load. Redirecting to a
    clean GET /calendar means a plain refresh always lands on a fresh page —
    the token is popped (single-use) the moment it's read."""
    token = uuid.uuid4().hex
    PREVIEWS[token] = context
    return token


@router.get("", response_class=HTMLResponse)
async def calendar_home(request: Request, token: str | None = None, _: None = Depends(require_auth)):
    flashed = PREVIEWS.pop(token, {}) if token else {}
    return _render_calendar_page(request, **flashed)


@router.post("/fetch")
async def calendar_fetch(attorney: str = Form(...), _: None = Depends(require_auth)):
    error = None
    calendar_text = ""
    fetch_stats = None
    comparison: dict = {}
    try:
        last_name = attorney.strip().split()[-1]
        calendar_text, fetch_stats = fetch_court_calendar_text(last_name, attorney)
        if fetch_stats["filtered_events"] == 0:
            error = f"No court events found for {attorney} on the court website right now."
        else:
            parsed = [parse_court_calendar_line(line) for line in calendar_text.splitlines()]
            parsed = [ce for ce in parsed if ce]
            upsert_court_events(parsed)
            comparison = _run_comparison()
            if "error" in comparison:
                error = comparison.pop("error")
    except (RuntimeError, requests.RequestException) as e:
        error = f"Failed to fetch court calendar: {e}"

    token = _flash({"error": error, "calendar_text": calendar_text, "fetch_stats": fetch_stats,
                     "selected_attorney": attorney, **comparison})
    return RedirectResponse(url=f"/calendar?token={token}", status_code=303)


def _run_comparison() -> dict:
    """Shared by /import (auto-compare right after parsing/storing) and
    /compare (manual re-run against whatever's already stored, e.g. after
    fixing something in Clio without re-pasting court text). Returns a dict
    of template overrides — either {"error": ...} or {"results", "from_date",
    "to_date"}."""
    events = fetch_upcoming_events()
    date_range = fetch_date_range()
    if not date_range:
        return {"error": "No upcoming court events stored — fetch or paste the court calendar text first."}

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

    results = compare_events(court_events, clio_entries, matters_by_name, purpose_mappings,
                              case_numbers_by_matter, matter_owner_by_matter)
    # Stays in the same date/time order compare_events() produced (from
    # fetch_upcoming_events()'s "ORDER BY datetime ASC") — chronological,
    # not grouped by status, so the list still reads like a real calendar.
    # The "needing attention" checkbox is what isolates exceptions instead.

    return {"results": results, "from_date": from_date, "to_date": to_date}


@router.post("/import")
async def calendar_import(calendar_text: str = Form(...), _: None = Depends(require_auth)):
    parsed = [parse_court_calendar_line(line) for line in calendar_text.splitlines()]
    parsed = [ce for ce in parsed if ce]
    upsert_court_events(parsed)

    token = _flash({"imported_count": len(parsed), **_run_comparison()})
    return RedirectResponse(url=f"/calendar?token={token}", status_code=303)


@router.post("/compare")
async def calendar_compare(_: None = Depends(require_auth)):
    token = _flash(_run_comparison())
    return RedirectResponse(url=f"/calendar?token={token}", status_code=303)


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
