"""
clio_calendar.py — Clio /calendar_entries.json client for the court calendar sync.

Fetches calendar entries in a date range and indexes them by matter ID (the
primary match path — see court_calendar/matcher.py) since staff usually link
a Matter when creating a hearing's calendar entry.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

PACIFIC = ZoneInfo("America/Los_Angeles")

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")
CALENDAR_ENTRIES_ENDPOINT = f"{BASE_URL}/api/v4/calendar_entries.json"
CALENDAR_ENTRIES_FIELDS = (
    "id,summary,description,location,start_at,end_at,all_day,"
    "matter{id,display_number},calendar_owner{id,name},attendees{id,name,email,type}"
)
PAGE_SIZE = 200


@dataclass
class CalendarEntry:
    id: int
    summary: str
    description: str
    location: str
    start_at: datetime
    end_at: datetime | None
    all_day: bool
    matter_id: int | None
    matter_display_number: str | None
    calendar_owner_name: str | None
    attendees: list[dict] = field(default_factory=list)  # [{id, name, email, type}]
    raw: dict = field(default_factory=dict)


def _parse_entry(raw: dict) -> CalendarEntry:
    matter = raw.get("matter") or {}
    owner = raw.get("calendar_owner") or {}
    return CalendarEntry(
        id=int(raw["id"]),
        summary=raw.get("summary") or "",
        description=raw.get("description") or "",
        location=raw.get("location") or "",
        start_at=datetime.fromisoformat(raw["start_at"]),
        end_at=datetime.fromisoformat(raw["end_at"]) if raw.get("end_at") else None,
        all_day=bool(raw.get("all_day")),
        matter_id=int(matter["id"]) if matter.get("id") else None,
        matter_display_number=matter.get("display_number"),
        calendar_owner_name=owner.get("name"),
        attendees=raw.get("attendees") or [],
        raw=raw,
    )


def fetch_calendar_entries(
    from_date: str,
    to_date: str,
    session: requests.Session | None = None,
) -> list[CalendarEntry]:
    """
    Fetch all calendar entries with start_at in [from_date, to_date] (YYYY-MM-DD,
    inclusive, Pacific time), across all calendars/owners, handling pagination.
    """
    own_session = session is None
    if own_session:
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
        })

    from_dt = datetime.combine(datetime.strptime(from_date, "%Y-%m-%d").date(), time.min, tzinfo=PACIFIC)
    to_dt = datetime.combine(datetime.strptime(to_date, "%Y-%m-%d").date(), time.max, tzinfo=PACIFIC)

    try:
        entries: list[CalendarEntry] = []
        next_url: str | None = None
        page = 1

        while True:
            if next_url:
                resp = session.get(next_url)
            else:
                params = {
                    "fields": CALENDAR_ENTRIES_FIELDS,
                    "from": from_dt.isoformat(),
                    "to": to_dt.isoformat(),
                    "limit": PAGE_SIZE,
                }
                resp = session.get(CALENDAR_ENTRIES_ENDPOINT, params=params)

            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch calendar entries (page {page}): {resp.status_code} {resp.text[:200]}")

            body = resp.json()
            records = body.get("data", [])
            entries.extend(_parse_entry(r) for r in records)

            next_url = body.get("meta", {}).get("paging", {}).get("next")
            logging.info("Fetched calendar entries page %d — %d records", page, len(records))
            page += 1
            if not next_url:
                break

        return entries
    finally:
        if own_session:
            session.close()


def index_by_matter_id(entries: list[CalendarEntry]) -> dict[int, list[CalendarEntry]]:
    index: dict[int, list[CalendarEntry]] = {}
    for e in entries:
        if e.matter_id:
            index.setdefault(e.matter_id, []).append(e)
    return index
