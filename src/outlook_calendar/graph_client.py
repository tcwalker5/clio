"""
graph_client.py — Microsoft Graph client for reading Heidi Collier's Outlook
calendar (one-time backfill, see outlook_migration.py). Mirrors
court_calendar/clio_calendar.py's structure for Graph instead of Clio's API.

Read-only: this module has no write/create/update functions on purpose —
nothing in this project writes back to Outlook.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

PACIFIC = ZoneInfo("America/Los_Angeles")

ACCESS_TOKEN = os.getenv("MICROSOFT_ACCESS_TOKEN", "")
CALENDAR_OWNER_EMAIL = os.getenv("MICROSOFT_CALENDAR_OWNER_EMAIL", "")
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
EVENT_FIELDS = "id,subject,start,end,location,bodyPreview,categories,isAllDay,seriesMasterId"
PAGE_SIZE = 100


@dataclass
class OutlookEvent:
    id: str
    subject: str
    start: datetime
    end: datetime
    location: str
    body_preview: str
    categories: list[str] = field(default_factory=list)
    is_all_day: bool = False
    series_master_id: str | None = None
    raw: dict = field(default_factory=dict)


def _parse_graph_datetime(value: str) -> datetime:
    """
    Graph can return up to 7 fractional-second digits; datetime.fromisoformat
    only accepts up to 6. We requested the Prefer: outlook.timezone header
    (Pacific), so these come back as naive local wall-clock times — attach
    that tzinfo directly rather than trying to map Graph's Windows-style zone
    name to an IANA one.
    """
    if "." in value:
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value).replace(tzinfo=PACIFIC)


def _parse_event(raw: dict) -> OutlookEvent:
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    return OutlookEvent(
        id=raw.get("id", ""),
        subject=raw.get("subject") or "",
        start=_parse_graph_datetime(start["dateTime"]) if start.get("dateTime") else None,
        end=_parse_graph_datetime(end["dateTime"]) if end.get("dateTime") else None,
        location=(raw.get("location") or {}).get("displayName", ""),
        body_preview=raw.get("bodyPreview") or "",
        categories=raw.get("categories") or [],
        is_all_day=bool(raw.get("isAllDay")),
        series_master_id=raw.get("seriesMasterId"),
        raw=raw,
    )


@dataclass
class RecurringSeries:
    series_master_id: str
    subject: str
    start: datetime
    end: datetime
    location: str
    body_preview: str
    categories: list[str]
    is_all_day: bool
    pattern: dict  # Graph recurrence.pattern — see recurrence.py for translation


def fetch_recurring_series(from_date: str, to_date: str, email: str | None = None) -> list[RecurringSeries]:
    """
    Every distinct recurring series with at least one occurrence in
    [from_date, to_date] — one entry per series (not per occurrence).
    calendarview already expands occurrences (that's what fetch_calendar_events
    uses), so this scans it just to collect distinct seriesMasterId values,
    then fetches each master individually for its `recurrence` pattern —
    calendarview itself doesn't return `recurrence` on expanded instances.
    """
    email = email or CALENDAR_OWNER_EMAIL
    if not email:
        raise RuntimeError("MICROSOFT_CALENDAR_OWNER_EMAIL not set in .env")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Prefer": 'outlook.timezone="Pacific Standard Time"',
    })

    view_url = f"{GRAPH_BASE}/users/{email}/calendarview"
    master_ids: dict[str, dict] = {}  # seriesMasterId -> first-seen occurrence (for categories)
    next_url: str | None = None

    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(view_url, params={
                "startDateTime": f"{from_date}T00:00:00",
                "endDateTime": f"{to_date}T23:59:59",
                "$select": "id,subject,seriesMasterId,categories",
                "$top": PAGE_SIZE,
            })
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch calendar view for series scan: {resp.status_code} {resp.text[:300]}")

        body = resp.json()
        for r in body.get("value", []):
            sid = r.get("seriesMasterId")
            if sid and sid not in master_ids:
                master_ids[sid] = r
        next_url = body.get("@odata.nextLink")
        if not next_url:
            break

    series: list[RecurringSeries] = []
    for sid in master_ids:
        resp = session.get(f"{GRAPH_BASE}/users/{email}/events/{sid}", params={
            "$select": "subject,start,end,location,bodyPreview,categories,isAllDay,recurrence",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch series master {sid}: {resp.status_code} {resp.text[:300]}")
        raw = resp.json()
        recurrence = raw.get("recurrence") or {}
        pattern = recurrence.get("pattern")
        if not pattern:
            continue  # shouldn't happen for a real seriesMasterId, but don't crash the run over one bad record
        start = raw.get("start") or {}
        end = raw.get("end") or {}
        series.append(RecurringSeries(
            series_master_id=sid,
            subject=raw.get("subject") or "",
            start=_parse_graph_datetime(start["dateTime"]) if start.get("dateTime") else None,
            end=_parse_graph_datetime(end["dateTime"]) if end.get("dateTime") else None,
            location=(raw.get("location") or {}).get("displayName", ""),
            body_preview=raw.get("bodyPreview") or "",
            categories=raw.get("categories") or [],
            is_all_day=bool(raw.get("isAllDay")),
            pattern=pattern,
        ))

    return series


def fetch_calendar_events(from_date: str, to_date: str, email: str | None = None) -> list[OutlookEvent]:
    """
    Fetch all events for `email` (defaults to MICROSOFT_CALENDAR_OWNER_EMAIL)
    with start in [from_date, to_date] (YYYY-MM-DD), handling pagination via
    @odata.nextLink (a full URL, like Clio's meta.paging.next — fetch as-is).
    """
    email = email or CALENDAR_OWNER_EMAIL
    if not email:
        raise RuntimeError("MICROSOFT_CALENDAR_OWNER_EMAIL not set in .env")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Prefer": 'outlook.timezone="Pacific Standard Time"',
    })

    url = f"{GRAPH_BASE}/users/{email}/calendarview"
    events: list[OutlookEvent] = []
    next_url: str | None = None
    page = 1

    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(url, params={
                "startDateTime": f"{from_date}T00:00:00",
                "endDateTime": f"{to_date}T23:59:59",
                "$select": EVENT_FIELDS,
                "$orderby": "start/dateTime",
                "$top": PAGE_SIZE,
            })

        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch calendar events (page {page}): {resp.status_code} {resp.text[:300]}")

        body = resp.json()
        records = body.get("value", [])
        events.extend(_parse_event(r) for r in records)

        next_url = body.get("@odata.nextLink")
        page += 1
        if not next_url:
            break

    return events
