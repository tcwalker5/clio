"""
clio_write.py — Shared helpers for the scripts that write directly to
Clio's API rather than going through the CSV-import path (outlook_
recurring_availability.py and outlook_exceptions_availability.py). Both
exist because Clio's CSV "Calendar events" import template can't target a
specific calendar (no calendar_owner column) or set recurrence — this
project's normal CSV-generate-then-human-imports pattern can't do either, so
these write via the API instead, with the project's standard live-write
safety rules: --dry-run, log every request, retry on 429, continue on
individual failures.
"""

import logging
import os
import time as time_module
from datetime import datetime

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
CALENDAR_ENTRIES_ENDPOINT = f"{BASE_URL}/api/v4/calendar_entries.json"
RETRY_DELAYS = [5, 15, 30]  # seconds, matches clio_calendar.py's pattern on 429

# Heidi's Clio *Calendar* id for calendar_owner — NOT her USER_ID_HEIDI env
# var. Those are different Clio resources (see CLAUDE.md's Subproject 4
# notes: calendar_owner/attendees are Calendar/Attendee records, no shared ID
# with /users.json). Found by inspecting an existing Heidi-owned calendar
# entry's calendar_owner.id.
HEIDI_CALENDAR_ID = 8860113

# calendar_entry_event_type id for "Heidi" (see CLAUDE.md, Subproject 5) —
# set directly at creation here instead of the CSV-import path's separate
# outlook_migration_tag.py follow-up step, since these write via the API.
HEIDI_EVENT_TYPE_ID = 591618


def clio_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {os.getenv('CLIO_ACCESS_TOKEN', '')}",
        "Content-Type": "application/json",
    })
    return s


def create_calendar_entry(
    session: requests.Session,
    *,
    summary: str,
    start_at: datetime,
    end_at: datetime,
    all_day: bool,
    location: str,
    description: str,
    recurrence_rule: str | None = None,
) -> dict:
    """POST a CalendarEntry onto Heidi's personal calendar, retrying on 429.
    recurrence_rule is omitted entirely (not sent as null/empty) for a
    one-off entry — outlook_exceptions_availability.py's use case."""
    data = {
        "summary": summary,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "all_day": all_day,
        "location": location,
        "description": description,
        "calendar_owner": {"id": HEIDI_CALENDAR_ID},
        "calendar_entry_event_type": {"id": HEIDI_EVENT_TYPE_ID},
    }
    if recurrence_rule:
        data["recurrence_rule"] = recurrence_rule
    body = {"data": data}

    for attempt, delay in enumerate([0, *RETRY_DELAYS], start=1):
        if delay:
            logging.warning("Rate limited creating '%s' — waiting %ds (attempt %d)", summary, delay, attempt)
            time_module.sleep(delay)
        resp = session.post(CALENDAR_ENTRIES_ENDPOINT, json=body, params={"fields": "id,summary,recurrence_rule,start_at"})
        logging.info("POST calendar_entries.json '%s' -> %d", summary, resp.status_code)
        if resp.status_code == 201:
            return resp.json()
        if resp.status_code == 429:
            continue
        raise RuntimeError(f"Failed to create '{summary}': {resp.status_code} {resp.text[:300]}")
    raise RuntimeError(f"Failed to create '{summary}': gave up after rate limiting")
