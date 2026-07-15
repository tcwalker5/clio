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
EVENT_FIELDS = "id,subject,start,end,location,bodyPreview,categories,isAllDay"
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
        raw=raw,
    )


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
